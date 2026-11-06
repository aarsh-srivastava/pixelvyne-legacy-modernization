"""Upload extraction: accepted types, ZIP handling, and the safety rules."""

import io
import zipfile

import pytest

from app.ingestion.upload import UploadError, extract_files

MB = 1_048_576


def make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_single_sql_file():
    [f] = extract_files("procs.sql", b"CREATE TABLE t (id NUMBER);", MB)
    assert f.path == "procs.sql"
    assert f.size_bytes == 27
    assert len(f.sha256) == 64


def test_windows_path_in_filename_keeps_only_the_name():
    [f] = extract_files("C:\\legacy\\pkg.pkb", b"x", MB)
    assert f.path == "pkg.pkb"


def test_unsupported_extension_rejected():
    with pytest.raises(UploadError):
        extract_files("notes.docx", b"x", MB)


def test_zip_keeps_sql_and_folders_and_skips_the_rest():
    data = make_zip({
        "legacy/01_schema.sql": b"CREATE TABLE a (x NUMBER);",
        "legacy/pkg/payroll.pkb": b"CREATE OR REPLACE PACKAGE BODY p AS END;",
        "legacy/README.md": b"ignore me",
        "__MACOSX/legacy/._01_schema.sql": b"junk",
        "legacy/.hidden.sql": b"junk",
    })
    paths = sorted(f.path for f in extract_files("upload.zip", data, MB))
    assert paths == ["legacy/01_schema.sql", "legacy/pkg/payroll.pkb"]


def test_zip_path_traversal_rejected():
    data = make_zip({"../../etc/evil.sql": b"x"})
    with pytest.raises(UploadError, match="Unsafe path"):
        extract_files("upload.zip", data, MB)


def test_zip_bomb_capped():
    data = make_zip({"big.sql": b"A" * (3 * MB)})   # compresses to a few KB
    with pytest.raises(UploadError, match="exceed"):
        extract_files("upload.zip", data, 1 * MB)


def test_zip_without_sql_rejected():
    with pytest.raises(UploadError, match="no SQL files"):
        extract_files("upload.zip", make_zip({"a.txt": b"x"}), MB)


def test_corrupt_zip_rejected():
    with pytest.raises(UploadError, match="corrupt"):
        extract_files("upload.zip", b"not a zip", MB)


def test_non_utf8_legacy_script_is_decoded():
    [f] = extract_files("old.sql", "-- café".encode("cp1252"), MB)
    assert "café" in f.content

"""Extract legacy SQL scripts from an upload (single file or ZIP), safely.

Pure functions, no web or DB imports, so they are easy to unit test.

Safety rules (uploads are untrusted):
  * only known SQL/PL-SQL extensions are accepted;
  * ZIP entries with absolute paths or '..' are rejected (path traversal);
  * total extracted size is capped, and entries are read with a hard limit,
    so a ZIP bomb cannot exhaust memory.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

ALLOWED_EXTENSIONS = {
    ".sql", ".pls", ".plsql", ".pks", ".pkb", ".prc", ".fnc", ".trg", ".vw", ".tab", ".seq",
}


class UploadError(ValueError):
    """The upload is not acceptable; message is safe to show the user."""


@dataclass(frozen=True)
class ExtractedFile:
    path: str
    content: str
    size_bytes: int
    sha256: str


def extract_files(filename: str, data: bytes, max_total_bytes: int) -> list[ExtractedFile]:
    name = PurePosixPath(filename.replace("\\", "/")).name
    if not name:
        raise UploadError("File has no name.")

    if name.lower().endswith(".zip"):
        return _extract_zip(data, max_total_bytes)

    _check_extension(name)
    if len(data) > max_total_bytes:
        raise UploadError(f"{name} is larger than the {max_total_bytes // 1_048_576} MB limit.")
    return [_make_file(name, data)]


def _extract_zip(data: bytes, max_total_bytes: int) -> list[ExtractedFile]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise UploadError("The ZIP file is corrupt or not a ZIP.") from exc

    files: list[ExtractedFile] = []
    total = 0
    with archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            path = _safe_member_path(info.filename)
            if path is None:
                continue  # hidden files, __MACOSX, etc.
            if PurePosixPath(path).suffix.lower() not in ALLOWED_EXTENSIONS:
                continue  # README.md, images, ... are ignored, not errors

            remaining = max_total_bytes - total
            with archive.open(info) as member:
                raw = member.read(remaining + 1)
            if len(raw) > remaining:
                raise UploadError(
                    f"ZIP contents exceed the {max_total_bytes // 1_048_576} MB limit."
                )
            total += len(raw)
            files.append(_make_file(path, raw))

    if not files:
        raise UploadError(
            "The ZIP contains no SQL files. Accepted: " + ", ".join(sorted(ALLOWED_EXTENSIONS))
        )
    return files


def _safe_member_path(raw_name: str) -> str | None:
    p = PurePosixPath(raw_name.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts:
        raise UploadError(f"Unsafe path inside ZIP: {raw_name}")
    if any(part.startswith(".") or part == "__MACOSX" for part in p.parts):
        return None
    return str(p)


def _check_extension(name: str) -> None:
    if PurePosixPath(name).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise UploadError(
            f"{name}: unsupported file type. Upload SQL scripts or a ZIP of them "
            f"({', '.join(sorted(ALLOWED_EXTENSIONS))})."
        )


def _make_file(path: str, raw: bytes) -> ExtractedFile:
    return ExtractedFile(
        path=path,
        content=_decode(raw),
        size_bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _decode(raw: bytes) -> str:
    """Legacy scripts are often not UTF-8. Try UTF-8 (with BOM), fall back to Windows-1252."""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")

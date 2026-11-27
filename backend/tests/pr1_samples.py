"""The seven Oracle sample files, for the PR 1 input-safety tests."""

from __future__ import annotations

from pathlib import Path

ORACLE_SAMPLES_DIR = Path(__file__).resolve().parents[2] / "samples" / "oracle"


def oracle_sample_sources() -> dict[str, str]:
    """{file name: text} for samples/oracle/*.sql, in name order."""
    files = sorted(ORACLE_SAMPLES_DIR.glob("*.sql"))
    assert len(files) == 7, f"expected 7 Oracle sample files, found {len(files)}"
    return {f.name: f.read_text(encoding="utf-8") for f in files}

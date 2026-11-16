"""The recorded Oracle corpus for generated tests (step 7c, D-7c.2).

``edge_corpus.json`` holds Oracle's results for the generated cases, recorded on a live
Oracle by ``scripts/run_edge_cases.py --record``. It is never edited by hand. Its header
pins everything the recording depends on; replay refuses a corpus whose header no longer
matches (it *fails closed*: the cases are reported as reference_stale, nothing is judged):

* generator version, seed, budget and the hash of the generated case set;
* SHA-256 of the Oracle sample sources (schema, seed data, objects), line endings
  normalised so a Windows checkout hashes the same as Linux;
* the Oracle version and the recording time (recorded for the record, not compared);
* (format 2) ``results_sha256``, a checksum of the recorded results themselves, so an edit to
  an expected result after recording is refused at replay (Aarsh's review of revision 1).

Values keep their types through JSON: Decimal and datetime are tagged, so a replayed
result compares exactly like a live one.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.contracts.models import ExecutionResult, TestCase
from app.validation.generators import GENERATOR_VERSION, case_set_hash

CORPUS_FORMAT = 2
DEFAULT_CORPUS = Path(__file__).with_name("edge_corpus.json")


def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return {"$decimal": str(value)}
    if isinstance(value, dt.datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, dt.date):
        return {"$date": value.isoformat()}
    if isinstance(value, dict):
        return {str(k): _encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    if isinstance(value, float):
        raise TypeError("a float in a recorded result: Oracle numbers must be fetched as Decimal")
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$decimal"}:
            return Decimal(value["$decimal"])
        if set(value) == {"$datetime"}:
            return dt.datetime.fromisoformat(value["$datetime"])
        if set(value) == {"$date"}:
            return dt.date.fromisoformat(value["$date"])
        return {k: _decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(v) for v in value]
    return value


def source_hashes(samples: Path) -> dict[str, str]:
    """SHA-256 of each Oracle sample file, with CRLF normalised to LF."""
    return {p.name: hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
            for p in sorted(samples.glob("*.sql"))}


def expected_header(cases: list[TestCase], seed: int, budget: int, samples: Path) -> dict[str, Any]:
    return {"format": CORPUS_FORMAT, "generator_version": GENERATOR_VERSION, "seed": seed, "budget": budget,
            "case_set_hash": case_set_hash(cases), "case_count": len(cases), "sources": source_hashes(samples)}


@dataclass
class Corpus:
    header: dict[str, Any]
    results: dict[str, ExecutionResult]

    def stale_reason(self, expected: dict[str, Any]) -> str | None:
        """Why this corpus can't be used for the current cases (None if it can)."""
        for key in ("format", "generator_version", "seed", "budget", "case_set_hash", "sources"):
            if self.header.get(key) != expected.get(key):
                return (f"corpus header {key!r} differs from the current generator and samples; "
                        "re-record it on Oracle (scripts/run_edge_cases.py --record)")
        if self.header.get("results_sha256") != results_digest(self.results):
            return ("the recorded results do not match the corpus checksum (changed after recording); "
                    "re-record it on Oracle (scripts/run_edge_cases.py --record)")
        return None


def _encoded(results: dict[str, ExecutionResult]) -> dict[str, Any]:
    return {k: _encode(results[k].model_dump(exclude={"duration_ms"})) for k in sorted(results)}


def results_digest(results: dict[str, ExecutionResult]) -> str:
    """SHA-256 of the recorded results, in their stored (encoded, sorted) form."""
    text = json.dumps(_encoded(results), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode()).hexdigest()


def save(path: Path, header: dict[str, Any], results: dict[str, ExecutionResult]) -> None:
    """Write the corpus; the header gains the results checksum."""
    body = {"header": dict(header, results_sha256=results_digest(results)), "results": _encoded(results)}
    path.write_text(json.dumps(body, indent=1, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")


def load(path: Path) -> Corpus:
    body = json.loads(path.read_text(encoding="utf-8"))
    results = {k: ExecutionResult.model_validate(_decode(v)) for k, v in body["results"].items()}
    return Corpus(header=body["header"], results=results)

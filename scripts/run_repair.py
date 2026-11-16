"""Run objects through the repair loop: convert, validate on Oracle and PostgreSQL,
send failures back, retry up to 3 times.

Usage, from the repo root on your laptop (stack running):
    python scripts/seed_oracle.py
    python scripts/seed_target.py              # deploys the careless conversions
    python scripts/run_repair.py all           # or one object: calc_bonus
    python scripts/run_repair.py all --json repair_runs.json

The converter here is the SCRIPTED STAND-IN (prepared files in
samples/postgres/repairs/), until Aarsh's converter replaces it. The run changes the
target database; run scripts/seed_target.py again to reset it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.contracts import ObjectType  # noqa: E402
from app.orchestration.repair_loop import RepairDeps, RepairOutcome, run_repair_loop  # noqa: E402
from app.orchestration.scripted_converter import ScriptedConverter  # noqa: E402
from app.parsers.oracle_parser import parse_sources  # noqa: E402
from app.validation.golden_tests import GOLDEN_TESTS, MASKED_COLUMNS, golden_tests_for  # noqa: E402
from app.validation.harness import validate_object  # noqa: E402

ORACLE_USER = os.getenv("ORACLE_USER", "legacy")
ORACLE_PASSWORD = os.getenv("ORACLE_PASSWORD", "legacy")
ORACLE_DSN = os.getenv("ORACLE_DSN", "localhost:1521/FREEPDB1")
TARGET_URL = os.getenv("TARGET_DATABASE_URL_HOST", "postgresql://sandbox:sandbox@localhost:5433/target")
REPAIRS = ROOT / "samples" / "postgres" / "repairs"

OUTCOME_LABEL = {
    RepairOutcome.PASSED: "PASSED",
    RepairOutcome.PASSED_NEEDS_REVIEW: "PASSED, needs sign-off",
    RepairOutcome.NEEDS_HUMAN: "NEEDS HUMAN",
}


def prepared_repairs(objects) -> dict[str, list[Path]]:
    """object_id -> samples/postgres/repairs/<name>.1.sql, <name>.2.sql, ..."""
    return {o.object_id: sorted(REPAIRS.glob(f"{o.name.lower()}.*.sql")) for o in objects}


def describe_source(version: int, prepared: int) -> str:
    if version == 1:
        return "as deployed by seed_target.py"
    if version - 1 <= prepared:
        return f"prepared repair {version - 1}"
    return "no prepared repair left, same code"


def show(run, prepared: int) -> None:
    print(f"\n{run.object_id}")
    for a in run.attempts:
        source = describe_source(a.version, prepared)
        if not a.compiled_ok:
            print(f"  attempt {a.attempt}  ({source})  did not compile: {a.compile_error}")
            continue
        verdict = f"{a.passed} passed, {a.failed} failed" + (f", {a.errors} harness errors" if a.errors else "")
        print(f"  attempt {a.attempt}  ({source})  {verdict}  [{a.status.value}]")
        if a.feedback_summary:
            for line in a.feedback_summary.splitlines()[1:]:
                print(f"      {line}")
    print(f"  -> {OUTCOME_LABEL[run.outcome]}: {run.reason}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("object", help="object name, or 'all' for every object with golden tests")
    parser.add_argument("--json", type=Path, help="also write the runs to this JSON file")
    args = parser.parse_args()

    sources = {p.name: p.read_text(encoding="utf-8") for p in sorted((ROOT / "samples" / "oracle").glob("*.sql"))}
    objects = parse_sources(sources)
    tables = sorted(o.name.lower() for o in objects if o.type == ObjectType.TABLE)
    wanted = args.object.lower()
    targets = [o for o in objects if o.object_id in GOLDEN_TESTS and wanted in ("all", o.name.lower())]
    targets.sort(key=lambda o: list(GOLDEN_TESTS).index(o.object_id))
    if not targets:
        print(f"No object named '{wanted}' with golden tests.")
        return 1

    import oracledb
    import psycopg

    runs = []
    with oracledb.connect(user=ORACLE_USER, password=ORACLE_PASSWORD, dsn=ORACLE_DSN) as ora, \
            psycopg.connect(TARGET_URL) as pg:

        def validate(obj, cases, version):
            return validate_object(ora, pg, obj, cases, conversion_version=version,
                                   watch_tables=tables, masked_columns=MASKED_COLUMNS)

        repairs = prepared_repairs(targets)
        deps = RepairDeps(converter=ScriptedConverter(pg, repairs), validate=validate)
        print("Converter: SCRIPTED STAND-IN (prepared files), not the real converter.")
        for obj in targets:
            run = run_repair_loop(deps, obj, golden_tests_for(obj.object_id))
            show(run, len(repairs[obj.object_id]))
            runs.append(run)

    print("\nSummary")
    for run in runs:
        print(f"  {run.object_id:32} {len(run.attempts)} attempt(s)  {OUTCOME_LABEL[run.outcome]}")
    print("\nThe target database now holds the repaired versions. "
          "Run scripts/seed_target.py to reset it.\n")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps([r.model_dump(mode="json") for r in runs], indent=2), encoding="utf-8")
        print(f"Wrote {args.json}")
    return 0 if all(r.outcome != RepairOutcome.NEEDS_HUMAN for r in runs) else 1


if __name__ == "__main__":
    sys.exit(main())

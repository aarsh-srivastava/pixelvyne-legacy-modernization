"""Run golden tests on Oracle and PostgreSQL and compare them.

Usage, from the repo root on your laptop (stack running, both databases seeded):
    python scripts/seed_oracle.py
    python scripts/seed_target.py
    python scripts/run_validation.py calc_bonus      # one object
    python scripts/run_validation.py all             # every object with golden tests

Signatures come from Aarsh's parser (samples/oracle), tests from
backend/app/validation/golden_tests.py. Every table the parser finds is watched for
side effects. Connection defaults are the docker-compose values as seen from the host;
override them with the same env vars as check_env.py.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.contracts.models import ObjectType, TestStatus  # noqa: E402
from app.parsers.oracle_parser import parse_sources  # noqa: E402
from app.validation.comparator import values_equal  # noqa: E402
from app.validation.golden_tests import (  # noqa: E402
    GOLDEN_TESTS,
    MASKED_COLUMNS,
    ORACLE_EXPECTED,
    golden_tests_for,
)
from app.validation.harness import validate_object  # noqa: E402

ORACLE_USER = os.getenv("ORACLE_USER", "legacy")
ORACLE_PASSWORD = os.getenv("ORACLE_PASSWORD", "legacy")
ORACLE_DSN = os.getenv("ORACLE_DSN", "localhost:1521/FREEPDB1")
TARGET_URL = os.getenv("TARGET_DATABASE_URL_HOST", "postgresql://sandbox:sandbox@localhost:5433/target")

LABEL = {TestStatus.PASSED: "PASS", TestStatus.FAILED: "FAIL", TestStatus.ERROR: "ERROR"}


def show(result) -> str:
    if result.error:
        return f"error {result.error.code}"
    parts = []
    if result.return_value is not None or not result.out_params:
        parts.append("NULL" if result.return_value is None else str(result.return_value))
    parts += [f"{k}={'NULL' if v is None else v}" for k, v in result.out_params.items()]
    changed = [d.table for d in result.table_diffs]
    if changed:
        parts.append("+" + ",".join(changed))
    return ", ".join(parts)


def matches_answer_key(outcome) -> str:
    expected = ORACLE_EXPECTED.get(outcome.test_id)
    if expected is None:
        return "-"
    src = outcome.source
    if "error" in expected:
        return "yes" if src.error and src.error.code == expected["error"] else "NO"
    if src.error:
        return "NO"
    if "return_value" in expected and not values_equal(src.return_value, expected["return_value"]):
        return "NO"
    for name, value in expected.get("out_params", {}).items():
        if not values_equal(src.out_params.get(name), value):
            return "NO"
    if "tables_changed" in expected and {d.table for d in src.table_diffs} != expected["tables_changed"]:
        return "NO"
    return "yes"


def report(result) -> None:
    print(f"\n{result.object_id}  ({len(result.tests)} golden tests)\n")
    print(f"{'test':7} {'oracle':34} {'postgres':34} {'result':7} oracle = EXPECTED.md")
    print("-" * 104)
    for o in result.tests:
        print(f"{o.test_id:7} {show(o.source)[:34]:34} {show(o.target)[:34]:34} "
              f"{LABEL[o.status]:7} {matches_answer_key(o)}")
    print("-" * 104)
    errors = sum(o.status == TestStatus.ERROR for o in result.tests)
    print(f"{result.passed} passed, {result.failed} failed, {errors} harness errors"
          f"  ->  status: {result.status.value}")
    for o in result.tests:
        for d in o.diffs:
            print(f"   {o.test_id}: {d.path} [{d.kind}] oracle={d.source_value} postgres={d.target_value}")


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    wanted = sys.argv[1].lower()

    sources = {p.name: p.read_text(encoding="utf-8")
               for p in sorted((ROOT / "samples" / "oracle").glob("*.sql"))}
    objects = parse_sources(sources)
    tables = sorted(o.name.lower() for o in objects if o.type == ObjectType.TABLE)

    if wanted == "all":
        targets = [o for o in objects if o.object_id in GOLDEN_TESTS]
        targets.sort(key=lambda o: list(GOLDEN_TESTS).index(o.object_id))
    else:
        targets = [o for o in objects if o.name.lower() == wanted]
    if not targets:
        print(f"No object named '{wanted}' with golden tests in samples/oracle.")
        return 1

    import oracledb
    import psycopg

    any_failed = False
    with oracledb.connect(user=ORACLE_USER, password=ORACLE_PASSWORD, dsn=ORACLE_DSN) as ora, \
            psycopg.connect(TARGET_URL) as pg:
        for obj in targets:
            cases = golden_tests_for(obj.object_id)
            if not cases:
                print(f"\nNo golden tests yet for {obj.object_id}.")
                continue
            result = validate_object(ora, pg, obj, cases,
                                     watch_tables=tables, masked_columns=MASKED_COLUMNS)
            report(result)
            any_failed |= result.status.value != "passed"
    print()
    return 1 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())

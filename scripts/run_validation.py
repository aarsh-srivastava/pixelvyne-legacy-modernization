"""Run golden tests on Oracle and PostgreSQL and compare them.

Usage, from the repo root on your laptop (stack running, both databases seeded):
    python scripts/seed_oracle.py
    python scripts/seed_target.py
    python scripts/setup_target_roles.py            # validation connects as pv_runner
    python scripts/run_validation.py calc_bonus      # one object (payroll_pkg = both members)
    python scripts/run_validation.py all             # all 29 answer-key tests, 8 objects

Every Oracle result is also checked against the answer key (EXPECTED.md, i.e.
ORACLE_EXPECTED); the last lines say on how many tests Oracle agrees with it. Oracle is
the source of truth: a disagreement means the answer key needs fixing, not Oracle.

Signatures come from the parser (samples/oracle), tests from
backend/app/validation/golden_tests.py. Every table the parser finds is watched for
side effects. PostgreSQL is used as pv_runner (TARGET_RUNNER_URL); trigger tests run
their UPDATE only as pv_runner. Connection defaults are the docker-compose values as seen
from the host; override them with the same env vars as check_env.py.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.contracts.models import ObjectType, TestStatus  # noqa: E402
from app.parsers.oracle_parser import parse_sources  # noqa: E402
from app.validation.answer_key import answer_key_diffs  # noqa: E402
from app.validation.golden_tests import (  # noqa: E402
    GOLDEN_TESTS,
    MASKED_COLUMNS,
    golden_tests_for,
)
from app.validation.harness import validate_object  # noqa: E402

ORACLE_USER = os.getenv("ORACLE_USER", "legacy")
ORACLE_PASSWORD = os.getenv("ORACLE_PASSWORD", "legacy")
ORACLE_DSN = os.getenv("ORACLE_DSN", "localhost:1521/FREEPDB1")
TARGET_RUNNER_URL = os.getenv("TARGET_RUNNER_URL", "postgresql://pv_runner:pv_runner@localhost:5433/target")
CASES = {c.test_id: c for cases in GOLDEN_TESTS.values() for c in cases}

LABEL = {TestStatus.PASSED: "PASS", TestStatus.FAILED: "FAIL", TestStatus.ERROR: "ERROR"}


def show(result) -> str:
    if result.error:
        return f"error {result.error.code}"
    parts = []
    if result.return_value is not None or not result.out_params:
        parts.append("NULL" if result.return_value is None else str(result.return_value))
    parts += [f"{k}={'NULL' if v is None else v}" for k, v in result.out_params.items()]
    if result.rows is not None:
        parts = [f"{len(result.rows)} rows"]
    changed = [d.table for d in result.table_diffs]
    if changed:
        parts.append("+" + ",".join(changed))
    return ", ".join(parts)


def answer_key_problems(outcome) -> list:
    """How Oracle's own result differs from the answer key ([] = it agrees)."""
    if outcome.source.error and outcome.source.error.code == "HARNESS":
        return [f"not run on Oracle: {outcome.source.error.message}"]
    return [f"{d.path}: key={d.source_value} oracle={d.target_value}"
            for d in answer_key_diffs(CASES[outcome.test_id], outcome.source)]


def matches_answer_key(outcome) -> str:
    return "yes" if not answer_key_problems(outcome) else "NO"


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

    by_id = {o.object_id: o for o in objects}
    targets = [o for o in objects if o.object_id in GOLDEN_TESTS
               and wanted in ("all", o.name.lower(), (o.package or "").lower())]
    targets.sort(key=lambda o: list(GOLDEN_TESTS).index(o.object_id))
    if not targets:
        print(f"No object named '{wanted}' with golden tests in samples/oracle.")
        return 1

    import oracledb
    import psycopg

    any_failed = False
    oracle_vs_key: list = []
    with oracledb.connect(user=ORACLE_USER, password=ORACLE_PASSWORD, dsn=ORACLE_DSN) as ora, \
            psycopg.connect(TARGET_RUNNER_URL) as pg:
        for obj in targets:
            result = validate_object(ora, pg, obj, golden_tests_for(obj.object_id),
                                     watch_tables=tables, masked_columns=MASKED_COLUMNS, objects=by_id)
            report(result)
            any_failed |= result.status.value != "passed"
            oracle_vs_key += [(o.test_id, answer_key_problems(o)) for o in result.tests]

    agree = [t for t, problems in oracle_vs_key if not problems]
    print(f"\nOracle agrees with the answer key (EXPECTED.md) on {len(agree)} of {len(oracle_vs_key)} tests.")
    for test_id, problems in oracle_vs_key:
        for problem in problems:
            print(f"   {test_id}: {problem}")
    print()
    return 1 if any_failed or len(agree) != len(oracle_vs_key) else 0


if __name__ == "__main__":
    sys.exit(main())

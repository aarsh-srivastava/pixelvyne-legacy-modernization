"""Run the golden tests for one object on Oracle and PostgreSQL and compare them.

Usage, from the repo root on your laptop (stack running, both databases seeded):
    python scripts/seed_oracle.py
    python scripts/seed_target.py
    python scripts/run_validation.py calc_bonus

The object's signature comes from Aarsh's parser (samples/oracle), the tests from
backend/app/validation/golden_tests.py. Connection defaults are the docker-compose
values as seen from the host; override them with the same env vars as check_env.py.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.contracts.models import TestStatus  # noqa: E402
from app.parsers.oracle_parser import parse_sources  # noqa: E402
from app.validation.comparator import values_equal  # noqa: E402
from app.validation.golden_tests import ORACLE_EXPECTED, golden_tests_for  # noqa: E402
from app.validation.harness import validate_object  # noqa: E402

ORACLE_USER = os.getenv("ORACLE_USER", "legacy")
ORACLE_PASSWORD = os.getenv("ORACLE_PASSWORD", "legacy")
ORACLE_DSN = os.getenv("ORACLE_DSN", "localhost:1521/FREEPDB1")
TARGET_URL = os.getenv("TARGET_DATABASE_URL_HOST", "postgresql://sandbox:sandbox@localhost:5433/target")


def show(result) -> str:
    if result.error:
        return f"error {result.error.code}"
    if result.return_value is not None:
        return str(result.return_value)
    return ", ".join(f"{k}={v}" for k, v in result.out_params.items()) or "(nothing)"


def matches_answer_key(outcome) -> str:
    expected = ORACLE_EXPECTED.get(outcome.test_id)
    if expected is None:
        return "-"
    src = outcome.source
    if "error" in expected:
        return "yes" if src.error and src.error.code == expected["error"] else "NO"
    if src.error:
        return "NO"
    for name, value in expected.get("out_params", {}).items():
        if not values_equal(src.out_params.get(name), value):
            return "NO"
    return "yes"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    name = sys.argv[1].lower()

    sources = {p.name: p.read_text(encoding="utf-8") for p in sorted((ROOT / "samples" / "oracle").glob("*.sql"))}
    objects = [o for o in parse_sources(sources) if o.name.lower() == name]
    if not objects:
        print(f"No object named '{name}' in samples/oracle.")
        return 1
    obj = objects[0]
    cases = golden_tests_for(obj.object_id)
    if not cases:
        print(f"No golden tests yet for {obj.object_id}.")
        return 1

    import oracledb
    import psycopg

    with oracledb.connect(user=ORACLE_USER, password=ORACLE_PASSWORD, dsn=ORACLE_DSN) as ora, \
            psycopg.connect(TARGET_URL) as pg:
        result = validate_object(ora, pg, obj, cases)

    print(f"\nValidating {obj.object_id}  ({len(cases)} golden tests)\n")
    print(f"{'test':7} {'oracle':22} {'postgres':22} {'result':7} oracle matches EXPECTED.md")
    print("-" * 88)
    for o in result.tests:
        status = "PASS" if o.status == TestStatus.PASSED else "FAIL"
        print(f"{o.test_id:7} {show(o.source):22} {show(o.target):22} {status:7} {matches_answer_key(o)}")
    print("-" * 88)
    print(f"{result.passed} passed, {result.failed} failed  ->  status: {result.status.value}\n")

    for o in result.tests:
        for d in o.diffs:
            print(f"   {o.test_id}: {d.path} differs (oracle={d.source_value}, postgres={d.target_value})")
    return 0 if result.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

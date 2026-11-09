"""Validate one converted object: run every test on both engines and compare.

This is the entry point the LangGraph workflow will call. Connections are passed in,
so the same code runs from a laptop script, a test, or the Celery worker.
"""

from __future__ import annotations

from app.contracts.models import (
    IRObject,
    TestCase,
    TestOutcome,
    TestStatus,
    ValidationResult,
    ValidationStatus,
)
from app.validation.comparator import compare
from app.validation.oracle_runner import run_oracle
from app.validation.postgres_runner import run_postgres


def run_case(oracle_conn, pg_conn, obj: IRObject, case: TestCase) -> TestOutcome:
    source = run_oracle(oracle_conn, obj, case)
    target = run_postgres(pg_conn, obj, case)
    return compare(case, source, target)


def validate_object(
    oracle_conn,
    pg_conn,
    obj: IRObject,
    cases: list[TestCase],
    conversion_version: int = 1,
) -> ValidationResult:
    outcomes = [run_case(oracle_conn, pg_conn, obj, case) for case in cases]
    passed = sum(o.status == TestStatus.PASSED for o in outcomes)
    failed = len(outcomes) - passed
    return ValidationResult(
        object_id=obj.object_id,
        conversion_version=conversion_version,
        tests=outcomes,
        passed=passed,
        failed=failed,
        status=ValidationStatus.PASSED if failed == 0 else ValidationStatus.REQUIRES_REPAIR,
    )

"""Validate one converted object: run every test on both engines and compare.

This is the entry point the LangGraph workflow will call. Connections are passed in,
so the same code runs from a laptop script, a test, or the Celery worker.

ValidationResult.status:
  passed           every test passed
  requires_repair  at least one behavioural failure: send FailureReport to the converter
  needs_human      no behavioural failure, but the harness could not judge some tests
`failed` counts behavioural failures only; harness errors show as TestStatus.ERROR.
"""

from __future__ import annotations

from typing import Iterable

from app.contracts.models import (
    ExecutionError,
    ExecutionResult,
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


def _guarded(engine: str, runner, conn, obj, case, watch_tables) -> ExecutionResult:
    """A runner that crashes (unsupported object, bad connection) is a harness error."""
    try:
        return runner(conn, obj, case, watch_tables)
    except Exception as exc:  # noqa: BLE001 - anything here means "could not judge"
        return ExecutionResult(
            engine=engine,
            test_id=case.test_id,
            error=ExecutionError(code="HARNESS", message=f"{type(exc).__name__}: {exc}"),
        )


def run_case(
    oracle_conn,
    pg_conn,
    obj: IRObject,
    case: TestCase,
    watch_tables: Iterable[str] = (),
    masked_columns: dict[str, set[str]] | None = None,
) -> TestOutcome:
    tables = list(watch_tables)
    source = _guarded("oracle", run_oracle, oracle_conn, obj, case, tables)
    target = _guarded("postgres", run_postgres, pg_conn, obj, case, tables)
    return compare(case, source, target, masked_columns=masked_columns)


def validate_object(
    oracle_conn,
    pg_conn,
    obj: IRObject,
    cases: list[TestCase],
    conversion_version: int = 1,
    watch_tables: Iterable[str] = (),
    masked_columns: dict[str, set[str]] | None = None,
) -> ValidationResult:
    tables = list(watch_tables)
    outcomes = [run_case(oracle_conn, pg_conn, obj, c, tables, masked_columns) for c in cases]
    passed = sum(o.status == TestStatus.PASSED for o in outcomes)
    failed = sum(o.status == TestStatus.FAILED for o in outcomes)
    errors = sum(o.status == TestStatus.ERROR for o in outcomes)
    if failed:
        status = ValidationStatus.REQUIRES_REPAIR
    elif errors:
        status = ValidationStatus.NEEDS_HUMAN
    else:
        status = ValidationStatus.PASSED
    return ValidationResult(
        object_id=obj.object_id,
        conversion_version=conversion_version,
        tests=outcomes,
        passed=passed,
        failed=failed,
        status=status,
    )

"""Judge one result against Oracle's recorded answer (ORACLE_EXPECTED, from EXPECTED.md).

Two uses, one set of rules:

* **Is the answer key right?** ``scripts/run_validation.py`` checks every live Oracle 23
  result against it. Real Oracle is the source of truth: a disagreement means the answer
  key is fixed, never the result.
* **Without Oracle at hand** (tests, CI, the sandbox), a PostgreSQL result is judged against
  the recorded Oracle answer instead of a live one, with the same comparison rules as
  ``comparator.compare``: numbers by value, errors by category, rows in ``order_by`` order,
  masked columns left out, and every difference a ``FieldDiff`` the repair loop can use.

A recorded answer only states what EXPECTED.md states: e.g. ``tables_changed`` names the
tables a test changes without spelling out every updated row, and ``inserted`` lists the
new rows of the tables where the rows themselves are the point (payroll runs, audit rows).
"""

from __future__ import annotations

from typing import Any

from app.contracts.models import (
    ExecutionError,
    ExecutionResult,
    FieldDiff,
    TestCase,
    TestOutcome,
    TestStatus,
)
from app.validation.comparator import _compare_row_sets, _compare_rows, _compare_value  # same rules
from app.validation.errors import categorize, is_harness_error
from app.validation.golden_tests import MASKED_COLUMNS, ORACLE_EXPECTED


def recorded_result(case: TestCase) -> ExecutionResult:
    """Oracle's recorded answer for ``case`` as an ExecutionResult (what EXPECTED.md states)."""
    expected = ORACLE_EXPECTED[case.test_id]
    return ExecutionResult(
        engine="oracle",
        test_id=case.test_id,
        return_value=expected.get("return_value"),
        out_params=dict(expected.get("out_params", {})),
        rows=expected.get("rows"),
        error=ExecutionError(code=expected["error"], message="recorded") if "error" in expected else None,
    )


def answer_key_diffs(case: TestCase, result: ExecutionResult,
                     masked_columns: dict[str, set[str]] | None = None) -> list[FieldDiff]:
    """Every way ``result`` differs from Oracle's recorded answer; [] if it matches."""
    expected = ORACLE_EXPECTED[case.test_id]
    masked = MASKED_COLUMNS if masked_columns is None else masked_columns
    diffs: list[FieldDiff] = []
    got_error = categorize(result.engine, result.error)
    if "error" in expected:
        want = categorize("oracle", ExecutionError(code=expected["error"], message=""))
        if want != got_error:
            diffs.append(FieldDiff(path="error", source_value=want, target_value=got_error, kind="error"))
        return diffs
    if result.error:
        diffs.append(FieldDiff(path="error", source_value=None, target_value=got_error, kind="error"))
        return diffs
    if "return_value" in expected:
        _compare_value("return_value", expected["return_value"], result.return_value, diffs)
    for name, value in expected.get("out_params", {}).items():
        _compare_value(f"out_params.{name}", value, result.out_params.get(name), diffs)
    if "rows" in expected:
        _compare_rows(expected["rows"], result.rows, bool(case.order_by), diffs)
    changed = {d.table for d in result.table_diffs if d.inserted or d.deleted or d.updated}
    if "tables_changed" in expected and changed != expected["tables_changed"]:
        diffs.append(FieldDiff(path="table_diffs", source_value=sorted(expected["tables_changed"]),
                               target_value=sorted(changed)))
    for table, rows in expected.get("inserted", {}).items():
        got: list[dict[str, Any]] = [r for d in result.table_diffs if d.table == table for r in d.inserted]
        _compare_row_sets(f"table_diffs.{table}.inserted", rows, got, set(masked.get(table, ())), diffs)
    return diffs


def judge(case: TestCase, result: ExecutionResult,
          masked_columns: dict[str, set[str]] | None = None) -> TestOutcome:
    """A TestOutcome for ``result`` judged against the recorded Oracle answer (harness errors: ERROR)."""
    if is_harness_error(result.engine, result.error):
        diffs = [FieldDiff(path=f"harness.{'source' if result.engine == 'oracle' else 'target'}",
                           target_value=result.error.message, kind="error")]
        return TestOutcome(test_id=case.test_id, status=TestStatus.ERROR, source=recorded_result(case),
                           target=result, diffs=diffs)
    diffs = answer_key_diffs(case, result, masked_columns)
    return TestOutcome(test_id=case.test_id, status=TestStatus.FAILED if diffs else TestStatus.PASSED,
                       source=recorded_result(case), target=result, diffs=diffs)

"""Decide whether the converted object behaved like the original, for one test.

Rules:
* Numbers compare by value: Oracle 24000 equals PostgreSQL 24000.00.
* Errors: if both engines raised, the behaviour matches (the codes differ by engine:
  ORA-01403 vs P0002). If only one raised, that is a failure.
* Every difference becomes a FieldDiff with a path, so the repair step can tell the
  converter exactly what went wrong.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.contracts.models import ExecutionResult, FieldDiff, TestCase, TestOutcome, TestStatus

_MISSING = object()


def values_equal(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float, Decimal)) and isinstance(b, (int, float, Decimal)):
        if isinstance(a, bool) or isinstance(b, bool):
            return a == b
        return Decimal(str(a)) == Decimal(str(b))
    return a == b


def _compare_value(path: str, src: Any, tgt: Any, diffs: list[FieldDiff]) -> None:
    if src is _MISSING:
        diffs.append(FieldDiff(path=path, target_value=tgt, kind="extra"))
    elif tgt is _MISSING:
        diffs.append(FieldDiff(path=path, source_value=src, kind="missing"))
    elif not values_equal(src, tgt):
        diffs.append(FieldDiff(path=path, source_value=src, target_value=tgt, kind="value"))


def compare(case: TestCase, source: ExecutionResult, target: ExecutionResult) -> TestOutcome:
    diffs: list[FieldDiff] = []

    if source.error or target.error:
        if not (source.error and target.error):
            diffs.append(FieldDiff(
                path="error",
                source_value=source.error.code if source.error else None,
                target_value=target.error.code if target.error else None,
                kind="error",
            ))
    else:
        _compare_value("return_value", source.return_value, target.return_value, diffs)
        for name in sorted(set(source.out_params) | set(target.out_params)):
            _compare_value(
                f"out_params.{name}",
                source.out_params.get(name, _MISSING),
                target.out_params.get(name, _MISSING),
                diffs,
            )
        if source.rows is not None or target.rows is not None:
            src_rows, tgt_rows = source.rows or [], target.rows or []
            if len(src_rows) != len(tgt_rows):
                diffs.append(FieldDiff(path="rows.length", source_value=len(src_rows),
                                       target_value=len(tgt_rows)))
            for i, (s, t) in enumerate(zip(src_rows, tgt_rows)):
                for col in sorted(set(s) | set(t)):
                    _compare_value(f"rows[{i}].{col}", s.get(col, _MISSING),
                                   t.get(col, _MISSING), diffs)

    return TestOutcome(
        test_id=case.test_id,
        status=TestStatus.FAILED if diffs else TestStatus.PASSED,
        source=source,
        target=target,
        diffs=diffs,
    )

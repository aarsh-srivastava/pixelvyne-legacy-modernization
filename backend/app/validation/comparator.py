"""Decide whether the converted object behaved like the original, for one test.

Rules:
* Numbers compare by value: Oracle 24000 equals PostgreSQL 24000.00.
* Errors compare by category (errors.py): ORA-01403 matches P0002 (both "no data
  found"), but not a unique violation. Only one side raising is a failure.
* A harness error (object missing, connection lost, unsupported object) on either side
  makes the outcome ERROR: the test could not be judged, which is not the same as the
  conversion being wrong.
* Rows and table side effects compare as sets by default: database row order is not
  guaranteed without ORDER BY. Pass ordered_rows=True when order is part of the contract.
* Masked columns (sequence ids, timestamps) are left out of table comparisons.
* Every difference becomes a FieldDiff with a path, so the repair step can tell the
  converter exactly what went wrong.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import Any

from app.contracts.models import (
    ExecutionResult,
    FieldDiff,
    TableDiff,
    TestCase,
    TestOutcome,
    TestStatus,
)
from app.validation.errors import categorize, is_harness_error
from app.validation.table_diff import canon, canon_row, strip

_MISSING = object()


def values_equal(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float, Decimal)) and isinstance(b, (int, float, Decimal)):
        if isinstance(a, bool) or isinstance(b, bool):
            return a == b
        return Decimal(str(a)) == Decimal(str(b))
    return canon(a) == canon(b)


def _compare_value(path: str, src: Any, tgt: Any, diffs: list[FieldDiff]) -> None:
    if src is _MISSING:
        diffs.append(FieldDiff(path=path, target_value=tgt, kind="extra"))
    elif tgt is _MISSING:
        diffs.append(FieldDiff(path=path, source_value=src, kind="missing"))
    elif not values_equal(src, tgt):
        diffs.append(FieldDiff(path=path, source_value=src, target_value=tgt, kind="value"))


def _compare_row_sets(path: str, src_rows: list[dict], tgt_rows: list[dict],
                      masked: set[str], diffs: list[FieldDiff]) -> None:
    """Compare two lists of rows as multisets; report rows only one side has."""
    src_c = Counter(canon_row(r, masked) for r in src_rows)
    tgt_c = Counter(canon_row(r, masked) for r in tgt_rows)
    first = {}
    for r in [*src_rows, *tgt_rows]:
        first.setdefault(canon_row(r, masked), strip(r, masked))
    for k in sorted((src_c - tgt_c).elements(), key=str):
        diffs.append(FieldDiff(path=path, source_value=first[k], kind="missing"))
    for k in sorted((tgt_c - src_c).elements(), key=str):
        diffs.append(FieldDiff(path=path, target_value=first[k], kind="extra"))


def _compare_rows(source_rows, target_rows, ordered: bool, diffs: list[FieldDiff]) -> None:
    src_rows, tgt_rows = source_rows or [], target_rows or []
    if not ordered:
        _compare_row_sets("rows", src_rows, tgt_rows, set(), diffs)
        return
    if len(src_rows) != len(tgt_rows):
        diffs.append(FieldDiff(path="rows.length", source_value=len(src_rows),
                               target_value=len(tgt_rows)))
    for i, (s, t) in enumerate(zip(src_rows, tgt_rows)):
        for col in sorted(set(s) | set(t)):
            _compare_value(f"rows[{i}].{col}", s.get(col, _MISSING), t.get(col, _MISSING), diffs)


def _changed(update: dict, masked: set[str]) -> dict:
    """Key columns plus only the columns whose value changed, for readable diffs."""
    before, after = update["before"], update["after"]
    changed = {c: v for c, v in after.items()
               if c not in masked and canon(before.get(c)) != canon(v)}
    return {**update["key"], **changed}


def _compare_table_diffs(source: list[TableDiff], target: list[TableDiff],
                         masked_columns: dict[str, set[str]], diffs: list[FieldDiff]) -> None:
    src = {d.table.lower(): d for d in source}
    tgt = {d.table.lower(): d for d in target}
    for table in sorted(src.keys() | tgt.keys()):
        s = src.get(table) or TableDiff(table=table)
        t = tgt.get(table) or TableDiff(table=table)
        masked = set(masked_columns.get(table, ()))
        base = f"table_diffs.{table}"
        _compare_row_sets(f"{base}.inserted", s.inserted, t.inserted, masked, diffs)
        _compare_row_sets(f"{base}.deleted", s.deleted, t.deleted, masked, diffs)

        def by_key(updates):
            return {canon_row(u["key"]): u for u in updates}

        s_up, t_up = by_key(s.updated), by_key(t.updated)
        for k in sorted(s_up.keys() | t_up.keys(), key=str):
            key_label = ",".join(f"{c}={v}" for c, v in k)
            path = f"{base}.updated[{key_label}]"
            if k not in t_up:
                diffs.append(FieldDiff(path=path, source_value=_changed(s_up[k], masked), kind="missing"))
            elif k not in s_up:
                diffs.append(FieldDiff(path=path, target_value=_changed(t_up[k], masked), kind="extra"))
            else:
                s_after, t_after = s_up[k]["after"], t_up[k]["after"]
                for col in sorted((set(s_after) | set(t_after)) - masked):
                    _compare_value(f"{path}.{col}", s_after.get(col, _MISSING),
                                   t_after.get(col, _MISSING), diffs)


def compare(
    case: TestCase,
    source: ExecutionResult,
    target: ExecutionResult,
    *,
    ordered_rows: bool = False,
    masked_columns: dict[str, set[str]] | None = None,
) -> TestOutcome:
    diffs: list[FieldDiff] = []
    status = TestStatus.PASSED

    if is_harness_error("oracle", source.error) or is_harness_error("postgres", target.error):
        status = TestStatus.ERROR
        for side, res in (("source", source), ("target", target)):
            if res.error and is_harness_error(res.engine, res.error):
                diffs.append(FieldDiff(
                    path=f"harness.{side}",
                    source_value=res.error.message if side == "source" else None,
                    target_value=res.error.message if side == "target" else None,
                    kind="error",
                ))
    elif source.error or target.error:
        src_cat, tgt_cat = categorize("oracle", source.error), categorize("postgres", target.error)
        if src_cat != tgt_cat:
            diffs.append(FieldDiff(path="error", source_value=src_cat, target_value=tgt_cat, kind="error"))
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
            _compare_rows(source.rows, target.rows, ordered_rows, diffs)
        _compare_table_diffs(source.table_diffs, target.table_diffs, masked_columns or {}, diffs)

    if status != TestStatus.ERROR and diffs:
        status = TestStatus.FAILED
    return TestOutcome(test_id=case.test_id, status=status, source=source, target=target, diffs=diffs)

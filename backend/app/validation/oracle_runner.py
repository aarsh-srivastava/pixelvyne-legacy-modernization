"""Run one TestCase against the legacy Oracle object and capture what it did.

Every call runs inside a transaction that is rolled back, so tests never change the
seed data and can run in any order. Watched tables are snapshotted before and after
the call (same transaction), so side effects, including trigger effects, are captured
as TableDiffs. Sequences are not transactional in Oracle, which is why sequence-fed
columns are masked when comparing (see golden_tests.MASKED_COLUMNS).

How an object is exercised (the same rules as postgres_runner):

* standalone and package procedures and functions with scalar parameters: called;
* views (step 7b): ``SELECT * FROM view ORDER BY <case.order_by>``; the rows are the result;
* triggers (step 7b): ``case.action``, one trusted INSERT/UPDATE/DELETE (actions.py), fires it;
* ``case.setup_steps()`` run first, in the same transaction and before the snapshot.
"""

from __future__ import annotations

import datetime as dt
import time
from decimal import Decimal
from typing import Any, Iterable, Mapping

import oracledb

from app.contracts.models import (
    ExecutionError,
    ExecutionResult,
    IRObject,
    ObjectType,
    ParamMode,
    TableDiff,
    TestCase,
)
from app.validation.actions import checked_action
from app.validation.table_diff import check_identifier, diff_snapshots

# Return Oracle NUMBERs as Decimal, never float, so 4250.03 stays exactly 4250.03.
oracledb.defaults.fetch_decimals = True

PK_SQL = """
    SELECT cc.column_name
      FROM user_constraints c
      JOIN user_cons_columns cc ON cc.constraint_name = c.constraint_name
     WHERE c.table_name = :t AND c.constraint_type = 'P'
     ORDER BY cc.position
"""


def _python_type(oracle_type: str | None) -> type:
    t = (oracle_type or "").upper()
    if t.startswith(("NUMBER", "INTEGER", "INT", "DECIMAL", "NUMERIC", "FLOAT", "PLS_INTEGER", "BINARY_")):
        return Decimal
    if t.startswith(("DATE", "TIMESTAMP")):
        return dt.datetime
    return str


def _routine_name(obj: IRObject) -> str:
    return f"{obj.package}.{obj.name}" if obj.package else obj.name


def _snapshot(cur, table: str) -> tuple[list[str], list[dict]]:
    name = check_identifier(table).upper()
    cur.execute(PK_SQL, t=name)
    key_columns = [r[0].lower() for r in cur.fetchall()]
    cur.execute(f"SELECT * FROM {name}")
    cols = [d[0].lower() for d in cur.description]
    return key_columns, [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


def _call(cur, obj: IRObject, inputs: dict) -> tuple[Any, dict]:
    """Call a function or procedure; (return value, OUT parameters)."""
    out_vars: dict[str, oracledb.Var] = {}
    args = []
    for p in obj.params:
        name = p.name.lower()
        if p.mode == ParamMode.IN:
            args.append(inputs.get(name))
            continue
        var = cur.var(_python_type(p.oracle_type))
        if p.mode == ParamMode.IN_OUT:
            var.setvalue(0, inputs.get(name))
        out_vars[name] = var
        args.append(var)
    if obj.type == ObjectType.FUNCTION:
        return_value = cur.callfunc(_routine_name(obj), _python_type(obj.return_type), args)
    elif obj.type == ObjectType.PROCEDURE:
        return_value = None
        cur.callproc(_routine_name(obj), args)
    else:
        raise NotImplementedError(f"Oracle runner cannot call a {obj.type.value}")
    return return_value, {k: v.getvalue() for k, v in out_vars.items()}


def _query_view(cur, obj: IRObject, order_by: list[str]) -> list[dict]:
    query = f"SELECT * FROM {check_identifier(obj.name)}"
    if order_by:
        query += " ORDER BY " + ", ".join(check_identifier(c) for c in order_by)
    cur.execute(query)
    cols = [d[0].lower() for d in cur.description]
    return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


def _setup_object(object_id: str, objects: Mapping[str, IRObject] | None) -> IRObject:
    if not objects or object_id not in objects:
        raise LookupError(f"setup object {object_id} was not given to the runner")
    return objects[object_id]


def run_oracle(
    conn: oracledb.Connection,
    obj: IRObject,
    case: TestCase,
    watch_tables: Iterable[str] = (),
    objects: Mapping[str, IRObject] | None = None,
) -> ExecutionResult:
    started = time.perf_counter()
    cur = conn.cursor()
    return_value = None
    out_params: dict = {}
    rows: list[dict] | None = None
    error = None
    table_diffs: list[TableDiff] = []
    setup = case.setup_steps()
    setup_objects = [_setup_object(step.object_id, objects) for step in setup]  # missing: harness error

    try:
        for step, target in zip(setup, setup_objects, strict=True):
            _call(cur, target, step.inputs)
        before = {t: _snapshot(cur, t) for t in watch_tables}

        if obj.type in (ObjectType.FUNCTION, ObjectType.PROCEDURE):
            return_value, out_params = _call(cur, obj, case.inputs)
        elif obj.type == ObjectType.VIEW:
            rows = _query_view(cur, obj, case.order_by)
        elif obj.type == ObjectType.TRIGGER:
            cur.execute(checked_action(case))
        else:
            raise NotImplementedError(f"Oracle runner cannot execute a {obj.type.value} yet")

        for table, (keys, rows_before) in before.items():
            diff = diff_snapshots(table.lower(), keys, rows_before, _snapshot(cur, table)[1])
            if diff:
                table_diffs.append(diff)

    except oracledb.DatabaseError as exc:
        (err,) = exc.args
        code = getattr(err, "full_code", None)
        message = getattr(err, "message", str(exc)).strip().splitlines()[0]
        error = ExecutionError(code=code, message=message)
        out_params, table_diffs, rows = {}, [], None
    finally:
        conn.rollback()

    return ExecutionResult(
        engine="oracle",
        test_id=case.test_id,
        return_value=return_value,
        out_params=out_params,
        rows=rows,
        table_diffs=table_diffs,
        error=error,
        duration_ms=round((time.perf_counter() - started) * 1000, 2),
    )

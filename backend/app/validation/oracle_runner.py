"""Run one TestCase against the legacy Oracle object and capture what it did.

Every call runs inside a transaction that is rolled back, so tests never change the
seed data and can run in any order. Watched tables are snapshotted before and after
the call (same transaction), so side effects, including trigger effects, are captured
as TableDiffs. Sequences are not transactional in Oracle, which is why sequence-fed
columns are masked when comparing (see golden_tests.MASKED_COLUMNS).

Supported today: standalone and package procedures and functions with scalar
parameters.
"""

from __future__ import annotations

import datetime as dt
import time
from decimal import Decimal
from typing import Iterable

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
    return key_columns, [dict(zip(cols, row)) for row in cur.fetchall()]


def run_oracle(
    conn: oracledb.Connection,
    obj: IRObject,
    case: TestCase,
    watch_tables: Iterable[str] = (),
) -> ExecutionResult:
    started = time.perf_counter()
    cur = conn.cursor()
    out_vars: dict[str, oracledb.Var] = {}
    return_value = None
    error = None
    table_diffs: list[TableDiff] = []

    try:
        before = {t: _snapshot(cur, t) for t in watch_tables}

        args = []
        for p in obj.params:
            name = p.name.lower()
            if p.mode == ParamMode.IN:
                args.append(case.inputs.get(name))
                continue
            var = cur.var(_python_type(p.oracle_type))
            if p.mode == ParamMode.IN_OUT:
                var.setvalue(0, case.inputs.get(name))
            out_vars[name] = var
            args.append(var)

        if obj.type == ObjectType.FUNCTION:
            return_value = cur.callfunc(_routine_name(obj), _python_type(obj.return_type), args)
        elif obj.type == ObjectType.PROCEDURE:
            cur.callproc(_routine_name(obj), args)
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
        table_diffs = []
    finally:
        conn.rollback()

    return ExecutionResult(
        engine="oracle",
        test_id=case.test_id,
        return_value=return_value,
        out_params={} if error else {k: v.getvalue() for k, v in out_vars.items()},
        table_diffs=table_diffs,
        error=error,
        duration_ms=round((time.perf_counter() - started) * 1000, 2),
    )

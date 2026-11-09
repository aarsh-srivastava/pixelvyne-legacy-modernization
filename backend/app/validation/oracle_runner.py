"""Run one TestCase against the legacy Oracle object and capture what it did.

Every call runs inside a transaction that is rolled back, so tests never change the
seed data and can run in any order.

Supported today: standalone and package procedures and functions with scalar
parameters. Table diffs (for objects that write data) come next.
"""

from __future__ import annotations

import datetime as dt
import time
from decimal import Decimal

import oracledb

from app.contracts.models import (
    ExecutionError,
    ExecutionResult,
    IRObject,
    ObjectType,
    ParamMode,
    TestCase,
)

# Return Oracle NUMBERs as Decimal, never float, so 4250.03 stays exactly 4250.03.
oracledb.defaults.fetch_decimals = True


def _python_type(oracle_type: str | None) -> type:
    t = (oracle_type or "").upper()
    if t.startswith(("NUMBER", "INTEGER", "INT", "DECIMAL", "NUMERIC", "FLOAT", "PLS_INTEGER", "BINARY_")):
        return Decimal
    if t.startswith(("DATE", "TIMESTAMP")):
        return dt.datetime
    return str


def _routine_name(obj: IRObject) -> str:
    return f"{obj.package}.{obj.name}" if obj.package else obj.name


def run_oracle(conn: oracledb.Connection, obj: IRObject, case: TestCase) -> ExecutionResult:
    started = time.perf_counter()
    cur = conn.cursor()
    out_vars: dict[str, oracledb.Var] = {}
    return_value = None
    error = None

    try:
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

    except oracledb.DatabaseError as exc:
        (err,) = exc.args
        code = getattr(err, "full_code", None)
        message = getattr(err, "message", str(exc)).strip().splitlines()[0]
        error = ExecutionError(code=code, message=message)
    finally:
        conn.rollback()

    return ExecutionResult(
        engine="oracle",
        test_id=case.test_id,
        return_value=return_value,
        out_params={} if error else {k: v.getvalue() for k, v in out_vars.items()},
        error=error,
        duration_ms=round((time.perf_counter() - started) * 1000, 2),
    )

"""Run one TestCase against the converted PostgreSQL object in target-db.

Mirrors oracle_runner: same TestCase in, same ExecutionResult shape out, rolled back
afterwards. Only ever connect this to target-db: converted code is untrusted.

A converted procedure is called with CALL; its OUT parameters come back as one row.
A package member is called as <package>.<name>, the schema Ora2Pg puts it in.
"""

from __future__ import annotations

import time

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from app.contracts.models import (
    ExecutionError,
    ExecutionResult,
    IRObject,
    ObjectType,
    ParamMode,
    TestCase,
)


def _routine_name(obj: IRObject) -> sql.Composable:
    # Lower-case: unquoted names fold to lower case in PostgreSQL, and the contract
    # says IR names are lower-case even if a parser hands us upper case.
    name = sql.Identifier(obj.name.lower())
    if obj.package:
        return sql.SQL("{}.{}").format(sql.Identifier(obj.package.lower()), name)
    return name


def run_postgres(conn: psycopg.Connection, obj: IRObject, case: TestCase) -> ExecutionResult:
    started = time.perf_counter()
    return_value = None
    out_params: dict = {}
    error = None

    placeholders: list[sql.Composable] = []
    values: list = []
    out_names: list[str] = []
    for p in obj.params:
        name = p.name.lower()
        if p.mode == ParamMode.OUT:
            placeholders.append(sql.SQL("NULL"))
        else:
            placeholders.append(sql.Placeholder())
            values.append(case.inputs.get(name))
        if p.mode in (ParamMode.OUT, ParamMode.IN_OUT):
            out_names.append(name)
    arg_list = sql.SQL(", ").join(placeholders)

    try:
        with conn.cursor(row_factory=dict_row) as cur:
            if obj.type == ObjectType.PROCEDURE:
                cur.execute(sql.SQL("CALL {}({})").format(_routine_name(obj), arg_list), values)
                row = cur.fetchone() if out_names else None
                out_params = {n: row[n] for n in out_names} if row else {}
            elif obj.type == ObjectType.FUNCTION:
                cur.execute(
                    sql.SQL("SELECT {}({}) AS return_value").format(_routine_name(obj), arg_list),
                    values,
                )
                return_value = cur.fetchone()["return_value"]
            else:
                raise NotImplementedError(f"Postgres runner cannot execute a {obj.type.value} yet")

    except psycopg.Error as exc:
        message = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
        error = ExecutionError(code=exc.sqlstate, message=message)
    finally:
        conn.rollback()

    return ExecutionResult(
        engine="postgres",
        test_id=case.test_id,
        return_value=return_value,
        out_params=out_params,
        error=error,
        duration_ms=round((time.perf_counter() - started) * 1000, 2),
    )

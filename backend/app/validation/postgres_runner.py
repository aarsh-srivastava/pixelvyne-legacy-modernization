"""Run one TestCase against the converted PostgreSQL object in target-db.

Mirrors oracle_runner: same TestCase in, same ExecutionResult shape out, watched tables
snapshotted before and after, rolled back afterwards. Only ever connect this to
target-db: converted code is untrusted.

How an object is exercised (step 7b adds views, triggers and setup calls):

* procedure: ``CALL``; its OUT parameters come back as one row. Function: ``SELECT f(...)``.
  A package member is called as <package>.<name>, the schema it is deployed into.
* view: ``SELECT * FROM view ORDER BY <case.order_by>``; the rows are the result.
* trigger: ``case.action`` (one trusted INSERT/UPDATE/DELETE, see actions.py) is run, and
  only as ``pv_runner``; what the trigger did shows up in the watched tables.
* ``case.setup_steps()`` run first, in the same transaction, before the snapshot, so only
  what the test itself does is compared. Setup objects are looked up in ``objects``.
* ``case.fixture_rows`` (step 7c) are inserted before the setup, only after the checks in
  fixtures.py and only as pv_runner, with every value bound as a parameter.
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Mapping

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from app.contracts.models import (
    ExecutionError,
    ExecutionResult,
    IRObject,
    ObjectType,
    ParamMode,
    TableDiff,
    TestCase,
)
from app.migration.target_roles import RUNNER_ROLE
from app.validation.actions import checked_action
from app.validation.fixtures import checked_fixtures
from app.validation.table_diff import check_identifier, diff_snapshots

PK_SQL = """
    SELECT a.attname
      FROM pg_index i
      JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)
     WHERE i.indrelid = %s::regclass AND i.indisprimary
     ORDER BY array_position(i.indkey::int2[], a.attnum)
"""


def _routine_name(obj: IRObject) -> sql.Composable:
    # Lower-case: unquoted names fold to lower case in PostgreSQL, and the contract
    # says IR names are lower-case even if a parser hands us upper case.
    name = sql.Identifier(obj.name.lower())
    if obj.package:
        return sql.SQL("{}.{}").format(sql.Identifier(obj.package.lower()), name)
    return name


def _setup_object(object_id: str, objects: Mapping[str, IRObject] | None) -> IRObject:
    if not objects or object_id not in objects:
        raise LookupError(f"setup object {object_id} was not given to the runner")
    return objects[object_id]


def _snapshot(cur, table: str) -> tuple[list[str], list[dict]]:
    name = check_identifier(table).lower()
    cur.execute(PK_SQL, [name])
    key_columns = [r["attname"] for r in cur.fetchall()]
    cur.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(name)))
    return key_columns, [dict(r) for r in cur.fetchall()]


def _call(cur, obj: IRObject, inputs: dict) -> tuple[Any, dict]:
    """Call a function or procedure; (return value, OUT parameters)."""
    placeholders: list[sql.Composable] = []
    values: list = []
    out_names: list[str] = []
    for p in obj.params:
        name = p.name.lower()
        if p.mode == ParamMode.OUT:
            placeholders.append(sql.SQL("NULL"))
        else:
            placeholders.append(sql.Placeholder())
            values.append(inputs.get(name))
        if p.mode in (ParamMode.OUT, ParamMode.IN_OUT):
            out_names.append(name)
    arg_list = sql.SQL(", ").join(placeholders)
    if obj.type == ObjectType.PROCEDURE:
        cur.execute(sql.SQL("CALL {}({})").format(_routine_name(obj), arg_list), values)
        row = cur.fetchone() if out_names else None
        return None, ({n: row[n] for n in out_names} if row else {})
    if obj.type == ObjectType.FUNCTION:
        cur.execute(sql.SQL("SELECT {}({}) AS return_value").format(_routine_name(obj), arg_list), values)
        return cur.fetchone()["return_value"], {}
    raise NotImplementedError(f"Postgres runner cannot call a {obj.type.value}")


def _query_view(cur, obj: IRObject, order_by: list[str]) -> list[dict]:
    query = sql.SQL("SELECT * FROM {}").format(sql.Identifier(check_identifier(obj.name).lower()))
    if order_by:
        query += sql.SQL(" ORDER BY {}").format(
            sql.SQL(", ").join(sql.Identifier(check_identifier(c).lower()) for c in order_by))
    cur.execute(query)
    return [{k.lower(): v for k, v in row.items()} for row in cur.fetchall()]


def _require_runner(cur, what: str) -> None:
    cur.execute("SELECT current_user AS who")
    who = cur.fetchone()["who"]
    if who != RUNNER_ROLE:
        raise PermissionError(f"{what} run only as {RUNNER_ROLE}, not as {who}")


def _fire(cur, case: TestCase) -> None:
    """Run the trigger test's action, only after every check and only as pv_runner."""
    statement = checked_action(case)
    _require_runner(cur, "trigger actions")
    cur.execute(statement)


def _insert_fixtures(cur, inserts: list[tuple[str, list[str], list]]) -> None:
    """Insert checked fixture rows (fixtures.py), only as pv_runner; values are bound."""
    if not inserts:
        return
    _require_runner(cur, "fixture rows")
    for table, columns, values in inserts:
        cur.execute(sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(table), sql.SQL(", ").join(sql.Identifier(c) for c in columns),
            sql.SQL(", ").join(sql.Placeholder() for _ in columns)), values)


def _context(exc: psycopg.Error) -> str | None:
    """PL/pgSQL's context (where in which function it was raised), bounded."""
    diag = getattr(exc, "diag", None)
    context = getattr(diag, "context", None) if diag is not None else None
    return context.strip()[:1000] if context else None


def run_postgres(
    conn: psycopg.Connection,
    obj: IRObject,
    case: TestCase,
    watch_tables: Iterable[str] = (),
    objects: Mapping[str, IRObject] | None = None,
) -> ExecutionResult:
    started = time.perf_counter()
    return_value = None
    out_params: dict = {}
    rows: list[dict] | None = None
    error = None
    table_diffs: list[TableDiff] = []
    setup = case.setup_steps()
    setup_objects = [_setup_object(step.object_id, objects) for step in setup]  # missing: harness error
    inserts = checked_fixtures(case)  # refused before any SQL: harness error

    try:
        with conn.cursor(row_factory=dict_row) as cur:
            _insert_fixtures(cur, inserts)
            for step, target in zip(setup, setup_objects, strict=True):
                _call(cur, target, step.inputs)
            before = {t: _snapshot(cur, t) for t in watch_tables}

            if obj.type in (ObjectType.PROCEDURE, ObjectType.FUNCTION):
                return_value, out_params = _call(cur, obj, case.inputs)
            elif obj.type == ObjectType.VIEW:
                rows = _query_view(cur, obj, case.order_by)
            elif obj.type == ObjectType.TRIGGER:
                _fire(cur, case)
            else:
                raise NotImplementedError(f"Postgres runner cannot execute a {obj.type.value}")

            for table, (keys, rows_before) in before.items():
                diff = diff_snapshots(table.lower(), keys, rows_before, _snapshot(cur, table)[1])
                if diff:
                    table_diffs.append(diff)

    except psycopg.Error as exc:
        message = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
        error = ExecutionError(code=exc.sqlstate, message=message, context=_context(exc))
        out_params, table_diffs, rows = {}, [], None
    finally:
        conn.rollback()

    return ExecutionResult(
        engine="postgres",
        test_id=case.test_id,
        return_value=return_value,
        out_params=out_params,
        rows=rows,
        table_diffs=table_diffs,
        error=error,
        duration_ms=round((time.perf_counter() - started) * 1000, 2),
    )

"""The two target-sandbox roles are genuinely restricted (real PostgreSQL).

Skipped when no PostgreSQL superuser is reachable (see target_pg.py).
"""

from __future__ import annotations

import pytest

from app.migration.target_roles import DEPLOYER_ROLE, RUNNER_ROLE, ensure_roles, role_report
from tests.target_pg import (  # noqa: F401  (fixtures)
    DEPLOYER_PASSWORD,
    RUNNER_PASSWORD,
    admin,
    deployer_conn,
    runner_conn,
    scratch_db,
)

psycopg = pytest.importorskip("psycopg")


def assert_denied(conn, statement: str) -> None:
    """The statement must fail with insufficient privilege (42501)."""
    conn.rollback()
    conn.autocommit = statement.upper().startswith("CREATE DATABASE")  # can't run in a transaction
    try:
        with pytest.raises(psycopg.Error) as info:
            conn.execute(statement)
    finally:
        conn.rollback()
        conn.autocommit = False
    assert info.value.sqlstate == "42501", f"{statement!r} failed with {info.value.sqlstate}: {info.value}"


# --- role attributes --------------------------------------------------------------

@pytest.mark.parametrize("role", [DEPLOYER_ROLE, RUNNER_ROLE])
def test_role_has_no_dangerous_attributes_or_memberships(admin, role):
    facts = role_report(admin, role)
    assert facts == {
        "superuser": False, "createdb": False, "createrole": False, "replication": False,
        "bypassrls": False, "inherit": False, "member_of": [],
    }


@pytest.mark.parametrize("role", [DEPLOYER_ROLE, RUNNER_ROLE])
def test_role_has_statement_timeout(scratch_db, role):
    from tests.target_pg import connect_as

    with connect_as(scratch_db, role) as conn:
        assert conn.execute("SHOW statement_timeout").fetchone()[0] == "30s"


def test_ensure_roles_tightens_a_loosened_role_again(admin):
    admin.execute(f"ALTER ROLE {RUNNER_ROLE} SUPERUSER CREATEDB")
    admin.execute(f"GRANT pg_read_server_files TO {RUNNER_ROLE}")
    admin.autocommit = False
    ensure_roles(admin, deployer_password=DEPLOYER_PASSWORD, runner_password=RUNNER_PASSWORD)
    facts = role_report(admin, RUNNER_ROLE)
    assert facts["superuser"] is False and facts["createdb"] is False
    assert facts["member_of"] == []


def test_ensure_roles_refuses_a_non_superuser(deployer_conn):
    with pytest.raises(PermissionError):
        ensure_roles(deployer_conn, deployer_password="x", runner_password="y")


# --- pv_runner: runs converted code, changes rows, nothing else ----------------------

def test_runner_can_read_and_write_rows_and_use_sequences(runner_conn):
    runner_conn.execute("UPDATE employees SET base_salary = base_salary + 1 WHERE emp_id = 101")
    runner_conn.execute("INSERT INTO salary_audit VALUES (nextval('salary_audit_seq'), 101, 1, 2)")
    runner_conn.execute("DELETE FROM salary_audit")
    assert runner_conn.execute("SELECT count(*) FROM employees").fetchone()[0] == 2
    runner_conn.rollback()


@pytest.mark.parametrize("statement", [
    "CREATE TABLE t (x int)",
    "CREATE FUNCTION f() RETURNS int LANGUAGE sql AS 'SELECT 1'",
    "CREATE SCHEMA s",
    "CREATE TEMP TABLE t (x int)",
    "DROP TABLE employees",
    "TRUNCATE employees",
    "ALTER TABLE employees ADD COLUMN x int",
    "CREATE TRIGGER t AFTER UPDATE ON employees FOR EACH ROW EXECUTE FUNCTION suppress_redundant_updates_trigger()",
    "SELECT pg_read_file('/etc/passwd')",
    "COPY employees TO PROGRAM 'id'",
    "SET ROLE sandbox",
    "SELECT * FROM pg_authid",
])
def test_runner_cannot(runner_conn, statement):
    assert_denied(runner_conn, statement)


# --- pv_deployer: creates and owns converted objects, cannot touch the data ---------

def test_deployer_can_create_routines_views_schemas_and_triggers(deployer_conn):
    deployer_conn.execute("CREATE FUNCTION public.answer() RETURNS int LANGUAGE sql AS 'SELECT 42'")
    deployer_conn.execute("CREATE VIEW public.emp_names AS SELECT full_name FROM employees")
    deployer_conn.execute("CREATE SCHEMA payroll_pkg")
    deployer_conn.execute(
        "CREATE FUNCTION public.noop_trg() RETURNS trigger LANGUAGE plpgsql AS 'BEGIN RETURN NULL; END'"
    )
    deployer_conn.execute(
        "CREATE TRIGGER noop AFTER UPDATE ON employees FOR EACH ROW EXECUTE FUNCTION public.noop_trg()"
    )
    deployer_conn.rollback()


@pytest.mark.parametrize("statement", [
    "INSERT INTO employees VALUES (999, 'x', 1)",
    "UPDATE employees SET base_salary = 0",
    "DELETE FROM employees",
    "DROP TABLE employees",
    "TRUNCATE employees",
    "ALTER TABLE employees ADD COLUMN x int",
    "SELECT nextval('salary_audit_seq')",
    "SELECT pg_read_file('/etc/passwd')",
    "CREATE DATABASE other",
    "SET ROLE sandbox",
])
def test_deployer_cannot(deployer_conn, statement):
    assert_denied(deployer_conn, statement)


# --- the split works together ------------------------------------------------------

def test_runner_can_call_what_the_deployer_created(scratch_db, deployer_conn, runner_conn):
    deployer_conn.execute("CREATE FUNCTION public.double_it(x numeric) RETURNS numeric LANGUAGE sql AS 'SELECT x * 2'")
    deployer_conn.execute("CREATE VIEW public.top_paid AS SELECT emp_id FROM employees WHERE base_salary > 100000")
    deployer_conn.commit()
    try:
        assert runner_conn.execute("SELECT public.double_it(21)").fetchone()[0] == 42
        assert runner_conn.execute("SELECT emp_id FROM public.top_paid").fetchone()[0] == 101
        runner_conn.rollback()
    finally:
        deployer_conn.execute("DROP VIEW public.top_paid")
        deployer_conn.execute("DROP FUNCTION public.double_it(numeric)")
        deployer_conn.commit()


def test_only_the_runner_may_execute_deployed_routines(admin, deployer_conn):
    deployer_conn.execute("CREATE FUNCTION public.private_fn() RETURNS int LANGUAGE sql AS 'SELECT 1'")
    deployer_conn.commit()
    try:
        granted = admin.execute(
            "SELECT array_agg(grantee::regrole::text ORDER BY 1) FROM "
            "(SELECT (aclexplode(proacl)).grantee FROM pg_proc WHERE proname = 'private_fn') a"
        ).fetchone()[0]
        assert "-" not in (granted or []), "PUBLIC must not have EXECUTE"
        assert RUNNER_ROLE in granted
    finally:
        deployer_conn.execute("DROP FUNCTION public.private_fn()")
        deployer_conn.commit()


def test_harmful_body_fails_at_run_time_even_if_it_was_deployed(deployer_conn, runner_conn, admin):
    """Least privilege is the real guard: a body that slipped past every check still can't drop data."""
    deployer_conn.execute(
        "CREATE FUNCTION public.evil() RETURNS void LANGUAGE plpgsql AS "
        "$$BEGIN EXECUTE 'DROP TABLE employees'; END$$"
    )
    deployer_conn.commit()
    try:
        assert_denied(runner_conn, "SELECT public.evil()")
        assert admin.execute("SELECT to_regclass('public.employees') IS NOT NULL").fetchone()[0]
    finally:
        deployer_conn.execute("DROP FUNCTION public.evil()")
        deployer_conn.commit()

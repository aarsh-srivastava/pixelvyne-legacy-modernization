"""PostgreSQL deployer: safety checks (no database) and real deployments.

The database tests use a throwaway database and the restricted roles from
target_pg.py, and are skipped when no PostgreSQL superuser is reachable.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest

from app.migration.postgres_deployer import (
    DeploymentRefused,
    DeployTarget,
    PostgresDeployer,
    plan_deployment,
    split_statements,
)
from tests.target_pg import (  # noqa: F401  (fixtures)
    admin,
    connect_as,
    deployer_conn,
    runner_conn,
    scratch_db,
)

ROOT = Path(__file__).resolve().parents[2]

FN = DeployTarget("function", "get_bonus_rate")
PROC = DeployTarget("procedure", "calc_bonus")
PKG_FN = DeployTarget("function", "net_pay", schema="payroll_pkg")
VIEW = DeployTarget("view", "org_chart")
TRG = DeployTarget(
    "trigger", "emp_salary_audit_trg", trigger_table="employees", trigger_function="emp_salary_audit_trg_fn"
)

GOOD_FN = """
CREATE OR REPLACE FUNCTION get_bonus_rate(p_rating varchar) RETURNS numeric
LANGUAGE plpgsql AS $$
BEGIN
  -- a comment that says DROP TABLE employees; must not count
  RAISE NOTICE 'not a real DROP or COMMIT: %', p_rating;
  RETURN CASE p_rating WHEN 'A' THEN 0.20 WHEN 'B' THEN 0.10 ELSE 0.05 END;
END;
$$;
"""

GOOD_PROC = """
CREATE OR REPLACE PROCEDURE calc_bonus(p_emp_id numeric, p_override varchar, OUT p_bonus numeric)
LANGUAGE plpgsql AS $$
DECLARE
  v_salary employees.base_salary%TYPE;
BEGIN
  SELECT base_salary INTO STRICT v_salary FROM employees WHERE emp_id = p_emp_id;
  p_bonus := ROUND(v_salary * 0.10, 2);
END;
$$;
"""

GOOD_TRIGGER = """
CREATE OR REPLACE FUNCTION emp_salary_audit_trg_fn() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO salary_audit (audit_id, emp_id, old_salary, new_salary)
  VALUES (nextval('salary_audit_seq'), NEW.emp_id, OLD.base_salary, NEW.base_salary);
  RETURN NULL;
END;
$$;

CREATE TRIGGER emp_salary_audit_trg
AFTER UPDATE OF base_salary ON employees
FOR EACH ROW
WHEN (NEW.base_salary <> OLD.base_salary)
EXECUTE FUNCTION emp_salary_audit_trg_fn();
"""

GOOD_PKG_FN = """
CREATE SCHEMA IF NOT EXISTS payroll_pkg;
CREATE OR REPLACE FUNCTION payroll_pkg.net_pay(p_gross numeric) RETURNS numeric
LANGUAGE sql AS $$ SELECT ROUND(COALESCE(p_gross, 0) * (1 - 0.10), 2) $$;
"""


def refusal(code: str, target: DeployTarget) -> str:
    with pytest.raises(DeploymentRefused) as info:
        plan_deployment(code, target)
    return str(info.value)


# --- splitting --------------------------------------------------------------------

def test_split_ignores_semicolons_in_bodies_strings_and_comments():
    code = (
        "CREATE FUNCTION f() RETURNS text LANGUAGE plpgsql AS $fn$ BEGIN RETURN 'a;b'; END $fn$;"
        " -- trailing comment; with semicolon\n"
        "/* block; /* nested; */ comment */ CREATE VIEW v AS SELECT 'x;y' AS \"odd;name\";"
    )
    statements = split_statements(code)
    assert len(statements) == 2
    assert statements[0].text.startswith("CREATE FUNCTION f()")
    assert statements[1].outline.startswith("CREATE VIEW v AS SELECT")


def test_split_handles_nested_dollar_tags_and_positional_params():
    code = "CREATE FUNCTION f(int) RETURNS int LANGUAGE sql AS $outer$ SELECT $1 + length($$x;y$$) $outer$;"
    (statement,) = split_statements(code)
    assert "$1" in statement.bodies[0] and "SELECT" in statement.bodies[0]


def test_split_handles_escape_strings():
    (statement,) = split_statements(r"CREATE VIEW v AS SELECT E'it\'s; fine' AS x")
    assert statement.outline.endswith("'?' AS x")
    assert "fine" not in statement.outline


@pytest.mark.parametrize("code", ["SELECT 'open", 'SELECT "open', "SELECT $$ open", "/* open"])
def test_unterminated_code_is_refused(code):
    assert "unterminated" in refusal(code, FN)


# --- what is allowed --------------------------------------------------------------

@pytest.mark.parametrize("code, target", [
    (GOOD_FN, FN), (GOOD_PROC, PROC), (GOOD_TRIGGER, TRG), (GOOD_PKG_FN, PKG_FN),
    ("CREATE OR REPLACE VIEW org_chart AS SELECT emp_id, full_name FROM employees", VIEW),
])
def test_valid_code_is_planned(code, target):
    plan = plan_deployment(code, target)
    assert plan.statements


def test_triggers_are_replaced_in_place_not_dropped():
    """DROP TRIGGER needs table ownership, which the deployer must not have."""
    plan = plan_deployment(GOOD_TRIGGER, TRG)
    assert plan.drops == []
    assert [s.split()[:4] for s in plan.statements] == [
        ["CREATE", "OR", "REPLACE", "FUNCTION"], ["CREATE", "OR", "REPLACE", "TRIGGER"],
    ]


def test_routines_and_views_are_dropped_first_so_signatures_can_change():
    for code, target, verb in ((GOOD_FN, FN, "ROUTINE"), ("CREATE VIEW org_chart AS SELECT 1 AS x", VIEW, "VIEW")):
        (drop,) = plan_deployment(code, target).drops
        assert drop.as_string(None).startswith(f"DROP {verb} IF EXISTS")
        assert "CASCADE" not in drop.as_string(None)


def test_package_member_grants_its_schema_to_the_runner():
    plan = plan_deployment(GOOD_PKG_FN, PKG_FN)
    assert plan.grant_usage_on == ["payroll_pkg"]


def _target_for(statement) -> DeployTarget | None:
    outline = statement.outline
    if m := re.match(r"CREATE (?:OR REPLACE )?(FUNCTION|PROCEDURE) (\w+)", outline, re.I):
        return DeployTarget(m.group(1).lower(), m.group(2).lower())
    return None


@pytest.mark.parametrize("path", sorted(
    p for p in (ROOT / "samples" / "postgres").rglob("*.sql") if not p.name.startswith(("01", "02"))
), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_every_hand_written_conversion_in_the_repo_is_accepted(path):
    """The stand-in and its repairs are what real conversions look like; none may be refused."""
    code = path.read_text()
    if "CREATE TRIGGER" in code.upper():
        assert plan_deployment(code, TRG).statements
        return
    for statement in split_statements(code):
        target = _target_for(statement)
        assert target, statement.outline[:60]
        assert plan_deployment(statement.text, target).statements


# --- what is refused ------------------------------------------------------------------

@pytest.mark.parametrize("extra, reason", [
    ("DROP TABLE employees", "not allowed"),
    ("TRUNCATE employees", "not allowed"),
    ("DELETE FROM employees", "not allowed"),
    ("DO $$ BEGIN PERFORM 1; END $$", "not allowed"),
    ("GRANT ALL ON employees TO PUBLIC", "not allowed"),
    ("CREATE EXTENSION dblink", "not allowed"),
    ("COPY employees TO '/tmp/x'", "not allowed"),
    ("ALTER SYSTEM SET work_mem = '1GB'", "not allowed"),
    ("CREATE INDEX CONCURRENTLY i ON employees (emp_id)", "not allowed"),
    ("CREATE SCHEMA evil", "not allowed"),
    ("SET ROLE sandbox", "not allowed"),
    ("SELECT pg_read_file('/etc/passwd')", "pg_read_file"),
])
def test_any_other_top_level_statement_is_refused(extra, reason):
    assert reason in refusal(GOOD_FN + "\n" + extra + ";", FN)


@pytest.mark.parametrize("code, reason", [
    (GOOD_FN.replace("get_bonus_rate", "other_fn"), "expected public.get_bonus_rate"),
    (GOOD_PROC, "creates a procedure; expected a function"),
    (GOOD_FN.replace("get_bonus_rate", "payroll_pkg.get_bonus_rate"), "expected public.get_bonus_rate"),
])
def test_wrong_object_is_refused(code, reason):
    assert reason in refusal(code, FN)


def test_unqualified_package_member_is_refused():
    code = GOOD_PKG_FN.replace("payroll_pkg.net_pay", "net_pay")
    assert "expected payroll_pkg.net_pay" in refusal(code, PKG_FN)


def test_other_package_schema_is_refused():
    assert "only 'payroll_pkg'" in refusal("CREATE SCHEMA IF NOT EXISTS hr_pkg;" + GOOD_PKG_FN, PKG_FN)


def test_trigger_on_the_wrong_table_or_function_is_refused():
    assert "expected employees" in refusal(GOOD_TRIGGER.replace("ON employees", "ON salary_audit"), TRG)
    other = GOOD_TRIGGER.replace("EXECUTE FUNCTION emp_salary_audit_trg_fn()", "EXECUTE FUNCTION other_fn()")
    assert "executes public.other_fn" in refusal(other, TRG)


def test_trigger_without_its_function_is_refused():
    trigger_only = GOOD_TRIGGER[GOOD_TRIGGER.index("CREATE TRIGGER"):]
    assert "does not create trigger function" in refusal(trigger_only, TRG)


def test_code_that_creates_nothing_expected_is_refused():
    assert "does not create" in refusal("CREATE SCHEMA IF NOT EXISTS payroll_pkg;", PKG_FN)
    assert "no code" in refusal("   ", FN)


def test_creating_the_object_twice_is_refused():
    assert "more than once" in refusal(GOOD_FN + GOOD_FN, FN)


@pytest.mark.parametrize("change, reason", [
    (("LANGUAGE plpgsql", "LANGUAGE plpython3u"), "LANGUAGE plpython3u"),
    (("LANGUAGE plpgsql", "LANGUAGE c"), "LANGUAGE c"),
    (("LANGUAGE plpgsql", "LANGUAGE plpgsql SECURITY DEFINER"), "SECURITY DEFINER"),
    (("LANGUAGE plpgsql", "LANGUAGE plpgsql SET search_path = pg_temp"), "configuration parameter"),
    (("LANGUAGE plpgsql AS", "AS"), "no LANGUAGE"),
])
def test_unsafe_routine_options_are_refused(change, reason):
    assert reason in refusal(GOOD_FN.replace(*change), FN)


@pytest.mark.parametrize("statement, reason", [
    ("DROP TABLE employees", "DROP"),
    ("TRUNCATE salary_audit", "TRUNCATE"),
    ("ALTER TABLE employees ADD COLUMN x int", "ALTER"),
    ("CREATE TEMP TABLE t (x int)", "CREATE"),
    ("GRANT SELECT ON employees TO PUBLIC", "GRANT"),
    ("COPY employees TO '/tmp/out'", "COPY"),
    ("EXECUTE 'DR' || 'OP TABLE employees'", "dynamic SQL"),
    ("COMMIT", "transaction control (COMMIT)"),
    ("ROLLBACK", "transaction control (ROLLBACK)"),
    ("UPDATE employees e SET base_salary = 0", "UPDATE without WHERE"),
    ("UPDATE ONLY public.employees AS e SET base_salary = 0", "UPDATE without WHERE"),
    ("DELETE FROM salary_audit sa", "DELETE without WHERE"),
    ("SET ROLE sandbox", "SET ROLE"),
    ("SET LOCAL search_path = evil", "SET SEARCH_PATH"),
    ("RESET ROLE", "RESET ROLE"),
    ("LOCK TABLE employees", "LOCK TABLE"),
    ("PERFORM pg_read_file('/etc/passwd')", "pg_read_file"),
    ("PERFORM dblink_exec('host=x', 'DELETE FROM t')", "dblink_exec"),
    ("PERFORM set_config('role', 'sandbox', true)", "set_config"),
    ("PERFORM lo_import('/etc/passwd')", "lo_import"),
    ("PERFORM pg_terminate_backend(1)", "pg_terminate_backend"),
    ("DELETE FROM salary_audit", "DELETE without WHERE"),
    ("UPDATE employees SET base_salary = 0", "UPDATE without WHERE"),
])
def test_dangerous_operations_inside_bodies_are_refused(statement, reason):
    code = GOOD_FN.replace("RETURN CASE", f"{statement};\n  RETURN CASE")
    assert reason in refusal(code, FN)


def test_dangerous_words_in_strings_and_comments_are_fine():
    plan_deployment(GOOD_FN, FN)  # GOOD_FN mentions DROP and COMMIT in a comment and a message


def test_filtered_dml_and_on_conflict_are_fine():
    body = (
        "UPDATE employees SET base_salary = base_salary * 1.1 WHERE emp_id = 101;\n"
        "  UPDATE employees e SET base_salary = 1 WHERE e.emp_id = 102;\n"
        "  DELETE FROM salary_audit WHERE emp_id = 101;\n"
        "  DELETE FROM salary_audit sa USING employees e WHERE sa.emp_id = e.emp_id;\n"
        "  DELETE FROM salary_audit WHERE CURRENT OF c;\n"
        "  INSERT INTO salary_audit VALUES (1, 1, 1, 1) ON CONFLICT (audit_id) DO UPDATE SET new_salary = 2;\n"
        "  RETURN CASE"
    )
    plan_deployment(GOOD_FN.replace("RETURN CASE", body), FN)


def test_dangerous_call_in_a_view_is_refused():
    code = "CREATE VIEW org_chart AS SELECT pg_read_file('/etc/passwd') AS f"
    assert "pg_read_file" in refusal(code, VIEW)


def test_target_validation():
    with pytest.raises(ValueError):
        DeployTarget("table", "employees")
    with pytest.raises(ValueError):
        DeployTarget("function", "Net_Pay")
    with pytest.raises(ValueError):
        DeployTarget("trigger", "t")


# --- real deployments (PostgreSQL) -----------------------------------------------------

@pytest.fixture
def deployer(scratch_db, deployer_conn):
    return PostgresDeployer(deployer_conn, database=scratch_db)


def _value(conn, query):
    value = conn.execute(query).fetchone()[0]
    conn.rollback()
    return value


def test_function_deploys_and_the_runner_can_call_it(deployer, runner_conn):
    result = deployer.deploy(GOOD_FN, FN)
    assert result.ok, result.error
    assert deployer.conn.info.transaction_status.name == "IDLE"
    assert _value(runner_conn, "SELECT get_bonus_rate('A')") == Decimal("0.20")


def test_procedure_with_out_parameter_deploys(deployer, runner_conn):
    assert deployer.deploy(GOOD_PROC, PROC).ok
    assert _value(runner_conn, "CALL calc_bonus(101, NULL, NULL)") == 12000


def test_package_member_deploys_into_its_schema(deployer, runner_conn):
    result = deployer.deploy(GOOD_PKG_FN, PKG_FN)
    assert result.ok, result.error
    assert _value(runner_conn, "SELECT payroll_pkg.net_pay(1000)") == 900


def test_view_deploys(deployer, runner_conn):
    code = "CREATE OR REPLACE VIEW org_chart AS SELECT emp_id, full_name FROM employees"
    assert deployer.deploy(code, VIEW).ok
    assert _value(runner_conn, "SELECT count(*) FROM org_chart") == 2


def test_trigger_deploys_fires_for_the_runner_and_redeploys(deployer, runner_conn):
    assert deployer.deploy(GOOD_TRIGGER, TRG).ok
    runner_conn.execute("UPDATE employees SET base_salary = base_salary + 1 WHERE emp_id = 101")
    assert runner_conn.execute("SELECT count(*) FROM salary_audit").fetchone()[0] == 1
    runner_conn.rollback()
    again = deployer.deploy(GOOD_TRIGGER, TRG)
    assert again.ok, again.error


def test_syntax_error_rolls_back_and_keeps_the_previous_version(deployer, runner_conn):
    assert deployer.deploy(GOOD_FN, FN).ok
    broken = GOOD_FN.replace("RETURN CASE", "RETURN CASE CASE")
    result = deployer.deploy(broken, FN)
    assert not result.ok and not result.refused
    assert result.sqlstate == "42601"
    assert deployer.conn.info.transaction_status.name == "IDLE"
    assert _value(runner_conn, "SELECT get_bonus_rate('B')") == Decimal("0.10")


def test_missing_dependency_is_reported(deployer):
    result = deployer.deploy("CREATE VIEW org_chart AS SELECT * FROM no_such_table", VIEW)
    assert not result.ok
    assert result.sqlstate == "42P01" and "no_such_table" in result.error


def test_signature_change_replaces_the_old_routine(deployer, runner_conn):
    assert deployer.deploy(GOOD_FN, FN).ok
    new = """
    CREATE OR REPLACE FUNCTION get_bonus_rate(p_rating varchar, p_default numeric) RETURNS text
    LANGUAGE sql AS $$ SELECT COALESCE(p_rating, 'none') || ':' || p_default $$;
    """
    result = deployer.deploy(new, FN)
    assert result.ok, result.error
    assert _value(runner_conn, "SELECT get_bonus_rate(NULL, 5)") == "none:5"
    assert _value(runner_conn, "SELECT count(*) FROM pg_proc WHERE proname = 'get_bonus_rate'") == 1


def test_function_a_trigger_depends_on_is_not_dropped(deployer, scratch_db):
    assert deployer.deploy(GOOD_TRIGGER, TRG).ok
    as_function = DeployTarget("function", "emp_salary_audit_trg_fn")
    code = GOOD_TRIGGER[: GOOD_TRIGGER.index("CREATE TRIGGER")]
    result = deployer.deploy(code, as_function)
    assert not result.ok and result.sqlstate == "2BP01"  # dependent objects still exist


def test_refused_code_runs_nothing(deployer, admin):
    result = deployer.deploy(GOOD_FN + "\nDROP TABLE employees;", FN)
    assert result.refused and "not allowed" in result.error
    assert admin.execute("SELECT to_regclass('public.employees') IS NOT NULL").fetchone()[0]


def test_refuses_a_superuser_connection(scratch_db, admin):
    admin.autocommit = False
    result = PostgresDeployer(admin, database=scratch_db, role=None).deploy(GOOD_FN, FN)
    assert result.refused and "superuser" in result.error
    assert admin.info.transaction_status.name == "IDLE"


def test_refuses_the_wrong_database_or_role(scratch_db, deployer_conn, runner_conn):
    wrong_db = PostgresDeployer(deployer_conn, database="target").deploy(GOOD_FN, FN)
    assert wrong_db.refused and "expected 'target'" in wrong_db.error
    wrong_role = PostgresDeployer(runner_conn, database=scratch_db).deploy(GOOD_FN, FN)
    assert wrong_role.refused and "expected 'pv_deployer'" in wrong_role.error


def test_refuses_when_a_transaction_is_already_open(deployer):
    deployer.conn.execute("SELECT 1")
    result = deployer.deploy(GOOD_FN, FN)
    assert result.refused and "open transaction" in result.error
    deployer.conn.rollback()


def test_errors_do_not_leak_connection_details(deployer):
    result = deployer.deploy("CREATE VIEW org_chart AS SELECT * FROM no_such_table", VIEW)
    for secret in ("password", "pv_deployer@", "host="):
        assert secret not in result.error


def test_runner_cannot_call_a_routine_from_another_role(scratch_db, admin, runner_conn):
    """Only deployer-created routines get EXECUTE for the runner; anything else stays closed."""
    admin.execute("CREATE FUNCTION public.admin_only() RETURNS int LANGUAGE sql AS 'SELECT 1'")
    admin.execute("REVOKE EXECUTE ON FUNCTION public.admin_only() FROM PUBLIC")
    try:
        with pytest.raises(Exception) as info:
            runner_conn.execute("SELECT public.admin_only()")
        assert getattr(info.value, "sqlstate", None) == "42501"
    finally:
        runner_conn.rollback()
        admin.execute("DROP FUNCTION public.admin_only()")

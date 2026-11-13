"""PL/SQL translator (Stage 2, step 4).

The unit tests need no database. The proof at the end needs PostgreSQL: it builds the
sandbox from the hand-written tables and seed data (D8, phase 1), translates every
sample routine and the trigger from the Oracle source, deploys them as pv_deployer,
runs them as pv_runner, and compares with Oracle's recorded answers: the 19 golden
tests (ORACLE_EXPECTED) and the answer key in samples/oracle/EXPECTED.md for
monthly_payroll (mp-01), payroll_pkg (pk-01..05) and the trigger (tr-01..03).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from app.contracts.models import ExecutionError
from app.migration.postgres_deployer import PostgresDeployer, plan_deployment
from app.migration.plsql_translator import (
    ConversionContext,
    build_contexts,
    column_types_from_ddl,
    translate_plsql,
    translate_type,
)
from app.migration.target_roles import DEPLOYER_ROLE, RUNNER_ROLE, ensure_roles
from app.parsers.dependency_analyzer import analyze_dependencies
from app.parsers.oracle_parser import parse_sources
from app.validation.comparator import values_equal
from app.validation.errors import categorize
from app.validation.golden_tests import GOLDEN_TESTS, ORACLE_EXPECTED
from app.validation.postgres_runner import run_postgres

ROOT = Path(__file__).resolve().parents[2]
ROUTINES = ("function", "procedure", "trigger")


@pytest.fixture(scope="module")
def sample():
    sources = {p.name: p.read_text() for p in sorted((ROOT / "samples" / "oracle").glob("*.sql"))}
    objects = {o.object_id: o for o in parse_sources(sources)}
    return objects, build_contexts(objects.values())


def translate(sample, object_id):
    objects, contexts = sample
    obj = objects[object_id]
    return translate_plsql(obj.source_code, kind=obj.type.value, name=obj.name, context=contexts[object_id])


# --- types ---------------------------------------------------------------------------

@pytest.mark.parametrize("oracle, pg, category", [
    ("NUMBER", "numeric", "number"),
    ("NUMBER(10,2)", "numeric(10, 2)", "number"),
    ("NUMBER(6)", "numeric(6)", "number"),
    ("INTEGER", "numeric", "number"),          # R4: never integer division
    ("PLS_INTEGER", "numeric", "number"),
    ("VARCHAR2(10)", "varchar(10)", "text"),
    ("VARCHAR2(10 CHAR)", "varchar(10)", "text"),
    ("VARCHAR2", "varchar", "text"),
    ("CHAR(1)", "char(1)", "text"),
    ("CLOB", "text", "text"),
    ("DATE", "timestamp(0)", "date"),          # Oracle DATE keeps the time
    ("TIMESTAMP(6)", "timestamp(6)", "date"),
    ("TIMESTAMP WITH TIME ZONE", "timestamptz", "date"),
    ("BOOLEAN", "boolean", "bool"),
    ("BLOB", "bytea", "binary"),
])
def test_translate_type(oracle, pg, category):
    assert translate_type(oracle) == (pg, category)


def test_type_anchors_use_column_types():
    columns = {"employees.hire_date": "DATE", "employees.base_salary": "NUMBER(10,2)"}
    assert translate_type("employees.hire_date%TYPE", columns) == ("employees.hire_date%TYPE", "date")
    assert translate_type("Employees.Base_Salary%TYPE", columns) == ("employees.base_salary%TYPE", "number")
    with pytest.raises(ValueError):
        translate_type("XMLTYPE")


def test_column_types_from_the_sample_ddl():
    columns = column_types_from_ddl([(ROOT / "samples" / "oracle" / "01_schema.sql").read_text()])
    assert columns["employees.hire_date"] == "DATE"
    assert columns["employees.base_salary"] == "NUMBER(10, 2)"
    assert columns["payroll_runs.pay_month"] == "DATE"


# --- every sample object ----------------------------------------------------------------

SAMPLE_ROUTINES = [
    "function:legacy.get_emp_grade", "function:legacy.top_earners", "procedure:legacy.calc_bonus",
    "procedure:legacy.apply_raise", "procedure:legacy.monthly_payroll", "function:legacy.payroll_pkg.net_pay",
    "procedure:legacy.payroll_pkg.close_month", "trigger:legacy.emp_salary_audit_trg",
]


def test_every_sample_routine_is_covered(sample):
    objects, _ = sample
    assert {k for k, o in objects.items() if o.type.value in ROUTINES} == set(SAMPLE_ROUTINES)


@pytest.mark.parametrize("object_id", SAMPLE_ROUTINES)
def test_sample_routine_translates_and_passes_the_deployer_checks(sample, object_id):
    result = translate(sample, object_id)
    assert result.successful, result.unsupported
    plan_deployment(result.code, result.target)  # raises if the deployer would refuse it
    assert "#variable_conflict use_column" in result.code


def test_select_into_is_strict(sample):
    code = translate(sample, "function:legacy.get_emp_grade").code
    assert "INTO STRICT v_salary, v_rating FROM employees" in code
    assert "WHEN no_data_found THEN" in code


def test_text_parameters_are_normalised_before_declarations(sample):
    code = translate(sample, "procedure:legacy.calc_bonus").code
    normalise, declare = code.index("p_override := NULLIF(p_override, '');"), code.index("DECLARE")
    assert normalise < declare  # declarations would otherwise see '' instead of NULL


def test_integer_parameter_becomes_numeric_and_division_is_exact(sample):
    code = translate(sample, "procedure:legacy.apply_raise").code
    assert "p_pct numeric" in code
    assert "CAST(p_pct AS DECIMAL) / 100" in code and "DOUBLE" not in code


def test_package_members_go_into_the_package_schema_with_inlined_state(sample):
    net_pay = translate(sample, "function:legacy.payroll_pkg.net_pay")
    assert net_pay.code.startswith("CREATE SCHEMA IF NOT EXISTS payroll_pkg;")
    assert "FUNCTION payroll_pkg.net_pay(p_gross numeric)" in net_pay.code
    assert "(1 - 0.10)" in net_pay.code  # g_tax_rate, body-level and never assigned (D4)
    close = translate(sample, "procedure:legacy.payroll_pkg.close_month").code
    assert "payroll_pkg.net_pay(gross_pay)" in close  # unqualified call qualified


def test_monthly_payroll_inlines_the_constant_and_flags_the_dropped_default(sample):
    result = translate(sample, "procedure:legacy.monthly_payroll")
    assert "ELSE 2500 END" in result.code
    assert "p_run_date timestamp(0)," in result.code and "DEFAULT" not in result.code
    assert result.needs_review and "p_run_date" in result.review_reasons[0]
    assert "e record;" in result.code


def test_trigger_becomes_a_function_and_a_trigger(sample):
    result = translate(sample, "trigger:legacy.emp_salary_audit_trg")
    assert result.target.trigger_function == "emp_salary_audit_trg_fn"
    assert result.target.trigger_table == "employees"
    assert "NEW.emp_id, OLD.base_salary" in result.code and ":NEW" not in result.code
    assert "WHEN (NEW.base_salary <> OLD.base_salary)" in result.code
    assert result.code.count("RETURN NULL;") == 1


# --- other supported constructs ---------------------------------------------------------

def tr(source, kind="procedure", name="p", **ctx):
    result = translate_plsql(source, kind=kind, name=name, context=ConversionContext(**ctx))
    assert result.successful, result.unsupported
    return result.code


def test_loops_case_and_nested_blocks():
    code = tr("""
    CREATE PROCEDURE p (n IN NUMBER, total IN OUT NUMBER) IS
      i NUMBER := 0;
    BEGIN
      WHILE i < n LOOP i := i + 1; END LOOP;
      LOOP i := i - 1; EXIT WHEN i <= 0; END LOOP;
      FOR k IN REVERSE 1..n LOOP total := total + k; CONTINUE WHEN k > 5; END LOOP;
      CASE WHEN total > 10 THEN total := 10; ELSE NULL; END CASE;
      DECLARE j NUMBER := 2; BEGIN total := total * j; END;
    END p;
    """)
    assert "total INOUT numeric" in code
    assert "FOR k IN REVERSE n..1 LOOP" in code  # PostgreSQL takes REVERSE bounds high..low
    assert "EXIT WHEN i <= 0;" in code and "CONTINUE WHEN k > 5;" in code
    assert "CASE\n" in code and "END CASE;" in code
    assert code.count("DECLARE") == 2


def test_calls_to_known_procedures_use_call():
    code = tr("CREATE PROCEDURE p IS BEGIN log_run(1, 'x'); payroll_pkg.close_month(SYSDATE, v); END;",
              known_functions=frozenset({"log_run", "payroll_pkg.close_month"}))
    assert "CALL LOG_RUN(1, 'x');" in code  # unquoted, so PostgreSQL folds it to log_run
    assert "CALL payroll_pkg.close_month(CAST(DATE_TRUNC('SECOND'" in code


def test_raise_and_handlers():
    code = tr("CREATE PROCEDURE p IS BEGIN RAISE NO_DATA_FOUND; "
              "EXCEPTION WHEN ZERO_DIVIDE OR DUP_VAL_ON_INDEX THEN NULL; WHEN OTHERS THEN RAISE; END;")
    assert "RAISE no_data_found;" in code
    assert "WHEN division_by_zero OR unique_violation THEN" in code
    assert "WHEN OTHERS THEN" in code and "RAISE;" in code


@pytest.mark.parametrize("header, events, returning", [
    ("BEFORE INSERT OR UPDATE ON employees FOR EACH ROW", "before insert or update", "RETURN NEW;"),
    ("BEFORE DELETE ON employees FOR EACH ROW", "before delete", "RETURN OLD;"),
    ("BEFORE INSERT OR DELETE ON employees FOR EACH ROW", "before insert or delete", "IF TG_OP = 'DELETE' THEN"),
    ("AFTER INSERT ON employees", "after insert", "RETURN NULL;"),
])
def test_trigger_kinds(header, events, returning):
    code = tr(f"CREATE OR REPLACE TRIGGER t {header} BEGIN IF INSERTING THEN NULL; END IF; END;",
              kind="trigger", name="t")
    assert f"{events} ON employees".upper() in code.upper()
    assert returning in code
    assert "(TG_OP = 'INSERT')" in code
    assert ("FOR EACH ROW" in code) == ("FOR EACH ROW" in header)


# --- what is reported instead of guessed ------------------------------------------------

@pytest.mark.parametrize("body, reason", [
    ("CURSOR c IS SELECT 1 FROM dual; BEGIN NULL; END;", "CURSOR declarations"),
    ("e_bad EXCEPTION; BEGIN NULL; END;", "user-defined exceptions"),
    ("BEGIN EXECUTE IMMEDIATE 'x'; END;", "dynamic SQL"),
    ("BEGIN COMMIT; END;", "transaction control"),
    ("BEGIN <<l>> NULL; END;", "labels"),
    ("v NUMBER; BEGIN SELECT a BULK COLLECT INTO v FROM t; END;", "BULK COLLECT"),
    ("v NUMBER; BEGIN UPDATE t SET a = 1 WHERE b = 2 RETURNING a INTO v; END;", "RETURNING"),
    ("BEGIN mystery_proc(1); END;", "unknown routine mystery_proc"),
    ("BEGIN DBMS_OUTPUT.PUT_LINE('x'); END;", "dbms_output.put_line"),
    ("v NUMBER; BEGIN v := SQLCODE; END;", "SQLCODE"),
    ("v NUMBER; BEGIN UPDATE t SET a = 1 WHERE b = 2; v := SQL%ROWCOUNT; END;", "SQL%ROWCOUNT"),
    ("BEGIN NULL; EXCEPTION WHEN my_error THEN NULL; END;", "exception MY_ERROR"),
    ("v XMLTYPE; BEGIN NULL; END;", "XMLTYPE"),
    ("v VARCHAR2(5) := q'[x]'; BEGIN NULL; END;", "q-quoted"),
    ("BEGIN SELECT 1 FROM dual; END;", "SELECT without INTO"),
    ("BEGIN OPEN c; END;", "explicit cursors"),
    ("v DATE; BEGIN v := TRUNC(unknown_thing); END;", "can't tell a date from a number"),
])
def test_unsupported_constructs_are_reported(body, reason):
    result = translate_plsql(f"CREATE PROCEDURE p IS {body}", kind="procedure", name="p")
    assert not result.successful
    assert reason in " ".join(result.unsupported)


@pytest.mark.parametrize("source, kind, reason", [
    ("CREATE FUNCTION f (x OUT NUMBER) RETURN NUMBER IS BEGIN RETURN 1; END;", "function", "OUT parameters"),
    ("CREATE FUNCTION f RETURN t_tab PIPELINED IS BEGIN NULL; END;", "function", "no rule"),
    ("CREATE TRIGGER t INSTEAD OF INSERT ON v BEGIN NULL; END;", "trigger", "INSTEAD OF"),
    ("CREATE TRIGGER t BEFORE UPDATE ON e REFERENCING NEW AS n FOR EACH ROW BEGIN NULL; END;", "trigger",
     "REFERENCING"),
    ("CREATE TRIGGER t BEFORE UPDATE ON e FOR EACH ROW BEGIN IF UPDATING('a') THEN NULL; END IF; END;",
     "trigger", "UPDATING('column')"),
    ("CREATE PROCEDURE other IS BEGIN NULL; END;", "procedure", "expected 'p'"),
    ("CREATE FUNCTION p RETURN NUMBER IS BEGIN RETURN 1; END;", "procedure", "expected a procedure"),
    ("CREATE PROCEDURE p IS BEGIN NULL; END; extra", "procedure", "after the end"),
])
def test_unsupported_routines_are_reported(source, kind, reason):
    name = "f" if kind == "function" else ("t" if kind == "trigger" else "p")
    result = translate_plsql(source, kind=kind, name=name)
    assert not result.successful
    assert reason in " ".join(result.unsupported)


# --- the proof: translated code reproduces Oracle's answers -------------------------------

psycopg = pytest.importorskip("psycopg")
from tests.target_pg import (  # noqa: E402,F401  (fixtures)
    DEPLOYER_PASSWORD,
    RUNNER_PASSWORD,
    _admin_connect,
    connect_as,
    deployer_conn,
    runner_conn,
    scratch_db,
)

WATCH = ["employees", "salary_audit", "payroll_runs"]


@pytest.fixture(scope="module")
def sandbox(sample):
    """A throwaway target database with every sample routine translated and deployed."""
    try:
        server = _admin_connect()
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL for target tests not reachable ({type(exc).__name__})")
    name = f"pv_test_{uuid.uuid4().hex[:8]}"
    server.execute(f'CREATE DATABASE "{name}"')
    try:
        with _admin_connect(name) as admin:
            for path in ("01_schema.sql", "02_seed_data.sql"):  # hand-written: D8 phase 1
                admin.execute((ROOT / "samples" / "postgres" / path).read_text())
            admin.autocommit = False
            ensure_roles(admin, deployer_password=DEPLOYER_PASSWORD, runner_password=RUNNER_PASSWORD)

        objects, contexts = sample
        order = analyze_dependencies(list(objects.values())).execution_order
        deployed = {}
        with connect_as(name, DEPLOYER_ROLE) as conn:
            deployer = PostgresDeployer(conn, database=name)
            for object_id in order:
                obj = objects[object_id]
                if obj.type.value not in ROUTINES:
                    continue
                result = translate_plsql(obj.source_code, kind=obj.type.value, name=obj.name,
                                         context=contexts[object_id])
                deployed[object_id] = deployer.deploy(result.code, result.target)
        yield name, objects, deployed
    finally:
        server.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                       "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
        server.execute(f'DROP DATABASE IF EXISTS "{name}"')
        server.close()


@pytest.fixture
def runner(sandbox):
    with connect_as(sandbox[0], RUNNER_ROLE) as conn:
        yield conn
        conn.rollback()


def test_every_translation_deploys_in_dependency_order(sandbox):
    _, _, deployed = sandbox
    assert set(deployed) == set(SAMPLE_ROUTINES)
    failures = {k: r.error for k, r in deployed.items() if not r.ok}
    assert not failures


GOLDEN = [(object_id, case) for object_id, cases in GOLDEN_TESTS.items() for case in cases]


@pytest.mark.parametrize("object_id, case", GOLDEN, ids=[case.test_id for _, case in GOLDEN])
def test_golden_test_matches_oracle(sandbox, runner, object_id, case):
    _, objects, _ = sandbox
    expected = ORACLE_EXPECTED[case.test_id]
    result = run_postgres(runner, objects[object_id], case, WATCH)
    if "error" in expected:
        oracle_error = ExecutionError(code=expected["error"], message="")
        assert categorize("postgres", result.error) == categorize("oracle", oracle_error)
        return
    assert result.error is None, result.error
    if "return_value" in expected:
        assert values_equal(result.return_value, expected["return_value"])
    for param, value in expected.get("out_params", {}).items():
        assert values_equal(result.out_params[param], value), (param, result.out_params[param], value)
    if "tables_changed" in expected:
        changed = {d.table for d in result.table_diffs if d.inserted or d.deleted or d.updated}
        assert changed == expected["tables_changed"]


MP_01 = {  # EXPECTED.md section 5: emp_id -> (days_worked, gross_pay)
    101: (30, "22500.00"), 102: (30, "12500.00"), 103: (30, "10666.67"), 104: (30, "9583.38"),
    105: (30, "9583.38"), 106: (30, "7833.33"), 107: (30, "8500.00"), 108: (30, "7166.67"),
    109: (14, "1944.44"), 111: (30, "10916.73"),
}


def test_mp01_monthly_payroll_matches_oracle_row_by_row(runner):
    runner.execute("DELETE FROM payroll_runs WHERE pay_month = TIMESTAMP '2026-09-01'")
    rows_out = runner.execute("CALL monthly_payroll(TIMESTAMP '2026-09-15', NULL)").fetchone()[0]
    assert rows_out == 10
    rows = runner.execute(
        "SELECT emp_id, days_worked, gross_pay, created_at FROM payroll_runs "
        "WHERE pay_month = TIMESTAMP '2026-09-01' ORDER BY emp_id"
    ).fetchall()
    got = {int(e): (int(d), str(g)) for e, d, g, _ in rows}
    assert got == {e: (d, g) for e, (d, g) in MP_01.items()}  # incl. trap row 108: 7166.67, not 4666.67
    assert {str(c) for *_, c in rows} == {"2026-09-15 00:00:00"}


@pytest.mark.parametrize("call, oracle_answer", [
    ("SELECT payroll_pkg.net_pay(10000)", Decimal("9000.00")),      # pk-01
    ("SELECT payroll_pkg.net_pay(NULL)", Decimal("0")),             # pk-02
    ("SELECT payroll_pkg.net_pay(9583.38)", Decimal("8625.04")),    # pk-03
    ("CALL payroll_pkg.close_month(TIMESTAMP '2026-10-01', NULL)", Decimal("0")),  # pk-05
])
def test_payroll_pkg_matches_oracle(runner, call, oracle_answer):
    runner.execute("DELETE FROM payroll_runs WHERE pay_month = TIMESTAMP '2026-10-01'")
    assert values_equal(runner.execute(call).fetchone()[0], oracle_answer)


def test_pk04_close_month_after_monthly_payroll(runner):
    runner.execute("DELETE FROM payroll_runs WHERE pay_month = TIMESTAMP '2026-09-01'")
    runner.execute("CALL monthly_payroll(TIMESTAMP '2026-09-15', NULL)")
    total = runner.execute("CALL payroll_pkg.close_month(TIMESTAMP '2026-09-15', NULL)").fetchone()[0]
    assert values_equal(total, Decimal("91075.14"))


@pytest.mark.parametrize("new_salary, change_type", [("70000", "RAISE"), ("60000", "CUT"), ("base_salary", None)])
def test_tr_trigger_matches_oracle(runner, new_salary, change_type):  # tr-01, tr-02, tr-03
    before = runner.execute("SELECT count(*) FROM salary_audit").fetchone()[0]
    runner.execute(f"UPDATE employees SET base_salary = {new_salary} WHERE emp_id = 106")
    rows = runner.execute(
        "SELECT emp_id, old_salary, new_salary, change_type, changed_on FROM salary_audit "
        "ORDER BY audit_id DESC LIMIT 1"
    ).fetchall()
    after = runner.execute("SELECT count(*) FROM salary_audit").fetchone()[0]
    if change_type is None:
        assert after == before
        return
    assert after == before + 1
    emp_id, old, new, kind, changed_on = rows[0]
    assert (int(emp_id), old, new, kind) == (106, Decimal("64000.00"), Decimal(new_salary), change_type)
    assert changed_on.hour == changed_on.minute == changed_on.second == 0  # TRUNC(SYSDATE)


# --- review fix 1: literals are data, comments are not ---------------------------------

LITERALS = {
    "multiple spaces": "'Hello    world'",
    "tab": "'a\tb'",
    "newline": "'line1\nline2'",
    "escaped quote": "'O''Brien  x'",
    "only spaces": "'   '",
    "comment markers": "'-- not a comment /* nor this */'",
}


@pytest.mark.parametrize("label", sorted(LITERALS))
def test_string_literals_are_copied_exactly(label):
    literal = LITERALS[label]
    code = tr(f"CREATE FUNCTION f RETURN VARCHAR2 IS\n  v VARCHAR2(100) := {literal};\n"
              f"BEGIN\n  v := v ||   {literal};\n  RETURN {literal};\nEND;", kind="function", name="f")
    assert code.count(literal) == 3, code


def test_comments_are_dropped_and_cannot_swallow_code():
    code = tr("CREATE FUNCTION f RETURN NUMBER IS v NUMBER;\nBEGIN\n"
              "  v := 1 -- a comment with ; and a quote '\n       + 2;  /* block\n comment */\n"
              "  RETURN v;\nEND;", kind="function", name="f")
    assert "v := 1 + 2;" in code
    assert "comment" not in code


def test_comments_in_into_targets_and_trigger_events_are_dropped():
    select = tr("CREATE PROCEDURE p IS a NUMBER; b NUMBER; BEGIN\n"
                "  SELECT 1, 2 INTO a, -- first\n b FROM dual;\nEND;")
    assert "INTO STRICT a, b;" in select
    trigger = tr("CREATE TRIGGER t AFTER INSERT /* new rows */ OR DELETE ON employees BEGIN NULL; END;",
                 kind="trigger", name="t")
    assert "AFTER insert or delete ON employees" in trigger


def test_literals_survive_in_postgresql(scratch_db, deployer_conn, runner_conn):
    """Deploy a translated function and read the literals back from PostgreSQL."""
    values = [LITERALS[k] for k in sorted(LITERALS)]
    body = " || '|' || ".join(values)
    code = tr(f"CREATE FUNCTION lit_check RETURN VARCHAR2 IS BEGIN RETURN {body}; END;",
              kind="function", name="lit_check")
    from app.migration.postgres_deployer import DeployTarget
    result = PostgresDeployer(deployer_conn, database=scratch_db).deploy(code, DeployTarget("function", "lit_check"))
    assert result.ok, result.error
    expected = "|".join(v[1:-1].replace("''", "'") for v in values)
    assert runner_conn.execute("SELECT lit_check()").fetchone()[0] == expected
    runner_conn.rollback()

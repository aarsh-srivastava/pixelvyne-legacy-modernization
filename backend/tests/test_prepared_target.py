"""PreparedTargetSchema (contract 0.7 stand-in) against a real prepared target, and the sample fixture.

Each test gets a fresh database prepared the way seed_target.py --schema-only and
setup_target_roles.py prepare one, then breaks it the way a live target breaks. Nothing
here touches the real target database or calls Gemini.
"""

from __future__ import annotations

import importlib.util
import json
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from app.contracts import ObjectType, SchemaObjectStatus, SourceFile, TargetEnvironment
from app.migration import prepared_target
from app.migration.prepared_target import (
    OracleColumn,
    OracleForeignKey,
    OracleTable,
    PreparedTargetSchema,
    _canonical_default,
    _canonical_type,
    oracle_sequences,
    oracle_tables,
)
from app.parsers.oracle_parser import parse_sources

ROOT = Path(__file__).resolve().parents[2]
SOURCES = [SourceFile.of(p.name, p.read_text(encoding="utf-8"))
           for p in sorted((ROOT / "samples" / "oracle").glob("*.sql"))]
OBJECTS = parse_sources({s.name: s.text for s in SOURCES})
TABLES = [o.object_id for o in OBJECTS if o.type == ObjectType.TABLE]
SEQUENCES = [o.object_id for o in OBJECTS if o.type == ObjectType.SEQUENCE]


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), ROOT / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_run_repair_uses_the_same_preflight():
    script = _script("run_repair.py")
    assert script.preflight is prepared_target.preflight
    assert script.foreign_routines is prepared_target.foreign_routines



# --- the Oracle DDL's definitions (Aarsh's v0.7 review, 19 Nov), no database -------------

_DDL = """
CREATE TABLE parent (id NUMBER(6) PRIMARY KEY, code VARCHAR2(5) UNIQUE);
CREATE TABLE child (
  a NUMBER NOT NULL,
  b VARCHAR2(5) DEFAULT SYSDATE,      -- not a literal
  c NUMBER(6) REFERENCES parent,      -- no column list: the parent's primary key
  d NUMBER(6),
  CONSTRAINT child_pk PRIMARY KEY (a, b),
  CONSTRAINT child_u UNIQUE (d),
  CONSTRAINT child_fk FOREIGN KEY (d) REFERENCES other.parent (id) ON DELETE CASCADE,
  CONSTRAINT child_fk2 FOREIGN KEY (c) REFERENCES parent (id) ON DELETE SET NULL);
CREATE SEQUENCE s1 START WITH 5 INCREMENT BY 2 MINVALUE 1 MAXVALUE 999 CYCLE;
CREATE SEQUENCE s2;  -- START WITH 9 in a comment does not count
CREATE SEQUENCE app.s3 NOCYCLE START WITH 7;
"""


def test_the_oracle_ddl_gives_columns_keys_and_foreign_keys_with_their_schema():
    tables = oracle_tables([_DDL])
    child = tables["child"]
    assert child.primary_key == ["a", "b"] and child.unique == {("d",)}
    assert child.columns["a"].not_null and not child.columns["d"].not_null
    assert child.columns["b"].default == "SYSDATE"
    assert child.foreign_keys == {
        OracleForeignKey(("c",), None, "parent", (), "no_action"),
        OracleForeignKey(("d",), "other", "parent", ("id",), "cascade"),
        OracleForeignKey(("c",), None, "parent", ("id",), "set_null"),
    }
    assert tables["parent"].primary_key == ["id"] and tables["parent"].unique == {("code",)}


def test_sequence_options_come_from_the_statement_not_from_comments():
    seqs = oracle_sequences([_DDL])
    assert (seqs["s1"].start_with, seqs["s1"].increment_by, seqs["s1"].min_value, seqs["s1"].max_value,
            seqs["s1"].cycle) == (5, 2, 1, 999, True)
    assert (seqs["s2"].start_with, seqs["s2"].cycle) == (None, None)
    assert (seqs["s3"].start_with, seqs["s3"].cycle) == (7, False)


@pytest.mark.parametrize("catalogue, rule", [
    ("timestamp(0) without time zone", "timestamp(0)"), ("numeric(6,0)", "numeric(6)"),
    ("numeric(10,2)", "numeric(10, 2)"), ("character varying(60)", "varchar(60)"), ("character(1)", "char"),
    ("timestamp(6) with time zone", "timestamptz(6)"), ("numeric", "numeric"),
])
def test_catalogue_and_type_rule_spellings_agree(catalogue, rule):
    assert _canonical_type(catalogue) == _canonical_type(rule)


@pytest.mark.parametrize("catalogue, rule", [
    ("numeric(10,2)", "numeric(12, 2)"), ("varchar(60)", "varchar(61)"), ("timestamp(0)", "timestamp(6)"),
    ("text", "varchar(10)"), ("timestamp(0) with time zone", "timestamp(0)"),
])
def test_different_types_stay_different(catalogue, rule):
    assert _canonical_type(catalogue) != _canonical_type(rule)


def test_defaults_lose_only_postgresql_casts():
    assert _canonical_default("'ACTIVE'::character varying") == "'ACTIVE'"
    assert _canonical_default("('x'::text)") == "'x'"
    assert _canonical_default("0") == "0" and _canonical_default(None) is None
    assert _canonical_default("NULL::character varying") is None


def test_a_non_literal_oracle_default_is_a_mismatch_not_a_pass():
    table = OracleTable("t", columns={"b": OracleColumn("b", "DATE", False, "SYSDATE")})
    reasons = PreparedTargetSchema._column_differences([("b", "timestamp(0) without time zone", True,
                                                         "LOCALTIMESTAMP(0)")], table)
    assert reasons == ["b: the default SYSDATE is not a literal, so the prepared-target check cannot compare it "
                       "(M3 creates such defaults)"]


psycopg = pytest.importorskip("psycopg")

from app.migration.target_roles import DEPLOYER_ROLE, RUNNER_ROLE, ensure_roles  # noqa: E402
from tests.target_pg import ADMIN_URL, DEPLOYER_PASSWORD, RUNNER_PASSWORD, _admin_connect  # noqa: E402


def _url(name: str, user: str, password: str) -> str:
    parts = urlsplit(ADMIN_URL)
    return f"postgresql://{user}:{password}@{parts.hostname}:{parts.port or 5432}/{name}"


def environment(name: str, *, database: str | None = None) -> TargetEnvironment:
    return TargetEnvironment(oracle_dsn="unused", oracle_user="legacy", oracle_password="unused",
                             deployer_url=_url(name, DEPLOYER_ROLE, DEPLOYER_PASSWORD),
                             runner_url=_url(name, RUNNER_ROLE, RUNNER_PASSWORD), database_name=database or name)


def _load_sample(admin) -> None:
    for path in ("01_schema.sql", "02_seed_data.sql"):
        admin.execute((ROOT / "samples" / "postgres" / path).read_text(encoding="utf-8"))


@pytest.fixture
def target():
    try:
        server = _admin_connect()
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL for target tests not reachable ({type(exc).__name__})")
    name = f"pv_test_{uuid.uuid4().hex[:8]}"
    server.execute(f'CREATE DATABASE "{name}"')
    try:
        with _admin_connect(name) as admin:
            _load_sample(admin)
            admin.autocommit = False
            ensure_roles(admin, deployer_password=DEPLOYER_PASSWORD, runner_password=RUNNER_PASSWORD)
        yield name
    finally:
        server.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                       "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
        server.execute(f'DROP DATABASE IF EXISTS "{name}"')
        server.close()


def check(name: str, **kwargs):
    return PreparedTargetSchema().migrate(SOURCES, environment(name, **kwargs),
                                          expected_tables=TABLES, expected_sequences=SEQUENCES)


def _admin(name: str, *statements: str) -> None:
    with _admin_connect(name) as admin:
        for statement in statements:
            admin.execute(statement)


def by_id(result) -> dict:
    return {o.object_id: o for o in result.objects}


def test_db_a_prepared_target_is_present_with_its_definitions(target):
    result = check(target)
    assert result.ok and result.problems == [] and result.mode == "prepared" and result.parity == "not_checked"
    assert [o.object_id for o in result.objects] == sorted(TABLES) + sorted(SEQUENCES)
    assert {o.status for o in result.objects} == {SchemaObjectStatus.PRESENT}
    employees = by_id(result)["table:legacy.employees"].definition
    assert employees.primary_key == ["emp_id"] and len(employees.columns) == 9
    assert employees.column("base_salary").oracle_type == "NUMBER(10, 2)"
    assert employees.column("base_salary").pg_type == "numeric(10,2)" and not employees.column("base_salary").nullable
    assert employees.column("status").default == "'ACTIVE'::character varying"
    assert {fk.ref_table for fk in employees.foreign_keys} >= {"table:legacy.departments"}
    sequence = by_id(result)["sequence:legacy.salary_audit_seq"].definition
    assert sequence.increment_by == 1 and sequence.target_name == "salary_audit_seq"


def test_db_the_check_changes_nothing_and_needs_no_admin(target):
    seen = []

    def connect(url):
        seen.append(urlsplit(url).username)
        return psycopg.connect(url)

    with _admin_connect(target) as admin:
        before = admin.execute("SELECT count(*) FROM employees").fetchone()[0]
    PreparedTargetSchema(connect).migrate(SOURCES, environment(target), expected_tables=TABLES,
                                          expected_sequences=SEQUENCES)
    assert sorted(seen) == [DEPLOYER_ROLE, RUNNER_ROLE]
    with _admin_connect(target) as admin:
        assert admin.execute("SELECT count(*) FROM employees").fetchone()[0] == before


def test_db_a_missing_table_is_missing_and_the_target_is_not_ok(target):
    _admin(target, "DROP TABLE salary_audit CASCADE")
    result = check(target)
    audit = by_id(result)["table:legacy.salary_audit"]
    assert audit.status is SchemaObjectStatus.MISSING and audit.definition is None and "seed_target.py" in audit.reason
    assert not result.ok and any("missing table(s) in public: salary_audit" in p for p in result.problems)


def test_db_a_column_the_oracle_ddl_does_not_have_is_a_mismatch(target):
    _admin(target, "ALTER TABLE departments ADD COLUMN extra_col integer")
    departments = by_id(check(target))["table:legacy.departments"]
    assert departments.status is SchemaObjectStatus.MISMATCH and departments.definition is None
    assert "column(s) not in the Oracle DDL: extra_col" in departments.reason


def test_db_a_column_missing_from_the_target_is_a_mismatch(target):
    _admin(target, "ALTER TABLE employees DROP COLUMN email")
    employees = by_id(check(target))["table:legacy.employees"]
    assert employees.status is SchemaObjectStatus.MISMATCH
    assert "column(s) of the Oracle DDL missing: email" in employees.reason and employees.definition is not None


def test_db_missing_rights_are_a_mismatch_with_the_fix(target):
    _admin(target, f"REVOKE INSERT ON salary_audit FROM {RUNNER_ROLE}",
           f"REVOKE USAGE ON SEQUENCE payroll_run_seq FROM {RUNNER_ROLE}")
    result = check(target)
    audit, sequence = by_id(result)["table:legacy.salary_audit"], by_id(result)["sequence:legacy.payroll_run_seq"]
    assert audit.status is SchemaObjectStatus.MISMATCH
    assert "pv_runner lacks INSERT on it" in audit.reason and "setup_target_roles.py" in audit.reason
    assert sequence.status is SchemaObjectStatus.MISMATCH and "pv_runner lacks USAGE on it" in sequence.reason
    assert by_id(result)["table:legacy.employees"].status is SchemaObjectStatus.PRESENT and not result.ok


def test_db_the_wrong_database_and_foreign_objects_are_problems(target):
    _admin(target, "CREATE FUNCTION public.stand_in() RETURNS int LANGUAGE sql AS 'SELECT 1'")
    result = check(target, database="target")
    joined = "\n".join(result.problems)
    assert f"is on database {target}, not target" in joined
    assert "objects pv_deployer does not own" in joined and "public.stand_in()" in joined
    assert not result.ok


def test_db_the_result_holds_no_secrets(target):
    text = check(target).model_dump_json()
    assert DEPLOYER_PASSWORD + "@" not in text and "postgresql://" not in text


def test_db_the_fixture_script_reproduces_the_committed_statuses(monkeypatch):
    """scripts/build_run_fixture.py, run now, gives each object the status the committed fixture has."""
    try:
        _admin_connect().close()
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL for target tests not reachable ({type(exc).__name__})")
    fresh = _script("build_run_fixture.py").build(ADMIN_URL)
    committed = json.loads((ROOT / "backend" / "tests" / "fixtures" / "migration_run_sample.json").read_text())

    def statuses(run):
        return {o["object_id"]: (o["route"], o["status"], o["category"]) for o in run["objects"]}

    assert statuses(fresh) == statuses(committed)
    assert fresh["metrics"] == committed["metrics"] and fresh["oracle_answers"] == "recorded"


def test_db_a_table_with_no_create_table_in_the_sources_is_a_mismatch(target):
    without_ddl = [s for s in SOURCES if s.name != "01_schema.sql"]
    result = PreparedTargetSchema().migrate(without_ddl, environment(target), expected_tables=TABLES,
                                            expected_sequences=SEQUENCES)
    departments = by_id(result)["table:legacy.departments"]
    assert departments.status is SchemaObjectStatus.MISMATCH and departments.definition is None
    assert departments.reason.startswith("no CREATE TABLE for it in the sources")



# --- every property is compared (Aarsh's v0.7 review, 19 Nov) -------------------------------

@pytest.mark.parametrize("object_id, statements, reason", [
    ("table:legacy.employees", ["ALTER TABLE employees ALTER COLUMN base_salary TYPE numeric(12,2)"],
     "base_salary is numeric(12,2), but NUMBER(10, 2) converts to numeric(10, 2)"),
    ("table:legacy.employees", ["ALTER TABLE employees ALTER COLUMN hire_date TYPE date"],
     "hire_date is date, but DATE converts to timestamp(0)"),
    ("table:legacy.employees", ["ALTER TABLE employees ALTER COLUMN hire_date DROP NOT NULL"],
     "hire_date allows NULL, but the Oracle DDL declares it NOT NULL"),
    ("table:legacy.employees", ["UPDATE employees SET email = 'x' WHERE email IS NULL",
                                "ALTER TABLE employees ALTER COLUMN email SET NOT NULL"],
     "email is NOT NULL, but the Oracle DDL allows NULL"),
    ("table:legacy.employees", ["ALTER TABLE employees ALTER COLUMN status SET DEFAULT 'GONE'"],
     "status has default 'GONE'::character varying, but the Oracle DDL has 'ACTIVE'"),
    ("table:legacy.employees", ["ALTER TABLE employees ALTER COLUMN status DROP DEFAULT"],
     "status has default none, but the Oracle DDL has 'ACTIVE'"),
    ("table:legacy.employees", ["ALTER TABLE employees ALTER COLUMN email SET DEFAULT 'x'"],
     "email has default 'x'::character varying, but the Oracle DDL has none"),
    ("table:legacy.salary_audit", ["ALTER TABLE salary_audit DROP CONSTRAINT salary_audit_pkey"],
     "primary key is (none), but the Oracle DDL has (audit_id)"),
    ("table:legacy.employees", ["ALTER TABLE employees ADD CONSTRAINT emp_email_u UNIQUE (email)"],
     "unique keys [['email']] differ from the Oracle DDL's []"),
    ("table:legacy.payroll_runs", ["ALTER TABLE payroll_runs DROP CONSTRAINT payroll_runs_emp_id_fkey"],
     "foreign key (emp_id) -> table:legacy.employees (emp_id) on delete no_action of the Oracle DDL is missing"),
    ("table:legacy.payroll_runs", ["ALTER TABLE payroll_runs DROP CONSTRAINT payroll_runs_emp_id_fkey",
                                   "ALTER TABLE payroll_runs ADD FOREIGN KEY (emp_id) REFERENCES employees (emp_id) "
                                   "ON DELETE CASCADE"],
     "foreign key (emp_id) -> table:legacy.employees (emp_id) on delete cascade is not in the Oracle DDL"),
    ("sequence:legacy.payroll_run_seq", ["ALTER SEQUENCE payroll_run_seq START WITH 5"],
     "START WITH is 5, but the Oracle DDL gives 1000"),
    ("sequence:legacy.salary_audit_seq", ["ALTER SEQUENCE salary_audit_seq INCREMENT BY 2"],
     "INCREMENT BY is 2, but the Oracle DDL gives 1"),
    ("sequence:legacy.salary_audit_seq", ["ALTER SEQUENCE salary_audit_seq CYCLE"],
     "CYCLE is True, but the Oracle DDL gives False"),
    ("sequence:legacy.salary_audit_seq", ["ALTER SEQUENCE salary_audit_seq MINVALUE 0"],
     "MINVALUE is 0, but the Oracle DDL gives 1"),
])
def test_db_each_schema_property_that_differs_is_a_mismatch(target, object_id, statements, reason):
    _admin(target, *statements)
    result = check(target)
    outcome = by_id(result)[object_id]
    assert outcome.status is SchemaObjectStatus.MISMATCH and reason in outcome.reason, outcome.reason
    assert not result.ok
    others = [o for o in result.objects if o.object_id != object_id]
    assert all(o.status is SchemaObjectStatus.PRESENT for o in others), [o.reason for o in others if not o.ready]


def test_db_a_foreign_key_to_another_schema_keeps_that_schema(target):
    """A table named departments in another schema never passes for the upload's departments."""
    _admin(target, "CREATE SCHEMA other",
           "CREATE TABLE other.departments (dept_id numeric(6) PRIMARY KEY)",
           "INSERT INTO other.departments SELECT dept_id FROM public.departments",
           "ALTER TABLE employees DROP CONSTRAINT employees_dept_id_fkey",
           "ALTER TABLE employees ADD FOREIGN KEY (dept_id) REFERENCES other.departments (dept_id)")
    employees = by_id(check(target))["table:legacy.employees"]
    refs = {fk.ref_table for fk in employees.definition.foreign_keys}
    assert "table:other.departments" in refs and "table:legacy.departments" not in refs
    assert employees.status is SchemaObjectStatus.MISMATCH
    assert ("foreign key (dept_id) -> table:other.departments (dept_id) on delete no_action is not in the "
            "Oracle DDL") in employees.reason
    assert ("foreign key (dept_id) -> table:legacy.departments (dept_id) on delete no_action of the Oracle DDL "
            "is missing") in employees.reason


def test_db_an_oracle_foreign_key_to_another_schema_is_a_mismatch(target):
    sources = [SourceFile.of(s.name, s.text.replace("REFERENCES departments (dept_id)",
                                                    "REFERENCES hr.departments (dept_id)"))
               for s in SOURCES]
    result = PreparedTargetSchema().migrate(sources, environment(target), expected_tables=TABLES,
                                            expected_sequences=SEQUENCES)
    employees = by_id(result)["table:legacy.employees"]
    assert employees.status is SchemaObjectStatus.MISMATCH
    assert "foreign key (dept_id) references hr.departments, which is not a table of the upload" in employees.reason


def test_db_a_used_sequence_is_still_present(target):
    _admin(target, "SELECT nextval('payroll_run_seq')", "SELECT nextval('payroll_run_seq')")
    assert by_id(check(target))["sequence:legacy.payroll_run_seq"].status is SchemaObjectStatus.PRESENT


def test_db_present_says_what_was_compared(target):
    employees = by_id(check(target))["table:legacy.employees"]
    assert employees.status is SchemaObjectStatus.PRESENT
    assert "types, NOT NULL and defaults, the primary, unique and foreign keys" in employees.reason


def test_db_a_reference_without_columns_means_the_primary_key(target):
    """Oracle's REFERENCES departments (no column list) points at its primary key, dept_id."""
    sources = [SourceFile.of(s.name, s.text.replace("REFERENCES departments (dept_id)", "REFERENCES departments"))
               for s in SOURCES]
    assert any("REFERENCES departments," in s.text or "REFERENCES departments\n" in s.text for s in sources)
    result = PreparedTargetSchema().migrate(sources, environment(target), expected_tables=TABLES,
                                            expected_sequences=SEQUENCES)
    assert by_id(result)["table:legacy.employees"].status is SchemaObjectStatus.PRESENT, \
        by_id(result)["table:legacy.employees"].reason

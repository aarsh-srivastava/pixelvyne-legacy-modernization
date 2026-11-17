"""F1 pre-flight in scripts/run_repair.py --converter real (Aarsh 17 Nov, decision 4 and safeguards A, C).

The pre-flight runs before the converter exists. Each target state that broke or could break a
live run (42P01: tables missing; 42501: rights missing after a reseed; no helper; wrong identity
or database; Redis down with --gemini) must be reported with its fix, and must stop the run
before the converter is built or Gemini is called. Nothing here calls Gemini.
"""

from __future__ import annotations

import contextlib
import importlib.util
import types
import uuid
from pathlib import Path

import pytest

from app.contracts import ObjectType
from app.parsers.oracle_parser import parse_sources

ROOT = Path(__file__).resolve().parents[2]


def _load_run_repair():
    spec = importlib.util.spec_from_file_location("run_repair_preflight", ROOT / "scripts" / "run_repair.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCRIPT = _load_run_repair()
_SOURCES = {p.name: p.read_text(encoding="utf-8") for p in sorted((ROOT / "samples" / "oracle").glob("*.sql"))}
OBJECTS = parse_sources(_SOURCES)
TABLES = sorted(o.name.lower() for o in OBJECTS if o.type == ObjectType.TABLE)
SEQUENCES = sorted(o.name.lower() for o in OBJECTS if o.type == ObjectType.SEQUENCE)


# --- offline -----------------------------------------------------------------------------------

def test_the_sample_has_the_tables_and_sequences_the_preflight_checks():
    assert TABLES == ["departments", "employees", "payroll_runs", "salary_audit"]
    assert SEQUENCES == ["payroll_run_seq", "salary_audit_seq"]


def test_the_rights_checked_are_the_rights_setup_grants():
    source = (ROOT / "backend" / "app" / "migration" / "target_roles.py").read_text(encoding="utf-8")
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}" in source
    assert SCRIPT.RUNNER_TABLE_PRIVILEGES == ("SELECT", "INSERT", "UPDATE", "DELETE")
    assert "GRANT SELECT, REFERENCES, TRIGGER ON ALL TABLES IN SCHEMA public TO {}" in source
    assert SCRIPT.DEPLOYER_TABLE_PRIVILEGES == ("SELECT", "REFERENCES", "TRIGGER")
    assert "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}" in source
    assert SCRIPT.RUNNER_SEQUENCE_PRIVILEGES == ("USAGE", "SELECT")


def test_an_unreachable_redis_is_a_problem_that_never_shows_credentials():
    problem = SCRIPT.redis_problem("redis://:s3cret-pass@localhost:1/0")
    assert problem and "localhost:1" in problem and "s3cret-pass" not in problem
    assert "docker compose up -d redis" in problem


def test_the_calls_report_sits_next_to_the_json_report():
    assert SCRIPT.calls_path(Path("reports/repair_7d_org_chart.json")) == Path("reports/repair_7d_org_chart.calls.json")


# --- PostgreSQL ----------------------------------------------------------------------------------

psycopg = pytest.importorskip("psycopg")
from psycopg.conninfo import make_conninfo  # noqa: E402

from app.migration.target_roles import DEPLOYER_ROLE, RUNNER_ROLE, ensure_roles  # noqa: E402
from tests.target_pg import ADMIN_URL, DEPLOYER_PASSWORD, RUNNER_PASSWORD, _admin_connect  # noqa: E402


def _load_sample(admin) -> None:
    for path in ("01_schema.sql", "02_seed_data.sql"):
        admin.execute((ROOT / "samples" / "postgres" / path).read_text(encoding="utf-8"))


def _roles(name: str) -> None:
    with _admin_connect(name) as admin:
        admin.autocommit = False
        ensure_roles(admin, deployer_password=DEPLOYER_PASSWORD, runner_password=RUNNER_PASSWORD)


def _url(name: str, role: str) -> str:
    password = {DEPLOYER_ROLE: DEPLOYER_PASSWORD, RUNNER_ROLE: RUNNER_PASSWORD}[role]
    return make_conninfo(ADMIN_URL, dbname=name, user=role, password=password)


@pytest.fixture
def target():
    """A fresh, prepared target (sample schema, data, roles, helper), dropped afterwards."""
    try:
        server = _admin_connect()
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL for target tests not reachable ({type(exc).__name__})")
    name = f"pv_test_{uuid.uuid4().hex[:8]}"
    server.execute(f'CREATE DATABASE "{name}"')
    try:
        with _admin_connect(name) as admin:
            _load_sample(admin)
        _roles(name)
        yield name
    finally:
        server.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                       "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
        server.execute(f'DROP DATABASE IF EXISTS "{name}"')
        server.close()


def _admin(name: str, *statements: str) -> None:
    with _admin_connect(name) as admin:
        for statement in statements:
            admin.execute(statement)


def _reseed_without_roles(name: str) -> None:
    """What seed_target.py --schema-only does, without setup_target_roles.py afterwards."""
    with _admin_connect(name) as admin:
        admin.execute("DROP SCHEMA public CASCADE")
        admin.execute("CREATE SCHEMA public")
        _load_sample(admin)


def problems(name: str, *, deployer_role=DEPLOYER_ROLE, runner_role=RUNNER_ROLE, database=None) -> list[str]:
    with psycopg.connect(_url(name, deployer_role)) as deployer, psycopg.connect(_url(name, runner_role)) as runner:
        return SCRIPT.preflight(deployer, runner, database=database or name, tables=TABLES, sequences=SEQUENCES)


def test_db_a_prepared_target_passes(target):
    assert problems(target) == []


def test_db_a_reseed_without_roles_is_reported_as_missing_rights(target):
    """The 42501 state: public recreated with no grants."""
    _reseed_without_roles(target)
    found = problems(target)
    joined = "\n".join(found)
    assert "pv_deployer lacks USAGE on schema public" in joined
    assert "pv_deployer lacks CREATE on schema public" in joined
    assert "pv_runner lacks USAGE on schema public" in joined
    assert "pv_runner lacks rights on table(s) departments (SELECT, INSERT, UPDATE, DELETE)" in joined
    assert all("setup_target_roles.py" in p for p in found)


def test_db_usage_without_create_is_its_own_problem(target):
    """Safeguard A: USAGE and CREATE are checked separately."""
    _admin(target, "REVOKE CREATE ON SCHEMA public FROM pv_deployer")
    assert problems(target) == [f"pv_deployer lacks CREATE on schema public: {SCRIPT.ROLES_HINT}"]


def test_db_a_missing_table_is_reported_as_missing(target):
    """The 42P01 state: employees does not exist."""
    _admin(target, "DROP TABLE employees CASCADE")
    found = problems(target)
    assert found[0] == f"missing table(s) in public: employees: {SCRIPT.RESET_HINT}"
    assert len(found) == 1


def test_db_an_empty_public_schema_is_reported_table_by_table(target):
    with _admin_connect(target) as admin:
        admin.execute("DROP SCHEMA public CASCADE")
        admin.execute("CREATE SCHEMA public")
    _roles(target)
    found = problems(target)
    assert found == [f"missing table(s) in public: {', '.join(TABLES)}: {SCRIPT.RESET_HINT}",
                     f"missing sequence(s) in public: {', '.join(SEQUENCES)}: {SCRIPT.RESET_HINT}"]


def test_db_a_missing_sequence_right_is_reported(target):
    _admin(target, "REVOKE USAGE ON SEQUENCE salary_audit_seq FROM pv_runner")
    assert problems(target) == [f"pv_runner lacks rights on sequence(s) salary_audit_seq (USAGE): {SCRIPT.ROLES_HINT}"]


def test_db_a_missing_helper_is_reported(target):
    _admin(target, "DROP SCHEMA pv_compat CASCADE")
    assert problems(target) == [
        f"pv_compat.connect_by_path_checked(text, boolean) is not installed: {SCRIPT.ROLES_HINT}"]


def test_db_a_helper_the_runner_cannot_execute_is_reported(target):
    _admin(target, "REVOKE EXECUTE ON FUNCTION pv_compat.connect_by_path_checked(text, boolean) FROM pv_runner")
    assert problems(target) == [f"pv_runner cannot use pv_compat.connect_by_path_checked: {SCRIPT.ROLES_HINT}"]


@pytest.mark.parametrize("deployer_role, runner_role", [(RUNNER_ROLE, RUNNER_ROLE), (DEPLOYER_ROLE, DEPLOYER_ROLE)])
def test_db_a_connection_running_as_the_wrong_role_is_reported(target, deployer_role, runner_role):
    """Safeguard A: the actual current_user of each connection, not just what the roles may do."""
    found = problems(target, deployer_role=deployer_role, runner_role=runner_role)
    wrong = ("the pv_deployer connection runs as pv_runner, not pv_deployer: check TARGET_DEPLOYER_URL"
             if deployer_role == RUNNER_ROLE else
             "the pv_runner connection runs as pv_deployer, not pv_runner: check TARGET_RUNNER_URL")
    assert wrong in found


def test_db_the_wrong_database_is_reported(target):
    found = problems(target, database="target")
    assert (f"the pv_deployer connection is on database {target}, not target: check TARGET_DEPLOYER_URL "
            "and TARGET_DATABASE_NAME") in found
    assert (f"the pv_runner connection is on database {target}, not target: check TARGET_RUNNER_URL "
            "and TARGET_DATABASE_NAME") in found


def test_db_a_helper_not_owned_by_an_admin_is_reported(target):
    _admin(target, "ALTER FUNCTION pv_compat.connect_by_path_checked(text, boolean) OWNER TO pv_deployer")
    expected = ("pv_compat.connect_by_path_checked is owned by pv_deployer, not by the admin that ran setup: "
                f"{SCRIPT.ROLES_HINT}")
    assert problems(target) == [expected]


def _foreign(name: str) -> list[str]:
    with psycopg.connect(_url(name, DEPLOYER_ROLE)) as deployer:
        return SCRIPT.foreign_routines(deployer)


def test_db_only_the_approved_helper_is_not_a_foreign_routine(target):
    """bd4a7f9 regression: the admin-owned helper made every --converter real run exit 2.

    The exception is that one signature, admin-owned (Aarsh's review of the patch, 17 Nov)."""
    assert _foreign(target) == []
    _admin(target,
           "CREATE FUNCTION pv_compat.other_helper() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$",
           "CREATE FUNCTION pv_compat.connect_by_path_checked(text) RETURNS text LANGUAGE sql AS $$ SELECT $1 $$",
           "CREATE FUNCTION public.get_emp_grade(numeric) RETURNS text LANGUAGE sql AS $$ SELECT 'x' $$",
           "CREATE VIEW public.org_chart AS SELECT 1 AS x")
    assert _foreign(target) == ["public.get_emp_grade(numeric)", "public.org_chart (view)",
                                "pv_compat.connect_by_path_checked(text)", "pv_compat.other_helper()"]


def test_db_the_approved_helper_counts_as_foreign_when_not_admin_owned(target):
    _admin(target, "ALTER FUNCTION pv_compat.connect_by_path_checked(text, boolean) OWNER TO pv_runner")
    assert _foreign(target) == ["pv_compat.connect_by_path_checked(text, boolean)"]


def test_db_a_missing_references_right_is_reported(target):
    _admin(target, "REVOKE REFERENCES ON employees FROM pv_deployer")
    assert problems(target) == [f"pv_deployer lacks rights on table(s) employees (REFERENCES): {SCRIPT.ROLES_HINT}"]


def test_db_the_preflight_leaves_no_transaction_open(target):
    with psycopg.connect(_url(target, DEPLOYER_ROLE)) as deployer, psycopg.connect(_url(target, RUNNER_ROLE)) as runner:
        SCRIPT.preflight(deployer, runner, database=target, tables=TABLES, sequences=SEQUENCES)
        assert deployer.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
        assert runner.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


# --- safeguard C: a failed pre-flight builds no converter and calls no Gemini ------------------

class _NoOracle:
    @staticmethod
    def connect(**kwargs):
        return contextlib.nullcontext(object())


@pytest.fixture
def gated(monkeypatch):
    """run_real with every way to reach Gemini or build the converter turned into a test failure."""
    import app.migration.converter as converter_module
    import app.migration.llm_fallback as fallback_module
    from app.services import llm_service

    touched = []

    def forbidden(what):
        def _fail(*args, **kwargs):
            touched.append(what)
            raise AssertionError(f"{what} was reached despite a failed pre-flight")
        return _fail

    monkeypatch.setattr(converter_module, "OracleToPostgresConverter", forbidden("converter"))
    monkeypatch.setattr(fallback_module, "GeminiFallback", forbidden("GeminiFallback"))
    monkeypatch.setattr(llm_service, "_client", forbidden("Gemini client"))
    monkeypatch.setattr(llm_service, "generate", forbidden("llm_service.generate"))
    monkeypatch.setattr(SCRIPT, "run_repair_loop", forbidden("repair loop"))

    def run(name: str, *, gemini: bool, expected: str | None = None) -> int:
        monkeypatch.setattr(SCRIPT, "TARGET_DEPLOYER_URL", _url(name, DEPLOYER_ROLE))
        monkeypatch.setattr(SCRIPT, "TARGET_RUNNER_URL", _url(name, RUNNER_ROLE))
        monkeypatch.setattr(SCRIPT.settings, "TARGET_DATABASE_NAME", expected or name)
        args = types.SimpleNamespace(gemini=gemini, with_edge_cases=False, json=None)
        targets = [o for o in OBJECTS if o.name.lower() == "org_chart"]
        return SCRIPT.run_real(args, OBJECTS, {o.object_id: o for o in OBJECTS}, TABLES, targets, _NoOracle, psycopg)

    run.touched = touched
    return run


def test_db_a_failed_preflight_stops_before_the_converter_and_gemini(target, gated, capsys):
    _admin(target, "DROP TABLE employees CASCADE")
    assert gated(target, gemini=True) == SCRIPT.EXIT_NOT_RUN
    assert gated.touched == []
    out = capsys.readouterr().out
    assert "Nothing was converted and Gemini was not called" in out and "missing table(s) in public: employees" in out


def test_db_redis_down_with_gemini_stops_before_the_converter_and_gemini(target, gated, monkeypatch, capsys):
    monkeypatch.setattr(SCRIPT.settings, "REDIS_URL", "redis://localhost:1/0")
    assert gated(target, gemini=True) == SCRIPT.EXIT_NOT_RUN
    assert gated.touched == []
    assert "Redis at localhost:1 does not answer" in capsys.readouterr().out


def test_db_a_prepared_but_wrong_database_stops_before_the_converter_and_gemini(target, gated, capsys):
    """Both URLs point at a fully prepared database that is not the configured target (Aarsh, 17 Nov).

    The expected name comes from TARGET_DATABASE_NAME, not from the URLs, so this is caught."""
    assert problems(target) == []            # the database itself is ready in every other respect
    assert gated(target, gemini=True, expected="target") == SCRIPT.EXIT_NOT_RUN
    assert gated.touched == []
    out = capsys.readouterr().out
    assert f"the pv_deployer connection is on database {target}, not target" in out
    assert f"the pv_runner connection is on database {target}, not target" in out


def test_the_expected_database_is_configured_on_its_own():
    from app.config import Settings

    assert Settings(_env_file=None).TARGET_DATABASE_NAME == "target"
    source = (ROOT / "scripts" / "run_repair.py").read_text(encoding="utf-8")
    assert "expected_db = settings.TARGET_DATABASE_NAME" in source
    assert "conninfo_to_dict" not in source   # never derived from the URL being checked


def test_db_redis_is_not_required_without_gemini(target, gated, monkeypatch, capsys):
    """Without --gemini, a ready target passes the pre-flight even with Redis down; the run then starts."""
    monkeypatch.setattr(SCRIPT.settings, "REDIS_URL", "redis://localhost:1/0")
    with pytest.raises(AssertionError, match="converter was reached"):
        gated(target, gemini=False)
    assert "Pre-flight: OK" in capsys.readouterr().out and gated.touched == ["converter"]


# --- safeguard B: each object's Gemini calls are reported with that object's run only -----------

def test_db_each_runs_calls_appear_only_in_that_runs_report(target, monkeypatch, tmp_path, capsys):
    """Two objects, one Gemini call each: each run reports exactly its own call, in print and JSON."""
    import json

    import app.migration.converter as converter_module
    import app.migration.llm_fallback as fallback_module
    from app.contracts import ConversionTool
    from app.orchestration.repair_loop import AttemptRecord, RepairOutcome, RepairRun
    from app.services import llm_service

    def fake_loop(deps, obj, cases):
        llm_service._record(llm_service.CallRecord("gemini-test", 1, 1500, "ok", 120, 100, 50, "stored"))
        attempt = AttemptRecord(attempt=1, version=1, tool=ConversionTool.GEMINI, compiled_ok=False,
                                compile_error="x", failure_code="gemini.api_error")
        return RepairRun(object_id=obj.object_id, outcome=RepairOutcome.NEEDS_HUMAN, reason="fake",
                         attempts=[attempt], run_id=f"run-{obj.name}")

    built = []
    monkeypatch.setattr(converter_module, "OracleToPostgresConverter", lambda deployer, *a, **k: built.append(deployer))
    monkeypatch.setattr(fallback_module, "GeminiFallback", lambda *a, **k: object())
    monkeypatch.setattr(SCRIPT, "run_repair_loop", fake_loop)
    monkeypatch.setattr(SCRIPT, "redis_problem", lambda url: None)
    monkeypatch.setattr(SCRIPT, "TARGET_DEPLOYER_URL", _url(target, DEPLOYER_ROLE))
    monkeypatch.setattr(SCRIPT, "TARGET_RUNNER_URL", _url(target, RUNNER_ROLE))
    monkeypatch.setattr(SCRIPT.settings, "TARGET_DATABASE_NAME", target)
    report = tmp_path / "repair.json"
    args = types.SimpleNamespace(gemini=True, with_edge_cases=False, json=report)
    targets = [o for o in OBJECTS if o.name.lower() in ("org_chart", "get_emp_grade")]
    code = SCRIPT.run_real(args, OBJECTS, {o.object_id: o for o in OBJECTS}, TABLES, targets, _NoOracle, psycopg)

    assert code == SCRIPT.EXIT_NEEDS_HUMAN
    # The deployer is given the configured database name too, so it re-checks it on every deploy.
    assert [d.database for d in built] == [target]
    calls = json.loads((tmp_path / "repair.calls.json").read_text(encoding="utf-8"))
    assert [(c["object_id"].split(".")[-1], len(c["calls"])) for c in calls] == [
        (o.object_id.split(".")[-1], 1) for o in targets]
    assert all(c["calls"][0]["outcome"] == "ok" and c["calls"][0]["timeout_s"] == 120 for c in calls)
    out = capsys.readouterr().out
    assert "Gemini calls" in out and out.count("try 1      1.5 s  ok") == 2

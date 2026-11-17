"""Step 7d: the platform helper that lets a converted CONNECT BY view refuse like ORA-30004.

DEF-7c-1: a name containing the SYS_CONNECT_BY_PATH separator makes Oracle refuse org_chart
(ORA-30004), while the recorded Gemini view returned an ambiguous path. H3 (16 Nov) measured
exactly when Oracle refuses; ``h3_oracle_evidence.json`` keeps those 8 outcomes, and the tests
here hold PostgreSQL to each of them.

``REFERENCE_VIEW`` is the approved design from the 7d proposal (section 10), written by hand
to test the platform helper, the deployer and the planner behaviour it relies on. It is NOT
a repair: no product path deploys it, and DEF-7c-1 stays in KNOWN_NON_PASSES until a repair
from the repair route (Gemini's, recorded, or a human-written fallback Aarsh approves) passes
the strict replay. No test calls Gemini; the repair-loop test uses a fake that replays fixed
answers.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from app.contracts.models import (
    ExecutionError,
    ExecutionResult,
    TestCase,
    TestStatus,
    ValidationResult,
    ValidationStatus,
)
from app.migration.compat import (
    COMPAT_SCHEMA,
    PATH_CHECK_FUNCTION,
    PATH_SEPARATOR_MESSAGE,
    PATH_SEPARATOR_SQLSTATE,
    compat_report,
)
from app.migration.features import CONNECT_BY_PATH_HINT, Handling, detect_features
from app.migration.llm_fallback import ConversionRequest, build_prompt, eligibility
from app.migration.postgres_deployer import DeploymentRefused, DeployTarget, plan_deployment
from app.validation.comparator import compare
from app.validation.edge_catalogue import KNOWN_NON_PASSES
from app.validation.errors import ORACLE_CATEGORIES, POSTGRES_CATEGORIES, categorize
from app.validation.golden_tests import MASKED_COLUMNS, ORG_CHART, golden_tests_for

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = json.loads((Path(__file__).parent / "h3_oracle_evidence.json").read_text(encoding="utf-8"))
SCENARIOS = {s["id"]: s for s in EVIDENCE["scenarios"]}
#: H3 queries that do not request the path: Oracle answers them, so PostgreSQL must too.
NO_PATH = ("H3.2", "H3.6", "H3.7")
DEF_7C_1 = "gen-oc-hierarchy-cef703cf"
CATEGORY = "connect_by_path_separator"

#: The approved design (proposal section 10). For tests only; see the module docstring.
REFERENCE_VIEW = """CREATE OR REPLACE VIEW org_chart AS
WITH RECURSIVE tree AS (
  SELECT emp_id, full_name, manager_id, 1 AS depth, CAST(' > ' || full_name AS text) AS path,
         strpos(full_name, ' > ') > 0 AS has_sep
    FROM employees WHERE manager_id IS NULL
  UNION ALL
  SELECT e.emp_id, e.full_name, e.manager_id, t.depth + 1, t.path || ' > ' || e.full_name,
         strpos(e.full_name, ' > ') > 0
    FROM employees e JOIN tree t ON e.manager_id = t.emp_id
)
SELECT emp_id, full_name, manager_id, depth,
       pv_compat.connect_by_path_checked(path, bool_or(has_sep) OVER ()) AS reporting_line
  FROM tree;
"""
HELPER_OF_ITS_OWN = """CREATE OR REPLACE FUNCTION org_chart_check(p text, b boolean) RETURNS text
LANGUAGE plpgsql AS $$ BEGIN RETURN p; END $$;
"""
VIEW_TARGET = DeployTarget("view", "org_chart")


def _sample():
    from app.parsers.oracle_parser import parse_sources

    sources = {p.name: p.read_text(encoding="utf-8") for p in sorted((ROOT / "samples" / "oracle").glob("*.sql"))}
    return {o.object_id: o for o in parse_sources(sources)}


SAMPLE = _sample()


def _error(engine: str, code: str) -> ExecutionResult:
    return ExecutionResult(engine=engine, test_id="t-7d", error=ExecutionError(code=code, message="x"))


CASE = TestCase(test_id="t-7d", object_id=ORG_CHART, description="error mapping")


# --- 1. the recorded Oracle evidence ---------------------------------------------------------

def test_the_h3_evidence_holds_all_eight_outcomes_and_where_they_came_from():
    assert list(SCENARIOS) == [f"H3.{n}" for n in range(1, 9)]
    recorded = EVIDENCE["recorded"]
    assert recorded["oracle_version"] == "23.26.3.0.0" and "rolled back" in recorded["method"]
    assert "11 employees, no 914" in recorded["rollback_confirmed"]
    refused = {sid for sid, s in SCENARIOS.items() if "error" in s["oracle"]}
    assert refused == {"H3.1", "H3.3", "H3.5", "H3.8"}
    assert all(SCENARIOS[sid]["oracle"]["error"] == "ORA-30004" for sid in refused)
    assert all("reporting_line" not in SCENARIOS[s]["query"] and "*" not in SCENARIOS[s]["query"].split("(")[0]
               for s in NO_PATH)


# --- 2. the error mapping: ORA-30004 <-> PV001 only ------------------------------------------

def test_ora_30004_and_pv001_are_one_exact_pair():
    assert [k for k, v in ORACLE_CATEGORIES.items() if v == CATEGORY] == ["ORA-30004"]
    assert [k for k, v in POSTGRES_CATEGORIES.items() if v == CATEGORY] == [PATH_SEPARATOR_SQLSTATE] == ["PV001"]


def test_ora_30004_matches_pv001_and_nothing_else():
    passed = compare(CASE, _error("oracle", "ORA-30004"), _error("postgres", "PV001"))
    assert passed.status is TestStatus.PASSED
    for other in ("P0001", "22023", "22001", "23502"):
        assert compare(CASE, _error("oracle", "ORA-30004"), _error("postgres", other)).status is TestStatus.FAILED
    for oracle in ("ORA-06502", "ORA-20001", "ORA-01407", "ORA-01400"):
        assert compare(CASE, _error("oracle", oracle), _error("postgres", "PV001")).status is TestStatus.FAILED
    assert categorize("postgres", ExecutionError(code="P0001", message="x")) == "user_raised"  # unchanged


# --- 3. the hint Gemini receives ---------------------------------------------------------------

def test_the_hint_is_part_of_the_single_connect_by_finding():
    report = detect_features(SAMPLE[ORG_CHART].source_code)
    connect_by = [f for f in report.findings if f.feature == "CONNECT_BY"]
    assert len(connect_by) == 1 and report.features == {"CONNECT_BY"}
    assert connect_by[0].detail == f"hierarchical query (CONNECT BY); {CONNECT_BY_PATH_HINT}"
    assert "pv_compat.connect_by_path_checked(path, bool_or(has_sep) OVER ())" in CONNECT_BY_PATH_HINT
    assert "do not create any function" in CONNECT_BY_PATH_HINT
    gate = eligibility(report)
    assert gate.eligible and gate.reason == "Eligible for Gemini: CONNECT_BY"  # eligibility unchanged


def test_a_connect_by_without_sys_connect_by_path_gets_no_hint():
    report = detect_features("SELECT emp_id FROM employees START WITH manager_id IS NULL "
                             "CONNECT BY PRIOR emp_id = manager_id")
    assert [f.detail for f in report.findings if f.feature == "CONNECT_BY"] == ["hierarchical query (CONNECT BY)"]


def test_the_hint_reaches_the_gemini_prompt_without_touching_llm_fallback():
    org = SAMPLE[ORG_CHART]
    findings = tuple(f.detail for f in detect_features(org.source_code).by_handling(Handling.LLM))
    prompt = build_prompt(ConversionRequest(obj=org, target=VIEW_TARGET, findings=findings))
    assert f"- hierarchical query (CONNECT BY); {CONNECT_BY_PATH_HINT}" in prompt.user


# --- 4. the deployer: unchanged; a view may call the helper, never bring a function ---------

def test_the_deployer_accepts_the_view_that_calls_the_platform_helper():
    plan = plan_deployment(REFERENCE_VIEW, VIEW_TARGET)
    assert len(plan.statements) == 1 and "pv_compat.connect_by_path_checked" in plan.statements[0]


@pytest.mark.parametrize("code", [
    HELPER_OF_ITS_OWN + REFERENCE_VIEW,
    REFERENCE_VIEW + HELPER_OF_ITS_OWN,
    HELPER_OF_ITS_OWN.replace("org_chart_check", "pv_compat.connect_by_path_checked") + REFERENCE_VIEW,
    "CREATE SCHEMA IF NOT EXISTS pv_compat;\n" + REFERENCE_VIEW,
], ids=["own-function-first", "own-function-last", "replaces-the-helper", "creates-pv-compat"])
def test_the_deployer_still_refuses_a_view_that_brings_a_function_or_schema(code):
    with pytest.raises(DeploymentRefused, match="creates (a function|schema)"):
        plan_deployment(code, VIEW_TARGET)


# --- 5. run_repair.py --with-edge-cases --------------------------------------------------------

def _load_run_repair():
    spec = importlib.util.spec_from_file_location("run_repair_7d", ROOT / "scripts" / "run_repair.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_run_repair_can_add_an_objects_generated_cases():
    script = _load_run_repair()
    golden = [c.test_id for c in golden_tests_for(ORG_CHART)]
    assert [c.test_id for c in script.cases_for(ORG_CHART)] == golden
    with_edges = [c.test_id for c in script.cases_for(ORG_CHART, with_edge_cases=True)]
    assert with_edges[:len(golden)] == golden and DEF_7C_1 in with_edges
    assert len(with_edges) == len(golden) + 4 and all(t.startswith("gen-oc-") for t in with_edges[len(golden):])


# --- 6. PostgreSQL: the helper, its rights, and the 8 H3 scenarios ---------------------------

psycopg = pytest.importorskip("psycopg")
from app.migration.postgres_deployer import PostgresDeployer  # noqa: E402
from app.migration.target_roles import DEPLOYER_ROLE, RUNNER_ROLE, ensure_roles  # noqa: E402
from tests.target_pg import (  # noqa: E402
    DEPLOYER_PASSWORD,
    RUNNER_PASSWORD,
    _admin_connect,
    connect_as,
)

INSERT_914 = ("INSERT INTO employees (emp_id, full_name, email, dept_id, manager_id, hire_date, base_salary, "
              "perf_rating, status) VALUES (914, %s, NULL, 10, %s, DATE '2024-01-02', 50000, NULL, 'ACTIVE')")


@contextlib.contextmanager
def _sample_database():
    """A throwaway database with the sample schema and data, both roles and the platform helper."""
    try:
        server = _admin_connect()
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL for target tests not reachable ({type(exc).__name__})")
    name = f"pv_test_{uuid.uuid4().hex[:8]}"
    server.execute(f'CREATE DATABASE "{name}"')
    try:
        with _admin_connect(name) as admin:
            for path in ("01_schema.sql", "02_seed_data.sql"):
                admin.execute((ROOT / "samples" / "postgres" / path).read_text())
            admin.autocommit = False
            ensure_roles(admin, deployer_password=DEPLOYER_PASSWORD, runner_password=RUNNER_PASSWORD)
        with connect_as(name, DEPLOYER_ROLE) as deployer_conn, connect_as(name, RUNNER_ROLE) as runner_conn:
            yield name, deployer_conn, runner_conn
    finally:
        server.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                       "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
        server.execute(f'DROP DATABASE IF EXISTS "{name}"')
        server.close()


@pytest.fixture(scope="module")
def db():
    """The sample database with REFERENCE_VIEW deployed by the real deployer, as pv_deployer."""
    with _sample_database() as (name, deployer_conn, runner_conn):
        result = PostgresDeployer(deployer_conn, database=name).deploy(REFERENCE_VIEW, VIEW_TARGET)
        assert result.ok, result.error
        yield {"name": name, "deployer": deployer_conn, "runner": runner_conn}


def run_h3(runner, scenario) -> tuple[str, object]:
    """One H3 scenario as pv_runner: insert 914, run the query, always roll back."""
    try:
        runner.execute(INSERT_914, (scenario["name"], scenario["manager_id"]))
        rows = runner.execute(scenario["query"]).fetchall()
        return "rows", [tuple(int(v) if isinstance(v, Decimal) else v for v in row) for row in rows]
    except psycopg.Error as exc:
        return "error", exc.sqlstate
    finally:
        runner.rollback()


@pytest.mark.parametrize("sid", list(SCENARIOS))
def test_db_postgres_gives_oracles_h3_outcome(db, sid):
    scenario_id, scenario = sid, SCENARIOS[sid]
    oracle = scenario["oracle"]
    kind, value = run_h3(db["runner"], scenario)
    if "error" in oracle:
        assert kind == "error", f"{scenario_id}: Oracle raised {oracle['error']}, PostgreSQL returned {value}"
        assert categorize("postgres", ExecutionError(code=value, message="")) == \
            categorize("oracle", ExecutionError(code=oracle["error"], message="")) == CATEGORY
        return
    if kind == "error" and scenario_id in NO_PATH:
        pytest.fail(f"{scenario_id}: PostgreSQL raised {value} for a query that does not request the path. "
                    "The planner no longer prunes the unused reporting_line column (see compat.py, "
                    "'Limitation'): PostgreSQL is now stricter than Oracle here.")
    assert kind == "rows", f"{scenario_id}: Oracle returned rows, PostgreSQL raised {value}"
    assert len(value) == oracle["rows"]
    if "row_914" in oracle:
        assert [r[:len(oracle["row_914"])] for r in value if r[0] == 914] == [tuple(oracle["row_914"])]
    if "values" in oracle:
        assert value == [tuple(v) for v in oracle["values"]]


@pytest.mark.parametrize("sid", NO_PATH)
def test_db_the_planner_still_prunes_the_unrequested_path(db, sid):
    """Pins the optimisation H3.2, H3.6 and H3.7 rely on: the helper is not in their plans at all."""
    plan = "\n".join(r[0] for r in db["runner"].execute("EXPLAIN (VERBOSE, COSTS OFF) " + SCENARIOS[sid]["query"]))
    db["runner"].rollback()
    assert PATH_CHECK_FUNCTION not in plan, (
        f"{sid}: the plan evaluates {PATH_CHECK_FUNCTION} although the query does not request the path; "
        f"PostgreSQL stopped pruning unused view columns (compat.py, 'Limitation').\n{plan}")
    full = "\n".join(r[0] for r in db["runner"].execute("EXPLAIN (VERBOSE, COSTS OFF) SELECT * FROM org_chart"))
    db["runner"].rollback()
    assert PATH_CHECK_FUNCTION in full  # the control: requesting the path does evaluate it


def test_db_the_check_covers_the_whole_hierarchy_despite_an_outer_filter(db):
    """H3.3 and H3.8, plus filters on the clean rows only: any request for the path refuses."""
    for query in ("SELECT * FROM org_chart WHERE emp_id = 101",
                  "SELECT reporting_line FROM org_chart WHERE depth = 1",
                  "SELECT emp_id, reporting_line FROM org_chart WHERE emp_id <> 914",
                  "SELECT emp_id, reporting_line FROM org_chart WHERE emp_id = 914"):
        assert run_h3(db["runner"], {"name": "A > B", "manager_id": 101, "query": query}) == \
            ("error", PATH_SEPARATOR_SQLSTATE), query


def test_db_the_error_says_what_oracle_says(db):
    try:
        db["runner"].execute(INSERT_914, ("A > B", 101))
        with pytest.raises(psycopg.Error) as raised:
            db["runner"].execute("SELECT * FROM org_chart").fetchall()
    finally:
        db["runner"].rollback()
    assert raised.value.sqlstate == "PV001" and raised.value.diag.message_primary == PATH_SEPARATOR_MESSAGE


def test_db_h3_rows_are_rolled_back(db):
    for scenario in SCENARIOS.values():
        run_h3(db["runner"], scenario)
    count, total = db["runner"].execute(
        "SELECT count(*) FILTER (WHERE emp_id = 914), count(*) FROM employees").fetchone()
    db["runner"].rollback()
    assert (count, total) == (0, 11)


def test_db_seed_data_gives_the_same_eleven_rows_as_before(db):
    from app.validation.postgres_runner import run_postgres
    from tests.test_validation_7b import WATCH, judge

    for case in golden_tests_for(ORG_CHART):
        outcome = judge(case, run_postgres(db["runner"], SAMPLE[ORG_CHART], case, WATCH, SAMPLE))
        assert outcome.status is TestStatus.PASSED, outcome.diffs


def test_db_the_strict_def_7c_1_case_passes_with_the_design_but_stays_a_known_non_pass(db):
    from app.validation.postgres_runner import run_postgres
    from tests.test_edge_cases_7c import BY_ID, CORPUS, CORPUS_STALE
    from tests.test_validation_7b import WATCH

    assert CORPUS is not None and CORPUS_STALE is None, CORPUS_STALE  # the 150-case corpus, unchanged
    for test_id in [t for t in BY_ID if t.startswith("gen-oc-")]:
        case = BY_ID[test_id]
        pg = run_postgres(db["runner"], SAMPLE[ORG_CHART], case, WATCH, SAMPLE)
        outcome = compare(case, CORPUS.results[test_id], pg, ordered_rows=bool(case.order_by),
                          masked_columns=MASKED_COLUMNS)
        assert outcome.status is TestStatus.PASSED, (test_id, outcome.diffs)
    # Removed only when a view from the repair route passes the strict replay (Aarsh, 16 Nov).
    assert KNOWN_NON_PASSES[DEF_7C_1][0] == "translation_defect"


def test_db_the_helper_is_admin_owned_stable_invoker_and_pinned(db):
    with _admin_connect(db["name"]) as admin:
        facts = compat_report(admin)
        admin_user = admin.execute("SELECT current_user").fetchone()[0]
    assert facts["owner"] == facts["schema_owner"] == admin_user
    assert facts["volatility"] == "s" and facts["security_definer"] is False and facts["language"] == "plpgsql"
    assert facts["config"] == ["search_path=pg_catalog, pg_temp"]


def test_the_helper_reads_no_table():
    from app.migration import compat

    body = compat._FUNCTION_SQL.split("$fn$")[1].upper()
    assert not any(word in body for word in (" FROM ", "SELECT", "INSERT", "UPDATE", "DELETE", "EXECUTE "))


def test_db_only_the_two_roles_may_use_the_helper_and_neither_may_create_in_its_schema(db):
    nobody = f"pv_test_nobody_{uuid.uuid4().hex[:6]}"
    signature = f"{COMPAT_SCHEMA}.{PATH_CHECK_FUNCTION}(text, boolean)"
    with _admin_connect(db["name"]) as admin:
        admin.execute(f'CREATE ROLE "{nobody}" NOLOGIN')
        try:
            def can(role, kind, what, privilege):
                return admin.execute(f"SELECT has_{kind}_privilege(%s, %s, %s)", (role, what, privilege)).fetchone()[0]

            for role in (DEPLOYER_ROLE, RUNNER_ROLE):
                assert can(role, "function", signature, "EXECUTE") and can(role, "schema", COMPAT_SCHEMA, "USAGE")
                assert not can(role, "schema", COMPAT_SCHEMA, "CREATE")
            assert not can(nobody, "function", signature, "EXECUTE")   # PUBLIC has no EXECUTE
            assert not can(nobody, "schema", COMPAT_SCHEMA, "USAGE")
            assert not can(nobody, "schema", COMPAT_SCHEMA, "CREATE")
        finally:
            admin.execute(f'DROP ROLE "{nobody}"')


@pytest.mark.parametrize("role", [DEPLOYER_ROLE, RUNNER_ROLE])
@pytest.mark.parametrize("statement", [
    ("CREATE OR REPLACE FUNCTION pv_compat.connect_by_path_checked(p_path text, p_has_separator boolean) "
     "RETURNS text LANGUAGE sql STABLE AS $$ SELECT p_path $$"),
    "ALTER FUNCTION pv_compat.connect_by_path_checked(text, boolean) VOLATILE",
    "ALTER FUNCTION pv_compat.connect_by_path_checked(text, boolean) RENAME TO renamed",
    "DROP FUNCTION pv_compat.connect_by_path_checked(text, boolean)",
    "CREATE FUNCTION pv_compat.other() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$",
    "CREATE VIEW pv_compat.other AS SELECT 1 AS x",
    "ALTER SCHEMA pv_compat RENAME TO other",
    "DROP SCHEMA pv_compat CASCADE",
], ids=["replace", "make-volatile", "rename", "drop", "add-function", "add-view", "rename-schema", "drop-schema"])
def test_db_neither_role_can_replace_or_change_the_helper(db, role, statement):
    conn = db["deployer"] if role == DEPLOYER_ROLE else db["runner"]
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(statement)
    finally:
        conn.rollback()
    with _admin_connect(db["name"]) as admin:
        assert compat_report(admin)["volatility"] == "s"


@pytest.fixture(scope="module")
def plain_db():
    """A second database for the setup checks that change ownership on purpose."""
    with _sample_database() as (name, deployer_conn, runner_conn):
        yield name


def test_db_ensure_roles_reinstalls_the_helper_idempotently(plain_db):
    with _admin_connect(plain_db) as admin:
        before = compat_report(admin)
        admin.autocommit = False
        ensure_roles(admin, deployer_password=DEPLOYER_PASSWORD, runner_password=RUNNER_PASSWORD)
        assert compat_report(admin) == before


def test_db_setup_restores_the_exact_privileges_after_stray_grants(plain_db):
    """Aarsh's review of 7d: grants made by hand, to any role, do not survive the next setup."""
    from app.migration.compat import privilege_entries

    third, fourth = (f"pv_test_stray_{uuid.uuid4().hex[:6]}_{n}" for n in (3, 4))
    signature = f"{COMPAT_SCHEMA}.{PATH_CHECK_FUNCTION}(text, boolean)"
    intended = {"schema": {(DEPLOYER_ROLE, "USAGE", False), (RUNNER_ROLE, "USAGE", False)},
                "function": {(DEPLOYER_ROLE, "EXECUTE", False), (RUNNER_ROLE, "EXECUTE", False)}}
    with _admin_connect(plain_db) as admin:
        before = compat_report(admin)
        for role in (third, fourth):
            admin.execute(f'CREATE ROLE "{role}" NOLOGIN')
        try:
            # 1-2. a third role gets USAGE, CREATE and EXECUTE, with grant options; the designated
            # roles are loosened; PUBLIC gets EXECUTE; and the third role grants on to a fourth.
            admin.execute(f'GRANT USAGE, CREATE ON SCHEMA pv_compat TO "{third}" WITH GRANT OPTION')
            admin.execute(f'GRANT EXECUTE ON FUNCTION {signature} TO "{third}" WITH GRANT OPTION')
            admin.execute(f"GRANT CREATE ON SCHEMA pv_compat TO {DEPLOYER_ROLE}")
            admin.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {RUNNER_ROLE} WITH GRANT OPTION")
            admin.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO PUBLIC")
            admin.execute(f'SET ROLE "{third}"')
            admin.execute(f'GRANT USAGE ON SCHEMA pv_compat TO "{fourth}"')
            admin.execute(f'GRANT EXECUTE ON FUNCTION {signature} TO "{fourth}"')
            admin.execute("RESET ROLE")
            with admin.cursor() as cur:
                assert (third, "CREATE", True) in privilege_entries(cur, "schema")      # really granted
                assert (fourth, "EXECUTE", False) in privilege_entries(cur, "function")
                assert ("PUBLIC", "EXECUTE", False) in privilege_entries(cur, "function")

            # 3. setup again, as admin
            admin.autocommit = False
            ensure_roles(admin, deployer_password=DEPLOYER_PASSWORD, runner_password=RUNNER_PASSWORD)
            admin.commit()
            admin.autocommit = True

            def can(role, kind, what, privilege):
                return admin.execute(f"SELECT has_{kind}_privilege(%s, %s, %s)", (role, what, privilege)).fetchone()[0]

            # 4. the third and fourth roles have none of it
            for role in (third, fourth):
                assert not can(role, "schema", COMPAT_SCHEMA, "USAGE")
                assert not can(role, "schema", COMPAT_SCHEMA, "CREATE")
                assert not can(role, "function", signature, "EXECUTE")
            # 5. the two roles have exactly what they need; no other entry survives, PUBLIC included
            with admin.cursor() as cur:
                assert set(privilege_entries(cur, "schema")) == intended["schema"]
                assert set(privilege_entries(cur, "function")) == intended["function"]
            for role in (DEPLOYER_ROLE, RUNNER_ROLE):
                assert can(role, "schema", COMPAT_SCHEMA, "USAGE") and can(role, "function", signature, "EXECUTE")
                assert not can(role, "schema", COMPAT_SCHEMA, "CREATE")
            # 6. ownership, volatility and the rest are as before
            assert compat_report(admin) == before
        finally:
            admin.rollback()
            admin.autocommit = True
            admin.execute("RESET ROLE")
            for role in (fourth, third):
                admin.execute(f'DROP OWNED BY "{role}"')
                admin.execute(f'DROP ROLE "{role}"')


def test_db_the_final_privilege_check_refuses_if_a_stray_grant_survived(plain_db, monkeypatch):
    """Defence in depth: if revoking ever missed a role, setup refuses instead of committing."""
    from app.migration import compat

    stray = f"pv_test_stray_{uuid.uuid4().hex[:6]}"
    with _admin_connect(plain_db) as admin:
        admin.execute(f'CREATE ROLE "{stray}" NOLOGIN')
        try:
            admin.execute(f'GRANT USAGE ON SCHEMA pv_compat TO "{stray}"')
            monkeypatch.setattr(compat, "_grantees", lambda cur, kind: [])   # simulate a revocation bug
            admin.autocommit = False
            with pytest.raises(PermissionError, match="schema privileges are .* expected"):
                ensure_roles(admin, deployer_password=DEPLOYER_PASSWORD, runner_password=RUNNER_PASSWORD)
            admin.rollback()
        finally:
            admin.rollback()
            admin.autocommit = True
            admin.execute(f'DROP OWNED BY "{stray}"')
            admin.execute(f'DROP ROLE "{stray}"')


@pytest.mark.parametrize("hijack", [
    "ALTER SCHEMA pv_compat OWNER TO pv_deployer",
    ("CREATE FUNCTION pv_compat.planted() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$; "
     "ALTER FUNCTION pv_compat.planted() OWNER TO pv_deployer"),
], ids=["schema-owned-by-deployer", "function-owned-by-deployer"])
def test_db_setup_refuses_a_helper_schema_another_role_could_change(plain_db, hijack):
    with _admin_connect(plain_db) as admin:
        admin.execute(hijack)
        try:
            admin.autocommit = False
            with pytest.raises(PermissionError, match="owned by"):
                ensure_roles(admin, deployer_password=DEPLOYER_PASSWORD, runner_password=RUNNER_PASSWORD)
            admin.rollback()
        finally:
            admin.autocommit = True
            admin.execute("ALTER SCHEMA pv_compat OWNER TO CURRENT_USER")
            admin.execute("DROP FUNCTION IF EXISTS pv_compat.planted()")


# --- 7. the repair loop with the hint, on a fake Gemini (no live call) ------------------------

def test_db_the_repair_loop_fixes_def_7c_1_when_the_answer_uses_the_helper():
    """Attempt 1: the recorded 7b view (DEF-7c-1 fails). Attempt 2: the approved design (passes).

    The fake replays fixed answers; it shows the route works end to end, not what live Gemini
    would write. That is the one paid run, which needs Aarsh's explicit approval.
    """
    from app.migration.converter import OracleToPostgresConverter
    from app.migration.llm_fallback import GeminiFallback
    from app.orchestration.repair_loop import RepairDeps, RepairOutcome, run_repair_loop
    from app.validation.postgres_runner import run_postgres
    from tests.test_converter_gemini import ORG_SQL, answer
    from tests.test_edge_cases_7c import BY_ID, CORPUS
    from tests.test_repair_7a import SequenceGenerate
    from tests.test_validation_7b import WATCH, judge

    cases = _load_run_repair().cases_for(ORG_CHART, with_edge_cases=True)
    with _sample_database() as (name, deployer_conn, runner_conn):
        def validate(obj, tests, version):
            outcomes = []
            for case in tests:
                pg = run_postgres(runner_conn, SAMPLE[ORG_CHART], case, WATCH, SAMPLE)
                outcomes.append(compare(case, CORPUS.results[case.test_id], pg, ordered_rows=bool(case.order_by),
                                        masked_columns=MASKED_COLUMNS)
                                if case.test_id in BY_ID else judge(case, pg))
            failed = sum(o.status is TestStatus.FAILED for o in outcomes)
            return ValidationResult(object_id=obj.object_id, conversion_version=version, tests=outcomes,
                                    passed=len(outcomes) - failed, failed=failed,
                                    status=ValidationStatus.REQUIRES_REPAIR if failed else ValidationStatus.PASSED)

        fake = SequenceGenerate(answer(ORG_SQL), answer(REFERENCE_VIEW))
        converter = OracleToPostgresConverter(PostgresDeployer(deployer_conn, database=name), SAMPLE.values(),
                                              llm=GeminiFallback(fake))
        run = run_repair_loop(RepairDeps(converter=converter, validate=validate), SAMPLE[ORG_CHART], cases)

    first, last = run.attempts[0], run.attempts[-1]
    assert len(run.attempts) == 2 and run.outcome is RepairOutcome.PASSED_NEEDS_REVIEW  # Gemini output: sign-off
    assert first.status is ValidationStatus.REQUIRES_REPAIR and first.failing_tests == [DEF_7C_1]
    assert last.status is ValidationStatus.PASSED and last.passed == len(cases) and last.failed == 0
    assert run.final_validation.passed == len(cases)
    first_prompt, repair_prompt = fake.calls[0][0], fake.calls[1][0]
    assert CONNECT_BY_PATH_HINT in first_prompt and CONNECT_BY_PATH_HINT in repair_prompt
    assert "Oracle error: ORA-30004" in repair_prompt and DEF_7C_1 in repair_prompt

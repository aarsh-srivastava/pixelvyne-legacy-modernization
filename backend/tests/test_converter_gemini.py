"""The Gemini route of the converter (Stage 2, step 6b-2).

Every test injects a recording fake ``generate``: no test calls Gemini. Unit tests use a
recording fake deployer; the PostgreSQL tests at the end use the real restricted deployer
(``pv_deployer``) and the real runner role (``pv_runner``) on a throwaway database.

What these tests pin down (the 6b-A design, with Aarsh's clarifications A and B):

* Without ``llm`` nothing changes; with ``llm`` every non-Gemini object is unchanged too.
* Only eligible LLM-only objects reach Gemini, on their first attempt only, once.
* REVIEW objects, translator gaps and deterministic failures never reach Gemini.
* Every Gemini-route result: ``tool=GEMINI``, ``needs_review=True``; ``model`` only from
  settings and only when Gemini was asked.
* Gemini's SQL reaches the database only through the injected deployer, after 6a's checks;
  every rejection is a failed result needing review; ``compiled_ok=True`` only after commit.
* A failed replacement leaves the previously deployed object untouched.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from app.config import settings
from app.contracts.models import ConversionTool, FailureReport, ObjectType, ValidationResult, ValidationStatus
from app.migration.converter import OracleToPostgresConverter
from app.migration.llm_fallback import PROMPT_VERSION, GeminiFallback
from app.migration.plsql_translator import translate_plsql
from app.migration.postgres_deployer import DeployResult, DeployTarget, PostgresDeployer
from app.migration.target_roles import DEPLOYER_ROLE, RUNNER_ROLE, ensure_roles
from app.orchestration.repair_loop import RepairDeps, RepairOutcome, run_repair_loop
from app.parsers.oracle_parser import parse_sources

ROOT = Path(__file__).resolve().parents[2]
MODEL = "gemini-test-model-6b2"
ORG = "view:legacy.org_chart"

# Extra objects for the cases the sample does not have. Each is parsed with the sample,
# so it gets real feature detection, IR and contexts.
EXTRA = """
CREATE OR REPLACE PROCEDURE dyn_sql AS BEGIN EXECUTE IMMEDIATE 'DELETE FROM employees'; END;
/
CREATE OR REPLACE PROCEDURE dyn_and_print AS BEGIN DBMS_OUTPUT.PUT_LINE('x');
  EXECUTE IMMEDIATE 'SELECT 1 FROM dual'; END;
/
CREATE OR REPLACE PROCEDURE bad_call AS BEGIN mystery_proc(1); END;
/
CREATE OR REPLACE FUNCTION say_hi RETURN NUMBER AS BEGIN DBMS_OUTPUT.PUT_LINE('hi'); RETURN 1; END;
/
CREATE OR REPLACE TRIGGER emp_print_trg AFTER INSERT ON employees FOR EACH ROW
BEGIN DBMS_OUTPUT.PUT_LINE('new employee'); END;
/
CREATE OR REPLACE TRIGGER ghost_trg AFTER INSERT ON ghost_table FOR EACH ROW
BEGIN DBMS_OUTPUT.PUT_LINE('ghost'); END;
/
"""

# What a correct Gemini answer for org_chart looks like (hand-written for the tests: a
# recursive CTE reproducing LEVEL and SYS_CONNECT_BY_PATH, see EXPECTED.md oc-01).
ORG_SQL = (
    "CREATE OR REPLACE VIEW org_chart AS\n"
    "WITH RECURSIVE tree AS (\n"
    "  SELECT emp_id, full_name, manager_id, 1 AS depth, CAST(' > ' || full_name AS text) AS reporting_line\n"
    "    FROM employees WHERE manager_id IS NULL\n"
    "  UNION ALL\n"
    "  SELECT e.emp_id, e.full_name, e.manager_id, t.depth + 1, t.reporting_line || ' > ' || e.full_name\n"
    "    FROM employees e JOIN tree t ON e.manager_id = t.emp_id\n"
    ")\n"
    "SELECT emp_id, full_name, manager_id, depth, reporting_line FROM tree;\n"
)
SAY_HI_SQL = (
    "CREATE OR REPLACE FUNCTION say_hi() RETURNS numeric LANGUAGE plpgsql AS $$\n"
    "BEGIN\n  RAISE NOTICE 'hi';\n  RETURN 1;\nEND;\n$$;\n"
)


def answer(sql: str = ORG_SQL, **overrides) -> str:
    data = {"postgresql": sql, "explanation": "Recursive CTE replaces CONNECT BY.", "warnings": [],
            "unsupported": [], "requires_human_review": False}
    data.update(overrides)
    return json.dumps(data)


class FakeGenerate:
    def __init__(self, reply: str | Exception = ""):
        self.reply = reply
        self.calls: list[tuple[str, str | None]] = []

    def __call__(self, prompt, *, system=None):
        self.calls.append((prompt, system))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class FakeDeployer:
    def __init__(self, result: DeployResult | None = None):
        self.result = result or DeployResult(ok=True)
        self.calls: list[tuple[str, DeployTarget]] = []

    def deploy(self, code, target):
        self.calls.append((code, target))
        return self.result


@pytest.fixture(autouse=True)
def configured_model(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_MODEL", MODEL)


@pytest.fixture(scope="module")
def objects():
    sources = {p.name: p.read_text() for p in sorted((ROOT / "samples" / "oracle").glob("*.sql"))}
    sources["99_extra.sql"] = EXTRA
    return {o.object_id: o for o in parse_sources(sources)}


def make(objects, reply="", deployer=None, llm=True):
    fake = FakeGenerate(reply)
    deployer = deployer or FakeDeployer()
    converter = OracleToPostgresConverter(deployer, objects.values(), llm=GeminiFallback(fake) if llm else None)
    return converter, fake, deployer


def comparable(result) -> dict:
    return result.model_dump(exclude={"produced_at"})


SAMPLE_NON_LLM = [
    "function:legacy.get_emp_grade", "function:legacy.top_earners", "procedure:legacy.calc_bonus",
    "procedure:legacy.apply_raise", "procedure:legacy.monthly_payroll", "function:legacy.payroll_pkg.net_pay",
    "procedure:legacy.payroll_pkg.close_month", "trigger:legacy.emp_salary_audit_trg",
    "table:legacy.employees", "sequence:legacy.salary_audit_seq", "package:legacy.payroll_pkg",
    "procedure:legacy.dyn_sql", "procedure:legacy.bad_call",
]


# --- T1, T11: the deterministic path is unchanged ----------------------------------------

@pytest.mark.parametrize("object_id", SAMPLE_NON_LLM)
def test_non_llm_objects_are_identical_with_and_without_gemini(objects, object_id):
    plain, _, plain_deployer = make(objects, llm=False)
    with_llm, fake, llm_deployer = make(objects, reply=answer())
    a, b = plain.convert(objects[object_id]), with_llm.convert(objects[object_id])
    assert comparable(a) == comparable(b)
    assert fake.calls == []
    assert plain_deployer.calls == llm_deployer.calls
    assert b.tool is not ConversionTool.GEMINI and b.model is None


def test_all_eight_sample_routines_stay_deterministic_with_zero_calls(objects):
    converter, fake, deployer = make(objects, reply=answer())
    routines = SAMPLE_NON_LLM[:8]
    results = [converter.convert(objects[k]) for k in routines]
    assert fake.calls == []
    assert all(r.compiled_ok and r.tool is ConversionTool.SQLGLOT and r.model is None for r in results)
    assert len(deployer.calls) == 8


def test_without_llm_the_llm_only_message_is_unchanged(objects):
    result = make(objects, llm=False)[0].convert(objects[ORG])
    assert result.tool is ConversionTool.SQLGLOT and result.model is None and not result.needs_review
    assert result.compile_error.startswith("No deterministic rule for:") and "step 6" in result.compile_error


# --- T2, T10: an eligible object, success and provenance ---------------------------------

def test_org_chart_is_converted_by_gemini_with_exactly_one_call(objects):
    converter, fake, deployer = make(objects, reply=answer(warnings=["LEVEL starts at 1"]))
    result = converter.convert(objects[ORG])
    assert len(fake.calls) == 1
    prompt, system = fake.calls[0]
    assert "Object ID: view:legacy.org_chart" in prompt and "CONNECT BY PRIOR emp_id = manager_id" in prompt
    assert PROMPT_VERSION in system
    assert deployer.calls == [(ORG_SQL, DeployTarget("view", "org_chart"))]
    assert result.compiled_ok and result.compile_error is None
    assert result.tool is ConversionTool.GEMINI and result.model == MODEL and result.needs_review
    assert result.target_code == ORG_SQL and result.version == 1
    assert result.warnings == [
        f"Generated by Gemini (model {MODEL}, prompt {PROMPT_VERSION}): human sign-off required",
        "Gemini explanation: Recursive CTE replaces CONNECT BY.",
        "Gemini warning: LEVEL starts at 1",
    ]


def test_model_comes_from_settings_never_from_the_answer(objects, monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_MODEL", "configured-model")
    text = answer(explanation="I am gemini-9-ultra.", warnings=["model: gemini-9-ultra"])
    result = make(objects, reply=text)[0].convert(objects[ORG])
    assert result.model == "configured-model"
    claimed = answer()[:-1] + ', "model": "gemini-9-ultra"}'  # a model name inside the answer itself
    refused = make(objects, reply=claimed)[0].convert(objects[ORG])
    assert refused.model == "configured-model" and "unexpected key(s): model" in refused.compile_error


def test_no_deterministic_partial_code_is_sent(objects):
    converter, fake, _ = make(objects, reply=answer())
    converter.convert(objects[ORG])
    prompt = fake.calls[0][0]
    assert "Deterministic translation" not in prompt and "could not convert" not in prompt


# --- T3, T4: never called -------------------------------------------------------------

@pytest.mark.parametrize("object_id", ["procedure:legacy.dyn_sql", "procedure:legacy.dyn_and_print"])
def test_review_objects_never_reach_gemini(objects, object_id):
    converter, fake, deployer = make(objects, reply=answer())
    result = converter.convert(objects[object_id])
    assert fake.calls == [] and deployer.calls == []
    assert result.compile_error.startswith("Unsupported:") and result.needs_review
    assert result.tool is ConversionTool.SQLGLOT and result.model is None


def test_translator_gaps_never_reach_gemini(objects):
    converter, fake, deployer = make(objects, reply=answer())
    result = converter.convert(objects["procedure:legacy.bad_call"])
    assert fake.calls == [] and deployer.calls == []
    # PR 1 (G3): caught by detection (review), so it never reaches the translator or Gemini.
    assert result.compile_error == "Unsupported: function MYSTERY_PROC is not on the allow-list"
    assert result.tool is ConversionTool.SQLGLOT


def test_a_failed_deterministic_deploy_never_reaches_gemini(objects):
    failed = FakeDeployer(DeployResult(ok=False, error="42601: syntax error", sqlstate="42601"))
    converter, fake, _ = make(objects, reply=answer(), deployer=failed)
    result = converter.convert(objects["procedure:legacy.calc_bonus"])
    assert fake.calls == [] and result.tool is ConversionTool.SQLGLOT and result.compile_error == "42601: syntax error"


def test_a_trigger_whose_table_is_unknown_is_not_eligible(objects):
    converter, fake, deployer = make(objects, reply=answer())
    result = converter.convert(objects["trigger:legacy.ghost_trg"])
    assert fake.calls == [] and deployer.calls == []
    assert result.compile_error == "Gemini not_eligible: the trigger's table is not in the upload (no TriggerInfo)"
    assert result.tool is ConversionTool.GEMINI and result.needs_review and result.model is None


def test_an_unlisted_llm_feature_is_not_eligible(objects, monkeypatch):
    import app.migration.llm_fallback as lf

    monkeypatch.setattr(lf, "ELIGIBLE_FEATURES", lf.ELIGIBLE_FEATURES - {"CONNECT_BY"})
    converter, fake, deployer = make(objects, reply=answer())
    result = converter.convert(objects[ORG])
    assert fake.calls == [] and deployer.calls == []
    assert result.compile_error == "Gemini not_eligible: Not eligible for Gemini: feature CONNECT_BY"


def test_nothing_is_built_for_an_ineligible_object(objects, monkeypatch):
    """The converter's own eligibility check runs first: no target, no request, no call."""
    import app.migration.llm_fallback as lf

    monkeypatch.setattr(lf, "ELIGIBLE_FEATURES", frozenset())
    converter, fake, deployer = make(objects, reply=answer())
    built = []
    monkeypatch.setattr(converter, "_target_for", lambda obj: built.append("target") or (None, "x"))
    monkeypatch.setattr(converter, "_request_for", lambda *a: built.append("request"))
    result = converter.convert(objects[ORG])
    assert built == [] and fake.calls == [] and deployer.calls == []
    assert result.compile_error.startswith("Gemini not_eligible: Not eligible for Gemini: feature CONNECT_BY")


# --- T5, T12: repairs are not part of 6b -------------------------------------------------

def test_a_repair_attempt_never_calls_gemini(objects):
    converter, fake, deployer = make(objects, reply=answer())
    first = converter.convert(objects[ORG])
    feedback = FailureReport(object_id=ORG, attempt=1, failing_tests=[], summary="oc-01: 10 rows vs 11")
    retry = converter.convert(objects[ORG], feedback)
    assert len(fake.calls) == 1 and len(deployer.calls) == 1  # only the first attempt
    assert first.version == 1 and retry.version == 2
    assert retry.compile_error.startswith("Gemini repair_unavailable:") and "D-6.7" in retry.compile_error
    assert "oc-01: 10 rows vs 11" in retry.compile_error
    assert retry.tool is ConversionTool.GEMINI and retry.needs_review and retry.model is None
    assert retry.target_code == "" and not retry.compiled_ok


@pytest.mark.parametrize("object_id", ["procedure:legacy.calc_bonus", "procedure:legacy.bad_call"])
def test_a_repair_of_a_deterministic_object_is_unchanged_by_gemini(objects, object_id):
    feedback = FailureReport(object_id=object_id, attempt=1, failing_tests=[], summary="cb-02: 24000 vs 6000")
    plain = make(objects, llm=False)[0].convert(objects[object_id], feedback)
    converter, fake, deployer = make(objects, reply=answer())
    with_llm = converter.convert(objects[object_id], feedback)
    assert comparable(plain) == comparable(with_llm)
    assert with_llm.tool is ConversionTool.SQLGLOT and fake.calls == [] and deployer.calls == []


def test_the_repair_loop_stops_when_gemini_repeats_rejected_sql(objects):
    """7a: a rejected deploy is repaired once; the same SQL again is unchanged output and stops."""
    failed = FakeDeployer(DeployResult(ok=False, error="42703: column does not exist", sqlstate="42703"))
    converter, fake, deployer = make(objects, reply=answer(), deployer=failed)

    def validate(obj, cases, version) -> ValidationResult:  # never reached: nothing compiles
        raise AssertionError("validate must not run")

    run = run_repair_loop(RepairDeps(converter=converter, validate=validate), objects[ORG], [])
    assert len(fake.calls) == 2 and len(deployer.calls) == 1  # the unchanged repair is never deployed
    assert run.outcome is RepairOutcome.NEEDS_HUMAN and len(run.attempts) == 2
    assert [a.failure_code for a in run.attempts] == ["gemini.deploy_failed", "gemini.unchanged_output"]
    assert [a.tool for a in run.attempts] == [ConversionTool.GEMINI] * 2


def test_a_passing_gemini_object_reports_passed_needs_review(objects):
    converter, fake, _ = make(objects, reply=answer())

    def validate(obj, cases, version) -> ValidationResult:
        return ValidationResult(object_id=obj.object_id, conversion_version=version, tests=[], passed=0, failed=0,
                                status=ValidationStatus.PASSED)

    run = run_repair_loop(RepairDeps(converter=converter, validate=validate), objects[ORG], [])
    assert run.outcome is RepairOutcome.PASSED_NEEDS_REVIEW and len(fake.calls) == 1


# --- T6, T7, T9: failures are safe and need review ---------------------------------------

@pytest.mark.parametrize("reply, code", [
    ("Sure! Here it is: " + answer(), "malformed_response"),
    (f"```json\n{answer()}\n```\n```sql\nSELECT 1;\n```", "malformed_response"),
    (answer(unsupported=["SYS_CONNECT_BY_PATH ordering"]), "unsupported"),
    (answer(requires_human_review=True), "review_requested"),
])
def test_rejected_answers_never_reach_the_deployer(objects, reply, code):
    converter, fake, deployer = make(objects, reply=reply)
    result = converter.convert(objects[ORG])
    assert len(fake.calls) == 1 and deployer.calls == []
    assert result.compile_error.startswith(f"Gemini {code}:")
    assert not result.compiled_ok and result.needs_review and result.tool is ConversionTool.GEMINI
    assert result.model == MODEL and result.target_code == ""


class LLMNotConfigured(RuntimeError):
    pass


class RedisError(Exception):
    pass


class APIError(Exception):
    def __init__(self, code):
        super().__init__(f"{code} quota exceeded for key AIzaSECRET-0000")
        self.code = code


@pytest.mark.parametrize("exc, code", [
    (LLMNotConfigured("GEMINI_API_KEY is empty"), "not_configured"),
    (APIError(503), "api_error"),
    (RedisError("redis://:pw@cache refused"), "cache_error"),
])
def test_service_failures_are_safe_failed_results(objects, exc, code):
    converter, fake, deployer = make(objects, reply=exc)
    result = converter.convert(objects[ORG])  # must not raise
    assert len(fake.calls) == 1 and deployer.calls == []
    assert result.compile_error.startswith(f"Gemini {code}: Gemini call failed (")
    assert "AIzaSECRET" not in result.model_dump_json() and "pw@" not in result.model_dump_json()
    assert result.needs_review and not result.compiled_ok and result.model == MODEL


def test_security_definer_is_refused_before_the_deployer(objects):
    definer = SAY_HI_SQL.replace("LANGUAGE plpgsql", "LANGUAGE plpgsql SECURITY DEFINER")
    converter, fake, deployer = make(objects, reply=answer(definer))
    result = converter.convert(objects["function:legacy.say_hi"])
    assert len(fake.calls) == 1 and deployer.calls == []
    assert result.compile_error.startswith("Gemini refused: Refused:")
    assert result.target_code == definer  # kept for review, never run
    assert result.needs_review and not result.compiled_ok and result.tool is ConversionTool.GEMINI


def test_sql_for_a_different_object_is_refused_before_the_deployer(objects):
    converter, fake, deployer = make(objects, reply=answer(ORG_SQL.replace("VIEW org_chart", "VIEW other_view")))
    result = converter.convert(objects[ORG])
    assert deployer.calls == [] and result.compile_error.startswith("Gemini refused:")


@pytest.mark.parametrize("deploy_result, code", [
    (DeployResult(ok=False, error="Refused: the connection has an open transaction", refused=True), "deploy_refused"),
    (DeployResult(ok=False, error="42703: column \"x\" does not exist", sqlstate="42703"), "deploy_failed"),
])
def test_deployer_rejection_is_a_failed_gemini_result_needing_review(objects, deploy_result, code):
    """Clarification A: never an ordinary retryable compile failure."""
    converter, fake, deployer = make(objects, reply=answer(), deployer=FakeDeployer(deploy_result))
    result = converter.convert(objects[ORG])
    assert len(deployer.calls) == 1
    assert result.compile_error == f"Gemini {code}: {deploy_result.error}"
    assert not result.compiled_ok and result.needs_review and result.tool is ConversionTool.GEMINI
    assert result.target_code == ORG_SQL and result.model == MODEL


def test_a_deployer_exception_is_a_failed_result_not_a_crash(objects):
    class Boom:
        def deploy(self, code, target):
            raise RuntimeError("connection lost")

    converter = OracleToPostgresConverter(Boom(), objects.values(), llm=GeminiFallback(FakeGenerate(answer())))
    result = converter.convert(objects[ORG])
    assert result.compile_error == "Gemini deploy_failed: Deployer error: RuntimeError: connection lost"
    assert result.needs_review and not result.compiled_ok


def test_an_unexpected_error_on_the_gemini_route_stays_a_gemini_result(objects, monkeypatch):
    converter, fake, deployer = make(objects, reply=answer())
    monkeypatch.setattr(converter, "_request_for", lambda *a: (_ for _ in ()).throw(KeyError("boom")))
    result = converter.convert(objects[ORG])
    assert result.compile_error == "Gemini converter_error: KeyError: 'boom'"
    assert result.tool is ConversionTool.GEMINI and result.needs_review and not result.compiled_ok
    assert fake.calls == [] and deployer.calls == [] and result.model is None


def test_the_warning_and_the_field_name_the_same_model(objects, monkeypatch):
    calls = iter(["first-read", "second-read"])
    monkeypatch.setattr(type(settings), "GEMINI_MODEL", property(lambda self: next(calls)), raising=False)
    result = make(objects, reply=answer())[0].convert(objects[ORG])
    assert result.model == "first-read" and "model first-read," in result.warnings[0]


def test_every_gemini_route_result_needs_review(objects):
    """D-6b.3 / clarification B, across every way the route can end."""
    outcomes = []
    for reply in (answer(), "nonsense", answer(requires_human_review=True), LLMNotConfigured("x")):
        outcomes.append(make(objects, reply=reply)[0].convert(objects[ORG]))
    outcomes.append(make(objects, reply=answer(), deployer=FakeDeployer(DeployResult(ok=False, error="e")))[0]
                    .convert(objects[ORG]))
    outcomes.append(make(objects, reply=answer())[0].convert(objects["trigger:legacy.ghost_trg"]))
    assert all(r.tool is ConversionTool.GEMINI and r.needs_review for r in outcomes)
    assert [r.compiled_ok for r in outcomes] == [True, False, False, False, False, False]


# --- T14, T15: what goes into the request -----------------------------------------------

def test_targets_match_the_translators_names(objects):
    """_target_for() must name every routine exactly as the deterministic translator does."""
    converter, *_ = make(objects)
    checked = set()
    for obj in objects.values():
        if obj.type not in (ObjectType.FUNCTION, ObjectType.PROCEDURE, ObjectType.TRIGGER):
            continue
        translated = translate_plsql(obj.source_code, kind=obj.type.value, name=obj.name,
                                     context=converter._contexts[obj.object_id])
        if translated.target is None:
            continue
        assert converter._target_for(obj) == (translated.target, None), obj.object_id
        checked.add(obj.object_id)
    # Every routine the translator can name; LLM-only extras have no translation, hence Gemini.
    assert checked == set(SAMPLE_NON_LLM[:8])


def test_a_package_member_and_a_trigger_get_the_right_targets(objects):
    converter, *_ = make(objects)
    assert converter._target_for(objects["function:legacy.payroll_pkg.net_pay"])[0] == DeployTarget(
        "function", "net_pay", schema="payroll_pkg")
    assert converter._target_for(objects["trigger:legacy.emp_print_trg"])[0] == DeployTarget(
        "trigger", "emp_print_trg", trigger_table="employees", trigger_function="emp_print_trg_fn")


def test_the_request_holds_only_ir_facts(objects):
    converter, *_ = make(objects)
    org = objects[ORG]
    from app.migration.features import detect_features

    report = detect_features(org.source_code)
    request = converter._request_for(org, report, DeployTarget("view", "org_chart"))
    assert request.findings and all("CONNECT BY" in f or "SYS_CONNECT_BY_PATH" in f for f in request.findings)
    assert request.dependencies == ()  # employees is a table: described by column types, not a signature
    assert request.column_types and all(k.startswith("employees.") for k in request.column_types)
    assert request.deterministic_code is None and request.deterministic_gaps == ()

    trg = objects["trigger:legacy.emp_salary_audit_trg"]
    trg_request = converter._request_for(trg, detect_features(trg.source_code), DeployTarget(
        "trigger", trg.name, trigger_table="employees", trigger_function=f"{trg.name}_fn"))
    assert [d.object_id for d in trg_request.dependencies] == ["sequence:legacy.salary_audit_seq"]
    assert {k.split(".")[0] for k in trg_request.column_types} == {"employees", "salary_audit"}


def test_a_secret_in_the_source_is_never_sent(objects, monkeypatch):
    monkeypatch.setattr(settings, "ORACLE_PASSWORD", "org-pw-77")
    leaky = objects[ORG].model_copy(update={"source_code": objects[ORG].source_code + "\n-- login org-pw-77\n"})
    converter, fake, deployer = make({**objects, ORG: leaky}, reply=answer())
    result = converter.convert(leaky)
    assert fake.calls == [] and deployer.calls == []
    assert result.compile_error.startswith("Gemini secret_in_source: Needs review:")
    assert "org-pw-77" not in result.model_dump_json()
    assert result.needs_review and result.model is None  # never asked


# --- PostgreSQL: the real restricted deployer ------------------------------------------

psycopg = pytest.importorskip("psycopg")
from tests.target_pg import DEPLOYER_PASSWORD, RUNNER_PASSWORD, _admin_connect, connect_as  # noqa: E402


@pytest.fixture(scope="module")
def database():
    try:
        server = _admin_connect()
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL for target tests not reachable ({type(exc).__name__})")
    name = f"pv_test_{uuid.uuid4().hex[:8]}"
    server.execute(f'CREATE DATABASE "{name}"')
    try:
        with _admin_connect(name) as admin:
            for path in ("01_schema.sql", "02_seed_data.sql"):  # D8 phase 1
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


def _view_state(runner_conn):
    definition = runner_conn.execute("SELECT pg_get_viewdef('public.org_chart'::regclass)").fetchone()[0]
    rows = runner_conn.execute("SELECT emp_id, depth, reporting_line FROM org_chart ORDER BY emp_id").fetchall()
    owner = runner_conn.execute("SELECT viewowner FROM pg_views WHERE viewname = 'org_chart'").fetchone()[0]
    runner_conn.rollback()
    return definition, rows, owner


def real_converter(objects, database, reply):
    name, deployer_conn, _ = database
    fake = FakeGenerate(reply)
    deployer = PostgresDeployer(deployer_conn, database=name)
    return OracleToPostgresConverter(deployer, objects.values(), llm=GeminiFallback(fake)), fake


def test_db_gemini_view_is_deployed_through_the_restricted_deployer(objects, database):
    converter, fake = real_converter(objects, database, answer())
    result = converter.convert(objects[ORG])
    assert result.compiled_ok, result.compile_error
    assert result.tool is ConversionTool.GEMINI and result.model == MODEL and result.needs_review
    definition, rows, owner = _view_state(database[2])
    assert owner == DEPLOYER_ROLE  # created by pv_deployer, not an admin connection
    assert len(rows) == 11  # read back as pv_runner
    assert (108, 4, " > Asha Verma > Rohan Mehta > Karan Singh > Arjun Das") in rows


def test_db_a_failed_replacement_leaves_the_deployed_view_unchanged(objects, database):
    _, _, runner_conn = database
    before = _view_state(runner_conn)
    broken = ORG_SQL.replace("t.depth + 1", "t.no_such_column + 1")
    converter, _ = real_converter(objects, database, answer(broken))
    result = converter.convert(objects[ORG])
    assert not result.compiled_ok and result.compile_error.startswith("Gemini deploy_failed: 42703")
    assert result.needs_review and result.target_code == broken
    assert _view_state(runner_conn) == before  # same definition, same rows, same owner


def test_db_a_refused_replacement_never_touches_the_database(objects, database):
    _, _, runner_conn = database
    before = _view_state(runner_conn)
    sneaky = ORG_SQL + "DROP TABLE salary_audit;\n"
    converter, _ = real_converter(objects, database, answer(sneaky))
    result = converter.convert(objects[ORG])
    assert result.compile_error.startswith("Gemini refused: Refused:")
    assert _view_state(runner_conn) == before
    assert runner_conn.execute("SELECT to_regclass('public.salary_audit') IS NOT NULL").fetchone()[0]
    runner_conn.rollback()


def test_db_security_definer_function_is_refused_and_not_created(objects, database):
    _, _, runner_conn = database
    definer = SAY_HI_SQL.replace("LANGUAGE plpgsql", "LANGUAGE plpgsql SECURITY DEFINER")
    converter, _ = real_converter(objects, database, answer(definer))
    result = converter.convert(objects["function:legacy.say_hi"])
    assert result.compile_error.startswith("Gemini refused: Refused:")
    assert runner_conn.execute("SELECT count(*) FROM pg_proc WHERE proname = 'say_hi'").fetchone()[0] == 0
    runner_conn.rollback()


def test_db_a_clean_gemini_function_deploys_as_pv_deployer(objects, database):
    _, _, runner_conn = database
    converter, _ = real_converter(objects, database, answer(SAY_HI_SQL))
    result = converter.convert(objects["function:legacy.say_hi"])
    assert result.compiled_ok, result.compile_error
    owner = runner_conn.execute("SELECT pg_get_userbyid(proowner), prosecdef FROM pg_proc "
                                "WHERE proname = 'say_hi'").fetchone()
    assert owner == (DEPLOYER_ROLE, False)
    assert runner_conn.execute("SELECT say_hi()").fetchone()[0] == 1
    runner_conn.rollback()

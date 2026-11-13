"""The converter (Stage 2, step 5): the deterministic path behind the Converter contract.

Unit tests use a fake deployer (no database). The integration test needs PostgreSQL: the
real converter, with the real restricted deployer, goes through the unchanged repair loop
for the four golden-tested objects, and each result is judged against Oracle's recorded
answers (ORACLE_EXPECTED).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.contracts.interfaces import Converter
from app.contracts.models import (
    ConversionTool,
    ExecutionError,
    ExecutionResult,
    FailureReport,
    TestOutcome,
    TestStatus,
    ValidationResult,
    ValidationStatus,
)
from app.migration.converter import OracleToPostgresConverter
from app.migration.postgres_deployer import DeployResult, PostgresDeployer
from app.migration.target_roles import DEPLOYER_ROLE, RUNNER_ROLE, ensure_roles
from app.orchestration.repair_loop import RepairDeps, RepairOutcome, run_repair_loop
from app.parsers.dependency_analyzer import analyze_dependencies
from app.parsers.oracle_parser import parse_sources
from app.validation.comparator import values_equal
from app.validation.errors import categorize
from app.validation.golden_tests import GOLDEN_TESTS, ORACLE_EXPECTED
from app.validation.postgres_runner import run_postgres

ROOT = Path(__file__).resolve().parents[2]

EXTRA = """
CREATE OR REPLACE PROCEDURE dyn_sql AS BEGIN EXECUTE IMMEDIATE 'DELETE FROM employees'; END;
/
CREATE OR REPLACE VIEW pay_view AS SELECT emp_id, NVL(base_salary, 0) AS salary FROM employees;
/
CREATE OR REPLACE PROCEDURE bad_call AS BEGIN mystery_proc(1); END;
/
"""


DEPLOYED = DeployResult(ok=True)


class FakeDeployer:
    def __init__(self, result: DeployResult | Exception = DEPLOYED):
        self.result = result
        self.calls: list = []

    def deploy(self, code, target):
        self.calls.append((code, target))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture(scope="module")
def objects():
    sources = {p.name: p.read_text() for p in sorted((ROOT / "samples" / "oracle").glob("*.sql"))}
    sources["99_extra.sql"] = EXTRA
    return {o.object_id: o for o in parse_sources(sources)}


def converter_with(objects, deployer=None):
    return OracleToPostgresConverter(deployer or FakeDeployer(), objects.values())


ROUTINES = [
    "function:legacy.get_emp_grade", "function:legacy.top_earners", "procedure:legacy.calc_bonus",
    "procedure:legacy.apply_raise", "procedure:legacy.monthly_payroll", "function:legacy.payroll_pkg.net_pay",
    "procedure:legacy.payroll_pkg.close_month", "trigger:legacy.emp_salary_audit_trg",
]


def test_it_is_a_converter(objects):
    converter: Converter = converter_with(objects)  # the existing contract, unchanged
    assert callable(converter.convert)


@pytest.mark.parametrize("object_id", ROUTINES)
def test_sample_routines_are_translated_and_deployed(objects, object_id):
    deployer = FakeDeployer()
    result = converter_with(objects, deployer).convert(objects[object_id])
    assert result.compiled_ok and result.compile_error is None
    assert result.tool is ConversionTool.SQLGLOT
    assert result.version == 1
    assert result.object_id == object_id
    (code, target), = deployer.calls
    assert code == result.target_code and target.name == objects[object_id].name


def test_versions_count_per_object(objects):
    converter = converter_with(objects)
    calc, grade = objects["procedure:legacy.calc_bonus"], objects["function:legacy.get_emp_grade"]
    assert [converter.convert(calc).version for _ in range(3)] == [1, 2, 3]
    assert converter.convert(grade).version == 1


def test_dropped_default_is_deployed_but_flagged_for_review(objects):
    result = converter_with(objects).convert(objects["procedure:legacy.monthly_payroll"])
    assert result.compiled_ok and result.needs_review
    assert any("Callers must pass it" in w for w in result.warnings)
    assert any(w.startswith("Needs review: parameter default dropped (p_run_date)") for w in result.warnings)


def test_review_features_stop_at_once_without_deploying(objects):
    deployer = FakeDeployer()
    result = converter_with(objects, deployer).convert(objects["procedure:legacy.dyn_sql"])
    assert not result.compiled_ok and result.needs_review
    assert result.compile_error.startswith("Unsupported: dynamic SQL")
    assert deployer.calls == []


def test_llm_only_constructs_are_not_converted(objects):
    deployer = FakeDeployer()
    result = converter_with(objects, deployer).convert(objects["view:legacy.org_chart"])
    assert not result.compiled_ok and not result.needs_review
    assert "CONNECT BY" in result.compile_error and "step 6" in result.compile_error
    assert deployer.calls == [] and result.target_code == ""


def test_translator_gaps_are_reported(objects):
    result = converter_with(objects).convert(objects["procedure:legacy.bad_call"])
    assert not result.compiled_ok
    assert result.compile_error == "Not converted: call to unknown routine mystery_proc"


def test_views_with_rules_are_deployed(objects):
    deployer = FakeDeployer()
    result = converter_with(objects, deployer).convert(objects["view:legacy.pay_view"])
    assert result.compiled_ok
    assert result.target_code == (
        "CREATE OR REPLACE VIEW pay_view AS SELECT emp_id, COALESCE(base_salary, 0) AS salary FROM employees;\n"
    )


@pytest.mark.parametrize("object_id, phrase", [
    ("table:legacy.employees", "hand-written in phase 1 (D8)"),
    ("sequence:legacy.salary_audit_seq", "hand-written in phase 1 (D8)"),
    ("package:legacy.payroll_pkg", "deployed into schema payroll_pkg (D1)"),
])
def test_objects_not_converted_on_their_own(objects, object_id, phrase):
    deployer = FakeDeployer()
    result = converter_with(objects, deployer).convert(objects[object_id])
    assert not result.compiled_ok and phrase in result.compile_error
    assert deployer.calls == []


def test_repair_attempts_wait_for_gemini_without_touching_the_database(objects):
    deployer = FakeDeployer()
    converter = converter_with(objects, deployer)
    calc = objects["procedure:legacy.calc_bonus"]
    converter.convert(calc)
    feedback = FailureReport(object_id=calc.object_id, attempt=1, failing_tests=[], summary="cb-02: 24000 vs 6000")
    retry = converter.convert(calc, feedback)
    assert retry.version == 2 and not retry.compiled_ok
    assert "step 6" in retry.compile_error and "cb-02: 24000 vs 6000" in retry.compile_error
    assert len(deployer.calls) == 1


def test_deploy_failures_are_returned(objects):
    failed = DeployResult(ok=False, error="42601: syntax error", sqlstate="42601")
    result = converter_with(objects, FakeDeployer(failed)).convert(objects["procedure:legacy.calc_bonus"])
    assert not result.compiled_ok and result.compile_error == "42601: syntax error"
    assert result.target_code  # the code that was tried is kept for the report


def test_never_raises(objects):
    result = converter_with(objects, FakeDeployer(RuntimeError("boom"))).convert(objects["procedure:legacy.calc_bonus"])
    assert not result.compiled_ok and result.compile_error == "Deployer error: RuntimeError: boom"


def test_translation_is_memoised(objects, monkeypatch):
    import app.migration.converter as module

    calls = []
    real = module.translate_plsql
    monkeypatch.setattr(module, "translate_plsql", lambda *a, **k: calls.append(1) or real(*a, **k))
    converter = converter_with(objects)
    for _ in range(3):
        converter.convert(objects["function:legacy.get_emp_grade"])
    assert len(calls) == 1


# --- integration: the real converter through the unchanged repair loop -------------------

psycopg = pytest.importorskip("psycopg")
from tests.target_pg import DEPLOYER_PASSWORD, RUNNER_PASSWORD, _admin_connect, connect_as  # noqa: E402

WATCH = ["employees", "salary_audit", "payroll_runs"]


def _judge(runner_conn, obj, case) -> TestOutcome:
    """PostgreSQL's answer for one golden test, judged against Oracle's recorded answer."""
    expected = ORACLE_EXPECTED[case.test_id]
    result = run_postgres(runner_conn, obj, case, WATCH)
    if "error" in expected:
        ok = categorize("postgres", result.error) == categorize(
            "oracle", ExecutionError(code=expected["error"], message=""))
    else:
        changed = {d.table for d in result.table_diffs if d.inserted or d.deleted or d.updated}
        ok = result.error is None and (
            "return_value" not in expected or values_equal(result.return_value, expected["return_value"])
        ) and all(values_equal(result.out_params.get(k), v) for k, v in expected.get("out_params", {}).items()) \
            and ("tables_changed" not in expected or changed == expected["tables_changed"])
    oracle = ExecutionResult(engine="oracle", test_id=case.test_id)  # recorded answer, see ORACLE_EXPECTED
    return TestOutcome(test_id=case.test_id, status=TestStatus.PASSED if ok else TestStatus.FAILED,
                       source=oracle, target=result)


@pytest.fixture(scope="module")
def sandbox(objects):
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
            converter = OracleToPostgresConverter(PostgresDeployer(deployer_conn, database=name), objects.values())
            yield converter, runner_conn
    finally:
        server.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                       "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
        server.execute(f'DROP DATABASE IF EXISTS "{name}"')
        server.close()


def test_whole_sample_in_dependency_order(sandbox, objects):
    converter, _ = sandbox
    sample = {k: o for k, o in objects.items() if o.source_file != "99_extra.sql"}
    order = analyze_dependencies(list(sample.values())).execution_order
    results = {k: converter.convert(objects[k]) for k in order}
    deployed = {k for k, r in results.items() if r.compiled_ok}
    assert deployed == set(ROUTINES)
    assert not results["view:legacy.org_chart"].compiled_ok  # CONNECT BY: step 6
    assert {k for k, r in results.items() if r.needs_review} == {"procedure:legacy.monthly_payroll"}


@pytest.mark.parametrize("object_id", sorted(GOLDEN_TESTS))
def test_repair_loop_with_the_real_converter(sandbox, objects, object_id):
    converter, runner_conn = sandbox

    def validate(obj, cases, version) -> ValidationResult:
        outcomes = [_judge(runner_conn, obj, case) for case in cases]
        runner_conn.rollback()
        failed = sum(o.status is TestStatus.FAILED for o in outcomes)
        return ValidationResult(
            object_id=obj.object_id, conversion_version=version, tests=outcomes,
            passed=len(outcomes) - failed, failed=failed,
            status=ValidationStatus.REQUIRES_REPAIR if failed else ValidationStatus.PASSED,
        )

    run = run_repair_loop(RepairDeps(converter=converter, validate=validate), objects[object_id],
                          GOLDEN_TESTS[object_id])
    assert run.outcome is RepairOutcome.PASSED, run.reason
    assert len(run.attempts) == 1  # right first time: no repair needed
    assert run.final_conversion.tool is ConversionTool.SQLGLOT
    assert run.final_validation.passed == len(GOLDEN_TESTS[object_id])

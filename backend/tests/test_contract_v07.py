"""Contract 0.7: the pipeline models, their promises, the protocols and the stand-ins.

Offline: no database, no Oracle, no Gemini. The PostgreSQL side of the PreparedTargetSchema
stand-in is in test_prepared_target.py.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from app import contracts
from app.contracts import (
    CONTRACT_VERSION,
    ColumnDef,
    FailureCategory,
    ForeignKeyDef,
    GeminiCall,
    MigrationEngine,
    MigrationInputError,
    MigrationOptions,
    MigrationRun,
    ObjectOutcome,
    ObjectStatus,
    ParityCheck,
    ProgressSink,
    RepairOutcome,
    RepairRun,
    ReportWriter,
    Route,
    RunMetrics,
    RunStatus,
    SchemaMigrationResult,
    SchemaMigrator,
    SchemaObjectOutcome,
    SchemaObjectStatus,
    SequenceDef,
    SourceFile,
    SourceProvider,
    Stage,
    TableDef,
    TargetEnvironment,
    status_for_repair,
)
from app.migration.prepared_target import PreparedTargetSchema
from tests.contract_fakes import (
    SAMPLE_RUN,
    FakeMigrationEngine,
    FakeSchemaMigrator,
    RecordingProgress,
    load_sample_run,
)

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
ORG_CHART = "view:legacy.org_chart"
EMPLOYEES = "table:legacy.employees"
CALC_BONUS = "procedure:legacy.calc_bonus"


def sample() -> dict:
    return json.loads(SAMPLE_RUN.read_text(encoding="utf-8"))


def outcome(data: dict, object_id: str) -> dict:
    return next(o for o in data["objects"] if o["object_id"] == object_id)


def with_metrics(data: dict) -> dict:
    """Recompute the metrics, so a test reaches the promise it is about."""
    objects = [ObjectOutcome.model_validate(o) for o in data["objects"]]
    schema = SchemaMigrationResult.model_validate(data["schema_result"])
    data["metrics"] = RunMetrics.of(objects, schema, len(data["discovered"])).model_dump(mode="json")
    return data


def refused(data: dict, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        MigrationRun.model_validate(data)


def env(**changes) -> TargetEnvironment:
    values = {"oracle_dsn": "localhost:1521/FREEPDB1", "oracle_user": "legacy", "oracle_password": "ora-SECRET-1",
                  "deployer_url": "postgresql://pv_deployer:dep-SECRET-2@localhost:5433/target",
                  "runner_url": "postgresql://pv_runner:run-SECRET-3@localhost:5433/target", "database_name": "target"}
    return TargetEnvironment(**(values | changes))


# --- the sample fixture ------------------------------------------------------------------------

def test_the_fixture_is_a_valid_current_run_that_round_trips():
    run = load_sample_run()
    assert run.contract_version == CONTRACT_VERSION == "0.7"
    assert MigrationRun.model_validate_json(run.model_dump_json()) == run
    assert MigrationRun.model_validate(json.loads(json.dumps(run.model_dump(mode="json")))) == run


def test_the_fixture_reports_every_sample_object_with_a_final_status():
    run = load_sample_run()
    assert run.status is RunStatus.COMPLETED and run.oracle_answers == "recorded" and not run.stand_in
    assert len(run.discovered) == len(run.objects) == run.metrics.reported_objects == run.metrics.source_objects == 16
    assert {o.status for o in run.objects} == {ObjectStatus.PASSED, ObjectStatus.PASSED_NEEDS_REVIEW,
                                                ObjectStatus.NEEDS_HUMAN, ObjectStatus.NOT_CONVERTED}
    assert run.outcome(ORG_CHART).route is Route.GEMINI and run.outcome(ORG_CHART).status is ObjectStatus.NEEDS_HUMAN
    assert run.outcome("procedure:legacy.monthly_payroll").status is ObjectStatus.PASSED_NEEDS_REVIEW
    assert run.outcome("package:legacy.payroll_pkg").status is ObjectStatus.NOT_CONVERTED
    assert run.metrics.objects.passed == 13 and run.metrics.needs_human == [ORG_CHART]
    assert run.metrics.deployed == run.metrics.validated == 8 and run.metrics.unexplained == []
    assert run.schema_result.mode == "prepared" and run.metrics.schema_parity == "not_checked"
    assert run.metrics.gemini_requests == 0 and not run.options.gemini


def test_the_fixture_holds_no_credentials():
    text = SAMPLE_RUN.read_text(encoding="utf-8")
    for marker in ("postgresql://", "postgres://", "redis://", "password", "pv_deployer:", "pv_runner:", "GEMINI_API"):
        assert marker not in text


# --- A17: no silent skips ---------------------------------------------------------------------

def test_a_run_missing_an_object_is_refused():
    data = sample()
    data["objects"] = [o for o in data["objects"] if o["object_id"] != CALC_BONUS]
    refused(with_metrics(data), f"no final status: {CALC_BONUS}")


def test_an_object_reported_twice_is_refused():
    data = sample()
    data["objects"].append(copy.deepcopy(outcome(data, CALC_BONUS)))
    refused(with_metrics(data), f"listed twice: {CALC_BONUS}")


def test_an_object_that_was_not_discovered_is_refused():
    data = sample()
    data["discovered"].remove(CALC_BONUS)
    refused(with_metrics(data), f"not discovered: {CALC_BONUS}")


def test_metrics_must_be_computed_from_the_outcomes():
    data = sample()
    data["metrics"]["objects"]["passed"] += 1
    refused(data, "metrics must be RunMetrics.of")


@pytest.mark.parametrize("reason", ["", "   "])
def test_every_outcome_needs_a_reason(reason):
    data = sample()
    outcome(data, CALC_BONUS)["reason"] = reason
    refused(data, "must not be blank")


# --- one object's outcome ------------------------------------------------------------------------

def test_the_status_must_be_what_the_repair_run_ends_in():
    data = sample()
    outcome(data, ORG_CHART)["status"] = "passed"
    outcome(data, ORG_CHART)["category"] = None
    refused(data, "the repair run ends needs_human, not passed")


def test_a_passed_outcome_has_no_failure_category():
    data = sample()
    outcome(data, CALC_BONUS)["category"] = "tests_failed"
    refused(data, "a passed object has no failure category")


def test_the_category_is_the_repair_runs():
    data = sample()
    outcome(data, ORG_CHART)["category"] = "deploy_failed"
    refused(data, "category must be the repair run's category")


def test_needs_human_without_evidence_is_refused():
    data = sample()
    outcome(data, CALC_BONUS).update(repair=None, status="needs_human")
    refused(data, "needs_human needs the repair run as evidence")


def test_a_repair_run_for_another_object_is_refused():
    data = sample()
    outcome(data, CALC_BONUS)["repair"]["object_id"] = "procedure:legacy.apply_raise"
    refused(data, "the repair run is for procedure:legacy.apply_raise")


def test_tables_and_only_tables_and_sequences_take_the_schema_route():
    data = sample()
    outcome(data, EMPLOYEES)["route"] = "rules"
    refused(data, "tables and sequences, and only they, take the schema route")
    data = sample()
    outcome(data, CALC_BONUS)["route"] = "schema"
    refused(data, "tables and sequences, and only they, take the schema route")


def test_the_type_must_match_the_object_id():
    data = sample()
    outcome(data, CALC_BONUS)["type"] = "function"
    refused(data, "is not a function")


def test_a_review_route_object_cannot_pass():
    data = sample()
    outcome(data, CALC_BONUS)["route"] = "review"
    refused(data, "a review-route object cannot pass without a human")


def test_a_blocked_object_is_not_converted():
    data = sample()
    outcome(data, CALC_BONUS)["blocked_by"] = [EMPLOYEES]
    refused(data, "an object blocked by another one is not_converted")


@pytest.mark.parametrize("status", ["passed", "needs_human"])
def test_an_object_with_no_route_was_not_converted(status):
    data = sample()
    outcome(data, "package:legacy.payroll_pkg")["status"] = status
    refused(data, f"{status} needs the repair run as evidence")
    data = sample()
    outcome(data, "package:legacy.payroll_pkg")["repair"] = outcome(data, CALC_BONUS)["repair"]
    refused(data, "a none-route object has no repair run")


def test_unresolved_ids_are_not_source_objects():
    with pytest.raises(ValidationError, match="not the object_id of a source object"):
        ObjectOutcome(object_id="unresolved:legacy.x", type="table", route="schema", status="passed", reason="x")


# --- schema result agrees with the outcomes -------------------------------------------------------

def test_the_schema_result_must_cover_every_table_and_sequence():
    data = sample()
    data["schema_result"]["objects"] = [o for o in data["schema_result"]["objects"] if o["object_id"] != EMPLOYEES]
    refused(data, f"the schema result must cover exactly the tables and sequences: {EMPLOYEES}")


def test_an_empty_schema_result_is_refused_even_though_it_reads_ok():
    """SchemaMigrationResult.ok is True for an empty list (all([]) is True), so the run, not ok,
    is where every table and sequence must have its outcome (Aarsh's PR #13 review, 19 Nov)."""
    data = sample()
    expected = sorted(o["object_id"] for o in data["schema_result"]["objects"])
    assert len(expected) == 6
    data["schema_result"]["objects"] = []
    assert SchemaMigrationResult.model_validate(data["schema_result"]).ok   # ok alone is not the gate
    refused(data, "the schema result must cover exactly the tables and sequences: " + ", ".join(expected))


def test_a_table_status_follows_its_schema_outcome():
    data = sample()
    schema = next(o for o in data["schema_result"]["objects"] if o["object_id"] == EMPLOYEES)
    schema.update(status="mismatch", reason="column(s) not in the Oracle DDL: x")
    refused(data, f"{EMPLOYEES}: the schema result makes it needs_human")   # the outcome still says passed


def test_prepared_mode_reports_only_present_missing_or_mismatch():
    data = sample()["schema_result"]
    data["objects"][0]["status"] = "created"
    with pytest.raises(ValidationError, match="prepared mode cannot report"):
        SchemaMigrationResult.model_validate(data)


def test_prepared_mode_copies_no_data():
    data = sample()["schema_result"]
    table = next(o for o in data["objects"] if o["object_id"] == EMPLOYEES)
    table["rows_copied"] = 12
    with pytest.raises(ValidationError, match="prepared mode copies no data"):
        SchemaMigrationResult.model_validate(data)


def _table_outcome(status, parity=None, definition=True, rows=None):
    table = TableDef(object_id=EMPLOYEES, target_name="employees",
                     columns=[ColumnDef(name="emp_id", oracle_type="NUMBER(6)", pg_type="numeric(6,0)")])
    return SchemaObjectOutcome(object_id=EMPLOYEES, status=status, reason="r", definition=table if definition else None,
                               parity=parity, rows_copied=rows)


def test_migrated_mode_parity():
    same, other = ParityCheck(oracle_rows=12, target_rows=12, oracle_checksum="a", target_checksum="a"), \
        ParityCheck(oracle_rows=12, target_rows=11, oracle_checksum="a", target_checksum="b")
    verified = SchemaMigrationResult(mode="migrated", migrator="m", database="target",
                                     objects=[_table_outcome("created", same, rows=12)])
    assert verified.parity == "verified" and verified.ok
    mismatch = SchemaMigrationResult(mode="migrated", migrator="m", database="target",
                                     objects=[_table_outcome("mismatch", other, rows=11)])
    assert mismatch.parity == "mismatch" and not mismatch.ok
    with pytest.raises(ValidationError, match="is a mismatch, not ready"):
        _table_outcome("created", other)
    with pytest.raises(ValidationError, match="give both checksums or neither"):
        ParityCheck(oracle_rows=1, target_rows=1, oracle_checksum="a")


def test_a_ready_object_needs_its_definition_and_a_missing_one_has_none():
    with pytest.raises(ValidationError, match="needs its definition"):
        _table_outcome("present", definition=False)
    with pytest.raises(ValidationError, match="a missing object has no definition"):
        _table_outcome("missing")


def test_a_definition_must_be_for_its_own_object():
    with pytest.raises(ValidationError, match="definition is for"):
        SchemaObjectOutcome(object_id="table:legacy.departments", status="present", reason="r",
                            definition=_table_outcome("present").definition)
    with pytest.raises(ValidationError, match="definition is for table:legacy.employees, not sequence:legacy.s"):
        SchemaObjectOutcome(object_id="sequence:legacy.s", status="present", reason="r",
                            definition=_table_outcome("present").definition)
    with pytest.raises(ValidationError, match="rows and parity apply to tables only"):
        SchemaObjectOutcome(object_id="sequence:legacy.s", status="present", reason="r", rows_copied=1,
                            definition=SequenceDef(object_id="sequence:legacy.s", target_name="s"))


# --- TableDef and SequenceDef ---------------------------------------------------------------------

def test_a_table_def_normalizes_names_and_keeps_column_order():
    table = TableDef(object_id=EMPLOYEES, target_name="EMPLOYEES",
                     columns=[ColumnDef(name="EMP_ID", oracle_type="NUMBER(6)", pg_type="numeric(6,0)", nullable=False),
                              ColumnDef(name='"Dept_Id"', oracle_type="NUMBER(6)", pg_type="numeric(6,0)")],
                     primary_key=["EMP_ID"],
                     foreign_keys=[ForeignKeyDef(columns=["dept_id"], ref_table="table:legacy.departments",
                                                 ref_columns=["DEPT_ID"])])
    assert [c.name for c in table.columns] == ["emp_id", "dept_id"] and table.target_name == "employees"
    assert table.primary_key == ["emp_id"] and table.column("DEPT_ID").pg_type == "numeric(6,0)"
    assert TableDef.model_validate_json(table.model_dump_json()) == table


@pytest.mark.parametrize("change, match", [
    ({"columns": []}, "at least 1 item"),
    ({"primary_key": ["nope"]}, "key column\\(s\\) not in the table: nope"),
    ({"unique": [[]]}, "a unique constraint needs at least one column"),
    ({"object_id": "view:legacy.employees"}, "must be a table object_id"),
    ({"columns": [{"name": "a", "oracle_type": "NUMBER", "pg_type": "numeric"}] * 2}, "duplicate column"),
    ({"extra": 1}, "Extra inputs are not permitted"),
])
def test_a_table_def_refuses(change, match):
    data = {"object_id": EMPLOYEES, "target_name": "employees",
            "columns": [{"name": "emp_id", "oracle_type": "NUMBER", "pg_type": "numeric"}]} | change
    with pytest.raises(ValidationError, match=match):
        TableDef.model_validate(data)


def test_a_foreign_key_refuses_mismatched_columns_and_non_tables():
    with pytest.raises(ValidationError, match="has 2 columns but references 1"):
        ForeignKeyDef(columns=["a", "b"], ref_table="table:legacy.t", ref_columns=["a"])
    with pytest.raises(ValidationError, match="must be a table object_id"):
        ForeignKeyDef(columns=["a"], ref_table="view:legacy.t", ref_columns=["a"])


def test_a_sequence_def_refuses_zero_increment_and_an_empty_range():
    with pytest.raises(ValidationError, match="increment_by must not be 0"):
        SequenceDef(object_id="sequence:legacy.s", target_name="s", increment_by=0)
    with pytest.raises(ValidationError, match="min_value is greater than max_value"):
        SequenceDef(object_id="sequence:legacy.s", target_name="s", min_value=5, max_value=1)


# --- inputs ----------------------------------------------------------------------------------------

def test_a_source_file_carries_the_hash_of_its_text():
    source = SourceFile.of("03_payroll_pkg.sql", "CREATE PACKAGE p AS END;")
    assert source.sha256 == hashlib.sha256(b"CREATE PACKAGE p AS END;").hexdigest()
    assert source.ref().chars == len(source.text)
    with pytest.raises(ValidationError, match="sha256 does not match the text of x.sql"):
        SourceFile(name="x.sql", text="changed", sha256=source.sha256)


@pytest.mark.parametrize("name", ["../x.sql", "/etc/x.sql", "a\\b.sql", "a/../b.sql", "./a.sql", "a//b.sql", ""])
def test_a_source_name_is_a_plain_relative_path(name):
    with pytest.raises(ValidationError):
        SourceFile.of(name, "x")


def test_options_state_a_gemini_budget_only_with_gemini_on():
    assert MigrationOptions().gemini is False and MigrationOptions().max_repair_attempts == 3
    assert MigrationOptions(gemini=True, gemini_request_budget=6).gemini_request_budget == 6
    with pytest.raises(ValidationError, match="state a request budget"):
        MigrationOptions(gemini=True)
    with pytest.raises(ValidationError, match="a request budget needs gemini on"):
        MigrationOptions(gemini_request_budget=3)
    with pytest.raises(ValidationError):
        MigrationOptions(max_repair_attempts=0)
    assert MigrationOptions(objects=["CALC_BONUS", CALC_BONUS]).objects == ["calc_bonus", CALC_BONUS]


def test_the_target_environment_never_shows_its_secrets():
    target = env()
    shown = repr(target) + str(target) + target.model_dump_json() + json.dumps(target.model_dump(mode="json"))
    for secret in ("ora-SECRET-1", "dep-SECRET-2", "run-SECRET-3"):
        assert secret not in shown
    assert target.deployer_url.get_secret_value().endswith("@localhost:5433/target")
    with pytest.raises(ValidationError) as error:
        env(deployer_url="mysql://pv_deployer:dep-SECRET-2@localhost/target")
    assert "must be a postgresql:// URL" in str(error.value) and "SECRET" not in str(error.value)


def test_no_stored_model_can_hold_the_target_environment():
    stored = [MigrationRun, ObjectOutcome, SchemaMigrationResult, RunMetrics, MigrationOptions]
    schemas = json.dumps([m.model_json_schema() for m in stored])
    assert "TargetEnvironment" not in schemas and '"format": "password"' not in schemas


# --- Gemini records, budget, attempts -------------------------------------------------------------

@pytest.mark.parametrize("call, ok", [
    ({"attempt": 1, "outcome": "ok", "response_chars": 10, "cache": "stored"}, True),
    ({"attempt": 0, "outcome": "cache_hit", "response_chars": 10, "cache": "hit"}, True),
    ({"attempt": 1, "outcome": "http_504", "response_chars": None, "cache": "not_stored"}, True),
    ({"attempt": 2, "outcome": "client_timeout", "response_chars": None, "cache": "off"}, True),
    ({"attempt": 1, "outcome": "error:ConnectError", "response_chars": None, "cache": "not_stored"}, True),
    ({"attempt": 0, "outcome": "ok", "response_chars": 10, "cache": "stored"}, False),       # attempt 0 is a cache hit
    ({"attempt": 1, "outcome": "cache_hit", "response_chars": 10, "cache": "hit"}, False),
    ({"attempt": 1, "outcome": "http_504", "response_chars": 5, "cache": "not_stored"}, False),  # no answer, no size
    ({"attempt": 1, "outcome": "ok", "response_chars": None, "cache": "stored"}, False),
    ({"attempt": 1, "outcome": "http_504 key=AIza", "response_chars": None, "cache": "not_stored"}, False),
])
def test_a_gemini_call_record_is_consistent(call, ok):
    values = {"model": "gemini-3.8-flash", "elapsed_ms": 900, "timeout_s": 120, "prompt_chars": 4000} | call
    if ok:
        assert GeminiCall(**values).is_request == (call["attempt"] > 0)
    else:
        with pytest.raises(ValidationError):
            GeminiCall(**values)


def _gemini_call(attempt=1):
    return {"model": "gemini-3.8-flash", "attempt": attempt, "elapsed_ms": 900,
            "outcome": "ok" if attempt else "cache_hit", "timeout_s": 120, "prompt_chars": 4000,
            "response_chars": 300, "cache": "stored" if attempt else "hit"}


def test_gemini_calls_with_gemini_off_are_refused():
    data = sample()
    outcome(data, ORG_CHART)["gemini_calls"] = [_gemini_call()]
    refused(with_metrics(data), "Gemini was off, yet Gemini calls are recorded")


def test_gemini_requests_must_stay_within_the_budget():
    data = sample()
    data["options"] |= {"gemini": True, "gemini_request_budget": 1}
    outcome(data, ORG_CHART)["gemini_calls"] = [_gemini_call(1), _gemini_call(2), _gemini_call(0)]
    refused(with_metrics(data), "2 Gemini requests exceed the budget of 1")
    data["options"]["gemini_request_budget"] = 2
    run = MigrationRun.model_validate(with_metrics(data))
    assert (run.metrics.gemini_requests, run.metrics.gemini_cache_hits) == (2, 1)


def test_only_gemini_route_objects_call_gemini():
    data = sample()
    data["options"] |= {"gemini": True, "gemini_request_budget": 5}
    outcome(data, CALC_BONUS)["gemini_calls"] = [_gemini_call()]
    refused(data, "only gemini-route objects call Gemini")
    data = sample()
    outcome(data, CALC_BONUS)["repair"]["attempts"][0]["tool"] = "gemini"
    refused(data, "only gemini-route objects call Gemini")


def test_no_object_takes_more_attempts_than_allowed():
    data = sample()
    first = outcome(data, CALC_BONUS)["repair"]["attempts"][0]
    outcome(data, CALC_BONUS)["repair"]["attempts"] = [first | {"attempt": 1}, first | {"attempt": 2}]
    data["options"]["max_repair_attempts"] = 1
    refused(data, f"{CALC_BONUS} took more than 1 attempts")


# --- run status and times --------------------------------------------------------------------------

def test_a_run_that_did_not_start_converts_nothing_and_says_why():
    data = sample()
    data["status"] = "not_run"
    refused(data, "a run that did not start says why")
    data["problems"] = ["pv_runner lacks USAGE on schema public: run scripts/setup_target_roles.py"]
    refused(data, "the run did not start, yet these were converted")


def test_a_run_cannot_finish_before_it_starts():
    run = load_sample_run()
    data = sample()
    data["finished_at"] = (run.started_at - timedelta(seconds=1)).isoformat()
    refused(data, "finished_at is before started_at")


def test_times_must_carry_a_time_zone():
    data = sample()
    data["started_at"] = "2026-11-17T09:00:00"
    refused(data, "timezone")


def test_unknown_fields_are_refused():
    data = sample()
    data["surprise"] = 1
    refused(data, "Extra inputs are not permitted")


# --- status semantics ------------------------------------------------------------------------------

def test_passed_without_any_test_is_not_passed():
    run = load_sample_run().outcome(CALC_BONUS).repair
    untested = run.model_copy(update={"final_validation": None})
    assert status_for_repair(run) is ObjectStatus.PASSED
    assert status_for_repair(untested) is ObjectStatus.PASSED_NEEDS_REVIEW
    assert status_for_repair(run.model_copy(update={"outcome": RepairOutcome.NEEDS_HUMAN})) is ObjectStatus.NEEDS_HUMAN


def test_the_serialized_enum_values_are_fixed():
    assert [s.value for s in ObjectStatus] == ["passed", "passed_needs_review", "needs_human", "not_converted", "error"]
    assert [r.value for r in Route] == ["rules", "gemini", "review", "schema", "none"]
    assert [s.value for s in SchemaObjectStatus] == ["created", "present", "missing", "mismatch", "failed"]
    assert [s.value for s in RunStatus] == ["completed", "not_run"]
    assert [s.value for s in Stage] == ["parse", "schema", "objects", "done"]
    assert [o.value for o in RepairOutcome] == ["passed", "passed_needs_review", "needs_human"]
    assert [c.value for c in FailureCategory] == [
        "unsupported", "no_repair_route", "deploy_failed", "deploy_refused", "tests_failed", "gemini_failed",
        "unchanged_output", "invalid_evidence", "harness_error", "converter_error"]


# --- moved models -------------------------------------------------------------------------------------

def test_the_repair_run_models_are_contracts_and_still_importable_from_the_loop():
    from app.orchestration import repair_loop

    for name in ("RepairRun", "AttemptRecord", "RepairOutcome", "FailureCategory"):
        assert getattr(repair_loop, name) is getattr(contracts, name)
        assert getattr(contracts, name).__module__ == "app.contracts.models"


def test_a_repair_run_written_before_0_7_still_loads():
    old = {"object_id": "procedure:legacy.calc_bonus", "outcome": "needs_human", "reason": "Still failing",
           "attempts": [{"attempt": 1, "version": 1, "tool": "sqlglot", "compiled_ok": True,
                         "status": "requires_repair", "passed": 4, "failed": 2,
                         "failing_tests": ["cb-03", "cb-05"]}]}
    run = RepairRun.model_validate(old)
    assert run.category is None and run.run_id is None and run.attempts[0].errors == 0


@pytest.mark.parametrize("field, value", [("attempt", 0), ("version", 0), ("passed", -1), ("failed", -1)])
def test_attempt_numbers_start_at_one_and_counts_are_never_negative(field, value):
    record = load_sample_run().outcome(CALC_BONUS).repair.attempts[0].model_dump()
    with pytest.raises(ValidationError):
        type(load_sample_run().outcome(CALC_BONUS).repair.attempts[0]).model_validate(record | {field: value})


# --- dependencies point one way ------------------------------------------------------------------------

def _modules_after_import(module: str) -> set[str]:
    code = f"import sys, {module}; print('\\n'.join(sorted(m for m in sys.modules if m.startswith('app'))))"
    result = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, check=True, capture_output=True, text=True)
    return set(result.stdout.split())


def test_the_contracts_import_nothing_else_from_the_project():
    assert {m for m in _modules_after_import("app.contracts") if not m.startswith("app.contracts")} == {"app"}


def test_reporting_does_not_import_the_engine():
    loaded = _modules_after_import("app.reporting.migration_report")
    assert not {m for m in loaded if m.startswith(("app.orchestration", "app.migration", "app.validation"))}


def test_the_report_renders_the_fixtures_repair_runs():
    from app.reporting.migration_report import ReportSummary, render_report

    runs = [o.repair for o in load_sample_run().objects if o.repair]
    html = render_report(runs, descriptions={})
    assert "view:legacy.org_chart" in html and ReportSummary.of(runs).objects == 9


# --- protocols and stand-ins -------------------------------------------------------------------------

def _parameters(function) -> list[tuple[str, object]]:
    return [(p.name, p.kind) for p in inspect.signature(function).parameters.values() if p.name != "self"]


@pytest.mark.parametrize("implementation, protocol, method", [
    (FakeMigrationEngine, MigrationEngine, "run_migration"),
    (FakeSchemaMigrator, SchemaMigrator, "migrate"),
    (PreparedTargetSchema, SchemaMigrator, "migrate"),
    (RecordingProgress, ProgressSink, "update"),
])
def test_the_stand_ins_satisfy_the_protocols(implementation, protocol, method):
    assert issubclass(implementation, protocol)
    assert _parameters(getattr(implementation, method)) == _parameters(getattr(protocol, method))


def test_every_protocol_is_runtime_checkable():
    for protocol in (MigrationEngine, SchemaMigrator, SourceProvider, ProgressSink, ReportWriter):
        assert getattr(protocol, "_is_runtime_protocol", False), protocol


def test_the_fake_engine_returns_the_sample_marked_as_a_stand_in_and_reports_progress():
    engine, progress = FakeMigrationEngine(), RecordingProgress()
    sources = [SourceFile.of("01_schema.sql", "x")]
    run = engine.run_migration(sources, MigrationOptions(), env(), schema=FakeSchemaMigrator(), progress=progress)
    assert run.stand_in and run.engine.startswith("FakeMigrationEngine") and len(run.objects) == 16
    assert MigrationRun.model_validate(run.model_dump()) == run
    assert progress.updates[0][0] is Stage.PARSE and progress.updates[-1] == (Stage.DONE, None, 16, 16)
    assert all(0 <= done <= total for _, _, done, total in progress.updates)
    with pytest.raises(MigrationInputError):
        engine.run_migration([], MigrationOptions(), env(), schema=FakeSchemaMigrator())
    with pytest.raises(MigrationInputError):
        engine.run_migration(sources * 2, MigrationOptions(), env(), schema=FakeSchemaMigrator())


def test_the_fake_schema_migrator_reports_each_expected_object_once():
    migrator = FakeSchemaMigrator(missing={"table:legacy.salary_audit"})
    result = migrator.migrate([], env(), expected_tables=[EMPLOYEES, "table:legacy.salary_audit"],
                              expected_sequences=["sequence:legacy.salary_audit_seq"])
    assert [o.status.value for o in result.objects] == ["present", "missing", "present"] and not result.ok
    assert FakeSchemaMigrator().migrate([], env(), expected_tables=[EMPLOYEES], expected_sequences=[]).ok


def test_the_exported_schemas_include_the_pipeline_and_the_moved_models():
    from app.contracts.export_schemas import EXPORT

    names = {m.__name__ for m in EXPORT}
    assert {"MigrationRun", "RepairRun", "TableDef", "SchemaMigrationResult", "SourceFile"} <= names
    assert "TargetEnvironment" not in names


# --- the engine's builder (orchestration/run_result.py) ---------------------------------------------

def _builder_inputs():
    from app.parsers.oracle_parser import parse_sources

    sources = [SourceFile.of(p.name, p.read_text(encoding="utf-8"))
               for p in sorted((ROOT / "samples" / "oracle").glob("*.sql"))]
    objects = parse_sources({s.name: s.text for s in sources})
    fixture = load_sample_run()
    schema = FakeSchemaMigrator().migrate(
        sources, env(), expected_tables=[o.object_id for o in objects if o.type.value == "table"],
        expected_sequences=[o.object_id for o in objects if o.type.value == "sequence"])
    repairs = {o.object_id: o.repair for o in fixture.objects if o.repair}
    return sources, objects, schema, repairs, fixture


def _build(**changes):
    from app.orchestration.run_result import build_migration_run

    sources, objects, schema, repairs, fixture = _builder_inputs()
    values = {"run_id": "t1", "started_at": fixture.started_at, "finished_at": fixture.finished_at, "engine": "test",
              "stand_in": True, "oracle_answers": "recorded", "options": MigrationOptions(), "sources": sources,
              "objects": objects, "schema_result": schema, "repairs": repairs}
    return build_migration_run(**(values | changes))


def test_the_builder_gives_every_object_its_outcome():
    run, fixture = _build(), load_sample_run()
    assert [(o.object_id, o.route, o.status) for o in run.objects] == \
        [(o.object_id, o.route, o.status) for o in fixture.objects]
    assert run.outcome("package:legacy.payroll_pkg").reason.startswith("Not deployed on its own")
    assert run.metrics.objects.migrated == 14 and run.metrics.objects.total == 16


def test_the_builder_refuses_a_silent_skip():
    _, _, _, repairs, _ = _builder_inputs()
    repairs.pop(CALC_BONUS)
    with pytest.raises(ValueError, match=f"{CALC_BONUS} has no repair run, schema outcome or reason: a silent skip"):
        _build(repairs=repairs)
    run = _build(repairs=repairs, not_converted={CALC_BONUS: "Not selected."},
                 blocked_by={CALC_BONUS: [EMPLOYEES]})
    assert run.outcome(CALC_BONUS).status is ObjectStatus.NOT_CONVERTED
    assert run.outcome(CALC_BONUS).blocked_by == [EMPLOYEES] and run.outcome(CALC_BONUS).reason == "Not selected."


def test_the_builder_needs_a_schema_outcome_for_each_table():
    sources, objects, schema, _, _ = _builder_inputs()
    partial = schema.model_copy(update={"objects": [o for o in schema.objects if o.object_id != EMPLOYEES]})
    with pytest.raises(ValueError, match=f"no schema outcome for {EMPLOYEES}"):
        _build(schema_result=partial)


def test_a_run_that_did_not_start_converts_nothing():
    problem = "pv_runner lacks USAGE on schema public: run scripts/setup_target_roles.py"
    run = _build(repairs={}, status=RunStatus.NOT_RUN, problems=[problem])
    routines = [o for o in run.objects if o.route is not Route.SCHEMA]
    assert {o.status for o in routines} == {ObjectStatus.NOT_CONVERTED}
    assert all(problem in o.reason for o in routines) and run.metrics.deployed == 0


def test_routes_are_classified_like_the_converter_does():
    from app.orchestration.run_result import routes_for

    _, objects, _, _, _ = _builder_inputs()
    routes = routes_for(objects)
    assert routes[ORG_CHART] is Route.GEMINI and routes[CALC_BONUS] is Route.RULES
    assert routes["package:legacy.payroll_pkg"] is Route.NONE and routes[EMPLOYEES] is Route.SCHEMA
    assert routes_for(parse_extra())["procedure:legacy.dyn_sql"] is Route.REVIEW


def parse_extra():
    from app.parsers.oracle_parser import parse_sources

    return parse_sources({"x.sql": "CREATE OR REPLACE PROCEDURE dyn_sql AS BEGIN "
                                   "EXECUTE IMMEDIATE 'DELETE FROM employees'; END;\n/\n"})


def test_call_records_become_gemini_calls():
    from app.orchestration.run_result import gemini_calls
    from app.services.llm_service import CallRecord

    [call] = gemini_calls([CallRecord("gemini-3.8-flash", 1, 900, "http_504", 120, 4000, None, "not_stored")])
    assert call.outcome == "http_504" and call.response_chars is None and call.is_request


# --- rules found untested by the mutation check ----------------------------------------------------

def test_source_names_are_unique_and_outcomes_name_a_source_of_the_run():
    data = sample()
    data["sources"].append(copy.deepcopy(data["sources"][0]))
    refused(data, "source names must be unique")
    data = sample()
    outcome(data, CALC_BONUS)["source_file"] = "99_other.sql"
    refused(data, f"{CALC_BONUS} names a source that is not in the run: 99_other.sql")


def test_problems_and_schema_reasons_must_not_be_blank():
    data = sample()
    data["problems"] = [" "]
    refused(data, "a problem must not be blank")
    data = sample()
    data["schema_result"]["problems"] = [""]
    refused(data, "a problem must not be blank")
    data = sample()
    data["schema_result"]["objects"][0]["reason"] = "  "
    refused(data, "must not be blank")


def test_a_schema_result_reports_each_object_once():
    data = sample()["schema_result"]
    data["objects"].append(copy.deepcopy(data["objects"][0]))
    with pytest.raises(ValidationError, match="more than one outcome for"):
        SchemaMigrationResult.model_validate(data)


def test_blocked_by_takes_object_ids():
    with pytest.raises(ValidationError, match="blocked_by takes object_ids"):
        ObjectOutcome(object_id=CALC_BONUS, type="procedure", route="rules", status="not_converted",
                      reason="blocked", blocked_by=["employees"])


def test_passed_needs_every_test_of_the_final_version_to_pass():
    run = load_sample_run().outcome(CALC_BONUS).repair
    tested = run.final_validation
    one_failed = tested.model_copy(update={"passed": tested.passed - 1})
    no_tests = tested.model_copy(update={"tests": [], "passed": 0})
    review = ObjectStatus.PASSED_NEEDS_REVIEW
    assert status_for_repair(run.model_copy(update={"final_validation": one_failed})) is review
    assert status_for_repair(run.model_copy(update={"final_validation": no_tests})) is review


def test_errors_never_echo_what_was_passed_in():
    with pytest.raises(ValidationError) as error:
        env(deployer_password="dep-SECRET-9")
    assert "Extra inputs are not permitted" in str(error.value) and "SECRET" not in str(error.value)


def _outcomes(**changes) -> list[ObjectOutcome]:
    """The fixture's outcomes, with calc_bonus's changed (validated as an ObjectOutcome on its own)."""
    run = load_sample_run()
    out = []
    for o in run.objects:
        if o.object_id == CALC_BONUS:
            o = ObjectOutcome.model_validate(o.model_dump() | changes)
        out.append(o)
    return out


def test_the_evidence_metrics_count_what_they_say():
    run = load_sample_run()
    repair = run.outcome(CALC_BONUS).repair
    untested = repair.model_copy(update={"final_validation": None})
    metrics = RunMetrics.of(_outcomes(repair=untested.model_dump(), status="passed_needs_review"),
                            run.schema_result, 16)
    assert (metrics.deployed, metrics.validated) == (8, 7)
    empty = repair.final_validation.model_copy(update={"tests": [], "passed": 0})
    metrics = RunMetrics.of(_outcomes(repair=repair.model_copy(update={"final_validation": empty}).model_dump(),
                                      status="passed_needs_review"), run.schema_result, 16)
    assert metrics.validated == 7
    never_deployed = repair.model_copy(update={"last_deployed_version": None})
    assert RunMetrics.of(_outcomes(repair=never_deployed.model_dump()), run.schema_result, 16).deployed == 7


def test_unexplained_lists_errors_and_harness_or_converter_failures():
    run = load_sample_run()
    errored = _outcomes(repair=None, status="error", reason="Engine error: KeyError")
    assert RunMetrics.of(errored, run.schema_result, 16).unexplained == [CALC_BONUS]
    harness = run.outcome(ORG_CHART).repair.model_copy(update={"category": FailureCategory.HARNESS_ERROR})
    objects = [ObjectOutcome.model_validate(o.model_dump() | {"repair": harness.model_dump(),
                                                              "category": "harness_error"})
               if o.object_id == ORG_CHART else o for o in run.objects]
    assert RunMetrics.of(objects, run.schema_result, 16).unexplained == [ORG_CHART]


def test_reported_and_source_counts_are_kept_apart():
    run = load_sample_run()
    metrics = RunMetrics.of(list(run.objects), run.schema_result, 17)
    assert (metrics.source_objects, metrics.reported_objects) == (17, 16)


def test_parity_is_verified_only_when_every_table_matches_in_rows_and_checksum():
    same = ParityCheck(oracle_rows=12, target_rows=12, oracle_checksum="a", target_checksum="a")
    assert not ParityCheck(oracle_rows=12, target_rows=11).matches
    assert not ParityCheck(oracle_rows=12, target_rows=11, oracle_checksum="a", target_checksum="a").matches
    other = SchemaObjectOutcome(object_id="table:legacy.departments", status="created", reason="r",
                                definition=TableDef(object_id="table:legacy.departments", target_name="departments",
                                                    columns=[ColumnDef(name="d", oracle_type="NUMBER",
                                                                       pg_type="numeric")]))
    partly = SchemaMigrationResult(mode="migrated", migrator="m", database="t",
                                   objects=[_table_outcome("created", same, rows=12), other])
    assert partly.parity == "not_checked"


# --- Aarsh's v0.7 review, 19 Nov: exact version, and parity needs checksum evidence ------


@pytest.mark.parametrize("version", ["0.8", "0.6", "99.99", "1.0"])
def test_a_run_for_another_contract_version_is_refused(version):
    data = sample()
    data["contract_version"] = version
    with pytest.raises(ValidationError, match=f"this is contract 0.7; a run written for contract {version}"):
        MigrationRun.model_validate(data)


def test_the_sample_run_carries_exactly_this_version():
    assert load_sample_run().contract_version == "0.7"
    assert MigrationRun.model_json_schema()["properties"]["contract_version"]["default"] == "0.7"


def test_equal_row_counts_without_checksums_are_not_a_match():
    rows_only = ParityCheck(oracle_rows=12, target_rows=12)
    assert not rows_only.matches and not rows_only.differs and not rows_only.checksummed
    assert ParityCheck(oracle_rows=12, target_rows=12, oracle_checksum="a", target_checksum="a").matches
    assert ParityCheck(oracle_rows=12, target_rows=12, oracle_checksum="a", target_checksum="b").differs


def test_row_counts_alone_give_rows_only_never_verified():
    rows_only = ParityCheck(oracle_rows=12, target_rows=12)
    full = ParityCheck(oracle_rows=12, target_rows=12, oracle_checksum="a", target_checksum="a")
    other = SchemaObjectOutcome(object_id="table:legacy.departments", status="created", reason="r",
                                definition=TableDef(object_id="table:legacy.departments", target_name="departments",
                                                    columns=[ColumnDef(name="d", oracle_type="NUMBER",
                                                                       pg_type="numeric")]),
                                parity=rows_only, rows_copied=4)
    result = SchemaMigrationResult(mode="migrated", migrator="m", database="t",
                                   objects=[_table_outcome("created", full, rows=12), other])
    assert result.parity == "rows_only" and result.ok   # created and usable, but A15 is not verified
    alone = SchemaMigrationResult(mode="migrated", migrator="m", database="t",
                                  objects=[_table_outcome("created", rows_only, rows=12)])
    assert alone.parity == "rows_only"
    metrics = RunMetrics.of([], result, 0)
    assert metrics.schema_parity == "rows_only"

"""Step 7a: RepairContext (D-6.7), Gemini repairs through the repair loop, stopping rules,
attempt history and last-known-good deployment.

No test calls Gemini: every Gemini answer comes from a recording fake ``generate``. Unit
tests use fake converters or a fake deployer; the PostgreSQL tests at the end use the real
restricted deployer (``pv_deployer``) and runner (``pv_runner``) on a throwaway database.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import settings
from app.contracts import (
    ConversionResult,
    ConversionTool,
    ExecutionResult,
    FailureReport,
    FieldDiff,
    IRObject,
    PreviousAttempt,
    RepairContext,
    TestCase,
    TestOutcome,
    TestStatus,
    ValidationResult,
    ValidationStatus,
)
from app.migration.converter import OracleToPostgresConverter
from app.migration.llm_fallback import GeminiFallback
from app.migration.postgres_deployer import DeployResult, PostgresDeployer
from app.migration.target_roles import DEPLOYER_ROLE, RUNNER_ROLE, ensure_roles
from app.orchestration.repair_loop import (
    FailureCategory,
    RepairDeps,
    RepairOutcome,
    RepairRun,
    can_retry,
    failure_category,
    repair_context,
    run_repair_loop,
)
from app.parsers.oracle_parser import parse_sources
from tests.test_converter_gemini import EXTRA, ORG, ORG_SQL, SAY_HI_SQL, FakeDeployer, answer

ROOT = Path(__file__).resolve().parents[2]
MODEL = "gemini-test-model-7a"
OBJ = IRObject(object_id="procedure:legacy.calc_bonus", name="calc_bonus", type="procedure", source_code="...")
CASES = [TestCase(test_id="t-01", object_id=OBJ.object_id, description="t")]

# Three different, deployable versions of org_chart for Gemini to "write".
ORG_V1 = ORG_SQL.replace("1 AS depth", "0 AS depth")              # deploys, but LEVEL is off by one
ORG_V2 = ORG_SQL.replace("t.depth + 1", "t.no_such_column + 1")    # PostgreSQL rejects it (42703)
ORG_V3 = ORG_SQL                                                   # correct


# --- helpers -------------------------------------------------------------------------

def outcome(test_id, status, diffs=()):
    return TestOutcome(test_id=test_id, status=status, source=ExecutionResult(engine="oracle", test_id=test_id),
                       target=ExecutionResult(engine="postgres", test_id=test_id), diffs=list(diffs))


def validation(status, version, object_id=OBJ.object_id):
    failed = status != ValidationStatus.PASSED
    test = outcome("t-01", TestStatus.FAILED if failed else TestStatus.PASSED,
                   [FieldDiff(path="rows[3].depth", source_value=4, target_value=3)] if failed else [])
    if status == ValidationStatus.NEEDS_HUMAN:
        test = outcome("t-01", TestStatus.ERROR)
    return ValidationResult(object_id=object_id, conversion_version=version, tests=[test],
                            passed=0 if failed else 1, failed=1 if status == ValidationStatus.REQUIRES_REPAIR else 0,
                            status=status)


def by_version(statuses: dict[int, ValidationStatus]):
    """A validate() that answers per conversion version, recording what it saw."""
    seen = []

    def validate(obj, cases, version):
        seen.append(version)
        return validation(statuses[version], version, obj.object_id)

    validate.seen = seen
    return validate


class ScriptConverter:
    """A converter whose attempts are scripted: {version: (compiled_ok, failure_code, needs_review, tool)}."""

    def __init__(self, script=None, default=(True, None, False, ConversionTool.MANUAL)):
        self.script = script or {}
        self.default = default
        self.calls: list[dict] = []

    def convert(self, obj, feedback=None, **kwargs):
        version = len(self.calls) + 1
        self.calls.append({"feedback": feedback, **kwargs})
        ok, code, review, tool = self.script.get(version, self.default)
        return ConversionResult(object_id=obj.object_id, version=version, tool=tool, target_code=f"-- v{version}",
                                compiled_ok=ok, compile_error=None if ok else f"failed v{version}",
                                failure_code=None if ok else code, needs_review=review,
                                model=MODEL if tool is ConversionTool.GEMINI else None)


def loop(converter, validate, max_attempts=3, run_id=None, obj=OBJ, cases=CASES):
    return run_repair_loop(RepairDeps(converter=converter, validate=validate, max_attempts=max_attempts), obj,
                           cases, run_id=run_id)


class SequenceGenerate:
    """A fake Gemini that answers each call with the next reply."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[tuple[str, str | None]] = []

    def __call__(self, prompt, *, system=None):
        self.calls.append((prompt, system))
        return self.replies[min(len(self.calls), len(self.replies)) - 1]


@pytest.fixture(autouse=True)
def configured_model(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_MODEL", MODEL)


@pytest.fixture(scope="module")
def objects():
    sources = {p.name: p.read_text() for p in sorted((ROOT / "samples" / "oracle").glob("*.sql"))}
    sources["99_extra.sql"] = EXTRA
    return {o.object_id: o for o in parse_sources(sources)}


def gemini_converter(objects, *replies, deployer=None):
    fake = SequenceGenerate(*replies)
    deployer = deployer or FakeDeployer()
    return OracleToPostgresConverter(deployer, objects.values(), llm=GeminiFallback(fake)), fake, deployer


# --- 1. contracts (D-6.7, contract 0.4) ---------------------------------------------

def previous(version=1, deployed=True, error=None, code="CREATE v1"):
    return PreviousAttempt(version=version, tool=ConversionTool.GEMINI, code=code, deployed=deployed,
                           deploy_error=error)


def test_a_valid_repair_context_round_trips():
    ctx = RepairContext(run_id="r1", object_id=OBJ.object_id, attempt=2, previous=previous(),
                        last_validation=validation(ValidationStatus.REQUIRES_REPAIR, 1))
    assert RepairContext.model_validate_json(ctx.model_dump_json()) == ctx


@pytest.mark.parametrize("kwargs, message", [
    ({"last_validation": validation(ValidationStatus.REQUIRES_REPAIR, 1, "function:legacy.other")},
     "the last test run is for function:legacy.other, not procedure:legacy.calc_bonus"),
    ({"last_validation": validation(ValidationStatus.REQUIRES_REPAIR, 2)},
     "the last test run is for v2, later than the previous attempt v1"),
    ({"previous": previous(deployed=False, error="x"), "last_validation": validation(
        ValidationStatus.REQUIRES_REPAIR, 1)}, "v1 did not deploy, so it cannot have been tested"),
    ({"attempt": 1}, "greater than or equal to 2"),
    ({"run_id": ""}, "at least 1 character"),
])
def test_inconsistent_repair_contexts_are_rejected(kwargs, message):
    base = {"run_id": "r1", "object_id": OBJ.object_id, "attempt": 2, "previous": previous()}
    with pytest.raises(ValidationError, match=message):
        RepairContext(**(base | kwargs))


def test_a_deployed_previous_attempt_has_no_deploy_error():
    with pytest.raises(ValidationError, match="a deployed attempt has no deploy error"):
        previous(deployed=True, error="boom")
    with pytest.raises(ValidationError):
        previous(version=0)


def test_older_serialized_results_still_load():
    v03 = {"object_id": "x", "version": 1, "tool": "gemini", "target_code": "", "compiled_ok": False,
           "compile_error": "e", "warnings": [], "needs_review": True, "model": "m",
           "produced_at": "2026-11-15T00:00:00Z"}
    assert ConversionResult.model_validate(v03).failure_code is None
    old_run = {"object_id": "x", "outcome": "needs_human", "reason": "Still not compiling after 3 attempts.",
               "attempts": [{"attempt": 1, "version": 1, "tool": "manual", "compiled_ok": False}],
               "final_conversion": None, "final_validation": None}
    run = RepairRun.model_validate_json(json.dumps(old_run))
    assert run.run_id is None and run.category is None and run.last_deployed_version is None
    assert run.attempts[0].failure_code is None and run.attempts[0].category is None


# --- 2. the loop's policy: what a repair gets, when it stops -------------------------

def test_the_first_attempt_carries_only_its_run_id():
    conv = ScriptConverter()
    loop(conv, by_version({1: ValidationStatus.PASSED}), run_id="run-1")
    assert conv.calls == [{"feedback": None, "run_id": "run-1"}]  # no feedback, no context on attempt 1


def test_each_repair_gets_a_fresh_context_for_this_run():
    conv = ScriptConverter()
    run = loop(conv, by_version({1: ValidationStatus.REQUIRES_REPAIR, 2: ValidationStatus.REQUIRES_REPAIR,
                                 3: ValidationStatus.PASSED}), run_id="run-42")
    contexts = [c["context"] for c in conv.calls[1:]]
    assert [c.attempt for c in contexts] == [2, 3] and {c.run_id for c in contexts} == {"run-42"}
    assert [c.previous.version for c in contexts] == [1, 2]
    assert [c.last_validation.conversion_version for c in contexts] == [1, 2]
    assert [c["feedback"].attempt for c in conv.calls[1:]] == [1, 2]
    assert [c["run_id"] for c in conv.calls] == ["run-42"] * 3  # every attempt names the same run
    assert run.run_id == "run-42" and run.outcome is RepairOutcome.PASSED


def test_two_runs_get_different_run_ids():
    assert loop(ScriptConverter(), by_version({1: ValidationStatus.PASSED})).run_id != \
        loop(ScriptConverter(), by_version({1: ValidationStatus.PASSED})).run_id


def test_evidence_stays_tied_to_the_version_it_tested():
    """v1 fails tests, v2 does not deploy, v3 is repaired from v2 with v1's test run (K3)."""
    conv = ScriptConverter({2: (False, "gemini.deploy_failed", True, ConversionTool.GEMINI),
                            3: (False, "gemini.deploy_failed", True, ConversionTool.GEMINI)})
    run = loop(conv, by_version({1: ValidationStatus.REQUIRES_REPAIR}))
    third = conv.calls[2]
    assert third["context"].previous.version == 2 and third["context"].previous.deployed is False
    assert third["context"].previous.deploy_error == "failed v2"
    assert third["context"].last_validation.conversion_version == 1
    assert third["feedback"].attempt == 1  # v1's failing tests, never a deploy error
    assert run.final_validation is None and run.last_validation.conversion_version == 1
    assert run.last_deployed_version == 1 and run.category is FailureCategory.DEPLOY_FAILED
    assert run.reason == ("Still not compiling after 3 attempts: v3: failed v3 "
                          "Last test run: v1, 0 passed, 1 failed.")


@pytest.mark.parametrize("code, category", [
    ("review_required", FailureCategory.UNSUPPORTED),
    ("translator_gap", FailureCategory.UNSUPPORTED),
    ("deploy_failed", FailureCategory.DEPLOY_FAILED),           # deterministic: same code again, so stop
    ("deploy_refused", FailureCategory.DEPLOY_REFUSED),
    ("gemini.refused", FailureCategory.DEPLOY_REFUSED),
    ("gemini.deploy_refused", FailureCategory.DEPLOY_REFUSED),
    ("gemini.unchanged_output", FailureCategory.UNCHANGED_OUTPUT),
    ("gemini.invalid_evidence", FailureCategory.INVALID_EVIDENCE),
    ("gemini.malformed_response", FailureCategory.GEMINI_FAILED),
    ("gemini.api_error", FailureCategory.GEMINI_FAILED),
    ("gemini.not_configured", FailureCategory.GEMINI_FAILED),
    ("gemini.cache_error", FailureCategory.GEMINI_FAILED),
    ("gemini.review_requested", FailureCategory.GEMINI_FAILED),
    ("gemini.unsupported", FailureCategory.GEMINI_FAILED),
    ("gemini.secret_in_source", FailureCategory.UNSUPPORTED),
    ("gemini.not_eligible", FailureCategory.UNSUPPORTED),
    ("gemini.repair_unavailable", FailureCategory.NO_REPAIR_ROUTE),
    ("gemini.converter_error", FailureCategory.CONVERTER_ERROR),
    ("converter_error", FailureCategory.CONVERTER_ERROR),
])
def test_permanent_failures_stop_at_once_with_the_right_category(code, category):
    conv = ScriptConverter({1: (False, code, True, ConversionTool.GEMINI)})
    run = loop(conv, by_version({}))
    assert len(conv.calls) == 1 and len(run.attempts) == 1
    assert run.outcome is RepairOutcome.NEEDS_HUMAN and run.category is category
    assert run.reason == f"Stopped at attempt 1 ({category.value}): failed v1"
    assert run.attempts[0].failure_code == code and run.attempts[0].category is category


def test_a_rejected_gemini_deploy_is_retried_up_to_the_limit():
    conv = ScriptConverter(default=(False, "gemini.deploy_failed", True, ConversionTool.GEMINI))
    run = loop(conv, by_version({}))
    assert len(conv.calls) == 3 and run.category is FailureCategory.DEPLOY_FAILED
    assert run.reason == "Still not compiling after 3 attempts: v3: failed v3"
    assert all(a.model == MODEL and a.needs_review for a in run.attempts)


def test_review_without_a_failure_code_still_stops_d5():
    conv = ScriptConverter({1: (False, None, True, ConversionTool.MANUAL)})
    run = loop(conv, by_version({}))
    assert len(conv.calls) == 1 and run.category is FailureCategory.UNSUPPORTED


def test_legacy_converters_without_codes_keep_retrying():
    conv = ScriptConverter({1: (False, None, False, ConversionTool.MANUAL)})
    run = loop(conv, by_version({2: ValidationStatus.PASSED}))
    assert len(conv.calls) == 2 and run.outcome is RepairOutcome.PASSED


def test_failing_tests_with_no_repair_route_say_so():
    conv = ScriptConverter({2: (False, "no_repair_route", False, ConversionTool.SQLGLOT)},
                           default=(True, None, False, ConversionTool.SQLGLOT))
    run = loop(conv, by_version({1: ValidationStatus.REQUIRES_REPAIR}))
    assert len(conv.calls) == 2 and run.category is FailureCategory.TESTS_FAILED
    assert run.reason == "v1 fails 1 tests and there is no repair route for this object: failed v2"
    assert run.final_validation is None and run.last_validation.conversion_version == 1
    assert run.last_deployed_version == 1


def test_tests_still_failing_after_the_last_attempt():
    run = loop(ScriptConverter(), by_version({v: ValidationStatus.REQUIRES_REPAIR for v in (1, 2, 3)}))
    assert run.category is FailureCategory.TESTS_FAILED
    assert run.reason == "Still 1 failing tests on v3 after 3 attempts."
    assert run.final_validation.conversion_version == 3 and run.last_deployed_version == 3


def test_a_harness_error_stops_at_once():
    conv = ScriptConverter()
    run = loop(conv, by_version({1: ValidationStatus.NEEDS_HUMAN}))
    assert len(conv.calls) == 1 and run.category is FailureCategory.HARNESS_ERROR
    assert run.attempts[0].category is FailureCategory.HARNESS_ERROR


def test_a_pass_has_no_category_and_records_everything():
    run = loop(ScriptConverter({1: (False, "gemini.deploy_failed", True, ConversionTool.GEMINI)},
                               default=(True, None, True, ConversionTool.GEMINI)),
               by_version({2: ValidationStatus.PASSED}))
    assert run.outcome is RepairOutcome.PASSED_NEEDS_REVIEW and run.category is None
    first, second = run.attempts
    assert (first.compiled_ok, first.failure_code, first.category, first.model) == (
        False, "gemini.deploy_failed", FailureCategory.DEPLOY_FAILED, MODEL)
    assert (second.compiled_ok, second.status, second.category, second.needs_review) == (
        True, ValidationStatus.PASSED, None, True)
    assert run.final_validation.conversion_version == 2 and run.last_deployed_version == 2


def test_can_retry_and_failure_category_are_consistent():
    ok = ConversionResult(object_id="x", version=1, tool=ConversionTool.GEMINI, target_code="", compiled_ok=True)
    assert failure_category(ok) is None
    retry = ok.model_copy(update={"compiled_ok": False, "failure_code": "gemini.deploy_failed", "needs_review": True})
    assert can_retry(retry) and failure_category(retry) is FailureCategory.DEPLOY_FAILED
    assert not can_retry(retry.model_copy(update={"failure_code": "deploy_failed"}))


# --- 3. Gemini repairs through the real converter (fake Gemini, fake deployer) ---------

def org_validate(statuses):
    return by_version(statuses)


def test_a_failed_gemini_view_is_repaired_by_gemini(objects):
    converter, fake, deployer = gemini_converter(objects, answer(ORG_V1), answer(ORG_V3))
    run = loop(converter, org_validate({1: ValidationStatus.REQUIRES_REPAIR, 2: ValidationStatus.PASSED}),
               obj=objects[ORG])
    assert run.outcome is RepairOutcome.PASSED_NEEDS_REVIEW and len(fake.calls) == 2
    assert [code for code, _ in deployer.calls] == [ORG_V1, ORG_V3]
    repair_prompt = fake.calls[1][0]
    assert "## Latest attempt (v1)" in repair_prompt and "0 AS depth" in repair_prompt
    assert "## Last test run (v1)" in repair_prompt and "rows[3].depth (value): Oracle=4 PostgreSQL=3" in repair_prompt
    assert run.final_conversion.warnings[1] == "Repair of v1"
    assert all(a.tool is ConversionTool.GEMINI and a.model == MODEL for a in run.attempts)


def test_a_rejected_repair_is_repaired_with_its_deploy_error(objects):
    failing = FakeDeployer()
    results = iter([DeployResult(ok=True), DeployResult(ok=False, error="42703: no_such_column", sqlstate="42703"),
                    DeployResult(ok=True)])
    failing.deploy = lambda code, target: failing.calls.append((code, target)) or next(results)
    converter, fake, _ = gemini_converter(objects, answer(ORG_V1), answer(ORG_V2), answer(ORG_V3),
                                          deployer=failing)
    run = loop(converter, org_validate({1: ValidationStatus.REQUIRES_REPAIR, 3: ValidationStatus.PASSED}),
               obj=objects[ORG])
    assert run.outcome is RepairOutcome.PASSED_NEEDS_REVIEW and len(fake.calls) == 3
    third = fake.calls[2][0]
    assert "v2 did NOT deploy. PostgreSQL said: Gemini deploy_failed: 42703: no_such_column" in third
    assert "## Last test run (v1)" in third and "These results are for v1, not v2" in third
    assert [a.compiled_ok for a in run.attempts] == [True, False, True]


def test_gemini_is_called_at_most_once_per_attempt(objects):
    v3 = ORG_SQL.replace("CAST(' > ' || full_name AS text)", "CAST(' > ' || full_name AS varchar)")
    converter, fake, _ = gemini_converter(objects, answer(ORG_V1), answer(ORG_V3), answer(v3))
    run = loop(converter, org_validate({v: ValidationStatus.REQUIRES_REPAIR for v in (1, 2, 3)}),
               obj=objects[ORG])
    assert len(fake.calls) == 3 and run.category is FailureCategory.TESTS_FAILED


def test_unchanged_repair_output_is_never_deployed(objects):
    converter, fake, deployer = gemini_converter(objects, answer(ORG_V1), answer(ORG_V1.replace("\n", "\n  ")))
    run = loop(converter, org_validate({1: ValidationStatus.REQUIRES_REPAIR}), obj=objects[ORG])
    assert len(fake.calls) == 2 and len(deployer.calls) == 1
    assert run.category is FailureCategory.UNCHANGED_OUTPUT and run.last_deployed_version == 1
    assert run.reason.startswith("Stopped at attempt 2 (unchanged_output): Gemini unchanged_output:")


def test_a_refused_repair_stops_and_keeps_the_deployed_version(objects):
    converter, fake, deployer = gemini_converter(objects, answer(ORG_V1),
                                                 answer(ORG_V3 + "DROP TABLE salary_audit;\n"))
    run = loop(converter, org_validate({1: ValidationStatus.REQUIRES_REPAIR}), obj=objects[ORG])
    assert len(fake.calls) == 2 and len(deployer.calls) == 1
    assert run.category is FailureCategory.DEPLOY_REFUSED and run.last_deployed_version == 1
    assert run.final_validation is None and run.last_validation.conversion_version == 1


def test_review_objects_never_reach_gemini_on_repair_either(objects):
    converter, fake, deployer = gemini_converter(objects, answer())
    ctx = RepairContext(run_id="r", object_id="procedure:legacy.dyn_sql", attempt=2, previous=previous())
    result = converter.convert(objects["procedure:legacy.dyn_sql"], None, context=ctx)
    assert result.failure_code == "review_required" and fake.calls == [] and deployer.calls == []


def test_a_deterministic_object_has_no_repair_route(objects):
    converter, fake, deployer = gemini_converter(objects, answer())
    calc = objects["procedure:legacy.calc_bonus"]
    first = converter.convert(calc, run_id="r")
    ctx = repair_context("r", calc, 2, first, validation(ValidationStatus.REQUIRES_REPAIR, 1, calc.object_id))
    retry = converter.convert(calc, None, context=ctx)
    assert retry.failure_code == "no_repair_route" and retry.tool is ConversionTool.SQLGLOT
    assert fake.calls == [] and len(deployer.calls) == 1


@pytest.mark.parametrize("deploy_result, code", [
    (DeployResult(ok=False, error="42601: syntax error", sqlstate="42601"), "deploy_failed"),
    (DeployResult(ok=False, error="Refused: open transaction", refused=True), "deploy_refused"),
])
def test_a_failed_deterministic_deploy_stops_at_once(objects, deploy_result, code):
    """The same source gives the same code: retrying a deterministic deploy cannot help."""
    calc = objects["procedure:legacy.calc_bonus"]
    converter, fake, deployer = gemini_converter(objects, answer(), deployer=FakeDeployer(deploy_result))
    assert converter.convert(calc).failure_code == code
    converter, fake, deployer = gemini_converter(objects, answer(), deployer=FakeDeployer(deploy_result))
    run = loop(converter, by_version({}), obj=calc)
    assert len(run.attempts) == 1 and len(deployer.calls) == 1 and fake.calls == []
    assert run.category is (FailureCategory.DEPLOY_FAILED if code == "deploy_failed" else
                            FailureCategory.DEPLOY_REFUSED)


def test_repair_evidence_never_grants_eligibility(objects, monkeypatch):
    import app.migration.llm_fallback as lf

    converter, fake, _ = gemini_converter(objects, answer(ORG_V1), answer(ORG_V3))
    first = converter.convert(objects[ORG], run_id="r")
    monkeypatch.setattr(lf, "ELIGIBLE_FEATURES", frozenset())
    retry = converter.convert(objects[ORG], None, context=repair_context("r", objects[ORG], 2, first, None))
    assert retry.failure_code == "gemini.not_eligible" and len(fake.calls) == 1


def _first_and_context(objects, converter, run_id="r"):
    first = converter.convert(objects[ORG], run_id=run_id)
    return first, repair_context(run_id, objects[ORG], 2, first,
                                 validation(ValidationStatus.REQUIRES_REPAIR, 1, ORG))


@pytest.mark.parametrize("tamper, message", [
    (lambda c: c.model_copy(update={"object_id": "view:legacy.other"}),
     "the repair context is for view:legacy.other, not view:legacy.org_chart"),
    (lambda c: c.model_copy(update={"previous": c.previous.model_copy(update={"code": "CREATE other"})}),
     "v1 in the repair context is not an attempt this converter produced in this run"),
    (lambda c: c.model_copy(update={"previous": c.previous.model_copy(update={"version": 5})}),
     "the previous attempt v5 is not earlier than this attempt v2"),
])
def test_foreign_or_tampered_evidence_is_never_sent(objects, tamper, message):
    converter, fake, deployer = gemini_converter(objects, answer(ORG_V1), answer(ORG_V3))
    _, ctx = _first_and_context(objects, converter)
    result = converter.convert(objects[ORG], None, context=tamper(ctx), run_id="r")
    assert result.failure_code == "gemini.invalid_evidence" and message in result.compile_error
    assert len(fake.calls) == 1 and len(deployer.calls) == 1  # only the first attempt


def test_evidence_from_another_converter_is_refused(objects):
    first_run, _, _ = gemini_converter(objects, answer(ORG_V1))
    _, ctx = _first_and_context(objects, first_run)
    second_run, fake, deployer = gemini_converter(objects, answer(ORG_V3))
    second_run.convert(objects[ORG], run_id="r")  # its own v1 (different code from the first run's)
    second_run_fake_calls = len(fake.calls)
    result = second_run.convert(objects[ORG], None, context=ctx, run_id="r")
    assert result.failure_code == "gemini.invalid_evidence" and len(fake.calls) == second_run_fake_calls


def test_evidence_from_an_earlier_run_on_the_same_converter_is_refused(objects):
    """Run identity is enforced, not only documented: the same converter instance produces
    byte-identical code with the same deploy outcome in two runs, and the first run's
    context is still refused in the second run, before Gemini or the deployer is called."""
    converter, fake, deployer = gemini_converter(objects, answer(ORG_V1))  # same answer every time
    run1_v1, run1_ctx = _first_and_context(objects, converter, run_id="run-1")
    run2_v2 = converter.convert(objects[ORG], run_id="run-2")
    assert run2_v2.version == 2 and run2_v2.target_code == run1_v1.target_code
    assert run1_v1.compiled_ok and run2_v2.compiled_ok and run1_ctx.previous.version == 1
    calls, deploys = len(fake.calls), len(deployer.calls)

    feedback = FailureReport(object_id=ORG, attempt=1, failing_tests=[], summary="oc-01 differs")
    as_is = converter.convert(objects[ORG], feedback, context=run1_ctx, run_id="run-2")
    relabelled = converter.convert(objects[ORG], feedback, context=run1_ctx.model_copy(update={"run_id": "run-2"}),
                                   run_id="run-2")
    assert as_is.failure_code == relabelled.failure_code == "gemini.invalid_evidence"
    assert "belongs to a different run" in as_is.compile_error
    assert "v1 in the repair context was not produced in this run" in relabelled.compile_error
    assert len(fake.calls) == calls and len(deployer.calls) == deploys  # neither Gemini nor PostgreSQL


def test_a_repair_without_an_explicit_run_id_is_refused(objects):
    """The context must never vouch for the attempt that checks it: a valid context from this
    run's own v1, sent without the attempt's run_id, is refused before Gemini or the deployer."""
    converter, fake, deployer = gemini_converter(objects, answer(ORG_V1), answer(ORG_V3))
    first, ctx = _first_and_context(objects, converter, run_id="run-1")
    assert first.compiled_ok and ctx.run_id == "run-1"
    feedback = FailureReport(object_id=ORG, attempt=1, failing_tests=[], summary="oc-01 differs")
    result = converter.convert(objects[ORG], feedback, context=ctx)  # no run_id
    assert result.failure_code == "gemini.invalid_evidence"
    assert "a repair needs the run_id of the attempt being made" in result.compile_error
    assert len(fake.calls) == 1 and len(deployer.calls) == 1  # only the first attempt
    accepted = converter.convert(objects[ORG], feedback, context=ctx, run_id="run-1")  # control: same context
    assert accepted.compiled_ok and len(fake.calls) == 2


def test_an_attempt_made_without_a_run_id_is_never_repaired(objects):
    converter, fake, deployer = gemini_converter(objects, answer(ORG_V1), answer(ORG_V3))
    first = converter.convert(objects[ORG])  # a caller that never repairs: no run_id, as before 7a
    assert first.compiled_ok
    result = converter.convert(objects[ORG], None, context=repair_context("r", objects[ORG], 2, first, None),
                               run_id="r")
    assert result.failure_code == "gemini.invalid_evidence" and len(fake.calls) == 1 and len(deployer.calls) == 1


def test_a_context_for_another_run_than_the_attempt_is_refused(objects):
    converter, fake, deployer = gemini_converter(objects, answer(ORG_V1), answer(ORG_V3))
    _, ctx = _first_and_context(objects, converter, run_id="run-1")
    result = converter.convert(objects[ORG], None, context=ctx, run_id="run-2")
    assert result.failure_code == "gemini.invalid_evidence" and "belongs to a different run" in result.compile_error
    assert len(fake.calls) == 1 and len(deployer.calls) == 1


def test_one_converter_repairs_correctly_across_two_runs(objects):
    """Each run's own evidence is accepted; versions keep counting across runs."""
    converter, fake, deployer = gemini_converter(objects, answer(ORG_V1), answer(ORG_V3), answer(ORG_V1),
                                                 answer(ORG_V3))
    statuses = {1: ValidationStatus.REQUIRES_REPAIR, 2: ValidationStatus.PASSED,
                3: ValidationStatus.REQUIRES_REPAIR, 4: ValidationStatus.PASSED}
    first = loop(converter, org_validate(statuses), obj=objects[ORG], run_id="run-1")
    second = loop(converter, org_validate(statuses), obj=objects[ORG], run_id="run-2")
    assert first.outcome is second.outcome is RepairOutcome.PASSED_NEEDS_REVIEW
    assert [a.version for a in second.attempts] == [3, 4] and second.final_conversion.warnings[1] == "Repair of v3"
    assert len(fake.calls) == 4 and len(deployer.calls) == 4


def test_a_repair_without_context_is_still_refused(objects):
    converter, fake, _ = gemini_converter(objects, answer(ORG_V1))
    converter.convert(objects[ORG])
    feedback = FailureReport(object_id=ORG, attempt=1, failing_tests=[], summary="oc-01 differs")
    retry = converter.convert(objects[ORG], feedback)
    assert retry.failure_code == "gemini.repair_unavailable" and len(fake.calls) == 1


def test_a_function_repair_keeps_its_target(objects):
    converter, fake, deployer = gemini_converter(objects, answer(SAY_HI_SQL.replace("RETURN 1", "RETURN 2")),
                                                 answer(SAY_HI_SQL))
    run = loop(converter, org_validate({1: ValidationStatus.REQUIRES_REPAIR, 2: ValidationStatus.PASSED}),
               obj=objects["function:legacy.say_hi"])
    assert run.outcome is RepairOutcome.PASSED_NEEDS_REVIEW
    assert {target.name for _, target in deployer.calls} == {"say_hi"}


# --- 4. PostgreSQL: last known good, through the real restricted deployer -------------

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


class WatchingDeployer:
    """The real deployer, plus a snapshot of the live view after every deploy call."""

    def __init__(self, real: PostgresDeployer, runner_conn):
        self.real, self.runner, self.after = real, runner_conn, []

    def deploy(self, code, target):
        result = self.real.deploy(code, target)
        self.after.append((result.ok, live_view(self.runner)))
        return result


def live_view(runner_conn):
    row = runner_conn.execute("SELECT pg_get_viewdef(to_regclass('public.org_chart'))").fetchone()
    depth = runner_conn.execute("SELECT depth FROM org_chart WHERE emp_id = 108").fetchone() if row[0] else None
    runner_conn.rollback()
    return row[0], depth[0] if depth else None


def db_validate(runner_conn):
    """Pass only when emp 108 has Oracle's LEVEL (4); the rows are read as pv_runner."""
    seen = []

    def validate(obj, cases, version):
        _, depth = live_view(runner_conn)
        seen.append((version, depth))
        status = ValidationStatus.PASSED if depth == 4 else ValidationStatus.REQUIRES_REPAIR
        return validation(status, version, obj.object_id)

    validate.seen = seen
    return validate


def test_db_a_rejected_repair_leaves_the_last_deployed_version_live(objects, database):
    name, deployer_conn, runner_conn = database
    watcher = WatchingDeployer(PostgresDeployer(deployer_conn, database=name), runner_conn)
    fake = SequenceGenerate(answer(ORG_V1), answer(ORG_V2), answer(ORG_V3))
    converter = OracleToPostgresConverter(watcher, objects.values(), llm=GeminiFallback(fake))
    validate = db_validate(runner_conn)
    run = loop(converter, validate, obj=objects[ORG])

    (ok1, live1), (ok2, live2), (ok3, live3) = watcher.after
    assert (ok1, ok2, ok3) == (True, False, True)
    assert live1[1] == 3 and live2 == live1  # v2 was rejected: v1 stayed live, definition unchanged
    assert live3[1] == 4 and live3[0] != live1[0]
    assert validate.seen == [(1, 3), (3, 4)]  # v2 never reached the tests
    assert run.outcome is RepairOutcome.PASSED_NEEDS_REVIEW and run.last_deployed_version == 3
    assert run.attempts[1].failure_code == "gemini.deploy_failed"
    assert run.attempts[1].compile_error.startswith("Gemini deploy_failed: 42703")


def test_db_a_refused_repair_stops_and_v1_stays_live(objects, database):
    name, deployer_conn, runner_conn = database
    watcher = WatchingDeployer(PostgresDeployer(deployer_conn, database=name), runner_conn)
    fake = SequenceGenerate(answer(ORG_V1), answer(ORG_V3 + "DROP TABLE salary_audit;\n"))
    converter = OracleToPostgresConverter(watcher, objects.values(), llm=GeminiFallback(fake))
    run = loop(converter, db_validate(runner_conn), obj=objects[ORG])
    assert len(watcher.after) == 1  # the refused SQL never reached the deployer
    assert live_view(runner_conn) == watcher.after[0][1] and live_view(runner_conn)[1] == 3
    assert run.category is FailureCategory.DEPLOY_REFUSED and run.last_deployed_version == 1
    assert runner_conn.execute("SELECT to_regclass('public.salary_audit') IS NOT NULL").fetchone()[0]
    runner_conn.rollback()


# --- 5. converters written before 7a keep working ---------------------------------------

class LegacyConverter:
    """A converter written against the pre-7a interface: no context, no run_id, no **kwargs."""

    def __init__(self, compiled=None):
        self.compiled = compiled or {}
        self.feedback: list = []

    def convert(self, obj, feedback=None):
        self.feedback.append(feedback)
        version = len(self.feedback)
        ok = self.compiled.get(version, True)
        return ConversionResult(object_id=obj.object_id, version=version, tool=ConversionTool.MANUAL,
                                target_code=f"-- v{version}", compiled_ok=ok,
                                compile_error=None if ok else f"syntax error in v{version}")


def test_a_legacy_converter_is_repaired_after_failing_tests():
    conv = LegacyConverter()
    run = loop(conv, by_version({1: ValidationStatus.REQUIRES_REPAIR, 2: ValidationStatus.PASSED}))
    assert run.outcome is RepairOutcome.PASSED and len(conv.feedback) == 2
    assert conv.feedback[0] is None and conv.feedback[1].attempt == 1 and conv.feedback[1].failing_tests


def test_a_legacy_converter_gets_the_pre_7a_compile_feedback():
    conv = LegacyConverter(compiled={1: False})
    run = loop(conv, by_version({2: ValidationStatus.PASSED}))
    assert run.outcome is RepairOutcome.PASSED and len(conv.feedback) == 2
    sent = conv.feedback[1]
    assert sent.attempt == 1 and sent.failing_tests == []
    assert sent.summary == "The converted code did not compile in PostgreSQL: syntax error in v1"


def test_a_legacy_converter_still_stops_at_three_attempts():
    conv = LegacyConverter(compiled={1: False, 2: False, 3: False})
    run = loop(conv, by_version({}))
    assert len(conv.feedback) == 3 and run.outcome is RepairOutcome.NEEDS_HUMAN
    assert run.reason == "Still not compiling after 3 attempts: v3: syntax error in v3"


def test_keyword_support_is_read_from_each_converter(objects):
    from app.orchestration.repair_loop import _accepted_keywords
    from app.orchestration.scripted_converter import ScriptedConverter

    class KwargsConverter:
        def convert(self, obj, feedback=None, **kwargs): ...

    class ContextOnly:
        def convert(self, obj, feedback=None, *, context=None): ...

    real, _, _ = gemini_converter(objects, answer())
    assert _accepted_keywords(real) == _accepted_keywords(KwargsConverter()) == {"context", "run_id"}
    assert _accepted_keywords(ScriptedConverter.__new__(ScriptedConverter)) == {"context", "run_id"}
    assert _accepted_keywords(LegacyConverter()) == frozenset()
    assert _accepted_keywords(ContextOnly()) == {"context"}


@pytest.mark.parametrize("signature", ["context_only", "run_id_only"])
def test_repair_evidence_goes_only_to_converters_that_can_check_the_run(signature):
    """A RepairContext needs both keywords: a converter accepting only one of them is called
    on the legacy path, with no RepairContext (and, for run_id-only, its run_id)."""
    seen = []

    def result(obj, feedback, context, run_id):
        seen.append({"feedback": feedback, "context": context, "run_id": run_id})
        version = len(seen)
        ok = version != 2
        return ConversionResult(object_id=obj.object_id, version=version, tool=ConversionTool.MANUAL,
                                target_code=f"-- v{version}", compiled_ok=ok,
                                compile_error=None if ok else f"syntax error in v{version}")

    class ContextOnly:
        def convert(self, obj, feedback=None, *, context=None):
            return result(obj, feedback, context, None)

    class RunIdOnly:
        def convert(self, obj, feedback=None, *, run_id=None):
            return result(obj, feedback, None, run_id)

    converter = ContextOnly() if signature == "context_only" else RunIdOnly()
    run = loop(converter, by_version({1: ValidationStatus.REQUIRES_REPAIR, 3: ValidationStatus.PASSED}),
               run_id="run-7")
    assert run.outcome is RepairOutcome.PASSED and len(seen) == 3
    assert all(call["context"] is None for call in seen)  # never any repair evidence
    assert seen[1]["feedback"].attempt == 1 and seen[1]["feedback"].failing_tests  # v1's failing tests
    assert seen[2]["feedback"].summary == "The converted code did not compile in PostgreSQL: syntax error in v2"
    expected_run = None if signature == "context_only" else "run-7"
    assert {call["run_id"] for call in seen} == {expected_run}


def test_a_decorated_converter_is_read_through_its_wrapper(objects):
    """functools.wraps keeps the real signature visible; a plain *args/**kwargs wrapper counts as both."""
    import functools

    from app.orchestration.repair_loop import _accepted_keywords

    def logged(method):
        @functools.wraps(method)
        def wrapper(*args, **kwargs):
            return method(*args, **kwargs)
        return wrapper

    class DecoratedLegacy:
        @logged
        def convert(self, obj, feedback=None): ...

    class DecoratedFull:
        @logged
        def convert(self, obj, feedback=None, *, context=None, run_id=None): ...

    assert _accepted_keywords(DecoratedLegacy()) == frozenset()
    assert _accepted_keywords(DecoratedFull()) == {"context", "run_id"}

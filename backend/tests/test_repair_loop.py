"""Repair loop routing, with a fake converter and a fake validator. No database needed."""

from decimal import Decimal

import pytest

from app.contracts import (
    ConversionResult,
    ConversionTool,
    ExecutionError,
    ExecutionResult,
    FieldDiff,
    IRObject,
    TestCase,
    TestOutcome,
    TestStatus,
    ValidationResult,
    ValidationStatus,
)
from app.orchestration.repair_loop import (
    RepairDeps,
    RepairOutcome,
    run_repair_loop,
    summarize_failures,
)

OBJ = IRObject(object_id="procedure:legacy.calc_bonus", name="calc_bonus", type="procedure",
               source_code="...")
CASES = [TestCase(test_id=f"cb-0{i}", object_id=OBJ.object_id, description="t") for i in (1, 2)]


def outcome(test_id, status, diffs=()):
    return TestOutcome(test_id=test_id, status=status,
                       source=ExecutionResult(engine="oracle", test_id=test_id),
                       target=ExecutionResult(engine="postgres", test_id=test_id),
                       diffs=list(diffs))


BONUS_DIFF = FieldDiff(path="out_params.p_bonus", source_value=Decimal("24000"),
                       target_value=Decimal("6000.00"), kind="value")
ERROR_DIFF = FieldDiff(path="error", source_value="no_data_found", target_value=None, kind="error")


def validation(status, *tests, version=1):
    return ValidationResult(
        object_id=OBJ.object_id, conversion_version=version, tests=list(tests),
        passed=sum(t.status == TestStatus.PASSED for t in tests),
        failed=sum(t.status == TestStatus.FAILED for t in tests), status=status)


PASS = validation(ValidationStatus.PASSED, outcome("cb-01", TestStatus.PASSED), outcome("cb-02", TestStatus.PASSED))
FAIL = validation(ValidationStatus.REQUIRES_REPAIR, outcome("cb-01", TestStatus.PASSED),
                  outcome("cb-02", TestStatus.FAILED, [BONUS_DIFF]))
HARNESS = validation(ValidationStatus.NEEDS_HUMAN, outcome("cb-01", TestStatus.PASSED),
                     outcome("cb-02", TestStatus.ERROR))


class FakeConverter:
    def __init__(self, compiled=None, needs_review=False):
        self.calls = []
        self.compiled = compiled or {}
        self.needs_review = needs_review

    def convert(self, obj, feedback=None):
        self.calls.append(feedback)
        version = len(self.calls)
        ok = self.compiled.get(version, True)
        return ConversionResult(object_id=obj.object_id, version=version, tool=ConversionTool.MANUAL,
                                target_code=f"-- v{version}", compiled_ok=ok,
                                compile_error=None if ok else 'syntax error at or near "ELSIF"',
                                needs_review=self.needs_review)


def scripted_validator(*results):
    queue = list(results)
    seen_versions = []

    def validate(obj, cases, version):
        seen_versions.append(version)
        return queue.pop(0)

    validate.versions = seen_versions
    return validate


def run(converter, validate, max_attempts=3):
    return run_repair_loop(RepairDeps(converter=converter, validate=validate, max_attempts=max_attempts),
                           OBJ, CASES)


def test_passes_on_the_first_attempt_without_feedback():
    conv = FakeConverter()
    result = run(conv, scripted_validator(PASS))
    assert result.outcome == RepairOutcome.PASSED
    assert len(result.attempts) == 1
    assert conv.calls == [None]


def test_failure_is_sent_back_and_the_repair_passes():
    conv, validate = FakeConverter(), scripted_validator(FAIL, PASS)
    result = run(conv, validate)
    assert result.outcome == RepairOutcome.PASSED
    assert [a.status for a in result.attempts] == [ValidationStatus.REQUIRES_REPAIR, ValidationStatus.PASSED]
    assert validate.versions == [1, 2]
    feedback = conv.calls[1]
    assert feedback.attempt == 1
    assert [t.test_id for t in feedback.failing_tests] == ["cb-02"]
    assert "out_params.p_bonus" in feedback.summary
    assert result.attempts[0].feedback_summary == feedback.summary


def test_gives_up_after_max_attempts_and_hands_over_to_a_human():
    conv = FakeConverter()
    result = run(conv, scripted_validator(FAIL, FAIL, FAIL))
    assert result.outcome == RepairOutcome.NEEDS_HUMAN
    assert len(conv.calls) == 3
    assert "after 3 attempts" in result.reason


def test_respects_a_custom_attempt_limit():
    conv = FakeConverter()
    result = run(conv, scripted_validator(FAIL, FAIL), max_attempts=2)
    assert result.outcome == RepairOutcome.NEEDS_HUMAN
    assert len(conv.calls) == 2


def test_harness_errors_go_to_a_human_without_retrying():
    conv = FakeConverter()
    result = run(conv, scripted_validator(HARNESS))
    assert result.outcome == RepairOutcome.NEEDS_HUMAN
    assert len(conv.calls) == 1
    assert "could not judge" in result.reason
    assert result.attempts[0].errors == 1


def test_compile_error_is_fed_back_and_not_validated():
    conv, validate = FakeConverter(compiled={1: False}), scripted_validator(PASS)
    result = run(conv, validate)
    assert result.outcome == RepairOutcome.PASSED
    assert validate.versions == [2]
    assert result.attempts[0].compiled_ok is False
    assert "did not compile" in conv.calls[1].summary
    assert conv.calls[1].failing_tests == []


def test_never_compiling_ends_with_a_human():
    conv = FakeConverter(compiled={1: False, 2: False, 3: False})
    result = run(conv, scripted_validator())
    assert result.outcome == RepairOutcome.NEEDS_HUMAN
    assert "not compiling" in result.reason
    assert result.final_validation is None


def test_passing_code_flagged_for_review_still_needs_sign_off():
    result = run(FakeConverter(needs_review=True), scripted_validator(PASS))
    assert result.outcome == RepairOutcome.PASSED_NEEDS_REVIEW


def test_every_attempt_is_recorded_with_its_version():
    result = run(FakeConverter(compiled={2: False}), scripted_validator(FAIL, PASS))
    assert [(a.attempt, a.version, a.compiled_ok) for a in result.attempts] == [(1, 1, True), (2, 2, False), (3, 3, True)]
    assert result.final_conversion.version == 3


def test_summary_explains_value_and_error_differences():
    summary = summarize_failures(validation(
        ValidationStatus.REQUIRES_REPAIR, outcome("cb-01", TestStatus.PASSED),
        outcome("cb-02", TestStatus.FAILED, [BONUS_DIFF]),
        outcome("cb-06", TestStatus.FAILED, [ERROR_DIFF])))
    assert summary.splitlines()[0] == "2 of 3 tests behave differently from Oracle."
    assert "cb-02: out_params.p_bonus: Oracle 24000, PostgreSQL 6000.00." in summary
    assert "cb-06: Oracle raised no_data_found, PostgreSQL raised nothing." in summary


def test_zero_attempts_is_rejected():
    with pytest.raises(ValueError):
        run(FakeConverter(), scripted_validator(), max_attempts=0)

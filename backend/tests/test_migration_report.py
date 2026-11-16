"""Migration report rendering. No database needed."""

import json
from decimal import Decimal

import pytest

from app.contracts import (
    ConversionResult,
    ConversionTool,
    ExecutionError,
    ExecutionResult,
    TableDiff,
    TestOutcome,
    TestStatus,
    ValidationResult,
    ValidationStatus,
)
from app.orchestration.repair_loop import AttemptRecord, RepairOutcome, RepairRun
from app.reporting.migration_report import ReportSummary, render_report


def outcome(test_id, status, src=None, tgt=None):
    return TestOutcome(test_id=test_id, status=status,
                       source=src or ExecutionResult(engine="oracle", test_id=test_id),
                       target=tgt or ExecutionResult(engine="postgres", test_id=test_id))


def run(object_id, outcome_, attempts, tests, code="CREATE PROCEDURE x() ...", warnings=("Scripted stand-in: ...",)):
    validation = ValidationResult(
        object_id=object_id, conversion_version=len(attempts), tests=tests,
        passed=sum(t.status == TestStatus.PASSED for t in tests),
        failed=sum(t.status == TestStatus.FAILED for t in tests),
        status=ValidationStatus.PASSED if all(t.status == TestStatus.PASSED for t in tests)
        else ValidationStatus.REQUIRES_REPAIR)
    return RepairRun(
        object_id=object_id, outcome=outcome_, reason="reason text", attempts=attempts,
        final_conversion=ConversionResult(object_id=object_id, version=len(attempts), tool=ConversionTool.MANUAL,
                                          target_code=code, compiled_ok=True, warnings=list(warnings)),
        final_validation=validation)


def attempt(n, passed, failed, status, feedback=None):
    return AttemptRecord(attempt=n, version=n, tool=ConversionTool.MANUAL, compiled_ok=True, status=status,
                         passed=passed, failed=failed, feedback_summary=feedback)


BONUS = run(
    "procedure:legacy.calc_bonus", RepairOutcome.PASSED,
    [attempt(1, 1, 1, ValidationStatus.REQUIRES_REPAIR,
             "1 of 2 tests behave differently from Oracle.\n- cb-02: out_params.p_bonus: Oracle 24000, PostgreSQL 6000.00."),
     attempt(2, 2, 0, ValidationStatus.PASSED)],
    [outcome("cb-02", TestStatus.PASSED,
             ExecutionResult(engine="oracle", test_id="cb-02", out_params={"p_bonus": Decimal("24000")}),
             ExecutionResult(engine="postgres", test_id="cb-02", out_params={"p_bonus": Decimal("24000.00")})),
     outcome("cb-06", TestStatus.PASSED,
             ExecutionResult(engine="oracle", test_id="cb-06", error=ExecutionError(code="ORA-01403", message="x")),
             ExecutionResult(engine="postgres", test_id="cb-06", error=ExecutionError(code="P0002", message="y")))],
    code="IF NULLIF(p_override, '') IS NOT NULL THEN <tag>")

RAISE = run(
    "procedure:legacy.apply_raise", RepairOutcome.NEEDS_HUMAN,
    [attempt(i, 1, 1, ValidationStatus.REQUIRES_REPAIR, "1 of 2 ...\n- ar-03: ...") for i in (1, 2, 3)],
    [outcome("ar-01", TestStatus.PASSED,
             ExecutionResult(engine="oracle", test_id="ar-01", table_diffs=[TableDiff(table="salary_audit")])),
     outcome("ar-03", TestStatus.FAILED)])


def test_summary_counts():
    s = ReportSummary.of([BONUS, RAISE])
    assert (s.objects, s.equivalent, s.needs_human) == (2, 1, 1)
    assert (s.tests_total, s.tests_same) == (4, 3)
    assert s.repairs == 3          # 1 for calc_bonus + 2 for apply_raise
    assert s.repaired_objects == 1  # apply_raise was repaired but never became equivalent
    assert s.stand_in_converter


def test_report_shows_verdicts_attempts_and_feedback():
    html = render_report([BONUS, RAISE], descriptions={"cb-02": "Trap: '' is NULL in Oracle"})
    for text in ["procedure:legacy.calc_bonus", "Equivalent", "Needs a human", "Attempt 2", "Repair 1",
                 "Sent back to the converter", "cb-02: out_params.p_bonus: Oracle 24000, PostgreSQL 6000.00.",
                 "p_bonus = 24000.00", "error ORA-01403", "error P0002", "changed salary_audit",
                 "1 / 2", "3 / 4"]:
        assert text in html, text
    assert "Trap: &#x27;&#x27; is NULL in Oracle" in html


def test_stand_in_banner_only_when_the_stand_in_was_used():
    assert "scripted stand-in" in render_report([BONUS])
    real = BONUS.model_copy(update={"final_conversion": BONUS.final_conversion.model_copy(update={"warnings": []})})
    assert "scripted stand-in" not in render_report([real])


def test_code_and_text_are_escaped():
    html = render_report([BONUS])
    assert "<tag>" not in html
    assert "&lt;tag&gt;" in html


def test_report_reads_runs_back_from_json():
    data = json.loads(json.dumps([BONUS.model_dump(mode="json")]))
    again = RepairRun.model_validate(data[0])
    assert ReportSummary.of([again]) == ReportSummary.of([BONUS])
    assert "p_bonus = 24000.00" in render_report([again])  # Decimals come back as JSON strings, shown as numbers


def test_object_that_never_compiled_is_reported():
    broken = RepairRun(object_id="function:legacy.x", outcome=RepairOutcome.NEEDS_HUMAN, reason="Still not compiling",
                       attempts=[AttemptRecord(attempt=1, version=1, tool=ConversionTool.GEMINI, compiled_ok=False,
                                               compile_error='syntax error at or near "ELSIF"')])
    html = render_report([broken])
    assert "Did not compile" in html and "ELSIF" in html and "No tests ran" in html


def test_empty_runs_are_rejected():
    with pytest.raises(ValueError):
        render_report([])

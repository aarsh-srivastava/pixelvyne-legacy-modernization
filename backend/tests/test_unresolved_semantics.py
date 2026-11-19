"""PR 1: GREATEST's unresolved NULL semantics are surfaced, not just documented (Aarsh, 19 Nov).

Routing is unchanged: GREATEST still takes the rules route. Every use adds a visible
"Needs review: semantic risk ..." warning to the ConversionResult, with the object, line,
the Oracle/PostgreSQL difference and the action, and sets needs_review, so the object ends
as "Validation passed — semantic sign-off required" and the report lists the warning. The
only deploy today is to the validation target; a future production deploy must enforce a
recorded sign-off itself. The fix (R14, NULL-safe) and the behavioural tests are PR 1b.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import settings
from app.contracts import (
    ConversionTool,
    ExecutionResult,
    TestOutcome,
    TestStatus,
    ValidationResult,
    ValidationStatus,
)
from app.migration import converter as converter_module
from app.migration.allow_list import UNRESOLVED
from app.migration.converter import OracleToPostgresConverter
from app.migration.features import Handling, detect_features, semantic_risks
from app.migration.llm_fallback import GeminiFallback
from app.migration.postgres_deployer import DeployResult
from app.orchestration.repair_loop import RepairDeps, RepairOutcome, run_repair_loop
from app.parsers.oracle_parser import parse_sources
from app.reporting.migration_report import render_report
from app.validation.golden_tests import GOLDEN_TESTS

ROOT = Path(__file__).resolve().parents[2]
NULL_DIFFERENCE = UNRESOLVED["GREATEST"]

TWO_USES = """CREATE OR REPLACE FUNCTION later_of (a IN DATE, b IN DATE, c IN DATE) RETURN DATE IS
  v DATE;
BEGIN
  -- GREATEST(a, b) in a comment does not count
  v := GREATEST(a, b);
  RETURN greatest (v, c);
END;
/
"""
NO_USE = """CREATE OR REPLACE FUNCTION plain_max (a IN NUMBER, b IN NUMBER) RETURN NUMBER IS
BEGIN
  RETURN CASE WHEN a > b THEN a ELSE b END;
END;
/
"""


class FakeDeployer:
    def __init__(self):
        self.calls = []

    def deploy(self, code, target):
        self.calls.append((code, target))
        return DeployResult(ok=True)


def _objects(*sources):
    return {o.object_id: o for o in parse_sources({f"{i}.sql": s for i, s in enumerate(sources)})}


def _sample_objects():
    return {o.object_id: o for o in parse_sources(
        {p.name: p.read_text() for p in sorted((ROOT / "samples" / "oracle").glob("*.sql"))})}


# --- finding every use ------------------------------------------------------------------


def test_every_use_is_found_with_its_line():
    risks = semantic_risks(TWO_USES.split("/")[0])
    assert [(r.function, r.line) for r in risks] == [("GREATEST", 5), ("GREATEST", 6)]
    assert risks[0].source_line == "v := GREATEST(a, b);"
    assert risks[1].source_line == "RETURN greatest (v, c);"


@pytest.mark.parametrize("source", [
    "BEGIN x := 'GREATEST(a, b)'; END;",          # in a string
    "BEGIN /* GREATEST(a, b) */ NULL; END;",       # in a comment
    "BEGIN x := util.greatest(a, b); END;",        # a package member, not the built-in
    "BEGIN x := my_greatest(a, b); END;",          # another name
    "BEGIN x := GREATEST_V; END;",                 # not a call
])
def test_only_calls_to_the_built_in_count(source):
    assert semantic_risks(source) == []


def test_unreadable_source_gives_no_risk_and_is_routed_to_review_anyway():
    source = "BEGIN x := GREATEST(1, 2); y := 'never closed; END;"
    assert semantic_risks(source) == []
    assert detect_features(source).handling is Handling.REVIEW


def test_routing_is_unchanged():
    body = TWO_USES.split("/")[0]
    report = detect_features(body)
    assert report.handling is Handling.RULE
    assert not any("GREATEST" in f.feature or "GREATEST" in f.detail for f in report.findings)


# --- the conversion result --------------------------------------------------------------


def test_each_use_adds_a_visible_warning_and_needs_review():
    objects = _objects(TWO_USES)
    deployer = FakeDeployer()
    result = OracleToPostgresConverter(deployer, objects.values()).convert(objects["function:legacy.later_of"])
    assert result.tool is ConversionTool.SQLGLOT and result.compiled_ok   # same route as before
    assert len(deployer.calls) == 1
    assert result.needs_review
    risks = [w for w in result.warnings if w.startswith("Needs review: semantic risk")]
    assert len(risks) == 2
    for warning, line in zip(risks, (5, 6), strict=True):
        assert f"function:legacy.later_of at line {line}: GREATEST." in warning
        assert NULL_DIFFERENCE in warning
        assert "Semantic equivalence is not verified" in warning
        assert "human review, or explicit acceptance of this known limitation, before deployment" in warning


def test_an_object_without_greatest_is_not_flagged():
    objects = _objects(NO_USE)
    result = OracleToPostgresConverter(FakeDeployer(), objects.values()).convert(objects["function:legacy.plain_max"])
    assert result.compiled_ok and not result.needs_review
    assert not any("semantic risk" in w for w in result.warnings)


def test_the_sample_use_is_reported_at_its_line():
    objects = _sample_objects()
    obj = objects["procedure:legacy.monthly_payroll"]
    result = OracleToPostgresConverter(FakeDeployer(), objects.values()).convert(obj)
    risks = [w for w in result.warnings if "semantic risk" in w]
    line = next(i for i, text in enumerate(obj.source_code.splitlines(), 1) if "GREATEST" in text)
    assert risks == [
        f"Needs review: semantic risk in procedure:legacy.monthly_payroll at line {line}: GREATEST. "
        f"{NULL_DIFFERENCE} Semantic equivalence is not verified. Action: human review, or explicit "
        "acceptance of this known limitation, before deployment. "
        "Line: v_days := v_month_end - GREATEST(TRUNC(e.hire_date), v_month_start) + 1;"
    ]
    # It was already flagged (dropped parameter default); that warning is still there.
    assert any(w.startswith("Needs review: parameter default dropped") for w in result.warnings)
    assert result.needs_review and result.tool is ConversionTool.SQLGLOT


#: Every sample object's outcome, as in v4 and before PR 1 (test_converter.py checks the
#: same deployed set and review flag against PostgreSQL): (tool, compiled_ok, needs_review,
#: failure_code). Only monthly_payroll needs review, as it already did.
_DEPLOYED = ("sqlglot", True, False, None)
_NOT_CONVERTED = ("sqlglot", False, False, "not_converted")
SAMPLE_OUTCOMES = {
    "function:legacy.get_emp_grade": _DEPLOYED,
    "function:legacy.payroll_pkg.net_pay": _DEPLOYED,
    "function:legacy.top_earners": _DEPLOYED,
    "package:legacy.payroll_pkg": _NOT_CONVERTED,
    "procedure:legacy.apply_raise": _DEPLOYED,
    "procedure:legacy.calc_bonus": _DEPLOYED,
    "procedure:legacy.monthly_payroll": ("sqlglot", True, True, None),
    "procedure:legacy.payroll_pkg.close_month": _DEPLOYED,
    "sequence:legacy.payroll_run_seq": _NOT_CONVERTED,
    "sequence:legacy.salary_audit_seq": _NOT_CONVERTED,
    "table:legacy.departments": _NOT_CONVERTED,
    "table:legacy.employees": _NOT_CONVERTED,
    "table:legacy.payroll_runs": _NOT_CONVERTED,
    "table:legacy.salary_audit": _NOT_CONVERTED,
    "trigger:legacy.emp_salary_audit_trg": _DEPLOYED,
    "view:legacy.org_chart": ("sqlglot", False, False, "needs_gemini"),
}


def test_sample_outcomes_are_unchanged():
    objects = _sample_objects()
    converter = OracleToPostgresConverter(FakeDeployer(), objects.values())
    outcomes = {}
    for object_id, obj in objects.items():
        r = converter.convert(obj)
        outcomes[object_id] = (r.tool.value, r.compiled_ok, r.needs_review, r.failure_code)
    assert outcomes == SAMPLE_OUTCOMES


def test_sample_results_are_unchanged_apart_from_the_warning(monkeypatch):
    """Every sample object converts exactly as before (v4: no semantic-risk check), except
    that monthly_payroll gains one warning. needs_review, tool, code, compiled_ok and
    failure codes are all compared, for every object."""
    objects = _sample_objects()

    def results():
        converter = OracleToPostgresConverter(FakeDeployer(), objects.values())
        return {k: converter.convert(o).model_dump(exclude={"produced_at"}) for k, o in objects.items()}

    now = results()
    monkeypatch.setattr(converter_module, "semantic_risks", lambda source: [])
    before = results()

    changed = set()
    for object_id, result in now.items():
        added = [w for w in result["warnings"] if w not in before[object_id]["warnings"]]
        if added:
            changed.add(object_id)
            assert all(w.startswith("Needs review: semantic risk") for w in added)
        assert {**result, "warnings": before[object_id]["warnings"]} == before[object_id], object_id
        assert [w for w in result["warnings"] if w not in added] == before[object_id]["warnings"]
    assert changed == {"procedure:legacy.monthly_payroll"}
    assert before["procedure:legacy.monthly_payroll"]["needs_review"]  # already flagged before PR 1


# --- before deployment: the sign-off outcome and the report -----------------------------


def test_passing_tests_still_end_as_needs_sign_off_and_the_report_shows_why():
    objects = _objects(TWO_USES)
    obj = objects["function:legacy.later_of"]
    case = next(iter(GOLDEN_TESTS.values()))[0].model_copy(update={"object_id": obj.object_id})

    def validate(o, cases, version):
        outcomes = [TestOutcome(test_id=c.test_id, status=TestStatus.PASSED,
                                source=ExecutionResult(engine="oracle", test_id=c.test_id),
                                target=ExecutionResult(engine="postgres", test_id=c.test_id)) for c in cases]
        return ValidationResult(object_id=o.object_id, conversion_version=version, tests=outcomes,
                                passed=len(outcomes), failed=0, status=ValidationStatus.PASSED)

    run = run_repair_loop(RepairDeps(converter=OracleToPostgresConverter(FakeDeployer(), objects.values()),
                                     validate=validate), obj, [case])
    assert run.outcome is RepairOutcome.PASSED_NEEDS_REVIEW
    html = render_report([run])
    assert "Validation passed — semantic sign-off required" in html
    assert "Objects passing validation" in html and "1 need semantic sign-off" in html
    assert "Equivalent" not in html  # never called equivalent while sign-off is outstanding
    assert "Needs sign-off before deployment" in html
    assert html.count("semantic risk in function:legacy.later_of at line") == 2
    assert "Oracle returns NULL when any argument is NULL" in html


# --- the Gemini route: already needs review; the warning is added, the prompt unchanged ---

GEMINI_SOURCE = """CREATE OR REPLACE FUNCTION hi_later (a IN DATE, b IN DATE) RETURN DATE AS
BEGIN
  DBMS_OUTPUT.PUT_LINE('hi');
  RETURN GREATEST(a, b);
END;
/
"""
GEMINI_SQL = (
    "CREATE OR REPLACE FUNCTION hi_later(a timestamp(0), b timestamp(0)) RETURNS timestamp(0) "
    "LANGUAGE plpgsql AS $$\nBEGIN\n  RAISE NOTICE 'hi';\n  RETURN GREATEST(a, b);\nEND;\n$$;\n"
)


def test_the_gemini_route_carries_the_warning_without_changing_the_prompt(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_MODEL", "gemini-test")
    calls = []

    def generate(prompt, *, system=None):
        calls.append(prompt)
        return json.dumps({"postgresql": GEMINI_SQL, "explanation": "", "warnings": [], "unsupported": [],
                           "requires_human_review": False})

    objects = _objects(GEMINI_SOURCE)
    result = OracleToPostgresConverter(FakeDeployer(), objects.values(), llm=GeminiFallback(generate)).convert(
        objects["function:legacy.hi_later"])
    assert result.tool is ConversionTool.GEMINI and result.compiled_ok and result.needs_review, result
    assert len(calls) == 1 and "semantic risk" not in calls[0]
    assert any(w.startswith("Needs review: semantic risk in function:legacy.hi_later at line 4: GREATEST.")
               for w in result.warnings)


# --- order: the warning exists before anything is deployed, on both routes (Aarsh, 19 Nov) ---


class _OrderDeployer:
    def __init__(self, log):
        self.log = log

    def deploy(self, code, target):
        self.log.append("deploy")
        return DeployResult(ok=True)


def _logging_risks(monkeypatch, log):
    real = converter_module.semantic_risks

    def logged(source):
        log.append("risks")
        return real(source)

    monkeypatch.setattr(converter_module, "semantic_risks", logged)


def test_rules_route_computes_the_warning_before_deploying(monkeypatch):
    log = []
    _logging_risks(monkeypatch, log)
    objects = _objects(TWO_USES)
    result = OracleToPostgresConverter(_OrderDeployer(log), objects.values()).convert(
        objects["function:legacy.later_of"])
    assert log == ["risks", "deploy"]
    assert result.needs_review and sum("semantic risk" in w for w in result.warnings) == 2


def test_gemini_route_computes_the_warning_before_deploying(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_MODEL", "gemini-test")
    log = []
    _logging_risks(monkeypatch, log)

    def generate(prompt, *, system=None):
        return json.dumps({"postgresql": GEMINI_SQL, "explanation": "", "warnings": [], "unsupported": [],
                           "requires_human_review": False})

    objects = _objects(GEMINI_SOURCE)
    result = OracleToPostgresConverter(_OrderDeployer(log), objects.values(), llm=GeminiFallback(generate)).convert(
        objects["function:legacy.hi_later"])
    assert result.tool is ConversionTool.GEMINI and result.compiled_ok
    assert log == ["risks", "deploy"]
    assert sum("semantic risk" in w for w in result.warnings) == 1

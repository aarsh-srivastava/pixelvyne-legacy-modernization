"""Comparator rules and golden-test consistency. No database needed."""

from decimal import Decimal
from pathlib import Path

from app.contracts.models import ExecutionError, ExecutionResult, TestCase, TestStatus
from app.parsers.oracle_parser import parse_sources
from app.validation.comparator import compare, values_equal
from app.validation.golden_tests import GOLDEN_TESTS, ORACLE_EXPECTED

CASE = TestCase(test_id="t-01", object_id="procedure:x", description="test")
ORACLE_SAMPLES = Path(__file__).resolve().parents[2] / "samples" / "oracle"


def result(engine, **kw):
    return ExecutionResult(engine=engine, test_id="t-01", **kw)


def test_numbers_compare_by_value_not_scale():
    assert values_equal(Decimal("24000"), Decimal("24000.00"))
    assert values_equal(0, Decimal("0.00"))
    assert not values_equal(Decimal("4250.03"), Decimal("4250.02"))


def test_same_out_params_pass():
    outcome = compare(CASE, result("oracle", out_params={"p_bonus": Decimal("24000")}),
                      result("postgres", out_params={"p_bonus": Decimal("24000.00")}))
    assert outcome.status == TestStatus.PASSED
    assert outcome.diffs == []


def test_different_out_param_fails_with_path():
    outcome = compare(CASE, result("oracle", out_params={"p_bonus": Decimal("24000")}),
                      result("postgres", out_params={"p_bonus": Decimal("6000.00")}))
    assert outcome.status == TestStatus.FAILED
    [diff] = outcome.diffs
    assert diff.path == "out_params.p_bonus"
    assert diff.kind == "value"


def test_error_only_on_oracle_fails():
    outcome = compare(CASE, result("oracle", error=ExecutionError(code="ORA-01403", message="no data found")),
                      result("postgres", out_params={"p_bonus": Decimal("0")}))
    assert outcome.status == TestStatus.FAILED
    assert outcome.diffs[0].kind == "error"
    assert outcome.diffs[0].source_value == "ORA-01403"


def test_both_engines_raising_counts_as_same_behaviour():
    outcome = compare(CASE, result("oracle", error=ExecutionError(code="ORA-01403", message="x")),
                      result("postgres", error=ExecutionError(code="P0002", message="y")))
    assert outcome.status == TestStatus.PASSED


def test_missing_out_param_is_reported():
    outcome = compare(CASE, result("oracle", out_params={"a": 1, "b": 2}),
                      result("postgres", out_params={"a": 1}))
    assert [(d.path, d.kind) for d in outcome.diffs] == [("out_params.b", "missing")]


def test_rows_compared_cell_by_cell():
    outcome = compare(CASE, result("oracle", rows=[{"name": "Asha"}, {"name": "Rohan"}]),
                      result("postgres", rows=[{"name": "Asha"}, {"name": "Ravi"}]))
    assert [d.path for d in outcome.diffs] == ["rows[1].name"]


def test_golden_inputs_match_parsed_signatures():
    sources = {p.name: p.read_text(encoding="utf-8") for p in ORACLE_SAMPLES.glob("*.sql")}
    objects = {o.object_id: o for o in parse_sources(sources)}
    for object_id, cases in GOLDEN_TESTS.items():
        assert object_id in objects, f"{object_id} not found by the parser"
        params = {p.name.lower() for p in objects[object_id].params}
        for case in cases:
            assert set(case.inputs) <= params, f"{case.test_id} uses unknown params"
            assert case.test_id in ORACLE_EXPECTED, f"{case.test_id} has no answer-key entry"

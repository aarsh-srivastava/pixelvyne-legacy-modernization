"""Comparator rules and golden-test consistency. No database needed."""

from decimal import Decimal
from pathlib import Path

from app.contracts.models import ExecutionError, ExecutionResult, TableDiff, TestCase, TestStatus
from app.parsers.oracle_parser import parse_sources
from app.validation.comparator import compare, values_equal
from app.validation.errors import categorize
from app.validation.table_diff import diff_snapshots
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
    assert outcome.diffs[0].source_value == "no_data_found"


def test_both_engines_raising_counts_as_same_behaviour():
    outcome = compare(CASE, result("oracle", error=ExecutionError(code="ORA-01403", message="x")),
                      result("postgres", error=ExecutionError(code="P0002", message="y")))
    assert outcome.status == TestStatus.PASSED


def test_missing_out_param_is_reported():
    outcome = compare(CASE, result("oracle", out_params={"a": 1, "b": 2}),
                      result("postgres", out_params={"a": 1}))
    assert [(d.path, d.kind) for d in outcome.diffs] == [("out_params.b", "missing")]


def test_rows_compared_cell_by_cell_when_order_matters():
    outcome = compare(CASE, result("oracle", rows=[{"name": "Asha"}, {"name": "Rohan"}]),
                      result("postgres", rows=[{"name": "Asha"}, {"name": "Ravi"}]),
                      ordered_rows=True)
    assert [d.path for d in outcome.diffs] == ["rows[1].name"]


def test_rows_ignore_order_by_default():
    outcome = compare(CASE, result("oracle", rows=[{"id": 1}, {"id": 2}]),
                      result("postgres", rows=[{"id": Decimal("2.0")}, {"id": 1}]))
    assert outcome.status == TestStatus.PASSED


def test_unordered_rows_report_missing_and_extra():
    outcome = compare(CASE, result("oracle", rows=[{"id": 1}, {"id": 2}]),
                      result("postgres", rows=[{"id": 1}, {"id": 3}]))
    assert sorted(d.kind for d in outcome.diffs) == ["extra", "missing"]


def test_different_error_categories_fail():
    outcome = compare(CASE, result("oracle", error=ExecutionError(code="ORA-01403", message="x")),
                      result("postgres", error=ExecutionError(code="23505", message="y")))
    assert outcome.status == TestStatus.FAILED
    [diff] = outcome.diffs
    assert (diff.source_value, diff.target_value) == ("no_data_found", "unique_violation")


def test_missing_target_object_is_a_harness_error_not_a_failure():
    outcome = compare(CASE, result("oracle", out_params={"p_bonus": 1}),
                      result("postgres", error=ExecutionError(code="42883", message="does not exist")))
    assert outcome.status == TestStatus.ERROR


def test_crashed_runner_is_a_harness_error():
    outcome = compare(CASE, result("oracle", error=ExecutionError(code="HARNESS", message="boom")),
                      result("postgres", out_params={}))
    assert outcome.status == TestStatus.ERROR


def test_user_raised_errors_match_across_engines():
    assert categorize("oracle", ExecutionError(code="ORA-20001", message="x")) == "user_raised"
    assert categorize("postgres", ExecutionError(code="P0001", message="y")) == "user_raised"


def test_table_side_effects_must_match():
    audit = TableDiff(table="salary_audit", inserted=[{"audit_id": 7, "emp_id": 104, "change_type": "RAISE"}])
    outcome = compare(CASE, result("oracle", table_diffs=[audit]), result("postgres"),
                      masked_columns={"salary_audit": {"audit_id"}})
    [diff] = outcome.diffs
    assert diff.path == "table_diffs.salary_audit.inserted"
    assert diff.kind == "missing"
    assert "audit_id" not in diff.source_value


def test_masked_columns_are_ignored():
    src = TableDiff(table="salary_audit", inserted=[{"audit_id": 7, "emp_id": 104}])
    tgt = TableDiff(table="salary_audit", inserted=[{"audit_id": 1, "emp_id": 104}])
    outcome = compare(CASE, result("oracle", table_diffs=[src]), result("postgres", table_diffs=[tgt]),
                      masked_columns={"salary_audit": {"audit_id"}})
    assert outcome.status == TestStatus.PASSED


def test_updated_row_values_compared_by_key():
    def upd(salary):
        return TableDiff(table="employees", updated=[{
            "key": {"emp_id": 104}, "before": {"emp_id": 104, "base_salary": Decimal("85000.50")},
            "after": {"emp_id": 104, "base_salary": salary}}])
    outcome = compare(CASE, result("oracle", table_diffs=[upd(Decimal("90950.54"))]),
                      result("postgres", table_diffs=[upd(Decimal("90950.5400"))]))
    assert outcome.status == TestStatus.PASSED
    outcome = compare(CASE, result("oracle", table_diffs=[upd(Decimal("90950.54"))]),
                      result("postgres", table_diffs=[upd(Decimal("85000.51"))]))
    assert [d.path for d in outcome.diffs] == ["table_diffs.employees.updated[emp_id=104].base_salary"]


def test_snapshot_diff_finds_insert_update_delete():
    before = [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}]
    after = [{"id": 1, "v": "a"}, {"id": 2, "v": "B"}, {"id": 3, "v": "c"}]
    diff = diff_snapshots("t", ["id"], before, after)
    assert diff.inserted == [{"id": 3, "v": "c"}]
    assert diff.deleted == []
    assert diff.updated == [{"key": {"id": 2}, "before": {"id": 2, "v": "b"}, "after": {"id": 2, "v": "B"}}]
    assert diff_snapshots("t", ["id"], before, before) is None


def test_snapshot_diff_without_primary_key_uses_whole_rows():
    diff = diff_snapshots("t", [], [{"v": 1}], [{"v": 1}, {"v": 1}])
    assert diff.inserted == [{"v": 1}]


def test_golden_inputs_match_parsed_signatures():
    sources = {p.name: p.read_text(encoding="utf-8") for p in ORACLE_SAMPLES.glob("*.sql")}
    objects = {o.object_id: o for o in parse_sources(sources)}
    for object_id, cases in GOLDEN_TESTS.items():
        assert object_id in objects, f"{object_id} not found by the parser"
        params = {p.name.lower() for p in objects[object_id].params}
        for case in cases:
            assert set(case.inputs) <= params, f"{case.test_id} uses unknown params"
            assert case.test_id in ORACLE_EXPECTED, f"{case.test_id} has no answer-key entry"

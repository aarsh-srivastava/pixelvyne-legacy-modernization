"""Step 7c: generated edge-case tests (deterministic generation, fixture rows, triage, relations,
the recorded corpus, safety, and careless conversions the golden tests miss).

Sections 1-7 need no database. Section 8 deploys the real converter's output into a throwaway
PostgreSQL database (the 7b `target` fixture) and skips without PostgreSQL. Section 9 replays
the Oracle-recorded corpus against it; it runs only when edge_corpus.json exists.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from app.contracts import CONTRACT_VERSION
from app.contracts.models import (
    ExecutionError,
    ExecutionResult,
    FieldDiff,
    GenerationInfo,
    TableDiff,
    TestCase,
    TestOutcome,
    TestStatus,
)
from app.validation import actions, edge_corpus, generators, relations, triage
from app.validation.edge_catalogue import (
    ACCEPTED_DIFFERENCES,
    CATEGORIES,
    KNOWN_NON_PASSES,
    RULES_BY_OBJECT,
    TRIGGER_ACTIONS,
    core_specs,
)
from app.validation.fixtures import ALLOWED_COLUMNS, UnsafeFixture, checked_fixtures, oracle_insert
from app.validation.golden_tests import (
    CALC_BONUS,
    GOLDEN_TESTS,
    MONTHLY_PAYROLL,
    NET_PAY,
    ORG_CHART,
    SALARY_AUDIT_TRG,
    TOP_EARNERS,
)

ROOT = Path(__file__).resolve().parents[2]
CASES = generators.generate()
BY_ID = {c.test_id: c for c in CASES}


def gen(object_id: str, **inputs) -> TestCase:
    """The generated case for this object with exactly these inputs (and no fixture rows or setup)."""
    found = [c for c in CASES if c.object_id == object_id and c.inputs == inputs and not c.fixture_rows
             and not c.setup]
    assert len(found) == 1, (object_id, inputs, len(found))
    return found[0]


# --- 1. deterministic generation --------------------------------------------------------------

def test_the_default_run_is_150_cases_and_the_contract_is_0_6():
    assert CONTRACT_VERSION == "0.6"
    assert len(CASES) == generators.DEFAULT_BUDGET == 150
    assert generators.MAX_BUDGET == 400


def test_generating_twice_gives_identical_cases():
    again = generators.generate()
    assert [c.model_dump() for c in again] == [c.model_dump() for c in CASES]
    assert generators.case_set_hash(again) == generators.case_set_hash(CASES)


def test_another_process_with_another_hash_seed_gives_the_same_set():
    code = ("import sys; sys.path.insert(0, %r); from app.validation import generators as g; "
            "print(g.case_set_hash(g.generate()))") % str(ROOT / "backend")
    env = dict(os.environ, PYTHONHASHSEED="12345")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True)
    assert out.stdout.strip() == generators.case_set_hash(CASES)


def test_a_different_seed_changes_only_the_sampled_cases():
    other = generators.generate(seed=generators.DEFAULT_SEED + 1)
    core = len(generators._unique(core_specs(), generators._golden_forms()))
    assert [c.test_id for c in other[:core]] == [c.test_id for c in CASES[:core]]
    assert [c.test_id for c in other[core:]] != [c.test_id for c in CASES[core:]]
    assert len(other) == len(CASES)


def test_the_budget_has_a_maximum_and_never_drops_coverage():
    with pytest.raises(ValueError, match="maximum supported budget of 400"):
        generators.generate(budget=401)
    with pytest.raises(ValueError, match="core cases"):
        generators.generate(budget=50)
    assert len(generators.generate(budget=400)) <= 400


def test_ids_are_stable_unique_and_say_what_the_case_is():
    assert len(BY_ID) == len(CASES)
    for case in CASES:
        prefix = f"gen-{generators.SHORT[case.object_id]}-{case.generation.category}-"
        assert case.test_id.startswith(prefix) and len(case.test_id) == len(prefix) + 8
    # an ID depends on the case only: the same case keeps it in a bigger budget
    bigger = {c.test_id for c in generators.generate(budget=300)}
    assert set(BY_ID) <= bigger


def test_no_generated_case_repeats_a_golden_test():
    golden = {generators._digest(generators.canonical_form(c)) for cs in GOLDEN_TESTS.values() for c in cs}
    pool = {generators._digest(generators.canonical_form(s)) for s in generators.pool_specs()}
    assert golden & pool  # the pool does contain golden inputs (e.g. calc_bonus(102, NULL)), so the skip matters
    for cases in (CASES, generators.generate(budget=generators.MAX_BUDGET)):
        assert not golden & {generators._digest(generators.canonical_form(c)) for c in cases}


def test_every_object_category_and_rule_is_covered_at_the_default_budget():
    assert {c.object_id for c in CASES} == set(GOLDEN_TESTS)
    assert {c.generation.category for c in CASES} == set(CATEGORIES)
    for object_id, rules in RULES_BY_OBJECT.items():
        covered = {c.generation.rule for c in CASES if c.object_id == object_id}
        assert rules <= covered, (object_id, rules - covered)


def test_generated_cases_are_values_only_and_marked_as_rule_tests():
    allowed = (type(None), str, int, Decimal, dt.datetime)
    for case in CASES:
        assert case.origin == "rule" and isinstance(case.generation, GenerationInfo)
        assert case.generation.generator_version == generators.GENERATOR_VERSION
        values = list(case.inputs.values()) + [v for rows in case.fixture_rows.values() for r in rows
                                               for v in r.values()]
        assert all(isinstance(v, allowed) and not isinstance(v, (bool, float)) for v in values), case.test_id
        if case.action:
            assert actions.checked_action(case) in actions.trusted_actions()
        checked_fixtures(case)  # every generated fixture passes the safety checks


def test_out_of_contract_cases_are_marked():
    out = {c.test_id for c in CASES if not c.generation.in_contract}
    assert gen(CALC_BONUS, p_emp_id=102, p_override="AB").test_id in out
    assert gen(CALC_BONUS, p_emp_id=103, p_override="").test_id not in out
    assert any(c.action == TRIGGER_ACTIONS["set_null"][0] for c in CASES if c.test_id in out)


# --- 2. fixture rows: values only, allow-listed names, trusted origin -------------------------

def _fixture_case(**changes) -> TestCase:
    base = TestCase(test_id="fx", object_id=CALC_BONUS, description="d", origin="rule",
                    fixture_rows={"employees": [{"emp_id": 900, "full_name": "X", "dept_id": 20,
                                                 "hire_date": dt.datetime(2024, 1, 2), "base_salary": Decimal("1")}]})
    return base.model_copy(update=changes)


def test_fixture_rows_become_bound_inserts_on_allow_listed_names():
    inserts = checked_fixtures(_fixture_case())
    assert inserts == [("employees", ["emp_id", "full_name", "dept_id", "hire_date", "base_salary"],
                        [900, "X", 20, dt.datetime(2024, 1, 2), Decimal("1")])]
    assert oracle_insert("employees", ["emp_id", "full_name"]) == \
        "INSERT INTO EMPLOYEES (EMP_ID, FULL_NAME) VALUES (:1, :2)"
    assert set(ALLOWED_COLUMNS) == {"departments", "employees", "payroll_runs", "salary_audit"}


@pytest.mark.parametrize("change, message", [
    ({"origin": "llm"}, "only allowed for golden or rule tests"),
    ({"fixture_rows": {"pg_authid": [{"rolname": "x"}]}}, "table 'pg_authid' is not allowed"),
    ({"fixture_rows": {"employees": [{"emp_id": 1, "salary; DROP TABLE x": 1}]}}, "are not allowed"),
    ({"fixture_rows": {"employees": [{"emp_id": 1.5}]}}, "has a float"),
    ({"fixture_rows": {"employees": [{"emp_id": [1]}]}}, "has a list"),
    ({"fixture_rows": {"employees": [{}]}}, "an empty fixture row"),
])
def test_unsafe_fixture_rows_are_refused_before_any_sql(change, message):
    with pytest.raises(UnsafeFixture, match=message.replace("(", r"\(")):
        checked_fixtures(_fixture_case(**change))


class _RecordingOracle:
    """Fake Oracle connection that records statements and binds, in order."""

    def __init__(self):
        self.log = []

    def cursor(self):
        log = self.log

        class Cur:
            description = [("X",)]

            def execute(self, statement, *args, **kwargs):
                log.append(("execute", " ".join(statement.split()), args[0] if args else kwargs))

            def fetchall(self):
                return []

            def callproc(self, name, args):
                log.append(("callproc", name))

            def var(self, _type):
                class V:
                    def getvalue(self):
                        return None

                    def setvalue(self, *_):
                        pass
                return V()
        return Cur()

    def rollback(self):
        self.log.append(("rollback",))


def test_the_oracle_runner_inserts_fixtures_first_with_binds(sample):
    from app.validation.oracle_runner import run_oracle

    case = [c for c in CASES if c.object_id == CALC_BONUS and c.inputs.get("p_emp_id") == 900][0]
    db = _RecordingOracle()
    run_oracle(db, sample[CALC_BONUS], case, ["employees"], sample)
    kinds = [entry[0] for entry in db.log]
    first_insert = kinds.index("execute")
    assert db.log[first_insert][1].startswith("INSERT INTO EMPLOYEES (EMP_ID, FULL_NAME")
    assert db.log[first_insert][2][0] == 900 and "10000.05" not in db.log[first_insert][1]  # bound, not text
    assert kinds.index("callproc") > first_insert and kinds[-1] == "rollback"


def test_an_unsafe_fixture_never_reaches_oracle(sample):
    from app.validation.oracle_runner import run_oracle

    db = _RecordingOracle()
    with pytest.raises(UnsafeFixture):
        run_oracle(db, sample[CALC_BONUS], _fixture_case(origin="llm"), ["employees"], sample)
    assert [e for e in db.log if e[0] == "execute"] == []


# --- 3. registered trigger actions (D-7c.4) ---------------------------------------------------

def test_registered_actions_need_a_trusted_origin_and_the_exact_statement():
    statement = TRIGGER_ACTIONS["multi_row_raise"][0]
    case = TestCase(test_id="t", object_id=SALARY_AUDIT_TRG, description="d", origin="rule", action=statement)
    assert actions.checked_action(case) == statement
    with pytest.raises(actions.UnsafeAction, match="not 'llm'"):
        actions.checked_action(case.model_copy(update={"origin": "llm"}))
    with pytest.raises(actions.UnsafeAction, match="not one defined"):
        actions.checked_action(case.model_copy(update={"action": statement.replace("1.1", "2")}))


# --- 4. triage: one class per non-pass, in table order -----------------------------------------

def _result(engine, error=None, context=None, **kw):
    err = ExecutionError(code=error, message=f"{error} message", context=context) if error else None
    return ExecutionResult(engine=engine, test_id="x", error=err, **kw)


def _outcome(src, tgt, status=TestStatus.FAILED, diffs=()):
    return TestOutcome(test_id="x", status=status, source=src, target=tgt, diffs=list(diffs))


CASE_IN = gen(TOP_EARNERS, p_dept_id=20, p_n=Decimal("2.5"))
ROUTING = {ORG_CHART: "needs Gemini (no deterministic rule for: CONNECT BY)"}


def classify(outcome, case=CASE_IN, **kw):
    kw.setdefault("object_name", "TOP_EARNERS")
    return triage.classify(case, outcome, **kw)


def test_a_pass_has_no_class():
    assert classify(_outcome(_result("oracle"), _result("postgres"), TestStatus.PASSED)) is None


def test_reference_stale_comes_first():
    t = classify(_outcome(_result("oracle"), _result("postgres"), TestStatus.PASSED), stale_reference="stale!")
    assert t.failure_class is triage.FailureClass.REFERENCE_STALE


def test_a_missing_object_is_unsupported_only_with_its_routing_reason():
    oc = [c for c in CASES if c.object_id == ORG_CHART][0]
    missing = _outcome(_result("oracle", rows=[]), _result("postgres", "42P01"), TestStatus.ERROR)
    t = triage.classify(oc, missing, object_name="ORG_CHART", routing=ROUTING, deployed=False)
    assert t.failure_class is triage.FailureClass.UNSUPPORTED and "needs Gemini" in t.reason
    # no routing reason, or the object is deployed: an environment problem, never "unsupported"
    assert triage.classify(oc, missing, object_name="ORG_CHART", routing={}, deployed=False).failure_class \
        is triage.FailureClass.ENVIRONMENT
    assert triage.classify(oc, missing, object_name="ORG_CHART", routing=ROUTING, deployed=True).failure_class \
        is triage.FailureClass.ENVIRONMENT


def test_an_in_contract_case_that_ran_and_differs_is_never_unsupported():
    differs = _outcome(_result("oracle", return_value="A"), _result("postgres", return_value="B"),
                       diffs=[FieldDiff(path="return_value", source_value="A", target_value="B")])
    t = classify(differs, routing={TOP_EARNERS: "routed to review: anything"}, deployed=True)
    assert t.failure_class is triage.FailureClass.TRANSLATION_DEFECT and t.severity == "defect"


def test_unsupported_by_accepted_difference_needs_a_registry_entry(monkeypatch):
    differs = _outcome(_result("oracle", return_value="A"), _result("postgres", return_value="B"))
    assert ACCEPTED_DIFFERENCES == {}  # none accepted today
    monkeypatch.setitem(triage.ACCEPTED_DIFFERENCES, CASE_IN.test_id, ("why", "D-x"))
    t = classify(differs)
    assert t.failure_class is triage.FailureClass.UNSUPPORTED and "(D-x)" in t.reason


def test_harness_errors_are_environment():
    t = classify(_outcome(_result("oracle", "DPY-4011"), _result("postgres"), TestStatus.ERROR))
    assert t.failure_class is triage.FailureClass.ENVIRONMENT and t.reason.startswith("Oracle harness error")


def test_invalid_input_only_when_oracle_rejected_it_at_the_call():
    at_call = _outcome(_result("oracle", "ORA-01722", context="ORA-06512: at line 1"), _result("postgres", "22P02"))
    assert classify(at_call).failure_class is triage.FailureClass.INVALID_INPUT
    inside = _outcome(_result("oracle", "ORA-06502", context='ORA-06512: at "LEGACY.TOP_EARNERS", line 9'),
                      _result("postgres", "22P02"), diffs=[FieldDiff(path="error", kind="error")])
    assert classify(inside).failure_class is triage.FailureClass.TRANSLATION_DEFECT


def test_unmapped_errors_are_an_error_map_gap_not_a_defect():
    t = classify(_outcome(_result("oracle", "ORA-01407"), _result("postgres", "23502")))
    assert t.failure_class is triage.FailureClass.ERROR_MAP_GAP and "other:ORA-01407" in t.reason


def test_mapped_but_different_errors_are_a_defect_unless_under_review():
    cb = gen(CALC_BONUS, p_emp_id=102, p_override="AB")
    inside = 'ORA-06512: at "LEGACY.CALC_BONUS", line 15'
    h2 = _outcome(_result("oracle", "ORA-06502", context=inside), _result("postgres", "22001"))
    t = triage.classify(cb, h2, object_name="CALC_BONUS")
    assert t.failure_class is triage.FailureClass.ERROR_MAP_GAP and "candidate equivalence" in t.reason
    assert t.in_contract is False
    other = _outcome(_result("oracle", "ORA-01403"), _result("postgres", "22001"))
    assert triage.classify(cb, other, object_name="CALC_BONUS").failure_class is triage.FailureClass.TRANSLATION_DEFECT


def test_oracle_disagreeing_with_itself_is_non_deterministic():
    first = _result("oracle", return_value="Rohan Mehta, Karan Singh")
    second = _result("oracle", return_value="Karan Singh, Rohan Mehta")
    t = classify(_outcome(first, _result("postgres", return_value="x")), oracle_again=second)
    assert t.failure_class is triage.FailureClass.NON_DETERMINISTIC
    assert classify(_outcome(first, first, TestStatus.PASSED), oracle_again=first) is None


# revision 2 (Aarsh's review): Oracle's stability is checked before any other class, and on its own

def test_unstable_oracle_errors_are_not_hidden_as_an_error_map_gap():
    cb = gen(CALC_BONUS, p_emp_id=102, p_override="AB")
    first = _result("oracle", "ORA-06502", context='ORA-06512: at "LEGACY.CALC_BONUS", line 15')
    second = _result("oracle", "ORA-01407")
    outcome = _outcome(first, _result("postgres", "22001"))
    assert triage.classify(cb, outcome, object_name="CALC_BONUS").failure_class is triage.FailureClass.ERROR_MAP_GAP
    t = triage.classify(cb, outcome, object_name="CALC_BONUS", oracle_again=second)
    assert t.failure_class is triage.FailureClass.NON_DETERMINISTIC and "ORA-06502 vs ORA-01407" in t.reason
    assert triage.unstable_cases([cb], {cb.test_id: first}, {cb.test_id: second}) == [cb.test_id]


def test_unstable_oracle_results_are_not_hidden_as_unsupported():
    oc = [c for c in CASES if c.object_id == ORG_CHART][0]
    first = _result("oracle", rows=[{"emp_id": 101, "depth": Decimal("1")}])
    second = _result("oracle", rows=[{"emp_id": 101, "depth": Decimal("2")}])
    missing = _outcome(first, _result("postgres", "42P01"), TestStatus.ERROR)
    kw = {"object_name": "ORG_CHART", "routing": ROUTING, "deployed": False}
    assert triage.classify(oc, missing, **kw).failure_class is triage.FailureClass.UNSUPPORTED
    again = triage.classify(oc, missing, oracle_again=second, **kw)
    assert again.failure_class is triage.FailureClass.NON_DETERMINISTIC
    assert triage.unstable_cases([oc], {oc.test_id: first}, {oc.test_id: second}) == [oc.test_id]


def test_unstable_successful_oracle_results_are_listed_even_when_postgresql_matches_one():
    first = _result("oracle", return_value="Rohan Mehta, Karan Singh")
    second = _result("oracle", return_value="Rohan Mehta")
    passed = _outcome(first, _result("postgres", return_value="Rohan Mehta, Karan Singh"), TestStatus.PASSED)
    t = classify(passed, oracle_again=second)
    assert t.failure_class is triage.FailureClass.NON_DETERMINISTIC
    stable = gen(TOP_EARNERS, p_dept_id=20, p_n=0)
    first_all = {CASE_IN.test_id: first, stable.test_id: _result("oracle")}
    second_all = {CASE_IN.test_id: second, stable.test_id: _result("oracle")}
    assert triage.unstable_cases([CASE_IN, stable], first_all, second_all) == [CASE_IN.test_id]
    assert triage.unstable_cases([CASE_IN], first_all, {}) == [CASE_IN.test_id]  # no second recording: unstable


def test_masked_sequence_columns_do_not_make_oracle_non_deterministic():
    rows = [lambda n: {"run_id": n, "emp_id": 101, "pay_month": dt.datetime(2026, 9, 1), "gross_pay": Decimal("1"),
                       "days_worked": 30, "created_at": dt.datetime(2026, 9, 15)}]
    a = _result("oracle", table_diffs=[TableDiff(table="payroll_runs", inserted=[rows[0](1000)])])
    b = _result("oracle", table_diffs=[TableDiff(table="payroll_runs", inserted=[rows[0](1010)])])
    assert triage.same_oracle_result(CASE_IN, a, b)[0]


# revision 3 (Aarsh's review of revision 2): an Oracle error is stable only if it is raised in the same place

AT_CALL = "ORA-06512: at line 1"
INSIDE_CB = 'ORA-06512: at "LEGACY.CALC_BONUS", line 15\nORA-06512: at line 1'


def test_same_oracle_error_code_raised_in_a_different_place_is_non_deterministic():
    cb = gen(CALC_BONUS, p_emp_id=102, p_override="AB")
    first = _result("oracle", "ORA-06502", context=AT_CALL)        # at the call site: looks like invalid input
    second = _result("oracle", "ORA-06502", context=INSIDE_CB)     # inside the object's logic
    outcome = _outcome(first, _result("postgres", "22001"))
    assert triage.classify(cb, outcome, object_name="CALC_BONUS").failure_class is triage.FailureClass.INVALID_INPUT
    same, detail = triage.same_oracle_result(cb, first, second)
    assert not same and "ORA-06502 raised at 'ORA-06512: at line 1' vs at" in detail and "CALC_BONUS" in detail
    t = triage.classify(cb, outcome, object_name="CALC_BONUS", oracle_again=second)
    assert t.failure_class is triage.FailureClass.NON_DETERMINISTIC
    assert triage.unstable_cases([cb], {cb.test_id: first}, {cb.test_id: second}) == [cb.test_id]
    no_context = _result("oracle", "ORA-06502")                     # the other way round, and with no context
    assert triage.unstable_cases([cb], {cb.test_id: second}, {cb.test_id: no_context}) == [cb.test_id]


def test_same_oracle_error_in_the_same_place_is_stable():
    cb = gen(CALC_BONUS, p_emp_id=102, p_override="AB")
    first = _result("oracle", "ORA-06502", context=INSIDE_CB)
    again = _result("oracle", "ORA-06502", context=INSIDE_CB.replace("\n", "\r\n  "))   # line endings only
    assert triage.same_oracle_result(cb, first, again)[0]
    assert triage.unstable_cases([cb], {cb.test_id: first}, {cb.test_id: again}) == []
    assert triage.same_oracle_result(cb, _result("oracle", "ORA-30004"), _result("oracle", "ORA-30004", context=""))[0]
    deeper = _result("oracle", "ORA-06502", context=INSIDE_CB.replace("line 15", "line 9"))
    assert not triage.same_oracle_result(cb, first, deeper)[0]       # a different line inside is different too


def test_routing_reasons_name_only_the_gemini_object(sample):
    reasons = triage.routing_reasons(sample.values())
    assert set(reasons) == {ORG_CHART} and reasons[ORG_CHART].startswith("needs Gemini")


# --- 5. metamorphic relations: preconditions, enforced only where Oracle holds -----------------

def _ret(case, value, engine="oracle"):
    return ExecutionResult(engine=engine, test_id=case.test_id, return_value=value)


def _outp(case, name, value, engine="oracle"):
    return ExecutionResult(engine=engine, test_id=case.test_id, out_params={name: value})


def test_every_relation_group_has_a_definition_and_its_members():
    groups = relations.groups(CASES)
    assert set(groups) == {"cb_empty_101", "cb_empty_104", "cb_empty_111", "te_prefix_20", "te_prefix_30",
                           "np_sym_10.05", "np_sym_123.45", "ar_zero", "cm_same_month", "cm_double"}
    assert [len(groups[g]) for g in ("te_prefix_20", "cm_same_month", "cm_double")] == [4, 3, 2]
    for name in groups:
        assert relations.relation_for(name).preconditions


def test_prefix_relation_holds_violates_and_needs_its_preconditions():
    members = relations.groups(CASES)["te_prefix_20"]
    names = ["A", "A, B", "A, B, C", "A, B, C, D"]
    ok = {c.test_id: _ret(c, names[i]) for i, c in enumerate(sorted(members, key=lambda c: c.inputs["p_n"]))}
    assert relations.relation_for("te_prefix_20").check(members, ok)[0] is relations.RelationVerdict.HOLDS
    swapped = dict(ok)
    third = sorted(members, key=lambda c: c.inputs["p_n"])[2]
    swapped[third.test_id] = _ret(third, "B, A, C")
    assert relations.relation_for("te_prefix_20").check(members, swapped)[0] is relations.RelationVerdict.VIOLATED
    assert relations.relation_for("te_prefix_20").check(members[:1] + members[2:], ok)[0] \
        is relations.RelationVerdict.NOT_APPLICABLE  # n not consecutive


def test_a_relation_is_enforced_on_postgresql_only_where_oracle_holds():
    members = relations.groups(CASES)["np_sym_10.05"]
    pos, neg = sorted(members, key=lambda c: c.inputs["p_gross"], reverse=True)
    oracle_ok = {pos.test_id: _ret(pos, Decimal("9.05")), neg.test_id: _ret(neg, Decimal("-9.05"))}
    pg_bad = {pos.test_id: _ret(pos, Decimal("9.05"), "postgres"), neg.test_id: _ret(neg, Decimal("-9.04"), "postgres")}
    [result] = [r for r in relations.evaluate(members, oracle_ok, pg_bad) if r.group == "np_sym_10.05"]
    assert result.enforced and result.defect
    oracle_bad = {pos.test_id: _ret(pos, Decimal("9.05")), neg.test_id: _ret(neg, Decimal("-9.04"))}
    [result] = relations.evaluate(members, oracle_bad, pg_bad)
    assert not result.enforced and not result.defect  # reported, nothing concluded about PostgreSQL


def test_close_month_doubling_and_same_month_relations():
    groups = relations.groups(CASES)
    once, twice = groups["cm_double"]
    good = {once.test_id: _outp(once, "p_total", Decimal("91075.14")),
            twice.test_id: _outp(twice, "p_total", Decimal("182150.28"))}
    assert relations.relation_for("cm_double").check(groups["cm_double"], good)[0] is relations.RelationVerdict.HOLDS
    same = {c.test_id: _outp(c, "p_total", Decimal("91075.14")) for c in groups["cm_same_month"]}
    assert relations.relation_for("cm_same_month").check(groups["cm_same_month"], same)[0] \
        is relations.RelationVerdict.HOLDS


def test_a_zero_raise_relation_fails_on_an_audit_row():
    [member, *_] = relations.groups(CASES)["ar_zero"]
    audited = ExecutionResult(engine="postgres", test_id=member.test_id, out_params={"p_new_salary": Decimal("1")},
                              table_diffs=[TableDiff(table="salary_audit", inserted=[{"emp_id": 101}])])
    assert relations.relation_for("ar_zero").check([member], {member.test_id: audited})[0] \
        is relations.RelationVerdict.VIOLATED


# --- 6. the recorded corpus: typed round trip, fails closed when stale --------------------------

def test_the_corpus_keeps_types_and_refuses_a_stale_header(tmp_path):
    result = ExecutionResult(engine="oracle", test_id="x", return_value=Decimal("4250.03"),
                             out_params={"d": dt.datetime(2028, 2, 29, 23, 59, 59)},
                             table_diffs=[TableDiff(table="t", inserted=[{"v": Decimal("0.10")}])],
                             error=None)
    header = edge_corpus.expected_header(CASES, generators.DEFAULT_SEED, 150, ROOT / "samples" / "oracle")
    path = tmp_path / "c.json"
    edge_corpus.save(path, header, {"x": result})
    loaded = edge_corpus.load(path)
    assert loaded.results["x"] == result.model_copy(update={"duration_ms": 0.0})
    assert loaded.stale_reason(header) is None
    for key, value in (("seed", 1), ("case_set_hash", "0"), ("generator_version", "x"), ("budget", 151)):
        assert key in loaded.stale_reason(dict(header, **{key: value}))
    sources = dict(header["sources"], **{"02_seed_data.sql": "0" * 64})
    assert "'sources'" in loaded.stale_reason(dict(header, sources=sources))
    assert loaded.header["format"] == 2 and loaded.header["results_sha256"] == edge_corpus.results_digest({"x": result})


def test_an_edited_recorded_result_is_refused(tmp_path):
    """Revision 2: the header carries a checksum of the recorded results; an edit after recording fails closed."""
    header = edge_corpus.expected_header(CASES, generators.DEFAULT_SEED, 150, ROOT / "samples" / "oracle")
    path = tmp_path / "c.json"
    edge_corpus.save(path, header, {"x": ExecutionResult(engine="oracle", test_id="x", return_value=Decimal("0.23"))})
    edited = path.read_text().replace('"0.23"', '"0.22"')
    assert edited != path.read_text()
    path.write_text(edited)
    reason = edge_corpus.load(path).stale_reason(header)
    assert reason and "do not match the corpus checksum" in reason


def test_source_hashes_ignore_windows_line_endings(tmp_path):
    (tmp_path / "a.sql").write_bytes(b"x\r\ny\r\n")
    lf = tmp_path / "lf"
    lf.mkdir()
    (lf / "a.sql").write_bytes(b"x\ny\n")
    assert edge_corpus.source_hashes(tmp_path) == edge_corpus.source_hashes(lf)


def test_a_float_can_never_be_recorded(tmp_path):
    with pytest.raises(TypeError, match="float"):
        edge_corpus.save(tmp_path / "c.json", {},
                         {"x": ExecutionResult(engine="oracle", test_id="x", return_value=0.1)})


# --- 7. the script: arguments, no corpus ----------------------------------------------------------

def _script():
    import importlib.util

    spec = importlib.util.spec_from_file_location("run_edge_cases", ROOT / "scripts" / "run_edge_cases.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("argv, message", [
    (["--record", "--replay"], "can't be combined"),
    (["--budget", "401"], "maximum supported budget"),
    (["--case", "gen-xx-none-00000000"], "No generated case matches"),
    (["top_earners", "--record"], "records the whole generated set"),
])
def test_the_script_refuses_before_connecting(argv, message, capsys):
    assert _script().main(argv) == 2
    assert message in capsys.readouterr().out


class _FakeOracleModule:
    """Stands in for the oracledb module: the script only connects, reads .version and closes."""

    class Connection:
        version = "23.fake"
        call_timeout = 0

        def close(self):
            pass

    def connect(self, **_kw):
        return self.Connection()


def _record_with(monkeypatch, tmp_path, unstable_id=None, error_contexts=None):
    """Run `run_edge_cases.py --record` with fake engines; Oracle differs on its 2nd run of unstable_id.

    With ``error_contexts`` (first, second), unstable_id raises the same ORA-06502 both times, with
    the given execution context on each run."""
    import app.validation.oracle_runner as oracle_runner
    import app.validation.postgres_runner as postgres_runner

    calls: dict[str, int] = {}

    def fake_oracle(conn, obj, case, tables, objects):
        calls[case.test_id] = calls.get(case.test_id, 0) + 1
        if case.test_id == unstable_id and error_contexts:
            error = ExecutionError(code="ORA-06502", message="ORA-06502: PL/SQL: value or conversion error",
                                   context=error_contexts[calls[case.test_id] - 1])
            return ExecutionResult(engine="oracle", test_id=case.test_id, error=error)
        rows = [{"n": Decimal(calls[case.test_id])}] if case.test_id == unstable_id else None
        return ExecutionResult(engine="oracle", test_id=case.test_id, rows=rows)

    def fake_postgres(conn, obj, case, tables, objects):
        rows = [{"n": Decimal(1)}] if case.test_id == unstable_id else None
        return ExecutionResult(engine="postgres", test_id=case.test_id, rows=rows)

    script = _script()
    monkeypatch.setitem(sys.modules, "oracledb", _FakeOracleModule())
    monkeypatch.setattr(oracle_runner, "run_oracle", fake_oracle)
    monkeypatch.setattr(postgres_runner, "run_postgres", fake_postgres)
    monkeypatch.setattr(script, "deployed_code", lambda _pg, _obj: "deployed")
    psycopg_module = pytest.importorskip("psycopg")
    monkeypatch.setattr(psycopg_module, "connect", lambda *_a, **_k: type("PG", (), {"close": lambda self: None})())
    corpus = tmp_path / "corpus.json"
    code = script.main(["--record", "--corpus", str(corpus), "--out", str(tmp_path / "out")])
    return code, corpus


def test_recording_is_refused_when_one_oracle_result_differs(monkeypatch, tmp_path, capsys):
    unstable = [c for c in CASES if c.object_id == ORG_CHART][0].test_id   # an object routed to Gemini
    code, corpus = _record_with(monkeypatch, tmp_path, unstable)
    out = capsys.readouterr().out
    assert code == 1 and not corpus.exists()
    assert f"Corpus NOT written: 1 case(s) differ between the two Oracle recordings ['{unstable}']" in out


def test_recording_is_refused_when_one_oracle_error_is_raised_in_a_different_place(monkeypatch, tmp_path, capsys):
    unstable = "gen-cb-text_domain-d9209f20"
    code, corpus = _record_with(monkeypatch, tmp_path, unstable, error_contexts=(AT_CALL, INSIDE_CB))
    out = capsys.readouterr().out
    assert code == 1 and not corpus.exists()
    assert f"Corpus NOT written: 1 case(s) differ between the two Oracle recordings ['{unstable}']" in out


def test_recording_writes_a_checksummed_corpus_when_every_case_is_stable(monkeypatch, tmp_path, capsys):
    code, corpus = _record_with(monkeypatch, tmp_path)
    assert code == 0 and corpus.exists() and "Corpus written" in capsys.readouterr().out
    loaded = edge_corpus.load(corpus)
    assert len(loaded.results) == 150 and loaded.header["oracle_version"] == "23.fake"
    expected = edge_corpus.expected_header(CASES, generators.DEFAULT_SEED, 150, ROOT / "samples" / "oracle")
    assert loaded.stale_reason(expected) is None


def test_replay_without_a_corpus_runs_nothing(tmp_path, capsys):
    assert _script().main(["--replay", "--corpus", str(tmp_path / "missing.json")]) == 2
    assert "No corpus" in capsys.readouterr().out


# --- 8. real PostgreSQL, real converter ---------------------------------------------------------

psycopg = pytest.importorskip("psycopg")
from app.validation.postgres_runner import run_postgres  # noqa: E402
from tests.target_pg import _admin_connect  # noqa: E402
from tests.test_validation_7b import WATCH, judge  # noqa: E402
from tests.test_validation_7b import sample as _sample_fixture  # noqa: E402
from tests.test_validation_7b import target as _target_fixture  # noqa: E402

# the 7b fixtures (module scope), registered here under their own names
sample = _sample_fixture
target = _target_fixture


@pytest.fixture(scope="module")
def pg_results(target, sample):
    """Every generated case on the real conversion, as pv_runner (each rolled back)."""
    return {c.test_id: run_postgres(target["runner"], sample[c.object_id], c, WATCH, sample) for c in CASES}


def test_db_every_generated_case_runs_without_a_harness_error(pg_results):
    from app.validation.errors import is_harness_error

    assert not [k for k, r in pg_results.items() if is_harness_error("postgres", r.error)]


def test_db_fixture_rows_are_rolled_back(target, pg_results):
    count = target["runner"].execute("SELECT count(*) FROM employees WHERE emp_id >= 900").fetchone()[0]
    total = target["runner"].execute("SELECT count(*) FROM employees").fetchone()[0]
    target["runner"].rollback()
    assert count == 0 and total == 11


def test_db_fixture_rows_run_only_as_pv_runner(target, sample):
    case = [c for c in CASES if c.object_id == CALC_BONUS and c.fixture_rows][0]
    with _admin_connect(target["name"]) as admin:
        with pytest.raises(PermissionError, match="fixture rows run only as pv_runner"):
            run_postgres(admin, sample[CALC_BONUS], case, WATCH, sample)
        assert admin.execute("SELECT count(*) FROM employees WHERE emp_id >= 900").fetchone()[0] == 0


def test_db_the_postgres_side_of_the_two_oracle_experiments(pg_results):
    """H1 and H2 (16 Nov): PostgreSQL's side, recorded here; Oracle's side is in the 7c report."""
    h1 = pg_results[[c for c in CASES if c.inputs == {"p_emp_id": 104, "p_pct": Decimal("7.5")}][0].test_id]
    assert h1.out_params["p_new_salary"] == Decimal("91375.54")  # Oracle 23 gave 91375.54 too: no rounding
    h2 = pg_results[gen(CALC_BONUS, p_emp_id=102, p_override="AB").test_id]
    assert h2.error.code == "22001" and "calc_bonus" in h2.error.context and "at assignment" in h2.error.context


def test_db_relations_hold_on_the_real_conversion(pg_results):
    for group, members in relations.groups(CASES).items():
        verdict, detail = relations.relation_for(group).check(members, pg_results)
        assert verdict is relations.RelationVerdict.HOLDS, (group, detail)


# careless conversions that pass every golden test (Aarsh, 16 Nov: genuine uncovered semantic boundaries)

def _no_floor(code: str) -> str:
    """ROWNUM <= n without FLOOR: CAST(2.5 AS BIGINT) rounds to 3, Oracle's ROWNUM <= 2.5 gives 2 rows."""
    assert "COALESCE(FLOOR(p_n), 0)" in code
    return code.replace("COALESCE(FLOOR(p_n), 0)", "COALESCE(p_n, 0)")


def _month_end_by_day(code: str) -> str:
    """ADD_MONTHS treating only days 30 and 31 as month ends (wrong for February)."""
    exact = "DATE_TRUNC('DAY', v_month_end) = DATE_TRUNC('MONTH', v_month_end) + INTERVAL '1 month - 1 day'"
    assert exact in code
    return code.replace(exact, "EXTRACT(DAY FROM v_month_end) >= 30")


def _float_net_pay(code: str) -> str:
    """net_pay in double precision: rounds half to even (0.225 -> 0.22) instead of away from zero."""
    exact = "RETURN ROUND(COALESCE(p_gross, 0) * (1 - 0.10), 2);"
    assert exact in code
    return code.replace(exact, "RETURN CAST(ROUND(CAST(COALESCE(p_gross, 0) AS DOUBLE PRECISION) * (1 - 0.10) * 100) "
                               "/ 100 AS NUMERIC);")


CARELESS = {
    # object: (careless code, generated cases the boundary predicts must change)
    TOP_EARNERS: (_no_floor, [{"p_dept_id": 20, "p_n": Decimal(x)} for x in ("0.5", "1.5", "2.5")]
                  + [{"p_dept_id": 30, "p_n": Decimal("2.5")}]),
    MONTHLY_PAYROLL: (_month_end_by_day, ["hire 2025-08-31, run 2026-02-15", "hire 2027-08-31, run 2028-02-15"]),
    NET_PAY: (_float_net_pay, [{"p_gross": Decimal(x)} for x in ("0.25", "-0.25", "2.25")]),
}


def _predicted(object_id, what):
    if object_id == MONTHLY_PAYROLL:
        hires = {"hire 2025-08-31, run 2026-02-15": (907, dt.datetime(2026, 2, 15)),
                 "hire 2027-08-31, run 2028-02-15": (908, dt.datetime(2028, 2, 15))}
        emp, run = hires[what]
        return [c for c in CASES if c.object_id == MONTHLY_PAYROLL and c.inputs == {"p_run_date": run}
                and c.fixture_rows and c.fixture_rows["employees"][0]["emp_id"] == emp][0]
    return gen(object_id, **what)


def _same_postgres_result(case, good, careless) -> bool:
    """Whether the careless conversion gives the real conversion's result (both PostgreSQL)."""
    from app.validation.comparator import compare
    from app.validation.golden_tests import MASKED_COLUMNS

    if good.error or careless.error:
        return (good.error and good.error.code) == (careless.error and careless.error.code)
    as_source = good.model_copy(update={"engine": "oracle"})
    return compare(case, as_source, careless, ordered_rows=bool(case.order_by),
                   masked_columns=MASKED_COLUMNS).status is TestStatus.PASSED


@pytest.mark.parametrize("object_id", list(CARELESS))
def test_db_careless_conversions_pass_every_golden_test_but_not_the_generated_ones(target, sample, pg_results,
                                                                                   object_id):
    make, predicted = CARELESS[object_id]
    deploy_target, _ = target["converter"]._target_for(sample[object_id])
    good = target["first"][object_id].target_code
    assert target["deployer"].deploy(make(good), deploy_target).ok
    try:
        golden = [judge(c, run_postgres(target["runner"], sample[c.object_id], c, WATCH, sample))
                  for cases in GOLDEN_TESTS.values() for c in cases]  # all 29, not only this object's
        mine = [c for c in CASES if c.object_id == object_id]
        careless = {c.test_id: run_postgres(target["runner"], sample[object_id], c, WATCH, sample) for c in mine}
    finally:
        assert target["deployer"].deploy(good, deploy_target).ok
    assert len(golden) == 29 and all(o.status is TestStatus.PASSED for o in golden), \
        [(o.test_id, o.diffs) for o in golden if o.status is not TestStatus.PASSED]
    from app.validation.comparator import compare
    from app.validation.golden_tests import MASKED_COLUMNS

    changed = {k for k, r in careless.items() if not _same_postgres_result(BY_ID[k], pg_results[k], r)}
    expected = {_predicted(object_id, p).test_id for p in predicted}
    assert expected <= changed, (sorted(expected - changed), sorted(changed))
    if CORPUS is not None:  # against Oracle itself: every predicted case fails, every golden test passed above
        for test_id in expected:
            outcome = compare(BY_ID[test_id], CORPUS.results[test_id], careless[test_id], masked_columns=MASKED_COLUMNS)
            assert outcome.status is TestStatus.FAILED, test_id


# --- 9. replay of the Oracle-recorded corpus (D-7c.2) -------------------------------------------

CORPUS = edge_corpus.load(edge_corpus.DEFAULT_CORPUS) if edge_corpus.DEFAULT_CORPUS.exists() else None
needs_corpus = pytest.mark.skipif(CORPUS is None, reason="edge_corpus.json is not recorded yet")
#: Why the committed corpus can't be used (None if it can). Replay fails closed on it: every replay test fails.
CORPUS_STALE = None if CORPUS is None else CORPUS.stale_reason(edge_corpus.expected_header(
    CASES, generators.DEFAULT_SEED, generators.DEFAULT_BUDGET, ROOT / "samples" / "oracle"))


@needs_corpus
def test_db_the_corpus_matches_the_generator_and_the_samples():
    expected = edge_corpus.expected_header(CASES, generators.DEFAULT_SEED, generators.DEFAULT_BUDGET,
                                           ROOT / "samples" / "oracle")
    assert CORPUS.stale_reason(expected) is None
    assert set(CORPUS.results) == set(BY_ID)
    assert CORPUS.header["oracle_version"].startswith("23.")


@needs_corpus
@pytest.mark.parametrize("test_id", sorted(BY_ID))
def test_db_replay(sample, pg_results, test_id):
    """Every generated case against Oracle's recorded result. Known non-passes are strict expectations."""
    from app.validation.comparator import compare
    from app.validation.golden_tests import MASKED_COLUMNS

    assert CORPUS_STALE is None, CORPUS_STALE  # fail closed: never judge against a stale reference
    case = BY_ID[test_id]
    outcome = compare(case, CORPUS.results[test_id], pg_results[test_id], ordered_rows=bool(case.order_by),
                      masked_columns=MASKED_COLUMNS)
    result = triage.classify(case, outcome, object_name=sample[case.object_id].name,
                             routing=triage.routing_reasons(sample.values()), deployed=True)
    known = KNOWN_NON_PASSES.get(test_id)
    if known is None:
        assert result is None, (result.failure_class.value, result.reason)
    else:
        assert result is not None, f"{test_id} now passes: remove it from KNOWN_NON_PASSES ({known[1]})"
        assert result.failure_class.value == known[0], (result.failure_class.value, result.reason)


@needs_corpus
def test_db_relations_hold_on_oracle_and_postgresql(pg_results):
    assert CORPUS_STALE is None, CORPUS_STALE
    for result in relations.evaluate(CASES, CORPUS.results, pg_results):
        assert result.oracle is relations.RelationVerdict.HOLDS, (result.group, result.oracle_detail)
        assert not result.defect, (result.group, result.postgres_detail)


def test_known_non_passes_have_a_class_and_a_reference():
    for test_id, (cls, reference) in KNOWN_NON_PASSES.items():
        assert test_id in BY_ID and triage.FailureClass(cls) and reference
        if cls == triage.FailureClass.TRANSLATION_DEFECT.value:
            assert reference.startswith("DEF-7c-")


def test_the_report_records_in_json(tmp_path):
    record = _script().write_failure(tmp_path, CASE_IN, _outcome(_result("oracle", return_value="A"),
                                                                 _result("postgres", return_value="B")),
                                     triage.Triage("x", TOP_EARNERS, triage.FailureClass.TRANSLATION_DEFECT, "r", True),
                                     {"seed": 1})
    body = json.loads(record.read_text())
    assert body["class"] == "translation_defect" and body["case"]["test_id"] == CASE_IN.test_id
    again = _script().case_from_failure(record)
    assert again.inputs == CASE_IN.inputs and again.generation == CASE_IN.generation

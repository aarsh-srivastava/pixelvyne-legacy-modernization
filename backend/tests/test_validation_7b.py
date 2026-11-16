"""Step 7b: behavioural validation of the whole answer key (29 golden tests, 8 objects).

1. Contract 0.5: SetupCall, TestCase.setup / action / order_by; older test cases still load.
2. Trigger actions (D-7b.2): trusted origin, defined in our code, one plain DML statement.
3. Runners and harness without a database: setup order, view queries, ordered rows, refusals.
4. Real PostgreSQL, real converter: all 29 tests judged against Oracle's recorded answers.
   org_chart is the Gemini route; its SQL here is a RECORDED Gemini answer (the same
   recursive CTE as in the 7a tests), never a live call.
5. Negative controls: a careless conversion fails exactly the tests predicted for it.
6. The 7a repair loop on the Gemini route, driven by real view rows and real FieldDiffs.

No test calls Gemini or Oracle. Live Oracle 23 checks the answer key on Tarun's laptop
(scripts/run_validation.py all); see the milestone report.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.contracts import CONTRACT_VERSION, SetupCall, TestCase
from app.contracts.models import (
    ExecutionError,
    ExecutionResult,
    IRObject,
    ObjectType,
    TableDiff,
    TestStatus,
    ValidationResult,
    ValidationStatus,
)
from app.validation import actions, harness, oracle_runner
from app.validation.actions import UnsafeAction, check_statement, checked_action
from app.validation.answer_key import answer_key_diffs, judge
from app.validation.golden_tests import (
    CLOSE_MONTH,
    GOLDEN_TESTS,
    MASKED_COLUMNS,
    MONTHLY_PAYROLL,
    ORACLE_EXPECTED,
    ORG_CHART,
    SALARY_AUDIT_TRG,
)

ROOT = Path(__file__).resolve().parents[2]
ALL_CASES = [c for cases in GOLDEN_TESTS.values() for c in cases]
CASE = {c.test_id: c for c in ALL_CASES}


def case(test_id: str, **changes) -> TestCase:
    return CASE[test_id].model_copy(update=changes)


# --- 1. contract 0.5 ---------------------------------------------------------------------

def test_contract_is_0_5_and_the_answer_key_is_complete():
    assert CONTRACT_VERSION == "0.5"
    assert len(ALL_CASES) == 29 and len(GOLDEN_TESTS) == 9  # 8 logical objects; payroll_pkg has 2 members
    assert set(CASE) == set(ORACLE_EXPECTED)
    assert all(c.origin == "golden" for c in ALL_CASES)


def test_a_0_4_test_case_without_the_new_fields_still_loads():
    old = {"test_id": "cb-01", "object_id": "procedure:legacy.calc_bonus", "description": "d",
           "inputs": {"p_emp_id": 102}, "fixture_rows": {}, "setup_calls": ["procedure:legacy.x"],
           "frozen_now": None, "origin": "golden", "rationale": None}
    loaded = TestCase.model_validate_json(json.dumps(old))
    assert loaded.setup == [] and loaded.action is None and loaded.order_by == []
    assert loaded.setup_steps() == [SetupCall(object_id="procedure:legacy.x")]


def test_setup_calls_run_before_setup_and_round_trip():
    tc = TestCase(test_id="t", object_id="o", description="d", setup_calls=["a"],
                  setup=[SetupCall(object_id="b", inputs={"p_run_date": dt.datetime(2026, 9, 15)})])
    assert [s.object_id for s in tc.setup_steps()] == ["a", "b"]
    again = TestCase.model_validate_json(tc.model_dump_json())
    assert again.setup[0].object_id == "b" and again.setup[0].inputs["p_run_date"] is not None


@pytest.mark.parametrize("bad", ["emp_id; DROP TABLE x", "emp id", "1col", "e.emp_id", ""])
def test_order_by_takes_only_plain_column_names(bad):
    with pytest.raises(ValidationError, match="plain column names"):
        TestCase(test_id="t", object_id="o", description="d", order_by=[bad])


def test_order_by_is_lower_cased():
    assert TestCase(test_id="t", object_id="o", description="d", order_by=["EMP_ID"]).order_by == ["emp_id"]


def test_the_exported_test_case_schema_includes_setup_call():
    schema = TestCase.model_json_schema()
    assert {"setup", "action", "order_by"} <= set(schema["properties"])
    assert "SetupCall" in schema["$defs"] and not {"setup", "action", "order_by"} & set(schema.get("required", []))


# --- 2. trigger actions (D-7b.2) -----------------------------------------------------------

def test_the_golden_actions_are_the_trusted_ones():
    assert actions.trusted_actions() == {
        "UPDATE employees SET base_salary = 70000 WHERE emp_id = 106",
        "UPDATE employees SET base_salary = 60000 WHERE emp_id = 106",
        "UPDATE employees SET base_salary = base_salary WHERE emp_id = 106",
    }


@pytest.mark.parametrize("statement", [
    "UPDATE employees SET base_salary = 70000 WHERE emp_id = 106",
    "update employees set full_name = 'a;b -- c' where emp_id = 1;",   # ; and -- inside a literal are data
    "INSERT INTO salary_audit (audit_id) VALUES (1)",
    "DELETE FROM payroll_runs WHERE emp_id = 1",
])
def test_one_plain_dml_statement_passes_the_statement_check(statement):
    assert check_statement(statement) == statement.rstrip(";")


@pytest.mark.parametrize("statement, reason", [
    ("", "empty"),
    ("SELECT 1", "must be INSERT, UPDATE or DELETE"),
    ("DROP TABLE employees", "must be INSERT, UPDATE or DELETE"),
    ("CALL apply_raise(104, 7, NULL)", "must be INSERT, UPDATE or DELETE"),
    ("UPDATE employees SET base_salary = 1; DROP TABLE employees", "more than one statement"),
    ("UPDATE employees SET base_salary = 1 -- WHERE emp_id = 1", "comment"),
    ("UPDATE employees /* x */ SET base_salary = 1", "comment"),
    ("UPDATE employees SET full_name = 'x", "unterminated"),
    ("UPDATE employees SET base_salary = pg_sleep(10)", "PG_SLEEP"),
    ("DELETE FROM employees WHERE emp_id IN (SELECT dbms_random.value FROM dual)", "DBMS_RANDOM"),
    ("UPDATE employees SET x = 1 WHERE EXECUTE IMMEDIATE", "EXECUTE"),
    ("INSERT INTO t SELECT * FROM lo_import('/etc/passwd')", "LO_IMPORT"),
    ("UPDATE t SET a = 1\x00", "NUL"),
])
def test_anything_else_is_refused(statement, reason):
    with pytest.raises(UnsafeAction, match=reason):
        check_statement(statement)


def test_an_llm_origin_test_may_not_supply_an_action_even_a_trusted_one():
    with pytest.raises(UnsafeAction, match="only run for golden or rule tests"):
        checked_action(case("tr-01", origin="llm"))


def test_origin_alone_is_not_enough_the_statement_must_be_ours():
    """D-7b.2: a test case from outside (JSON, upload) cannot add a statement by claiming origin."""
    forged = case("tr-01", action="UPDATE employees SET base_salary = 0 WHERE emp_id = 101")
    assert forged.origin == "golden"
    with pytest.raises(UnsafeAction, match="not one defined in this repository's tests"):
        checked_action(forged)


def test_a_trusted_action_is_returned_without_its_semicolon():
    assert checked_action(case("tr-01", action=CASE["tr-01"].action + ";")) == CASE["tr-01"].action


# --- 3. runners and harness, no database -----------------------------------------------------

class FakeVar:
    def __init__(self):
        self.value = None

    def setvalue(self, _pos, value):
        self.value = value

    def getvalue(self):
        return self.value


class FakeOracleCursor:
    """Records what the Oracle runner asks Oracle to do, in order."""

    def __init__(self, log, view_rows=()):
        self.log, self.view_rows, self.description, self._rows = log, list(view_rows), None, []

    def var(self, _type):
        return FakeVar()

    def callproc(self, name, args):
        self.log.append(("callproc", name))
        for a in args:
            if isinstance(a, FakeVar):
                a.setvalue(0, Decimal("7"))

    def callfunc(self, name, _type, args):
        self.log.append(("callfunc", name))
        return Decimal("1")

    def execute(self, statement, **_binds):
        self.log.append(("execute", " ".join(statement.split())))
        if statement.startswith("SELECT * FROM org_chart"):
            self.description = [("EMP_ID",), ("DEPTH",)]
            self._rows = self.view_rows
        else:
            self.description = [("X",)]
            self._rows = []

    def fetchall(self):
        return self._rows


class FakeOracle:
    def __init__(self, view_rows=()):
        self.log: list = []
        self.view_rows = view_rows

    def cursor(self):
        return FakeOracleCursor(self.log, self.view_rows)

    def rollback(self):
        self.log.append(("rollback",))


def ir(object_id: str, type_: ObjectType, name: str, package=None, params=()):
    return IRObject(object_id=object_id, name=name, type=type_, package=package, params=list(params),
                    source_code="...")


@pytest.fixture(scope="module")
def sample():
    from app.parsers.oracle_parser import parse_sources

    sources = {p.name: p.read_text() for p in sorted((ROOT / "samples" / "oracle").glob("*.sql"))}
    return {o.object_id: o for o in parse_sources(sources)}


def test_oracle_setup_runs_before_the_snapshot_and_the_call(sample):
    db = FakeOracle()
    result = oracle_runner.run_oracle(db, sample[CLOSE_MONTH], CASE["pk-04"], ["payroll_runs"], sample)
    steps = [e[0] if e[0] != "execute" else e[1].split(" FROM ")[-1].lower() for e in db.log]
    assert db.log[0] == ("callproc", "monthly_payroll")          # setup first
    assert ("callproc", "payroll_pkg.close_month") in db.log
    assert steps.index("callproc") < steps.index("payroll_runs")  # snapshot after setup
    assert db.log[-1] == ("rollback",) and result.error is None


def test_oracle_view_is_queried_in_order_and_rows_lower_cased(sample):
    db = FakeOracle(view_rows=[(Decimal(101), Decimal(1))])
    result = oracle_runner.run_oracle(db, sample[ORG_CHART], CASE["oc-01"], [], sample)
    assert ("execute", "SELECT * FROM org_chart ORDER BY emp_id") in db.log
    assert result.rows == [{"emp_id": Decimal(101), "depth": Decimal(1)}]


def test_oracle_trigger_action_runs_only_when_trusted(sample):
    db = FakeOracle()
    oracle_runner.run_oracle(db, sample[SALARY_AUDIT_TRG], CASE["tr-01"], ["salary_audit"], sample)
    assert ("execute", CASE["tr-01"].action) in db.log
    db = FakeOracle()
    with pytest.raises(UnsafeAction):
        oracle_runner.run_oracle(db, sample[SALARY_AUDIT_TRG], case("tr-01", origin="llm"), [], sample)
    assert not any(e[0] == "execute" and e[1].startswith("UPDATE") for e in db.log)


def test_a_missing_setup_object_is_refused_before_anything_runs(sample):
    db = FakeOracle()
    with pytest.raises(LookupError, match="setup object"):
        oracle_runner.run_oracle(db, sample[CLOSE_MONTH], CASE["pk-04"], [], objects={})
    assert db.log == []


def test_the_harness_turns_refusals_into_harness_errors(sample, monkeypatch):
    monkeypatch.setattr(harness, "run_postgres", lambda *a: ExecutionResult(engine="postgres", test_id="tr-01"))
    outcome = harness.run_case(FakeOracle(), None, sample[SALARY_AUDIT_TRG], case("tr-01", origin="llm"),
                               ["salary_audit"], MASKED_COLUMNS, sample)
    assert outcome.status is TestStatus.ERROR and outcome.source.error.code == "HARNESS"
    assert "only run for golden or rule tests" in outcome.source.error.message


@pytest.mark.parametrize("ordered, status", [(True, TestStatus.FAILED), (False, TestStatus.PASSED)])
def test_rows_are_compared_in_order_only_when_the_test_says_so(sample, monkeypatch, ordered, status):
    rows = [{"emp_id": 1}, {"emp_id": 2}]
    monkeypatch.setattr(harness, "run_oracle",
                        lambda *a: ExecutionResult(engine="oracle", test_id="oc-01", rows=rows))
    monkeypatch.setattr(harness, "run_postgres",
                        lambda *a: ExecutionResult(engine="postgres", test_id="oc-01", rows=rows[::-1]))
    test = case("oc-01", order_by=["emp_id"] if ordered else [])
    assert harness.run_case(None, None, sample[ORG_CHART], test, [], None, sample).status is status


def test_the_answer_key_judge_uses_the_comparator_rules():
    good = ExecutionResult(engine="postgres", test_id="pk-01", return_value=Decimal("9000"))
    assert answer_key_diffs(CASE["pk-01"], good) == []                     # 9000 == 9000.00
    wrong = good.model_copy(update={"return_value": Decimal("9000.01")})
    assert [d.path for d in judge(CASE["pk-01"], wrong).diffs] == ["return_value"]
    no_data = ExecutionResult(engine="postgres", test_id="cb-06", error=ExecutionError(code="P0002", message="x"))
    assert judge(CASE["cb-06"], no_data).status is TestStatus.PASSED      # ORA-01403 == P0002
    missing = ExecutionResult(engine="postgres", test_id="oc-01", error=ExecutionError(code="42P01", message="x"))
    assert judge(CASE["oc-01"], missing).status is TestStatus.ERROR       # not judged, never a FAIL


def test_masked_columns_are_left_out_of_inserted_rows():
    row = {**ORACLE_EXPECTED["tr-01"]["inserted"]["salary_audit"][0], "audit_id": 999,
           "changed_on": dt.datetime(2030, 1, 1)}
    result = ExecutionResult(engine="postgres", test_id="tr-01", table_diffs=[
        TableDiff(table="employees", updated=[{"key": {"emp_id": 106}}]),
        TableDiff(table="salary_audit", inserted=[row])])
    assert answer_key_diffs(CASE["tr-01"], result) == []


def test_the_run_order_puts_setup_objects_and_triggers_first(sample):
    from app.orchestration.run_order import in_test_order
    from app.validation.golden_tests import APPLY_RAISE, golden_tests_for

    order = [o.object_id for o in in_test_order(sample.values(), golden_tests_for)]
    assert set(order) == set(GOLDEN_TESTS) and len(order) == 9           # only objects with tests
    assert order.index(MONTHLY_PAYROLL) < order.index(CLOSE_MONTH)       # pk-04's setup
    assert order.index(SALARY_AUDIT_TRG) < order.index(APPLY_RAISE)      # apply_raise fires the trigger


def test_the_run_order_survives_a_setup_cycle(sample):
    from app.orchestration.run_order import in_test_order

    def tests_for(object_id):
        if object_id == CLOSE_MONTH:
            return [case("pk-04")]
        if object_id == MONTHLY_PAYROLL:  # a (nonsensical) cycle must not recurse forever
            return [case("mp-01", setup=[SetupCall(object_id=CLOSE_MONTH)])]
        return []

    order = [o.object_id for o in in_test_order(sample.values(), tests_for)]
    assert sorted(order) == sorted([CLOSE_MONTH, MONTHLY_PAYROLL])


# --- 4. real PostgreSQL, real converter ----------------------------------------------------

psycopg = pytest.importorskip("psycopg")
from app.migration.converter import OracleToPostgresConverter  # noqa: E402
from app.migration.llm_fallback import GeminiFallback  # noqa: E402
from app.migration.postgres_deployer import PostgresDeployer  # noqa: E402
from app.migration.target_roles import DEPLOYER_ROLE, RUNNER_ROLE, ensure_roles  # noqa: E402
from app.orchestration.repair_loop import RepairDeps, RepairOutcome, run_repair_loop  # noqa: E402
from app.parsers.dependency_analyzer import analyze_dependencies  # noqa: E402
from app.validation.postgres_runner import run_postgres  # noqa: E402
from tests.target_pg import DEPLOYER_PASSWORD, RUNNER_PASSWORD, _admin_connect, connect_as  # noqa: E402
from tests.test_converter_gemini import ORG_SQL, answer  # noqa: E402

#: org_chart's SQL as Gemini RECORDED it (the recursive CTE also used by the 7a tests).
#: Replayed by a fake generate(); no test makes a live Gemini call.
RECORDED_GEMINI_ORG_CHART = answer(ORG_SQL)
#: A recorded Gemini answer with LEVEL off by one (depth starts at 0): a real behavioural bug.
RECORDED_GEMINI_ORG_CHART_OFF_BY_ONE = answer(ORG_SQL.replace("1 AS depth", "0 AS depth"))


class Recorded:
    """A fake generate() that replays recorded Gemini answers in order."""

    def __init__(self, *answers):
        self.answers, self.prompts = list(answers), []

    def __call__(self, prompt, *, system=None):
        self.prompts.append(prompt)
        return self.answers[min(len(self.prompts), len(self.answers)) - 1]


@pytest.fixture(scope="module")
def target(sample):
    """A throwaway PostgreSQL database holding the real converter's output for the whole sample."""
    try:
        server = _admin_connect()
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL for target tests not reachable ({type(exc).__name__})")
    name = f"pv_test_{uuid.uuid4().hex[:8]}"
    server.execute(f'CREATE DATABASE "{name}"')
    try:
        with _admin_connect(name) as admin:
            for path in ("01_schema.sql", "02_seed_data.sql"):  # D8 phase 1: tables, sequences, data
                admin.execute((ROOT / "samples" / "postgres" / path).read_text())
            admin.autocommit = False
            ensure_roles(admin, deployer_password=DEPLOYER_PASSWORD, runner_password=RUNNER_PASSWORD)
        with connect_as(name, DEPLOYER_ROLE) as deployer_conn, connect_as(name, RUNNER_ROLE) as runner_conn:
            deployer = PostgresDeployer(deployer_conn, database=name)
            converter = OracleToPostgresConverter(deployer, sample.values(),
                                                  llm=GeminiFallback(Recorded(RECORDED_GEMINI_ORG_CHART)))
            first = {oid: converter.convert(sample[oid], run_id="deploy")
                     for oid in analyze_dependencies(list(sample.values())).execution_order}
            yield {"name": name, "deployer": deployer, "converter": converter, "runner": runner_conn,
                   "first": first}
    finally:
        server.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                       "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
        server.execute(f'DROP DATABASE IF EXISTS "{name}"')
        server.close()


WATCH = ["departments", "employees", "payroll_runs", "salary_audit"]


def run_and_judge(target, sample, test):
    return judge(test, run_postgres(target["runner"], sample[test.object_id], test, WATCH, sample))


def test_db_the_real_converter_deploys_all_eight_objects(target):
    first = target["first"]
    deployed = {oid for oid, r in first.items() if r.compiled_ok}
    assert set(GOLDEN_TESTS) <= deployed  # every tested object, incl. the view (recorded Gemini answer)
    assert first[ORG_CHART].tool.value == "gemini" and first[ORG_CHART].needs_review


@pytest.mark.parametrize("test_id", [c.test_id for c in ALL_CASES])
def test_db_every_answer_key_test_passes_on_the_real_conversion(target, sample, test_id):
    outcome = run_and_judge(target, sample, CASE[test_id])
    assert outcome.status is TestStatus.PASSED, [(d.path, d.source_value, d.target_value) for d in outcome.diffs]


def test_db_the_view_returns_eleven_rows_in_order(target, sample):
    result = run_postgres(target["runner"], sample[ORG_CHART], CASE["oc-01"], WATCH, sample)
    assert [r["emp_id"] for r in result.rows] == [101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111]
    assert result.rows[7]["reporting_line"] == " > Asha Verma > Rohan Mehta > Karan Singh > Arjun Das"


def test_db_trigger_tests_see_only_their_own_audit_row(target, sample):
    result = run_postgres(target["runner"], sample[SALARY_AUDIT_TRG], CASE["tr-02"], WATCH, sample)
    audit = [d for d in result.table_diffs if d.table == "salary_audit"]
    assert len(audit) == 1 and [r["change_type"] for r in audit[0].inserted] == ["CUT"]
    again = run_postgres(target["runner"], sample[SALARY_AUDIT_TRG], CASE["tr-02"], WATCH, sample)
    assert answer_key_diffs(CASE["tr-02"], again) == []  # rolled back: the test is repeatable
    assert again.table_diffs[1].inserted[0]["audit_id"] != audit[0].inserted[0]["audit_id"]  # why it is masked


def test_db_a_setup_call_is_not_part_of_what_is_compared(target, sample):
    result = run_postgres(target["runner"], sample[CLOSE_MONTH], CASE["pk-04"], WATCH, sample)
    assert result.out_params["p_total"] == Decimal("91075.14") and result.table_diffs == []


def test_db_actions_run_only_as_pv_runner(target, sample):
    with _admin_connect(target["name"]) as admin:
        result_error = None
        try:
            run_postgres(admin, sample[SALARY_AUDIT_TRG], CASE["tr-01"], WATCH, sample)
        except PermissionError as exc:
            result_error = str(exc)
        audit_rows = admin.execute("SELECT count(*) FROM salary_audit").fetchone()[0]
    assert result_error is not None and result_error.startswith("trigger actions run only as pv_runner, not as ")
    assert audit_rows == 0


def test_db_a_refused_action_changes_nothing(target, sample):
    forged = case("tr-01", action="UPDATE employees SET base_salary = 0 WHERE emp_id = 101")
    with pytest.raises(UnsafeAction):
        run_postgres(target["runner"], sample[SALARY_AUDIT_TRG], forged, WATCH, sample)
    salary = target["runner"].execute("SELECT base_salary FROM employees WHERE emp_id = 101").fetchone()[0]
    target["runner"].rollback()
    assert salary == Decimal("240000.00")


# --- 5. negative controls --------------------------------------------------------------------

def _careless_payroll(code: str) -> str:
    """ADD_MONTHS(month_end, -6) converted literally: month_end - 6 months (30 Mar, not 31 Mar)."""
    start, end = code.index("e.hire_date > CASE"), code.index(" THEN 0 ELSE 2500")
    return code[:start] + "e.hire_date > v_month_end - INTERVAL '6 months'" + code[end:]


def _careless_trigger(code: str) -> str:
    """The WHEN clause dropped: the trigger fires even when the salary does not change."""
    return code.replace("WHEN (NEW.base_salary <> OLD.base_salary)\n", "")


NEGATIVE_CONTROLS = {
    # object: (make the careless code, the tests EXPECTED.md predicts it fails)
    MONTHLY_PAYROLL: (_careless_payroll, {"mp-01", "pk-04"}),     # pk-04 inherits it via its setup call
    # Without WHEN, an unchanged salary is audited: tr-03, and ar-02 (a 0% raise, "no audit row").
    SALARY_AUDIT_TRG: (_careless_trigger, {"tr-03", "ar-02"}),
    ORG_CHART: (lambda _code: ORG_SQL.replace("1 AS depth", "0 AS depth"), {"oc-01"}),
}


@pytest.mark.parametrize("object_id", list(NEGATIVE_CONTROLS))
def test_db_negative_controls_fail_exactly_the_predicted_tests(target, sample, object_id):
    make_careless, predicted = NEGATIVE_CONTROLS[object_id]
    good = target["first"][object_id].target_code
    careless = make_careless(good)
    assert careless != good
    deploy_target, _ = target["converter"]._target_for(sample[object_id])
    assert target["deployer"].deploy(careless, deploy_target).ok
    try:
        failed = {c.test_id for c in ALL_CASES if run_and_judge(target, sample, c).status is TestStatus.FAILED}
    finally:
        assert target["deployer"].deploy(good, deploy_target).ok
    assert failed == predicted


def test_db_the_payroll_control_fails_on_row_108_only(target, sample):
    deploy_target, _ = target["converter"]._target_for(sample[MONTHLY_PAYROLL])
    good = target["first"][MONTHLY_PAYROLL].target_code
    assert target["deployer"].deploy(_careless_payroll(good), deploy_target).ok
    try:
        outcome = run_and_judge(target, sample, CASE["mp-01"])
    finally:
        assert target["deployer"].deploy(good, deploy_target).ok
    by_kind = {d.kind: d for d in outcome.diffs}
    assert set(by_kind) == {"missing", "extra"} and len(outcome.diffs) == 2
    missing = by_kind["missing"].source_value
    assert missing["emp_id"] == 108 and missing["gross_pay"] == Decimal("7166.67")
    assert by_kind["extra"].target_value["gross_pay"] == Decimal("4666.67")


# --- 6. the 7a repair loop on the Gemini route, with real rows ---------------------------------

def test_db_a_failing_view_is_repaired_from_real_row_differences(target, sample):
    recorded = Recorded(RECORDED_GEMINI_ORG_CHART_OFF_BY_ONE, RECORDED_GEMINI_ORG_CHART)
    converter = OracleToPostgresConverter(target["deployer"], sample.values(), llm=GeminiFallback(recorded))
    seen = []

    def validate(obj, cases, version):
        outcomes = [run_and_judge(target, sample, c) for c in cases]
        seen.append((version, [d.path for o in outcomes for d in o.diffs][:3]))
        failed = sum(o.status is TestStatus.FAILED for o in outcomes)
        return ValidationResult(object_id=obj.object_id, conversion_version=version, tests=outcomes,
                                passed=len(outcomes) - failed, failed=failed,
                                status=ValidationStatus.REQUIRES_REPAIR if failed else ValidationStatus.PASSED)

    run = run_repair_loop(RepairDeps(converter=converter, validate=validate), sample[ORG_CHART],
                          GOLDEN_TESTS[ORG_CHART], run_id="7b-demo")
    assert run.outcome is RepairOutcome.PASSED_NEEDS_REVIEW and len(recorded.prompts) == 2
    assert seen == [(1, ["rows[0].depth", "rows[1].depth", "rows[2].depth"]), (2, [])]
    repair_prompt = recorded.prompts[1]
    assert '    rows[0].depth (value): Oracle="1" PostgreSQL=0' in repair_prompt  # 6a renders Decimal quoted
    assert repair_prompt.count(".depth (value)") == 11
    assert run.final_validation.passed == 1 and run.attempts[0].failing_tests == ["oc-01"]
    assert run_and_judge(target, sample, CASE["oc-01"]).status is TestStatus.PASSED  # the good view is live


# --- 7. run_repair.py exit codes: 0 only when everything passed with nothing outstanding -------

def _load_run_repair():
    import importlib.util

    spec = importlib.util.spec_from_file_location("run_repair_script", ROOT / "scripts" / "run_repair.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(object_id, outcome, tests=1, passed=None):
    from types import SimpleNamespace

    validation = None
    if tests is not None:
        outcomes = [CASE["pk-01"]] * tests
        validation = SimpleNamespace(tests=outcomes, passed=len(outcomes) if passed is None else passed)
    return SimpleNamespace(object_id=object_id, outcome=outcome, final_validation=validation)


@pytest.fixture(scope="module")
def run_repair_script():
    return _load_run_repair()


def test_exit_0_only_when_every_object_fully_passed(run_repair_script):
    from app.orchestration.repair_loop import RepairOutcome as O

    code, why = run_repair_script.exit_status([_run("a", O.PASSED), _run("b", O.PASSED, tests=5)])
    assert code == 0 and why.startswith("Exit 0 (ALL PASSED)")


@pytest.mark.parametrize("outstanding", ["review", "no_tests", "never_validated"])
def test_exit_3_when_something_still_awaits_sign_off_or_testing(run_repair_script, outstanding):
    from app.orchestration.repair_loop import RepairOutcome as O

    other = {"review": _run("b", O.PASSED_NEEDS_REVIEW),
             "no_tests": _run("b", O.PASSED, tests=0),
             "never_validated": _run("b", O.PASSED, tests=None)}[outstanding]
    code, why = run_repair_script.exit_status([_run("a", O.PASSED), other])
    assert code == 3 and why.startswith("Exit 3 (NEEDS SIGN-OFF)") and why.endswith(": b.")


def test_exit_1_takes_precedence_over_sign_off(run_repair_script):
    """The laptop run of 16 Nov: 7 passed, monthly_payroll flagged, org_chart needs Gemini -> 1."""
    from app.orchestration.repair_loop import RepairOutcome as O

    runs = [_run(f"o{i}", O.PASSED) for i in range(7)]
    runs += [_run("monthly_payroll", O.PASSED_NEEDS_REVIEW), _run("org_chart", O.NEEDS_HUMAN, tests=0)]
    code, why = run_repair_script.exit_status(runs)
    assert code == 1 and why == "Exit 1 (NEEDS HUMAN): 1 object(s) stopped: org_chart."


def test_with_gemini_everything_passing_still_exits_3_for_review(run_repair_script):
    """Gemini results always need review, so a fully converted sample is 'done but not approved'."""
    from app.orchestration.repair_loop import RepairOutcome as O

    runs = [_run(f"o{i}", O.PASSED) for i in range(7)]
    runs += [_run("monthly_payroll", O.PASSED_NEEDS_REVIEW), _run("org_chart", O.PASSED_NEEDS_REVIEW)]
    assert run_repair_script.exit_status(runs)[0] == 3


def test_exit_2_when_nothing_was_run(run_repair_script, monkeypatch, capsys):
    assert run_repair_script.exit_status([])[0] == 2
    cases = [["run_repair.py", "all", "--gemini"],                              # --gemini needs real
             ["run_repair.py", "no_such_object"],                               # unknown object
             ["run_repair.py", "all", "--converter", "real", "--gemini"]]       # no key configured
    monkeypatch.setattr(run_repair_script.settings, "GEMINI_API_KEY", "")
    for argv in cases:
        monkeypatch.setattr("sys.argv", argv)
        assert run_repair_script.main() == 2, argv
    assert "Nothing was run." in capsys.readouterr().out

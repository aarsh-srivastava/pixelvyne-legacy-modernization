"""Golden test cases, copied from samples/oracle/EXPECTED.md.

Each TestCase is run on Oracle and on PostgreSQL by the harness; the two results are
compared with each other, not with these notes. ORACLE_EXPECTED is a separate sanity
check: it confirms the Oracle run matches the answer key, i.e. that the harness itself
calls things correctly. If a real Oracle run disagrees with it, Oracle wins: fix
EXPECTED.md and this file.

Input keys are lower-case parameter names, as in the IRObject contract.
"""

from __future__ import annotations

from decimal import Decimal

from app.contracts.models import TestCase

CALC_BONUS = "procedure:calc_bonus"
GET_EMP_GRADE = "function:get_emp_grade"
TOP_EARNERS = "function:top_earners"
APPLY_RAISE = "procedure:apply_raise"


def _t(test_id: str, object_id: str, description: str, **inputs) -> TestCase:
    return TestCase(test_id=test_id, object_id=object_id, origin="golden",
                    description=description, inputs=inputs)


GOLDEN_TESTS: dict[str, list[TestCase]] = {
    CALC_BONUS: [
        TestCase(test_id="cb-01", object_id=CALC_BONUS, origin="golden",
                 description="Happy path, rating A",
                 inputs={"p_emp_id": 102, "p_override": None}),
        TestCase(test_id="cb-02", object_id=CALC_BONUS, origin="golden",
                 description="Trap: Oracle treats '' as NULL, so the stored rating is used",
                 inputs={"p_emp_id": 102, "p_override": ""}),
        TestCase(test_id="cb-03", object_id=CALC_BONUS, origin="golden",
                 description="Override 'A' replaces stored rating B",
                 inputs={"p_emp_id": 103, "p_override": "A"}),
        TestCase(test_id="cb-04", object_id=CALC_BONUS, origin="golden",
                 description="Unrated employee gets 0",
                 inputs={"p_emp_id": 106, "p_override": None}),
        TestCase(test_id="cb-05", object_id=CALC_BONUS, origin="golden",
                 description="4250.025 rounds half up to 4250.03",
                 inputs={"p_emp_id": 105, "p_override": None}),
        TestCase(test_id="cb-06", object_id=CALC_BONUS, origin="golden",
                 description="Trap: unknown employee must raise no-data-found",
                 inputs={"p_emp_id": 999, "p_override": None}),
    ],
    GET_EMP_GRADE: [
        _t("gg-01", GET_EMP_GRADE, "Happy path", p_emp_id=101),
        _t("gg-02", GET_EMP_GRADE, "NVL on a NULL rating", p_emp_id=106),
        _t("gg-03", GET_EMP_GRADE, "Boundary: 101000.75 >= 100000", p_emp_id=111),
        _t("gg-04", GET_EMP_GRADE, "Rating B, standard band", p_emp_id=104),
        _t("gg-05", GET_EMP_GRADE, "Trap: unknown employee hits the NO_DATA_FOUND handler", p_emp_id=999),
    ],
    TOP_EARNERS: [
        _t("te-01", TOP_EARNERS, "Salary tie broken by emp_id", p_dept_id=20, p_n=2),
        _t("te-02", TOP_EARNERS, "Single result", p_dept_id=30, p_n=1),
        _t("te-03", TOP_EARNERS, "N larger than the department", p_dept_id=30, p_n=10),
        _t("te-04", TOP_EARNERS, "Empty department", p_dept_id=40, p_n=3),
        _t("te-05", TOP_EARNERS, "Inactive employee excluded", p_dept_id=10, p_n=5),
    ],
    APPLY_RAISE: [
        _t("ar-01", APPLY_RAISE, "Trap: 7% raise; integer division would make it 0%", p_emp_id=104, p_pct=7),
        _t("ar-02", APPLY_RAISE, "0% raise: salary unchanged, trigger writes no audit row", p_emp_id=106, p_pct=0),
        _t("ar-03", APPLY_RAISE, "Trap: unknown employee must return -1 and change nothing", p_emp_id=999, p_pct=5),
    ],
}

# Columns left out of table comparisons, per EXPECTED.md: sequence values are not
# rolled back in Oracle, and SYSDATE-based dates depend on when the test runs.
MASKED_COLUMNS: dict[str, set[str]] = {
    "salary_audit": {"audit_id", "changed_on"},
    "payroll_runs": {"run_id"},
}

# What Oracle itself must return, per EXPECTED.md. "error" = the Oracle error code,
# "tables_changed" = which watched tables must show a side effect.
ORACLE_EXPECTED: dict[str, dict] = {
    "cb-01": {"out_params": {"p_bonus": Decimal("24000.00")}},
    "cb-02": {"out_params": {"p_bonus": Decimal("24000.00")}},
    "cb-03": {"out_params": {"p_bonus": Decimal("19600.00")}},
    "cb-04": {"out_params": {"p_bonus": Decimal("0")}},
    "cb-05": {"out_params": {"p_bonus": Decimal("4250.03")}},
    "cb-06": {"error": "ORA-01403"},
    "gg-01": {"return_value": "G1-SENIOR"},
    "gg-02": {"return_value": "UNRATED-STANDARD"},
    "gg-03": {"return_value": "G3-SENIOR"},
    "gg-04": {"return_value": "G2-STANDARD"},
    "gg-05": {"return_value": None},
    "te-01": {"return_value": "Rohan Mehta, Karan Singh"},
    "te-02": {"return_value": "Ira Kapoor"},
    "te-03": {"return_value": "Ira Kapoor, Priya Nair, Sana Khan, Vikram Rao"},
    "te-04": {"return_value": None},
    "te-05": {"return_value": "Asha Verma"},
    "ar-01": {"out_params": {"p_new_salary": Decimal("90950.54")}, "tables_changed": {"employees", "salary_audit"}},
    "ar-02": {"out_params": {"p_new_salary": Decimal("64000")}, "tables_changed": set()},
    "ar-03": {"out_params": {"p_new_salary": Decimal("-1")}, "tables_changed": set()},
}


def golden_tests_for(object_id: str) -> list[TestCase]:
    return GOLDEN_TESTS.get(object_id, [])

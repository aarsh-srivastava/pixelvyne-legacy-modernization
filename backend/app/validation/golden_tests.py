"""Golden test cases, copied from samples/oracle/EXPECTED.md.

Each TestCase is run on Oracle and on PostgreSQL by the harness; the two results are
compared with each other, not with these notes. ORACLE_EXPECTED is a separate sanity
check: it confirms the Oracle run matches the answer key, i.e. that the harness itself
calls things correctly. If a real Oracle run disagrees with it, Oracle wins: fix
EXPECTED.md and this file.

Input keys are lower-case parameter names, as in the IRObject contract.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.config import settings
from app.contracts import default_schema, make_object_id
from app.contracts.models import SetupCall, TestCase

# Canonical ids (contract 0.2), in the same schema the parser assigns to the
# unqualified sample objects, so the keys always match parser output.
SAMPLE_SCHEMA = default_schema(settings.ORACLE_USER)

CALC_BONUS = make_object_id("procedure", "calc_bonus", schema=SAMPLE_SCHEMA)
GET_EMP_GRADE = make_object_id("function", "get_emp_grade", schema=SAMPLE_SCHEMA)
TOP_EARNERS = make_object_id("function", "top_earners", schema=SAMPLE_SCHEMA)
APPLY_RAISE = make_object_id("procedure", "apply_raise", schema=SAMPLE_SCHEMA)
# Step 7b: the rest of the answer key (EXPECTED.md sections 5 to 8).
MONTHLY_PAYROLL = make_object_id("procedure", "monthly_payroll", schema=SAMPLE_SCHEMA)
NET_PAY = make_object_id("function", "net_pay", schema=SAMPLE_SCHEMA, package="payroll_pkg")
CLOSE_MONTH = make_object_id("procedure", "close_month", schema=SAMPLE_SCHEMA, package="payroll_pkg")
SALARY_AUDIT_TRG = make_object_id("trigger", "emp_salary_audit_trg", schema=SAMPLE_SCHEMA)
ORG_CHART = make_object_id("view", "org_chart", schema=SAMPLE_SCHEMA)

SEPT_15 = dt.datetime(2026, 9, 15)   # DATE '2026-09-15'; Oracle DATE and PostgreSQL timestamp(0)
SEPT_1 = dt.datetime(2026, 9, 1)


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
    MONTHLY_PAYROLL: [
        _t("mp-01", MONTHLY_PAYROLL, "Trap: ADD_MONTHS keeps month-end, so emp 108 is off probation",
           p_run_date=SEPT_15),
    ],
    NET_PAY: [
        _t("pk-01", NET_PAY, "10% tax, rounded to 2 decimals", p_gross=Decimal("10000")),
        _t("pk-02", NET_PAY, "NULL gross is treated as 0", p_gross=None),
        _t("pk-03", NET_PAY, "9583.38 -> 8625.04 (half-up rounding)", p_gross=Decimal("9583.38")),
    ],
    CLOSE_MONTH: [
        TestCase(test_id="pk-04", object_id=CLOSE_MONTH, origin="golden",
                 description="Total of September's runs, after monthly_payroll (inherits the payroll trap)",
                 inputs={"p_pay_month": SEPT_15},
                 setup=[SetupCall(object_id=MONTHLY_PAYROLL, inputs={"p_run_date": SEPT_15})]),
        _t("pk-05", CLOSE_MONTH, "A month with no runs totals 0", p_pay_month=dt.datetime(2026, 10, 1)),
    ],
    SALARY_AUDIT_TRG: [
        TestCase(test_id="tr-01", object_id=SALARY_AUDIT_TRG, origin="golden",
                 description="A raise writes one RAISE audit row",
                 action="UPDATE employees SET base_salary = 70000 WHERE emp_id = 106"),
        TestCase(test_id="tr-02", object_id=SALARY_AUDIT_TRG, origin="golden",
                 description="A cut writes one CUT audit row",
                 action="UPDATE employees SET base_salary = 60000 WHERE emp_id = 106"),
        TestCase(test_id="tr-03", object_id=SALARY_AUDIT_TRG, origin="golden",
                 description="Trap: an unchanged salary writes no row (the WHEN clause)",
                 action="UPDATE employees SET base_salary = base_salary WHERE emp_id = 106"),
    ],
    ORG_CHART: [
        TestCase(test_id="oc-01", object_id=ORG_CHART, origin="golden",
                 description="Trap: CONNECT BY, LEVEL and SYS_CONNECT_BY_PATH as a recursive CTE",
                 order_by=["emp_id"]),
    ],
}

# Columns left out of table comparisons, per EXPECTED.md: sequence values are not
# rolled back in Oracle, and SYSDATE-based dates depend on when the test runs.
MASKED_COLUMNS: dict[str, set[str]] = {
    "salary_audit": {"audit_id", "changed_on"},
    "payroll_runs": {"run_id"},
}

def _payroll(emp_id: int, days: int, gross: str) -> dict:
    """One payroll_runs row of mp-01 (run_id is masked)."""
    return {"emp_id": Decimal(emp_id), "pay_month": SEPT_1, "gross_pay": Decimal(gross),
            "days_worked": Decimal(days), "created_at": SEPT_15}


def _audit(emp_id: int, old: str, new: str, change_type: str) -> dict:
    """One salary_audit row (audit_id and changed_on are masked)."""
    return {"emp_id": Decimal(emp_id), "old_salary": Decimal(old), "new_salary": Decimal(new),
            "change_type": change_type}


def _org(emp_id: int, name: str, manager_id: int | None, depth: int, above: tuple[str, ...]) -> dict:
    """One org_chart row; reporting_line is the path from the CEO, each name after ' > '."""
    return {"emp_id": Decimal(emp_id), "full_name": name,
            "manager_id": None if manager_id is None else Decimal(manager_id), "depth": Decimal(depth),
            "reporting_line": "".join(f" > {n}" for n in (*above, name))}


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
    # Step 7b. "inserted": rows a watched table gains (masked columns left out);
    # "rows": what a view returns, in the test's order_by order.
    "mp-01": {"out_params": {"p_rows_out": Decimal("10")}, "tables_changed": {"payroll_runs"},
              "inserted": {"payroll_runs": [_payroll(*row) for row in (
                  (101, 30, "22500.00"), (102, 30, "12500.00"), (103, 30, "10666.67"),
                  (104, 30, "9583.38"), (105, 30, "9583.38"), (106, 30, "7833.33"),
                  (107, 30, "8500.00"), (108, 30, "7166.67"), (109, 14, "1944.44"),
                  (111, 30, "10916.73"))]}},
    "pk-01": {"return_value": Decimal("9000.00")},
    "pk-02": {"return_value": Decimal("0")},
    "pk-03": {"return_value": Decimal("8625.04")},
    "pk-04": {"out_params": {"p_total": Decimal("91075.14")}, "tables_changed": set()},
    "pk-05": {"out_params": {"p_total": Decimal("0")}, "tables_changed": set()},
    "tr-01": {"tables_changed": {"employees", "salary_audit"},
              "inserted": {"salary_audit": [_audit(106, "64000", "70000", "RAISE")]}},
    "tr-02": {"tables_changed": {"employees", "salary_audit"},
              "inserted": {"salary_audit": [_audit(106, "64000", "60000", "CUT")]}},
    "tr-03": {"tables_changed": set()},
    "oc-01": {"rows": [_org(*row) for row in (
        (101, "Asha Verma", None, 1, ()),
        (102, "Rohan Mehta", 101, 2, ("Asha Verma",)),
        (103, "Priya Nair", 101, 2, ("Asha Verma",)),
        (104, "Karan Singh", 102, 3, ("Asha Verma", "Rohan Mehta")),
        (105, "Neha Gupta", 102, 3, ("Asha Verma", "Rohan Mehta")),
        (106, "Vikram Rao", 103, 3, ("Asha Verma", "Priya Nair")),
        (107, "Sana Khan", 103, 3, ("Asha Verma", "Priya Nair")),
        (108, "Arjun Das", 104, 4, ("Asha Verma", "Rohan Mehta", "Karan Singh")),
        (109, "Meera Iyer", 104, 4, ("Asha Verma", "Rohan Mehta", "Karan Singh")),
        (110, "Dev Malhotra", 101, 2, ("Asha Verma",)),
        (111, "Ira Kapoor", 103, 3, ("Asha Verma", "Priya Nair")))]},
}


def golden_tests_for(object_id: str) -> list[TestCase]:
    return GOLDEN_TESTS.get(object_id, [])

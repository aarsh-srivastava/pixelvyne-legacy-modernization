"""What the step 7c generator may produce: input domains, fixture scenarios, trigger actions.

Everything a generated test can do is written down here, in code, for review:

* ``core_specs()``: the deterministic cases. Boundary values per parameter type, the inputs
  that target each translator rule (R1-R13), fixture scenarios and metamorphic-relation
  members. Every object and every rule an object uses is covered by these, whatever the
  budget, so sampling can never leave one out.
* ``pool_specs()``: ordinary in-contract combinations the seeded sampler draws from to fill
  the budget (generators.py).
* ``TRIGGER_ACTIONS``: the only extra trigger actions (D-7c.4). ``actions.trusted_actions()``
  includes them; a test case from anywhere else still cannot add a statement.
* ``ACCEPTED_DIFFERENCES``: the only cases triage may call *unsupported* without a routing
  reason; each needs a decision reference (empty today).

``in_contract`` says whether the inputs are inside what EXPECTED.md promises. Out-of-contract
cases are still compared against Oracle; their differences are reported separately and are
not fixed without a separate decision (Aarsh, 16 Nov).

Fixture rows use IDs from 900 up, existing departments and managers, and bound values only
(fixtures.py checks them).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.contracts.models import SetupCall
from app.validation.golden_tests import (
    APPLY_RAISE,
    CALC_BONUS,
    CLOSE_MONTH,
    GET_EMP_GRADE,
    MONTHLY_PAYROLL,
    NET_PAY,
    ORG_CHART,
    SALARY_AUDIT_TRG,
    TOP_EARNERS,
)

#: Short names used in generated test IDs: gen-<short>-<category>-<hash>.
SHORT = {
    CALC_BONUS: "cb", GET_EMP_GRADE: "gg", TOP_EARNERS: "te", APPLY_RAISE: "ar",
    MONTHLY_PAYROLL: "mp", NET_PAY: "np", CLOSE_MONTH: "cm", SALARY_AUDIT_TRG: "tr", ORG_CHART: "oc",
}

#: The categories of the proposal (section 5), plus text_domain for values outside a column's set.
CATEGORIES = (
    "null_empty", "month_end", "leap_year", "numeric_boundary", "missing_record", "duplicate_record",
    "rownum_limit", "sequence", "package_state", "trigger_firing", "hierarchy", "text_domain",
)

#: Translator rules each object's conversion relies on; every one is targeted by a core case.
RULES_BY_OBJECT = {
    CALC_BONUS: {"R1", "R2", "R4"},
    GET_EMP_GRADE: {"R2"},
    TOP_EARNERS: {"R3", "R9"},
    APPLY_RAISE: {"R2", "R4"},
    MONTHLY_PAYROLL: {"R5", "R6", "R7", "R8", "R13"},
    NET_PAY: {"R4", "R13"},
    CLOSE_MONTH: {"R6"},
    SALARY_AUDIT_TRG: {"R8"},
    ORG_CHART: set(),          # Gemini route: no deterministic rule
}


@dataclass(frozen=True)
class Spec:
    """One case the generator will turn into a TestCase."""

    object_id: str
    category: str
    rule: str | None = None
    inputs: dict[str, Any] = field(default_factory=dict)
    in_contract: bool = True
    fixtures: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    setup: tuple[SetupCall, ...] = ()
    action: str | None = None
    order_by: tuple[str, ...] = ()
    relation: str | None = None
    note: str = ""


def d(year: int, month: int, day: int, *hms: int) -> dt.datetime:
    return dt.datetime(year, month, day, *hms)


def _emp(emp_id: int, name: str, dept_id: int, manager_id: int | None, hired: dt.datetime,
         salary: str, rating: str | None, status: str = "ACTIVE") -> dict[str, Any]:
    return {"emp_id": emp_id, "full_name": name, "email": None, "dept_id": dept_id,
            "manager_id": manager_id, "hire_date": hired, "base_salary": Decimal(salary),
            "perf_rating": rating, "status": status}


def _employees(*rows: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {"employees": list(rows)}


SEPT_15 = d(2026, 9, 15)
PAYROLL_SEPT = SetupCall(object_id=MONTHLY_PAYROLL, inputs={"p_run_date": SEPT_15})

# --- trigger actions (D-7c.4): written here, never generated ------------------------------------

TRIGGER_ACTIONS: dict[str, tuple[str, str, bool]] = {
    # key: (statement, what it probes, in_contract)
    "multi_row_raise": ("UPDATE employees SET base_salary = base_salary * 1.1 WHERE dept_id = 20",
                        "FOR EACH ROW: one audit row per updated employee", True),
    "other_column": ("UPDATE employees SET email = 'vikram.rao@pv.test' WHERE emp_id = 106",
                     "AFTER UPDATE OF base_salary: another column fires nothing", True),
    "plus_one_cent": ("UPDATE employees SET base_salary = base_salary + 0.01 WHERE emp_id = 106",
                      "smallest raise: RAISE", True),
    "minus_one_cent": ("UPDATE employees SET base_salary = base_salary - 0.01 WHERE emp_id = 106",
                       "smallest cut: CUT", True),
    "same_value_literal": ("UPDATE employees SET base_salary = 64000 WHERE emp_id = 106",
                           "WHEN clause with the same value written as a literal: no row", True),
    "half_cent_rounding": ("UPDATE employees SET base_salary = base_salary * 1.005 WHERE emp_id = 104",
                           "NUMBER(10,2) rounds 85425.5025 on store; audit row has the stored value", True),
    "set_null": ("UPDATE employees SET base_salary = NULL WHERE emp_id = 106",
                 "NOT NULL column: the update fails, nothing is audited", False),
}

#: Cases triage may call *unsupported* without a routing reason: case id -> (reason, decision).
#: Empty: no accepted difference is known. Adding one needs a decision reference (Aarsh, 16 Nov).
ACCEPTED_DIFFERENCES: dict[str, tuple[str, str]] = {}

#: Generated cases that do not pass against Oracle's recorded result, and why: case id ->
#: (triage class, reference). Filled from the live Oracle run's triage, never to hide a
#: failure: the offline replay checks each entry strictly (a listed case that starts passing
#: fails the suite, so the entry must be removed), and a translation defect needs a
#: DEF-7c-<n> reference in the 7c report. From the Oracle 23 recording of 16 Nov 2026.
KNOWN_NON_PASSES: dict[str, tuple[str, str]] = {
    # calc_bonus(102, 'AB'): ORA-06502 at CALC_BONUS line 15 vs 22001 at the same assignment (H2).
    "gen-cb-text_domain-d9209f20": ("error_map_gap", "candidate equivalence value_error/value_too_long; D-7c.5"),
    # (gen-ar-null_empty-15a1ddde and gen-tr-trigger_firing-3f4ac15c, ORA-01407 vs 23502, pass since
    # ORA-01407 was mapped to not_null_violation in errors.py.)
    # org_chart with a name containing ' > ': Oracle raises ORA-30004 (SYS_CONNECT_BY_PATH separator in a
    # value); the recorded Gemini view returns rows. Gemini route; reported, not fixed in 7c.
    "gen-oc-hierarchy-cef703cf": ("translation_defect", "DEF-7c-1: org_chart separator in a name (ORA-30004)"),
}


def core_specs() -> list[Spec]:
    """The deterministic cases: always generated, whatever the budget or seed."""
    specs: list[Spec] = []
    add = specs.append

    # calc_bonus: R1 '' is NULL, R2 STRICT, R4 exact decimals, override domain.
    for emp in (101, 104, 111):  # relation: calc_bonus(x, '') = calc_bonus(x, NULL); not golden IDs
        add(Spec(CALC_BONUS, "null_empty", "R1", {"p_emp_id": emp, "p_override": ""}, relation=f"cb_empty_{emp}"))
        add(Spec(CALC_BONUS, "null_empty", "R1", {"p_emp_id": emp, "p_override": None}, relation=f"cb_empty_{emp}"))
    add(Spec(CALC_BONUS, "null_empty", "R1", {"p_emp_id": 103, "p_override": ""}))
    add(Spec(CALC_BONUS, "text_domain", None, {"p_emp_id": 102, "p_override": " "}, in_contract=False,
             note="a blank is not NULL in Oracle: ELSE branch"))
    add(Spec(CALC_BONUS, "text_domain", None, {"p_emp_id": 106, "p_override": "a"}, in_contract=False,
             note="ratings are case-sensitive"))
    add(Spec(CALC_BONUS, "text_domain", None, {"p_emp_id": 102, "p_override": "AB"}, in_contract=False,
             note="longer than perf_rating%TYPE: an error inside the object (H2)"))
    add(Spec(CALC_BONUS, "missing_record", "R2", {"p_emp_id": None, "p_override": None}))
    add(Spec(CALC_BONUS, "missing_record", "R2", {"p_emp_id": 999, "p_override": "A"}))
    add(Spec(CALC_BONUS, "missing_record", "R2", {"p_emp_id": Decimal("102.5"), "p_override": None},
             in_contract=False, note="a fractional ID matches no row"))
    add(Spec(CALC_BONUS, "numeric_boundary", "R4", {"p_emp_id": 104, "p_override": "C"},
             note="85000.50 x 5% = 4250.025 rounds half up on the override path"))
    add(Spec(CALC_BONUS, "numeric_boundary", "R4", {"p_emp_id": 900, "p_override": None},
             fixtures=_employees(_emp(900, "Fixture Rounding", 20, 104, d(2024, 1, 2), "10000.05", "C")),
             note="10000.05 x 5% = 500.0025 rounds down"))
    add(Spec(CALC_BONUS, "numeric_boundary", "R4", {"p_emp_id": 901, "p_override": None},
             fixtures=_employees(_emp(901, "Fixture Max Salary", 10, 101, d(2024, 1, 2), "99999999.99", "A")),
             note="NUMBER(10,2) maximum salary: 19999999.998 rounds to 20000000.00"))

    # get_emp_grade: band boundary, unexpected stored ratings, missing employee.
    add(Spec(GET_EMP_GRADE, "missing_record", "R2", {"p_emp_id": None}))
    add(Spec(GET_EMP_GRADE, "missing_record", "R2", {"p_emp_id": Decimal("101.5")}, in_contract=False))
    add(Spec(GET_EMP_GRADE, "numeric_boundary", None, {"p_emp_id": 902},
             fixtures=_employees(_emp(902, "Fixture Exactly 100k", 20, 102, d(2024, 1, 2), "100000.00", "B")),
             note="salary exactly 100000: SENIOR"))
    add(Spec(GET_EMP_GRADE, "numeric_boundary", None, {"p_emp_id": 903},
             fixtures=_employees(_emp(903, "Fixture Just Below", 20, 102, d(2024, 1, 2), "99999.99", "C")),
             note="99999.99: STANDARD"))
    add(Spec(GET_EMP_GRADE, "text_domain", None, {"p_emp_id": 904}, in_contract=False,
             fixtures=_employees(_emp(904, "Fixture Rating D", 30, 103, d(2024, 1, 2), "70000", "D")),
             note="a stored rating outside A/B/C/NULL falls to G4"))

    # top_earners: R9 limit semantics (decision D3), R3 concatenation, ties, missing department.
    for n in (None, -1, 0, Decimal("0.5"), Decimal("1.5"), Decimal("2.5"), 1000000):
        add(Spec(TOP_EARNERS, "rownum_limit", "R9", {"p_dept_id": 20, "p_n": n},
                 note="ROWNUM <= n with NULL, non-positive, fractional or huge n (D3)"))
    add(Spec(TOP_EARNERS, "rownum_limit", "R9", {"p_dept_id": 30, "p_n": Decimal("2.5")}))
    add(Spec(TOP_EARNERS, "missing_record", "R3", {"p_dept_id": None, "p_n": 3}))
    add(Spec(TOP_EARNERS, "missing_record", "R3", {"p_dept_id": 999, "p_n": 3}))
    ties = _employees(_emp(905, "Tia Bose", 30, 103, d(2024, 5, 6), "98000", "B"),
                      _emp(906, "Om Rana", 30, 103, d(2024, 5, 7), "98000", "B"))
    for n in (2, 3, 4):
        add(Spec(TOP_EARNERS, "duplicate_record", "R3", {"p_dept_id": 30, "p_n": n}, fixtures=ties,
                 note="three-way tie at 98000, broken by emp_id"))
    for n in (3, 4, 5, 6):  # relation: top_earners(20, n) is a prefix of top_earners(20, n + 1)
        add(Spec(TOP_EARNERS, "rownum_limit", "R9", {"p_dept_id": 20, "p_n": n}, relation="te_prefix_20"))
    for n in (2, 3, 4, 5):
        add(Spec(TOP_EARNERS, "rownum_limit", "R9", {"p_dept_id": 30, "p_n": n}, relation="te_prefix_30"))

    # apply_raise: R4 exact decimal division, R2 STRICT, the trigger, failure partway.
    for pct in (-100, -1, 1, 100):
        add(Spec(APPLY_RAISE, "numeric_boundary", "R4", {"p_emp_id": 104, "p_pct": pct}))
    add(Spec(APPLY_RAISE, "numeric_boundary", "R4", {"p_emp_id": 104, "p_pct": 1000000},
             note="NUMBER(10,2) overflow on UPDATE: raises after SELECT FOR UPDATE, nothing left behind"))
    add(Spec(APPLY_RAISE, "numeric_boundary", "R4", {"p_emp_id": 104, "p_pct": Decimal("7.5")},
             in_contract=False, note="H1: fractional value on an INTEGER parameter"))
    add(Spec(APPLY_RAISE, "null_empty", None, {"p_emp_id": 104, "p_pct": None}, in_contract=False,
             note="NULL percentage: UPDATE to NULL on a NOT NULL column"))
    add(Spec(APPLY_RAISE, "missing_record", "R2", {"p_emp_id": None, "p_pct": 5}))
    for emp in (101, 104, 111):  # relation: a 0% raise writes no audit row (golden ar-02 is 106)
        add(Spec(APPLY_RAISE, "trigger_firing", "R4", {"p_emp_id": emp, "p_pct": 0}, relation="ar_zero"))

    # monthly_payroll: every month length, leap years, month ends, time of day, R7 ADD_MONTHS.
    for run, cat, rule in (
        (d(2026, 2, 15), "month_end", "R7"),        # 28-day month; ADD_MONTHS(28 Feb, -6) = 31 Aug
        (d(2028, 2, 15), "leap_year", "R7"),        # 29 Feb 2028
        (d(2026, 4, 15), "month_end", "R6"),        # 30-day month
        (d(2026, 8, 15), "month_end", "R7"),        # ADD_MONTHS(31 Aug, -6) = 28 Feb 2026
        (d(2026, 9, 1), "month_end", "R6"),         # run on the 1st
        (d(2026, 9, 30), "month_end", "R5"),        # run on the last day
        (d(2026, 9, 30, 23, 59, 59), "month_end", "R6"),  # a time of day: TRUNC keeps the day
        (d(2026, 3, 31), "month_end", "R5"),        # Arjun's hire day: 1 day worked
        (d(2024, 2, 29), "leap_year", "R5"),        # Sana's hire day, a leap day
        (d(2014, 6, 15), "missing_record", "R8"),   # before any hire: 0 rows
    ):
        add(Spec(MONTHLY_PAYROLL, cat, rule, {"p_run_date": run}))
    add(Spec(MONTHLY_PAYROLL, "null_empty", "R6", {"p_run_date": None}, in_contract=False,
             note="explicit NULL run date (the SYSDATE default is not used)"))
    add(Spec(MONTHLY_PAYROLL, "month_end", "R7", {"p_run_date": d(2026, 2, 15)},
             fixtures=_employees(_emp(907, "Fixture Hired 31 Aug", 20, 104, d(2025, 8, 31), "60000", None)),
             note="ADD_MONTHS(28 Feb 2026, -6) = 31 Aug 2025: hired that day, so off probation"))
    add(Spec(MONTHLY_PAYROLL, "leap_year", "R7", {"p_run_date": d(2028, 2, 15)},
             fixtures=_employees(_emp(908, "Fixture Hired 31 Aug 27", 20, 104, d(2027, 8, 31), "60000", None)),
             note="ADD_MONTHS(29 Feb 2028, -6) = 31 Aug 2027"))
    add(Spec(MONTHLY_PAYROLL, "leap_year", "R7", {"p_run_date": d(2028, 8, 15)},
             fixtures=_employees(_emp(909, "Fixture Leap Hire", 30, 103, d(2028, 2, 29), "48000", None)),
             note="ADD_MONTHS(31 Aug 2028, -6) = 29 Feb 2028: hired that day"))
    add(Spec(MONTHLY_PAYROLL, "sequence", "R8", {"p_run_date": SEPT_15}, setup=(PAYROLL_SEPT,),
             note="a second run in the same month: 10 more rows, new sequence values (masked)"))
    add(Spec(MONTHLY_PAYROLL, "package_state", "R13", {"p_run_date": d(2026, 10, 15)},
             note="payroll_pkg.c_allowance inlined: same allowance in another month"))

    # payroll_pkg.net_pay: half-cent rounding after tax, sign, size, fractional input.
    for gross in ("0.25", "-0.25", "2.25"):
        add(Spec(NET_PAY, "numeric_boundary", "R4", {"p_gross": Decimal(gross)},
                 note="gross x 0.9 ends in a half cent: ROUND half away from zero"))
    for gross in ("10.05", "123.45"):  # relation: net_pay(-x) = -net_pay(x)
        add(Spec(NET_PAY, "numeric_boundary", "R4", {"p_gross": Decimal(gross)}, relation=f"np_sym_{gross}"))
        add(Spec(NET_PAY, "numeric_boundary", "R4", {"p_gross": -Decimal(gross)}, relation=f"np_sym_{gross}"))
    add(Spec(NET_PAY, "numeric_boundary", "R4", {"p_gross": Decimal(0)}))
    add(Spec(NET_PAY, "numeric_boundary", "R4", {"p_gross": Decimal("99999999999.99")}))
    add(Spec(NET_PAY, "numeric_boundary", "R4", {"p_gross": Decimal("12345.678")}, in_contract=False,
             note="a fractional cent in the input"))
    add(Spec(NET_PAY, "package_state", "R13", {"p_gross": Decimal("20000")},
             note="g_tax_rate inlined: still 10%"))

    # payroll_pkg.close_month: TRUNC(d, 'MM'), duplicates, empty months.
    for day in (d(2026, 9, 1), d(2026, 9, 17), d(2026, 9, 30, 23, 59, 59)):  # relation: same month, same total
        add(Spec(CLOSE_MONTH, "month_end", "R6", {"p_pay_month": day}, setup=(PAYROLL_SEPT,),
                 relation="cm_same_month"))
    add(Spec(CLOSE_MONTH, "duplicate_record", "R6", {"p_pay_month": d(2026, 9, 1)},
             setup=(PAYROLL_SEPT, PAYROLL_SEPT), relation="cm_double",
             note="two runs in one month: every row counted"))
    add(Spec(CLOSE_MONTH, "leap_year", "R6", {"p_pay_month": d(2028, 2, 29)},
             setup=(SetupCall(object_id=MONTHLY_PAYROLL, inputs={"p_run_date": d(2028, 2, 15)}),)))
    add(Spec(CLOSE_MONTH, "missing_record", "R6", {"p_pay_month": d(2027, 1, 1)}))
    add(Spec(CLOSE_MONTH, "null_empty", "R6", {"p_pay_month": None}, in_contract=False))
    add(Spec(CLOSE_MONTH, "package_state", "R6", {"p_pay_month": SEPT_15},
             setup=(PAYROLL_SEPT, SetupCall(object_id=MONTHLY_PAYROLL, inputs={"p_run_date": d(2026, 10, 15)})),
             note="runs in two months: only September's rows count"))

    # emp_salary_audit_trg: the registered actions.
    for key, (statement, note, in_contract) in TRIGGER_ACTIONS.items():
        add(Spec(SALARY_AUDIT_TRG, "trigger_firing", "R8", action=statement, in_contract=in_contract,
                 note=f"{key}: {note}"))

    # org_chart: hierarchy shapes (Gemini route).
    for key, rows, note in (
        ("second_root", (_emp(910, "Root Two", 40, None, d(2024, 1, 2), "90000", None),
                         _emp(911, "Under Root Two", 40, 910, d(2024, 1, 3), "50000", None)),
         "a second employee with no manager starts a second tree"),
        ("deep_chain", (_emp(912, "Chain Five", 20, 108, d(2026, 4, 1), "40000", None),
                        _emp(913, "Chain Six", 20, 912, d(2026, 4, 2), "40000", None)),
         "depth 5 and 6 under Arjun"),
        ("separator_in_name", (_emp(914, "A > B", 10, 101, d(2024, 1, 2), "50000", None),),
         "a name containing the path separator"),
        ("inactive_manager", (_emp(915, "Reports To Dev", 10, 110, d(2024, 1, 2), "50000", None),),
         "an active report under the inactive manager 110"),
    ):
        add(Spec(ORG_CHART, "hierarchy", None, fixtures=_employees(*rows), order_by=("emp_id",),
                 note=f"{key}: {note}"))
    return specs


def pool_specs() -> list[Spec]:
    """Ordinary in-contract combinations the seeded sampler draws from (seed data only)."""
    pool: list[Spec] = []
    seed_ids = range(101, 112)
    for emp in seed_ids:
        for override in (None, "A", "B", "C"):
            pool.append(Spec(CALC_BONUS, "numeric_boundary", "R4", {"p_emp_id": emp, "p_override": override}))
        pool.append(Spec(GET_EMP_GRADE, "numeric_boundary", None, {"p_emp_id": emp}))
        for pct in (2, 3, 5, 10, 15, 25, 50):
            pool.append(Spec(APPLY_RAISE, "numeric_boundary", "R4", {"p_emp_id": emp, "p_pct": pct}))
    for dept in (10, 20, 30, 40):
        for n in range(1, 7):
            pool.append(Spec(TOP_EARNERS, "rownum_limit", "R9", {"p_dept_id": dept, "p_n": n}))
    for year in (2025, 2026, 2027):
        for month in range(1, 13):
            pool.append(Spec(MONTHLY_PAYROLL, "month_end", "R7", {"p_run_date": d(year, month, 15)}))
    for cents in (5, 15, 45, 85, 105, 115, 145, 165, 185, 205, 1005, 99995):
        pool.append(Spec(NET_PAY, "numeric_boundary", "R4", {"p_gross": Decimal(cents) / 100}))
    for month in range(1, 13):
        pool.append(Spec(CLOSE_MONTH, "missing_record", "R6", {"p_pay_month": d(2027, month, 1)}))
    return pool

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
}

# What Oracle itself must return, per EXPECTED.md. "error" = the Oracle error code.
ORACLE_EXPECTED: dict[str, dict] = {
    "cb-01": {"out_params": {"p_bonus": Decimal("24000.00")}},
    "cb-02": {"out_params": {"p_bonus": Decimal("24000.00")}},
    "cb-03": {"out_params": {"p_bonus": Decimal("19600.00")}},
    "cb-04": {"out_params": {"p_bonus": Decimal("0")}},
    "cb-05": {"out_params": {"p_bonus": Decimal("4250.03")}},
    "cb-06": {"error": "ORA-01403"},
}


def golden_tests_for(object_id: str) -> list[TestCase]:
    return GOLDEN_TESTS.get(object_id, [])

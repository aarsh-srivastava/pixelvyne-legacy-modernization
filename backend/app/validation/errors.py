"""Map engine error codes to shared categories.

Oracle and PostgreSQL report the same failure with different codes (no rows found is
ORA-01403 in Oracle, P0002 in PostgreSQL). The comparator compares these categories,
so two errors only count as "the same behaviour" when they mean the same thing.

Some errors mean the harness could not judge the test at all: the object is missing
or invalid, or the connection broke. Those are harness errors (TestStatus.ERROR),
never behavioural failures.

Extend both tables when a new golden test exercises a new error.
"""

from __future__ import annotations

from app.contracts.models import ExecutionError

HARNESS = "harness"

ORACLE_CATEGORIES = {
    "ORA-01403": "no_data_found",
    "ORA-01422": "too_many_rows",
    "ORA-00001": "unique_violation",
    "ORA-02291": "foreign_key_violation",
    "ORA-02292": "foreign_key_violation",
    "ORA-01400": "not_null_violation",
    "ORA-02290": "check_violation",
    "ORA-01476": "division_by_zero",
    "ORA-12899": "value_too_long",
    "ORA-01438": "numeric_overflow",
    "ORA-01722": "invalid_number",
    "ORA-06502": "value_error",
    # The harness could not run the test: object missing or invalid, connection lost.
    "ORA-00942": HARNESS,
    "ORA-04063": HARNESS,
    "ORA-04068": HARNESS,
    "ORA-06508": HARNESS,
    "ORA-06550": HARNESS,
    "ORA-03113": HARNESS,
    "ORA-03114": HARNESS,
    "ORA-03135": HARNESS,
    "PLS-00201": HARNESS,
}

POSTGRES_CATEGORIES = {
    "P0002": "no_data_found",
    "P0003": "too_many_rows",
    "23505": "unique_violation",
    "23503": "foreign_key_violation",
    "23502": "not_null_violation",
    "23514": "check_violation",
    "22012": "division_by_zero",
    "22001": "value_too_long",
    "22003": "numeric_overflow",
    "22P02": "invalid_number",
    "P0001": "user_raised",
    "42883": HARNESS,   # undefined function / procedure
    "42P01": HARNESS,   # undefined table
    "42703": HARNESS,   # undefined column
    "3F000": HARNESS,   # undefined schema
    "57P01": HARNESS,   # server shut down
}


def categorize(engine: str, error: ExecutionError | None) -> str | None:
    if error is None:
        return None
    code = (error.code or "").upper()
    if code == "" or code == "HARNESS" or code.startswith("DPY-"):
        return HARNESS
    if engine == "oracle":
        if code.startswith("ORA-20") and code[4:].isdigit() and 20000 <= int(code[4:]) <= 20999:
            return "user_raised"          # RAISE_APPLICATION_ERROR
        return ORACLE_CATEGORIES.get(code, f"other:{code}")
    if code.startswith("08"):
        return HARNESS                    # connection exception class
    return POSTGRES_CATEGORIES.get(code, f"other:{code}")


def is_harness_error(engine: str, error: ExecutionError | None) -> bool:
    return categorize(engine, error) == HARNESS

"""Fixture rows: test data a case adds before it runs (step 7c, D-7c.3).

``TestCase.fixture_rows`` maps a table to the rows to insert, e.g. a second employee with
the same salary, or a hire on 29 Feb 2028. The rows are data, never SQL: both runners build
the INSERT themselves from checked names and bind every value as a parameter. They insert
inside the test's own transaction, before setup and before the snapshot, so the rows are
never part of what is compared, and they are rolled back with everything else.

Every check below runs before any SQL:

1. **Trusted origin.** Only ``golden`` and ``rule`` cases may carry fixture rows.
2. **Allow-listed names.** The table and every column must be in ``ALLOWED_COLUMNS``: the
   four sample tables and their columns, written here in code.
3. **Plain values.** ``None``, ``str``, ``int``, ``Decimal`` or ``datetime`` only; never
   ``float`` (it binds differently on the two engines) or a container.

The PostgreSQL runner also requires the connection to be ``pv_runner``, as for actions.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.contracts.models import TestCase
from app.validation.table_diff import check_identifier

FIXTURE_ORIGINS = frozenset({"golden", "rule"})

#: The only tables and columns fixture rows may name (samples/oracle/01_schema.sql).
ALLOWED_COLUMNS: dict[str, frozenset[str]] = {
    "departments": frozenset({"dept_id", "dept_name"}),
    "employees": frozenset({"emp_id", "full_name", "email", "dept_id", "manager_id", "hire_date",
                            "base_salary", "perf_rating", "status"}),
    "payroll_runs": frozenset({"run_id", "emp_id", "pay_month", "gross_pay", "days_worked", "created_at"}),
    "salary_audit": frozenset({"audit_id", "emp_id", "old_salary", "new_salary", "changed_on", "change_type"}),
}

_PLAIN = (str, int, Decimal, dt.datetime)


class UnsafeFixture(ValueError):
    """The case's fixture rows are not allowed; the test cannot be run."""


def checked_fixtures(case: TestCase) -> list[tuple[str, list[str], list]]:
    """(table, columns, values) per row, in the order given, after every check above."""
    if not case.fixture_rows:
        return []
    if case.origin not in FIXTURE_ORIGINS:
        raise UnsafeFixture(f"{case.test_id}: fixture rows are only allowed for golden or rule tests, "
                            f"not {case.origin!r}")
    inserts: list[tuple[str, list[str], list]] = []
    for table, rows in case.fixture_rows.items():
        name = table.lower()
        if name not in ALLOWED_COLUMNS:
            raise UnsafeFixture(f"{case.test_id}: table {table!r} is not allowed for fixture rows")
        check_identifier(name)
        for row in rows:
            if not row:
                raise UnsafeFixture(f"{case.test_id}: an empty fixture row for {name}")
            columns = [c.lower() for c in row]
            unknown = sorted(set(columns) - ALLOWED_COLUMNS[name])
            if unknown:
                raise UnsafeFixture(f"{case.test_id}: column(s) {unknown} of {name} are not allowed")
            for column in columns:
                check_identifier(column)
            values = list(row.values())
            for column, value in zip(columns, values, strict=True):
                if value is not None and (isinstance(value, bool) or not isinstance(value, _PLAIN)):
                    raise UnsafeFixture(f"{case.test_id}: {name}.{column} has a {type(value).__name__}; "
                                        "only None, str, int, Decimal or datetime are allowed")
            inserts.append((name, columns, values))
    return inserts


def oracle_insert(table: str, columns: list[str]) -> str:
    """The INSERT for one checked fixture row, with Oracle positional binds."""
    binds = ", ".join(f":{i}" for i in range(1, len(columns) + 1))
    return f"INSERT INTO {table.upper()} ({', '.join(c.upper() for c in columns)}) VALUES ({binds})"

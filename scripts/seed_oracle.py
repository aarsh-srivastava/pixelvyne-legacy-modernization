"""Load (or reset) the sample legacy system into the Oracle container.

Usage, from the repo root on your laptop:
    python scripts/seed_oracle.py            # drop everything we own, then load 01..07
    python scripts/seed_oracle.py --drop     # only drop
    python scripts/seed_oracle.py --dry-run  # parse the files and print statements, no DB

Needs: pip install oracledb   (thin mode, no Oracle Instant Client required)

Connection comes from env vars, defaulting to the docker-compose values as seen
from the host machine:
    ORACLE_USER=legacy  ORACLE_PASSWORD=legacy  ORACLE_DSN=localhost:1521/FREEPDB1

File conventions (keep them when adding objects):
  * Plain SQL statements end with ';' at the end of a line.
  * PL/SQL units (PACKAGE, PACKAGE BODY, PROCEDURE, FUNCTION, TRIGGER, TYPE) end with
    a line containing only '/'.
  * '--' comment lines between statements are ignored.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples" / "oracle"
LOAD_ORDER = [
    "01_schema.sql",
    "02_seed_data.sql",
    "03_payroll_pkg.sql",
    "04_functions.sql",
    "05_procedures.sql",
    "06_triggers.sql",
    "07_views.sql",
]

# Everything the sample system creates, in a safe drop order.
DROPS = [
    "DROP VIEW org_chart",
    "DROP TRIGGER emp_salary_audit_trg",
    "DROP PROCEDURE monthly_payroll",
    "DROP PROCEDURE apply_raise",
    "DROP PROCEDURE calc_bonus",
    "DROP FUNCTION top_earners",
    "DROP FUNCTION get_emp_grade",
    "DROP PACKAGE payroll_pkg",
    "DROP TABLE salary_audit CASCADE CONSTRAINTS PURGE",
    "DROP TABLE payroll_runs CASCADE CONSTRAINTS PURGE",
    "DROP TABLE employees CASCADE CONSTRAINTS PURGE",
    "DROP TABLE departments CASCADE CONSTRAINTS PURGE",
    "DROP SEQUENCE payroll_run_seq",
    "DROP SEQUENCE salary_audit_seq",
]

PLSQL_START = re.compile(
    r"^\s*CREATE\s+(OR\s+REPLACE\s+)?"
    r"(EDITIONABLE\s+|NONEDITIONABLE\s+)?"
    r"(PACKAGE(\s+BODY)?|PROCEDURE|FUNCTION|TRIGGER|TYPE(\s+BODY)?)\b",
    re.IGNORECASE,
)


def split_statements(text: str) -> list[tuple[str, bool]]:
    """Split a script into (statement, is_plsql) pairs, without terminators.

    Plain SQL loses its trailing ';'. PL/SQL keeps the ';' after END (Oracle needs it)
    and loses the '/' line.
    """
    statements: list[tuple[str, bool]] = []
    buf: list[str] = []
    in_plsql = False

    for line in text.splitlines():
        stripped = line.strip()

        if not buf:
            if not stripped or stripped.startswith("--"):
                continue
            in_plsql = bool(PLSQL_START.match(line))

        if in_plsql:
            if stripped == "/":
                statements.append(("\n".join(buf).strip(), True))
                buf = []
            else:
                buf.append(line)
            continue

        code = _code_part(line)  # inline '-- comment' removed, string literals kept
        buf.append(code)
        if code.rstrip().endswith(";"):
            stmt = "\n".join(part for part in buf if part.strip()).strip()
            statements.append((stmt[:-1].rstrip(), False))
            buf = []

    if any(part.strip() for part in buf):
        raise ValueError(
            "Unterminated statement at end of file (missing ';' or '/'):\n" + "\n".join(buf)
        )
    return statements


def _code_part(line: str) -> str:
    """Drop a trailing '-- comment' that is not inside a string literal."""
    in_str = False
    for i, ch in enumerate(line):
        if ch == "'":
            in_str = not in_str
        elif not in_str and line.startswith("--", i):
            return line[:i].rstrip()
    return line


def load_all() -> list[tuple[str, str, bool]]:
    """Return (file name, statement, is_plsql) for every statement, in load order."""
    out = []
    for name in LOAD_ORDER:
        text = (SAMPLES / name).read_text(encoding="utf-8")
        for stmt, is_plsql in split_statements(text):
            out.append((name, stmt, is_plsql))
    return out


def first_line(stmt: str) -> str:
    return stmt.splitlines()[0][:90]


def connect():
    import oracledb  # imported here so --dry-run works without the driver

    return oracledb.connect(
        user=os.getenv("ORACLE_USER", "legacy"),
        password=os.getenv("ORACLE_PASSWORD", "legacy"),
        dsn=os.getenv("ORACLE_DSN", "localhost:1521/FREEPDB1"),
    )


def drop_all(cur) -> None:
    import oracledb

    for stmt in DROPS:
        try:
            cur.execute(stmt)
            print(f"  dropped   {stmt}")
        except oracledb.DatabaseError:
            pass  # object did not exist; fine on a fresh database


def seed(cur, conn) -> int:
    errors = 0
    for name, stmt, is_plsql in load_all():
        if stmt.upper() == "COMMIT":
            conn.commit()
            continue
        cur.execute(stmt)
        # python-oracledb sets cursor.warning when a PL/SQL unit compiles with errors.
        if is_plsql and cur.warning is not None:
            errors += 1
            print(f"  WARNING   {name}: {first_line(stmt)}\n            {cur.warning}")
        else:
            print(f"  ok        {name}: {first_line(stmt)}")
    conn.commit()

    cur.execute(
        "SELECT name, type, line, position, text FROM user_errors ORDER BY name, sequence"
    )
    rows = cur.fetchall()
    for n, t, line, pos, text in rows:
        print(f"  COMPILE ERROR {t} {n} line {line}:{pos}  {text.strip()}")
    return errors + len(rows)


def summary(cur) -> None:
    cur.execute(
        "SELECT object_type, object_name, status FROM user_objects "
        "WHERE object_type IN ('TABLE','VIEW','SEQUENCE','PACKAGE','PACKAGE BODY',"
        "'PROCEDURE','FUNCTION','TRIGGER') ORDER BY object_type, object_name"
    )
    print("\nObjects in schema:")
    for otype, oname, status in cur.fetchall():
        print(f"  {otype:<13} {oname:<24} {status}")
    cur.execute("SELECT COUNT(*) FROM employees")
    print(f"\nemployees rows: {cur.fetchone()[0]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--drop", action="store_true", help="only drop the sample objects")
    parser.add_argument("--dry-run", action="store_true", help="parse files, no database")
    args = parser.parse_args()

    if args.dry_run:
        for name, stmt, is_plsql in load_all():
            kind = "PL/SQL" if is_plsql else "SQL   "
            print(f"{kind} {name}: {first_line(stmt)}")
        return 0

    with connect() as conn:
        cur = conn.cursor()
        print("Dropping existing sample objects...")
        drop_all(cur)
        if args.drop:
            return 0
        print("Loading sample system...")
        problems = seed(cur, conn)
        summary(cur)
        if problems:
            print(f"\n{problems} compile problem(s). Fix them before running tests.")
            return 1
        print("\nSample legacy system loaded cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

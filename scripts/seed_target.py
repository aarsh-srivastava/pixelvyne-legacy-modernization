"""Load (or reset) the hand-written PostgreSQL stand-in into the target sandbox.

Usage, from the repo root on your laptop:
    python scripts/seed_target.py                  # schema, data and the careless stand-ins
    python scripts/seed_target.py --schema-only    # schema and data only (step 7b, real converter)

Wipes the target sandbox's public schema (and the payroll_pkg schema), then runs
samples/postgres/*.sql in name order; with --schema-only only 01_schema.sql and
02_seed_data.sql, so the real converter deploys every routine, trigger and view itself.
Only ever points at target-db (port 5433), never at app-db.

Connection defaults to the docker-compose value as seen from the host; override with
    TARGET_DATABASE_URL_HOST=postgresql://sandbox:sandbox@localhost:5433/target
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples" / "postgres"
TARGET_URL = os.getenv(
    "TARGET_DATABASE_URL_HOST", "postgresql://sandbox:sandbox@localhost:5433/target"
)


def main() -> int:
    import psycopg

    schema_only = "--schema-only" in sys.argv[1:]
    files = sorted(SAMPLES.glob("*.sql"))
    if schema_only:
        files = [f for f in files if f.name in ("01_schema.sql", "02_seed_data.sql")]
    if not files:
        print(f"No .sql files in {SAMPLES}")
        return 1

    with psycopg.connect(TARGET_URL) as conn:
        db = conn.execute("SELECT current_database()").fetchone()[0]
        if db != "target":
            print(f"Refusing to wipe database '{db}': expected 'target'.")
            return 1
        conn.execute("DROP SCHEMA IF EXISTS payroll_pkg CASCADE")  # a package schema the real converter made
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        for path in files:
            conn.execute(path.read_text(encoding="utf-8"))
            print(f"   ok  {path.name}")
        rows = conn.execute("SELECT count(*) FROM employees").fetchone()[0]
        procs = conn.execute(
            "SELECT string_agg(proname, ', ' ORDER BY proname) FROM pg_proc "
            "WHERE pronamespace = 'public'::regnamespace"
        ).fetchone()[0]

    print(f"\nemployees rows: {rows}")
    print(f"routines: {procs}")
    print("\nTarget sandbox loaded cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

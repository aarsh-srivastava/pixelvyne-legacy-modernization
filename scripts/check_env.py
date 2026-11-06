"""Day-1 check: can we talk to every database the validation harness needs?

Run from the repo root on your laptop after `docker compose up -d`:
    pip install oracledb "psycopg[binary]" redis
    python scripts/check_env.py

Checks, in order:
  1. Oracle: connect, create a procedure with an OUT parameter, call it, read the value.
  2. PostgreSQL target sandbox: connect, run a function, read the value.
  3. PostgreSQL platform DB (app-db): connect.
  4. Redis: PING.
  5. Shows the empty-string trap live: the same SQL gives different answers.

Defaults are the docker-compose values as seen from the host (localhost + mapped
ports). Override any of them with env vars.
"""

from __future__ import annotations

import os
import sys
import traceback

ORACLE_USER = os.getenv("ORACLE_USER", "legacy")
ORACLE_PASSWORD = os.getenv("ORACLE_PASSWORD", "legacy")
ORACLE_DSN = os.getenv("ORACLE_DSN", "localhost:1521/FREEPDB1")
TARGET_URL = os.getenv("TARGET_DATABASE_URL_HOST", "postgresql://sandbox:sandbox@localhost:5433/target")
APP_URL = os.getenv("APP_DATABASE_URL_HOST", "postgresql://pixelvyne:pixelvyne@localhost:5432/pixelvyne")
REDIS_URL = os.getenv("REDIS_URL_HOST", "redis://localhost:6379/0")

EMPTY_STRING_SQL = "SELECT CASE WHEN '' IS NULL THEN 'NULL' ELSE 'NOT NULL' END"

results: dict[str, bool] = {}


def check(name):
    def wrap(fn):
        def run():
            print(f"\n== {name}")
            try:
                fn()
                results[name] = True
                print("   PASS")
            except Exception as exc:  # noqa: BLE001 - we want every failure reported
                results[name] = False
                print(f"   FAIL: {exc.__class__.__name__}: {exc}")
                if os.getenv("VERBOSE"):
                    traceback.print_exc()
        return run
    return wrap


@check("Oracle: call a procedure and read its OUT parameter")
def oracle_out_param():
    import oracledb

    with oracledb.connect(user=ORACLE_USER, password=ORACLE_PASSWORD, dsn=ORACLE_DSN) as conn:
        cur = conn.cursor()
        print(f"   server: Oracle {conn.version}")  # v$version needs extra grants
        cur.execute(
            """
            CREATE OR REPLACE PROCEDURE pv_env_check (p_in IN NUMBER, p_out OUT NUMBER) AS
            BEGIN
              p_out := p_in * 2;
            END;
            """
        )
        if cur.warning:
            raise RuntimeError(f"procedure compiled with errors: {cur.warning}")
        out = cur.var(oracledb.DB_TYPE_NUMBER)
        cur.callproc("pv_env_check", [21, out])
        value = out.getvalue()
        print(f"   pv_env_check(21) -> OUT p_out = {value}")
        cur.execute("DROP PROCEDURE pv_env_check")
        if value != 42:
            raise AssertionError(f"expected 42, got {value}")


@check("PostgreSQL target sandbox: run a function and read the result")
def postgres_target():
    import psycopg

    with psycopg.connect(TARGET_URL) as conn:
        server = conn.execute("SHOW server_version").fetchone()[0]
        print(f"   server: PostgreSQL {server}")
        conn.execute(
            """
            CREATE OR REPLACE FUNCTION pv_env_check(p_in numeric, OUT p_out numeric) AS $$
            BEGIN
              p_out := p_in * 2;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        value = conn.execute("SELECT pv_env_check(21)").fetchone()[0]
        print(f"   pv_env_check(21) -> OUT p_out = {value}")
        conn.execute("DROP FUNCTION pv_env_check(numeric)")
        conn.rollback()  # leave the sandbox exactly as we found it
        if value != 42:
            raise AssertionError(f"expected 42, got {value}")


@check("PostgreSQL platform DB (app-db): connect")
def postgres_app():
    import psycopg

    with psycopg.connect(APP_URL) as conn:
        print(f"   database: {conn.execute('SELECT current_database()').fetchone()[0]}")


@check("Redis: PING")
def redis_ping():
    import redis

    if not redis.Redis.from_url(REDIS_URL).ping():
        raise RuntimeError("PING returned False")


@check("Trap preview: is '' NULL?")
def empty_string_trap():
    import oracledb
    import psycopg

    with oracledb.connect(user=ORACLE_USER, password=ORACLE_PASSWORD, dsn=ORACLE_DSN) as conn:
        ora = conn.cursor().execute(EMPTY_STRING_SQL + " FROM dual").fetchone()[0]
    with psycopg.connect(TARGET_URL) as conn:
        pg = conn.execute(EMPTY_STRING_SQL).fetchone()[0]
    print(f"   Oracle     says '' is {ora}")
    print(f"   PostgreSQL says '' is {pg}")
    print("   Same SQL, different answer: this is what calc_bonus test cb-02 will catch.")


def main() -> int:
    for fn in (oracle_out_param, postgres_target, postgres_app, redis_ping, empty_string_trap):
        fn()
    print("\nSummary")
    for name, ok in results.items():
        print(f"   {'PASS' if ok else 'FAIL'}  {name}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())

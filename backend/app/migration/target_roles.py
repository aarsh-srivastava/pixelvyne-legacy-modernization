"""Least-privilege roles for the target sandbox.

Converted code is partly written by an LLM, so it is never deployed or run as a
superuser. Two roles split ownership from execution:

* ``pv_deployer`` creates converted objects (functions, procedures, views,
  triggers, package schemas) and owns them. It can read the sandbox tables and
  attach triggers to them, but cannot change their data or structure.
* ``pv_runner`` executes converted code during validation. It can read and
  write rows in the sandbox tables and call the deployed routines, but cannot
  create, alter, drop or truncate anything.

Neither role is a superuser, can create databases or roles, or belongs to any
other role (so no ``pg_read_server_files`` and similar). Both get a
``statement_timeout``. The sandbox tables stay owned by the admin that loaded
them, so neither role can drop or truncate them.

``ensure_roles`` is idempotent and re-applies the restrictions every time, so a
role that was loosened by hand is tightened again. It must run as a superuser
inside the target database, after the tables exist.
"""

from __future__ import annotations

import re

import psycopg
from psycopg import sql

DEPLOYER_ROLE = "pv_deployer"
RUNNER_ROLE = "pv_runner"

_RESTRICTED = "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT"
_TIMEOUT_RE = re.compile(r"^\d+(ms|s|min)?$")


def ensure_roles(
    admin_conn: psycopg.Connection,
    *,
    deployer_password: str,
    runner_password: str,
    statement_timeout: str = "30s",
    deployer_role: str = DEPLOYER_ROLE,
    runner_role: str = RUNNER_ROLE,
) -> None:
    """Create or re-restrict both roles and grant exactly what each one needs.

    Runs in one transaction on ``admin_conn`` (a superuser connected to the
    target database) and commits.
    """
    if not _TIMEOUT_RE.match(statement_timeout):
        raise ValueError(f"statement_timeout must look like '30s', got {statement_timeout!r}")

    deployer, runner = sql.Identifier(deployer_role), sql.Identifier(runner_role)

    with admin_conn.transaction(), admin_conn.cursor() as cur:
        cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        if not cur.fetchone()[0]:
            raise PermissionError("ensure_roles must run as a superuser")
        cur.execute("SELECT current_database()")
        database = sql.Identifier(cur.fetchone()[0])

        for role_name, password in ((deployer_role, deployer_password), (runner_role, runner_password)):
            role = sql.Identifier(role_name)
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,))
            verb = "ALTER" if cur.fetchone() else "CREATE"
            cur.execute(
                sql.SQL(f"{verb} ROLE {{}} {_RESTRICTED} PASSWORD {{}}").format(role, sql.Literal(password))
            )
            cur.execute(
                sql.SQL("ALTER ROLE {} SET statement_timeout = {}").format(role, sql.Literal(statement_timeout))
            )
            # Drop any role memberships (pg_read_server_files, pg_execute_server_program, ...).
            cur.execute(
                """SELECT g.rolname FROM pg_auth_members m
                     JOIN pg_roles g ON g.oid = m.roleid
                     JOIN pg_roles u ON u.oid = m.member
                    WHERE u.rolname = %s""",
                (role_name,),
            )
            for (group,) in cur.fetchall():
                cur.execute(sql.SQL("REVOKE {} FROM {}").format(sql.Identifier(group), role))

        # Database: connect for both; only the deployer may create (package) schemas.
        # Nobody else gets CREATE or TEMP through PUBLIC.
        cur.execute(sql.SQL("REVOKE CREATE, TEMPORARY ON DATABASE {} FROM PUBLIC").format(database))
        cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}, {}").format(database, deployer, runner))
        cur.execute(sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(database, deployer))
        cur.execute(sql.SQL("REVOKE CREATE ON DATABASE {} FROM {}").format(database, runner))

        # Schema public: both may use it, only the deployer may create in it.
        cur.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}, {}").format(deployer, runner))
        cur.execute(sql.SQL("GRANT CREATE ON SCHEMA public TO {}").format(deployer))
        cur.execute(sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(runner))

        # Existing sandbox tables and sequences (owned by the admin, not by either role).
        cur.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {}, {}").format(deployer, runner))
        cur.execute(
            sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}").format(runner)
        )
        cur.execute(sql.SQL("GRANT SELECT, REFERENCES, TRIGGER ON ALL TABLES IN SCHEMA public TO {}").format(deployer))
        cur.execute(sql.SQL("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {}, {}").format(deployer, runner))
        cur.execute(sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(runner))

        # Objects the deployer creates later: only the runner may execute or read them.
        cur.execute(
            sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} REVOKE EXECUTE ON ROUTINES FROM PUBLIC").format(deployer)
        )
        cur.execute(
            sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} GRANT EXECUTE ON ROUTINES TO {}").format(deployer, runner)
        )
        cur.execute(
            sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} GRANT SELECT ON TABLES TO {}").format(deployer, runner)
        )


def role_report(conn: psycopg.Connection, role: str) -> dict:
    """Facts about a role, for checks and tests: attributes and memberships."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolinherit,
                      ARRAY(SELECT g.rolname FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid
                             WHERE m.member = r.oid ORDER BY 1)
                 FROM pg_roles r WHERE rolname = %s""",
            (role,),
        )
        row = cur.fetchone()
    if row is None:
        raise LookupError(f"role {role!r} does not exist")
    keys = ("superuser", "createdb", "createrole", "replication", "bypassrls", "inherit", "member_of")
    return dict(zip(keys, row[:6] + (list(row[6]),), strict=True))

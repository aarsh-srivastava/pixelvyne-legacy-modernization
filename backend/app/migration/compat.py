"""Platform compatibility functions for converted code (step 7d).

Converted code is untrusted (part of it comes from Gemini), so the deployer only lets it
create the object being converted, never a helper function of its own. Some Oracle
behaviour cannot be written in plain SQL, though: a PostgreSQL view cannot raise an
error. For that, the platform itself installs a few small, reviewed functions, once, as
the admin, during target setup (``ensure_roles`` calls ``ensure_compat``). Converted
code may call them; it can never create, replace or drop them.

``pv_compat.connect_by_path_checked(path text, has_separator boolean) RETURNS text``

    Returns ``path`` unchanged, or raises SQLSTATE ``PV001`` with Oracle's ORA-30004
    message when ``has_separator`` is true. ``errors.py`` maps ORA-30004 and PV001 to
    the same category, ``connect_by_path_separator``, and nothing else to it.

How a converted ``CONNECT BY`` view uses it (DEF-7c-1; Oracle behaviour measured in H3,
16 Nov 2026, Oracle 23.26.3.0.0)::

    WITH RECURSIVE tree AS (
      SELECT ..., CAST(' > ' || full_name AS text) AS path, strpos(full_name, ' > ') > 0 AS has_sep
        FROM employees WHERE manager_id IS NULL
      UNION ALL
      SELECT ..., t.path || ' > ' || e.full_name, strpos(e.full_name, ' > ') > 0
        FROM employees e JOIN tree t ON e.manager_id = t.emp_id
    )
    SELECT ..., pv_compat.connect_by_path_checked(path, bool_or(has_sep) OVER ()) AS reporting_line
      FROM tree;

Oracle evaluates ``SYS_CONNECT_BY_PATH`` only when its column is requested, and then for
the whole hierarchy, before an outer WHERE: ``SELECT emp_id`` succeeds, ``SELECT * ...
WHERE emp_id = 101`` raises ORA-30004 if anyone in the hierarchy has the separator. The
pattern reproduces both:

* ``bool_or(has_sep) OVER ()`` sees every row of the hierarchy before any outer WHERE is
  applied. That is SQL semantics (a window is computed before the outer query filters
  the view), so a query that returns the path refuses whenever any row has the separator.
* A query that does not use ``reporting_line`` does not evaluate it, because PostgreSQL
  removes an unused output column of a view's subquery, which it does only for
  expressions with no VOLATILE function. That is why this helper is ``STABLE``
  (Aarsh's decision H3-1).

**Limitation, documented on purpose:** the second point is a PostgreSQL planner
optimisation (unused subquery outputs are replaced by NULL), not a documented guarantee.
It was checked on PostgreSQL 16, and tests pin it (``test_compat_7d.py``: H3.2, H3.6,
H3.7). If a future PostgreSQL stopped doing it, those tests would fail, and PostgreSQL
would become stricter than Oracle (refusing more queries), never return a misleading path.

Security: the schema and function are owned by the admin that runs setup. ``PUBLIC``
has no rights on either, ``CREATE`` on the schema is revoked from both roles, and only
``pv_deployer`` (to create views that call it) and ``pv_runner`` (to query them) get
``USAGE`` and ``EXECUTE``. Every run restores that exact state: any other grant on the
schema or the function, to any role (including grant options and grants made from them),
is revoked, and the result is checked against the intended privileges before commit.
The function is ``SECURITY INVOKER``, reads no table, and pins its ``search_path``.
``ensure_compat`` refuses to run if the schema or any function in it is owned by another
role, since that role could then replace the function.
"""

from __future__ import annotations

import psycopg
from psycopg import sql

COMPAT_SCHEMA = "pv_compat"
PATH_CHECK_FUNCTION = "connect_by_path_checked"
PATH_CHECK_SIGNATURE = "connect_by_path_checked(text, boolean)"
PATH_SEPARATOR_SQLSTATE = "PV001"
#: Oracle's ORA-30004 message, so a person reading the PostgreSQL error sees the same words.
PATH_SEPARATOR_MESSAGE = "when using SYS_CONNECT_BY_PATH function, cannot have separator as part of column value"

_FUNCTION_SQL = f"""
CREATE OR REPLACE FUNCTION {COMPAT_SCHEMA}.{PATH_CHECK_FUNCTION}(p_path text, p_has_separator boolean)
RETURNS text
LANGUAGE plpgsql
STABLE
SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $fn$
BEGIN
  IF p_has_separator THEN
    RAISE EXCEPTION USING ERRCODE = '{PATH_SEPARATOR_SQLSTATE}', MESSAGE = '{PATH_SEPARATOR_MESSAGE}';
  END IF;
  RETURN p_path;
END
$fn$
"""


def ensure_compat(cur: psycopg.Cursor, *, deployer_role: str, runner_role: str) -> None:
    """Install (or re-apply) the compatibility schema, its function and their grants.

    Runs on a superuser cursor inside the caller's transaction (``ensure_roles``).
    Idempotent: the function is replaced in place and every grant is re-applied.
    """
    schema = sql.Identifier(COMPAT_SCHEMA)
    deployer, runner = sql.Identifier(deployer_role), sql.Identifier(runner_role)
    function = sql.SQL("{}.{}(text, boolean)").format(schema, sql.Identifier(PATH_CHECK_FUNCTION))

    cur.execute("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = %s", (COMPAT_SCHEMA,))
    row = cur.fetchone()
    cur.execute("SELECT current_user")
    admin = cur.fetchone()[0]
    if row is not None and row[0] != admin:
        raise PermissionError(f"schema {COMPAT_SCHEMA} is owned by {row[0]}, not {admin}; refusing to install into it")
    if row is None:
        cur.execute(sql.SQL("CREATE SCHEMA {}").format(schema))
    foreign = _foreign_functions(cur, admin)
    if foreign:
        raise PermissionError(f"{COMPAT_SCHEMA} holds functions owned by other roles: {foreign}; refusing")

    cur.execute(_FUNCTION_SQL)

    # Restore the complete intended privilege state, whatever was granted by hand before:
    # revoke everything from every role but the owner (CASCADE also removes grants made
    # through a grant option), then grant exactly what the two roles need.
    for grantee in ["PUBLIC", *_grantees(cur, "schema")]:
        cur.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM {} CASCADE").format(schema, _role(grantee)))
    for grantee in ["PUBLIC", *_grantees(cur, "function")]:
        cur.execute(sql.SQL("REVOKE ALL ON FUNCTION {} FROM {} CASCADE").format(function, _role(grantee)))
    cur.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}, {}").format(schema, deployer, runner))
    cur.execute(sql.SQL("GRANT EXECUTE ON FUNCTION {} TO {}, {}").format(function, deployer, runner))

    expected = {"schema": {(deployer_role, "USAGE", False), (runner_role, "USAGE", False)},
                "function": {(deployer_role, "EXECUTE", False), (runner_role, "EXECUTE", False)}}
    for kind, wanted in expected.items():
        actual = set(privilege_entries(cur, kind))
        if actual != wanted:
            raise PermissionError(f"{COMPAT_SCHEMA} {kind} privileges are {sorted(actual)}, expected "
                                  f"{sorted(wanted)}; refusing")


_ACL = {
    "schema": "SELECT n.nspacl, n.nspowner FROM pg_namespace n WHERE n.nspname = %s",
    "function": "SELECT p.proacl, p.proowner FROM pg_proc p WHERE p.oid = %s::regprocedure",
}
_ACL_ARG = {"schema": COMPAT_SCHEMA, "function": f"{COMPAT_SCHEMA}.{PATH_CHECK_SIGNATURE}"}


def privilege_entries(cur: psycopg.Cursor, kind: str) -> list[tuple[str, str, bool]]:
    """(grantee, privilege, grantable) for every role but the owner; 'PUBLIC' for PUBLIC."""
    cur.execute(
        f"""SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END,
                   a.privilege_type, a.is_grantable
              FROM ({_ACL[kind]}) o(acl, owner), aclexplode(o.acl) a
             WHERE a.grantee <> o.owner""",
        (_ACL_ARG[kind],),
    )
    return [(grantee, privilege, bool(grantable)) for grantee, privilege, grantable in cur.fetchall()]


def _grantees(cur: psycopg.Cursor, kind: str) -> list[str]:
    return sorted({grantee for grantee, _p, _g in privilege_entries(cur, kind) if grantee != "PUBLIC"})


def _role(name: str) -> sql.Composable:
    return sql.SQL("PUBLIC") if name == "PUBLIC" else sql.Identifier(name)


def _foreign_functions(cur: psycopg.Cursor, admin: str) -> list[str]:
    cur.execute(
        """SELECT p.oid::regprocedure::text, pg_get_userbyid(p.proowner)
             FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = %s""",
        (COMPAT_SCHEMA,),
    )
    return sorted(f"{name} ({owner})" for name, owner in cur.fetchall() if owner != admin)


def compat_report(conn: psycopg.Connection) -> dict:
    """Facts about the installed helper, for checks and tests."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT pg_get_userbyid(p.proowner), p.provolatile, p.prosecdef, l.lanname, p.proconfig,
                      pg_get_userbyid(n.nspowner), n.nspacl::text, p.proacl::text
                 FROM pg_proc p
                 JOIN pg_namespace n ON n.oid = p.pronamespace
                 JOIN pg_language l ON l.oid = p.prolang
                WHERE n.nspname = %s AND p.proname = %s""",
            (COMPAT_SCHEMA, PATH_CHECK_FUNCTION),
        )
        row = cur.fetchone()
    if row is None:
        raise LookupError(f"{COMPAT_SCHEMA}.{PATH_CHECK_FUNCTION} is not installed")
    keys = ("owner", "volatility", "security_definer", "language", "config", "schema_owner", "schema_acl",
            "function_acl")
    return dict(zip(keys, row, strict=True))

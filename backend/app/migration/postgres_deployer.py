"""Deploy converted PostgreSQL code into the target sandbox, safely.

Converted code is untrusted: part of it comes from an LLM. Before anything runs,
the code is split into top-level statements and checked:

1. **Allow-list.** Only ``CREATE [OR REPLACE] FUNCTION / PROCEDURE / VIEW / TRIGGER``
   and ``CREATE SCHEMA IF NOT EXISTS <package schema>`` are accepted, and each must
   create the object being converted (or its trigger function / package schema).
   Everything else, including any ``DROP``, is refused.
2. **Routine options.** ``LANGUAGE`` must be ``plpgsql`` or ``sql``; ``SECURITY
   DEFINER`` and ``SET`` clauses are refused.
3. **Dangerous operations inside bodies.** Routine bodies are scanned (with string
   literals and comments masked, so the words in a message don't count) for DDL,
   dynamic SQL, transaction control, role changes, server-file and network
   functions, and ``UPDATE`` / ``DELETE`` without ``WHERE``.

Text checks can't be complete. The real guard is privilege: the deployer must be
connected as ``pv_deployer`` and validation runs as ``pv_runner`` (see
``target_roles``), neither of them a superuser. The deployer refuses to run on a
superuser connection or on any database other than the target.

Deployment is one transaction: ``SET LOCAL`` timeouts and ``search_path``, then the
deployer's own narrow drops of the same-named routine or view (``IF EXISTS``,
never ``CASCADE``, so a new signature can replace the old one), then the code.
Triggers and their functions are never dropped (``DROP TRIGGER`` needs ownership of
the table, which the deployer deliberately lacks); their ``CREATE`` is turned into
``CREATE OR REPLACE`` instead. Success
commits; any error rolls back completely, so the previously deployed version stays
live. The connection is always left with no transaction open, because validation
uses its own transactions afterwards.

``DeployResult.ok`` is True only after commit. Errors carry SQLSTATE and
PostgreSQL's message, never connection details.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus

from app.migration.target_roles import DEPLOYER_ROLE, RUNNER_ROLE

KINDS = ("function", "procedure", "view", "trigger")
ALLOWED_LANGUAGES = frozenset({"plpgsql", "sql"})
MAX_CODE_BYTES = 200_000
_TIMEOUT_RE = re.compile(r"^\d+(ms|s|min)?$")
_PLAIN_NAME = re.compile(r"^[a-z_][a-z0-9_$]*$")


class DeploymentRefused(Exception):
    """The code was not run because a safety check failed."""


@dataclass(frozen=True)
class DeployTarget:
    """What the code is supposed to create.

    ``schema`` is ``public`` for standalone objects and the package name for
    package members (``payroll_pkg``). A trigger also names its table and the
    function it executes, which the same code must create in ``schema``.
    """

    kind: str
    name: str
    schema: str = "public"
    trigger_table: str | None = None
    trigger_function: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}, got {self.kind!r}")
        for label, value in (("name", self.name), ("schema", self.schema)):
            if not _PLAIN_NAME.match(value or ""):
                raise ValueError(f"{label} must be a lower-case unquoted identifier, got {value!r}")
        if self.kind == "trigger":
            if not (self.trigger_table and self.trigger_function):
                raise ValueError("a trigger target needs trigger_table and trigger_function")
            if not _PLAIN_NAME.match(self.trigger_function):
                raise ValueError(f"bad trigger_function {self.trigger_function!r}")

    @property
    def table_key(self) -> tuple[str, str] | None:
        if not self.trigger_table:
            return None
        schema, _, name = self.trigger_table.rpartition(".")
        return (schema or "public", name)


@dataclass(frozen=True)
class DeployResult:
    ok: bool
    error: str | None = None
    sqlstate: str | None = None
    refused: bool = False
    statements: tuple[str, ...] = ()


@dataclass
class DeployPlan:
    schema_statements: list[str] = field(default_factory=list)
    drops: list[sql.Composable] = field(default_factory=list)
    statements: list[str] = field(default_factory=list)
    grant_usage_on: list[str] = field(default_factory=list)


# --- lexing: statements, strings, comments, dollar-quoted bodies -------------------

@dataclass
class _Segment:
    kind: str  # code | string | ident | comment | dollar
    text: str  # the raw text, delimiters included
    body: str = ""  # for dollar: the text between the tags


_DOLLAR_TAG = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


def _lex(text: str) -> list[_Segment]:
    segments: list[_Segment] = []
    code_start, i, n = 0, 0, len(text)

    def flush_code(end: int) -> None:
        if end > code_start:
            segments.append(_Segment("code", text[code_start:end]))

    while i < n:
        ch = text[i]
        two = text[i:i + 2]
        if two == "--":
            end = text.find("\n", i)
            end = n if end == -1 else end + 1
            flush_code(i)
            segments.append(_Segment("comment", text[i:end]))
            i = code_start = end
        elif two == "/*":
            depth, j = 1, i + 2
            while j < n and depth:
                if text.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif text.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            if depth:
                raise DeploymentRefused("unterminated /* comment")
            flush_code(i)
            segments.append(_Segment("comment", text[i:j]))
            i = code_start = j
        elif ch == "'":
            backslash = i > 0 and text[i - 1] in "eE" and (i < 2 or not (text[i - 2].isalnum() or text[i - 2] == "_"))
            j = i + 1
            while True:
                if j >= n:
                    raise DeploymentRefused("unterminated string literal")
                if backslash and text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "'":
                    if text[j + 1:j + 2] == "'":
                        j += 2
                        continue
                    break
                j += 1
            flush_code(i)
            segments.append(_Segment("string", text[i:j + 1]))
            i = code_start = j + 1
        elif ch == '"':
            j = i + 1
            while True:
                if j >= n:
                    raise DeploymentRefused("unterminated quoted identifier")
                if text[j] == '"':
                    if text[j + 1:j + 2] == '"':
                        j += 2
                        continue
                    break
                j += 1
            flush_code(i)
            segments.append(_Segment("ident", text[i:j + 1]))
            i = code_start = j + 1
        elif ch == "$" and not (i > 0 and (text[i - 1].isalnum() or text[i - 1] in "_$")):
            match = _DOLLAR_TAG.match(text, i)
            if not match:
                i += 1
                continue
            tag = match.group(0)
            end = text.find(tag, match.end())
            if end == -1:
                raise DeploymentRefused(f"unterminated {tag} body")
            flush_code(i)
            segments.append(_Segment("dollar", text[i:end + len(tag)], body=text[match.end():end]))
            i = code_start = end + len(tag)
        else:
            i += 1
    flush_code(n)
    return segments


@dataclass
class _Statement:
    text: str
    segments: list[_Segment]

    @property
    def outline(self) -> str:
        """Top-level text: strings masked, comments dropped, bodies replaced by $BODY$."""
        parts = []
        for seg in self.segments:
            if seg.kind == "code" or seg.kind == "ident":
                parts.append(seg.text)
            elif seg.kind == "string":
                parts.append("'?'")
            elif seg.kind == "dollar":
                parts.append(" $BODY$ ")
            else:
                parts.append(" ")
        return re.sub(r"\s+", " ", "".join(parts)).strip()

    @property
    def bodies(self) -> list[str]:
        return [_masked_code(seg.body) for seg in self.segments if seg.kind == "dollar"]


def _masked_code(text: str) -> str:
    """Code with string literals emptied and comments removed; nested bodies kept as code."""
    parts = []
    for seg in _lex(text):
        if seg.kind in ("code", "ident"):
            parts.append(seg.text)
        elif seg.kind == "string":
            parts.append("''")
        elif seg.kind == "dollar":
            parts.append(" " + _masked_code(seg.body) + " ")
        else:
            parts.append(" ")
    return "".join(parts)


def split_statements(code: str) -> list[_Statement]:
    """Split on top-level semicolons. Empty and comment-only statements are dropped."""
    statements: list[_Statement] = []
    current: list[_Segment] = []

    def close() -> None:
        if any(seg.kind != "comment" and seg.text.strip() for seg in current):
            text = "".join(seg.text for seg in current).strip()
            statements.append(_Statement(text, list(current)))
        current.clear()

    for seg in _lex(code):
        if seg.kind != "code":
            current.append(seg)
            continue
        pieces = seg.text.split(";")
        for k, piece in enumerate(pieces):
            if piece:
                current.append(_Segment("code", piece))
            if k < len(pieces) - 1:
                close()
    close()
    return statements


# --- the checks ------------------------------------------------------------------

_IDENT = r'(?:"(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_$]*)'
_QNAME = rf"({_IDENT})(?:\s*\.\s*({_IDENT}))?"
_ROUTINE = re.compile(rf"^CREATE\s+(?:OR\s+REPLACE\s+)?(FUNCTION|PROCEDURE)\s+{_QNAME}\s*\(", re.I)
_VIEW = re.compile(rf"^CREATE\s+(?:OR\s+REPLACE\s+)?(?:RECURSIVE\s+)?VIEW\s+{_QNAME}(?=[\s(]|$)", re.I)
_TRIGGER = re.compile(
    rf"^CREATE\s+(?:OR\s+REPLACE\s+)?TRIGGER\s+({_IDENT})\s+(?:BEFORE|AFTER|INSTEAD\s+OF)\b.*?"
    rf"\bON\s+{_QNAME}\b.*\bEXECUTE\s+(?:FUNCTION|PROCEDURE)\s+{_QNAME}\s*\(",
    re.I | re.S,
)
_SCHEMA = re.compile(rf"^CREATE\s+SCHEMA\s+IF\s+NOT\s+EXISTS\s+({_IDENT})$", re.I)
_LANGUAGE = re.compile(rf"\bLANGUAGE\s+({_IDENT}|'\?')", re.I)

# Functions that reach outside the sandbox (files, network, other sessions, settings).
_DANGEROUS_CALLS = re.compile(
    r"\b(pg_read_file|pg_read_binary_file|pg_ls_\w+|pg_stat_file|pg_file_\w+|lo_import|lo_export|"
    r"dblink\w*|pg_terminate_backend|pg_cancel_backend|pg_reload_conf|pg_rotate_logfile|pg_promote|"
    r"set_config)\s*\(",
    re.I,
)
# Statements a converted routine body must never contain.
_DANGEROUS_IN_BODY = [
    (re.compile(r"\b(DROP|TRUNCATE|ALTER|GRANT|REVOKE|CREATE|COPY|VACUUM|CLUSTER|REINDEX|LISTEN|NOTIFY|LOAD)\b", re.I),
     "{0}"),
    (re.compile(r"\bLOCK\s+TABLE\b", re.I), "LOCK TABLE"),
    (re.compile(r"\bEXECUTE\b", re.I), "dynamic SQL (EXECUTE)"),
    (re.compile(r"\b(COMMIT|ROLLBACK|SAVEPOINT)\b", re.I), "transaction control ({0})"),
    (re.compile(r"\b(SET|RESET)\s+(?:SESSION\s+|LOCAL\s+)?(ROLE|SESSION\s+AUTHORIZATION|search_path|ALL)\b", re.I),
     "{0} {1}"),
]
_DML_TABLE = (
    rf"(?:ONLY\s+)?{_IDENT}(?:\s*\.\s*{_IDENT})?"
    rf"(?:\s+(?:AS\s+)?(?!(?:WHERE|SET|USING|RETURNING)\b){_IDENT})?"  # optional alias
)
_UNFILTERED_DML = re.compile(rf"\b(DELETE\s+FROM\s+{_DML_TABLE}|UPDATE\s+{_DML_TABLE}\s+SET)\b", re.I)


def _name(raw: str) -> str:
    if raw.startswith('"'):
        return raw[1:-1].replace('""', '"')
    return raw.lower()


def _qualified(first: str, second: str | None) -> tuple[str, str]:
    return (_name(first), _name(second)) if second else ("public", _name(first))


def _check_body(body: str, where: str) -> None:
    for pattern, label in _DANGEROUS_IN_BODY:
        match = pattern.search(body)
        if match:
            what = label.format(*(g.upper() for g in match.groups())) if match.groups() else label
            raise DeploymentRefused(f"{where} contains {what}")
    call = _DANGEROUS_CALLS.search(body)
    if call:
        raise DeploymentRefused(f"{where} calls {call.group(1)}()")
    for piece in body.split(";"):
        for match in _UNFILTERED_DML.finditer(piece):
            if not re.search(r"\bWHERE\b", piece[match.end():], re.I):
                verb = match.group(1).split()[0].upper()
                raise DeploymentRefused(f"{where} has {verb} without WHERE")


_CREATE_NOT_REPLACE = re.compile(r"^(\s*)CREATE\s+(?!OR\s+REPLACE\b)", re.I)


def _or_replace(stmt: _Statement) -> str:
    """The statement's text with ``CREATE`` turned into ``CREATE OR REPLACE`` (if it isn't already)."""
    parts, done = [], False
    for seg in stmt.segments:
        if not done and seg.kind == "code" and seg.text.strip():
            parts.append(_CREATE_NOT_REPLACE.sub(r"\1CREATE OR REPLACE ", seg.text, count=1))
            done = True
        else:
            parts.append(seg.text)
    return "".join(parts).strip()


def plan_deployment(code: str, target: DeployTarget) -> DeployPlan:
    """Check the code against ``target`` and return what to run. Raises DeploymentRefused."""
    if not code or not code.strip():
        raise DeploymentRefused("no code to deploy")
    if len(code.encode()) > MAX_CODE_BYTES:
        raise DeploymentRefused(f"code is larger than {MAX_CODE_BYTES} bytes")

    statements = split_statements(code)
    if not statements:
        raise DeploymentRefused("no statements to deploy")

    plan = DeployPlan()
    main_created = 0
    trigger_function_created = False
    routine_drops: list[tuple[str, str]] = []
    view_drops: list[tuple[str, str]] = []
    expected = (target.schema, target.name)

    for number, stmt in enumerate(statements, start=1):
        outline = stmt.outline
        where = f"statement {number}"
        text = stmt.text

        if match := _SCHEMA.match(outline):
            schema = _name(match.group(1))
            if target.schema == "public" or schema != target.schema:
                raise DeploymentRefused(f"{where} creates schema {schema!r}; only {target.schema!r} is expected")
            plan.schema_statements.append(stmt.text)
            plan.grant_usage_on.append(schema)
            continue

        call = _DANGEROUS_CALLS.search(outline)
        if call:
            raise DeploymentRefused(f"{where} calls {call.group(1)}()")

        if match := _ROUTINE.match(outline):
            kind = match.group(1).lower()
            created = _qualified(match.group(2), match.group(3))
            is_trigger_fn = (
                target.kind == "trigger" and kind == "function"
                and created == (target.schema, target.trigger_function)
            )
            if not is_trigger_fn:
                if target.kind not in ("function", "procedure"):
                    raise DeploymentRefused(f"{where} creates a {kind}; expected a {target.kind} {target.name!r}")
                if kind != target.kind:
                    raise DeploymentRefused(f"{where} creates a {kind}; expected a {target.kind}")
                if created != expected:
                    raise DeploymentRefused(
                        f"{where} creates {'.'.join(created)}; expected {target.schema}.{target.name}"
                    )
            if re.search(r"\bSECURITY\s+DEFINER\b", outline, re.I):
                raise DeploymentRefused(f"{where} uses SECURITY DEFINER")
            if re.search(r"\bSET\b", outline, re.I):
                raise DeploymentRefused(f"{where} sets a configuration parameter")
            languages = {_name(m.group(1)) for m in _LANGUAGE.finditer(outline)}
            if not languages:
                raise DeploymentRefused(f"{where} has no LANGUAGE clause")
            bad = languages - ALLOWED_LANGUAGES
            if bad:
                raise DeploymentRefused(f"{where} uses LANGUAGE {sorted(bad)[0]}; only plpgsql or sql")
            if not stmt.bodies:
                raise DeploymentRefused(f"{where} has no $$-quoted body")
            for body in stmt.bodies:
                _check_body(body, f"{where} body")
            if is_trigger_fn:
                # Replaced in place: the trigger depends on it, and its signature never changes.
                trigger_function_created = True
                text = _or_replace(stmt)
            else:
                main_created += 1
                routine_drops.append(created)

        elif match := _VIEW.match(outline):
            created = _qualified(match.group(1), match.group(2))
            if target.kind != "view" or created != expected:
                raise DeploymentRefused(
                    f"{where} creates view {'.'.join(created)}; expected {target.kind} {target.schema}.{target.name}"
                )
            masked = _masked_code(stmt.text)
            query = re.search(r"\bAS\b", masked, re.I)
            if not query:
                raise DeploymentRefused(f"{where} has no AS query")
            _check_body(masked[query.end():], f"{where} query")
            main_created += 1
            view_drops.append(created)

        elif match := _TRIGGER.match(outline):
            name = _name(match.group(1))
            table = _qualified(match.group(2), match.group(3))
            function = _qualified(match.group(4), match.group(5))
            if target.kind != "trigger" or name != target.name:
                raise DeploymentRefused(f"{where} creates trigger {name!r}; expected {target.kind} {target.name!r}")
            if table != target.table_key:
                raise DeploymentRefused(
                    f"{where} puts the trigger on {'.'.join(table)}; expected {target.trigger_table}"
                )
            if function != (target.schema, target.trigger_function):
                raise DeploymentRefused(
                    f"{where} executes {'.'.join(function)}; expected {target.schema}.{target.trigger_function}"
                )
            if re.search(r"\$BODY\$", outline):
                raise DeploymentRefused(f"{where} has an unexpected quoted body")
            main_created += 1
            # DROP TRIGGER needs ownership of the table, which the deployer doesn't have;
            # CREATE OR REPLACE TRIGGER only needs the TRIGGER privilege.
            text = _or_replace(stmt)

        else:
            first = " ".join(outline.split()[:4])
            raise DeploymentRefused(f"{where} is not allowed: {first!r}")

        plan.statements.append(text)

    if main_created == 0:
        raise DeploymentRefused(f"code does not create {target.kind} {target.schema}.{target.name}")
    if main_created > 1:
        raise DeploymentRefused(f"code creates {target.kind} {target.name!r} more than once")
    if target.kind == "trigger" and not trigger_function_created:
        raise DeploymentRefused(f"code does not create trigger function {target.schema}.{target.trigger_function}")

    # Drops let a new signature replace the old one: views first, then routines.
    # Triggers and their functions are never dropped, only replaced (see above).
    for schema, name in view_drops:
        plan.drops.append(sql.SQL("DROP VIEW IF EXISTS {}").format(sql.Identifier(schema, name)))
    for schema, name in routine_drops:
        plan.drops.append(sql.SQL("DROP ROUTINE IF EXISTS {}").format(sql.Identifier(schema, name)))
    return plan


# --- running it ------------------------------------------------------------------

def _describe(exc: psycopg.Error) -> str:
    message = exc.diag.message_primary if exc.diag else None
    message = message or (str(exc).strip().splitlines() or [type(exc).__name__])[0]
    return f"{exc.sqlstate}: {message}" if exc.sqlstate else message


class PostgresDeployer:
    """Deploys one object at a time on a ``pv_deployer`` connection to the target."""

    def __init__(
        self,
        conn: psycopg.Connection,
        *,
        database: str = "target",
        role: str = DEPLOYER_ROLE,
        runner_role: str = RUNNER_ROLE,
        statement_timeout: str = "30s",
        lock_timeout: str = "5s",
    ) -> None:
        for value in (statement_timeout, lock_timeout):
            if not _TIMEOUT_RE.match(value):
                raise ValueError(f"timeouts must look like '30s', got {value!r}")
        self.conn = conn
        self.database = database
        self.role = role
        self.runner_role = runner_role
        self.statement_timeout = statement_timeout
        self.lock_timeout = lock_timeout

    def deploy(self, code: str, target: DeployTarget) -> DeployResult:
        try:
            plan = plan_deployment(code, target)
        except DeploymentRefused as exc:
            return DeployResult(ok=False, error=f"Refused: {exc}", refused=True)

        if self.conn.closed:
            return DeployResult(ok=False, error="Refused: the target connection is closed", refused=True)
        if self.conn.info.transaction_status != TransactionStatus.IDLE:
            return DeployResult(ok=False, error="Refused: the connection has an open transaction", refused=True)

        try:
            with self.conn.transaction(), self.conn.cursor() as cur:
                self._check_identity(cur)
                cur.execute(sql.SQL("SET LOCAL statement_timeout = {}").format(sql.Literal(self.statement_timeout)))
                cur.execute(sql.SQL("SET LOCAL lock_timeout = {}").format(sql.Literal(self.lock_timeout)))
                cur.execute("SET LOCAL search_path = public")
                for statement in plan.schema_statements:
                    cur.execute(statement)
                for schema in plan.grant_usage_on:
                    cur.execute(
                        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                            sql.Identifier(schema), sql.Identifier(self.runner_role)
                        )
                    )
                for drop in plan.drops:
                    cur.execute(drop)
                for statement in plan.statements:
                    cur.execute(statement)
        except DeploymentRefused as exc:
            return DeployResult(ok=False, error=f"Refused: {exc}", refused=True)
        except psycopg.Error as exc:
            return DeployResult(ok=False, error=_describe(exc), sqlstate=exc.sqlstate)

        return DeployResult(ok=True, statements=tuple(plan.schema_statements + plan.statements))

    def _check_identity(self, cur: psycopg.Cursor) -> None:
        cur.execute(
            """SELECT current_database(), current_user, session_user,
                      (SELECT rolsuper FROM pg_roles WHERE rolname = current_user),
                      (SELECT rolsuper FROM pg_roles WHERE rolname = session_user)"""
        )
        database, current, session, current_super, session_super = cur.fetchone()
        if database != self.database:
            raise DeploymentRefused(f"connected to database {database!r}, expected {self.database!r}")
        if current_super or session_super:
            raise DeploymentRefused("connected as a superuser; deploy as the restricted deployer role")
        if self.role and current != self.role:
            raise DeploymentRefused(f"connected as {current!r}, expected {self.role!r}")

"""Is the target prepared? The F1 pre-flight, and the PreparedTargetSchema stand-in (contract 0.7).

**Pre-flight** (approved by Aarsh on 17 Nov as F1; moved here unchanged from
scripts/run_repair.py, which still imports it): read-only catalogue checks that the
deployer and runner connections run as pv_deployer and pv_runner on the configured
database, that every expected table and sequence exists in public, that both roles hold
exactly the rights setup_target_roles.py grants, and that the pv_compat helper is
installed, admin-owned and usable. ``preflight`` returns every problem with its fix.

**PreparedTargetSchema** is the SchemaMigrator stand-in until M3: it creates nothing. It
runs the same pre-flight (plus the check for objects pv_deployer does not own) and
reports, per expected table and sequence, ``present`` (with its TableDef or SequenceDef
read from the catalogue), ``missing``, or ``mismatch``. ``present`` means every property
below matches the Oracle DDL in the sources; any difference, and anything it cannot check,
is a ``mismatch`` with the reason (Aarsh's v0.7 review, 19 Nov):

* tables: the column names; each column's type (the Oracle type through the converter's
  type rule, ``translate_type``); NOT NULL (declared, or implied by the primary key); the
  default (literals only: a non-literal Oracle default such as SYSDATE cannot be checked
  here and is a mismatch, M3 creates those); the primary key; unique keys; foreign keys
  (columns, referenced table with its schema, referenced columns, ON DELETE); the rights
  setup_target_roles.py grants.
* sequences: START WITH, INCREMENT BY, MINVALUE and CYCLE, with Oracle's defaults when
  the DDL leaves them out; MAXVALUE only when declared (Oracle's default, 10^28 - 1, does
  not fit a PostgreSQL sequence); the runner's rights.

It checks the schema, never the data: parity (A15) stays ``not_checked`` in prepared mode.
It needs only the deployer and runner connections, never the admin URL, and leaves both
rolled back.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from app.contracts import (
    ColumnDef,
    ForeignKeyDef,
    SchemaMigrationResult,
    SchemaObjectOutcome,
    SchemaObjectStatus,
    SequenceDef,
    SourceFile,
    TableDef,
    TargetEnvironment,
    parse_object_id,
)
from app.migration.compat import COMPAT_SCHEMA, PATH_CHECK_FUNCTION
from app.migration.features import mask_oracle
from app.migration.plsql_translator import translate_type
from app.migration.target_roles import DEPLOYER_ROLE, RUNNER_ROLE

TARGET_SCHEMA = "public"   # phase 1 (D8): the sample's tables live in public under their source names


# ---------------------------------------------------------------- F1 pre-flight (moved unchanged)

#: The one routine pv_deployer may find but not own: the platform helper (step 7d), exactly this
#: signature and admin-owned. Anything else in pv_compat, or any other overload, still counts.
PLATFORM_HELPER_ARGTYPES = "25 16"   # (text, boolean) as pg_proc.proargtypes


def foreign_routines(conn) -> list[str]:
    """Routines, views or triggers in the target that pv_deployer does not own (e.g. seeded stand-ins).

    Only the approved platform helper is not counted, and only while a superuser owns it; the
    pre-flight checks the helper separately. Routines are reported as schema.name(argument types).
    """
    rows = conn.execute(
        "SELECT n.nspname || '.' || p.proname || '(' || oidvectortypes(p.proargtypes) || ')' "
        "  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_roles r ON r.oid = p.proowner "
        " WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND r.rolname <> 'pv_deployer' "
        "   AND NOT (n.nspname = %s AND p.proname = %s AND p.proargtypes = %s::oidvector AND r.rolsuper)",
        (COMPAT_SCHEMA, PATH_CHECK_FUNCTION, PLATFORM_HELPER_ARGTYPES),
    ).fetchall()
    found = [r[0] for r in rows]
    views = conn.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind = 'v' AND n.nspname = 'public' AND pg_get_userbyid(c.relowner) <> 'pv_deployer'"
    ).fetchall()
    return sorted(found + [f"public.{v[0]} (view)" for v in views])


# What setup_target_roles.py grants (target_roles.ensure_roles); the pre-flight checks exactly these.
DEPLOYER_TABLE_PRIVILEGES = ("SELECT", "REFERENCES", "TRIGGER")
RUNNER_TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE")
RUNNER_SEQUENCE_PRIVILEGES = ("USAGE", "SELECT")
RESET_HINT = "run scripts/seed_target.py --schema-only, then scripts/setup_target_roles.py"
ROLES_HINT = "run scripts/setup_target_roles.py"


def _missing(conn, role: str, kind: str, found: dict[str, int], privileges: tuple[str, ...]) -> list[str]:
    """'name (PRIV, PRIV)' for each existing public table or sequence the role lacks privileges on.

    By OID, so the check works even when the connection itself has no USAGE on public (a name
    would have to be looked up in the schema first, which needs that USAGE)."""
    out = []
    for name, oid in sorted(found.items()):
        lacking = [p for p in privileges
                   if not conn.execute(f"SELECT has_{kind}_privilege(%s, %s::oid, %s)", (role, oid, p)).fetchone()[0]]
        if lacking:
            out.append(f"{name} ({', '.join(lacking)})")
    return out


def preflight(deployer, runner, *, database: str, tables: list[str], sequences: list[str]) -> list[str]:
    """Every reason the target is not ready for the real converter; empty when it is.

    Read-only catalogue queries (both connections are rolled back afterwards). Called before
    the converter or Gemini exists, so a failure here costs no Gemini call.
    """
    problems: list[str] = []
    try:
        for conn, role, variable in ((deployer, DEPLOYER_ROLE, "TARGET_DEPLOYER_URL"),
                                     (runner, RUNNER_ROLE, "TARGET_RUNNER_URL")):
            user, db = conn.execute("SELECT current_user, current_database()").fetchone()
            if user != role:
                problems.append(f"the {role} connection runs as {user}, not {role}: check {variable}")
            if db != database:
                problems.append(f"the {role} connection is on database {db}, not {database}: check {variable} "
                                "and TARGET_DATABASE_NAME")

        rows = deployer.execute(
            "SELECT c.relname, c.relkind, c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relname = ANY(%s)", (tables + sequences,)).fetchall()
        found_tables = {n: oid for n, k, oid in rows if k in ("r", "p") and n in tables}
        found_sequences = {n: oid for n, k, oid in rows if k == "S" and n in sequences}
        for kind, wanted, found in (("table", tables, found_tables), ("sequence", sequences, found_sequences)):
            absent = sorted(set(wanted) - set(found))
            if absent:
                problems.append(f"missing {kind}(s) in public: {', '.join(absent)}: {RESET_HINT}")

        for role, privilege in ((DEPLOYER_ROLE, "USAGE"), (DEPLOYER_ROLE, "CREATE"), (RUNNER_ROLE, "USAGE")):
            if not deployer.execute("SELECT has_schema_privilege(%s, 'public', %s)", (role, privilege)).fetchone()[0]:
                problems.append(f"{role} lacks {privilege} on schema public: {ROLES_HINT}")
        for role, kind, names, privileges in (
            (DEPLOYER_ROLE, "table", found_tables, DEPLOYER_TABLE_PRIVILEGES),
            (RUNNER_ROLE, "table", found_tables, RUNNER_TABLE_PRIVILEGES),
            (RUNNER_ROLE, "sequence", found_sequences, RUNNER_SEQUENCE_PRIVILEGES),
        ):
            lacking = _missing(deployer, role, kind, names, privileges)
            if lacking:
                problems.append(f"{role} lacks rights on {kind}(s) {'; '.join(lacking)}: {ROLES_HINT}")

        helper = deployer.execute(
            "SELECT p.oid, r.rolname, r.rolsuper FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "JOIN pg_roles r ON r.oid = p.proowner "
            "WHERE n.nspname = %s AND p.proname = %s AND p.proargtypes = '25 16'::oidvector",
            (COMPAT_SCHEMA, PATH_CHECK_FUNCTION)).fetchone()
        if helper is None:
            problems.append(f"{COMPAT_SCHEMA}.{PATH_CHECK_FUNCTION}(text, boolean) is not installed: {ROLES_HINT}")
        else:
            if not helper[2]:   # admin-owned by design; the two restricted roles are never superusers
                problems.append(f"{COMPAT_SCHEMA}.{PATH_CHECK_FUNCTION} is owned by {helper[1]}, not by the admin "
                                f"that ran setup: {ROLES_HINT}")
            for role in (DEPLOYER_ROLE, RUNNER_ROLE):
                usable = deployer.execute(
                    "SELECT has_schema_privilege(%s, %s, 'USAGE') AND has_function_privilege(%s, %s, 'EXECUTE')",
                    (role, COMPAT_SCHEMA, role, helper[0])).fetchone()[0]
                if not usable:
                    problems.append(f"{role} cannot use {COMPAT_SCHEMA}.{PATH_CHECK_FUNCTION}: {ROLES_HINT}")
    finally:
        deployer.rollback()
        runner.rollback()
    return problems


# ---------------------------------------------------------------- the Oracle DDL's definitions

_ON_DELETE_ORACLE = {"CASCADE": "cascade", "SET NULL": "set_null"}


@dataclass(frozen=True)
class OracleColumn:
    name: str
    oracle_type: str
    not_null: bool
    default: str | None          # the Oracle default as written, e.g. "'ACTIVE'" or "SYSDATE"


@dataclass(frozen=True)
class OracleForeignKey:
    columns: tuple[str, ...]
    ref_schema: str | None       # None: same schema as the table
    ref_table: str
    ref_columns: tuple[str, ...]
    on_delete: str


@dataclass
class OracleTable:
    name: str
    columns: dict[str, OracleColumn] = field(default_factory=dict)
    primary_key: list[str] = field(default_factory=list)
    unique: set[tuple[str, ...]] = field(default_factory=set)
    foreign_keys: set[OracleForeignKey] = field(default_factory=set)


@dataclass(frozen=True)
class OracleSequence:
    name: str
    start_with: int | None
    increment_by: int | None
    min_value: int | None
    max_value: int | None
    cycle: bool | None


def _names(node) -> tuple[str, ...]:
    return tuple(i.name.lower() for i in node.expressions)


def _foreign_key(columns: tuple[str, ...], reference: exp.Reference) -> OracleForeignKey:
    target = reference.this
    table = target.this if isinstance(target, exp.Schema) else target
    options = " ".join(str(o).upper() for o in reference.args.get("options") or [])
    on_delete = next((v for k, v in _ON_DELETE_ORACLE.items() if f"ON DELETE {k}" in options), "no_action")
    return OracleForeignKey(columns, table.db.lower() or None, table.name.lower(),
                            _names(target) if isinstance(target, exp.Schema) else (), on_delete)


def oracle_tables(sources: Iterable[str]) -> dict[str, OracleTable]:
    """{table name: its definition} from the Oracle CREATE TABLE statements in the sources."""
    tables: dict[str, OracleTable] = {}
    for source in sources:
        try:
            statements = sqlglot.parse(source, read="oracle")
        except sqlglot.errors.ParseError:
            continue
        for stmt in statements:
            if not (isinstance(stmt, exp.Create) and (stmt.kind or "").upper() == "TABLE"
                    and isinstance(stmt.this, exp.Schema)):
                continue
            table = tables.setdefault(stmt.this.this.name.lower(), OracleTable(stmt.this.this.name.lower()))
            for item in stmt.this.expressions:
                if isinstance(item, exp.ColumnDef) and item.args.get("kind") is not None:
                    name, not_null, default = item.name.lower(), False, None
                    for constraint in item.args.get("constraints") or []:
                        kind = constraint.args.get("kind")
                        if isinstance(kind, exp.NotNullColumnConstraint) and not kind.args.get("allow_null"):
                            not_null = True
                        elif isinstance(kind, exp.PrimaryKeyColumnConstraint):
                            table.primary_key = [name]
                        elif isinstance(kind, exp.UniqueColumnConstraint):
                            table.unique.add((name,))
                        elif isinstance(kind, exp.DefaultColumnConstraint):
                            default = kind.this.sql(dialect="oracle")
                        elif isinstance(kind, exp.Reference):
                            table.foreign_keys.add(_foreign_key((name,), kind))
                    table.columns[name] = OracleColumn(name, item.args["kind"].sql(dialect="oracle"), not_null, default)
                elif isinstance(item, exp.Constraint):
                    for part in item.expressions:
                        if isinstance(part, exp.PrimaryKey):
                            table.primary_key = list(_names(part))
                        elif isinstance(part, exp.UniqueColumnConstraint) and isinstance(part.this, exp.Schema):
                            table.unique.add(_names(part.this))
                        elif isinstance(part, exp.ForeignKey):
                            table.foreign_keys.add(_foreign_key(_names(part), part.args["reference"]))
    return tables


_SEQUENCE = re.compile(r"\bCREATE\s+SEQUENCE\s+(?:(?:\"?\w+\"?)\s*\.\s*)?\"?(\w+)\"?([^;]*);", re.I)


def oracle_sequences(sources: Iterable[str]) -> dict[str, OracleSequence]:
    """{sequence name: the options its CREATE SEQUENCE declares}. SQLGlot drops these options,
    so they are read from the statement with comments and strings blanked."""
    found = {}
    for source in sources:
        try:
            text = mask_oracle(source)
        except ValueError:
            continue
        for match in _SEQUENCE.finditer(text):
            options = match.group(2).upper()

            def number(word: str) -> int | None:
                m = re.search(rf"\b{word}\s+(-?\d+)", options)
                return int(m.group(1)) if m else None

            cycle = True if re.search(r"(?<!NO)\bCYCLE\b", options) else (False if "NOCYCLE" in options else None)
            found[match.group(1).lower()] = OracleSequence(
                match.group(1).lower(), number(r"START\s+WITH"), number(r"INCREMENT\s+BY"),
                number("MINVALUE"), number("MAXVALUE"), cycle)
    return found


def _canonical_type(pg_type: str) -> str:
    """One spelling for a PostgreSQL type, so the catalogue's and the type rule's agree."""
    t = " ".join(pg_type.lower().split())
    t = re.sub(r"\(\s*", "(", re.sub(r"\s*\)", ")", re.sub(r"\s*,\s*", ",", t)))
    t = re.sub(r"^character varying", "varchar", t)
    t = re.sub(r"^character(?=\(|$)", "char", t)
    t = re.sub(r"^timestamp(\(\d\))? with time zone$", r"timestamptz\1", t)
    t = re.sub(r"^timestamp(\(\d\))? without time zone$", r"timestamp\1", t)
    t = re.sub(r"^numeric\((\d+),0\)$", r"numeric(\1)", t)
    return "char(1)" if t == "char" else t


_LITERAL = re.compile(r"^(?:'(?:[^']|'')*'|-?\d+(?:\.\d+)?|NULL)$", re.I)


def _canonical_default(expression: str | None) -> str | None:
    """A literal default without PostgreSQL's casts and brackets: "'ACTIVE'::character varying" -> "'ACTIVE'"."""
    if expression is None:
        return None
    text = expression.strip()
    while True:
        stripped = re.sub(r"::[a-z ]+(?:\(\d+(?:,\s*\d+)?\))?$", "", text, flags=re.I).strip()
        if stripped.startswith("(") and stripped.endswith(")"):
            stripped = stripped[1:-1].strip()
        if stripped == text:
            return None if text.upper() == "NULL" else text
        text = stripped


# ---------------------------------------------------------------- PreparedTargetSchema

_ON_DELETE = {"a": "no_action", "r": "restrict", "c": "cascade", "n": "set_null", "d": "set_default"}


def _privileges(lacking: list[str]) -> str:
    """'salary_audit (INSERT, DELETE)' from _missing() for one object -> 'INSERT, DELETE'."""
    return lacking[0].split(" ", 1)[1].strip("()")


def _connect(url: str):
    import psycopg

    return psycopg.connect(url, connect_timeout=5)


class PreparedTargetSchema:
    """SchemaMigrator stand-in: checks a target prepared by seed_target.py and setup_target_roles.py.

    ``connect`` opens a connection from a URL (default: psycopg.connect); tests pass their own.
    """

    name = "PreparedTargetSchema"

    def __init__(self, connect: Callable[[str], object] | None = None):
        self._connect = connect or _connect

    def migrate(
        self,
        sources: list[SourceFile],
        env: TargetEnvironment,
        *,
        expected_tables: list[str],
        expected_sequences: list[str],
    ) -> SchemaMigrationResult:
        tables = {parse_object_id(t).name: t for t in expected_tables}
        sequences = {parse_object_id(s).name: s for s in expected_sequences}
        oracle = oracle_tables(s.text for s in sources)
        oracle_seqs = oracle_sequences(s.text for s in sources)
        with self._connect(env.deployer_url.get_secret_value()) as deployer, \
                self._connect(env.runner_url.get_secret_value()) as runner:
            try:
                problems = preflight(deployer, runner, database=env.database_name,
                                     tables=sorted(tables), sequences=sorted(sequences))
                foreign = foreign_routines(deployer)
                if foreign:
                    problems.append("the target holds objects pv_deployer does not own, so the converter cannot "
                                    f"replace them: {', '.join(foreign)}: {RESET_HINT}")
                outcomes = [self._table(deployer, name, object_id, oracle, tables)
                            for name, object_id in sorted(tables.items())]
                outcomes += [self._sequence(deployer, name, object_id, oracle_seqs.get(name))
                             for name, object_id in sorted(sequences.items())]
            finally:
                deployer.rollback()
                runner.rollback()
        return SchemaMigrationResult(mode="prepared", migrator=self.name, database=env.database_name,
                                     objects=outcomes, problems=problems)

    # --- one table ------------------------------------------------------------------------

    @staticmethod
    def _oid(conn, name: str, kinds: tuple[str, ...]) -> int | None:
        row = conn.execute(
            "SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relname = %s AND c.relkind = ANY(%s)",
            (TARGET_SCHEMA, name, list(kinds))).fetchone()
        return row[0] if row else None

    def _table(self, conn, name: str, object_id: str, oracle_all: dict[str, OracleTable],
               tables: dict[str, str]) -> SchemaObjectOutcome:
        oracle = oracle_all.get(name)
        oid = self._oid(conn, name, ("r", "p"))
        if oid is None:
            return SchemaObjectOutcome(object_id=object_id, status=SchemaObjectStatus.MISSING,
                                       reason=f"not in schema {TARGET_SCHEMA}: {RESET_HINT}")
        columns = conn.execute(
            "SELECT a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull, "
            "       pg_get_expr(d.adbin, d.adrelid) "
            "  FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            " WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum", (oid,)).fetchall()
        declared = oracle.columns if oracle else {}
        found = [c[0] for c in columns]
        reasons = []
        primary = self._key_columns(conn, oid, "p")
        unique = self._key_columns(conn, oid, "u")
        foreign_keys = self._foreign_keys(conn, oid, tables)
        if not declared:
            reasons.append("no CREATE TABLE for it in the sources")
        else:
            extra = [c for c in found if c not in declared]
            absent = [c for c in declared if c not in found]
            if extra:
                reasons.append(f"column(s) not in the Oracle DDL: {', '.join(extra)}")
            if absent:
                reasons.append(f"column(s) of the Oracle DDL missing: {', '.join(absent)}")
            reasons += self._column_differences(columns, oracle)
            reasons += self._key_differences(object_id, oracle, oracle_all, primary, unique, foreign_keys, tables)
        for role, privileges in ((DEPLOYER_ROLE, DEPLOYER_TABLE_PRIVILEGES), (RUNNER_ROLE, RUNNER_TABLE_PRIVILEGES)):
            lacking = _missing(conn, role, "table", {name: oid}, privileges)
            if lacking:
                reasons.append(f"{role} lacks {_privileges(lacking)} on it: {ROLES_HINT}")

        definition = None
        if declared and all(c in declared for c in found):   # a TableDef needs every column's Oracle type
            definition = TableDef(
                object_id=object_id, target_schema=TARGET_SCHEMA, target_name=name,
                columns=[ColumnDef(name=c, oracle_type=declared[c].oracle_type, pg_type=t, nullable=nullable,
                                   default=default) for c, t, nullable, default in columns],
                primary_key=primary[0] if primary else [],
                unique=unique,
                foreign_keys=foreign_keys,
            )
        if reasons:
            return SchemaObjectOutcome(object_id=object_id, status=SchemaObjectStatus.MISMATCH,
                                       reason="; ".join(reasons), definition=definition)
        return SchemaObjectOutcome(
            object_id=object_id, status=SchemaObjectStatus.PRESENT, definition=definition,
            reason=(f"present in {TARGET_SCHEMA}.{name} as the Oracle DDL defines it (the {len(found)} columns with "
                    "their types, NOT NULL and defaults, the primary, unique and foreign keys) with the rights "
                    "setup_target_roles.py grants; prepared before the run, not migrated by it"))

    @staticmethod
    def _column_differences(columns, oracle: OracleTable) -> list[str]:
        reasons = []
        for name, pg_type, nullable, default in columns:
            column = oracle.columns.get(name)
            if column is None:
                continue
            try:
                expected = translate_type(column.oracle_type)[0]
            except ValueError:
                reasons.append(f"{name}: Oracle type {column.oracle_type} has no type rule to compare with")
            else:
                if _canonical_type(expected) != _canonical_type(pg_type):
                    reasons.append(f"{name} is {pg_type}, but {column.oracle_type} converts to {expected}")
            required = column.not_null or name in oracle.primary_key
            if required and nullable:
                reasons.append(f"{name} allows NULL, but the Oracle DDL declares it NOT NULL")
            elif not required and not nullable:
                reasons.append(f"{name} is NOT NULL, but the Oracle DDL allows NULL")
            if column.default is not None and not _LITERAL.match(column.default.strip()):
                reasons.append(f"{name}: the default {column.default} is not a literal, so the prepared-target "
                               "check cannot compare it (M3 creates such defaults)")
            elif _canonical_default(column.default) != _canonical_default(default):
                reasons.append(f"{name} has default {default or 'none'}, but the Oracle DDL has "
                               f"{column.default or 'none'}")
        return reasons

    @staticmethod
    def _key_differences(object_id: str, oracle: OracleTable, oracle_all: dict[str, OracleTable],
                         primary: list[list[str]], unique: list[list[str]], foreign_keys: list[ForeignKeyDef],
                         tables: dict[str, str]) -> list[str]:
        reasons = []
        if (primary[0] if primary else []) != oracle.primary_key:
            reasons.append(f"primary key is ({', '.join(primary[0]) if primary else 'none'}), but the Oracle DDL "
                           f"has ({', '.join(oracle.primary_key) or 'none'})")
        if {tuple(u) for u in unique} != oracle.unique:
            reasons.append(f"unique keys {sorted(map(list, (tuple(u) for u in unique)))} differ from the Oracle "
                           f"DDL's {sorted(map(list, oracle.unique))}")
        schema = parse_object_id(object_id).schema
        expected = set()
        for fk in oracle.foreign_keys:
            ref = f"table:{(fk.ref_schema or schema)}.{fk.ref_table}"
            if ref not in tables.values():
                reasons.append(f"foreign key ({', '.join(fk.columns)}) references {fk.ref_schema or schema}."
                               f"{fk.ref_table}, which is not a table of the upload")
            # REFERENCES t without columns means t's primary key.
            referenced = oracle_all.get(fk.ref_table) if fk.ref_schema in (None, schema) else None
            ref_columns = fk.ref_columns or tuple(referenced.primary_key if referenced else ())
            expected.add((fk.columns, ref, ref_columns, fk.on_delete))
        actual = {(tuple(k.columns), k.ref_table, tuple(k.ref_columns), k.on_delete) for k in foreign_keys}
        for ref in sorted(actual - expected):
            reasons.append(f"foreign key ({', '.join(ref[0])}) -> {ref[1]} ({', '.join(ref[2])}) on delete {ref[3]} "
                           "is not in the Oracle DDL")
        for ref in sorted(expected - actual):
            if ref[1] in tables.values():
                reasons.append(f"foreign key ({', '.join(ref[0])}) -> {ref[1]} ({', '.join(ref[2])}) on delete "
                               f"{ref[3]} of the Oracle DDL is missing")
        return reasons

    @staticmethod
    def _key_columns(conn, oid: int, contype: str) -> list[list[str]]:
        rows = conn.execute(
            "SELECT array_agg(a.attname ORDER BY k.ord) FROM pg_constraint c "
            "CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) "
            "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum "
            "WHERE c.conrelid = %s AND c.contype = %s GROUP BY c.oid ORDER BY c.conname", (oid, contype)).fetchall()
        return [list(r[0]) for r in rows]

    @staticmethod
    def _foreign_keys(conn, oid: int, tables: dict[str, str]) -> list[ForeignKeyDef]:
        rows = conn.execute(
            "SELECT c.conname, rn.nspname, r.relname, c.confdeltype, "
            "       (SELECT array_agg(a.attname ORDER BY k.ord) FROM unnest(c.conkey) WITH ORDINALITY k(n, ord) "
            "          JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.n), "
            "       (SELECT array_agg(a.attname ORDER BY k.ord) FROM unnest(c.confkey) WITH ORDINALITY k(n, ord) "
            "          JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.n) "
            "  FROM pg_constraint c JOIN pg_class r ON r.oid = c.confrelid "
            "  JOIN pg_namespace rn ON rn.oid = r.relnamespace "
            " WHERE c.conrelid = %s AND c.contype = 'f' ORDER BY c.conname", (oid,)).fetchall()
        keys = []
        for name, ref_schema, ref_name, on_delete, columns, ref_columns in rows:
            # Only a table in the target schema maps to an upload table; any other schema keeps
            # its own name, so it never passes for a local table (Aarsh's v0.7 review, 19 Nov).
            if ref_schema == TARGET_SCHEMA and ref_name in tables:
                ref_table = tables[ref_name]
            else:
                ref_table = f"table:{ref_schema}.{ref_name}"
            keys.append(ForeignKeyDef(name=name, columns=list(columns), ref_table=ref_table,
                                      ref_columns=list(ref_columns), on_delete=_ON_DELETE[on_delete]))
        return keys

    # --- one sequence ---------------------------------------------------------------------

    def _sequence(self, conn, name: str, object_id: str, oracle: OracleSequence | None) -> SchemaObjectOutcome:
        oid = self._oid(conn, name, ("S",))
        if oid is None:
            return SchemaObjectOutcome(object_id=object_id, status=SchemaObjectStatus.MISSING,
                                       reason=f"not in schema {TARGET_SCHEMA}: {RESET_HINT}")
        start, increment, minimum, maximum, cycle = conn.execute(
            "SELECT seqstart, seqincrement, seqmin, seqmax, seqcycle FROM pg_sequence WHERE seqrelid = %s",
            (oid,)).fetchone()
        definition = SequenceDef(object_id=object_id, target_schema=TARGET_SCHEMA, target_name=name,
                                 start_with=start, increment_by=increment, min_value=minimum, max_value=maximum,
                                 cycle=cycle)
        reasons = []
        if oracle is None:
            reasons.append("no CREATE SEQUENCE for it in the sources")
        else:
            increment_by = oracle.increment_by if oracle.increment_by is not None else 1
            # Oracle's defaults for an ascending sequence: MINVALUE 1, START WITH MINVALUE, NOCYCLE.
            min_value = oracle.min_value if oracle.min_value is not None else (1 if increment_by > 0 else None)
            start_with = oracle.start_with if oracle.start_with is not None else min_value
            expected = {"START WITH": start_with, "INCREMENT BY": increment_by, "MINVALUE": min_value,
                        "CYCLE": bool(oracle.cycle)}
            if oracle.max_value is not None:
                expected["MAXVALUE"] = oracle.max_value
            actual = {"START WITH": start, "INCREMENT BY": increment, "MINVALUE": minimum, "CYCLE": cycle,
                      "MAXVALUE": maximum}
            for option, value in expected.items():
                if value is not None and actual[option] != value:
                    reasons.append(f"{option} is {actual[option]}, but the Oracle DDL gives {value}")
        lacking = _missing(conn, RUNNER_ROLE, "sequence", {name: oid}, RUNNER_SEQUENCE_PRIVILEGES)
        if lacking:
            reasons.append(f"{RUNNER_ROLE} lacks {_privileges(lacking)} on it: {ROLES_HINT}")
        if reasons:
            return SchemaObjectOutcome(object_id=object_id, status=SchemaObjectStatus.MISMATCH, definition=definition,
                                       reason="; ".join(reasons))
        return SchemaObjectOutcome(
            object_id=object_id, status=SchemaObjectStatus.PRESENT, definition=definition,
            reason=(f"present in {TARGET_SCHEMA}.{name} with the options of the Oracle DDL and the rights "
                    "setup_target_roles.py grants; prepared before the run, not migrated by it"))

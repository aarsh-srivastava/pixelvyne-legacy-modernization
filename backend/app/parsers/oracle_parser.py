
"""First-pass Oracle SQL/PLSQL object discovery and IR extraction.

This is a lightweight MVP parser. It identifies database objects and extracts
basic metadata using regular expressions. It is not a complete PL/SQL compiler.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from app.config import settings
from app.contracts import (
    IRObject,
    ObjectType,
    Param,
    ParamMode,
    check_unique_object_ids,
    default_schema,
    make_object_id,
    make_unresolved_id,
    normalize_identifier,
    parse_object_id,
    TriggerInfo,
)
from app.migration.features import mask_oracle

DEFAULT_SCHEMA = default_schema(settings.ORACLE_USER)

# ---------------------------------------------------------------------------
# 1. Object recognition
# ---------------------------------------------------------------------------

CREATE_PATTERN = re.compile(
    r"""
    \bCREATE\s+
    (?:OR\s+REPLACE\s+)?
    (?:(?:EDITIONABLE|NONEDITIONABLE)\s+)?
    (?P<kind>
        PACKAGE\s+BODY |
        PACKAGE |
        FUNCTION |
        PROCEDURE |
        TRIGGER |
        VIEW |
        TABLE |
        SEQUENCE
    )
    \s+
    (?:(?P<schema>"[^"]+"|[\w$#]+)\s*\.\s*)?   # CREATE legacy.x: the schema, if written (v11)
    (?P<name>"[^"]+"|[\w$#]+)
    """,
    re.IGNORECASE | re.VERBOSE,
)

PLSQL_TYPES = {"PACKAGE BODY", "PACKAGE", "FUNCTION", "PROCEDURE", "TRIGGER"}

TYPE_MAP = {
    "TABLE": ObjectType.TABLE,
    "VIEW": ObjectType.VIEW,
    "SEQUENCE": ObjectType.SEQUENCE,
    "FUNCTION": ObjectType.FUNCTION,
    "PROCEDURE": ObjectType.PROCEDURE,
    "TRIGGER": ObjectType.TRIGGER,
    "PACKAGE": ObjectType.PACKAGE,
    "PACKAGE BODY": ObjectType.PACKAGE,
}


# ---------------------------------------------------------------------------
# 2. Comment handling
# ---------------------------------------------------------------------------

def remove_comments(sql: str) -> str:
    """Remove SQL line and block comments without removing quoted text."""

    pattern = re.compile(
        r"""
        ('(?:''|[^'])*')       # single-quoted string
        |("(?:\"\"|[^"])*")    # double-quoted identifier/string
        |(--[^\n]*)            # line comment
        |(/\*.*?\*/)           # block comment
        """,
        re.DOTALL | re.VERBOSE,
    )

    def replace(match: re.Match) -> str:
        if match.group(1) or match.group(2):
            return match.group(0)
        # Keep a block comment's line breaks, so line numbers after it stay right.
        return "\n" * match.group(0).count("\n")

    return pattern.sub(replace, sql)


# ---------------------------------------------------------------------------
# 3. SQL/PLSQL statement splitting
# ---------------------------------------------------------------------------

def split_statements(sql: str) -> list[str]:
    """Split SQL statements on semicolons and PL/SQL units on slash lines."""

    return [statement for statement, _ in split_statements_with_lines(sql)]


def split_statements_with_lines(sql: str) -> list[tuple[str, int]]:
    """Like split_statements, with the 1-based line each statement starts on."""

    statements: list[tuple[str, int]] = []
    buffer: list[str] = []
    start = 1

    def flush() -> None:
        statement = "\n".join(buffer).strip()
        if statement:
            first = next(i for i, text in enumerate(buffer) if text.strip())
            statements.append((statement, start + first))
        buffer.clear()

    for number, line in enumerate(sql.splitlines(), start=1):
        if not buffer:
            start = number
        if line.strip() == "/":
            flush()
            continue

        buffer.append(line)
        current = "\n".join(buffer).strip()

        # PL/SQL units are terminated by a slash on its own line.
        # Do not split their internal semicolons.
        if re.match(
            r"(?is)^CREATE\s+(?:OR\s+REPLACE\s+)?"
            r"(?:(?:EDITIONABLE|NONEDITIONABLE)\s+)?"
            r"(?:PACKAGE(?:\s+BODY)?|FUNCTION|PROCEDURE|TRIGGER)\b",
            current,
        ):
            continue

        # Ordinary SQL DDL statements end with semicolons.
        if current.endswith(";"):
            flush()

    flush()
    return statements


# ---------------------------------------------------------------------------
# 4. General extraction helpers
# ---------------------------------------------------------------------------

def normalize_name(name: str) -> str:
    """Normalize an Oracle identifier using the shared canonical contract."""
    return normalize_identifier(name)

def get_object_kind(match: re.Match) -> str:
    return re.sub(r"\s+", " ", match.group("kind").upper())


def split_top_level_commas(value: str) -> list[str]:
    """Split a parameter list without splitting inside nested parentheses."""

    parts: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None

    for index, char in enumerate(value):
        if quote:
            if char == quote:
                quote = None
            continue

        if char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(value[start:index].strip())
            start = index + 1

    final = value[start:].strip()
    if final:
        parts.append(final)

    return parts


# ---------------------------------------------------------------------------
# 5. Parameter and return type extraction
# ---------------------------------------------------------------------------


def extract_params(source: str, kind: str) -> list[Param]:
    """Extract routine parameters using balanced-parenthesis scanning."""

    if kind not in {"FUNCTION", "PROCEDURE"}:
        return []

    header_match = re.search(
        r"\b" + kind + r"\s+(?:[\w$#\"]+\s*\.\s*)?[\w$#\"]+\s*\(",
        source,
        re.IGNORECASE | re.DOTALL,
    )

    if not header_match:
        return []

    # Locate the opening parenthesis of the parameter list.
    opening_index = source.find("(", header_match.start())
    depth = 0
    quote = None
    closing_index = None
    index = opening_index

    while index < len(source):
        char = source[index]

        if quote == "'":
            if char == "'":
                # Oracle escapes single quotes by doubling them.
                if index + 1 < len(source) and source[index + 1] == "'":
                    index += 2
                    continue
                quote = None

        elif quote == '"':
            if char == '"':
                quote = None

        elif char in {"'", '"'}:
            quote = char

        elif char == "(":
            depth += 1

        elif char == ")":
            depth -= 1

            if depth == 0:
                closing_index = index
                break

        index += 1

    if closing_index is None:
        return []

    parameter_text = source[opening_index + 1:closing_index]
    params: list[Param] = []

    for declaration in split_top_level_commas(parameter_text):
        declaration = declaration.strip()

        if not declaration:
            continue

        param_match = re.match(
            r"""
            (?P<name>"?[\w$#]+"?)
            \s+
            (?:(?P<mode>IN\s+OUT|OUT|IN)\s+)?
            (?P<type>[\w$#]+(?:\s*\([^)]*\))?)
            (?:\s+(?:DEFAULT|:=)\s+(?P<default>.+))?
            """,
            declaration,
            re.IGNORECASE | re.VERBOSE | re.DOTALL,
        )

        if not param_match:
            continue

        mode_text = re.sub(
            r"\s+",
            " ",
            (param_match.group("mode") or "IN").upper(),
        )

        mode = {
            "IN": ParamMode.IN,
            "OUT": ParamMode.OUT,
            "IN OUT": ParamMode.IN_OUT,
        }[mode_text]

        default_expr = param_match.group("default")

        params.append(
            Param(
                name=normalize_name(param_match.group("name")),
                oracle_type=param_match.group("type").strip().upper(),
                mode=mode,
                has_default=default_expr is not None,
                default_expr=default_expr.strip() if default_expr else None,
            )
        )

    return params


def extract_return_type(source: str, kind: str) -> str | None:
    if kind != "FUNCTION":
        return None

    match = re.search(
        r"\bRETURN\s+([\w$#]+(?:\s*\([^)]*\))?)",
        source,
        re.IGNORECASE,
    )

    return match.group(1).strip().upper() if match else None


# ---------------------------------------------------------------------------
# 6. Table reads, writes and Oracle features
# ---------------------------------------------------------------------------



def _blank_strings(source: str) -> str:
    """``source`` with string literals and comments blanked, offsets kept (PR 1, G2).

    Uses the same masker as feature detection, so q-quoted strings (``q'[it's]'``,
    ``nq'{...}'``) are handled too. Unreadable source (an unterminated string) falls back
    to blanking ordinary literals; feature detection routes such an object to review.
    """
    try:
        return mask_oracle(source, keep_positions=True)
    except ValueError:
        return re.sub(r"'(?:''|[^'])*'", "''", source)


def extract_table_references(source: str) -> tuple[list[str], list[str]]:
    """Extract table read and write references from Oracle SQL/PLSQL.

    Schema-qualified names are preserved, normalized.
    Example: HR.EMPLOYEES -> hr.employees
    Resolution reads the names as written instead (_raw_table_references), because the
    quoting matters there: "Employees" is not EMPLOYEES (PR 1 v11).
    """

    def extract_object_name(value: str) -> str:
        """Normalize identifiers while preserving schema qualification."""
        return _normalized(split_reference(value))

    return _table_references(source, extract_object_name)


def _table_references(source: str, name_of) -> tuple[list[str], list[str]]:
    """Sorted (reads, writes), each name passed through ``name_of``."""

    reads: set[str] = set()
    writes: set[str] = set()

    # Words inside string literals are not table references: a message such as
    # 'Loaded from staging' must not become a dependency on "staging" (PR 1, G2).
    source = _blank_strings(source)

    # Support regular and quoted Oracle identifiers.
    identifier = r'(?:"[^"]+"|[\w$#]+)'
    qualified_name = rf'{identifier}(?:\s*\.\s*{identifier})?'

    # Tables referenced by FROM and JOIN.
    read_patterns = [
        re.compile(
            rf"\bFROM\s+({qualified_name})",
            re.IGNORECASE,
        ),
        re.compile(
            rf"\bJOIN\s+({qualified_name})",
            re.IGNORECASE,
        ),
    ]

    # Tables modified by DML or Oracle trigger declarations.
    write_patterns = [


        # Normal UPDATE: UPDATE employees SET ...
        re.compile(
            # Not a trigger's UPDATE OF / UPDATE ON / UPDATE OR (reserved words, never
            # table names; v10 read ON and OR as tables, v11).
            rf"\bUPDATE\s+(?!(?:OF|ON|OR)\b)({qualified_name})",
            re.IGNORECASE,
        ),

        # INSERT INTO employees
        re.compile(
            rf"\bINSERT\s+INTO\s+({qualified_name})",
            re.IGNORECASE,
        ),

        # DELETE FROM employees
        re.compile(
            rf"\bDELETE\s+FROM\s+({qualified_name})",
            re.IGNORECASE,
        ),

        # MERGE INTO employees
        re.compile(
            rf"\bMERGE\s+INTO\s+({qualified_name})",
            re.IGNORECASE,
        ),
    ]

    for pattern in read_patterns:
        for match in pattern.finditer(source):
            reads.add(name_of(match.group(1)))

    for pattern in write_patterns:
        for match in pattern.finditer(source):
            writes.add(name_of(match.group(1)))

    return sorted(reads), sorted(writes)

# ---------------------------------------------------------------------------
# 7. Dependency extraction
# ---------------------------------------------------------------------------

def extract_dependencies(
    source: str,
    known_objects: set[str],
    current_object: str,
) -> list[str]:
    """Find references to other discovered database objects."""

    source_normalized = source.lower()
    dependencies = []

    for name in known_objects:
        if name == current_object:
            continue

        if re.search(
            rf"(?<![\w$#]){re.escape(name)}(?![\w$#])",
            source_normalized,
        ):
            dependencies.append(name)

    return sorted(dependencies)


def extract_trigger_metadata(source: str) -> dict | None:
    """Extract the firing conditions from an Oracle CREATE TRIGGER statement.

    Returns the table reference, timing, DML events, UPDATE OF columns,
    and row-level setting. Canonical table resolution happens separately.
    """

    trigger_match = re.search(
        r"\b(BEFORE|AFTER|INSTEAD\s+OF)\b"
        r"(?P<events>.*?)"
        r"\bON\s+"
        r'(?P<table>"[^"]+"|[\w$#]+)'
        r'(?:\s*\.\s*("[^"]+"|[\w$#]+))?',
        source,
        re.IGNORECASE | re.DOTALL,
    )

    if not trigger_match:
        return None

    timing_text = re.sub(
        r"\s+", " ", trigger_match.group(1).strip().lower()
    )

    timing = timing_text.replace(" ", "_")

    if timing not in {"before", "after", "instead_of"}:
        return None

    event_clause = trigger_match.group("events")

    events = list(
        dict.fromkeys(
            event.lower()
            for event in re.findall(
                r"\b(INSERT|UPDATE|DELETE)\b",
                event_clause,
                re.IGNORECASE,
            )
        )
    )

    if not events:
        return None

    update_columns = []

    update_match = re.search(
        r"\bUPDATE\s+OF\s+(.+?)"
        r"(?=\s+OR\s+(?:INSERT|UPDATE|DELETE)\b|$)",
        event_clause,
        re.IGNORECASE | re.DOTALL,
    )

    if update_match:
        update_columns = [
            normalize_name(column.strip())
            for column in update_match.group(1).split(",")
            if column.strip()
        ]


    table_reference = re.sub(
        r"\s+", "", trigger_match.group("table")
    )

    # Include the optional schema qualification.
    if trigger_match.group(4):
        table_reference += "." + re.sub(
            r"\s+", "", trigger_match.group(4)
        )


    return {
        "table_reference": table_reference,
        "timing": timing,
        "events": events,
        "update_columns": update_columns,
        "for_each_row": bool(
            re.search(r"\bFOR\s+EACH\s+ROW\b", source, re.IGNORECASE)
        ),
    }


# ---------------------------------------------------------------------------
# 7b. Oracle name resolution (PR 1 v11)
# ---------------------------------------------------------------------------
#
# A reference resolves to an uploaded object only when Oracle would resolve it there:
# unquoted names are stored upper-case and quoted names exactly ("EMPLOYEES" is EMPLOYEES,
# "Employees" is not), and a schema-qualified name resolves only in the upload's schema
# (hr.employees is HR's table, never the upload's). The same rules apply to tables and
# views, sequences, package members and standalone routines, and references.py uses this
# resolver too, so a line is placed on the object the parser linked.

_CHAIN_ID = r'(?:"[^"]+"|[A-Za-z_][\w$#]*)'
#: A dotted name of two or more parts, not starting in the middle of another name.
_DOTTED_CHAIN = re.compile(rf'(?<![\w$#."]){_CHAIN_ID}(?:\s*\.\s*{_CHAIN_ID})+')
_PARTS = re.compile(r'"[^"]+"|[^\s."]+')
_UNQUOTED_FORM = re.compile(r"^[A-Z_][A-Z0-9_$#]*$")
_SEQUENCE_PSEUDOCOLUMNS = {"NEXTVAL", "CURRVAL"}


def oracle_canonical(identifier: str) -> str:
    """The name Oracle stores: unquoted names fold to upper case, quoted names stay exact.

    Matches features._canonical (the G3 allow-list), so both checks compare names alike.
    """
    identifier = identifier.strip()
    if len(identifier) >= 2 and identifier[0] == identifier[-1] == '"':
        return identifier[1:-1]
    return identifier.upper()


def split_reference(reference: str) -> list[str]:
    """The parts of a dotted name as written, quotes kept: 'hr . "Emp"' -> ['hr', '"Emp"']."""
    return _PARTS.findall(reference)


def _normalized(parts: list[str]) -> str:
    return ".".join(normalize_name(part) for part in parts)


def name_regex(canonical: str) -> str:
    """A regex for every spelling Oracle reads as ``canonical``.

    The quoted spelling always; the unquoted spelling, in any case, only when the canonical
    name is one an unquoted name can produce (upper case). Compile it without IGNORECASE.
    """
    alternatives = [re.escape(f'"{canonical}"')]
    if _UNQUOTED_FORM.match(canonical):
        alternatives.append(rf"(?i:{re.escape(canonical)})(?![\w$#])")
    return "(?:" + "|".join(alternatives) + ")"


UPLOAD_SCHEMA = oracle_canonical(DEFAULT_SCHEMA)


class NameResolver:
    """Resolves references as written to uploaded object ids, by Oracle's rules.

    ``declared_names`` maps object_id -> the Oracle name as declared (ParseResult.declared_names).
    Without it, every object is taken as declared unquoted (its name upper-cased).
    """

    def __init__(self, objects: Iterable[IRObject], declared_names: dict[str, str] | None = None):
        declared_names = declared_names or {}
        objects = list(objects)
        self.schema = UPLOAD_SCHEMA
        self.tables: dict[str, str] = {}
        self.sequences: dict[str, str] = {}
        self.packages: dict[str, str] = {}
        self.package_canonical: dict[str, str] = {}   # normalized package name -> canonical
        self.routines: dict[tuple[str | None, str], list[str]] = defaultdict(list)

        def declared(obj: IRObject) -> str:
            return declared_names.get(obj.object_id) or obj.name.upper()

        for obj in objects:
            if obj.type == ObjectType.PACKAGE:
                self.packages[declared(obj)] = obj.object_id
                self.package_canonical[obj.name] = declared(obj)
        for obj in objects:
            if obj.type in {ObjectType.TABLE, ObjectType.VIEW}:
                self.tables[declared(obj)] = obj.object_id
            elif obj.type == ObjectType.SEQUENCE:
                self.sequences[declared(obj)] = obj.object_id
            elif obj.type in {ObjectType.FUNCTION, ObjectType.PROCEDURE}:
                package = self.package_of(obj.package)
                self.routines[(package, declared(obj))].append(obj.object_id)

    def package_of(self, package: str | None) -> str | None:
        """The canonical name of a package, from an IRObject's (normalized) package field."""
        if package is None:
            return None
        return self.package_canonical.get(package, package.upper())

    def _in_schema(self, parts: list[str], size: int) -> list[str] | None:
        """The canonical name parts without the schema, when the reference is in the upload's
        schema; None when it names another schema (or has too many parts)."""
        canonical = [oracle_canonical(p) for p in parts]
        if len(canonical) == size + 1:
            return canonical[1:] if canonical[0] == self.schema else None
        return canonical if len(canonical) == size else None

    def _unresolved(self, parts: list[str]) -> str:
        return make_unresolved_id(_normalized(parts), default_schema=DEFAULT_SCHEMA)

    def table(self, reference: str) -> str:
        """A table or view reference ([schema.]name): its object_id, or an unresolved id."""
        parts = split_reference(reference)
        name = self._in_schema(parts, 1)
        if name and name[0] in self.tables:
            return self.tables[name[0]]
        return self._unresolved(parts)

    def sequence(self, parts: list[str]) -> str:
        """A sequence ([schema.]name, without .NEXTVAL): its object_id, or an unresolved id."""
        name = self._in_schema(parts, 1)
        if name and name[0] in self.sequences:
            return self.sequences[name[0]]
        return self._unresolved(parts)

    def routine(self, name: str, package: str | None) -> str | None:
        """A routine by canonical name and canonical package (None: standalone), if unique."""
        candidates = self.routines.get((package, name), [])
        return candidates[0] if len(candidates) == 1 else None

    def dotted(self, parts: list[str]) -> str | None:
        """A dotted name that is not a sequence pseudocolumn (see sequence()).

        * [schema.]pkg.member...: the member's id, or the package's for a constant or state;
        * schema.routine and schema.pkg: that routine or package, in the upload's schema;
        * other_schema.pkg.member, where pkg is an uploaded package's name: another schema's
          package, kept unresolved (external) rather than linked to the upload's package;
        * anything else (alias.column, record.field, an unknown package): not a reference
          this resolver can place, so None, as before.
        """
        canonical = [oracle_canonical(p) for p in parts]
        if len(canonical) >= 2 and canonical[0] == self.schema:
            rest = canonical[1:]
            if rest[0] in self.packages:
                return self._member(rest)
            if len(rest) == 1:
                return self.routine(rest[0], None)
            return None
        if canonical[0] in self.packages:
            return self._member(canonical)
        if len(canonical) >= 3 and canonical[1] in self.packages:
            return self._unresolved(parts[:3])
        return None

    def _member(self, canonical: list[str]) -> str:
        package_id = self.packages[canonical[0]]
        if len(canonical) == 1:
            return package_id
        return self.routine(canonical[1], canonical[0]) or package_id

    def resolve(self, reference: str, kind: str, current_package: str | None = None) -> str | None:
        """What ``reference`` (as written, without .NEXTVAL or the call's parenthesis) points
        to, read as an object of ``kind`` (an ObjectType value). None: it is not one."""
        parts = split_reference(reference)
        if not parts:
            return None
        if kind in {"table", "view"}:
            return self.table(reference)
        if kind == "sequence":
            return self.sequence(parts)
        if len(parts) == 1:
            name = oracle_canonical(parts[0])
            if kind == "package":
                return self.packages.get(name)
            package = self.package_of(current_package)
            return (package and self.routine(name, package)) or self.routine(name, None)
        return self.dotted(parts)


def _raw_table_references(source: str) -> list[str]:
    """Table and view references as written (quotes kept), for resolution."""
    reads, writes = _table_references(source, lambda value: re.sub(r"\s+", "", value))
    return [*reads, *writes]


_FK_REFERENCE = re.compile(r'\bREFERENCES\s+((?:"[^"]+"|[\w$#]+)(?:\s*\.\s*(?:"[^"]+"|[\w$#]+))?)', re.IGNORECASE)


def _foreign_key_references(source: str) -> list[str]:
    """Tables a CREATE TABLE's foreign keys reference, as written."""
    return [re.sub(r"\s+", "", m.group(1)) for m in _FK_REFERENCE.finditer(_blank_strings(source))]


def resolve_object_dependencies(
    objects: list[IRObject],
    declared_names: dict[str, str] | None = None,
) -> None:
    """Resolve extracted SQL references to canonical object IDs.

    Mutates each IRObject.depends_on after all objects have been discovered.
    Unknown references are retained as unresolved IDs. ``declared_names`` is
    ParseResult.declared_names; see NameResolver for the rules.
    """

    resolver = NameResolver(objects, declared_names)

    for obj in objects:
        dependencies: set[str] = set()
        # Every scan below reads the code with strings and comments blanked (q-quotes too),
        # so a name inside a message never becomes a dependency (PR 1, G2).
        code = _blank_strings(obj.source_code)

        # 1. Table reads and writes, including foreign-key references, as written: the
        # quoting and the schema decide what they resolve to (v11). reads_tables and
        # writes_tables entries not found in the source (an IRObject built by hand) are
        # resolved as unquoted names.
        references = _raw_table_references(obj.source_code)
        if obj.type == ObjectType.TABLE:
            references += _foreign_key_references(obj.source_code)
        seen = {_normalized(split_reference(r)) for r in references}
        references += [r for r in [*obj.reads_tables, *obj.writes_tables] if r not in seen]
        for reference in references:
            dependency = resolver.table(reference)
            if dependency != obj.object_id:
                dependencies.add(dependency)

        # 2 and 3. Dotted names: sequence pseudocolumns ([schema.]seq.NEXTVAL / CURRVAL) and
        # package-qualified references ([schema.]pkg.member, other_schema.pkg.member).
        for match in _DOTTED_CHAIN.finditer(code):
            parts = split_reference(match.group(0))
            if oracle_canonical(parts[-1]) in _SEQUENCE_PSEUDOCOLUMNS:
                if len(parts) > 3:
                    continue
                dependency = resolver.sequence(parts[:-1])
            else:
                dependency = resolver.dotted(parts)
            if dependency and dependency != obj.object_id:
                dependencies.add(dependency)

        # 4. Unqualified routine calls inside the same package, with or without arguments
        # (flush(1) or flush;), as in 4b. A qualified other_pkg.flush( is not this package's
        # member; step 3 handles qualified names (PR 1, G2). Names match by Oracle's rules:
        # "FLUSH"( is flush(, "Flush"( is not (v11).
        current_package = resolver.package_of(obj.package)

        if current_package:
            for (package_name, routine_name), candidates in resolver.routines.items():
                if package_name != current_package:
                    continue
                if re.search(rf"(?<![\w$#.\"]){name_regex(routine_name)}\s*(?:\(|;)", code):
                    dependency = resolver.routine(routine_name, current_package)
                    if dependency and dependency != obj.object_id:
                        dependencies.add(dependency)

        # 4b. Calls to standalone (non-package) functions and procedures, with or
        # without arguments: calc_bonus(102) or log_run;. (PR 1, G2 and G10 groundwork.)
        for (package_name, routine_name), candidates in resolver.routines.items():
            if package_name is not None or len(candidates) != 1:
                continue
            if candidates[0] == obj.object_id:
                continue
            if re.search(rf"(?<![\w$#.\"]){name_regex(routine_name)}\s*(?:\(|;)", code):
                dependencies.add(candidates[0])

        # 5. Trigger attachment is a dependency, not a body write.
        if obj.type == ObjectType.TRIGGER:
            metadata = extract_trigger_metadata(obj.source_code)

            if metadata:
                dependency = resolver.table(metadata["table_reference"])

                if dependency != obj.object_id:
                    dependencies.add(dependency)

                # TriggerInfo must reference a discovered table or view.
                # Missing targets remain unresolved dependencies.
                if not dependency.startswith("unresolved:"):
                    obj.trigger = TriggerInfo(
                        table=dependency,
                        timing=metadata["timing"],
                        events=metadata["events"],
                        update_columns=metadata["update_columns"],
                        for_each_row=metadata["for_each_row"],
                    )
        # Store the resolved canonical dependencies on the IR object.
        obj.depends_on = sorted(dependencies)


ORACLE_FEATURE_PATTERNS = {
    "NVL": r"\bNVL\s*\(",
    "DECODE": r"\bDECODE\s*\(",
    "ROWNUM": r"\bROWNUM\b",
    "SYSDATE": r"\bSYSDATE\b",
    "CONNECT_BY": r"\bCONNECT\s+BY\b",
    "START_WITH": r"\bSTART\s+WITH\b",
    "SYS_CONNECT_BY_PATH": r"\bSYS_CONNECT_BY_PATH\s*\(",
    "SEQUENCE_NEXTVAL": r"\b[\w$#]+\.NEXTVAL\b",
    "SEQUENCE_CURRVAL": r"\b[\w$#]+\.CURRVAL\b",
    "DUAL": r"\bFROM\s+DUAL\b",
    "PRAGMA": r"\bPRAGMA\b",
    "EXCEPTION": r"\bEXCEPTION\b",
    "BULK_COLLECT": r"\bBULK\s+COLLECT\b",
    "FORALL": r"\bFORALL\b",
    "AUTONOMOUS_TRANSACTION": r"\bAUTONOMOUS_TRANSACTION\b",
}


def extract_oracle_features(source: str) -> list[str]:
    """Identify Oracle-specific SQL and PL/SQL constructs."""

    features = []

    for feature, pattern in ORACLE_FEATURE_PATTERNS.items():
        if re.search(pattern, source, re.IGNORECASE):
            features.append(feature)

    return sorted(features)

def extract_package_members(
    source: str,
) -> list[dict]:
    """Extract implemented functions and procedures from a package body.

    Each returned member contains its kind, name and complete source block.
    Package specifications are intentionally not used: implementations in
    the body are the source of truth for member IR objects.
    """
        # The merged package source contains both the specification
    # and body. Extract members only from the package body.
    body_match = re.search(
        r"\bCREATE\s+(?:OR\s+REPLACE\s+)?"
        r"(?:(?:EDITIONABLE|NONEDITIONABLE)\s+)?"
        r"PACKAGE\s+BODY\b",
        source,
        re.IGNORECASE,
    )

    if not body_match:
        return []

    source = source[body_match.start():]

    members = []

    # Match routine declarations at the beginning of a package-body line.
    member_pattern = re.compile(
        r"""
        ^[ \t]*
        (?P<kind>FUNCTION|PROCEDURE)
        \s+
        (?P<name>"[^"]+"|[\w$#]+)
        (?![\w$#])   # not \b, which never follows a closing quote: "Mx" was dropped (v11)
        """,
        re.IGNORECASE | re.MULTILINE | re.VERBOSE,
    )

    for match in member_pattern.finditer(source):
        kind = match.group("kind").upper()
        name = normalize_name(match.group("name"))
        token = match.group("name")

        # Find the end of this routine implementation.
        end_pattern = re.compile(
            rf"^\s*END\s+{re.escape(match.group('name'))}\s*;",
            re.IGNORECASE | re.MULTILINE,
        )

        end_match = end_pattern.search(source, match.end())

        if not end_match:
            continue

        member_source = source[match.start():end_match.end()]

        # Only accept actual implementations containing IS or AS.
        if not re.search(r"\b(?:IS|AS)\b", member_source, re.IGNORECASE):
            continue

        members.append(
            {
                "kind": kind,
                "name": name,
                "declared": oracle_canonical(token),
                "source": member_source,
                # Offset of the member in the source passed in (for line numbers).
                "start": body_match.start() + match.start(),
            }
        )

    return members

# ---------------------------------------------------------------------------
# 8. Main parser
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceLocation:
    """Where an object's definition starts: file and 1-based line."""

    file: str
    line: int


@dataclass(frozen=True)
class DuplicateDefinition:
    """One object defined more than once (G1)."""

    object_id: str
    places: tuple[SourceLocation, ...]   # sorted by file, then line
    identical: bool                      # every definition has the same text


class DuplicateDefinitionError(ValueError):
    """The upload defines the same object more than once.

    Which definition is meant cannot be guessed safely, so nothing is parsed.
    Every duplicate is listed, sorted, so the message is the same whatever the
    upload order. One package spec plus one package body is not a duplicate.
    """

    def __init__(self, duplicates: list[DuplicateDefinition]):
        self.duplicates = sorted(duplicates, key=lambda d: d.object_id)
        lines = ["Duplicate definitions: each object must be defined once."]
        for dup in self.duplicates:
            places = "; ".join(f"{p.file} line {p.line}" for p in dup.places)
            same = " (identical)" if dup.identical else ""
            lines.append(f"  {dup.object_id} is defined in {len(dup.places)} places: {places}{same}")
        lines.append("Remove the extra definitions and run again.")
        super().__init__("\n".join(lines))


@dataclass(frozen=True)
class ForeignSchemaDefinition:
    name: str              # as written: hr.x, "legacy".x
    place: SourceLocation


class ForeignSchemaDefinitionError(ValueError):
    """The upload creates objects in a schema other than the upload's (PR 1 v11).

    Object ids carry the upload's schema, so CREATE hr.x cannot be filed as legacy.x
    without changing what it is. Schemas compare as Oracle compares names: "legacy" (quoted
    lower case) is not LEGACY. Every such CREATE is listed, sorted by file and line.
    """

    def __init__(self, definitions: list[ForeignSchemaDefinition]):
        self.definitions = sorted(definitions, key=lambda d: (d.place.file, d.place.line, d.name))
        lines = [f"Objects in another schema: this upload is for schema {DEFAULT_SCHEMA}."]
        lines += [f"  {d.place.file} line {d.place.line}: {d.name}" for d in self.definitions]
        lines.append("Upload each schema separately, or remove the schema prefix.")
        super().__init__("\n".join(lines))


@dataclass(frozen=True)
class ParseResult:
    """The parser's output: IR objects plus where each one starts.

    ``declared_names`` maps object_id -> the Oracle name as declared (EMPLOYEES for an
    unquoted employees, Employees for "Employees"); object ids are lower-case by contract,
    so this is where the quoting survives (PR 1 v11). See NameResolver.
    """

    objects: list[IRObject]
    locations: dict[str, SourceLocation] = field(default_factory=dict)  # object_id -> start
    declared_names: dict[str, str] = field(default_factory=dict)


def _line_at(text: str, index: int, first_line: int) -> int:
    return first_line + text.count("\n", 0, index)


def _find_duplicates(discovered: list[dict]) -> list[DuplicateDefinition]:
    """Definitions sharing (type, name, spec-or-body), across files or within one."""
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for item in discovered:
        groups[(TYPE_MAP[item["kind"]].value, item["name"], item["kind"])].append(item)
    duplicates = []
    for (object_type, name, _), items in groups.items():
        if len(items) < 2:
            continue
        texts = {re.sub(r"\s+", " ", item["source"]).strip() for item in items}
        duplicates.append(
            DuplicateDefinition(
                object_id=make_object_id(object_type, name, schema=DEFAULT_SCHEMA),
                places=tuple(sorted((SourceLocation(i["filename"], i["line"]) for i in items),
                                    key=lambda p: (p.file, p.line))),
                identical=len(texts) == 1,
            )
        )
    return duplicates


def parse_sources(sources: dict[str, str]) -> list[IRObject]:
    """Parse multiple Oracle source files into a list of IR objects."""

    return parse_sources_detailed(sources).objects


def parse_sources_detailed(sources: dict[str, str]) -> ParseResult:
    """parse_sources, plus each object's starting file and line.

    Raises ForeignSchemaDefinitionError when a CREATE names another schema, and
    DuplicateDefinitionError when an object is defined more than once.
    """

    discovered: list[dict] = []
    foreign: list[ForeignSchemaDefinition] = []

    # First pass: discover object declarations.
    for filename, content in sources.items():
        cleaned = remove_comments(content)

        for statement, first_line in split_statements_with_lines(cleaned):
            match = CREATE_PATTERN.search(statement)
            if not match:
                continue

            kind = get_object_kind(match)
            name = normalize_name(match.group("name"))
            line = _line_at(statement, match.start(), first_line)

            schema = match.group("schema")
            if schema is not None and oracle_canonical(schema) != UPLOAD_SCHEMA:
                foreign.append(
                    ForeignSchemaDefinition(f"{schema}.{match.group('name')}", SourceLocation(filename, line))
                )
                continue

            discovered.append(
                {
                    "name": name,
                    "declared": oracle_canonical(match.group("name")),
                    "kind": kind,
                    "source": statement.strip(),
                    "filename": filename,
                    "line": line,
                }
            )

    if foreign:
        raise ForeignSchemaDefinitionError(foreign)

    duplicates = _find_duplicates(discovered)
    if duplicates:
        raise DuplicateDefinitionError(duplicates)

    known_objects = {item["name"] for item in discovered}

    # Merge package specification and package body into one IR object.
    merged: dict[tuple[str, str], dict] = {}

    for item in discovered:
        name = item["name"]
        kind = item["kind"]
        object_type = TYPE_MAP[kind]
        key = (object_type.value, name)

        if key not in merged:
            merged[key] = {
                **item,
                "kind": kind,
                "source_parts": [item["source"]],
                "part_places": [(item["filename"], item["line"])],
                "filename": item["filename"],
            }
        else:
            merged[key]["source_parts"].append(item["source"])
            merged[key]["part_places"].append((item["filename"], item["line"]))

    objects: list[IRObject] = []
    locations: dict[str, SourceLocation] = {}
    declared_names: dict[str, str] = {}

    # Second pass: extract metadata and construct IR.
    for item in merged.values():
        name = item["name"]
        kind = item["kind"]
        object_type = TYPE_MAP[kind]
        source = "\n\n".join(item["source_parts"])

        reads, writes = extract_table_references(source)

        # For CREATE TABLE foreign keys, capture referenced tables too, schema kept
        # (REFERENCES hr.departments is hr.departments, not hr; v11).
        if kind == "TABLE":
            reads += [_normalized(split_reference(r)) for r in _foreign_key_references(source)]

        reads = sorted(set(reads))
        writes = sorted(set(writes))

        dependencies = extract_dependencies(
            source=source,
            known_objects=known_objects,
            current_object=name,
        )

        # A package body may refer to its package specification.
        # That is the same logical IR object, so remove self-dependency.
        dependencies = [dep for dep in dependencies if dep != name]

        object_id = make_object_id(
            object_type.value,
            name,
            schema=DEFAULT_SCHEMA,
        )
        locations[object_id] = SourceLocation(item["filename"], item["line"])
        declared_names[object_id] = item["declared"]
        objects.append(
            IRObject(
                object_id=object_id,
                name=name,
                type=object_type,
                package=None,
                params=extract_params(source, kind),
                return_type=extract_return_type(source, kind),
                reads_tables=reads,
                writes_tables=writes,
                depends_on=dependencies,
                business_rules=[],
                source_code=source,
                source_file=item["filename"],
                oracle_features=extract_oracle_features(source),
            )
        )
	        # Extract implemented functions and procedures from package bodies.
        if kind in {"PACKAGE", "PACKAGE BODY"}:
            for member in extract_package_members(source):
                member_kind = member["kind"]
                member_name = member["name"]
                member_source = member["source"]

                member_type = TYPE_MAP[member_kind]
                member_reads, member_writes = extract_table_references(
                    member_source
                )

                member_id = make_object_id(
                    member_type.value,
                    member_name,
                    schema=DEFAULT_SCHEMA,
                    package=name,
                )
                member_place = _member_location(item, member["start"])
                if member_id not in locations:
                    locations[member_id] = member_place
                    declared_names[member_id] = member["declared"]
                objects.append(
                    IRObject(
                        object_id=member_id,
                        name=member_name,
                        type=member_type,
                        package=name,
                        params=extract_params(member_source, member_kind),
                        return_type=extract_return_type(
                            member_source, member_kind
                        ),
                        reads_tables=member_reads,
                        writes_tables=member_writes,
                        depends_on=[],
                        business_rules=[],
                        source_code=member_source,
                        source_file=item["filename"],
                        oracle_features=extract_oracle_features(
                            member_source
                        ),
                    )
                )

    check_unique_object_ids(
        (obj.object_id, f"{obj.name} in {obj.source_file}")
        for obj in objects
    )

    resolve_object_dependencies(objects, declared_names)

    return ParseResult(
        objects=sorted(objects, key=lambda obj: (obj.type.value, obj.object_id)),
        locations=locations,
        declared_names=declared_names,
    )


def _member_location(item: dict, start: int) -> SourceLocation:
    """File and line of a package member, from its offset in the merged package source."""
    offset = 0
    for part, (filename, first_line) in zip(item["source_parts"], item["part_places"], strict=True):
        end = offset + len(part)
        if start < end:
            return SourceLocation(filename, _line_at(part, start - offset, first_line))
        offset = end + 2  # the "\n\n" between parts
    raise ValueError("member offset outside the package source")  # pragma: no cover


def parse_file(filename: str, content: str) -> list[IRObject]:
    """Convenience wrapper for parsing a single SQL file."""

    return parse_sources({filename: content})


def summarize_objects(objects: Iterable[IRObject]) -> dict[str, int]:
    """Return the number of discovered objects grouped by type."""

    summary: dict[str, int] = defaultdict(int)

    for obj in objects:
        summary[obj.type.value] += 1

    return dict(sorted(summary.items()))


"""First-pass Oracle SQL/PLSQL object discovery and IR extraction.

This is a lightweight MVP parser. It identifies database objects and extracts
basic metadata using regular expressions. It is not a complete PL/SQL compiler.
"""

from __future__ import annotations

import re
from collections import defaultdict
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
        return ""

    return pattern.sub(replace, sql)


# ---------------------------------------------------------------------------
# 3. SQL/PLSQL statement splitting
# ---------------------------------------------------------------------------

def split_statements(sql: str) -> list[str]:
    """Split SQL statements on semicolons and PL/SQL units on slash lines."""

    statements: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        statement = "\n".join(buffer).strip()
        if statement:
            statements.append(statement)
        buffer.clear()

    for line in sql.splitlines():
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
        r"\b" + kind + r"\s+[\w$#\"]+\s*\(",
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



def extract_table_references(source: str) -> tuple[list[str], list[str]]:
    """Extract table read and write references from Oracle SQL/PLSQL.

    Schema-qualified names are preserved.
    Example: HR.EMPLOYEES -> HR.EMPLOYEES
    """

    reads: set[str] = set()
    writes: set[str] = set()

    # Support regular and quoted Oracle identifiers.
    identifier = r'(?:"[^"]+"|[\w$#]+)'
    qualified_name = rf'{identifier}(?:\s*\.\s*{identifier})?'

    def extract_object_name(value: str) -> str:
        """Normalize identifiers while preserving schema qualification."""
        parts = re.split(r"\s*\.\s*", value.strip())
        return ".".join(normalize_name(part) for part in parts)

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
            rf"\bUPDATE\s+(?!OF\b)({qualified_name})",
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
            reads.add(extract_object_name(match.group(1)))

    for pattern in write_patterns:
        for match in pattern.finditer(source):
            writes.add(extract_object_name(match.group(1)))

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


def resolve_object_dependencies(objects: list[IRObject]) -> None:
    """Resolve extracted SQL references to canonical object IDs.

    Mutates each IRObject.depends_on after all objects have been discovered.
    Unknown references are retained as unresolved IDs.
    """

    from app.contracts import parse_object_id

    by_id = {obj.object_id: obj for obj in objects}

    tables: dict[str, str] = {}
    sequences: dict[str, str] = {}
    packages: dict[str, str] = {}
    routines: dict[tuple[str | None, str], list[str]] = {}

    # Build indexes from the complete set of discovered objects.
    for obj in objects:
        ref = parse_object_id(obj.object_id)

        if obj.type in {ObjectType.TABLE, ObjectType.VIEW}:
            tables[obj.name] = obj.object_id

        elif obj.type == ObjectType.SEQUENCE:
            sequences[obj.name] = obj.object_id

        elif obj.type == ObjectType.PACKAGE:
            packages[obj.name] = obj.object_id

        elif obj.type in {ObjectType.FUNCTION, ObjectType.PROCEDURE}:
            routines.setdefault((obj.package, obj.name), []).append(
                obj.object_id
            )

    def resolve_table(reference: str) -> str:
        name = reference.split(".")[-1]
        normalized = normalize_name(name)

        if normalized in tables:
            return tables[normalized]

        return make_unresolved_id(
            reference,
            default_schema=DEFAULT_SCHEMA,
        )

    def resolve_routine(
        name: str,
        package: str | None = None,
    ) -> str | None:
        candidates = routines.get((package, normalize_name(name)), [])

        if len(candidates) == 1:
            return candidates[0]

        return None

    for obj in objects:
        dependencies: set[str] = set()

        # 1. Table reads and writes, including foreign-key references.
        for reference in [*obj.reads_tables, *obj.writes_tables]:
            dependency = resolve_table(reference)
            if dependency != obj.object_id:
                dependencies.add(dependency)

        # 2. Sequence NEXTVAL and CURRVAL references.
        for match in re.finditer(
            r'\b([\w$#]+(?:\s*\.\s*[\w$#]+)?)\s*\.\s*(?:NEXTVAL|CURRVAL)\b',
            obj.source_code,
            re.IGNORECASE,
        ):
            reference = re.sub(r"\s+", "", match.group(1))
            sequence_name = normalize_name(reference.split(".")[-1])

            dependency = sequences.get(sequence_name)
            if dependency is None:
                dependency = make_unresolved_id(
                    reference,
                    default_schema=DEFAULT_SCHEMA,
                )

            if dependency != obj.object_id:
                dependencies.add(dependency)

        # 3. Explicit package-qualified references.
        for match in re.finditer(
            r'\b([A-Za-z_$#][\w$#]*)\s*\.\s*([A-Za-z_$#][\w$#]*)\b',
            obj.source_code,
            re.IGNORECASE,
        ):
            package_name = normalize_name(match.group(1))
            member_name = normalize_name(match.group(2))

            package_id = packages.get(package_name)
            if package_id is None:
                continue

            # A known routine member gets its own ID.
            # Package constants and state depend on the package itself.
            member_id = resolve_routine(member_name, package_name)
            dependency = member_id or package_id

            if dependency != obj.object_id:
                dependencies.add(dependency)

        # 4. Unqualified routine calls inside the same package.
        current_package = obj.package

        if current_package:
            for member_name in routines:
                package_name, routine_name = member_name

                if package_name != current_package:
                    continue

                if re.search(
                    rf'\b{re.escape(routine_name)}\s*\(',
                    obj.source_code,
                    re.IGNORECASE,
                ):
                    dependency = resolve_routine(
                        routine_name,
                        current_package,
                    )
                    if dependency and dependency != obj.object_id:
                        dependencies.add(dependency)

        # 5. Trigger attachment is a dependency, not a body write.

        # 5. Trigger attachment is a dependency, not a body write.
        if obj.type == ObjectType.TRIGGER:
            metadata = extract_trigger_metadata(obj.source_code)

            if metadata:
                dependency = resolve_table(metadata["table_reference"])

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
        \b
        """,
        re.IGNORECASE | re.MULTILINE | re.VERBOSE,
    )

    for match in member_pattern.finditer(source):
        kind = match.group("kind").upper()
        name = normalize_name(match.group("name"))

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
                "source": member_source,
            }
        )

    return members

# ---------------------------------------------------------------------------
# 8. Main parser
# ---------------------------------------------------------------------------

def parse_sources(sources: dict[str, str]) -> list[IRObject]:
    """Parse multiple Oracle source files into a list of IR objects."""

    discovered: list[dict] = []

    # First pass: discover object declarations.
    for filename, content in sources.items():
        cleaned = remove_comments(content)

        for statement in split_statements(cleaned):
            match = CREATE_PATTERN.search(statement)
            if not match:
                continue

            kind = get_object_kind(match)
            name = normalize_name(match.group("name"))

            discovered.append(
                {
                    "name": name,
                    "kind": kind,
                    "source": statement.strip(),
                    "filename": filename,
                }
            )

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
                "filename": item["filename"],
            }
        else:
            merged[key]["source_parts"].append(item["source"])

    objects: list[IRObject] = []

    # Second pass: extract metadata and construct IR.
    for item in merged.values():
        name = item["name"]
        kind = item["kind"]
        object_type = TYPE_MAP[kind]
        source = "\n\n".join(item["source_parts"])

        reads, writes = extract_table_references(source)

        # For CREATE TABLE foreign keys, capture referenced tables too.
        if kind == "TABLE":
            for match in re.finditer(
                r"\bREFERENCES\s+([\w$#]+)",
                source,
                re.IGNORECASE,
            ):
                reads.append(normalize_name(match.group(1)))

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

        objects.append(
            IRObject(
                object_id=make_object_id(
                    object_type.value,
                    name,
                    schema=DEFAULT_SCHEMA,
                ),
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

                objects.append(
                    IRObject(
                        object_id=make_object_id(
                            member_type.value,
                            member_name,
                            schema=DEFAULT_SCHEMA,
                            package=name,
                        ),
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

    resolve_object_dependencies(objects)

    return sorted(objects, key=lambda obj: (obj.type.value, obj.object_id))


def parse_file(filename: str, content: str) -> list[IRObject]:
    """Convenience wrapper for parsing a single SQL file."""

    return parse_sources({filename: content})


def summarize_objects(objects: Iterable[IRObject]) -> dict[str, int]:
    """Return the number of discovered objects grouped by type."""

    summary: dict[str, int] = defaultdict(int)

    for obj in objects:
        summary[obj.type.value] += 1

    return dict(sorted(summary.items()))

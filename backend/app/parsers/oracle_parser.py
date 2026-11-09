
"""First-pass Oracle SQL/PLSQL object discovery and IR extraction.

This is a lightweight MVP parser. It identifies database objects and extracts
basic metadata using regular expressions. It is not a complete PL/SQL compiler.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable

from app.contracts.models import IRObject, ObjectType, Param, ParamMode


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
    return name.strip().strip('"').upper()


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
    """Return basic table read and write references.

    Schema-qualified names are normalized to their final object name.
    Example: HR.EMPLOYEES -> EMPLOYEES
    """

    reads: set[str] = set()
    writes: set[str] = set()

    identifier = r'(?:"[^"]+"|[\w$#]+)'
    qualified_name = rf'{identifier}(?:\s*\.\s*{identifier})?'

    read_pattern = re.compile(
        rf"\b(?:FROM|JOIN)\s+({qualified_name})",
        re.IGNORECASE,
    )

    write_patterns = [
        re.compile(
            rf"\bINSERT\s+INTO\s+({qualified_name})",
            re.IGNORECASE,
        ),
        re.compile(
            rf"\bUPDATE\s+({qualified_name})",
            re.IGNORECASE,
        ),
        re.compile(
            rf"\bDELETE\s+FROM\s+({qualified_name})",
            re.IGNORECASE,
        ),
        re.compile(
            rf"\bMERGE\s+INTO\s+({qualified_name})",
            re.IGNORECASE,
        ),
    ]

    def extract_object_name(value: str) -> str:
        parts = re.split(r"\s*\.\s*", value.strip())
        return normalize_name(parts[-1])

    for match in read_pattern.finditer(source):
        reads.add(extract_object_name(match.group(1)))

    for pattern in write_patterns:
        for match in pattern.finditer(source):
            writes.add(extract_object_name(match.group(1)))

    return sorted(reads), sorted(writes)



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
    features = []

    for feature, pattern in ORACLE_FEATURE_PATTERNS.items():
        if re.search(pattern, source, re.IGNORECASE):
            features.append(feature)

    return sorted(features)


# ---------------------------------------------------------------------------
# 7. Dependency extraction
# ---------------------------------------------------------------------------

def extract_dependencies(
    source: str,
    known_objects: set[str],
    current_object: str,
) -> list[str]:
    """Find references to other discovered database objects."""

    source_upper = source.upper()
    dependencies = []

    for name in known_objects:
        if name == current_object:
            continue

        if re.search(
            rf"(?<![\w$#]){re.escape(name)}(?![\w$#])",
            source_upper,
        ):
            dependencies.append(name)

    return sorted(dependencies)


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
                object_id=f"{object_type.value.lower()}:{name.lower()}",
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

    return sorted(objects, key=lambda obj: (obj.type.value, obj.name))


def parse_file(filename: str, content: str) -> list[IRObject]:
    """Convenience wrapper for parsing a single SQL file."""

    return parse_sources({filename: content})


def summarize_objects(objects: Iterable[IRObject]) -> dict[str, int]:
    """Return the number of discovered objects grouped by type."""

    summary: dict[str, int] = defaultdict(int)

    for obj in objects:
        summary[obj.type.value] += 1

    return dict(sorted(summary.items()))

"""Oracle feature detection: decide how each object can be converted, before converting it.

The parser already lists some features (``IRObject.oracle_features``). This module looks
at the source in more detail and sorts every finding into one of three handlings:

* ``RULE``: a deterministic, tested rule converts it (Stage 1, rules R1-R13).
* ``LLM``: no rule exists yet; Gemini may convert it, and validation must prove it.
* ``REVIEW``: never converted automatically. The object stops at once and goes to a
  person (decision D5). Dynamic SQL, autonomous transactions, database links,
  transaction control, ROWNUM outside the top-N pattern, and package state that
  could change are in this group.

Strings and comments are masked first, so words inside messages and comments never
count. Detection is text-based on purpose: it has to work on PL/SQL, which SQLGlot
can't parse. It errs on the side of REVIEW when unsure.

Package members only carry their own text, so package context comes from
``analyze_package``: which names are package constants and variables, which are
members, and whether a variable could ever change. A variable is inlined (D4) only if
it is declared in the package *body* (private, so no outside code can change it), has
a literal initial value, and is never assigned in the body: not by ``:=``, not by
``INTO``, and not as a bare argument of a procedure call (it might be an OUT
argument). Anything else that a member uses is REVIEW.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum


class Handling(str, Enum):
    RULE = "rule"
    LLM = "llm"
    REVIEW = "review"


_ORDER = {Handling.RULE: 0, Handling.LLM: 1, Handling.REVIEW: 2}


@dataclass(frozen=True)
class Finding:
    feature: str
    handling: Handling
    detail: str


@dataclass
class FeatureReport:
    findings: list[Finding] = field(default_factory=list)

    @property
    def features(self) -> set[str]:
        return {f.feature for f in self.findings}

    @property
    def handling(self) -> Handling:
        """The strictest handling any finding needs (RULE when there are none)."""
        return max((f.handling for f in self.findings), key=_ORDER.__getitem__, default=Handling.RULE)

    def by_handling(self, handling: Handling) -> list[Finding]:
        return [f for f in self.findings if f.handling is handling]

    def review_reason(self) -> str | None:
        """One line for NEEDS_HUMAN, e.g. 'Unsupported: dynamic SQL (EXECUTE IMMEDIATE)'."""
        review = self.by_handling(Handling.REVIEW)
        if not review:
            return None
        return "Unsupported: " + "; ".join(f.detail for f in review)

    def add(self, feature: str, handling: Handling, detail: str) -> None:
        finding = Finding(feature, handling, detail)
        if finding not in self.findings:
            self.findings.append(finding)


# --- masking ------------------------------------------------------------------------

_Q_CLOSE = {"[": "]", "{": "}", "(": ")", "<": ">"}


def mask_oracle(source: str) -> str:
    """Oracle source with comments removed and non-empty strings replaced by ``'x'``.

    An empty literal ``''`` stays as ``''`` (it matters: it is NULL in Oracle).
    Handles ``''`` escapes and q-quoting (``q'[...]'``, ``Q'{...}'`` and so on).
    Quoted identifiers are kept. Unterminated strings or comments raise ValueError.
    """
    out: list[str] = []
    i, n = 0, len(source)
    while i < n:
        ch, two = source[i], source[i:i + 2]
        if two == "--":
            end = source.find("\n", i)
            i = n if end == -1 else end
            out.append(" ")
        elif two == "/*":
            end = source.find("*/", i + 2)
            if end == -1:
                raise ValueError("unterminated /* comment")
            i = end + 2
            out.append(" ")
        elif ch in "qQ" and source[i + 1:i + 2] == "'" and i + 2 < n and not _word_before(source, i):
            open_ch = source[i + 2]
            close = _Q_CLOSE.get(open_ch, open_ch) + "'"
            end = source.find(close, i + 3)
            if end == -1:
                raise ValueError("unterminated q-quoted string")
            out.append("'x'" if end > i + 3 else "''")
            i = end + 2
        elif ch == "'":
            j = i + 1
            while True:
                if j >= n:
                    raise ValueError("unterminated string literal")
                if source[j] == "'":
                    if source[j + 1:j + 2] == "'":
                        j += 2
                        continue
                    break
                j += 1
            out.append("''" if j == i + 1 else "'x'")
            i = j + 1
        elif ch == '"':
            end = source.find('"', i + 1)
            if end == -1:
                raise ValueError("unterminated quoted identifier")
            out.append(source[i:end + 1])
            i = end + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _word_before(text: str, i: int) -> bool:
    return i > 0 and (text[i - 1].isalnum() or text[i - 1] in "_$#")


# --- packages ---------------------------------------------------------------------

@dataclass(frozen=True)
class PackageVariable:
    name: str
    scope: str  # "spec" (public) or "body" (private)
    constant: bool
    initial: str | None  # the literal, or None if missing or not a literal
    assigned: bool

    @property
    def inlinable(self) -> bool:
        if self.initial is None:
            return False
        return self.constant or (self.scope == "body" and not self.assigned)

    @property
    def why_not_inlinable(self) -> str:
        if self.initial is None:
            return "its initial value is not a literal"
        if self.scope == "spec":
            return "it is public, so other code could change it"
        return "it is assigned inside the package"


@dataclass(frozen=True)
class PackageSymbols:
    name: str
    variables: Mapping[str, PackageVariable]
    members: frozenset[str]
    other_declarations: tuple[str, ...] = ()  # TYPE, CURSOR, EXCEPTION ... at package level


_PKG_HEADER = re.compile(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?PACKAGE\s+(BODY\s+)?([\w$#\"]+)\s+(?:AS|IS)\b", re.I)
_MEMBER = re.compile(r"\b(?:FUNCTION|PROCEDURE)\s+([A-Za-z_][\w$#]*)", re.I)
_LITERAL = re.compile(r"^(?:-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?|'x'|''|NULL|TRUE|FALSE)$", re.I)
_DECLARATION = re.compile(
    r"^\s*([A-Za-z_][\w$#]*)\s+(CONSTANT\s+)?([A-Za-z_][\w$#.%]*(?:\s*\([^)]*\))?)\s*(?:(?::=|DEFAULT)\s*(.+?))?\s*$",
    re.I | re.S,
)
_NOT_VARIABLES = {"TYPE", "SUBTYPE", "CURSOR", "PRAGMA", "FUNCTION", "PROCEDURE", "END", "BEGIN"}


def _sections(masked: str) -> dict[str, str]:
    """{'spec': text, 'body': text} for a package's source (either may be missing)."""
    headers = list(_PKG_HEADER.finditer(masked))
    parts: dict[str, str] = {}
    for k, match in enumerate(headers):
        end = headers[k + 1].start() if k + 1 < len(headers) else len(masked)
        parts["body" if match.group(1) else "spec"] = masked[match.end():end]
    return parts


def _declarations(section: str) -> list[str]:
    """Package-level declarations: everything before the first member (PL/SQL requires that order)."""
    first_member = _MEMBER.search(section)
    head = section[: first_member.start()] if first_member else re.split(r"\bEND\b", section, maxsplit=1, flags=re.I)[0]
    return [d.strip() for d in head.split(";") if d.strip()]


def _assigned_in(masked: str, name: str) -> bool:
    n = re.escape(name)
    if re.search(rf"(?<![\w.$#]){n}\s*:=", masked, re.I):
        return True
    for into in re.finditer(r"(?<!INSERT\s)(?<!MERGE\s)\bINTO\b([^;]*)", masked, re.I):
        targets = re.split(r"\b(?:FROM|USING|VALUES|WHERE)\b", into.group(1), maxsplit=1, flags=re.I)[0]
        if re.search(rf"(?<![\w.$#]){n}(?![\w$#])", targets, re.I):
            return True
    # A bare argument of a procedure-call statement might be an OUT argument.
    for statement in masked.split(";"):
        statement = re.sub(r"^\s*(?:(?:BEGIN|THEN|ELSE|LOOP|IS|AS|DECLARE)\s+)*", "", statement, flags=re.I)
        if re.match(r"[A-Za-z_][\w$#.]*\s*\(", statement) and not re.match(
            r"(?:IF|ELSIF|WHILE|FOR|RETURN|SELECT|INSERT|UPDATE|DELETE|OPEN|CASE)\b", statement, re.I
        ):
            if re.search(rf"[(,]\s*{n}\s*[,)]", statement, re.I):
                return True
    return False


def analyze_package(source: str, other_sources: Iterable[str] = ()) -> PackageSymbols:
    """Symbols of one package (spec and body together, as the parser keeps them)."""
    masked = mask_oracle(source)
    names = [m.group(2).strip('"').lower() for m in _PKG_HEADER.finditer(masked)]
    if not names:
        raise ValueError("not a package: no CREATE PACKAGE header found")
    name = names[0]
    sections = _sections(masked)
    members = frozenset(m.group(1).lower() for m in _MEMBER.finditer(masked))
    others = [mask_oracle(s) for s in other_sources]

    variables: dict[str, PackageVariable] = {}
    other_declarations: list[str] = []
    for scope in ("spec", "body"):
        for declaration in _declarations(sections.get(scope, "")):
            first = declaration.split()[0].upper()
            match = _DECLARATION.match(declaration)
            if first in _NOT_VARIABLES or not match or "EXCEPTION" == match.group(3).upper():
                other_declarations.append(declaration.split()[0] + " " + " ".join(declaration.split()[1:3]))
                continue
            var_name = match.group(1).lower()
            initial = match.group(4).strip() if match.group(4) else None
            if initial is not None and not _LITERAL.match(initial):
                initial = None
            # Changes inside the body (excluding the declaration itself), or from outside via pkg.var.
            body_without_decl = sections.get("body", "").replace(declaration, " ", 1)
            assigned = _assigned_in(body_without_decl, var_name) or any(
                _assigned_in(text, f"{name}.{var_name}") for text in others
            )
            variables[var_name] = PackageVariable(
                name=var_name,
                scope=scope,
                constant=bool(match.group(2)),
                initial=initial,
                assigned=assigned,
            )
    return PackageSymbols(name, variables, members, tuple(other_declarations))


def literal_value(source: str, package: PackageSymbols, variable: str) -> str:
    """The original (unmasked) initial literal of an inlinable package variable."""
    var = package.variables[variable]
    if not var.inlinable:
        raise ValueError(f"{package.name}.{variable} is not inlinable: {var.why_not_inlinable}")
    pattern = re.compile(
        rf"(?<![\w$#]){re.escape(variable)}\s+(?:CONSTANT\s+)?[\w$#.%]+(?:\s*\([^)]*\))?\s*(?::=|DEFAULT)\s*([^;]+);",
        re.I,
    )
    match = pattern.search(source)
    if not match:
        raise ValueError(f"declaration of {variable} not found")
    return match.group(1).strip()


# --- detection -------------------------------------------------------------------

_PATTERNS: list[tuple[str, Handling, re.Pattern, str]] = [
    # Deterministic rules exist (Stage 1 rules R1-R13).
    ("EMPTY_STRING", Handling.RULE, re.compile(r"''"), "'' literal (NULL in Oracle)"),
    ("CONCAT", Handling.RULE, re.compile(r"\|\|"), "|| concatenation (NULL-safe in Oracle)"),
    ("SYSDATE", Handling.RULE, re.compile(r"\bSYSDATE\b", re.I), "SYSDATE"),
    ("NVL", Handling.RULE, re.compile(r"\bNVL\s*\(", re.I), "NVL"),
    ("DECODE", Handling.RULE, re.compile(r"\bDECODE\s*\(", re.I), "DECODE"),
    ("TRUNC", Handling.RULE, re.compile(r"\bTRUNC\s*\(", re.I), "TRUNC"),
    ("ADD_MONTHS", Handling.RULE, re.compile(r"\bADD_MONTHS\s*\(", re.I), "ADD_MONTHS"),
    ("LAST_DAY", Handling.RULE, re.compile(r"\bLAST_DAY\s*\(", re.I), "LAST_DAY"),
    ("SEQUENCE", Handling.RULE, re.compile(r"\b[\w$#]+\s*\.\s*(?:NEXTVAL|CURRVAL)\b", re.I),
     "sequence NEXTVAL/CURRVAL"),
    ("CURSOR_FOR_LOOP", Handling.RULE, re.compile(r"\bFOR\s+\w+\s+IN\s*\(\s*SELECT\b", re.I), "cursor FOR loop"),
    ("TYPE_ANCHOR", Handling.RULE, re.compile(r"%(?:ROW)?TYPE\b", re.I), "%TYPE / %ROWTYPE"),
    ("EXCEPTION_HANDLER", Handling.RULE, re.compile(r"\bEXCEPTION\s+WHEN\b", re.I), "exception handler"),
    ("SELECT_FOR_UPDATE", Handling.RULE, re.compile(r"\bFOR\s+UPDATE\b", re.I), "SELECT ... FOR UPDATE"),
    ("INTEGER_TYPE", Handling.RULE, re.compile(r"\b(?:INTEGER|PLS_INTEGER|BINARY_INTEGER|SMALLINT)\b", re.I),
     "integer type (numeric division in Oracle)"),
    ("TRIGGER_ROW_REF", Handling.RULE, re.compile(r":(?:NEW|OLD)\b", re.I), ":NEW / :OLD"),
    # No rule yet: Gemini may try, validation must prove it.
    ("CONNECT_BY", Handling.LLM, re.compile(r"\bCONNECT\s+BY\b|\bSYS_CONNECT_BY_PATH\b", re.I),
     "hierarchical query (CONNECT BY)"),
    ("OUTER_JOIN_PLUS", Handling.LLM, re.compile(r"\(\s*\+\s*\)"), "(+) outer join"),
    ("MERGE", Handling.LLM, re.compile(r"\bMERGE\s+INTO\b", re.I), "MERGE"),
    ("EXPLICIT_CURSOR", Handling.LLM, re.compile(r"\bCURSOR\s+\w+\s*(?:IS|\()|\bREF\s+CURSOR\b|\bSYS_REFCURSOR\b",
     re.I), "explicit or REF cursor"),
    ("COLLECTION", Handling.LLM, re.compile(r"\bTABLE\s+OF\b|\bVARRAY\b|\bBULK\s+COLLECT\b|\bFORALL\b", re.I),
     "collections / bulk operations"),
    ("RAISE_APPLICATION_ERROR", Handling.LLM, re.compile(r"\bRAISE_APPLICATION_ERROR\s*\(", re.I),
     "RAISE_APPLICATION_ERROR"),
    ("DBMS_OUTPUT", Handling.LLM, re.compile(r"\bDBMS_OUTPUT\s*\.", re.I), "DBMS_OUTPUT"),
    ("ORACLE_FUNCTION", Handling.LLM, re.compile(
        r"\b(?:MONTHS_BETWEEN|NEXT_DAY|LISTAGG|NVL2|INSTR|SYS_GUID|REGEXP_\w+)\s*\(", re.I),
     "Oracle-only function"),
    # Never converted automatically (D5).
    ("DYNAMIC_SQL", Handling.REVIEW, re.compile(r"\bEXECUTE\s+IMMEDIATE\b|\bDBMS_SQL\s*\.", re.I),
     "dynamic SQL (EXECUTE IMMEDIATE / DBMS_SQL)"),
    ("AUTONOMOUS_TRANSACTION", Handling.REVIEW, re.compile(r"\bPRAGMA\s+AUTONOMOUS_TRANSACTION\b", re.I),
     "autonomous transaction"),
    ("DATABASE_LINK", Handling.REVIEW, re.compile(r"[\w$#\"]@[A-Za-z_\"]"), "database link (@dblink)"),
    ("TRANSACTION_CONTROL", Handling.REVIEW, re.compile(r"\b(?:COMMIT|ROLLBACK|SAVEPOINT)\b", re.I),
     "transaction control (COMMIT / ROLLBACK / SAVEPOINT)"),
    ("SYSTEM_PACKAGE", Handling.REVIEW, re.compile(r"\b(?:UTL_\w+|DBMS_(?!OUTPUT\b|SQL\b)\w+)\s*\.", re.I),
     "Oracle system package (UTL_* / DBMS_*)"),
]

_SELECT_INTO = re.compile(r"\bSELECT\b(?:(?!\bFROM\b).)*?\bINTO\b", re.I | re.S)
_ROWNUM = re.compile(r"\bROWNUM\b", re.I)
_TOP_N = re.compile(
    r"\)\s*(?:[A-Za-z_][\w$#]*\s+)?WHERE\s+ROWNUM\s*(<=|<)\s*([A-Za-z_][\w$#.]*|\d+(?:\.\d+)?)\s*(?=\)|;|$)", re.I
)


def _top_n_rownums(masked: str) -> int:
    """How many ROWNUM uses are the safe top-N pattern: an ordered subquery, then only ROWNUM <=/< n."""
    count = 0
    for match in _TOP_N.finditer(masked):
        close = match.start()
        depth, i = 0, close
        while i >= 0:
            if masked[i] == ")":
                depth += 1
            elif masked[i] == "(":
                depth -= 1
                if depth == 0:
                    break
            i -= 1
        subquery = masked[i + 1:close] if i >= 0 else ""
        if re.match(r"\s*SELECT\b", subquery, re.I) and _ends_with_order_by(subquery):
            count += 1
    return count


def _ends_with_order_by(subquery: str) -> bool:
    """ORDER BY at the subquery's own level (not inside a nested subquery)."""
    depth, top = 0, []
    for ch in subquery:
        depth += ch == "("
        top.append(ch if depth == 0 else " ")
        depth -= ch == ")"
    return re.search(r"\bORDER\s+BY\b", "".join(top), re.I) is not None


def detect_features(
    source: str,
    *,
    package: str | None = None,
    packages: Mapping[str, PackageSymbols] | None = None,
) -> FeatureReport:
    """Findings for one object's Oracle source.

    ``package`` is the package this object belongs to (for members); ``packages`` maps
    package name to its symbols, for resolving ``pkg.name`` references anywhere.
    """
    packages = {k.lower(): v for k, v in (packages or {}).items()}
    report = FeatureReport()
    try:
        masked = mask_oracle(source)
    except ValueError as exc:
        report.add("UNREADABLE", Handling.REVIEW, f"source could not be read ({exc})")
        return report

    for feature, handling, pattern, detail in _PATTERNS:
        if pattern.search(masked):
            report.add(feature, handling, detail)
    if any(_SELECT_INTO.search(piece) for piece in masked.split(";")):
        report.add("SELECT_INTO", Handling.RULE, "SELECT ... INTO (raises NO_DATA_FOUND / TOO_MANY_ROWS)")

    rownums = len(_ROWNUM.findall(masked))
    if rownums:
        top_n = _top_n_rownums(masked)
        if top_n == rownums:
            report.add("ROWNUM_TOP_N", Handling.RULE, "ROWNUM top-N over an ordered subquery")
        else:
            report.add("ROWNUM", Handling.REVIEW, "ROWNUM outside the top-N pattern")

    _package_references(masked, report, package and package.lower(), packages)
    return report


def _package_references(
    masked: str, report: FeatureReport, own: str | None, packages: Mapping[str, PackageSymbols]
) -> None:
    def classify(pkg: PackageSymbols, item: str, how: str) -> None:
        if item in pkg.members:
            report.add("PACKAGE_CALL", Handling.RULE, f"calls {pkg.name}.{item}")
        elif item in pkg.variables:
            var = pkg.variables[item]
            label = "constant" if var.constant else "variable"
            if var.inlinable:
                report.add(f"PACKAGE_{label.upper()}", Handling.RULE, f"{pkg.name}.{item} inlined ({label})")
            else:
                report.add("PACKAGE_STATE", Handling.REVIEW,
                           f"package state {pkg.name}.{item}: {var.why_not_inlinable}")
        else:
            report.add("PACKAGE_UNKNOWN", Handling.REVIEW, f"{how} {pkg.name}.{item} is not declared in the package")

    for match in re.finditer(r"(?<![\w$#.])([A-Za-z_][\w$#]*)\s*\.\s*([A-Za-z_][\w$#]*)", masked):
        pkg = packages.get(match.group(1).lower())
        if pkg:
            classify(pkg, match.group(2).lower(), "reference to")

    if own and own in packages:
        pkg = packages[own]
        for name in pkg.variables:
            if re.search(rf"(?<![\w$#.]){re.escape(name)}(?![\w$#])", masked, re.I):
                classify(pkg, name, "use of")
        header = re.match(r"\s*(?:FUNCTION|PROCEDURE)\s+([A-Za-z_][\w$#]*)", masked, re.I)
        own_name = header.group(1).lower() if header else None
        body = masked[header.end():] if header else masked
        for name in pkg.members:
            if re.search(rf"(?<![\w$#.]){re.escape(name)}\s*\(", body, re.I):
                what = "recursive call" if name == own_name else f"calls {pkg.name}.{name}"
                report.add("PACKAGE_INTERNAL_CALL", Handling.RULE, f"{what} (unqualified; qualified in PostgreSQL)")

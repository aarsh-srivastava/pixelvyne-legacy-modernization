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

from app.migration.allow_list import (
    ALLOWED_FUNCTIONS,
    ALLOWED_WORDS,
    ORACLE_RESERVED,
    RECOGNISED,
    SYNTAX_WORDS,
    UNRESOLVED,
)


class Handling(str, Enum):
    RULE = "rule"
    LLM = "llm"
    REVIEW = "review"


#: A construct that is neither allow-listed nor recognised (PR 1, G3). Always REVIEW.
UNKNOWN_CONSTRUCT = "UNKNOWN_CONSTRUCT"


_ORDER = {Handling.RULE: 0, Handling.LLM: 1, Handling.REVIEW: 2}


@dataclass(frozen=True)
class Finding:
    feature: str
    handling: Handling
    detail: str
    #: 1-based line of the first occurrence in the object's source, and the words there
    #: (PR 1, G3). Not part of a finding's identity: one finding per feature and detail.
    line: int | None = field(default=None, compare=False)
    text: str | None = field(default=None, compare=False)

    @property
    def state(self) -> str:
        """Aarsh's three states: supported, recognised_unsupported or not_recognised."""
        if self.feature == UNKNOWN_CONSTRUCT:
            return "not_recognised"
        return "supported" if self.handling is Handling.RULE else "recognised_unsupported"


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

    def add(self, feature: str, handling: Handling, detail: str, line: int | None = None,
            text: str | None = None) -> None:
        finding = Finding(feature, handling, detail, line, text)
        if finding not in self.findings:
            self.findings.append(finding)


# --- masking ------------------------------------------------------------------------

_Q_CLOSE = {"[": "]", "{": "}", "(": ")", "<": ">"}


def mask_oracle(source: str, *, keep_positions: bool = False) -> str:
    """Oracle source with comments removed and non-empty strings replaced by ``'x'``.

    An empty literal ``''`` stays as ``''`` (it matters: it is NULL in Oracle).
    Handles ``''`` escapes and q-quoting (``q'[...]'``, ``Q'{...}'`` and so on).
    Quoted identifiers are kept. Unterminated strings or comments raise ValueError.

    With ``keep_positions=True`` every character keeps its offset (PR 1, line numbers):
    a comment becomes spaces, a non-empty string becomes ``'xxx'`` of the same length,
    and line breaks inside either are kept, so a match's line is its line in the source.
    """
    out: list[str] = []

    def blank(text: str) -> str:
        return re.sub(r"[^\n]", " ", text)

    def masked_string(text: str) -> str:
        if not keep_positions:
            return "'x'"
        inner = re.sub(r"[^\n]", "x", text[1:-1])
        return "'" + inner + "'"

    i, n = 0, len(source)
    while i < n:
        ch, two = source[i], source[i:i + 2]
        if two == "--":
            end = source.find("\n", i)
            end = n if end == -1 else end
            out.append(blank(source[i:end]) if keep_positions else " ")
            i = end
        elif two == "/*":
            end = source.find("*/", i + 2)
            if end == -1:
                raise ValueError("unterminated /* comment")
            out.append(blank(source[i:end + 2]) if keep_positions else " ")
            i = end + 2
        elif ch in "qQ" and source[i + 1:i + 2] == "'" and i + 2 < n and (
            not _word_before(source, i) or (source[i - 1] in "nN" and not _word_before(source, i - 1))
        ):  # q'[...]' and the national nq'[...]'; the n stays in the output
            open_ch = source[i + 2]
            close = _Q_CLOSE.get(open_ch, open_ch) + "'"
            end = source.find(close, i + 3)
            if end == -1:
                raise ValueError("unterminated q-quoted string")
            if end > i + 3:
                out.append(masked_string(source[i:end + 2]))
            else:
                out.append("''" + ("   " if keep_positions else ""))
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
            out.append("''" if j == i + 1 else masked_string(source[i:j + 1]))
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


def line_of(text: str, index: int) -> int:
    """1-based line of ``index`` in ``text``."""
    return text.count("\n", 0, index) + 1


# --- unresolved semantics (Aarsh, 19 Nov) ------------------------------------------------

_UNRESOLVED_CALL = re.compile(
    r"(?<![\w$#.\"])(" + "|".join(re.escape(name) for name in sorted(UNRESOLVED)) + r")\s*\(", re.I
)


@dataclass(frozen=True)
class SemanticRisk:
    """One use of an allow-listed function whose Oracle and PostgreSQL semantics differ
    (``allow_list.UNRESOLVED``). Routing is unchanged; the object needs human sign-off."""

    function: str
    line: int          # 1-based, in the object's own source
    source_line: str   # that line, whitespace-normalised and shortened

    def warning(self, object_id: str) -> str:
        return (
            f"Needs review: semantic risk in {object_id} at line {self.line}: {self.function}. "
            f"{UNRESOLVED[self.function]} Semantic equivalence is not verified. Action: human review, "
            f"or explicit acceptance of this known limitation, before deployment. Line: {self.source_line}"
        )


def semantic_risks(source: str) -> list[SemanticRisk]:
    """Every call to an UNRESOLVED function, one per occurrence, in source order.

    Strings and comments do not count. A qualified name (``pkg.greatest``) is not the
    built-in. Unreadable source gives none: it is already routed to review.
    """
    try:
        masked = mask_oracle(source, keep_positions=True)
    except ValueError:
        return []
    lines = source.splitlines()
    risks = []
    for match in _UNRESOLVED_CALL.finditer(masked):
        line = line_of(masked, match.start())
        text = " ".join(lines[line - 1].split()) if line <= len(lines) else ""
        risks.append(SemanticRisk(match.group(1).upper(), line, text if len(text) <= 160 else text[:157] + "..."))
    return risks


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


def _masked_or_as_written(source: str) -> str:
    """Another object's source, masked; searched as written when it cannot be read (PR 1).

    Unreadable code (an unterminated string) is routed to review on its own; it must not
    stop the analysis of a package it may assign to. As written, a real ``pkg.var :=`` is
    never missed; a name inside a string can only make a variable count as assigned, so it
    is not inlined (the safe direction).
    """
    try:
        return mask_oracle(source)
    except ValueError:
        return source


def analyze_package(source: str, other_sources: Iterable[str] = ()) -> PackageSymbols:
    """Symbols of one package (spec and body together, as the parser keeps them)."""
    masked = mask_oracle(source)
    names = [m.group(2).strip('"').lower() for m in _PKG_HEADER.finditer(masked)]
    if not names:
        raise ValueError("not a package: no CREATE PACKAGE header found")
    name = names[0]
    sections = _sections(masked)
    members = frozenset(m.group(1).lower() for m in _MEMBER.finditer(masked))
    others = [_masked_or_as_written(s) for s in other_sources]

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

#: Step 7d: how converted code reproduces ORA-30004. Appended to the CONNECT_BY finding's detail when
#: SYS_CONNECT_BY_PATH is used, so Gemini sees it among the constructs without a rule; still one finding
#: with the same code, so routing and Gemini eligibility are unchanged. The function is the platform's
#: own (compat.py); converted code may call it but never create a function.
_SYS_CONNECT_BY_PATH = re.compile(r"\bSYS_CONNECT_BY_PATH\b", re.I)
CONNECT_BY_PATH_HINT = (
    "SYS_CONNECT_BY_PATH must raise like ORA-30004 when a value contains the separator: in the recursive CTE "
    "carry has_sep = strpos(value, separator) > 0 for each row, and return the path from the outer SELECT as "
    "pv_compat.connect_by_path_checked(path, bool_or(has_sep) OVER ()). The platform provides that function "
    "(it raises SQLSTATE PV001); do not create any function"
)

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
    names: Iterable[str] | None = None,
) -> FeatureReport:
    """Findings for one object's Oracle source.

    ``package`` is the package this object belongs to (for members); ``packages`` maps
    package name to its symbols, for resolving ``pkg.name`` references anywhere.
    ``names`` are the names of the uploaded objects (see ``known_names``): a call to one
    of them is not an unknown construct. Without ``names`` every call to a function that
    is not built in, declared here or a package member is reported as unknown (PR 1, G3:
    the allow-list fails closed).

    Each finding carries the line of its first occurrence in ``source``.
    """
    packages = {k.lower(): v for k, v in (packages or {}).items()}
    report = FeatureReport()
    try:
        masked = mask_oracle(source, keep_positions=True)
    except ValueError as exc:
        report.add("UNREADABLE", Handling.REVIEW, f"source could not be read ({exc})")
        return report

    def where(match: re.Match) -> tuple[int, str]:
        return line_of(masked, match.start()), " ".join(match.group(0).split())

    for feature, handling, pattern, detail in _PATTERNS:
        match = pattern.search(masked)
        if match:
            if feature == "CONNECT_BY" and _SYS_CONNECT_BY_PATH.search(masked):
                detail = f"{detail}; {CONNECT_BY_PATH_HINT}"
            report.add(feature, handling, detail, *where(match))
    select_into = _SELECT_INTO.search(masked)
    if select_into and any(_SELECT_INTO.search(piece) for piece in masked.split(";")):
        report.add("SELECT_INTO", Handling.RULE, "SELECT ... INTO (raises NO_DATA_FOUND / TOO_MANY_ROWS)",
                   line_of(masked, select_into.start()), "SELECT ... INTO")

    rownum = _ROWNUM.search(masked)
    if rownum:
        rownums = len(_ROWNUM.findall(masked))
        top_n = _top_n_rownums(masked)
        if top_n == rownums:
            report.add("ROWNUM_TOP_N", Handling.RULE, "ROWNUM top-N over an ordered subquery", *where(rownum))
        else:
            report.add("ROWNUM", Handling.REVIEW, "ROWNUM outside the top-N pattern", *where(rownum))

    for feature, pattern, detail in RECOGNISED:
        match = pattern.search(masked)
        if match:
            report.add(feature, Handling.REVIEW, detail, *where(match))

    _package_references(masked, report, package and package.lower(), packages)
    _unknown_constructs(masked, report, packages, names)
    return report


def known_names(objects: Iterable) -> set[str]:
    """Lower-case names an object's code may call or use: every uploaded object's name,
    each package member as ``pkg.member`` and bare ``member``, and the schemas."""
    found: set[str] = set()
    for obj in objects:
        found.add(obj.name.lower())
        if getattr(obj, "package", None):
            found.add(f"{obj.package.lower()}.{obj.name.lower()}")
        schema = obj.object_id.split(":", 1)[1].split(".")[0]
        found.add(schema.lower())
    return found


_WORD = re.compile(r'"[^"]*"|[A-Za-z_][\w$#]*')
_ID = r'"[^"]+"|[A-Za-z_][\w$#]*'  # an identifier, quoted or not
_DECLARED = [
    re.compile(rf"(?:^|[(,;]|\bIS\b|\bAS\b|\bDECLARE\b|\bBEGIN\b)\s*({_ID})\s+(?:CONSTANT\s+)?"
               r"(?:IN\s+OUT\s+|IN\s+|OUT\s+)?(?:NOCOPY\s+)?[A-Za-z_][\w$#.%]*", re.I | re.M),
    re.compile(rf"\b(?:TYPE|SUBTYPE|CURSOR)\s+({_ID})", re.I),
    re.compile(rf"\bFOR\s+({_ID})\s+IN\b", re.I),
    re.compile(rf"<<\s*({_ID})\s*>>"),
    re.compile(rf"\b(?:FUNCTION|PROCEDURE|TRIGGER|VIEW)\s+(?:(?:{_ID})\s*\.\s*)?({_ID})", re.I),
    re.compile(rf"(?:\bFROM|\bJOIN|,)\s+[A-Za-z_][\w$#.]*\s+({_ID})", re.I),  # table aliases
]
_CURSOR_ATTRIBUTES = {"FOUND", "NOTFOUND", "ISOPEN", "ROWCOUNT", "BULK_ROWCOUNT", "BULK_EXCEPTIONS"}
#: Function names a pattern above already reports (so they are recognised, never unknown).
_PATTERN_FUNCTIONS = re.compile(
    r"^(?:MONTHS_BETWEEN|NEXT_DAY|LISTAGG|NVL2|INSTR|SYS_GUID|REGEXP_\w+|SYS_CONNECT_BY_PATH|"
    r"RAISE_APPLICATION_ERROR|NVL|DECODE|TRUNC|ADD_MONTHS|LAST_DAY)$", re.I)


def _unknown_constructs(
    masked: str, report: FeatureReport, packages: Mapping[str, PackageSymbols], names: Iterable[str] | None
) -> None:
    """UNKNOWN_CONSTRUCT for reserved words, calls and attributes that are not allow-listed."""
    # Names compare the way Oracle compares them: unquoted names fold to upper case, quoted
    # names are exact ("mystery" and "MYSTERY" are different; MYSTERY and "MYSTERY" are the
    # same). Uploaded names arrive case-folded (normalize_identifier), so they are treated as
    # unquoted; a quoted mixed-case object in the upload therefore fails closed (review).
    known = {n.upper() for n in (names or ())} | {k.upper() for k in packages}
    known |= {member.upper() for pkg in packages.values() for member in pkg.members}
    declared = {_canonical(m.group(1)) for pattern in _DECLARED for m in pattern.finditer(masked)}
    package_members = {k.upper(): {m.upper() for m in pkg.members} for k, pkg in packages.items()}

    def unknown(index: int, text: str, detail: str) -> None:
        report.add(UNKNOWN_CONSTRUCT, Handling.REVIEW, detail, line_of(masked, index), text)

    for match in _WORD.finditer(masked):
        word = match.group(0)
        before = masked[:match.start()].rstrip()
        if before.endswith((".", "%", ":")):
            continue  # part of a qualified name (checked with its first part), an attribute or a bind
        upper = word.upper()
        after = masked[match.end():]
        # INSERT INTO t (a, b) and REFERENCES t (a) name a table and its columns, not a call.
        table_context = re.search(r"\b(?:INTO|REFERENCES|TABLE)$", before, re.I) is not None
        chain = _CHAIN.match(after)
        if chain:
            # The complete qualified identifier, resolved as a whole: schema.routine,
            # package.member or schema.package.member (quoted parts included).
            written = [word, *_PART.findall(chain.group(1))]
            parts = [_canonical(p) for p in written]
            # A system package (DBMS_x.y, SYS.UTL_x.y) is reported by its own finding, which
            # needs the unquoted package name followed by a dot: so only an unquoted part
            # before the last one counts, never the called name itself (utl_thing( is unknown).
            system = any(w.upper().startswith(("DBMS_", "UTL_")) for w in written[:-1])
            # t2.id(+) is the old outer-join marker (OUTER_JOIN_PLUS reports it), not a call.
            outer_join = chain.group(2) and re.match(r"\(\s*\+\s*\)", after[chain.start(2):])
            if chain.group(2) and not table_context and not system and not outer_join and not _qualified_call_known(
                parts, known, package_members, declared
            ):
                text = ".".join(written)
                unknown(match.start(), text + "(",
                        f"call to {text}, which is not in the upload or the allow-list")
            continue
        called = re.match(r"\s*\(", after) is not None and not table_context
        if word.startswith('"'):
            # A quoted call is a call: it fails closed unless that exact identifier is known.
            name = _canonical(word)
            if called and not (name in known or name in declared or name in ALLOWED_FUNCTIONS):
                unknown(match.start(), f"{word}(", f"function {word} is not on the allow-list")
            continue
        if upper == "SQL" and re.match(r"\s*%", after):
            continue  # SQL%ROWCOUNT and friends: IMPLICIT_CURSOR_ATTRIBUTE reports it
        if upper in ORACLE_RESERVED:
            if upper not in ALLOWED_WORDS and not _recognised_at(report, upper):
                unknown(match.start(), upper, f"reserved word {upper} is not on the allow-list")
            continue
        if called and not (
            upper in SYNTAX_WORDS or upper in ALLOWED_FUNCTIONS or _PATTERN_FUNCTIONS.match(upper)
            or upper in known or upper in declared
        ):
            unknown(match.start(), f"{upper}(", f"function {upper} is not on the allow-list")

    for match in re.finditer(r"([A-Za-z_][\w$#]*)\s*%\s*([A-Za-z_]+)", masked):
        prefix, attribute = match.group(1).upper(), match.group(2).upper()
        if attribute in ("TYPE", "ROWTYPE"):
            continue  # %TYPE / %ROWTYPE are a rule
        if prefix == "SQL" and attribute in _CURSOR_ATTRIBUTES:
            continue  # reported as IMPLICIT_CURSOR_ATTRIBUTE
        if attribute in _CURSOR_ATTRIBUTES:
            report.add("CURSOR_ATTRIBUTE", Handling.LLM, "explicit cursor attribute (%FOUND, %NOTFOUND ...)",
                       line_of(masked, match.start()), f"{match.group(1)}%{match.group(2)}")
        else:
            unknown(match.start(), f"%{attribute}", f"attribute %{attribute} is not on the allow-list")


#: Reserved words that belong to a feature already reported for the object (so the object
#: keeps that feature's route, e.g. Gemini for collections and explicit cursors).
_PART_OF = {
    "INDEX": "COLLECTION",
    "TABLE": "COLLECTION",
    "CURSOR": "EXPLICIT_CURSOR",
    "FETCH": "EXPLICIT_CURSOR",
    "CURRENT": "EXPLICIT_CURSOR",
}


#: One to three more parts after a name (".b", ".b.c", ".b.c.d"), then maybe "(".
_PART = re.compile(r'"[^"]*"|[A-Za-z_][\w$#]*')
#: Collection methods that take arguments: the only calls accepted on a declared name
#: (v.EXTEND(1), v.EXISTS(i)). Anything else on a declared record or collection is unknown.
_COLLECTION_METHODS = frozenset({"EXISTS", "DELETE", "EXTEND", "TRIM", "PRIOR", "NEXT"})
_CHAIN = re.compile(r'((?:\s*\.\s*(?:"[^"]*"|[A-Za-z_][\w$#]*)){1,3})\s*(\()?')


def _canonical(identifier: str) -> str:
    """Oracle's view of an identifier: unquoted folds to upper case, quoted stays exact."""
    if len(identifier) >= 2 and identifier[0] == identifier[-1] == '"':
        return identifier[1:-1]
    return identifier.upper()


def _qualified_call_known(
    parts: list[str], known: set[str], packages: Mapping[str, set[str]], declared: set[str]
) -> bool:
    """Whether a qualified call resolves as a whole: a known qualifier alone is not enough.

    ``parts`` are canonical (see _canonical); ``packages`` maps a canonical package name to its
    canonical member names.

    Accepted: a collection method with arguments on a declared name (v.EXTEND(1),
    v.EXISTS(i), see _COLLECTION_METHODS; any other call on a declared record or collection
    is unknown); ``pkg.member`` for a package in ``packages`` (an undeclared
    member is reported by _package_references); ``pkg.member`` from the upload;
    ``schema.routine`` whose schema and routine are both in the upload; and
    ``schema.pkg.member`` whose schema is in the upload and whose member is declared in the
    package (from ``packages``, or ``pkg.member`` in the upload).
    """
    if parts[0] in declared:
        return len(parts) == 2 and parts[1] in _COLLECTION_METHODS
    if len(parts) == 2:
        head, last = parts
        if head in packages:
            return True  # pkg.member: _package_references reports an undeclared member
        return f"{head}.{last}" in known or (head in known and last in known)
    if len(parts) == 3:
        schema, package, member = parts
        if package in packages:
            return schema in known and member in packages[package]
        return schema in known and f"{package}.{member}" in known
    return False


def _recognised_at(report: FeatureReport, word: str) -> bool:
    """A reserved word some finding already reports (ROWID, GOTO, MINUS, INTERSECT ...)."""
    return any(
        word in re.findall(r"[A-Z_$#]+", (f.text or "").upper()) or f.feature in (word, _PART_OF.get(word))
        for f in report.findings
    )


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

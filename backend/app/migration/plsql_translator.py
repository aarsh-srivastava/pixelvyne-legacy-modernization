"""Oracle PL/SQL to PostgreSQL PL/pgSQL, for a listed subset (Stage 2, step 4).

Deterministic and conservative: anything outside the subset is reported as unsupported
(the converter then sends it to Gemini or to a person) instead of being guessed.

Supported:

* Standalone functions and procedures, package members (into a schema named after the
  package, D1), and row or statement triggers (a ``<trigger>_fn()`` trigger function plus
  ``CREATE TRIGGER``).
* Parameters ``IN`` / ``OUT`` / ``IN OUT`` with Oracle types (``NUMBER`` and every integer
  type become ``numeric``, R4; ``DATE`` becomes ``timestamp(0)``) and ``%TYPE``.
* Declarations (``CONSTANT``, ``NOT NULL``, initial values), assignments, ``IF / ELSIF /
  ELSE``, ``CASE`` statements, ``LOOP``, ``WHILE``, numeric ``FOR`` (``REVERSE`` too),
  cursor ``FOR`` loops over a query, ``EXIT`` / ``CONTINUE [WHEN]``, ``RETURN``, ``NULL``,
  ``RAISE``, nested blocks, procedure calls (``CALL``).
* ``SELECT ... INTO`` becomes ``SELECT ... INTO STRICT`` (R2: Oracle raises NO_DATA_FOUND
  and TOO_MANY_ROWS). ``INSERT`` / ``UPDATE`` / ``DELETE``. All SQL and every expression
  goes through ``sql_translator``, so rules R1, R3 and R5-R13 apply everywhere.
* Exception handlers for ``NO_DATA_FOUND``, ``TOO_MANY_ROWS``, ``ZERO_DIVIDE``,
  ``DUP_VAL_ON_INDEX``, ``INVALID_NUMBER``, ``CASE_NOT_FOUND`` and ``OTHERS``.

Oracle meaning kept on purpose:

* R1: text ``IN`` parameters start with ``p := NULLIF(p, '')``, because a caller's ``''``
  is NULL in Oracle. When declarations exist, the routine body becomes an outer block
  (normalisation) around an inner block (declarations and code), so initial values see
  the normalised parameters, as they would in Oracle.
* ``#variable_conflict use_column``: inside SQL, Oracle resolves a name that is both a
  column and a variable to the column. PostgreSQL would raise an error instead.
* A parameter default before an ``OUT`` parameter can't exist in PostgreSQL ("procedure
  OUT parameters cannot appear after one with a default value"). The default is dropped,
  with a warning, and the result is flagged for review; parameters are never reordered,
  because that would break existing callers.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from app.migration.postgres_deployer import DeployTarget
from app.migration.sql_translator import translate_expression, translate_sql


@dataclass(frozen=True)
class ConversionContext:
    """What the translator needs to know about the rest of the database."""

    schema: str = "public"  # the package name for package members
    column_types: Mapping[str, str] = field(default_factory=dict)  # "table.column" -> Oracle type
    known_functions: frozenset[str] = frozenset()  # routines that will exist in PostgreSQL
    function_renames: Mapping[str, str] = field(default_factory=dict)  # net_pay -> payroll_pkg.net_pay
    constants: Mapping[str, str] = field(default_factory=dict)  # name -> literal, inlined (R13)


@dataclass
class PlsqlResult:
    code: str
    target: DeployTarget | None
    warnings: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    review_reasons: list[str] = field(default_factory=list)

    @property
    def successful(self) -> bool:
        return not self.unsupported and bool(self.code)

    @property
    def needs_review(self) -> bool:
        return bool(self.review_reasons)


class _Unsupported(Exception):
    pass


# --- types ---------------------------------------------------------------------------

_INTEGER_TYPES = {"INTEGER", "INT", "SMALLINT", "PLS_INTEGER", "BINARY_INTEGER", "NATURAL", "NATURALN",
                  "POSITIVE", "POSITIVEN", "SIGNTYPE", "SIMPLE_INTEGER"}


def translate_type(oracle_type: str, column_types: Mapping[str, str] | None = None) -> tuple[str, str]:
    """(PostgreSQL type, category) for an Oracle type. Category: date, number, text, bool, binary, other.

    Raises ValueError for types with no safe mapping.
    """
    raw = " ".join(oracle_type.split())
    t = raw.upper()
    if t.endswith("%TYPE"):
        anchor = raw[: -len("%TYPE")].strip().lower()
        base = (column_types or {}).get(anchor)
        category = translate_type(base)[1] if base else "other"
        return f"{anchor}%TYPE", category
    if t.endswith("%ROWTYPE"):
        return f"{raw[: -len('%ROWTYPE')].strip().lower()}%ROWTYPE", "other"

    m = re.fullmatch(r"(NUMBER|NUMERIC|DECIMAL|DEC)\s*(?:\(\s*(\d+|\*)\s*(?:,\s*(-?\d+)\s*)?\))?", t)
    if m:
        precision, scale = m.group(2), m.group(3)
        if precision in (None, "*"):
            return ("numeric" if scale in (None, "0") or precision is None else f"numeric(38, {scale})"), "number"
        return (f"numeric({precision}, {scale})" if scale is not None else f"numeric({precision})"), "number"
    if t in _INTEGER_TYPES:
        return "numeric", "number"  # R4: Oracle integer division keeps decimals
    if re.fullmatch(r"FLOAT(\s*\(\s*\d+\s*\))?|REAL|DOUBLE PRECISION", t):
        return "numeric", "number"
    if t == "BINARY_DOUBLE":
        return "double precision", "number"
    if t == "BINARY_FLOAT":
        return "real", "number"
    m = re.fullmatch(r"(N?VARCHAR2|VARCHAR|N?CHAR|CHARACTER)\s*(?:\(\s*(\d+)(?:\s+(?:BYTE|CHAR))?\s*\))?", t)
    if m:
        kind = "char" if m.group(1) in ("CHAR", "NCHAR", "CHARACTER") else "varchar"
        return (f"{kind}({m.group(2)})" if m.group(2) else kind), "text"
    if t in ("CLOB", "NCLOB", "LONG"):
        return "text", "text"
    if t == "DATE":
        return "timestamp(0)", "date"
    m = re.fullmatch(r"TIMESTAMP\s*(?:\(\s*(\d)\s*\))?(\s+WITH\s+(?:LOCAL\s+)?TIME\s+ZONE)?", t)
    if m:
        base = "timestamptz" if m.group(2) else "timestamp"
        return (f"{base}({m.group(1)})" if m.group(1) else base), "date"
    if t == "BOOLEAN":
        return "boolean", "bool"
    if re.fullmatch(r"BLOB|RAW\s*\(\s*\d+\s*\)|LONG RAW", t):
        return "bytea", "binary"
    raise ValueError(f"Oracle type {raw} has no rule")


def column_types_from_ddl(sources: Iterable[str]) -> dict[str, str]:
    """{"table.column": Oracle type} from Oracle CREATE TABLE statements."""
    columns: dict[str, str] = {}
    for source in sources:
        try:
            statements = sqlglot.parse(source, read="oracle")
        except sqlglot.errors.ParseError:
            continue
        for stmt in statements:
            if isinstance(stmt, exp.Create) and (stmt.kind or "").upper() == "TABLE":
                schema = stmt.this
                table = schema.this.name.lower() if isinstance(schema, exp.Schema) else None
                for col in getattr(schema, "expressions", []):
                    if table and isinstance(col, exp.ColumnDef) and col.args.get("kind") is not None:
                        columns[f"{table}.{col.name.lower()}"] = col.args["kind"].sql(dialect="oracle")
    return columns


def _name_sets(column_types: Mapping[str, str]) -> tuple[set[str], set[str]]:
    """Column names that are dates / numbers. A bare name counts only if it has the same
    category in every table that has it; otherwise TRUNC(name) is reported, not guessed."""
    seen: dict[str, set[str]] = {}
    for key, oracle_type in column_types.items():
        seen.setdefault(key.split(".")[-1], set()).add(_category(oracle_type))
    dates = {name for name, cats in seen.items() if cats == {"date"}}
    numbers = {name for name, cats in seen.items() if cats == {"number"}}
    return dates, numbers


def _category(oracle_type: str) -> str:
    try:
        return translate_type(oracle_type)[1]
    except ValueError:
        return "other"


# --- masking and tokens ---------------------------------------------------------------

def _mask(source: str) -> str:
    """Same length as ``source``: strings become 'xxx' (an empty '' stays ''), comments become spaces."""
    out = list(source)
    i, n = 0, len(source)
    while i < n:
        two = source[i:i + 2]
        if two == "--":
            end = source.find("\n", i)
            end = n if end == -1 else end
            out[i:end] = " " * (end - i)
            i = end
        elif two == "/*":
            end = source.find("*/", i + 2)
            if end == -1:
                raise _Unsupported("unterminated /* comment")
            end += 2
            out[i:end] = [c if c == "\n" else " " for c in source[i:end]]
            i = end
        elif source[i] in "qQ" and source[i + 1:i + 2] == "'" and (i == 0 or not source[i - 1].isalnum()):
            raise _Unsupported("q-quoted string literals have no rule yet")
        elif source[i] == "'":
            j = i + 1
            while True:
                if j >= n:
                    raise _Unsupported("unterminated string literal")
                if source[j] == "'":
                    if source[j + 1:j + 2] == "'":
                        j += 2
                        continue
                    break
                j += 1
            out[i + 1:j] = "x" * (j - i - 1)
            i = j + 1
        elif source[i] == '"':
            end = source.find('"', i + 1)
            if end == -1:
                raise _Unsupported("unterminated quoted identifier")
            i = end + 1
        else:
            i += 1
    return "".join(out)


_TOKEN = re.compile(
    r"\s+|(?P<str>'[^']*')|(?P<qid>\"[^\"]*\")|(?P<num>\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)"
    r"|(?P<word>[A-Za-z_][\w$#]*)|(?P<label><<|>>)"
    r"|(?P<op>:=|\.\.|\|\||<=|>=|<>|!=|=>|\*\*|[-+*/=<>(),;.%:@\[\]])"
)


@dataclass(frozen=True)
class _Tok:
    kind: str
    text: str  # upper-cased for words
    start: int
    end: int


def _tokens(masked: str) -> list[_Tok]:
    tokens, pos = [], 0
    while pos < len(masked):
        m = _TOKEN.match(masked, pos)
        if not m:
            raise _Unsupported(f"unexpected character {masked[pos]!r}")
        if m.lastgroup:
            text = m.group(m.lastgroup)
            tokens.append(_Tok(m.lastgroup, text.upper() if m.lastgroup == "word" else text, m.start(), m.end()))
        pos = m.end()
    return tokens


_EXCEPTIONS = {
    "NO_DATA_FOUND": "no_data_found", "TOO_MANY_ROWS": "too_many_rows", "ZERO_DIVIDE": "division_by_zero",
    "DUP_VAL_ON_INDEX": "unique_violation", "INVALID_NUMBER": "invalid_text_representation",
    "CASE_NOT_FOUND": "case_not_found", "OTHERS": "OTHERS",
}
_NOT_YET = {
    "COMMIT": "transaction control (COMMIT)", "ROLLBACK": "transaction control (ROLLBACK)",
    "SAVEPOINT": "transaction control (SAVEPOINT)", "EXECUTE": "dynamic SQL (EXECUTE IMMEDIATE)",
    "OPEN": "explicit cursors (OPEN)", "FETCH": "explicit cursors (FETCH)", "CLOSE": "explicit cursors (CLOSE)",
    "FORALL": "bulk operations (FORALL)", "GOTO": "GOTO", "PIPE": "pipelined functions", "MERGE": "MERGE",
}


def _indent(lines: list[str], depth: int = 1) -> list[str]:
    return [("  " * depth + line) if line else line for line in lines]


# --- the parser -----------------------------------------------------------------------

class _Translator:
    def __init__(self, source: str, ctx: ConversionContext, *, trigger: bool = False) -> None:
        self.ctx = ctx
        self.trigger = trigger
        masked = _mask(source)
        if trigger:
            # :NEW.x / :OLD.x -> NEW.x / OLD.x, keeping every offset.
            for m in re.finditer(r":(?=(?:NEW|OLD)\b)", masked, re.I):
                source = source[: m.start()] + " " + source[m.end():]
                masked = masked[: m.start()] + " " + masked[m.end():]
        self.source, self.masked = source, masked
        self.toks = _tokens(masked)
        self.pos = 0
        self.unsupported: list[str] = []
        self.warnings: list[str] = []
        self.review: list[str] = []
        dates, numbers = _name_sets(ctx.column_types)
        self.date_names, self.number_names = set(dates), set(numbers)
        self.records: list[str] = []

    # tokens
    def peek(self, k: int = 0) -> _Tok | None:
        i = self.pos + k
        return self.toks[i] if i < len(self.toks) else None

    def word(self, k: int = 0) -> str | None:
        t = self.peek(k)
        return t.text if t and t.kind == "word" else None

    def is_op(self, op: str, k: int = 0) -> bool:
        t = self.peek(k)
        return bool(t and t.kind in ("op", "label") and t.text == op)

    def take(self) -> _Tok:
        t = self.peek()
        if t is None:
            raise _Unsupported("unexpected end of source")
        self.pos += 1
        return t

    def expect_word(self, *words: str) -> _Tok:
        t = self.take()
        if t.kind != "word" or t.text not in words:
            raise _Unsupported(f"expected {' or '.join(words)}, found {self.source[t.start:t.end]!r}")
        return t

    def expect_op(self, op: str) -> _Tok:
        t = self.take()
        if t.text != op:
            raise _Unsupported(f"expected {op!r}, found {self.source[t.start:t.end]!r}")
        return t

    def ident(self) -> str:
        t = self.take()
        if t.kind == "word":
            return t.text.lower()
        if t.kind == "qid":
            raise _Unsupported("quoted identifiers have no rule yet")
        raise _Unsupported(f"expected a name, found {self.source[t.start:t.end]!r}")

    def span_until(self, words: set[str] = frozenset(), ops: set[str] = frozenset()) -> tuple[int, int]:
        """Source span from here to the first stop at depth 0 (parentheses and CASE ... END)."""
        start_tok, start_pos = self.peek(), self.pos
        if start_tok is None:
            raise _Unsupported("unexpected end of source")
        depth = case_depth = 0
        while True:
            t = self.peek()
            if t is None:
                raise _Unsupported("unexpected end of source")
            if depth == 0 and case_depth == 0 and (
                (t.kind == "word" and t.text in words) or (t.kind == "op" and t.text in ops)
            ):
                break
            if t.kind == "op" and t.text == "(":
                depth += 1
            elif t.kind == "op" and t.text == ")":
                if depth == 0:
                    break
                depth -= 1
            elif t.kind == "word" and t.text == "CASE":
                case_depth += 1
            elif t.kind == "word" and t.text == "END" and case_depth:
                case_depth -= 1
            self.pos += 1
        if self.pos == start_pos:
            raise _Unsupported(f"expected an expression at {self.source[start_tok.start:start_tok.end]!r}")
        return start_tok.start, self.toks[self.pos - 1].end

    # translation of pieces
    def _sql_context(self) -> dict:
        return dict(
            date_names=self.date_names,
            number_names=self.number_names,
            known_functions=self.ctx.known_functions,
            function_renames=self.ctx.function_renames,
            constants=self.ctx.constants,
        )

    def _clean(self, a: int, b: int) -> tuple[str, str]:
        """source[a:b] ready for SQLGlot, and its masked twin (same length).

        Comments are removed and whitespace runs outside literals become one space, so
        a line comment can't swallow the rest of a statement once lines are joined.
        String literals and quoted identifiers are copied exactly: spaces, tabs,
        newlines and '' escapes inside them are data.
        """
        text, masked = self.source[a:b], self.masked[a:b]
        out: list[str] = []
        out_masked: list[str] = []
        i, n = 0, len(text)
        while i < n:
            ch = masked[i]
            if ch in "'\"":
                # In the masked text a literal's inside is all 'x' (escapes included), so the
                # next quote of the same kind closes it.
                j = masked.find(ch, i + 1)
                j = n - 1 if j == -1 else j
                out.append(text[i:j + 1])
                out_masked.append(masked[i:j + 1])
                i = j + 1
            elif ch.isspace():  # real whitespace, or a comment (masked to spaces)
                while i < n and masked[i].isspace():
                    i += 1
                if out and out[-1] != " ":
                    out.append(" ")
                    out_masked.append(" ")
            else:
                out.append(text[i])
                out_masked.append(ch)
                i += 1
        return "".join(out).strip(), "".join(out_masked).strip()

    def _prepare(self, a: int, b: int) -> str:
        text, masked = self._clean(a, b)
        if self.trigger:
            ops = {"INSERTING": "(TG_OP = 'INSERT')", "UPDATING": "(TG_OP = 'UPDATE')",
                   "DELETING": "(TG_OP = 'DELETE')"}
            for m in reversed(list(re.finditer(r"\b(INSERTING|UPDATING|DELETING)\b(\s*\()?", masked, re.I))):
                if m.group(2):
                    raise _Unsupported(f"{m.group(1).upper()}('column') has no rule yet")
                text = text[: m.start()] + ops[m.group(1).upper()] + text[m.end():]
        if re.search(r"\bSQLCODE\b", masked, re.I):
            raise _Unsupported("SQLCODE has no rule yet (PostgreSQL's SQLSTATE is text)")
        if re.search(r"\bSQL\s*%\s*(ROWCOUNT|FOUND|NOTFOUND|ISOPEN)\b", masked, re.I):
            raise _Unsupported("SQL%ROWCOUNT / SQL%FOUND have no rule yet")
        return text

    def expr(self, a: int, b: int) -> str:
        text = self._prepare(a, b)
        result = translate_expression(text, **self._sql_context())
        self.unsupported.extend(f"{text[:60]}: {u}" for u in result.unsupported_features)
        return result.target_sql or text

    def sql(self, text: str) -> str:
        result = translate_sql(text, **self._sql_context())
        self.unsupported.extend(f"{text[:60]}: {u}" for u in result.unsupported_features)
        return result.target_sql or text

    def expr_until(self, words: set[str] = frozenset(), ops: set[str] = frozenset()) -> str:
        return self.expr(*self.span_until(words, ops))

    def remember(self, name: str, category: str) -> None:
        if category == "date":
            self.date_names.add(name)
        elif category == "number":
            self.number_names.add(name)

    def type_text(self, a: int, b: int) -> tuple[str, str]:
        try:
            return translate_type(self.source[a:b], self.ctx.column_types)
        except ValueError as exc:
            raise _Unsupported(str(exc)) from None

    # declarations
    def declarations(self, stop: str = "BEGIN") -> list[str]:
        lines = []
        while self.word() != stop:
            first = self.word()
            if first in ("CURSOR", "TYPE", "SUBTYPE", "PRAGMA", "FUNCTION", "PROCEDURE"):
                raise _Unsupported(f"{first} declarations have no rule yet")
            name = self.ident()
            constant = False
            if self.word() == "CONSTANT":
                self.take()
                constant = True
            if self.word() == "EXCEPTION":
                raise _Unsupported("user-defined exceptions have no rule yet")
            pg_type, category = self.type_text(*self.span_until({"NOT", "DEFAULT"}, {":=", ";"}))
            not_null = False
            if self.word() == "NOT":
                self.take()
                self.expect_word("NULL")
                not_null = True
            init = None
            if self.is_op(":=") or self.word() == "DEFAULT":
                self.take()
                init = self.expr_until(ops={";"})
            self.expect_op(";")
            self.remember(name, category)
            line = f"{name} {'CONSTANT ' if constant else ''}{pg_type}{' NOT NULL' if not_null else ''}"
            lines.append(line + (f" := {init};" if init is not None else ";"))
        return lines

    # statements
    def statements(self, stop: set[str]) -> list[str]:
        lines: list[str] = []
        while True:
            t = self.peek()
            if t is None:
                raise _Unsupported("unexpected end of source (missing END?)")
            if t.kind == "word" and t.text in stop:
                return lines
            lines.extend(self.statement())

    def statement(self) -> list[str]:
        t = self.peek()
        if t.kind == "label":
            raise _Unsupported("labels (<<name>>) have no rule yet")
        w = t.text if t.kind == "word" else None
        if w in _NOT_YET:
            raise _Unsupported(f"{_NOT_YET[w]} has no rule yet")
        handler = {
            "IF": self.if_stmt, "CASE": self.case_stmt, "LOOP": self.loop_stmt, "WHILE": self.while_stmt,
            "FOR": self.for_stmt, "EXIT": self.exit_stmt, "CONTINUE": self.exit_stmt,
            "RETURN": self.return_stmt, "NULL": self.null_stmt, "RAISE": self.raise_stmt,
            "BEGIN": self.block_stmt, "DECLARE": self.block_stmt, "SELECT": self.select_stmt,
            "INSERT": self.dml_stmt, "UPDATE": self.dml_stmt, "DELETE": self.dml_stmt,
        }.get(w)
        if handler:
            return handler()
        return self.assign_or_call()

    def if_stmt(self) -> list[str]:
        self.take()
        lines = [f"IF {self.expr_until({'THEN'})} THEN"]
        self.expect_word("THEN")
        lines += _indent(self.statements({"ELSIF", "ELSE", "END"}))
        while self.word() == "ELSIF":
            self.take()
            lines.append(f"ELSIF {self.expr_until({'THEN'})} THEN")
            self.expect_word("THEN")
            lines += _indent(self.statements({"ELSIF", "ELSE", "END"}))
        if self.word() == "ELSE":
            self.take()
            lines.append("ELSE")
            lines += _indent(self.statements({"END"}))
        self.expect_word("END")
        self.expect_word("IF")
        self.expect_op(";")
        return lines + ["END IF;"]

    def case_stmt(self) -> list[str]:
        self.take()
        lines = ["CASE" if self.word() == "WHEN" else f"CASE {self.expr_until({'WHEN'})}"]
        while self.word() == "WHEN":
            self.take()
            lines.append(f"  WHEN {self.expr_until({'THEN'})} THEN")
            self.expect_word("THEN")
            lines += _indent(self.statements({"WHEN", "ELSE", "END"}), 2)
        if self.word() == "ELSE":
            self.take()
            lines.append("  ELSE")
            lines += _indent(self.statements({"END"}), 2)
        self.expect_word("END")
        self.expect_word("CASE")
        self.expect_op(";")
        return lines + ["END CASE;"]

    def loop_body(self, header: str) -> list[str]:
        self.expect_word("LOOP")
        lines = [header] + _indent(self.statements({"END"}))
        self.expect_word("END")
        self.expect_word("LOOP")
        if self.word():
            raise _Unsupported("loop labels have no rule yet")
        self.expect_op(";")
        return lines + ["END LOOP;"]

    def loop_stmt(self) -> list[str]:
        return self.loop_body("LOOP")

    def while_stmt(self) -> list[str]:
        self.take()
        return self.loop_body(f"WHILE {self.expr_until({'LOOP'})} LOOP")

    def for_stmt(self) -> list[str]:
        self.take()
        var = self.ident()
        self.expect_word("IN")
        reverse = self.word() == "REVERSE"
        if reverse:
            self.take()
        if self.is_op("(") and self.word(1) == "SELECT" and not reverse:
            self.take()
            a, b = self.span_until(ops={")"})
            self.expect_op(")")
            query = self.sql(self._prepare(a, b))
            if var not in self.records:
                self.records.append(var)
            return self.loop_body(f"FOR {var} IN {query} LOOP")
        if self.is_op("("):
            raise _Unsupported("FOR loop over a parenthesised expression has no rule")
        start = self.pos
        try:
            low = self.expr_until(ops={".."})
            self.expect_op("..")
        except _Unsupported:
            self.pos = start
            raise _Unsupported("cursor FOR loops over a declared cursor have no rule yet") from None
        high = self.expr_until({"LOOP"})
        # PostgreSQL's REVERSE takes the bounds the other way round.
        bounds = f"REVERSE {high}..{low}" if reverse else f"{low}..{high}"
        return self.loop_body(f"FOR {var} IN {bounds} LOOP")

    def exit_stmt(self) -> list[str]:
        verb = self.take().text
        if self.word() not in (None, "WHEN"):
            raise _Unsupported("loop labels have no rule yet")
        if self.word() == "WHEN":
            self.take()
            cond = self.expr_until(ops={";"})
            self.expect_op(";")
            return [f"{verb} WHEN {cond};"]
        self.expect_op(";")
        return [f"{verb};"]

    def return_stmt(self) -> list[str]:
        self.take()
        if self.is_op(";"):
            self.take()
            if self.trigger:
                raise _Unsupported("RETURN inside a trigger has no rule yet")
            return ["RETURN;"]
        value = self.expr_until(ops={";"})
        self.expect_op(";")
        return [f"RETURN {value};"]

    def null_stmt(self) -> list[str]:
        self.take()
        self.expect_op(";")
        return ["NULL;"]

    def raise_stmt(self) -> list[str]:
        self.take()
        if self.is_op(";"):
            self.take()
            return ["RAISE;"]
        name = self.take().text
        self.expect_op(";")
        if name not in _EXCEPTIONS or name == "OTHERS":
            raise _Unsupported(f"RAISE {name} has no rule yet")
        return [f"RAISE {_EXCEPTIONS[name]};"]

    def block_stmt(self) -> list[str]:
        decls: list[str] = []
        if self.word() == "DECLARE":
            self.take()
            decls = self.declarations()
        return self.block(decls)

    def block(self, decls: list[str], trailer: list[str] | None = None) -> list[str]:
        """BEGIN ... [EXCEPTION ...] END [name]; (the BEGIN is next)."""
        self.expect_word("BEGIN")
        body = self.statements({"EXCEPTION", "END"})
        handlers = self.handlers() if self.word() == "EXCEPTION" else []
        self.expect_word("END")
        if self.word():
            self.take()  # END name
        self.expect_op(";")
        lines = (["DECLARE"] + _indent(decls)) if decls else []
        lines += ["BEGIN"] + _indent(body)
        if handlers:
            lines += ["EXCEPTION"] + _indent(handlers)
        return lines + ["END;"]

    def handlers(self) -> list[str]:
        self.expect_word("EXCEPTION")
        lines: list[str] = []
        while self.word() == "WHEN":
            self.take()
            names = [self.take().text]
            while self.word() == "OR":
                self.take()
                names.append(self.take().text)
            unknown = [n for n in names if n not in _EXCEPTIONS]
            if unknown:
                raise _Unsupported(f"exception {unknown[0]} has no rule yet")
            self.expect_word("THEN")
            lines.append(f"WHEN {' OR '.join(_EXCEPTIONS[n] for n in names)} THEN")
            lines += _indent(self.statements({"WHEN", "END"}))
        if not lines:
            raise _Unsupported("EXCEPTION section without handlers")
        return lines

    def select_stmt(self) -> list[str]:
        a, b = self.span_until(ops={";"})
        self.expect_op(";")
        toks = [t for t in self.toks if a <= t.start < b]
        depth, into = 0, None
        for k, t in enumerate(toks):
            depth += t.text == "(" and t.kind == "op"
            depth -= t.text == ")" and t.kind == "op"
            if depth == 0 and t.kind == "word" and t.text == "INTO":
                into = k
                break
            if depth == 0 and t.kind == "word" and t.text == "FROM":
                break
        if into is None:
            raise _Unsupported("SELECT without INTO inside PL/SQL")
        if into > 0 and toks[into - 1].text == "COLLECT":
            raise _Unsupported("BULK COLLECT has no rule yet")
        end = next((k for k in range(into + 1, len(toks))
                    if toks[k].kind == "word" and toks[k].text in ("FROM", "WHERE")), len(toks))
        targets = self._clean(toks[into + 1].start, toks[end - 1].end)[0].lower()
        before = self._prepare(a, toks[into].start)
        after = self._prepare(toks[end].start, b) if end < len(toks) else ""
        translated = self.sql(f"{before} {after}".strip())
        cut = _top_level_from(translated)
        if cut is None:
            return [f"{translated} INTO STRICT {targets};"]
        return [f"{translated[:cut].rstrip()} INTO STRICT {targets} {translated[cut:]};"]

    def dml_stmt(self) -> list[str]:
        a, b = self.span_until(ops={";"})
        self.expect_op(";")
        if re.search(r"\bRETURNING\b", self.masked[a:b], re.I):
            raise _Unsupported("RETURNING ... INTO has no rule yet")
        return [f"{self.sql(self._prepare(a, b))};"]

    def assign_or_call(self) -> list[str]:
        start = self.pos
        first = self.peek()
        if first.kind not in ("word",):
            raise _Unsupported(f"statement starting with {self.source[first.start:first.end]!r} has no rule")
        names = [self.ident()]
        while self.is_op("."):
            self.take()
            names.append(self.ident())
        if self.is_op(":="):
            target = ".".join(names)
            self.take()
            value = self.expr_until(ops={";"})
            self.expect_op(";")
            return [f"{target} := {value};"]
        if self.is_op("(") or self.is_op(";"):
            routine = ".".join(names)
            if routine.split(".")[0] in ("dbms_output", "dbms_lock", "utl_file", "dbms_sql"):
                raise _Unsupported(f"{routine} has no rule yet")
            if routine not in self.ctx.known_functions and routine not in self.ctx.function_renames:
                raise _Unsupported(f"call to unknown routine {routine}")
            if self.is_op("("):
                self.take()
                args = self.span_until(ops={")"}) if not self.is_op(")") else None
                self.expect_op(")")
                arg_text = self._prepare(*args) if args else ""
            else:
                arg_text = ""
            self.expect_op(";")
            translated = translate_expression(f"{routine}({arg_text})", **self._sql_context())
            self.unsupported.extend(f"call {routine}: {u}" for u in translated.unsupported_features)
            return [f"CALL {translated.target_sql or routine + '(' + arg_text + ')'};"]
        if self.is_op("("):
            raise _Unsupported("assignments to collection elements have no rule yet")
        self.pos = start
        t = self.peek()
        raise _Unsupported(f"statement starting with {self.source[t.start:t.end]!r} has no rule")


def _top_level_from(sql: str) -> int | None:
    """Index of the first FROM outside parentheses and strings, or None."""
    depth, i, n = 0, 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            i = sql.find("'", i + 1) + 1 or n
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and sql[i:i + 4].upper() == "FROM" and (i == 0 or not sql[i - 1].isalnum()) \
                and (i + 4 == n or not (sql[i + 4].isalnum() or sql[i + 4] == "_")):
            return i
        i += 1
    return None


# --- routines and triggers ------------------------------------------------------------

_EVENT = r"(?:INSERT|DELETE|UPDATE(?:\s+OF\s+[A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)?)"
_EVENTS = re.compile(rf"{_EVENT}(?:\s+OR\s+{_EVENT})*", re.I)

_ROUTINE_OPTIONS = {"DETERMINISTIC", "AUTHID", "DEFINER", "CURRENT_USER", "PARALLEL_ENABLE", "RESULT_CACHE"}


def _dollar_tag(body: str) -> str:
    for tag in ("$fn$", "$pv_fn$", "$pv_body_1$"):
        if tag not in body:
            return tag
    raise _Unsupported("no free $-quote tag")


def _routine(tr: _Translator, kind: str, name: str) -> PlsqlResult:
    if tr.word() == "CREATE":
        tr.take()
        if tr.word() == "OR":
            tr.take()
            tr.expect_word("REPLACE")
        if tr.word() in ("EDITIONABLE", "NONEDITIONABLE"):
            tr.take()
    found = tr.expect_word("FUNCTION", "PROCEDURE").text.lower()
    if found != kind:
        raise _Unsupported(f"source is a {found}, expected a {kind}")
    parts = [tr.ident()]
    while tr.is_op("."):
        tr.take()
        parts.append(tr.ident())
    if parts[-1] != name:
        raise _Unsupported(f"source defines {parts[-1]!r}, expected {name!r}")

    params: list[str] = []
    text_params: list[str] = []
    out_seen_after_default = False
    defaults_seen = False
    if tr.is_op("("):
        tr.take()
        while True:
            pname = tr.ident()
            mode = "IN"
            if tr.word() in ("IN", "OUT"):
                mode = tr.take().text
                if mode == "IN" and tr.word() == "OUT":
                    tr.take()
                    mode = "IN OUT"
            if tr.word() == "NOCOPY":
                tr.take()
            pg_type, category = tr.type_text(*tr.span_until({"DEFAULT"}, {",", ")", ":="}))
            default = None
            if tr.is_op(":=") or tr.word() == "DEFAULT":
                tr.take()
                default = tr.expr_until(ops={",", ")"})
            tr.remember(pname, category)
            if kind == "function" and mode != "IN":
                raise _Unsupported("functions with OUT parameters have no rule yet")
            if mode == "OUT" and defaults_seen:
                out_seen_after_default = True
            defaults_seen = defaults_seen or default is not None
            if category == "text" and mode in ("IN", "IN OUT"):
                text_params.append(pname)
            pg_mode = {"IN": "", "OUT": "OUT ", "IN OUT": "INOUT "}[mode]
            params.append((pname, pg_mode, pg_type, default))
            if tr.is_op(","):
                tr.take()
                continue
            tr.expect_op(")")
            break

    if out_seen_after_default:
        dropped = [p[0] for p in params if p[3] is not None]
        tr.warnings.append(
            f"PostgreSQL can't keep a default before an OUT parameter; dropped the default of {', '.join(dropped)}. "
            "Callers must pass it."
        )
        tr.review.append(f"parameter default dropped ({', '.join(dropped)})")
        params = [(n, m, t, None) for n, m, t, _ in params]

    returns = ""
    if kind == "function":
        tr.expect_word("RETURN")
        a, b = tr.span_until({"IS", "AS", *_ROUTINE_OPTIONS, "PIPELINED"})
        returns = tr.type_text(a, b)[0]
        if "%" in returns:
            raise _Unsupported("RETURN ...%TYPE has no rule yet")
    while tr.word() in _ROUTINE_OPTIONS or tr.word() == "PIPELINED":
        option = tr.take().text
        if option == "PIPELINED":
            raise _Unsupported("pipelined functions have no rule yet")
        if option == "AUTHID":
            tr.warnings.append("AUTHID ignored: converted routines run with the caller's rights")
    tr.expect_word("IS", "AS")
    decls = tr.declarations()

    # The body (BEGIN ... END name;), translated as an inner block.
    inner = tr.block([])
    trailing = tr.peek()
    if trailing is not None and not (trailing.kind == "op" and trailing.text == "/" and tr.pos == len(tr.toks) - 1):
        raise _Unsupported("unexpected text after the end of the routine")

    decls = decls + [f"{r} record;" for r in tr.records]
    normalise = [f"{p} := NULLIF({p}, '');" for p in text_params]
    if normalise and decls:
        body = ["BEGIN"] + _indent(normalise + ["DECLARE"] + _indent(decls) + inner) + ["END;"]
    elif normalise:
        body = ["BEGIN"] + _indent(normalise) + inner[1:]
    else:
        body = (["DECLARE"] + _indent(decls) if decls else []) + inner

    schema = tr.ctx.schema
    qualified = name if schema == "public" else f"{schema}.{name}"
    signature = ", ".join(f"{n} {m}{t}{f' DEFAULT {d}' if d is not None else ''}" for n, m, t, d in params)
    body_text = "\n".join(["#variable_conflict use_column", *body])
    tag = _dollar_tag(body_text)
    head = f"CREATE OR REPLACE {kind.upper()} {qualified}({signature})"
    if kind == "function":
        head += f" RETURNS {returns}"
    code = f"{head}\nLANGUAGE plpgsql AS {tag}\n{body_text}\n{tag};\n"
    if schema != "public":
        code = f"CREATE SCHEMA IF NOT EXISTS {schema};\n" + code
    return PlsqlResult(
        code=code,
        target=DeployTarget(kind, name, schema=schema),
        warnings=tr.warnings,
        unsupported=tr.unsupported,
        review_reasons=tr.review,
    )


def _trigger(tr: _Translator, name: str) -> PlsqlResult:
    tr.expect_word("CREATE")
    if tr.word() == "OR":
        tr.take()
        tr.expect_word("REPLACE")
    if tr.word() in ("EDITIONABLE", "NONEDITIONABLE"):
        tr.take()
    tr.expect_word("TRIGGER")
    parts = [tr.ident()]
    while tr.is_op("."):
        tr.take()
        parts.append(tr.ident())
    if parts[-1] != name:
        raise _Unsupported(f"source defines trigger {parts[-1]!r}, expected {name!r}")
    timing = tr.expect_word("BEFORE", "AFTER", "INSTEAD").text
    if timing == "INSTEAD":
        raise _Unsupported("INSTEAD OF triggers have no rule yet")
    a, b = tr.span_until({"ON"})
    events_text = tr._clean(a, b)[0]
    if not _EVENTS.fullmatch(events_text):
        raise _Unsupported(f"trigger events {events_text!r} have no rule")
    events = {w.upper() for w in re.findall(r"\b(INSERT|UPDATE|DELETE)\b", events_text, re.I)}
    tr.expect_word("ON")
    table = [tr.ident()]
    while tr.is_op("."):
        tr.take()
        table.append(tr.ident())
    table_name = table[-1]
    for_each_row = False
    when = None
    while tr.word() not in ("BEGIN", "DECLARE"):
        w = tr.word()
        if w == "FOR":
            tr.take()
            tr.expect_word("EACH")
            tr.expect_word("ROW")
            for_each_row = True
        elif w == "WHEN":
            tr.take()
            tr.expect_op("(")
            when = tr.expr_until(ops={")"})
            tr.expect_op(")")
        elif w == "ENABLE":
            tr.take()
        elif w in ("REFERENCING", "FOLLOWS", "PRECEDES", "COMPOUND", "DISABLE"):
            raise _Unsupported(f"trigger option {w} has no rule yet")
        else:
            t = tr.peek()
            raise _Unsupported(f"unexpected {tr.source[t.start:t.end]!r} in trigger header")
    decls: list[str] = []
    if tr.word() == "DECLARE":
        tr.take()
        decls = tr.declarations()
    inner = tr.block([])
    trailing = tr.peek()
    if trailing is not None and not (trailing.kind == "op" and trailing.text == "/" and tr.pos == len(tr.toks) - 1):
        raise _Unsupported("unexpected text after the end of the trigger")

    if not for_each_row or timing == "AFTER":
        returning = ["RETURN NULL;"]
    elif events == {"DELETE"}:
        returning = ["RETURN OLD;"]
    elif "DELETE" in events:
        returning = ["IF TG_OP = 'DELETE' THEN", "  RETURN OLD;", "END IF;", "RETURN NEW;"]
    else:
        returning = ["RETURN NEW;"]
    has_handlers = "EXCEPTION" in inner
    if has_handlers:
        body = ["BEGIN"] + _indent(inner) + _indent(returning) + ["END;"]
    else:
        body = ["BEGIN"] + inner[1:-1] + _indent(returning) + ["END;"]
    decls = decls + [f"{r} record;" for r in tr.records]
    if decls:
        body = ["DECLARE"] + _indent(decls) + body

    function = f"{name}_fn"
    schema = tr.ctx.schema
    qualified_fn = function if schema == "public" else f"{schema}.{function}"
    body_text = "\n".join(["#variable_conflict use_column", *body])
    tag = _dollar_tag(body_text)
    events_pg = " ".join(events_text.lower().split())
    lines = [
        f"CREATE OR REPLACE FUNCTION {qualified_fn}() RETURNS trigger",
        f"LANGUAGE plpgsql AS {tag}",
        body_text,
        f"{tag};",
        "",
        f"CREATE OR REPLACE TRIGGER {name}",
        f"{timing} {events_pg} ON {table_name}",
        f"FOR EACH {'ROW' if for_each_row else 'STATEMENT'}",
    ]
    if when:
        if not for_each_row:
            raise _Unsupported("WHEN on a statement trigger")
        lines.append(f"WHEN ({when})")
    lines.append(f"EXECUTE FUNCTION {qualified_fn}();")
    return PlsqlResult(
        code="\n".join(lines) + "\n",
        target=DeployTarget("trigger", name, schema=schema, trigger_table=table_name, trigger_function=function),
        warnings=tr.warnings,
        unsupported=tr.unsupported,
        review_reasons=tr.review,
    )


def translate_plsql(source: str, *, kind: str, name: str, context: ConversionContext | None = None) -> PlsqlResult:
    """Translate one Oracle function, procedure or trigger. ``name`` is its IR name (lower case)."""
    ctx = context or ConversionContext()
    if kind not in ("function", "procedure", "trigger"):
        return PlsqlResult("", None, unsupported=[f"{kind} objects are not PL/SQL routines"])
    try:
        tr = _Translator(source, ctx, trigger=kind == "trigger")
        if not tr.toks:
            raise _Unsupported("empty source")
        return _trigger(tr, name.lower()) if kind == "trigger" else _routine(tr, kind, name.lower())
    except _Unsupported as exc:
        return PlsqlResult("", None, unsupported=[str(exc)])


def build_contexts(objects: Iterable) -> dict[str, ConversionContext]:
    """One ConversionContext per object ID, from all parsed objects of a project.

    Column types come from the tables; known routines are every function and procedure
    (package members qualified); constants are every inlinable package constant or
    variable (D4), qualified, plus unqualified for members of that package, whose
    unqualified calls to sibling members are qualified too.
    """
    from app.contracts.models import ObjectType
    from app.migration.features import analyze_package, literal_value

    objs = list(objects)
    column_types = column_types_from_ddl(o.source_code for o in objs if o.type == ObjectType.TABLE)
    packages = {}
    package_sources = {}
    for o in objs:
        if o.type == ObjectType.PACKAGE:
            others = [x.source_code for x in objs if x is not o]
            packages[o.name.lower()] = analyze_package(o.source_code, others)
            package_sources[o.name.lower()] = o.source_code
    known = frozenset(
        (f"{o.package.lower()}.{o.name.lower()}" if o.package else o.name.lower())
        for o in objs if o.type in (ObjectType.FUNCTION, ObjectType.PROCEDURE)
    )
    qualified_constants = {
        f"{name}.{var}": literal_value(package_sources[name], pkg, var)
        for name, pkg in packages.items()
        for var, info in pkg.variables.items() if info.inlinable
    }
    contexts = {}
    for o in objs:
        constants = dict(qualified_constants)
        renames: dict[str, str] = {}
        own = o.package.lower() if o.package else None
        if own in packages:
            pkg = packages[own]
            for var, info in pkg.variables.items():
                if info.inlinable:
                    constants[var] = qualified_constants[f"{own}.{var}"]
            renames = {member: f"{own}.{member}" for member in pkg.members}
        contexts[o.object_id] = ConversionContext(
            schema=own or "public",
            column_types=column_types,
            known_functions=known,
            function_renames=renames,
            constants=constants,
        )
    return contexts

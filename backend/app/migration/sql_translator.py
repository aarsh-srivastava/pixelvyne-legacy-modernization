"""Deterministic Oracle SQL to PostgreSQL translation.

SQLGlot handles supported dialect transformations. This module also detects
Oracle constructs that need special handling instead of silently emitting
potentially incorrect PostgreSQL SQL.

Rules on top of SQLGlot (Stage 1 design, section 5 and decisions D2/D3):

* R1  ``''`` is NULL in Oracle: an empty string literal becomes ``NULL``.
* R3  ``a || b`` ignores NULLs in Oracle: ``NULLIF(CONCAT(a, b), '')``.
* R5  ``CAST(x AS DATE)`` keeps the time in Oracle: ``TIMESTAMP(0)``. Date arithmetic is
      in days: ``d1 - d2`` becomes ``EXTRACT(EPOCH FROM (d1 - d2)) / 86400`` (fractional,
      like Oracle) and ``d +/- n`` becomes ``d +/- (n) * INTERVAL '1 day'``. Needs to know
      which operands are dates (``date_names``, date functions, GREATEST/CASE of dates).
* R4  Division is exact decimal, as with Oracle NUMBER: ``CAST(a AS DECIMAL) / b``.
* R6  ``TRUNC(d)`` / ``TRUNC(d, 'MM')`` on dates: ``DATE_TRUNC('day' | 'month', d)``.
* R7  ``ADD_MONTHS(d, n)`` keeps month-ends month-ends, like Oracle.
* R8  ``seq.NEXTVAL`` / ``seq.CURRVAL``: ``NEXTVAL('seq')`` / ``CURRVAL('seq')``.
* R9  The top-N ROWNUM pattern becomes ``LIMIT`` with Oracle's NULL, negative
      and fractional behaviour (D3). Any other ROWNUM is still unsupported.
* R10 ``SYSDATE``: ``CAST(DATE_TRUNC('second', STATEMENT_TIMESTAMP()) AS TIMESTAMP(0))``
      (D2). Truncate, then cast: a plain cast to ``TIMESTAMP(0)`` would round up.
* R13 Package constants and inlinable variables are replaced by their literal.

Everything SQLGlot can't render safely is reported, never emitted as if it worked:
``(+)`` outer joins (SQLGlot would silently drop the ``(+)`` and turn the outer
join into an inner join), ``CONNECT BY``, database links, ``TO_DATE``, functions
with no PostgreSQL rule, and any Oracle-only name left in the output.

Context the caller (the converter) supplies, all optional:

* ``date_names`` / ``number_names``: columns, variables and parameters known to be
  dates or numbers. ``TRUNC(x)`` with one argument is a date or a number function
  in Oracle; without knowing which, it's reported rather than guessed.
* ``known_functions``: user routines that will exist in PostgreSQL
  (``get_emp_grade``, ``payroll_pkg.net_pay``).
* ``function_renames``: unqualified calls to qualify, e.g. ``net_pay`` inside the
  package becomes ``payroll_pkg.net_pay``.
* ``constants``: names to inline, ``{"payroll_pkg.c_allowance": "2500"}``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp


@dataclass
class TranslationResult:
    """Result of translating one SQL statement."""

    source_sql: str
    target_sql: str
    warnings: list[str] = field(default_factory=list)
    unsupported_features: list[str] = field(default_factory=list)

    @property
    def successful(self) -> bool:
        return not self.unsupported_features


@dataclass(frozen=True)
class _Context:
    date_names: frozenset[str]
    number_names: frozenset[str]
    known_functions: frozenset[str]
    function_renames: Mapping[str, str]
    constants: Mapping[str, str]


# Functions that exist in PostgreSQL with the same meaning, when SQLGlot leaves them
# as plain (anonymous) calls. Anything else unknown is reported.
_POSTGRES_NATIVE = frozenset({
    "abs", "ceil", "floor", "mod", "power", "sqrt", "sign", "round", "trunc", "greatest", "least",
    "coalesce", "nullif", "upper", "lower", "length", "substr", "trim", "ltrim", "rtrim", "lpad", "rpad",
    "replace", "count", "sum", "avg", "min", "max", "to_char", "initcap", "ascii", "chr",
})

# Oracle TRUNC/ROUND date formats -> PostgreSQL DATE_TRUNC units. Formats whose
# meaning depends on Oracle NLS settings (DAY, DY, D, W, WW) are left out on purpose.
_TRUNC_UNITS = {
    **dict.fromkeys(("SYYYY", "YYYY", "YEAR", "SYEAR", "YYY", "YY", "Y"), "year"),
    **dict.fromkeys(("Q",), "quarter"),
    **dict.fromkeys(("MONTH", "MON", "MM", "RM"), "month"),
    **dict.fromkeys(("IW",), "week"),
    **dict.fromkeys(("DDD", "DD", "J"), "day"),
    **dict.fromkeys(("HH", "HH12", "HH24"), "hour"),
    **dict.fromkeys(("MI",), "minute"),
}

_SYSDATE_SQL = "CAST(DATE_TRUNC('second', STATEMENT_TIMESTAMP()) AS TIMESTAMP(0))"

# Oracle-only names that must never reach PostgreSQL (checked on the output, strings masked).
_ORACLE_LEFTOVERS = re.compile(
    r"\b(ROWNUM|SYSDATE|NVL2?|DECODE|ADD_MONTHS|MONTHS_BETWEEN|LAST_DAY|NEXT_DAY|LISTAGG|INSTR|SYS_GUID|"
    r"SYS_CONNECT_BY_PATH|PRIOR|DUAL)\b|\bCONNECT\s+BY\b|\.\s*(NEXTVAL|CURRVAL)\b",
    re.I,
)


def _pg(sql: str) -> exp.Expression:
    return sqlglot.parse_one(sql, read="postgres")


def _sql(node: exp.Expression) -> str:
    return node.sql(dialect="postgres")


def _contains_rownum(tree: exp.Expression) -> bool:
    """Detect ROWNUM references in the parsed SQL expression."""
    return any(
        isinstance(node, exp.Column)
        and node.name.upper() == "ROWNUM"
        for node in tree.walk()
    )


def _remove_dual(tree: exp.Expression) -> None:
    """Remove Oracle DUAL from simple SELECT statements."""
    for select in tree.find_all(exp.Select):
        from_clause = select.args.get("from_")

        if from_clause is None:
            continue

        source = from_clause.this

        if (
            isinstance(source, exp.Table)
            and source.name.upper() == "DUAL"
            and not source.db
            and not source.catalog
            and not select.args.get("joins")
        ):
            select.args.pop("from_", None)


# --- checks on the Oracle tree -------------------------------------------------------

def _function_name(node: exp.Anonymous) -> str:
    parent = node.parent
    if isinstance(parent, exp.Dot) and parent.expression is node:
        return f"{parent.this.name}.{node.name}".lower()
    return node.name.lower()


def _check(tree: exp.Expression, ctx: _Context) -> list[str]:
    problems: list[str] = []
    if tree.find(exp.Connect):
        problems.append("CONNECT BY (hierarchical query) has no rule yet; it needs a recursive CTE.")
    for table in tree.find_all(exp.Table):
        if "@" in table.sql(dialect="oracle"):
            problems.append(f"Database link {table.sql(dialect='oracle')} is not supported.")
    if tree.find(exp.StrToDate):
        problems.append("TO_DATE has no rule yet (Oracle DATE keeps the time of day).")
    for call in tree.find_all(exp.Anonymous):
        name = _function_name(call)
        if name == "trunc" or name in _POSTGRES_NATIVE:
            continue
        if name in ctx.known_functions or name in ctx.function_renames:
            continue
        problems.append(f"Function {name}() has no PostgreSQL rule and is not a known routine.")
    return problems


# --- R9: top-N ROWNUM ----------------------------------------------------------------

def _rownum_limit(where: exp.Expression) -> tuple[str, exp.Expression] | None:
    """('<=' | '<', n) when the WHERE is exactly ROWNUM <= n / ROWNUM < n (or n >= ROWNUM ...)."""
    def is_rownum(node: exp.Expression) -> bool:
        return isinstance(node, exp.Column) and node.name.upper() == "ROWNUM" and not node.table

    if isinstance(where, (exp.LTE, exp.LT)) and is_rownum(where.this) and not _contains_rownum(where.expression):
        return ("<=" if isinstance(where, exp.LTE) else "<"), where.expression
    if isinstance(where, (exp.GTE, exp.GT)) and is_rownum(where.expression) and not _contains_rownum(where.this):
        return ("<=" if isinstance(where, exp.GTE) else "<"), where.this
    return None


def _apply_top_n(tree: exp.Expression) -> None:
    """Rewrite every safe top-N select in place: the LIMIT goes where the ORDER BY is."""
    for select in list(tree.find_all(exp.Select)):
        where = select.args.get("where")
        from_clause = select.args.get("from_")
        if not where or not from_clause or select.args.get("joins"):
            continue
        limit = _rownum_limit(where.this)
        subquery = from_clause.this
        if not limit or not isinstance(subquery, exp.Subquery) or not isinstance(subquery.this, exp.Select):
            continue
        inner = subquery.this
        if not inner.args.get("order") or inner.args.get("limit") or inner.args.get("offset"):
            continue
        if any(_contains_rownum(e) for e in select.expressions):
            continue
        op, n = limit
        n_sql = _sql(n)
        count = f"FLOOR({n_sql})" if op == "<=" else f"CEIL({n_sql}) - 1"
        # Oracle: ROWNUM <= NULL gives no rows, a negative or zero limit gives no rows.
        inner.set("limit", exp.Limit(expression=_pg(f"CAST(GREATEST(COALESCE({count}, 0), 0) AS BIGINT)")))
        select.set("where", None)


# --- the other rules (applied children first) ------------------------------------------

def _has_interval(node: exp.Expression) -> bool:
    return isinstance(node, exp.Interval) or (
        isinstance(node, exp.Mul) and any(isinstance(side, exp.Interval) for side in (node.this, node.expression))
    )


def _is_date(node: exp.Expression, ctx: _Context) -> bool:
    if isinstance(node, exp.Paren):
        return _is_date(node.this, ctx)
    if isinstance(node, (exp.CurrentTimestamp, exp.AddMonths, exp.LastDay, exp.DateTrunc)):
        return True
    if isinstance(node, exp.Cast):
        return node.to.is_type("timestamp", "date", "timestamptz")
    if isinstance(node, exp.Column):
        return node.name.lower() in ctx.date_names or node.sql(dialect="oracle").lower() in ctx.date_names
    if isinstance(node, exp.Anonymous) and node.name.lower() == "date_trunc":
        return True
    if isinstance(node, (exp.Greatest, exp.Least, exp.Coalesce)):
        return any(_is_date(arg, ctx) for arg in [node.this, *node.expressions] if arg is not None)
    if isinstance(node, exp.Case):
        branches = [i.args.get("true") for i in node.args.get("ifs", [])] + [node.args.get("default")]
        return any(_is_date(b, ctx) for b in branches if b is not None)
    if isinstance(node, (exp.Add, exp.Sub)):
        # date +/- n days, as rewritten by R5 (date + n * INTERVAL '1 day')
        return (_is_date(node.this, ctx) and _has_interval(node.expression)) or (
            isinstance(node, exp.Add) and _has_interval(node.this) and _is_date(node.expression, ctx)
        )
    return False


def _is_number(node: exp.Expression, ctx: _Context) -> bool:
    if isinstance(node, exp.Paren):
        return _is_number(node.this, ctx)
    if isinstance(node, exp.Literal) and not node.is_string:
        return True
    if isinstance(node, exp.Column):
        return node.name.lower() in ctx.number_names or node.sql(dialect="oracle").lower() in ctx.number_names
    if isinstance(node, (exp.Add, exp.Sub)) and (_has_interval(node.this) or _has_interval(node.expression)):
        return False
    return isinstance(node, (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Round, exp.Trunc, exp.Extract, exp.Neg))


def _rewrite(node: exp.Expression, ctx: _Context, problems: list[str]) -> exp.Expression | None:
    """Return a replacement for ``node`` (its children are already rewritten), or None."""
    if isinstance(node, exp.Literal) and node.is_string and node.this == "":
        return exp.Null()

    if isinstance(node, exp.CurrentTimestamp) and node.args.get("sysdate"):
        return _pg(_SYSDATE_SQL)

    if isinstance(node, exp.Column) and node.name.upper() in ("NEXTVAL", "CURRVAL") and node.table:
        sequence = node.table.lower()
        return exp.Anonymous(this=node.name.upper(), expressions=[exp.Literal.string(sequence)])

    if isinstance(node, exp.Column):
        key = node.sql(dialect="oracle").lower()
        if key in ctx.constants:
            return sqlglot.parse_one(ctx.constants[key], read="oracle")

    if isinstance(node, exp.DateTrunc):
        unit = node.args.get("unit")
        fmt = unit.name.upper() if unit is not None else "DD"
        if fmt not in _TRUNC_UNITS:
            problems.append(f"TRUNC(date, '{fmt}') has no rule (format depends on Oracle settings).")
            return None
        return _pg(f"DATE_TRUNC('{_TRUNC_UNITS[fmt]}', {_sql(node.this)})")

    if isinstance(node, exp.Anonymous) and node.name.upper() == "TRUNC" and len(node.expressions) == 1:
        arg = node.expressions[0]
        if _is_date(arg, ctx):
            return _pg(f"DATE_TRUNC('day', {_sql(arg)})")
        if _is_number(arg, ctx):
            return None  # PostgreSQL TRUNC(numeric) is the same
        problems.append(f"TRUNC({_sql(arg)}): can't tell a date from a number; its type is needed.")
        return None

    if isinstance(node, exp.AddMonths):
        d, n = _sql(node.this), _sql(node.expression)
        months = f"CAST(TRUNC({n}) AS INT)"
        # Oracle: a month-end date stays a month-end (30 Nov - 6 months = 31 May), time kept.
        return _pg(
            f"CASE WHEN DATE_TRUNC('day', {d}) = DATE_TRUNC('month', {d}) + INTERVAL '1 month - 1 day' "
            f"THEN DATE_TRUNC('month', {d}) + ({months} + 1) * INTERVAL '1 month' - INTERVAL '1 day' "
            f"+ ({d} - DATE_TRUNC('day', {d})) "
            f"ELSE {d} + {months} * INTERVAL '1 month' END"
        )

    if isinstance(node, exp.Div) and not node.args.get("typed"):
        # Oracle NUMBER division is exact decimal. SQLGlot would cast to DOUBLE PRECISION
        # (float drift, and ROUND(double precision, 2) doesn't exist in PostgreSQL), and
        # leaving it alone would make 1 / 3 an integer division (0) in PostgreSQL.
        left = node.this
        if not (isinstance(left, exp.Cast) and left.to.is_type("decimal")):
            left = exp.Cast(this=left, to=exp.DataType.build("DECIMAL"))
        return exp.Div(this=left, expression=node.expression, typed=True)

    if isinstance(node, (exp.Sub, exp.Add)) and not isinstance(node, exp.DPipe):
        left, right = node.this, node.expression
        left_date, right_date = _is_date(left, ctx), _is_date(right, ctx)
        if isinstance(node, exp.Sub) and left_date and right_date:
            # Oracle: date - date = days, with a fraction for the time of day.
            return _pg(f"EXTRACT(EPOCH FROM ({_sql(left)} - {_sql(right)})) / 86400")
        if left_date and not right_date and not _has_interval(right):
            sign = "-" if isinstance(node, exp.Sub) else "+"
            return _pg(f"{_sql(left)} {sign} ({_sql(right)}) * INTERVAL '1 day'")
        if isinstance(node, exp.Add) and right_date and not left_date and not _has_interval(left):
            return _pg(f"{_sql(right)} + ({_sql(left)}) * INTERVAL '1 day'")

    if isinstance(node, exp.LastDay):
        d = _sql(node.this)
        # Oracle LAST_DAY keeps the time of day.
        return _pg(f"DATE_TRUNC('month', {d}) + INTERVAL '1 month - 1 day' + ({d} - DATE_TRUNC('day', {d}))")

    if isinstance(node, exp.DataType) and isinstance(node.parent, exp.Cast):
        if node.is_type("date"):
            return exp.DataType.build("TIMESTAMP(0)", dialect="postgres")
        if node.is_type("int", "smallint", "bigint", "tinyint"):
            return exp.DataType.build("DECIMAL(38, 0)")

    if isinstance(node, exp.Anonymous) and node.name.lower() in ctx.function_renames:
        if not isinstance(node.parent, exp.Dot):
            qualified = ctx.function_renames[node.name.lower()]
            return _pg(f"{qualified}({', '.join(_sql(a) for a in node.expressions)})")

    return None


def _apply_rules(tree: exp.Expression, ctx: _Context, problems: list[str]) -> exp.Expression:
    for node in reversed(list(tree.walk(bfs=False))):
        if node is tree:
            replacement = _rewrite(node, ctx, problems)
            tree = replacement or tree
            continue
        replacement = _rewrite(node, ctx, problems)
        if replacement is not None:
            node.replace(replacement)

    # R3: only the outermost || of a chain, so a || b || c becomes one CONCAT.
    for node in [n for n in tree.find_all(exp.DPipe) if not isinstance(n.parent, exp.DPipe)]:
        parts: list[exp.Expression] = []
        stack = [node]
        while stack:
            current = stack.pop()
            if isinstance(current, exp.DPipe):
                stack.extend([current.expression, current.this])
            else:
                parts.append(current)
        concat = _pg(f"NULLIF(CONCAT({', '.join(_sql(p) for p in parts)}), '')")
        if node is tree:
            tree = concat
        else:
            node.replace(concat)
    return tree


def _leftovers(target_sql: str) -> list[str]:
    masked = re.sub(r"'(?:[^']|'')*'", "''", target_sql)
    names = sorted({(m.group(1) or m.group(2) or "CONNECT BY").upper() for m in _ORACLE_LEFTOVERS.finditer(masked)})
    return [f"PostgreSQL output still contains Oracle-only {name}." for name in names]


def translate_sql(
    source_sql: str,
    *,
    date_names: Iterable[str] = (),
    number_names: Iterable[str] = (),
    known_functions: Iterable[str] = (),
    function_renames: Mapping[str, str] | None = None,
    constants: Mapping[str, str] | None = None,
) -> TranslationResult:
    """Translate one Oracle SQL statement into PostgreSQL SQL.

    Unsupported constructs are reported rather than silently treated as
    successfully translated.
    """
    if not source_sql or not source_sql.strip():
        raise ValueError("source_sql must not be empty")

    ctx = _Context(
        date_names=frozenset(n.lower() for n in date_names),
        number_names=frozenset(n.lower() for n in number_names),
        known_functions=frozenset(n.lower() for n in known_functions),
        function_renames={k.lower(): v for k, v in (function_renames or {}).items()},
        constants={k.lower(): v for k, v in (constants or {}).items()},
    )
    warnings: list[str] = []
    unsupported: list[str] = []

    try:
        statements = sqlglot.parse(
            source_sql,
            read="oracle",
            error_level=sqlglot.ErrorLevel.RAISE,
        )
    except sqlglot.errors.ParseError as exc:
        return TranslationResult(
            source_sql=source_sql,
            target_sql="",
            unsupported_features=[f"Oracle SQL parsing failed: {exc}"],
        )

    if len(statements) != 1 or statements[0] is None:
        return TranslationResult(
            source_sql=source_sql,
            target_sql="",
            unsupported_features=[
                "Expected exactly one SQL statement. "
                "Translate multiple statements individually."
            ],
        )

    tree = statements[0]
    unsupported.extend(_check(tree, ctx))

    # Rules first, then top-N: the LIMIT it adds is already PostgreSQL and must not be rewritten.
    tree = _apply_rules(tree, ctx, unsupported)
    _apply_top_n(tree)
    if _contains_rownum(tree):
        unsupported.append(
            "ROWNUM requires query-specific pagination/order analysis; "
            "automatic conversion is not enabled."
        )

    _remove_dual(tree)

    try:
        # RAISE: SQLGlot must refuse what PostgreSQL can't express (it would otherwise
        # drop an Oracle (+) and turn an outer join into an inner join).
        target_sql = tree.sql(dialect="postgres", unsupported_level=sqlglot.ErrorLevel.RAISE)
    except Exception as exc:
        return TranslationResult(
            source_sql=source_sql,
            target_sql="",
            warnings=warnings,
            unsupported_features=unsupported + [
                f"PostgreSQL SQL generation failed: {type(exc).__name__}: {exc}"
            ],
        )

    if not unsupported:
        # Safety net: nothing Oracle-only may reach PostgreSQL unnoticed.
        unsupported.extend(_leftovers(target_sql))

    if unsupported:
        warnings.append(
            "Generated SQL is provisional and must not be deployed until "
            "unsupported features are resolved."
        )

    return TranslationResult(
        source_sql=source_sql,
        target_sql=target_sql,
        warnings=warnings,
        unsupported_features=unsupported,
    )


def translate_expression(expression: str, **context) -> TranslationResult:
    """Translate one Oracle expression (for PL/SQL assignments, conditions, RETURN values)."""
    result = translate_sql(f"SELECT {expression} FROM dual", **context)
    if result.target_sql:
        result.target_sql = re.sub(r"^SELECT\s+", "", result.target_sql, count=1)
    result.source_sql = expression
    return result

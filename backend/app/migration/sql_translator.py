
"""Deterministic Oracle SQL to PostgreSQL translation.

SQLGlot handles supported dialect transformations. This module also detects
Oracle constructs that need special handling instead of silently emitting
potentially incorrect PostgreSQL SQL.
"""

from __future__ import annotations

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



def translate_sql(source_sql: str) -> TranslationResult:
    """Translate one Oracle SQL statement into PostgreSQL SQL.

    Unsupported constructs are reported rather than silently treated as
    successfully translated.
    """
    if not source_sql or not source_sql.strip():
        raise ValueError("source_sql must not be empty")

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

    if _contains_rownum(tree):
        unsupported.append(
            "ROWNUM requires query-specific pagination/order analysis; "
            "automatic conversion is not enabled."
        )

    _remove_dual(tree)

    try:
        target_sql = tree.sql(dialect="postgres")
    except Exception as exc:
        return TranslationResult(
            source_sql=source_sql,
            target_sql="",
            warnings=warnings,
            unsupported_features=[
                f"PostgreSQL SQL generation failed: {type(exc).__name__}: {exc}"
            ],
        )

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

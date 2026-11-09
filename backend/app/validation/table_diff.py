"""Turn before/after table snapshots into TableDiffs, and compare values across engines.

Both runners snapshot the watched tables before and after a call, inside the same
transaction, then roll back. What changed in between is the object's side effect,
including anything its triggers did.

Values are compared in a canonical form, so Oracle and PostgreSQL agree on equal data:
24000 == 24000.00, a DATE at midnight == the same timestamp, column names lower-case.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from decimal import Decimal
from typing import Any, Iterable

from app.contracts.models import TableDiff

SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]*$")


def check_identifier(name: str) -> str:
    """Table names come from parsed source; refuse anything that is not a plain name."""
    if not SAFE_IDENTIFIER.match(name):
        raise ValueError(f"Unsafe table name: {name!r}")
    return name


def canon(value: Any) -> Any:
    """A hashable, engine-neutral form of one value."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, (int, float, Decimal)):
        return format(Decimal(str(value)).normalize(), "f")
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None).isoformat()
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time()).isoformat()
    return str(value)


def canon_row(row: dict[str, Any], masked: Iterable[str] = ()) -> tuple:
    skip = set(masked)
    return tuple(sorted((k, canon(v)) for k, v in row.items() if k not in skip))


def strip(row: dict[str, Any], masked: Iterable[str] = ()) -> dict[str, Any]:
    skip = set(masked)
    return {k: v for k, v in row.items() if k not in skip}


def diff_snapshots(
    table: str,
    key_columns: list[str],
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
) -> TableDiff | None:
    """Rows inserted, deleted or updated between two snapshots. None if nothing changed."""
    if key_columns:
        def key(r):
            return tuple(canon(r[c]) for c in key_columns)

        old = {key(r): r for r in before}
        new = {key(r): r for r in after}
        inserted = [new[k] for k in new.keys() - old.keys()]
        deleted = [old[k] for k in old.keys() - new.keys()]
        updated = [
            {"key": {c: new[k][c] for c in key_columns}, "before": old[k], "after": new[k]}
            for k in old.keys() & new.keys()
            if canon_row(old[k]) != canon_row(new[k])
        ]
    else:  # no primary key: compare as multisets of whole rows
        old_c, new_c = Counter(canon_row(r) for r in before), Counter(canon_row(r) for r in after)
        by_canon = {canon_row(r): r for r in [*before, *after]}
        inserted = [by_canon[k] for k in (new_c - old_c).elements()]
        deleted = [by_canon[k] for k in (old_c - new_c).elements()]
        updated = []

    if not (inserted or deleted or updated):
        return None
    return TableDiff(table=table, inserted=inserted, deleted=deleted, updated=updated)

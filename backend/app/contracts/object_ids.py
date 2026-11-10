"""Canonical object identity, shared by every stage (contract version 0.2).

An object_id names one source object, independent of how the converter later names it
in PostgreSQL:

    <kind>:<schema>.<object>                  table:legacy.employees
    <kind>:<schema>.<package>.<member>        function:legacy.payroll_pkg.net_pay
    ...plus (<types>) only for overloads      procedure:hr.pkg.calculate(number,date)
    unresolved:<reference as written>         unresolved:legacy.bonus_rules, unresolved:pkg.fn

Rules (agreed by Aarsh and Tarun, 10 Nov 2026):
* Everything is lower-case; identifiers go through normalize_identifier().
* The schema is always present. This module never reads settings or the environment:
  callers pass default_schema(settings.ORACLE_USER) for unqualified source names.
* A signature is added only when the CALLER has found that a name is overloaded.
  make_object_id() never guesses. signature=[] gives "()" (a zero-argument overload).
* Collisions ("Emp" and EMP both become "emp") can only be seen across the whole set
  of discovered objects, so check_unique_object_ids() takes the collection.
* Unresolved references are never dropped and never guessed: a 2-part reference such
  as pkg.fn could be schema.object or package.member, so it is kept as written.

The parser, dependency analyzer, converter and validation all import from here.
Nothing else formats or splits object_ids by hand.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable, NamedTuple, Sequence

# Kept in step with models.ObjectType by test_object_ids.py (this module must not
# import models, because models imports it).
OBJECT_KINDS: tuple[str, ...] = (
    "table", "view", "sequence", "function", "procedure", "trigger", "package",
)
UNRESOLVED = "unresolved"
FALLBACK_SCHEMA = "legacy"
_MEMBER_KINDS = {"function", "procedure"}

_IDENT = r"[a-z_][a-z0-9_$#]*"
_TYPE = r"[a-z0-9_$#.%]+"
IDENTIFIER_PATTERN = re.compile(rf"^{_IDENT}$")

OBJECT_ID_PATTERN = re.compile(
    rf"^(?P<kind>{'|'.join(OBJECT_KINDS)}|{UNRESOLVED})"
    rf":(?P<path>{_IDENT}\.{_IDENT}(?:\.{_IDENT})?)"
    rf"(?:\((?P<signature>{_TYPE}(?:,{_TYPE})*)?\))?$"
)


class ObjectRef(NamedTuple):
    """An object_id split into parts. For unresolved ids only kind and name are set:
    name holds the reference as written, because its parts cannot be interpreted."""

    kind: str
    schema: str | None
    package: str | None
    name: str
    signature: tuple[str, ...] | None


class ObjectIdCollisionError(ValueError):
    """Two or more distinct source objects normalize to the same object_id."""

    def __init__(self, collisions: dict[str, list[str]]):
        self.collisions = collisions
        lines = [f"  {oid}: {', '.join(sources)}" for oid, sources in sorted(collisions.items())]
        super().__init__(
            "Distinct source objects map to the same object_id. Rename one of them in the "
            "source, or migrate them in separate projects:\n" + "\n".join(lines)
        )


def normalize_identifier(identifier: str) -> str:
    """One Oracle identifier to its canonical form: quotes removed, lower-case.

    `EMP`, `emp` and `"Emp"` all become `emp`. Raises ValueError for anything that
    cannot be a canonical identifier (empty, spaces, dots, other symbols).
    """
    text = identifier.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1]
    text = text.lower()
    if not IDENTIFIER_PATTERN.match(text):
        raise ValueError(f"Not a supported identifier: {identifier!r}")
    return text


def default_schema(oracle_user: str | None) -> str:
    """Schema for unqualified source names: the Oracle user, else 'legacy'."""
    if oracle_user is None or not oracle_user.strip():
        return FALLBACK_SCHEMA
    return normalize_identifier(oracle_user)


def normalize_signature(param_types: Sequence[str]) -> tuple[str, ...]:
    """Parameter types for an overload suffix: lower-case, size/precision removed,
    declaration order kept. `VARCHAR2(20)` -> `varchar2`, `NUMBER(10,2)` -> `number`,
    `TIMESTAMP WITH TIME ZONE` -> `timestamp_with_time_zone`."""
    result = []
    for raw in param_types:
        text = re.sub(r"\([^)]*\)", "", raw).strip().lower()
        text = re.sub(r"\s+", "_", text)
        if not re.fullmatch(_TYPE, text):
            raise ValueError(f"Not a supported parameter type: {raw!r}")
        result.append(text)
    return tuple(result)


def make_object_id(
    kind: str,
    name: str,
    *,
    schema: str,
    package: str | None = None,
    signature: Sequence[str] | None = None,
) -> str:
    """Build the canonical object_id for one source object.

    kind       an ObjectType value ("procedure") or the enum itself
    name       the object's own name, unqualified
    schema     required; use default_schema(settings.ORACLE_USER) for unqualified names
    package    for package members only (functions and procedures)
    signature  raw parameter types, ONLY when the caller has found the name overloaded
    """
    kind = getattr(kind, "value", kind)
    if kind == UNRESOLVED:
        raise ValueError("Use make_unresolved_id() for unresolved references")
    if kind not in OBJECT_KINDS:
        raise ValueError(f"Unknown object kind: {kind!r}")
    if package is not None and kind not in _MEMBER_KINDS:
        raise ValueError(f"Only functions and procedures can be package members, not {kind}")
    if signature is not None and kind not in _MEMBER_KINDS:
        raise ValueError(f"Only functions and procedures can be overloaded, not {kind}")

    parts = [normalize_identifier(schema)]
    if package is not None:
        parts.append(normalize_identifier(package))
    parts.append(normalize_identifier(name))
    object_id = f"{kind}:{'.'.join(parts)}"
    if signature is not None:
        object_id += f"({','.join(normalize_signature(signature))})"
    return object_id


def make_unresolved_id(reference: str, *, default_schema: str) -> str:
    """Keep a reference that matches nothing in the upload, without guessing.

    A 1-part reference gets the default schema (bonus_rules -> unresolved:legacy.bonus_rules).
    2- and 3-part references are kept as written (pkg.fn -> unresolved:pkg.fn), because
    pkg.fn could be schema.object or package.member.
    """
    parts = [normalize_identifier(p) for p in reference.split(".")]
    if len(parts) > 3:
        raise ValueError(f"Reference has more than 3 parts: {reference!r}")
    if len(parts) == 1:
        parts.insert(0, normalize_identifier(default_schema))
    return f"{UNRESOLVED}:{'.'.join(parts)}"


def parse_object_id(object_id: str) -> ObjectRef:
    """Split a canonical object_id into its parts. Raises ValueError if it is not one."""
    match = OBJECT_ID_PATTERN.match(object_id)
    if not match:
        raise ValueError(f"Not a canonical object_id: {object_id!r}")
    kind, path, sig = match["kind"], match["path"], match["signature"]
    has_signature = object_id.endswith(")")

    if kind == UNRESOLVED:
        if has_signature:
            raise ValueError(f"Unresolved ids carry no signature: {object_id!r}")
        return ObjectRef(kind, None, None, path, None)

    parts = path.split(".")
    package = parts[1] if len(parts) == 3 else None
    if (package is not None or has_signature) and kind not in _MEMBER_KINDS:
        raise ValueError(f"Only functions and procedures have packages or signatures: {object_id!r}")
    signature = tuple(sig.split(",")) if sig else (() if has_signature else None)
    return ObjectRef(kind, parts[0], package, parts[-1], signature)


def is_object_id(value: str) -> bool:
    try:
        parse_object_id(value)
    except ValueError:
        return False
    return True


def check_unique_object_ids(entries: Iterable[tuple[str, str]]) -> None:
    """Raise ObjectIdCollisionError if two discovered objects share an object_id.

    entries: (object_id, where it came from), e.g.
        ("table:legacy.emp", '"Emp" in 01_schema.sql')
    The caller decides when two definitions are the same object (for example a
    package spec and body, merged before this check). Anything still sharing an id
    here is a collision and is never merged silently.
    """
    seen: dict[str, list[str]] = defaultdict(list)
    for object_id, source in entries:
        seen[object_id].append(source)
    collisions = {oid: sources for oid, sources in seen.items() if len(sources) > 1}
    if collisions:
        raise ObjectIdCollisionError(collisions)

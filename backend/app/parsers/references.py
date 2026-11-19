"""References by line: which object each name on a line points to (PR 1, G10 groundwork).

A repair ticket shows the failing line. A line can touch other objects: a table it writes,
a function it calls, or a trigger its UPDATE fires without naming it. This module lists,
for every object, each reference with its line and what it points to, and says whether
the analysis is complete. Nothing here changes conversion or the execution order.

Lines are counted in the object's own source (``line``) and, when the parser knows where
the object starts, in its file (``file_line``). PL/SQL is read with the position-keeping
masker (strings and comments blanked, offsets unchanged), not with SQLGlot, which cannot
parse PL/SQL.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.contracts import IRObject, ObjectType, parse_object_id
from app.migration.features import line_of, mask_oracle
from app.parsers.dependency_analyzer import DependencyGraph

_WRITE_BEFORE = re.compile(r"(?:\bUPDATE|\bINSERT\s+INTO|\bDELETE\s+FROM|\bDELETE|\bMERGE\s+INTO)\s+$", re.I)
_DYNAMIC_SQL = re.compile(r"\bEXECUTE\s+IMMEDIATE\b|\bDBMS_SQL\s*\.", re.I)
_CREATE = re.compile(r"\bCREATE\b", re.I)


@dataclass(frozen=True)
class LineReference:
    """One reference on one line.

    kind: calls, reads, writes or references for objects in the upload; fires for a
    trigger run by a write on this line; missing, system or external for a reference
    that matches nothing in the upload (see dependency_analyzer.classify_reference).
    """

    line: int                 # 1-based, within the object's source
    file_line: int | None     # 1-based, within its file, when known
    target: str               # object_id, or "unresolved:..."
    kind: str
    text: str                 # the words matched


@dataclass(frozen=True)
class ObjectReferences:
    object_id: str
    references: tuple[LineReference, ...]
    impact_complete: bool
    incomplete_reasons: tuple[str, ...]

    def on_line(self, line: int) -> tuple[LineReference, ...]:
        return tuple(r for r in self.references if r.line == line)


def _name_pattern(target: str) -> re.Pattern:
    """Matches the target's name, optionally schema- or package-qualified."""
    ref = parse_object_id(target)
    if ref.kind == "unresolved":
        last = ref.name.split(".")[-1]
    else:
        last = ref.name
    qualifier = r"(?:(?:\"[^\"]+\"|[A-Za-z_][\w$#]*)\s*\.\s*){0,2}"
    return re.compile(rf"(?<![\w$#.\"]){qualifier}\"?{re.escape(last)}\"?(?![\w$#])", re.I)


def references_by_line(
    objects: Iterable[IRObject],
    graph: DependencyGraph,
    locations: Mapping[str, object] | None = None,
) -> dict[str, ObjectReferences]:
    """For each object: its references by line, and whether the impact analysis is complete.

    ``locations`` is ParseResult.locations (object_id -> SourceLocation); without it
    ``file_line`` is None.
    """
    locations = locations or {}
    links_from: dict[str, list] = defaultdict(list)
    for link in graph.links:
        links_from[link.source].append(link)
    triggers_on: dict[str, list[str]] = defaultdict(list)
    for link in graph.fires:
        triggers_on[link.source].append(link.target)
    unresolved_kind = {u.reference: u.kind for u in graph.unresolved}

    result = {}
    for obj in objects:
        result[obj.object_id] = _for_object(
            obj, links_from[obj.object_id], triggers_on, unresolved_kind, locations.get(obj.object_id)
        )
    return result


def _for_object(obj, links, triggers_on, unresolved_kind, location) -> ObjectReferences:
    reasons: list[str] = []
    try:
        masked = mask_oracle(obj.source_code, keep_positions=True)
    except ValueError as exc:
        return ObjectReferences(obj.object_id, (), False, (f"source could not be read ({exc})",))

    first_line = None
    if location is not None and obj.type != ObjectType.PACKAGE:
        create = _CREATE.search(masked) if obj.package is None else None
        before = line_of(masked, create.start()) - 1 if create else 0
        first_line = location.line - before

    found: set[LineReference] = set()

    def add(line: int, target: str, kind: str, text: str) -> None:
        file_line = first_line + line - 1 if first_line is not None else None
        found.add(LineReference(line, file_line, target, kind, text))

    kinds_by_target: dict[str, set[str]] = defaultdict(set)
    for link in links:
        kinds_by_target[link.target].add(link.kind)

    for target, kinds in sorted(kinds_by_target.items()):
        for match in _name_pattern(target).finditer(masked):
            line, text = line_of(masked, match.start()), match.group(0)
            if {"reads", "writes"} <= kinds:
                kind = "writes" if _WRITE_BEFORE.search(masked[max(0, match.start() - 40):match.start()]) else "reads"
            else:
                kind = sorted(kinds)[0]
            add(line, target, kind, text)
            if kind == "writes":
                for trigger in triggers_on.get(target, []):
                    add(line, trigger, "fires", text)

    for dependency in obj.depends_on:
        if not dependency.startswith("unresolved:"):
            continue
        kind = unresolved_kind.get(dependency, "missing")
        hits = list(_name_pattern(dependency).finditer(masked))
        for match in hits:
            add(line_of(masked, match.start()), dependency, kind, match.group(0))
        if kind != "system":
            reasons.append(f"refers to {dependency.split(':', 1)[1]}, which is not in the upload ({kind})")

    if _DYNAMIC_SQL.search(masked):
        reasons.append("dynamic SQL: what it runs is only known at run time")

    references = tuple(sorted(found, key=lambda r: (r.line, r.target, r.kind, r.text)))
    return ObjectReferences(obj.object_id, references, not reasons, tuple(reasons))

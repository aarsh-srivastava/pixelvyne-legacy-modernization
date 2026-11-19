
"""Dependency analysis for parsed Oracle database objects.

The analyzer builds a typed dependency graph from canonical object IDs.

Edge convention:
    dependent -> dependency

Migration execution order is dependency-first.

PR 1 (G2 parser half, G10 groundwork) adds, without changing the execution order:

* ``links``: each edge with how it is used: calls, reads, writes or references.
* ``fires``: table -> trigger links (an UPDATE on the table runs the trigger). They are
  execution relationships, not creation dependencies, so they never enter the execution
  order or the cycle check.
* ``unresolved``: every reference that matches nothing in the upload, sorted into
  missing (a schema in the upload, so a file is probably missing), system (an Oracle
  object such as DUAL or SYS.*) or external (another schema; a person decides).
* ``blocked`` / ``needs_decision``: for each missing / external reference, every object
  that depends on it, directly or through other objects.
* ``upstream`` / ``downstream``: what an object relies on and what relies on it, with the
  distance in links from that object.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Iterable

from app.contracts.models import IRObject, ObjectType
from app.contracts import normalize_identifier, parse_object_id


@dataclass(frozen=True, slots=True)
class DependencyEdge:
    """A relationship between two database objects."""

    dependent: str
    dependency: str
    dependency_type: str


@dataclass(frozen=True, slots=True)
class Link:
    """A typed relationship: source uses target (calls, reads, writes, references), or the
    table ``source`` fires the trigger ``target`` (fires)."""

    source: str
    target: str
    kind: str


@dataclass(frozen=True, slots=True)
class UnresolvedReference:
    """A reference that matches no uploaded object."""

    reference: str                    # "unresolved:..."
    kind: str                         # "missing" | "system" | "external"
    referenced_by: tuple[str, ...]    # sorted object_ids that make the reference


@dataclass(frozen=True, slots=True)
class DependencyGraph:
    """Immutable result of dependency analysis."""

    nodes: tuple[str, ...]
    edges: tuple[DependencyEdge, ...]
    execution_order: tuple[str, ...]
    cycles: tuple[tuple[str, ...], ...]
    links: tuple[Link, ...] = ()
    fires: tuple[Link, ...] = ()
    unresolved: tuple[UnresolvedReference, ...] = ()
    blocked: Mapping[str, tuple[str, ...]] = field(default_factory=dict, hash=False)
    needs_decision: Mapping[str, tuple[str, ...]] = field(default_factory=dict, hash=False)

    @property
    def has_cycles(self) -> bool:
        return bool(self.cycles)

    def upstream(self, object_id: str) -> dict[str, int]:
        """What object_id relies on, each with its distance in links (1 = direct).

        Follows uses and fires links: apply_raise writes employees (1), and employees
        fires emp_salary_audit_trg (2).
        """
        forward: dict[str, set[str]] = defaultdict(set)
        for link in (*self.links, *self.fires):
            forward[link.source].add(link.target)
        return _distances(object_id, lambda node: forward[node])

    def downstream(self, object_id: str) -> dict[str, int]:
        """What relies on object_id, each with its distance in links (1 = direct).

        Uses links are followed backwards. A trigger is relied on by its table's writers
        (through the fires link), not by everything that reads the table.
        """
        users: dict[str, set[str]] = defaultdict(set)
        writers: dict[str, set[str]] = defaultdict(set)
        for link in self.links:
            users[link.target].add(link.source)
            if link.kind == "writes":
                writers[link.target].add(link.source)
        fired_by: dict[str, set[str]] = defaultdict(set)
        for link in self.fires:
            fired_by[link.target].add(link.source)

        distances: dict[str, int] = {}
        queue: deque[tuple[str, int, bool]] = deque([(object_id, 0, False)])
        seen = {(object_id, False)}
        while queue:
            node, distance, via_fires = queue.popleft()
            nexts = [(t, True) for t in sorted(fired_by[node])]
            nexts += [(u, False) for u in sorted(writers[node] if via_fires else users[node])]
            for nxt, flag in nexts:
                if nxt == object_id or (nxt, flag) in seen:
                    continue
                seen.add((nxt, flag))
                distances.setdefault(nxt, distance + 1)
                queue.append((nxt, distance + 1, flag))
        return distances


def _distances(start: str, neighbours) -> dict[str, int]:
    """Breadth-first distances from start (start itself excluded)."""
    distances: dict[str, int] = {}
    queue = deque([start])
    depth = {start: 0}
    while queue:
        node = queue.popleft()
        for nxt in sorted(neighbours(node)):
            if nxt not in depth:
                depth[nxt] = depth[node] + 1
                distances[nxt] = depth[nxt]
                queue.append(nxt)
    return distances


#: Oracle data dictionary views commonly read by application code.
_DICTIONARY_VIEW = re.compile(
    r"^(?:user|all|dba|cdb)_(?:tables|objects|tab_columns|source|views|sequences|indexes|"
    r"ind_columns|constraints|cons_columns|triggers|procedures|users|synonyms|dependencies|"
    r"errors|tab_privs|role_privs|sys_privs|arguments|scheduler_jobs)$|^g?v\$"
)
_SYSTEM_SCHEMAS = {"sys", "system"}


def classify_reference(reference: str, upload_schemas: Iterable[str]) -> str:
    """'system', 'missing' or 'external' for an "unresolved:..." reference.

    system:   DUAL, SYS/SYSTEM objects, DBMS_*/UTL_* packages, dictionary views.
              PUBLIC is not a system schema: a public.x reference is classified like any
              other schema (missing if the upload uses it, otherwise external).
    missing:  a schema that the upload uses: a file is probably missing.
    external: any other schema (or a package that is not in the upload); a person decides
              whether it is in scope.
    """
    ref = parse_object_id(reference)
    if ref.kind != "unresolved":
        raise ValueError(f"not an unresolved reference: {reference!r}")
    parts = [normalize_identifier(p) for p in ref.name.split(".")]
    name = parts[-1]
    if (
        name == "dual"
        or parts[0] in _SYSTEM_SCHEMAS
        or any(p.startswith(("dbms_", "utl_")) for p in parts)
        or _DICTIONARY_VIEW.match(name)
    ):
        return "system"
    if parts[0] in set(upload_schemas):  # unresolved ids always carry a schema
        return "missing"
    return "external"


def _find_cycles(
    nodes: set[str],
    edges: set[tuple[str, str]],
) -> tuple[tuple[str, ...], ...]:
    """Find strongly connected components using Tarjan's algorithm."""

    adjacency: dict[str, list[str]] = defaultdict(list)

    for dependent, dependency in edges:
        adjacency[dependency].append(dependent)

    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[tuple[str, ...]] = []

    def strong_connect(node: str) -> None:
        nonlocal index

        indices[node] = index
        lowlinks[node] = index
        index += 1

        stack.append(node)
        on_stack.add(node)

        for neighbor in adjacency[node]:
            if neighbor not in indices:
                strong_connect(neighbor)
                lowlinks[node] = min(lowlinks[node], lowlinks[neighbor])
            elif neighbor in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[neighbor])

        if lowlinks[node] == indices[node]:
            component: list[str] = []

            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)

                if member == node:
                    break

            if len(component) > 1 or (node, node) in edges:
                components.append(tuple(sorted(component)))

    for node in sorted(nodes):
        if node not in indices:
            strong_connect(node)

    return tuple(sorted(components))


def _topological_order(
    nodes: set[str],
    edges: set[tuple[str, str]],
) -> tuple[str, ...]:
    """Return a dependency-first order using Kahn's algorithm."""

    adjacency: dict[str, set[str]] = defaultdict(set)
    indegree = {node: 0 for node in nodes}

    for dependent, dependency in edges:
        if dependent == dependency:
            continue

        if dependent not in adjacency[dependency]:
            adjacency[dependency].add(dependent)
            indegree[dependent] += 1

    ready = deque(
        sorted(node for node, degree in indegree.items() if degree == 0)
    )

    order: list[str] = []

    while ready:
        current = ready.popleft()
        order.append(current)

        for dependent in sorted(adjacency[current]):
            indegree[dependent] -= 1

            if indegree[dependent] == 0:
                ready.append(dependent)

    return tuple(order)


def analyze_dependencies(
    objects: Iterable[IRObject],
) -> DependencyGraph:
    """Build a dependency graph using canonical object identities."""

    objects = list(objects)

    objects_by_id: dict[str, IRObject] = {}

    for obj in objects:
        if obj.object_id in objects_by_id:
            raise ValueError(
                f"Duplicate object name or identity '{obj.object_id}'."
            )

        objects_by_id[obj.object_id] = obj

    nodes = set(objects_by_id)
    edge_pairs: set[tuple[str, str]] = set()

    for obj in objects:
        dependent = obj.object_id

        for dependency in obj.depends_on:
            # Unresolved references are retained in the IR, but cannot
            # participate in an executable graph of discovered objects.
            if dependency not in nodes:
                continue

            if dependency == dependent:
                continue

            edge_pairs.add((dependent, dependency))

    edges = tuple(
        DependencyEdge(
            dependent=dependent,
            dependency=dependency,
            dependency_type=parse_object_id(
                dependency
            ).kind,
        )
        for dependent, dependency in sorted(edge_pairs)
    )

    cycles = _find_cycles(nodes, edge_pairs)
    execution_order = _topological_order(nodes, edge_pairs)

    links = _typed_links(objects_by_id, edge_pairs)
    fires = tuple(sorted(
        (Link(obj.trigger.table, obj.object_id, "fires")
         for obj in objects if obj.trigger is not None and obj.trigger.table in nodes),
        key=lambda link: (link.source, link.target),
    ))
    unresolved = _unresolved(objects)
    dependents: dict[str, set[str]] = defaultdict(set)
    for dependent, dependency in edge_pairs:
        dependents[dependency].add(dependent)

    def affected(kind: str) -> dict[str, tuple[str, ...]]:
        result = {}
        for ref in unresolved:
            if ref.kind != kind:
                continue
            reached = set(ref.referenced_by)
            queue = deque(sorted(reached))
            while queue:
                for dependent in sorted(dependents[queue.popleft()]):
                    if dependent not in reached:
                        reached.add(dependent)
                        queue.append(dependent)
            result[ref.reference] = tuple(sorted(reached))
        return result

    return DependencyGraph(
        nodes=tuple(sorted(nodes)),
        edges=edges,
        execution_order=execution_order,
        cycles=cycles,
        links=links,
        fires=fires,
        unresolved=unresolved,
        blocked=affected("missing"),
        needs_decision=affected("external"),
    )


def _last_name(reference: str) -> str:
    return normalize_identifier(reference.split(".")[-1])


def _typed_links(objects_by_id: dict[str, IRObject], edge_pairs: set[tuple[str, str]]) -> tuple[Link, ...]:
    """Each edge as one or more typed links (a table both read and written gives two)."""
    links = []
    for dependent, dependency in sorted(edge_pairs):
        obj, target = objects_by_id[dependent], objects_by_id[dependency]
        if target.type in (ObjectType.FUNCTION, ObjectType.PROCEDURE):
            kinds = ["calls"]
        elif target.type in (ObjectType.TABLE, ObjectType.VIEW):
            kinds = [kind for kind, names in (("reads", obj.reads_tables), ("writes", obj.writes_tables))
                     if target.name in {_last_name(n) for n in names}] or ["references"]
        else:
            kinds = ["references"]
        links.extend(Link(dependent, dependency, kind) for kind in kinds)
    return tuple(links)


def _unresolved(objects: list[IRObject]) -> tuple[UnresolvedReference, ...]:
    schemas = {parse_object_id(o.object_id).schema for o in objects}
    referrers: dict[str, set[str]] = defaultdict(set)
    for obj in objects:
        for dependency in obj.depends_on:
            if dependency.startswith("unresolved:"):
                referrers[dependency].add(obj.object_id)
    return tuple(
        UnresolvedReference(ref, classify_reference(ref, schemas), tuple(sorted(by)))
        for ref, by in sorted(referrers.items())
    )

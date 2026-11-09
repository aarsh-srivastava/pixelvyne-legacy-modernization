
"""Dependency analysis for parsed Oracle database objects.

The analyzer builds a typed dependency graph from IR objects.

Edge convention:
    dependent -> dependency

For example:
    EMPLOYEE_VIEW -> EMPLOYEES

The migration execution order is calculated separately, with prerequisites
appearing before the objects that depend on them.

This is a first-pass static analyzer, not a complete PL/SQL semantic analyzer.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Iterable

from app.contracts.models import IRObject, ObjectType


@dataclass(frozen=True, slots=True)
class DependencyEdge:
    """A relationship between two database objects."""

    dependent: str
    dependency: str
    dependency_type: str


@dataclass(frozen=True, slots=True)
class DependencyGraph:
    """Immutable result of dependency analysis."""

    nodes: tuple[str, ...]
    edges: tuple[DependencyEdge, ...]
    execution_order: tuple[str, ...]
    cycles: tuple[tuple[str, ...], ...]

    @property
    def has_cycles(self) -> bool:
        return bool(self.cycles)


def _normalize_identifier(identifier: str) -> str:
    """Normalize an unqualified SQL identifier."""

    return identifier.strip().strip('"').upper()


def _extract_references(
    obj: IRObject,
    objects_by_name: dict[str, IRObject],
) -> set[str]:
    """Extract references to known objects from an object's source."""

    source = obj.source_code or ""
    references: set[str] = set()

    tables = {
        name
        for name, candidate in objects_by_name.items()
        if candidate.type == ObjectType.TABLE
    }

    sequences = {
        name
        for name, candidate in objects_by_name.items()
        if candidate.type == ObjectType.SEQUENCE
    }

    packages = {
        name
        for name, candidate in objects_by_name.items()
        if candidate.type == ObjectType.PACKAGE
    }

    routines = {
        name
        for name, candidate in objects_by_name.items()
        if candidate.type in {ObjectType.FUNCTION, ObjectType.PROCEDURE}
    }

    # 1. Table reads and writes already identified by the Oracle parser.
    references.update(
        name for name in obj.reads_tables if name in tables
    )
    references.update(
        name for name in obj.writes_tables if name in tables
    )

    # 2. Foreign-key references.
    for match in re.finditer(
        r"\bREFERENCES\s+(?:[\w$#]+\.)?([\w$#]+)",
        source,
        re.IGNORECASE,
    ):
        name = _normalize_identifier(match.group(1))
        if name in tables:
            references.add(name)

    # 3. Trigger attachment: CREATE TRIGGER ... ON table.
    if obj.type == ObjectType.TRIGGER:
        for match in re.finditer(
            r"\bON\s+(?:[\w$#]+\.)?([\w$#]+)",
            source,
            re.IGNORECASE,
        ):
            name = _normalize_identifier(match.group(1))
            if name in tables:
                references.add(name)

    # 4. Sequence references such as payroll_run_seq.NEXTVAL.
    for match in re.finditer(
        r"\b([\w$#]+)\s*\.\s*(?:NEXTVAL|CURRVAL)\b",
        source,
        re.IGNORECASE,
    ):
        name = _normalize_identifier(match.group(1))
        if name in sequences:
            references.add(name)

    # 5. Package-qualified references, including methods and constants.
    for match in re.finditer(
        r"\b([\w$#]+)\s*\.\s*[\w$#]+",
        source,
        re.IGNORECASE,
    ):
        name = _normalize_identifier(match.group(1))
        if name in packages:
            references.add(name)

    # 6. Direct function/procedure calls.
    for match in re.finditer(
        r"\b([\w$#]+)\s*\(",
        source,
        re.IGNORECASE,
    ):
        name = _normalize_identifier(match.group(1))
        if name in routines:
            references.add(name)

    references.discard(obj.name)
    return references


def _find_cycles(
    nodes: set[str],
    edges: set[tuple[str, str]],
) -> tuple[tuple[str, ...], ...]:
    """Find strongly connected components using Tarjan's algorithm."""

    adjacency: dict[str, list[str]] = defaultdict(list)

    # Convert dependent -> dependency into dependency -> dependent.
    # SCC membership is unchanged by reversing every edge.
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

            # A multi-node component is cyclic. A self-loop is also cyclic.
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

    # dependency -> dependent is the direction required for execution.
    adjacency: dict[str, set[str]] = defaultdict(set)
    indegree = {node: 0 for node in nodes}

    for dependent, dependency in edges:
        if dependent == dependency:
            continue

        if dependent not in adjacency[dependency]:
            adjacency[dependency].add(dependent)
            indegree[dependent] += 1

    ready = deque(sorted(
        node for node, degree in indegree.items() if degree == 0
    ))

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
    """Build a typed dependency graph from parsed IR objects."""

    objects = list(objects)

    objects_by_name: dict[str, IRObject] = {}

    for obj in objects:
        name = _normalize_identifier(obj.name)

        if name in objects_by_name:
            raise ValueError(
                f"Duplicate object name '{name}'. "
                "Overloaded or schema-qualified objects need "
                "a richer object identity before graph construction."
            )

        objects_by_name[name] = obj

    nodes = set(objects_by_name)
    edge_pairs: set[tuple[str, str]] = set()

    for obj in objects:
        dependent = _normalize_identifier(obj.name)

        references = _extract_references(obj, objects_by_name)

        for dependency in references:
            edge_pairs.add((dependent, dependency))

    edges = tuple(
        DependencyEdge(
            dependent=dependent,
            dependency=dependency,
            dependency_type=objects_by_name[dependency].type.value,
        )
        for dependent, dependency in sorted(edge_pairs)
    )

    cycles = _find_cycles(nodes, edge_pairs)
    execution_order = _topological_order(nodes, edge_pairs)

    return DependencyGraph(
        nodes=tuple(sorted(nodes)),
        edges=edges,
        execution_order=execution_order,
        cycles=cycles,
    )


"""Dependency analysis for parsed Oracle database objects.

The analyzer builds a typed dependency graph from canonical object IDs.

Edge convention:
    dependent -> dependency

Migration execution order is dependency-first.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Iterable

from app.contracts.models import IRObject
from app.contracts import parse_object_id


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

    return DependencyGraph(
        nodes=tuple(sorted(nodes)),
        edges=edges,
        execution_order=execution_order,
        cycles=cycles,
    )

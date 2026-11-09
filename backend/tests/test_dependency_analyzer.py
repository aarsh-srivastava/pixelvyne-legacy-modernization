
"""Tests for Oracle database dependency analysis."""

from pathlib import Path

import pytest

from app.contracts.models import ObjectType
from app.parsers.oracle_parser import parse_sources
from app.parsers.dependency_analyzer import (
    analyze_dependencies,
    DependencyGraph,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ORACLE_SAMPLES_DIR = PROJECT_ROOT / "samples" / "oracle"


@pytest.fixture(scope="module")
def parsed_objects():
    """Parse the seven actual Oracle sample files."""

    files = sorted(ORACLE_SAMPLES_DIR.glob("*.sql"))

    assert len(files) == 7, (
        f"Expected 7 Oracle SQL files, found {len(files)}"
    )

    sources = {
        file.name: file.read_text(encoding="utf-8")
        for file in files
    }

    return parse_sources(sources)


@pytest.fixture(scope="module")
def dependency_graph(parsed_objects):
    """Build the dependency graph once for the test module."""

    return analyze_dependencies(parsed_objects)


# ---------------------------------------------------------------------
# Graph structure
# ---------------------------------------------------------------------

def test_graph_contains_all_14_objects(dependency_graph):
    assert len(dependency_graph.nodes) == 14


def test_graph_has_unique_nodes(dependency_graph):
    assert len(dependency_graph.nodes) == len(set(dependency_graph.nodes))


def test_graph_contains_dependencies(dependency_graph):
    assert len(dependency_graph.edges) > 0


def test_all_edges_reference_existing_objects(dependency_graph):
    nodes = set(dependency_graph.nodes)

    for edge in dependency_graph.edges:
        assert edge.dependent in nodes
        assert edge.dependency in nodes


def test_edges_have_dependency_types(dependency_graph):
    for edge in dependency_graph.edges:
        assert edge.dependency_type
        assert isinstance(edge.dependency_type, str)


def test_no_duplicate_edges(dependency_graph):
    edge_pairs = [
        (edge.dependent, edge.dependency)
        for edge in dependency_graph.edges
    ]

    assert len(edge_pairs) == len(set(edge_pairs))


# ---------------------------------------------------------------------
# Table dependencies
# ---------------------------------------------------------------------

def test_employee_table_depends_on_department_table(dependency_graph):
    assert any(
        edge.dependent == "EMPLOYEES"
        and edge.dependency == "DEPARTMENTS"
        and edge.dependency_type == ObjectType.TABLE.value
        for edge in dependency_graph.edges
    )


def test_payroll_runs_depends_on_employees(dependency_graph):
    assert any(
        edge.dependent == "PAYROLL_RUNS"
        and edge.dependency == "EMPLOYEES"
        for edge in dependency_graph.edges
    )


# ---------------------------------------------------------------------
# View and trigger dependencies
# ---------------------------------------------------------------------

def test_org_chart_view_depends_on_employees(dependency_graph):
    assert any(
        edge.dependent == "ORG_CHART"
        and edge.dependency == "EMPLOYEES"
        for edge in dependency_graph.edges
    )


def test_trigger_depends_on_employees(
    dependency_graph,
    parsed_objects,
):
    trigger = next(
        obj for obj in parsed_objects
        if obj.type == ObjectType.TRIGGER
    )

    assert any(
        edge.dependent == trigger.name
        and edge.dependency == "EMPLOYEES"
        for edge in dependency_graph.edges
    )


# ---------------------------------------------------------------------
# Package and sequence dependencies
# ---------------------------------------------------------------------

def test_monthly_payroll_depends_on_package(dependency_graph):
    assert any(
        edge.dependent == "MONTHLY_PAYROLL"
        and edge.dependency == "PAYROLL_PKG"
        and edge.dependency_type == ObjectType.PACKAGE.value
        for edge in dependency_graph.edges
    )


def test_monthly_payroll_depends_on_sequence(dependency_graph):
    assert any(
        edge.dependent == "MONTHLY_PAYROLL"
        and edge.dependency == "PAYROLL_RUN_SEQ"
        and edge.dependency_type == ObjectType.SEQUENCE.value
        for edge in dependency_graph.edges
    )


# ---------------------------------------------------------------------
# Execution ordering
# ---------------------------------------------------------------------

def test_execution_order_places_dependencies_first(dependency_graph):
    positions = {
        name: index
        for index, name in enumerate(dependency_graph.execution_order)
    }

    for edge in dependency_graph.edges:
        if (
            edge.dependent in positions
            and edge.dependency in positions
        ):
            assert positions[edge.dependency] < positions[edge.dependent]


def test_execution_order_contains_all_objects_when_acyclic(
    dependency_graph,
):
    if not dependency_graph.has_cycles:
        assert len(dependency_graph.execution_order) == len(
            dependency_graph.nodes
        )


def test_execution_order_contains_only_known_objects(dependency_graph):
    assert set(dependency_graph.execution_order).issubset(
        set(dependency_graph.nodes)
    )


# ---------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------

def test_sample_graph_has_no_cycles(dependency_graph):
    assert dependency_graph.has_cycles is False
    assert dependency_graph.cycles == ()


def test_has_cycles_matches_cycle_collection(dependency_graph):
    assert dependency_graph.has_cycles == bool(dependency_graph.cycles)


# ---------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------

def test_empty_input_returns_empty_graph():
    graph = analyze_dependencies([])

    assert isinstance(graph, DependencyGraph)
    assert graph.nodes == ()
    assert graph.edges == ()
    assert graph.execution_order == ()
    assert graph.cycles == ()
    assert graph.has_cycles is False


def test_duplicate_object_names_are_rejected(parsed_objects):
    with pytest.raises(ValueError, match="Duplicate object name"):
        analyze_dependencies(parsed_objects + [parsed_objects[0]])

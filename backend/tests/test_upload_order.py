"""PR 1 / G7: the order files are uploaded in never changes the analysis.

The 18 Nov experiment, made permanent: the seven sample files in 51 orders from fixed
seeds, plus the reverse order, must give identical objects, locations, dependencies,
links, unresolved references, blocked objects, execution order, cycles, findings (with
lines), routes and references by line. Two files defining the same object must give the
same G1 error text in either order. A failure names the seed.
"""

from __future__ import annotations

import random

import pytest

from app.migration.features import analyze_package, detect_features, known_names
from app.parsers.dependency_analyzer import analyze_dependencies
from app.parsers.oracle_parser import DuplicateDefinitionError, parse_sources_detailed
from app.parsers.references import references_by_line
from tests.pr1_samples import oracle_sample_sources

SEEDS = list(range(51))


def analysis(sources: dict[str, str]) -> dict:
    """Everything the order must not change, as plain comparable values."""
    parsed = parse_sources_detailed(sources)
    objects = parsed.objects
    graph = analyze_dependencies(objects)
    packages = {o.name: analyze_package(o.source_code, [x.source_code for x in objects if x is not o])
                for o in objects if o.type.value == "package"}
    names = known_names(objects)
    findings = {}
    for obj in objects:
        if obj.type.value in ("table", "sequence", "package"):
            continue
        report = detect_features(obj.source_code, package=obj.package, packages=packages, names=names)
        findings[obj.object_id] = (
            report.handling.value,
            tuple((f.feature, f.handling.value, f.detail, f.line, f.text) for f in report.findings),
        )
    return {
        "objects": [o.model_dump() for o in objects],
        "locations": dict(sorted(parsed.locations.items())),
        "edges": graph.edges,
        "links": graph.links,
        "fires": graph.fires,
        "unresolved": graph.unresolved,
        "blocked": dict(graph.blocked),
        "needs_decision": dict(graph.needs_decision),
        "execution_order": graph.execution_order,
        "cycles": graph.cycles,
        "findings": findings,
        "references": references_by_line(objects, graph, parsed.locations),
    }


@pytest.fixture(scope="module")
def baseline():
    sources = oracle_sample_sources()
    return sources, analysis(sources)


@pytest.mark.parametrize("seed", SEEDS)
def test_any_upload_order_gives_the_same_analysis(baseline, seed):
    sources, expected = baseline
    items = list(sources.items())
    random.Random(seed).shuffle(items)
    assert analysis(dict(items)) == expected, f"upload order from seed {seed} changed the analysis"


def test_the_reverse_order_gives_the_same_analysis(baseline):
    sources, expected = baseline
    assert analysis(dict(reversed(list(sources.items())))) == expected


def test_the_seeds_really_produce_different_orders(baseline):
    sources, _ = baseline
    orders = set()
    for seed in SEEDS:
        names = list(sources)
        random.Random(seed).shuffle(names)
        orders.add(tuple(names))
    assert len(orders) >= 45


def test_duplicates_give_the_same_error_in_any_order(baseline):
    sources, _ = baseline
    with_copy = dict(sources) | {"backup/05_procedures_old.sql": sources["05_procedures.sql"]}
    texts = set()
    for seed in SEEDS[:10]:
        items = list(with_copy.items())
        random.Random(seed).shuffle(items)
        with pytest.raises(DuplicateDefinitionError) as info:
            parse_sources_detailed(dict(items))
        texts.add(str(info.value))
    assert len(texts) == 1
    (text,) = texts
    assert "05_procedures.sql line" in text and "backup/05_procedures_old.sql line" in text

"""PR 1 / G4: the labelled detection benchmark, scored on every test run.

Gate (agreed with Aarsh, 18 Nov): no snippet that is not eligible for deterministic rules
is routed to the rules. That is recall of the "not eligible" class, and it must be 100%.

Reported, not gated: precision (of the snippets sent away from the rules, how many truly
needed it), every false positive and false negative by id, and route mismatches inside the
"not eligible" class (Gemini vs review). Passing means the detector passed this benchmark;
it is not a claim about real databases.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from app.migration.features import Handling, detect_features

BENCHMARK = Path(__file__).with_name("fixtures") / "detection_benchmark.json"
SNIPPETS = json.loads(BENCHMARK.read_text(encoding="utf-8"))["snippets"]


def route(snippet: dict) -> Handling:
    return detect_features(snippet["source"], names=snippet.get("names")).handling


def score(snippets: list[dict]) -> dict:
    """Confusion counts for 'not eligible for deterministic rules' (the positive class)."""
    tp = fp = fn = tn = 0
    false_positives, false_negatives, wrong_route = [], [], []
    for snippet in snippets:
        expected, actual = Handling(snippet["expected_route"]), route(snippet)
        positive, flagged = expected is not Handling.RULE, actual is not Handling.RULE
        if positive and flagged:
            tp += 1
            if actual is not expected:
                wrong_route.append(f"{snippet['id']}: expected {expected.value}, got {actual.value}")
        elif positive:
            fn += 1
            false_negatives.append(snippet["id"])
        elif flagged:
            fp += 1
            false_positives.append(snippet["id"])
        else:
            tn += 1
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "recall": tp / (tp + fn) if tp + fn else 1.0,
        "precision": tp / (tp + fp) if tp + fp else 1.0,
        "false_positives": false_positives, "false_negatives": false_negatives, "wrong_route": wrong_route,
    }


def test_benchmark_has_at_least_40_snippets_with_unique_ids():
    assert len(SNIPPETS) >= 40
    assert len({s["id"] for s in SNIPPETS}) == len(SNIPPETS)
    groups = Counter(s["group"] for s in SNIPPETS)
    assert set(groups) <= {"supported", "recognised_gemini", "recognised_review", "not_recognised", "trap", "held_out"}
    assert all(groups[g] for g in ("supported", "recognised_gemini", "recognised_review", "not_recognised", "trap"))


def test_gate_no_ineligible_snippet_reaches_the_rules(capsys):
    result = score(SNIPPETS)
    groups = sorted({s["group"] for s in SNIPPETS})
    with capsys.disabled():
        print("\n\nDetection benchmark (positive = not eligible for deterministic rules)")
        print(f"  snippets {len(SNIPPETS)}  TP {result['tp']}  FP {result['fp']}  FN {result['fn']}  TN {result['tn']}")
        print(f"  recall {result['recall']:.3f} (gate: 1.000)  precision {result['precision']:.3f} (reported only)")
        for group in groups:
            part = score([s for s in SNIPPETS if s["group"] == group])
            print(f"  {group:18} recall {part['recall']:.3f}  precision {part['precision']:.3f}")
        print(f"  false positives: {result['false_positives'] or 'none'}")
        print(f"  false negatives: {result['false_negatives'] or 'none'}")
        print(f"  Gemini/review mismatches: {result['wrong_route'] or 'none'}")
    assert result["false_negatives"] == [], "ineligible snippets routed to the rules"


@pytest.mark.parametrize("snippet", [s for s in SNIPPETS if "expected_feature" in s], ids=lambda s: s["id"])
def test_expected_feature_is_found_on_its_line(snippet):
    findings = detect_features(snippet["source"], names=snippet.get("names")).findings
    matching = [f for f in findings if f.feature == snippet["expected_feature"]]
    assert matching, f"{snippet['id']}: {snippet['expected_feature']} not found in {[f.feature for f in findings]}"
    if "expected_line" in snippet:
        assert matching[0].line == snippet["expected_line"]


def test_the_scorer_counts_a_miss_as_a_false_negative():
    fake = [{"id": "x", "group": "trap", "expected_route": "review", "source": "v := NVL(a, 0);"}]
    assert score(fake)["false_negatives"] == ["x"]

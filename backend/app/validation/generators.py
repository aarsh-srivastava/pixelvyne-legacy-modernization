"""Deterministic edge-case generation (step 7c, D-7c.1).

``generate(seed, budget)`` returns the same list of TestCases, byte for byte, for the same
generator version, seed and budget, on any machine and Python version:

* the core cases (edge_catalogue.core_specs) always come first and cover every object,
  category and translator rule, whatever the budget;
* the rest of the budget is filled from the in-contract pool by a seeded draw that uses
  only ``random.Random(seed).random()`` (the one method whose sequence Python guarantees
  across versions; ``sample`` and ``shuffle`` are not guaranteed), round-robin over the
  objects so none is crowded out;
* a case identical to a golden test is never emitted, so no behaviour is judged twice
  against two different references;
* IDs are ``gen-<object>-<category>-<8 hex>``, the hex a hash of the case's canonical form,
  so a case keeps its ID across runs, machines and budget changes.

No clock, no environment, no unordered iteration. Generators produce values only; the SQL a
case can cause is fixed elsewhere (runners, fixtures.py, edge_catalogue.TRIGGER_ACTIONS).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import random
from decimal import Decimal
from typing import Any

from app.contracts.models import GenerationInfo, SetupCall, TestCase
from app.validation.edge_catalogue import SHORT, Spec, core_specs, pool_specs
from app.validation.golden_tests import GOLDEN_TESTS

GENERATOR_VERSION = "7c.1"
DEFAULT_SEED = 7_000_003
DEFAULT_BUDGET = 150          # Aarsh, 16 Nov: the default live run
MAX_BUDGET = 400              # the maximum supported budget; no override in 7c


def _canon(value: Any) -> Any:
    """A JSON-ready, engine-neutral form: numbers by value (7 == 7.0 == Decimal('7'))."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, (int, Decimal)):
        number = Decimal(value).normalize()
        return {"n": format(number, "f") if number == number.to_integral() else str(number)}
    if isinstance(value, dt.datetime):
        return {"t": value.isoformat()}
    if isinstance(value, dict):
        return {str(k).lower(): _canon(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]).lower())}
    if isinstance(value, (list, tuple)):
        return [_canon(v) for v in value]
    raise TypeError(f"generated values must be None, str, int, Decimal or datetime, not {type(value).__name__}")


def canonical_form(case: TestCase | Spec) -> dict[str, Any]:
    """What makes two cases the same test: object, inputs, data, setup, action, ordering."""
    setup = case.setup_steps() if isinstance(case, TestCase) else list(case.setup)
    fixtures = case.fixture_rows if isinstance(case, TestCase) else case.fixtures
    return {
        "object_id": case.object_id,
        "inputs": _canon(case.inputs),
        "fixtures": _canon(fixtures),
        "setup": [{"object_id": s.object_id, "inputs": _canon(s.inputs)} for s in setup],
        "action": case.action,
        "order_by": list(case.order_by),
    }


def _digest(form: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(form, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def case_id(spec: Spec) -> str:
    return f"gen-{SHORT[spec.object_id]}-{spec.category}-{_digest(canonical_form(spec))[:8]}"


def _golden_forms() -> set[str]:
    return {_digest(canonical_form(c)) for cases in GOLDEN_TESTS.values() for c in cases}


def _to_case(spec: Spec, seed: int) -> TestCase:
    return TestCase(
        test_id=case_id(spec),
        object_id=spec.object_id,
        description=spec.note or f"{spec.category} ({spec.rule or 'no rule'})",
        inputs=dict(spec.inputs),
        fixture_rows={t: [dict(r) for r in rows] for t, rows in spec.fixtures.items()},
        setup=list(spec.setup),
        action=spec.action,
        order_by=list(spec.order_by),
        origin="rule",
        rationale=spec.note or None,
        generation=GenerationInfo(generator_version=GENERATOR_VERSION, seed=seed, category=spec.category,
                                  rule=spec.rule, in_contract=spec.in_contract, relation=spec.relation),
    )


def _unique(specs: list[Spec], skip: set[str]) -> list[Spec]:
    """Drop golden duplicates and repeats, keeping the first occurrence (order is fixed)."""
    out, seen = [], set(skip)
    for spec in specs:
        key = _digest(canonical_form(spec))
        if key not in seen:
            seen.add(key)
            out.append(spec)
    return out


def generate(seed: int = DEFAULT_SEED, budget: int = DEFAULT_BUDGET,
             objects: set[str] | None = None) -> list[TestCase]:
    """The generated cases for ``seed`` and ``budget``, optionally for some objects only."""
    if budget > MAX_BUDGET:
        raise ValueError(f"budget {budget} is above the maximum supported budget of {MAX_BUDGET}")
    golden = _golden_forms()
    core = _unique(core_specs(), golden)
    if len(core) > budget:
        raise ValueError(f"budget {budget} is below the {len(core)} core cases that cover every object, "
                         "category and rule; it would leave coverage out")
    taken = {_digest(canonical_form(s)) for s in core}
    pool = _unique(pool_specs(), golden | taken)

    rng = random.Random(seed)
    keyed = [(rng.random(), i, spec) for i, spec in enumerate(pool)]   # one draw per pool entry, in order
    by_object: dict[str, list[Spec]] = {}
    for _key, _i, spec in sorted(keyed, key=lambda t: (t[0], t[1])):
        by_object.setdefault(spec.object_id, []).append(spec)
    order = sorted(by_object)
    sampled: list[Spec] = []
    while len(core) + len(sampled) < budget and any(by_object[o] for o in order):
        for object_id in order:
            if by_object[object_id] and len(core) + len(sampled) < budget:
                sampled.append(by_object[object_id].pop(0))

    cases = [_to_case(s, seed) for s in core + sampled]
    if objects is not None:
        cases = [c for c in cases if c.object_id in objects]
    return cases


def case_set_hash(cases: list[TestCase]) -> str:
    """One hash for the whole generated set: ids and canonical forms, in order."""
    return _digest({"cases": [[c.test_id, canonical_form(c)] for c in cases]})


def find_case(test_id: str, seed: int = DEFAULT_SEED, budget: int = MAX_BUDGET) -> TestCase:
    """The generated case with this ID (searched in the largest supported set)."""
    for case in generate(seed, budget):
        if case.test_id == test_id:
            return case
    raise LookupError(f"no generated case {test_id} for seed {seed}")


def setup_calls_of(case: TestCase) -> list[SetupCall]:
    return case.setup_steps()

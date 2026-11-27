"""The repair loop for one object: convert, validate, and on failure ask the converter
for a repaired version, up to max_attempts times.

    convert ──deployed──────────> validate ──passed────────────────────> passed
       ^   ├─failed, retryable,  │  ├─requires_repair, tries left──────┐
       │   │  tries left ────────┤  ├─requires_repair, no tries left──> needs_human
       │   └─failed, permanent   │  └─needs_human (harness error)─────> needs_human
       │      or no tries left ──┼──────────────────────────────────────> needs_human
       └──── RepairContext + FailureReport <─────────────────────────────┘

The graph depends only on the Converter protocol and a validate callable, so the real
converter and a stand-in plug in the same way, and the graph is tested without any
database. Database connections live inside the validate callable and the converter.

**What a repair attempt is given** (step 7a, D-6.7). Every attempt names its run:
``convert(obj, None, run_id=...)`` first, then ``convert(obj, feedback,
context=RepairContext(...), run_id=...)``, the context built fresh from this run's own state
(the converter refuses a context whose run does not match the attempt it describes).
Converters written before 7a, ``convert(obj, feedback=None)``, keep working: the loop reads
the signature and passes only the keywords a converter accepts. A RepairContext is passed
only to a converter that accepts BOTH ``context`` and ``run_id``, so it can check the
evidence against the attempt's own run; any other converter gets exactly what it got before
7a (a deploy error as a FailureReport) and no repair evidence, so it can never be a route to
Gemini repairs:

* ``context.previous``: the attempt being repaired (its version, code, whether it
  deployed, and its deploy error if not);
* ``context.last_validation``: the last real test run, with its own ``conversion_version``;
* ``feedback``: the FailureReport of that same test run (``None`` before any test ran).
  It only ever describes failing tests; a deploy error is never dressed up as one.

**When the loop stops** (D5, permanent failures). A failed attempt is retried only if
another attempt can help: ``failure_code == "gemini.deploy_failed"`` (Gemini can be
asked to fix SQL PostgreSQL rejected), or a converter that sets no failure code and did
not ask for review (legacy behaviour, e.g. the scripted stand-in). Everything else stops
at once: REVIEW findings, security refusals, unchanged Gemini output, Gemini/API errors,
invalid repair evidence, objects with no repair route. A test run that fails is repaired
while attempts remain; a harness error (the test could not be judged) stops at once.

**What the run records.** Every attempt with its version, tool, model, failure code and
category, deploy and test outcome; the final category and an accurate reason; the last
test run with the version it tested (``final_validation`` only when it tested the final
version); and ``last_deployed_version``, the version PostgreSQL holds when the run ends
(a failed or refused deploy is rolled back, so the previous version stays live).
"""

from __future__ import annotations

import inspect
import operator
import uuid
from dataclasses import dataclass
from typing import Annotated, Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from app.contracts import (
    AttemptRecord,
    ConversionResult,
    Converter,
    FailureCategory,
    FailureReport,
    IRObject,
    PreviousAttempt,
    RepairContext,
    RepairOutcome,
    RepairRun,
    TestCase,
    TestStatus,
    ValidationResult,
    ValidationStatus,
)

DEFAULT_MAX_ATTEMPTS = 3

# RepairOutcome, FailureCategory, AttemptRecord and RepairRun are contracts since 0.7
# (app.contracts.models). They are re-exported here, so existing imports keep working.
__all__ = [
    "DEFAULT_MAX_ATTEMPTS", "RETRYABLE_CODES", "AttemptRecord", "FailureCategory", "RepairDeps", "RepairOutcome",
    "RepairRun", "RepairState", "ValidateFn", "build_repair_graph", "can_retry", "failure_category",
    "repair_context", "run_repair_loop", "summarize_failures",
]

ValidateFn = Callable[[IRObject, list[TestCase], int], ValidationResult]


_CATEGORY_BY_CODE = {
    "review_required": FailureCategory.UNSUPPORTED,
    "not_converted": FailureCategory.UNSUPPORTED,
    "needs_gemini": FailureCategory.UNSUPPORTED,
    "translator_gap": FailureCategory.UNSUPPORTED,
    "not_eligible": FailureCategory.UNSUPPORTED,
    "secret_in_source": FailureCategory.UNSUPPORTED,
    "no_repair_route": FailureCategory.NO_REPAIR_ROUTE,
    "repair_unavailable": FailureCategory.NO_REPAIR_ROUTE,
    "deploy_failed": FailureCategory.DEPLOY_FAILED,
    "deploy_refused": FailureCategory.DEPLOY_REFUSED,
    "refused": FailureCategory.DEPLOY_REFUSED,
    "unchanged_output": FailureCategory.UNCHANGED_OUTPUT,
    "invalid_evidence": FailureCategory.INVALID_EVIDENCE,
    "converter_error": FailureCategory.CONVERTER_ERROR,
    "not_configured": FailureCategory.GEMINI_FAILED,
    "api_error": FailureCategory.GEMINI_FAILED,
    "cache_error": FailureCategory.GEMINI_FAILED,
    "malformed_response": FailureCategory.GEMINI_FAILED,
    "unsupported": FailureCategory.GEMINI_FAILED,       # only as gemini.unsupported: Gemini declined
    "review_requested": FailureCategory.GEMINI_FAILED,
}

#: The only failure another attempt can fix: Gemini SQL that PostgreSQL rejected.
RETRYABLE_CODES = frozenset({"gemini.deploy_failed"})


def failure_category(conversion: ConversionResult) -> FailureCategory | None:
    """The category of a failed attempt; None if it deployed."""
    if conversion.compiled_ok:
        return None
    code = conversion.failure_code
    if code is None:  # a converter that sets no code: all we know is that it did not deploy
        return FailureCategory.UNSUPPORTED if conversion.needs_review else FailureCategory.DEPLOY_FAILED
    return _CATEGORY_BY_CODE.get(code.removeprefix("gemini."), FailureCategory.CONVERTER_ERROR)


def can_retry(conversion: ConversionResult) -> bool:
    """May another attempt help after this failed one? (D5: never for review or refusals.)"""
    if conversion.failure_code is None:
        return not conversion.needs_review  # legacy converters: as before, unless review was asked for
    return conversion.failure_code in RETRYABLE_CODES


@dataclass(frozen=True)
class RepairDeps:
    converter: Converter
    validate: ValidateFn
    max_attempts: int = DEFAULT_MAX_ATTEMPTS


class RepairState(TypedDict, total=False):
    obj: IRObject
    cases: list[TestCase]
    run_id: str
    attempt: int
    feedback: FailureReport | None        # always the FailureReport of `validation`
    conversion: ConversionResult | None   # the latest attempt
    validation: ValidationResult | None   # the last real test run (kept across failed deploys)
    attempts: Annotated[list[AttemptRecord], operator.add]
    outcome: RepairOutcome
    reason: str
    category: FailureCategory | None


# ---------------------------------------------------------------- diagnosis

def _fmt(value) -> str:
    """A value as a reader (or an LLM prompt) should see it: 24000.00, 'text', NULL."""
    if value is None:
        return "NULL"
    if isinstance(value, str):
        return repr(value)
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {_fmt(v)}" for k, v in value.items()) + "}"
    return str(value)


def summarize_failures(validation: ValidationResult, limit: int = 8) -> str:
    """Plain-English diagnosis of failing tests, for the converter's next attempt."""
    failing = [t for t in validation.tests if t.status == TestStatus.FAILED]
    lines = [f"{len(failing)} of {len(validation.tests)} tests behave differently from Oracle."]
    for outcome in failing[:limit]:
        for diff in outcome.diffs:
            if diff.kind == "error":
                lines.append(f"- {outcome.test_id}: Oracle raised {diff.source_value or 'nothing'}, "
                             f"PostgreSQL raised {diff.target_value or 'nothing'}.")
            elif diff.kind == "missing":
                lines.append(f"- {outcome.test_id}: {diff.path}: Oracle produced {_fmt(diff.source_value)}, "
                             f"PostgreSQL did not.")
            elif diff.kind == "extra":
                lines.append(f"- {outcome.test_id}: {diff.path}: PostgreSQL produced {_fmt(diff.target_value)}, "
                             f"Oracle did not.")
            else:
                lines.append(f"- {outcome.test_id}: {diff.path}: Oracle {_fmt(diff.source_value)}, "
                             f"PostgreSQL {_fmt(diff.target_value)}.")
    if len(failing) > limit:
        lines.append(f"- ...and {len(failing) - limit} more failing tests.")
    return "\n".join(lines)


def repair_context(run_id: str, obj: IRObject, attempt: int, previous: ConversionResult,
                   last_validation: ValidationResult | None) -> RepairContext:
    """The evidence for repair attempt ``attempt``, from this run's own previous attempt."""
    return RepairContext(
        run_id=run_id,
        object_id=obj.object_id,
        attempt=attempt,
        previous=PreviousAttempt(
            version=previous.version,
            tool=previous.tool,
            code=previous.target_code,
            deployed=previous.compiled_ok,
            deploy_error=None if previous.compiled_ok else previous.compile_error,
        ),
        last_validation=last_validation,
    )


#: A converter gets a RepairContext only if its convert() accepts both of these.
REPAIR_KEYWORDS = frozenset({"context", "run_id"})


def _accepted_keywords(converter: Converter) -> frozenset[str]:
    """Which of the 7a keywords (``context``, ``run_id``) this converter's ``convert`` accepts.

    Converters written against the interface before 7a, ``convert(obj, feedback=None)``,
    accept neither; the loop then calls them exactly as before 7a (see the convert node).
    A ``**kwargs`` parameter accepts both. If the signature cannot be read, assume neither.
    """
    try:
        params = inspect.signature(converter.convert).parameters.values()
    except (TypeError, ValueError):
        return frozenset()
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
        return frozenset({"context", "run_id"})
    named = {p.name for p in params
             if p.kind in (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)}
    return frozenset({"context", "run_id"} & named)


def _legacy_compile_feedback(obj: IRObject, attempt: int, conversion: ConversionResult) -> FailureReport:
    """What a pre-7a converter was sent after an attempt that did not deploy (unchanged from before 7a)."""
    return FailureReport(
        object_id=obj.object_id, attempt=attempt, failing_tests=[],
        summary=f"The converted code did not compile in PostgreSQL: {conversion.compile_error}",
    )


def _last_run_note(validation: ValidationResult | None) -> str:
    if validation is None:
        return ""
    return (f" Last test run: v{validation.conversion_version}, "
            f"{validation.passed} passed, {validation.failed} failed.")


# ---------------------------------------------------------------- graph

def build_repair_graph(deps: RepairDeps):
    """Compile the loop. Invoke with {"obj": IRObject, "cases": [TestCase, ...]}."""
    accepted = _accepted_keywords(deps.converter)

    def convert(state: RepairState) -> RepairState:
        attempt = state.get("attempt", 0) + 1
        previous = state.get("conversion")
        run_id = state["run_id"]  # every attempt names its run, so evidence cannot cross runs
        extra = {"run_id": run_id} if "run_id" in accepted else {}
        if previous is None:  # the first attempt: no feedback, no context
            conversion = deps.converter.convert(state["obj"], None, **extra)
        elif REPAIR_KEYWORDS <= accepted:  # evidence only where the run can be checked independently
            extra["context"] = repair_context(run_id, state["obj"], attempt, previous, state.get("validation"))
            conversion = deps.converter.convert(state["obj"], state.get("feedback"), **extra)
        else:  # pre-7a, or cannot check the run: exactly the inputs a converter got before 7a
            feedback = (state.get("feedback") if previous.compiled_ok
                        else _legacy_compile_feedback(state["obj"], attempt - 1, previous))
            conversion = deps.converter.convert(state["obj"], feedback, **extra)
        update: RepairState = {"attempt": attempt, "conversion": conversion}
        if not conversion.compiled_ok:
            update["attempts"] = [AttemptRecord(
                attempt=attempt, version=conversion.version, tool=conversion.tool, compiled_ok=False,
                compile_error=conversion.compile_error, failure_code=conversion.failure_code,
                category=failure_category(conversion), model=conversion.model,
                needs_review=conversion.needs_review,
            )]
        return update

    def validate(state: RepairState) -> RepairState:
        conversion = state["conversion"]
        result = deps.validate(state["obj"], state["cases"], conversion.version)
        errors = sum(t.status == TestStatus.ERROR for t in result.tests)
        record = AttemptRecord(
            attempt=state["attempt"], version=conversion.version, tool=conversion.tool,
            compiled_ok=True, model=conversion.model, needs_review=conversion.needs_review,
            status=result.status, passed=result.passed, failed=result.failed,
            errors=errors, failing_tests=[t.test_id for t in result.tests if t.status != TestStatus.PASSED],
        )
        if result.status == ValidationStatus.REQUIRES_REPAIR:
            record.category = FailureCategory.TESTS_FAILED
        elif result.status == ValidationStatus.NEEDS_HUMAN:
            record.category = FailureCategory.HARNESS_ERROR
        update: RepairState = {"validation": result, "feedback": None}
        if result.status == ValidationStatus.REQUIRES_REPAIR:
            feedback = FailureReport(
                object_id=state["obj"].object_id, attempt=state["attempt"],
                failing_tests=[t for t in result.tests if t.status == TestStatus.FAILED],
                summary=summarize_failures(result),
            )
            update["feedback"] = feedback
            record.feedback_summary = feedback.summary
        update["attempts"] = [record]
        return update

    def finish_passed(state: RepairState) -> RepairState:
        if state["conversion"].needs_review:
            return {"outcome": RepairOutcome.PASSED_NEEDS_REVIEW, "category": None,
                    "reason": "All tests pass; the converter flagged this object for human sign-off."}
        return {"outcome": RepairOutcome.PASSED, "category": None,
                "reason": f"All tests pass on attempt {state['attempt']}."}

    def finish_human(state: RepairState) -> RepairState:
        conversion, validation, attempt = state["conversion"], state.get("validation"), state["attempt"]
        tested_last = (conversion.compiled_ok and validation is not None
                       and validation.conversion_version == conversion.version)
        if tested_last and validation.status == ValidationStatus.NEEDS_HUMAN:
            category = FailureCategory.HARNESS_ERROR
            reason = "The harness could not judge some tests (missing object or connection problem)."
        elif tested_last:
            category = FailureCategory.TESTS_FAILED
            reason = f"Still {validation.failed} failing tests on v{conversion.version} after {attempt} attempts."
        else:
            category = failure_category(conversion)
            error = conversion.compile_error or "no error text"
            if category is FailureCategory.NO_REPAIR_ROUTE and validation is not None:
                category = FailureCategory.TESTS_FAILED
                reason = (f"v{validation.conversion_version} fails {validation.failed} tests and there is no "
                          f"repair route for this object: {error}")
            elif can_retry(conversion):  # retryable, but out of attempts
                reason = f"Still not compiling after {attempt} attempts: v{conversion.version}: {error}"
            else:
                reason = f"Stopped at attempt {attempt} ({category.value}): {error}"
            reason += _last_run_note(validation) if category is not FailureCategory.TESTS_FAILED else ""
        return {"outcome": RepairOutcome.NEEDS_HUMAN, "reason": reason, "category": category}

    def after_convert(state: RepairState) -> str:
        conversion = state["conversion"]
        if conversion.compiled_ok:
            return "validate"
        if can_retry(conversion) and state["attempt"] < deps.max_attempts:
            return "convert"
        return "finish_human"

    def after_validate(state: RepairState) -> str:
        status = state["validation"].status
        if status == ValidationStatus.PASSED:
            return "finish_passed"
        if status == ValidationStatus.REQUIRES_REPAIR and state["attempt"] < deps.max_attempts:
            return "convert"
        return "finish_human"

    graph = StateGraph(RepairState)
    graph.add_node("convert", convert)
    graph.add_node("validate", validate)
    graph.add_node("finish_passed", finish_passed)
    graph.add_node("finish_human", finish_human)
    graph.add_edge(START, "convert")
    graph.add_conditional_edges("convert", after_convert, ["validate", "convert", "finish_human"])
    graph.add_conditional_edges("validate", after_validate, ["finish_passed", "convert", "finish_human"])
    graph.add_edge("finish_passed", END)
    graph.add_edge("finish_human", END)
    return graph.compile()


def run_repair_loop(deps: RepairDeps, obj: IRObject, cases: list[TestCase], *,
                    run_id: str | None = None) -> RepairRun:
    """Run one object through the loop and return the evidence for the report."""
    if deps.max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    run_id = run_id or uuid.uuid4().hex
    final = build_repair_graph(deps).invoke(
        {"obj": obj, "cases": cases, "run_id": run_id, "attempt": 0, "feedback": None, "conversion": None,
         "validation": None, "attempts": []},
        config={"recursion_limit": 4 * deps.max_attempts + 10},
    )
    conversion, validation = final.get("conversion"), final.get("validation")
    deployed = [a.version for a in final["attempts"] if a.compiled_ok]
    return RepairRun(
        object_id=obj.object_id,
        outcome=final["outcome"],
        reason=final["reason"],
        attempts=final["attempts"],
        final_conversion=conversion,
        final_validation=(validation if validation is not None and conversion is not None
                          and validation.conversion_version == conversion.version and conversion.compiled_ok
                          else None),
        run_id=run_id,
        category=final.get("category"),
        last_validation=validation,
        last_deployed_version=max(deployed) if deployed else None,
    )

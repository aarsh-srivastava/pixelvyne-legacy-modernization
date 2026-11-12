"""The repair loop for one object: convert, validate, and on failure send the
differences back to the converter, up to max_attempts times.

    convert ──compiled──> validate ──passed──────────────> passed
       ^   └─compile error──┐   ├─requires_repair, tries left─> diagnose ─┐
       │                    │   ├─requires_repair, no tries left─> needs_human
       │                    │   └─needs_human (harness error)───> needs_human
       └──── diagnose <─────┘                                            │
             (FailureReport) <───────────────────────────────────────────┘

The graph depends only on the Converter protocol and a validate callable, so the
real converter (Aarsh) and a stand-in plug in the same way, and the graph is tested
without any database. Database connections live inside the validate callable.

A harness error (missing object, lost connection) goes straight to a human: it means
the test could not be judged, so another conversion attempt would not help.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from app.contracts import (
    ConversionResult,
    ConversionTool,
    Converter,
    FailureReport,
    IRObject,
    TestCase,
    TestStatus,
    ValidationResult,
    ValidationStatus,
)

DEFAULT_MAX_ATTEMPTS = 3

ValidateFn = Callable[[IRObject, list[TestCase], int], ValidationResult]


class RepairOutcome(str, Enum):
    PASSED = "passed"                            # every test passed
    PASSED_NEEDS_REVIEW = "passed_needs_review"  # passed, but the converter flagged it for sign-off
    NEEDS_HUMAN = "needs_human"                  # out of attempts, or the harness could not judge


class AttemptRecord(BaseModel):
    """One convert + validate round, kept for the report."""

    attempt: int
    version: int
    tool: ConversionTool
    compiled_ok: bool
    compile_error: str | None = None
    status: ValidationStatus | None = None       # None when it did not compile
    passed: int = 0
    failed: int = 0
    errors: int = 0
    failing_tests: list[str] = Field(default_factory=list)
    feedback_summary: str | None = None          # what was sent back for the next attempt


class RepairRun(BaseModel):
    """Everything the report needs about one object's trip through the loop."""

    object_id: str
    outcome: RepairOutcome
    reason: str
    attempts: list[AttemptRecord]
    final_conversion: ConversionResult | None = None
    final_validation: ValidationResult | None = None


@dataclass(frozen=True)
class RepairDeps:
    converter: Converter
    validate: ValidateFn
    max_attempts: int = DEFAULT_MAX_ATTEMPTS


class RepairState(TypedDict, total=False):
    obj: IRObject
    cases: list[TestCase]
    attempt: int
    feedback: FailureReport | None
    conversion: ConversionResult | None
    validation: ValidationResult | None
    attempts: Annotated[list[AttemptRecord], operator.add]
    outcome: RepairOutcome
    reason: str


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


def _compile_feedback(obj: IRObject, attempt: int, conversion: ConversionResult) -> FailureReport:
    return FailureReport(
        object_id=obj.object_id, attempt=attempt, failing_tests=[],
        summary=f"The converted code did not compile in PostgreSQL: {conversion.compile_error}",
    )


# ---------------------------------------------------------------- graph

def build_repair_graph(deps: RepairDeps):
    """Compile the loop. Invoke with {"obj": IRObject, "cases": [TestCase, ...]}."""

    def convert(state: RepairState) -> RepairState:
        attempt = state.get("attempt", 0) + 1
        conversion = deps.converter.convert(state["obj"], state.get("feedback"))
        update: RepairState = {"attempt": attempt, "conversion": conversion, "validation": None}
        if not conversion.compiled_ok:
            feedback = _compile_feedback(state["obj"], attempt, conversion)
            update["feedback"] = feedback
            update["attempts"] = [AttemptRecord(
                attempt=attempt, version=conversion.version, tool=conversion.tool, compiled_ok=False,
                compile_error=conversion.compile_error, feedback_summary=feedback.summary,
            )]
        return update

    def validate(state: RepairState) -> RepairState:
        conversion = state["conversion"]
        result = deps.validate(state["obj"], state["cases"], conversion.version)
        errors = sum(t.status == TestStatus.ERROR for t in result.tests)
        record = AttemptRecord(
            attempt=state["attempt"], version=conversion.version, tool=conversion.tool,
            compiled_ok=True, status=result.status, passed=result.passed, failed=result.failed,
            errors=errors, failing_tests=[t.test_id for t in result.tests if t.status != TestStatus.PASSED],
        )
        update: RepairState = {"validation": result}
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
            return {"outcome": RepairOutcome.PASSED_NEEDS_REVIEW,
                    "reason": "All tests pass; the converter flagged this object for human sign-off."}
        return {"outcome": RepairOutcome.PASSED,
                "reason": f"All tests pass on attempt {state['attempt']}."}

    def finish_human(state: RepairState) -> RepairState:
        validation = state.get("validation")
        if validation is not None and validation.status == ValidationStatus.NEEDS_HUMAN:
            reason = "The harness could not judge some tests (missing object or connection problem)."
        elif validation is None:
            reason = f"Still not compiling after {state['attempt']} attempts."
        else:
            reason = f"Still {validation.failed} failing tests after {state['attempt']} attempts."
        return {"outcome": RepairOutcome.NEEDS_HUMAN, "reason": reason}

    def after_convert(state: RepairState) -> str:
        if state["conversion"].compiled_ok:
            return "validate"
        return "convert" if state["attempt"] < deps.max_attempts else "finish_human"

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


def run_repair_loop(deps: RepairDeps, obj: IRObject, cases: list[TestCase]) -> RepairRun:
    """Run one object through the loop and return the evidence for the report."""
    if deps.max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    final = build_repair_graph(deps).invoke(
        {"obj": obj, "cases": cases, "attempt": 0, "feedback": None, "attempts": []},
        config={"recursion_limit": 4 * deps.max_attempts + 10},
    )
    return RepairRun(
        object_id=obj.object_id,
        outcome=final["outcome"],
        reason=final["reason"],
        attempts=final["attempts"],
        final_conversion=final.get("conversion"),
        final_validation=final.get("validation"),
    )

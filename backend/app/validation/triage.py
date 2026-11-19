"""Failure classification for generated tests (step 7c, proposal section 7).

A fixed decision table, checked top to bottom in exactly this order (the code follows it);
the first rule that matches gives the class. Every result that is not a pass gets exactly one
class and a reason. Triage only labels a failure; it never turns one into a pass (Aarsh, 16 Nov):

1. reference_stale     replay against a corpus whose header no longer matches
2. non_deterministic   Oracle gave different results for the same case in two recordings
                       (for errors: a different code, or the same code raised in a different
                       place, per the ORA-06512 context; revision 3).
                       Checked before everything else that looks at the results, so an
                       unstable reference can never be labelled anything milder (Aarsh's
                       review of revision 1). Recording also checks every case on its own
                       (``unstable_cases``), whatever its class.
3. unsupported         ONLY with an explicit, documented reason: the object was not deployed
                       because feature detection routed it to Gemini or review (the reason is
                       the routing finding), or the case is listed in
                       edge_catalogue.ACCEPTED_DIFFERENCES with a decision reference. A case
                       that ran on both engines and differs is never unsupported.
4. environment         a harness error the routing does not explain (connection, timeout,
                       permission, refused fixture or action, object missing for no reason)
5. invalid_input       Oracle rejected the input at the call, before the object's logic ran:
                       a conversion error (invalid_number, value_error) whose ORA-06512 lines
                       never name the object. The generator is at fault.
6. error_map_gap       both engines raised, the categories differ, and either one is unmapped
                       (``other:<code>``) or the pair is a candidate equivalence under review
                       (CANDIDATE_EQUIVALENCES, with its evidence). No mapping is added here.
7. translation_defect  everything else: both ran, Oracle is the reference, PostgreSQL differs.
                       Out-of-contract cases are reported separately (severity), same class.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum

from app.contracts.models import ExecutionResult, IRObject, ObjectType, TestCase, TestOutcome, TestStatus
from app.migration.features import Handling, analyze_package, detect_features, known_names
from app.validation.comparator import compare
from app.validation.edge_catalogue import ACCEPTED_DIFFERENCES
from app.validation.errors import HARNESS, categorize
from app.validation.golden_tests import MASKED_COLUMNS


class FailureClass(str, Enum):
    ENVIRONMENT = "environment"
    REFERENCE_STALE = "reference_stale"
    UNSUPPORTED = "unsupported"
    INVALID_INPUT = "invalid_input"
    ERROR_MAP_GAP = "error_map_gap"
    NON_DETERMINISTIC = "non_deterministic"
    TRANSLATION_DEFECT = "translation_defect"


#: Pairs of different error categories under review as possibly the same behaviour, with the
#: evidence so far. A pair here makes the difference an error-map gap, not a defect; it does
#: NOT make the errors equal. Mapping them in errors.py needs Aarsh's decision (D-7c.5).
CANDIDATE_EQUIVALENCES: dict[frozenset[str], str] = {
    frozenset({"value_error", "value_too_long"}): (
        "H2, 16 Nov: calc_bonus(102, 'AB'). Oracle 23 raised ORA-06502 'character string buffer too small' "
        "at LEGACY.CALC_BONUS line 15; PostgreSQL 16 raised 22001 'value too long for type character "
        "varying(1)' in calc_bonus line 11 at assignment. Same statement (v_rating := p_override), same "
        "cause. ORA-06502 also covers numeric conversion errors, so a general mapping is not proposed."
    ),
}

_CONVERSION_CATEGORIES = {"invalid_number", "value_error"}


@dataclass(frozen=True)
class Triage:
    test_id: str
    object_id: str
    failure_class: FailureClass
    reason: str
    in_contract: bool

    @property
    def severity(self) -> str:
        if self.failure_class is not FailureClass.TRANSLATION_DEFECT:
            return "n/a"
        return "defect" if self.in_contract else "out-of-contract difference"


def routing_reasons(objects: Iterable[IRObject]) -> dict[str, str]:
    """Why an object is not converted by rules: object_id -> the routing finding (LLM/REVIEW only)."""
    objects = list(objects)
    packages = {o.name.lower(): analyze_package(o.source_code, [x.source_code for x in objects if x is not o])
                for o in objects if o.type == ObjectType.PACKAGE}
    names = known_names(objects)
    reasons = {}
    for obj in objects:
        if obj.type in (ObjectType.TABLE, ObjectType.SEQUENCE, ObjectType.PACKAGE):
            continue
        report = detect_features(obj.source_code, package=obj.package, packages=packages, names=names)
        if report.handling is Handling.REVIEW:
            reasons[obj.object_id] = "routed to review: " + report.review_reason()
        elif report.handling is Handling.LLM:
            details = "; ".join(f.detail for f in report.by_handling(Handling.LLM))
            reasons[obj.object_id] = f"needs Gemini (no deterministic rule for: {details})"
    return reasons


def _raised_inside(result: ExecutionResult, obj_name: str) -> bool:
    context = (result.error.context or "") if result.error else ""
    return obj_name.upper() in context.upper()


def _error_origin(result: ExecutionResult) -> str:
    """Where Oracle raised the error: its ORA-06512 context, whitespace-normalised ('' when none).

    The context says whether the error came from inside the object (and at which line) or at
    the call site, which is what tells invalid input from behaviour inside the object (rule 5).
    """
    context = (result.error.context or "") if result.error else ""
    return "\n".join(line.strip() for line in context.splitlines() if line.strip())


def _where(origin: str) -> str:
    return f"at {origin.splitlines()[0]!r}" if origin else "with no PL/SQL context"


def same_oracle_result(case: TestCase, first: ExecutionResult, second: ExecutionResult) -> tuple[bool, str]:
    """Whether two Oracle results for one case agree (masked columns left out), and how they differ.

    Two errors agree only if both the error code and the execution context match (revision 3,
    Aarsh's review): the same code raised at the call in one recording and inside the object in
    the other is different behaviour, so the case is unstable.
    """
    if first.error or second.error:
        a = first.error.code if first.error else None
        b = second.error.code if second.error else None
        if a != b:
            return False, f"error {a} vs {b}"
        where_a, where_b = _error_origin(first), _error_origin(second)
        if where_a != where_b:
            return False, f"error {a} raised {_where(where_a)} vs {_where(where_b)}"
        return True, f"error {a} {_where(where_a)}"
    outcome = compare(case, first, second, ordered_rows=bool(case.order_by), masked_columns=MASKED_COLUMNS)
    return outcome.status is TestStatus.PASSED, "; ".join(
        f"{d.path}: {d.source_value} vs {d.target_value}" for d in outcome.diffs[:3])


def unstable_cases(cases: Iterable[TestCase], first: Mapping[str, ExecutionResult],
                   second: Mapping[str, ExecutionResult]) -> list[str]:
    """IDs of the cases whose two Oracle recordings differ, or that lack a recording: checked for
    every case on its own, independently of triage. A corpus is never written unless this is empty."""
    unstable = []
    for case in cases:
        a, b = first.get(case.test_id), second.get(case.test_id)
        if a is None or b is None or not same_oracle_result(case, a, b)[0]:
            unstable.append(case.test_id)
    return unstable


def classify(case: TestCase, outcome: TestOutcome, *, object_name: str,
             routing: Mapping[str, str] | None = None, deployed: bool = True,
             stale_reference: str | None = None,
             oracle_again: ExecutionResult | None = None) -> Triage | None:
    """The class of a non-passing outcome (None for a pass). See the module docstring."""
    in_contract = case.generation.in_contract if case.generation else True

    def out(cls: FailureClass, reason: str) -> Triage:
        return Triage(case.test_id, case.object_id, cls, reason, in_contract)

    if stale_reference:
        return out(FailureClass.REFERENCE_STALE, stale_reference)
    src, tgt = outcome.source, outcome.target
    if oracle_again is not None:
        same, detail = same_oracle_result(case, src, oracle_again)
        if not same:
            return out(FailureClass.NON_DETERMINISTIC, "Oracle gave different results in two recordings: " + detail)
    if outcome.status is TestStatus.PASSED:
        return None

    src_cat = categorize("oracle", src.error) if src else HARNESS
    tgt_cat = categorize("postgres", tgt.error) if tgt else HARNESS
    routed = (routing or {}).get(case.object_id)

    # 3: a missing PostgreSQL object explained by its routing finding is unsupported.
    if tgt_cat == HARNESS and src_cat != HARNESS and not deployed and routed:
        return out(FailureClass.UNSUPPORTED, f"not deployed on PostgreSQL: {routed}")
    if src_cat == HARNESS or tgt_cat == HARNESS:
        side = "Oracle" if src_cat == HARNESS else "PostgreSQL"
        message = (src.error if src_cat == HARNESS else tgt.error)
        return out(FailureClass.ENVIRONMENT, f"{side} harness error: {message.message if message else 'no result'}")
    if case.test_id in ACCEPTED_DIFFERENCES:
        reason, decision = ACCEPTED_DIFFERENCES[case.test_id]
        return out(FailureClass.UNSUPPORTED, f"accepted difference ({decision}): {reason}")

    if src.error and src_cat in _CONVERSION_CATEGORIES and not _raised_inside(src, object_name):
        return out(FailureClass.INVALID_INPUT,
                   f"Oracle rejected the input at the call ({src.error.code}: {src.error.message})")

    if src.error and tgt.error and src_cat != tgt_cat:
        pair = frozenset({src_cat, tgt_cat})
        if src_cat.startswith("other:") or tgt_cat.startswith("other:"):
            return out(FailureClass.ERROR_MAP_GAP,
                       f"unmapped error: Oracle {src.error.code} ({src_cat}), PostgreSQL {tgt.error.code} ({tgt_cat})")
        if pair in CANDIDATE_EQUIVALENCES:
            return out(FailureClass.ERROR_MAP_GAP,
                       f"candidate equivalence under review: Oracle {src.error.code} ({src_cat}) vs PostgreSQL "
                       f"{tgt.error.code} ({tgt_cat}). Evidence: {CANDIDATE_EQUIVALENCES[pair]}")

    diffs = "; ".join(f"{d.path}: Oracle {d.source_value!r}, PostgreSQL {d.target_value!r}" for d in outcome.diffs[:4])
    return out(FailureClass.TRANSLATION_DEFECT, diffs or "results differ")

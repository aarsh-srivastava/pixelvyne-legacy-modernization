"""Metamorphic relations: results of several generated cases that must relate (step 7c).

A relation is checked on each engine separately, over the results of its member cases
(``GenerationInfo.relation`` names the group). The discipline (Aarsh, 16 Nov):

* every relation states its **preconditions** in words and checks what it can of them in
  code; when a precondition does not hold, the relation is *not applicable*, never failed;
* a relation is **enforced only where Oracle satisfies it** in the same run (or recording).
  If Oracle does not, that is reported (the relation, or our reading of Oracle, is wrong)
  and nothing is concluded about PostgreSQL;
* a PostgreSQL violation of a relation Oracle satisfies is a translation defect (triage).

Relations only compare results of one engine with each other; they never compute an
expected value themselves.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from app.contracts.models import ExecutionResult, TestCase


class RelationVerdict(str, Enum):
    HOLDS = "holds"
    VIOLATED = "violated"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class Relation:
    prefix: str                        # GenerationInfo.relation starts with this
    statement: str
    preconditions: str
    check: Callable[[Sequence[TestCase], Mapping[str, ExecutionResult]], tuple[RelationVerdict, str]]


def _ok(results: Mapping[str, ExecutionResult], cases: Sequence[TestCase]) -> bool:
    return all(c.test_id in results and results[c.test_id].error is None for c in cases)


def _out(result: ExecutionResult, name: str):
    return result.out_params.get(name)


def _cb_empty(cases, results):
    if len(cases) != 2 or not _ok(results, cases):
        return RelationVerdict.NOT_APPLICABLE, "both calls must exist and succeed"
    empty = next((c for c in cases if c.inputs.get("p_override") == ""), None)
    null = next((c for c in cases if c.inputs.get("p_override") is None), None)
    if empty is None or null is None or empty.inputs["p_emp_id"] != null.inputs["p_emp_id"]:
        return RelationVerdict.NOT_APPLICABLE, "needs the same employee with '' and NULL"
    a, b = _out(results[empty.test_id], "p_bonus"), _out(results[null.test_id], "p_bonus")
    same = a is not None and b is not None and Decimal(str(a)) == Decimal(str(b))
    return (RelationVerdict.HOLDS if same else RelationVerdict.VIOLATED), f"p_bonus('')={a}, p_bonus(NULL)={b}"


def _names(value) -> list[str]:
    return [] if value is None else [part for part in str(value).split(", ")]


def _te_prefix(cases, results):
    if not _ok(results, cases):
        return RelationVerdict.NOT_APPLICABLE, "every call must succeed"
    depts = {c.inputs.get("p_dept_id") for c in cases}
    ns = sorted(int(c.inputs["p_n"]) for c in cases)
    if len(depts) != 1 or any(c.fixture_rows for c in cases) or ns != list(range(ns[0], ns[0] + len(ns))):
        return RelationVerdict.NOT_APPLICABLE, "needs one department, seed data only, consecutive whole N"
    by_n = {int(c.inputs["p_n"]): _names(results[c.test_id].return_value) for c in cases}
    for n in ns[:-1]:
        shorter, longer = by_n[n], by_n[n + 1]
        if longer[:len(shorter)] != shorter or len(shorter) > n or len(longer) > n + 1:
            return RelationVerdict.VIOLATED, f"top_earners(n={n}) = {shorter} is not a prefix of n={n + 1}: {longer}"
    return RelationVerdict.HOLDS, f"prefix chain over n={ns[0]}..{ns[-1]}"


def _np_symmetry(cases, results):
    if len(cases) != 2 or not _ok(results, cases):
        return RelationVerdict.NOT_APPLICABLE, "both calls must exist and succeed"
    pos = next((c for c in cases if Decimal(str(c.inputs["p_gross"])) > 0), None)
    neg = next((c for c in cases if Decimal(str(c.inputs["p_gross"])) < 0), None)
    if pos is None or neg is None or Decimal(str(pos.inputs["p_gross"])) != -Decimal(str(neg.inputs["p_gross"])):
        return RelationVerdict.NOT_APPLICABLE, "needs x and -x"
    a, b = results[pos.test_id].return_value, results[neg.test_id].return_value
    same = a is not None and b is not None and Decimal(str(a)) == -Decimal(str(b))
    return (RelationVerdict.HOLDS if same else RelationVerdict.VIOLATED), f"net_pay(x)={a}, net_pay(-x)={b}"


def _ar_zero(cases, results):
    verdicts = []
    for c in cases:
        r = results.get(c.test_id)
        if r is None or r.error is not None or c.inputs.get("p_pct") != 0:
            return RelationVerdict.NOT_APPLICABLE, "every 0% call must exist and succeed"
        audit = [d for d in r.table_diffs if d.table == "salary_audit" and (d.inserted or d.deleted or d.updated)]
        salary = [d for d in r.table_diffs if d.table == "employees" and (d.inserted or d.deleted or d.updated)]
        verdicts.append((c.inputs["p_emp_id"], bool(audit), bool(salary)))
    bad = [v for v in verdicts if v[1] or v[2]]
    if bad:
        return RelationVerdict.VIOLATED, f"a 0% raise changed data (emp, audit row, salary changed): {bad}"
    return RelationVerdict.HOLDS, f"no audit row and no salary change for {[v[0] for v in verdicts]}"


def _cm_same_month(cases, results):
    if not _ok(results, cases) or len({tuple(map(str, c.setup)) for c in cases}) != 1:
        return RelationVerdict.NOT_APPLICABLE, "every call must succeed after the same setup"
    months = {(c.inputs["p_pay_month"].year, c.inputs["p_pay_month"].month) for c in cases}
    if len(months) != 1:
        return RelationVerdict.NOT_APPLICABLE, "every date must be in the same month"
    totals = [_out(results[c.test_id], "p_total") for c in cases]
    same = None not in totals and len({Decimal(str(t)) for t in totals}) == 1
    return (RelationVerdict.HOLDS if same else RelationVerdict.VIOLATED), f"totals {totals}"


def _cm_double(cases, results):
    """close_month after two identical payroll runs = 2 x close_month after one (members: 1 run, 2 runs)."""
    if not _ok(results, cases) or len(cases) != 2:
        return RelationVerdict.NOT_APPLICABLE, "both calls must exist and succeed"
    once = next((c for c in cases if len(c.setup) == 1), None)
    twice = next((c for c in cases if len(c.setup) == 2), None)
    if once is None or twice is None or twice.setup[0] != twice.setup[1] or twice.setup[0] != once.setup[0] \
            or once.inputs != twice.inputs:
        return RelationVerdict.NOT_APPLICABLE, "needs the same month after one run and after the same run twice"
    a, b = _out(results[once.test_id], "p_total"), _out(results[twice.test_id], "p_total")
    same = a is not None and b is not None and 2 * Decimal(str(a)) == Decimal(str(b))
    return (RelationVerdict.HOLDS if same else RelationVerdict.VIOLATED), f"one run {a}, two runs {b}"


RELATIONS: tuple[Relation, ...] = (
    Relation("cb_empty_", "calc_bonus(x, '') = calc_bonus(x, NULL)",
             "same employee, both calls succeed; in Oracle '' is NULL (R1)", _cb_empty),
    Relation("te_prefix_", "top_earners(d, n) is a prefix of top_earners(d, n + 1)",
             "one department; seed data only; consecutive whole n >= 1; the ORDER BY is base_salary DESC, "
             "emp_id, and emp_id is the primary key, so the order is total and ties are broken the same way",
             _te_prefix),
    Relation("np_sym_", "net_pay(-x) = -net_pay(x)",
             "x and -x both non-NULL; ROUND is half away from zero, which is symmetric about 0", _np_symmetry),
    Relation("ar_zero", "apply_raise(x, 0) writes no audit row and leaves the salary unchanged",
             "base_salary is NUMBER(10,2), so ROUND(salary x 1, 2) = salary; the trigger's WHEN clause fires "
             "only when the value changes (EXPECTED.md section 7)", _ar_zero),
    Relation("cm_same_month", "close_month(d) is the same for every d in one month",
             "same setup before each call; pay_month = TRUNC(d, 'MM'), so the day and time do not matter",
             _cm_same_month),
    Relation("cm_double", "close_month(m) after the same payroll run twice = 2 x close_month(m) after it once",
             "identical setup runs (same date, same data); payroll_runs has no unique key, so both runs insert; "
             "net_pay is rounded per row, so the total doubles exactly", _cm_double),
)


@dataclass(frozen=True)
class RelationResult:
    group: str
    statement: str
    preconditions: str
    members: tuple[str, ...]
    oracle: RelationVerdict
    postgres: RelationVerdict | None
    oracle_detail: str
    postgres_detail: str | None

    @property
    def enforced(self) -> bool:
        """Only a relation Oracle satisfies is enforced on PostgreSQL."""
        return self.oracle is RelationVerdict.HOLDS

    @property
    def defect(self) -> bool:
        return self.enforced and self.postgres is RelationVerdict.VIOLATED


def relation_for(group: str) -> Relation:
    for relation in RELATIONS:
        if group.startswith(relation.prefix):
            return relation
    raise LookupError(f"no relation is defined for group {group!r}")


def groups(cases: Sequence[TestCase]) -> dict[str, list[TestCase]]:
    out: dict[str, list[TestCase]] = {}
    for case in cases:
        if case.generation and case.generation.relation:
            out.setdefault(case.generation.relation, []).append(case)
    # cm_double also needs the one-run member of cm_same_month on the same date
    if "cm_double" in out and "cm_same_month" in out:
        twice = out["cm_double"][0]
        once = [c for c in out["cm_same_month"] if c.inputs == twice.inputs]
        out["cm_double"] = once[:1] + out["cm_double"]
    return out


def evaluate(cases: Sequence[TestCase], oracle: Mapping[str, ExecutionResult],
             postgres: Mapping[str, ExecutionResult] | None) -> list[RelationResult]:
    """Every relation group, judged on Oracle, and on PostgreSQL only where Oracle holds."""
    results = []
    for group, members in sorted(groups(cases).items()):
        relation = relation_for(group)
        o_verdict, o_detail = relation.check(members, oracle)
        p_verdict = p_detail = None
        if postgres is not None:
            p_verdict, p_detail = relation.check(members, postgres)
        results.append(RelationResult(group, relation.statement, relation.preconditions,
                                      tuple(c.test_id for c in members), o_verdict, p_verdict, o_detail, p_detail))
    return results

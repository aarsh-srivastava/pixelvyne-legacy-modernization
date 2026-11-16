"""Migration report: one self-contained HTML page of evidence from repair runs.

Input is what the repair loop produces (RepairRun, one per object). For each object
the page shows the verdict, every attempt, exactly what was sent back for repair, and
the final per-test comparison of Oracle against PostgreSQL. Nothing is summarised
without the evidence beneath it.

    runs = [RepairRun.model_validate(r) for r in json.load(...)]
    html = render_report(runs, descriptions={"cb-01": "Happy path, rating A", ...})

The page has no external resources, so it opens offline and can be attached as-is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from typing import Any, Iterable

from app.contracts import CONTRACT_VERSION, ExecutionResult, TestStatus
from app.orchestration.repair_loop import AttemptRecord, RepairOutcome, RepairRun

STAND_IN_MARKER = "Scripted stand-in"

# JSON has no decimal type, so numbers read back from run_repair.py --json are strings.
_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")

OUTCOME_TEXT = {
    RepairOutcome.PASSED: "Equivalent",
    RepairOutcome.PASSED_NEEDS_REVIEW: "Equivalent, needs sign-off",
    RepairOutcome.NEEDS_HUMAN: "Needs a human",
}
OUTCOME_CLASS = {
    RepairOutcome.PASSED: "ok",
    RepairOutcome.PASSED_NEEDS_REVIEW: "warn",
    RepairOutcome.NEEDS_HUMAN: "bad",
}
TEST_TEXT = {TestStatus.PASSED: "Same", TestStatus.FAILED: "Different", TestStatus.ERROR: "Not judged"}
TEST_CLASS = {TestStatus.PASSED: "ok", TestStatus.FAILED: "bad", TestStatus.ERROR: "warn"}


@dataclass(frozen=True)
class ReportSummary:
    objects: int
    equivalent: int
    needs_review: int
    needs_human: int
    tests_total: int
    tests_same: int
    repairs: int                  # attempts after the first, across all objects
    repaired_objects: int         # equivalent only after at least one repair
    stand_in_converter: bool

    @classmethod
    def of(cls, runs: Iterable[RepairRun]) -> "ReportSummary":
        runs = list(runs)
        final_tests = [t for r in runs if r.final_validation for t in r.final_validation.tests]
        return cls(
            objects=len(runs),
            equivalent=sum(r.outcome == RepairOutcome.PASSED for r in runs),
            needs_review=sum(r.outcome == RepairOutcome.PASSED_NEEDS_REVIEW for r in runs),
            needs_human=sum(r.outcome == RepairOutcome.NEEDS_HUMAN for r in runs),
            tests_total=len(final_tests),
            tests_same=sum(t.status == TestStatus.PASSED for t in final_tests),
            repairs=sum(max(len(r.attempts) - 1, 0) for r in runs),
            repaired_objects=sum(r.outcome != RepairOutcome.NEEDS_HUMAN and len(r.attempts) > 1 for r in runs),
            stand_in_converter=any(_used_stand_in(r) for r in runs),
        )


def _used_stand_in(run: RepairRun) -> bool:
    conv = run.final_conversion
    return conv is not None and any(STAND_IN_MARKER in w for w in conv.warnings)


# ---------------------------------------------------------------- value formatting

def _val(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, str):
        return value if _NUMBER.match(value) else f"'{value}'"
    return str(value)


def _show(result: ExecutionResult) -> str:
    """What one engine did in one test, in a line a reader can check."""
    if result.error:
        return f"error {result.error.code}"
    parts = []
    if result.return_value is not None or not result.out_params:
        parts.append(_val(result.return_value))
    parts += [f"{k} = {_val(v)}" for k, v in result.out_params.items()]
    changed = [d.table for d in result.table_diffs]
    if changed:
        parts.append("changed " + ", ".join(changed))
    return "; ".join(parts)


def _attempt_source(a: AttemptRecord) -> str:
    return "Initial conversion" if a.attempt == 1 else f"Repair {a.attempt - 1}"


def _attempt_result(a: AttemptRecord) -> str:
    if not a.compiled_ok:
        return "Did not compile"
    total = a.passed + a.failed + a.errors
    text = f"{a.passed} of {total} tests the same"
    return text + (f", {a.errors} not judged" if a.errors else "")


# ---------------------------------------------------------------- sections

def _summary_html(s: ReportSummary) -> str:
    tiles = [
        ("Objects equivalent", f"{s.equivalent + s.needs_review} / {s.objects}",
         f"{s.needs_review} need sign-off" if s.needs_review else "all tests match Oracle"),
        ("Tests matching Oracle", f"{s.tests_same} / {s.tests_total}", "in the final version of each object"),
        ("Automatic repairs", str(s.repairs), f"{s.repaired_objects} objects fixed by the repair loop"),
        ("Needs a human", str(s.needs_human), "after 3 attempts, or tests could not be judged"),
    ]
    cells = "".join(
        f'<div class="tile"><span class="k">{escape(k)}</span><span class="v">{escape(v)}</span>'
        f'<span class="s">{escape(sub)}</span></div>'
        for k, v, sub in tiles
    )
    return f'<section class="tiles">{cells}</section>'


def _overview_html(runs: list[RepairRun]) -> str:
    rows = []
    for r in runs:
        first, last = r.attempts[0], r.attempts[-1]
        rows.append(
            f'<tr><td><a href="#{escape(_anchor(r))}"><code>{escape(r.object_id)}</code></a></td>'
            f"<td>{escape(_attempt_result(first))}</td>"
            f"<td class=num>{len(r.attempts)}</td>"
            f"<td>{escape(_attempt_result(last))}</td>"
            f'<td><span class="pill {OUTCOME_CLASS[r.outcome]}">{escape(OUTCOME_TEXT[r.outcome])}</span></td></tr>'
        )
    return (
        '<section><h2>Objects</h2><div class="scroll"><table>'
        "<thead><tr><th>Object</th><th>First attempt</th><th class=num>Attempts</th>"
        "<th>Final</th><th>Verdict</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div></section>"
    )


def _anchor(run: RepairRun) -> str:
    return "obj-" + "".join(ch if ch.isalnum() else "-" for ch in run.object_id)


def _attempts_html(run: RepairRun) -> str:
    items = []
    for a in run.attempts:
        status = "ok" if a.status and a.status.value == "passed" else ("bad" if a.compiled_ok else "warn")
        feedback = ""
        if a.compile_error:
            feedback = f'<pre class="fb">{escape(a.compile_error)}</pre>'
        elif a.feedback_summary:
            lines = a.feedback_summary.splitlines()[1:]
            feedback = (
                '<div class="fbwrap"><span class="fbk">Sent back to the converter</span>'
                f'<pre class="fb">{escape(chr(10).join(lines))}</pre></div>'
            )
        items.append(
            f'<li class="att {status}"><div class="att-h"><b>Attempt {a.attempt}</b>'
            f"<span>{escape(_attempt_source(a))} · {escape(a.tool.value)}</span>"
            f"<span class=res>{escape(_attempt_result(a))}</span></div>{feedback}</li>"
        )
    return f'<ol class="atts">{"".join(items)}</ol>'


def _evidence_html(run: RepairRun, descriptions: dict[str, str]) -> str:
    v = run.final_validation
    if v is None:
        return '<p class="muted">No tests ran: the final version did not compile.</p>'
    rows = []
    for t in v.tests:
        rows.append(
            f"<tr><td class=id><code>{escape(t.test_id)}</code></td>"
            f"<td>{escape(descriptions.get(t.test_id, ''))}</td>"
            f"<td><code>{escape(_show(t.source))}</code></td>"
            f"<td><code>{escape(_show(t.target))}</code></td>"
            f'<td><span class="pill {TEST_CLASS[t.status]}">{TEST_TEXT[t.status]}</span></td></tr>'
        )
    return (
        f'<h4>Final evidence, version {v.conversion_version}</h4><div class="scroll"><table>'
        "<thead><tr><th>Test</th><th>What it checks</th><th>Oracle</th><th>PostgreSQL</th><th>Result</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )


def _object_html(run: RepairRun, descriptions: dict[str, str]) -> str:
    code = ""
    if run.final_conversion and run.final_conversion.target_code:
        code = (
            "<details><summary>Final PostgreSQL code</summary>"
            f"<pre class=code>{escape(run.final_conversion.target_code)}</pre></details>"
        )
    return (
        f'<section class="obj" id="{escape(_anchor(run))}">'
        f'<div class="obj-h"><h3><code>{escape(run.object_id)}</code></h3>'
        f'<span class="pill {OUTCOME_CLASS[run.outcome]}">{escape(OUTCOME_TEXT[run.outcome])}</span></div>'
        f'<p class="muted">{escape(run.reason)}</p>'
        f"{_attempts_html(run)}{_evidence_html(run, descriptions)}{code}</section>"
    )


# ---------------------------------------------------------------- page

_CSS = """
:root{--bg:#f5f7f8;--surface:#fff;--ink:#16212b;--muted:#5b6874;--line:#d7dee3;--accent:#0e7a71;
--ok:#1d8148;--ok-t:#e2f2e7;--warn:#9a6410;--warn-t:#fbefd9;--bad:#b4400f;--bad-t:#fbe6dc;--code:#edf1f3}
@media (prefers-color-scheme:dark){:root{--bg:#0f1418;--surface:#161d22;--ink:#e2e9ed;--muted:#97a4ae;
--line:#2a343c;--accent:#3dbfb1;--ok:#4bbf78;--ok-t:#15301f;--warn:#dfa344;--warn-t:#33280f;--bad:#ef8650;
--bad-t:#3b1e11;--code:#1c252b;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.55 "Segoe UI",system-ui,-apple-system,sans-serif;padding:0 max(16px,env(safe-area-inset-left))}
main{max-width:1000px;margin:0 auto;padding:36px 0 64px;display:grid;gap:28px}
main>*,.obj,details,.atts,.att{min-width:0}td.id,td.id code{white-space:nowrap}
h1{font-size:1.9rem;margin:0;line-height:1.2}h2{font-size:1.2rem;margin:0 0 10px}
h3{font-size:1.02rem;margin:0}h4{font-size:.9rem;margin:16px 0 8px;color:var(--muted);font-weight:600}
code,pre{font-family:ui-monospace,Consolas,"Cascadia Mono",monospace;font-size:.85em}
.eyebrow{font:600 .74rem/1 ui-monospace,Consolas,monospace;letter-spacing:.08em;text-transform:uppercase;color:var(--accent)}
.muted{color:var(--muted);margin:6px 0 0}
.banner{background:var(--warn-t);border:1px solid var(--warn);border-radius:10px;padding:12px 14px}
.tiles{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px;display:grid;gap:4px}
.tile .k{font-size:.78rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.tile .v{font-size:1.7rem;font-weight:700;font-variant-numeric:tabular-nums}.tile .s{font-size:.82rem;color:var(--muted)}
.scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;background:var(--surface);
border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:9px 11px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:.78rem;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;font-weight:600}
tr:last-child td{border-bottom:0}.num{text-align:right;font-variant-numeric:tabular-nums}
td code{white-space:pre-wrap;word-break:break-word}a{color:var(--accent)}
.pill{display:inline-block;font-size:.75rem;font-weight:600;padding:3px 9px;border-radius:999px;white-space:nowrap}
.pill.ok{color:var(--ok);background:var(--ok-t)}.pill.warn{color:var(--warn);background:var(--warn-t)}
.pill.bad{color:var(--bad);background:var(--bad-t)}
.obj{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:18px}
.obj table{border-radius:8px}.obj-h{display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap}
.atts{list-style:none;margin:14px 0 0;padding:0;display:grid;gap:8px}
.att{border-left:3px solid var(--line);padding:4px 0 4px 12px}.att.ok{border-color:var(--ok)}
.att.bad{border-color:var(--bad)}.att.warn{border-color:var(--warn)}
.att-h{display:flex;gap:6px 14px;flex-wrap:wrap;align-items:baseline}.att-h span{color:var(--muted)}
.att-h .res{color:var(--ink)}.fbwrap{margin-top:6px}.fbk{font-size:.74rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
pre{margin:4px 0 0;background:var(--code);border-radius:8px;padding:10px 12px;overflow-x:auto;white-space:pre-wrap}
details{margin-top:14px}summary{cursor:pointer;font-weight:600}pre.code{white-space:pre}
footer{color:var(--muted);font-size:.85rem}
@media (max-width:720px){.tiles{grid-template-columns:repeat(2,minmax(0,1fr))}}
"""


def render_report(
    runs: list[RepairRun],
    *,
    descriptions: dict[str, str] | None = None,
    title: str = "PixelVyne migration report",
    source_label: str = "Oracle 23 (legacy) → PostgreSQL 16 (target)",
    generated_at: datetime | None = None,
) -> str:
    """The full HTML page for these runs."""
    if not runs:
        raise ValueError("No repair runs to report on")
    descriptions = descriptions or {}
    summary = ReportSummary.of(runs)
    when = (generated_at or datetime.now(timezone.utc)).strftime("%d %b %Y, %H:%M UTC")
    banner = ""
    if summary.stand_in_converter:
        banner = (
            '<div class="banner"><b>Converter: scripted stand-in.</b> The PostgreSQL versions were '
            "prepared by hand and replayed, not generated. The tests, both database runs, the "
            "comparisons and the repair loop are real.</div>"
        )
    objects = "".join(_object_html(r, descriptions) for r in runs)
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)}</title><style>{_CSS}</style></head><body><main>"
        f'<header><span class="eyebrow">{escape(source_label)} · generated {escape(when)}</span>'
        f"<h1>{escape(title)}</h1>"
        f'<p class="muted">Each object was converted, deployed to PostgreSQL and run against the same '
        f"tests as the Oracle original. Any difference was sent back to the converter, up to 3 attempts. "
        f"An object counts as equivalent only when every test gives the same result on both databases.</p>"
        f"</header>{banner}{_summary_html(summary)}{_overview_html(runs)}"
        f"<section><h2>Evidence per object</h2><div style='display:grid;gap:14px'>{objects}</div></section>"
        f"<footer>Compared per test: return value, OUT parameters, error category (e.g. ORA-01403 = P0002 = "
        f"no data found) and every row inserted, updated or deleted, including trigger effects. "
        f"Sequence ids and SYSDATE columns are ignored. Contract {escape(CONTRACT_VERSION)}.</footer>"
        f"</main></body></html>"
    )

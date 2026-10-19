"""
vomeos/evaluate.py

Replay an agent's answer key and report whether it still agrees with a human.

WHY THIS EXISTS
---------------
`AUTOMATION_ROADMAP.md` puts this first on the list, and the reason it gives is
exact: three classifiers are in production making fuzzy judgment calls and none
of them has ever been scored against a human. Continuous learning is a slogan
until there is a number that can go down.

The harness is deliberately small. A case is a context dict and the answer a
person gave. Running the agent on that context and comparing is the whole idea.
What makes it useful is not sophistication, it is that it exists and runs on a
schedule, so a prompt edit that quietly makes an agent worse shows up as a
number instead of as a complaint three weeks later.

    py -m vomeos.evaluate support.education_reviewer

Cost note: this makes one model call per case. Ten cases is cents. Do not put
it on a frequent cron.

WHAT COUNTS AS AGREEMENT
------------------------
Every key in the case's `expect` object must match the agent's output for that
key. Keys the agent returns that `expect` does not mention are ignored, which
is what lets an answer key pin the decision ("recommendation") without also
pinning the prose ("reason"). Comparison is case insensitive on strings after
stripping, because "Send" and "send" are the same decision.

TEXT AGENTS
-----------
An agent whose output is the message itself (`output = "text"`) has no keys
to compare, and wrapping an email in JSON would put braces in front of the
guards that are meant to read exactly what the customer reads. So a text
agent's `expect` uses four checks instead:

    status        "ok" or "blocked": did the draft pass its guards
    contains      phrases that must appear (case insensitive)
    not_contains  phrases that must not
    max_words     the body may not run longer than this

That pins what can be checked (the decision, the must-say, the must-never-say,
the length) and leaves the prose to the person who approves it.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from vomeos.manifest import AgentManifest
from vomeos.registry import get_agent


@dataclass
class CaseOutcome:
    case_id: str
    agreed: bool
    expected: dict
    actual: dict | None
    status: str
    note: str = ""
    mismatches: list[str] = field(default_factory=list)


@dataclass
class Report:
    agent: str
    outcomes: list[CaseOutcome]
    min_agreement: float

    @property
    def scored(self) -> int:
        return len(self.outcomes)

    @property
    def agreed(self) -> int:
        return sum(1 for o in self.outcomes if o.agreed)

    @property
    def agreement(self) -> float:
        return (self.agreed / self.scored) if self.scored else 0.0

    @property
    def passed(self) -> bool:
        return self.scored > 0 and self.agreement >= self.min_agreement

    def render(self) -> str:
        lines = [f"{self.agent}", ""]
        for outcome in self.outcomes:
            mark = "ok  " if outcome.agreed else "MISS"
            lines.append(f"  [{mark}] {outcome.case_id}")
            if not outcome.agreed:
                for mismatch in outcome.mismatches:
                    lines.append(f"         {mismatch}")
                if outcome.note:
                    lines.append(f"         key says: {outcome.note}")
                if outcome.status != "ok":
                    lines.append(f"         run status: {outcome.status}")
        pct = round(self.agreement * 100)
        need = round(self.min_agreement * 100)
        verdict = "PASS" if self.passed else "FAIL"
        lines += [
            "",
            f"  {self.agreed}/{self.scored} agreed ({pct}%), "
            f"threshold {need}%  ->  {verdict}",
        ]
        return "\n".join(lines)


def load_cases(manifest: AgentManifest) -> list[dict]:
    path: Path = manifest.evals_path
    if not path.exists():
        raise FileNotFoundError(f"no answer key at {path}")
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            cases.append(json.loads(line))
    return cases


def _same(expected, actual) -> bool:
    if isinstance(expected, str) and isinstance(actual, str):
        return expected.strip().lower() == actual.strip().lower()
    return expected == actual


def compare(expected: dict, actual: dict | None) -> list[str]:
    """Which expected keys did not match. Empty list means agreement."""
    if actual is None:
        return ["agent produced no usable output"]
    problems = []
    for key, want in expected.items():
        # "code_check.verdict" reads a nested field; "items.0.verdict" reads
        # into a list.
        got = actual
        for part in key.split("."):
            if isinstance(got, dict):
                got = got.get(part)
            elif isinstance(got, list) and part.isdigit():
                got = got[int(part)] if int(part) < len(got) else None
            else:
                got = None
        if not _same(want, got):
            problems.append(f"{key}: expected {want!r}, got {got!r}")
    return problems


def compare_text(expected: dict, text: str, status: str) -> list[str]:
    """The four text checks. Empty list means agreement."""
    problems = []
    want_status = expected.get("status")
    if want_status and str(want_status).lower() != status:
        problems.append(f"status: expected {want_status!r}, got {status!r}")
    lowered = (text or "").lower()
    for phrase in expected.get("contains") or []:
        if str(phrase).lower() not in lowered:
            problems.append(f"missing {phrase!r}")
    for phrase in expected.get("not_contains") or []:
        if str(phrase).lower() in lowered:
            problems.append(f"must not say {phrase!r}")
    limit = expected.get("max_words")
    if limit and len((text or "").split()) > int(limit):
        problems.append(
            f"{len(text.split())} words, over the {limit} word limit"
        )
    return problems


def evaluate(qualified: str, limit: int = 0) -> Report:
    """Run every case in the agent's answer key. Makes real model calls."""
    from vomeos.runner import run_agent  # local import: needs an API key

    manifest = get_agent(qualified)
    cases = load_cases(manifest)
    if limit:
        cases = cases[:limit]

    outcomes: list[CaseOutcome] = []
    for index, case in enumerate(cases, start=1):
        case_id = str(case.get("id") or f"case-{index}")
        expected = case.get("expect") or {}
        result = run_agent(
            qualified,
            context=case.get("context") or {},
            subject_type="eval",
            subject_id=case_id,
            # Facts the guards need that are not the agent's input, such as
            # the shipped task behind a resolution reply.
            guard_context=case.get("guard_context"),
        )
        if manifest.job.output == "text":
            actual = {"text": result.text}
            mismatches = compare_text(expected, result.text, result.status)
        else:
            actual = result.data if result.ok else None
            mismatches = compare(expected, actual)
        outcomes.append(CaseOutcome(
            case_id=case_id,
            agreed=not mismatches,
            expected=expected,
            actual=actual,
            status=result.status,
            note=str(case.get("note") or ""),
            mismatches=mismatches,
        ))

    return Report(
        agent=qualified,
        outcomes=outcomes,
        min_agreement=manifest.evals.min_agreement,
    )


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        print("usage: py -m vomeos.evaluate <division.agent> [limit]")
        return 2
    report = evaluate(args[0], limit=int(args[1]) if len(args) > 1 else 0)
    print(report.render())
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

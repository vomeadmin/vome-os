"""
vomeos/onboarding.py

The hiring standard, as a check rather than as a person.

WHY THIS EXISTS
---------------
The plan calls for a Director of HR who makes sure onboarding is smooth and
every new agent learns the same processes. The mistake would be to build that
as an agent first. An agent that reviews other agents is only as good as the
standard it applies, so the standard comes first and it comes as code.

This module is that standard. An agent is not fit to be scheduled until:

  * its manifest parses and its address matches where it lives on disk;
  * it belongs to a division that has a handbook, so it inherits something;
  * it has a charter, and the charter is under the word cap, because a job
    description that runs to 3000 words is not a job description;
  * every skill it claims exists in the central registry;
  * its model tier is real;
  * if it can talk to a client, it declares the client_message guard;
  * if it returns JSON, it declares the json_shape guard;
  * it has an answer key with enough cases to mean something;
  * it names where to escalate when it is unsure.

`check_agent()` returns findings rather than raising, so one run reports every
problem with a new hire instead of one problem per run. `main()` in the CLI
exits non-zero if anything is blocking, which is how this becomes a required
check before an agent can be merged.

Nothing here calls a model. The gate must be runnable in CI, offline, in under
a second, by someone who has not set an API key.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from vomeos import config, tiers
from vomeos.composer import division_handbook_path
from vomeos.guards import known_guards
from vomeos.manifest import (
    CHARTER_WORD_CAP,
    VALID_OUTPUTS,
    AgentManifest,
    ManifestError,
    load_manifest,
)
from vomeos.registry import (
    _load_skills,
    agent_directories,
    find_similar_skills,
    list_skills,
    skill_users,
)

# An answer key smaller than this cannot distinguish a working classifier from
# a lucky one. Ten is the number the support roadmap already settled on for the
# KB gap clusterer ("do not treat the topic list as roadmap input until the
# clusterer agrees with a human on 8 of 10").
MIN_EVAL_CASES = config.MIN_EVAL_CASES

BLOCKING = "blocking"
ADVISORY = "advisory"


@dataclass(frozen=True)
class Finding:
    agent: str
    severity: str
    message: str

    def __str__(self) -> str:
        mark = "FAIL" if self.severity == BLOCKING else "warn"
        return f"  [{mark}] {self.agent}: {self.message}"


def _word_count(text: str) -> int:
    return len(text.split())


def check_agent(manifest: AgentManifest) -> list[Finding]:
    """Every reason this agent is or is not ready to be scheduled."""
    out: list[Finding] = []
    who = manifest.qualified

    def fail(msg: str) -> None:
        out.append(Finding(who, BLOCKING, msg))

    def warn(msg: str) -> None:
        out.append(Finding(who, ADVISORY, msg))

    # It must inherit something. An agent in a division with no handbook is
    # an agent that only knows its own charter.
    handbook = division_handbook_path(manifest.identity.division)
    if not handbook.exists():
        fail(
            f"division {manifest.identity.division!r} has no handbook at "
            f"{handbook.relative_to(handbook.parents[2])}"
        )

    # Charter.
    if not manifest.charter_path.exists():
        fail(f"charter missing at {manifest.job.charter}")
    else:
        words = _word_count(manifest.charter_text())
        if words > CHARTER_WORD_CAP:
            fail(
                f"charter is {words} words, over the {CHARTER_WORD_CAP} cap; "
                "move shared procedure into a skill"
            )
        elif words > CHARTER_WORD_CAP * 0.85:
            warn(
                f"charter is {words} words, close to the "
                f"{CHARTER_WORD_CAP} cap"
            )
        elif words < 40:
            warn(f"charter is only {words} words; is the job fully described?")

    # Skills must exist centrally. This is the rule that stops every agent
    # author from writing their own copy of a shared procedure.
    skills = _load_skills()
    for name in manifest.job.skills:
        if name not in skills:
            fail(
                f"claims skill {name!r} which is not in skills/; add it there "
                "so other agents can inherit it"
            )

    # Model tier.
    try:
        tiers.resolve(manifest.model.tier)
    except tiers.UnknownTier as exc:
        fail(str(exc))
    if manifest.model.tier == "senior" and not manifest.approval.escalate_to:
        warn("senior tier without an escalation channel")

    # Output contract.
    if manifest.job.output not in VALID_OUTPUTS:
        fail(
            f"job.output is {manifest.job.output!r}; expected one of "
            f"{', '.join(VALID_OUTPUTS)}"
        )

    # Guards. These two rules are the whole point of the gate.
    declared = set(manifest.guards.output)
    unknown = declared - set(known_guards())
    for name in sorted(unknown):
        fail(f"declares unknown guard {name!r}")

    if manifest.guards.client_facing and "client_message" not in declared:
        fail(
            "is client_facing but does not declare the client_message guard; "
            "this is the check that exists because of ticket #8945"
        )
    if manifest.job.output == "json" and "json_shape" not in declared:
        fail("returns JSON but does not declare the json_shape guard")
    if not declared:
        warn("declares no output guards at all")

    # Escalation. An agent with nowhere to go when unsure will guess.
    if not manifest.approval.escalate_to:
        warn("no approval.escalate_to; there is nowhere to send a hard case")

    # Client-facing work is outward facing, which is where the CEO approval
    # flag is supposed to live. Flag the absence loudly rather than assuming.
    if (manifest.guards.client_facing
            and not manifest.approval.requires_ceo_approval):
        warn(
            "is client_facing with requires_ceo_approval = false; confirm "
            "that is deliberate"
        )

    # Context contract.
    if not manifest.job.requires_context:
        warn(
            "declares no requires_context; callers can then pass nothing and "
            "the agent will answer about an empty thread"
        )

    # The answer key.
    out.extend(_check_evals(manifest))

    return out


def _check_evals(manifest: AgentManifest) -> list[Finding]:
    who = manifest.qualified
    path = manifest.evals_path
    if not path.exists():
        return [Finding(
            who, BLOCKING,
            f"no answer key at {manifest.evals.path}; an agent that has never "
            "been scored against a human is not ready to be scheduled",
        )]

    cases: list[dict] = []
    bad_lines: list[int] = []
    for number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            case = json.loads(line)
        except ValueError:
            bad_lines.append(number)
            continue
        if isinstance(case, dict):
            cases.append(case)
        else:
            bad_lines.append(number)

    findings: list[Finding] = []
    if bad_lines:
        findings.append(Finding(
            who, BLOCKING,
            f"answer key has invalid JSON on line(s) "
            f"{', '.join(str(n) for n in bad_lines)}",
        ))
    if len(cases) < MIN_EVAL_CASES:
        findings.append(Finding(
            who, BLOCKING,
            f"answer key has {len(cases)} cases, needs at least "
            f"{MIN_EVAL_CASES}",
        ))

    # Each case needs the inputs the agent says it requires, plus the expected
    # answer. Otherwise the harness cannot replay it.
    required = set(manifest.job.requires_context)
    for index, case in enumerate(cases, start=1):
        context = case.get("context")
        if not isinstance(context, dict):
            findings.append(Finding(
                who, BLOCKING, f"answer key case {index} has no context object"
            ))
            continue
        missing = required - set(context)
        if missing:
            findings.append(Finding(
                who, BLOCKING,
                f"answer key case {index} is missing context "
                f"{', '.join(sorted(missing))}",
            ))
        if "expect" not in case:
            findings.append(Finding(
                who, BLOCKING,
                f"answer key case {index} has no 'expect' (the human answer)",
            ))
    return findings


def check_skills() -> list[Finding]:
    """Registry-wide hygiene. This is the chief-of-staff pass.

    Two questions: does every skill say what it teaches, and do any two skills
    say the same thing. The second is the one that keeps the registry from
    turning into nine versions of "how to read a ticket thread".
    """
    findings: list[Finding] = []
    skills = list_skills()

    for skill in skills:
        who = f"skills/{skill.name}"
        if not skill.description:
            findings.append(Finding(
                who, BLOCKING,
                "no description in frontmatter; the duplicate check compares "
                "descriptions, so a skill without one is invisible to it",
            ))
        if not skill.owner:
            findings.append(Finding(who, ADVISORY, "no owner in frontmatter"))
        if not skill.body.strip():
            findings.append(Finding(who, BLOCKING, "skill body is empty"))
        if not skill_users(skill.name):
            findings.append(Finding(
                who, ADVISORY,
                "no agent declares this skill; delete it or use it",
            ))

    seen: set[frozenset[str]] = set()
    for skill in skills:
        if not skill.description:
            continue
        for other, score in find_similar_skills(
            skill.description, exclude=skill.name
        ):
            pair = frozenset({skill.name, other})
            if pair in seen:
                continue
            seen.add(pair)
            findings.append(Finding(
                f"skills/{skill.name}", ADVISORY,
                f"looks {int(score * 100)}% similar to {other!r}; confirm "
                "these are genuinely different procedures before both ship",
            ))
    return findings


def check_all() -> tuple[list[Finding], int]:
    """Validate every agent and the skill registry.

    Returns (findings, blocking_count). The CLI turns a non-zero blocking
    count into a non-zero exit code.
    """
    findings: list[Finding] = []
    for directory in agent_directories():
        try:
            manifest = load_manifest(directory)
        except ManifestError as exc:
            findings.append(Finding(
                str(Path(directory).name), BLOCKING, str(exc)
            ))
            continue
        findings.extend(check_agent(manifest))

    findings.extend(check_skills())
    blocking = sum(1 for f in findings if f.severity == BLOCKING)
    return findings, blocking

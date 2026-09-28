"""
vomeos/composer.py

Builds the system prompt every agent runs under, from four layers.

WHY THIS EXISTS
---------------
Today `system_prompt.md` (31KB) is read by agent.py alone, `intake_prompt.md`
(46KB) by intake.py alone, and `context.md` by field_feedback.py alone. A new
handler inherits nothing, so it either re-states the company rules in its own
words or silently does without them. Both are how a fleet of agents stops
agreeing with itself.

The four layers, outermost first:

  1. org/handbook.md          Every agent, every division. The hard rules.
  2. org/divisions/<div>.md   What this division does and does not do.
  3. skills/<name>.md         Shared procedures the manifest opted into.
  4. charter.md               This agent's own job. Capped at 600 words.

Order matters and is deliberate: the company's rules are stated before the
agent's job, so a charter that conflicts with the handbook loses. The output
contract is appended last because it is the thing the model must obey most
literally, and last is where that lands hardest.

Context is NOT part of this. Instructions live in files that a human reviews
and versions; data is passed by the caller at run time. Keeping that line
sharp is what stops charters from turning back into f-strings in Python.
"""

from __future__ import annotations

from pathlib import Path

from vomeos.manifest import ORG_DIR, AgentManifest
from vomeos.registry import get_skill

HANDBOOK_PATH = ORG_DIR / "handbook.md"
DIVISIONS_DIR = ORG_DIR / "divisions"

_JSON_CONTRACT = (
    "## Output contract\n\n"
    "Return valid JSON only. No prose before or after it, no markdown code "
    "fences, no explanation of what you returned. If you cannot produce the "
    "requested shape, return the shape with your best values and say why in "
    "the field the charter provides for it."
)

_TEXT_CONTRACT = (
    "## Output contract\n\n"
    "Return the requested text only. No preamble, no labels, no commentary "
    "about the task, no markdown code fences."
)


class CompositionError(RuntimeError):
    """A layer the manifest depends on is missing from disk."""


def division_handbook_path(division: str) -> Path:
    return DIVISIONS_DIR / f"{division}.md"


def _read(path: Path, what: str) -> str:
    if not path.exists():
        raise CompositionError(f"{what} not found at {path}")
    return path.read_text(encoding="utf-8").strip()


def compose_system_prompt(manifest: AgentManifest) -> str:
    """The full system prompt for one agent.

    Deterministic: same files in, same string out. That is what makes a run
    reproducible from its trace, and what lets the eval harness replay a case
    against the exact prompt that produced the original answer.
    """
    parts: list[str] = [
        _read(HANDBOOK_PATH, "org handbook"),
        _read(
            division_handbook_path(manifest.identity.division),
            f"{manifest.identity.division} division handbook",
        ),
    ]

    if manifest.job.skills:
        rendered = [get_skill(name).render() for name in manifest.job.skills]
        parts.append("## Shared skills\n\n" + "\n\n".join(rendered))

    title = manifest.identity.title or manifest.identity.name
    parts.append(
        f"## Your job: {title}\n\n{manifest.charter_text()}"
    )

    parts.append(
        _JSON_CONTRACT if manifest.job.output == "json" else _TEXT_CONTRACT
    )

    return "\n\n---\n\n".join(parts)


def render_context(
    manifest: AgentManifest, context: dict[str, object]
) -> str:
    """Turn the caller's data into the user message.

    Every key the manifest declares in `requires_context` must be present.
    A missing key is an error rather than an empty section, because an agent
    quietly reasoning about a thread it was never given is the failure mode
    that is hardest to see in the output.
    """
    missing = [
        key for key in manifest.job.requires_context if key not in context
    ]
    if missing:
        raise CompositionError(
            f"{manifest.qualified}: missing required context "
            f"{', '.join(sorted(missing))}"
        )

    # Declared keys first, in manifest order, so the prompt an agent sees is
    # stable no matter what order the caller built its dict in. Extras follow,
    # sorted, so an unplanned addition is visible rather than buried.
    declared = list(manifest.job.requires_context)
    extras = sorted(k for k in context if k not in declared)

    blocks: list[str] = []
    for key in declared + extras:
        value = context[key]
        if value is None or value == "":
            value = "(none provided)"
        label = key.replace("_", " ").strip().capitalize()
        blocks.append(f"## {label}\n\n{value}")

    return "\n\n".join(blocks)

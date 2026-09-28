"""
vomeos/registry.py

Discovery of agents and skills, plus the duplicate check.

WHY THIS EXISTS
---------------
Two problems, one module.

First, agents need to be findable by name from anywhere: a Celery task, an
endpoint, the CLI, a report. `get_agent("support.duplicate_reply_check")` reads
the manifest off disk and caches it.

Second, and this is the part that keeps the system from rotting: before a new
skill is added, something has to ask whether we already have it. A system that
lets every agent author write their own "how to read a ticket thread" ends up
with nine of them that disagree. `find_similar_skills()` is the deterministic
first pass for that check. It is intentionally boring (token overlap, no model
call, no embeddings) because it runs on every validate and a check that is slow
or costs money gets switched off.

Skills are markdown with a small TOML-ish frontmatter block:

    ---
    name: reading-a-ticket-thread
    description: How to read a Zoho conversation thread and tell who said what.
    owner: support
    ---

The description is what the duplicate check compares, so it carries weight:
write it as the thing the skill teaches, not as a title.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from vomeos.manifest import (
    AGENTS_DIR,
    MANIFEST_FILENAME,
    SKILLS_DIR,
    AgentManifest,
    ManifestError,
    load_manifest,
)

# Words too common to carry signal in a skill description. Kept short on
# purpose: an over-eager stoplist makes every skill look distinct.
_STOPWORDS = frozenset(
    """
    a an and are as at be by for from how in into is it its of on or that the
    to use used using what when where which who with you your this these those
    """.split()
)


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    owner: str
    path: Path
    body: str

    def render(self) -> str:
        """The form a skill takes inside a composed system prompt."""
        return f"### Skill: {self.name}\n\n{self.body.strip()}"


class UnknownAgent(KeyError):
    """No agent directory matched the requested address."""


class UnknownSkill(KeyError):
    """A manifest referenced a skill that is not in skills/."""


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

def agent_directories() -> list[Path]:
    """Every directory under agents/ that holds a manifest."""
    if not AGENTS_DIR.exists():
        return []
    return sorted(
        p.parent for p in AGENTS_DIR.glob(f"*/*/{MANIFEST_FILENAME}")
    )


@lru_cache(maxsize=None)
def _load_all() -> dict[str, AgentManifest]:
    found: dict[str, AgentManifest] = {}
    for directory in agent_directories():
        manifest = load_manifest(directory)
        # The address must match where the agent lives on disk, or a rename
        # leaves two ways to refer to one agent and they drift apart.
        expected = f"{directory.parent.name}.{directory.name}"
        if manifest.qualified != expected:
            raise ManifestError(
                f"{directory}: [identity] says {manifest.qualified!r} but the "
                f"directory says {expected!r}; they must agree"
            )
        found[manifest.qualified] = manifest
    return found


def list_agents() -> list[AgentManifest]:
    return sorted(_load_all().values(), key=lambda m: m.qualified)


def get_agent(qualified: str) -> AgentManifest:
    """Look up "division.name". Raises UnknownAgent with the near misses."""
    agents = _load_all()
    if qualified in agents:
        return agents[qualified]
    known = ", ".join(sorted(agents)) or "(none)"
    raise UnknownAgent(f"no agent {qualified!r}; known agents: {known}")


def divisions() -> list[str]:
    return sorted({m.identity.division for m in _load_all().values()})


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def _parse_skill(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    meta: dict[str, str] = {}
    body = text
    if match:
        for line in match.group(1).splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            meta[key.strip().lower()] = value.strip()
        body = text[match.end():]
    return Skill(
        name=meta.get("name") or path.stem,
        description=meta.get("description", ""),
        owner=meta.get("owner", ""),
        path=path,
        body=body,
    )


@lru_cache(maxsize=None)
def _load_skills() -> dict[str, Skill]:
    if not SKILLS_DIR.exists():
        return {}
    return {s.name: s for s in (_parse_skill(p) for p in sorted(
        SKILLS_DIR.glob("*.md")
    ))}


def list_skills() -> list[Skill]:
    return sorted(_load_skills().values(), key=lambda s: s.name)


def get_skill(name: str) -> Skill:
    skills = _load_skills()
    if name not in skills:
        known = ", ".join(sorted(skills)) or "(none)"
        raise UnknownSkill(f"no skill {name!r}; known skills: {known}")
    return skills[name]


def skill_users(name: str) -> list[str]:
    """Which agents declare this skill.

    Check this before changing or removing one.
    """
    return sorted(
        m.qualified for m in _load_all().values() if name in m.job.skills
    )


# ---------------------------------------------------------------------------
# The duplicate check
# ---------------------------------------------------------------------------

def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z][a-z0-9-]+", (text or "").lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def similarity(left: str, right: str) -> float:
    """Jaccard overlap of the meaningful words in two descriptions.

    Deterministic and cheap on purpose. This is a prompt to look, not a
    verdict: the reviewer decides, the number only decides what gets read.
    """
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def find_similar_skills(
    description: str, threshold: float = 0.4, exclude: str = ""
) -> list[tuple[str, float]]:
    """Existing skills that may already cover `description`.

    Returned highest overlap first. An empty list is the answer "nothing in
    the registry looks like this", which is what lets a new skill through.
    """
    scored = [
        (skill.name, similarity(description, skill.description))
        for skill in _load_skills().values()
        if skill.name != exclude
    ]
    hits = [(name, round(score, 3)) for name, score in scored
            if score >= threshold]
    return sorted(hits, key=lambda pair: pair[1], reverse=True)


def reset_cache() -> None:
    """Drop the discovery caches. For tests and for the CLI after an edit."""
    _load_all.cache_clear()
    _load_skills.cache_clear()

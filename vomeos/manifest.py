"""
vomeos/manifest.py

An agent is a manifest, not a module.

WHY THIS EXISTS
---------------
Before this, "hiring an agent" meant writing a 20-40KB Python handler with its
own `anthropic.Anthropic()`, its own hardcoded model ID, its own prompt built
from inline f-strings, and no retry, no trace, no evaluation. There are ~25
such client instantiations and ~30 `messages.create` call sites in this repo.
Nothing is shared, so every new agent re-decides every question and the answers
drift.

A manifest moves those decisions into data: which division the agent belongs
to, which model tier it may use, which skills it inherits, which guards run on
its output, whether it may talk to a client, and who approves it. The runtime
reads the manifest and does the rest identically for every agent.

The format is TOML read with stdlib `tomllib`. No new dependency, and it is
readable by someone who does not write Python, which matters because charters
and manifests are meant to be reviewed the way a job description is reviewed.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from vomeos import config

# Where the OS keeps its content. Resolved by config so the OS can be
# installed anywhere and still find its handbooks and staff.
ROOT = config.HOME
AGENTS_DIR = config.AGENTS_DIR
ORG_DIR = config.ORG_DIR
SKILLS_DIR = config.SKILLS_DIR

MANIFEST_FILENAME = "agent.toml"

# Charters are capped so an agent's job description stays a job description.
# The cap is enforced by the onboarding gate, not here, so a validator can
# report every finding at once instead of raising on the first one.
CHARTER_WORD_CAP = config.CHARTER_WORD_CAP

VALID_TIERS = config.VALID_TIERS
VALID_OUTPUTS = ("json", "text")


class ManifestError(ValueError):
    """A manifest could not be read or is missing something structural."""


@dataclass(frozen=True)
class Identity:
    name: str
    division: str
    title: str = ""
    reports_to: str = ""
    hired: str = ""

    @property
    def qualified(self) -> str:
        """The address used everywhere: "support.education_reviewer"."""
        return f"{self.division}.{self.name}"


@dataclass(frozen=True)
class ModelSpec:
    tier: str = "standard"
    max_tokens: int = 1024
    temperature: float | None = None


@dataclass(frozen=True)
class JobSpec:
    charter: str = "charter.md"
    skills: tuple[str, ...] = ()
    requires_context: tuple[str, ...] = ()
    output: str = "text"


@dataclass(frozen=True)
class GuardSpec:
    output: tuple[str, ...] = ()
    client_facing: bool = False


@dataclass(frozen=True)
class LimitSpec:
    timeout_seconds: int = 120
    max_retries: int = 2


@dataclass(frozen=True)
class ApprovalSpec:
    requires_ceo_approval: bool = False
    escalate_to: str = ""


@dataclass(frozen=True)
class EvalSpec:
    path: str = "evals/cases.jsonl"
    min_agreement: float = 0.8


@dataclass(frozen=True)
class AgentManifest:
    identity: Identity
    model: ModelSpec
    job: JobSpec
    guards: GuardSpec
    limits: LimitSpec
    approval: ApprovalSpec
    evals: EvalSpec
    directory: Path = field(compare=False, default=ROOT)

    @property
    def qualified(self) -> str:
        return self.identity.qualified

    @property
    def charter_path(self) -> Path:
        return self.directory / self.job.charter

    @property
    def evals_path(self) -> Path:
        return self.directory / self.evals.path

    def charter_text(self) -> str:
        path = self.charter_path
        if not path.exists():
            raise ManifestError(
                f"{self.qualified}: charter not found at {path}"
            )
        return path.read_text(encoding="utf-8").strip()


def _table(raw: dict, key: str) -> dict:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ManifestError(f"[{key}] must be a table")
    return value


def _tuple(value, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(
        not isinstance(v, str) for v in value
    ):
        raise ManifestError(f"{key} must be a list of strings")
    return tuple(value)


def parse_manifest(raw: dict, directory: Path) -> AgentManifest:
    """Turn a decoded TOML table into a manifest.

    Raises ManifestError only for damage that makes the manifest unusable
    (missing identity, wrong types). Everything that is merely *wrong* such as
    an unknown tier or a missing skill is left for the onboarding gate, which
    reports all findings together.
    """
    ident_raw = _table(raw, "identity")
    name = str(ident_raw.get("name") or "").strip()
    division = str(ident_raw.get("division") or "").strip()
    if not name or not division:
        raise ManifestError(
            "[identity] requires both name and division"
        )

    identity = Identity(
        name=name,
        division=division,
        title=str(ident_raw.get("title") or "").strip(),
        reports_to=str(ident_raw.get("reports_to") or "").strip(),
        hired=str(ident_raw.get("hired") or "").strip(),
    )

    model_raw = _table(raw, "model")
    temperature = model_raw.get("temperature")
    model = ModelSpec(
        tier=str(model_raw.get("tier") or "standard").strip(),
        max_tokens=int(model_raw.get("max_tokens") or 1024),
        temperature=(
            float(temperature) if temperature is not None else None
        ),
    )

    job_raw = _table(raw, "job")
    job = JobSpec(
        charter=str(job_raw.get("charter") or "charter.md"),
        skills=_tuple(job_raw.get("skills"), "job.skills"),
        requires_context=_tuple(
            job_raw.get("requires_context"), "job.requires_context"
        ),
        output=str(job_raw.get("output") or "text").strip(),
    )

    guards_raw = _table(raw, "guards")
    guards = GuardSpec(
        output=_tuple(guards_raw.get("output"), "guards.output"),
        client_facing=bool(guards_raw.get("client_facing", False)),
    )

    limits_raw = _table(raw, "limits")
    limits = LimitSpec(
        timeout_seconds=int(limits_raw.get("timeout_seconds") or 120),
        max_retries=int(limits_raw.get("max_retries") or 2),
    )

    approval_raw = _table(raw, "approval")
    approval = ApprovalSpec(
        requires_ceo_approval=bool(
            approval_raw.get("requires_ceo_approval", False)
        ),
        escalate_to=str(approval_raw.get("escalate_to") or "").strip(),
    )

    evals_raw = _table(raw, "evals")
    evals = EvalSpec(
        path=str(evals_raw.get("path") or "evals/cases.jsonl"),
        min_agreement=float(evals_raw.get("min_agreement") or 0.8),
    )

    return AgentManifest(
        identity=identity,
        model=model,
        job=job,
        guards=guards,
        limits=limits,
        approval=approval,
        evals=evals,
        directory=directory,
    )


def load_manifest(directory: Path) -> AgentManifest:
    """Read agents/<division>/<name>/agent.toml."""
    path = Path(directory) / MANIFEST_FILENAME
    if not path.exists():
        raise ManifestError(f"no {MANIFEST_FILENAME} in {directory}")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"{path}: invalid TOML ({exc})") from exc
    return parse_manifest(raw, Path(directory))

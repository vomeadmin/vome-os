"""
Vome OS

The company's operating system for agents. An agent is a manifest plus a
charter, not a Python module, and every agent in every division runs through
the same path: composed prompt, tiered model, retry, guards, trace.

    from vomeos import run_agent

    result = run_agent(
        "support.duplicate_reply_check",
        context={"draft": draft, "thread": thread_text},
        subject_type="zoho_ticket",
        subject_id=ticket_id,
    )
    if result.ok:
        verdict = result.get("duplicate")

INDEPENDENCE
------------
This package imports nothing from any application or any other Vome
repository. Its configuration, its database connection, its text handling and
its safety guards are all its own. Everything it needs from the rest of the
company it reaches over HTTP through `vomeos.integrations`.

That is not tidiness. It is what lets Vome OS be deployed, versioned and
scaled on its own, and what stops a refactor in another repository from taking
the agent fleet down with it.

The only permitted direction of dependency is inward: an application may
import Vome OS. Vome OS may not import an application.

LAYOUT
------
    vomeos/            the kernel (this package)
    org/               handbook and division handbooks
    skills/            shared procedures agents opt into
    agents/            the staff, one directory each

`org/`, `skills/` and `agents/` are found via VOMEOS_HOME, which defaults to
the directory containing this package.

See RUNTIME.md for how to hire an agent.

Imports of `runner` (Anthropic SDK) and `trace` (SQLAlchemy) are deferred so
that `py -m vomeos.cli validate` runs in CI with neither an API key nor a
database.
"""

from vomeos.manifest import AgentManifest, ManifestError
from vomeos.registry import (
    UnknownAgent,
    UnknownSkill,
    divisions,
    get_agent,
    get_skill,
    list_agents,
    list_skills,
)

__all__ = [
    "AgentManifest",
    "AgentResult",
    "ManifestError",
    "UnknownAgent",
    "UnknownSkill",
    "divisions",
    "get_agent",
    "get_skill",
    "list_agents",
    "list_skills",
    "run_agent",
]


def run_agent(*args, **kwargs):
    """Run one agent. See vomeos.runner.run_agent for the full signature."""
    from vomeos.runner import run_agent as _run

    return _run(*args, **kwargs)


def __getattr__(name):
    if name == "AgentResult":
        from vomeos.runner import AgentResult

        return AgentResult
    raise AttributeError(f"module 'vomeos' has no attribute {name!r}")

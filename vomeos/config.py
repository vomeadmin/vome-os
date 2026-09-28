"""
vomeos/config.py

Everything the OS reads from its environment, in one place.

WHY THIS EXISTS
---------------
Vome OS is its own system. It runs the company's agents; it is not a feature
of the support app, of django-core, or of anything else. That independence has
to be real at the import level or it is just a naming convention: the moment
the kernel imports a setting from an application module, the OS cannot be
deployed without that application and the boundary is gone.

So every value the kernel needs is read here, from `VOMEOS_*` environment
variables, with defaults that work out of the box. No kernel module reads
`os.environ` directly, and no kernel module imports an application's config.

MODEL TIERS
-----------
An agent manifest names a tier, never a model. Three of them:

    fast      lightweight classifiers where latency and cost beat reasoning
    standard  the default: classification, drafting, review
    senior    judgment calls, governance, anything expensive to unwind

Why a tier and not a model ID: model IDs go stale. A pinned snapshot was once
hardcoded at ~18 call sites in the support app, Anthropic retired it, and
intake and drafting started returning 404s in production. A tier is a
capability judgment an author can actually make, and upgrading the whole bench
is one edit here.
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Where the OS lives
# ---------------------------------------------------------------------------

# VOMEOS_HOME is the directory holding org/, skills/ and agents/. It defaults
# to the parent of this package, which is correct whether the OS sits in its
# own repository or alongside an application that embeds it.
HOME = Path(
    os.environ.get("VOMEOS_HOME") or Path(__file__).resolve().parent.parent
)

AGENTS_DIR = HOME / "agents"
ORG_DIR = HOME / "org"
SKILLS_DIR = HOME / "skills"

# ---------------------------------------------------------------------------
# The bench
# ---------------------------------------------------------------------------

MODEL_FAST = os.environ.get("VOMEOS_MODEL_FAST", "claude-haiku-4-5")
MODEL_STANDARD = os.environ.get("VOMEOS_MODEL_STANDARD", "claude-sonnet-5")
MODEL_SENIOR = os.environ.get("VOMEOS_MODEL_SENIOR", "claude-opus-5")

TIERS: dict[str, str] = {
    "fast": MODEL_FAST,
    "standard": MODEL_STANDARD,
    "senior": MODEL_SENIOR,
}

VALID_TIERS = tuple(TIERS)

# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

# The OS keeps its own state. VOMEOS_DATABASE_URL points at its database;
# DATABASE_URL is accepted as a fallback so an embedded deployment does not
# need a second Postgres on day one. The tables are namespaced either way, so
# sharing an instance never means sharing a schema with an application.
DATABASE_URL = (
    os.environ.get("VOMEOS_DATABASE_URL")
    or os.environ.get("DATABASE_URL", "")
)

# ---------------------------------------------------------------------------
# Authoring limits
# ---------------------------------------------------------------------------

# A charter is a job description. One that runs to 3000 words is a manual,
# and nobody reviews a manual.
CHARTER_WORD_CAP = int(os.environ.get("VOMEOS_CHARTER_WORD_CAP", "600"))

# An answer key smaller than this cannot tell a working classifier from a
# lucky one.
MIN_EVAL_CASES = int(os.environ.get("VOMEOS_MIN_EVAL_CASES", "10"))

# ---------------------------------------------------------------------------
# Outbound safety
# ---------------------------------------------------------------------------

# Names that must never appear in the body of a message leaving the company.
# The signature block is stripped before this is checked, so a message
# legitimately signed by a person does not trip it.
_DEFAULT_INTERNAL_NAMES = ("sam", "ron", "sanjay", "onlyg", "vic")

# The domain that appears in an outgoing signature block. Used to find where
# the body ends and the signature begins.
SIGNATURE_DOMAIN = os.environ.get(
    "VOMEOS_SIGNATURE_DOMAIN", "support.vomevolunteer.com"
)

# A message longer than this is not the brief note an unattended reply is
# meant to be, and is almost always the model writing an essay to itself.
OUTBOUND_MAX_CHARS = int(os.environ.get("VOMEOS_OUTBOUND_MAX_CHARS", "3500"))


def internal_names() -> tuple[str, ...]:
    """Roster of internal names, overridable with VOMEOS_INTERNAL_NAMES.

    OUTBOUND_INTERNAL_NAMES is still honoured so an existing deployment that
    set it keeps working after the guard moved into the OS.
    """
    raw = (
        os.environ.get("VOMEOS_INTERNAL_NAMES")
        or os.environ.get("OUTBOUND_INTERNAL_NAMES", "")
    )
    if raw.strip():
        return tuple(n.strip().lower() for n in raw.split(",") if n.strip())
    return _DEFAULT_INTERNAL_NAMES


# ---------------------------------------------------------------------------
# Integrations
# ---------------------------------------------------------------------------

# How long any outbound HTTP call to another system may take. The OS talks to
# other repositories over the network and never by import, so this is the one
# timeout that governs all of it.
INTEGRATION_TIMEOUT_SECONDS = int(
    os.environ.get("VOMEOS_INTEGRATION_TIMEOUT", "20")
)


def describe() -> dict[str, object]:
    """The current configuration, for the CLI and for a health endpoint."""
    return {
        "home": str(HOME),
        "tiers": dict(TIERS),
        "database": "configured" if DATABASE_URL else "not set",
        "charter_word_cap": CHARTER_WORD_CAP,
        "min_eval_cases": MIN_EVAL_CASES,
    }

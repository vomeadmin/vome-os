"""
vomeos/tiers.py

Tier name to model ID.

The table itself lives in `vomeos.config` so every environment-driven value
the OS has is in one file. This module is the lookup and the error, because
"unknown tier" needs to be loud: silently falling back to standard would let a
manifest typo quietly downgrade a senior agent, which is exactly the kind of
failure nobody notices until the output is worse and nobody knows why.
"""

from __future__ import annotations

from vomeos import config


class UnknownTier(ValueError):
    """A manifest named a tier that does not exist."""


def resolve(tier: str) -> str:
    """Tier name to model ID. Raises rather than guessing."""
    key = (tier or "").strip().lower()
    if key not in config.TIERS:
        raise UnknownTier(
            f"unknown model tier {tier!r}; expected one of "
            f"{', '.join(sorted(config.TIERS))}"
        )
    return config.TIERS[key]


def describe() -> dict[str, str]:
    """The current bench, for the CLI and for reports."""
    return dict(config.TIERS)

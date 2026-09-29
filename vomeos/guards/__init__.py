"""
vomeos/guards/

Named, deterministic checks that run on every agent's output.

A guard is declared in an agent's manifest, not passed by a caller. That is
the difference between a policy and a hope: as the number of agents grows,
"remember to call the guard" fails exactly once and that once is expensive.

Rules for every guard in this package:

  * Deterministic. A parse, a regex, a length test. No guard may call a model
    or touch the network. A guard runs on the output of a model that has
    already misbehaved, so grading it with a second model adds a second thing
    that can fail open.
  * Fails closed. A failed guard means the result is not usable and the caller
    routes it to a human. There is no "send it anyway" path.
  * Cheap. Every declared guard runs on every result, even after one fails,
    because a report naming all the reasons is far more useful to the person
    who picks it up than the first reason alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class GuardResult:
    name: str
    ok: bool
    reason: str = ""
    # Some guards produce a parsed or annotated value. json_shape returns the
    # decoded object so the runner does not parse the text a second time.
    value: object = None


GuardFn = Callable[[str, dict], GuardResult]

_REGISTRY: dict[str, GuardFn] = {}


def guard(name: str) -> Callable[[GuardFn], GuardFn]:
    """Register a guard under a name a manifest can declare."""
    def register(fn: GuardFn) -> GuardFn:
        _REGISTRY[name] = fn
        return fn
    return register


def known_guards() -> list[str]:
    return sorted(_REGISTRY)


def get_guard(name: str) -> GuardFn:
    if name not in _REGISTRY:
        raise KeyError(
            f"unknown guard {name!r}; known guards: "
            f"{', '.join(known_guards())}"
        )
    return _REGISTRY[name]


def run_guards(
    names: tuple[str, ...] | list[str], text: str, ctx: dict | None = None
) -> list[GuardResult]:
    """Run every named guard. All of them, even after one fails."""
    context = ctx or {}
    return [get_guard(name)(text, context) for name in names]


# Importing the implementations registers them. Kept at the bottom so the
# decorator and GuardResult exist first.
from vomeos.guards import builtin as _builtin  # noqa: E402,F401
from vomeos.guards import claims as _claims  # noqa: E402,F401
from vomeos.guards import client as _client  # noqa: E402,F401

__all__ = [
    "GuardResult",
    "GuardFn",
    "guard",
    "get_guard",
    "known_guards",
    "run_guards",
]

"""
vomeos/integrations/

How Vome OS reaches the rest of the company.

THE RULE
--------
The OS talks to every other system over the network. It never imports one.

No `from vomedjango import ...`, no Django settings, no shared ORM models, no
reading another repository's database directly. If the OS needs a fact that
lives in django-core, it asks django-core's API for it. If the API cannot
answer, that is a ticket against the API, not a reason to reach into the
database.

WHY THIS IS THE RULE
--------------------
Three reasons, in order of how much they will hurt.

1. **The OS has to be deployable on its own.** An import is a hard dependency:
   it means Vome OS cannot start unless that repository is installed, at a
   compatible version, with its settings loaded. One such import turns an
   independent system into a plugin.

2. **Schema coupling is invisible until it breaks.** A model field renamed in
   django-core is a normal refactor there and a production outage here, and
   nothing in either repository's tests would catch it. An HTTP contract can
   be versioned, mocked and tested from the outside.

3. **The blast radius has to stay small.** The OS runs agents. Agents are
   probabilistic. Giving probabilistic code direct ORM access to the
   production database is a category of risk we are not taking. Going through
   an API means every write the OS can perform is a write somebody
   deliberately exposed.

WHAT A CONNECTOR IS
-------------------
A small class with an explicit list of the operations it offers. Not a generic
HTTP client an agent can point anywhere: the operations are the contract, and
adding one is a deliberate act that gets reviewed.

Connectors are read-mostly by default. Any connector method that writes to
another system belongs behind an approval in the calling agent's manifest.
"""

from __future__ import annotations

from vomeos.integrations.base import (
    Connector,
    ConnectorError,
    IntegrationResult,
)

__all__ = ["Connector", "ConnectorError", "IntegrationResult"]

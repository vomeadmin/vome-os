"""
test_vomeos_independence.py

The boundary test. Vome OS must not import the application it ships inside.

WHY THIS FILE EXISTS SEPARATELY
-------------------------------
Every other rule in the OS is enforced by the onboarding gate at authoring
time. This one cannot be, because it is violated by a single convenient import
added in a hurry, in a module that otherwise looks fine, and it does not break
anything that day. It breaks on the day somebody tries to deploy Vome OS on
its own and discovers it needs `database.py`, `model_config.py` and
`signatures.py` from a support application to boot.

So it is asserted mechanically, over the actual import graph, every test run.

The rule: `vomeos/` may import the standard library, a short list of declared
third-party packages, and itself. Nothing else. Anything it needs from another
Vome repository it reaches over HTTP through `vomeos.integrations`.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).parent
KERNEL = ROOT / "vomeos"

# Third-party packages Vome OS declares as its own dependencies. This list is
# the OS's dependency budget: adding to it is a deliberate decision, which is
# why the test names them rather than allowing anything installed.
ALLOWED_THIRD_PARTY = {
    "anthropic",   # the model API
    "httpx",       # every outbound integration call
    "sqlalchemy",  # the OS's own persistence
    "celery",      # durable execution
    "kombu",       # Celery's queue primitives
    "redis",       # the broker
}

# Modules that belong to the support application, not to the OS. Named
# explicitly so the failure message can say what went wrong rather than just
# "unexpected import".
APPLICATION_MODULES = {
    "agent", "database", "model_config", "signatures", "outbound_guard",
    "intake", "kb_search", "kb_sync", "kb_gap", "kb_context", "knowledge",
    "knowledge_synthesis", "ticket_analyzer", "clickup_tasks",
    "clickup_knowledge", "slack", "slack_digest", "zoho_desk_api",
    "zoho_crm_api", "zoho_links", "status_constants", "main", "ops",
    "field_feedback", "setup_guide", "error_reports", "env_utils",
    "calendly_api", "response_templates", "intake_prompt", "system_prompt",
}


def _kernel_files() -> list[Path]:
    return sorted(KERNEL.rglob("*.py"))


def _imported_roots(path: Path) -> set[str]:
    """Every top-level module name imported anywhere in `path`.

    Walks the whole tree, so a deferred import inside a function is caught
    too. Those are exactly where a convenient shortcut hides.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import, which stays inside the package.
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


def test_kernel_has_files():
    assert _kernel_files(), "vomeos/ contains no Python files"


def test_kernel_imports_nothing_from_the_application():
    """The rule, stated as a test.

    A failure here means Vome OS can no longer be deployed on its own.
    """
    stdlib = set(sys.stdlib_module_names)
    offences: list[str] = []

    for path in _kernel_files():
        relative = path.relative_to(ROOT).as_posix()
        for root in sorted(_imported_roots(path)):
            if root in ("vomeos", "__future__") or root in stdlib:
                continue
            if root in ALLOWED_THIRD_PARTY:
                continue
            if root in APPLICATION_MODULES:
                offences.append(
                    f"{relative} imports the application module {root!r}; "
                    "the OS must reach the application over HTTP through "
                    "vomeos.integrations, never by import"
                )
            else:
                offences.append(
                    f"{relative} imports {root!r}, which is not in the OS "
                    f"dependency budget "
                    f"({', '.join(sorted(ALLOWED_THIRD_PARTY))}). Add it "
                    "deliberately or do without it."
                )

    assert not offences, "\n".join(offences)


def test_application_may_depend_on_the_os():
    """The permitted direction: inward.

    The support app's outbound_guard is now a shim over the kernel's. That is
    correct and this test pins the direction so a later refactor does not
    quietly invert it.
    """
    shim = (ROOT / "outbound_guard.py").read_text(encoding="utf-8")
    assert "from vomeos.guards.outbound import" in shim


def test_integrations_declare_a_system_and_never_import_a_repo():
    """Connectors are HTTP clients with a named system, not import wrappers."""
    from vomeos.integrations.base import Connector
    from vomeos.integrations.vome_core import VomeCore

    core = VomeCore()
    assert core.system == "vome-core"
    assert isinstance(core, Connector)
    # Unconfigured is a normal state, not an exception, and it must report
    # rather than raise so a partial environment does not crash a workflow.
    if not core.configured():
        result = core.auth_check("someone@example.com")
        assert result.ok is False
        assert "not configured" in result.error


def test_store_refuses_a_table_outside_the_os_namespace():
    """Sharing a database instance must never mean sharing a schema."""
    import pytest

    from vomeos import store

    with pytest.raises(ValueError, match="vomeos_"):
        store.register_table("agent_runs", "CREATE TABLE agent_runs ()")


def test_kernel_declares_its_own_dependencies():
    """vomeos/requirements.txt is what makes extraction a copy, not a hunt."""
    path = KERNEL / "requirements.txt"
    assert path.exists(), "vomeos/requirements.txt is missing"
    declared = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        spec = line.split("#")[0].strip()
        if not spec:
            continue
        declared.add(spec.split("==")[0].split(">=")[0].strip().lower())
    missing = ALLOWED_THIRD_PARTY - declared
    assert not missing, f"not declared in vomeos/requirements.txt: {missing}"

"""
test_vomeos.py

Tests for the agent substrate.

The important ones are not "does a valid agent validate". They are "does the
onboarding gate REJECT a bad hire", because a gate that passes everything is
worse than no gate: it provides the feeling of review without the review.

No test here calls a model or touches the database.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vomeos import composer, guards, onboarding, registry, tiers
from vomeos.manifest import ManifestError, load_manifest, parse_manifest

ROOT = Path(__file__).parent


# ---------------------------------------------------------------------------
# Fixtures: a minimal, valid agent tree written to tmp_path
# ---------------------------------------------------------------------------

GOOD_MANIFEST = """
[identity]
name = "tester"
division = "support"
title = "Tester"

[model]
tier = "standard"
max_tokens = 200

[job]
skills = []
requires_context = ["thread"]
output = "json"

[guards]
output = ["json_shape"]
client_facing = false

[approval]
escalate_to = "#somewhere"

[evals]
path = "evals/cases.jsonl"
"""


def _write_agent(base: Path, manifest_text: str, *, cases: int = 10,
                 charter: str = "Decide a thing. " * 20) -> Path:
    directory = base / "support" / "tester"
    (directory / "evals").mkdir(parents=True, exist_ok=True)
    (directory / "agent.toml").write_text(manifest_text, encoding="utf-8")
    (directory / "charter.md").write_text(charter, encoding="utf-8")
    lines = [
        json.dumps({
            "id": f"c{i}",
            "context": {"thread": "a thread"},
            "expect": {"verdict": "yes"},
        })
        for i in range(cases)
    ]
    (directory / "evals" / "cases.jsonl").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return directory


# ---------------------------------------------------------------------------
# Manifest parsing
# ---------------------------------------------------------------------------

def test_manifest_requires_identity():
    with pytest.raises(ManifestError, match="name and division"):
        parse_manifest({"identity": {"name": "x"}}, ROOT)


def test_manifest_defaults_are_conservative():
    manifest = parse_manifest(
        {"identity": {"name": "a", "division": "support"}}, ROOT
    )
    assert manifest.model.tier == "standard"
    assert manifest.job.output == "text"
    assert manifest.guards.client_facing is False
    assert manifest.approval.requires_ceo_approval is False
    assert manifest.qualified == "support.a"


def test_manifest_rejects_wrong_types():
    with pytest.raises(ManifestError, match="list of strings"):
        parse_manifest(
            {
                "identity": {"name": "a", "division": "support"},
                "job": {"skills": "not-a-list"},
            },
            ROOT,
        )


def test_load_manifest_roundtrip(tmp_path):
    directory = _write_agent(tmp_path, GOOD_MANIFEST)
    manifest = load_manifest(directory)
    assert manifest.qualified == "support.tester"
    assert manifest.job.requires_context == ("thread",)
    assert manifest.guards.output == ("json_shape",)


# ---------------------------------------------------------------------------
# Tiers
# ---------------------------------------------------------------------------

def test_every_tier_resolves():
    for tier in ("fast", "standard", "senior"):
        assert tiers.resolve(tier)


def test_unknown_tier_raises_rather_than_defaulting():
    # Silently falling back to standard would let a manifest typo quietly
    # downgrade a senior agent, which is exactly the failure nobody notices.
    with pytest.raises(tiers.UnknownTier):
        tiers.resolve("principal")


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def test_json_shape_accepts_fenced_json():
    result = guards.get_guard("json_shape")('```json\n{"a": 1}\n```', {})
    assert result.ok
    assert result.value == {"a": 1}


def test_json_shape_rejects_prose():
    result = guards.get_guard("json_shape")("Sure, here you go!", {})
    assert not result.ok


def test_json_shape_tolerates_trailing_content():
    # Seen live at temperature 0: a complete object followed by a sentence.
    # The verdict is usable, so this passes and reports the slip.
    result = guards.get_guard("json_shape")(
        '{"duplicate": false}\n\nNote: the thread was short.', {}
    )
    assert result.ok
    assert result.value == {"duplicate": False}
    assert "trailing" in result.reason


def test_json_shape_rejects_leading_prose():
    # Prose BEFORE the object is the model narrating instead of answering,
    # which is the ticket #8945 shape. That must fail.
    result = guards.get_guard("json_shape")(
        'Here is my assessment:\n{"duplicate": false}', {}
    )
    assert not result.ok


def test_json_shape_rejects_a_bare_list():
    result = guards.get_guard("json_shape")("[1, 2, 3]", {})
    assert not result.ok
    assert "object" in result.reason


def test_non_empty_rejects_blank():
    assert not guards.get_guard("non_empty")("   ", {}).ok


def test_max_length_fails_closed_without_a_limit():
    # Declaring the guard but forgetting the number must not silently pass.
    result = guards.get_guard("max_length")("hello", {})
    assert not result.ok


def test_max_length_enforces_the_limit():
    fn = guards.get_guard("max_length")
    assert fn("hello", {"max_length_chars": 10}).ok
    assert not fn("x" * 50, {"max_length_chars": 10}).ok


def test_client_message_guard_blocks_model_commentary():
    # The shape of the ticket #8945 failure: the model explaining itself.
    draft = (
        "I cannot write this reply because the system prompt instructs me to "
        "explain that the feature works as intended, but the ticket actually "
        "describes seven open bugs. I would be happy to draft a structured "
        "update template instead."
    )
    result = guards.get_guard("client_message")(draft, {})
    assert not result.ok
    assert result.reason


def test_run_guards_reports_every_failure_not_just_the_first():
    results = guards.run_guards(["json_shape", "max_length"], "nope", {})
    assert len(results) == 2
    assert all(not r.ok for r in results)


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------

def test_real_agent_composes_all_four_layers():
    manifest = registry.get_agent("support.duplicate_reply_check")
    prompt = composer.compose_system_prompt(manifest)
    assert "Vome staff handbook" in prompt          # org layer
    assert "Support division" in prompt             # division layer
    assert "Skill: reading-a-ticket-thread" in prompt  # skills layer
    assert "would repeat something" in prompt       # charter layer
    assert "Output contract" in prompt              # contract, appended last
    # The handbook must come before the charter so it cannot be overridden.
    assert prompt.index("Vome staff handbook") < prompt.index("Your job:")


def test_composition_is_deterministic():
    manifest = registry.get_agent("support.duplicate_reply_check")
    assert composer.compose_system_prompt(manifest) == (
        composer.compose_system_prompt(manifest)
    )


def test_render_context_rejects_missing_required_keys():
    manifest = registry.get_agent("support.duplicate_reply_check")
    with pytest.raises(composer.CompositionError, match="thread"):
        composer.render_context(manifest, {"draft": "d"})


def test_render_context_orders_declared_keys_first():
    manifest = registry.get_agent("support.duplicate_reply_check")
    rendered = composer.render_context(manifest, {
        "thread": "T", "draft": "D", "extra": "E",
    })
    positions = [rendered.index(f"## {label}") for label in
                 ("Draft", "Thread", "Extra")]
    assert positions == sorted(positions)


def test_render_context_labels_empty_values_rather_than_dropping_them():
    manifest = registry.get_agent("support.duplicate_reply_check")
    rendered = composer.render_context(manifest, {"draft": "", "thread": "T"})
    assert "(none provided)" in rendered


# ---------------------------------------------------------------------------
# The onboarding gate: it must reject
# ---------------------------------------------------------------------------

def _blocking(findings):
    return [f.message for f in findings if f.severity == onboarding.BLOCKING]


def test_gate_passes_the_real_agent():
    manifest = registry.get_agent("support.duplicate_reply_check")
    assert _blocking(onboarding.check_agent(manifest)) == []


def test_gate_blocks_client_facing_without_the_client_message_guard(tmp_path):
    text = GOOD_MANIFEST.replace(
        'client_facing = false', 'client_facing = true'
    )
    manifest = load_manifest(_write_agent(tmp_path, text))
    problems = _blocking(onboarding.check_agent(manifest))
    assert any("client_message" in p for p in problems)
    assert any("#8945" in p for p in problems)


def test_gate_blocks_json_agent_without_json_shape(tmp_path):
    text = GOOD_MANIFEST.replace('output = ["json_shape"]', "output = []")
    manifest = load_manifest(_write_agent(tmp_path, text))
    assert any(
        "json_shape" in p for p in _blocking(onboarding.check_agent(manifest))
    )


def test_gate_blocks_an_unknown_guard(tmp_path):
    text = GOOD_MANIFEST.replace(
        'output = ["json_shape"]', 'output = ["json_shape", "vibes"]'
    )
    manifest = load_manifest(_write_agent(tmp_path, text))
    assert any(
        "vibes" in p for p in _blocking(onboarding.check_agent(manifest))
    )


def test_gate_blocks_an_unknown_tier(tmp_path):
    text = GOOD_MANIFEST.replace('tier = "standard"', 'tier = "principal"')
    manifest = load_manifest(_write_agent(tmp_path, text))
    assert any(
        "principal" in p for p in _blocking(onboarding.check_agent(manifest))
    )


def test_gate_blocks_a_skill_that_is_not_in_the_registry(tmp_path):
    text = GOOD_MANIFEST.replace("skills = []", 'skills = ["invented-skill"]')
    manifest = load_manifest(_write_agent(tmp_path, text))
    assert any(
        "invented-skill" in p
        for p in _blocking(onboarding.check_agent(manifest))
    )


def test_gate_blocks_an_over_long_charter(tmp_path):
    manifest = load_manifest(
        _write_agent(tmp_path, GOOD_MANIFEST, charter="word " * 700)
    )
    assert any(
        "over the 600" in p
        for p in _blocking(onboarding.check_agent(manifest))
    )


def test_gate_blocks_a_thin_answer_key(tmp_path):
    manifest = load_manifest(_write_agent(tmp_path, GOOD_MANIFEST, cases=3))
    assert any(
        "needs at least" in p
        for p in _blocking(onboarding.check_agent(manifest))
    )


def test_gate_blocks_a_missing_answer_key(tmp_path):
    directory = _write_agent(tmp_path, GOOD_MANIFEST)
    (directory / "evals" / "cases.jsonl").unlink()
    manifest = load_manifest(directory)
    assert any(
        "never been scored" in p
        for p in _blocking(onboarding.check_agent(manifest))
    )


def test_gate_blocks_answer_key_cases_missing_required_context(tmp_path):
    directory = _write_agent(tmp_path, GOOD_MANIFEST)
    (directory / "evals" / "cases.jsonl").write_text(
        "\n".join(
            json.dumps({"id": f"c{i}", "context": {"wrong": "key"},
                        "expect": {"verdict": "yes"}})
            for i in range(10)
        ),
        encoding="utf-8",
    )
    manifest = load_manifest(directory)
    problems = _blocking(onboarding.check_agent(manifest))
    assert any("missing context thread" in p for p in problems)


def test_gate_blocks_a_division_with_no_handbook(tmp_path):
    text = GOOD_MANIFEST.replace(
        'division = "support"', 'division = "telepathy"'
    )
    directory = tmp_path / "telepathy" / "tester"
    (directory / "evals").mkdir(parents=True, exist_ok=True)
    (directory / "agent.toml").write_text(text, encoding="utf-8")
    (directory / "charter.md").write_text(
        "Do a thing. " * 20, encoding="utf-8"
    )
    (directory / "evals" / "cases.jsonl").write_text(
        "\n".join(
            json.dumps({"id": f"c{i}", "context": {"thread": "t"},
                        "expect": {"verdict": "y"}}) for i in range(10)
        ),
        encoding="utf-8",
    )
    manifest = load_manifest(directory)
    assert any(
        "no handbook" in p for p in _blocking(onboarding.check_agent(manifest))
    )


# ---------------------------------------------------------------------------
# Registry and the duplicate check
# ---------------------------------------------------------------------------

def test_address_must_match_the_directory():
    # Guards against an agent renamed in TOML but not on disk, which would
    # leave two addresses for one agent.
    registry.reset_cache()
    for manifest in registry.list_agents():
        assert manifest.qualified == (
            f"{manifest.directory.parent.name}.{manifest.directory.name}"
        )


def test_unknown_agent_lists_what_exists():
    with pytest.raises(registry.UnknownAgent, match="known agents"):
        registry.get_agent("support.nobody")


def test_duplicate_check_flags_a_restatement():
    hits = registry.find_similar_skills(
        "How to read a Zoho conversation thread in order and work out which "
        "customer question is still unanswered."
    )
    assert any(name == "reading-a-ticket-thread" for name, _ in hits)


def test_duplicate_check_ignores_an_unrelated_skill():
    hits = registry.find_similar_skills(
        "Reconcile a Stripe payout against invoices and flag any variance."
    )
    assert hits == []


def test_every_registered_skill_is_used_by_someone():
    # An unused skill is either dead weight or an agent that forgot to
    # declare it. Advisory in the gate, asserted here so it stays visible.
    for skill in registry.list_skills():
        assert registry.skill_users(skill.name), (
            f"skill {skill.name} is declared by no agent"
        )


# ---------------------------------------------------------------------------
# The repository as it actually stands
# ---------------------------------------------------------------------------

def test_repository_passes_the_gate():
    registry.reset_cache()
    findings, blocking = onboarding.check_all()
    assert blocking == 0, "\n".join(
        str(f) for f in findings if f.severity == onboarding.BLOCKING
    )


def test_no_em_dashes_in_any_prompt_layer():
    # Company rule, and outbound_guard treats an em dash as evidence the
    # prompt was not followed. It must not come from our own prompts.
    layers = list((ROOT / "org").rglob("*.md")) + \
        list((ROOT / "skills").glob("*.md")) + \
        list((ROOT / "agents").rglob("charter.md"))
    assert layers
    for path in layers:
        text = path.read_text(encoding="utf-8")
        assert "—" not in text, f"em dash in {path.name}"
        assert "–" not in text, f"en dash in {path.name}"

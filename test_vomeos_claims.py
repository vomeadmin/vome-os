"""
test_vomeos_claims.py

Tests for the guards that block unbacked claims.

Every case here is a real failure from the Sprint Lessons Log, 2026-09-02,
where four customers were told something no record supported and three came
back to say it was still broken. These guards exist so that class of message
cannot leave the building, and these tests exist so the guards keep working.

The must-pass cases matter as much as the blocks. A guard that fires on
ordinary support copy gets switched off, and then it protects nothing.
"""

from __future__ import annotations

from vomeos.guards import get_guard

SIG = "\n\nSam | Vome team\nsupport.vomevolunteer.com"

SHIPPED = {"verified_fix_task": "868kzkb9b", "verified_fix_status": "on prod"}
IN_FLIGHT = {"verified_fix_task": "868kzkb9b", "verified_fix_status": "queued"}


def claims(name: str, text: str, ctx: dict | None = None):
    return get_guard(name)(text, ctx or {})


# ---------------------------------------------------------------------------
# no_unverified_claims
# ---------------------------------------------------------------------------

def test_fix_claim_without_evidence_is_blocked():
    # Summit Metro Parks: told an update was pushed, never verified.
    result = claims(
        "no_unverified_claims",
        "Hi Nora, we pushed an update that should take care of the error you "
        "reported." + SIG,
    )
    assert not result.ok
    assert "no verified task" in result.reason


def test_fix_claim_with_a_queued_task_is_still_blocked():
    # Dallas Retirement Village: told hour approval "should be restored"
    # while the task sat at queued with no work recorded. A task existing is
    # not a task shipping.
    result = claims(
        "no_unverified_claims",
        "Hi Nora, hour approval should be working again now." + SIG,
        IN_FLIGHT,
    )
    assert not result.ok
    assert "not shipped" in result.reason


def test_fix_claim_with_a_shipped_task_passes():
    result = claims(
        "no_unverified_claims",
        "Hi Nora, this has been fixed and the change is live." + SIG,
        SHIPPED,
    )
    assert result.ok
    assert "868kzkb9b" in result.reason


def test_ordinary_acknowledgement_is_not_blocked():
    # The single most common reply shape. If this trips, the guard is useless
    # because it will be switched off within a week.
    result = claims(
        "no_unverified_claims",
        "Hi Nora, thanks for flagging this for us. Our team will look into "
        "it and we will follow up with any questions or updates." + SIG,
    )
    assert result.ok


def test_future_tense_is_not_a_claim():
    result = claims(
        "no_unverified_claims",
        "Hi Nora, we will be checking the form submission and will let you "
        "know what we find." + SIG,
    )
    assert result.ok


# ---------------------------------------------------------------------------
# no_roadmap_without_task
# ---------------------------------------------------------------------------

def test_roadmap_language_without_a_task_is_blocked():
    result = claims(
        "no_roadmap_without_task",
        "Hi Nora, availability sync is on our roadmap." + SIG,
    )
    assert not result.ok


def test_roadmap_language_with_a_task_passes():
    result = claims(
        "no_roadmap_without_task",
        "Hi Nora, this is on our roadmap." + SIG,
        {"roadmap_task": "868m0dqhj"},
    )
    assert result.ok


# ---------------------------------------------------------------------------
# no_confirmation
# ---------------------------------------------------------------------------

def test_confirming_the_bug_is_blocked():
    result = claims(
        "no_confirmation",
        "Hi Nora, we can see the issue you are experiencing and we have "
        "identified the problem." + SIG,
    )
    assert not result.ok


def test_claiming_data_is_safe_is_blocked():
    # #8960: the first draft said missing emergency contact data was intact
    # in the form submission, before anyone had checked.
    result = claims(
        "no_confirmation",
        "Hi Nora, your data is safe and nothing has been lost." + SIG,
    )
    assert not result.ok
    assert "verified" in result.reason


def test_claiming_to_have_looked_at_an_account_is_blocked():
    result = claims(
        "no_confirmation",
        "Hi Nora, we can see Marta's account and everything looks normal."
        + SIG,
    )
    assert not result.ok


def test_praising_an_unseen_attachment_is_blocked():
    result = claims(
        "no_confirmation",
        "Hi Nora, thanks for the video, that is super clear." + SIG,
    )
    assert not result.ok


def test_praising_an_attachment_someone_actually_opened_passes():
    result = claims(
        "no_confirmation",
        "Hi Nora, thanks for the video, that is super clear." + SIG,
        {"attachment_reviewed": True},
    )
    assert result.ok


def test_a_resolution_reply_on_shipped_work_may_describe_the_cause():
    # The one legitimate exception: by the time a fix has shipped,
    # engineering really has investigated.
    result = claims(
        "no_confirmation",
        "Hi Nora, we have confirmed this was affecting reservations created "
        "before the shift was published." + SIG,
        {**SHIPPED, "resolution_reply": True},
    )
    assert result.ok


def test_neutral_thanks_passes():
    result = claims(
        "no_confirmation",
        "Hi Nora, thanks for sending the screenshot over. Our team will take "
        "a look." + SIG,
    )
    assert result.ok


# ---------------------------------------------------------------------------
# required_signature
# ---------------------------------------------------------------------------

def test_missing_signature_is_blocked():
    result = claims(
        "required_signature", "Hi Nora, our team will look into it."
    )
    assert not result.ok


def test_wrong_signer_is_blocked():
    # Breached twice in one sprint, both signed "Vic / Support Team".
    result = claims(
        "required_signature",
        "Hi Nora, our team will look into it.\n\nVic\nSupport Team",
    )
    assert not result.ok


def test_correct_signature_passes():
    result = claims(
        "required_signature", "Hi Nora, our team will look into it." + SIG
    )
    assert result.ok


# ---------------------------------------------------------------------------
# The guards compose
# ---------------------------------------------------------------------------

def test_the_full_outbound_set_blocks_the_worst_case():
    """One draft, every failure mode from the lessons log at once."""
    from vomeos.guards import run_guards

    draft = (
        "Hi Nora, we can see the issue and we have identified the problem. "
        "We pushed a fix so it should be working now, and the rest is on our "
        "roadmap. Your data is safe.\n\nVic\nSupport Team"
    )
    results = run_guards(
        [
            "no_unverified_claims",
            "no_roadmap_without_task",
            "no_confirmation",
            "required_signature",
        ],
        draft,
        {},
    )
    assert all(not r.ok for r in results), [r.name for r in results if r.ok]


def test_a_correct_acknowledgement_passes_the_full_set():
    from vomeos.guards import run_guards

    draft = (
        "Hi Nora, thanks for flagging this for us. Our team will look into "
        "it and we will follow up with any questions or updates." + SIG
    )
    results = run_guards(
        [
            "no_unverified_claims",
            "no_roadmap_without_task",
            "no_confirmation",
            "required_signature",
        ],
        draft,
        {},
    )
    failed = [f"{r.name}: {r.reason}" for r in results if not r.ok]
    assert not failed, failed

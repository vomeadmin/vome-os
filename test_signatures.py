"""
test_signatures.py

Sam, 2026-10-23: every email that goes out without his explicit approval is
signed "Vome team", never with his name. Drafts stay signed "Sam | Vome
team" so he can send them as they are; only an automatic send is re-signed.
"""

import on_prod_handler
from signatures import AUTO_SENDER, signature, team_signed


def test_automatic_sender_is_the_team():
    assert AUTO_SENDER == "team"
    assert "Sam" not in signature(AUTO_SENDER)
    assert "Sam" not in signature(AUTO_SENDER, "fr")


def test_team_signed_swaps_sams_name():
    draft = ("Hi Dana,\n\nAn update is now live.\n\nBest,\n\n"
             "Sam | Vome team\nsupport.vomevolunteer.com")
    assert team_signed(draft) == (
        "Hi Dana,\n\nAn update is now live.\n\nBest,\n\n"
        "Vome team\nsupport.vomevolunteer.com")


def test_team_signed_keeps_french_french():
    draft = ("Bonjour Dana,\n\nUne mise à jour est en ligne.\n\n"
             "Cordialement,\n\nSam | Vome team\nsupport.vomevolunteer.com")
    out = team_signed(draft)
    assert out.endswith("Cordialement,\n\nÉquipe Vome\n"
                        "support.vomevolunteer.com")
    assert "Sam" not in out


def test_team_signed_signs_an_unsigned_body_once():
    out = team_signed("Hi Dana,\n\nAn update is now live.")
    assert out.count("support.vomevolunteer.com") == 1
    assert "Vome team" in out


def test_on_prod_resolution_is_signed_vome_team(monkeypatch):
    class _Msg:
        content = [type("B", (), {"text": "Hi Dana, the update is live."})()]

    monkeypatch.setattr(on_prod_handler._anthropic.messages, "create",
                        lambda **kw: _Msg())
    draft = on_prod_handler._generate_resolution_draft(
        {"contact_name": "Dana"}, "", "Bug", "Schedule")
    assert "Sam" not in draft
    assert draft.endswith("Vome team\nsupport.vomevolunteer.com")

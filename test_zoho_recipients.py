"""
test_zoho_recipients.py

Who a reply greets. #9596 (2026-10-20): the author "Ia Faye Sison" was
greeted as "Ia", because the first word of a display name is not always the
whole first name. Zoho keeps the first name on the author; it wins.
"""

import zoho_actions as za


def test_the_first_name_zoho_keeps_wins():
    assert za.first_name("Ia Faye Sison", "isison@x.org", "Ia Faye") == \
        "Ia Faye"


def test_without_it_the_display_name_still_works():
    assert za.first_name("Phan, Dalena") == "Dalena"
    assert za.first_name("Stephanie Sielaty") == "Stephanie"
    assert za.first_name("", "jane.doe@x.org") == "Jane"


def test_an_address_in_the_first_name_is_ignored():
    assert za.first_name("Stephanie Sielaty", "", "s@x.org") == "Stephanie"


def test_recipients_greet_with_the_authors_first_name(monkeypatch):
    thread = {"id": "T1", "direction": "in", "createdTime": "2026-10-20",
              "author": {"name": "Ia Faye Sison", "firstName": "Ia Faye",
                         "email": "isison@candlelightersnv.org"}}

    def get(path, params=None):
        if path.endswith("/threads"):
            return {"data": [thread]}
        return {"secondaryContacts": []}

    monkeypatch.setattr(za, "_get", get)
    out = za.recipients("1")
    assert out["to"] == "isison@candlelightersnv.org"
    assert out["first_name"] == "Ia Faye"


# #9610 (2026-10-23): CHUM wrote to support with Ron and Sam on To, not CC.
# The reply read only the newest message's CC and dropped Ron. The client's
# next message left Ron off too, so the whole thread decides, not the newest.
CHUM = [
    {"id": "T1", "direction": "in", "createdTime": "2026-10-21T18:26",
     "fromEmailAddress": "rocheteau@chum.qc.ca",
     "to": '"r.segev@vomevolunteer.com"<r.segev@vomevolunteer.com>,'
           '"s.fagen@vomevolunteer.com"<s.fagen@vomevolunteer.com>,'
           '"vome volunteer"<support@vomevolunteer.zohodesk.com>',
     "cc": '"dominique"<dominique@chum.qc.ca>',
     "author": {"name": "Rocheteau D", "firstName": "Rocheteau"}},
    {"id": "T2", "direction": "out", "createdTime": "2026-10-23T03:46",
     "to": "rocheteau@chum.qc.ca", "cc": "dominique@chum.qc.ca"},
    {"id": "T3", "direction": "in", "createdTime": "2026-10-23T17:45",
     "fromEmailAddress": "rocheteau@chum.qc.ca",
     "to": "support@vomevolunteer.zohodesk.com",
     "cc": '"dominique"<dominique@chum.qc.ca>',
     "author": {"name": "Rocheteau D", "firstName": "Rocheteau"}},
]


def _chum(monkeypatch, threads=CHUM):
    def get(path, params=None):
        if path.endswith("/threads"):
            return {"data": threads}
        return {"secondaryContacts": []}
    monkeypatch.setattr(za, "_get", get)


def test_a_teammate_the_client_wrote_to_stays_copied(monkeypatch):
    _chum(monkeypatch)
    out = za.recipients("9610")
    assert out["to"] == "rocheteau@chum.qc.ca"
    assert out["cc"] == ["dominique@chum.qc.ca", "r.segev@vomevolunteer.com"]


def test_sam_and_shared_inboxes_are_not_copied(monkeypatch):
    _chum(monkeypatch)
    cc = za.recipients("9610")["cc"]
    assert "s.fagen@vomevolunteer.com" not in cc
    assert not any("zohodesk" in c for c in cc)


def test_a_forward_to_an_engineer_does_not_copy_them(monkeypatch):
    fwd = {"id": "F", "direction": "out", "isForward": True,
           "createdTime": "2026-10-22", "to": "dev@vomevolunteer.com"}
    _chum(monkeypatch, [CHUM[2], fwd])
    assert za.recipients("9610")["cc"] == ["dominique@chum.qc.ca"]

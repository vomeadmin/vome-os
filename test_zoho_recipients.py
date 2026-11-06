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


# #9521 (2026-11-04): Zoho kept the Outlook name "Phan, Dalena" as the first
# name with no last name, and the reply went out as "Hi Phan, Dalena".
def test_a_last_comma_first_name_zoho_keeps_is_split():
    assert za.first_name("Phan, Dalena", "phanda@mlhs.org",
                         "Phan, Dalena") == "Dalena"
    assert za.first_name("", "phanda@mlhs.org", "Phan, Dalena") == "Dalena"


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
    assert out["cc"][:2] == ["dominique@chum.qc.ca",
                             "r.segev@vomevolunteer.com"]


def test_shared_inboxes_are_not_copied(monkeypatch):
    _chum(monkeypatch)
    cc = za.recipients("9610")["cc"]
    assert not any("zohodesk" in c for c in cc)
    assert "admin@vomevolunteer.com" not in cc


def test_a_forward_to_an_engineer_does_not_copy_them(monkeypatch):
    fwd = {"id": "F", "direction": "out", "isForward": True,
           "createdTime": "2026-10-22", "to": "dev@vomevolunteer.com"}
    _chum(monkeypatch, [CHUM[2], fwd])
    assert za.recipients("9610")["cc"] == ["dominique@chum.qc.ca"]


# Sam, 2026-11-06: copy him when it is a customer thread he should know
# about, never on a plain inquiry from a volunteer or admin.
def test_sam_is_copied_on_a_customer_thread_at_the_address_used(monkeypatch):
    _chum(monkeypatch)
    assert za.recipients("9610")["cc"] == [
        "dominique@chum.qc.ca", "r.segev@vomevolunteer.com",
        "s.fagen@vomevolunteer.com"]


def test_sam_is_not_copied_on_an_inquiry(monkeypatch):
    inquiry = {"id": "Q", "direction": "in", "createdTime": "2026-11-06",
               "fromEmailAddress": "volunteer@gmail.com",
               "to": "support@vomevolunteer.zohodesk.com",
               "author": {"name": "Jim Cranston", "firstName": "Jim"}}
    _chum(monkeypatch, [inquiry])
    out = za.recipients("Q")
    assert out["to"] == "volunteer@gmail.com"
    assert out["cc"] == []
    assert out["forwarded_by"] == ""


def test_a_client_who_wrote_to_sam_alone_copies_sam(monkeypatch):
    msg = {"id": "S", "direction": "in", "createdTime": "2026-11-06",
           "fromEmailAddress": "ed@client.org",
           "to": "s.fagen@vomevolunteer.co,support@vomevolunteer.zohodesk.com",
           "author": {"name": "Ed"}}
    _chum(monkeypatch, [msg])
    assert za.recipients("S")["cc"] == ["s.fagen@vomevolunteer.co"]


# 2026-11-06: Ron wrote to Heather with support copied, the ticket's contact
# was Ron, and Vic emailed Ron the "claim your profile" reply. A teammate's
# message is answered to the customer, with Ron and Sam copied.
RON_TO_HEATHER = {
    "id": "R1", "direction": "in", "createdTime": "2026-11-06T13:28",
    "fromEmailAddress": "r.segev@vomevolunteer.co",
    "to": '"Heather Brown"<heather@hospice.org>',
    "cc": '"vome volunteer"<support@vomevolunteer.zohodesk.com>',
    "author": {"name": "Ron Segev", "firstName": "Ron",
               "email": "r.segev@vomevolunteer.co"},
}


def test_a_teammates_message_is_answered_to_the_customer(monkeypatch):
    _chum(monkeypatch, [RON_TO_HEATHER])
    out = za.recipients("R")
    assert out["to"] == "heather@hospice.org"
    assert out["first_name"] == "Heather"
    assert out["forwarded_by"] == "r.segev@vomevolunteer.co"
    assert out["cc"] == ["r.segev@vomevolunteer.co", "s.fagen@vomevolunteer.co"]


def test_a_teammate_forward_falls_back_to_the_client_who_wrote_in(monkeypatch):
    client = {"id": "C1", "direction": "in", "createdTime": "2026-11-05",
              "fromEmailAddress": "heather@hospice.org",
              "to": "r.segev@vomevolunteer.co",
              "author": {"name": "Heather Brown", "firstName": "Heather"}}
    fwd = {**RON_TO_HEATHER, "to": "support@vomevolunteer.zohodesk.com",
           "cc": ""}
    _chum(monkeypatch, [client, fwd])
    out = za.recipients("R")
    assert out["to"] == "heather@hospice.org"
    assert out["first_name"] == "Heather"


def test_a_pure_forward_reads_the_quoted_sender(monkeypatch):
    fwd = {**RON_TO_HEATHER, "to": "support@vomevolunteer.zohodesk.com",
           "cc": ""}
    content = ("Can you help her?<br><br>---------- Forwarded message "
               "---------<br>From: <b>Heather Brown</b> &lt;heather@hospice"
               ".org&gt;<br>Date: Thu, Nov 5<br>To: Ron Segev &lt;"
               "r.segev@vomevolunteer.co&gt;")

    def get(path, params=None):
        if path.endswith("/threads"):
            return {"data": [fwd]}
        if path.endswith("/threads/R1"):
            return {"content": content}
        return {"secondaryContacts": []}

    monkeypatch.setattr(za, "_get", get)
    out = za.recipients("R")
    assert out["to"] == "heather@hospice.org"
    assert out["first_name"] == "Heather"
    assert "r.segev@vomevolunteer.co" in out["cc"]


def test_no_customer_found_never_falls_back_to_the_teammate(monkeypatch):
    fwd = {**RON_TO_HEATHER, "to": "support@vomevolunteer.zohodesk.com",
           "cc": ""}
    _chum(monkeypatch, [fwd])
    out = za.recipients("R")
    assert out["to"] == ""
    assert out["forwarded_by"] == "r.segev@vomevolunteer.co"

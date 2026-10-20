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

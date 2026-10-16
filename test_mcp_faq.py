"""
test_mcp_faq.py

The /faq workflow behind the connector: the rule check, the Zoho client's
trap handling, and publishing (write order, read-back, repair, and the
promise that a failed publish never creates a second article).

Zoho is faked. The rules under test are the ones the FAQ spec records as
having actually bitten.
"""

from __future__ import annotations

import json
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from mcp_connector import faq, kb_rules, store, zoho_kb
from mcp_connector.feature_requests import RequestError

INFO_BANNER = (
    '<span class="KB_New_Editor_Highlights" style="background-color: '
    'rgb(193, 225, 244); border-right: 5px solid rgb(167, 212, 234)">'
    '<div><img src="https://img.zohostatic.com/zde/static/images/info.png" '
    'alt="Info"></div><div><b>Plan required:</b> Ultimate.</div></span>'
)
BANNER_FR = INFO_BANNER.replace("Plan required:", "Forfait requis :")


def _draft(**over) -> dict:
    draft = {
        "action": "create",
        "article_id": None,
        "category_id": "leaf-1",
        "plan": "ultimate",
        "en": {
            "title": "How do I start the kiosk with single sign-on?",
            "permalink": "start-kiosk-with-sso",
            "answer_html": "<h2>Overview</h2><p>Start it with SSO.</p>"
                           + INFO_BANNER + "<h2>1. How?</h2><p>Click "
                           "<b>Admin Login with SSO</b>.</p>",
            "seo_title": "Start the kiosk with SSO",
            "seo_description": "Start a Vome kiosk with single sign-on.",
        },
        "fr": {
            "title": "Comment démarrer la borne avec le SSO ?",
            "permalink": "demarrer-borne-avec-sso",
            "answer_html": "<h2>Aperçu</h2><p>Démarrez-la avec le SSO.</p>"
                           + BANNER_FR + "<h2>1. Comment ?</h2><p>Cliquez "
                           "sur <b>Connexion admin avec SSO</b>.</p>",
            "seo_title": "Démarrer la borne avec le SSO",
            "seo_description": "Démarrez une borne Vome avec le SSO.",
        },
    }
    draft.update(over)
    return draft


@pytest.fixture(autouse=True)
def _store():
    store.set_engine(create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    ))


# ---------------------------------------------------------------------------
# the rule check
# ---------------------------------------------------------------------------

def test_a_clean_draft_passes():
    assert kb_rules.check_draft(_draft()) == {"errors": [], "warnings": []}


@pytest.mark.parametrize("locale,field,value,needle", [
    ("en", "answer_html", f"<p>Fast {kb_rules.EM_DASH} and easy.</p>",
     "em dash"),
    ("fr", "title", "Rapide &mdash; et simple", "em dash"),
    ("fr", "title", "Comment fonctionne l'int&eacute;gration ?", "entity"),
    ("en", "seo_title", "Kiosk &amp; SSO", "entity"),
    ("en", "permalink", "Start Kiosk", "permalink"),
    ("fr", "answer_html", "<p>Voir https://support.vomevolunteer.com/"
     "portal/en/kb/articles/x</p>", "English portal URL"),
    ("en", "answer_html", "<p>Email support@vomevolunteer.com</p>",
     "does not reach the desk"),
    ("en", "answer_html", "<p>Email support@vomevolunteer.zohodesk.com</p>",
     "submit-a-ticket"),
    ("en", "answer_html", "<p>Gone � here</p>", "replacement"),
    ("en", "answer_html", "## Overview\n**Bold** text", "Markdown"),
    ("en", "answer_html", "<p>See the French version.</p>",
     "cross-language"),
])
def test_rules_that_have_bitten_block_publishing(
    locale, field, value, needle,
):
    draft = _draft()
    draft[locale][field] = value
    errors = kb_rules.check_draft(draft)["errors"]
    assert any(needle.lower() in e.lower() for e in errors), errors


def test_a_banner_inside_a_paragraph_is_refused():
    draft = _draft()
    draft["en"]["answer_html"] = "<p>Note: " + INFO_BANNER + "</p>"
    draft["fr"]["answer_html"] = "<p>Note : " + BANNER_FR + "</p>"
    errors = kb_rules.check_draft(draft)["errors"]
    assert any("inside a <p>" in e for e in errors)


def test_a_banner_in_one_language_must_be_in_the_other():
    draft = _draft()
    draft["fr"]["answer_html"] = draft["fr"]["answer_html"].replace(
        BANNER_FR, "",
    )
    errors = kb_rules.check_draft(draft)["errors"]
    assert any("EN has 1 banners and FR has 0" in e for e in errors)


def test_an_update_keeps_an_accented_live_permalink():
    """The live Shift Series FR permalink is les-séries-de-quarts-sur-vome.
    Updates never send it, so the slug rule must not block them."""
    draft = _draft(action="update", article_id="569440000038631061")
    draft["fr"]["permalink"] = "les-séries-de-quarts-sur-vome"
    assert kb_rules.check_draft(draft)["errors"] == []
    new = _draft()
    new["fr"]["permalink"] = "les-séries-de-quarts-sur-vome"
    assert kb_rules.check_draft(new)["errors"]


def test_terminology_is_a_warning_not_a_block():
    draft = _draft()
    draft["fr"]["answer_html"] += "<p>Chaque bénévole peut attribuer.</p>"
    draft["en"]["answer_html"] += "<p>Each volunteer and Opportunity Role.</p>"
    result = kb_rules.check_draft(draft)
    assert result["errors"] == []
    joined = " ".join(result["warnings"])
    for needle in ("bénévole", "attribuer", "volunteer", "Opportunity Role"):
        assert needle in joined


# ---------------------------------------------------------------------------
# the Zoho client
# ---------------------------------------------------------------------------

class _Response:
    def __init__(self, envelope, content_type="application/json"):
        self.status_code = 200
        self.headers = {"content-type": content_type}
        body = json.dumps(envelope)
        if content_type == "text/event-stream":
            body = f"event: message\ndata: {body}\n\n"
        self.content = body.encode("utf-8")


def _ok(payload) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "result": {"content": [
        {"type": "text", "text": json.dumps(payload)},
    ]}}


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def fake_post(url, json=None, **kwargs):
        calls.append(json["params"])
        return _Response(_ok({"id": "1"}))

    monkeypatch.setenv("ZOHO_DESK_MCP_URL", "https://desk.example/mcp/x")
    # Other test modules set a dummy org id; these tests pin the defaults.
    monkeypatch.delenv("ZOHO_ORG_ID", raising=False)
    monkeypatch.delenv("MCP_KB_AUTHOR_ID", raising=False)
    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


def test_every_call_carries_org_id_and_uses_body(sent):
    zoho_kb.create_article(category_id="c", en=_draft()["en"])
    arguments = sent[0]["arguments"]
    assert arguments["query_params"]["orgId"] == "736165782"
    assert "body" in arguments and "request_body" not in arguments
    assert arguments["body"]["permission"] == "ALL"
    assert arguments["body"]["authorId"] == "569440000000192003"
    assert arguments["body"]["status"] == "Published"


def test_path_variable_names_follow_each_tool(sent):
    zoho_kb.get_article("A")
    zoho_kb.get_translation("A", "fr")
    zoho_kb.update_translation(article_id="A", locale="fr",
                               fields=_draft()["fr"])
    assert sent[0]["arguments"]["path_variables"] == {"id": "A"}
    assert sent[1]["arguments"]["path_variables"] == {
        "id": "A", "locale": "fr"}
    update = sent[2]["arguments"]
    assert update["path_variables"] == {"articleId": "A", "locale": "fr"}
    # In the body, locale silently voids the write.
    assert "locale" not in update["body"]


def test_locale_in_an_update_body_is_refused_before_sending(sent):
    with pytest.raises(zoho_kb.ZohoKBError, match="locale"):
        zoho_kb.call("ZohoDesk_updateArticleTranslation",
                     path={"articleId": "A", "locale": "fr"},
                     body={"locale": "fr", "title": "x"})
    assert sent == []


def test_permalinks_are_never_pre_checked(sent):
    zoho_kb.create_article(category_id="c", en=_draft()["en"])
    zoho_kb.create_translation(article_id="A", locale="fr",
                               fields=_draft()["fr"])
    names = [call["name"] for call in sent]
    assert "ZohoDesk_checkArticlePermalinkAvailability" not in names


def test_a_zoho_error_is_raised_with_its_message(monkeypatch):
    import httpx

    monkeypatch.setenv("ZOHO_DESK_MCP_URL", "https://desk.example/mcp/x")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Response({
        "jsonrpc": "2.0", "id": 1, "result": {"isError": True, "content": [
            {"type": "text", "text": '{"errorCode":"INVALID_DATA"}'}]},
    }))
    with pytest.raises(zoho_kb.ZohoKBError, match="INVALID_DATA"):
        zoho_kb.get_article("A")


def test_event_stream_replies_are_read(monkeypatch):
    import httpx

    monkeypatch.setenv("ZOHO_DESK_MCP_URL", "https://desk.example/mcp/x")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Response(
        _ok({"id": "42"}), content_type="text/event-stream",
    ))
    assert zoho_kb.get_article("42") == {"id": "42"}


def test_category_tree_flattens_and_marks_leaves(monkeypatch):
    tree = {"id": "root", "translations": [{"locale": "en", "name": "FAQ"}],
            "children": [{
                "id": "admin", "name": "For administrators", "children": [
                    {"id": "parent", "name": "Forms", "children": [
                        {"id": "leaf-1", "translations": [
                            {"locale": "en", "name": "Sharing forms"},
                            {"locale": "fr", "name": "Partager"}],
                         "children": []},
                    ]},
                ]}]}
    monkeypatch.setattr(zoho_kb, "call", lambda *a, **k: tree)
    zoho_kb.category_tree(force=True)
    leaves = zoho_kb.leaf_categories()
    assert [c["id"] for c in leaves] == ["leaf-1"]
    assert leaves[0]["path"] == "For administrators > Forms > Sharing forms"
    assert leaves[0]["name_fr"] == "Partager"
    assert zoho_kb.category("parent")["is_leaf"] is False


# ---------------------------------------------------------------------------
# publishing
# ---------------------------------------------------------------------------

class FakeZoho:
    """An in-memory help centre with Zoho's quirks switchable on."""

    def __init__(self):
        self.articles: dict[str, dict] = {}
        self.calls: list[str] = []
        self.fail_translation = False
        self.drop_permission_once = False

    def install(self, monkeypatch):
        for name in ("create_article", "create_translation",
                     "update_article", "update_translation", "get_article",
                     "get_translation", "list_translations", "category"):
            monkeypatch.setattr(zoho_kb, name, getattr(self, name))

    def _record(self, fields, status, locale, permalink=None):
        slug = permalink or fields["permalink"]
        permission = "ALL"
        if self.drop_permission_once:
            self.drop_permission_once = False
            permission = "REGISTEREDUSERS"
        return {
            "title": fields["title"], "answer": fields["answer_html"],
            "status": status, "permission": permission,
            "permalink": slug, "categoryId": "leaf-1",
            "portalUrl": f"https://support.vomevolunteer.com/portal/"
                         f"{locale}/kb/articles/{slug}",
        }

    def create_article(self, *, category_id, en, status="Published"):
        self.calls.append("create_article")
        article_id = str(len(self.articles) + 1000)
        self.articles[article_id] = {"en": self._record(en, status, "en")}
        return {"id": article_id}

    def create_translation(self, *, article_id, locale, fields,
                           status="Published"):
        self.calls.append("create_translation")
        if self.fail_translation:
            raise zoho_kb.ZohoKBError("translation refused")
        self.articles[article_id][locale] = self._record(
            fields, status, locale)
        return {}

    def update_article(self, *, article_id, en, status="Published"):
        self.calls.append("update_article")
        old = self.articles[article_id]["en"]
        self.articles[article_id]["en"] = self._record(
            en, status, "en", permalink=old["permalink"])
        return {}

    def update_translation(self, *, article_id, locale, fields,
                           status="Published"):
        self.calls.append("update_translation")
        old = self.articles[article_id][locale]
        self.articles[article_id][locale] = self._record(
            fields, status, locale, permalink=old["permalink"])
        return {}

    def get_article(self, article_id):
        return {"id": article_id, **self.articles[article_id]["en"]}

    def get_translation(self, article_id, locale):
        return self.articles[article_id].get(locale) or {}

    def list_translations(self, article_id):
        return [{"locale": loc} for loc in self.articles.get(article_id, {})]

    def category(self, category_id, force=False):
        return {"id": category_id, "path": "Admin > Kiosks",
                "is_leaf": category_id == "leaf-1", "name_fr": ""}


@pytest.fixture
def zoho(monkeypatch):
    fake = FakeZoho()
    fake.install(monkeypatch)
    monkeypatch.setattr(faq, "_refresh_index", lambda *a: True)
    return fake


def _ready_job(draft=None) -> str:
    draft = draft or _draft()
    store.create_faq_job("job1", "sam@vomevolunteer.co", {"request": "x"},
                         status="ready", draft=draft,
                         lint=kb_rules.check_draft(draft))
    return "job1"


def test_publish_writes_english_then_french_and_reads_back(zoho):
    result = faq.publish(_ready_job(), "sam@vomevolunteer.co")
    assert zoho.calls == ["create_article", "create_translation"]
    assert result["problems"] == []
    assert result["en"]["portal_url"].endswith("/en/kb/articles/"
                                               "start-kiosk-with-sso")
    assert "/fr/kb/articles/demarrer-borne-avec-sso" in (
        result["fr"]["portal_url"])
    assert store.get_faq_job("job1")["status"] == "published"


def test_a_published_job_cannot_publish_twice(zoho):
    faq.publish(_ready_job(), "sam@vomevolunteer.co")
    with pytest.raises(RequestError, match="only a ready draft"):
        faq.publish("job1", "sam@vomevolunteer.co")
    assert zoho.calls.count("create_article") == 1


def test_a_failed_french_write_never_duplicates_on_retry(zoho):
    zoho.fail_translation = True
    with pytest.raises(RequestError, match="will update it"):
        faq.publish(_ready_job(), "sam@vomevolunteer.co")
    job = store.get_faq_job("job1")
    assert job["status"] == "publish_failed"
    assert job["draft"]["action"] == "update"

    zoho.fail_translation = False
    faq.publish("job1", "sam@vomevolunteer.co")
    assert zoho.calls.count("create_article") == 1
    assert len(zoho.articles) == 1
    assert "fr" in next(iter(zoho.articles.values()))


def test_a_dropped_permission_is_repaired_once(zoho):
    zoho.drop_permission_once = True
    result = faq.publish(_ready_job(), "sam@vomevolunteer.co")
    assert "update_article" in zoho.calls
    assert result["problems"] == []


def test_publish_refuses_a_draft_that_fails_the_rules(zoho):
    draft = _draft()
    draft["en"]["title"] = f"Fast {kb_rules.EM_DASH} easy"
    _ready_job(draft)
    with pytest.raises(RequestError, match="nothing was published"):
        faq.publish("job1", "sam@vomevolunteer.co")
    assert zoho.calls == []
    assert store.get_faq_job("job1")["status"] == "needs_revision"


def test_a_parent_category_is_refused_before_writing(zoho):
    _ready_job(_draft(category_id="parent"))
    with pytest.raises(RequestError, match="child sections"):
        faq.publish("job1", "sam@vomevolunteer.co")
    assert zoho.calls == []


def test_as_draft_saves_without_publishing(zoho):
    result = faq.publish(_ready_job(), "sam@vomevolunteer.co", as_draft=True)
    assert result["status"] == "Draft"
    assert result["indexed_for_search"] is False
    assert store.get_faq_job("job1")["status"] == "saved_as_draft"


def test_an_update_keeps_the_live_permalinks(zoho, monkeypatch):
    created = zoho.create_article(category_id="leaf-1", en=_draft()["en"])
    zoho.create_translation(article_id=created["id"], locale="fr",
                            fields=_draft()["fr"])
    monkeypatch.setattr(faq, "resolve_article", lambda ref: ref)
    changed = _draft(action="update", article_id=created["id"])
    changed["en"]["permalink"] = "a-new-slug-it-invented"
    pinned = faq.normalise(changed, {})
    assert pinned["en"]["permalink"] == "start-kiosk-with-sso"
    assert pinned["fr"]["permalink"] == "demarrer-borne-avec-sso"


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------

def test_search_queries_are_short():
    found = faq.queries({
        "request": "Kiosk SSO start",
        "developer_notes": "Admins can start the kiosk with SAML. The start "
                           "screen shows an Admin Login with SSO button. ok",
    })
    assert found[0] == "Kiosk SSO start"
    assert all(len(q.split()) <= 16 for q in found)
    assert any("Admin Login with SSO" in q for q in found)


def test_a_job_stuck_after_a_restart_says_so():
    store.create_faq_job("old", "x", {"request": "r"}, status="drafting")
    job = store.get_faq_job("old")
    job["updated_at"] = time.time() - faq.STALE_AFTER - 5
    assert faq.view(job)["status"] == "interrupted"


def test_a_job_runs_the_agent_and_stores_a_checked_draft(monkeypatch, zoho):
    from vomeos import runner

    class Result:
        status = "ok"
        error = ""
        run_id = "run-1"
        data = _draft()

        def why(self):
            return ""

    monkeypatch.setattr(faq, "build_context", lambda request: {})
    monkeypatch.setattr(runner, "run_agent", lambda *a, **k: Result())
    store.create_faq_job("j", "x", {"request": "r"})
    faq._run("j")
    job = store.get_faq_job("j")
    assert job["status"] == "ready"
    assert job["lint"]["errors"] == []
    assert faq.view(job)["en"]["title"].startswith("How do I start")


def test_needs_input_is_its_own_state(monkeypatch):
    from vomeos import runner

    class Result:
        status = "ok"
        error = ""
        run_id = "run-2"
        data = {"action": "needs_input", "questions": ["Which plan?"],
                "en": None, "fr": None}

        def why(self):
            return ""

    monkeypatch.setattr(faq, "build_context", lambda request: {})
    monkeypatch.setattr(runner, "run_agent", lambda *a, **k: Result())
    store.create_faq_job("q", "x", {"request": "r"})
    faq._run("q")
    view = faq.view(store.get_faq_job("q"))
    assert view["status"] == "needs_input"
    assert view["questions"] == ["Which plan?"]

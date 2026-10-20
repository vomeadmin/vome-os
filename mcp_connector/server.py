"""
mcp_connector/server.py

The MCP server, and how it attaches to the existing web service.

ONE SERVICE, NOT TWO
--------------------
The connector rides on `main.app` rather than running as its own Railway
service. It needs the same database, the same ClickUp and code tokens and the
same product modules, and a second service would mean a second copy of every
secret. It is attached by a small ASGI dispatcher in front of FastAPI: the
handful of paths the MCP and OAuth protocols define go to the MCP app, and
everything else reaches FastAPI exactly as before, including the Command
Center SPA mounted at "/".

Stateless HTTP with JSON responses: no server-side sessions to lose on a
deploy, and nothing that needs sticky routing.

OFF UNTIL CONFIGURED
--------------------
With MCP_PUBLIC_URL or the Google client unset, `attach` does nothing and
says so on boot. There is no unauthenticated mode.
"""

from __future__ import annotations

import contextlib
import threading
import time
from typing import Annotated, Literal

import anyio
from pydantic import Field
from starlette.requests import Request

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from mcp_connector import config, product_source, store, tools
from mcp_connector.feature_requests import RequestError
from mcp_connector.oauth import GoogleOAuthProvider

INSTRUCTIONS = """\
Vome OS: live access to how the Vome platform actually works, for people at \
Vome answering customers.

Order of work for a customer question:
1. lookup_feature: help centre steps, the product's own UI text, and caveats \
the team has learned. Start here.
2. where_in_app: the page and the exact wording of its buttons, when the \
answer needs "go to X, click Y".
3. check_existing_work: before saying anything is fixed, coming, known or \
planned. Only a shipped status means fixed.
4. search_code then read_code_file: only when the sources above do not \
settle it. read_code_file reads the branch production runs.
5. If the customer wants something Vome does not do: log_feature_request. \
It checks for duplicates first and never creates one silently over a match.
6. Help centre articles (the /faq workflow): run_faq hands the article to \
the Vome OS writer, faq_job_status returns its English and French draft, \
and publish_faq_draft publishes it. Show the draft and wait for an \
explicit go-ahead before publishing. Report the portal URLs exactly as \
publish_faq_draft returns them.

Rules for anything that will reach a customer:
- Never include a file path, component or function name, translation key, \
branch, commit or ClickUp task id. Describe the product in the words the \
customer sees.
- Never say something is fixed, safe, on the roadmap or coming unless \
check_existing_work shows a record that backs it. A logged request may be \
described only as "logged with the product team".
- When a source says NOT searched, the answer is unknown, not no.
- What exists on the live branch can still be hidden by plan or role.
- No em dashes. Use periods, commas, parentheses, or "and".
- These tools never send anything to a customer on their own. A person \
sends: send_ticket_reply runs only on Sam's explicit word, passed verbatim.

Support sprint (Sam): sprint_queue lists every open ticket with the agents' \
first pass; walk_queue shows every New or Processing ticket and who has \
it, with no agent run; ticket_first_pass runs the triage agent on one; \
fix_status and sprint_followups show agent fixes and what is owed after a \
release; propose_fix, send_ticket_reply, route_queue, route_ticket, \
close_ticket_quietly and log_ticket_request act, Sam only. Show every reply \
in full before sending.
"""

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False,
    open_world_hint=True,
)

MCP_EXACT_PATHS = frozenset({
    config.MCP_PATH,
    config.MCP_PATH + "/",
    "/authorize",
    "/token",
    "/register",
    "/revoke",
    "/.well-known/oauth-authorization-server",
    config.GOOGLE_CALLBACK_PATH,
})
MCP_PREFIXES = ("/.well-known/oauth-protected-resource",)

_server: MCPServer | None = None
_asgi = None


def caller() -> str:
    token = get_access_token()
    return (token.subject or "") if token else ""


async def _run(name: str, fn, **arguments):
    """Run one tool in a thread, audited. RequestError becomes a refusal
    the model can read and relay; anything else is logged as a crash."""
    who = caller()
    started = time.monotonic()
    ok, error = False, ""
    try:
        result = await anyio.to_thread.run_sync(lambda: fn(**arguments))
        ok = True
        return result
    except RequestError as exc:
        error = str(exc)
        raise ToolError(error) from exc
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        print(f"[MCP] {name} failed for {who}: {error}")
        raise
    finally:
        await anyio.to_thread.run_sync(
            lambda: store.record_call(
                who, name, arguments, ok,
                int((time.monotonic() - started) * 1000), error,
            )
        )


def build() -> MCPServer:
    provider = GoogleOAuthProvider()
    server = MCPServer(
        name="vome-os",
        title="Vome OS",
        description=(
            "How the Vome platform works, where things are, what is already "
            "logged, and the code behind it. For Vome staff."
        ),
        instructions=INSTRUCTIONS,
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=config.public_url(),
            resource_server_url=config.resource_url(),
            validate_token_resource=True,
            required_scopes=[config.SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=[config.SCOPE],
                default_scopes=[config.SCOPE],
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
    )

    @server.custom_route(config.GOOGLE_CALLBACK_PATH, methods=["GET"])
    async def google_callback(request: Request):
        return await provider.google_callback(request)

    # -- product knowledge -------------------------------------------------

    @server.tool(annotations=READ)
    async def lookup_feature(
        question: Annotated[str, Field(
            description="The customer's question in plain words, e.g. "
            "'export only the filtered volunteers'"
        )],
        language: Annotated[Literal["en", "fr"], Field(
            description="Help centre language to search"
        )] = "en",
    ) -> dict:
        """Does a feature exist, what is it called, how does it work, and
        what are the known catches. Searches the help centre, the Setup
        Guide, the product's own UI text on the live branch, the Sprint
        Lessons Log and lessons from closed tickets. Start here."""
        return await _run(
            "lookup_feature", tools.lookup_feature,
            question=question, language=language,
        )

    @server.tool(annotations=READ)
    async def where_in_app(
        question: Annotated[str, Field(
            description="What the customer is trying to reach, e.g. "
            "'approve hour claims'"
        )],
    ) -> dict:
        """Which page in the web app holds a feature, and the exact text of
        the buttons and hints on that page, from the live frontend."""
        return await _run("where_in_app", tools.where_in_app,
                          question=question)

    @server.tool(annotations=READ)
    async def check_existing_work(
        query: Annotated[str, Field(
            description="The symptom or request in a few words, e.g. "
            "'kiosk check-in timezone'"
        )],
    ) -> dict:
        """Whether something is already logged in the product backlog, and
        its real status: shipped, in flight, sleeping or declined. Use
        before telling a customer anything is fixed, known or planned."""
        return await _run("check_existing_work", tools.check_existing_work,
                          query=query)

    # -- code ----------------------------------------------------------------

    @server.tool(annotations=READ)
    async def list_repositories() -> dict:
        """The repositories reachable, what each one is, and the branch
        production runs."""
        return await _run("list_repositories", tools.list_repositories)

    @server.tool(annotations=READ)
    async def search_code(
        query: Annotated[str, Field(
            description="A string, symbol, setting name or error message"
        )],
        repo: Annotated[str, Field(
            description="Optional: core, database, web, mobile, chats, "
            "integrations, or a full repository name. Empty searches all."
        )] = "",
    ) -> dict:
        """Find where something appears in the code. For when the product
        sources do not settle how a feature really behaves. Internal only."""
        return await _run("search_code", tools.search_code,
                          query=query, repo=repo)

    @server.tool(annotations=READ)
    async def read_code_file(
        repo: Annotated[str, Field(
            description="core, database, web, mobile, chats, integrations, "
            "or a full repository name"
        )],
        path: Annotated[str, Field(
            description="Path inside the repository, from search_code"
        )],
        ref: Annotated[str, Field(
            description="Branch or commit. Leave empty for the branch "
            "production runs."
        )] = "",
        start_line: Annotated[int, Field(ge=0)] = 0,
        end_line: Annotated[int, Field(ge=0)] = 0,
    ) -> dict:
        """Read one file, or list a directory, from the live branch.
        Internal only."""
        return await _run(
            "read_code_file", tools.read_code_file, repo=repo, path=path,
            ref=ref, start_line=start_line, end_line=end_line,
        )

    # -- feature requests ----------------------------------------------------

    @server.tool(annotations=WRITE)
    async def log_feature_request(
        title: Annotated[str, Field(
            description="Short name for the capability, e.g. 'Recurring "
            "shifts that skip public holidays'"
        )],
        problem: Annotated[str, Field(
            description="What the customer is trying to do and why, in "
            "their terms"
        )],
        requested_by: Annotated[str, Field(
            description="The customer's organization name"
        )],
        ticket_url: Annotated[str, Field(
            description="Zoho ticket link, if there is one"
        )] = "",
        platform: Annotated[Literal["", "web", "mobile", "both"], Field(
            description="Where they need it"
        )] = "",
        module: Annotated[str, Field(
            description="Product area, e.g. 'admin scheduling', 'forms', "
            "'hour tracking', 'chat', 'reports'"
        )] = "",
        workaround: Annotated[str, Field(
            description="Any workaround the customer was given"
        )] = "",
        confirm_new: Annotated[bool, Field(
            description="Set true only after reviewing possible_duplicates "
            "and deciding none of them is this request"
        )] = False,
    ) -> dict:
        """Log a feature request in the product backlog (ClickUp Raw
        Intake), untriaged. Checks for existing requests first and creates
        nothing if any look similar, unless confirm_new is true."""
        return await _run(
            "log_feature_request", tools.log_feature_request,
            title=title, problem=problem, requested_by=requested_by,
            logged_by=caller(), ticket_url=ticket_url, platform=platform,
            module=module, workaround=workaround, confirm_new=confirm_new,
        )

    @server.tool(annotations=WRITE)
    async def add_request_to_existing(
        task_id: Annotated[str, Field(
            description="task_id from check_existing_work or "
            "log_feature_request's possible_duplicates"
        )],
        requested_by: Annotated[str, Field(
            description="The customer's organization name"
        )],
        note: Annotated[str, Field(
            description="Anything new this customer adds to the request"
        )] = "",
        ticket_url: Annotated[str, Field(
            description="Zoho ticket link, if there is one"
        )] = "",
    ) -> dict:
        """Record another customer asking for something already in the
        backlog: a +1 comment, and their organization added to Requesting
        Clients."""
        return await _run(
            "add_request_to_existing", tools.add_request_to_existing,
            task_id=task_id, requested_by=requested_by, logged_by=caller(),
            note=note, ticket_url=ticket_url,
        )

    @server.tool(annotations=WRITE)
    async def triage_feature_request(
        task_id: Annotated[str, Field(description="The ClickUp task id")],
        decision: Annotated[Literal[
            "build_now", "priority_queue", "accepted_backlog", "sleeping",
            "declined", "keep_in_intake",
        ], Field(description=(
            "Where the request goes. build_now is Sam Accepted (Build "
            "Now): Sam builds it himself, soon"))],
        priority: Annotated[Literal["", "urgent", "high", "normal", "low"],
                            Field(description="Leave empty to not change")
                            ] = "",
        assign_to_me: bool = False,
        note: Annotated[str, Field(
            description="Optional reasoning, left as a comment on the task"
        )] = "",
    ) -> dict:
        """Prioritise and place a feature request. Only Sam can use this;
        anyone else gets a refusal."""
        return await _run(
            "triage_feature_request", tools.triage_feature_request,
            task_id=task_id, decision=decision, priority=priority,
            assign_to_me=assign_to_me, note=note, caller=caller(),
        )

    # -- help centre (the /faq workflow) -----------------------------------

    @server.tool(annotations=WRITE)
    async def run_faq(
        request: Annotated[str, Field(
            description="What the article should cover, e.g. 'new guest "
            "form submissions toggle' or 'update the kiosk PIN article for "
            "the new reset flow'"
        )] = "",
        developer_notes: Annotated[str, Field(
            description="Raw notes on what shipped: behaviour, limits, "
            "defaults, plan, who can do it. Also where to put answers to "
            "the writer's questions."
        )] = "",
        article: Annotated[str, Field(
            description="To update an existing article: its id, portal URL "
            "or permalink. Empty lets the writer decide create or update."
        )] = "",
        revise_job_id: Annotated[str, Field(
            description="To revise a previous draft instead: its job_id"
        )] = "",
        feedback: Annotated[str, Field(
            description="With revise_job_id: what to change"
        )] = "",
    ) -> dict:
        """Hand a help centre article to the Vome OS writer (the /faq
        workflow): it gathers the product facts, checks what already
        exists, and drafts English and French for review. Returns a job_id;
        nothing is published until publish_faq_draft."""
        return await _run(
            "run_faq", tools.run_faq, request=request,
            developer_notes=developer_notes, article=article,
            revise_job_id=revise_job_id, feedback=feedback, caller=caller(),
        )

    @server.tool(annotations=READ)
    async def faq_job_status(
        job_id: Annotated[str, Field(
            description="From run_faq. Empty lists recent jobs."
        )] = "",
    ) -> dict:
        """The state of a help centre job: still drafting, the writer's
        questions, or the full English and French draft with the rule
        check, ready for review."""
        return await _run("faq_job_status", tools.faq_job_status,
                          job_id=job_id)

    @server.tool(annotations=WRITE)
    async def publish_faq_draft(
        job_id: Annotated[str, Field(description="A ready draft's job_id")],
        as_draft: Annotated[bool, Field(
            description="True saves it in Zoho as a Draft (not public) "
            "instead of publishing"
        )] = False,
    ) -> dict:
        """Publish a reviewed draft to the help centre: English article and
        French translation, permission ALL, then read both back from Zoho
        and return the live portal URLs. Refuses anything that fails the
        rule check."""
        return await _run(
            "publish_faq_draft", tools.publish_faq_draft, job_id=job_id,
            as_draft=as_draft, caller=caller(),
        )

    @server.tool(annotations=WRITE)
    async def save_faq_draft(
        en_title: str,
        en_permalink: str,
        en_html: str,
        fr_title: str,
        fr_permalink: str,
        fr_html: str,
        article_id: Annotated[str, Field(
            description="Set to update that article; empty creates a new one"
        )] = "",
        category_id: Annotated[str, Field(
            description="Required for a new article; from list_kb_categories"
        )] = "",
        en_seo_title: str = "",
        en_seo_description: str = "",
        fr_seo_title: str = "",
        fr_seo_description: str = "",
        based_on_job: str = "",
        note: str = "",
    ) -> dict:
        """Save an edited or hand-written article (raw HTML, both
        languages) as a reviewable draft. It goes through the same rule
        check and the same publish_faq_draft step as a writer's draft."""
        fields = {
            "article_id": article_id, "category_id": category_id,
            "based_on_job": based_on_job, "note": note,
            "en": {"title": en_title, "permalink": en_permalink,
                   "answer_html": en_html, "seo_title": en_seo_title,
                   "seo_description": en_seo_description},
            "fr": {"title": fr_title, "permalink": fr_permalink,
                   "answer_html": fr_html, "seo_title": fr_seo_title,
                   "seo_description": fr_seo_description},
        }
        return await _run("save_faq_draft", tools.save_faq_draft,
                          fields=fields, caller=caller())

    @server.tool(annotations=READ)
    async def get_kb_article(
        article: Annotated[str, Field(
            description="Article id, portal URL or permalink"
        )],
    ) -> dict:
        """One help centre article in English and French exactly as Zoho
        holds it now (HTML, status, permission, portal URLs)."""
        return await _run("get_kb_article", tools.get_kb_article,
                          article=article)

    @server.tool(annotations=READ)
    async def list_kb_categories() -> dict:
        """Help centre categories that accept articles, with ids."""
        return await _run("list_kb_categories", tools.list_kb_categories)

    # -- the support sprint (sprint_tools.py) -------------------------------
    # The agents' first pass on every ticket, and Sam's actions on it, over
    # the same guarded paths as the sprint thread. Writes are Sam only.

    import sprint_tools

    @server.tool(annotations=READ)
    async def sprint_queue(
        include_finished: Annotated[bool, Field(
            description="Also list items decided in the last day")] = False,
    ) -> dict:
        """Every open support ticket with the agents' first pass, grouped:
        ready to send (full drafts), fix first, needs a decision, close
        quietly, nothing owed, held. Start a sprint here."""
        return await _run("sprint_queue", sprint_tools.sprint_queue,
                          include_finished=include_finished)

    @server.tool(annotations=READ)
    async def ticket_first_pass(
        ticket: Annotated[str, Field(description="Ticket number, e.g. 9500")],
        refresh: Annotated[bool, Field(
            description="Run the triage agent again even if the card is "
            "current")] = False,
    ) -> dict:
        """The agents' first pass on one ticket: category, lane, the code
        check on the live branch (bug, works as designed, or design
        question), the recommended action and the draft. Runs the triage
        director again when the card is missing or out of date."""
        return await _run("ticket_first_pass", sprint_tools.ticket_first_pass,
                          ticket=ticket, refresh=refresh)

    @server.tool(annotations=READ)
    async def fix_status(
        ticket: Annotated[str, Field(description="Ticket number")],
    ) -> dict:
        """Where an agent fix for a ticket stands (PR open, merged to dev,
        live, resolution drafted) and whose move it is."""
        return await _run("fix_status", sprint_tools.fix_status,
                          ticket=ticket)

    @server.tool(annotations=READ)
    async def walk_queue() -> dict:
        """Every Zoho ticket at New or Processing (each one owes the client
        a reply) and what happens to it now: routed to an engineer, with
        the agent's fix, or Sam's call. Read-only and runs no agent: it
        uses the cards triage already wrote."""
        return await _run("walk_queue", sprint_tools.walk_queue)

    @server.tool(annotations=WRITE)
    async def route_queue(
        tickets: Annotated[str, Field(
            description="Only these ticket numbers, e.g. '9500 9501'. Empty "
            "routes everything walk_queue lists under urgent and route")
        ] = "",
    ) -> dict:
        """File or join the engineer task for the tickets walk_queue says
        to route (Sam only). Urgent ones ping the engineer's channel. Sends
        nothing to any client and leaves Zoho statuses alone."""
        return await _run("route_queue", sprint_tools.route_queue,
                          caller=caller(), tickets=tickets)

    @server.tool(annotations=READ)
    async def sprint_followups() -> dict:
        """Follow-ups since the last sprint: replies owed for fixes and
        features that shipped, agent PRs waiting on Sam's merge, fixes
        waiting on production, failed release checks."""
        return await _run("sprint_followups", sprint_tools.sprint_followups)

    @server.tool(annotations=WRITE)
    async def propose_fix(
        ticket: Annotated[str, Field(description="Ticket number")],
    ) -> dict:
        """Ask the agent to write the fix and open a PR into the dev branch
        (Sam only). Only low-risk fixes are opened; never merged by an
        agent. Check the result with fix_status."""
        return await _run("propose_fix", sprint_tools.propose_fix,
                          ticket=ticket, caller=caller())

    @server.tool(annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=False,
        open_world_hint=True))
    async def send_ticket_reply(
        ticket: Annotated[str, Field(description="Ticket number")],
        sam_said: Annotated[str, Field(
            description="Sam's own words approving this send, verbatim")],
        text: Annotated[str, Field(
            description="Sam's edited reply. Empty sends the agent's draft "
            "exactly as shown")] = "",
    ) -> dict:
        """Email the customer (Sam only, on his explicit word). Every
        outbound guard runs again, recipients are the latest sender, their
        cc and the ticket's secondary contacts, and it refuses if the
        customer wrote since the draft. Zoho status and Agent Fixes follow.
        An edit teaches the drafter."""
        return await _run("send_ticket_reply",
                          sprint_tools.send_ticket_reply, ticket=ticket,
                          sam_said=sam_said, text=text, caller=caller())

    @server.tool(annotations=WRITE)
    async def route_ticket(
        ticket: Annotated[str, Field(description="Ticket number")],
        engineer: Annotated[Literal["onlyg", "sanjay"], Field(
            description="onlyg: backend (data, API, auth, notifications, "
            "imports). sanjay: frontend and mobile")],
    ) -> dict:
        """Hand one ticket to an engineer (Sam only), when its `engineer`
        field is empty: Priority Queue task with the agent's findings and a
        private Zoho note. Sends nothing to the customer and leaves the Zoho
        status alone (New or Processing means Sam's reply is still owed);
        Slack only when urgent. Prefer route_queue for several."""
        return await _run("route_ticket", sprint_tools.route_ticket,
                          ticket=ticket, engineer=engineer, caller=caller())

    @server.tool(annotations=WRITE)
    async def close_ticket_quietly(
        ticket: Annotated[str, Field(description="Ticket number")],
    ) -> dict:
        """Close a ticket in Zoho with no closure email, when nothing is
        owed (Sam only)."""
        return await _run("close_ticket_quietly",
                          sprint_tools.close_ticket_quietly, ticket=ticket,
                          caller=caller())

    @server.tool(annotations=WRITE)
    async def log_ticket_request(
        ticket: Annotated[str, Field(description="Ticket number")],
        build: Annotated[bool, Field(description=(
            "True when Sam will build it himself, soon (build, build now, "
            "I'll take it, I want to handle this): Sam Accepted (Build "
            "Now), assigned to Sam, high priority. False when it is for "
            "later (log, log it, backlog, future, not now): Raw Intake, "
            "unassigned. Saying log while also saying he will handle it "
            "himself means True. Unclear: ask 'build now or log for "
            "later?'"))] = False,
        existing_task_id: Annotated[str, Field(
            description=("Add this client to an existing request instead; "
                         "with build, that task also moves to Sam Accepted"))
        ] = "",
        confirm_new: Annotated[bool, Field(
            description="Log it even though similar requests exist")] = False,
        title: Annotated[str, Field(description=(
            "Short name for the capability in product words, e.g. 'Shared "
            "and bulk settings for sequences and forms'. Not the client's "
            "question"))] = "",
        module: Annotated[str, Field(description=(
            "Product area, e.g. 'sequences', 'forms', 'admin scheduling'"))
        ] = "",
    ) -> dict:
        """Log a ticket's feature request (Sam only), build now or for
        later: checks for an existing request first, and posts the decision
        to #vome-feature-requests with the client. Tells the customer
        nothing."""
        return await _run("log_ticket_request",
                          sprint_tools.log_ticket_request, ticket=ticket,
                          build=build, existing_task_id=existing_task_id,
                          confirm_new=confirm_new, title=title,
                          module=module, caller=caller())

    @server.tool(annotations=WRITE)
    async def teach_agents(
        rule: Annotated[str, Field(
            description="One imperative sentence the agent should follow "
            "from now on, e.g. 'In a resolution reply, never mention an "
            "earlier reply.'")],
        agent: Annotated[Literal["drafter", "triage", "partner",
                                 "incidents", "all"], Field(
            description="drafter writes client replies; triage writes the "
            "card and code check")],
        sam_said: Annotated[str, Field(
            description="Sam's own words, verbatim")],
        ticket: Annotated[str, Field(
            description="The ticket that prompted it, if any")] = "",
    ) -> dict:
        """Record Sam's general correction as a live lesson for an agent
        (Sam only). Use for rules that apply beyond one ticket; for one
        reply, edit it with send_ticket_reply instead."""
        return await _run("teach_agents", sprint_tools.teach_agents,
                          rule=rule, agent=agent, sam_said=sam_said,
                          ticket=ticket, caller=caller())

    @server.tool(annotations=READ)
    async def connector_status() -> dict:
        """Which sources this connector can reach right now, and who you
        are signed in as. Use when results look thin."""
        return await _run("connector_status", tools.connector_status,
                          caller=caller())

    return server


class _Dispatch:
    """Send MCP and OAuth paths to the MCP app, everything else onward."""

    def __init__(self, app, mcp_app):
        self.app = app
        self.mcp_app = mcp_app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path in MCP_EXACT_PATHS or path.startswith(MCP_PREFIXES):
                if path == config.MCP_PATH + "/":
                    scope = dict(scope, path=config.MCP_PATH)
                await self.mcp_app(scope, receive, send)
                return
        await self.app(scope, receive, send)


def attach(app) -> bool:
    """Put the connector in front of `app`. False when not configured."""
    global _server, _asgi
    if not config.enabled():
        print(
            "[MCP] connector OFF: set MCP_PUBLIC_URL, MCP_GOOGLE_CLIENT_ID "
            "and MCP_GOOGLE_CLIENT_SECRET to enable it"
        )
        return False
    _server = build()
    _asgi = _server.streamable_http_app(
        streamable_http_path=config.MCP_PATH,
        stateless_http=True,
        json_response=True,
        host="0.0.0.0",
    )
    app.add_middleware(_Dispatch, mcp_app=_asgi)
    print(f"[MCP] connector ON at {config.resource_url()}")
    return True


@contextlib.asynccontextmanager
async def lifespan(app):
    """Run the MCP session manager for the life of the web process."""
    if _server is None:
        yield
        return
    # Warm the frontend source off the request path, so the first person to
    # ask a question after a deploy does not wait for a download.
    threading.Thread(
        target=product_source.ensure_fresh, name="mcp-source", daemon=True,
    ).start()
    async with _server.session_manager.run():
        yield

"""
vic_tools.py

The two VomeOS lookups Vic may use while chatting with a client, and nothing
else.

Staff get the full VomeOS through the MCP connector: code search, file
reads, ClickUp, internal caveats. Vic answers clients live with nobody
reviewing first, so it gets a narrower view of the same answers:

  * Only lookup_feature and where_in_app. No code, no ClickUp, no tickets.
    run_tool dispatches by an allowlist, so a tool name the model invents
    gets nothing.
  * Results are rebuilt from an allowlist of fields: help centre articles,
    Setup Guide text and the words on the product's own screens.
    Translation keys, file paths, branches and the team's internal caveats
    are never copied across, so they cannot end up in a reply.
  * Any item that carries security or internal detail (the catalog's
    compliance line, for one) is dropped by vic_security.safe_text.
  * Never waits on a frontend download. Django gives a turn 30 seconds, so
    a stale copy is used while a refresh runs in the background.
"""

from __future__ import annotations

import json

from vic_security import safe_text

TOOLS = [
    {
        "name": "lookup_feature",
        "description": (
            "Search Vome's help centre, in-app Setup Guide and the words on "
            "the product's own screens for how a feature works. Use it for "
            "any 'how do I', 'where do I' or 'can Vome' question before "
            "answering or handing off to the team."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The client's question, in plain words.",
                },
                "language": {"type": "string", "enum": ["en", "fr"]},
            },
            "required": ["question"],
        },
    },
    {
        "name": "where_in_app",
        "description": (
            "Find which page of the Vome web app holds a feature, and the "
            "exact wording of the buttons and labels on it. Use it to turn "
            "an answer into 'go to X, click Y' steps."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "What the client is trying to do.",
                },
            },
            "required": ["question"],
        },
    },
]

_HOW_TO_USE = (
    "Use only what is here. Describe pages and buttons by the words the "
    "client sees. What exists here may still be hidden by the client's "
    "plan or role. If nothing here answers the question, say you will pass "
    "it to the team instead of guessing."
)


def _keep(items: list[dict], *fields: str) -> list[dict]:
    kept = []
    for item in items or []:
        row = {f: item.get(f) for f in fields if item.get(f)}
        if row and safe_text(" ".join(str(v) for v in row.values())):
            kept.append(row)
    return kept


def lookup_feature(question: str, language: str = "en") -> dict:
    from mcp_connector import tools

    raw = tools.lookup_feature(question, language, wait_for_source=False)
    labels = [
        h["text"] for h in raw.get("ui_text") or []
        if h.get("text") and safe_text(h["text"])
    ]
    return {
        "help_centre": _keep(
            raw.get("help_centre"), "title", "url", "excerpt"),
        "setup_guide": [
            {"section": h["section"], "text": h["text"]}
            for h in _keep(
                raw.get("setup_guide_and_catalog"), "section", "text")
            if "text" in h and "section" in h
        ],
        "screen_text": labels[:10],
        "how_to_use_this": _HOW_TO_USE,
    }


def where_in_app(question: str) -> dict:
    from mcp_connector import tools

    raw = tools.where_in_app(question, wait_for_source=False)
    pages = []
    for page in raw.get("pages") or []:
        words = [
            t for t in page.get("matched_text") or []
            if t and safe_text(t)
        ]
        words += [
            entry["text"] for entry in page.get("text_on_this_screen") or []
            if entry.get("text") and safe_text(entry["text"])
        ]
        if words:
            pages.append({"text_on_this_page": words[:14]})
    return {"pages": pages, "how_to_use_this": _HOW_TO_USE}


_DISPATCH = {
    "lookup_feature": lambda args: lookup_feature(
        str(args.get("question") or ""),
        "fr" if args.get("language") == "fr" else "en",
    ),
    "where_in_app": lambda args: where_in_app(
        str(args.get("question") or "")),
}


def run_tool(name: str, args: dict) -> str:
    """The JSON a tool_result carries back to Vic. Never raises, and never
    passes an exception's text on: it can name files and hosts."""
    handler = _DISPATCH.get(name)
    if handler is None:
        return json.dumps({"error": "unknown tool"})
    try:
        return json.dumps(handler(args if isinstance(args, dict) else {}))
    except Exception as e:
        print(f"[VIC-TOOLS] {name} failed: {e}")
        return json.dumps({
            "error": "lookup unavailable",
            "how_to_use_this": (
                "The lookup did not run, so this is unknown, not a no. "
                "Hand the question to the team."
            ),
        })

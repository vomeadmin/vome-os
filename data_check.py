"""
data_check.py

Some tickets cannot be settled from the code. The answer is in one
account's records: whether a file was saved, which request is open, what an
import wrote. Triage reads code only, so on those tickets it either guessed
or sent the ticket to an engineer to look the record up.

WHY THIS EXISTS
---------------
#9760 (UMMS, 2026-11-05): the code check found a real bug (an admin's
replacement upload never closed the open request) with high confidence, an
agent PR shipped, and the client's case was not fixed. Three read queries
in the prod shell showed the uploads had never saved at all. A plausible
cause was treated as the confirmed one because nothing on the card asked
"is this what happened to these people?". Before that, a card that knew it
needed a record could only say `engineer` + `data_lookup`, which routed to
OnlyG by default and never showed Sam the question.

THE CARD (skill checking-the-data)
----------------------------------
    "data_check": {
      "needed": "none" | "confirm" | "required",
      "why": what the code cannot show,
      "hypothesis": what the records show if the code check is right,
      "questions": the facts to look up, one per line, by name,
      "query": paste-ready lines for the prod Django shell,
      "settles": what each answer means for the reply or the fix
    }

WHAT EACH LEVEL DOES
--------------------
required  No honest answer without the records. Never routed to an engineer
          on its own, no agent fix PR, the batch shows it under DATA CHECK
          FIRST with the query, and the draft may only be a holding reply.
confirm   The code found a plausible cause, but the ticket names specific
          records. The fix PR may open (the code is wrong regardless); the
          automatic "fixed" email waits until the check is done
          (resolution_scope).
none      Nothing changes.

THE LOOP
--------
Sam runs the query in the prod Django shell (AWS Session Manager into the
core box, read replica only) and pastes the output back: `data N: <output>`
in the sprint thread, or data_check_result from Claude.ai. The output is
kept on the sprint item (it survives every re-triage) and the ticket is
triaged again with it as evidence, always with the deep pass. A card that
the data settles comes back with `needed: none` and `found` filled in.

SAFETY
------
The agents never connect to a database. They write read-only queries that
Sam reads before he runs them, and `safe_query` drops any line that could
write, call out, or run a command, and points every manager at the
read-only alias. What Sam pastes back stays internal: it reaches the triage
agents (as the thread already does) and never the drafter or a client.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone

NONE, CONFIRM, REQUIRED = "none", "confirm", "required"
LEVELS = (NONE, CONFIRM, REQUIRED)
PENDING = (CONFIRM, REQUIRED)

# The steps before any query (feedback: paste blocks start with setup).
SETUP = (
    "sudo su - ubuntu\n"
    "cd /home/ubuntu/webapps/projects/vomedjango\n"
    "source /home/ubuntu/webapps/projects/venv/bin/activate\n"
    "python manage.py shell"
)
WHERE_TO_RUN = ("AWS console, ca-central-1, EC2 > prod core (3.97.96.148) > "
                "Connect > Session Manager")

# What one paste may hold, and how many are kept per item.
RESULT_CHARS = 12000
KEEP_RESULTS = 5
CONTEXT_CHARS = 20000

# A line that matches any of these is dropped from a query: it writes,
# deletes, sends, schedules, or reaches outside the shell.
_UNSAFE = re.compile(
    r"\.(save|delete|update|create|get_or_create|update_or_create|"
    r"bulk_create|bulk_update|add|remove|clear|set|send|delay|apply_async|"
    r"execute|executemany|raw|write|unlink)\s*\("
    r"|\b(import|from)\s+(os|sys|subprocess|shutil|socket|requests|httpx|"
    r"urllib|boto3|smtplib|pathlib)\b"
    r"|\b(exec|eval|compile|open|__import__|call_command|send_mail|"
    r"cursor)\s*\("
    r"|\bconnections?\s*[.\[]"
    r"|\bimport\b.*\bconnections?\b",
    re.I,
)
_MANAGER = re.compile(r"\.objects\.(?!using\()")


def of(card: dict | None) -> dict:
    """The card's data_check, always a dict with a valid `needed`."""
    raw = (card or {}).get("data_check") if isinstance(card, dict) else None
    check = dict(raw) if isinstance(raw, dict) else {}
    needed = str(check.get("needed") or "").strip().lower()
    check["needed"] = needed if needed in LEVELS else NONE
    questions = check.get("questions")
    if isinstance(questions, str):
        questions = [q for q in questions.splitlines() if q.strip()]
    check["questions"] = [str(q).strip() for q in questions or []
                          if str(q).strip()]
    return check


def level(card: dict | None) -> str:
    return of(card)["needed"]


def pending(item_or_card: dict | None) -> bool:
    """A check the card asks for and nobody has answered yet. An answered
    check comes back from re-triage as `none`, so the card decides."""
    target = item_or_card or {}
    card = target.get("card") if "card" in target else target
    return level(card) in PENDING


def safe_query(text: str) -> tuple[str, list[str]]:
    """(query to show Sam, lines dropped). Every manager reads the replica."""
    kept, dropped = [], []
    for line in str(text or "").replace("```", "").splitlines():
        if not line.strip():
            continue
        if _UNSAFE.search(line):
            dropped.append(line.strip())
            continue
        kept.append(_MANAGER.sub('.objects.using("readonly").', line.rstrip()))
    return "\n".join(kept), dropped


def paste_block(card: dict | None) -> str:
    """Setup, then the safe query, as one block Sam can copy."""
    query, _ = safe_query(of(card).get("query") or "")
    return f"{SETUP}\n\n{query}" if query else ""


def clean_paste(text: str) -> str:
    """What Sam pasted, without Slack's markup: <mailto:a@b|a@b>, <url|x>,
    &amp; and the code fences around it."""
    text = html.unescape(str(text or ""))
    text = re.sub(r"<mailto:[^|>]+\|([^>]+)>", r"\1", text)
    text = re.sub(r"<(https?://[^|>]+)\|[^>]+>", r"\1", text)
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
    return text.replace("```", "").strip()


def entry(text: str, by: str) -> dict:
    cleaned = clean_paste(text)
    if len(cleaned) > RESULT_CHARS:
        cleaned = cleaned[:RESULT_CHARS] + "\n(cut: the paste was longer)"
    return {"at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "by": by, "text": cleaned}


def results(item: dict | None) -> list[dict]:
    raw = (item or {}).get("data_results")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = []
    return [r for r in raw or [] if isinstance(r, dict) and r.get("text")]


def context_text(item: dict | None) -> str:
    """The pasted results as the triage agents read them, newest last."""
    found = results(item)
    if not found:
        return ""
    blocks = []
    for result in found:
        blocks.append(f"Sam ran the data check on {result.get('at')} and "
                      f"pasted this output from the production read "
                      f"replica:\n{result.get('text')}")
    text = "\n\n".join(blocks)
    return text[-CONTEXT_CHARS:]


def record(item: dict, text: str, by: str = "sam") -> dict:
    """Keep the paste on the item. Returns the stored entry, or {}."""
    import sprint_store

    if not clean_paste(text):
        return {}
    stored = entry(text, by)
    sprint_store.add_data_result(item["id"], stored, keep=KEEP_RESULTS)
    return stored


def retriage(item: dict, reply_thread: str = "") -> bool:
    """Queue the ticket for triage again with the pasted results (the deep
    pass always runs on new data). Force: Sam asked, even on a closed
    ticket. `reply_thread`: the Slack thread the new card is posted in."""
    try:
        from vomeos.worker import enqueue_event

        import shadow_triage

        enqueue_event(shadow_triage.HANDLER_KEY,
                      {"ticket_id": str(item.get("ticket_id")),
                       "reason": "data", "refresh": True, "force": True,
                       "reply_thread": reply_thread},
                      queue="support")
        return True
    except Exception as exc:
        print(f"[DATA] could not queue #{item.get('ticket_number')}: {exc}")
        return False


def take(item: dict, text: str, by: str = "sam",
         reply_thread: str = "") -> dict:
    """Record a paste and re-triage. {"ok", "lines", "queued"}."""
    stored = record(item, text, by)
    if not stored:
        return {"ok": False, "lines": 0, "queued": False}
    return {"ok": True, "lines": len(stored["text"].splitlines()),
            "queued": retriage(item, reply_thread)}


def view(item: dict) -> dict:
    """The data check for Claude.ai: what to run and what came back."""
    check = of(item.get("card"))
    if check["needed"] == NONE and not results(item):
        return {}
    query, dropped = safe_query(check.get("query") or "")
    out = {"needed": check["needed"], "why": check.get("why") or "",
           "hypothesis": check.get("hypothesis") or "",
           "questions": check["questions"],
           "where_to_run": WHERE_TO_RUN, "setup": SETUP, "query": query,
           "settles": check.get("settles") or "",
           "found": check.get("found") or "",
           "results_pasted": [r.get("at") for r in results(item)]}
    if dropped:
        out["dropped_lines"] = dropped
    return out


def slack_lines(item: dict) -> list[str]:
    """The data check in a batch detail block, or [] when none is owed."""
    check = of(item.get("card"))
    if check["needed"] not in PENDING:
        found = check.get("found")
        return [f"*Data check:* done. {found}"] if found and results(
            item) else []
    n = item.get("number")
    head = ("required: no honest answer without the records"
            if check["needed"] == REQUIRED else
            "confirm before the fixed email: the code found a likely "
            "cause, the records say whether it is theirs")
    lines = [f":mag: *Data check ({head}).* {check.get('why') or ''}".rstrip()]
    if check.get("hypothesis"):
        lines.append(f"*If the code is right:* {check['hypothesis']}")
    if check["questions"]:
        lines.append("*Look up:*\n" + "\n".join(
            f"• {q}" for q in check["questions"][:8]))
    query, dropped = safe_query(check.get("query") or "")
    if query:
        lines.append(f"Prod shell ({WHERE_TO_RUN}), setup first:")
        lines.append("```" + SETUP + "```")
        lines.append("Then in the Django shell:")
        lines.append("```" + query + "```")
    if dropped:
        lines.append(f":warning: Left out {len(dropped)} line(s) that could "
                     "write; the rest is read-only.")
    if check.get("settles"):
        lines.append(f"*What it settles:* {check['settles']}")
    if results(item):
        lines.append(f"_Results pasted {results(item)[-1].get('at')}; the "
                     "card above is from before them if this still shows._")
    lines.append(f"`data {n}: <paste the output>` triages it again with the "
                 "results.")
    return lines

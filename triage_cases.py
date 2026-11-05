"""
triage_cases.py

Turn a ticket Sam re-triaged by hand into an answer-key case for the triage
agents (stored in vomeos_eval_cases; see vomeos/eval_cases.py for why).

When Sam opens a ticket in Claude Code and disagrees with its card, the
category, lane and code-check verdict he settles on are the right answer.
This records them against the ticket's real context, for the first pass
(triage_director) and, when a card was stored, for the deep pass
(triage_investigator) with the card the first pass actually wrote. Then
`py -m vomeos.evaluate customer_success.triage_investigator` scores the
agents on exactly the tickets that failed him.

    railway run -s vome-os-worker py triage_cases.py add --ticket 9586 \\
        --expect category=clear_bug lane=engineer code_check.verdict=bug \\
        --note "kiosk hides evening shifts after 17h"
    railway run -s vome-os-worker py triage_cases.py list
    railway run -s vome-os-worker py triage_cases.py remove 12

It needs the worker's environment: Zoho to read the ticket, the database to
store the case.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from vomeos import eval_cases, store

TRIAGE = "customer_success.triage_director"
INVESTIGATOR = "customer_success.triage_investigator"


def parse_expect(pairs: list[str]) -> dict:
    expect = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"expected key=value, got {pair!r}")
        value = value.strip()
        if "|" in value:
            # data_check.needed=confirm|required: either answer agrees.
            expect[key.strip()] = [v.strip() for v in value.split("|")
                                   if v.strip()]
            continue
        expect[key.strip()] = {"true": True, "false": False}.get(
            value.lower(), value)
    return expect


# "[2026-10-30 13:57] Laurel: ..." (hand-written cases) or the live
# format, "--- Laurel Gaffney [in] -- 2026-10-30T13:57:15.000Z ---".
_STAMP = re.compile(r"^\[(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2})")
_HEADER = re.compile(r"^--- .* -- (\S*) ---\s*$")
_ISO = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})")


def thread_before(thread: str, before: str) -> str:
    """The thread as it stood before `before` ("2026-11-01" or
    "2026-11-01 12:00"): every message stamped at or after it is cut, so a
    case can judge the ticket the way triage first saw it. A message with
    no date (an internal note) is cut too: it may be from later."""
    if not before:
        return thread
    kept, cutting = [], False
    for line in (thread or "").splitlines():
        header = _HEADER.match(line)
        stamp = _STAMP.match(line)
        if header:
            found = _ISO.search(header.group(1))
            cutting = (not found
                       or f"{found.group(1)} {found.group(2)}" >= before)
        elif stamp:
            cutting = stamp.group(1).replace("T", " ") >= before
        if not cutting:
            kept.append(line)
    return "\n".join(kept).strip()


def from_ticket(ticket: str, expect: dict, note: str,
                before: str = "") -> list[tuple[str, dict]]:
    """(agent, case) pairs built from the ticket's live context. `before`
    cuts the thread to how it stood then; the stored card is from later,
    so no investigator case is made."""
    import sprint
    import sprint_store

    row = sprint.resolve_ticket(ticket)
    if not row:
        raise ValueError(f"ticket {ticket} not found in Zoho")
    context = sprint.build_context(row)
    if not context:
        raise ValueError(f"ticket {ticket} could not be read")
    for key in [k for k in context if k.startswith("_")]:
        context.pop(key)
    number = context.get("ticket_number") or ticket
    case_id = f"sam-{number}"
    if before:
        context["thread"] = thread_before(context.get("thread") or "",
                                          before)
        # The task search is today's: on #9760 it named the task filed for
        # a later message, and read_task showed the agent that message.
        context["existing_tasks"] = (
            f"(not searched: this case judges the ticket as it stood before "
            f"{before})")
        case_id = f"{case_id}-before-{before[:10]}"
    cases = [(TRIAGE, {"id": case_id, "note": note, "context": context,
                       "expect": expect})]
    if before:
        return cases
    item = sprint_store.by_ticket(str(row.get("id"))) or {}
    card = dict(item.get("card") or {})
    first = card.pop("first_pass", None) or {}
    card.pop("deep", None)
    card.pop("incident", None)
    if card:
        # Judge the investigator on correcting what the first pass wrote:
        # where the deep pass already replaced the card, put the first
        # pass's own category, lane and confidence back.
        card.update({k: v for k, v in first.items() if k != "code_check"})
        if first.get("code_check"):
            card["code_check"] = {**(card.get("code_check") or {}),
                                  "verdict": first["code_check"]}
        cases.append((INVESTIGATOR, {
            "id": case_id, "note": note,
            "context": {**context, "first_card": json.dumps(
                card, ensure_ascii=False)},
            "expect": expect}))
    return cases


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="py triage_cases.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add")
    add.add_argument("--ticket", required=True)
    add.add_argument("--expect", nargs="+", required=True,
                     help="key=value, e.g. category=clear_bug lane=engineer "
                          "code_check.verdict=bug")
    add.add_argument("--note", default="")
    add.add_argument("--source", default="claude_code")
    add.add_argument("--before", default="",
                     help="judge the thread as it stood before this date "
                          "(2026-11-01 or '2026-11-01 12:00')")
    show = sub.add_parser("list")
    show.add_argument("--agent", default="")
    drop = sub.add_parser("remove")
    drop.add_argument("id", type=int)
    args = parser.parse_args(argv)

    if args.cmd == "add":
        pairs = from_ticket(args.ticket, parse_expect(args.expect),
                            args.note, before=args.before)
        for agent, case in pairs:
            new_id = eval_cases.add(agent, case, ticket=args.ticket,
                                    source=args.source)
            print(f"{agent}: case {case['id']} "
                  f"{'saved as ' + str(new_id) if new_id else 'NOT saved'}")
        return 0
    if args.cmd == "list":
        where = " WHERE agent = :a" if args.agent else ""
        rows = store.query(
            f"SELECT id, agent, case_id, source, created_at,"
            f" body->'expect' AS expect FROM {eval_cases.TABLE}{where}"
            " ORDER BY id", {"a": args.agent} if args.agent else {})
        for r in rows:
            print(f"{r['id']:>4} {r['agent']} {r['case_id']} "
                  f"{json.dumps(r['expect'])} ({r['source']}, "
                  f"{str(r['created_at'])[:10]})")
        return 0
    ok = store.execute(f"DELETE FROM {eval_cases.TABLE} WHERE id = :i",
                       {"i": args.id})
    print("removed" if ok else "not removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

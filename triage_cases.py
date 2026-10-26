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
        expect[key.strip()] = {"true": True, "false": False}.get(
            value.lower(), value)
    return expect


def from_ticket(ticket: str, expect: dict,
                note: str) -> list[tuple[str, dict]]:
    """(agent, case) pairs built from the ticket's live context."""
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
    cases = [(TRIAGE, {"id": case_id, "note": note, "context": context,
                       "expect": expect})]
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
    show = sub.add_parser("list")
    show.add_argument("--agent", default="")
    drop = sub.add_parser("remove")
    drop.add_argument("id", type=int)
    args = parser.parse_args(argv)

    if args.cmd == "add":
        pairs = from_ticket(args.ticket, parse_expect(args.expect),
                            args.note)
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

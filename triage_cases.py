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
    item = sprint_store.by_ticket(str(row.get("id"))) or {}
    audit = card_vs_truth(expect, item.get("card") or {})
    cases = [(TRIAGE, {"id": case_id, "note": note, "context": context,
                       "expect": expect, "audit": audit})]
    if before:
        return cases
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
            "expect": expect, "audit": audit}))
    return cases


def _first_pass_card(card: dict) -> dict:
    """The first pass's own answer, shaped like a card: where the deep
    pass replaced the card, `first_pass` keeps what it said before."""
    first = dict(card.get("first_pass") or {})
    if not first:
        return {}
    shaped = {k: v for k, v in first.items() if k != "code_check"}
    verdict = first.get("code_check")
    if isinstance(verdict, dict):
        shaped["code_check"] = verdict
    elif verdict:
        shaped["code_check"] = {"verdict": verdict}
    return shaped


def card_vs_truth(expect: dict, card: dict) -> dict:
    """What the agents' card said against what Sam's audit settled, kept
    on the case so agreement can be counted per category. `card` is the
    stored sprint card (deep pass included); `first` is the first pass
    alone. An audit that agrees with the card is still recorded: those are
    the cases that say a category can be trusted."""
    from vomeos.evaluate import compare

    def said(source: dict) -> dict:
        out = {}
        for key in expect:
            got = source
            for part in key.split("."):
                got = got.get(part) if isinstance(got, dict) else None
            out[key] = got
        return out

    card = card or {}
    first = _first_pass_card(card) or ({k: v for k, v in card.items()
                                        if k not in ("deep", "incident")}
                                       if card else {})
    final = {k: v for k, v in card.items()
             if k not in ("first_pass", "deep", "incident")}
    audit = {"had_card": bool(final),
             "category": str(expect.get("category") or
                             final.get("category") or "unknown")}
    if final:
        misses = compare(expect, final)
        audit.update(card_said=said(final), agreed=not misses, misses=misses)
    # The deep pass keeps only category, lane, confidence and the verdict
    # of the first pass, so judge it on those alone.
    first_expect = {k: v for k, v in expect.items()
                    if k.split(".")[0] in first}
    if first and first_expect:
        first_misses = compare(first_expect, first)
        audit.update(first_said=said(first), first_agreed=not first_misses)
    return audit


def agreement_since(days: int = 30) -> dict:
    """agreement() over the audits recorded in the last `days`."""
    if not store.usable() or not store.ensure_table(eval_cases.TABLE):
        return {}
    rows = store.query(
        f"SELECT body->'audit' AS audit FROM {eval_cases.TABLE}"
        " WHERE agent = :a AND created_at > NOW() - make_interval("
        "days => :d)", {"a": TRIAGE, "d": int(days)})
    for row in rows:
        if isinstance(row.get("audit"), str):
            try:
                row["audit"] = json.loads(row["audit"])
            except ValueError:
                row["audit"] = None
    return agreement(rows)


def trust_lines(table: dict, days: int = 30) -> list[str]:
    """The weekly report's view of agreement: one line per category."""
    if not table:
        return [f"No audits with a card in the last {days} days. "
                "`/audit N` in Claude Code records one."]
    lines = []
    for cat, b in sorted(table.items(), key=lambda kv: -kv[1]["audited"]):
        n = b["audited"]
        lines.append(f"`{cat}` audited {n} · card right "
                     f"{b['card_agreed'] / n:.0%} · first pass right "
                     f"{b['first_agreed'] / n:.0%}")
    return lines


# Which open tickets Sam should audit (G69). Chosen on purpose, where a
# wrong card costs most or the agents were unsure, plus a fixed 1 in 10
# sample of everything else so the agreement number is not only measured
# on the hard tickets.
AUDIT_ARR = 4000
SAMPLE_ONE_IN = 10


def sampled(ticket_number: str) -> bool:
    """A stable 1 in 10 pick: the same ticket is always in or out."""
    import hashlib

    digest = hashlib.sha256(str(ticket_number).encode()).hexdigest()
    return int(digest, 16) % SAMPLE_ONE_IN == 0


def audit_reasons(item: dict) -> list[str]:
    """Why this sprint item is worth an audit, most telling first. Empty
    when it is not."""
    from ticket_routing import arr_of

    card = item.get("card") or {}
    if not card:
        return []
    reasons = []
    if ((card.get("data_check") or {}).get("needed")) == "required":
        reasons.append("data check required")
    if ((card.get("deep") or {}).get("verdict")) == "corrected":
        reasons.append("deep pass corrected the first card")
    verdict = (card.get("code_check") or {}).get("verdict")
    if card.get("confidence") == "low" or verdict == "unclear":
        reasons.append("agents unsure (low confidence or code unclear)")
    if card.get("category") == "needs_human":
        reasons.append("triage said needs_human")
    # Waiting on the client leaves nothing to check, however big the
    # account (live run 2026-11-05: #9505 and #9824 were picked on ARR alone).
    if arr_of(item.get("account") or "") >= AUDIT_ARR \
            and card.get("category") != "awaiting_client":
        reasons.append(f"{AUDIT_ARR}+ ARR account")
    if not reasons and sampled(item.get("ticket_number") or ""):
        reasons.append("random sample (keeps the agreement number honest)")
    return reasons


def audited_tickets(numbers: list[str]) -> set[str]:
    """Ticket numbers that already have an audit recorded."""
    if not numbers or not store.usable() \
            or not store.ensure_table(eval_cases.TABLE):
        return set()
    rows = store.query(
        f"SELECT DISTINCT ticket FROM {eval_cases.TABLE}"
        " WHERE ticket = ANY(:n)", {"n": [str(n) for n in numbers]})
    return {str(r["ticket"]) for r in rows}


def audit_picks(items: list[dict], limit: int = 6) -> list[dict]:
    """The open items most worth Sam's audit, not yet audited."""
    scored = [(item, audit_reasons(item)) for item in items]
    scored = [(i, r) for i, r in scored if r]
    done = audited_tickets([str(i.get("ticket_number")) for i, _ in scored])
    picks = [{"ticket": f"#{i.get('ticket_number')}",
              "account": i.get("account") or "",
              "category": (i.get("card") or {}).get("category") or "",
              "why": r,
              "run": f"/audit {i.get('ticket_number')}"}
             for i, r in scored if str(i.get("ticket_number")) not in done]
    # Most reasons first; the order items came in (Sam's priority) breaks
    # ties, so sort is stable on the count alone.
    picks.sort(key=lambda p: -len(p["why"]))
    return picks[:limit]


def agreement(rows: list[dict]) -> dict:
    """Per settled category: how many audits had a card, and how often
    the final card (and the first pass) matched Sam. One row per ticket:
    the triage director's case, never the investigator copy."""
    out: dict[str, dict] = {}
    for row in rows:
        audit = row.get("audit") or {}
        if not audit.get("had_card"):
            continue
        cat = audit.get("category") or "unknown"
        bucket = out.setdefault(cat, {"audited": 0, "card_agreed": 0,
                                      "first_agreed": 0})
        bucket["audited"] += 1
        bucket["card_agreed"] += 1 if audit.get("agreed") else 0
        bucket["first_agreed"] += 1 if audit.get("first_agreed") else 0
    return out


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
    agree = sub.add_parser(
        "agreement", help="how often the cards matched Sam's audits")
    agree.add_argument("--days", type=int, default=30)
    args = parser.parse_args(argv)

    # From a laptop the worker's database host (postgres.railway.internal)
    # does not resolve. Like vomeos.lessons: set DATABASE_PUBLIC_URL (the
    # Postgres service's public URL) in the shell before `railway run -s
    # vome-os-worker`, which does not define it, so it passes through.
    import os

    public = os.environ.get("DATABASE_PUBLIC_URL", "")
    if public:
        from vomeos import config

        config.DATABASE_URL = public
        store.reset()

    if args.cmd == "add":
        pairs = from_ticket(args.ticket, parse_expect(args.expect),
                            args.note, before=args.before)
        for agent, case in pairs:
            new_id = eval_cases.add(agent, case, ticket=args.ticket,
                                    source=args.source)
            print(f"{agent}: case {case['id']} "
                  f"{'saved as ' + str(new_id) if new_id else 'NOT saved'}")
        audit = pairs[0][1].get("audit") or {}
        if not audit.get("had_card"):
            print("card: none stored for this ticket")
        elif audit.get("agreed"):
            print("card: AGREED with the audit")
        else:
            print("card: MISSED " + "; ".join(audit.get("misses") or []))
        return 0
    if args.cmd == "agreement":
        table = agreement_since(args.days)
        if not table:
            print(f"no audits with a card in the last {args.days} days")
            return 0
        print(f"last {args.days} days: category  audited  card  first pass")
        for cat, b in sorted(table.items(), key=lambda kv: -kv[1]["audited"]):
            n = b["audited"]
            print(f"  {cat:<22} {n:>4}  {b['card_agreed'] / n:>5.0%}"
                  f"  {b['first_agreed'] / n:>5.0%}")
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

"""
deep_triage.py

The second, senior look at a ticket whose first card is not sure of itself:
the triage investigator re-checks the card against the thread, the code and
the engineers' own task notes, and its card replaces the first one before
anything is drafted, fixed or routed.

WHY
---
Sam, 2026-10-23: a ticket pasted into Claude Code (same model, Opus 5.5)
kept coming back with a different and better answer than shadow triage gave
it. The model was not the difference; the budget was. On the Oct 21 backlog
sweep 40 of 64 first passes spent every one of their ten tool calls, 46 of
79 cards ended with the code check "unclear", 71 of 79 were medium
confidence, and 34 said in so many words what they had not read. Lessons
could not fix that: a lesson tells an agent what to conclude, not how to
find out. So a card that is unsure gets the budget a person would spend.

WHEN IT RUNS (`reason_for`)
---------------------------
Every card except the ones nothing is owed on or no code can change
(housekeeping, awaiting_client, misdirected, email_update,
account_deletion), when any of these hold:

    the first pass failed to produce a card
    confidence is not high
    the code check is unclear, not_checked or missing on a category that
        needs one (clear_bug, vague_bug, user_education, feature_request)
    the lane is engineer or sam_ops (someone's time is about to be spent)
    the client is important (flag on the card, or over 1500 CAD ARR)

A high-confidence card with a settled code check on a small account goes
straight through, which is most how-to questions.

WHAT IT RECORDS
---------------
One row per triaged ticket in vomeos_triage_runs: whether the deep pass ran
and why, whether it corrected the first card, and the cost of both runs in
USD from their token counts. `py deep_triage.py report` turns that into the
numbers for a monthly forecast.

SWITCHES
--------
    DEEP_TRIAGE_ENABLED     default on. "false" skips the deep pass.
    DEEP_TRIAGE_DAILY_CAP   deep passes per day, default 60. Past the cap
                            the first card stands and says so.
"""

from __future__ import annotations

import json
import os
import sys

from vomeos import store

AGENT = "customer_success.triage_investigator"
TABLE = "vomeos_triage_runs"

# Opus 5.5 list prices, USD per million tokens (2026-10). Cache writes are
# 1.25x input, cache reads 0.05x. Only used for the cost estimate.
PRICES = {
    "claude-opus-5-5": {"in": 4.0, "out": 20.0, "read": 0.20, "write": 5.0},
    "claude-opus-5": {"in": 5.0, "out": 25.0, "read": 0.50, "write": 6.25},
    "claude-sonnet-5-5": {"in": 2.0, "out": 10.0, "read": 0.20, "write": 2.5},
    "claude-sonnet-4-6": {"in": 3.0, "out": 15.0, "read": 0.30,
                          "write": 3.75},
}

NOTHING_TO_CHECK = {"housekeeping"}
SKIP_CATEGORIES = {"awaiting_client", "misdirected", "email_update",
                   "account_deletion"}
NEEDS_CODE = {"clear_bug", "vague_bug", "user_education", "feature_request"}
UNSETTLED = {"unclear", "not_checked", ""}
COSTLY_LANES = {"engineer", "sam_ops"}
IMPORTANT_ARR = 1500

store.register_table(
    TABLE,
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
        id              SERIAL PRIMARY KEY,
        ticket_id       VARCHAR,
        ticket_number   VARCHAR,
        source          VARCHAR,
        first_category  VARCHAR,
        first_lane      VARCHAR,
        first_verdict   VARCHAR,
        first_calls     INTEGER,
        first_usd       NUMERIC(10, 4),
        deep_reason     VARCHAR,
        deep_status     VARCHAR,
        deep_verdict    VARCHAR,
        final_category  VARCHAR,
        final_lane      VARCHAR,
        final_verdict   VARCHAR,
        deep_calls      INTEGER,
        deep_usd        NUMERIC(10, 4),
        changes         TEXT,
        created_at      TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,
    (f"CREATE INDEX IF NOT EXISTS {TABLE}_created ON {TABLE} (created_at)",),
)


def enabled() -> bool:
    value = os.environ.get("DEEP_TRIAGE_ENABLED", "")
    return value.strip().lower() not in ("false", "0", "no", "off")


def daily_cap() -> int:
    return int(os.environ.get("DEEP_TRIAGE_DAILY_CAP", "60"))


def usd(result) -> float:
    """Estimated cost of one agent run from its token counts."""
    price = PRICES.get(getattr(result, "model", "") or "")
    if not price:
        return 0.0
    read = int(getattr(result, "cache_read_tokens", 0) or 0)
    write = int(getattr(result, "cache_write_tokens", 0) or 0)
    plain = max(0, int(getattr(result, "input_tokens", 0) or 0) - read
                - write)
    out = int(getattr(result, "output_tokens", 0) or 0)
    return round((plain * price["in"] + read * price["read"]
                  + write * price["write"] + out * price["out"]) / 1e6, 4)


def _verdict(card: dict) -> str:
    return str(((card or {}).get("code_check") or {}).get("verdict") or "")


def reason_for(card: dict, arr: float = 0.0, failed: bool = False) -> str:
    """Why this card needs the deep pass, or '' when it does not."""
    if not enabled():
        return ""
    if failed:
        return "first pass failed"
    card = card or {}
    category = str(card.get("category") or "")
    lane = str(card.get("lane") or "")
    if lane in NOTHING_TO_CHECK or category in SKIP_CATEGORIES:
        return ""
    # #9586's first card (2026-10-23 live run) came back with no lane and
    # no code check: routing cannot act on that, whatever the confidence.
    if not lane:
        return "lane missing"
    if str(card.get("confidence") or "").lower() != "high":
        return "confidence " + (str(card.get("confidence") or "missing"))
    if category in NEEDS_CODE and _verdict(card) in UNSETTLED:
        return f"code check {_verdict(card) or 'missing'}"
    if lane in COSTLY_LANES:
        return f"lane {lane}"
    if card.get("important_client") or float(arr or 0) > IMPORTANT_ARR:
        return "important client"
    return ""


def _deep_today() -> int:
    if not store.ensure_table(TABLE):
        return 0
    rows = store.query(
        f"SELECT COUNT(*) AS n FROM {TABLE} WHERE deep_status IS NOT NULL"
        " AND deep_status <> 'skipped'"
        " AND created_at >= date_trunc('day', NOW())")
    return int(rows[0]["n"]) if rows else 0


def investigate(context: dict, card: dict, images, subject_id: str,
                reason: str) -> tuple[dict, dict]:
    """(card to use, run info). Never raises; on any failure the first card
    stands, marked so Sam can see the deep pass did not run."""
    info = {"reason": reason, "status": "skipped", "verdict": "",
            "changes": [], "calls": 0, "usd": 0.0}
    if _deep_today() >= daily_cap():
        info["status"] = "skipped"
        info["why"] = f"daily cap of {daily_cap()} reached"
        return {**card, "deep": {**info}}, info
    try:
        from vomeos import run_agent

        result = run_agent(
            AGENT,
            context={**context, "first_card": json.dumps(
                card or {}, ensure_ascii=False)},
            subject_type="zoho_ticket", subject_id=subject_id,
            images=images,
        )
    except Exception as exc:
        info.update(status="error", why=f"{type(exc).__name__}: {exc}")
        return {**card, "deep": {**info}}, info
    info["calls"] = len(result.tool_calls or [])
    info["usd"] = usd(result)
    data = result.data or {}
    if not result.ok or not data.get("category"):
        info.update(status="error", why=result.why())
        return {**card, "deep": {**info}}, info
    # A sentence here ("No, they are low tier...") would read as true
    # everywhere downstream that does bool(card["important_client"]).
    flag = data.get("important_client")
    if not isinstance(flag, bool):
        data["important_client"] = str(flag or "").strip().lower().startswith(
            ("yes", "true"))
    review = data.pop("review", {}) or {}
    info.update(status="ok", verdict=str(review.get("verdict") or ""),
                changes=[str(c) for c in review.get("changes") or []][:8],
                checked=str(review.get("checked") or "")[:300])
    first = {k: (card or {}).get(k) for k in
             ("category", "lane", "confidence")}
    first["code_check"] = _verdict(card)
    return {**data, "first_pass": first, "deep": {**info}}, info


def record(ticket_id: str, ticket_number: str, source: str, first: dict,
           first_result, final: dict, info: dict | None) -> None:
    """One row per triaged ticket. A failed write costs a report line."""
    if not store.ensure_table(TABLE):
        return
    info = info or {}
    store.execute(
        f"INSERT INTO {TABLE} (ticket_id, ticket_number, source,"
        " first_category, first_lane, first_verdict, first_calls, first_usd,"
        " deep_reason, deep_status, deep_verdict, final_category,"
        " final_lane, final_verdict, deep_calls, deep_usd, changes)"
        " VALUES (:tid, :num, :src, :fc, :fl, :fv, :fcalls, :fusd, :dr,"
        " :ds, :dv, :xc, :xl, :xv, :dcalls, :dusd, :ch)",
        {
            "tid": str(ticket_id), "num": str(ticket_number), "src": source,
            "fc": str(first.get("category") or ""),
            "fl": str(first.get("lane") or ""),
            "fv": _verdict(first),
            "fcalls": len(getattr(first_result, "tool_calls", None) or []),
            "fusd": usd(first_result) if first_result is not None else 0,
            "dr": info.get("reason") or None,
            "ds": info.get("status") if info.get("reason") else None,
            "dv": info.get("verdict") or "",
            "xc": str(final.get("category") or ""),
            "xl": str(final.get("lane") or ""),
            "xv": _verdict(final),
            "dcalls": int(info.get("calls") or 0),
            "dusd": float(info.get("usd") or 0),
            "ch": json.dumps(info.get("changes") or [], ensure_ascii=False),
        },
    )


RECHECK_HOURS = int(os.environ.get("DEEP_TRIAGE_RECHECK_HOURS", "72"))


def recently_checked(previous: dict | None, card: dict,
                     updated_at=None) -> bool:
    """The ticket's last card came from a deep pass within RECHECK_HOURS and
    the new first pass agrees with it on category and lane. Every customer
    reply re-triages a ticket; a "thanks, will try" should not buy a second
    full investigation of a question already settled."""
    previous = previous or {}
    if (previous.get("deep") or {}).get("status") != "ok":
        return False
    if not updated_at or not hasattr(updated_at, "tzinfo"):
        return False
    from datetime import datetime, timezone

    when = updated_at if updated_at.tzinfo else updated_at.replace(
        tzinfo=timezone.utc)
    hours = (datetime.now(timezone.utc) - when).total_seconds() / 3600
    return (hours < RECHECK_HOURS
            and previous.get("category") == card.get("category")
            and previous.get("lane") == card.get("lane"))


def deepen(context: dict, card: dict, images, *, ticket_id: str,
           ticket_number: str, arr: float, first_result,
           source: str = "shadow", previous: dict | None = None,
           previous_at=None) -> dict:
    """The whole step for a caller: decide, investigate, record. Returns
    the card to use from here on. `previous` is the ticket's stored card
    (and `previous_at` when it was written), if it has one."""
    failed = first_result is not None and not getattr(first_result, "ok",
                                                      True)
    reason = reason_for(card, arr, failed=failed)
    info = None
    final = card
    if reason and not failed and recently_checked(previous, card,
                                                  previous_at):
        # Keep the settled investigation; the first pass already agrees.
        keep = ("code_check", "existing_work", "escalation_reason")
        final = {**card, **{k: previous[k] for k in keep if previous.get(k)},
                 "deep": {**previous["deep"],
                          "reason": f"reused ({reason})"}}
        reason = ""
    if reason:
        final, info = investigate(context, card, images, ticket_id, reason)
        print(f"[DEEP] #{ticket_number} {reason}: {info['status']} "
              f"{info.get('verdict') or ''} ({info['calls']} calls, "
              f"${info['usd']:.2f})")
    try:
        record(ticket_id, ticket_number, source, card, first_result, final,
               info)
    except Exception as exc:
        print(f"[DEEP] could not record #{ticket_number}: {exc}")
    return final


# ---------------------------------------------------------------------------
# Report: what triage costs and how often the deep pass changes the answer
# ---------------------------------------------------------------------------

def report(days: int = 14) -> dict:
    if not store.ensure_table(TABLE):
        return {"error": "no database"}
    rows = store.query(
        f"SELECT COUNT(*) AS tickets,"
        " COUNT(*) FILTER (WHERE deep_status = 'ok') AS deep_runs,"
        " COUNT(*) FILTER (WHERE deep_verdict = 'corrected') AS corrected,"
        " COUNT(*) FILTER (WHERE deep_status = 'error') AS deep_errors,"
        " COALESCE(AVG(first_usd), 0) AS avg_first,"
        " COALESCE(AVG(deep_usd) FILTER (WHERE deep_status = 'ok'), 0)"
        "   AS avg_deep,"
        " COALESCE(SUM(first_usd) + SUM(deep_usd), 0) AS total,"
        " COUNT(DISTINCT date_trunc('day', created_at)) AS days"
        f" FROM {TABLE} WHERE created_at >= NOW() - (:d || ' days')::interval",
        {"d": str(int(days))},
    )
    row = rows[0] if rows else {}
    tickets = int(row.get("tickets") or 0)
    span = max(1, int(row.get("days") or 0))
    total = float(row.get("total") or 0)
    per_day = total / span
    return {
        "days_with_data": span,
        "tickets_triaged": tickets,
        "deep_runs": int(row.get("deep_runs") or 0),
        "deep_share": round(int(row.get("deep_runs") or 0) / tickets, 2)
        if tickets else 0,
        "corrected_by_deep": int(row.get("corrected") or 0),
        "deep_errors": int(row.get("deep_errors") or 0),
        "avg_first_pass_usd": round(float(row.get("avg_first") or 0), 3),
        "avg_deep_pass_usd": round(float(row.get("avg_deep") or 0), 3),
        "avg_per_ticket_usd": round(total / tickets, 3) if tickets else 0,
        "total_usd": round(total, 2),
        "forecast_month_usd": round(per_day * 30, 0),
        "forecast_year_usd": round(per_day * 365, 0),
        "note": "Triage only (first pass + deep pass). The reply drafter, "
                "incident matcher and fix author are billed separately.",
    }


def main(argv: list[str]) -> int:
    if not argv or argv[0] != "report":
        print("usage: py deep_triage.py report [days]")
        return 1
    days = int(argv[1]) if len(argv) > 1 else 14
    print(json.dumps(report(days), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

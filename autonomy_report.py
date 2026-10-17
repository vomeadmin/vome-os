"""
autonomy_report.py

How often each kind of reply goes out exactly as the agent wrote it, and
what Sam changes when it does not. Weekly, in #support-sprints.

WHY THIS EXISTS
---------------
Roadmap item 8 says a category may earn auto-send (with a cancel window)
after about 95% unedited approvals over 50 tickets. That rule is only real
if someone counts. This is the count, per category, over the last 30 days:

    drafted     the agent wrote a draft
    sent        Sam sent it (from the sprint thread)
    unedited    what was sent is byte-for-byte the agent's first draft
    edited      Sam rewrote it, or had it rewritten, before sending
    redrafted   Sam asked the drafter to try again

It also lists Sam's edits. Each is a correction the drafter got wrong, which
makes it a candidate answer-key case: `--export FILE` writes them as JSONL
for a person to turn into eval cases. Nothing is added to an answer key
automatically, because the key is the human's judgment, not the agent's.

Nothing here sends, closes or changes anything. Auto-send is not built; this
only says which categories would qualify.
"""

from __future__ import annotations

import json
import os

import sprint_store
from vomeos import store

WINDOW_DAYS = int(os.environ.get("AUTONOMY_WINDOW_DAYS", "30"))
MIN_SENT = int(os.environ.get("AUTONOMY_MIN_SENT", "50"))
MIN_UNEDITED = float(os.environ.get("AUTONOMY_MIN_UNEDITED", "0.95"))


def category_stats(days: int = WINDOW_DAYS) -> list[dict]:
    """One row per category, busiest first."""
    if not sprint_store.ready():
        return []
    rows = store.query(
        f"SELECT COALESCE(category, '?') AS category,"
        " COUNT(*) FILTER (WHERE original_draft IS NOT NULL"
        "   AND original_draft <> '') AS drafted,"
        " COUNT(*) FILTER (WHERE sent_at IS NOT NULL) AS sent,"
        " COUNT(*) FILTER (WHERE sent_unedited) AS unedited,"
        " COUNT(*) FILTER (WHERE sent_at IS NOT NULL"
        "   AND NOT sent_unedited) AS edited,"
        " COALESCE(SUM(redrafts), 0) AS redrafts"
        f" FROM {sprint_store.ITEMS}"
        " WHERE arrived_at >= NOW() - make_interval(days => :d)"
        " GROUP BY 1 ORDER BY 3 DESC, 2 DESC",
        {"d": int(days)},
    )
    for row in rows:
        sent = int(row["sent"] or 0)
        row["unedited_rate"] = (int(row["unedited"] or 0) / sent
                                if sent else 0.0)
        row["eligible"] = (sent >= MIN_SENT
                           and row["unedited_rate"] >= MIN_UNEDITED)
    return rows


def recent_edits(days: int = WINDOW_DAYS, limit: int = 200) -> list[dict]:
    if not store.ensure_table(sprint_store.EDITS):
        return []
    return store.query(
        f"SELECT ticket_number, category, kind, before_text, after_text,"
        f" created_at FROM {sprint_store.EDITS}"
        " WHERE created_at >= NOW() - make_interval(days => :d)"
        " ORDER BY created_at DESC LIMIT :n",
        {"d": int(days), "n": int(limit)},
    )


def render(rows: list[dict], edits: list[dict]) -> str:
    lines = [f":bar_chart: *Draft autonomy, last {WINDOW_DAYS} days*",
             f"_A category qualifies for auto-send at {MIN_SENT}+ sent and "
             f"{int(MIN_UNEDITED * 100)}%+ unedited. Auto-send is not "
             "built; this only shows who would qualify._"]
    if not rows:
        lines.append("No drafts in the window yet.")
        return "\n".join(lines)
    for r in rows:
        rate = f"{r['unedited_rate'] * 100:.0f}%" if r["sent"] else "-"
        flag = " :white_check_mark: *qualifies*" if r["eligible"] else ""
        lines.append(
            f"`{r['category']}` drafted {r['drafted']} · sent {r['sent']} · "
            f"unedited {r['unedited']} ({rate}) · edited {r['edited']} · "
            f"redrafts {r['redrafts']}{flag}"
        )
    if edits:
        lines.append(f"\n*Your edits this period: {len(edits)}.* Each is a "
                     "correction the drafter got wrong and a candidate "
                     "answer-key case (`py autonomy_report.py --export "
                     "edits.jsonl`).")
    return "\n".join(lines)


def run_autonomy_report() -> dict:
    """The weekly post. Read-only apart from the Slack message."""
    rows = category_stats()
    edits = recent_edits()
    import sprint_thread

    ts = sprint_thread._post(render(rows, edits))
    return {"status": "ok" if ts else "post failed",
            "categories": len(rows), "edits": len(edits)}


def export_edits(path: str) -> int:
    """Sam's edits as JSONL, for a person to turn into answer-key cases."""
    edits = recent_edits(limit=1000)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for e in edits:
            fh.write(json.dumps({
                "ticket": e.get("ticket_number"),
                "category": e.get("category"),
                "kind": e.get("kind"),
                "agent_wrote": e.get("before_text"),
                "sam_sent": e.get("after_text"),
                "when": str(e.get("created_at")),
            }, ensure_ascii=False) + "\n")
    return len(edits)


if __name__ == "__main__":
    import argparse

    from dotenv import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(description="Draft autonomy report")
    parser.add_argument("--export", default="",
                        help="write Sam's edits as JSONL to this file")
    args = parser.parse_args()
    if args.export:
        print(f"Wrote {export_edits(args.export)} edits to {args.export}")
    else:
        print(render(category_stats(), recent_edits()))

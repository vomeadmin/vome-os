"""
scripts/render_workflow_map.py

Render WORKFLOWS.md into one self-contained HTML page: the status key with
counts, every diagram (Mermaid, colour-coded by status), the schedule, and
the gap register with filters.

    py scripts/render_workflow_map.py [WORKFLOWS.md] [out.html]

WORKFLOWS.md is the source of truth. Edit it, then run this. The page is a
view of the file and holds no facts of its own, so the map and the docs can
never drift apart.
"""

from __future__ import annotations

import html
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

STATUSES = [
    # class, label, fill, stroke, text, dashed
    ("live", "Live and proven", "#dcf2e3", "#2f7a52", "#0f2a1c", False),
    ("deployed", "Deployed, not yet proven", "#dde9fb", "#2c5fb8", "#0c1f45",
     False),
    ("built", "Built, not pushed", "#d6f1f2", "#1f7a80", "#08303a", False),
    ("proposed", "Proposed, not built", "#fbf0d4", "#a8650f", "#3a2304",
     True),
    ("broken", "Broken or at risk", "#fbe1e1", "#b42323", "#450b0b", False),
    ("decide", "Needs your decision", "#ebe5fb", "#6a3fc9", "#26104d",
     False),
]
STATUS_KEYS = [s[0] for s in STATUSES]

_DIAGRAM = re.compile(
    r'<!-- map:diagram id=(\w+) title="([^"]+)" -->\s*```mermaid\n(.*?)```',
    re.S,
)


def class_defs() -> str:
    lines = []
    for key, _, fill, stroke, text, dashed in STATUSES:
        extra = ",stroke-dasharray:6 4" if dashed else ""
        width = "2px" if key in ("broken", "decide") else "1.5px"
        lines.append(f"  classDef {key} fill:{fill},stroke:{stroke},"
                     f"color:{text},stroke-width:{width}{extra}")
    return "\n".join(lines)


def parse(md: str) -> dict:
    diagrams = []
    for ident, title, body in _DIAGRAM.findall(md):
        lines = body.rstrip().splitlines()
        # classDefs go right after the "flowchart XX" line.
        source = "\n".join([lines[0], class_defs()] + lines[1:])
        counts = Counter(re.findall(r":::(\w+)", body))
        diagrams.append({"id": ident, "title": title, "source": source,
                         "counts": counts})

    def table_after(marker: str) -> list[list[str]]:
        start = md.find(marker)
        if start < 0:
            return []
        rows = []
        for line in md[start:].splitlines()[1:]:
            if not line.strip():
                if rows:
                    break
                continue
            if not line.startswith("|"):
                if rows:
                    break
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if set("".join(cells)) <= set("-: "):
                continue
            rows.append(cells)
        return rows

    register = table_after("<!-- map:register -->")
    schedule = table_after("## Schedule")
    as_of = re.search(r"\*\*As of:\*\*\s*([\d-]+)", md)
    return {
        "diagrams": diagrams,
        "register_head": register[0] if register else [],
        "register": register[1:],
        "schedule_head": schedule[0] if schedule else [],
        "schedule": schedule[1:],
        "as_of": as_of.group(1) if as_of else "",
    }


def _md_inline(text: str) -> str:
    """Escape, then render `code` spans. Nothing else is markdown here."""
    out = html.escape(text)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", out)


def render(data: dict) -> str:
    totals = Counter()
    for d in data["diagrams"]:
        totals.update(d["counts"])
    register_counts = Counter(r[3] for r in data["register"] if len(r) > 3)

    key_items = "".join(
        f'<li class="key" data-s="{k}"><span class="sw sw-{k}"></span>'
        f'<span class="kl">{html.escape(label)}</span>'
        f'<span class="kn">{totals.get(k, 0)}</span></li>'
        for k, label, *_ in STATUSES
    )
    nav = "".join(
        f'<a href="#{d["id"]}">{html.escape(d["title"])}</a>'
        for d in data["diagrams"]
    )
    sections = []
    for d in data["diagrams"]:
        mix = " ".join(
            f'<span class="pill p-{k}">{d["counts"][k]} {k}</span>'
            for k in STATUS_KEYS if d["counts"].get(k)
        )
        sections.append(
            f'<section class="diagram" id="{d["id"]}">'
            f'<header><h2>{html.escape(d["title"])}</h2>'
            f'<div class="mix">{mix}</div></header>'
            f'<div class="board"><pre class="mermaid">'
            f'{html.escape(d["source"])}</pre></div></section>'
        )
    head = data["register_head"]
    rows = []
    for r in data["register"]:
        cells = (r + [""] * len(head))[:len(head)]
        status = cells[3]
        rows.append(
            f'<tr data-s="{html.escape(status)}">'
            f'<td class="mono">{html.escape(cells[0])}</td>'
            f'<td>{html.escape(cells[1])}</td>'
            f'<td class="item">{_md_inline(cells[2])}</td>'
            f'<td><span class="pill p-{html.escape(status)}">'
            f'{html.escape(status)}</span></td>'
            f'<td>{_md_inline(cells[4])}</td>'
            f'<td>{_md_inline(cells[5])}</td>'
            f'<td>{html.escape(cells[6])}</td></tr>'
        )
    filters = '<button class="f on" data-f="all">All ' \
              f'{len(data["register"])}</button>' + "".join(
                  f'<button class="f" data-f="{k}">'
                  f'<span class="sw sw-{k}"></span>{k} '
                  f'{register_counts.get(k, 0)}</button>'
                  for k in STATUS_KEYS if register_counts.get(k))
    schedule = "".join(
        f"<tr><td>{_md_inline(r[0])}</td><td>{_md_inline(r[1])}</td>"
        f'<td><span class="pill p-{html.escape(r[2])}">'
        f"{html.escape(r[2])}</span></td></tr>"
        for r in data["schedule"] if len(r) >= 3
    )
    open_gaps = sum(v for k, v in register_counts.items()
                    if k in ("proposed", "broken", "decide"))

    return TEMPLATE.format(
        as_of=html.escape(data["as_of"]),
        key_items=key_items,
        nav=nav,
        sections="".join(sections),
        filters=filters,
        rows="".join(rows),
        schedule=schedule,
        open_gaps=open_gaps,
        broken=register_counts.get("broken", 0),
        decide=register_counts.get("decide", 0),
        statuses=json.dumps(STATUS_KEYS),
        swatch_css="".join(
            f".sw-{k}{{background:{fill};border-color:{stroke}}}"
            f".p-{k}{{background:{fill};color:{text};border-color:{stroke}}}"
            for k, _, fill, stroke, text, _ in STATUSES
        ),
    )


TEMPLATE = """<title>Vome Support Agent Map</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&family=Literata:opsz,wght@7..72,600&display=swap">
<style>
/* Layout: a working map. Summary first (key + counts), then one paper board
   per workflow, then the gap register you act on. */
:root {{
  --bg: #f3f5f4; --surface: #ffffff; --ink: #18211d; --muted: #5a6762;
  --line: #d5dcd8; --accent: #1f6f5c; --paper: #fbfcfb;
  --display: "Literata", Georgia, serif;
  --body: "IBM Plex Sans", "Segoe UI", system-ui, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, Consolas, monospace;
}}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --bg: #121715; --surface: #1a211e; --ink: #e5ebe8; --muted: #9aa7a1;
  --line: #2c3632; --accent: #6cc4a8; --paper: #eef2f0; color-scheme: dark; }} }}
:root[data-theme="dark"] {{
  --bg: #121715; --surface: #1a211e; --ink: #e5ebe8; --muted: #9aa7a1;
  --line: #2c3632; --accent: #6cc4a8; --paper: #eef2f0; color-scheme: dark; }}
* {{ box-sizing: border-box; }}
body {{ background: var(--bg); color: var(--ink); font: 15px/1.55 var(--body);
  padding-left: max(16px, env(safe-area-inset-left, 0px));
  padding-right: max(16px, env(safe-area-inset-right, 0px));
  padding-block: 28px 64px; }}
.wrap {{ max-width: 1180px; margin: 0 auto; display: grid; gap: 28px; }}
h1 {{ font: 600 clamp(26px, 4vw, 36px)/1.15 var(--display); margin: 0;
  text-wrap: balance; }}
h2 {{ font: 600 19px/1.3 var(--display); margin: 0; text-wrap: balance; }}
.lede {{ color: var(--muted); max-width: 68ch; margin: 6px 0 0; }}
.eyebrow {{ font: 500 12px var(--mono); letter-spacing: .06em;
  text-transform: uppercase; color: var(--accent); }}
code, .mono {{ font-family: var(--mono); font-size: .88em; }}
.top {{ display: grid; gap: 18px; grid-template-columns: 1fr; }}
@media (min-width: 900px) {{ .top {{ grid-template-columns: 1.3fr 1fr; }} }}
.panel {{ background: var(--surface); border: 1px solid var(--line);
  border-radius: 10px; padding: 18px 20px; min-width: 0; }}
.keys {{ list-style: none; margin: 0; padding: 0; display: grid; gap: 8px;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); }}
.key {{ display: grid; grid-template-columns: 18px 1fr auto; gap: 10px;
  align-items: center; }}
.kn {{ font: 500 15px var(--mono); font-variant-numeric: tabular-nums; }}
.sw {{ display: inline-block; width: 16px; height: 16px; border-radius: 4px;
  border: 2px solid; vertical-align: -3px; }}
.sw-proposed {{ border-style: dashed; }}
.stats {{ display: flex; flex-wrap: wrap; gap: 22px; }}
.stat b {{ display: block; font: 600 30px/1 var(--display);
  font-variant-numeric: tabular-nums; }}
.stat span {{ color: var(--muted); font-size: 13px; }}
nav {{ display: flex; flex-wrap: wrap; gap: 8px; }}
nav a {{ color: var(--ink); text-decoration: none; border: 1px solid var(--line);
  background: var(--surface); padding: 6px 12px; border-radius: 999px;
  font-size: 13px; }}
nav a:hover, nav a:focus-visible {{ border-color: var(--accent);
  outline: none; }}
.diagram {{ display: grid; gap: 10px; scroll-margin-top: 16px; }}
.diagram header {{ display: flex; flex-wrap: wrap; gap: 10px;
  align-items: baseline; justify-content: space-between; }}
.mix {{ display: flex; flex-wrap: wrap; gap: 6px; }}
.board {{ background: var(--paper); border: 1px solid var(--line);
  border-radius: 10px; padding: 18px; overflow-x: auto; }}
.board pre {{ margin: 0; min-width: 640px; color: #18211d; }}
.pill {{ display: inline-block; border: 1px solid; border-radius: 999px;
  padding: 1px 9px; font: 500 12px var(--mono); white-space: nowrap; }}
.p-proposed {{ border-style: dashed; }}
.filters {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
.f {{ font: 500 13px var(--body); color: var(--ink); background: var(--surface);
  border: 1px solid var(--line); border-radius: 999px; padding: 5px 12px;
  cursor: pointer; display: inline-flex; gap: 6px; align-items: center; }}
.f.on {{ border-color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent); }}
.f:focus-visible, #q:focus-visible {{ outline: 2px solid var(--accent);
  outline-offset: 2px; }}
#q {{ font: 14px var(--body); color: var(--ink); background: var(--surface);
  border: 1px solid var(--line); border-radius: 8px; padding: 6px 10px;
  min-width: 0; flex: 1 1 200px; }}
.tablewrap {{ overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
th, td {{ text-align: left; vertical-align: top; padding: 9px 10px;
  border-bottom: 1px solid var(--line); }}
th {{ font: 500 12px var(--mono); letter-spacing: .04em; color: var(--muted);
  text-transform: uppercase; white-space: nowrap; }}
td.item {{ font-weight: 500; min-width: 220px; }}
.reg td:nth-child(5), .reg td:nth-child(6) {{ min-width: 220px; }}
.how ol {{ margin: 8px 0 0; padding-left: 20px; display: grid; gap: 6px; }}
.muted {{ color: var(--muted); }}
{swatch_css}
@media (prefers-reduced-motion: reduce) {{ * {{ scroll-behavior: auto; }} }}
</style>

<div class="wrap">
  <header>
    <div class="eyebrow">Customer success agents · as of {as_of}</div>
    <h1>Vome Support Agent Map</h1>
    <p class="lede">How a ticket, a bug, a production error and a feature
    request move through the agents today. Colour shows what is proven,
    what is built but unproven, and what is missing, so gaps stand out.
    Generated from <code>support-agent/WORKFLOWS.md</code>.</p>
  </header>

  <div class="top">
    <div class="panel">
      <div class="eyebrow">Status key · boxes on the map</div>
      <ul class="keys" style="margin-top:12px">{key_items}</ul>
    </div>
    <div class="panel">
      <div class="eyebrow">Gap register</div>
      <div class="stats" style="margin-top:12px">
        <div class="stat"><b>{open_gaps}</b><span>open gaps</span></div>
        <div class="stat"><b>{broken}</b><span>broken or at risk</span></div>
        <div class="stat"><b>{decide}</b><span>need your decision</span></div>
      </div>
    </div>
  </div>

  <nav aria-label="Workflows">{nav}<a href="#schedule">Schedule</a>
    <a href="#register">Gap register</a></nav>

  {sections}

  <section class="panel" id="schedule">
    <h2>What runs when</h2>
    <p class="muted" style="margin:4px 0 12px">America/Montreal. Celery beat
    schedules, the worker runs.</p>
    <div class="tablewrap"><table>
      <thead><tr><th>When</th><th>Job</th><th>Status</th></tr></thead>
      <tbody>{schedule}</tbody></table></div>
  </section>

  <section class="panel" id="register">
    <h2>Gap register</h2>
    <p class="muted" style="margin:4px 0 12px">Everything on the map that is
    not live, with the next step and who owns it.</p>
    <div class="filters">{filters}
      <input id="q" type="search" placeholder="Filter by word"
        aria-label="Filter the gap register"></div>
    <div class="tablewrap" style="margin-top:12px"><table class="reg">
      <thead><tr><th>Id</th><th>Area</th><th>Item</th><th>Status</th>
      <th>Why it matters</th><th>Next step</th><th>Owner</th></tr></thead>
      <tbody id="rows">{rows}</tbody></table></div>
  </section>

  <section class="panel how">
    <h2>Proposing a change</h2>
    <ol>
      <li>Say it in any Claude session: "add X to the map as proposed", or
      "mark G4 fixed".</li>
      <li>The session edits <code>WORKFLOWS.md</code> (diagram box and a
      register row, with a status) and runs
      <code>py scripts/render_workflow_map.py</code>.</li>
      <li>The page is republished to this same link.</li>
    </ol>
  </section>
</div>

<script>
(function () {{
  var active = "all";
  var q = document.getElementById("q");
  var rows = Array.prototype.slice.call(
    document.querySelectorAll("#rows tr"));
  function apply() {{
    var needle = (q.value || "").toLowerCase();
    rows.forEach(function (r) {{
      var okS = active === "all" || r.getAttribute("data-s") === active;
      var okQ = !needle || r.textContent.toLowerCase().indexOf(needle) >= 0;
      r.hidden = !(okS && okQ);
    }});
  }}
  document.querySelectorAll(".f").forEach(function (b) {{
    b.addEventListener("click", function () {{
      active = b.getAttribute("data-f");
      document.querySelectorAll(".f").forEach(function (x) {{
        x.classList.toggle("on", x === b);
      }});
      apply();
    }});
  }});
  q.addEventListener("input", apply);
}})();
</script>
"""


def main(argv: list[str]) -> int:
    src = Path(argv[1]) if len(argv) > 1 else ROOT / "WORKFLOWS.md"
    out = Path(argv[2]) if len(argv) > 2 else ROOT / "workflow_map.html"
    data = parse(src.read_text(encoding="utf-8"))
    out.write_text(render(data), encoding="utf-8", newline="\n")
    nodes = sum(sum(d["counts"].values()) for d in data["diagrams"])
    print(f"Wrote {out} ({len(data['diagrams'])} diagrams, {nodes} boxes, "
          f"{len(data['register'])} register rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

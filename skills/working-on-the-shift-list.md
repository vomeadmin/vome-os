---
name: working-on-the-shift-list
description: Which admin shift-list endpoint (V3, V4, V5) each screen really calls, where a fix belongs, and Sam's rule that extra per-shift work runs only when a request asks for it.
owner: engineering
---

"V4 versus V5" questions come up often (Ron, 868mmya1g). This is the map,
audited 2026-10-24 on core `development` and `master`, web `develop` and
`master`, and mobile `development`. Re-check a caller with `grep_code` before
citing it; call sites move.

**Who calls what.**

- **V5** `/opportunity/v5/institution/opportunity-shifts/within-duration/`
  (`opportunity_app/api_version/v5/api.py`): every admin shift list on web
  (Schedule, Shift data, Bookings, dashboard and insights, kiosk, mass
  actions, export, sites, groups, analytics, chat explore). Mobile uses it
  for opportunity screens, the next-shift card and shift chat.
- **V4** within-duration (`api_version/v4/api.py`): **no caller anywhere**.
  It lives on only as V5's parent class: V5 inherits its request parsing,
  permissions, saved-view loading and filter helpers.
- **V4 insights** (`api_version/v4/insights_api.py`): live, Bookings report.
- **V3** within-duration: mobile's main Schedule and dashboard. Mobile has
  no saved Schedule views.

**Where a fix belongs.** A shift-list fix is done when V5 has it. Never
propose a V4-only change; nobody would see it. The usual bug shape is a step
in V4's own `_get_shifts` that V5 never re-ran: V5 rebuilds scope in
`_v5_scoped_queryset` and V4's body never runs. Example: the saved view's
vacancy rule (fixed 2026-10-24, branch `fix/v5-schedule-view-vacancy`). To
find one, compare every `.filter`/`_apply_*` step in V4's `_get_shifts` with
`_v5_scoped_queryset`.

- Shift-set filters go in `_v5_scoped_queryset`, before the count, so
  `count` and paging match. Filtering the page after it is fetched, in
  Python or in the browser, leaves the count and paging wrong. Never
  propose a browser stopgap for a server filter.
- `spots_booked` in V5 counts people (party size), in the SQL `booked`
  lateral and in `_spots_booked_subquery`. V4's annotation counted
  enrollment rows. Use the people count so a rule agrees with the card.
- A field V5 returns is hand-written in `SHIFT_JSON_SQL`; see
  shaping-a-backend-change.

**Sam's load rule (2026-10-24).** This endpoint feeds about 24 screens and
pages through whole date ranges. Anything that adds per-shift work (a join,
a subquery, roster or custom-field data) runs **only when the request
explicitly asks for it**: a flag the frontend sends from a user action, or a
setting on the saved view the admin picked. Every other request must pay
nothing. Precedents: `include_guest_roster` defaults off on V5,
`include_custom_fields` is capped at 31 days, and the vacancy count is
joined only when the view has a vacancy rule.

So in any proposal for this endpoint:

- say what turns the new work on, and confirm it is off by default;
- an unconditional new annotation, join or prefetch is never low risk,
  even when it is small; say why in the risk, in plain words;
- prefer reusing `schedule_queries.py` helpers over copying V4 code.

# Video studio: marketing videos from Slack

**The reference for how Vome makes feature videos.** Read this before
running, changing or debugging the workflow. The map version is diagram 11
in `WORKFLOWS.md`; the render side is documented in the vome-studio repo
(`HANDOFF.md`, `INTEGRATION.md`, `DESIGN.md`).

**As of:** 2026-10-20

---

## What it does

Someone posts a feature idea in **#video-studio** (`C0C9P2JMEEN`). A team of
agents researches the feature, works out how Vome should position it,
offers three concepts, writes the script for the one a person picks, and
only when a person says so builds a branded video in four formats and posts
a link to watch it on a phone.

Nothing costs real money (voice and rendering) until someone sends
**go build the video**. Everything before that is agent time: about $1.90
for a full round from idea to a reviewed script (see Costs).

Two services do the work:

| Service | Repo | Railway service | Job |
|---|---|---|---|
| Vome OS (this repo) | support-agent | `vome-support-agent` (web) | Slack, the marketing team, thread state |
| Vome Studio | vome-studio | `vome-studio` | Validates scripts, voices them (ElevenLabs), renders, publishes to R2 |

They talk over HTTP only (`VOME_STUDIO_API_URL`, `VOME_STUDIO_API_KEY` on
the web service). Studio never needs a Slack token.

---

## How to use it (for anyone in #video-studio)

**Start a video:** post a new top-level message with the idea and the help
centre link, plus any context you have:

> New video idea from a feature we just shipped.
> https://support.vomevolunteer.com/portal/en/kb/articles/...
> Angle: volunteer managers stuck between an outside email tool and a basic
> built-in one...

The more you say about the angle and what Vome offers around the feature,
the better. What you write about Vome's product counts as a source of fact;
anything only your message supports is flagged for you to confirm.

**No help centre link?** Nothing runs yet. The bot asks the person who
posted for the link, or, if there is no article yet, for as much as they can
say about the feature (what it does, who it is for, where it lives in Vome,
what is new), and asks: "Do you want to continue to build a video with only
the context provided here?" Every detail you add goes into the brief and
the question is asked again, so you decide when there is enough. A link
that cannot be found (a typo, or an article still in draft) gets the same
question.

**Then reply in the thread:**

| You reply | What happens |
|---|---|
| A help centre link, while the bot is asking for one | The team starts on that article |
| More about the feature, while it is asking | Added to the brief; asked again |
| `continue` (also "go ahead", "yes, continue with the context provided"), **as the whole message**, while it is asking | The team starts with only what the thread says; assumptions are flagged on the script |
| `1`, `2` or `3` (also "go with 2", "the second one") | That concept is written up as a script |
| Anything else while concepts wait | Direction: three new concepts, and the strategist may re-position |
| A note on the script ("make the hook say volunteer emails") | The script is revised |
| `go build the video` (also `approve`, `ship it`, "ok, go build it"), **as the whole message** | Builds it. The only reply that spends money |
| `start over` (or `start over, lead with automations`) | Fresh research and three new concepts, same feature |
| `scrap this` (or "kill this one", "discard this video") | Closes the thread. Files stay; it stops counting as an earlier video |
| `I don't like it` (nothing specific) | You get the three options above instead of a guessed revision |
| `forget that` | Drops the lesson the bot just announced |

Rules that protect you:

- **Approval is the whole message.** "Make it clear that..." inside a note
  is a note, and the bot says "I read this as notes, not approval". This
  exists because a note beginning "make it clear" once built a rejected
  script.
- **No link, no spend.** A thread without a help centre link never reaches
  a model until someone posts the link or replies `continue`. The bot never
  guesses which article a feature is about.
- **Approving while a script is being rewritten waits** for the new version.
- **Nothing pulls back a running build**: it is already spending.
- **Slackbot, apps, workflows and reminders are ignored entirely**, so a
  reminder in the channel never starts a video.

When it is built, the thread gets **Watch on your phone** with the 9:16 link
first, then a link for every format.

---

## The team

Each step is a Vome OS agent under `agents/content/`, with a charter
(`charter.md`), a manifest (`agent.toml`: model tier, effort, required
inputs, limits) and an answer key (`evals/cases.jsonl`).

| Step | Agent | Model (tier) | Effort | Reads | Returns |
|---|---|---|---|---|---|
| 1 | `feature_scout` | Sonnet 5.5 (`current`) | medium | FAQ, brief | Parent capability, is it new, what to look up |
| 2 | (code) `_related_articles` | none | | Scout's queries | Up to 8 related help articles |
| 3 | `content_strategist` | Fable 5.1 (`executive`) | high | FAQ, brief, articles, playbook (with competitive), website, earlier videos, lessons | Positioning: audience, landscape, one claim, story shape |
| 4 | `creative_director` | Fable 5.1 (`executive`) | high | Positioning, playbook, website, Studio's formats, earlier videos, lessons | Three concepts |
| 5 | `content_producer` (copywriter) | Opus 5.5 (`premium`) | high | Concept, positioning, Studio's contract for the format, playbook, website, labels, lessons | The storyboard (script) |
| 6 | `brand_editor` | Opus 5.5 (`premium`) | high | Storyboard, concept, contract, playbook, labels, lessons | Revised storyboard + list of changes |
| any reply | `lesson_keeper` | Opus 5.5 (`premium`) | high | Your message, what you were reacting to, current lessons | Lessons to keep, or none |

Tiers resolve in `vomeos/config.py` and can be overridden per environment
(`VOMEOS_MODEL_EXECUTIVE`, `VOMEOS_MODEL_PREMIUM`, `VOMEOS_MODEL_CURRENT`).
The newest models get the server-side refusal fallback, so a false-positive
safety decline reruns on another model instead of failing.

Deterministic checks between steps (`video_concepts.check_concepts`): exactly
three concepts, only ready formats, every hook says "volunteer", no two
hooks open with the same four words, no hook from an earlier video. A
refused set gets one retry with the reason before anything is posted.

---

## What the team knows

| Source | Where it comes from | Used for |
|---|---|---|
| Help centre articles | KB copy in Postgres (nightly sync), or live from Zoho for a link newer than the sync | What the feature does |
| Product Marketing Playbook | ClickUp doc `3dztm-3371`, read live, cached an hour (`video_marketing.py`) | Positioning, the two ICPs, prospect language, messaging, brand voice |
| Website copy | `knowledge_book/feature_catalog.md`, from the landing page by `scripts/sync_landing_strings.py` | How Vome talks about the area in public |
| Product labels | `product_strings` | Exact on-screen wording |
| Your brief | The thread | Angle, and product facts (flagged if nothing else confirms them) |
| House lessons | `video_lessons` table | Standing taste from earlier feedback |
| Studio's contract | `GET /v1/contract?format=` | The exact storyboard shape a format can render |

Playbook pages used are set by `VIDEO_PLAYBOOK_PAGES` (default: positioning,
the April 2026 positioning update, ICP, pain points, prospect language,
messaging framework, content strategy, voice cheat sheet) and
`VIDEO_PLAYBOOK_STRATEGY_PAGES` (competitive landscape, strategist only).
Edit the ClickUp pages and the next video picks the change up within an
hour, no deploy.

**Hard rule:** the playbook names real customers, prospects and revenue. It
is context only. No customer, prospect or competitor name, no attributed
quote and no revenue figure ever reaches a video.

When the landing page copy changes, refresh the website source:

    py scripts/sync_landing_strings.py     # then commit knowledge_book/

---

## The voice rules

The shared skill `skills/keeping-the-vome-video-voice.md` holds the rules
every writing agent follows. Sam's rules, all from real reviews:

- **Say what it is, plainly.** The first line says what Vome does, the way
  you would tell a coordinator. No riddles or stunt lines. The picture may
  be loud; the words are clear.
- **Say "volunteer", and highlight it in the hook.** Volunteer email
  campaigns, volunteer applications; never "users".
- **Step back before the detail.** Most viewers have never used Vome: say
  what Vome does in this area, then land what is new.
- **No em dashes**, anywhere. The validator rejects them.

The brand contract for the picture is `vome-studio/DESIGN.md` (dated rules,
with reasons).

---

## It learns from every reply

Every piece of direction on concepts and every script note goes to the
`lesson_keeper` after the thread's own reply is posted. It keeps standing
taste ("keep hooks to one sentence") and ignores one-offs ("make line 3
shorter"). Each kept lesson is announced in the thread:

> Noted for every future video: ... Reply *forget that* to drop it.

Lessons live in the `video_lessons` table (rule, why, your words, the thread,
which roles) and reach the four writing agents on every run, read live. A
lesson that contradicts an older one replaces it. Lessons never override
sourcing: a lesson cannot make a fact true.

---

## Formats (what a video can look like)

Defined in Studio, `scripts/storyboard.py` `FORMATS`; the team only offers
formats marked `ready`, and plans beats from each format's `can_show`.

| Format | Length | Scenes | Best for |
|---|---|---|---|
| Explainer | 40 to 50s | hook, problem (form mock), ui_demo (rules mock), benefit, cta | A feature that replaces a painful old way |
| Reveal | 25 to 40s | hook (dark beat), optional context cards, optional send in motion, hero (email builder: blocks, Desktop/Mobile, dark inbox), statement, cta | A feature with a visual hero moment, set in the bigger capability |

A new format is a design session in vome-studio (a master HTML, templates in
`scripts/apply_script.py`, an approved example storyboard, `can_show`), then
`ready = True`. Gaps are in the register (G47, G48).

---

## Costs

From production's run records (`vomeos_agent_runs`), a real round on
2026-10-18:

| Step | Model | Cost |
|---|---|---|
| Scout | Sonnet 5.5 | $0.01 |
| Strategist | Fable 5.1 | $0.62 |
| Creative director | Fable 5.1 | $0.63 |
| Copywriter | Opus 5.5 | $0.34 |
| Brand editor | Opus 5.5 | $0.28 |
| **Idea to reviewed script** | | **$1.89, about 4 minutes** |

Direction re-runs the scout, strategist and director (about $1.25). A script
note re-runs the copywriter (about $0.35). A lesson check is about $0.04.
ElevenLabs voice and rendering are billed separately, on build only.

Decided 2026-10-20 (G46): **the strategist and the creative director stay on
Fable 5.1.** Opus 5.5 would save about $0.45 a round, and the concepts are
the step that decides whether a video is any good.

Decided 2026-10-20 (G51): **every video is built with the paid voice
(Maya, ElevenLabs).** A draft voice does not show how a video feels, so
there are no free draft builds.

---

## Where the data lives

| What | Where |
|---|---|
| One row per Slack thread: state, the brief (the first message plus any detail added while the bot asked for a link), who started it (`started_by`), whether it runs without an article (`brief_only`), positioning, concepts, pick, storyboard | `video_threads` (Postgres, web service) |
| Lessons | `video_lessons` |
| Every agent run: agent, model, tokens, time, thread | `vomeos_agent_runs`, `subject_id` = the Slack thread_ts |
| Finished videos, each render's manifest and storyboard | Cloudflare R2 bucket `vome-studio-renders`, `<feature>/<date>/` |

Thread states: `idea`, `awaiting_context` (no link yet, nothing spent),
`concepting`, `awaiting_concept`, `drafting`, `awaiting_review`,
`building`, `delivered`, `scrapped`.

---

## Code map

| File | Job |
|---|---|
| `slack_video_studio_handler.py` | Routes each Slack message: `classify()` decides what it means from the text and the thread's state; `is_automated()` drops Slackbot, apps and reminders |
| `video_studio_jobs.py` | The work: `run_concepts`, `draft_from_concept`, `revise_script`, `restart_concepts`, `build_video`, `capture_lessons`, `_delivery_post` |
| `video_concepts.py` | Pick matching, concept checks, the concepts post |
| `video_threads.py` | The thread table |
| `video_lessons.py` | The lessons table |
| `video_marketing.py` | Playbook, website copy, labels |
| `vomeos/integrations/vome_studio.py` | The HTTP client for Studio |
| `agents/content/*` | The six agents |
| `skills/keeping-the-vome-video-voice.md`, `skills/writing-a-vome-storyboard.md` | Shared rules |

Tests: `test_video_studio_handler.py`, `test_video_studio_jobs.py`,
`test_video_studio_marketing.py`, `test_video_concepts.py`,
`test_vomeos_models.py`. Run `py -m pytest -q`. No test may reach ClickUp,
Zoho, Slack, Studio, a model or the production database.

---

## Changing it

- **A rule from a review:** say it in the thread and the lesson keeper keeps
  it. For a rule that should be permanent and visible, add it to the voice
  skill and to `vome-studio/DESIGN.md` with the date and the reason.
- **An agent's instructions:** edit its `charter.md` (600 word cap, enforced
  by `test_vomeos.py`); shared procedure goes in a skill.
- **A new required input:** add it to the agent's `requires_context` and to
  every case in its `evals/cases.jsonl` (the gate checks both).
- **A model:** change the agent's `tier`, or the tier's model in
  `vomeos/config.py`.
- **Positioning:** edit the ClickUp playbook; no deploy needed.
- **Deploy:** push to `main` (Railway deploys the web service). Studio
  deploys with `railway up --service vome-studio` from the vome-studio repo.

---

## Debugging: "why did the bot post this?"

Every thread records the message that started it and who sent it, and every
agent run is recorded with its thread. Paste this into a Claude Code session
opened in this repo, then paste the message underneath:

```
Explain a message the Vome Agent posted in #video-studio (C0C9P2JMEEN): why
it was sent, what started it, and whether that was intended. Investigate
from the data, do not guess. Read only: no writes, Slack posts, agent runs
or builds. No em dashes. Use VIDEO_STUDIO.md in this repo as the map.

1. Find the thread: read #video-studio around the message's time with the
   Slack connector and get its thread_ts.
2. Read its video_threads row on production (railway ssh --service
   vome-support-agent, write a script to /tmp, run it from /app with
   PYTHONPATH=/app): brief (the first message and any detail added),
   started_by, state, faq_url, brief_only, created_at.
3. Run classify() and is_automated() from slack_video_studio_handler.py
   locally on that text and the thread's state to show which path it took.
4. Read vomeos_agent_runs for subject_id = that thread_ts: agents, models,
   time, and cost (Fable 5.1 $10/$50, Opus 5.5 $4/$20, Sonnet 5.5 $2/$10 per
   million input/output tokens).
5. If there is no faq_url, check whether someone replied "continue"
   (brief_only true). Without either, no agent should have run at all.

Report: one paragraph on what started it and why the bot responded,
whether that was intended or a bug (with the code line), what it cost, and
the smallest fix. Do not change code or deploy until I say.

The message:
```

Railway logs reset on every deploy, so the database is the reliable record.

---

## History

| Date | Change |
|---|---|
| 2026-10-15 | First version: one agent, one help article, Explainer only |
| 2026-10-17 | Marketing team (strategist, creative director, brand editor), concepts before scripts, the Reveal format, plain hooks, "volunteer" in every hook, lessons, start over and scrap, approval as the whole message |
| 2026-10-18 | Playbook and website copy as context; Fable 5.1 and Opus 5.5; phone links; deployed |
| 2026-10-20 | Slackbot, apps and reminders ignored after a 3:55 AM reminder started a video; threads record who started them |
| 2026-10-20 | No link, no spend: a thread without a help centre link asks for one, or for detail, or for "continue"; the keyword search for an article is gone. Fable stays for strategy (G46); every build uses Maya (G51) |

# Hand-off: make every Vome production error reach Sentry, readable

Paste everything below the line into a new Claude Code session opened in
`C:\Users\sfage\Projects\Vome\support-agent`.

---

You are fixing Vome's Sentry coverage. Today only the Django core backend
reports errors well. The web app, the mobile app, the chats service and the
integrations service either send nothing, send unreadable traces, or lose
errors. The goal: every production error from every Vome app reaches Sentry
with a readable stack trace and a release, and the support agent
(`support-agent`, which receives Sentry webhooks and triages them) accepts
and routes it.

Read first: `support-agent/CLAUDE.md`, `support-agent/SENTRY_RUNBOOK.md`,
`support-agent/SENTRY_TRIAGE.md`, and `support-agent/WORKFLOWS.md` (diagram
3 and gap rows G4, G5, G6). Do not start building until you have shown Sam a
short plan and he has said go.

## Rules (non-negotiable)

- Never write a Django migration. Never touch auth, billing or permissions
  code for this.
- Never print, log or commit a secret (DSNs are public by design in client
  apps, but auth tokens are not). Do not read `.env` values aloud.
- Branches: open PRs into `development` (Django repos, Bitbucket) or
  `develop` (vome-react, GitHub). Never push to `master`/`main`. Sam merges
  agent PRs into the dev branches and deploys web to production; OnlyG
  merges core to production.
- `support-agent` is different: a push to its `main` deploys to Railway.
  Show Sam the diff and the test results first. Its local `.env`
  DATABASE_URL is production: never run anything that writes with it.
  Tests: `docker run -d --rm --name vomeos-sprint-test -e
  POSTGRES_PASSWORD=test -e POSTGRES_DB=vomeos -p 127.0.0.1:65432:5432
  postgres:16`, then run pytest with DATABASE_URL, VOMEOS_DATABASE_URL and
  SPRINT_TEST_DATABASE_URL all set to
  `postgresql://postgres:test@127.0.0.1:65432/vomeos`.
- Another Claude session may be editing support-agent at the same time:
  commit by path (`git commit -- <files>`), and check `git diff --cached`
  for someone else's staged changes first.
- No em dashes anywhere. Periods, commas, parentheses or "and".
- If a permission check blocks an action, stop and tell Sam which rule to
  add. Do not work around it.
- Anything that needs Sam's accounts (AWS S3, Sentry admin, GitHub secrets,
  Railway variables, EAS) is a list for Sam with exact values, not something
  you guess at.

## What is already known (verified 2026-10-18, re-check before relying)

**Sentry:** org `vome-2j`. Projects in the receiver's table
(`support-agent/sentry_projects.py:114-244`): `prod-vome`, `dev-vome-app`
(core), `prod-volunteer-database`, `dev-volunteer-database`,
`prod-vome-web` (ref master), `dev-vome-web` (ref develop), and `vome-2j`
mapped to VomeApp (mobile). That last slug equals the org slug: confirm the
real mobile project slug (project id 5454320) with the Sentry API before
trusting it.

**1. vome-react (web) is instrumented but silent.**
- `@sentry/react ^7.120.4`, `src/utils/sentry.js:244 initSentry()` reads
  `REACT_APP_SENTRY_DSN` (line 248) and does nothing when it is unset
  (line 249). Called from `src/index.js:41`. ErrorBoundary reports
  (`src/components/ErrorBoundary.js:47`). Release is
  `vome-react@<window.__VOME_BUILD__.sha>`.
- `.github/workflows/cicd-prod.yml` has no Sentry variable. Line 43 copies
  `s3://vome-frontend-resource-files/env-files/prod/vome-react/env-vome-react-fe-prod.txt`
  over `.env`; the DSN would have to be in that file (unverified, needs
  Sam's AWS access).
- `cicd-prod.yml:55` sets `GENERATE_SOURCEMAP=false`, and
  `SENTRY_AUTH_TOKEN` is never passed, so `scripts/sentry-upload-sourcemaps.js`
  skips (lines 94, 98). Traces stay minified. `cicd-dev.yml` has the same
  pattern.
- `docs/SENTRY_SETUP.md:26` lists the prod and dev DSNs.
- Fix: put `REACT_APP_SENTRY_DSN` and `REACT_APP_SENTRY_ENVIRONMENT` into
  the workflow env (the DSN ships in the bundle anyway), turn source maps on
  in CI with `SENTRY_AUTH_TOKEN` (GitHub secret), `SENTRY_ORG=vome-2j`,
  `SENTRY_PROJECT=prod-vome-web` (dev: `dev-vome-web`) and
  `SENTRY_DELETE_SOURCEMAPS=true` so maps are uploaded, not served. The
  upload's release name must equal the runtime one. Turn on IP scrubbing in
  the project settings (`SENTRY_SETUP.md:132-142`), Sam's step.

**2. VomeApp (mobile, Expo 57, RN 0.86) loses crashes.**
- `src/components/ErrorBoundary.js:32` calls
  `Sentry.Native.captureException`. `Native` does not exist in
  `@sentry/react-native` 8.x (check the installed version), so the call
  throws and every boundary-caught crash is lost. Use
  `Sentry.captureException`.
- `App.js:52-56`: hard-coded DSN, no environment, no release,
  `enableInExpoDevelopment` (a leftover `sentry-expo` option). Add
  environment and a release that matches the source map upload.
- `app.json:66-70`: the Sentry plugin uploads source maps to `prod-vome`
  (the backend project). Point it at the mobile project.
- A store build is needed before users get any of it; say so to Sam.

**3. Chats service (`django-chats/vomedjango-chats-app`) has no Sentry.**
- No `sentry-sdk`; settings via python-decouple; production module
  `vome_chat_project/project_settings/production_settings.py`; Celery
  present (`vome_chat_project/celery.py`); ASGI with Channels/Daphne.
- The pipeline deploys `development` only; production is deployed by hand
  (likely from `master`, unverified).

**4. Integrations service (`django-integrations/vomedjango-integrations-app`)
has the SDK but never starts it.**
- `sentry-sdk==1.39.2` pinned (`requirements.txt:52`,
  `requirements_prod.txt:57`), no `init`. One settings module
  (`vome_integrations/settings.py`), so the environment must come from
  `SENTRY_ENVIRONMENT`. No Celery. Production branch likely `main`.

**The pattern to copy for 3 and 4** is core's
`vome_project/project_settings/sentry_setup.py:269 init_sentry()` (lines
276-293): DSN from `SENTRY_DSN`, `DjangoIntegration` (add
`CeleryIntegration` for chats), release from `GIT_COMMIT_SHA` /
`SENTRY_RELEASE` / `.git/HEAD`, environment from `SENTRY_ENVIRONMENT`,
`send_default_pii` off, `before_send` dropping noise and scrubbing PII.
Core uses `sentry-sdk==2.14.0`; match it. Start nothing when `SENTRY_DSN`
is unset, so local and dev keep working.

**5. The receiver (support-agent) must accept the new projects.**
- Add chats and integrations projects to `sentry_projects.py` (repo, stack
  backend, fix target in `_FIX_TARGETS` at line 327), and drop the
  hard-coded `no_sentry_project` (lines 370-373) and `receiving_nothing`
  (line 377) once each is proven.
- The gate (`sentry_gate.check()`, lines 280-294) drops unmapped slugs, and
  `SENTRY_PROJECT_ALLOWLIST` (read in `triaged_slugs()`, lines 271-289)
  narrows it further. Dev projects need `SENTRY_TRIAGE_DEV_PROJECTS=true`.
- Tests that pin the table: `test_sentry_gate.py:86-103` and `381-387`.
  Docs: `SENTRY_TRIAGE.md:73-95, 135-163`, `SENTRY_RUNBOOK.md:260-283`. Add
  an "adding a Sentry project" checklist to the runbook.
- The Sentry webhook (internal integration) must fire for the new projects:
  a Sam step in Sentry, `POST /webhook/sentry` (`main.py:891`).
- The analyst reads web source only if `VOMEOS_CODE_REPOS` includes
  `vome-react` (it does today) and the trace is unminified (item 1).

## Order of work

1. Plan for Sam: one PR per repo, plus the list of steps only he can do.
2. VomeApp ErrorBoundary fix (the only one losing real crashes today).
3. vome-react workflows (DSN, environment, source maps).
4. Chats and integrations SDK setup.
5. Receiver table, tests and docs (support-agent; diff and tests to Sam
   before any push).
6. Prove each end to end on its dev environment: a deliberate test error
   reaches Sentry with a readable trace and a release, the webhook lands, a
   row appears in `vomeos_sentry_issues` with a triage verdict. Never
   trigger a test error in production without Sam's go-ahead.

## When you finish

- Update `support-agent/WORKFLOWS.md`: diagram 3 (E2, E3, E4 boxes) and gap
  rows G4, G5 and the mobile row, with honest statuses (`built` until
  pushed, `deployed` until proven on a real event, `live` once proven).
  Re-render with `py scripts/render_workflow_map.py` and republish to
  https://claude.ai/artifact/3a7sh6MMwjQ9X2LtJbh2Go (that URL only; if the
  publish is refused because someone published since, merge onto the live
  version).
- Append a dated section to the ClickUp handoff page (doc 3dztm-3311, page
  3dztm-6351) and to `support-agent/SENTRY_RUNBOOK.md`.
- Report to Sam: what is live, what is waiting on him (with exact values),
  and what you could not verify.

# Vome OS in claude.ai

Vome OS as a claude.ai connector. Any claude.ai project (the support sprint,
or a colleague drafting a reply) can ask how the platform actually works, where
a feature lives, whether a request is already logged, and read the code behind
it. It can log feature requests to ClickUp. It cannot send anything to a
customer.

Code: [mcp_connector/](mcp_connector/). Tests: `py -m pytest test_mcp_connector.py`.

---

## What it gives Claude

| Tool | Answers | Source |
|---|---|---|
| `lookup_feature` | Does it exist, what is it called, how does it work, what are the catches | Help centre (Postgres), Setup Guide, UI strings on the live branch, Sprint Lessons Log, lessons from closed tickets |
| `where_in_app` | Which page, and the exact text of its buttons and hints | vome-react routes and components on `master` |
| `check_existing_work` | Is it logged, in flight, shipped, sleeping or declined | The five ClickUp backlog lists |
| `list_repositories` / `search_code` / `read_code_file` | How it really behaves, when the above do not settle it | Bitbucket and GitHub, read-only, live branch by default |
| `log_feature_request` | Log a missing capability, after a duplicate check | ClickUp Raw Intake |
| `add_request_to_existing` | Another customer wants the same thing | +1 comment and Requesting Clients |
| `triage_feature_request` | Set priority and move a request (Sam only) | ClickUp |
| `connector_status` | Which sources are reachable, who you are signed in as | |

Every result says which sources it searched and which it did NOT, so "not
checked" never reads as "does not exist".

### Repositories and the branch each one reads

Verified 2026-10-15 against each repo's CI config and history. Defined in
[mcp_connector/repos.py](mcp_connector/repos.py); override any branch with
`MCP_LIVE_BRANCHES=repo=branch,...` without a deploy.

| Repo | Host | Branch read | Why |
|---|---|---|---|
| vomedjango-restored-core-app (core) | Bitbucket | `master` | pipeline deploys master; `main` is a 2024 stub |
| vomedjango-database-app (db) | Bitbucket | `master` | pipeline deploys master; `main` is a 2024 stub |
| vomedjango-chats-app | Bitbucket | `master` | PRs merge to master; `main` is a 2024 stub |
| vomedjango-integrations-app | Bitbucket | `main` | its only release branch |
| vomesalesforce | Bitbucket | `main` | its only long-lived branch |
| vome-react | GitHub, vomeadmin | `master` | `cicd-prod.yml` deploys master |
| vome-landing-page | GitHub, vomeadmin | `master` | `cicd-prod.yml` deploys master |
| VomeApp | GitHub, samfagen15 | `master` | store build (`sanjay_expo_upgrade`, v3.6.6) promoted to master 2026-10-15 |

Code search itself indexes each repo's default branch; `read_code_file`
then reads the branch above. The code tools use their own allowlist,
`MCP_CODE_REPOS` (default: all of the above), so they never widen what the
autonomous Sentry analyst can read through `VOMEOS_CODE_REPOS`.

Token reach: the Bitbucket token is workspace-wide, so all five Bitbucket
repos are covered. The two GitHub tokens are fine-grained and list their
repositories explicitly: the vomeadmin token must include
`vome-landing-page` as well as `vome-react`.

---

## One-time setup

### 1. Google sign-in client

Google Cloud console, the project that owns the vomevolunteer.co Workspace:

1. **APIs & Services, OAuth consent screen.** User type **Internal** if every
   person who will connect has a vomevolunteer.co Google account. If some
   colleagues only have a vomevolunteer.com (Microsoft) address, choose
   **External** and add them as test users, or have them create a Google
   account on their .com address.
2. **Credentials, Create credentials, OAuth client ID**, type **Web
   application**.
3. Authorised redirect URI: `https://<railway host>/oauth/google/callback`
4. Keep the client ID and secret for step 2.

### 2. Railway variables (web service)

| Variable | Value |
|---|---|
| `MCP_PUBLIC_URL` | `https://<railway host>`, no trailing slash |
| `MCP_GOOGLE_CLIENT_ID` | from step 1 |
| `MCP_GOOGLE_CLIENT_SECRET` | from step 1 |
| `CLICKUP_TEAM_ID` | the workspace id, for the Lessons Log |
| `VOMEOS_GITHUB_TOKEN_VOMEADMIN`, `VOMEOS_GITHUB_TOKEN_SAMFAGEN15` | already used by Sentry triage; the connector downloads vome-react and VomeApp with them |

Optional: `MCP_ALLOWED_DOMAINS` (default `vomevolunteer.co,vomevolunteer.com`),
`MCP_ALLOWED_EMAILS` (individuals outside those domains), `MCP_TRIAGE_EMAILS`
(default Sam's two addresses), `MCP_FEATURE_REQUEST_LIST` (default Raw
Intake).

The connector stays OFF until the first three are set. Deploy, then check
`GET /health`: `mcp_connector.on` should be `true`.

### 3. Add it in claude.ai

Org owner, once: add a **custom connector** in the organization's connector
settings with the URL `https://<railway host>/mcp`. Leave the OAuth client
fields empty; it registers itself.

Each person, once: open Settings, Connectors, find Vome OS, **Connect**, and
sign in with their Vome Google account. Then turn it on in the chat or project
where they want it.

---

## Paste into the Support Sprint project instructions

```
You have the Vome OS connector. Use it before answering any question about
how Vome works.

1. lookup_feature first. Prefer help centre steps and link the article.
2. where_in_app when the reply needs "go to X, click Y". Name pages and
   buttons exactly as text_on_this_screen shows them.
3. check_existing_work before saying anything is fixed, known, planned or
   coming. Only a shipped status means fixed.
4. If the sources do not settle it, search_code then read_code_file. Use what
   you learn to get the answer right, never quote it.
5. If the customer is asking for something Vome does not do, call
   log_feature_request with their organization and the Zoho ticket link. If it
   returns possible_duplicates, pick the match and call
   add_request_to_existing, or confirm_new=true if none match. Tell me which
   you did, then ask me for a priority and placement.
6. A logged request is "logged with the product team", never "on the roadmap".
7. Never put a file path, function, translation key, branch or task id in a
   customer reply. If a source says NOT searched, the answer is unknown.
```

---

## Feature requests

1. `log_feature_request` searches all five backlog lists first. If anything
   looks similar it creates nothing and returns the candidates.
2. Repeat request: `add_request_to_existing` adds a +1 comment and appends the
   organization to Requesting Clients.
3. New request: lands in **Raw Intake**, Type Feature, no priority, with who
   logged it and the ticket link.
4. Sam replies with a decision, for example "high, priority queue, assign to
   me", and Claude calls `triage_feature_request`. Decisions: `priority_queue`,
   `accepted_backlog`, `sleeping`, `declined`, `keep_in_intake`. Priorities:
   `urgent`, `high`, `normal`, `low` (the title gets `-- P1` to `-- P4`).

Writes are refused on any task outside those five lists.

---

## Access, audit, offboarding

- Sign-in is Google, restricted to `MCP_ALLOWED_DOMAINS` and
  `MCP_ALLOWED_EMAILS`, and re-checked on every call, so removing a domain or
  address cuts access on the next request.
- Only claude.ai, claude.com and localhost may register as OAuth clients.
- Tokens are stored as SHA-256 hashes in `mcp_oauth_tokens`. Access tokens
  last 8 hours, refresh tokens 30 days and rotate on every use.
- Every tool call is logged in `mcp_tool_calls`:
  `GET /mcp-admin/calls` (ops token).
- **Offboarding:** `POST /mcp-admin/revoke?email=<address>` (ops token).
  Needed because a refresh token outlives the Google account it came from.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| claude.ai says it cannot connect | `/health`, `mcp_connector.on`. Then `/.well-known/oauth-protected-resource/mcp` should return JSON. |
| Sign-in ends with "not allowed" | The Google account's email domain is not in `MCP_ALLOWED_DOMAINS`. |
| `ui_text` or `where_in_app` says NOT searched | `connector_status`, `frontend_source.error`. Usually a missing or expired GitHub token. |
| `sprint_lessons_log` NOT searched | `CLICKUP_TEAM_ID` unset. |
| `help_centre` NOT searched | `DATABASE_URL`. |
| Everyone signed out after a deploy | `connector_status`, `sign_ins_survive_restart` false means no database. |

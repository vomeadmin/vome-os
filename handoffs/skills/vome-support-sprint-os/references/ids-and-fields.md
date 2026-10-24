# IDs and custom fields

Snapshot of System Configuration v1.2 (with the Oct 15 VomeOS note and the Oct 19 "Superseded by Playbook v2.1" note), Engineering Slack Channels and Dev IDs (updated Oct 19, 2026), the VomeOS page (v1.0, Oct 15) and the IDs in CS Agent Workflow and Handoff, all pulled on Oct 19, 2026. If the live docs differ, the live docs win. **Where any of those pages disagree with Playbook v2.1, the Playbook wins** (see `sprint-playbook.md`). The System Configuration page says its IDs, custom fields and list structure are still current; its sending and routing sections are superseded.

ClickUp doc pages: Playbook `3dztm-3311` / `3dztm-1871`, Lessons Log `3dztm-3311` / `3dztm-2491`, CS Agent Workflow and Handoff `3dztm-3311` / `3dztm-6351`, System Configuration `3dztm-3331` / `3dztm-1891`, Engineering Slack Channels and Dev IDs `3dztm-3331` / `3dztm-2911`, VomeOS Code Access and Repo Map `3dztm-3331` / `3dztm-6331`.

## Contents
- Team
- Zoho Desk
- Slack
- ClickUp lists and spaces
- ClickUp custom fields
- ClickUp statuses
- Creating a task: parameter shapes

## Team

| Name | Role | ClickUp ID | Zoho Agent ID | Slack ID |
|---|---|---|---|---|
| Sam (Sammy, Sam Fagen) | CEO, full-stack engineer, primary reviewer | 3691763 | 569440000000139001 | U01B9FQNSBU |
| OnlyG | Lead backend engineer | 49257687 | 569440000023160001 | U0APZ7JUHRD |
| Sanjay | Frontend engineer (web and mobile) | 4434086 | 569440000023159001 | U0AQJ8YJF6Y |
| Ron | Sales, field feedback, support sprint operator | 4434980 | 569440000000192003 | U01B6D235N1 |

Zoho team owner IDs used for ticket owner updates (the OnlyG and Sanjay agent IDs double as the team owners):
- Frontend Team: `569440000023159001`
- Backend Team: `569440000023160001`
- Agent Fixes (a Zoho team, not an agent; set `teamId` with `assigneeId` null): `569440000043486190`

Engineer names are for internal use only. They never appear in client-facing content.

## Zoho Desk

- Org ID `736165782`
- Department ID `569440000000006907`
- From address: support@vomevolunteer.zohodesk.com
- Team addresses excluded from client-reply detection: admin@vomevolunteer.com, sam@vomevolunteer.com, s.fagen@vomevolunteer.com, r.segev@vomevolunteer.co (the `.co` is intentional, never "correct" it to `.com`)
- Statuses: New, Processing (client replied), In Progress, Awaiting Client Response, Closed
- The excluded-address list applies to client-reply detection on existing tickets. It is not applied at ticket creation, so a team outbound email that cc's support can create a phantom ticket (tracked as `868m05epv`).

Tool notes (tool names vary by environment, the behaviour is what matters):
- Status and owner bulk updates need plain string values (`"In Progress"`, `"569440000023160001"`). The object form `{"value": ...}` returns success and changes nothing.
- Setting status inline through the reply call is the most reliable, but only after approval of the reply. It sets status, not owner.
- Private comments use `isPublic: false`, content type HTML.
- Thread reading is two steps: list the ticket conversations, then fetch each thread as plain text.
- Oldest-first queue pulls sort by customer response time.
- A closed or awaiting-client ticket reopens to Processing when the client replies. That is expected, not a failed close.
- Inside HTML, `#1234` patterns can auto-convert to ticket chips. The underlying href still works.
- Zoho does not accept "Escalated" as a status through the API (sendReply or updateTicket). Set it manually if needed.
- Replying to a client: `sendReply` with `channel: EMAIL`, `fromEmailAddress: support@vomevolunteer.zohodesk.com`, `to`, `cc` (latest message audience plus `secondaryContacts`), `contentType: html`, `inReplyToThreadId` (the latest client thread), and `ticketStatus` when the reply should also change status (for example `Closed` on a how-to).

## Slack

Bug notices go by the Playbook v2.1 Slack routing (updated Oct 19, 2026):

| Channel | ID | Use |
|---|---|---|
| #bug-reports | C0CAJ269RAQ | Every bug notice Claude posts, frontend or backend, ticket or internal, JavaScript error reports included. Tag the assigned engineer |
| #vome-feature-requests | C0ALL4VPWK0 | Feature requests from tickets (`log_ticket_request`), backend hand-offs, new functionality, scored pings |
| #feature-releases | C0BCSQKM2N8 | Newly shipped or updated UI/UX, with branch and what to test |
| #support-sprints | C0CA1QB2XSQ | Sam's ping for urgent work only (4,000 CAD+ ARR account, or the same problem from a second account) |
| #vome-agent-log | C0AMGELBDB2 | Send log, auto-sends, Monday autonomy report |
| #vome-support-engineering | C0ALJPCAE93 | Combined P1 escalations (OnlyG and Sanjay) |
| #vome-field-feedback | C0AL6NTJP8F | Ron and Sam. Field feedback intake |
| #vome-tickets | C0AMTJA2UTE | Ticket brief channel |
| #eng-backend | C0AQ5U0GXEF | No longer receives bug notices (since Oct 19, 2026) |
| #eng-frontend | C0AQ5TWQPTM | No longer receives bug notices (since Oct 19, 2026) |

The agents' automatic routing posts nothing in Slack. The connected Slack account is Sam's (`U01B9FQNSBU`), so messages post as Sam. It was not a member of #vome-support-engineering as of Sep 30; if a combined P1 post fails, post it in #bug-reports with both devs tagged and say so.

Ron Protocol: when a No-Zoho task (Zoho Ticket Link empty and no Zoho ticket number in the description) was created by Ron and goes on prod, DM Ron (`U01B6D235N1`) with the task link saying the fix is on prod and the task is closed, then close the task. Do not flag to Sam. Never send Zoho replies for No-Zoho tasks.

## ClickUp lists and spaces

- Team (workspace) ID `3604276`
- Vome Product space `90114113004`
- Customer Success + Support space `90110703798`

| Folder | List | ID |
|---|---|---|
| Master Queue | Priority Queue (active bugs and tickets) | `901113386257` |
| Master Queue | Done | `901113386518` |
| Master Queue | Agent Fixes (agent PRs; same statuses as Priority Queue) | `901115573468` |
| Feature Requests | Sam Accepted (Build Now) | `901115573620` |
| Feature Requests | Raw Intake (new feature requests land here) | `901113386484` |
| Feature Requests | Accepted Backlog | `901113389889` |
| Feature Requests | Sleeping | `901113389897` |
| Feature Requests | Declined | `901113389900` |
| Customer Success + Support | Playbooks and SOPs | `901113584932` |

## ClickUp custom fields

Dropdown fields take the option UUID. The Tags field is a labels field, so it takes a JSON array of UUIDs as a string. Platform and Module IDs are easy to swap, so verify after creation.

**Type** `e0e439f5-397d-432d-addd-e90fbf50cd30`
- Bug `d9c82e67-c46b-48d1-95f7-9c1f5b2fc2df`
- Feature `41a1ea4e-eec9-418d-a684-3c17cdd8dd67`
- UX `da749879-7b3a-4fd5-a1cb-c85fcb719569`
- Improvement `9864f852-39cc-481c-aafc-c2f2ebdba30b`
- Investigation `f9bd67bb-5b85-49fb-bd4a-21295f01cf5a`

**Platform** `5f1ff65b-18fc-49db-89aa-2c1f355ec1e7`
- Web `946c8214-6a65-4e63-a437-d98415dc1439`
- Mobile `070470c3-c248-4d64-8ceb-8c95df82506b`
- Both `2d69c526-e58f-4486-bc6a-168cd812f0bf`

**Module** `3f111d48-e92a-4d5e-92d9-e193c80b20cc`
- Volunteer Homepage `1fd64528-970a-48da-8881-9a0fb4ac96f4`
- Reserve Schedule `197109a7-2974-4210-94f1-a1e97990830f`
- Opportunities `af0b8949-5281-4655-8326-c77dcfb2ecf7`
- Sequences `04d7e808-d94f-48bb-b5eb-f567e6cf41ca`
- Forms `aa6f6c17-7260-44fd-a862-99daaf7d77c0`
- Admin Dashboard `b13da71b-46ab-45c6-82ab-fa37a18fc0b3`
- Admin Scheduling `f36d0e31-2f2b-4044-bb5c-fb4fef06cb74`
- Admin Settings `d9d9051c-d733-4ac0-8607-1a75153b021b`
- Admin Permissions `bf68973f-f858-4083-9a60-7dc014b6e1f3`
- Sites `5f5cc57b-6259-4bab-88dd-64b3a34036f1`
- Groups `36178405-828f-4471-80a4-cedb2eb0be59`
- Categories `f4f5021b-d528-41c1-b832-87a67e5ba0ae`
- Hour Tracking `53f02923-e8a5-4c30-8a42-c2bddaa75778`
- Kiosk `d92abcaa-e2a3-46f6-bb99-2025aa3984d3`
- Email Communications `c2c3a5ae-e8db-4da5-9ab3-736c3a76b66f`
- Chat `a4d5a4bd-049b-4dfa-b6c2-2843661faad4`
- Reports `95a6e4bf-eaa0-4cd4-87ed-9298a37da0c6`
- KPI Dashboards `ef0db184-315f-4420-bccb-bb7dee73e7a1`
- Integrations `938a5549-70dd-40f2-9db2-4176d10b4221`
- Access / Authentication `fd457e45-e25c-4910-871f-fe67bf5391d3`
- Other `cbe38d18-9d9f-4e49-abf5-5101f09349ff`

**Source** `857e1262-cb5c-4c22-b8da-770a8fcfa82e`
- Migration `21681d2f-d0b7-4eb5-9fce-5f41600ffe6f`
- Zoho Ticket `9b678f29-3b49-4842-9305-ada436cfc0b3`
- Field Feedback `ef5fcb3c-c27d-443c-bef2-32be1521baf1`
- Internal `ea82838e-f5ee-4cc6-b5d3-da4ef9052343`
- Roadmap `0a60ef2b-bb3c-4023-a21a-ad1375c84ef8`

**Tags** `291fd0e7-42e3-4376-bacd-2b54a6c1d48c`
- Notification `2eaf6034-ae01-42da-ace7-5832cfc3e44e`
- Performance `427c7f85-ee11-4cd0-99d2-10dcb67eb4c0`
- Security `a580d60c-889a-4e5e-a199-d89dc5bb2001`
- Data `0bf3c2d8-2ac2-44c5-b214-0acfbfd94fd3`
- Regression `743fb482-4c9c-4e64-a33f-2d8437c7b9f7`

**Other fields**

| Field | ID |
|---|---|
| Highest Tier | `be348a1d-6a63-4da8-83bb-9038b24264ff` |
| Requesting Clients | `e2de3bd0-6ad9-4b31-bb09-104f6bef383d` |
| Combined ARR | `29c41859-f24b-4143-9af4-a34202205641` |
| Auto Score | `fd77f978-eca8-499e-bc3c-dc1bf4b8181e` |
| Sprint Batch | `97b38eb3-4416-40c9-9e27-fd26b0174849` |
| Wake Date | `701fdb31-1341-426a-be88-23d2e10edfec` |
| Release Note | `49f6daf4-1eba-4ec9-9102-f5140a9f81c5` |
| Notified Client | `479a95ce-5129-42d7-8fc6-48fcccc2ce7e` |
| Design Spec | `723d8e39-a6b1-40b3-9154-0d64f843313d` |
| Zoho Ticket Link | `4776215b-c725-4d79-8f20-c16f0f0145ac` |
| Resolution | `63ef3458-cfa6-4a0b-ae44-18858cd555f0` |

The Resolution field's option IDs (Completed, Declined, Sleeping, Duplicate) have not been fetched. Look them up with `clickup_get_custom_fields` before setting it.

Highest Tier and Combined ARR field types were not documented. Check the field type with `clickup_get_custom_fields` before writing a value, and skip them rather than guessing a format.

**Agent Fixes list fields (list level only, not visible in Priority Queue):** Fix Commit `ee6d6f9b-2093-40df-846c-4c5e7003f2c5` (format `core@sha; web@sha`), Merge Owner `12682083-5a03-4f27-a6b6-5c716aa5dd73` (people; Sam for agent fixes), Release Check `6d2d0de6-298f-40b3-b628-ab2e9633f6e7` (Waiting / Failed / Live). Structure: one parent task per fix (no Zoho link), one child per Zoho ticket (each with its own Zoho Ticket Link, so every client gets a resolution). These are managed by the agents through `propose_fix` and the release watcher; do not edit them by hand.

## ClickUp statuses (Priority Queue)

| Status | Meaning |
|---|---|
| queued | Not started, engineer's active queue. A routed task stays here until the client is acknowledged |
| in progress | Engineer actively working |
| on dev | Deployed to dev |
| needs client info | Engineer needs detail from the client. The needs-client-info handler sends the info-request automatically (Playbook v2.1) and sets `awaiting client` |
| awaiting client | Info-request sent. Hidden from engineer views, resurfaces to `queued` on a genuine client reply |
| escalated | Engineer handed it to Sam. Summarize, no client reply until Sam decides |
| on prod | Fix deployed. The on-prod handler emails the client and closes both systems automatically. Never set `on prod` yourself, and never on an Agent Fixes task |
| sleeping | Parked until the Wake Date |

OOO autoresponders and emoji reactions are not client replies. Never resurface an `awaiting client` task because of one. Check the task comments to see why the engineer changed the status.

## Creating a task: parameter shapes

`clickup_create_task`:
- `list_id`: `901113386257`
- `name`: task title
- `assignees`: one user ID as a string in an array, for example `["4434086"]`
- `priority`: `urgent`, `high`, `normal` or `low`
- `markdown_description`: see `templates.md`
- `custom_fields`: array of `{id, value}` where every value is a string. Dropdown value is the option UUID. URL value is the URL text. Labels value is a JSON array string such as `["2eaf6034-ae01-42da-ace7-5832cfc3e44e"]`.

Example `custom_fields` for a web forms bug from a Zoho ticket:

```json
[
  {"id": "e0e439f5-397d-432d-addd-e90fbf50cd30", "value": "d9c82e67-c46b-48d1-95f7-9c1f5b2fc2df"},
  {"id": "5f1ff65b-18fc-49db-89aa-2c1f355ec1e7", "value": "946c8214-6a65-4e63-a437-d98415dc1439"},
  {"id": "3f111d48-e92a-4d5e-92d9-e193c80b20cc", "value": "aa6f6c17-7260-44fd-a862-99daaf7d77c0"},
  {"id": "857e1262-cb5c-4c22-b8da-770a8fcfa82e", "value": "9b678f29-3b49-4842-9305-ada436cfc0b3"},
  {"id": "4776215b-c725-4d79-8f20-c16f0f0145ac", "value": "https://desk.zoho.com/..."}
]
```

Status updates, comments and field fixes after creation need `clickup_update_task` and `clickup_create_comment`. Load them with `tool_search` if they are deferred.

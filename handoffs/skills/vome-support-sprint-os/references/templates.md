# Templates

All templates follow the no em dash rule. Replace the bracketed parts. Internal templates (ClickUp, Zoho private comment, Slack) can name engineers. Client-facing templates never do.

Aligned with Playbook v2.1 (Oct 19, 2026), which wins over this file on any difference. On the agent path the reply drafter writes client drafts and `send_ticket_reply` sends them, so the client templates below are mainly for the manual path and for checking a draft.

## Contents
- ClickUp task description
- Zoho private comment
- Engineer Slack notice (#bug-reports)
- Client info-request
- Client bug acknowledgment (draft for approval)
- Misdirected ticket
- Account deletion
- Resolution reply (automatic)

## ClickUp task description

```markdown
**Zoho ticket:** [#NNNN](zoho-ticket-url)
**Client:** [Org name] ([tier], Combined ARR [amount or unknown]) [IMPORTANT CLIENT or standard]
**Reported by:** [name], [email]
**Affected user(s):** [volunteer or admin email(s), or "not provided"]

**What the client reported**
[Plain description in the client's own terms. Quote error text exactly.]

**Steps to reproduce**
[Steps if given, otherwise "Not provided".]

**Latest message asks**
[What the most recent message actually wants.]

**Environment**
[Web or mobile, browser or device, build ID from any error report.]

**Attachments**
[Zoho attachments cannot be read through tools. Say what exists: "Client attached 2 screenshots and a screen recording, see the Zoho ticket."]

**Related**
[Existing tasks, same symptom on other accounts, timezone or schedule display clusters.]

**Executive summary (from the code)**
- Likely cause: [one or two sentences on what the code shows]
- Where: [repo, file path, line range]
- Potential fix: [what a fix could look like]
- Confidence: [high / medium / low, and why]
- Not verified: [what the code could not settle: account data, prod vs master, mobile build, etc.]

[If nothing solid turned up, write "Code check inconclusive" and list what was searched, so the engineer does not repeat it.]

**Notes for the engineer**
[Anything internal. If data may be affected, say so here, for example "check whether the submission data can be recouped". This is internal only, never a client promise.]
```

Keep the title in the style of recent Priority Queue tasks. A fallback that reads well: `[Org] #NNNN: short symptom`.

## Zoho private comment

Create with `isPublic: false`, content type HTML.

```html
<p>ClickUp task created: <a href="https://app.clickup.com/t/TASKID">Task title</a></p>
<p>Routed to [Frontend or Backend] team. Zoho status left as is: the client is still owed a reply from Sam.</p>
```

Do not write "Status set to In Progress" when routing. Only Sam's sent acknowledgment moves the ticket to In Progress (Playbook Rule 6).

## Engineer Slack notice

Every bug notice Claude posts goes to #bug-reports (`C0CAJ269RAQ`) with the assigned engineer tagged (Playbook v2.1). If the hand-off came in a Slack thread, confirm in that thread too. A few lines, no recap, one @-mention.

```
@Sanjay [one-line summary]. Likely cause: [one line]. ClickUp: <url|task title> · Zoho: <url|#NNNN>
```

Backend notices tag @OnlyG. The full executive summary lives in the ClickUp task, not in Slack. #eng-backend and #eng-frontend no longer receive bug notices. The agents' automatic routing posts nothing.

## Client info-request

When an engineer moves a task to `needs client info`, the needs-client-info handler sends this automatically (Playbook v2.1). Draft it yourself only for an info-request Sam asks for, such as the Bonnie pattern, and it waits for his "send". Strip any diagnosis. Never reveal what the engineer suspects.

```
Hi [Name],

Thanks for flagging this for us. To help our team look into it, could you send us [specific item 1] and [specific item 2]?

Our team will take it from there and we'll follow up with any questions or updates.

Sam | Vome team
support.vomevolunteer.com
```

For third-party reports (the Bonnie pattern), ask for the volunteer's email address, the name of the form or opportunity, and what happens when they try.

## Client bug acknowledgment (draft for approval)

One line, no timeline (Playbook v2.1). Follows the No-Confirmation Rule: no "we can see", "we've confirmed", "we've identified", no named component. Always waits for Sam's explicit "send".

```
Hi [Name],

Thanks, our team is going to look into this.

Sam | Vome team
support.vomevolunteer.com
```

Do not use this on raw JavaScript or console error reports. Those are agent hotfixes closed quietly with no client reply.

## Misdirected ticket

```
Thanks for reaching out! Vome is a technology company that helps organizations streamline their volunteer management operations. We are not the organizer of the event itself. For [activity], we recommend reaching out to [org] directly. They will be able to assist with registering for the event and any onboarding steps required.
```

## Account deletion

```
You can delete your account directly from Vome by going to your Settings page and selecting the Delete Account option. If you run into any trouble with that, just let us know and we'll help get it sorted.
```

Do not create a ClickUp task for these.

## Resolution reply (automatic)

Resolution emails send and close on their own: the on-prod handler when an engineer moves a Priority Queue task to `on prod`, and the release watcher when an agent fix is proven live. Do not draft or send one by hand, and never set `on prod` yourself. A resolution a guard held shows up in `sprint_followups` for Sam; it is two or three short sentences worded for the client.

## Before presenting any client draft

1. No engineer names (Rule 1).
2. No-Confirmation Rule respected.
3. No claim of a fix, a roadmap item or safe data without a verified task behind it. Forward-looking lines need a task or the reviewer's explicit go-ahead. Nothing from the code in code terms (no file paths, function names, translation keys, branches, commits or task IDs).
4. Client's language, with accents if French.
5. Recipients: latest message audience plus the ticket's `secondaryContacts`. Per the Sep 30 Lessons Log entry, every Kimberly Crest reply CCs janetmiller1320@gmail.com. Wellspring replies CC the listed secondary contacts. Read the ticket's `secondaryContacts` field rather than inferring the list.
6. No em dashes, no corporate filler ("please do not hesitate to reach out"), no quality adjectives about attachments, no offer of a call.
7. Signed "Sam | Vome team" with support.vomevolunteer.com (English sign-off on French replies too).
8. If the reviewer gave an instruction rather than verbatim text, the composed draft has been shown and approved.

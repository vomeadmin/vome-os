---
name: checking-the-data
description: Decide whether a ticket needs one account's records checked before anyone answers or fixes it, and when it does, write the exact questions and a read-only query Sam can paste into the prod shell.
owner: customer_success
---

Code tells you what Vome does. It cannot tell you what happened to these
people. When the ticket is about specific records (a named volunteer, a
file, a shift, an hour log, a date), the records are the evidence, and a
cause the code makes plausible is still a guess until they agree.

#9760 (UMMS, 2026-11-05): the code check found a real bug (an admin's
replacement upload never closed the open request) and an agent PR shipped.
The client's case was not fixed. Three read queries showed the uploads had
never saved at all. Nothing on the card asked whether the bug was what
happened to these four people.

**Add this to the card:**

```
"data_check": {
  "needed": "none" | "confirm" | "required",
  "why": "<what the code cannot show, one sentence>",
  "hypothesis": "<what the records show if the code check is right>",
  "questions": ["<one fact to look up, by name>", ...],
  "query": "<paste-ready lines for the prod Django shell>",
  "settles": "<if X: reply or fix A. If Y: B>",
  "found": "<only after results came back: what they showed>"
}
```

**Pick the level.**

- **required**: there is no honest answer without the records. "These
  people still show X", "her hours are missing for this shift", "I
  uploaded it and it is not there", "this volunteer got the email twice",
  an import or a delivery that went wrong for one account. Nothing gets
  fixed or explained until the check is done. The lane is still the lane
  the ticket would have without the data (usually `answer` or `sam_ops`);
  never `engineer` just to have someone look a record up.
- **confirm**: the code check found a likely cause (`bug`, medium or high)
  but the ticket names specific records. The fix can go ahead, because the
  code is wrong either way. The records decide whether it is *their*
  cause, so the "fixed" email waits for them. #9760 is this case.
- **none**: how-tos, feature requests, a screen that looks wrong for
  everyone, anything the code alone settles.

Rule of thumb: **if the ticket names specific people, records or dates,
the check is at least `confirm`.**

**Write the questions as facts, by name.** "Does James McCauley have a
saved Flu Shot Record upload, and when?" not "check the uploads". Use the
names, emails and dates from the thread. One fact per line.

**Write the query for the shell Sam actually uses.** It is the production
Django shell (`python manage.py shell`, a plain console) on the core box.

- Read only. Never `.save()`, `.delete()`, `.update()`, `.create()`, raw
  SQL, `connection`, files, or network calls. Anything like that is
  stripped before Sam sees it, so the query breaks.
- Every manager reads the replica: `Model.objects.using("readonly")`.
- One statement per line. Loops as one-line comprehensions:
  `print([(u.id, u.email) for u in User.objects.using("readonly").filter(last_name__icontains="stokes")])`.
  A `for` block over several lines breaks in the console.
- Import the models on their own lines first, from their real modules (you
  read the code: use the module and field names you saw).
- Find people loosely: clients misspell names ("Sirliliar" was
  "Sirlilar"). Search the last name with `__icontains` inside the
  organisation, then narrow.
- Print labelled results (`print("OPEN", ...)`) so the paste reads on its
  own, and print ids and dates, not whole objects.
- Keep it short. Three to eight lines that answer the questions beat a
  dump.

**When results come back** (`data_results` in the context), they are the
evidence. Read them before anything else:

- They settle it: `needed` becomes `none`, `found` says in one line what
  they showed, and the rest of the card (category, lane, code check, the
  recommended action) follows the records, not the earlier guess.
- They raise a new question: keep the level, write the next questions and
  query, and say in `found` what the first round ruled out.
- They contradict the code check: say so in the situation. The records
  win over a plausible reading of the code.

**Never let it reach the customer.** Model names, fields, queries and what
the records showed stay on the card. The customer hears what we found in
product words, from Sam.

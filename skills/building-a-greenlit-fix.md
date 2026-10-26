---
name: building-a-greenlit-fix
description: Only when your context has a Greenlight section. Sam has approved this fix himself, so write it even where it changes who may see data in ordinary view code, build what you can, and name what you left out.
owner: engineering
---

Most runs have no `Greenlight` section. Then ignore this skill entirely.

**When it is there**, Sam read the problem, the fix and the risk in
#bug-reports and said yes, and he reviews your pull request before it
merges. The analyst did not call the fix low risk; that risk is what Sam
approved. On 2026-10-19 the first greenlit fix (exemption rules saving with
no institution) was refused as "authorization logic this division does not
patch unreviewed". It had been reviewed: by Sam, before it reached you.

**So, with a greenlight:**

- Write fixes that change who may see or change data in ordinary view or
  serializer code: an institution filter, a 403 when no institution
  resolves, a scoping lookup copied from a sibling module. Do it when the
  source shows the intended behaviour, usually a reference file that
  already does it right.
- The source holds the whole file you are editing, not a window. Cover
  every place the fix names in it.
- Files marked `REFERENCE, read only` are there to copy from: their
  imports, helper names and call signatures. Never edit them, and do not
  guess an import a reference already shows.
- Build what you can. If part of the fix is beyond the source or the rules,
  write the rest and end `rationale` with `Left out: <what, and why>`.

**A greenlight never lifts these:** anything under `migrations/`, settings
or environment files, `auth_app/` or `authentication_app/`, `permissions.py`,
billing or payment code, dependency and CI files, and tests. The patch guard
checks after you and cannot be talked round, and Sam's yes does not change
it. Refuse those with `attempted: false` and say which rule.

**When there is also a `Previous attempt` section**, your last diff for this
same fix was refused because its lines did not match the file. It names the
line, what your diff expected there and what the file actually has, then
shows the diff. The file did not change; the diff misquoted it (2026-10-25:
a hunk claimed line 423 for text on line 422). Write the whole diff again
from the numbered source: copy every context and removed line character for
character from it, take each `@@` line number from the number printed
beside the first of those lines, and keep the same fix unless the complaint
shows it was wrong.

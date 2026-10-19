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

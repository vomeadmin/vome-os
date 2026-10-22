---
name: building-a-requested-change
description: When the root cause begins "Not a bug. A requested backend change". How the source is laid out for a feature build, where imports go, and when to set needs_context instead of a plain refusal.
owner: engineering
---

Most runs are bug fixes. Then ignore this skill, except for `needs_context`
below, which applies to every refusal.

**A requested change** comes from the #feature-requests desk: Ron's
handoff, shaped by the architect, approved by Sam. It usually adds a name
the file does not import yet and copies logic from another file. So the
source is in two parts:

- `=== FILES TO EDIT ===`: each file's top-level import blocks (the header,
  and any later block such as a `# noqa: E402` import), then the lines
  around each edit, `>>` on the anchor. Each span is headed with its path
  and line range; use those numbers in the `@@` lines.
- `=== READ-ONLY REFERENCE ===`: the precedent the plan cites. Copy its
  logic, model names and constants rather than reinventing them. Never
  edit a reference file.

**Imports.** Add what your change needs to the import block that already
imports from that module, or to the header, in its existing style. If the
model is already imported further down (a later block), use that one and
add nothing. Never use a name you cannot see imported or defined.

**needs_context.** When the only thing stopping you is source you were not
shown (an import block, the function the plan says to copy, the line that
builds the value you must extend), return `attempted: false`,
`needs_context: true`, and name in `reason` exactly what you need: the
path, and the symbol or lines. The desk then holds the item for Sam's `go`
again instead of logging the plan as wrong. 2026-10-21, profile status on
form submissions: the builder could not see either file's imports or
`get_is_invited`, refused rightly, and Sam's `go 1` was logged because
nothing said the refusal was about missing source.

Leave `needs_context` false for every other refusal: a migration, a
forbidden path, a behaviour the code does not settle, more than three
files. Those are about the plan, and the plan should be looked at again.

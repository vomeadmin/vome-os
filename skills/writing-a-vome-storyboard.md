---
name: writing-a-vome-storyboard
description: The exact JSON shape of a Vome Studio storyboard in any format, worked from the contract Studio supplies for that format: structure, on-screen keys, cues, captions and emphasis.
owner: content
---

You are given the **storyboard contract** from Vome Studio for one video
format: the `format` name, the JSON `schema`, an `example` storyboard in that
format that a person approved, the `required_cues`, and the `templates`.
Work from it, never from memory. Formats differ in their scenes, so the
contract you are given is the only one that applies.

**Output one JSON object and nothing else.** No prose before or after it, no
code fence.

**Copy the example's structure exactly, then change the content.** Same top
level keys. The same scenes in the same order, with the same `id` and
`template` on each as the example has. Keep `voice`, `music` and `pacing`
exactly as the example has them. Set the top level `id` to a lower-case slug
of the feature, like `kiosk-check-in`; it names the render folder and every
output file. Set `feature_name` and `source_faq_url`. Set the top level
`format` to the contract's `format`, except for `explainer`, which is the
default and is left out.

**A concept's beats land on the format's scenes**, in order. If a beat has
no scene to land on, merge it into the nearest one or drop it; never add a
scene the example does not have.

**`on_screen` keeps the example's keys for each template.** Each template
draws one specific mock and reads exactly those keys. Change the values (the
chip, the title, the field labels, the rules) to fit the new feature. Never
add a key, rename one, or drop one.

**Every cue in `required_cues` appears exactly once**, on the line where the
example has it. The animation is timed off these names; a missing one means
that part of the picture never moves.

**What each scene is for, in the explainer format.** Other formats describe
their scenes in their own schema and example; follow those.

- **hook** one line. A question or an instruction naming the pain. It has to
  land before anyone decides to scroll.
- **problem** the old way, shown on the example's form mock.
- **ui_demo** the feature working, each line landing on something on screen.
  The mock is a list of rules, each a condition and an action. Keep the same
  number of rules as the example.
- **benefit** one line. The outcome, not the mechanism.
- **cta** one line, ending in Vome.

**Lines.** Each carries `vo` (spoken) and `captions` (chunked on screen). A
line whose words are already large on screen gets `"captions": []`;
duplicating them is noise. Mark a line `"stack": true` when its chunks should
build up underneath each other rather than replace each other.

**Emphasis.** Wrap a phrase in asterisks to render it in brand orange:
`*every volunteer*`. One phrase per line, the phrase the sentence turns on
rather than the loudest word. The markers never reach the voice.

**Never write a timing.** Studio measures the real voice and derives every
one. A storyboard with a timestamp in it is wrong.

**Anything the reviewer must know goes in `review_notes`**, a plain string
shown at the top of the review and never in the video. Keep `one_thing` for
the one sentence a viewer should remember, and nothing else. Leave
`review_notes` out when there is nothing to say.

**When the feature does not fit these mocks**, do not bend it to fit and do
not invent a screen. Return the closest honest storyboard you can and start
`review_notes` with `TEMPLATE GAP:` followed by the screen the feature
actually needs. A person reads that before anything is built. Use the same
field for a claim you could not source, or a feature that already has a
video.

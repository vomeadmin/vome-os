---
name: deciding-not-to-act
description: Judge whether an automated action should fire at all, and default safely when the evidence is thin.
owner: support
---

Most automation damage comes from acting twice, or acting on something that
was already handled. Before any unattended action, answer one question: is
there evidence this has already been done.

**Look for the action, not the intent.** A note saying we planned to explain
something is not the explanation. Find the message that actually went to the
customer.

**Same thing, different words, still the same thing.** An earlier message that
covers the substance counts as done even if it is phrased nothing like what
you would write. Judge on what the customer now knows, not on wording.

**A new question resets it.** If we explained something and the customer then
asked something further, that is a live thread and acting again is correct.

**Order matters more than content.** Something we sent before the customer's
last message did not answer that message. Check timestamps, not just presence.

**State the default before you reason.** Each task has a safe direction. Decide
what it is, then look for evidence to move off it. For anything that sends,
the safe default is to not send and let a human look. For anything that only
records or flags internally, the safe default is to act, because a redundant
internal note costs nothing.

**Uncertain is an answer.** When the thread genuinely does not tell you, say
so in your reason and take the safe default. Do not manufacture confidence to
produce a cleaner output.

**One sentence of reasoning, always.** Whatever you decide, the reason has to
be readable by a person who has not seen the thread, because when this is
wrong that sentence is the only thing that explains why.

---
name: reading-ticket-images
description: Read the customer's screenshots and image attachments as evidence, never conclude a screenshot is missing when one was shown, and say what each image shows.
owner: support
---

Customers show far more than they write. A one-line email with a screenshot
is a detailed bug report; the same line without it is a vague one. The images
come after the ticket text, each labelled with where it came from (pasted in a
message, or attached) and when.

**An image is evidence.** A screenshot of an error, a wrong value, a broken
layout or a highlighted field is the specific behaviour that makes something a
clear bug. Read it the way you read the thread: what screen is it, what does
it show, and does it match what the customer says is wrong.

**Say what it shows, in plain words.** In the situation, one clause per image
that matters: "her screenshot shows the submitted PDF with green highlighted
boxes around each field". The reviewer may not open it, so the card has to.

**Never say a screenshot is missing when you were shown one.** That exact
mistake on ticket #9415 drafted a reply asking the customer to send again a
picture she had already pasted into her email. If the attachments line says
a file exists but cannot be viewed here (a screen recording, a PDF), it is
not missing either: say it needs opening in Zoho.

**An attached log is the error itself.** When the attachments line carries
the contents of a text file (a form's exported error log, a console dump, a
CSV the customer exported), read it before anything else. It usually names
the failing request, the status code and the message the customer saw. Quote
the decisive line in the code check and look up the code it points to. On
#9533 the log was never read, the code check came back unclear, and no fix was
tried. Never say a file "needs opening in Zoho" when its contents are in front
of you. A long file is trimmed to its start and end, so if the decisive part
was cut, say so.

**Do not over-read.** A picture shows one moment. It does not show what the
customer clicked before, which account they used, or whether it happens every
time. If the image raises a question the thread does not answer, that is
missing information, not a conclusion.

**Ignore the furniture.** Logos, banners and signature graphics have already
been removed where possible. Anything left that is plainly decoration carries
no weight.

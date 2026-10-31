---
name: writing-vome-social-captions
description: How a Vome feature video is captioned for each social platform (LinkedIn, Facebook, Instagram, TikTok, YouTube Shorts): who reads each feed, length, hashtags, links, and the checks every caption must pass before a person sees it.
owner: content
---

A caption is the words beside the video. The video already carries its own
burned-in subtitles, so the caption never transcribes the voiceover. It
gives a scrolling stranger a reason to watch and tells them what Vome is.

**The first line does the work.** Every feed cuts a caption off after one or
two lines behind "see more". The first line says, plainly, what the video
shows and that it is for volunteer work, in the words a coordinator uses.
Same rule as the hook: say what it is, say "volunteer", no riddles.

**One claim.** Use the positioning brief's single claim. A caption is not a
feature list.

**Facts come from the script.** Say only what the approved storyboard and the
positioning brief say Vome does. No statistics, prices, plans or promises
that neither of them makes.

## Per platform

The `platforms` input gives each one's character limit, hashtag range and
whether links work, and the code checks them. What differs in voice:

- **LinkedIn.** Read by the volunteer manager and the people they report
  to. Two to four short paragraphs: the problem in their words, what Vome
  does about it, one plain line inviting them to see more, the website. A
  professional peer, not an ad. Up to three hashtags at the end, or none.
- **Facebook.** Read by coordinators and volunteers in nonprofit groups.
  Two or three sentences, warm and direct, then the website.
- **Instagram.** The first line is the hook (under 125 characters, it is all
  that shows). Short lines, then "Link in bio" instead of a URL, then three
  to five hashtags on their own line.
- **TikTok.** One or two sentences. Line breaks are removed on publish, so
  write it as one paragraph. Three to five hashtags at the end. No link.
- **YouTube Shorts.** A `title` that names the job and the volunteer thing
  (it is what search reads), then a description of two or three sentences,
  the website, and one to three hashtags.

## Hashtags

Specific beats broad. Prefer the coordinator's own words: #VolunteerManagement,
#VolunteerCoordinator, #Nonprofit, #VolunteerScheduling, #VolunteerRecruitment.
Never a hashtag for a competitor, a customer or a trend unrelated to the
video. Write them in CamelCase so screen readers can say them.

## What the code refuses

A caption is sent back to be rewritten, once, if it: is missing for any
platform; has an em dash or en dash; does not say "volunteer"; is over its
platform's limit; has hashtags outside the range; has a link on Instagram or
TikTok; names a competitor. A YouTube title must exist and fit in 100
characters.

"""Tests for the concept stage: the pick matcher, the checks on what the
creative director returns, earlier videos, and the post itself.

The pick matcher gets the most cases, for the same reason the greenlight
does. A pick commits the thread to one concept and starts the copywriter.
Reading praise as a pick ("I like 2 but make it shorter") commits the
thread to something the person did not choose, so anything that is not
plainly a pick is direction.
"""

import json

import pytest

import video_concepts as vc


# ------------------------------------------------------------------ the pick

@pytest.mark.parametrize("text,number", [
    ("1", 1), ("2", 2), ("3", 3),
    ("1.", 1), ("2!", 2), (" 3 ", 3), ("(2)", 2),
    ("#2", 2), ("# 3", 3),
    ("option 3", 3), ("Option 1", 1), ("option #2", 2),
    ("concept 2", 2), ("number 1", 1), ("Number 3.", 3), ("idea 2", 2),
    ("go with 2", 2), ("Go with #2", 2), ("go with option 3", 3),
    ("let's go with 1", 1), ("lets go with concept 2", 2),
    ("we'll go with 3", 3), ("I'll take 1", 1), ("pick 3", 3),
    ("choose 2", 2), ("let's do 3", 3), ("go for 1", 1),
    ("I'd like to go with option 2", 2), ("i want option 3", 3),
    ("the second one", 2), ("The first one.", 1), ("the third option", 3),
    ("go with the second one", 2), ("second one", 2), ("third", 3),
    ("the 2nd one", 2), ("two", 2), ("Three.", 3), ("option two", 2),
    ("2 please", 2), ("option 3, thanks", 3), ("go with 1 thank you", 1),
    ("ok, 2", 2), ("Okay go with 3", 3), ("yes 1", 1),
    ("<@U123ABC> 2", 2), ("<@U123ABC> go with option 3", 3),
    ("GO WITH 2", 2),
])
def test_explicit_picks(text, number):
    assert vc.match_concept_pick(text) == number


@pytest.mark.parametrize("text", [
    # Praise that names a number is direction, not a choice. Decided and
    # documented: the creative director gets it as direction and builds on
    # the concept it names.
    "I like 2 but make it shorter",
    "I like 2",
    "2 is great",
    "love number 3",
    "2 looks good but the hook is weak",
    "go with 2 but make it more emotional",
    "the second one but shorter",
    # Hesitation and questions.
    "2?",
    "maybe 2",
    "go with 2?",
    "1 or 2?",
    "what about 3?",
    # More than one number, or a refusal.
    "2 and 3 are both good",
    "not 1",
    "no 2",
    "none of these",
    "neither",
    # Numbers that are not concepts.
    "4", "0", "option 4", "go with 5", "12", "2023",
    "make it 20 seconds",
    "cut 2 seconds",
    # Nothing to pick.
    "", "   ", "ok", "thanks", "please",
    "go build it", "approve",
    "make the hook a question",
])
def test_not_picks(text):
    assert vc.match_concept_pick(text) is None


# --------------------------------------------------------------- catalogue

CATALOGUE = {"formats": [
    {"name": "explainer", "title": "Explainer", "seconds": "40-50",
     "best_for": "rules", "description": "d", "ready": True},
    {"name": "reveal", "title": "Reveal", "seconds": "20-30",
     "best_for": "looks", "description": "d", "ready": True},
    {"name": "trio", "title": "Trio", "seconds": "30-40",
     "best_for": "choices", "description": "d", "ready": False},
    {"name": "Bad Name!", "ready": True},
    "not a format",
]}


def test_catalogue_keeps_only_well_formed_formats():
    names = [f["name"] for f in vc.parse_catalogue(CATALOGUE)]
    assert names == ["explainer", "reveal", "trio"]
    assert vc.parse_catalogue(CATALOGUE["formats"][:1])[0]["name"] == \
        "explainer"
    assert vc.parse_catalogue(None) == []
    assert vc.parse_catalogue({"formats": "nope"}) == []


def test_only_ready_formats_are_offered():
    ready = vc.ready_formats(vc.parse_catalogue(CATALOGUE))
    assert [f["name"] for f in ready] == ["explainer", "reveal"]
    # "ready": "yes" is not True. A format is ready only when Studio says so.
    assert vc.ready_formats([{"name": "x", "ready": "yes"}]) == []


def test_format_title():
    cat = vc.parse_catalogue(CATALOGUE)
    assert vc.format_title("reveal", cat) == "Reveal"
    assert vc.format_title("story") == "Story"


# -------------------------------------------------------- earlier videos

PILOT_SB = {"id": "recruitment-workflows", "feature_name": "Recruitment "
            "workflows", "scenes": [{"id": "hook", "template": "hook",
                                     "lines": [{"vo": "[calm, measured] "
                                                "Still sending *every "
                                                "volunteer* the same "
                                                "form?"}]}]}

REVEAL_CONCEPTS = [
    {"title": "Before and after", "format": "reveal",
     "angle": "A plain email becomes a designed one.",
     "hook": "Your emails, before.", "beats": ["Plain", "Designed"]},
    {"title": "B", "format": "explainer", "angle": "a", "hook": "h2",
     "beats": ["x"]},
    {"title": "C", "format": "explainer", "angle": "a", "hook": "h3",
     "beats": ["x"]},
]


def test_prior_videos_from_concepts_and_from_old_threads():
    rows = [
        {"feature_name": "Email formats",
         "storyboard_json": json.dumps({"id": "email", "format": "reveal",
                                        "scenes": []}),
         "concepts_json": json.dumps(REVEAL_CONCEPTS),
         "chosen_concept": 1},
        # Predates concepts: the first line is its hook, and no format key
        # means explainer.
        {"feature_name": "Recruitment workflows",
         "storyboard_json": json.dumps(PILOT_SB),
         "concepts_json": "", "chosen_concept": None},
    ]
    summary = vc.summarise_prior(rows)
    assert summary[0]["format"] == "reveal"
    assert summary[0]["hook"] == "Your emails, before."
    assert summary[1]["format"] == "explainer"
    assert summary[1]["hook"] == "Still sending every volunteer the same form?"
    text = vc.prior_videos_text(summary)
    assert text.splitlines()[0] == (
        "- Email formats | reveal | concept: Before and after | angle: A "
        "plain email becomes a designed one. | hook: Your emails, before.")
    assert "[calm" not in text and "*" not in text


def test_no_prior_videos_says_so():
    assert vc.prior_videos_text([]) == "(no earlier videos)"


def test_prior_video_with_broken_json_does_not_crash():
    summary = vc.summarise_prior([{"feature_name": "X",
                                   "storyboard_json": "{not json",
                                   "concepts_json": "[", "chosen_concept": 9}])
    assert summary[0]["feature"] == "X" and summary[0]["hook"] == ""


def test_chosen_concept_and_positioning_loading():
    row = {"concepts_json": json.dumps(REVEAL_CONCEPTS), "chosen_concept": 1,
           "positioning_json": json.dumps({"single_claim": "c"})}
    assert vc.chosen_concept(row)["title"] == "Before and after"
    assert vc.load_positioning(row) == {"single_claim": "c"}
    assert vc.chosen_concept(dict(row, chosen_concept=None)) is None
    assert vc.chosen_concept(dict(row, chosen_concept=4)) is None
    assert vc.chosen_concept({}) is None
    assert vc.load_positioning({}) is None


# ------------------------------------------------- checking the concepts

def _concept(n, fmt="explainer", **kw):
    c = {"title": "Concept %d" % n, "format": fmt, "angle": "angle %d" % n,
         "hook": "Hook number %d." % n, "beats": ["one", "two", "three"],
         "why_it_works": "because", "purpose": "teach the choice",
         "seconds": 45}
    c.update(kw)
    return c


GOOD = {"concepts": [_concept(1, "reveal", seconds=25), _concept(2),
                     _concept(3)], "notes": "Trio would suit this later."}


def test_good_concepts_pass():
    concepts, err = vc.check_concepts(GOOD, ["explainer", "reveal"])
    assert err == "" and len(concepts) == 3


@pytest.mark.parametrize("data,reason", [
    ({"concepts": [_concept(1), _concept(2)]}, "exactly three"),
    ({"concepts": []}, "exactly three"),
    ({}, "exactly three"),
    (None, "exactly three"),
    ({"concepts": [_concept(1), _concept(2), "x"]}, "not an object"),
    ({"concepts": [_concept(1), _concept(2), _concept(3, hook="")]},
     "no hook"),
    ({"concepts": [_concept(1), _concept(2), _concept(3, beats=[])]},
     "no beats"),
    ({"concepts": [_concept(1), _concept(2), _concept(3, fmt="trio")]},
     "trio format"),
    ({"concepts": [_concept(1), _concept(2), _concept(3, fmt="story")]},
     "story format"),
    ({"concepts": [_concept(1, "reveal"), _concept(2),
                   _concept(3, hook="hook NUMBER 2")]}, "share a hook"),
])
def test_bad_concepts_are_refused(data, reason):
    concepts, err = vc.check_concepts(data, ["explainer", "reveal"])
    assert concepts == [] and reason in err


def test_a_wholly_blank_extra_concept_is_dropped():
    """The first live run: three good concepts and a fourth with every field
    empty. The blank one is dropped; a half-filled one is still an error."""
    blank = {"title": "", "format": "", "angle": "", "hook": "",
             "beats": [], "why_it_works": "", "purpose": "", "seconds": 0}
    concepts, err = vc.check_concepts(
        {"concepts": GOOD["concepts"] + [blank]}, ["explainer", "reveal"])
    assert err == "" and len(concepts) == 3
    half = dict(blank, title="Half done")
    _, err = vc.check_concepts(
        {"concepts": GOOD["concepts"] + [half]}, ["explainer", "reveal"])
    assert "exactly three" in err


def test_hooks_may_not_share_an_opening():
    """The third real run: every hook began 'Vome sends email to your
    users'. Plain, but one formula three times."""
    same = "Vome sends email to your users, and %s"
    data = {"concepts": [_concept(1, "reveal", hook=same % "one"),
                         _concept(2, hook=same % "two"),
                         _concept(3, hook="A different opening line")]}
    _, err = vc.check_concepts(data, ["explainer", "reveal"])
    assert "open with the same words" in err


def test_one_format_needs_a_reason_when_two_are_ready():
    data = {"concepts": [_concept(1), _concept(2), _concept(3)]}
    _, err = vc.check_concepts(data, ["explainer", "reveal"])
    assert "one format" in err and "why" in err
    # With only explainer ready, three explainers is the only option.
    concepts, err = vc.check_concepts(data, ["explainer"])
    assert err == "" and len(concepts) == 3
    # The first real run: all reveal, because the explainer cannot draw an
    # email, and it said so. Forcing variety would force a broken concept.
    reasoned = dict(data, notes="The explainer cannot show an email.")
    concepts, err = vc.check_concepts(reasoned, ["explainer", "reveal"])
    assert err == "" and len(concepts) == 3


def test_a_hook_from_an_earlier_video_is_refused():
    data = {"concepts": [_concept(1, "reveal"), _concept(2),
                         _concept(3, hook="Still sending every volunteer "
                                          "the same form?")]}
    _, err = vc.check_concepts(
        data, ["explainer", "reveal"],
        ["Still sending every volunteer the same form?"])
    assert "earlier video" in err


# --------------------------------------------------------------- the post

POSITIONING = {"single_claim": "Your Vome emails can now look like your "
                               "organization, with no code."}

SAMPLE_CONCEPTS = [
    {"title": "Before and after", "format": "reveal",
     "angle": "A plain email turns into a designed one in a single cut.",
     "hook": "This is the email your volunteers got last week.",
     "beats": ["A plain text email, logo at the top",
               "One cut: the same message, Designed",
               "Header image, Brand colour, Buttons",
               "Held on the after",
               "Choose the Email format when you compose"],
     "why_it_works": "The difference is visual, so the video shows it "
                     "instead of describing it.",
     "purpose": "premium feel", "seconds": 25},
    {"title": "Which format, when", "format": "explainer",
     "angle": "Three formats, and the one question that decides between "
              "them.",
     "hook": "Basic, Designed or Custom HTML?",
     "beats": ["Three choices in the Email format menu",
               "Basic for a quick note", "Designed for anything you want "
               "to look considered", "Custom HTML when you already have "
               "your own", "Preview before it goes"],
     "why_it_works": "Admins who already email from Vome need to know "
                     "which to pick, not that email exists.",
     "purpose": "teach the choice", "seconds": 45},
    {"title": "Saved once, sent all year", "format": "explainer",
     "angle": "The designed email becomes a Message template the whole "
              "team reuses.",
     "hook": "Design it once. Send it all year.",
     "beats": ["A designed welcome email", "Saved as a Message template",
               "Reused for the next intake",
               "It adapts to mobile screens automatically"],
     "why_it_works": "Retention: the value is in every send after the "
                     "first.",
     "purpose": "retention", "seconds": 40},
]

SAMPLE_CATALOGUE = [
    {"name": "explainer", "title": "Explainer", "seconds": "40-50",
     "ready": True},
    {"name": "reveal", "title": "Reveal", "seconds": "20-30", "ready": True},
    {"name": "trio", "title": "Trio", "seconds": "30-40", "ready": False},
]

SAMPLE_POST = """\
Three ways to tell *Choosing an email format*.
Each one proves the same claim: _Your Vome emails can now look like your organization, with no code._

*1. Before and after*
_Reveal, about 25 seconds, for premium feel._
Hook: "This is the email your volunteers got last week."
• A plain text email, logo at the top
• One cut: the same message, Designed
• Header image, Brand colour, Buttons
• Held on the after
• Choose the Email format when you compose
Why it works: The difference is visual, so the video shows it instead of describing it.

*2. Which format, when*
_Explainer, about 45 seconds, for teach the choice._
Hook: "Basic, Designed or Custom HTML?"
• Three choices in the Email format menu
• Basic for a quick note
• Designed for anything you want to look considered
• Custom HTML when you already have your own
• Preview before it goes
Why it works: Admins who already email from Vome need to know which to pick, not that email exists.

*3. Saved once, sent all year*
_Explainer, about 40 seconds, for retention._
Hook: "Design it once. Send it all year."
• A designed welcome email
• Saved as a Message template
• Reused for the next intake
• It adapts to mobile screens automatically
Why it works: Retention: the value is in every send after the first.

_A Trio, comparing the three formats side by side, would suit this later._

Reply with 1, 2 or 3 to pick one. Anything else I will treat as direction and come back with new concepts."""


def test_the_concept_post():
    """The exact text a person sees in the thread."""
    post = vc.concepts_post(
        SAMPLE_CONCEPTS, feature_name="Choosing an email format",
        single_claim=POSITIONING["single_claim"],
        notes="A Trio, comparing the three formats side by side, would suit "
              "this later.",
        catalogue=SAMPLE_CATALOGUE)
    assert post == SAMPLE_POST


def test_post_numbers_three_and_ends_with_the_pick_line():
    post = vc.concepts_post(SAMPLE_CONCEPTS)
    assert post.startswith("Three ways to tell this one.")
    for n in (1, 2, 3):
        assert "\n*%d. " % n in post
    assert post.endswith(vc.PICK_LINE)
    assert vc.PICK_LINE == ("Reply with 1, 2 or 3 to pick one. Anything "
                            "else I will treat as direction and come back "
                            "with new concepts.")


def test_post_after_direction_says_so():
    post = vc.concepts_post(SAMPLE_CONCEPTS, feature_name="X",
                            direction=True)
    assert post.startswith("Three new concepts, taking your direction.")


def test_post_escapes_slack_markup():
    c = dict(SAMPLE_CONCEPTS[0], hook="Under <18> & over")
    post = vc.concepts_post([c])
    assert "Under &lt;18&gt; &amp; over" in post


def test_post_has_no_dashes():
    post = vc.concepts_post(SAMPLE_CONCEPTS, feature_name="X",
                            single_claim="y", notes="z")
    assert "\u2014" not in post and "\u2013" not in post

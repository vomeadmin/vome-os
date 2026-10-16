---
name: writing-a-help-centre-article
description: House style, terminology, banner markup and structure for a Vome help centre article in English and French.
owner: knowledge
---

**Format.** Raw HTML, never Markdown. Use `<h2>`, `<h3>`, `<p>`, `<ul>`,
`<ol>`, `<li>`, `<b>`, `<a href>`. No elaborate tables or charts.

**Structure.** A title phrased as a customer's question or a plain feature
name. A short Overview for integration and setup articles (two to four
sentences: what it is, who it is for). Then numbered questions as `<h2>`
headings, phrased the way a customer would ask them. Answer first, detail
second: lead with "Yes.", "No." or "You decide." where that fits. Short
bulleted lists for options, limits and examples. An optional Summary of four
to six bullets for integration and setup articles. Feature and policy
articles can go straight to the questions.

**Voice.** Plain, warm, not marketing. Second person for administrators.
Name every button, menu and setting exactly as the UI labels show it,
bolded.

**Terminology, English.** Opportunity, never Opportunity Role. User or
profile, never volunteer, except inside a product name.

**Terminology, French.** utilisateur, profil. affecter / affectation, never
attribuer. tag de profil (masculine: un tag, taguer), never étiquette.
Horaire for the Schedule module, planification for the activity. Sélection
for screening, never dépistage or présélection. opportunité, never occasion.
quart for shift. ticket, never billet. Use literal accented characters, not
HTML entities, in French titles and bodies.

**No em dashes.** Anywhere, either language. Use periods, commas, parentheses
or "and".

**Contacting support.** Ticket link first, email second, never email alone.
EN: `https://support.vomevolunteer.com/portal/en/newticket`, text "submit a
ticket". FR: `https://support.vomevolunteer.com/portal/fr/newticket`, text
"soumettre un ticket". Email: `support@vomevolunteer.zohodesk.com` as a
`mailto:` link whose text matches it. A French article never links an
English URL.

**Banners.** For a plan requirement (always a banner, never a sentence), a
default that catches people out, an irreversible action, a prerequisite, or a
cross-reference to another module. Two or three per article at most, never
two in a row, never wrapping ordinary prose, and **never inside a `<p>`**:
emit the `<span>` at top level between blocks. Every banner in English has
its twin in French. Markup, with BG, BAR, ICON and ALT from the table:

```html
<span class="KB_New_Editor_Highlights" style="background-color: BG; border-right: 5px solid BAR; margin: 10px 0px; position: relative; display: inline-block; padding: 10px 10px 10px 40px"><div><img data-image="contentStyle" style="position: absolute; left: 12px; top: 14px; width: 15px; max-width: 100%" data-type="non-resize" src="https://img.zohostatic.com/zde/static/images/ICON" alt="ALT"></div><div><b>Lead-in:</b> body text.</div></span>
```

| Use | ICON | ALT | BG | BAR |
|---|---|---|---|---|
| Plan, prerequisite, heads-up | info.png | Info | rgb(193, 225, 244) | rgb(167, 212, 234) |
| Gotcha, irreversible | caution.png | Caution | rgb(252, 177, 177) | rgb(252, 139, 139) |
| Strong emphasis | exclamation.png | Alert | rgb(255, 198, 151) | rgb(249, 180, 128) |
| Suggested workaround | lights.png | Idea | rgb(189, 229, 214) | rgb(136, 209, 181) |
| Aside | file.png | Notes | rgb(252, 237, 158) | rgb(242, 219, 104) |

French lead-ins: `<b>Forfait requis :</b>` for plans, `<b>Attention :</b>`,
`<b>Important :</b>`.

**Never** add "see the French version" style links. The portal language
switcher does it.

**Permalinks.** Lowercase words joined by hyphens, readable, one per
language: `group-reservation-management`,
`gestion-des-reservations-de-groupe`.

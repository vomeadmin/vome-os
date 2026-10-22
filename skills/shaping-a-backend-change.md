---
name: shaping-a-backend-change
description: The extension patterns of Vome's Django core, how to tell them apart, and what each one costs, so a feature request is built in the shape that keeps the backend coherent.
owner: engineering
---

Most requests arrive shaped by the screen that needs them. Before judging
how to build one, name which pattern it is. Line numbers below drift; read
the file on `development` before citing it.

**Schema changes.** Migrations are generated on deploy (`makemigrations`
runs on the server, nothing reviews it first, docs/INFRASTRUCTURE.md). So
any new field, model or table is a schema change nobody reviews, and never
agent work. A new value in an existing `choices` list is state-only, but
still a product change.

**Plans and trials.** A trial is not a plan. An org on trial keeps the plan
it is trialling (Enterprise 3 or Ultimate 4) with `is_trialing` true on
`OrganizationSubscription`; plan type 1 "Trial" is the legacy flow
(`subscription_app/organization/old_api.py`). So a gate on (3, 4) already
covers orgs trialling those plans, and "add Trial (plan 1) to the gate" is
`already_exists`, not an entitlement change. Say so; never offer options
for widening it.

## Patterns, lightest first

1. **Return or accept an existing field.** The model has it; a serializer
   or SQL projection does not. Add it to `Meta.fields`, or to the
   `jsonb_build_object` in a V5 projection. Trap: V5
   (`opportunity_app/api_version/v5/api.py`) hand-writes the SQL its V4
   parent serializes; a field added to one must be added to both
   (guides/performance/V5_POSTGRES_JSON_ENDPOINT.md). Agent work when the
   caller may already see the data.
2. **Bulk version of an existing write.** Reuse the single-row serializer
   per row with `partial=True`, `@transaction.atomic`, institution-scoped
   queryset, response `{updated_ids, missing_ids, errors}`. Precedent:
   `opportunity_app/institution/api.py` bulk role update. Wrong when the
   field only needs adding to the single-row serializer: bulk inherits it.
3. **Inline field on OpportunityRole (shape A).** A per-opportunity bool,
   enum, number or date. BOTH update serializers
   (`opportunity_app/serializers.py` and `opportunity_app/institution/
   serializers.py`), draft read, dashboard read, the settings matrix
   `get_settings`, and every enforcement path. Duplicate and bulk come free.
   guides/opportunity/ADDING_OPPORTUNITY_SETTINGS.md.
4. **OneToOne micro-policy on OpportunityRole (shape B).** Three or more
   fields that only make sense together, a "not configured" state, or an FK
   (`OpportunityRoleDropInPolicy`, auto check-in/out policy). Trap: missing
   from the clone tuple in `opportunity_app/api.py`, duplicates come out
   unconfigured.
5. **New option inside an existing setting.** A new enum value or a new
   sub-field on an existing micro-policy (`CHECK_IN_OUT_POLICY_CHOICES` grew
   `CUSTOM`). Often the right answer when a request looks like a new
   setting. Trap: the active-inactive `update()` extracts policy keys by
   hand, so a new key is silently ignored unless added there.
6. **Reusable library policy attached by FK.** Admins name it once and
   attach it to many shifts or roles: `FormSubmissionPolicy` with its rules,
   `GroupReservationPolicy`, `ShiftNotificationPolicy`. A new rule trigger
   is a new `FormSubmissionPolicyRule.action_type`. Confused with 4, which
   is per role and cloned.
7. **Form-level setting** (inline on `Form`). About ten serializers carry
   form fields (draft, published, template, create, list, three responses
   versions, volunteer views); create drops fields it does not list. Wrong
   when it decides when a form is required (6), or reacts to an answer
   (`FormResponseAutomation`).
8. **Sequence-level setting.** On `Sequence` AND its `SequenceEditDraft`
   mirror, copied live to draft and merged back on publish. Miss the mirror
   and publishing reverts it.
9. **Step-level setting.** On `SequenceStep` AND `SequenceStepEditDraft`;
   writes are hand-extracted in the draft and published step update views,
   reads in four serializers plus the builder. Precedent:
   `notify_admins_on_step_change`. Wrong when it is an automation (11) or
   belongs to the library entity behind the step (10).
10. **Library entity setting** (the auto-expiration shape). Behaviour of the
    reusable waiver or training wherever it is used, duplicated across the
    `Institution*` entity models. guides/auto-expiration/.
11. **Step automation trigger or action.** "When X happens on a step, run
    the existing actions" is a trigger (`sequence_app/step_automation/
    models.py`: on_step_enter, on_step_submitted, on_step_complete,
    pending_for_duration, on_status_set). A new effect is an action (14
    exist, from send_notification to set_step_status). Each needs an engine
    hook, the scheduler if time-based, and the cross-engine catalogue in
    `automations_app/providers/sequences.py`.
12. **New sequence step type.** The heaviest: an integer on
    `SequenceStep.type` (13 today: forms and waiver, interview, training,
    orientation, custom text, e-signature, fillable form, video, checklist,
    background check, SCORM, course, shift reservation) plus a payload model,
    its edit-draft twin, a library entity, a per-user record, and about
    fifteen registries (`step_types.py`, `step_owner.py`, step status,
    deletion specs, three admin step serializers, volunteer grid, my tasks,
    analytics). Only when volunteers do a genuinely new kind of thing.
    Before proposing it, ask whether a fillable form step (form step plus
    guest submissions), a checklist, or an option on an existing type does
    the job.
13. **Admin role permission.** `AdminRolePermission`, four serializer lists
    (the nested one is usually missed), an access helper, enforcement on
    every write twin, the django-db mirror. Defaults permissive; the account
    holder is always allowed. Never agent work.
14. **Notification preference.** Admin: a `NotificationType` with defaults,
    rows backfilled lazily, senders fall back to defaults. Volunteer: a
    column on both push and email settings. Confused with a per-step or
    per-opportunity "notify" toggle (9 or 3).

## Choosing between them

- Prefer extending what exists (5 over 3, 9 over 12) unless the new thing
  behaves differently enough that sharing storage would need special cases.
- Ask where the rule lives for the admin: on the opportunity, the shift, the
  form, the step, or a policy reused across many. That is usually the shape.
- Name the precedent you would copy. No precedent is a sign of a new shape,
  and a new shape is Sam's call.
- Count the registries the pattern needs. A request that "just adds a
  setting" to a step type touches the draft twin and every serializer above.

## Before offering a build

- **Settle every "not yet checked".** If a shape depends on behaviour you
  have not read (is a flag ever cleared, does a stand-in survive
  activation, is a value set anywhere else), read the code and answer it
  before the item is agent_build, and write the answer into `build.plan`.
  An agent_build whose analysis or plan still says "not yet checked" goes
  back to Sam as a question, not to the builder. 2026-10-21, profile status
  on form submissions: option B said "not yet checked: whether
  `is_offline_volunteer` is cleared on claim", Sam said go b, and the
  builder stopped on the same question. The answer was in the code:
  `form_app/guest_claim.py` moves the submissions and deletes the stand-in,
  and `user_app/signals.py` keeps UUID users inactive.
- **Cite the precedent the plan copies** as `path:line` in `evidence` or
  in the plan. The builder is shown each edited file's import blocks, the
  lines around each anchor, and every cited `path:line` as read-only
  reference, and nothing else. A plan that says "copy get_is_invited"
  without its location cannot be built.

# Product knowledge from sprints

Captured in the Sprint Lessons Log (Jun 3 through Oct 16, 2026, pulled Oct 19) and in sessions using VomeOS (from Oct 15, 2026). Use these to answer how-to tickets and to tell a user-education ticket apart from a bug. VomeOS (`lookup_feature`, `where_in_app`, the code) is checked first and is newer than this file. If a fact is not here, not in the live Lessons Log (`3dztm-3311`, page `3dztm-2491`) and not confirmed through VomeOS, say it is unverified. Do not invent a UI path. Facts marked (confirmed in code) were read on the live branch.

## Contents
- Hours and logging
- Reports
- Forms, tags, permissions
- Shifts and opportunities
- Volunteer-facing
- Admin and accounts
- Kiosk and notifications
- Known limits
- Diagnosis hints

## Hours and logging

- **Bulk approve hour claims:** Opportunities module, filter by Pending hour claims on the left, click the hour claims pill on an opportunity, Select All, Approve All. Or select all opportunities, then Actions, Approve hour claims. The Actions option only appears when at least one hour claim exists, and volunteers generate claims. No claims means the option is hidden, which is not a fault.
- **Bulk log hours where a past reservation exists** (attendee status "Not logged"): Schedule, Actions, Log Hours and Absences, filter the date range to cover the reserved dates, bulk log. No new shift needed.
- **Bulk log hours where no reservation existed:** Schedule, Actions, Assign shifts, Assign and log past shifts. A shift must be created and published, but publishing does not require notifying anyone, and attendees can be pre-assigned.
- **Log hours with no shift at all:** Database, open the profile, Actions, Edit Hours Logged (Off-Vome). Individual only, and it cannot attribute hours to what they were connected to. Prefer the schedule-based routes.
- **Approval timing:** approving hours sets attendee status to Hours Logged immediately. The report reflects it on the next refresh. No processing window.
- **Rounding:** automatic hour rounding is not configurable.
- **Hours columns are decimal hours**, not hours and minutes. 6.75 is 6h45m, not 6h75m. Multiply the decimal by 60 to convert. Correct this proactively.
- **Group Hours Logged** captures the group's collective time, including guests logging against an individual's reservation. Only relevant where group reservations are enabled. Otherwise use Hours Logged and Impact Value.
- **Planned hours for volunteers:** Volunteer, Schedule, Actions, Export schedule. Exporting upcoming shifts includes a Planned hours column.
- **Dashboard clock-in and correcting time:** three dots next to the volunteer's name in Today's shifts to check in, Edit hours to correct logged time. (Sourced from Ron's internal note, exact UI labels not verified.)

## Reports

- Reports, Reservations and hours log, set the date range, then click **Run Report**. Results do not refresh from changing the date range alone. This missed step once ran a ticket for seven weeks, so spell it out.
- Caveat: Welcome Hall reports the Grid view sometimes runs on the current week even after choosing a range and clicking Run Report, and Grid and Insights can give different counts for the same custom report. Held by the reviewer, so do not give guidance on this yet.
- Export volunteers from an opportunity, two paths: Database page, use the left filters to select the opportunity, Select All (or checkboxes), Export (top right). Or Opportunities (left nav), open the opportunity, People tab, Export.
- Exporting opportunity details (role descriptions, volunteers per role) is only possible through the API on Ultimate.

## Forms, tags, permissions

- All historical form submissions are retained. Resubmitting after an expiry adds a new record rather than replacing the old one.
- Shift restriction tags only show a shift to volunteers who carry the matching profile tag. Fix in bulk: Database, filter to profiles approved for the opportunity, filter the profile tags field using the "is not" condition on the tag, Select All, Action, Assign Profile Tags.
- Availability cannot be sorted or filtered in the Database. Workaround: collect availability on a form and assign profile tags, then filter on tags. The workaround does not hold over time because tags do not update when availability changes.
- Site auto-assignment from a form is a custom automation chosen by whoever manages the form. Only an admin with form edit access can change it.
- An admin without Reports permission sees an endless loading spinner rather than an access error. The account holder can change role permissions. The spinner-instead-of-error behaviour is itself noted as a defect.
- A form that blocks submission on missing required fields means a completed submission rules out missing fields as the cause of a write failure.
- Forms vanishing after edit and save are believed to come from the form editor process in a recent update. The team is correcting it. Check the open incident in the Lessons Log before answering.
- **Guest submissions now exist (supersedes the Sep 2 "not available" answer from #8949).** Per form: open the form for editing, form settings panel, turn on Allow guest submissions. Anyone with the link can submit without logging in. Help article: "Can people submit a form without a Vome account?" (support.vomevolunteer.com/portal/en/kb/articles/can-people-submit-a-form-without-a-vome-account). Only published general forms with responses open accept guests.
- **Guests are always asked first name, last name and email,** even if the form has no name question. These cannot be switched off, so name columns in the submissions table are never blank for guest submissions. Other Profile information fields the form requests (phone, date of birth, gender, address) are asked too. (confirmed in code, #9504)
- **Every guest submission creates or matches a profile.** Repeat submissions with the same email reuse one offline profile rather than creating duplicates. The profile joins the Database only if Auto-add to database is on for that form; otherwise the submission sits in the table with the Accept, Add to database flow available. If the guest later verifies an account on that email, the submission moves onto their real account automatically. (confirmed in code)
- **Admins cannot complete a published form while logged in,** regardless of the guest setting, because every submission attaches to a profile and an admin account is not a volunteer profile. Use the form's admin page, actions menu, Create a form submission, choose a profile, and the form opens fully editable. Needs the form published and the Create/Edit Forms permission on the admin's role. (#9504)
- **Admins submitting as a guest in an incognito window** (Ron's tip on #9504) must enter a name and email like any guest. If that email already belongs to a profile in the org, the submission is attributed to that profile; if not, a new offline profile is created. Tell admins to enter the partner contact's details, not their own. Internal caveat, not for clients unless asked. (confirmed in code)
- Every signed-in submission already includes the user's first name, last name, email and phone number, so admins do not need custom questions for them (form builder text).
- Submission errors: ask the volunteer to retry, and if it recurs, click the blue "Export Debug Logs" button and send the downloaded file.
- **Form titles are unique per organisation.** A duplicate is named "<source> - Copy"; renaming it back to the source's name collides with the source. Until the web fix ships, the editor closes as if the rename saved. (#9500, confirmed in code)
- **Duplicating a form did not copy** the header, embed or admin-email settings. Fixed in web; check `fix_status` before saying so. (#9393)
- **Removing the last informational note on a form was silently ignored.** Fixed in core; after release the admin can remove it herself. (#9436, confirmed in code)
- **A volunteer's Fillable PDF submit saved a picture of the on-screen form,** so fill guidance (field glow, required tint, placeholder text) was baked into the stored PDF. Fixed for new submissions; existing PDFs stay as they are unless resubmitted. (#9415, confirmed in code)

## Shifts and opportunities

- Cancel a recurring shift series: click any upcoming shift in the series, Actions in the shift popup, Cancel shifts, choose a cancellation option. Whether cancelling notifies already reserved volunteers is not confirmed, so do not promise either way.
- Per-volunteer recurrence within a series (one person on every other Wednesday) is not supported. Workaround: add the attendee to each target shift individually.
- A shift type sits inside an opportunity, so the opportunity card should still appear in a volunteer's Opportunities module even when no shifts of that type are published. This separates "the org has not published shifts" from a visibility fault.
- **Overlapping shifts: the overlap setting only stops volunteers from reserving overlapping shifts themselves.** Admin assignments are not blocked by it, so a volunteer an admin assigned can still end up on two overlapping shifts. Help article: "Overlapping Reservations on Vome". Use `where_in_app` for the exact setting name before answering. Open question: whether waitlist sign-up or promotion runs the overlap check (#9477). (#9477, confirmed in UI text and code)
- The Schedule view shows reserved shifts only. The Reserve Shifts view is where waitlisted shifts appear.
- Schedule calendar default changed in the week of Sep 21: upcoming months with shifts now show without scrolling. Web first, app in the next release. Possibly linked to a "no shifts" display cluster.
- Video step vs web pages: .html course pages (for example Articulate) are not video files and will not play in the Video step. Either extract and upload the video, or use a step with a title and description that links to the page.

## Volunteer-facing

- Volunteer unsubscribe: account, Settings, Advanced notification settings. Vome notifications and organization emails can be switched off separately. If the volunteer cannot access their account or asks twice, action it directly.
- Volunteer accounts persist through inactivity unless the volunteer deletes them. Reset the password, then join the new organization through its link.
- Volunteers can delete their own account: Settings, Delete Account.
- Testing a new volunteer sign-up while logged in as an admin: use a private or incognito window and an email never used on Vome, or the test merges into the existing account.
- A "can't see opportunities" or "no account found" report may be a second active account under another email. Offer a merge by the database team.
- "Account not found" on password reset is often wrong. Confirm the active profile exists, ask the user to double-check the email spelling, and tell them the reset was re-sent and sent successfully.
- Email non-delivery: we can verify when the recipient's provider is blocking us. Ask them to whitelist our domain, including noreply@vomenotifications.com.

## Admin and accounts

- Data is kept on downgrade or a pause of a few months unless deletion is explicitly requested.
- Account holder transfer when the old holder is gone is done manually by our team. Confirm the old and new emails first.
- Data merges take longer because of manual effort and security precautions. The intention is to offer in-portal merge workflows.
- How volunteers get into the Database: manually by any admin, or through an automation (for example a form submission that auto-assigns). We can check when specific profiles were created. More visibility on the profile itself is coming.
- "Download the app" sits in the shortcuts panel on the right-hand side of the admin dashboard.
- Removing a test volunteer account so it can re-register fresh is handled by our team on request.

## Kiosk and notifications

- Kiosk tabs crashed on every second deploy (chunk-load reload flag never cleared). Fixed in web. New chunk-load error reports are agent hotfixes closed quietly. (#9534, #9561)
- Kiosk login must use an administrative account. The home page has a "Manage Attendance Kiosks" button that lists the kiosks a user can use. A dedicated kiosk admin account needs a licence seat.
- Reducing notification volume: shift notification policies can switch off the default reminders. FAQ for admins: "How Do Shift Notification Policies Work on Vome?" at support.vomevolunteer.com/portal/en/kb/articles/how-do-shift-notification-policies-work-on-vome. Volunteers can switch off Vome and organization emails under Settings, Advanced notification settings. Admin notifications follow Coordinator and Watcher settings. Core transactional notifications cannot be turned off from the admin side.
- Open question: should the web kiosk require volunteers to type their last name, when the app kiosk let them tap their timeslot? Not decided.

## Known limits

- Zoho ticket attachments cannot be read through tools. Note that they exist.
- The old "no RAG layer" limit is retired: `lookup_feature` and `check_existing_work` now show whether a feature exists or is already requested. Feature requests still go to the reviewer, with that evidence.
- VomeOS limitations (frontend search, mobile source, allowlist, no commit history) are listed in `vomeos-code-guide.md`.
- Sleeping item wake-date monitoring and the weekly feature request digest are not built.
- Planned: engineer intake performance dashboard (needs a status-transition log).

## Diagnosis hints

- **Timezone and date offset cluster:** check before opening a new task. Reported symptoms include notification times off by several hours, a date picker saving one day early, reminders at the wrong time, and shift times an hour out after daylight saving changes (a hard Nov 1 date for Many Hands). Oct 9 clue: Venables (BC, which has no DST) still sees November shifts an hour early, and Wellspring sees it only for some admins, only in calendar view, from November on. The cause is likely a timezone rule mismatch, not Nov 1 handling alone.
- **"No shifts" and week navigation cluster:** possibly linked to the Sep 21 calendar default change.
- **Database and form editor incident around build `cf0b6e0` (Sep 29):** volunteers signed up but not findable, forms vanishing after edit and save, `/database` crash with a null `precision` TypeError. Match build IDs in error reports against it.
- **Import delays:** an import took about 48 hours with no completion notification for one client. Earlier related tasks exist.
- **Email-change verification loop:** WRHN and one other case share a task.
- **Notification delays:** not a bug task by default. DM OnlyG to check the endpoint logs and the Celery queue (#9447).
- **Sarasota County** (#9413, #9350, #9467, escalated #9347, plus #9500 and #9393 on the form editor fix): Sam handles these. Check with him before acting on Sarasota tickets.
- **Form editor saves cluster (Oct 16):** rename (#9500), settings won't save (#9393), note won't remove (#9436). Re-test after the web release.
- **User creation regression (Oct 15 to 16):** profile creation, guest submissions and mass imports failed between Oct 15 16:41 UTC and the fix deploy. Fixed and closed for the six tickets; lost submissions not yet counted.

For ClickUp task IDs on these clusters and the current owners, read the live Lessons Log. IDs go stale quickly, so they are not copied here.

---
name: process-calls
description: Work the PXL Kraft dialer's after-call queue. Reads new call transcripts, moves each lead through the pipeline, saves contact details, creates trial invite links on the PXL Kraft platform, and drops ready-to-send follow-up emails into Gmail Drafts (never sends). Also drafts nudges for follow-ups that are due and writes today's agenda: who to call, callbacks, booked calls, what needs marking. Use when Pawan says "process my calls", "go through today's calls", "do my follow-ups", "work the pipeline", "what's on today", "who should I call", or runs /process-calls.
---

# Process calls

Pawan cold-calls US manufacturers (machining, stamping, forging, casting, fab shops) about PXL Kraft: the AI builds their PPAP / AS9102 FAI package from the drawing, in their customer's template. The sale goes: pitch on the call, then he emails a personal trial invite to the PXL Kraft platform, they sign in and run a package or two, then a call to close.

Your job: everything after the call, so he only has to read drafts and press send.

## Tools

- **Dialer connector** (MCP server `dialer`): `list_new_calls`, `get_lead`, `list_pipeline`, `list_followups_due`, `list_callbacks`, `list_bookings`, `set_stage`, `set_follow_up`, `update_lead_details`, `log_event`, `mark_call_processed`, `list_campaigns`.
- **Gmail connector**: use `create_draft` only. Never `send_message`, never anything that sends. If no Gmail connector is available, write each draft into the chat instead.
- **Trial invite links**: made by running this from a shell. It sends nothing; it only returns the link.
  ```
  cd ~/pxlkraft-ppap && uv run python ~/.claude/skills/process-calls/make_trial_link.py --email EMAIL --company "COMPANY"
  ```
  It prints JSON with `links[0].link` and `reachable_by_prospects`. If that is false, the platform is only on Pawan's Mac (localhost). Then don't put the link in the email: use the placeholder `[trial link]` in the draft, and tell Pawan at the end that the platform needs to be online.

## Part 1: new calls

Call `list_new_calls` (repeat until it's empty; it returns the oldest first). For each call:

1. **Read the transcript and the lead.** `Pawan:` is Pawan; `Them:` is whoever picked up (often a receptionist first). Work out:
   - who they are and their role;
   - interest level;
   - the next step they agreed to;
   - any email address they spelled out;
   - objections;
   - their PPAP volume.
2. **Save what you learned.** Use `update_lead_details` for email, dm_name, title and first/last, and put key facts (volume, customers, pain in their words) into `lead_notes`. Never invent an email address. If they gave one but the transcript looks garbled, put your best reading in the draft and say "check the address" in your summary to Pawan.
3. **Set the stage** with `set_stage`:
   - **interested:** they liked it but there's no email or agreed next step yet.
   - **invite_sent:** they agreed to try it and you drafted the invite (step 4).
   - **call_booked:** they agreed to a call with a date.
   - **lost:** clear no, not a fit (no PPAP work, ITAR/defence, out of business), or they asked not to be contacted.
   - **No stage:** gatekeeper or voicemail calls, unless the lead is already in the pipeline.
4. **Trial invite.** Make one for every lead who agreed to try it, and for an interested lead who gave an email, unless their timeline already has an `invite` event from the last 7 days (invites last 7 days).
   - Run the trial link command with their email and company.
   - **Attach it to the lead:** `log_event` kind `invite` with the full link, so it shows on the lead's timeline in the dialer.
   - Then `create_draft` in Gmail: to their email, with the subject set to their first name only, and the link in the body.
   - Log the draft too: `log_event` kind `draft` ("Gmail draft to X, subject Y").
   - Set the stage to `invite_sent`.
5. **Other follow-up emails** when the call earned one: they asked for "send me something", or interested with no email yet but a known address. Draft the same way and log kind `draft`.
6. **Set the follow-up date** with `set_follow_up`, in UTC `YYYY-MM-DD HH:MM`, at about 10am on the prospect's clock (their time zone is on the lead):
   - **invite_sent:** in 2 business days, note "check they signed up".
   - **interested with no email:** in 2 business days, note "call back, get email".
   - **call_booked:** none (the dialer tracks the booking).
   - **a callback they asked for:** the dialer already set it; leave it.
7. **Close the call** with `mark_call_processed`. Give a one or two sentence summary in plain words: who, what they said, what you did.

## Part 2: follow-ups due

Call `list_followups_due`. For each lead, read its recent calls and timeline, then:

- **invite_sent, still not signed up:** a short nudge draft ("did the link come through?"). Set the follow-up 3 business days out. On the second nudge with no reply, set the note to "call them" instead of emailing again.
- **interested, no email:** set the note to "call back to get their email". No draft.
- **signed_up or using:** draft a check-in that asks how the first package went and offers a 15-minute call.
- **no_show:** draft a friendly reschedule offer.
- **Anything else:** use judgement, and keep it to one email per lead per follow-up.

Log every draft with `log_event` and move each follow-up date on.

## Part 3: today's agenda

Pawan is in India (IST) and calls US prospects, so his calling session runs in the IST evening and night. Every time shown to him goes in both IST and the prospect's local time, e.g. "7:00pm IST (9:30am ET)". The dialer stores UTC.

Gather:
- `list_bookings`: calls booked for the next 24 hours, and past bookings with `needs_status` (he must mark showed / no-show / sale in the dialer's Bookings tab).
- `list_callbacks`: callbacks due today and overdue ones.
- `list_followups_due` plus what you just did in Parts 1 and 2: leads whose note says to call (e.g. "call back, get email", "call them").
- `list_pipeline` with no stage: hot leads (interested, signed_up, using) with no follow-up set.
- `list_campaigns`: open and untouched leads per campaign, so he knows which campaign to dial for new leads.

Write the agenda as a short plan, in this order:
1. **Booked calls today**, with time, who, company, and one line of prep from their calls and notes.
2. **Call first**: callbacks and call-type follow-ups, ordered by time. For each: time window, company, who to ask for (a name from the transcript, e.g. "ask for Doug"), why, and the opening line to use.
3. **Hot leads to nudge**, if any have no follow-up.
4. **Needs you**: bookings to mark, garbled emails, drafts waiting in Gmail.
5. **Then dial**: which campaign to work for fresh leads, and how many are untouched.

Keep it scannable; skip any section that's empty. Don't invent times: if a callback has no time, say "anytime in their business hours".

## Email rules (Pawan's)

- **Short:** 2 to 4 sentences, casual, written like one person to another. Use their first name.
- **Subject:** their first name only.
- **No sign-off name.** No em dashes, no long dashes at all.
- **One ask per email.** For trial invites the ask is to sign in and run one of their drawings. Later the ask is a 15-minute call.
- **Make no promises the product doesn't keep.** No turnaround times ("within the hour", "in minutes"), no "engineer-checked" or "reviewed by an engineer", no security or compliance claims (NDA, ITAR-safe, SOC 2, data deletion). Don't mention price unless they asked on the call. If they did: the first package is free; after that, prices are discussed on the call.
- **Refer to the call once,** in their words if possible ("you mentioned six PPAPs a month...").
- **Skip any ITAR or defence shop:** mark it lost, no email.

Example invite (adapt; don't copy word for word):

> Dale, good talking just now. Here's your access to PXL Kraft: [link]
> Drop in one of the drawings you mentioned and it builds the PPAP in your customer's format. Tell me what you think.

## At the end

Report to Pawan in a few lines:
- calls processed;
- drafts created, with who they're to;
- invite links made;
- stage changes;
- anything that needs him: a garbled email address, a hot lead to call now, or the platform being offline.

Then give the Part 3 agenda. Remind him the drafts are waiting in Gmail Drafts.

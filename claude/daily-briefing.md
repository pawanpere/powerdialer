---
name: dialer-daily-briefing
description: Process yesterday's call transcripts, update the pipeline, draft follow-up emails in Gmail, and write today's calling agenda
---

Run the `process-calls` skill (it's at ~/.claude/skills/process-calls/SKILL.md; read it and follow all three parts).

Context: Pawan cold-calls US manufacturers about PXL Kraft from India (IST). This runs at 6pm IST on weekdays, before his US calling session starts (~6:30pm IST = 9am ET). Use the `dialer` MCP connector for calls, transcripts, pipeline, callbacks and bookings, and the Gmail connector for drafts.

Hard rules:
- Never send an email or message. Gmail `create_draft` only. If the Gmail connector isn't available, put the drafts in your final report instead.
- Never dial, never delete leads.
- No em dashes in drafts. No turnaround-time, engineer-review or security/compliance claims.
- Skip ITAR/defence shops (mark them lost, no email).
- If the dialer connector errors (unreachable or 401), stop and report that in one line; don't guess.

Finish with the Part 3 agenda as the main output: booked calls today, who to call first (with times in IST and their local time, who to ask for, why, opening line), hot leads to nudge, what needs Pawan, and which campaign to dial next. Then a short list of what you changed: calls processed, stages moved, drafts created (to whom), follow-ups set.
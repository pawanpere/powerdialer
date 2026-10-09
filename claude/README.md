# Claude setup for the dialer

`process-calls/` is the Claude Code skill behind `/process-calls` ("process my calls"):
it reads new call transcripts through the dialer connector, moves leads through the
pipeline, makes PXL Kraft trial invite links, and drops follow-up emails into Gmail
Drafts. It never sends.

Install on a Mac that has the dialer password in `~/.pxl-dialer-password`:

```bash
mkdir -p ~/.claude/skills && cp -R claude/process-calls ~/.claude/skills/
claude mcp add dialer --scope user -- python3 "$PWD/dialer/mcp_server.py"
```

`daily-briefing.md` is the prompt of the scheduled task that runs the skill every
weekday at 6pm IST and ends with today's calling agenda. Recreate it in the Claude
desktop app (Scheduled, New task, cron `0 18 * * 1-5`) with that prompt.

`make_trial_link.py` runs inside the PXL Kraft platform repo (`~/pxlkraft-ppap`,
`uv run python ...`) and needs its database; see the comment at the top of the file.
Set `PXL_PUBLIC_URL` to the platform's public address so the links work for prospects;
without it they point at localhost and the drafts get a `[trial link]` placeholder.

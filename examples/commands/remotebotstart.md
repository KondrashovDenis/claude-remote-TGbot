---
description: Start Claude Remote Bot — push tool requests that need approval to Telegram so they can be approved/denied from the phone
allowed-tools: Bash, Read
---

Start Claude Remote Bot for remote tool-call approval via Telegram.

```bash
# Replace with your install path
python /path/to/claude-remote-TGbot/manage.py start
```

`manage.py` is cross-platform (Linux / macOS / Windows) — it spawns
`bot.py` detached from the current shell via `psutil` and creates
the `state/active` flag.

After start: send `/start` or `/status` to the bot in Telegram — it
should reply with "ACTIVE".

**Important:** TG pushes happen **only** for tool calls that are NOT
covered by `permissions.allow` in `~/.claude/settings.json`.
Auto-approved tools (Bash entries in the allow list, MCP tools, etc.)
flow through without a push.

---

## CRITICAL for Claude — while the bot is active

After a successful start you must obey **one rule** for the rest of the
session, until `/remotebotstop`:

**Whenever you need to ask the user a question or request a decision**
(picking between options, clarifying a parameter, "what should we name it",
"should we proceed", etc.) — **ALWAYS use the `mcp__remote-bot__ask`
tool** INSTEAD of asking as plain text in chat.

```
mcp__remote-bot__ask(question="Which filename — A or B?", timeout_seconds=600)
```

The tool returns:
- **The user's reply text** — you receive it as the tool result and continue
- **"ERROR: ..."** — fall back to a regular text question in chat

After `/remotebotstop` the tool automatically returns ERROR (state/active
is removed) — the switch back to the normal flow is automatic.

This matters because the user may not be at the computer — a plain-text
chat question would be invisible to them. The MCP tool delivers the
question as a Telegram push, the user replies from TG, you get the
answer and continue the session uninterrupted.

There is also a backstop — the Stop hook (`notify.py`) detects a question
mark in the tail of your reply and, while the bot is active, returns
`decision: block` to force you to re-issue via `ask`. Don't rely on it
though — follow the rule yourself.

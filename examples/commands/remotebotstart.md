---
description: Start Claude Remote Bot — push tool requests that need approval to Telegram so they can be approved/denied from the phone
allowed-tools: Bash, Read
---

Start Claude Remote Bot for remote tool-call approval via Telegram.

Run this as **a single Bash command** without `cd` (absolute paths
matter — they avoid the Desktop-UI guard against untrusted git hooks
that triggers on any `cd ...; X` command):

```bash
# Replace BOT_HOME with the absolute path to your install directory.
BOT_HOME=/path/to/claude-remote-TGbot

# 1. Config check
test -f "$BOT_HOME/.env" || { echo "FAIL: create .env from .env.example"; exit 1; }

# 2. Dependency check
python -c "import telegram, dotenv, requests" 2>&1 || { echo "deps missing, installing..."; pip install -q -r "$BOT_HOME/requirements.txt"; }

# 3. Already running?
if [ -f "$BOT_HOME/state/bot.pid" ]; then
  PID=$(cat "$BOT_HOME/state/bot.pid")
  if tasklist //FI "PID eq $PID" //NH 2>/dev/null | grep -q "$PID"; then
    [ ! -f "$BOT_HOME/state/active" ] && touch "$BOT_HOME/state/active"
    echo "Bot already running (PID: $PID)"; exit 0
  fi
  rm -f "$BOT_HOME/state/bot.pid"
fi

# 4-5. Launch + flag
pythonw "$BOT_HOME/bot.py" &
touch "$BOT_HOME/state/active"
sleep 3

# 6-7. Verify
if [ -f "$BOT_HOME/state/bot.pid" ] && tasklist //FI "PID eq $(cat $BOT_HOME/state/bot.pid)" //NH 2>/dev/null | grep -q "$(cat $BOT_HOME/state/bot.pid)"; then
  echo "Remote Bot started (PID: $(cat $BOT_HOME/state/bot.pid)). Tool requests outside permissions.allow will be pushed to Telegram until /remotebotstop."
  echo "Log: $BOT_HOME/logs/bot.log"
else
  echo "FAIL: bot did not start. Log:"
  tail -20 "$BOT_HOME/logs/bot.log" 2>/dev/null
fi
```

After start: send `/start` or `/status` to the bot in Telegram — it should reply with "ACTIVE".

**Important:** TG pushes happen **only** for tool calls that are NOT
covered by `permissions.allow` in `~/.claude/settings.json`.
Auto-approved tools (Bash entries in the allow list, MCP tools, etc.)
flow through without a push.

If anything fails on any step, surface the error — don't pretend it succeeded.

---

## CRITICAL for Claude — while the bot is active

After a successful start you must obey **one rule** for the rest of the
session, until `/remotebotstop`:

**Whenever you need to ask the user a question or request a decision**
(picking between options, clarifying a parameter, "what should we name it",
"should we proceed", and so on) — **ALWAYS use the
`mcp__remote-bot__ask` tool** INSTEAD of asking as plain text in chat.

```
mcp__remote-bot__ask(question="Which filename — A or B?", timeout_seconds=600)
```

The tool returns:
- **The user's reply text** — you receive it as the tool result and continue working
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

---
description: Stop Claude Remote Bot — return to local approvals in Claude Desktop / Code
allowed-tools: Bash, Read
---

Stop Claude Remote Bot as **a single Bash command** without `cd`
(absolute paths to avoid the Desktop-UI guard against untrusted git hooks):

```bash
# Replace BOT_HOME with the absolute path to your install directory.
BOT_HOME=/path/to/claude-remote-TGbot

# 1. Drop the flag — passthrough mode kicks in immediately even if the bot is still alive
[ -f "$BOT_HOME/state/active" ] && rm -f "$BOT_HOME/state/active" && echo "[1] state/active removed" || echo "[1] state/active already gone"

# 2-3. PID + kill
if [ -f "$BOT_HOME/state/bot.pid" ]; then
  PID=$(cat "$BOT_HOME/state/bot.pid")
  if taskkill //PID $PID //F 2>/dev/null; then
    echo "[2-3] killed PID $PID"
  else
    if tasklist //FI "PID eq $PID" //NH 2>/dev/null | grep -q "$PID"; then
      echo "[2-3] FAIL — process not killed (insufficient rights?)"; exit 1
    fi
    echo "[2-3] PID $PID was already dead"
  fi
  rm -f "$BOT_HOME/state/bot.pid"
  echo "[4] bot.pid removed"
  echo
  echo "Remote Bot stopped (was PID: $PID). Approvals are handled locally in Claude Desktop / Code again."
else
  echo "Bot already stopped"
fi
```

If `taskkill` fails for a reason other than "process is gone but reported alive" — ask the user to kill it via Task Manager.

After stop, `state/active` is gone, and subsequent tool calls flow
through to the regular Desktop UI prompt.

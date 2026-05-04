# Claude Remote Bot

> 🇬🇧 English · [🇷🇺 Русский](README.md)

[![tests](https://github.com/KondrashovDenis/claude-remote-TGbot/actions/workflows/tests.yml/badge.svg)](https://github.com/KondrashovDenis/claude-remote-TGbot/actions/workflows/tests.yml)

A Telegram bridge for **Claude Desktop / Claude Code**: approve tool calls and **answer Claude's questions in plain text** straight from Telegram while you're away from your computer.

<p align="center">
  <img src="ccmascot.jpg" alt="Claude Code mascot" width="240"/>
</p>

## Why

Claude often pauses for confirmation — Allow on a bash command, file edit, or an MCP tool that isn't in `permissions.allow`. If you've stepped away, the session sits frozen on a permission prompt. Claude also tends to ask clarifying questions in plain chat — those are equally invisible without you at the keyboard.

This bot covers both cases:

1. **PreToolUse hook** — intercepts permission requests before the Desktop UI prompt is rendered, pushes them to Telegram with Allow / Deny inline buttons
2. **MCP `ask` tool** — Claude can ask a **text question** and get a **text answer** through Telegram, all within the same session
3. **Stop hook** — if Claude reverts to asking a plain-text question in chat, the hook detects the trailing `?` and blocks the turn end, forcing Claude to re-issue the question via `ask`
4. **Graceful degradation** — if Telegram is down or the bot stalls, the hook falls back to the regular Desktop UI prompt; you're never deadlocked

## Features

- **Smart auto-approve.** The hook parses `permissions.allow` from `~/.claude/settings.json` itself — if a tool call is covered (`Bash(npm *)`, `mcp__github__*`, etc.), no Telegram push goes out.
- **Two-way dialogue via MCP `ask`.** Not just approve/deny — full text answers.
- **Question detection.** The Stop hook looks for `?` in the last 400 characters of Claude's response (excluding code blocks) and forces use of `ask`.
- **Chat-id allowlist.** The bot only reacts to messages from your `TELEGRAM_CHAT_ID`. Even if someone learns the bot's username, they can't issue commands.
- **Graceful TG-down fallback.** On a send error or 60s answer timeout, the hook falls through to passthrough — the Desktop UI prompt shows up locally.

## How it works

```
┌─ Scenario 1: tool requires permission ──────────────────────────┐
│  Claude → PreToolUse hook (hook.py)                              │
│    │                                                              │
│    ├─ no state/active → passthrough (regular Desktop flow)       │
│    ├─ covered by permissions.allow → passthrough (no push)       │
│    └─ not covered → push to TG with Allow/Deny                   │
│         poll state/responses/<id>.json                           │
│         ├─ approve → permissionDecision: "allow"                 │
│         ├─ deny → permissionDecision: "deny"                     │
│         └─ 60s timeout or TG error → passthrough (Desktop UI)    │
└──────────────────────────────────────────────────────────────────┘

┌─ Scenario 2: Claude asks a text question ───────────────────────┐
│  Claude → mcp__remote-bot__ask("which option — A or B?")        │
│    └─ pending_question/<id>.json + push to TG (force_reply)      │
│         poll state/answers/<id>.txt until timeout                │
│         └─ returns the answer text to Claude                     │
│                                                                   │
│  bot.py on a text message from ALLOWED_CHAT:                     │
│    └─ finds the oldest pending → writes answer file → removes    │
└──────────────────────────────────────────────────────────────────┘

┌─ Scenario 3: Claude finished with a question, forgot ask ──────┐
│  Stop hook (notify.py Stop)                                      │
│    └─ reads transcript_path → last assistant message             │
│         └─ '?' in tail → {decision:block, reason}                │
│              Claude is forced to re-issue via ask                │
└──────────────────────────────────────────────────────────────────┘
```

**Why PreToolUse, not PermissionRequest?** Logically PermissionRequest fits better, but in Claude Desktop the hook output **doesn't dismiss** the built-in UI prompt — it sits there alongside the Telegram push, so you'd answer twice. PreToolUse intercepts the permission before the UI is rendered, but it sees ALL tool calls — hence the `permissions.allow` filter is implemented manually inside `hook.py:is_auto_approved`.

## Files

```
.
├── bot.py                  # Telegram daemon: callback queries (Allow/Deny) + text answers
├── hook.py                 # PreToolUse hook: permissions.allow filter + push approve/deny
├── notify.py               # Stop / Notification hook: question detection + push notif
├── mcp_server.py           # MCP server: `ask` tool for two-way dialogue
├── requirements.txt
├── .env.example
├── examples/
│   └── commands/           # slash commands for Claude Code
│       ├── remotebotstart.md
│       └── remotebotstop.md
├── state/                  # runtime IPC (contents gitignored)
│   ├── active              # flag "bot pushes to TG"
│   ├── bot.pid
│   ├── pending_question/<id>.json
│   ├── answers/<id>.txt
│   └── responses/<id>.json
└── logs/
```

## Setup

### 1. Create a Telegram bot
[@BotFather](https://t.me/BotFather) → `/newbot` → name/username → grab the token.

### 2. Find your chat_id
[@userinfobot](https://t.me/userinfobot) → send any message → copy the numeric ID.

### 3. Clone and fill in .env

```bash
git clone https://github.com/KondrashovDenis/claude-remote-TGbot.git
cd claude-remote-TGbot
cp .env.example .env
# edit .env:
#   TELEGRAM_BOT_TOKEN  — from BotFather
#   TELEGRAM_BOT_NAME   — bot username without @
#   TELEGRAM_CHAT_ID    — your numeric ID
#   APPROVAL_TIMEOUT    — seconds to wait for an answer (default 60)
```

### 4. Install dependencies

```bash
pip install -r requirements.txt
```

### 5. Register the MCP server

In `~/.claude/mcp.json` (or whatever your client uses):

```json
{
  "mcpServers": {
    "remote-bot": {
      "command": "python",
      "args": ["/path/to/claude-remote-TGbot/mcp_server.py"]
    }
  }
}
```

### 6. Add hooks and permissions to `~/.claude/settings.json`

```json
{
  "permissions": {
    "allow": [
      "ToolSearch",
      "mcp__remote-bot__ask"
    ]
  },
  "env": {
    "MCP_TOOL_TIMEOUT": "3700000"
  },
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python /path/to/claude-remote-TGbot/hook.py",
            "timeout": 90
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python /path/to/claude-remote-TGbot/notify.py Stop",
            "timeout": 15
          }
        ]
      }
    ],
    "Notification": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python /path/to/claude-remote-TGbot/notify.py Notification",
            "timeout": 10,
            "async": true
          }
        ]
      }
    ]
  }
}
```

Important:
- The `Stop` hook **must NOT** be `async: true` — otherwise Claude doesn't wait for the `decision:block` and the block won't fire
- `Notification` may be `async: true` (fire-and-forget notification)
- `MCP_TOOL_TIMEOUT=3700000` (3700s) overrides the default 60s MCP client timeout — without it long `ask` calls die early

### 7. Copy slash commands

```bash
mkdir -p ~/.claude/commands
cp examples/commands/remotebotstart.md ~/.claude/commands/
cp examples/commands/remotebotstop.md ~/.claude/commands/
# edit the path inside them to your install directory
```

### 8. Verify

In Telegram → message the bot `/start` → it should reply «Claude Remote Bot подключён» (Claude Remote Bot connected).

## Usage

In Claude Code / Desktop:
- `/remotebotstart` — spawn bot.py + create `state/active`
- `/remotebotstop` — kill the bot + remove `state/active`

While active:

| Event | Behaviour |
|---|---|
| Tool requires permission | push to TG with Allow/Deny; no answer in `APPROVAL_TIMEOUT`s → passthrough to Desktop UI |
| Auto-approved tool (in `permissions.allow`) | passes silently, no push |
| Claude finished responding (Stop) | `?` in tail → block + push «Stop hook fired»; otherwise quiet notification |
| Claude calls `mcp__remote-bot__ask(...)` | push with force_reply → reply with any text → Claude receives it |
| TG unreachable or bot doesn't answer for 60s | passthrough — local Desktop UI prompt is shown |

## Security

- **`.env` is gitignored** — token and chat_id never enter git
- **Chat-id allowlist** — bot.py only processes messages from `TELEGRAM_CHAT_ID`
- **settings.json double-prompt** — Claude Desktop **additionally** shows its own prompt when editing files in `~/.claude/`, even if the hook approved. By design — defense against self-escalation: otherwise the hook could quietly extend its own `allow:` list
- **Logs without secrets** — only `tool_name`, `req_id`, status are logged. Payload contents stay private

## Known limitations

- **Current implementation is Windows-only.** Uses `pythonw`, `tasklist`, `taskkill`. A Linux/macOS port replaces these with `nohup`/`pkill`/`ps`
- **The hook fires on EVERY tool use** while the bot is active (including Read/Glob/Grep). Most are auto-approved via `permissions.allow`, but the script still runs each time. Narrow it via `matcher` in settings.json if it's noisy
- **Bot is a separate process — survives Claude restart.** If you restart Claude Desktop without `/remotebotstop`, bot.py keeps running while the MCP session is recreated; the link can break briefly. For a clean restart: `/remotebotstop` → restart → `/remotebotstart`
- **Question detection heuristic is simple (`?` only).** Questions phrased without a `?` go undetected (false negative). False positives are unlikely but possible on quotes
- **One pending `ask` at a time.** If Claude calls `ask` while a previous answer is still pending, the second call returns ERROR

## Debug

```bash
# State
cat state/active && echo "active"
cat state/bot.pid

# Bot alive?
tasklist //FI "PID eq $(cat state/bot.pid)"

# Logs
tail -f logs/bot.log     # daemon
tail -f logs/hook.log    # PreToolUse approval requests
tail -f logs/notify.log  # Stop / Notification
tail -f logs/mcp.log     # MCP server (questions and answers)

# Inside Telegram
/start    # connection
/status   # current state (active/off) + PID
```

## Tests

Pytest on the pure helpers `looks_like_question` and `get_last_assistant_text`.
CI runs the suite across Linux / macOS / Windows × Python 3.11 / 3.12.

```bash
pip install -r requirements-dev.txt
pytest
```

## License

MIT

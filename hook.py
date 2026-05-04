"""
PreToolUse hook for Claude Code / Desktop.

Runs before EVERY tool use. The hook itself checks whether the call is
covered by permissions.allow in ~/.claude/settings.json. If yes —
passthrough (no push). If not — pushes inline Allow/Deny buttons to
Telegram and waits for the answer.

Implemented as PreToolUse rather than PermissionRequest because in
Claude Desktop a PermissionRequest hook does not dismiss the built-in
UI prompt — the user would have to answer twice (in TG and in Desktop
UI). PreToolUse intercepts before the UI prompt is rendered, so the
Desktop UI prompt does not appear.

Reads the payload from stdin (JSON). If state/active exists and the
tool is NOT auto-approved — sends an approval request to Telegram and
waits for a response from bot.py.

Stdout output (JSON, the PreToolUse format):
  {} — passthrough (Claude handles permission via the regular flow)
  {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                          "permissionDecision": "allow"}} — approved
  {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                          "permissionDecision": "deny",
                          "permissionDecisionReason": "..."}} — denied
"""
import fnmatch
import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

# Force UTF-8 on stdin/stdout/stderr — otherwise non-ASCII chars in the
# payload (e.g. Bash description) get mangled by the Windows OEM codepage
# (cp866/cp1251).
try:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent
STATE = ROOT / "state"
RESPONSES = STATE / "responses"
LOGS = ROOT / "logs"

LOGS.mkdir(exist_ok=True)

load_dotenv(ROOT / ".env")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TIMEOUT = int(os.getenv("APPROVAL_TIMEOUT", "60"))


def log(msg: str):
    line = f"[{datetime.now().isoformat()}] {msg}\n"
    try:
        with (LOGS / "hook.log").open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def respond(payload: dict, exit_code: int = 0):
    """Print JSON to stdout and exit."""
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(exit_code)


def passthrough():
    """Don't intervene — let Claude Code handle the permission normally."""
    respond({})


def approve():
    """Allow the tool call. PreToolUse hookSpecificOutput format."""
    respond({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        },
    })


def block(reason: str):
    """Deny the tool call with a reason. PreToolUse hookSpecificOutput format."""
    respond({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    })


def load_allow_patterns():
    """Read permissions.allow from ~/.claude/settings.json."""
    settings_path = Path.home() / ".claude" / "settings.json"
    try:
        with settings_path.open(encoding="utf-8") as f:
            data = json.load(f)
        return data.get("permissions", {}).get("allow", [])
    except Exception as e:
        log(f"WARN: failed to read settings.json: {e}")
        return []


def is_auto_approved(tool_name: str, tool_input: dict, allow_patterns: list) -> bool:
    """
    Check whether the tool call is covered by a permissions.allow rule.

    Supported pattern shapes:
    - "ToolName"            — matches every call to that tool
    - "ToolName(literal)"   — exact match of the relevant argument
    - "ToolName(prefix:*)"  — startswith prefix
    - "ToolName(*pattern*)" — fnmatch glob
    """
    for pattern in allow_patterns:
        if "(" not in pattern:
            # Plain tool name — every call
            if pattern.strip() == tool_name:
                return True
            continue

        # Parse "ToolName(args)"
        try:
            tool_part, args_part = pattern.split("(", 1)
            args_part = args_part.rstrip(")")
        except Exception:
            continue

        if tool_part.strip() != tool_name:
            continue

        # Pull the relevant argument from tool_input
        if tool_name == "Bash":
            value = str(tool_input.get("command", ""))
        elif tool_name in ("Read", "Write", "Edit", "MultiEdit"):
            value = str(tool_input.get("file_path", ""))
        elif tool_name == "WebFetch":
            value = str(tool_input.get("url", ""))
        elif tool_name in ("Grep", "Glob"):
            value = str(tool_input.get("pattern", ""))
        else:
            value = json.dumps(tool_input, ensure_ascii=False)

        # Special syntax ":*" at the end of the pattern = startswith
        if args_part.endswith(":*"):
            prefix = args_part[:-2]
            if value.startswith(prefix):
                return True
            continue

        # Otherwise — fnmatch (supports *, ?, [...])
        if fnmatch.fnmatch(value, args_part):
            return True

        # Exact match (for escape sequences or literal strings)
        if value == args_part:
            return True

    return False


def format_summary(payload: dict) -> str:
    """Short HTML summary of the tool use for Telegram."""
    tool = payload.get("tool_name", "?")
    inp = payload.get("tool_input", {}) or {}

    def esc(s):
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    if tool == "Bash":
        cmd = esc(str(inp.get("command", ""))[:400])
        desc = esc(str(inp.get("description", "")))
        body = "<b>Bash</b>"
        if desc:
            body += f"\n<i>{desc}</i>"
        body += f"\n<pre>{cmd}</pre>"
        return body

    if tool in ("Write", "Edit", "Read", "MultiEdit"):
        path = esc(inp.get("file_path", "?"))
        return f"<b>{tool}</b>: <code>{path}</code>"

    if tool == "WebFetch":
        url = esc(inp.get("url", "?"))
        prompt = esc(str(inp.get("prompt", ""))[:200])
        return f"<b>WebFetch</b>: {url}\n<i>{prompt}</i>"

    if tool in ("Grep", "Glob"):
        pattern = esc(inp.get("pattern", "?"))
        return f"<b>{tool}</b>: <code>{pattern}</code>"

    # Default — truncated tool_input as JSON
    inp_str = esc(json.dumps(inp, ensure_ascii=False)[:400])
    return f"<b>{tool}</b>\n<pre>{inp_str}</pre>"


def main():
    # Parse stdin
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        log(f"failed to parse stdin: {e}")
        passthrough()

    # Bot is off — passthrough (fast path)
    if not (STATE / "active").exists():
        passthrough()

    # Config sanity check
    if not TOKEN or not CHAT_ID:
        log("WARN: bot active but .env missing TOKEN/CHAT_ID, passthrough")
        passthrough()

    tool_name = payload.get("tool_name", "?")
    tool_input = payload.get("tool_input", {}) or {}

    # If the tool is already covered by permissions.allow — passthrough
    # without push. Claude Code will handle the permission via the regular
    # flow; the Desktop UI prompt will not appear.
    allow_patterns = load_allow_patterns()
    if is_auto_approved(tool_name, tool_input, allow_patterns):
        log(f"auto-approved: {tool_name}")
        passthrough()

    req_id = uuid.uuid4().hex[:8]
    summary = format_summary(payload)

    log(f"[{req_id}] sending: {tool_name}")

    # Send to Telegram
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": f"<b>Claude requests approval</b> [<code>{req_id}</code>]\n\n{summary}",
                "parse_mode": "HTML",
                "reply_markup": {
                    "inline_keyboard": [[
                        {"text": "Allow", "callback_data": f"{req_id}:approve"},
                        {"text": "Deny", "callback_data": f"{req_id}:deny"},
                    ]]
                },
            },
            timeout=10,
        )
        r.raise_for_status()
    except Exception as e:
        # TG unreachable → graceful degradation onto the Desktop UI prompt.
        log(f"[{req_id}] send failed: {e} — passthrough to Desktop UI")
        passthrough()

    # Wait for the answer (file polling)
    response_file = RESPONSES / f"{req_id}.json"
    deadline = time.time() + TIMEOUT

    while time.time() < deadline:
        if response_file.exists():
            try:
                resp = json.loads(response_file.read_text(encoding="utf-8"))
                response_file.unlink()
                decision = resp.get("decision")
                log(f"[{req_id}] decision: {decision}")
                if decision == "approve":
                    approve()
                else:
                    block("Denied via Telegram")
            except Exception as e:
                log(f"[{req_id}] response parse failed: {e}")
                block(f"Failed to read response: {e}")
        time.sleep(0.5)

    # Timeout — graceful degradation: passthrough rather than block. Then
    # Claude Desktop will render its own permission prompt and the user
    # can answer locally. This covers cases where the TG bot stalled, the
    # callback didn't arrive, or the user is away from the phone. A double
    # prompt (if the user later taps Allow in TG too) is better than a
    # silent block.
    log(f"[{req_id}] timeout {TIMEOUT}s — passthrough to Desktop UI")
    passthrough()


if __name__ == "__main__":
    main()

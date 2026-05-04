"""
MCP server for Claude Remote Bot — exposes the `ask` tool for two-way
chat with the user via Telegram, when they're away from the computer.

Tool: ask(question: str, timeout_seconds: int = 600) -> str
- If Remote Bot is active (state/active exists) — sends the question
  to Telegram and waits for a text reply
- If the bot is off — returns an ERROR string (Claude should fall
  back to asking the question as plain text in chat)

Architecture:
    Claude → ask() tool → Telegram API (sendMessage)
                       → state/pending_question/<req_id>.json (write)
                       → poll state/answers/<req_id>.txt
                       ← return the answer text

    bot.py separately handles plain-text messages from ALLOWED_CHAT_ID:
        text received → is there a pending question? → write to state/answers/

Started by Claude Code's MCP harness (~/.claude/mcp.json) at session start.
"""
import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

try:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import requests
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

ROOT = Path(__file__).parent
STATE = ROOT / "state"
PENDING = STATE / "pending_question"
ANSWERS = STATE / "answers"
LOGS = ROOT / "logs"

PENDING.mkdir(parents=True, exist_ok=True)
ANSWERS.mkdir(parents=True, exist_ok=True)
LOGS.mkdir(exist_ok=True)

load_dotenv(ROOT / ".env")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


def log(msg: str):
    try:
        with (LOGS / "mcp.log").open("a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat()}] {msg}\n")
    except Exception:
        pass


mcp = FastMCP("remote-bot")


@mcp.tool()
def ask(question: str, timeout_seconds: int = 600) -> str:
    """
    Ask the user a question via Telegram and wait for a text reply.

    Use INSTEAD of asking a plain-text question in chat when Remote
    Bot is active (i.e. the user has stepped away from the computer).
    This delivers the answer back from Telegram so the session can
    continue without interruption.

    Args:
        question: The question text. Shown in Telegram. Multi-line
            messages are supported; HTML tags are escaped.
        timeout_seconds: How long to wait for a reply, in seconds.
            Default 600 (10 minutes). Maximum 3600 (1 hour).

    Returns:
        The user's reply text.

    Raises:
        Returns a string prefixed with "ERROR:" if:
        - Remote Bot is not active (no state/active)
        - .env is missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID
        - Another pending question is already in flight
        - The timeout expired
        - A network error occurred while sending to Telegram
    """
    # Fail-safe: don't send anything if the bot is off
    if not (STATE / "active").exists():
        log("ask called but bot not active")
        return (
            "ERROR: Remote Bot is not active (state/active is missing). "
            "Ask the user as plain text in the chat instead."
        )

    if not TOKEN or not CHAT_ID:
        log("ask called but .env missing TOKEN/CHAT_ID")
        return "ERROR: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set in .env"

    # Only one pending question at a time
    existing = list(PENDING.glob("*.json"))
    if existing:
        log(f"ask refused — already pending: {existing[0].name}")
        return (
            "ERROR: There's already an unanswered question in flight. "
            "Wait for the previous answer or ask the user to reply in Desktop."
        )

    timeout_seconds = max(10, min(int(timeout_seconds), 3600))
    req_id = uuid.uuid4().hex[:8]

    # Record the pending question (bot.py uses this to pair the reply)
    pending_file = PENDING / f"{req_id}.json"
    pending_file.write_text(
        json.dumps({
            "req_id": req_id,
            "question": question,
            "created_at": datetime.now().isoformat(),
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    log(f"[{req_id}] pending created")

    # HTML-escape the question
    q_html = question.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # Send to TG — no inline buttons; force_reply nudges the client to focus
    text = (
        f"<b>Claude is asking</b> [<code>{req_id}</code>]\n\n{q_html}\n\n"
        f"<i>Reply with any text — Claude will receive it and continue.</i>"
    )
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": {"force_reply": True, "selective": False},
            },
            timeout=10,
        )
        r.raise_for_status()
    except Exception as e:
        log(f"[{req_id}] send failed: {e}")
        try:
            pending_file.unlink()
        except Exception:
            pass
        return f"ERROR: Failed to send the question to Telegram: {e}"

    log(f"[{req_id}] sent to TG, waiting up to {timeout_seconds}s")

    # Poll state/answers/<req_id>.txt
    answer_file = ANSWERS / f"{req_id}.txt"
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        if answer_file.exists():
            try:
                answer = answer_file.read_text(encoding="utf-8").strip()
                answer_file.unlink()
                # The pending should already be gone (cleaned by bot.py),
                # but remove it just in case.
                if pending_file.exists():
                    pending_file.unlink()
                log(f"[{req_id}] got answer ({len(answer)} chars)")
                return answer
            except Exception as e:
                log(f"[{req_id}] answer read failed: {e}")
                return f"ERROR: Failed to read the answer: {e}"
        time.sleep(0.5)

    # Timeout — clean up the pending file
    log(f"[{req_id}] timeout {timeout_seconds}s")
    try:
        pending_file.unlink()
    except Exception:
        pass
    return (
        f"ERROR: No reply within {timeout_seconds}s. "
        f"Ask the question as plain text in the chat instead."
    )


if __name__ == "__main__":
    log("mcp_server starting")
    mcp.run()

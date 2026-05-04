"""
Fire-and-forget Telegram notifier for Stop / Notification hooks.

Invoked as:
    python notify.py <event-name>

Reads the hook payload (JSON) from stdin and sends a short Telegram
message WITHOUT inline buttons. Doesn't wait for a reply — it's just
a phone push.

Differences vs hook.py:
- doesn't block Claude (exits right after sending)
- doesn't wait for a user callback
- only runs when state/active exists (same gate as hook.py)

Used for:
- Stop event — Claude finished responding, push "ready for input"
- Notification event — Claude needs the user's attention

Special case: when the Stop event fires AND the tail of the last
assistant message contains a question mark, the hook returns a
{"decision": "block", "reason": ...} response so Claude is forced
to re-issue the question through the mcp__remote-bot__ask tool
instead of leaving it as plain text in the chat.
"""
import os
import re
import sys
import json
from pathlib import Path
from datetime import datetime

# Force UTF-8 on stdin/stdout/stderr — payload may contain non-ASCII text
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
LOGS = ROOT / "logs"

LOGS.mkdir(exist_ok=True)

load_dotenv(ROOT / ".env")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


def log(msg: str):
    try:
        with (LOGS / "notify.log").open("a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat()}] {msg}\n")
    except Exception:
        pass


def silent_exit():
    """Exit without intervening in Claude's flow."""
    print(json.dumps({}), flush=True)
    sys.exit(0)


def block_stop(reason: str):
    """Block the Stop event — Claude receives the reason and continues the turn."""
    print(json.dumps({"decision": "block", "reason": reason}), flush=True)
    sys.exit(0)


def looks_like_question(text: str) -> bool:
    """
    Heuristic: does the tail of the response contain a question to the user?

    Only checks for a question mark within the last 400 characters,
    excluding fenced code blocks and inline code (so a `?` inside a
    code example does not trigger a false positive). Marker words
    like "делать"/"продолжить" were intentionally removed — they
    occur in narrative far too often and produced false positives.
    If the author phrases a question without a question mark, that's
    a rare case where a false negative is acceptable.
    """
    if not text:
        return False
    tail = text[-400:]
    cleaned = re.sub(r"```.*?```", "", tail, flags=re.DOTALL)
    cleaned = re.sub(r"`[^`]*`", "", cleaned)
    return "?" in cleaned


def send_telegram_text(text: str):
    """Send a short Telegram notification. Best-effort, errors are swallowed."""
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_notification": False,
            },
            timeout=5,
        )
    except Exception as e:
        log(f"send_telegram_text failed: {e}")


def get_last_assistant_text(transcript_path_str: str) -> str:
    """
    Parse Claude Code's JSONL transcript and return the text of the
    last assistant message. Returns an empty string if nothing matched.
    """
    if not transcript_path_str:
        return ""
    p = Path(transcript_path_str)
    if not p.exists():
        return ""

    last_text = ""
    try:
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue

                # Several possible shapes are supported:
                # 1) {"type": "assistant", "message": {"content": [...]}}
                # 2) {"role": "assistant", "content": [...] | "..."}
                # 3) message.content is a list of blocks {"type":"text","text":"..."}

                role = obj.get("role") or (obj.get("type") if obj.get("type") in ("assistant", "user") else None)
                if role != "assistant":
                    if obj.get("type") == "assistant":
                        role = "assistant"
                    else:
                        continue

                content = None
                msg = obj.get("message")
                if isinstance(msg, dict):
                    content = msg.get("content")
                if content is None:
                    content = obj.get("content")

                if isinstance(content, str):
                    last_text = content
                elif isinstance(content, list):
                    texts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            t = block.get("text", "")
                            if t:
                                texts.append(t)
                    if texts:
                        last_text = "\n".join(texts)
    except Exception:
        pass

    return last_text


def format_message(event: str, payload: dict) -> str:
    """Build the notification text."""
    if event == "Stop":
        # Read the last assistant message from the transcript JSONL
        transcript_path = payload.get("transcript_path", "")
        last = get_last_assistant_text(transcript_path)

        text = "<b>Claude finished responding</b>\n\nReady for your input."
        if last:
            # Telegram message limit is 4096 chars. Keep some headroom for
            # the title (~50 chars) and HTML-escape expansion (& → &amp;).
            # 3500 is a safe ceiling in practice.
            preview_len = 3500
            preview = last[:preview_len].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            suffix = "..." if len(last) > preview_len else ""
            text += f"\n\n<i>{preview}{suffix}</i>"
        return text

    if event == "Notification":
        # Claude needs the user's attention
        msg = payload.get("message", "Claude requires attention")
        msg_esc = str(msg)[:400].replace("<", "&lt;").replace(">", "&gt;")
        return f"<b>Claude needs attention</b>\n\n{msg_esc}"

    # Default
    return f"<b>{event}</b>\n\n<pre>{json.dumps(payload, ensure_ascii=False)[:300]}</pre>"


def main():
    event = sys.argv[1] if len(sys.argv) > 1 else "Unknown"

    # Parse stdin
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        log(f"[{event}] failed to parse stdin: {e}")
        silent_exit()

    # Bot off — silent
    if not (STATE / "active").exists():
        silent_exit()

    if not TOKEN or not CHAT_ID:
        log(f"[{event}] WARN: .env missing TOKEN/CHAT_ID")
        silent_exit()

    # Stop event + question at the end of the answer → block so Claude
    # re-issues via mcp__remote-bot__ask. Loop guard: stop_hook_active=True
    # means we already blocked once — don't block again.
    if event == "Stop":
        already_blocked = bool(payload.get("stop_hook_active", False))
        if not already_blocked:
            transcript_path = payload.get("transcript_path", "")
            last_text = get_last_assistant_text(transcript_path)
            if looks_like_question(last_text):
                log("[Stop] question detected, blocking to force ask tool")
                send_telegram_text(
                    "⚠ <b>Stop hook fired</b>\n\n"
                    "Claude asked a question as plain text without calling "
                    "<code>ask</code>. Forcing it to re-issue via the tool."
                )
                block_stop(
                    "Remote Bot is active — the user is not at the keyboard. "
                    "You ended your turn with a question, but when Remote Bot is "
                    "active you MUST use the mcp__remote-bot__ask tool for any "
                    "question to the user. Re-issue your last question now via "
                    "mcp__remote-bot__ask. Do NOT just repeat the question as text."
                )

    text = format_message(event, payload)
    log(f"[{event}] sending notification")

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_notification": False,
            },
            timeout=8,
        )
        r.raise_for_status()
        log(f"[{event}] sent ok")
    except Exception as e:
        log(f"[{event}] send failed: {e}")

    silent_exit()


if __name__ == "__main__":
    main()

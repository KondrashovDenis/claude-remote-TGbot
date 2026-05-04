"""
Claude Remote Bot — Telegram daemon for Claude Desktop / Code.

Listens to Telegram callback queries from Allow/Deny inline buttons and
writes responses to state/responses/<uuid>.json for hook.py to pick up.
Also forwards plain-text replies into state/answers/<uuid>.txt for the
two-way `ask` flow handled by mcp_server.py.

Run as a daemon:
    pythonw bot.py

Run with console output (debug):
    python bot.py
"""
import os
import sys
import json
import logging
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

ROOT = Path(__file__).parent
STATE = ROOT / "state"
RESPONSES = STATE / "responses"
PENDING_QUESTION = STATE / "pending_question"
ANSWERS = STATE / "answers"
LOGS = ROOT / "logs"

LOGS.mkdir(exist_ok=True)
STATE.mkdir(exist_ok=True)
RESPONSES.mkdir(exist_ok=True)
PENDING_QUESTION.mkdir(exist_ok=True)
ANSWERS.mkdir(exist_ok=True)

load_dotenv(ROOT / ".env")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()
BOT_NAME = os.getenv("TELEGRAM_BOT_NAME", "claude-remote-bot").strip()

logging.basicConfig(
    filename=str(LOGS / "bot.log"),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    encoding="utf-8",
)
logger = logging.getLogger("bot")


def is_allowed(update: Update) -> bool:
    """Allowlist check by chat_id."""
    if update.effective_chat is None:
        return False
    return str(update.effective_chat.id) == ALLOWED_CHAT


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        logger.warning(f"unauthorized /start from chat_id={update.effective_chat.id}")
        return
    active = (STATE / "active").exists()
    state_msg = "ACTIVE" if active else "off"
    await update.message.reply_text(
        f"Claude Remote Bot ({BOT_NAME}) connected.\n"
        f"State: {state_msg}\n\n"
        f"Commands:\n"
        f"/status — current state\n\n"
        f"Toggle from Claude Code via slash commands:\n"
        f"/remotebotstart — push tool requests here\n"
        f"/remotebotstop — passthrough (local approvals)"
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    active = (STATE / "active").exists()
    pid_file = STATE / "bot.pid"
    pid = pid_file.read_text().strip() if pid_file.exists() else "?"
    state_msg = "ACTIVE (all requests are pushed here)" if active else "off (passthrough)"
    await update.message.reply_text(
        f"State: {state_msg}\n"
        f"PID: {pid}"
    )


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    query = update.callback_query
    await query.answer()

    # callback_data format: "<req_id>:<approve|deny>"
    try:
        req_id, decision = query.data.split(":", 1)
    except ValueError:
        logger.warning(f"malformed callback_data: {query.data}")
        return

    if decision not in ("approve", "deny"):
        logger.warning(f"unknown decision: {decision}")
        return

    response_file = RESPONSES / f"{req_id}.json"
    response_file.write_text(
        json.dumps({
            "decision": decision,
            "timestamp": datetime.now().isoformat(),
        }),
        encoding="utf-8",
    )
    logger.info(f"[{req_id}] {decision}")

    # Strip the buttons and post a confirmation reply
    label = "[OK] Allowed" if decision == "approve" else "[X] Denied"
    try:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"{label} — req_id={req_id}")
    except Exception as e:
        logger.warning(f"edit_message failed: {e}")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Plain-text message handler (non-command) — feeds the two-way
    `ask` flow. If there's a pending question, write the answer to
    a file and mcp_server.py will pick it up.
    """
    if not is_allowed(update):
        return
    if update.message is None or update.message.text is None:
        return
    text = update.message.text.strip()
    if not text or text.startswith("/"):
        return  # command — handled by its own handler

    # Take the oldest pending question (FIFO)
    pending_files = sorted(
        PENDING_QUESTION.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
    )
    if not pending_files:
        await update.message.reply_text(
            "No active question from Claude. Message ignored.\n"
            "When Claude calls mcp__remote-bot__ask — reply in this chat."
        )
        return

    pending = pending_files[0]
    req_id = pending.stem

    # Write the answer to state/answers/<req_id>.txt
    answer_file = ANSWERS / f"{req_id}.txt"
    answer_file.write_text(text, encoding="utf-8")

    # Drop the pending — slot freed
    try:
        pending.unlink()
    except Exception:
        pass

    logger.info(f"[{req_id}] text answer accepted ({len(text)} chars)")
    await update.message.reply_text(
        f"[OK] Answer delivered to Claude (req_id={req_id}, {len(text)} chars)."
    )


async def on_error(update, ctx):
    logger.error(f"handler error: {ctx.error}")


def main():
    if not TOKEN or not ALLOWED_CHAT:
        logger.error("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set in .env")
        print("ERROR: fill .env (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)", file=sys.stderr)
        sys.exit(1)

    pid_file = STATE / "bot.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    logger.info(f"bot started, pid={os.getpid()}, name={BOT_NAME}")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(on_callback))
    # Plain text (non-command) — used to answer mcp__remote-bot__ask
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    finally:
        if pid_file.exists():
            try:
                pid_file.unlink()
            except Exception:
                pass
        logger.info("bot stopped")


if __name__ == "__main__":
    main()

"""
MCP server для Claude Remote Bot — tool `ask` для двустороннего диалога с
Денисом через Telegram, когда он не за компом.

Tool: ask(question: str, timeout_seconds: int = 600) -> str
- Если Remote Bot активен (state/active существует) — отправляет вопрос
  в Telegram и ждёт текстового ответа
- Если bot не активен — возвращает error (Claude должен fallback на
  обычный текстовый вопрос)

Архитектура:
    Claude → ask() tool → Telegram API (sendMessage)
                       → state/pending_question/<req_id>.json (запись)
                       → polling state/answers/<req_id>.txt
                       ← возврат текста ответа

    bot.py отдельно обрабатывает текстовые сообщения от ALLOWED_CHAT_ID:
        получил текст → есть pending question? → пишет в state/answers/

Запускается harness'ом Claude Code (через ~/.claude/mcp.json) на каждый старт сессии.
"""
import os
import sys
import json
import time
import uuid
from pathlib import Path
from datetime import datetime

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
    Задать Денису вопрос через Telegram и дождаться текстового ответа.

    Используй ВМЕСТО обычного текстового вопроса в чате когда Remote Bot
    активен (то есть Денис куда-то ушёл от компа). Это позволяет получить
    ответ из Telegram и продолжить работу в той же сессии.

    Args:
        question: Текст вопроса. Будет показан в Telegram. Поддерживает
            многострочные сообщения; HTML-теги будут заэскейплены.
        timeout_seconds: Сколько ждать ответа в секундах. По умолчанию 600
            (10 минут). Максимум 3600 (1 час).

    Returns:
        Текст ответа Дениса.

    Raises:
        Возвращает строку с префиксом "ERROR:" если:
        - Remote Bot не активен (нет state/active)
        - В .env не настроены TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID
        - Уже есть pending question (нужно дождаться предыдущего ответа)
        - Таймаут истёк
        - Сетевая ошибка при отправке в Telegram
    """
    # Fail-safe: если bot не активен - не отправляем
    if not (STATE / "active").exists():
        log("ask called but bot not active")
        return (
            "ERROR: Remote Bot не активен (state/active отсутствует). "
            "Задай вопрос Денису обычным текстом в чате."
        )

    if not TOKEN or not CHAT_ID:
        log("ask called but .env missing TOKEN/CHAT_ID")
        return "ERROR: TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не заданы в .env"

    # Только один pending question за раз
    existing = list(PENDING.glob("*.json"))
    if existing:
        log(f"ask refused — already pending: {existing[0].name}")
        return (
            "ERROR: Уже есть неотвеченный вопрос Денису. "
            "Дождись ответа на предыдущий вопрос или попроси Дениса в Desktop ответить."
        )

    timeout_seconds = max(10, min(int(timeout_seconds), 3600))
    req_id = uuid.uuid4().hex[:8]

    # Записываем pending question (для bot.py — он связывает с ответом)
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

    # HTML escape вопроса
    q_html = question.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # Отправка в ТГ — без inline-кнопок, force_reply чтобы клиент подсветил
    text = f"<b>Claude спрашивает</b> [<code>{req_id}</code>]\n\n{q_html}\n\n<i>Ответь любым текстом — Claude получит и продолжит.</i>"
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
        return f"ERROR: Не удалось отправить вопрос в Telegram: {e}"

    log(f"[{req_id}] sent to TG, waiting up to {timeout_seconds}s")

    # Polling state/answers/<req_id>.txt
    answer_file = ANSWERS / f"{req_id}.txt"
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        if answer_file.exists():
            try:
                answer = answer_file.read_text(encoding="utf-8").strip()
                answer_file.unlink()
                # pending уже должен быть удалён bot.py, но на всякий
                if pending_file.exists():
                    pending_file.unlink()
                log(f"[{req_id}] got answer ({len(answer)} chars)")
                return answer
            except Exception as e:
                log(f"[{req_id}] answer read failed: {e}")
                return f"ERROR: Не удалось прочитать ответ: {e}"
        time.sleep(0.5)

    # Timeout — чистим pending
    log(f"[{req_id}] timeout {timeout_seconds}s")
    try:
        pending_file.unlink()
    except Exception:
        pass
    return f"ERROR: Денис не ответил за {timeout_seconds} секунд. Задай вопрос обычным текстом в чате."


if __name__ == "__main__":
    log("mcp_server starting")
    mcp.run()

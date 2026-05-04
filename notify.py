"""
Fire-and-forget уведомление в Telegram для Stop / Notification hooks.

Запускается как:
    python notify.py <event-name>

Читает stdin (hook payload JSON) и шлёт в ТГ короткое уведомление БЕЗ inline-кнопок.
Не ждёт ответа — это просто пуш на телефон.

В отличие от hook.py:
- не блокирует Claude (выходит сразу после отправки)
- не ждёт callback от пользователя
- работает только если state/active существует (как и hook.py)

Используется для:
- Stop event — Claude закончил отвечать, push "готов ждать ввод"
- Notification event — Claude нужно внимание пользователя (вопрос/блокировка)
"""
import os
import re
import sys
import json
from pathlib import Path
from datetime import datetime

# UTF-8 на Windows-консоли (stdin тоже — payload может содержать кириллицу)
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
    """Выход без вмешательства в работу Claude."""
    print(json.dumps({}), flush=True)
    sys.exit(0)


def block_stop(reason: str):
    """Заблокировать Stop event — Claude получит reason и продолжит turn."""
    print(json.dumps({"decision": "block", "reason": reason}), flush=True)
    sys.exit(0)


def looks_like_question(text: str) -> bool:
    """
    Эвристика: содержит ли хвост ответа вопрос к пользователю.

    Только проверка наличия знака вопроса в последних 400 символах,
    исключая содержимое fenced code-блоков и inline-кода (чтобы
    знак вопроса внутри примера кода не триггерил false positive).
    Маркеры типа "делать", "продолжить" и т.п. убраны — они слишком
    часто встречаются в нарративе и дают ложные срабатывания.
    Если автор формулирует вопрос без знака вопроса — это редкий
    кейс, false negative окей.
    """
    if not text:
        return False
    tail = text[-400:]
    cleaned = re.sub(r"```.*?```", "", tail, flags=re.DOTALL)
    cleaned = re.sub(r"`[^`]*`", "", cleaned)
    return "?" in cleaned


def send_telegram_text(text: str):
    """Отправка короткого уведомления в ТГ. Best-effort, ошибки молча."""
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
    Прочитать JSONL transcript Claude Code и вернуть текст последнего
    assistant-сообщения. Возвращает пустую строку если ничего не нашли.
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

                # Поддержка нескольких возможных форматов:
                # 1) {"type": "assistant", "message": {"content": [...]}}
                # 2) {"role": "assistant", "content": [...] | "..."}
                # 3) внутри message.content — list текстовых блоков {"type":"text","text":"..."}

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
    """Собрать текст уведомления."""
    if event == "Stop":
        # Читаем последнее assistant-сообщение из transcript JSONL
        transcript_path = payload.get("transcript_path", "")
        last = get_last_assistant_text(transcript_path)

        text = "<b>Claude закончил отвечать</b>\n\nГотов ждать твой ввод."
        if last:
            # Telegram лимит на сообщение - 4096 символов. Оставляем запас на
            # заголовок (~50 chars) и на расширение HTML-escape (& → &amp; и т.п.).
            # 3500 даёт безопасный запас в большинстве случаев.
            preview_len = 3500
            preview = last[:preview_len].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            suffix = "..." if len(last) > preview_len else ""
            text += f"\n\n<i>{preview}{suffix}</i>"
        return text

    if event == "Notification":
        # Claude нужно внимание пользователя
        msg = payload.get("message", "Claude requires attention")
        msg_esc = str(msg)[:400].replace("<", "&lt;").replace(">", "&gt;")
        return f"<b>Claude требует внимания</b>\n\n{msg_esc}"

    # Дефолт
    return f"<b>{event}</b>\n\n<pre>{json.dumps(payload, ensure_ascii=False)[:300]}</pre>"


def main():
    event = sys.argv[1] if len(sys.argv) > 1 else "Unknown"

    # Парсинг stdin
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        log(f"[{event}] failed to parse stdin: {e}")
        silent_exit()

    # Bot не активен - молчим
    if not (STATE / "active").exists():
        silent_exit()

    if not TOKEN or not CHAT_ID:
        log(f"[{event}] WARN: .env missing TOKEN/CHAT_ID")
        silent_exit()

    # Stop event + вопрос в конце ответа → блокируем, чтобы Claude перевызвал
    # через mcp__remote-bot__ask. Защита от цикла: stop_hook_active=True
    # значит блокировка уже была — больше не блокируем.
    if event == "Stop":
        already_blocked = bool(payload.get("stop_hook_active", False))
        if not already_blocked:
            transcript_path = payload.get("transcript_path", "")
            last_text = get_last_assistant_text(transcript_path)
            # DEBUG: фиксируем что именно увидел hook
            log(f"[Stop] transcript={transcript_path}")
            log(f"[Stop] last_text len={len(last_text)} tail200={last_text[-200:]!r}")
            if looks_like_question(last_text):
                log("[Stop] question detected, blocking to force ask tool")
                send_telegram_text(
                    "⚠ <b>Stop hook сработал</b>\n\n"
                    "Claude задал вопрос текстом, не вызвав <code>ask</code>. "
                    "Заставляю перевызвать через инструмент."
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

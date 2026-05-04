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
    print(json.dumps({}))
    sys.exit(0)


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
            preview_len = 200
            preview = last[:preview_len].replace("<", "&lt;").replace(">", "&gt;")
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

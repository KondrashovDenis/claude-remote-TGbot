"""
PreToolUse hook для Claude Code.

Запускается перед КАЖДЫМ tool use. Hook сам проверяет, покрыт ли вызов
permissions.allow из ~/.claude/settings.json. Если да — passthrough (без push).
Если нет — отправляет в Telegram inline-кнопки и ждёт ответа.

Это сделано вместо event PermissionRequest, потому что в Claude Desktop
PermissionRequest hook не закрывает встроенный UI prompt — пользователю
пришлось бы отвечать дважды (в ТГ и в Desktop UI). PreToolUse перехватывает
до отрисовки UI prompt'а, поэтому в Desktop UI prompt не появляется.

Читает payload из stdin (JSON). Если state/active существует и tool НЕ
auto-approved — шлёт запрос в Telegram и ждёт ответа от bot.py.

Stdout output (JSON, формат для PreToolUse):
  {} — passthrough (Claude обработает permission обычным flow)
  {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                          "permissionDecision": "allow"}} — одобрено
  {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                          "permissionDecision": "deny",
                          "permissionDecisionReason": "..."}} — отклонено
"""
import os
import sys
import json
import time
import uuid
import fnmatch
from pathlib import Path
from datetime import datetime

# Принудительно UTF-8 на stdin/stdout/stderr — иначе кириллица в payload
# (description у Bash и т.п.) приходит/уходит в Windows-кодировке (cp866/cp1251)
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
TIMEOUT = int(os.getenv("APPROVAL_TIMEOUT", "300"))


def log(msg: str):
    line = f"[{datetime.now().isoformat()}] {msg}\n"
    try:
        with (LOGS / "hook.log").open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def respond(payload: dict, exit_code: int = 0):
    """Вывести JSON в stdout и выйти."""
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(exit_code)


def passthrough():
    """Не вмешиваемся — пусть Claude Code обработает permission обычным образом."""
    respond({})


def approve():
    """Разрешить tool call. Формат hookSpecificOutput для PreToolUse."""
    respond({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        },
    })


def block(reason: str):
    """Заблокировать tool call с причиной. Формат hookSpecificOutput для PreToolUse."""
    respond({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    })


# Кеш загруженного списка allow-патернов чтобы не читать settings.json
# на каждый hook invocation (но hook это отдельный процесс, кеш живёт только
# в рамках одного вызова — оставлено для будущей оптимизации).
def load_allow_patterns():
    """Прочитать permissions.allow из ~/.claude/settings.json."""
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
    Проверяет, покрыт ли tool call правилом из permissions.allow.

    Поддерживает:
    - "ToolName" — matches все вызовы этого tool
    - "ToolName(args)" с literal args — exact match
    - "ToolName(prefix:*)" — startswith prefix
    - "ToolName(*pattern*)" — fnmatch glob
    """
    for pattern in allow_patterns:
        if "(" not in pattern:
            # Tool name only — все вызовы
            if pattern.strip() == tool_name:
                return True
            continue

        # Парсим "ToolName(args)"
        try:
            tool_part, args_part = pattern.split("(", 1)
            args_part = args_part.rstrip(")")
        except Exception:
            continue

        if tool_part.strip() != tool_name:
            continue

        # Достаём релевантный аргумент из tool_input
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

        # Спец-синтаксис ":*" в конце паттерна = startswith
        if args_part.endswith(":*"):
            prefix = args_part[:-2]
            if value.startswith(prefix):
                return True
            continue

        # Иначе — fnmatch (поддержит *, ?, [...])
        if fnmatch.fnmatch(value, args_part):
            return True

        # Точное совпадение (escape sequences и exact strings)
        if value == args_part:
            return True

    return False


def format_summary(payload: dict) -> str:
    """Краткое HTML-описание tool use для Telegram."""
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

    # Дефолт - JSON tool_input в обрезанном виде
    inp_str = esc(json.dumps(inp, ensure_ascii=False)[:400])
    return f"<b>{tool}</b>\n<pre>{inp_str}</pre>"


def main():
    # Парсинг stdin
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        log(f"failed to parse stdin: {e}")
        passthrough()

    # Bot не активен - passthrough (быстрый путь)
    if not (STATE / "active").exists():
        passthrough()

    # Валидация конфига
    if not TOKEN or not CHAT_ID:
        log("WARN: bot active but .env missing TOKEN/CHAT_ID, passthrough")
        passthrough()

    tool_name = payload.get("tool_name", "?")
    tool_input = payload.get("tool_input", {}) or {}

    # Если tool уже покрыт permissions.allow - passthrough без push
    # (Claude обработает permission обычным flow, в Desktop UI prompt не появится)
    allow_patterns = load_allow_patterns()
    if is_auto_approved(tool_name, tool_input, allow_patterns):
        log(f"auto-approved: {tool_name}")
        passthrough()

    req_id = uuid.uuid4().hex[:8]
    summary = format_summary(payload)

    log(f"[{req_id}] sending: {tool_name}")

    # Отправка в ТГ
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
        log(f"[{req_id}] send failed: {e}")
        block(f"Не удалось доставить запрос на approval в Telegram: {e}")

    # Ждём ответа (polling файла)
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
                    block("Денис отказал через Telegram")
            except Exception as e:
                log(f"[{req_id}] response parse failed: {e}")
                block(f"Ошибка чтения ответа: {e}")
        time.sleep(0.5)

    log(f"[{req_id}] timeout {TIMEOUT}s")
    block(f"Таймаут ожидания ответа в Telegram ({TIMEOUT}s)")


if __name__ == "__main__":
    main()

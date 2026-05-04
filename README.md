# Claude Remote Bot

Telegram-бот для удалённого approve/deny tool-запросов Claude Code, когда Денис не за компом.

Работает через три hook'а в `~/.claude/settings.json`:
- **PreToolUse** (hook.py) — на каждый tool call. Hook сам читает `permissions.allow` из settings.json: если tool покрыт — passthrough без push. Если нет — push с Allow/Deny в ТГ.
- **Stop** (notify.py Stop) — fire-and-forget push когда Claude закончил отвечать. С превью текста последнего сообщения (читается из transcript JSONL).
- **Notification** (notify.py Notification) — fire-and-forget push когда Claude требует внимания.

Включается/выключается slash-командами `/remotebotstart` и `/remotebotstop` прямо из Claude Code.

## Архитектура

```
Сценарий 1 — tool требует подтверждения:
       Claude Code → PreToolUse hook (hook.py)
              │
              ├── state/active нет ──► passthrough (обычный flow: либо auto-allow,
              │                                     либо UI prompt в Desktop)
              │
              ├── state/active есть И tool покрыт permissions.allow ──► passthrough
              │   (быстрый путь: tool пройдёт обычным auto-approve, без push)
              │
              └── state/active есть И tool НЕ покрыт permissions.allow:
                     │
                     ▼
                Telegram API: sendMessage + inline-кнопки Allow/Deny
                     │
                hook.py polling state/responses/<req_id>.json (до APPROVAL_TIMEOUT)
                     │
              ┌──────┴──────┐
              ▼             ▼
   {hookSpecificOutput.   {hookSpecificOutput.
    permissionDecision:    permissionDecision:
    "allow"}               "deny",
                          "permissionDecisionReason": ...}

Сценарий 2 — Claude закончил / нужно внимание:
       Claude Code → Stop / Notification hook (notify.py)
              │
              ├── state/active нет ──► silent exit
              │
              └── state/active есть:
                     │
                     ▼
                Stop: читаем transcript_path JSONL, берём текст последнего
                      assistant-сообщения, превью первые 200 символов
                Notification: текст из payload.message
                     │
                     ▼
                Telegram API: sendMessage БЕЗ кнопок (информация)
                fire-and-forget, не блокирует Claude

bot.py (daemon, отдельный процесс):
   слушает Telegram updates → callback_query на Allow/Deny
   → пишет state/responses/<req_id>.json для hook.py
```

**Почему не PermissionRequest event?** Логически он подходит больше (срабатывает только на не-auto-approved tools), но в Claude Desktop версии 2.1.121 hook output **не закрывает** встроенный UI prompt — он висит параллельно с push в ТГ. Пришлось бы отвечать дважды. PreToolUse перехватывает permission ДО отрисовки UI prompt'а, но видит ВСЕ tool calls — поэтому фильтр `permissions.allow` реализован самостоятельно в `hook.py:is_auto_approved`.

## Установка

### 1. Создать ТГ-бота
@BotFather → /newbot → имя/username → получить token.

### 2. Узнать свой chat_id
@userinfobot → отправить любое сообщение → получить ID.

### 3. Заполнить .env
```
cp D:\Claude\claude-remote-bot\.env.example D:\Claude\claude-remote-bot\.env
```
Открыть `.env` и заполнить:
- `TELEGRAM_BOT_TOKEN` — из BotFather
- `TELEGRAM_BOT_NAME` — username бота без `@`
- `TELEGRAM_CHAT_ID` — твой ID (числовой)
- `APPROVAL_TIMEOUT` — секунды ожидания ответа (по умолчанию 300)

### 4. Установить Python-зависимости
```
pip install -r D:\Claude\claude-remote-bot\requirements.txt
```

### 5. Прописать hooks в `~/.claude/settings.json`

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python D:/Claude/claude-remote-bot/hook.py",
            "timeout": 360
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python D:/Claude/claude-remote-bot/notify.py Stop",
            "timeout": 10,
            "async": true
          }
        ]
      }
    ],
    "Notification": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python D:/Claude/claude-remote-bot/notify.py Notification",
            "timeout": 10,
            "async": true
          }
        ]
      }
    ]
  }
}
```

`timeout: 360` для PreToolUse должен быть больше `APPROVAL_TIMEOUT` (300) с запасом на сеть. Для notify.py `async: true` — не блокировать Claude вообще.

В Claude Desktop settings.json подхватывается без перезапуска вкладки (по крайней мере для версии 2.1.121). Если изменения не подхватились — открыть `/hooks` или перезапустить вкладку.

### 6. Проверить
В Telegram: написать боту `/start` — должен ответить "Claude Remote Bot подключён".

## Использование

В Claude Code:
- `/remotebotstart` — включить (запустит bot процесс + создаст state/active)
- `/remotebotstop` — выключить (убьёт bot + удалит state/active)

Когда включено:
- **Tool requiring permission** → push с Allow/Deny. Не ответил за `APPROVAL_TIMEOUT` сек → автоматический deny.
- **Claude закончил отвечать** (Stop) → push "готов ждать твой ввод" с превью последнего сообщения.
- **Claude нужно внимание** (Notification) → push с текстом уведомления.

Auto-approved tools (через `permissions.allow` в settings.json) **не** триггерят push на approval. Это намеренно: смысл подтверждать удалённо то, на что и в Desktop-UI не было бы prompt'а.

## Файловая структура

```
D:\Claude\claude-remote-bot\
├── .env                      # секреты, gitignored
├── .env.example
├── .gitignore
├── README.md
├── requirements.txt
├── bot.py                    # Telegram daemon
├── hook.py                   # PreToolUse hook (двусторонний flow с Allow/Deny + фильтр permissions.allow)
├── notify.py                 # Stop / Notification hook (fire-and-forget push)
├── state/
│   ├── active                # флаг — есть = бот пушит
│   ├── bot.pid               # PID запущенного bot процесса
│   ├── pending/              # (зарезервировано)
│   └── responses/<req_id>.json  # ответы от bot.py для hook.py
└── logs/
    ├── bot.log               # лог daemon
    ├── hook.log              # лог approval-запросов
    └── notify.log            # лог fire-and-forget уведомлений
```

## Безопасность

- **Allowlist по chat_id** — bot отвечает только на сообщения с `TELEGRAM_CHAT_ID` из .env. Если кто-то узнает username бота — он не сможет отдавать команды.
- **Безопасный default при сбое** — если ТГ недоступен или ответ не пришёл за timeout, hook возвращает `block`. Лучше блок ложно-позитивный, чем разрешить опасное действие.
- **`.env` в .gitignore** — токен не попадает в git.
- **Запросы видны только тебе** — bot не логирует payload в открытом виде, только tool_name и req_id в `logs/`.

## Известные ограничения

- **Только Windows** (использует `pythonw`, `taskkill`, `Start-Process`).
- **Сессия Claude Code должна быть запущена** на компе — это hook, не cloud-агент. Если ноут спит, ничего не работает.
- **Hook вызывается на КАЖДОМ tool use** когда бот активен (включая Read/Glob/Grep). Это намеренно — так задумал Денис. Если станет шумно, можно ограничить через `matcher` в settings.json.
- **Bot переживает перезапуск Claude Code сессии** — он отдельный процесс. Чтобы остановить — `/remotebotstop` или вручную `taskkill /PID <pid> /F`.

## Отладка

- Логи: `D:\Claude\claude-remote-bot\logs\bot.log`, `logs\hook.log`
- Состояние: в Telegram → `/status`
- Если bot не отвечает на `/start` — проверь токен и что процесс запущен (`tasklist | findstr pythonw`)
- Если hook вешает Claude Code — проверь что `APPROVAL_TIMEOUT` < hook `timeout` в settings.json и что bot процесс жив

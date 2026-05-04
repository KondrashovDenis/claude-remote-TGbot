# Claude Remote Bot

> 🇷🇺 Русский · [🇬🇧 English](README.en.md)

Telegram-мост для **Claude Desktop / Claude Code**: одобряй tool calls и **отвечай на вопросы Claude текстом** прямо из Telegram, когда не у компа.

<p align="center">
  <img src="ccmascot.jpg" alt="Claude Code mascot" width="240"/>
</p>

## Зачем

Claude часто упирается в подтверждение — нажать Allow на bash-команду, edit файла, MCP-tool вне `permissions.allow`. Если ты отошёл от компа — сессия зависает на permission-prompt. А ещё Claude нередко задаёт уточняющие вопросы текстом в чате — тоже без тебя они никому не видны.

Этот бот закрывает оба кейса:

1. **PreToolUse hook** — ловит permission requests до отрисовки Desktop UI и пушит их в Telegram с кнопками Allow / Deny
2. **MCP tool `ask`** — Claude может задать **текстовый вопрос** и получить **текстовый ответ** через Telegram, продолжая ту же сессию
3. **Stop hook** — если Claude по привычке задал вопрос текстом в чате, hook ловит знак вопроса в хвосте ответа и блокирует завершение turn'а, заставляя Claude перевызвать через `ask`
4. **Graceful degradation** — если Telegram недоступен или бот завис, hook автоматически делает passthrough на обычный Desktop UI prompt; ты не блокирован

## Фичи

- **Smart auto-approve.** Hook парсит `permissions.allow` из `~/.claude/settings.json` сам — если tool call покрыт паттерном (`Bash(npm *)`, `mcp__github__*` и т.п.), пуш в Telegram не идёт.
- **Двусторонний диалог через MCP `ask`.** Не только approve/deny, но и текстовые ответы.
- **Question detection.** Stop hook детектит `?` в последних 400 символах ответа Claude (исключая code-блоки) и форсит использование `ask`.
- **Allowlist по chat_id.** Бот реагирует только на сообщения с твоего `TELEGRAM_CHAT_ID`. Узнает username бота сторонний — команды отдать не сможет.
- **Graceful TG-down fallback.** При ошибке отправки в TG или таймауте 60s ожидания ответа hook возвращает passthrough вместо block — Desktop UI отрисует свой prompt.

## Как это работает

```
┌─ Сценарий 1: tool требует подтверждения ────────────────────────┐
│  Claude → PreToolUse hook (hook.py)                              │
│    │                                                              │
│    ├─ state/active нет → passthrough (обычный Desktop flow)      │
│    ├─ покрыт permissions.allow → passthrough (без push)          │
│    └─ не покрыт → push в TG с Allow/Deny                         │
│         polling state/responses/<id>.json                        │
│         ├─ approve → permissionDecision: "allow"                 │
│         ├─ deny → permissionDecision: "deny"                     │
│         └─ timeout 60s или ошибка TG → passthrough (Desktop UI)  │
└──────────────────────────────────────────────────────────────────┘

┌─ Сценарий 2: Claude задаёт текстовый вопрос ────────────────────┐
│  Claude → mcp__remote-bot__ask("какой вариант — A или B?")      │
│    └─ pending_question/<id>.json + push в TG (force_reply)       │
│         polling state/answers/<id>.txt до timeout                │
│         └─ возвращает текст ответа Claude'у                      │
│                                                                   │
│  bot.py при текстовом сообщении от ALLOWED_CHAT:                 │
│    └─ ищет старейший pending → пишет answer file → удаляет PEND  │
└──────────────────────────────────────────────────────────────────┘

┌─ Сценарий 3: Claude закончил с вопросом, забыл ask ─────────────┐
│  Stop hook (notify.py Stop)                                      │
│    └─ читает transcript_path → последнее assistant сообщение     │
│         └─ есть '?' в хвосте → {decision:block, reason}          │
│              Claude вынужден перевызвать через ask               │
└──────────────────────────────────────────────────────────────────┘
```

**Почему PreToolUse, а не PermissionRequest?** Логически PermissionRequest подходит больше, но в Claude Desktop hook output **не закрывает** встроенный UI prompt — он висит параллельно с push в TG, надо отвечать дважды. PreToolUse перехватывает permission ДО отрисовки UI, но видит ВСЕ tool calls — поэтому фильтр `permissions.allow` реализован самостоятельно в `hook.py:is_auto_approved`.

## Файлы

```
.
├── bot.py                  # Telegram daemon: callback queries (Allow/Deny) + текстовые ответы
├── hook.py                 # PreToolUse hook: фильтр permissions.allow + push approve/deny
├── notify.py               # Stop / Notification hook: question detection + push notif
├── mcp_server.py           # MCP server: tool `ask` для двустороннего диалога
├── requirements.txt
├── .env.example
├── examples/
│   └── commands/           # slash-команды для Claude Code
│       ├── remotebotstart.md
│       └── remotebotstop.md
├── state/                  # runtime IPC (gitignored содержимое)
│   ├── active              # флаг "бот пушит в TG"
│   ├── bot.pid
│   ├── pending_question/<id>.json
│   ├── answers/<id>.txt
│   └── responses/<id>.json
└── logs/
```

## Установка

### 1. Создать TG-бота
[@BotFather](https://t.me/BotFather) → `/newbot` → имя/username → получить token.

### 2. Узнать свой chat_id
[@userinfobot](https://t.me/userinfobot) → отправить любое сообщение → получить numeric ID.

### 3. Клонировать и заполнить .env

```bash
git clone https://github.com/KondrashovDenis/claude-remote-TGbot.git
cd claude-remote-TGbot
cp .env.example .env
# открыть .env и заполнить:
#   TELEGRAM_BOT_TOKEN  — из BotFather
#   TELEGRAM_BOT_NAME   — username бота без @
#   TELEGRAM_CHAT_ID    — твой numeric ID
#   APPROVAL_TIMEOUT    — секунды ожидания ответа (по умолчанию 60)
```

### 4. Установить зависимости

```bash
pip install -r requirements.txt
```

### 5. Зарегистрировать MCP server

В `~/.claude/mcp.json` (или эквивалентном для твоего клиента):

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

### 6. Прописать hooks и permissions в `~/.claude/settings.json`

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

Важно:
- `Stop` hook **НЕ** должен быть `async: true` — иначе Claude не дожидается `decision:block` и блокировка не сработает
- `Notification` может быть `async: true` (это fire-and-forget уведомление)
- `MCP_TOOL_TIMEOUT=3700000` (3700 сек) перекрывает дефолтный 60s от MCP client'а — иначе долгие `ask` упадут раньше времени

### 7. Скопировать slash-commands

```bash
mkdir -p ~/.claude/commands
cp examples/commands/remotebotstart.md ~/.claude/commands/
cp examples/commands/remotebotstop.md ~/.claude/commands/
# отредактируй пути внутри них на свой каталог установки
```

### 8. Проверить

В Telegram → написать боту `/start` → должен ответить «Claude Remote Bot подключён».

## Использование

В Claude Code / Desktop:
- `/remotebotstart` — поднять bot.py + создать `state/active`
- `/remotebotstop` — убить bot + удалить `state/active`

Когда включено:

| Событие | Поведение |
|---|---|
| Tool requiring permission | push в TG с Allow/Deny; не ответил за `APPROVAL_TIMEOUT`s → passthrough на Desktop UI |
| Auto-approved tool (в `permissions.allow`) | проходит без push |
| Claude закончил отвечать (Stop) | если в хвосте есть `?` → block + push «Stop hook сработал»; иначе тихое уведомление |
| Claude вызывает `mcp__remote-bot__ask(...)` | push с force_reply → ответь любым текстом → Claude получит результат |
| TG недоступен или бот не отвечает за 60s | passthrough — Desktop UI prompt появится локально |

## Безопасность

- **`.env` в .gitignore** — токен и chat_id не попадают в git
- **Allowlist по chat_id** — bot.py обрабатывает только сообщения с `TELEGRAM_CHAT_ID`
- **Защита settings.json** — Claude Desktop поверх hook approve **дополнительно** показывает свой prompt при редактировании файлов в `~/.claude/`. Это by-design защита от self-escalation: иначе hook мог бы дописать любые `allow:` patterns
- **Логи без секретов** — в `logs/` пишется только `tool_name`, `req_id`, статус. Содержимое payload не утекает

## Известные ограничения

- **Текущая реализация — Windows.** Использует `pythonw`, `tasklist`, `taskkill`. Адаптация под Linux/macOS требует замены на `nohup`/`pkill`/`ps`
- **Hook вызывается на КАЖДЫЙ tool use** при активном боте (включая Read/Glob/Grep). Для большинства они auto-approved через `permissions.allow`, но скрипт всё равно запускается. Можно ограничить через `matcher` в settings.json
- **Bot — отдельный процесс, переживает рестарт Claude.** Если рестартнул Claude Desktop без `/remotebotstop`, bot.py остаётся жить — но MCP-сессия пересоздаётся, и связка может временно поломаться. Делай чистый рестарт через `/remotebotstop` → рестарт → `/remotebotstart`
- **Эвристика question detection — простая (по символу `?`)**. Вопросы без знака вопроса не ловятся (false negative). False positive (срабатывание на не-вопрос) маловероятен, но возможен на цитатах
- **Один pending `ask` за раз.** Если Claude вызовет `ask` пока предыдущий ответ ещё не получен — второй вернёт ERROR

## Отладка

```bash
# Состояние
cat state/active && echo "active"
cat state/bot.pid

# Bot жив?
tasklist //FI "PID eq $(cat state/bot.pid)"

# Логи
tail -f logs/bot.log     # daemon
tail -f logs/hook.log    # PreToolUse approval requests
tail -f logs/notify.log  # Stop / Notification
tail -f logs/mcp.log     # MCP server (вопросы и ответы)

# В Telegram
/start    # подключение
/status   # текущее состояние (АКТИВЕН/выключен) + PID
```

## Лицензия

MIT

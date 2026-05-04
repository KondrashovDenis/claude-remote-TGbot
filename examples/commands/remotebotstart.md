---
description: Запустить Claude Remote Bot — пушить tool-запросы (требующие подтверждения) в Telegram для удалённого approve/deny с телефона
allowed-tools: Bash, Read
---

Запусти Claude Remote Bot для удалённого подтверждения действий через Telegram.

Делай **одной Bash-командой**:

```bash
# Замени путь на свой каталог установки
cd /path/to/claude-remote-bot

# 1. Проверка конфига
test -f .env || { echo "FAIL: создай .env из .env.example и заполни TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_BOT_NAME"; exit 1; }

# 2. Проверка зависимостей
python -c "import telegram, dotenv, requests" 2>&1 || { echo "deps missing, ставлю..."; pip install -q -r requirements.txt; }

# 3. Проверка что бот не запущен
if [ -f state/bot.pid ]; then
  PID=$(cat state/bot.pid)
  if tasklist //FI "PID eq $PID" //NH 2>/dev/null | grep -q "$PID"; then
    [ ! -f state/active ] && touch state/active
    echo "Bot уже запущен (PID: $PID)"; exit 0
  fi
  rm -f state/bot.pid
fi

# 4-5. Запуск + флаг
pythonw bot.py &
touch state/active
sleep 3

# 6-7. Проверка
if [ -f state/bot.pid ] && tasklist //FI "PID eq $(cat state/bot.pid)" //NH 2>/dev/null | grep -q "$(cat state/bot.pid)"; then
  echo "Remote Bot запущен (PID: $(cat state/bot.pid)). Tool-запросы вне permissions.allow пушатся в Telegram до /remotebotstop."
  echo "Лог: logs/bot.log"
else
  echo "FAIL: bot не стартанул. Лог:"
  tail -20 logs/bot.log 2>/dev/null
fi
```

После запуска: проверь в Telegram бот ответ на `/start` или `/status` — должен показать "АКТИВЕН".

**Важно:** push в TG приходит **только** на tool calls которые НЕ покрыты `permissions.allow` в `~/.claude/settings.json`. Auto-approved tools (Bash из allow-list, MCP tools и т.п.) проходят обычным flow без push.

Если на любом шаге ошибка — покажи её, не делай вид что всё ок.

---

## КРИТИЧНО для Claude — пока bot активен

После успешного запуска ты должен **в этой сессии и до /remotebotstop** соблюдать одно правило:

**Когда тебе нужно задать пользователю вопрос или попросить решение** (выбор между вариантами, уточнение параметра, "как назвать", "продолжить ли" и т.п.) — **ВСЕГДА используй tool `mcp__remote-bot__ask`** ВМЕСТО обычного текстового вопроса в чате.

```
mcp__remote-bot__ask(question="Какое имя файла выбрать — A или B?", timeout_seconds=600)
```

Tool вернёт:
- **Текст ответа пользователя** → ты получаешь как результат и продолжаешь работать
- **"ERROR: ..."** → fallback на обычный текстовый вопрос в чате (bot выключился, сетевая ошибка, и т.п.)

После `/remotebotstop` tool автоматически возвращает ERROR (state/active удалён) — то есть переключение обратно на обычный flow самопроизвольное.

Это нужно потому что пользователь может быть не за компом — обычный текстовый вопрос ему не виден. MCP-tool пушит вопрос в Telegram, пользователь отвечает из TG, Claude получает ответ и продолжает сессию без перерыва.

Дополнительная подстраховка — Stop hook (`notify.py`) детектит знак вопроса в хвосте ответа Claude и при активном боте возвращает `decision: block`, заставляя Claude перевызвать вопрос через `ask`. Но опираться на это не стоит — следуй правилу сам.

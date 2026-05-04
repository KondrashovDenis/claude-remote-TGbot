---
description: Запустить Claude Remote Bot — пушить tool-запросы (требующие подтверждения) в Telegram для удалённого approve/deny с телефона
allowed-tools: Bash, Read
---

Запусти Claude Remote Bot для удалённого подтверждения действий через Telegram.

Делай **одной Bash-командой** без `cd` (абсолютные пути нужны чтобы избежать
Desktop-UI защиты от untrusted git hooks — она срабатывает на любую команду
вида `cd ...; git ...`):

```bash
# Замени BOT_HOME на абсолютный путь к каталогу установки. Если bash
# не поддерживает heredoc-переменные в проде — раскрой вручную:
BOT_HOME=/path/to/claude-remote-TGbot

# 1. Проверка конфига
test -f "$BOT_HOME/.env" || { echo "FAIL: создай .env из .env.example"; exit 1; }

# 2. Проверка зависимостей
python -c "import telegram, dotenv, requests" 2>&1 || { echo "deps missing, ставлю..."; pip install -q -r "$BOT_HOME/requirements.txt"; }

# 3. Проверка что бот не запущен
if [ -f "$BOT_HOME/state/bot.pid" ]; then
  PID=$(cat "$BOT_HOME/state/bot.pid")
  if tasklist //FI "PID eq $PID" //NH 2>/dev/null | grep -q "$PID"; then
    [ ! -f "$BOT_HOME/state/active" ] && touch "$BOT_HOME/state/active"
    echo "Bot уже запущен (PID: $PID)"; exit 0
  fi
  rm -f "$BOT_HOME/state/bot.pid"
fi

# 4-5. Запуск + флаг
pythonw "$BOT_HOME/bot.py" &
touch "$BOT_HOME/state/active"
sleep 3

# 6-7. Проверка
if [ -f "$BOT_HOME/state/bot.pid" ] && tasklist //FI "PID eq $(cat $BOT_HOME/state/bot.pid)" //NH 2>/dev/null | grep -q "$(cat $BOT_HOME/state/bot.pid)"; then
  echo "Remote Bot запущен (PID: $(cat $BOT_HOME/state/bot.pid)). Tool-запросы вне permissions.allow пушатся в Telegram до /remotebotstop."
  echo "Лог: $BOT_HOME/logs/bot.log"
else
  echo "FAIL: bot не стартанул. Лог:"
  tail -20 "$BOT_HOME/logs/bot.log" 2>/dev/null
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
- **"ERROR: ..."** → fallback на обычный текстовый вопрос в чате

После `/remotebotstop` tool автоматически возвращает ERROR (state/active удалён) — переключение обратно на обычный flow самопроизвольное.

Это нужно потому что пользователь может быть не за компом — обычный текстовый вопрос ему не виден. MCP-tool пушит вопрос в Telegram, пользователь отвечает из TG, Claude получает ответ и продолжает сессию без перерыва.

Дополнительная подстраховка — Stop hook (`notify.py`) детектит знак вопроса в хвосте ответа Claude и при активном боте возвращает `decision: block`, заставляя Claude перевызвать через `ask`. Но опираться на это не стоит — следуй правилу сам.

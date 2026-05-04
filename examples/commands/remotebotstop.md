---
description: Остановить Claude Remote Bot — вернуться к локальным подтверждениям в Claude Desktop / Code
allowed-tools: Bash, Read
---

Останови Claude Remote Bot **одной Bash-командой**:

```bash
# Замени путь на свой каталог установки
cd /path/to/claude-remote-bot

# 1. Удалить флаг — сразу passthrough даже если bot ещё жив
[ -f state/active ] && rm -f state/active && echo "[1] state/active удалён" || echo "[1] state/active отсутствует"

# 2-3. PID + kill
if [ -f state/bot.pid ]; then
  PID=$(cat state/bot.pid)
  if taskkill //PID $PID //F 2>/dev/null; then
    echo "[2-3] killed PID $PID"
  else
    if tasklist //FI "PID eq $PID" //NH 2>/dev/null | grep -q "$PID"; then
      echo "[2-3] FAIL процесс не убит (нужны права?)"; exit 1
    fi
    echo "[2-3] PID $PID уже мёртв"
  fi
  rm -f state/bot.pid
  echo "[4] bot.pid удалён"
  echo
  echo "Remote Bot остановлен (PID был: $PID). Подтверждения снова обрабатываются локально в Claude Desktop / Code."
else
  echo "Bot уже остановлен"
fi
```

Если `taskkill` упал не из-за "процесса нет, но процесс жив" — попроси пользователя убить вручную через Диспетчер задач.

После остановки `state/active` удалён, `pending tool calls` пройдут обычным flow через Desktop UI prompt.

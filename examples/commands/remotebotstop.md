---
description: Stop Claude Remote Bot — return to local approvals in Claude Desktop / Code
allowed-tools: Bash, Read
---

Stop Claude Remote Bot:

```bash
# Replace with your install path
python /path/to/claude-remote-TGbot/manage.py stop
```

`manage.py` removes the `state/active` flag (passthrough kicks in
immediately) and terminates the bot process via `psutil`. Cross-platform
(Linux / macOS / Windows).

After stop, subsequent tool calls flow through to the regular Desktop UI
prompt.

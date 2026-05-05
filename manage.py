"""
Cross-platform CLI for managing the Claude Remote Bot daemon.

Usage:
    python manage.py start       # spawn bot.py in background, set state/active
    python manage.py stop        # kill the daemon, remove state/active
    python manage.py status      # report whether the bot is running

Designed so the slash commands /remotebotstart and /remotebotstop work
identically on Windows, Linux, and macOS — no `tasklist`, `taskkill`,
or `pythonw` shenanigans. Process detection / kill goes through psutil.

Exit codes:
    0  success / bot already in requested state
    1  failure (bad config, unable to spawn, etc.)
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
LOGS = ROOT / "logs"
ENV_FILE = ROOT / ".env"
REQUIREMENTS = ROOT / "requirements.txt"
BOT_SCRIPT = ROOT / "bot.py"
ACTIVE_FLAG = STATE / "active"
PID_FILE = STATE / "bot.pid"

STATE.mkdir(exist_ok=True)
LOGS.mkdir(exist_ok=True)


def _load_psutil():
    """Lazy import so a missing psutil yields a friendly error, not a traceback."""
    try:
        import psutil  # noqa: WPS433
        return psutil
    except ImportError:
        print(
            "ERROR: psutil is not installed. Run:\n"
            f"    pip install -r {REQUIREMENTS}",
            file=sys.stderr,
        )
        sys.exit(1)


def _is_running(pid: int) -> bool:
    """True if the given PID exists AND looks like our bot (psutil-backed)."""
    psutil = _load_psutil()
    try:
        proc = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    if not proc.is_running():
        return False
    if proc.status() == psutil.STATUS_ZOMBIE:
        return False
    # Heuristic: anything python-ish is good enough — we're not paranoid
    # about PID reuse here, the worst case is `stop` refuses to kill a
    # stranger's process, which is exactly what we want.
    name = (proc.name() or "").lower()
    if "python" not in name:
        # Some platforms report just the script name; check cmdline too
        try:
            cmdline = " ".join(proc.cmdline())
        except (psutil.AccessDenied, psutil.ZombieProcess):
            cmdline = ""
        if "python" not in cmdline.lower() and "bot.py" not in cmdline.lower():
            return False
    return True


def _read_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None


def _spawn_bot() -> int:
    """Start bot.py detached from this process. Returns the new PID."""
    # Cross-platform background launch:
    # - On Windows we use DETACHED_PROCESS so the child doesn't inherit
    #   the console and survives the parent's exit.
    # - On POSIX we use start_new_session=True (setsid) for the same.
    kwargs = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
        "cwd": str(ROOT),
    }
    if os.name == "nt":
        DETACHED_PROCESS = 0x00000008  # noqa: N806
        CREATE_NEW_PROCESS_GROUP = 0x00000200  # noqa: N806
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        kwargs["close_fds"] = True
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen([sys.executable, str(BOT_SCRIPT)], **kwargs)
    return proc.pid


def cmd_status() -> int:
    pid = _read_pid()
    active = ACTIVE_FLAG.exists()

    if pid is None:
        print("Bot is NOT running (no state/bot.pid).")
        if active:
            print("WARNING: state/active exists without a PID file.")
        return 0

    if _is_running(pid):
        print(f"Bot is RUNNING (PID: {pid}).")
        print(f"State: {'ACTIVE' if active else 'inactive (passthrough)'}")
        return 0

    print(f"Bot is NOT running (stale PID {pid}).")
    if active:
        print("WARNING: state/active exists; consider removing it.")
    return 0


def cmd_start() -> int:
    if not ENV_FILE.exists():
        print(
            f"ERROR: .env not found at {ENV_FILE}. "
            f"Copy .env.example and fill in TELEGRAM_BOT_TOKEN, "
            f"TELEGRAM_CHAT_ID, TELEGRAM_BOT_NAME.",
            file=sys.stderr,
        )
        return 1

    existing_pid = _read_pid()
    if existing_pid and _is_running(existing_pid):
        if not ACTIVE_FLAG.exists():
            ACTIVE_FLAG.touch()
        print(f"Bot already running (PID: {existing_pid}).")
        return 0

    # Stale pid file? Drop it.
    if PID_FILE.exists():
        PID_FILE.unlink()

    new_pid = _spawn_bot()
    ACTIVE_FLAG.touch()

    # bot.py writes its own PID file on startup; wait briefly for it
    deadline = time.time() + 5.0
    while time.time() < deadline:
        recorded = _read_pid()
        if recorded and _is_running(recorded):
            print(
                f"Remote Bot started (PID: {recorded}). "
                f"Tool requests outside permissions.allow will be pushed "
                f"to Telegram until /remotebotstop."
            )
            print(f"Log: {LOGS / 'bot.log'}")
            return 0
        time.sleep(0.2)

    print(
        f"FAIL: bot did not start within 5s (spawned as PID {new_pid}). "
        f"Check {LOGS / 'bot.log'}",
        file=sys.stderr,
    )
    return 1


def cmd_stop() -> int:
    psutil = _load_psutil()

    # Drop the active flag first — passthrough kicks in immediately even
    # if the process is still alive while we wait for it to exit.
    if ACTIVE_FLAG.exists():
        ACTIVE_FLAG.unlink()
        print("[1] state/active removed")
    else:
        print("[1] state/active already gone")

    pid = _read_pid()
    if pid is None:
        print("Bot already stopped (no PID file).")
        return 0

    if not _is_running(pid):
        print(f"[2-3] PID {pid} was already dead")
        PID_FILE.unlink()
        print("[4] bot.pid removed")
        return 0

    try:
        proc = psutil.Process(pid)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except psutil.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        print(f"[2-3] killed PID {pid}")
    except psutil.NoSuchProcess:
        print(f"[2-3] PID {pid} disappeared before kill")
    except psutil.AccessDenied:
        print(
            f"[2-3] FAIL — no permission to kill PID {pid} "
            f"(stop manually via Task Manager / kill -9)",
            file=sys.stderr,
        )
        return 1

    if PID_FILE.exists():
        PID_FILE.unlink()
        print("[4] bot.pid removed")

    print(
        f"\nRemote Bot stopped (was PID: {pid}). "
        f"Approvals are handled locally in Claude Desktop / Code again."
    )
    return 0


COMMANDS = {
    "start": cmd_start,
    "stop": cmd_stop,
    "status": cmd_status,
}


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1 or args[0] not in COMMANDS:
        print(
            "Usage: python manage.py {start|stop|status}",
            file=sys.stderr,
        )
        return 1
    return COMMANDS[args[0]]()


if __name__ == "__main__":
    sys.exit(main())

"""
Tests for manage.py — the cross-platform CLI.

Process spawning is hard to test portably (we'd need to actually fork a
child), so we focus on:
- main() argument dispatch
- cmd_status output for the no-PID / stale-PID cases
- cmd_start failure when .env is missing
- cmd_stop happy path with monkeypatched psutil

The status / start / stop helpers all rely on the module-level path
constants STATE / PID_FILE / ACTIVE_FLAG / ENV_FILE — we redirect those
to a temporary directory in each test.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import manage  # noqa: E402


@pytest.fixture
def tmp_env(monkeypatch, tmp_path):
    """Redirect manage.py path constants into tmp_path."""
    state = tmp_path / "state"
    logs = tmp_path / "logs"
    state.mkdir()
    logs.mkdir()
    env_file = tmp_path / ".env"
    pid_file = state / "bot.pid"
    active = state / "active"

    monkeypatch.setattr(manage, "STATE", state)
    monkeypatch.setattr(manage, "LOGS", logs)
    monkeypatch.setattr(manage, "ENV_FILE", env_file)
    monkeypatch.setattr(manage, "PID_FILE", pid_file)
    monkeypatch.setattr(manage, "ACTIVE_FLAG", active)
    return tmp_path


# ---------------------------------------------------------------------------
# main() dispatch
# ---------------------------------------------------------------------------

class TestMainDispatch:
    def test_no_args_returns_1(self, capsys):
        rc = manage.main([])
        assert rc == 1
        assert "Usage" in capsys.readouterr().err

    def test_unknown_command_returns_1(self, capsys):
        rc = manage.main(["unknown"])
        assert rc == 1
        assert "Usage" in capsys.readouterr().err

    def test_too_many_args_returns_1(self, capsys):
        rc = manage.main(["start", "extra"])
        assert rc == 1


# ---------------------------------------------------------------------------
# cmd_status
# ---------------------------------------------------------------------------

class TestStatus:
    def test_no_pid_file(self, tmp_env, capsys):
        rc = manage.cmd_status()
        assert rc == 0
        out = capsys.readouterr().out
        assert "NOT running" in out

    def test_no_pid_with_stale_active_flag_warns(self, tmp_env, capsys):
        manage.ACTIVE_FLAG.touch()
        rc = manage.cmd_status()
        assert rc == 0
        out = capsys.readouterr().out
        assert "WARNING" in out
        assert "without a PID file" in out

    def test_stale_pid_in_file(self, tmp_env, monkeypatch, capsys):
        manage.PID_FILE.write_text("999999")
        monkeypatch.setattr(manage, "_is_running", lambda pid: False)
        rc = manage.cmd_status()
        assert rc == 0
        out = capsys.readouterr().out
        assert "stale PID" in out

    def test_running_pid_reports_active(self, tmp_env, monkeypatch, capsys):
        manage.PID_FILE.write_text("12345")
        manage.ACTIVE_FLAG.touch()
        monkeypatch.setattr(manage, "_is_running", lambda pid: True)
        rc = manage.cmd_status()
        assert rc == 0
        out = capsys.readouterr().out
        assert "RUNNING" in out
        assert "12345" in out
        assert "ACTIVE" in out

    def test_running_pid_inactive_state(self, tmp_env, monkeypatch, capsys):
        manage.PID_FILE.write_text("12345")
        # No ACTIVE_FLAG
        monkeypatch.setattr(manage, "_is_running", lambda pid: True)
        manage.cmd_status()
        out = capsys.readouterr().out
        assert "RUNNING" in out
        assert "passthrough" in out


# ---------------------------------------------------------------------------
# cmd_start
# ---------------------------------------------------------------------------

class TestStart:
    def test_no_env_returns_1(self, tmp_env, capsys):
        rc = manage.cmd_start()
        assert rc == 1
        assert ".env not found" in capsys.readouterr().err

    def test_already_running_short_circuit(self, tmp_env, monkeypatch, capsys):
        manage.ENV_FILE.write_text("TOKEN=x")
        manage.PID_FILE.write_text("777")
        monkeypatch.setattr(manage, "_is_running", lambda pid: True)
        rc = manage.cmd_start()
        assert rc == 0
        out = capsys.readouterr().out
        assert "already running" in out
        assert "777" in out
        assert manage.ACTIVE_FLAG.exists()

    def test_clears_stale_pid_then_spawns(self, tmp_env, monkeypatch, capsys):
        manage.ENV_FILE.write_text("TOKEN=x")
        manage.PID_FILE.write_text("999999")  # stale

        # First _is_running call (existing pid) → False (stale)
        # After spawn we replicate what bot.py would do — write a fresh
        # PID file — and report it alive on the next check.
        running_state = {"alive": False}

        def fake_spawn():
            running_state["alive"] = True
            manage.PID_FILE.write_text("4242")
            return 4242

        monkeypatch.setattr(manage, "_spawn_bot", fake_spawn)
        monkeypatch.setattr(manage, "_is_running",
                            lambda pid: running_state["alive"] and pid == 4242)

        rc = manage.cmd_start()
        assert rc == 0
        out = capsys.readouterr().out
        assert "Remote Bot started" in out
        assert "4242" in out
        assert manage.ACTIVE_FLAG.exists()


# ---------------------------------------------------------------------------
# cmd_stop
# ---------------------------------------------------------------------------

class TestStop:
    def test_no_pid_file(self, tmp_env, capsys):
        rc = manage.cmd_stop()
        assert rc == 0
        out = capsys.readouterr().out
        assert "already stopped" in out

    def test_active_flag_removed(self, tmp_env, capsys):
        manage.ACTIVE_FLAG.touch()
        manage.cmd_stop()
        out = capsys.readouterr().out
        assert "state/active removed" in out
        assert not manage.ACTIVE_FLAG.exists()

    def test_dead_pid_cleanup(self, tmp_env, monkeypatch, capsys):
        manage.PID_FILE.write_text("999999")
        monkeypatch.setattr(manage, "_is_running", lambda pid: False)
        rc = manage.cmd_stop()
        assert rc == 0
        out = capsys.readouterr().out
        assert "already dead" in out
        assert not manage.PID_FILE.exists()

    def test_running_pid_terminated(self, tmp_env, monkeypatch, capsys):
        manage.PID_FILE.write_text("4242")
        monkeypatch.setattr(manage, "_is_running", lambda pid: True)

        # Fake the psutil module: the Process(pid) call returns a mock
        # whose terminate() / wait() succeed without exceptions.
        fake_proc = MagicMock()
        fake_psutil = MagicMock()
        fake_psutil.Process.return_value = fake_proc
        # Pre-define the exception classes so `except psutil.X` doesn't
        # trip over a non-Exception MagicMock.
        fake_psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
        fake_psutil.AccessDenied = type("AccessDenied", (Exception,), {})
        fake_psutil.TimeoutExpired = type("TimeoutExpired", (Exception,), {})
        monkeypatch.setattr(manage, "_load_psutil", lambda: fake_psutil)

        rc = manage.cmd_stop()
        assert rc == 0
        out = capsys.readouterr().out
        assert "killed PID 4242" in out
        assert "Remote Bot stopped" in out
        fake_proc.terminate.assert_called_once()

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
        fake_psutil = _make_fake_psutil(process=fake_proc)
        monkeypatch.setattr(manage, "_load_psutil", lambda: fake_psutil)

        rc = manage.cmd_stop()
        assert rc == 0
        out = capsys.readouterr().out
        assert "killed PID 4242" in out
        assert "Remote Bot stopped" in out
        fake_proc.terminate.assert_called_once()

    def test_terminate_timeout_falls_through_to_kill(self, tmp_env, monkeypatch, capsys):
        manage.PID_FILE.write_text("4242")
        monkeypatch.setattr(manage, "_is_running", lambda pid: True)

        fake_psutil = _make_fake_psutil()
        # First wait() raises TimeoutExpired → kill() then second wait()
        fake_psutil.Process.return_value.wait.side_effect = [
            fake_psutil.TimeoutExpired("boom"),
            None,
        ]
        monkeypatch.setattr(manage, "_load_psutil", lambda: fake_psutil)

        rc = manage.cmd_stop()
        assert rc == 0
        fake_psutil.Process.return_value.kill.assert_called_once()

    def test_no_such_process_during_kill(self, tmp_env, monkeypatch, capsys):
        manage.PID_FILE.write_text("4242")
        monkeypatch.setattr(manage, "_is_running", lambda pid: True)

        fake_psutil = _make_fake_psutil()
        fake_psutil.Process.side_effect = fake_psutil.NoSuchProcess("gone")
        monkeypatch.setattr(manage, "_load_psutil", lambda: fake_psutil)

        rc = manage.cmd_stop()
        assert rc == 0
        out = capsys.readouterr().out
        assert "disappeared" in out

    def test_access_denied_returns_1(self, tmp_env, monkeypatch, capsys):
        manage.PID_FILE.write_text("4242")
        monkeypatch.setattr(manage, "_is_running", lambda pid: True)

        fake_psutil = _make_fake_psutil()
        fake_psutil.Process.side_effect = fake_psutil.AccessDenied("nope")
        monkeypatch.setattr(manage, "_load_psutil", lambda: fake_psutil)

        rc = manage.cmd_stop()
        assert rc == 1
        err = capsys.readouterr().err
        assert "no permission" in err


# ---------------------------------------------------------------------------
# _is_running internal helper
# ---------------------------------------------------------------------------

class TestIsRunning:
    def test_no_such_process_returns_false(self, monkeypatch):
        fake_psutil = _make_fake_psutil()
        fake_psutil.Process.side_effect = fake_psutil.NoSuchProcess("x")
        monkeypatch.setattr(manage, "_load_psutil", lambda: fake_psutil)
        assert manage._is_running(123) is False

    def test_not_running_returns_false(self, monkeypatch):
        fake_psutil = _make_fake_psutil()
        fake_psutil.Process.return_value.is_running.return_value = False
        monkeypatch.setattr(manage, "_load_psutil", lambda: fake_psutil)
        assert manage._is_running(123) is False

    def test_zombie_returns_false(self, monkeypatch):
        fake_psutil = _make_fake_psutil()
        fake_psutil.Process.return_value.is_running.return_value = True
        fake_psutil.Process.return_value.status.return_value = "zombie"
        fake_psutil.STATUS_ZOMBIE = "zombie"
        monkeypatch.setattr(manage, "_load_psutil", lambda: fake_psutil)
        assert manage._is_running(123) is False

    def test_python_process_returns_true(self, monkeypatch):
        fake_psutil = _make_fake_psutil()
        proc = fake_psutil.Process.return_value
        proc.is_running.return_value = True
        proc.status.return_value = "running"
        proc.name.return_value = "python.exe"
        fake_psutil.STATUS_ZOMBIE = "zombie"
        monkeypatch.setattr(manage, "_load_psutil", lambda: fake_psutil)
        assert manage._is_running(123) is True

    def test_unknown_name_but_cmdline_matches_bot_py(self, monkeypatch):
        fake_psutil = _make_fake_psutil()
        proc = fake_psutil.Process.return_value
        proc.is_running.return_value = True
        proc.status.return_value = "running"
        proc.name.return_value = "myapp"
        proc.cmdline.return_value = ["/usr/bin/something", "/path/bot.py"]
        fake_psutil.STATUS_ZOMBIE = "zombie"
        monkeypatch.setattr(manage, "_load_psutil", lambda: fake_psutil)
        assert manage._is_running(123) is True

    def test_unknown_name_unknown_cmdline_returns_false(self, monkeypatch):
        fake_psutil = _make_fake_psutil()
        proc = fake_psutil.Process.return_value
        proc.is_running.return_value = True
        proc.status.return_value = "running"
        proc.name.return_value = "totally-different"
        proc.cmdline.return_value = ["/usr/bin/totally-different"]
        fake_psutil.STATUS_ZOMBIE = "zombie"
        monkeypatch.setattr(manage, "_load_psutil", lambda: fake_psutil)
        assert manage._is_running(123) is False


# ---------------------------------------------------------------------------
# _read_pid
# ---------------------------------------------------------------------------

class TestReadPid:
    def test_missing_file_returns_none(self, tmp_env):
        assert manage._read_pid() is None

    def test_malformed_content_returns_none(self, tmp_env):
        manage.PID_FILE.write_text("not-a-number")
        assert manage._read_pid() is None

    def test_valid_pid_returned_as_int(self, tmp_env):
        manage.PID_FILE.write_text("12345\n")
        assert manage._read_pid() == 12345


# ---------------------------------------------------------------------------
# cmd_start failure branches
# ---------------------------------------------------------------------------

class TestStartFailure:
    def test_spawn_timeout_returns_1(self, tmp_env, monkeypatch, capsys):
        """If bot.py never writes its PID file, cmd_start gives up after 5s."""
        manage.ENV_FILE.write_text("TOKEN=x")
        monkeypatch.setattr(manage, "_spawn_bot", lambda: 9999)
        monkeypatch.setattr(manage, "_is_running", lambda pid: False)
        # Skip the real sleep so the polling loop hits its deadline instantly
        monkeypatch.setattr(manage.time, "sleep", lambda *_a, **_k: None)
        # Make time.time() jump past the deadline on every call after the
        # first, so the loop body executes once then exits.
        ticks = iter([0.0, 0.0, 100.0, 100.0, 100.0, 100.0, 100.0])
        monkeypatch.setattr(manage.time, "time",
                            lambda: next(ticks, 1000.0))

        rc = manage.cmd_start()
        assert rc == 1
        err = capsys.readouterr().err
        assert "did not start" in err


# ---------------------------------------------------------------------------
# main() happy paths
# ---------------------------------------------------------------------------

class TestMainHappyPath:
    def test_main_status_dispatches(self, tmp_env, monkeypatch, capsys):
        rc = manage.main(["status"])
        # cmd_status returns 0 even when nothing is running
        assert rc == 0
        assert "NOT running" in capsys.readouterr().out


def _make_fake_psutil(*, process=None):
    """Build a MagicMock that quacks like the psutil module enough for tests."""
    fake = MagicMock()
    fake.Process.return_value = process if process is not None else MagicMock()
    fake.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
    fake.AccessDenied = type("AccessDenied", (Exception,), {})
    fake.ZombieProcess = type("ZombieProcess", (Exception,), {})
    fake.TimeoutExpired = type("TimeoutExpired", (Exception,), {})
    fake.STATUS_ZOMBIE = "zombie"
    return fake

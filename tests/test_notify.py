"""
Tests for notify.py covering:
- looks_like_question: heuristic that detects a question in Claude's
  response tail
- get_last_assistant_text: JSONL transcript parser
- format_message: Stop / Notification / default rendering for TG
- silent_exit / block_stop: stdout JSON contracts
- send_telegram_text: best-effort POST with mocked requests
- main: end-to-end paths with mocked stdin / requests / state

The notify.py module has import-time side effects (mkdir, load_dotenv),
but they are harmless on a CI runner — the file simply creates an
empty logs/ directory and silently no-ops on a missing .env.
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Make the package root importable when pytest is invoked from anywhere
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import notify  # noqa: E402
from notify import (  # noqa: E402
    block_stop,
    format_message,
    get_last_assistant_text,
    looks_like_question,
    send_telegram_text,
    silent_exit,
)

# ---------------------------------------------------------------------------
# looks_like_question
# ---------------------------------------------------------------------------

class TestLooksLikeQuestion:
    def test_empty_string(self):
        assert looks_like_question("") is False

    def test_none(self):
        assert looks_like_question(None) is False

    def test_plain_statement_no_question_mark(self):
        assert looks_like_question("Done. Everything pushed.") is False

    def test_trailing_question_mark(self):
        assert looks_like_question("Should we keep going?") is True

    def test_question_mid_tail(self):
        # Question mark not at the very end but inside the last 400 chars
        text = (
            "I thought about it and decided as follows: "
            "which option do we pick? Standing by for an answer."
        )
        assert looks_like_question(text) is True

    def test_question_outside_400_char_window(self):
        # 500 chars of plain prose where the only "?" is at position 0
        prefix = "Is this a question? "
        body = "And then a long narrative without any marks. " * 30
        assert len(prefix + body) > 400
        assert looks_like_question(prefix + body) is False

    def test_fenced_code_block_question_ignored(self):
        text = "Done.\n```python\nif x?:\n    pass\n```\n"
        assert looks_like_question(text) is False

    def test_inline_code_question_ignored(self):
        text = "Using a `x?y:z` ternary, all good."
        assert looks_like_question(text) is False

    def test_question_outside_code_still_caught(self):
        text = "```python\nif x?:\n    pass\n```\nWhich one do we pick?"
        assert looks_like_question(text) is True

    def test_no_false_positive_on_old_marker_word(self):
        # An earlier (now-removed) heuristic triggered on words like
        # "продолжить" inside narrative — make sure we don't regress.
        text = (
            "Fixed async + added flush. The settings.json is most "
            "likely cached for the session, so in this chat the block "
            "may still not fire. Close Claude Desktop completely and "
            "reopen — this chat will remain in history, you can resume "
            "right where you left off."
        )
        assert looks_like_question(text) is False

    def test_multiple_questions_returns_true(self):
        text = "Should we go? Or wait?"
        assert looks_like_question(text) is True

    def test_question_mark_inside_quoted_text(self):
        # We don't try to be clever — a quoted "?" still triggers. This
        # is documented as acceptable false-positive territory.
        text = 'He asked: "Now what?" — but I didn\'t reply.'
        assert looks_like_question(text) is True

    def test_only_whitespace(self):
        assert looks_like_question("   \n\t  ") is False


# ---------------------------------------------------------------------------
# get_last_assistant_text
# ---------------------------------------------------------------------------

class TestGetLastAssistantText:
    def test_missing_path_returns_empty(self):
        assert get_last_assistant_text("") == ""
        assert get_last_assistant_text("/nonexistent/path/transcript.jsonl") == ""

    def test_empty_file(self, tmp_path):
        p = tmp_path / "transcript.jsonl"
        p.write_text("", encoding="utf-8")
        assert get_last_assistant_text(str(p)) == ""

    def test_single_assistant_text_block(self, tmp_path):
        p = tmp_path / "transcript.jsonl"
        p.write_text(
            json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Hello!"}]},
            }) + "\n",
            encoding="utf-8",
        )
        assert get_last_assistant_text(str(p)) == "Hello!"

    def test_returns_last_when_multiple_assistant_messages(self, tmp_path):
        p = tmp_path / "transcript.jsonl"
        lines = [
            json.dumps({"type": "user", "message": {"content": "first question"}}),
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "first answer"},
            ]}}),
            json.dumps({"type": "user", "message": {"content": "another question"}}),
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "last answer"},
            ]}}),
        ]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        assert get_last_assistant_text(str(p)) == "last answer"

    def test_concatenates_multiple_text_blocks(self, tmp_path):
        p = tmp_path / "transcript.jsonl"
        p.write_text(
            json.dumps({
                "type": "assistant",
                "message": {"content": [
                    {"type": "text", "text": "part 1"},
                    {"type": "tool_use", "name": "Bash"},
                    {"type": "text", "text": "part 2"},
                ]},
            }) + "\n",
            encoding="utf-8",
        )
        result = get_last_assistant_text(str(p))
        assert "part 1" in result
        assert "part 2" in result

    def test_skips_malformed_json_lines(self, tmp_path):
        p = tmp_path / "transcript.jsonl"
        p.write_text(
            "not json at all\n"
            + json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "valid"},
            ]}}) + "\n"
            + "{broken json\n",
            encoding="utf-8",
        )
        assert get_last_assistant_text(str(p)) == "valid"

    def test_role_field_format(self, tmp_path):
        # Alternative shape: top-level role+content (not type+message)
        p = tmp_path / "transcript.jsonl"
        p.write_text(
            json.dumps({"role": "assistant", "content": "plain string content"})
            + "\n",
            encoding="utf-8",
        )
        assert get_last_assistant_text(str(p)) == "plain string content"

    def test_skips_user_messages(self, tmp_path):
        p = tmp_path / "transcript.jsonl"
        p.write_text(
            json.dumps({"type": "user", "message": {"content": "from user"}})
            + "\n",
            encoding="utf-8",
        )
        assert get_last_assistant_text(str(p)) == ""


# ---------------------------------------------------------------------------
# format_message
# ---------------------------------------------------------------------------

class TestFormatMessage:
    def test_stop_with_no_transcript(self):
        result = format_message("Stop", {"transcript_path": ""})
        assert "Claude finished responding" in result
        assert "Ready for your input" in result

    def test_stop_includes_last_assistant_preview(self, tmp_path):
        transcript = tmp_path / "t.jsonl"
        transcript.write_text(
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "the latest answer"},
            ]}}) + "\n",
            encoding="utf-8",
        )
        result = format_message("Stop", {"transcript_path": str(transcript)})
        assert "Claude finished responding" in result
        assert "the latest answer" in result

    def test_stop_html_escapes_preview(self, tmp_path):
        transcript = tmp_path / "t.jsonl"
        transcript.write_text(
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "<script>alert('x')</script>"},
            ]}}) + "\n",
            encoding="utf-8",
        )
        result = format_message("Stop", {"transcript_path": str(transcript)})
        assert "<script>" not in result
        assert "&lt;script&gt;" in result

    def test_stop_truncates_long_preview(self, tmp_path):
        transcript = tmp_path / "t.jsonl"
        long_text = "x" * 5000
        transcript.write_text(
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": long_text},
            ]}}) + "\n",
            encoding="utf-8",
        )
        result = format_message("Stop", {"transcript_path": str(transcript)})
        assert "..." in result
        assert len(result) < 5000

    def test_notification_renders_message(self):
        result = format_message("Notification", {"message": "needs attention"})
        assert "Claude needs attention" in result
        assert "needs attention" in result

    def test_notification_default_when_message_missing(self):
        result = format_message("Notification", {})
        assert "Claude requires attention" in result

    def test_notification_html_escape(self):
        result = format_message("Notification", {"message": "<b>danger</b>"})
        assert "<b>danger</b>" not in result.replace("Claude needs attention", "")
        assert "&lt;b&gt;danger&lt;/b&gt;" in result

    def test_default_branch_dumps_payload(self):
        result = format_message("UnknownEvent", {"foo": "bar"})
        assert "UnknownEvent" in result
        assert "foo" in result
        assert "bar" in result


# ---------------------------------------------------------------------------
# silent_exit / block_stop
# ---------------------------------------------------------------------------

class TestSilentExit:
    def test_prints_empty_json_and_exits_zero(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            silent_exit()
        assert excinfo.value.code == 0
        out = capsys.readouterr().out.strip()
        assert json.loads(out) == {}


class TestBlockStop:
    def test_prints_decision_block_and_exits_zero(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            block_stop("because reasons")
        assert excinfo.value.code == 0
        out = capsys.readouterr().out.strip()
        payload = json.loads(out)
        assert payload == {"decision": "block", "reason": "because reasons"}


# ---------------------------------------------------------------------------
# send_telegram_text
# ---------------------------------------------------------------------------

class TestSendTelegramText:
    def test_posts_to_telegram_with_html_parse_mode(self, monkeypatch):
        post_mock = MagicMock()
        monkeypatch.setattr(notify, "TOKEN", "fake-token")
        monkeypatch.setattr(notify, "CHAT_ID", "12345")
        monkeypatch.setattr(notify.requests, "post", post_mock)

        send_telegram_text("hello")

        assert post_mock.call_count == 1
        args, kwargs = post_mock.call_args
        assert "fake-token" in args[0]
        assert kwargs["json"]["chat_id"] == "12345"
        assert kwargs["json"]["text"] == "hello"
        assert kwargs["json"]["parse_mode"] == "HTML"

    def test_swallows_exception_silently(self, monkeypatch):
        def raise_exc(*a, **kw):
            raise RuntimeError("network down")
        monkeypatch.setattr(notify, "TOKEN", "x")
        monkeypatch.setattr(notify, "CHAT_ID", "y")
        monkeypatch.setattr(notify.requests, "post", raise_exc)
        # Should not propagate
        send_telegram_text("hello")


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

class TestMain:
    """End-to-end main() coverage with stdin / requests / state mocked."""

    def _setup_main(self, monkeypatch, tmp_path, *, event, payload, active=True,
                    token="t", chat_id="c"):
        """Common scaffolding: redirect STATE/LOGS to tmp, fake env, fake stdin."""
        state_dir = tmp_path / "state"
        logs_dir = tmp_path / "logs"
        state_dir.mkdir()
        logs_dir.mkdir()
        if active:
            (state_dir / "active").touch()
        monkeypatch.setattr(notify, "STATE", state_dir)
        monkeypatch.setattr(notify, "LOGS", logs_dir)
        monkeypatch.setattr(notify, "TOKEN", token)
        monkeypatch.setattr(notify, "CHAT_ID", chat_id)
        monkeypatch.setattr(sys, "argv", ["notify.py", event])
        monkeypatch.setattr(sys, "stdin", _StdinShim(json.dumps(payload)))

    def test_silent_exit_when_bot_inactive(self, monkeypatch, tmp_path, capsys):
        self._setup_main(monkeypatch, tmp_path, event="Stop",
                         payload={}, active=False)
        with pytest.raises(SystemExit) as ex:
            notify.main()
        assert ex.value.code == 0
        assert capsys.readouterr().out.strip() == "{}"

    def test_silent_exit_when_env_missing(self, monkeypatch, tmp_path, capsys):
        self._setup_main(monkeypatch, tmp_path, event="Stop",
                         payload={}, token="", chat_id="")
        with pytest.raises(SystemExit) as ex:
            notify.main()
        assert ex.value.code == 0
        assert capsys.readouterr().out.strip() == "{}"

    def test_blocks_stop_when_question_detected(self, monkeypatch, tmp_path, capsys):
        # Build a transcript whose last assistant message ends with "?"
        transcript = tmp_path / "t.jsonl"
        transcript.write_text(
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "should we proceed?"},
            ]}}) + "\n",
            encoding="utf-8",
        )
        post_mock = MagicMock()
        monkeypatch.setattr(notify.requests, "post", post_mock)
        self._setup_main(monkeypatch, tmp_path, event="Stop",
                         payload={"transcript_path": str(transcript)})

        with pytest.raises(SystemExit) as ex:
            notify.main()
        assert ex.value.code == 0
        out = json.loads(capsys.readouterr().out.strip())
        assert out["decision"] == "block"
        assert "ask" in out["reason"].lower()
        # The stop hook also fires the "Stop hook fired" notification first
        assert post_mock.call_count >= 1

    def test_does_not_block_when_already_blocked(self, monkeypatch, tmp_path, capsys):
        # stop_hook_active=True must short-circuit the block to avoid loops
        transcript = tmp_path / "t.jsonl"
        transcript.write_text(
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "do this?"},
            ]}}) + "\n",
            encoding="utf-8",
        )
        post_mock = MagicMock()
        monkeypatch.setattr(notify.requests, "post", post_mock)
        self._setup_main(monkeypatch, tmp_path, event="Stop",
                         payload={
                             "transcript_path": str(transcript),
                             "stop_hook_active": True,
                         })

        with pytest.raises(SystemExit) as ex:
            notify.main()
        assert ex.value.code == 0
        # No block decision — just the regular notification path
        out = capsys.readouterr().out.strip()
        assert out == "{}"

    def test_notification_event_sends_and_exits(self, monkeypatch, tmp_path, capsys):
        post_mock = MagicMock()
        monkeypatch.setattr(notify.requests, "post", post_mock)
        self._setup_main(monkeypatch, tmp_path, event="Notification",
                         payload={"message": "wake up"})

        with pytest.raises(SystemExit) as ex:
            notify.main()
        assert ex.value.code == 0
        assert post_mock.call_count == 1
        sent_text = post_mock.call_args.kwargs["json"]["text"]
        assert "wake up" in sent_text
        assert capsys.readouterr().out.strip() == "{}"

    def test_send_failure_is_swallowed(self, monkeypatch, tmp_path, capsys):
        # When the TG POST fails, main() must still exit cleanly with {}
        def raise_exc(*a, **kw):
            raise RuntimeError("network down")
        monkeypatch.setattr(notify.requests, "post", raise_exc)
        self._setup_main(monkeypatch, tmp_path, event="Notification",
                         payload={"message": "x"})

        with pytest.raises(SystemExit) as ex:
            notify.main()
        assert ex.value.code == 0
        assert capsys.readouterr().out.strip() == "{}"

    def test_malformed_stdin_results_in_silent_exit(self, monkeypatch, tmp_path, capsys):
        state_dir = tmp_path / "state"
        logs_dir = tmp_path / "logs"
        state_dir.mkdir()
        logs_dir.mkdir()
        (state_dir / "active").touch()
        monkeypatch.setattr(notify, "STATE", state_dir)
        monkeypatch.setattr(notify, "LOGS", logs_dir)
        monkeypatch.setattr(sys, "argv", ["notify.py", "Stop"])
        monkeypatch.setattr(sys, "stdin", _StdinShim("not-valid-json{"))

        with pytest.raises(SystemExit) as ex:
            notify.main()
        assert ex.value.code == 0
        assert capsys.readouterr().out.strip() == "{}"


class _StdinShim:
    """Minimal stdin replacement supporting .read() with a fixed payload."""

    def __init__(self, content: str):
        self._content = content

    def read(self, *_args, **_kwargs) -> str:
        return self._content

    def reconfigure(self, *_args, **_kwargs) -> None:
        # notify.py calls sys.stdin.reconfigure(...) at import time on real
        # stdin; if our shim is installed AFTER import there's nothing to
        # do, but the attribute must exist so any unforeseen re-import or
        # future code path doesn't crash.
        return None

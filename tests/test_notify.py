"""
Tests for notify.py — focused on the pure helpers that have no side
effects:
- looks_like_question: heuristic that detects a question in Claude's
  response tail
- get_last_assistant_text: parses a JSONL transcript produced by
  Claude Code

The notify.py module has import-time side effects (mkdir, load_dotenv),
but they are harmless on a CI runner — the file simply creates an empty
logs/ directory and silently no-ops on a missing .env.
"""
import json
import sys
from pathlib import Path

# Make the package root importable when pytest is invoked from anywhere
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from notify import looks_like_question, get_last_assistant_text  # noqa: E402


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

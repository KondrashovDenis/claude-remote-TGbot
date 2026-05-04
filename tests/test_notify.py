"""
Tests for notify.py — focus on the pure helpers that have no side effects:
- looks_like_question: heuristic for detecting a question in Claude's tail
- get_last_assistant_text: parses a transcript JSONL produced by Claude Code

The notify.py module has import-time side effects (mkdir, load_dotenv), but
those are harmless on a CI runner — the file just creates an empty logs/
directory and silently no-ops on the missing .env.
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
        assert looks_like_question("Готово, всё запушено.") is False

    def test_trailing_question_mark(self):
        assert looks_like_question("Делать дальше?") is True

    def test_question_mid_tail(self):
        # Question mark not at the very end but inside the last 400 chars
        text = "Я подумал и решил вот что: какой вариант возьмём? Готов ждать ответа."
        assert looks_like_question(text) is True

    def test_question_outside_400_char_window(self):
        # 500 chars of plain prose where the only "?" is at position 0
        prefix = "Это вопрос? "
        body = "А дальше длинный нарратив без знаков. " * 30
        assert len(prefix + body) > 400
        assert looks_like_question(prefix + body) is False

    def test_fenced_code_block_question_ignored(self):
        text = "Готово.\n```python\nif x?:\n    pass\n```\n"
        assert looks_like_question(text) is False

    def test_inline_code_question_ignored(self):
        text = "Использую `x?y:z` тернарник, всё ок."
        assert looks_like_question(text) is False

    def test_question_outside_code_still_caught(self):
        text = "```python\nif x?:\n    pass\n```\nКакой выбираем?"
        assert looks_like_question(text) is True

    def test_no_false_positive_on_old_marker_word(self):
        # Earlier heuristic (now removed) triggered on words like "продолжить"
        # in narrative — make sure we no longer regress.
        text = (
            "Поправил async + добавил flush. Скорее всего settings.json "
            "кешируется на сессию, поэтому в этом чате блокировка может всё "
            "ещё не сработать. Закрой Claude Desktop полностью и открой "
            "снова — этот чат останется в истории, можно продолжить с того "
            "же места."
        )
        assert looks_like_question(text) is False

    def test_multiple_questions_returns_true(self):
        text = "Делать? Или подождать?"
        assert looks_like_question(text) is True

    def test_question_mark_inside_quoted_text(self):
        # We don't try to be too clever — quoted "?" still triggers. This
        # is documented as acceptable false-positive territory.
        text = 'Он спросил: «Что делать?» — но я не ответил.'
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
                "message": {"content": [{"type": "text", "text": "Привет!"}]},
            }) + "\n",
            encoding="utf-8",
        )
        assert get_last_assistant_text(str(p)) == "Привет!"

    def test_returns_last_when_multiple_assistant_messages(self, tmp_path):
        p = tmp_path / "transcript.jsonl"
        lines = [
            json.dumps({"type": "user", "message": {"content": "вопрос"}}),
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "первый ответ"},
            ]}}),
            json.dumps({"type": "user", "message": {"content": "ещё вопрос"}}),
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "последний ответ"},
            ]}}),
        ]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        assert get_last_assistant_text(str(p)) == "последний ответ"

    def test_concatenates_multiple_text_blocks(self, tmp_path):
        p = tmp_path / "transcript.jsonl"
        p.write_text(
            json.dumps({
                "type": "assistant",
                "message": {"content": [
                    {"type": "text", "text": "часть 1"},
                    {"type": "tool_use", "name": "Bash"},
                    {"type": "text", "text": "часть 2"},
                ]},
            }) + "\n",
            encoding="utf-8",
        )
        result = get_last_assistant_text(str(p))
        assert "часть 1" in result
        assert "часть 2" in result

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
        # Alternative format: top-level role+content (not type+message)
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
            json.dumps({"type": "user", "message": {"content": "из user"}})
            + "\n",
            encoding="utf-8",
        )
        assert get_last_assistant_text(str(p)) == ""

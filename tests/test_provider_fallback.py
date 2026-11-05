import json
import subprocess
from types import SimpleNamespace

import pytest

from listener.claude_inspect import ClaudeInspector, FallbackInspector, unfence
from listener.intent_validation import json_text, validated_intent
from listener.provider_errors import ProviderError

ACTION = ('{"reply": "Понял", "actions": [{"type": "enqueue_products", "category": "духовки настольные", '
          '"count": null, "quotas": {"homeline": null}, "source": null, "selection": "choose", "at": null}]}')


@pytest.mark.parametrize("raw", [
    ACTION,
    "```json\n" + ACTION + "\n```",
    "Nothing conflicting — proceeding.\n\n```json\n" + ACTION + "\n```",
    "Готово:\n" + ACTION,
])
def test_intent_in_fence_or_after_prose_is_accepted(raw):
    intent = validated_intent(raw)
    assert intent["actions"][0]["category"] == "духовки настольные"


@pytest.mark.parametrize("raw", ["просто текст", "```json\n{\"reply\": \n```", ACTION + "\nи ещё " + ACTION])
def test_garbage_or_two_objects_still_rejected(raw):
    with pytest.raises(ValueError):
        validated_intent(raw)


def test_json_text_keeps_plain_object():
    assert json_text("  " + ACTION + " ") == ACTION


class Fake:
    def __init__(self, name, outcomes):
        self.name, self.outcomes, self.calls = name, list(outcomes), 0

    def ready(self):
        return True

    def inspect(self, instructions, content, *, search=False, domains=()):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return {"text": outcome, "sources": []}


@pytest.fixture(autouse=True)
def clean_state():
    FallbackInspector._until.clear()
    FallbackInspector._errors.clear()


def test_chatgpt_limit_switches_to_claude_and_remembers_pause():
    now = [1000.0]
    gpt = Fake("chatgpt", [ProviderError("chatgpt", "usage_limit", "лимит",
                                         code="subscription_sharing_usage_limit_exceeded")])
    claude = Fake("claude", ["ответ claude", "второй"])
    box = FallbackInspector([("chatgpt", gpt), ("claude", claude)], clock=lambda: now[0])
    assert box.inspect("i", [{"type": "input_text", "text": "x"}])["text"] == "ответ claude"
    # Пока ChatGPT на паузе, его не дёргаем (новый экземпляр — как новое задание аудитора).
    box2 = FallbackInspector([("chatgpt", gpt), ("claude", claude)], clock=lambda: now[0])
    assert box2.inspect("i", [{"type": "input_text", "text": "x"}])["text"] == "второй"
    assert gpt.calls == 1


def test_claude_limit_falls_back_to_chatgpt_when_claude_is_first():
    claude = Fake("claude", [ProviderError("claude", "usage_limit", "лимит")])
    gpt = Fake("chatgpt", ["ответ gpt"])
    box = FallbackInspector([("claude", claude), ("chatgpt", gpt)], clock=lambda: 0)
    assert box.inspect("i", [{"type": "input_text", "text": "x"}])["text"] == "ответ gpt"


def test_both_down_reports_temporary_wait_not_configuration():
    gpt = Fake("chatgpt", [ProviderError("chatgpt", "usage_limit", "лимит", retry_at=5000,
                                         code="subscription_sharing_usage_limit_exceeded")])
    claude = Fake("claude", [ProviderError("claude", "configuration", "нет claude")])
    box = FallbackInspector([("chatgpt", gpt), ("claude", claude)], clock=lambda: 1000)
    with pytest.raises(ProviderError) as caught:
        box.inspect("i", [{"type": "input_text", "text": "x"}])
    error = caught.value
    assert (error.provider, error.kind, error.code) == ("chatgpt", "usage_limit",
                                                        "subscription_sharing_usage_limit_exceeded")
    assert error.retry_at == 1000 + 900          # повтор — когда освободится первый из двух (Claude)


def test_content_error_is_not_masked_by_switch():
    gpt = Fake("chatgpt", [ProviderError("chatgpt", "invalid_response", "брак")])
    claude = Fake("claude", ["не должен вызываться"])
    box = FallbackInspector([("chatgpt", gpt), ("claude", claude)], clock=lambda: 0)
    with pytest.raises(ProviderError):
        box.inspect("i", [{"type": "input_text", "text": "x"}])
    assert claude.calls == 0


def _stream(*events):
    return "\n".join(json.dumps(e, ensure_ascii=False) for e in events)


def test_claude_inspector_writes_images_and_collects_sources(tmp_path):
    seen = {}

    def runner(cmd, cwd, timeout, **kwargs):
        prompt = cmd[cmd.index("-p") + 1]
        task = next(p for p in prompt.split() if p.endswith("task.md"))
        body = (tmp_path / task).read_text(encoding="utf-8")
        seen["body"], seen["cmd"] = body, cmd
        image = body.split("[ИЗОБРАЖЕНИЕ 1: ")[1].split("]")[0]
        seen["image"] = (tmp_path / image).read_bytes()
        return SimpleNamespace(returncode=0, stderr="", stdout=_stream(
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "WebFetch", "input": {"url": "https://maker.example/p/1"}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "content": "Links: https://shop.example/x"}]}},
            {"type": "result", "is_error": False, "result": "```json\n{\"sources\": []}\n```"}))

    data = "aGVsbG8="  # b"hello"
    result = ClaudeInspector(tmp_path, runner=runner).inspect(
        "Найди фото", [{"type": "input_text", "text": "GENERATED"},
                       {"type": "input_image", "image_url": "data:image/jpeg;base64," + data}],
        search=True, domains=["maker.example"])
    assert result == {"text": '{"sources": []}', "sources": ["https://maker.example/p/1", "https://shop.example/x"]}
    assert seen["image"] == b"hello" and "Найди фото" in seen["body"]
    assert "Bash" in seen["cmd"][seen["cmd"].index("--disallowedTools") + 1]
    assert not list((tmp_path / "runs/photo-auditor/claude-inspect").iterdir())   # временные файлы убраны


def test_claude_inspector_limit_is_usage_limit(tmp_path):
    runner = lambda cmd, **kw: SimpleNamespace(returncode=1, stderr="", stdout=_stream(
        {"type": "result", "is_error": True, "result": "You've hit your limit · resets 3am"}))
    with pytest.raises(ProviderError) as caught:
        ClaudeInspector(tmp_path, runner=runner).inspect("i", [{"type": "input_text", "text": "x"}])
    assert caught.value.kind == "usage_limit"


def test_claude_inspector_timeout(tmp_path):
    def runner(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)
    with pytest.raises(ProviderError) as caught:
        ClaudeInspector(tmp_path, runner=runner).inspect("i", [{"type": "input_text", "text": "x"}])
    assert caught.value.kind == "timeout"


def test_unfence():
    assert unfence("```json\n{}\n```") == "{}"
    assert unfence(" ok ") == "ok"

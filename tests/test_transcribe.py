import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from orchestrator_bot.factory import Factory, FactoryError
from orchestrator_bot.transcribe import Transcriber, decode_ogg


class FakeModel:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        return [SimpleNamespace(text=" поставь"), SimpleNamespace(text="стабилизаторы ")], None


def test_transcriber_passes_hint_and_language(monkeypatch):
    model = FakeModel()
    monkeypatch.setattr("orchestrator_bot.transcribe.decode_ogg", lambda data: [0.0])
    t = Transcriber(model_factory=lambda size: model)
    assert t(b"OGG", "Бренды: Ресанта.") == "поставь стабилизаторы"
    assert model.calls[0]["initial_prompt"] == "Бренды: Ресанта."
    assert model.calls[0]["language"] == "ru"
    assert model.calls[0]["vad_filter"] is True


def test_transcriber_loads_model_once(monkeypatch):
    created = []
    monkeypatch.setattr("orchestrator_bot.transcribe.decode_ogg", lambda data: [0.0])
    t = Transcriber(model_factory=lambda size: created.append(size) or FakeModel())
    t(b"1", "")
    t(b"2", "")
    assert created == ["small"]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="нет ffmpeg")
def test_decode_ogg_uses_ffmpeg():
    ogg = subprocess.run(["ffmpeg", "-v", "quiet", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
                          "-t", "1", "-c:a", "libopus", "-f", "ogg", "-"], capture_output=True, check=True).stdout
    audio = decode_ogg(ogg)
    assert 15000 <= len(audio) <= 17000


def _runner(record, stdout='{"accepted": ["k1"]}', code=0):
    def run(args, **kwargs):
        record.append((args, kwargs))
        return SimpleNamespace(returncode=code, stdout=stdout, stderr="boom")
    return run


def test_factory_builds_cli_args_and_parses_json():
    record = []
    f = Factory("/venv/python", "/opt/cf", runner=_runner(record))
    action = {"type": "enqueue_products", "category": "стабилизаторы", "count": None,
              "quotas": {"ресанта": None}, "source": None, "at": "12:00", "day": None}
    assert f.enqueue(action) == {"accepted": ["k1"]}
    args, kwargs = record[0]
    assert args[:4] == ["/venv/python", "-m", "orchestrator_bot.catalog_cli", "enqueue"]
    assert args[args.index("--category") + 1] == "стабилизаторы"
    assert "--due" not in args          # время — выход партии, генерация сразу
    assert json.loads(args[args.index("--quotas") + 1]) == {"ресанта": None}
    assert "--count" not in args
    assert kwargs["cwd"] == "/opt/cf"


def test_factory_raises_on_nonzero_exit():
    f = Factory("/venv/python", "/opt/cf", runner=_runner([], stdout='{"error": "x"}', code=2))
    with pytest.raises(FactoryError):
        f.hint()


def test_factory_sets_pythonpath_to_src():
    record = []
    Factory("/venv/python", "/opt/cf", runner=_runner(record)).status(0)
    assert record[0][1]["env"]["PYTHONPATH"] == "/opt/cf/src"


def test_factory_hint_cached_for_ttl():
    record, now = [], [1000.0]
    f = Factory("/venv/python", "/opt/cf", runner=_runner(record, stdout='{"brands": []}'), clock=lambda: now[0])
    f.hint()
    f.hint()
    assert len(record) == 1
    now[0] += 601
    f.hint()
    assert len(record) == 2


def test_factory_workers_and_start_commands():
    record = []
    f = Factory("/venv/python", "/opt/cf", runner=_runner(record, stdout='{"generation": true}'))
    f.workers()
    f.start_workers()
    assert [r[0][3] for r in record] == ["workers", "start-workers"]


def test_collapse_repeats_removes_whisper_loop():
    from orchestrator_bot.transcribe import collapse_repeats
    assert collapse_repeats("Поставщика Брис, LG, RULE, RULE, RULE, RULE, RULE,") == "Поставщика Брис, LG, RULE, RULE,"
    assert collapse_repeats("да да нормально") == "да да нормально"


class SequencedModel:
    def __init__(self, *outputs):
        self.outputs = outputs
        self.calls = []
        self.audio = []

    def transcribe(self, audio, **kwargs):
        self.audio.append(audio)
        self.calls.append(kwargs)
        return [SimpleNamespace(text=self.outputs[len(self.calls) - 1])], None


def test_product_hint_echo_retries_same_audio_without_prompt_or_vad(monkeypatch):
    decoded = [0.1, -0.2, 0.05]
    monkeypatch.setattr('orchestrator_bot.transcribe.decode_ogg', lambda data: decoded)
    model = SequencedModel('Белый, 255л, 255л, 7ящиков',
                           'На фото морозильный ларь, а должна быть вертикальная морозильная камера')
    t = Transcriber(model_factory=lambda _: model)
    text = t(b'OGG', 'Beko. Вертикальная морозильная камера, белый, 255 л, 7 ящиков')
    assert text == 'На фото морозильный ларь, а должна быть вертикальная морозильная камера'
    assert len(model.calls) == 2 and model.audio == [decoded, decoded]
    assert model.calls[0]['vad_filter'] is True
    assert model.calls[1]['vad_filter'] is False
    assert model.calls[1]['initial_prompt'] is None
    assert model.calls[1]['condition_on_previous_text'] is False


def test_prompt_echo_is_not_presented_as_owner_speech_when_retry_is_still_bad(monkeypatch):
    monkeypatch.setattr('orchestrator_bot.transcribe.decode_ogg', lambda _: [0.2])
    model = SequencedModel('Белый 255л 7ящиков', 'Белый 255л 7ящиков')
    t = Transcriber(model_factory=lambda _: model)
    assert t(b'OGG', 'Beko белый, 255 л, 7 ящиков') == ''
    assert len(model.calls) == 2


@pytest.mark.parametrize('first', ['', 'RULE RULE RULE RULE RULE RULE', 'RULE,RULE,RULE',
                                  'Убери бензин Убери бензин Убери бензин'])
def test_blank_or_looped_pass_with_voiced_audio_gets_one_unbiased_retry(monkeypatch, first):
    monkeypatch.setattr('orchestrator_bot.transcribe.decode_ogg', lambda _: [0.2])
    model = SequencedModel(first, 'Убери неверный тип товара')
    t = Transcriber(model_factory=lambda _: model)
    assert t(b'OGG', 'Бренды: Beko') == 'Убери неверный тип товара'
    assert len(model.calls) == 2 and model.calls[1]['vad_filter'] is False


def test_silence_does_not_trigger_unprompted_hallucination(monkeypatch):
    monkeypatch.setattr('orchestrator_bot.transcribe.decode_ogg', lambda _: [0.0] * 100)
    model = SequencedModel('')
    assert Transcriber(model_factory=lambda _: model)(b'OGG', 'Бренды: Beko') == ''
    assert len(model.calls) == 1


def test_normal_brand_and_correction_speech_stays_on_one_pass(monkeypatch):
    monkeypatch.setattr('orchestrator_bot.transcribe.decode_ogg', lambda _: [0.2])
    model = SequencedModel('Ресанта', 'Убери бензин, генератор дизельный')
    t = Transcriber(model_factory=lambda _: model)
    assert t(b'brand', 'Бренды: Ресанта, Carver') == 'Ресанта'
    assert t(b'correction', 'CARVER PPG-5000D. Генератор бензиновый') == 'Убери бензин, генератор дизельный'
    assert all(call['vad_filter'] is True for call in model.calls)

import base64
import json
import threading
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from orchestrator_bot.api import make_server
from orchestrator_bot.store import Store
from orchestrator_bot.photo_reviews import create_feedback, get_card
from tests.test_photo_reviews import payload


@pytest.fixture
def server(tmp_path):
    store = Store(tmp_path / 'orch.db')
    corrections, voices = [], []
    def correct(value):
        corrections.append(value)
        return {'handled': True, 'job_id': 124}
    def transcribe(audio, hint):
        voices.append((audio, hint))
        return 'Это дизельный генератор, убери упоминание бензина'
    srv = make_server(store, SimpleNamespace(correct_photo=correct), lambda *a: None, 'test', 0,
                      on_transcribe=transcribe)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield store, corrections, voices, f'http://127.0.0.1:{srv.server_address[1]}'
    srv.shutdown()
    srv.server_close()
    thread.join(2)


def post(base, route, body, token='test'):
    request = Request(base + route, data=json.dumps(body).encode(),
                      headers={'X-Orch-Token': token, 'Content-Type': 'application/json'})
    with urlopen(request, timeout=5) as response:
        return json.load(response)


def test_photo_review_correction_and_speech_wire_contract(server):
    store, calls, voices, base = server
    assert post(base, '/photo-reviews', payload())['registered']
    assert calls == []
    transcript = post(base, '/photo-feedback-transcribe',
                      {'audio': base64.b64encode(b'voice').decode(), 'hint': 'CARVER PPG-11000 DVR'})
    assert voices == [(b'voice', 'CARVER PPG-11000 DVR')]
    request = create_feedback(get_card(store, payload()['audit_id']), transcript['text'], 42, 'a' * 32)
    assert post(base, '/photo-feedback', request)['job_id'] == 124
    assert calls == [request]


@pytest.mark.parametrize('route,body', [('/photo-reviews', payload()),
                                      ('/photo-feedback-transcribe', {'audio': 'dm9pY2U='}),
                                      ('/photo-feedback', {})])
def test_private_feedback_routes_require_authentication(server, route, body):
    _, calls, voices, base = server
    with pytest.raises(HTTPError) as error:
        post(base, route, body, token='wrong')
    assert error.value.code == 401 and calls == voices == []


def test_invalid_audio_never_reaches_recognizer(server):
    _, _, voices, base = server
    with pytest.raises(HTTPError) as error:
        post(base, '/photo-feedback-transcribe', {'audio': 'not-base64'})
    assert error.value.code == 400 and not voices

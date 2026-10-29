"""Cross bot/HTTP/database tests: only speech and paid rendering are substituted."""
import json
import sqlite3
import threading
import hashlib
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from listener.client import ApiClient
from orchestrator_bot import bot, photo_correction, photo_feedback_ui, photo_reviews
from orchestrator_bot.api import make_server
from orchestrator_bot.store import Store
from photo_auditor import owner_feedback, review_sync, runtime, service
from photo_auditor.store import AuditStore
from tests.test_photo_auditor import picture, report


OWNER = 1264067528
BRAND, MODEL = 'CARVER', 'PPG-11000 DVR'
KEY = 'supplier|carver|full-diesel-product-name'
NOTE = 'Это дизельный генератор. Убери упоминание бензина.'


class Telegram:
    """Local message transport only; bot handlers and payload creation stay real."""
    def __init__(self):
        self.events = []

    def send(self, chat, text, keyboard=None, reply_keyboard=None):
        self.events.append(('send', chat, text, keyboard))
        return len(self.events)

    def send_photo(self, chat, path, caption='', keyboard=None):
        # Unlike older UI tests, require a real mirrored exact image on disk.
        self.events.append(('photo', chat, Path(path).read_bytes(), keyboard))
        return len(self.events)

    def answer_callback(self, *args):
        pass

    def edit(self, chat, mid, text, keyboard=None):
        self.events.append(('edit', chat, text, keyboard))

    def clear_buttons(self, *args):
        pass

    def download_voice(self, file_id):
        assert file_id == 'local-voice'
        return b'local-speech-fixture'


class Pipeline:
    def __init__(self, root):
        self.root, self.queue, self.state = root, root / 'queue.db', root / 'factory.db'
        self.output = root / 'output'
        self.output.mkdir()
        self.original, self.previous = picture('blue'), picture('red')
        (self.output / 'old-card.png').write_bytes(self.previous)
        self.submit_attempts, self.actual_jobs, self.speeches = [], {}, []
        self.lose_first_reply = False
        with sqlite3.connect(self.queue) as db:
            db.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY,mode TEXT,brand TEXT,model TEXT,status TEXT,'
                       'specs TEXT,output_filename TEXT,input_filename TEXT,archived_filename TEXT,'
                       'failed_filename TEXT,error_text TEXT,request_key TEXT UNIQUE)')
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (123, 'kbt', BRAND, MODEL, 'done', 'Исходное задание и цена из прайса.',
                        'old-card.png', 'cleaned.png', 'cleaned.png', None, None, 'original'))
        with sqlite3.connect(self.state) as db:
            db.execute('CREATE TABLE excel_items(key TEXT PRIMARY KEY,brand TEXT,model TEXT,status TEXT,'
                       'card_job INTEGER,research_job INTEGER,card_request_key TEXT,error TEXT,tries INTEGER,ts REAL)')
            db.execute('INSERT INTO excel_items VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (KEY, BRAND, MODEL, 'preview', 123, None, 'original', None, 0, 1))
            db.execute('CREATE TABLE awaiting(key TEXT PRIMARY KEY,status TEXT)')
            db.execute('INSERT INTO awaiting VALUES(?,?)', (KEY, 'pending'))

    def hint(self):
        return {}

    def photo_review_key(self, job_id):
        with sqlite3.connect(self.state) as db:
            row = db.execute('SELECT key FROM excel_items WHERE card_job=?', (job_id,)).fetchone()
        return row[0] if row else None

    def submit(self, brand, model, specs, source, mode, *, request_key):
        """Replace the actual paid submitter at its final side-effect boundary."""
        data = Path(source).read_bytes()
        self.submit_attempts.append(request_key)
        if request_key in self.actual_jobs:
            return self.actual_jobs[request_key]
        assert (brand, model, mode, data) == (BRAND, MODEL, 'kbt', self.original)
        with sqlite3.connect(self.queue) as db:
            job_id = db.execute('INSERT INTO jobs(mode,brand,model,status,specs,request_key) VALUES(?,?,?,?,?,?)',
                                (mode, brand, model, 'pending', specs, request_key)).lastrowid
        self.actual_jobs[request_key] = job_id
        if self.lose_first_reply:
            self.lose_first_reply = False
            raise TimeoutError('accepted reply was lost')
        return job_id

    def correct_photo(self, value):
        # Production CLI performs these same operations. Keep the complete
        # correction implementation, source selection, SQL/CAS and receipts real.
        original = photo_correction.mirrored_supplier(self.reviews.path, value)
        return photo_correction.correct_photo(self.state, self.queue, value, submit_card=self.submit,
                                              output_dir=self.output, verified_supplier=original,
                                              submit_standalone=getattr(self, 'raw_submit', None),
                                              preferred_reference=getattr(self, 'preferred_reference', None),
                                              preferred_reference_note=getattr(self, 'preferred_note', ''))

    def transcribe(self, data, hint):
        self.speeches.append((data, hint))
        assert data == b'local-speech-fixture'
        return NOTE

    def check_result(self, expected_note=NOTE):
        assert len(self.actual_jobs) == 1
        with sqlite3.connect(self.queue) as db:
            job = db.execute('SELECT id,brand,model,specs FROM jobs WHERE id>123').fetchone()
        assert job[1:3] == (BRAND, MODEL)
        assert job[3].startswith('OWNER_CORRECTION_REQUEST_ID=')
        assert expected_note in job[3] and 'Исходное задание и цена из прайса.' in job[3]
        assert self.audit['id'] in job[3] and self.audit['sha256'] in job[3]
        assert 'Корпус отличается' in job[3]  # previous auditor context reached renderer
        with sqlite3.connect(self.state) as db:
            assert db.execute('SELECT status,card_job FROM excel_items').fetchone() == ('card', job[0])
            assert db.execute('SELECT status FROM awaiting').fetchone()[0] == 'correcting'
        return job


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, 'load', lambda _: {'api_token': 'local-signature-key'})
    p = Pipeline(tmp_path)
    runtime.register_job(tmp_path, 123, 'kbt', BRAND, MODEL, 'Исходное задание и цена из прайса.')
    p.audits = AuditStore(tmp_path / 'runs/photo-auditor')
    product = dict(product_id='fotogen-123', revision=0, brand=BRAND, model=MODEL)
    submitted = p.audits.submit(product, p.previous, p.original)
    p.audits.finish(p.audits.claim(audit_id=submitted['id']), report('review'))
    p.audit = p.audits.get(submitted['id'])
    p.reviews = Store(tmp_path / 'orchestrator.db')
    p.tg = Telegram()
    server = make_server(p.reviews, p, lambda *a: None, 'local-http-key', 0, on_transcribe=p.transcribe)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    p.base = f'http://127.0.0.1:{server.server_address[1]}'
    monkeypatch.setenv('ORCH_API_TOKEN', 'local-http-key')
    monkeypatch.setenv('ORCH_API_URL', p.base)
    with httpx.Client(timeout=5, trust_env=False) as http:
        p.api = ApiClient(p.base, 'local-http-key', http=http)
        # Real cross-process serialization and authenticated review registration.
        assert review_sync.sync(p.audits) == 1
        mirrored = photo_reviews.get_card(p.reviews, p.audit['id'])
        assert mirrored['item_key'] == KEY
        assert Path(mirrored['supplier_path']).read_bytes() == p.original
        yield p
    server.shutdown()
    server.server_close()
    thread.join(2)


def callback(data):
    return {'callback_query': {'id': 'test-callback', 'data': data, 'from': {'id': OWNER},
                              'message': {'message_id': 7, 'chat': {'id': OWNER}}}}


def message(*, voice=False):
    return {'message': {'message_id': 10, 'from': {'id': OWNER}, 'chat': {'id': OWNER},
                        **({'voice': {'file_id': 'local-voice', 'duration': 5}} if voice else {'text': NOTE})}}


def orchestrator_deps(p):
    # Both bots share the real server recognizer route, just like production.
    def speech(data, hint):
        import base64
        return p.api._post('/photo-feedback-transcribe',
                           {'audio': base64.b64encode(data).decode(), 'hint': hint})['text']
    return bot.Deps(store=p.reviews, factory=p, tg=p.tg, transcriber=speech,
                    owner_chat_id=OWNER)


@pytest.mark.parametrize('use_voice', [False, True])
def test_orchestrator_exact_card_note_reaches_real_correction_once(pipeline, use_voice):
    p = pipeline
    deps = orchestrator_deps(p)
    bot.handle_update(callback('pf:edit:' + p.audit['id']), deps)
    assert p.tg.events[0][0] == 'photo' and p.tg.events[0][2] == p.previous
    bot.handle_update(message(voice=use_voice), deps)
    assert not p.actual_jobs  # note/transcription alone cannot purchase rendering
    request_id = deps.store.get_meta(photo_feedback_ui.PENDING)
    bot.handle_update(callback('pf:send:' + request_id), deps)
    p.check_result()
    bot.handle_update(callback('pf:send:' + request_id), deps)
    assert len(p.submit_attempts) == 1
    assert len(p.speeches) == int(use_voice)


@pytest.mark.parametrize('use_voice', [False, True])
def test_auditor_note_passes_real_authenticated_api_and_corrects_once(pipeline, use_voice):
    p = pipeline
    service.handle_update(callback('qf:begin:' + p.audit['id']), p.audits, p.tg, OWNER)
    service.handle_update(message(voice=use_voice), p.audits, p.tg, OWNER)
    draft = owner_feedback.pending(p.audits, OWNER)
    assert draft and NOTE == draft['notes'] and not p.actual_jobs
    service.handle_update(callback('qf:send:' + draft['request_id']), p.audits, p.tg, OWNER)
    owner_feedback.recover_owner_feedback(p.audits, tg=p.tg, owner=OWNER)
    p.check_result()
    # Reopen persistent store, receive same Telegram confirmation again and recover.
    restarted = AuditStore(p.audits.root)
    service.handle_update(callback('qf:send:' + draft['request_id']), restarted, p.tg, OWNER)
    owner_feedback.recover_owner_feedback(restarted, tg=p.tg, owner=OWNER)
    assert len(p.submit_attempts) == 1 and len(p.speeches) == int(use_voice)


def test_auditor_lost_submit_reply_restarts_and_reuses_accepted_job(pipeline):
    p = pipeline
    service.handle_update(callback('qf:begin:' + p.audit['id']), p.audits, p.tg, OWNER)
    service.handle_update(message(), p.audits, p.tg, OWNER)
    draft = owner_feedback.pending(p.audits, OWNER)
    service.handle_update(callback('qf:send:' + draft['request_id']), p.audits, p.tg, OWNER)
    p.lose_first_reply = True
    owner_feedback.recover_owner_feedback(p.audits, tg=p.tg, owner=OWNER, now=100)
    assert len(p.actual_jobs) == 1
    with sqlite3.connect(p.state) as db:
        assert db.execute('SELECT status,card_job FROM excel_items').fetchone() == ('correcting', 123)
    owner_feedback.recover_owner_feedback(AuditStore(p.audits.root), tg=p.tg, owner=OWNER, now=131)
    p.check_result()
    assert len(p.actual_jobs) == 1 and len(p.submit_attempts) == 1


def test_both_bots_publish_guard_rejects_without_paid_submit(pipeline):
    p = pipeline
    with sqlite3.connect(p.state) as db:
        db.execute("UPDATE awaiting SET status='published'")
    deps = orchestrator_deps(p)
    bot.handle_update(callback('pf:edit:' + p.audit['id']), deps)
    bot.handle_update(message(), deps)
    request_id = deps.store.get_meta(photo_feedback_ui.PENDING)
    bot.handle_update(callback('pf:send:' + request_id), deps)
    assert not p.actual_jobs and not p.submit_attempts
    service.handle_update(callback('qf:begin:' + p.audit['id']), p.audits, p.tg, OWNER)
    service.handle_update(message(), p.audits, p.tg, OWNER)
    draft = owner_feedback.pending(p.audits, OWNER)
    service.handle_update(callback('qf:send:' + draft['request_id']), p.audits, p.tg, OWNER)
    owner_feedback.recover_owner_feedback(p.audits, tg=p.tg, owner=OWNER)
    assert not p.actual_jobs and not p.submit_attempts
    assert any('опубликована' in str(event) for event in p.tg.events)


def standalone(p, *, owner=OWNER):
    """Actual API multipart adapter, replacing only its external HTTP endpoint."""
    with sqlite3.connect(p.state) as db:
        db.execute('DELETE FROM excel_items')
        db.execute('DELETE FROM awaiting')
    with sqlite3.connect(p.queue) as db:
        for name, type_ in [('chat_id', 'INTEGER'), ('caption', 'TEXT'), ('publish_mode', 'TEXT'),
                            ('assigned_account', 'TEXT'), ('result_sent', 'INTEGER DEFAULT 0')]:
            db.execute('ALTER TABLE jobs ADD COLUMN ' + name + ' ' + type_)
        db.execute('UPDATE jobs SET chat_id=?,caption=?,publish_mode=?,assigned_account=?,result_sent=1,request_key=NULL',
                   (owner, 'Исходная подпись владельца', 'conditioner', 'acc2'))
    p.raw_posts, p.raw_images, p.raw_receipts = [], [], {}

    def api(request):
        assert request.url.path == '/api/submit-job'
        assert request.headers['x-agent-token'] == 'local-agent-key'
        envelope = BytesParser(policy=default).parsebytes(
            ('Content-Type: ' + request.headers['content-type'] + '\r\n\r\n').encode() + request.content)
        fields, image = {}, None
        for part in envelope.iter_parts():
            name = part.get_param('name', header='content-disposition')
            if name == 'photo':
                image = part.get_payload(decode=True)
            else:
                fields[name] = part.get_payload(decode=True).decode()
        p.raw_posts.append(fields)
        p.raw_images.append(image)
        key = fields['request_key']
        immutable = (json.dumps(fields, sort_keys=True), image)
        if key in p.raw_receipts:
            job_id, previous = p.raw_receipts[key]
            assert immutable == previous
            return httpx.Response(200, json={'job_id': job_id, 'queued': 'new-input.png', 'reused': True})
        with sqlite3.connect(p.queue) as db:
            job_id = db.execute('INSERT INTO jobs(mode,brand,model,status,specs,request_key,chat_id,caption,'
                                'publish_mode,assigned_account) VALUES(?,?,?,?,?,?,?,?,?,?)',
                                (fields['mode'], fields['brand'], fields['model'], 'pending', fields['specs'], key,
                                 int(fields['chat_id']), fields['caption'], fields['publish_mode'], fields['assigned_account'])).lastrowid
        p.raw_receipts[key] = (job_id, immutable)
        if p.lose_first_reply:
            p.lose_first_reply = False
            raise httpx.ReadTimeout('accepted response lost', request=request)
        return httpx.Response(200, json={'job_id': job_id, 'queued': 'new-input.png', 'reused': False})

    p.raw_http = httpx.Client(transport=httpx.MockTransport(api))
    p.raw_submit = photo_correction.make_standalone_submitter('http://local-photogen',
                                                             {'x-agent-token': 'local-agent-key'}, p.raw_http)
    return p


def auditor_submit(p):
    service.handle_update(callback('qf:begin:' + p.audit['id']), p.audits, p.tg, OWNER)
    service.handle_update(message(voice=True), p.audits, p.tg, OWNER)
    draft = owner_feedback.pending(p.audits, OWNER)
    service.handle_update(callback('qf:send:' + draft['request_id']), p.audits, p.tg, OWNER)
    return draft


def test_standalone_auditor_correction_keeps_normal_result_delivery(pipeline):
    p = standalone(pipeline)
    try:
        draft = auditor_submit(p)
        assert not p.raw_posts
        owner_feedback.recover_owner_feedback(p.audits, tg=p.tg, owner=OWNER)
        assert len(p.raw_posts) == 1 and p.raw_images == [p.original]
        sent = p.raw_posts[0]
        assert sent['chat_id'] == str(OWNER) and sent['assigned_account'] == 'acc2'
        assert sent['caption'] == 'Исходная подпись владельца' and sent['publish_mode'] == 'conditioner'
        assert NOTE in sent['specs'] and sent['mode'] == 'kbt'
        assert 'result_sent' not in sent
        with sqlite3.connect(p.queue) as db:
            assert db.execute('SELECT chat_id,result_sent FROM jobs WHERE id>123').fetchone() == (OWNER, 0)
        with sqlite3.connect(p.state) as db:
            assert db.execute('SELECT count(*) FROM excel_items').fetchone()[0] == 0
        service.handle_update(callback('qf:send:' + draft['request_id']), p.audits, p.tg, OWNER)
        owner_feedback.recover_owner_feedback(AuditStore(p.audits.root), tg=p.tg, owner=OWNER)
        assert len(p.raw_posts) == 1
    finally:
        p.raw_http.close()


def test_standalone_unknown_reply_reuses_same_raw_request_after_restart(pipeline):
    p = standalone(pipeline)
    try:
        auditor_submit(p)
        p.lose_first_reply = True
        owner_feedback.recover_owner_feedback(p.audits, tg=p.tg, owner=OWNER, now=100)
        assert len(p.raw_receipts) == 1
        owner_feedback.recover_owner_feedback(AuditStore(p.audits.root), tg=p.tg, owner=OWNER, now=131)
        assert len(p.raw_posts) == 1  # accepted job resolved from queue request_key
        assert len(p.raw_receipts) == 1
    finally:
        p.raw_http.close()


def test_standalone_cannot_be_corrected_by_foreign_actor_or_duplicate_parent(pipeline):
    p = standalone(pipeline, owner=999)
    try:
        auditor_submit(p)
        owner_feedback.recover_owner_feedback(p.audits, tg=p.tg, owner=OWNER)
        assert not p.raw_posts
        assert any('другому чату' in str(event) for event in p.tg.events)
    finally:
        p.raw_http.close()


def test_standalone_parent_cannot_purchase_another_correction_under_new_request(pipeline):
    p = standalone(pipeline)
    try:
        auditor_submit(p)
        owner_feedback.recover_owner_feedback(p.audits, tg=p.tg, owner=OWNER)
        assert len(p.raw_posts) == 1
        auditor_submit(p)
        owner_feedback.recover_owner_feedback(p.audits, tg=p.tg, owner=OWNER)
        assert len(p.raw_posts) == 1
        assert any('уже запрошено' in str(event) for event in p.tg.events)
    finally:
        p.raw_http.close()


def test_accepted_standalone_job_is_recovered_after_frozen_source_disappears(pipeline):
    p = standalone(pipeline)
    try:
        auditor_submit(p)
        p.lose_first_reply = True
        owner_feedback.recover_owner_feedback(p.audits, tg=p.tg, owner=OWNER, now=100)
        with sqlite3.connect(p.state) as db:
            context = json.loads(db.execute('SELECT context FROM photo_owner_corrections').fetchone()[0])
        Path(context['source']).unlink()
        owner_feedback.recover_owner_feedback(AuditStore(p.audits.root), tg=p.tg, owner=OWNER, now=131)
        with sqlite3.connect(p.state) as db:
            receipt = json.loads(db.execute('SELECT result FROM photo_owner_corrections').fetchone()[0])
        assert receipt['submitted'] and receipt['standalone']
        assert len(p.raw_posts) == 1
    finally:
        p.raw_http.close()


def test_retired_factory_card_cannot_be_reclassified_as_standalone(pipeline):
    p = pipeline
    with sqlite3.connect(p.state) as db:
        db.execute('UPDATE excel_items SET card_job=999')
    with sqlite3.connect(p.queue) as db:
        db.execute('ALTER TABLE jobs ADD COLUMN chat_id INTEGER')
        db.execute('UPDATE jobs SET chat_id=?,request_key=NULL', (OWNER,))
    auditor_submit(p)
    owner_feedback.recover_owner_feedback(p.audits, tg=p.tg, owner=OWNER)
    assert not p.submit_attempts and not p.actual_jobs
    assert any('заменена в контент-заводе' in str(event) for event in p.tg.events)


@pytest.mark.parametrize('tamper', [False, True])
def test_explicit_full_product_reference_has_priority_and_cannot_fall_back(pipeline, tamper):
    p = standalone(pipeline)
    try:
        archive = p.root / 'archive'
        archive.mkdir()
        (archive / 'cleaned.png').write_bytes(p.original)  # existing incomplete input
        whole = p.root / 'verified-whole-product.png'
        full_bytes = picture('green')
        whole.write_bytes(full_bytes)
        p.preferred_reference = (whole, hashlib.sha256(full_bytes).hexdigest())
        p.preferred_note = 'Подтверждённый источник показывает полный вертикальный корпус. Удалить выдуманную мощность.'
        if tamper:
            whole.write_bytes(picture('black'))
        auditor_submit(p)
        owner_feedback.recover_owner_feedback(p.audits, tg=p.tg, owner=OWNER)
        if tamper:
            assert not p.raw_posts
            assert any('эталон исправления изменился' in str(event) for event in p.tg.events)
        else:
            assert p.raw_images == [full_bytes]
            assert p.preferred_note in p.raw_posts[0]['specs']
            assert 'Подтверждённый эталон полного товара заменяет прежний неполный кадр' in p.raw_posts[0]['specs']
            assert (archive / 'cleaned.png').read_bytes() == p.original
    finally:
        p.raw_http.close()

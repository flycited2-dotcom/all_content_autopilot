import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from orchestrator_bot import photo_correction as correction
from orchestrator_bot.factory import Factory
from tests.test_photo_auditor import picture


@pytest.fixture
def pipeline(tmp_path):
    state, queue, output = tmp_path / 'state.db', tmp_path / 'queue.db', tmp_path / 'output'
    output.mkdir()
    (tmp_path / 'archive').mkdir()
    source, old_card = picture('blue'), picture('red')
    (tmp_path / 'archive/source.png').write_bytes(source)
    (output / 'old-card.png').write_bytes(old_card)
    with sqlite3.connect(queue) as db:
        db.execute('CREATE TABLE jobs (id INTEGER PRIMARY KEY,mode TEXT,brand TEXT,model TEXT,status TEXT,'
                   'specs TEXT,output_filename TEXT,input_filename TEXT,archived_filename TEXT,failed_filename TEXT,error_text TEXT)')
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (123, 'kbt', 'CARVER', 'PPG-11000 DVR', 'done', 'Цена 116000 рублей. Старые характеристики.',
                    'old-card.png', 'source.png', 'source.png', None, None))
    with sqlite3.connect(state) as db:
        db.execute('CREATE TABLE excel_items(key TEXT PRIMARY KEY,brand TEXT,model TEXT,status TEXT,card_job INTEGER,'
                   'research_job INTEGER,card_request_key TEXT,error TEXT,tries INTEGER,ts REAL)')
        db.execute('INSERT INTO excel_items VALUES(?,?,?,?,?,?,?,?,?,?)',
                   ('excel|carver|ppg-11000 dvr', 'CARVER', 'PPG-11000 DVR', 'preview', 123, None, None, None, 0, 1))
        db.execute('CREATE TABLE awaiting(key TEXT PRIMARY KEY,status TEXT)')
        db.execute("INSERT INTO awaiting VALUES('excel|carver|ppg-11000 dvr','pending')")
    value = {'request_id': 'a' * 32, 'audit_id': 'b' * 32, 'job_id': 123, 'revision': 2,
             'sha256': hashlib.sha256(old_card).hexdigest(), 'brand': 'CARVER', 'model': 'PPG-11000 DVR',
             'notes': 'Это дизельный генератор. Убери упоминание бензина. Раньше правка не сработала.', 'actor_id': 7}
    return SimpleNamespace(root=tmp_path, state=state, queue=queue, output=output, value=value, source=source)


def call(p, submit, **kwargs):
    return correction.correct_photo(p.state, p.queue, p.value, submit_card=submit, output_dir=p.output, now=10, **kwargs)


def state(p):
    with sqlite3.connect(p.state) as db:
        return db.execute('SELECT status,card_job FROM excel_items').fetchone(), db.execute('SELECT status FROM awaiting').fetchone()[0]


def test_owner_correction_preserves_supplier_identity_notes_and_one_render_marker(pipeline):
    p = pipeline
    seen = []
    def submit(brand, model, specs, source, mode, **kw):
        assert state(p) == (('correcting', 123), 'correcting')
        assert Path(source).read_bytes() == p.source
        assert (brand, model, mode) == ('CARVER', 'PPG-11000 DVR', 'kbt')
        assert specs.splitlines()[0] == 'OWNER_CORRECTION_REQUEST_ID=' + p.value['request_id']
        assert p.value['notes'] in specs and p.value['sha256'] in specs
        assert p.value['audit_id'] in specs and 'Цена 116000 рублей' in specs
        assert 'приоритет' in specs
        seen.append(kw['request_key'])
        return 124
    result = call(p, submit)
    assert result['submitted'] and result['linked'] and result['new_job_id'] == 124
    assert result['published'] is False
    assert state(p) == (('card', 124), 'correcting')
    assert seen == ['owner-correction-' + p.value['request_id']]
    # Neither the old output nor its immutable source was edited in place.
    assert hashlib.sha256((p.output / 'old-card.png').read_bytes()).hexdigest() == p.value['sha256']


def test_duplicate_confirmations_submit_only_once(pipeline):
    calls = []
    def submit(*args, **kw):
        calls.append(kw)
        return 124
    first = call(pipeline, submit)
    second = call(pipeline, submit)
    assert second['duplicate'] and second['job_id'] == first['job_id']
    assert len(calls) == 1
    pipeline.value['notes'] = 'Другой текст под тем же запросом'
    with pytest.raises(ValueError, match='другим содержимым'):
        call(pipeline, submit)
    assert len(calls) == 1


def test_lost_submit_reply_reuses_queue_request_key_without_new_render(pipeline):
    requests, rendered = [], {}
    def submit(*args, **kw):
        key = kw['request_key']
        requests.append(key)
        if key not in rendered:
            rendered[key] = 124
            raise TimeoutError('reply lost after queue accepted the job')
        return rendered[key]
    with pytest.raises(TimeoutError):
        call(pipeline, submit)
    assert state(pipeline) == (('correcting', 123), 'correcting')
    assert call(pipeline, submit)['job_id'] == 124
    assert requests[0] == requests[1] and len(rendered) == 1


@pytest.mark.parametrize('mutation,reason,handled', [
    ("UPDATE jobs SET status='processing'", 'выполняется', False),
    ("UPDATE jobs SET mode='research'", 'поиска исходника', True),
    ("UPDATE jobs SET status='failed',error_text='PHOTO_AUDIT_HOLD: Не подтверждён результат начатой генерации'", 'неизвестен', True),
    ("UPDATE jobs SET model='PPG-11000 DR'", 'другому товару', True),
])
def test_in_flight_unknown_and_wrong_identity_never_submit(pipeline, mutation, reason, handled):
    with sqlite3.connect(pipeline.queue) as db:
        db.execute(mutation)
    def forbidden(*a, **kw):
        pytest.fail('No generation is allowed for this state')
    result = call(pipeline, forbidden)
    assert result['handled'] is handled and not result['submitted'] and reason in result['reason']
    assert state(pipeline) == (('preview', 123), 'pending')


@pytest.mark.parametrize('mutation', [
    "UPDATE excel_items SET card_job=120",
    "UPDATE excel_items SET status='cancelled'",
    "UPDATE awaiting SET status='published'",
])
def test_stale_or_published_card_cannot_be_revised(pipeline, mutation):
    with sqlite3.connect(pipeline.state) as db:
        db.execute(mutation)
    result = call(pipeline, lambda *a, **kw: pytest.fail('Stale action cannot buy a generation'))
    assert result['handled'] and not result['submitted']


def test_mismatched_previous_snapshot_and_missing_supplier_are_terminal(pipeline):
    p = pipeline
    p.value['sha256'] = 'c' * 64
    result = call(p, lambda *a, **kw: pytest.fail('Wrong snapshot'))
    assert 'отличается' in result['reason']
    p.value['sha256'] = hashlib.sha256((p.output / 'old-card.png').read_bytes()).hexdigest()
    (p.root / 'archive/source.png').unlink()
    result = call(p, lambda *a, **kw: pytest.fail('No supplier: do not reuse the wrong composite'))
    assert result['handled'] and 'исходного фото' in result['reason']
    assert state(p) == (('preview', 123), 'pending')


def test_new_request_cannot_overtake_unknown_reserved_submission(pipeline):
    p = pipeline
    def unknown(*a, **kw):
        raise TimeoutError()
    with pytest.raises(TimeoutError):
        call(p, unknown)
    p.value = {**p.value, 'request_id': 'c' * 32}
    result = call(p, lambda *a, **kw: pytest.fail('Reserved generation cannot be duplicated'))
    assert result['handled'] and not result['submitted']


@pytest.mark.parametrize('tampered', [False, True])
def test_old_card_uses_exact_mirrored_supplier_when_server_archive_was_cleaned(pipeline, tampered):
    import base64
    from orchestrator_bot.photo_reviews import register
    from orchestrator_bot.store import Store
    p = pipeline
    (p.root / 'archive/source.png').unlink()
    reviews = Store(p.root / 'orch.db')
    card = {k: p.value[k] for k in ('job_id', 'audit_id', 'revision', 'sha256', 'brand', 'model')}
    card.update(status='review', summary='Дизельный генератор',
                preview=base64.b64encode((p.output / 'old-card.png').read_bytes()).decode(),
                supplier=base64.b64encode(p.source).decode(), supplier_sha256=hashlib.sha256(p.source).hexdigest())
    register(reviews, card)
    original = correction.mirrored_supplier(reviews.path, p.value)
    assert original
    if tampered:
        original[0].write_bytes(picture('black'))
    calls = []
    def submit(*args, **kwargs):
        assert Path(args[3]).read_bytes() == p.source
        calls.append(args)
        return 124
    result = call(p, submit, verified_supplier=original)
    assert bool(result.get('submitted')) is not tampered
    assert len(calls) == (0 if tampered else 1)


def test_cancel_during_submit_does_not_rebind_or_publish(pipeline):
    def submit(*a, **kw):
        with sqlite3.connect(pipeline.state) as db:
            db.execute("UPDATE excel_items SET status='cancelled'")
        return 124
    result = call(pipeline, submit)
    assert result['submitted'] and not result['linked'] and not result['published']
    assert state(pipeline)[0] == ('cancelled', 123)


def test_factory_routes_correction_to_local_bridge_with_unicode_notes():
    seen = []
    def runner(cmd, **kwargs):
        seen.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"handled":true}', stderr='')
    decision = {'notes': 'Дизельный, убери бензин'}
    assert Factory('python', '/cf', runner=runner).correct_photo(decision) == {'handled': True}
    cmd, kwargs = seen[0]
    assert cmd[2:5] == ['orchestrator_bot.catalog_cli', 'correct-photo', '--feedback-json']
    assert json.loads(cmd[5]) == decision
    assert kwargs['cwd'] == '/cf'


@pytest.mark.parametrize('field,value', [('job_id', True), ('actor_id', 0), ('notes', ''), ('notes', 'x' * 6001),
                                        ('revision', 3), ('request_id', '../bad'), ('sha256', 'bad')])
def test_malformed_request_is_rejected_before_reservation(pipeline, field, value):
    pipeline.value[field] = value
    with pytest.raises(ValueError):
        call(pipeline, lambda *a, **kw: pytest.fail('Malformed request'))
    assert state(pipeline) == (('preview', 123), 'pending')

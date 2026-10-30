from datetime import datetime
from types import SimpleNamespace

import pytest

from orchestrator_bot import feed
from orchestrator_bot.store import Store


NOW = datetime(2026, 10, 30, 12, 50).timestamp()


class Telegram:
    def __init__(self):
        self.sent = []
        self.fail = False

    def send(self, recipient, text, keyboard=None):
        self.sent.append((recipient, text, keyboard))
        if self.fail:
            raise OSError('delivery outcome unknown')


class Factory:
    def __init__(self):
        self.status = {'total': 8, 'ready': 0, 'in_work': 8, 'published': 0, 'dropped': 0}

    def batch_status(self, keys):
        assert len(keys) == 8
        return dict(self.status)

    def publish_keys(self, keys):
        pytest.fail('A progress notice must not publish')


def setup(tmp_path, *, age=0, manual=True):
    store = Store(tmp_path / 'orchestrator.db')
    result = {'manual_generation_id': 'supplier-all8-source-sha'} if manual else {}
    log_id = store.log_action(None, 'enqueue', {'type': 'enqueue_products'}, result)
    store.add_batch(log_id, 'Генераторы — все 8 с фото', [f'cohort-{n}' for n in range(8)], None, now=NOW-age)
    store.set_meta('digest_day', datetime.fromtimestamp(NOW).date().isoformat())
    return SimpleNamespace(store=store, tg=Telegram(), factory=Factory(), owner_chat_id=123), log_id


def tick(deps, now):
    feed.tick(deps, now)


def test_recent_manual_cohort_reports_queue_facts_once_and_survives_restart(tmp_path):
    deps, log_id = setup(tmp_path)
    tick(deps, NOW)
    text = deps.tg.sent[0][1]
    assert '0 из 8' in text and 'В очереди и обработке: 8' in text
    assert 'Без готового превью: 0' in text
    assert 'генерация началась' not in text
    restarted = SimpleNamespace(**{**vars(deps), 'store': Store(deps.store.path)})
    tick(restarted, NOW + feed.POLL_EVERY + 1)
    tick(restarted, NOW + 2 * feed.POLL_EVERY + 2)
    assert len(deps.tg.sent) == 1
    assert deps.store.get_batch(log_id)['status'] == 'open'


def test_changed_partial_counts_report_actual_ready_without_marking_batch_complete(tmp_path):
    deps, log_id = setup(tmp_path)
    tick(deps, NOW)
    deps.factory.status.update(ready=2, in_work=5, dropped=1,
                               errors=[{'brand': 'CARVER', 'model': 'PPG-11000 DR', 'error': 'SOURCE_UNAVAILABLE'}])
    tick(deps, NOW + feed.POLL_EVERY + 1)
    assert len(deps.tg.sent) == 2
    text = deps.tg.sent[-1][1]
    assert '2 из 8' in text and 'В очереди и обработке: 5' in text
    assert 'Без готового превью: 1' in text and 'PPG-11000 DR' in text
    assert deps.tg.sent[-1][2] is None
    assert deps.store.get_batch(log_id)['status'] == 'open'


def test_research_to_card_transition_notifies_even_before_any_preview_is_ready(tmp_path):
    deps, _ = setup(tmp_path)
    deps.factory.status['stages'] = dict(new=0, submission=0, research=8, card=0, preview=0)
    tick(deps, NOW)
    deps.factory.status['stages'].update(research=7, card=1)
    tick(deps, NOW + feed.POLL_EVERY + 1)
    assert len(deps.tg.sent) == 2
    text = deps.tg.sent[-1][1]
    assert '0 из 8' in text and 'В очереди и обработке: 8' in text
    assert 'поиска исходников: 7' in text and 'создания карточек: 1' in text
    tick(deps, NOW + 2 * feed.POLL_EVERY + 2)
    assert len(deps.tg.sent) == 2


def test_changed_counts_are_throttled_and_latest_snapshot_is_sent(tmp_path, monkeypatch):
    deps, _ = setup(tmp_path)
    monkeypatch.setattr(feed, 'POLL_EVERY', 0)
    tick(deps, NOW)
    deps.factory.status.update(ready=1, in_work=7)
    tick(deps, NOW + 20)
    assert len(deps.tg.sent) == 1
    deps.factory.status.update(ready=2, in_work=6)
    tick(deps, NOW + feed.PROGRESS_MIN_INTERVAL + 1)
    assert len(deps.tg.sent) == 2 and '2 из 8' in deps.tg.sent[-1][1]


def test_old_manual_backlog_is_quiet_until_actual_counts_change(tmp_path):
    deps, _ = setup(tmp_path, age=feed.PROGRESS_INITIAL_AGE + 1)
    tick(deps, NOW)
    tick(deps, NOW + feed.POLL_EVERY + 1)
    assert not deps.tg.sent
    deps.factory.status.update(ready=1, in_work=7)
    tick(deps, NOW + 2 * feed.POLL_EVERY + 2)
    assert len(deps.tg.sent) == 1 and '1 из 8' in deps.tg.sent[0][1]


def test_ordinary_existing_batches_keep_their_existing_notification_policy(tmp_path):
    deps, _ = setup(tmp_path, manual=False)
    tick(deps, NOW)
    assert not deps.tg.sent


def test_finished_manual_cohort_uses_existing_review_notice_without_publishing(tmp_path):
    deps, log_id = setup(tmp_path)
    tick(deps, NOW)
    deps.factory.status.update(ready=8, in_work=0)
    tick(deps, NOW + feed.POLL_EVERY + 1)
    assert len(deps.tg.sent) == 2
    assert 'готова: 8 карточек' in deps.tg.sent[-1][1]
    assert deps.tg.sent[-1][2][0][0]['callback_data'] == f'b:p:{log_id}'
    assert deps.store.get_batch(log_id)['status'] == 'ready'


def test_lost_telegram_ack_does_not_repeat_unchanged_status(tmp_path):
    deps, _ = setup(tmp_path)
    deps.tg.fail = True
    tick(deps, NOW)
    deps.tg.fail = False
    tick(deps, NOW + feed.POLL_EVERY + 1)
    assert len(deps.tg.sent) == 1


def test_cancelled_during_status_query_never_sends_stale_progress(tmp_path):
    deps, log_id = setup(tmp_path)

    def stale(keys):
        deps.store.move_batch(log_id, ('open',), 'closed')
        return deps.factory.status

    deps.factory.batch_status = stale
    tick(deps, NOW)
    assert not deps.tg.sent


def test_batch_status_counts_live_pipeline_stages_separately_from_ready_and_failed(tmp_path, monkeypatch):
    import importlib.util
    import json
    from pathlib import Path
    import sys
    from orchestrator_bot.key_commands import run

    source = Path(__file__).resolve().parents[1] / 'snapshots/2026-10-20/src/content_factory'
    if not (source / 'orchestrator/excel_pipeline.py').exists():
        pytest.skip('Local content-factory source snapshot is unavailable')
    modules = {}
    for name in ('sqlite_state', 'orchestrator.excel_pipeline', 'orchestrator.confirm_store'):
        alias = '_manual_batch_snapshot_' + name.replace('.', '_')
        spec = importlib.util.spec_from_file_location(alias, source / (name.replace('.', '/') + '.py'))
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, alias, module)
        monkeypatch.setitem(sys.modules, 'content_factory.' + name, module)
        spec.loader.exec_module(module)
        modules[name] = module
    path = tmp_path / 'factory.db'
    store = modules['orchestrator.excel_pipeline'].ExcelStore(path)
    confirming = modules['orchestrator.confirm_store'].ConfirmStore(path)
    states = ('research', 'card', 'new', 'submission', 'preview', 'cancelled', 'failed', 'preview')
    rows = [(f'cohort-{n}', 'CARVER', f'PPG-{n}', 'Генератор', 100) for n in range(8)]
    assert len(store.select_items(rows)) == 8
    with store._c() as db:
        db.executemany('UPDATE excel_items SET status=?,ts=100 WHERE key=?',
                       [(state, row[0]) for state, row in zip(states, rows)])
        db.execute("UPDATE excel_items SET error='SOURCE_UNAVAILABLE' WHERE key='cohort-6'")
    confirming.add('cohort-7', 'telegram', 'card.jpg', 'caption')
    args = SimpleNamespace(command='batch-status', keys_json=json.dumps([row[0] for row in rows]))
    result = run(SimpleNamespace(_paths=lambda args: (path, None, None)), args)
    assert {key: result[key] for key in ('total', 'ready', 'in_work', 'published', 'dropped')} == {
        'total': 8, 'ready': 1, 'in_work': 5, 'published': 0, 'dropped': 2}
    assert result['stages'] == dict(new=1, submission=1, research=1, card=1, preview=1)
    assert result['errors'] == [{'brand': 'CARVER', 'model': 'PPG-6', 'error': 'SOURCE_UNAVAILABLE'}]

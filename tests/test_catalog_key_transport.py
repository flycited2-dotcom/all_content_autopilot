import json
from types import SimpleNamespace

import pytest

from orchestrator_bot.factory import Factory
from orchestrator_bot.key_commands import keys_from_json, readable_errors, current_decision


KEY = 'excel|willmark|wmg-3672gs (2000вт,реверс,3реш)'


@pytest.mark.parametrize('command', ['cancel', 'batch_status', 'publish_keys'])
def test_complete_supplier_keys_survive_every_batch_command(command):
    calls = []
    def runner(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout='{}')
    factory = Factory('python', '/opt/content-factory', runner=runner)
    getattr(factory, command)([KEY, 'excel|jvc|jk-mg123'])
    args = calls[0]
    assert args[2] == 'orchestrator_bot.catalog_cli'
    assert keys_from_json(args[args.index('--keys-json') + 1]) == [KEY, 'excel|jvc|jk-mg123']
    assert '--keys' not in args


@pytest.mark.parametrize('value', [None, '', [], [KEY, KEY], [''], [1], 'a,b'])
def test_invalid_or_ambiguous_key_lists_are_rejected(value):
    with pytest.raises(ValueError):
        keys_from_json(json.dumps(value))


def test_failed_batch_describes_actual_source_problem():
    text = readable_errors([{'brand': 'Willmark', 'model': 'WMG-3672GS',
                             'error': "Server error '503 Service Unavailable'"}])
    assert 'Willmark WMG-3672GS' in text
    assert '503' in text and 'сайт источника' in text
    assert 'фотоаудитора' not in text


def test_previous_campaign_decision_cannot_publish_or_count_a_new_card():
    old = SimpleNamespace(ts=100, status='published')
    pending = SimpleNamespace(ts=300, status='pending')
    preview = SimpleNamespace(status='preview')
    assert current_decision(preview, old, 200) is None
    assert current_decision(SimpleNamespace(status='research'), pending, 200) is None
    assert current_decision(SimpleNamespace(status='failed'), old, 200) is None
    assert current_decision(preview, pending, 200) is pending
    assert current_decision(preview, pending, None) is None


def test_late_audit_reopens_failed_batch_but_never_owner_cancelled_batch(tmp_path):
    from orchestrator_bot.store import Store
    store = Store(tmp_path / 'orchestrator.db')
    for i, state in enumerate(['failed', 'closed', 'published', 'approved'], 1):
        store.add_batch(i, 'мясорубки', [KEY], None)
        store.move_batch(i, ('open',), state)
    assert store.reopen_recovered_batches(KEY) == 1
    assert [store.get_batch(i)['status'] for i in range(1, 5)] == ['open', 'closed', 'published', 'approved']
    assert store.reopen_recovered_batches(KEY) == 0


def test_late_audit_reopens_partial_ready_batch_once_per_receipt(tmp_path):
    from orchestrator_bot.store import Store
    store = Store(tmp_path / 'orchestrator.db')
    store.add_batch(18, 'мясорубки', [KEY, 'excel|jvc|jk-mg123'], None, now=100)
    store.move_batch(18, ('open',), 'ready', now=101)
    assert store.reopen_recovered_batches(KEY, now=102, recovery_id='audit:0') == 1
    assert store.get_batch(18)['status'] == 'open'
    store.move_batch(18, ('open',), 'ready', now=103)
    # A retry after a lost API response cannot announce the same recovered card again.
    assert store.reopen_recovered_batches(KEY, now=104, recovery_id='audit:0') == 0
    assert store.get_batch(18)['status'] == 'ready'
    assert store.get_batch(18)['updated'] == 103
    assert store.reopen_recovered_batches(KEY, now=105, recovery_id='audit:1') == 1


def test_recovery_never_overwrites_owner_decisions_or_unrelated_batches(tmp_path):
    from orchestrator_bot.store import Store
    store = Store(tmp_path / 'orchestrator.db')
    states = ['closed', 'approved', 'publishing', 'published']
    for i, state in enumerate(states, 1):
        store.add_batch(i, 'мясорубки', [KEY], None)
        store.move_batch(i, ('open',), state)
    store.add_batch(5, 'другие товары', ['another-key'], None)
    store.move_batch(5, ('open',), 'ready')
    assert store.reopen_recovered_batches(KEY, recovery_id='audit:0') == 0
    assert [store.get_batch(i)['status'] for i in range(1, 6)] == states + ['ready']


def test_recovery_invalidates_a_concurrent_stale_batch_poll(tmp_path):
    from orchestrator_bot.store import Store
    store = Store(tmp_path / 'orchestrator.db')
    store.add_batch(18, 'мясорубки', [KEY], None, now=100)
    old = store.get_batch(18)
    assert store.reopen_recovered_batches(KEY, now=100, recovery_id='audit:0') == 1
    assert not store.move_batch(18, ('open',), 'ready', expected_updated=old['updated'])
    assert not store.move_batch(18, ('open',), 'failed', expected_updated=old['updated'])
    assert store.get_batch(18)['status'] == 'open'

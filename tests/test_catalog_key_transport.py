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

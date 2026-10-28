import ast
import asyncio
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from photo_auditor import runtime
from photo_auditor.integration import AuditHold
from photo_auditor.research import SourceUnavailable, search_product


@pytest.fixture
def config(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, 'load', lambda root: {'api_token': 'runtime-test-secret'})
    return tmp_path


def test_product_upload_requires_exact_approved_image(config, monkeypatch):
    assert runtime.register_job(config, 12, 'kbt', 'Midea', 'EXACT-1', 'specs')
    called = []
    monkeypatch.setattr(runtime, 'require_permit', lambda *args: called.append(args))
    runtime.guard_upload(config, 12, Path('image.png'))
    assert called == [(config, 'fotogen-12', Path('image.png'))]
    with pytest.raises(AuditHold):
        runtime.guard_upload(config, 12, None)


def test_unknown_legacy_outbox_is_not_silently_released(config):
    with pytest.raises(AuditHold, match='старый outbox'):
        runtime.guard_upload(config, 999, Path('old.png'))


def test_editorial_scene_is_not_claimed_as_audited_product(config):
    assert not runtime.register_job(config, 13, 'research', '', 'Дом зимой', 'editorial')
    runtime.guard_upload(config, 13, None)
    with pytest.raises(AuditHold):
        runtime.register_job(config, 14, 'research', '', 'ABC-12', 'кондиционеры')


def test_job_scope_cannot_be_changed_to_bypass_audit(config):
    runtime.register_job(config, 15, 'kbt', 'Midea', 'EXACT-1', '')
    with pytest.raises(AuditHold):
        runtime.register_job(config, 15, 'ritual', '', '', '')
    path = config / 'runs/photo-auditor/job-scope/15.json'
    value = json.loads(path.read_text(encoding='utf-8'))
    value['payload']['required'] = False
    path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(AuditHold):
        runtime.guard_upload(config, 15, Path('image.png'))


def test_human_calibration_never_changes_automatic_verdict_or_upload_permission(tmp_path):
    from photo_auditor.store import AuditStore
    from tests.test_photo_auditor import picture, product
    store = AuditStore(tmp_path)
    job = store.submit(product(), picture())
    claimed = store.claim(audit_id=job['id'])
    store.finish(claimed, {'verdict': 'review', 'reason': 'Нет официального эталона',
                          'checks': [], 'sources': [], 'dimensions_note': 'Не измерены'})
    assert store.feedback(job['id'], 'match')
    assert store.get(job['id'])['status'] == 'review'
    assert not store.feedback('missing-id', 'match')
    with pytest.raises(ValueError):
        store.feedback(job['id'], 'approved')


@pytest.fixture(scope='module')
def generator_patch():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('audit_patch', root / 'scripts/prepare-photo-auditor-integration.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    backup = root / 'runs/2026-10-23/autopilot/photo-agent-backup/remote_agent.py'
    agent = root.parent / 'agent_convert_foto_rituailb2b2_V2_office/agent_convert_foto_rituailb2b2/remote_agent.py'
    if backup.exists():
        source = backup.read_text(encoding='utf-8')
    elif agent.exists() and 'PHOTO_AUDIT_BRIDGE_V' not in agent.read_text(encoding='utf-8'):
        source = agent.read_text(encoding='utf-8')
    else:
        pytest.skip('Original photo-agent source is not part of this checkout')
    return module, source, module.patched(source)


def test_generator_patch_gates_research_card_and_both_replay_routes(generator_patch):
    module, original, patched = generator_patch
    compile(patched, 'remote_agent.py', 'exec')
    assert patched.count('await checked_output(') == 2
    assert patched.count('guard_upload(AUDIT_ROOT, job_id, output_path)') == 2
    assert patched.count('register_job(AUDIT_ROOT, job_id, job_mode') == 3
    assert patched.count('except PhotoAuditHold as hold:') == 3
    assert 'restore_audit_output(' in patched and 'regeneration_specs(' in patched
    assert 'render_exact_product_card' not in patched
    assert 'Исходник %d восстановлен из снимка контроля' in patched
    with pytest.raises(ValueError, match='уже присутствует'):
        module.patched(patched)

    # Research must still use the original import, with no replacement function.
    tree = ast.parse(patched)
    assert not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name == 'process_research' for node in ast.walk(tree))
    original_tree = ast.parse(original)
    for name in ('process_one_file', 'process_research'):
        original_call = next(node for node in ast.walk(original_tree)
                             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                             and node.func.id == name)
        current_calls = [node for node in ast.walk(tree)
                         if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                         and node.func.id == name]
        assert len(current_calls) == (2 if name == 'process_one_file' else 1)
        assert any(ast.dump(call) == ast.dump(original_call) for call in current_calls)
        if name == 'process_research':
            wrapped = _calls(tree, 'search_product')
            assert len(wrapped) == 1
            assert isinstance(wrapped[0].args[0], ast.Name) and wrapped[0].args[0].id == name
            assert [ast.dump(arg) for arg in wrapped[0].args[1:]] == [ast.dump(arg) for arg in original_call.args]
            assert [ast.dump(arg) for arg in wrapped[0].keywords] == [ast.dump(arg) for arg in original_call.keywords]


def _calls(node, name):
    return [item for item in ast.walk(node)
            if isinstance(item, ast.Call) and isinstance(item.func, ast.Name)
            and item.func.id == name]


def _generation_branch(source, mode, namespace):
    """Run the actual patched branch through its audit, without starting the agent."""
    generator = 'process_one_file' if mode == 'card' else 'process_research'
    tree = ast.parse(source)
    enclosing = [node for node in ast.walk(tree)
                 if isinstance(node, ast.Try) and any(_calls(item, generator) for item in node.body)]
    body = min(enclosing, key=lambda node: node.end_lineno - node.lineno).body
    through_audit = []
    for statement in body:
        through_audit.append(copy.deepcopy(statement))
        if _calls(statement, 'checked_output'):
            break
    else:
        raise AssertionError('Generator no longer passes through the independent audit')
    names = ('output_path',) if mode == 'card' else ('photo', 'utp', 'research_revision')
    through_audit.append(ast.Return(value=ast.Tuple(
        elts=[ast.Name(id=name, ctx=ast.Load()) for name in names], ctx=ast.Load())))
    function = ast.AsyncFunctionDef(name='run_branch',
        args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=through_audit, decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    exec(compile(module, '<patched-generation-branch>', 'exec'), namespace)
    return asyncio.run(namespace['run_branch']())


def _generation_namespace(tmp_path, events, *, restored=None, research=None, audit=True):
    async def process_one_file(path, **kwargs):
        events.append(('card', path, kwargs))
        return tmp_path / 'chatgpt.png'

    async def process_research(*args, **kwargs):
        events.append(('research', args, kwargs))
        return tmp_path / 'chatgpt.png', 'Original ChatGPT product facts'

    async def checked_output(*args, **kwargs):
        events.append(('audit', args, kwargs))
        return args[4]

    def starting(*args, **kwargs):
        events.append(('starting', args, kwargs))

    def researching(*args, **kwargs):
        events.append(('researching', args, kwargs))

    return dict(AUDIT_ROOT=tmp_path if audit else None, ROOT=tmp_path,
        attempt=1, job_id=51, job_mode='kbt', job_brand='Willmark', job_model='WMG-2083W',
        job_specs='Original job specs', tmp_input=tmp_path / 'supplier.png',
        LANE=None, CDP_URL='http://127.0.0.1:9222', PhotoAuditHold=AuditHold,
        requires_audit=lambda *_: True, register_job=lambda *args: events.append(('register', args)),
        restore_audit_output=lambda *_: restored, restore_research=lambda *_: research,
        start_audit_generation=starting, start_audit_research=researching, process_one_file=process_one_file,
        process_research=process_research, checked_output=checked_output,
        search_product=search_product,
        save_research=lambda *args: events.append(('facts', args)),
        save_audit_output=lambda *args: events.append(('snapshot', args)))


@pytest.mark.parametrize('mode', ['card', 'research'])
def test_first_processing_uses_existing_chatgpt_then_independent_audit(generator_patch, tmp_path, mode):
    events = []
    namespace = _generation_namespace(tmp_path, events)
    result = _generation_branch(generator_patch[2], mode, namespace)
    assert result[0] == tmp_path / 'chatgpt.png'
    assert [item[0] for item in events] == (
        ['register', 'starting', 'card', 'audit'] if mode == 'card'
        else ['register', 'researching', 'research', 'facts', 'snapshot', 'audit'])
    if mode == 'card':
        assert events[1][1][-1] == 0
        assert events[1][2] == {'paid': True}
    else:
        assert events[1][1][-2:] == ('Willmark', 'WMG-2083W')
        assert events[1][2] == {}
    generation = events[2]
    assert generation[2]['cdp_url'] == 'http://127.0.0.1:9222'
    assert generation[2]['project_url'] is None
    if mode == 'card':
        assert generation[1] == tmp_path / 'supplier.png'
        assert generation[2]['specs'] == 'Original job specs'
    else:
        assert generation[1] == ('Willmark', 'WMG-2083W', 'Original job specs')
    assert events[-1][2] == {'start_revision': 0, 'repair_reference': mode == 'research'}


def test_correction_uses_original_generator_style_and_concrete_feedback(generator_patch, tmp_path):
    from photo_auditor.repairs import regeneration_specs
    events = []
    namespace = _generation_namespace(tmp_path, events)
    exact_photo = tmp_path / 'verified-photo.jpg'
    namespace['verified_reference_asset'] = lambda *args: exact_photo
    namespace['regeneration_specs'] = regeneration_specs

    async def repair(*args, **kwargs):
        assert kwargs['repair_reference'] is False  # A paid render retains its unknown-outcome guard.
        return await args[6]('На карточке 3000 Вт, источник подтверждает 2000 Вт. Исправить значение.')

    namespace['checked_output'] = repair
    result = _generation_branch(generator_patch[2], 'card', namespace)
    calls = [e for e in events if e[0] == 'card']
    assert len(calls) == 2 and result[0] == tmp_path / 'chatgpt.png'
    assert calls[0][1] == tmp_path / 'supplier.png'
    assert calls[1][1] == exact_photo
    assert calls[1][2]['mode'] == calls[0][2]['mode'] == 'kbt'
    assert calls[1][2]['brand'] == 'Willmark' and calls[1][2]['model'] == 'WMG-2083W'
    assert calls[1][2]['cdp_url'] == calls[0][2]['cdp_url']
    assert calls[1][2]['specs'].startswith('Original job specs')
    assert '3000 Вт' in calls[1][2]['specs'] and '2000 Вт' in calls[1][2]['specs']


@pytest.mark.parametrize('mode', ['card', 'research'])
def test_restore_keeps_original_revision_without_another_generation(generator_patch, tmp_path, mode):
    events = []
    saved = {'path': tmp_path / 'saved.png', 'revision': 2, 'specs': 'Saved original facts'}
    namespace = _generation_namespace(tmp_path, events, restored=saved, research=saved)
    result = _generation_branch(generator_patch[2], mode, namespace)
    assert result[0] == saved['path']
    assert not any(item[0] in ('starting', 'researching', 'card', 'research') for item in events)
    assert events[-1][0] == 'audit'
    assert events[-1][2]['start_revision'] == 2
    if mode == 'research':
        assert result[1:] == ('Saved original facts', 2)


def test_unknown_paid_card_generation_outcome_cannot_start_again(generator_patch, tmp_path):
    from photo_auditor import progress
    events = []
    namespace = _generation_namespace(tmp_path, events)
    namespace['start_audit_generation'] = progress.starting
    namespace['restore_audit_output'] = progress.restore
    namespace['restore_research'] = progress.restore_research

    async def interrupted(*args, **kwargs):
        events.append(('generation-started',))
        raise RuntimeError('Connection lost after sending generation request')

    namespace['process_one_file'] = interrupted
    with pytest.raises(RuntimeError, match='Connection lost'):
        _generation_branch(generator_patch[2], 'card', namespace)
    with pytest.raises(AuditHold, match='Не подтверждён результат'):
        _generation_branch(generator_patch[2], 'card', namespace)
    assert events.count(('generation-started',)) == 1


def test_failed_source_retries_exact_sku_and_preserves_browser_connection(generator_patch, tmp_path):
    events, attempts = [], []
    namespace = _generation_namespace(tmp_path, events)
    namespace.update(job_brand='Xiaomi', job_model='L50M8-A2RU')

    async def source(*args, **kwargs):
        attempts.append((args, kwargs))
        if len(attempts) == 1:
            raise RuntimeError('research: source has no model-bound product photo')
        return tmp_path / 'source.png', 'Exact model facts'

    namespace['process_research'] = source
    result = _generation_branch(generator_patch[2], 'research', namespace)
    assert result[:2] == (tmp_path / 'source.png', 'Exact model facts')
    assert len(attempts) == 2
    assert all(args[:2] == ('Xiaomi', 'L50M8-A2RU') for args, _ in attempts)
    assert attempts[0][0][2] == 'Original job specs'
    assert 'source has no model-bound product photo' in attempts[1][0][2]
    assert all(kwargs == {'cdp_url': 'http://127.0.0.1:9222', 'project_url': None}
               for _, kwargs in attempts)
    assert [item[0] for item in events] == ['register', 'researching', 'facts', 'snapshot', 'audit']


def test_product_source_search_is_bounded_to_three_attempts_without_paid_render(generator_patch, tmp_path):
    events, attempts = [], []
    namespace = _generation_namespace(tmp_path, events)

    async def unavailable(*args, **kwargs):
        attempts.append(args)
        raise RuntimeError('research: source page does not contain exact model')

    namespace['process_research'] = unavailable
    with pytest.raises(SourceUnavailable, match='три источника'):
        _generation_branch(generator_patch[2], 'research', namespace)
    assert len(attempts) == 3
    assert all(args[:2] == ('Willmark', 'WMG-2083W') for args in attempts)
    assert [item[0] for item in events] == ['register', 'researching']


def test_editorial_research_keeps_original_single_call_without_product_search_or_checkpoint(generator_patch, tmp_path):
    events = []
    namespace = _generation_namespace(tmp_path, events)
    namespace.update(job_mode='research', job_brand='', job_model='Scene', job_specs='editorial',
                     requires_audit=lambda *_: False)

    async def editorial(*args, **kwargs):
        events.append(('editorial', args, kwargs))
        raise RuntimeError('research: editorial scene failed')

    namespace['process_research'] = editorial
    with pytest.raises(RuntimeError, match='editorial scene failed'):
        _generation_branch(generator_patch[2], 'research', namespace)
    assert [item[0] for item in events] == ['register', 'editorial']
    assert events[-1][1] == ('', 'Scene', 'editorial')


def test_interrupted_source_search_can_retry_without_weakening_paid_card_guard(generator_patch, config):
    from photo_auditor import progress
    events = []
    namespace = _generation_namespace(config, events)
    namespace.update(job_mode='research', register_job=runtime.register_job,
        start_audit_research=progress.start_research, restore_audit_output=progress.restore,
        restore_research=progress.restore_research)

    async def interrupted(*args, **kwargs):
        events.append(('source-request',))
        raise RuntimeError('Browser closed during source search')

    namespace['process_research'] = interrupted
    for _ in range(2):
        with pytest.raises(RuntimeError, match='Browser closed'):
            _generation_branch(generator_patch[2], 'research', namespace)
    assert events.count(('source-request',)) == 2
    assert not any(item[0] in ('starting', 'card', 'audit') for item in events)


def test_downloaded_research_photo_and_facts_are_frozen_before_audit(generator_patch, config):
    from photo_auditor import progress
    from tests.test_photo_auditor import picture
    source = config / 'source.png'
    source.write_bytes(picture())
    namespace = _generation_namespace(config, [])
    namespace.update(job_mode='research', register_job=runtime.register_job,
        start_audit_research=progress.start_research, restore_audit_output=progress.restore,
        restore_research=progress.restore_research, save_research=progress.save_research,
        save_audit_output=progress.generated)

    async def downloaded(*args, **kwargs):
        return source, 'Frozen source facts'

    async def interrupted_audit(*args, **kwargs):
        saved = progress.restore_research(config, 'fotogen-51', 'Willmark', 'WMG-2083W')
        assert saved['path'].read_bytes() == source.read_bytes()
        assert saved['specs'] == 'Frozen source facts'
        assert saved['revision'] == 0
        raise AuditHold('Audit connection interrupted after source download')

    namespace['process_research'] = downloaded
    namespace['checked_output'] = interrupted_audit
    with pytest.raises(AuditHold, match='Audit connection interrupted'):
        _generation_branch(generator_patch[2], 'research', namespace)

    async def must_not_research(*args, **kwargs):
        raise AssertionError('Completed source search must be restored')

    namespace['process_research'] = must_not_research
    with pytest.raises(AuditHold, match='Audit connection interrupted'):
        _generation_branch(generator_patch[2], 'research', namespace)


def test_research_saved_photo_without_facts_is_held_before_generation(generator_patch, tmp_path):
    events = []
    namespace = _generation_namespace(tmp_path, events,
        restored={'path': tmp_path / 'saved.png', 'revision': 1})
    with pytest.raises(AuditHold, match='восстановления характеристик'):
        _generation_branch(generator_patch[2], 'research', namespace)
    assert [item[0] for item in events] == ['register']


@pytest.mark.parametrize('mode', ['card', 'research'])
def test_disabled_audit_preserves_existing_generator(generator_patch, tmp_path, mode):
    events = []
    namespace = _generation_namespace(tmp_path, events, audit=False)
    assert _generation_branch(generator_patch[2], mode, namespace)[0] == tmp_path / 'chatgpt.png'
    assert [item[0] for item in events] == [mode]


def test_all_explicit_generation_limit_handlers_clear_unstarted_checkpoint(generator_patch, tmp_path):
    from photo_auditor import progress
    tree = ast.parse(generator_patch[2])
    handlers = [node for node in ast.walk(tree)
                if isinstance(node, ast.ExceptHandler)
                and any(_calls(statement, 'audit_generation_rejected') for statement in node.body)]
    assert len(handlers) == 3
    for handler in handlers:
        progress.starting(tmp_path, 'fotogen-51', 'Willmark', 'WMG-2083W', 0, paid=True)
        cleanup = ast.fix_missing_locations(ast.Module(body=[copy.deepcopy(handler.body[0])], type_ignores=[]))
        exec(compile(cleanup, '<quota-rejection-cleanup>', 'exec'), dict(
            AUDIT_ROOT=tmp_path, job_id=51, job_brand='Willmark', job_model='WMG-2083W',
            requires_audit=lambda *_: True, audit_generation_rejected=progress.rejected))
        assert progress.restore(tmp_path, 'fotogen-51', 'Willmark', 'WMG-2083W') is None


def test_upload_guard_blocks_network_before_unsigned_card_upload(generator_patch, tmp_path):
    tree = ast.parse(generator_patch[2])
    upload = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                  and node.name == '_upload_result')
    calls = []

    def held(*args):
        calls.append(args)
        raise AuditHold('Unsigned photo')

    namespace = dict(AUDIT_ROOT=tmp_path, Path=Path, guard_upload=held)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[upload], type_ignores=[])),
                 '<guarded-upload>', 'exec'), namespace)
    with pytest.raises(AuditHold, match='Unsigned photo'):
        asyncio.run(namespace['_upload_result']('http://unused', 51, tmp_path / 'unsigned.png'))
    assert calls == [(tmp_path, 51, tmp_path / 'unsigned.png')]


@pytest.mark.parametrize('kind', ['card', 'research'])
def test_unsigned_outbox_is_quarantined_in_both_delivery_modes(generator_patch, tmp_path, kind):
    tree = ast.parse(generator_patch[2])
    deliver = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                   and node.name == '_deliver_entry')
    events = []

    def held(*args):
        events.append(('guard', args))
        raise AuditHold('Unsigned photo')

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            events.append(('post', url))

    journal = tmp_path / 'outbox.json'
    output = tmp_path / 'unsigned.png'
    namespace = dict(AUDIT_ROOT=tmp_path, Path=Path, guard_upload=held,
        PhotoAuditHold=AuditHold, VPS_API_TOKEN='test-token',
        httpx=SimpleNamespace(AsyncClient=Client),
        upload_outbox=SimpleNamespace(quarantine=lambda path: events.append(('quarantine', path))),
        log=SimpleNamespace(warning=lambda *args: None))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[deliver], type_ignores=[])),
                 '<guarded-outbox-delivery>', 'exec'), namespace)
    asyncio.run(namespace['_deliver_entry']('http://unused', journal,
        dict(job_id=51, kind=kind, output_path=str(output), utp='Saved facts')))
    assert events == [('guard', (tmp_path, 51, output)), ('quarantine', journal),
                      ('post', 'http://unused/api/fail/51')]

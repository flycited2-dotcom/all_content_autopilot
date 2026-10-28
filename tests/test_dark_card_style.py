import ast
from pathlib import Path

import pytest

from tools.apply_dark_card_style import dark_config, dark_prompt


@pytest.fixture
def original_prompt():
    # The deployed prompt is preserved by the reviewed plan; no live files are edited by tests.
    root = Path(__file__).resolve().parents[1]
    plan = root / 'runs/2026-10-27/dark-text-style/plan.json'
    from tools.install_photo_audit import AGENT
    prompt = AGENT / 'prompts/kbt.txt'
    backup = root / 'runs/2026-10-27/dark-text-style/backup/kbt.txt'
    if not backup.exists() and not prompt.exists():
        pytest.skip('The local photo-generator prompt is not part of this checkout')
    return (backup if backup.exists() else prompt).read_text(encoding='utf-8-sig')


def test_dark_prompt_removes_conflicting_visual_commands_and_keeps_product_rules(original_prompt):
    result = dark_prompt(original_prompt)
    assert result.count('{{SPECS}}') == 1
    assert 'тёмно-графитовый студийный фон' in result
    assert 'товар располагается слева' in result and 'справа располагается основной блок' in result
    assert 'золотисто-бежевые' in result
    assert 'КРИТИЧЕСКИЙ КОНТРОЛЬ ГЕОМЕТРИИ' in result
    assert 'заводские надписи' in result and 'watermark' in result
    assert 'нельзя дублировать' in result and 'бренды и модели из памяти проекта' in result
    assert not any(x in result for x in ('Фото 1', 'Фото 2', 'Фото 3', 'эталон'))


def test_unrecognized_prompt_cannot_be_overwritten():
    with pytest.raises(ValueError):
        dark_prompt('Different user prompt {{SPECS}}')


def test_reference_upload_is_disabled_for_kbt_without_changing_other_modes():
    source = '''MODES = {
    "mcp": Mode(key="mcp", reference_files=["mcp.png"]),
    "kbt": Mode(
        key="kbt",
        reference_files=_mode_refs("kbt"),
        prompt=_mode_prompt("kbt", "KBT_PROMPT"),
    ),
    "ready_light": Mode(key="ready_light", reference_files=["light.png"]),
}
'''
    changed = dark_config(source)
    before = ast.parse(source).body[0].value
    after = ast.parse(changed).body[0].value
    assert ast.dump(before.values[0]) == ast.dump(after.values[0])
    assert ast.dump(before.values[2]) == ast.dump(after.values[2])
    keywords = {kw.arg: kw.value for kw in after.values[1].keywords}
    assert ast.literal_eval(keywords['reference_files']) == []
    assert ast.literal_eval(keywords['requires_reference']) is False
    assert ast.dump(keywords['prompt']) == ast.dump(before.values[1].keywords[2].value)


def test_changed_mode_configuration_is_not_silently_replaced():
    with pytest.raises(ValueError):
        dark_config('MODES = {"kbt": None}')

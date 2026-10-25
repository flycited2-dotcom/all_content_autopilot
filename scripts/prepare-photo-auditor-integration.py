"""Prepare a reviewable patch; never edits the photo-agent checkout itself."""
import argparse
import difflib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AGENT = ROOT.parent / "agent_convert_foto_rituailb2b2_V2_office/agent_convert_foto_rituailb2b2/remote_agent.py"


def patched(source):
    if 'PHOTO_AUDIT_BRIDGE_V' in source or 'Контроль товара до любых outbox' in source:
        raise ValueError("Интеграция уже присутствует")
    bootstrap = '''
# PHOTO_AUDIT_BRIDGE_V5: native composition, verified reference repair, two-pass audit.
AUDIT_ROOT = None
class PhotoAuditHold(RuntimeError):
    pass
class SourceUnavailable(RuntimeError):
    pass
_audit_settings = ROOT / "photo-audit.json"
if _audit_settings.exists():
    _audit_config = json.loads(_audit_settings.read_text(encoding="utf-8-sig"))
    if _audit_config.get("enabled") is True:
        AUDIT_ROOT = Path(_audit_config["project_root"]).resolve()
        if not (AUDIT_ROOT / "photo_auditor").is_dir():
            raise RuntimeError("Photo auditor project is unavailable")
        sys.path.insert(0, str(AUDIT_ROOT))
        from photo_auditor.integration import AuditHold as PhotoAuditHold, checked_output
        from photo_auditor.runtime import register_job, requires_audit, guard_upload
        from photo_auditor.progress import restore as restore_audit_output, starting as start_audit_generation, rejected as audit_generation_rejected
        from photo_auditor.layout import render_card as render_exact_product_card
        from photo_auditor.reference_asset import latest as verified_reference_asset
        from photo_auditor.research import search_product, SourceUnavailable
        from uuid import uuid4 as audit_uuid
        _original_product_research = process_research
        async def process_research(*args, **kwargs):
            return await search_product(_original_product_research, *args, **kwargs)
'''
    bind = '''                            if AUDIT_ROOT is not None:
                                register_job(AUDIT_ROOT, job_id, job_mode, job_brand, job_model, job_specs)
'''
    card_gate = '''                            if AUDIT_ROOT is not None and requires_audit(AUDIT_ROOT, job_id):
                                async def regenerate_with_audit(notes):
                                    # The audit identified a product distortion. Preserve the real asset.
                                    target = ROOT / "output" / f"audit_repair_{job_id}_{audit_uuid().hex[:8]}.png"
                                    try:
                                        return await render_exact_product_card(
                                            verified_reference_asset(AUDIT_ROOT, f"fotogen-{job_id}", job_brand, job_model) or tmp_input,
                                            target, job_brand, job_model, job_specs)
                                    except Exception as error:
                                        raise PhotoAuditHold("Не удалось собрать карточку с исходным фото") from error
                                output_path = await checked_output(
                                    AUDIT_ROOT, f"fotogen-{job_id}", job_brand, job_model,
                                    output_path, tmp_input, regenerate_with_audit,
                                    start_revision=restored_audit["revision"] if restored_audit else 0,
                                    repair_reference=True)
'''
    research_gate = '''                            if AUDIT_ROOT is not None and requires_audit(AUDIT_ROOT, job_id):
                                async def regenerate_research_with_audit(notes):
                                    nonlocal utp
                                    result_photo, utp = await process_research(
                                        job_brand, job_model, (job_specs or "") + "\\n\\n" + notes,
                                        cdp_url=CDP_URL,
                                        project_url=(LANE.project_url_for("research") if LANE else None) or None)
                                    return result_photo
                                if photo is None:
                                    raise PhotoAuditHold("Research did not return a product photo")
                                photo = await checked_output(
                                    AUDIT_ROOT, f"fotogen-{job_id}", job_brand, job_model,
                                    photo, None, regenerate_research_with_audit)
'''
    upload_guard = '''    if AUDIT_ROOT is not None:
        guard_upload(AUDIT_ROOT, job_id, output_path)
'''
    replay_guard = '''    if AUDIT_ROOT is not None:
        try:
            guard_upload(AUDIT_ROOT, job_id, output_path)
        except PhotoAuditHold:
            upload_outbox.quarantine(journal)
            async with httpx.AsyncClient(timeout=30, trust_env=False) as held:
                await held.post(f"{api_url}/api/fail/{job_id}", headers={"x-agent-token": VPS_API_TOKEN},
                                data={"error": "Фото удержано контролем; требуется разбор старого outbox"})
            log.warning("Фото %d удержано контролем; outbox сохранён отдельно", job_id)
            return
'''
    hold_card = '''                        except PhotoAuditHold as hold:
                            last_error = hold
                            output_path = None
                            break
'''
    quota_card = '''                            if AUDIT_ROOT is not None and requires_audit(AUDIT_ROOT, job_id):
                                audit_generation_rejected(AUDIT_ROOT, f"fotogen-{job_id}", job_brand, job_model)
'''
    missing_input = '''                if r.status_code == 404 and AUDIT_ROOT is not None:
                    try:
                        register_job(AUDIT_ROOT, job_id, job_mode, job_brand, job_model, job_specs)
                        frozen = restore_audit_output(AUDIT_ROOT, f"fotogen-{job_id}", job_brand, job_model)
                        if frozen and frozen.get("supplier"):
                            r = httpx.Response(200, content=frozen["supplier"].read_bytes(),
                                               request=httpx.Request("GET", f"{api_url}/api/input/{job_id}"))
                            log.info("Исходник %d восстановлен из снимка контроля", job_id)
                    except PhotoAuditHold as hold:
                        await client.post(f"{api_url}/api/fail/{job_id}", headers=headers,
                                          data={"error": str(hold)})
                        continue
'''
    hold_research = '''                        except PhotoAuditHold as hold:
                            last_error = hold
                            break
                        except SourceUnavailable as unavailable:
                            last_error = unavailable
                            break
'''
    # Preserve a frozen output across an outage instead of paying for another initial render.
    anchor = '                            output_path = await process_one_file('
    begin = source.index(anchor)
    end_marker = '\n                            )'
    end = source.index(end_marker, begin) + len(end_marker)
    call = source[begin:end]
    card_start = bind + '''                            restored_audit = None
                            if AUDIT_ROOT is not None and requires_audit(AUDIT_ROOT, job_id):
                                restored_audit = restore_audit_output(
                                    AUDIT_ROOT, f"fotogen-{job_id}", job_brand, job_model)
                            if restored_audit:
                                output_path = restored_audit["path"]
                            elif AUDIT_ROOT is not None and requires_audit(AUDIT_ROOT, job_id):
                                # Product geometry is an asset, not something to redraw.
                                target = ROOT / "output" / f"product_card_{job_id}_{audit_uuid().hex[:8]}.png"
                                try:
                                    output_path = await render_exact_product_card(
                                        tmp_input, target, job_brand, job_model, job_specs)
                                except Exception as error:
                                    raise PhotoAuditHold("Не удалось собрать карточку с исходным фото") from error
                            else:
''' + '\n'.join('    ' + line for line in call.splitlines())
    source = source[:begin] + card_start + source[end:]
    replacements = [
        ('import upload_outbox  # noqa: E402\n', 'import upload_outbox  # noqa: E402\n' + bootstrap),
        ('                            photo, utp = await process_research(', bind + '                            photo, utp = await process_research('),
        ('                            log.info("Обработано → %s", output_path)', card_gate + '                            log.info("Обработано → %s", output_path)'),
        ('                            journal = upload_outbox.stage(\n                                OUTBOX_DIR, job_id, "research",', research_gate + '                            journal = upload_outbox.stage(\n                                OUTBOX_DIR, job_id, "research",'),
        ('async def _upload_result(api_url: str, job_id: int, output_path: Path) -> dict:\n',
         'async def _upload_result(api_url: str, job_id: int, output_path: Path) -> dict:\n' + upload_guard),
        ('    if output_path and not output_path.is_file():', replay_guard + '    if output_path and not output_path.is_file():'),
        ('                        except UploadLimitError as e:', hold_card + '                        except UploadLimitError as e:\n' + quota_card.rstrip()),
        ('                        except GenerationRateLimitError as e:', '                        except GenerationRateLimitError as e:\n' + quota_card.rstrip()),
        ('                if r.status_code == 404:', missing_input + '                if r.status_code == 404:'),
        ('                        except (UploadLimitError, GenerationRateLimitError) as e:', hold_research + '                        except (UploadLimitError, GenerationRateLimitError) as e:'),
    ]
    for anchor, replacement in replacements:
        if source.count(anchor) != 1:
            raise ValueError('Фотоагент изменился: нужна повторная сверка точки подключения: ' + anchor[:70])
        source = source.replace(anchor, replacement)
    compile(source, 'remote_agent.py', 'exec')
    return source


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", type=Path, default=DEFAULT_AGENT)
    args = parser.parse_args()
    original = args.agent.read_text(encoding="utf-8")
    result = patched(original)
    compile(result, str(args.agent), "exec")
    output = ROOT / "integrations/fotogen-photo-audit.patch"
    output.parent.mkdir(exist_ok=True)
    output.write_text("".join(difflib.unified_diff(original.splitlines(keepends=True), result.splitlines(keepends=True),
        fromfile="a/remote_agent.py", tofile="b/remote_agent.py")), encoding="utf-8")
    print(str(output))


if __name__ == "__main__":
    main()

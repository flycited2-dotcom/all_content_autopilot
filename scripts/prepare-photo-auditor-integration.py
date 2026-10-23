"""Prepare a reviewable patch; never edits the photo-agent checkout itself."""
import argparse
import difflib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AGENT = ROOT.parent / "agent_convert_foto_rituailb2b2_V2_office/agent_convert_foto_rituailb2b2/remote_agent.py"


def patched(source):
    if 'PHOTO_AUDIT_BRIDGE_V2' in source or 'Контроль товара до любых outbox' in source:
        raise ValueError("Интеграция уже присутствует")
    bootstrap = '''
# PHOTO_AUDIT_BRIDGE_V2: local settings contain no credentials.
AUDIT_ROOT = None
class PhotoAuditHold(RuntimeError):
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
'''
    bind = '''                            if AUDIT_ROOT is not None:
                                register_job(AUDIT_ROOT, job_id, job_mode, job_brand, job_model, job_specs)
'''
    card_gate = '''                            if AUDIT_ROOT is not None and requires_audit(AUDIT_ROOT, job_id):
                                async def regenerate_with_audit(notes):
                                    return await process_one_file(
                                        tmp_input, mode=job_mode,
                                        specs=(job_specs or "") + "\\n\\n" + notes,
                                        brand=job_brand, model=job_model, cdp_url=CDP_URL,
                                        project_url=(LANE.project_url_for(job_mode) if LANE else None) or None)
                                output_path = await checked_output(
                                    AUDIT_ROOT, f"fotogen-{job_id}", job_brand, job_model,
                                    output_path, tmp_input, regenerate_with_audit)
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
    hold_research = '''                        except PhotoAuditHold as hold:
                            last_error = hold
                            break
'''
    replacements = [
        ('import upload_outbox  # noqa: E402\n', 'import upload_outbox  # noqa: E402\n' + bootstrap),
        ('                            output_path = await process_one_file(', bind + '                            output_path = await process_one_file('),
        ('                            photo, utp = await process_research(', bind + '                            photo, utp = await process_research('),
        ('                            log.info("Обработано → %s", output_path)', card_gate + '                            log.info("Обработано → %s", output_path)'),
        ('                            journal = upload_outbox.stage(\n                                OUTBOX_DIR, job_id, "research",', research_gate + '                            journal = upload_outbox.stage(\n                                OUTBOX_DIR, job_id, "research",'),
        ('async def _upload_result(api_url: str, job_id: int, output_path: Path) -> dict:\n',
         'async def _upload_result(api_url: str, job_id: int, output_path: Path) -> dict:\n' + upload_guard),
        ('    if output_path and not output_path.is_file():', replay_guard + '    if output_path and not output_path.is_file():'),
        ('                        except UploadLimitError as e:', hold_card + '                        except UploadLimitError as e:'),
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

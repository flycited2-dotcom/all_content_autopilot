"""Prepare a reviewable patch; never edits the photo-agent checkout itself."""
import argparse
import difflib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AGENT = ROOT.parent / "agent_convert_foto_rituailb2b2_V2_office/agent_convert_foto_rituailb2b2/remote_agent.py"


def patched(source):
    gate = '''
                            # Контроль товара до любых outbox/Drive/VPS операций.
                            if os.environ.get("PHOTO_AUDIT_ENABLED") == "1":
                                from photo_auditor.integration import AuditHold, checked_output
                                audit_root = Path(os.environ["PHOTO_AUDIT_ROOT"])

                                async def regenerate_with_audit(notes):
                                    return await process_one_file(
                                        tmp_input, mode=job_mode,
                                        specs=(job_specs or "") + "\\n\\n" + notes,
                                        brand=job_brand, model=job_model, cdp_url=CDP_URL,
                                        project_url=(LANE.project_url_for(job_mode)
                                                     if LANE else None) or None,
                                    )

                                try:
                                    output_path = await checked_output(
                                        audit_root, f"fotogen-{job_id}", job_brand, job_model,
                                        output_path, tmp_input, regenerate_with_audit)
                                except AuditHold as hold:
                                    last_error = hold
                                    output_path = None  # нет выгрузки и повторной генерации на общем retry
                                    break
'''
    anchor = '                            log.info("Обработано → %s", output_path)'
    upload = 'async def _upload_result(api_url: str, job_id: int, output_path: Path) -> dict:\n'
    guard = '''    if os.environ.get("PHOTO_AUDIT_ENABLED") == "1":
        from photo_auditor.integration import require_permit
        require_permit(Path(os.environ["PHOTO_AUDIT_ROOT"]), f"fotogen-{job_id}", output_path)
'''
    if "Контроль товара до любых outbox" in source:
        raise ValueError("Интеграция уже присутствует")
    if source.count(anchor) != 1 or source.count(upload) != 1:
        raise ValueError("Фотоагент изменился: нужна повторная сверка точек подключения")
    if "import os" not in source:
        raise ValueError("Фотоагент не импортирует os")
    return source.replace(anchor, gate + anchor).replace(upload, upload + guard)


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

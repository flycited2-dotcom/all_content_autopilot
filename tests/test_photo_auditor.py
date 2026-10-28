import asyncio
import base64
import io
import json
import socket
import threading
from pathlib import Path

import httpx
import pytest
from PIL import Image

from photo_auditor import integration, service
from photo_auditor.engine import Auditor, CHECKS, checked_comparison, decision, feedback
from photo_auditor.images import digest, image_bytes
from photo_auditor.sources import PublicWeb, contains_model, official
from photo_auditor.store import AuditStore


def picture(color="white"):
    output = io.BytesIO()
    Image.new("RGB", (120, 80), color).save(output, "PNG")
    return output.getvalue()


def product(revision=0):
    return {"product_id": "fotogen-123", "revision": revision, "brand": "Midea", "model": "ABC-12-RU"}


def report(verdict="regenerate"):
    return {"verdict": verdict, "reason": "Корпус отличается", "checks": [], "sources": [], "dimensions_note": "Масштаба нет"}


def comparison(status="match"):
    return {"checks": [{"aspect": a, "status": status, "confidence": .95, "observation": "Признаки совпадают",
                        "fix": "Исправить форму" if status == "mismatch" else "", "source_ids": [1]} for a in CHECKS],
            "dimensions_note": "Физический размер по пикселям не определён"}


def test_queue_snapshot_idempotence_and_conflicting_image(tmp_path):
    store = AuditStore(tmp_path)
    data = picture()
    first = store.submit(product(), data)
    assert store.submit(product(), data)["id"] == first["id"]
    assert (tmp_path / first["id"] / "generated.bin").read_bytes() == data
    with pytest.raises(ValueError):
        store.submit(product(), picture("black"))
    with pytest.raises(ValueError):
        store.submit({**product(), "model": "ABC-13"}, data)
    assert store.status() == {"queued": 1}


def test_regeneration_requires_previous_decision_and_stops_after_two(tmp_path):
    store = AuditStore(tmp_path)
    with pytest.raises(ValueError):
        store.submit(product(1), picture())
    for revision in range(3):
        store.submit(product(revision), picture())
        job = store.claim()
        assert store.claim() is None
        store.finish(job, report())
        assert store.get(job["id"])["status"] == ("regenerate" if revision < 2 else "review")
    with pytest.raises(ValueError):
        store.submit(product(3), picture())


def test_recovered_lease_rejects_late_result(tmp_path):
    store = AuditStore(tmp_path)
    store.submit(product(), picture())
    old = store.claim(now=100)
    new = store.claim(now=1001)
    assert old["lease_token"] != new["lease_token"]
    assert not store.finish(old, report("approved"))
    assert store.finish(new, report("review"))


def test_provider_failure_defers_without_approval_and_finishes_as_review(tmp_path):
    store = AuditStore(tmp_path)
    store.submit(product(), picture())
    for now in (100, 500, 900):
        job = store.claim(now=now)
        store.defer(job, "provider unavailable", now=now)
    assert store.get(job["id"])["status"] == "review"


@pytest.mark.parametrize("bad", [{"revision": True}, {"revision": -1}, {"model": ""}, {"product_id": "../x"}])
def test_invalid_identity_or_revision_rejected(tmp_path, bad):
    with pytest.raises(ValueError):
        AuditStore(tmp_path).submit({**product(), **bad}, picture())


def test_fake_image_rejected():
    with pytest.raises(ValueError):
        image_bytes(b"not a photograph")


def test_exact_model_suffix_and_official_host_boundaries():
    assert contains_model("Midea ABC - 12 - RU, белый", "ABC-12-RU")
    assert not contains_model("Midea ABC-12-RU2", "ABC-12-RU")
    assert not contains_model("Midea ABC-12-RU-2", "ABC-12-RU")
    assert not contains_model("Midea ABC-12", "ABC-12-RU")
    assert official("https://www.midea.com/p", ["midea.com"])
    assert not official("https://midea.com.evil.example/p", ["midea.com"])


def test_no_official_source_cannot_approve_or_trigger_paid_regeneration():
    sources = [{"official": False}]
    assert decision(comparison(), sources)[0] == "review"
    assert decision(comparison("mismatch"), sources)[0] == "review"


def test_uncertainty_and_confirmed_difference_are_distinct():
    sources = [{"official": True, "model_image_verified": True}]
    value = comparison()
    assert decision(value, sources)[0] == "approved"
    value["checks"][0]["status"] = "not_verifiable"
    assert decision(value, sources)[0] == "review"
    value["checks"][0].update(status="mismatch", fix="Восстановить форму")
    assert decision(value, sources)[0] == "regenerate"
    value["checks"][0]["confidence"] = .7
    assert decision(value, sources)[0] == "review"


@pytest.mark.parametrize("change", [
    {"status": "ok"}, {"source_ids": [2]}, {"confidence": True}, {"confidence": 2},
    {"source_ids": []}, {"aspect": "price"}, {"observation": ""}, {"status": "mismatch", "fix": ""}])
def test_invalid_model_comparison_cannot_be_used(change):
    value = comparison()
    value["checks"][0].update(change)
    with pytest.raises(ValueError):
        checked_comparison(json.dumps(value), [{"official": True}])


def test_public_reference_requires_cited_exact_model_and_linked_image():
    data = picture()
    def handle(request):
        if request.url.path == "/p":
            return httpx.Response(200, text='<h1>Midea ABC-12-RU</h1><img src="/product.png">')
        return httpx.Response(200, content=data)
    client = httpx.Client(transport=httpx.MockTransport(handle))
    resolver = lambda *a, **k: [(socket.AF_INET, 0, 0, "", ("93.184.216.34", 443))]
    web = PublicWeb(client, resolver)
    candidate = {"url": "https://midea.com/p", "image_url": "https://midea.com/product.png"}
    source, photo, _ = web.reference(candidate, "ABC-12-RU", [candidate["url"]], ["midea.com"])
    assert source["official"] and photo == data
    # Discovery metadata may omit a URL; the real fetched model page is evidence.
    uncited, _, _ = web.reference(candidate, "ABC-12-RU", [], ["midea.com"])
    assert uncited['model_image_verified'] == source['model_image_verified']
    with pytest.raises(ValueError):
        web.reference(candidate, "ABC-12-RU2", [candidate["url"]], ["midea.com"])
    with pytest.raises(ValueError):
        web.reference({**candidate, "image_url": "https://midea.com/unrelated.png"}, "ABC-12-RU", [candidate["url"]], ["midea.com"])


def test_private_redirect_is_blocked_before_request():
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://127.0.0.1/secret"})
    resolver = lambda host, *a, **k: [(socket.AF_INET, 0, 0, "", ("127.0.0.1" if host == "127.0.0.1" else "93.184.216.34", 443))]
    web = PublicWeb(httpx.Client(transport=httpx.MockTransport(handle)), resolver)
    with pytest.raises(ValueError):
        web.fetch("https://midea.com/redirect", 1000)
    assert len(calls) == 1


def test_auditor_uses_actual_images_and_max_five_sources(tmp_path):
    root = Path(__file__).resolve().parents[1]
    store = AuditStore(tmp_path)
    job = store.submit(product(), picture())
    folder = tmp_path / job["id"]
    calls = []
    class Provider:
        def inspect(self, instruction, content, search=False, domains=()):
            calls.append((content, search, domains))
            if search:
                source = {"url": "https://midea.com/p", "image_url": "https://midea.com/product.png"}
                return {"text": json.dumps({"sources": [source] if domains else []}), "sources": [source["url"]]}
            return {"text": json.dumps({**comparison(), 'limitations': [], 'source_checks': [
                {'id': 1, 'main_product_visible': True, 'suitable_for_repair': True,
                 'reason': 'Основной товар виден.'}]}), "sources": []}
    class Web:
        def reference(self, candidate, model, cited, domains):
            return {**candidate, "official": True, "model_image_verified": True, "model": model, "text": "Midea ABC-12-RU"}, picture(), b"page"
    result = Auditor(root, Provider(), Web()).run(product(), folder)
    assert result["verdict"] == "approved"
    assert calls[0][2] == ["midea.com", "midearussia.ru"]
    assert len([c for c in calls[-1][0] if c["type"] == "input_image"]) == 2
    assert 'text' not in json.loads(calls[-1][0][0]['text'])['sources'][0]
    assert (folder / "comparison.jpg").is_file()
    assert result["sources"][0]["image_sha256"] == digest(picture())


def test_failed_snapshot_does_not_call_model(tmp_path):
    store = AuditStore(tmp_path)
    job = store.submit(product(), picture())
    (tmp_path / job["id"] / "generated.bin").write_bytes(picture("black"))
    assert service.process_one(store, object())
    assert store.get(job["id"])["status"] == "review"


def test_only_owner_can_submit_photos(tmp_path):
    store = AuditStore(tmp_path)
    class Tg:
        def send(self, *args):
            pytest.fail("stranger must not trigger response")
    service.handle_update({"message": {"from": {"id": 9}, "chat": {"id": 9}, "photo": [{}]}}, store, Tg(), owner=1)
    assert store.status() == {}


def test_local_hook_retries_with_feedback_and_releases_only_matching_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(integration, "load", lambda root: {"api_token": "test"})
    monkeypatch.setattr(integration, '_current_approval', lambda *args: True)
    path = tmp_path / "generated.png"
    path.write_bytes(picture())
    calls, prompts = [], []
    def handle(request):
        value = json.loads(request.content)
        calls.append(value)
        return httpx.Response(200, json={"id": "a", "product": value["product"],
            "sha256": digest(base64.b64decode(value["generated"])),
            "status": "regenerate" if len(calls) == 1 else "approved", "feedback": "Исправить ручку"})
    async def regenerate(notes):
        prompts.append(notes)
        path.write_bytes(picture("black"))
        return path
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await integration.checked_output(tmp_path, "job1", "Midea", "ABC-12-RU", path, None, regenerate, client=client)
            assert result == path
    asyncio.run(run())
    assert prompts == ["Исправить ручку"] and len(calls) == 2
    assert calls[1]["product"]["revision"] == 1
    integration.require_permit(tmp_path, "job1", path)
    path.write_bytes(picture("blue"))
    with pytest.raises(integration.AuditHold):
        integration.require_permit(tmp_path, "job1", path)


def test_regeneration_does_not_restart_overall_audit_time_budget(monkeypatch, tmp_path):
    from types import SimpleNamespace
    now = [0]
    monkeypatch.setattr(integration, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(integration, 'load', lambda root: {'api_token': 'test'})
    path = tmp_path / 'generated.png'
    path.write_bytes(picture())
    submitted = []
    def handle(request):
        value = json.loads(request.content)
        submitted.append(value)
        now[0] = 4
        return httpx.Response(200, json={'id': 'a', 'product': value['product'],
            'sha256': digest(base64.b64decode(value['generated'])),
            'status': 'regenerate', 'feedback': 'Correct the display'})
    async def regenerate(notes):
        now[0] = 11
        path.write_bytes(picture('black'))
        return path
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(integration.AuditHold, match='общий срок'):
                await integration.checked_output(tmp_path, 'job1', 'Midea', 'ABC-12-RU',
                                                  path, None, regenerate, timeout=10, client=client)
    asyncio.run(run())
    assert len(submitted) == 1
    with pytest.raises(integration.AuditHold):
        integration.require_permit(tmp_path, 'job1', path)


def test_weak_reference_from_related_product_cannot_approve():
    assert decision(comparison(), [{"official": True, "model_image_verified": False}])[0] == "review"


def test_structured_product_images_match_exact_model_only():
    from photo_auditor.sources import Page
    page = Page()
    page.feed('<h1>ABC-12-RU</h1><script type="application/ld+json">' + json.dumps([
        {"@type": "Product", "model": "ABC-12-RU", "image": "/exact.png"},
        {"@type": "Product", "model": "ABC-12-RU-2", "image": "/other.png"}]) + '</script>')
    assert page.product_images("ABC-12-RU") == {"/exact.png"}


def test_related_models_in_body_do_not_validate_main_product_photo():
    from photo_auditor.sources import Page
    page = Page()
    page.feed('<h1>Midea ABC-12-RU-2</h1><meta property="og:image" content="/other.png">'
              '<div>Рекомендуем: Midea ABC-12-RU</div>')
    assert not page.product_images("ABC-12-RU")
    assert page.product_images("ABC-12-RU-2") == {"/other.png"}


@pytest.mark.parametrize("status", ["review", "unexpected", "approved"])
def test_hook_cannot_release_manual_or_wrong_image(monkeypatch, tmp_path, status):
    monkeypatch.setattr(integration, "load", lambda root: {"api_token": "test"})
    path = tmp_path / "generated.png"
    path.write_bytes(picture())
    def handle(request):
        value = json.loads(request.content)
        return httpx.Response(200, json={"id": "a", "product": value["product"], "sha256": "wrong", "status": status})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(integration.AuditHold):
                await integration.checked_output(tmp_path, "job1", "Midea", "ABC-12-RU", path, None, None, client=client)
    asyncio.run(run())


def test_api_auth_and_duplicate_ingestion(tmp_path):
    store = AuditStore(tmp_path)
    server = service.make_server(store, "token", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with httpx.Client(base_url=url, trust_env=False) as client:
            assert client.get("/status").status_code == 401
            headers = {"X-Audit-Token": "token"}
            body = {"product": product(), "generated": base64.b64encode(picture()).decode()}
            first = client.post("/audits", json=body, headers=headers)
            assert first.status_code == 200
            assert client.post("/audits", json=body, headers=headers).json()["id"] == first.json()["id"]
            body["generated"] = base64.b64encode(b"broken").decode()
            assert client.post("/audits", json=body, headers=headers).status_code == 400
    finally:
        server.shutdown()
        server.server_close()

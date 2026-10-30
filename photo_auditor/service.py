"""Loopback API, audit worker and owner-only Telegram interface."""
import base64
import hmac
import json
import logging
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from listener.provider_errors import ProviderError
from orchestrator_bot.api import text_chunks
from photo_auditor.engine import feedback
from photo_auditor.content_review import ContentReviewError
from photo_auditor.store import sanitize_provider_error
from photo_auditor.integration import AuditHold

log = logging.getLogger("photo_auditor")
MAX_BODY = 29 * 1024 * 1024
LABELS = {"approved": "✅ Соответствие изображения товара проверено", "regenerate": "🔄 Требуется исправить изображение товара", "review": "🔎 Нужна ручная проверка товара"}
ADVISORY_LABELS = {"approved": LABELS["approved"], "regenerate": "💡 Аудитор нашёл расхождение с эталоном",
                   "review": "💡 Замечания аудитора — фото не задержано"}


def public_result(audit, root=None):
    report = audit["report"]
    status = audit['status']
    if root is not None and status == 'queued' and audit.get('retry_at', 0) > time.time():
        from photo_auditor.mode import advisory
        if advisory(root):
            # An explicitly deferred read-only check continues in the background.
            # Existing advisory clients pass on this same file, without approval
            # or another render; gate clients retain the queued contract.
            status = 'deferred'
    return {"id": audit["id"], "status": status, "sha256": audit["sha256"],
            "product": audit["product"], "report": report, "feedback": feedback(report) if report else None,
            "provider_error": audit.get("provider_error"), "retry_at": audit.get("retry_at", 0)}


def provider_reason(diagnostic):
    """Explain this adapter's failure without claiming the user's ChatGPT is down."""
    diagnostic = sanitize_provider_error(diagnostic) or {}
    if diagnostic.get('code') == 'subscription_sharing_usage_limit_exceeded':
        return ('Программное подключение аудитора отклонило запрос по ограничению использования '
                '(subscription_sharing_usage_limit_exceeded). Это не проверка доступности '
                'вашего генератора в ChatGPT. Доступ приложения: https://chatgpt.com/settings/usage')
    return {
        'usage_limit': 'Программное подключение аудитора отклонило запрос по ограничению использования.',
        'network': 'Программное подключение аудитора не завершило сетевой запрос.',
        'rate_limit': 'Программное подключение аудитора временно ограничило частоту запросов.',
        'auth': 'Требуется проверить вход программного подключения аудитора.',
        'configuration': 'Требуется проверить разрешения программного подключения аудитора.',
        'invalid_response': 'Программное подключение аудитора не вернуло корректный завершённый ответ.',
    }.get(diagnostic.get('kind'), 'Причина прежнего ожидания не сохранена; доступность вашего генератора в ChatGPT не проверялась.')


def waiting_text(row):
    delay = max(0, int(row.get('retry_at', 0) - time.time()))
    return (f"⏳ {row['product']['brand']} {row['product']['model']}: фото сохранено. "
            f"{provider_reason(row.get('provider_error'))}\n"
            f"Повтор проверки этого файла — примерно через {(delay + 59) // 60} мин. "
            f"Повторить сейчас: /retry {row['id']}\n"
            "Новая генерация не запускается. Выгрузка ожидает завершения проверки.")


def status_text(store):
    counts = store.status()
    text = 'Контроль фотографий: ' + (', '.join(f'{k}: {v}' for k, v in counts.items()) or 'очередь пуста')
    waiting = store.waiting()
    if waiting:
        text += '\n\n' + '\n\n'.join(waiting_text(row) for row in waiting)
    return text


def make_server(store, token, port=8793):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_json(self, code, value):
            data = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            if not hmac.compare_digest(self.headers.get("X-Audit-Token", ""), token):
                self.send_json(401, {"error": "unauthorized"})
                return False
            return True

        def do_GET(self):
            if not self.authorized():
                return
            if self.path == '/health':
                return self.send_json(200, worker_health(server.audit_workers, store))
            if self.path == "/status":
                return self.send_json(200, store.status())
            if self.path.startswith("/audits/"):
                row = store.get(self.path.removeprefix("/audits/"))
                if row:
                    store.consumer_waiting(row['id'])
                return self.send_json(200 if row else 404, public_result(row, store.root.parent.parent) if row else {"error": "not found"})
            self.send_json(404, {"error": "not found"})

        def do_POST(self):
            if not self.authorized():
                return
            if self.path != "/audits":
                return self.send_json(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY:
                    return self.send_json(413, {"error": "body too large"})
                value = json.loads(self.rfile.read(length))
                if not isinstance(value, dict) or set(value) - {"product", "generated", "supplier"}:
                    raise ValueError()
                generated = base64.b64decode(value["generated"], validate=True)
                supplier = base64.b64decode(value["supplier"], validate=True) if value.get("supplier") else None
                audit = store.submit(value["product"], generated, supplier)
                store.consumer_waiting(audit['id'])
                if audit['status'] == 'approved':
                    from photo_auditor.runtime import approval_covers_brief
                    if not approval_covers_brief(store.root.parent.parent, audit['product'], audit['report']):
                        store.recheck(audit['id'])
                        audit = store.get(audit['id'])
            except (ValueError, TypeError, KeyError):
                return self.send_json(400, {"error": "Invalid product, revision or image; snapshot conflict"})
            self.send_json(200, public_result(audit, store.root.parent.parent))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.audit_workers = []
    return server


def process_one(store, auditor, audit_id=None):
    row = store.claim(audit_id=audit_id)
    if row is None:
        return False
    lease_stop = threading.Event()
    def keep_lease():
        while not lease_stop.wait(30):
            try:
                if not store.renew_lease(row):
                    return
            except Exception:
                log.warning('Продление проверки %s отложено', row['id'][:12])
    lease_worker = threading.Thread(target=keep_lease, daemon=True)
    lease_worker.start()
    try:
        path = store.root / row["id"]
        from photo_auditor.images import digest
        if digest((path / "generated.bin").read_bytes()) != row["sha256"]:
            store.finish(row, {"verdict": "review", "reason": "Снимок изменился после постановки проверки.",
                               "checks": [], "sources": [], "dimensions_note": "Проверка остановлена."})
        else:
            store.finish(row, auditor.run(row["product"], path))
    except ContentReviewError as exc:
        store.defer(row, 'Контроль содержания не завершён: ' + str(exc))
    except ProviderError as exc:
        diagnostic = sanitize_provider_error({'provider': exc.provider, 'kind': exc.kind, 'code': exc.code})
        reason = provider_reason(diagnostic)
        log.warning("Программное подключение аудитора: %s; %s", exc.kind, reason)
        if exc.kind in ('auth', 'configuration'):
            store.finish(row, {'verdict': 'review', 'reason': reason,
                              'checks': [], 'sources': [], 'dimensions_note': 'Проверка не завершена.'})
            return True
        delay = 3600 if exc.kind == 'usage_limit' else 300
        store.defer(row, reason, retry_at=exc.retry_at or time.time() + delay,
                    provider_wait=exc.kind in ('usage_limit', 'network', 'rate_limit'),
                    provider_error=diagnostic)
    except Exception as exc:
        log.warning("Проверка не завершена: %s", type(exc).__name__)
        store.defer(row, "Проверка источников/изображений не завершена: " + type(exc).__name__)
    finally:
        lease_stop.set()
        lease_worker.join(timeout=1)
    return True


def worker_loop(store, auditor, stop, health=None):
    """A transient queue/lease error must not silently remove an inspector."""
    health = health if health is not None else {}
    failures = 0
    try:
        while not stop.is_set():
            health.update(last_poll=time.time(), state='working')
            try:
                processed = process_one(store, auditor)
            except Exception as exc:
                failures += 1
                # Exception messages can contain connection details or source
                # data. The category is enough for safe operational diagnosis.
                category = type(exc).__name__
                log.warning('Поток фотоаудитора восстановится после ошибки очереди: %s', category)
                health.update(state='backoff', consecutive_errors=failures, last_error=category)
                stop.wait(min(30, 2 ** min(failures, 5)))
                continue
            failures = 0
            health.update(last_success=time.time(), consecutive_errors=0, last_error=None,
                          state='working' if processed else 'idle')
            if not processed:
                stop.wait(2)
    finally:
        health['state'] = 'stopped'


def worker_health(workers, store, now=None):
    """Observe the actual pool, not merely a listening HTTP socket."""
    now = time.time() if now is None else now
    details = []
    for worker in workers:
        health = dict(getattr(worker, 'audit_health', {}))
        polled = health.get('last_poll')
        details.append({'alive': worker.is_alive(), 'state': health.get('state', 'starting'),
                        'last_poll_age': max(0, now - polled) if isinstance(polled, (int, float)) else None,
                        'consecutive_errors': health.get('consecutive_errors', 0),
                        'last_error': health.get('last_error')})
    alive = sum(row['alive'] for row in details)
    return {'ok': bool(workers) and alive == len(workers),
            'workers': {'expected': len(workers), 'alive': alive, 'details': details},
            'queue': store.status()}


def start_workers(store, auditor_factory, stop, count=2):
    if type(count) is not int or not 1 <= count <= 2:
        raise ValueError('Допустима одна или две параллельные проверки')
    workers = []
    for _ in range(count):
        health = {'state': 'starting', 'consecutive_errors': 0, 'last_error': None}
        worker = threading.Thread(target=worker_loop, args=(store, auditor_factory(), stop, health), daemon=True)
        worker.audit_health = health
        worker.start()
        workers.append(worker)
    return workers


def watch_parent(parent, stop):
    """Task Scheduler stops conhost, so its Python child must stop itself."""
    from listener.lifecycle import parent_alive
    while not stop.wait(2):
        if not parent_alive(parent):
            log.info('Родительский процесс завершён — фотоаудитор останавливается')
            stop.set()


def notify(store, tg, owner):
    for row in store.waiting_notifications():
        tg.send(owner, waiting_text(row))
        store.notified(row['id'])
    for row in store.pending_notifications():
        if store.latest(row['product_id'])['id'] != row['id']:
            store.notified(row['id'])
            continue
        generated_job = re.fullmatch(r'fotogen-([1-9][0-9]*)', row['product_id'])
        from photo_auditor.integration import released
        from photo_auditor.mode import advisory
        root = store.root.parent.parent
        advisory_mode = advisory(root)
        # In advisory mode the file may already be on its way to the preview:
        # its notes are then advice for the owner, not an internal repair step.
        passed_on = bool(advisory_mode and generated_job and released(root, row['product_id'], row['sha256']))
        intermediate = bool(generated_job and row['status'] == 'regenerate' and not passed_on)
        if generated_job and row['status'] == 'approved':
            from photo_auditor import runtime
            root = store.root.parent.parent
            job_id = int(generated_job[1])
            scope_path = root / 'runs/photo-auditor/job-scope' / (str(job_id) + '.json')
            if scope_path.exists():
                intermediate = runtime._read(root, job_id).get('mode') == 'research'
        if intermediate:
            # The generator receives this feedback over the API. Do not flood the
            # owner with intermediate revisions that are already being corrected.
            store.notified(row['id'])
            continue
        report = row["report"]
        managed = bool(report.get('content_review'))
        folder = store.root / row["id"]
        from photo_auditor.images import digest, image_bytes
        snapshot = folder / 'generated.bin'
        image_problem = None
        try:
            data = image_bytes(snapshot.read_bytes())
            if digest(data) != row['sha256']:
                raise ValueError('Changed snapshot')
        except (OSError, ValueError):
            image_problem = 'Фото отсутствует или повреждено; визуальную оценку запросить нельзя.'
        if image_problem is None:
            tg.send_photo(owner, snapshot, caption=(
                f"Карточка для проверки: {row['product']['brand']} {row['product']['model']}\n"
                f"Ревизия: {row['revision']}. Проверка: {row['id'][:12]}"))
        sheet = folder / "comparison.jpg"
        if image_problem is None and sheet.exists():
            tg.send_photo(owner, sheet, caption=f"{row['product']['brand']} {row['product']['model']}: GENERATED / SOURCE")
        human = bool(report.get('owner_approval'))
        label = ('✅ Результат подтверждён владельцем' if human else
                 '✅ Карточка прошла контент-контроль' if managed and row['status'] == 'approved' else
                 ADVISORY_LABELS[row['status']] if advisory_mode else LABELS[row['status']])
        scope_note = ('Результат принят владельцем; автоматическое заключение сохранено в истории. '
                      'Публикация — по решению владельца партии.' if human else
                      'Проверены товар, надписи, факты, читаемость и компоновка по исходному заданию. '
                      'Публикацию подтверждает владелец.' if managed and row['status'] == 'approved' else
                      ('Фото не задержано: карточка передана дальше в превью с этими замечаниями. '
                       'Решение — за вами: опубликовать как есть или отправить на исправление.'
                       if passed_on or not generated_job else
                       'Аудитор советует посмотреть эти замечания до публикации.')
                      if advisory_mode and row['status'] != 'approved' else
                      'Автоматическое одобрение не выдано. Публикация не разрешена этим отчётом.'
                      if row['status'] != 'approved' else
                      'Этот результат подтверждает только соответствие изображения товара. '
                      'Оформление карточки и пригодность для Telegram или ВК требуют отдельного согласования. '
                      'Публикацию подтверждает владелец.')
        text = (f"{label}\n{row['product']['brand']} {row['product']['model']}\n"
                f"Ревизия: {row['revision']}. Проверка: {row['id'][:12]}\n\n{feedback(report)}\n\n"
                f"Размеры: {report['dimensions_note']}\n\n"
                f"{scope_note}")
        parts = list(text_chunks(text))
        keyboard = [[{'text': title, 'callback_data': f"qa:{code}:{row['id']}"}]
                    for title, code in [('✅ Принять без публикации', 'approve'),
                                        ('🔎 Перепроверить фото', 'recheck')]]
        if generated_job:
            keyboard.append([{'text': '✏️ Указать ошибку', 'callback_data': f"qf:begin:{row['id']}"}])
        from photo_auditor.runtime import content_brief
        try:
            publishable=content_brief(store.root.parent.parent,row['product']) is not None
        except (AuditHold,OSError,ValueError):
            publishable=False
        with store.connect() as db:
            route = db.execute('SELECT value FROM meta WHERE key=?', ('photo-review-route:' + row['id'],)).fetchone()
        if route and json.loads(route[0]).get('supports_publication') is False:
            publishable = False
        if publishable:
            keyboard.insert(0,[{'text':'✅ Подтвердить и опубликовать в канал','callback_data':f"qa:publish:{row['id']}"}])
        if image_problem is None:
            parts[-1] += ('\n\nПодтверждение принимает именно приложенный файл от вашего имени. '
                          '«Опубликовать в канал» также запрашивает его публикацию после доставки; '
                          '«Без публикации» оставляет результат в группе проверки. Новая генерация не запускается.')
        else:
            parts[-1] += '\n\n' + image_problem
            keyboard = None
        # Keep the extra footer within Telegram's limit even for long source lists.
        chunks = list(text_chunks('\n'.join(parts)))
        for n, part in enumerate(chunks):
            tg.send(owner, part, keyboard if n == len(chunks) - 1 else None)
        store.notified(row["id"])


REVIEW_RESUME_PAUSE = 600
_review_resume_paused_until = 0.0


def recover_pipeline(store, deliver=None):
    """Only the latest decision can resume a failed card; the server deduplicates it."""
    if deliver is None:
        token = os.environ.get('ORCH_API_TOKEN')
        if not token:
            return
        import httpx
        from listener.client import ApiClient
        with httpx.Client(timeout=20, trust_env=False) as http:
            api = ApiClient(os.environ.get('ORCH_API_URL', 'http://127.0.0.1:8790'), token, http=http)
            return recover_pipeline(store, lambda payload: api._post('/photo-audit-result', payload))
    global _review_resume_paused_until
    from photo_auditor.mode import advisory
    include_review = advisory(store.root.parent.parent) and time.time() >= _review_resume_paused_until
    # Approved/regenerate decisions go first: a server that does not yet accept
    # advisory `review` resumptions must not delay the decisions it does accept.
    rows = sorted(store.recovery_results(include_review=include_review), key=lambda r: r['status'] == 'review')
    for row in rows:
        match = re.fullmatch(r'fotogen-([1-9][0-9]*)', row['product_id'])
        if not match:
            continue
        # Research can resume only together with its frozen, verified facts.
        from photo_auditor import runtime, progress
        root = store.root.parent.parent
        scope_path = root / 'runs/photo-auditor/job-scope' / (match[1] + '.json')
        if not scope_path.exists():
            continue
        try:
            scope = runtime._read(root, int(match[1]))
            if scope.get('mode') == 'research' and not progress.restore_research(
                    root, row['product_id'], row['product']['brand'], row['product']['model']):
                continue
            if row['status'] == 'approved' and not runtime.approval_covers_brief(root, row['product'], row['report']):
                continue
        except Exception:
            log.warning('Восстановление задания %s отложено: не подтверждено исходное задание', match[1])
            continue
        payload = {'job_id': int(match[1]), 'audit_id': row['id'], 'attempt': row['attempt'],
                   'revision': row['revision'], 'status': row['status'], 'sha256': row['sha256'],
                   'brand': row['product']['brand'], 'model': row['product']['model']}
        try:
            result = deliver(payload)
            if result.get('handled'):
                store.recovery_delivered(row['id'], row['attempt'], result)
        except Exception as exc:
            if row['status'] == 'review':
                _review_resume_paused_until = time.time() + REVIEW_RESUME_PAUSE
                log.warning('Возврат удержанных фото отложен на %d мин: %s (сервер оркестратора ещё не обновлён?)',
                            REVIEW_RESUME_PAUSE // 60, type(exc).__name__)
            else:
                log.warning('Доставка решения в конвейер отложена: %s', type(exc).__name__)
            break


def handle_update(update, store, tg, owner, *, transcriber=None):
    callback = update.get('callback_query')
    if callback:
        if callback.get('from', {}).get('id') != owner or callback.get('message', {}).get('chat', {}).get('id') != owner:
            return
        from photo_auditor.owner_feedback import handle_callback
        if handle_callback(callback, store, tg, owner):
            return
        data = callback.get('data', '').split(':')
        if len(data) == 3 and data[0] == 'qa' and re.fullmatch('[a-f0-9]{32}', data[2]):
            # Acknowledge before recovery/network work; a late Telegram callback
            # acknowledgement must not undo an already persisted owner decision.
            try:
                tg.answer_callback(callback['id'], 'Обрабатываю решение…')
            except Exception:
                pass
            try:
                if data[1] in ('approve','match','publish'):
                    if data[1] == 'publish':
                        with store.connect() as db:
                            route = db.execute('SELECT value FROM meta WHERE key=?', ('photo-review-route:' + data[2],)).fetchone()
                        if route and json.loads(route[0]).get('supports_publication') is False:
                            raise ValueError('Это самостоятельное фотозадание: результат доставляет фотоагент; кнопка публикации контент-завода к нему не относится.')
                    result = store.owner_approve(data[2], owner, callback['id'],publish=data[1]=='publish')
                    product=result['product']
                    message=(f"✅ Подтверждено: {product['brand']} {product['model']}.\n"
                             'Сохранённый файл принят и передан для продолжения исходного задания. '
                             'Новая генерация не запускается. '+
                             ('Публикация в канал запрошена; бот сообщит о результате.' if data[1]=='publish' else
                              'Принято без публикации; готовая карточка останется в группе проверки.'))
                elif data[1]=='recheck':
                    row=store.get(data[2])
                    if not row or store.latest(row['product_id'])['id']!=row['id']:
                        raise ValueError('Это старая фотография. Откройте последнюю ревизию в аудиторе.')
                    accepted = store.retry_waiting(row['id']) if row['status']=='waiting_provider' else store.recheck(row['id'])
                    if not accepted:
                        raise ValueError('Проверка уже выполняется или исправление уже передано генератору.')
                    store.consumer_waiting(row['id'])
                    message='🔎 Сохранённое фото поставлено на повторную проверку. Чтобы объяснить ошибку и заказать исправление карточки, нажмите «Указать ошибку» под её последним отчётом.'
                else:
                    saved=store.feedback(data[2],data[1])
                    if not saved:raise ValueError('Проверка не найдена')
                    message='Оценка сохранена. Для действия используйте «Принять без публикации», «Перепроверить фото» или «Указать ошибку» под последним фото.'
            except (ValueError,OSError,AuditHold) as exc:
                message='Решение не применено: '+str(exc)
                old=store.get(data[2])
                latest=store.latest(old['product_id']) if old else None
                if latest and latest['id']!=old['id'] and latest['report']:
                    with store.connect() as db:
                        db.execute('UPDATE audits SET notified=0 WHERE id=?',(latest['id'],))
            keyboard = None
            if data[1] == 'recheck':
                row = store.get(data[2])
                if row:
                    latest = store.latest(row['product_id'])
                    keyboard = [[{'text': '✏️ Указать ошибку', 'callback_data': 'qf:begin:' + latest['id']}]]
            tg.send(owner, message, keyboard)
            try:
                tg.clear_buttons(owner,callback['message']['message_id'])
            except Exception:
                pass
        return
    message = update.get("message") or {}
    if message.get("from", {}).get("id") != owner or message.get("chat", {}).get("id") != owner:
        return
    from photo_auditor.owner_feedback import handle_message
    if handle_message(message, store, tg, owner, transcriber=transcriber):
        return
    text = (message.get("text") or "").strip()
    if text in ("/start", "/help"):
        from photo_auditor.mode import settings
        mode = settings(store.root.parent.parent)
        if mode['mode'] == 'advisory':
            tg.send(owner, "Я помощник контент-завода: проверяю фото по точной номенклатуре и присылаю замечания, "
                           "но не останавливаю конвейер. Подтверждённое расхождение — исправлений генератором: "
                           f"{mode['repairs']}; "
                           "сомнения и сбои — фото идёт в превью с пометкой, публикуете вы. "
                           "Отправьте фото с подписью «Бренд | Модель» для ручной проверки. /status — очередь. "
                           "«Указать ошибку» под карточкой принимает текст или голосовое замечание; "
                           "новая генерация — только после отдельного подтверждения. /fix — выбрать карточку для исправления.")
            return
        tg.send(owner, "Я проверяю фотографии по точной номенклатуре. Отправьте фото с подписью «Бренд | Модель» "
                       "или подключите фотоагент. /status — очередь и причины ожидания. "
                       "/retry <id> — повторить ожидающую проверку того же файла. При замечаниях — до двух повторных генераций; "
                       "при нехватке эталонов — ручная проверка.")
    elif text == "/status":
        for part in text_chunks(status_text(store)):
            tg.send(owner, part)
        tg.send(owner, 'Для исправления с объяснением выберите карточку:',
                [[{'text': '✏️ Указать ошибку в карточке', 'callback_data': 'qf:list:all'}]])
    elif text in ('/fix', '/feedback'):
        from photo_auditor.owner_feedback import show_cards
        show_cards(store, tg, owner)
    elif text.split(maxsplit=1)[0:1] == ['/retry']:
        parts = text.split()
        if len(parts) != 2 or not re.fullmatch('[a-f0-9]{32}', parts[1]):
            tg.send(owner, 'Команда: /retry <полный id проверки из /status>. Повторяется только проверка сохранённого фото.')
            return
        try:
            accepted = store.retry_waiting(parts[1])
        except (OSError, ValueError):
            tg.send(owner, 'Снимок не прошёл проверку целостности; повтор остановлен.')
            return
        tg.send(owner, 'Повтор проверки сохранённого фото поставлен в очередь. Новая генерация не запускается.'
                if accepted else 'Ожидающая проверка не найдена или уже запущена. Проверьте /status.')
    elif message.get("photo") or message.get("document"):
        caption = (message.get("caption") or "").strip().split("|", 1)
        if len(caption) != 2 or not all(s.strip() for s in caption):
            tg.send(owner, "Нужна точная номенклатура: отправьте фото с подписью «Бренд | Модель».")
            return
        media = message["photo"][-1] if message.get("photo") else message["document"]
        if media.get("file_size", 0) > 10 * 1024 * 1024:
            tg.send(owner, "Фото должно быть не больше 10 МБ.")
            return
        photo = tg.download_voice(media["file_id"])  # same Telegram getFile transport, any media
        product = {"product_id": f"telegram-{message['message_id']}", "revision": 0,
                   "brand": caption[0].strip(), "model": caption[1].strip()}
        try:
            row = store.submit(product, photo)
        except ValueError:
            tg.send(owner, "Не удалось принять фото: проверьте формат PNG/JPEG/WEBP и номенклатуру.")
            return
        tg.send(owner, f"Принял на проверку: {product['brand']} {product['model']}. № {row['id'][:12]}")
    else:
        tg.send(owner, "Отправьте фото с подписью «Бренд | Модель». /help — порядок проверки.")


def run_bot(store, tg, owner, stop):
    while not stop.is_set():
        try:
            for update in tg.get_updates(store.offset(), timeout=20):
                handle_update(update, store, tg, owner)
                store.offset(update["update_id"] + 1)
        except Exception as exc:
            # Telegram HTTP errors contain credential-bearing URLs; never log them.
            log.warning("Telegram временно недоступен: %s", type(exc).__name__)
            stop.wait(10)


def maintenance_loop(store, tg, owner, stop):
    """Slow recovery/photo delivery cannot block the owner's Telegram buttons."""
    from photo_auditor.owner_feedback import recover_owner_feedback
    from photo_auditor.review_sync import sync
    while not stop.is_set():
        stages = [('Синхронизация карточек', lambda: sync(store)),
                  ('Отправка замечаний', lambda: recover_owner_feedback(store, tg=tg, owner=owner)),
                  ('Возврат заданий', lambda: recover_pipeline(store)),
                  ('Подтверждённая публикация', lambda: recover_publications(store, tg=tg, owner=owner)),
                  ('Доставка отчётов', lambda: notify(store, tg, owner))]
        for name, action in stages:
            try:
                action()
            except Exception as exc:
                log.warning('%s отложена: %s', name, type(exc).__name__)
        stop.wait(3)


def recover_publications(store, deliver=None, *, tg=None, owner=None):
    if deliver is None:
        token=os.environ.get('ORCH_API_TOKEN')
        if not token:return
        from listener.client import ApiClient
        import httpx
        with httpx.Client(timeout=20,trust_env=False) as http:
            api=ApiClient(os.environ.get('ORCH_API_URL','http://127.0.0.1:8790'),token,http=http)
            return recover_publications(store,lambda p:api._post('/photo-audit-publish',p),tg=tg,owner=owner)
    from photo_auditor import owner_decisions
    for request in store.pending_publications():
        row=store.get(request['audit_id'])
        if not row or store.latest(row['product_id'])['id']!=row['id']:
            store.publication_finished(request['audit_id'],{'cancelled':True,'reason':'superseded'});continue
        report=row['report'] or {}
        if row['status']!='approved' or not owner_decisions.valid(store.root.parent.parent,row['product'],report):
            store.publication_finished(row['id'],{'cancelled':True,'reason':'approval_revoked'});continue
        approval=report['owner_approval']['payload']
        if not approval['publish_requested'] or owner is not None and approval['actor_id']!=owner:
            store.publication_finished(row['id'],{'cancelled':True,'reason':'publication_not_authorized'});continue
        match=re.fullmatch(r'fotogen-([1-9][0-9]*)',row['product_id'])
        if not match:continue
        payload={'job_id':int(match[1]),'audit_id':row['id'],'attempt':0,'revision':row['revision'],
                 'status':'approved','sha256':row['sha256'],'brand':row['product']['brand'],'model':row['product']['model']}
        try:
            result=deliver(payload)
        except Exception as exc:
            log.warning('Публикация подтверждённой карточки отложена: %s',type(exc).__name__);continue
        if result.get('handled'):
            store.publication_finished(row['id'],result)
            if tg is not None:
                label='✅ Опубликовано в канале' if result.get('published') else '⚠️ Публикация остановлена'
                tg.send(owner,f"{label}: {row['product']['brand']} {row['product']['model']}."+
                        ('' if result.get('published') else ' Причина: '+str(result.get('reason','проверьте статус задания'))))

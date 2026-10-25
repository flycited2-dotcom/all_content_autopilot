"""Preserve complete catalog keys, including commas in old supplier fields."""
import json


def keys_from_json(text):
    value = json.loads(text or 'null')
    if (not isinstance(value, list) or not 1 <= len(value) <= 1000 or
            any(not isinstance(k, str) or not 0 < len(k) <= 2000 for k in value) or
            len(value) != len(set(value))):
        raise ValueError('Нужен список полных ключей товаров')
    return value


def current_decision(item, decision, selected_at):
    """A previous campaign's approval/publication does not decide a new card."""
    return (decision if item and item.status in ('preview', 'published') and decision and
            decision.ts is not None and selected_at is not None and decision.ts >= selected_at else None)


def run(cf, args):
    from content_factory.orchestrator.confirm_store import ConfirmStore
    from content_factory.orchestrator.excel_pipeline import ExcelStore
    keys = keys_from_json(args.keys_json)
    state, _, queue = cf._paths(args)
    awaiting, excel = ConfirmStore(state), ExcelStore(state)
    with excel._c() as db:
        selected = dict(db.execute('SELECT key,ts FROM excel_items'))
    def decision_for(key):
        return current_decision(excel.get(key), awaiting.get(key), selected.get(key))
    if args.command == 'publish-keys':
        items = [item for key in keys if (item := decision_for(key)) is not None and item.status == 'pending']
        return cf._publish_batch(state, items)
    if args.command == 'cancel':
        from content_factory.bot.run import make_cancel_excel_fn
        before = {k for k in keys if (item := excel.get(k)) is not None and item.status != 'cancelled'}
        cancel = make_cancel_excel_fn(state, queue)
        texts = [cancel(k) for k in keys]
        cancelled = {k for k in before if (item := excel.get(k)) is not None and item.status == 'cancelled'}
        def status(key):
            item = excel.get(key)
            decision = decision_for(key)
            return decision.status if decision and decision.status in ('rejected', 'published') else item.status if item else 'missing'
        return {'cancelled': len(cancelled), 'not_cancelled': [{'key': k, 'status': status(k)} for k in keys if k not in cancelled],
                'text': '\n'.join(t for t in texts if t)}
    out = {'total': len(keys), 'ready': 0, 'in_work': 0, 'published': 0, 'dropped': 0, 'errors': []}
    for key in keys:
        decision, item = decision_for(key), excel.get(key)
        if decision and decision.status == 'pending':
            out['ready'] += 1
        elif decision and decision.status == 'published':
            out['published'] += 1
        elif decision and decision.status in ('rejected', 'expired'):
            out['dropped'] += 1
        elif item and item.status in ('new', 'research', 'card', 'submission', 'preview'):
            out['in_work'] += 1
        else:
            out['dropped'] += 1
            if item and item.error:
                out['errors'].append({'brand': item.brand, 'model': item.model, 'error': item.error})
    return out


def readable_errors(items):
    lines = []
    for item in items[:6]:
        error = item.get('error', '')
        if '503' in error:
            reason = 'сайт источника временно недоступен (503)'
        elif '404' in error:
            reason = 'страница или исходный файл не найдены (404)'
        elif 'PHOTO_AUDIT_HOLD:' in error or 'Фото остановлено для ручной проверки' in error:
            reason = 'контроль не разрешил выгрузку; подробности в отчёте фотоаудитора'
        elif 'Timeout' in error or 'timeout' in error:
            reason = 'источник или обработчик не ответил вовремя'
        else:
            reason = ' '.join(error.split())[:160]
        lines.append(f"• {item.get('brand', '')} {item.get('model', '')}: {reason}")
    return '\n'.join(lines)

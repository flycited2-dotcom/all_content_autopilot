"""Owner decisions after a price comparison: keep, revise, or save a threshold."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import re
import time
import uuid

from orchestrator_bot.existing_cards import _money, _price, _when

PENDING = 'price_review_pending'
RULE = 'price_review_rule'
TTL = 86400


def _save(store, ticket):
    store.set_meta('price_review:' + ticket['id'], json.dumps(ticket, ensure_ascii=False))


def get_ticket(store, ticket_id):
    if not re.fullmatch('[a-f0-9]{32}', str(ticket_id)):
        return None
    try:
        return json.loads(store.get_meta('price_review:' + ticket_id) or 'null')
    except (ValueError, TypeError):
        return None


def get_rule(store):
    try:
        value = json.loads(store.get_meta(RULE) or 'null')
        if value is not None:
            _rule_values(value['ignore_percent'], value['urgent_percent'])
        return value
    except (ValueError, TypeError, KeyError):
        return None


def _rule_values(ignore, urgent):
    try:
        ignore, urgent = Decimal(str(ignore)), Decimal(str(urgent))
    except InvalidOperation:
        raise ValueError('Нужны два процентных порога') from None
    if not (ignore.is_finite() and urgent.is_finite() and 0 <= ignore < urgent <= 1000):
        raise ValueError('Первый порог должен быть от 0, второй — больше первого и не выше 1000%')
    return ignore, urgent


def set_rule(store, ignore_percent, urgent_percent):
    ignore, urgent = _rule_values(ignore_percent, urgent_percent)
    rule = {'ignore_percent': str(ignore), 'urgent_percent': str(urgent), 'unit': 'percent'}
    store.set_meta(RULE, json.dumps(rule))
    return rule


def classify(old, new, rule, conflict=False):
    old, new = _price(old), _price(new)
    if conflict or old is None or new is None:
        return 'unknown'
    change = abs(new - old) / old * 100
    if change == 0:
        return 'equal'
    if rule:
        ignore, urgent = _rule_values(rule['ignore_percent'], rule['urgent_percent'])
        if change <= ignore:
            return 'ignored'
        if change > urgent:
            return 'urgent'
    return 'review'


def prepare(store, msg_id, inventory, statuses, pricing, now=None):
    positions = {(s['attachment_index'], s['row_index']): s for s in statuses}
    grouped = {}
    for row in inventory['rows']:
        status = positions.get((row['attachment_index'], row['row_index']), {})
        if status.get('state') not in ('ready', 'published', 'expired') or status.get('stage') == 'research':
            continue
        job = status.get('job_id')
        if type(job) is not int:
            continue
        entry = grouped.setdefault(job, {'job_id': job, 'brand': row['brand'], 'model': row['model'],
            'old_price': status.get('card_price'), 'created_at': status.get('card_created_at'),
            'state': status['state'], 'new_prices': [], 'selected_price': None,
            'source_uncertain': False})
        source = _price(row['cost_price'])
        markup = pricing.get('markup_percent')
        if source is None or markup is None:
            entry['source_uncertain'] = True
            continue
        target = str((source * (1 + Decimal(str(markup)) / 100)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        if target not in entry['new_prices']:
            entry['new_prices'].append(target)
    if not grouped:
        return None
    items = list(grouped.values())
    for item in items:
        if len(item['new_prices']) == 1 and not item['source_uncertain']:
            item['selected_price'] = item['new_prices'][0]
    fingerprint = json.dumps([msg_id, items, pricing], ensure_ascii=False, sort_keys=True)
    ticket_id = uuid.uuid5(uuid.NAMESPACE_URL, 'supplier-price-review:' + fingerprint).hex
    old = get_ticket(store, ticket_id)
    if old:
        return old
    ticket = {'id': ticket_id, 'msg_id': msg_id, 'items': items, 'phase': 'open', 'created': now or time.time(),
              'complete': inventory['complete'], 'pricing': pricing, 'results': {}, 'message_ids': []}
    _save(store, ticket)
    store.set_meta(PENDING, ticket_id)
    return ticket


def _class(item, rule):
    return classify(item['old_price'], item['selected_price'], rule,
                    conflict=item.get('source_uncertain') or len(item['new_prices']) > 1 and not item['selected_price'])


def requires_attention(ticket, rule):
    return any(_class(item, rule) not in ('equal', 'ignored') for item in ticket['items'])


def render(ticket, rule=None):
    lines = ['💰 Готовые карточки: решите, что делать с ценами.']
    if rule:
        lines.append(f"Правило: до {rule['ignore_percent']}% включительно оставляю старую цену; "
                     f"выше {rule['urgent_percent']}% — повышенное внимание. Сам цены не меняю.")
    else:
        lines.append('Порог пока не задан: каждое изменение требует вашего решения.')
    if not ticket['complete']:
        lines.append('⚠️ Разбор источника не подтверждён полностью. Перед обновлением проверьте суммы ниже.')
    keyboard = []
    quiet = {'equal': 0, 'ignored': 0}
    for index, item in enumerate(ticket['items']):
        kind = _class(item, rule)
        if kind in quiet:
            quiet[kind] += 1
            continue
        labels = {'equal': 'цены совпадают', 'ignored': 'оставить по правилу', 'review': 'нужно решение',
                  'urgent': '⚠️ выше порога', 'unknown': 'нужно уточнение'}
        old, new = _price(item['old_price']), _price(item['selected_price'])
        targets = ' / '.join(_money(Decimal(price)) for price in item['new_prices']) or 'не подтверждена'
        line = f"• {item['brand']} {item['model']}: " + (_money(old) if old else 'старая цена неизвестна') + ' → ' + targets
        if old is not None and new is not None:
            percent = format((abs(new-old)/old*100).quantize(Decimal('0.00000001')), 'f').rstrip('0').rstrip('.')
            line += f" ({percent}%)"
        lines.append(line + ' — ' + labels[kind] + '.')
        if item['state'] != 'ready':
            lines.append('Эта карточка уже опубликована или превью истекло. Здесь доступно только сохранение старой версии.')
        if len(item['new_prices']) > 1 and item['selected_price'] is None:
            lines.append('В прайсе разные цены одной модели. Выберите нужную:')
            for choice, price in enumerate(item['new_prices']):
                keyboard.append([{'text': f"{item['model']}: {_money(Decimal(price))}"[:60],
                                  'callback_data': f"pr:choose:{ticket['id']}:{index}:{choice}"}])
    if quiet['equal'] or quiet['ignored']:
        lines.append(f"Без действий: цены совпадают — {quiet['equal']}; оставлено по порогу — {quiet['ignored']}.")
    lines += ['', 'Цены взяты из сохранённых заданий; цифры на изображениях проверьте вручную.',
              '«Обновить цены» покажет точные суммы и число переделок перед запуском. '
              '«Оставить карточки» сохранит старые цены. Публикация — отдельно.']
    if ticket['phase'] in ('kept', 'done'):
        lines.append('Решение по этой сверке уже сохранено.')
        return '\n'.join(lines), []
    keyboard += [[{'text': '✏️ Обновить цены', 'callback_data': 'pr:update:' + ticket['id']}],
                 [{'text': '✅ Оставить карточки', 'callback_data': 'pr:keep:' + ticket['id']}],
                 [{'text': '⚙️ Настроить порог', 'callback_data': 'pr:rule:' + ticket['id']}]]
    return '\n'.join(lines), keyboard


def _send(deps, chat, ticket, text=None, keyboard=None):
    from orchestrator_bot.api import text_chunks
    if text is None:
        text, keyboard = render(ticket, get_rule(deps.store))
    parts = list(text_chunks(text))
    for i, part in enumerate(parts):
        mid = deps.tg.send(chat, part, keyboard if i == len(parts)-1 else [])
        if mid is not None:
            ticket.setdefault('message_ids', []).append(mid)
    _save(deps.store, ticket)


def _updates(ticket, rule):
    return [item for item in ticket['items'] if item['state'] == 'ready' and
            not (ticket.get('results',{}).get(str(item['job_id']),{}).get('submitted') or
                 ticket.get('results',{}).get(str(item['job_id']),{}).get('handled')) and
            _price(item['old_price']) is not None and _price(item['selected_price']) is not None and
            _class(item, rule) in ('review', 'urgent')]


def _confirm(deps, chat, ticket):
    updates = _updates(ticket, get_rule(deps.store))
    if not updates:
        _send(deps, chat, ticket, 'Нет выбранных цен для обновления. Уточните конфликтующие суммы или измените порог.',
              [[{'text':'Вернуться к сверке','callback_data':'pr:show:'+ticket['id']}]])
        return
    ticket['phase'] = 'confirm'
    ticket['confirmed_targets'] = [{'job_id': item['job_id'], 'price': item['selected_price']} for item in updates]
    _save(deps.store, ticket)
    lines = [f'Обновление цен: {len(updates)} карточек. Это запустит {len(updates)} переделок с новыми превью.']
    for item in updates:
        lines.append(f"• {item['brand']} {item['model']}: {_money(Decimal(item['old_price']))} → {_money(Decimal(item['selected_price']))}")
    unresolved = sum(_class(item,get_rule(deps.store)) == 'unknown' for item in ticket['items'])
    if unresolved:
        lines.append(f'Неуточнённые позиции ({unresolved}) пока не обновляю. Их можно выбрать после этой операции.')
    lines.append('Публикация не запускается. Подтвердите обновление или оставьте старые карточки.')
    _send(deps,chat,ticket,'\n'.join(lines),
          [[{'text':f'🔄 Подтвердить: {len(updates)} переделок','callback_data':'pr:confirm:'+ticket['id']}],
           [{'text':'Назад','callback_data':'pr:show:'+ticket['id']}]])


def _submit(deps, chat, ticket):
    if ticket['phase'] not in ('confirm','updating') or not ticket.get('confirmed_targets'):
        _send(deps,chat,ticket,'Сначала выберите «Обновить цены» и проверьте суммы.',
              [[{'text':'Открыть сверку','callback_data':'pr:show:'+ticket['id']}]])
        return
    ticket['phase']='updating';_save(deps.store,ticket)
    for target in ticket['confirmed_targets']:
        key=str(target['job_id'])
        prior=ticket['results'].get(key)
        if prior and (prior.get('submitted') or prior.get('handled')):
            continue
        item=next(i for i in ticket['items'] if i['job_id']==target['job_id'])
        payload={'request_id':uuid.uuid5(uuid.UUID(ticket['id']),key+':'+target['price']).hex,
                 'job_id':item['job_id'],'brand':item['brand'],'model':item['model'],
                 'old_price':str(item['old_price']),'new_price':target['price'],'actor_id':deps.owner_chat_id}
        try:
            result=deps.factory.update_card_price(payload)
        except Exception as exc:
            _save(deps.store,ticket)
            _send(deps,chat,ticket,f'Передача обновления не подтверждена ({type(exc).__name__}). '
                  'Проверьте тот же запрос кнопкой: новый запрос для этой карточки не создаётся.',
                  [[{'text':'Проверить передачу','callback_data':'pr:confirm:'+ticket['id']}]])
            return
        ticket['results'][key]=result;_save(deps.store,ticket)
        if result.get('submitted') and result.get('linked') is False:
            ticket['phase']='partial';_save(deps.store,ticket)
            _send(deps,chat,ticket,'Переделка принята, но связь с товаром требует проверки. Повторный запуск остановлен.')
            return
        if not result.get('submitted') and not result.get('handled'):
            _send(deps,chat,ticket,str(result.get('message') or 'Передача ещё не подтверждена.'),
                  [[{'text':'Проверить передачу','callback_data':'pr:confirm:'+ticket['id']}]])
            return
    accepted=[r for r in ticket['results'].values() if r.get('submitted') and r.get('linked') is not False]
    rejected=[r for r in ticket['results'].values() if not r.get('submitted')]
    unresolved = [item for item in ticket['items'] if item['state']=='ready' and
                  str(item['job_id']) not in ticket['results'] and _class(item,get_rule(deps.store))=='unknown']
    ticket['phase']='open' if unresolved else 'done';_save(deps.store,ticket)
    lines=[f'Обновление передано: {len(accepted)} карточек. Новые превью придут после обработки; публикация — по вашему подтверждению.']
    for result in rejected:
        lines.append(str(result.get('message') or result.get('reason') or 'Карточка уже изменилась; обновление не запущено.'))
    if unresolved:
        lines.append(f'Ещё {len(unresolved)} позиций требуют выбора цены. Уже переданные карточки повторно не запускаются.')
    _send(deps,chat,ticket,'\n'.join(lines))
    if unresolved:_send(deps,chat,ticket)


def _rule_prompt(deps,chat,ticket,ignore='1',urgent='5'):
    _rule_values(ignore,urgent)
    ticket['proposed_rule']={'ignore_percent':str(ignore),'urgent_percent':str(urgent)}
    ticket['phase']='rule';_save(deps.store,ticket)
    _send(deps,chat,ticket,f'Предлагаемое правило: изменение до {ignore}% включительно — оставить старую цену; '
          f'свыше {ignore}% до {urgent}% — спросить; свыше {urgent}% — срочная проверка. '
          'Процент считается от старой цены одинаково для повышения и снижения. '
          'Противоречия и неизвестные цены всегда показываю. Обновления сами не запускаются. '
          'Для другого правила напишите, например: «порог 0,5% и 3%».',
          [[{'text':f'Сохранить правило {ignore}% / {urgent}%','callback_data':'pr:save1:'+ticket['id']}],
           [{'text':'Отмена','callback_data':'pr:show:'+ticket['id']}]])


def callback(deps, chat_id, data):
    fields=data.split(':')
    if len(fields)<3 or fields[0]!='pr':
        return False
    op,ticket_id=fields[1:3];ticket=get_ticket(deps.store,ticket_id)
    if not ticket or deps.clock()-ticket['created']>TTL:
        deps.tg.send(chat_id,'Сверка устарела или не найдена. Пришлите актуальный прайс.');return True
    deps.store.set_meta(PENDING,ticket_id)
    if ticket['phase']=='partial':
        deps.tg.send(chat_id,'Задание принято, но его привязка требует проверки. Новая передача по этой сверке остановлена.')
        return True
    if ticket['phase']=='updating' and op not in ('show','confirm','update'):
        _send(deps,chat_id,ticket,'Передача обновлений ещё не подтверждена. Сначала проверьте тот же запрос; изменить его сейчас нельзя.',
              [[{'text':'Проверить передачу','callback_data':'pr:confirm:'+ticket['id']}]])
        return True
    if ticket['phase'] in ('kept','done') and op!='show':
        deps.tg.send(chat_id,'Решение по этой сверке уже сохранено. Повторный запуск не выполняется.');return True
    if op=='show':
        if ticket['phase']!='updating' and ticket['phase'] not in ('kept','done'):
            ticket['phase']='open'
        _send(deps,chat_id,ticket)
    elif op=='keep':
        if ticket['phase']=='updating':
            deps.tg.send(chat_id,'Передача обновлений уже началась. Сначала проверьте тот же запрос.');return True
        ticket['phase']='kept';_save(deps.store,ticket)
        _send(deps,chat_id,ticket,'Сохранено: оставить существующие карточки и цены без изменений. Генерация и публикация не запускались.')
    elif op=='update':
        if ticket['phase']=='updating': _submit(deps,chat_id,ticket)
        else: _confirm(deps,chat_id,ticket)
    elif op=='confirm': _submit(deps,chat_id,ticket)
    elif op=='rule': _rule_prompt(deps,chat_id,ticket)
    elif op=='save1' and ticket.get('proposed_rule'):
        proposal=ticket['proposed_rule'];set_rule(deps.store,proposal['ignore_percent'],proposal['urgent_percent'])
        ticket['phase']='open';_save(deps.store,ticket)
        _send(deps,chat_id,ticket,'Правило сохранено. Небольшие изменения оставляю без переделки, существенные покажу для решения.')
        _send(deps,chat_id,ticket)
    elif op=='choose' and len(fields)==5 and ticket['phase']!='updating':
        try:
            index,choice=int(fields[3]),int(fields[4]);assert index>=0 and choice>=0
            item=ticket['items'][index];item['selected_price']=item['new_prices'][choice]
        except (ValueError,IndexError,AssertionError):
            deps.tg.send(chat_id,'Этот вариант цены не найден. Откройте актуальную сверку.');return True
        ticket['phase']='open';_save(deps.store,ticket);_send(deps,chat_id,ticket)
    return True


def take_text(deps,chat_id,text,reply_message_id=None,message_id=None):
    current=deps.store.get_meta(PENDING)
    ticket=get_ticket(deps.store,current) if current else None
    if reply_message_id is not None:
        with deps.store._c() as db:
            rows=db.execute("SELECT value FROM meta WHERE key LIKE 'price_review:%'").fetchall()
        matches=[json.loads(row[0]) for row in rows if reply_message_id in json.loads(row[0]).get('message_ids',[])]
        if not matches: return False
        ticket=matches[-1]
    if not ticket or deps.clock()-ticket['created']>TTL: return False
    def finish():
        if message_id is not None:
            mid=deps.store.add_message(message_id,'price_decision',text,now=deps.clock())
            if mid is not None:
                deps.store.save_response(mid,{'reply':'Решение по проверке цен обработано.'})
                deps.store.mark_intent(mid);deps.store.mark_done(mid)
        return True
    lower=' '.join(text.strip().casefold().split())
    update_words = ('обновить карточки по новым ценам','обновить цены на карточках','обнови цены на карточках',
                    'обновить цены в карточках','обнови цены в карточках','поменять цены','поменяй цены')
    if lower in update_words or re.fullmatch(r'(?:обнови(?:ть)?|измени(?:ть)?|исправь)\s+(?:все\s+)?цен[уыа]',lower):
        callback(deps,chat_id,'pr:update:'+ticket['id']);return finish()
    if lower in ('оставить карточки','оставь карточки','оставить без изменений','принять без изменений','оставь как есть'):
        callback(deps,chat_id,'pr:keep:'+ticket['id']);return finish()
    if lower in ('настроить порог','настрой порог','правило цен','настроить правило'):
        callback(deps,chat_id,'pr:rule:'+ticket['id']);return finish()
    if lower in ('подтвердить обновление','подтверждаю обновление цен') and ticket['phase']=='confirm':
        callback(deps,chat_id,'pr:confirm:'+ticket['id']);return finish()
    if lower in ('сохранить правило','подтверждаю правило') and ticket['phase']=='rule':
        callback(deps,chat_id,'pr:save1:'+ticket['id']);return finish()
    chosen = re.fullmatch(r'(?:(?:цена|выбираю|выбрать цену)\s+)?(\d[\d ]*)(?:\s*руб(?:лей|ля|ль)?\.?)?', lower)
    if chosen and ticket['phase'] not in ('rule','done','kept','updating','partial'):
        price = str(int(chosen[1].replace(' ','')))
        matches = [(n,p) for n,item in enumerate(ticket['items']) for p,value in enumerate(item['new_prices'])
                   if len(item['new_prices'])>1 and value==price]
        if len(matches)==1:
            n,p=matches[0];callback(deps,chat_id,f"pr:choose:{ticket['id']}:{n}:{p}");return finish()
        if len(matches)>1:
            _send(deps,chat_id,ticket,'Эта сумма относится к нескольким моделям. Выберите цену кнопкой нужной модели.')
            return finish()
    if ticket['phase']=='rule':
        for word, number in {'ноль':'0','одного':'1','один':'1','два':'2','двух':'2','три':'3','трёх':'3',
                             'четыре':'4','четырёх':'4','пять':'5','пяти':'5','десять':'10','десяти':'10'}.items():
            lower=re.sub(r'\b'+word+r'\b',number,lower)
        values=re.findall(r'(\d+(?:[.,]\d+)?)\s*(?:%|процент\w*)',lower)
        if len(values)==2:
            try:_rule_prompt(deps,chat_id,ticket,*[v.replace(',','.') for v in values])
            except ValueError as exc:deps.tg.send(chat_id,str(exc))
            return finish()
        if 'руб' in lower or re.search(r'\d',lower):
            deps.tg.send(chat_id,'Здесь нужен процент от старой цены, например «порог 1% и 5%». Сумму в рублях правилом не сохраняю.');return finish()
    return False


def open_latest(deps,chat_id):
    ticket=get_ticket(deps.store,deps.store.get_meta(PENDING))
    if ticket:_send(deps,chat_id,ticket)
    else:deps.tg.send(chat_id,'Проверок цен пока нет. Пришлите прайс или фото с актуальными ценами.')

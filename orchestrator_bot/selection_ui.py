"""Owner-only durable product picker. Buttons refer to server snapshots, never names."""
import json
import math
import re
import time

from orchestrator_bot.catalog_selection import select_rows

PAGE_SIZE = 6
TTL = 3600
AWAIT_COUNT = 'selection_await_count'


class SelectionError(ValueError):
    pass


class Sessions:
    def __init__(self, store):
        self.store = store
        with store._c() as db:
            db.execute('CREATE TABLE IF NOT EXISTS product_selections ('
                       'id INTEGER PRIMARY KEY, payload TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0)')

    def create(self, msg_id, action, items, now):
        payload = {'msg_id': msg_id, 'action': action, 'items': items, 'selected': [],
                   'view': 'manual' if action.get('selection') == 'manual' else 'choose',
                   'page': 0, 'state': 'open', 'expires': now + TTL}
        with self.store._c() as db:
            ident = db.execute('INSERT INTO product_selections(payload) VALUES(?)',
                               (json.dumps(payload, ensure_ascii=False),)).lastrowid
        return {**payload, 'id': ident, 'revision': 0}

    def get(self, ident):
        with self.store._c() as db:
            row = db.execute('SELECT * FROM product_selections WHERE id=?', (ident,)).fetchone()
        return {**json.loads(row['payload']), 'id': row['id'], 'revision': row['revision']} if row else None

    def change(self, ident, revision, operation, now):
        with self.store._c() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM product_selections WHERE id=?', (ident,)).fetchone()
            if not row:
                raise SelectionError('Выбор не найден. Задайте категорию заново.')
            value = json.loads(row['payload'])
            if value['state'] != 'open':
                raise SelectionError('Этот выбор уже обработан или ожидает разбора. /status — состояние задач.')
            if value['expires'] < now:
                raise SelectionError('Каталог мог измениться. Задайте категорию заново, чтобы обновить модели.')
            if row['revision'] != revision:
                raise SelectionError('Выбор уже изменился. Используйте последние кнопки.')
            operation(value)
            db.execute('UPDATE product_selections SET payload=?,revision=revision+1 WHERE id=?',
                       (json.dumps(value, ensure_ascii=False), ident))
            db.execute('COMMIT')
        return self.get(ident)

    def finish(self, ident, state):
        value = self.get(ident)
        value['state'] = state
        with self.store._c() as db:
            db.execute('UPDATE product_selections SET payload=? WHERE id=?',
                       (json.dumps(value, ensure_ascii=False), ident))


def button(session, title, operation, argument=0):
    return {'text': title, 'callback_data': f"s:{session['id']}:{session['revision']}:{operation}:{argument}"}


def render(session):
    items, chosen = session['items'], set(session['selected'])
    category = session['action']['category']
    text = f"«{category}»: доступно {len(items)} моделей. Выбрано: {len(chosen)}."
    keyboard = []
    if session['view'] == 'manual':
        page = session['page']
        text += f"\nСтраница {page + 1}/{max(1, math.ceil(len(items) / PAGE_SIZE))}. Отметьте нужные модели."
        for index in range(page * PAGE_SIZE, min((page + 1) * PAGE_SIZE, len(items))):
            item = items[index]
            mark = '✅' if item['key'] in chosen else '▫️'
            name = item.get('name') or item.get('model') or item['brand']
            keyboard.append([button(session, f'{mark} {index + 1}. {name[:48]}', 'toggle', index)])
        navigation = []
        if page:
            navigation.append(button(session, '← Назад', 'page', page - 1))
        if (page + 1) * PAGE_SIZE < len(items):
            navigation.append(button(session, 'Дальше →', 'page', page + 1))
        if navigation:
            keyboard.append(navigation)
        if chosen:
            keyboard.append([button(session, f'Создать выбранные ({len(chosen)})', 'run')])
        keyboard.append([button(session, 'Другой способ выбора', 'choose')])
    else:
        text += '\nВыберите способ. В авторежиме товары чередуются по брендам и поставщикам.'
        keyboard.append([button(session, f'Все модели ({len(items)})', 'all')])
        for count in sorted({min(len(items), n) for n in (5, 10, 20)}):
            keyboard.append([button(session, f'Автоматически выбрать {count}', 'auto', count)])
        keyboard.append([button(session, 'Указать количество', 'count')])
        keyboard.append([button(session, 'Выбрать модели вручную', 'manual')])
    keyboard.append([button(session, 'Отмена', 'cancel')])
    return text + '\nГотовые карточки пройдут контроль фото и придут на согласование.', keyboard


def open_selection(store, factory, msg_id, action):
    request = {**action, 'count': None, 'selection': 'all'}
    found = factory.resolve(request)
    items = found.get('items') or []
    if not items:
        return f"Нет доступных моделей: «{action['category']}».", []
    if any(not item.get('key') for item in items):
        raise SelectionError('Каталог не передал идентификаторы моделей; выбор не запущен.')
    session = Sessions(store).create(msg_id, action, items, time.time())
    return render(session)


def execute_session(deps, chat_id, session, keys):
    from orchestrator_bot.actions import execute
    from orchestrator_bot.intents import Outcome, render as render_result
    sessions = Sessions(deps.store)
    def reserve(value):
        value['state'] = 'working'
        value['selected'] = keys
    sessions.change(session['id'], session['revision'], reserve, deps.clock())
    action = {**session['action'], 'selection': 'manual', 'keys': keys, 'count': len(keys)}
    try:
        result = execute(Outcome('enqueue', action, action['category']), deps.factory,
                         deps.store, session['msg_id'])
    except Exception:
        # An interrupted external enqueue may already have succeeded: do not retry blindly.
        sessions.finish(session['id'], 'held')
        deps.tg.send(chat_id, 'Не удалось подтвердить постановку. Выбор сохранён для разбора; '
                             'автоматического повтора нет. /status — проверьте очередь.')
        return
    sessions.finish(session['id'], 'done')
    text, keyboard = render_result('', [result])
    deps.store.record_exchange(f"Выбор моделей «{action['category']}»: {len(keys)}", text)
    deps.tg.send(chat_id, text, keyboard)


def handle_callback(deps, chat_id, callback):
    try:
        _, ident, revision, operation, argument = callback['data'].split(':')
        ident, revision, argument = int(ident), int(revision), int(argument)
        sessions = Sessions(deps.store)
        session = sessions.get(ident)
        if not session:
            raise SelectionError('Выбор не найден. Задайте категорию заново.')
        if operation in ('all', 'auto', 'run'):
            if operation == 'run':
                keys = list(session['selected'])
            else:
                rows = select_rows(session['items'], mode='auto' if operation == 'auto' else 'all',
                                   count=argument if operation == 'auto' else None)
                keys = [row['key'] for row in rows]
            if not keys:
                raise SelectionError('Сначала выберите хотя бы одну модель.')
            execute_session(deps, chat_id, {**session, 'revision': revision}, keys)
            return
        def change(value):
            if operation in ('manual', 'choose'):
                value['view'], value['page'] = operation, 0
            elif operation == 'page':
                if not 0 <= argument < math.ceil(len(value['items']) / PAGE_SIZE):
                    raise SelectionError('Нет такой страницы.')
                value['page'] = argument
            elif operation == 'toggle':
                if not 0 <= argument < len(value['items']):
                    raise SelectionError('Нет такой модели.')
                key = value['items'][argument]['key']
                if key in value['selected']:
                    value['selected'].remove(key)
                else:
                    value['selected'].append(key)
            elif operation == 'cancel':
                value['state'] = 'cancelled'
            elif operation != 'count':
                raise SelectionError('Неизвестная кнопка.')
        session = sessions.change(ident, revision, change, deps.clock())
        if operation == 'count':
            deps.store.set_meta(AWAIT_COUNT, str(ident))
            deps.tg.send(chat_id, f"Сколько моделей «{session['action']['category']}» выбрать? "
                                 f"Напишите число от 1 до {len(session['items'])}.")
        elif operation == 'cancel':
            deps.tg.send(chat_id, 'Выбор отменён. Генерация не запускалась.')
        else:
            text, keyboard = render(session)
            deps.tg.edit(chat_id, callback['message']['message_id'], text, keyboard)
    except (SelectionError, ValueError, KeyError):
        deps.tg.send(chat_id, 'Кнопка устарела или выбор уже обработан. Используйте последние кнопки '
                             'либо задайте категорию заново.')


def handle_count(deps, chat_id, message_id, text):
    ident = deps.store.get_meta(AWAIT_COUNT)
    if not ident:
        return False
    match = re.fullmatch(r'\s*(\d+)\s*(?:шт\.?|штук[аи]?)?\s*', text, re.I)
    if not match:
        deps.store.set_meta(AWAIT_COUNT, '')
        return False
    sessions = Sessions(deps.store)
    session = sessions.get(int(ident))
    if not session or session['state'] != 'open' or session['expires'] < deps.clock():
        deps.store.set_meta(AWAIT_COUNT, '')
        deps.tg.send(chat_id, 'Выбор устарел. Задайте категорию заново.')
        return True
    count = int(match[1])
    if not 1 <= count <= len(session['items']):
        deps.tg.send(chat_id, f"Укажите число от 1 до {len(session['items'])}.")
        return True
    msg_id = deps.store.add_message(message_id, 'selection-count', text, now=deps.clock())
    if msg_id is None:
        return True
    deps.store.set_meta(AWAIT_COUNT, '')
    keys = [row['key'] for row in select_rows(session['items'], count=count, mode='auto')]
    execute_session(deps, chat_id, session, keys)
    deps.store.mark_done(msg_id)
    return True

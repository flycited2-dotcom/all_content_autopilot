"""Read-only comparison of new supplier prices against saved card evidence."""
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re

MOSCOW = timezone(timedelta(hours=3))


def pricing(message, actions=()):
    # Only the owner's caption/answer controls the price basis. In particular,
    # names and instructions printed inside a file cannot set the markup.
    text = str((message or {}).get('text', '')).casefold()
    final = bool(re.search(
        r'без\s+(?:дополнительной\s+)?наценки|'
        r'(?:добавлять|прибавлять)\s+(?:проценты|наценку)\s+не\s+(?:надо|нужно)|'
        r'не\s+(?:надо|нужно)\s+(?:добавлять|прибавлять)\s+(?:проценты|наценку)|'
        r'не\s+(?:добавляй|прибавляй)(?:те)?\s+(?:проценты|наценку)|'
        r'цен[аы]\s+(?:уже\s+)?(?:конечн|продажн|с\s+наценкой)', text))
    if final:
        return {'markup_percent': '0', 'label': 'готовая цена из нового прайса'}
    values = {str(a.get('markup_percent')) for a in actions if a.get('type') == 'enqueue_supplier_products'}
    if len(values) == 1:
        try:
            value = Decimal(next(iter(values)))
            if value.is_finite() and 0 <= value <= 1000:
                return {'markup_percent': str(value), 'label': 'цена с указанной наценкой'}
        except InvalidOperation:
            pass
    return {'markup_percent': None, 'label': 'цена источника; наценка не задана'}


def _price(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number > 0 else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _money(value):
    return f'{value:,.2f}'.replace(',', ' ').rstrip('0').rstrip('.') + ' ₽'


def _when(value):
    try:
        date = (datetime.fromtimestamp(float(value), timezone.utc) if isinstance(value, (int, float))
                else datetime.fromisoformat(str(value).replace('Z', '+00:00')))
        if date.tzinfo is not None:
            date = date.astimezone(MOSCOW)
        # Naive timestamps from older jobs have no reliable timezone. A date
        # remains useful without inventing an hour or asserting recency.
        return date.strftime('%d.%m.%Y')
    except (ValueError, TypeError, OverflowError, OSError):
        return 'дата не сохранена'


def notice(inventory, statuses, policy):
    by_position = {(s['attachment_index'], s['row_index']): s for s in statuses}
    source_prices = {}
    def identity_for(row):
        return (str(row.get('brand') or '').casefold(), re.sub(r'\s+', '', str(row.get('model') or '')).casefold())
    for row in inventory['rows']:
        price = _price(row.get('cost_price'))
        if price is not None:
            source_prices.setdefault(identity_for(row), set()).add(price)
    lines, seen, needs_visual_check = [], set(), False
    for row in inventory['rows']:
        status = by_position.get((row['attachment_index'], row['row_index']), {})
        if status.get('state') not in ('ready', 'published', 'expired') or status.get('stage') == 'research':
            continue
        identity = (status.get('job_id'), *identity_for(row), row.get('cost_price'))
        if identity in seen:
            continue
        seen.add(identity)
        title = ' '.join(part for part in (row.get('brand'), row.get('model')) if part)
        if not title:
            continue
        old, incoming = _price(status.get('card_price')), _price(row.get('cost_price'))
        markup = policy.get('markup_percent')
        target = ((incoming * (1 + Decimal(markup) / 100)).quantize(Decimal(1), rounding=ROUND_HALF_UP)
                  if incoming is not None and markup is not None else None)
        lines += ['', f"• {title} — задание"
                       + (f" №{status['job_id']}" if status.get('job_id') else '')
                       + f" от {_when(status.get('card_created_at'))}."]
        if old is not None:
            label = 'проверенная цена карточки' if status.get('card_price_verified') else 'цена в задании'
            lines.append(f'{label.capitalize()}: {_money(old)}.')
        else:
            lines.append('Сохранённую цену карточки подтвердить не удалось.')
        if incoming is not None:
            lines.append(f"Новый прайс: {_money(incoming)}.")
        else:
            lines.append('Цена в новом прайсе не подтверждена.')
        if len(source_prices.get(identity_for(row), ())) > 1:
            lines.append('⚠️ В новом прайсе для этой модели указаны разные цены. Уточните нужную строку вручную.')
        if old is not None and target is not None:
            if old == target:
                lines.append(f'Цены совпадают: {_money(target)}.')
            else:
                delta = target - old
                sign = '+' if delta > 0 else '−'
                lines.append(f'⚠️ Цена изменилась: {_money(old)} → {_money(target)} '
                             f'({sign}{_money(abs(delta))}).')
        else:
            lines.append('Автоматическое сравнение продажных цен невозможно; проверьте цену вручную.')
        if old is not None and not status.get('card_price_verified'):
            needs_visual_check = True
    if not lines:
        return None
    prefix = ['📋 Эти карточки уже созданы — отдельная проверка цен.',
              'Проверьте цены перед публикацией. Повторная генерация и изменение карточек этим сообщением не запускаются.',
              'Сравнение: ' + policy['label'] + '.']
    if needs_visual_check:
        prefix.append('Цену на самом изображении проверьте вручную: сравнение выполнено по сохранённым данным.')
    if not inventory.get('complete'):
        prefix.append('⚠️ Разбор нового источника не подтверждён полностью. Сравнение предварительное; проверьте строки вручную.')
    return '\n'.join(prefix + lines)

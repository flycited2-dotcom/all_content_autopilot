"""Shared source-selection rules for the server and the listener.

New pasted data always wins over optional previous files or photographs. Only
an explicit reference may attach an earlier document to a later instruction.
"""
import re


def pasted_table(text):
    """Recognize source tables without routing ordinary commands to extraction."""
    if not isinstance(text, str) or '\n' not in text:
        return False
    lower = text.casefold()
    identity = re.search(r'модел|наименован|название|\bmodel\b|\bname\b', lower)
    price = re.search(r'цен[аыуе]|стоимост|\b(?:cost_price|price|cost)\b', lower)
    stock = re.search(r'остат|количеств|наличи|\b(?:stock|quantity|qty)\b', lower)
    if identity and price and stock and re.search(r'\d', text):
        return True
    # Plain copied price lists often omit column headers entirely. Require two
    # independent product rows with a model containing a digit, a price and qty.
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    rows = [line for line in lines if
            re.search(r'[A-Za-zА-Яа-я][A-Za-zА-Яа-я0-9_./()-]*\d', line) and
            len(re.findall(r'(?<![A-Za-zА-Яа-я0-9])\d+(?:[.,]\d+)?', line)) >= 2]
    return len(rows) >= 2


def explicit_reference(text):
    return bool(re.search(
        r'\b(?:из|по|в|с|эт\w*|прислан\w*|последн\w*|прикрепл\w*)\s+'
        r'(?:(?:эт\w*|прислан\w*|последн\w*|прикрепл\w*)\s+)?'
        r'(?:файл\w*|документ\w*|таблиц\w*|прайс\w*|списк\w*)|'
        r'\b(?:pdf|excel|xlsx|csv|yaml|word|docx)\b|'
        r'\bиз\s+(?:него|неё|нее)\b', str(text), re.I))


def pending_ask_id(text):
    """Only the bot-generated exact ask marker can select an already used ask."""
    found = re.search(r'\[ответ на вопрос id: ([1-9]\d{0,18})\]\s*$', str(text))
    return int(found.group(1)) if found else None


def pending_response(text):
    """Short answers only; status questions and new categories cannot inherit data."""
    text = str(text).strip().casefold().rstrip('.!')
    if re.fullmatch(r'(?:все|всё|всех|все позиции|все товары|да|подтверждаю|запускай|в работу)', text):
        return True
    return percentage_response(text)


def percentage_response(text):
    text = str(text).strip().casefold().rstrip('.!')
    number = r'(?:\d{1,4}(?:[.,]\d{1,2})?|ноль|пять|десять|пятнадцать|двадцать|тридцать|сорок|пятьдесят|сто)'
    return bool(re.fullmatch(r'(?:наценка\s*|плюс\s*|\+\s*)?' + number +
                             r'\s*(?:%|процент(?:а|ов)?)', text))

"""Run with the content-factory venv and cwd; its source code is not modified."""
import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import sqlite3

from orchestrator_bot.catalog_selection import select_rows, model_name


def hint(cf, args):
    _, prices_dir, _ = cf._paths(args)
    slots, labelled = cf._items(prices_dir)
    counts = Counter(str(item.brand).strip() for _, item in labelled if item.brand)
    sections = Counter(str(item.section).strip() for _, item in labelled if item.section)
    return {'brands': sorted(counts, key=str.casefold), 'brands_complete': True,
            'voice_brands': [brand for brand, _ in counts.most_common(40)],
            'sources': [label for label, _ in slots],
            'source_counts': {label: len(items) for label, items in slots},
            'categories': sorted(sections, key=str.casefold), 'catalog_rows': len(labelled)}


def resolve(cf, args):
    from content_factory.ingest.excel_price import load_search_aliases, search_items
    state_db, prices_dir, queue_db = cf._paths(args)
    _, labelled = cf._items(prices_dir, args.source)
    labelled = cf._spec_filter(labelled, args, prices_dir)
    source_of = {id(item): label for label, item in labelled}
    found = search_items([item for _, item in labelled], args.category,
                         cf._taken(state_db, queue_db), limit=10**9,
                         aliases=load_search_aliases(Path('config/search_aliases.yaml')))
    rows = [cf._item_json(source_of.get(id(item), ''), item) for item in found]
    rows = [{**row, 'model': model_name(row['model'])} for row in rows]
    quotas = cf._quotas(args.quotas)
    eligible = select_rows(rows, quotas=quotas, mode='first')
    selected = select_rows(rows, count=args.count, quotas=quotas,
                           mode=args.selection, keys=json.loads(args.keys_json) if args.keys_json else None)
    return {'total_matches': len(eligible), 'items': selected}


def enqueue(cf, args):
    from content_factory.bot.commands import parse_due_at
    from content_factory.orchestrator.excel_pipeline import ExcelStore
    state_db, _, queue_db = cf._paths(args)
    due_at = parse_due_at(args.due, datetime.now()) if args.due else None
    if args.due and due_at is None:
        raise ValueError('Неверное время выхода партии')
    selected = resolve(cf, args)['items']  # revalidate exact keys against live stock/taken
    rows = [(r['key'], r['brand'], r['model'], r['name'], r['price']) for r in selected]
    accepted = ExcelStore(state_db).select_items(rows, due_at=due_at, queue_db=queue_db) if rows else []
    keys = [row[0] for row in accepted]
    if keys and not args.no_wake:
        from content_factory.bot import worker_control
        from content_factory.cards_pipeline import wake_agent
        try:
            worker_control.request_run()
        except Exception:
            pass  # existing cf-excel timer remains the recovery path
        if queue_db:
            try:
                wake_agent(queue_db)
            except sqlite3.Error:
                pass
    return {'requested': len(selected), 'accepted': keys,
            'items': [r for r in selected if r['key'] in set(keys)], 'due_at': due_at}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('hint', 'resolve', 'enqueue', 'resume-photo', 'batch-status', 'publish-keys', 'cancel'))
    parser.add_argument('--audit-json')
    for name in ('state-db', 'prices-dir', 'queue-db', 'category', 'source', 'quotas', 'btu',
                 'keys-json', 'due'):
        parser.add_argument('--' + name)
    parser.add_argument('--count', type=int)
    parser.add_argument('--min-temp', type=int)
    parser.add_argument('--selection', default='first', choices=('first', 'auto', 'all', 'choose', 'manual'))
    for flag in ('inverter', 'heat-pump', 'full-dc', 'no-wake'):
        parser.add_argument('--' + flag, action='store_true')
    args = parser.parse_args(argv)
    if args.command not in ('hint', 'resume-photo', 'batch-status', 'publish-keys', 'cancel') and not args.category:
        parser.error('--category is required')
    from content_factory import agent_cli as cf
    if args.command in ('batch-status', 'publish-keys', 'cancel'):
        from orchestrator_bot.key_commands import run
        result = run(cf, args)
    elif args.command == 'resume-photo':
        from orchestrator_bot.photo_recovery import reconcile
        state, _, queue = cf._paths(args)
        result = reconcile(state, queue, json.loads(args.audit_json or '{}'))
    else:
        result = {'hint': hint, 'resolve': resolve, 'enqueue': enqueue}[args.command](cf, args)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError) as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False))
        raise SystemExit(2)

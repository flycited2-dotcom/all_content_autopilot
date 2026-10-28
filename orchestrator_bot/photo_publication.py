"""Publish only the current card whose exact bytes the owner accepted in the auditor."""
import hashlib
import json
from pathlib import Path
import sqlite3

from orchestrator_bot.photo_recovery import validate


def inspect(state_path, queue_path, decision):
    value=validate(decision)
    if value['status']!='approved':raise ValueError('Публикация требует подтверждения карточки')
    with sqlite3.connect(Path(state_path).resolve().as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        db.execute('ATTACH DATABASE ? AS q',(Path(queue_path).resolve().as_uri()+'?mode=ro',))
        job=db.execute('SELECT id,mode,brand,model,status FROM q.jobs WHERE id=?',(value['job_id'],)).fetchone()
        items=db.execute('SELECT * FROM excel_items WHERE card_job=?',(value['job_id'],)).fetchall()
        if not job or len(items)!=1:return {'handled':True,'published':False,'reason':'задание заменено или отменено'}
        if job['mode']=='research' or any(str(job[k]).strip().casefold()!=value[k].strip().casefold() for k in ('brand','model')):
            raise ValueError('Подтверждение относится к другому товару или этапу поиска')
        item=items[0]
        if item['status']=='cancelled' or job['status']=='cancelled':
            return {'handled':True,'published':False,'reason':'товар отменён владельцем'}
        awaiting=db.execute('SELECT * FROM awaiting WHERE key=?',(item['key'],)).fetchone()
        if job['status']!='done' or item['status']!='preview' or not awaiting or awaiting['ts']<item['ts']:
            return {'handled':False,'published':False,'reason':'ожидается доставка подтверждённого файла'}
        if awaiting['status'] not in ('pending','published'):
            return {'handled':True,'published':False,'reason':'превью отклонено или истекло'}
        path=Path(awaiting['card_path'])
        if (not path.is_file() or path.is_symlink() or path.stat().st_size>10*1024*1024 or
                hashlib.sha256(path.read_bytes()).hexdigest()!=value['sha256']):
            return {'handled':True,'published':False,'reason':'файл превью отличается от подтверждённой фотографии'}
        return {'handled':awaiting['status']=='published','published':awaiting['status']=='published',
                'ready':awaiting['status']=='pending','key':item['key'],'card_path':str(path)}


def awaiting_item(state,key):
    from content_factory.orchestrator.confirm_store import ConfirmStore
    return ConfirmStore(state).get(key)


def run(cf,args):
    state,_,queue=cf._paths(args)
    result=inspect(state,queue,json.loads(args.audit_json or '{}'))
    if not result.get('ready'):return result
    pending=awaiting_item(state,result['key'])
    if not pending or pending.status!='pending' or pending.card_path!=result['card_path']:
        return {'handled':False,'published':False,'reason':'состояние превью изменилось'}
    current=inspect(state,queue,json.loads(args.audit_json or '{}'))
    if not current.get('ready') or current['key']!=result['key'] or current['card_path']!=result['card_path']:
        return current
    published=cf._publish_batch(state,[pending])
    if published.get('published'):
        return {'handled':True,'published':True,'key':result['key'],'result':published}
    return {'handled':True,'published':False,'reason':'ошибка отправки в канал; файл сохранён в группе проверки'}

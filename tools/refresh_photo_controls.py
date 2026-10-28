"""Resend current saved cards with real owner actions; never simulate a click."""
import argparse
from datetime import datetime
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from listener.pending import write_json
from orchestrator_bot.telegram_api import TelegramApi
from photo_auditor.credentials import load
from photo_auditor.images import digest,image_bytes
from photo_auditor.runtime import content_brief
from photo_auditor.store import AuditStore


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job_ids',nargs='+',type=int);parser.add_argument('--apply',action='store_true')
    args=parser.parse_args();store=AuditStore(ROOT/'runs/photo-auditor');rows=[]
    for job in args.job_ids:
        row=store.latest(f'fotogen-{job}')
        if not row or row['status'] not in ('approved','review') or not row['report']:
            raise RuntimeError('Card has no current final inspection')
        image=store.root/row['id']/'generated.bin'
        if digest(image_bytes(image.read_bytes()))!=row['sha256'] or content_brief(ROOT,row['product']) is None:
            raise RuntimeError('Missing exact card photo or signed brief')
        rows.append(row)
    if not args.apply:
        print({'ready_to_resend':[r['product_id'] for r in rows]});return
    tg=TelegramApi(load(ROOT)['bot_token']);receipts=[];owner=1264067528
    for row in rows:
        if store.latest(row['product_id'])['id']!=row['id']:raise RuntimeError('A newer image appeared')
        status='Аудитор одобрил карточку.' if row['status']=='approved' else 'Аудитор удержал карточку; подтвердите только если принимаете результат.'
        caption=(f"{row['product']['brand']} {row['product']['model']}\nРевизия {row['revision']}; {row['id'][:12]}\n"
                 f"{status}\n\nНовые рабочие кнопки: публикация отправит именно этот файл в канал после доставки. "
                 'Принятие без публикации оставит его в группе проверки.')
        keyboard=[[{'text':'✅ Подтвердить и опубликовать в канал','callback_data':f"qa:publish:{row['id']}"}],
                  [{'text':'✅ Принять без публикации','callback_data':f"qa:approve:{row['id']}"}],
                  [{'text':'🔄 Перепроверить / исправить','callback_data':f"qa:recheck:{row['id']}"}]]
        message_id=tg.send_photo(owner,store.root/row['id']/'generated.bin',caption=caption,keyboard=keyboard)
        receipts.append({'audit_id':row['id'],'product_id':row['product_id'],'message_id':message_id})
    folder=ROOT/'runs/2026-10-27/owner-controls';folder.mkdir(parents=True,exist_ok=True)
    write_json(folder/(datetime.now().strftime('%H%M%S')+'.json'),{'photos':receipts,'simulated_clicks':False,'publications_triggered':False})
    print({'photos_sent':len(receipts),'publications_triggered':False})


if __name__=='__main__':main()

"""Explicit owner approval, bound to one immutable photo and its signed brief."""
import hmac
import re
import time

from photo_auditor import runtime


def _scope(root, product):
    match=re.fullmatch(r'fotogen-([1-9][0-9]*)', product['product_id'])
    if not match:
        return None
    scope=runtime._read(root,int(match[1]))
    if any(scope[k]!=product[k] for k in ('brand','model')):
        raise ValueError('Номенклатура задания изменилась')
    return scope


def sign(root, row, actor_id, callback_id, *, publish=False):
    if type(actor_id) is not int or actor_id<=0 or not isinstance(callback_id,str) or not 0<len(callback_id)<=128:
        raise ValueError('Нет подтверждённого действия владельца')
    brief=runtime.content_brief(root,row['product'])
    payload={'product':row['product'],'generated_sha256':row['sha256'],
             'scope_sha256':runtime.brief_digest(_scope(root,row['product'])),
             'brief_sha256':runtime.brief_digest(brief),'actor_id':actor_id,
             'callback_id':callback_id,'decision':'approve_saved_result','created':time.time(),
             'publish_requested':publish}
    return {'payload':payload,'signature':runtime._signature(root,payload)}


def valid(root, product, report):
    try:
        value=report['owner_approval'];p=value['payload']
        return bool(set(value)=={'payload','signature'} and set(p)=={
            'product','generated_sha256','scope_sha256','brief_sha256','actor_id',
            'callback_id','decision','created','publish_requested'} and type(p['publish_requested']) is bool and
            type(p['actor_id']) is int and p['actor_id']>0 and
            isinstance(p['callback_id'],str) and 0<len(p['callback_id'])<=128 and
            p['decision']=='approve_saved_result' and p['product']==product and
            p['generated_sha256']==report['generated_sha256'] and
            p['scope_sha256']==runtime.brief_digest(_scope(root,product)) and
            p['brief_sha256']==runtime.brief_digest(runtime.content_brief(root,product)) and
            isinstance(value['signature'],str) and
            hmac.compare_digest(value['signature'],runtime._signature(root,p)))
    except (KeyError,ValueError,TypeError,OSError,runtime.AuditHold):
        return False

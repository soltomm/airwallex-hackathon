"""RFP-owned subcontracts: durable state, exact-quote approval and simulated fulfillment.

No delivery, signing or vendor money movement occurs in this prototype.
"""
from datetime import datetime
from decimal import Decimal
from functools import lru_cache
import copy
import hashlib
import json
from pathlib import Path
from .extract import stay_nights

CATEGORIES = {'rooms','meeting','catering','av','technician','amenities'}
COVERS = {'catering': {'breakfast','group dinner','lunch','drinks reception'}, 'av': set(), 'technician': set(), 'amenities': {'airport transfers','transfers','spa','parking','coach transport'}}

@lru_cache(maxsize=1)
def directory():
    return json.loads((Path(__file__).resolve().parents[3]/'data/groupsales/vendor_directory.json').read_text())

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()[:20]

def rfp_scope(d):
    req = d.get('requirements') or {}
    return digest({k: req.get(k) for k in ('arrival','departure','rooms','guests','room_type','meeting','meeting_count','breakfast','unsupported')})

def integer(value, label, minimum=0, maximum=100000000):
    if isinstance(value,bool): raise ValueError(label+' must be a whole number.')
    try: number=Decimal(str(value))
    except Exception: raise ValueError(label+' must be a whole number.')
    if not number.is_finite() or number!=number.to_integral_value() or not minimum<=number<=maximum:
        raise ValueError(label+' is outside the supported range.')
    return int(number)

def workspace(d):
    return d.get('third_party') or {'enabled':False,'host_rooms':None,'external_meetings':0,'requests':[]}

def active(d): return [r for r in workspace(d)['requests'] if r['status']!='cancelled']

def snapshots(d):
    lines=[]
    for r in active(d):
        if not r.get('quote'): continue
        supplier = next(v for v in directory()['providers'] if v['id'] == r['vendor_id'])
        q=r['quote']; quantity=r['quantity']*(len(stay_nights(r['start'],r['end'])) if r['category'] in ('rooms','meeting') else 1)
        cost=quantity*q['unit_cost']
        uplift=q['markup'] if q['markup_type']=='flat' else (cost*q['markup']+50)//100
        lines.append({'request_id':r['id'],'quote_hash':q['hash'],'vendor_id':r['vendor_id'],
                      'category':r['category'],'label':r['title'],'start':r['start'],'end':r['end'],
                      'capacity':r['quantity'],'quantity':quantity,'cost':cost,'total':cost+uplift,'covers':r['covers'],
                      'location': supplier['name'] + ' · ' + supplier['location']})
    return lines

def covered(d):
    return {tag for r in active(d) if r.get('quote') for tag in r['covers']}

def readiness(d, now, terms=None):
    w=workspace(d)
    if not w['enabled']: return [] if not (terms or {}).get('vendor_lines') else ['Third-party management has been disabled.']
    problems=[]
    for r in active(d):
        q=r.get('quote')
        if not q: problems.append(r['title']+': awaiting vendor quote');continue
        if r['status'] not in ('confirmed','in_progress','delivered'): problems.append(r['title']+': approval and vendor confirmation required')
        if r.get('approval_hash')!=q['hash']: problems.append(r['title']+': internal approval is missing or stale')
        if not d.get('accepted_version') and q.get('rfp_scope') != rfp_scope(d): problems.append(r['title']+': inquiry requirements changed; a revised vendor quote is required')
        if not d.get('booking') and not d.get('accepted_version') and datetime.fromisoformat(q['valid_until'])<=datetime.fromisoformat(now): problems.append(r['title']+': quote expired')
    if terms is not None and terms.get('vendor_lines',[])!=snapshots(d): problems.append('Vendor scope or pricing changed; generate a new master proposal.')
    req=d.get('requirements') or {}
    if req.get('arrival') and req.get('departure'):
        requested=int(req.get('rooms') or 0); host=w['host_rooms'] if w['host_rooms'] is not None else requested
        for night in stay_nights(req['arrival'],req['departure']):
            external=sum(r['quantity'] for r in active(d) if r['category']=='rooms' and r['start']<=night<r['end'])
            if host+external!=requested: problems.append(f'{night}: host and partner rooms must exactly cover the requested {requested} rooms')
        needed=w['external_meetings']
        if needed:
            meeting_date=(req.get('meeting') or {}).get('date') or req['arrival']
            available=sum(r['quantity'] for r in active(d) if r['category']=='meeting' and r['start']<=meeting_date<r['end'])
            if available<needed: problems.append(f'{meeting_date}: {needed} external meeting rooms required')
        if 'breakfast' in covered(d):
            guests = int(req.get('guests') or requested * int(req.get('occupancy') or 1))
            for night in stay_nights(req['arrival'], req['departure']):
                covers = sum(r['quantity'] for r in active(d) if 'breakfast' in r['covers'] and r['start'] <= night < r['end'])
                if covers < guests: problems.append(f'{night}: external breakfast must cover all {guests} guests')
    if not active(d):problems.append('Add at least one vendor request, or disable third-party management.')
    return list(dict.fromkeys(problems))

def ensure_capacity(request, deal_id, all_deals, now):
    vendor=next(v for v in directory()['providers'] if v['id']==request['vendor_id'])
    limit=vendor['capacity'][request['category']]
    for day in stay_nights(request['start'],request['end']):
        used=sum(r['quantity'] for d in all_deals for r in active(d) if r['id']!=request['id'] and r['vendor_id']==request['vendor_id'] and r['category']==request['category'] and r['status'] in ('confirmed','in_progress','delivered') and r['start']<=day<r['end'] and (d.get('accepted_version') or d.get('booking') or datetime.fromisoformat(r['quote']['valid_until'])>datetime.fromisoformat(now)))
        if used+request['quantity']>limit:raise ValueError(f'{vendor["name"]} lacks simulated {request["category"]} capacity on {day}.')

def mutate(d, command, body, now, make_id, all_deals, actor):
    w=copy.deepcopy(workspace(d));d['third_party']=w
    operational=command in ('execution','payment_status')
    if (d.get('accepted_version') or d.get('booking')) and not operational:
        raise ValueError('Customer terms are accepted; vendor financial changes need a separately approved contract amendment.')
    if command=='configure':
        if not isinstance(body.get('enabled'),bool):raise ValueError('Enabled must be true or false.')
        if not body['enabled'] and active(d):raise ValueError('Cancel vendor requests before disabling this workflow.')
        requested=int((d.get('requirements') or {}).get('rooms') or 0)
        host = body.get('host_rooms')
        w.update(enabled=body['enabled'],host_rooms=None if host is None else integer(host,'Host rooms',0,requested),external_meetings=integer(body.get('external_meetings',0),'External meeting rooms',0,20))
        return 'Third-party management '+('enabled' if w['enabled'] else 'disabled')
    if not w['enabled']:raise ValueError('Enable third-party management first.')
    if command=='request':
        category=body.get('category');vendor=next((v for v in directory()['providers'] if v['id']==body.get('vendor_id')),None)
        if category not in CATEGORIES or not vendor or category not in vendor['categories']:raise ValueError('Choose a provider supporting this service.')
        start,end=str(body.get('start','')),str(body.get('end','')); nights=stay_nights(start,end)
        if not 1<=len(nights)<=366:raise ValueError('Choose a service window of 1–366 days, excluding checkout.')
        req=d.get('requirements') or {}
        if req.get('arrival') and (start<req['arrival'] or end>req['departure']):raise ValueError('Vendor service dates must be within the requested stay.')
        title=str(body.get('title') or category).strip()
        if not title or len(title)>180:raise ValueError('Provide a service description up to 180 characters.')
        tags=body.get('covers') or []
        if not isinstance(tags,list) or any(t not in COVERS.get(category,set()) for t in tags):raise ValueError('Unsupported requirement coverage for this category.')
        w['requests'].append({'id':make_id('vr_'),'vendor_id':vendor['id'],'category':category,'title':title,'start':start,'end':end,'quantity':integer(body.get('quantity',1),'Quantity',1,1000),'covers':tags,'status':'awaiting_quote','quote':None,'quote_versions':[],'approval_hash':None,'owner':'revops','execution_note':'','payment_status':'unpaid','simulated':True})
        return 'Quote request issued (simulated delivery): '+title
    r=next((r for r in w['requests'] if r['id']==body.get('request_id')),None)
    if not r:raise ValueError('Vendor request not found in this inquiry.')
    if r['status']=='cancelled':raise ValueError('This request is cancelled.')
    if command=='quote':
        if body.get('markup_type') not in ('flat','percent'):raise ValueError('Choose flat or percentage markup.')
        q={'revision':len(r['quote_versions'])+1,'unit_cost':integer(body.get('unit_cost'),'Unit cost in pence',1),'markup_type':body['markup_type'],'markup':integer(body.get('markup',0),'Markup',0,1000 if body['markup_type']=='percent' else 100000000),'valid_until':str(body.get('valid_until',''))}
        if datetime.fromisoformat(q['valid_until'])<=datetime.fromisoformat(now):raise ValueError('Quote expiry must be in the future.')
        q['rfp_scope'] = rfp_scope(d)
        q['hash']=digest({**q,'scope':{k:r[k] for k in ('id','vendor_id','category','title','start','end','quantity','covers')}})
        r.update(quote=q,status='quoted',approval_hash=None,vendor_unavailable=False);r['quote_versions'].append(copy.deepcopy(q))
        return 'Vendor quote recorded (simulated) · revision '+str(q['revision'])
    if command=='unavailable':
        reason=str(body.get('reason','')).strip()
        if not reason:raise ValueError('Record the provider’s availability reason.')
        r.update(quote=None,approval_hash=None,status='revision_requested',vendor_unavailable=True,review_note='Provider unavailable (simulated): '+reason[:1000])
        return 'Provider unavailable (simulated); source an alternative. Prior approvals invalidated.'
    if command in ('approve','reject'):
        if actor not in ('hotel_manager','revenue_ops'):raise ValueError('Only hotel management or revenue operations may review vendor quotes.')
        if r['status']!='quoted' or not r.get('quote'):raise ValueError('A current vendor quote is required.')
        if body.get('quote_hash')!=r['quote']['hash']:raise ValueError('This quote review is stale.')
        if r['quote'].get('rfp_scope') != rfp_scope(d):raise ValueError('Inquiry requirements changed; record a revised quote before approval.')
        if command=='reject' and not str(body.get('reason','')).strip():raise ValueError('Give a revision reason.')
        r.update(status='awaiting_confirmation' if command=='approve' else 'revision_requested',approval_hash=r['quote']['hash'] if command=='approve' else None,review_note=str(body.get('reason',''))[:1000])
        return 'RevOps '+('approved exact quote' if command=='approve' else 'requested revision')
    if command=='confirm':
        if r['status']!='awaiting_confirmation' or r['approval_hash']!=r['quote']['hash'] or body.get('quote_hash')!=r['quote']['hash']:raise ValueError('Current internal approval is required before vendor confirmation.')
        if r['quote'].get('rfp_scope') != rfp_scope(d):raise ValueError('Inquiry requirements changed; vendor confirmation must be renewed.')
        if datetime.fromisoformat(r['quote']['valid_until'])<=datetime.fromisoformat(now):raise ValueError('Vendor quote expired.')
        ensure_capacity(r,d['id'],all_deals,now);r['status']='confirmed'
        return 'Vendor availability and terms confirmed (simulated)'
    if command=='cancel':r['status']='cancelled';return 'Vendor request cancelled; simulated capacity released'
    if command=='execution':
        if not d.get('booking') or d['booking']['status']!='confirmed':raise ValueError('Confirm the master booking before delivery handoff.')
        if body.get('status') not in ('in_progress','delivered') or r['status'] not in ('confirmed','in_progress','delivered'):raise ValueError('Choose a valid delivery status.')
        if r['status']=='delivered' and body['status']!='delivered':raise ValueError('Delivered work cannot regress.')
        r.update(status=body['status'],execution_note=str(body.get('note',''))[:1000]);return 'Vendor delivery: '+r['status']
    if command=='payment_status':
        if not d.get('booking') or d['booking']['status']!='confirmed':raise ValueError('Vendor commitment tracking requires a confirmed booking.')
        if body.get('status') not in ('unpaid','scheduled','paid'):raise ValueError('Choose unpaid, scheduled or paid.')
        r['payment_status']=body['status'];return 'Vendor payment marked '+r['payment_status']+' (tracking only; no transfer)'
    raise ValueError('Unknown vendor workflow action.')

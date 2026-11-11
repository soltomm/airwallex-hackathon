"""Post-stay PMS snapshots and consent-based collection. No live PMS connector is assumed."""
from __future__ import annotations
import copy
import hashlib
import json
from datetime import date
from decimal import Decimal
from . import vendors
from .provider import AirwallexError, AmbiguousResult

RECEIPTS = {'deposit_received', 'folio_received', 'folio_external_received'}
CATEGORIES = {'accommodation','minibar','food_beverage','parking','incidentals','adjustment'}

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()[:24]

def reference(e,d):
    return (d.get('folio') or {}).get('reservation_ref') or 'PMS-'+digest([e.s['run_id'],d['id']])[:12].upper()

def cash(e,d):
    return sum(x['amount'] for x in e.s['ledger'] if x['deal_id']==d['id'] and x['type'] in RECEIPTS | {'refund_completed'})

def operations(e,d):
    return [o for o in e.s['operations'].values() if o['deal_id']==d['id'] and o['kind']=='folio_payment']

def pending(e,d):
    return [o for o in operations(e,d) if o['status'] not in ('succeeded','failed','cancelled')]

def text(value,label,limit=200):
    if not isinstance(value,str) or not value.strip() or len(value)>limit: raise ValueError(label+' is required (maximum '+str(limit)+' characters).')
    return value.strip()

def normalize(e,d,body):
    ref=text(body.get('reservation_ref'),'Reservation reference',80)
    if ref!=reference(e,d): raise ValueError('PMS reservation reference does not match this booking.')
    if body.get('currency')!='GBP': raise ValueError('This folio supports GBP only; convert/reconcile other currencies before import.')
    revision=vendors.integer(body.get('source_revision'),'PMS revision',1)
    source=text(body.get('source_id'),'PMS folio identifier',100)
    raw=body.get('lines')
    if not isinstance(raw,list) or not 1<=len(raw)<=300: raise ValueError('Provide 1–300 complete, tax-inclusive folio lines.')
    lines=[];ids=set()
    for line in raw:
        if not isinstance(line,dict): raise ValueError('Each folio line must be an object.')
        key=text(line.get('posting_id'),'PMS posting ID',100)
        if key in ids: raise ValueError('Duplicate PMS posting ID: '+key)
        ids.add(key)
        category=line.get('category');payer=line.get('payer','guest')
        if category not in CATEGORIES or payer not in ('master','guest'): raise ValueError('Choose a valid charge category and payer allocation.')
        guest=text(line.get('guest_ref'),'Guest reference',80) if payer=='guest' else None
        amount=vendors.integer(line.get('amount'),'Tax-inclusive line amount in pence',-100000000,100000000)
        if not amount: raise ValueError('Folio charges cannot be zero.')
        if amount<0 and category!='adjustment': raise ValueError('Negative charges must be explicit adjustment lines.')
        lines.append(dict(posting_id=key,description=text(line.get('description'),'Charge description'),category=category,payer=payer,guest_ref=guest,amount=amount))
    if not any(l['category']=='accommodation' and l['payer']=='master' and l['amount']>0 for l in lines):
        raise ValueError('Import the complete folio, including master accommodation charges, not only incidentals.')
    receipts=body.get('payments',[])
    if not isinstance(receipts,list) or len(receipts)>300: raise ValueError('Payments must be a list of at most 300 PMS receipts.')
    payments=[];ids=set()
    for receipt in receipts:
        if not isinstance(receipt,dict): raise ValueError('Each receipt must be an object.')
        key=text(receipt.get('payment_id'),'PMS receipt ID',100)
        if key in ids: raise ValueError('Duplicate PMS receipt ID.')
        ids.add(key)
        payer=receipt.get('payer','master')
        if payer not in ('master','guest'): raise ValueError('Choose a valid receipt payer.')
        guest=text(receipt.get('guest_ref'),'Guest reference',80) if payer=='guest' else None
        amount=vendors.integer(receipt.get('amount'),'Receipt amount in pence',1)
        method=receipt.get('method')
        if method not in ('airwallex','external'): raise ValueError('Receipt method must be airwallex or external.')
        proof=text(receipt.get('reference'),'Provider intent ID or external receipt reference',160)
        payments.append(dict(payment_id=key,payer=payer,guest_ref=guest,amount=amount,method=method,reference=proof))
    payload=dict(reservation_ref=ref,currency='GBP',source_id=source,source_revision=revision,lines=lines,payments=payments)
    payload['hash']=digest(payload)
    return payload

class Service:
    def __init__(self,engine): self.e=engine

    def view(self,deal_id,public=False):
        e=self.e
        with e.lock:
            d=e.deal(deal_id);f=d.get('folio') or {};snap=f.get('snapshot')
            if not d.get('booking') or d['booking']['status']!='confirmed': raise ValueError('Confirm the booking before reconciling a final folio.')
            base=e._version(d,d['accepted_version']);master=sum(l['amount'] for l in (snap or {}).get('lines',[]) if l['payer']=='master')
            guest_total=sum(l['amount'] for l in (snap or {}).get('lines',[]) if l['payer']=='guest')
            credits=cash(e,d);balance=master-credits if snap else None
            guests={}
            for l in (snap or {}).get('lines',[]):
                if l['payer']=='guest': guests[l['guest_ref']]=guests.get(l['guest_ref'],0)+l['amount']
            for r in f.get('receipt_journal',[]):
                if r['payer']=='guest': guests[r['guest_ref']]=guests.get(r['guest_ref'],0)-r['amount']
            approved=bool(snap and f.get('approved_hash')==snap['hash'] and f.get('agreement_hash')==base['hash'])
            blocks=[]
            if any(o['status']=='mismatch' for o in pending(e,d)):blocks.append('A provider payment mismatch requires manual reconciliation.')
            if d['finance']['refund_owed'] or any(o['status'] in ('submitting','unknown','created','pending') for o in e._refund_ops(d)):
                blocks.append('Resolve the existing reservation refund before collecting a final balance.')
            if e.now()[:10]<=max(base['terms']['rooms']):
                blocks.append('Final reconciliation is available after the booked stay ends.')
            if snap and master<0:blocks.append('Master folio total cannot be negative.')
            if snap and master<base['pricing']['total']:
                blocks.append('PMS master charges are below the accepted contract; reconcile a separately approved adjustment before collection or finalization.')
            if not approved:blocks.append('Hotel manager must review and approve this exact PMS snapshot and payer allocation.')
            if balance is not None and balance<0:blocks.append('Credit due: manager reconciliation/refund is required; no new collection is allowed.')
            if any(v<0 for v in guests.values()):blocks.append('A guest folio has a credit due; reconcile it with the PMS.')
            if f.get('document'):status='finalized'
            elif not snap:status='not_imported'
            elif balance is not None and balance<0:status='credit_due'
            elif not approved:status='review_required'
            elif balance>0:status='balance_due'
            elif any(v>0 for v in guests.values()):status='guest_balances_open'
            else:status='settled'
            ops=operations(e,d);latest=ops[-1] if ops else None
            out={'deal_id':d['id'],'company':d['company'],'checkout_reached':e.now()[:10]>max(base['terms']['rooms']),'linked':bool(f),'reservation_ref':reference(e,d),'status':status,
                 'reservation_total':base['pricing']['total'],'master_total':master if snap else None,
                 'guest_total':guest_total,'final_total':master+guest_total if snap else None,
                 'variance':master+guest_total-base['pricing']['total'] if snap else None,
                 'receipts':[{k:x[k] for k in ('type','amount','provider_id','at')} for x in e.s['ledger'] if x['deal_id']==d['id'] and x['type'] in RECEIPTS | {'refund_completed'}],
                 'paid':credits,'airwallex_paid':sum(x['amount'] for x in e.s['ledger'] if x['deal_id']==d['id'] and x['type'] in ('deposit_received','folio_received','refund_completed')),'external_paid':sum(x['amount'] for x in e.s['ledger'] if x['deal_id']==d['id'] and x['type']=='folio_external_received'),'balance_due':max(0,balance) if balance is not None else None,
                 'credit_due':max(0,-balance) if balance is not None else 0,'guest_balances':guests,
                 'approved':approved,'snapshot_hash':(snap or {}).get('hash'),'snapshot':copy.deepcopy(snap),
                 'receipt_journal':copy.deepcopy(f.get('receipt_journal',[])), 'blocks':blocks,
                 'payment_status':latest['status'] if latest else None,'payment_url':self.url(d) if latest and latest['status'] in ('created','unknown','submitting') else None,
                 'document':copy.deepcopy(f.get('document')), 'simulated_pms':(snap or {}).get('source_id','').startswith('DEMO-')}
            if public:
                if not approved: raise ValueError('Final folio has not been approved for the payer.')
                out['snapshot']['lines']=[l for l in out['snapshot']['lines'] if l['payer']=='master']
                out['snapshot']['payments']=[]
                for key in ('receipt_journal','guest_balances'):out.pop(key,None)
                if out.get('document'):
                    for key in ('guest_lines','guest_total','grand_total'):out['document'].pop(key,None)
                out['guest_total']=None;out['final_total']=out['master_total'];out['variance']=out['master_total']-out['reservation_total']
            return out

    def url(self,d,suffix='pay'):
        return f"{self.e.public_base}/folio/{d['ref']}/{suffix}?t={d['buyer_token']}"

    def action(self,deal_id,command,body,actor='hotel_manager'):
        e=self.e
        with e.lock:
            if actor!='hotel_manager':raise ValueError('Only hotel management can reconcile and approve the folio.')
            d=e.deal(deal_id);self.view(deal_id)
            if not isinstance(body,dict):raise ValueError('Folio payload must be an object.')
            if command=='demo_checkout':
                if e.provider.name!='simulated':raise ValueError('Use the PMS checkout date for sandbox/live payment providers.')
                from datetime import timedelta
                departure=(date.fromisoformat(max(e._version(d,d['accepted_version'])['terms']['rooms']))+timedelta(days=1)).isoformat()
                if e.now()[:10]<departure:e.advance_clock(to=departure+'T12:00:00+00:00')
                return self.view(deal_id)
            if command=='link':
                if d.get('folio'):raise ValueError('The reservation binding is immutable after the first link/import.')
                ref=text(body.get('reservation_ref'),'PMS reservation reference',80)
                if any(reference(e,x)==ref for x in e.s['deals'].values() if x['id']!=d['id']):raise ValueError('This reservation reference is already linked to another inquiry.')
                d['folio']={'reservation_ref':ref};e._save();return self.view(deal_id)
            if command=='receipt':
                f=d.get('folio') or {};snap=f.get('snapshot')
                if not snap:raise ValueError('Import a folio before recording a PMS receipt.')
                payload={**copy.deepcopy(snap),'source_revision':snap['source_revision']+1,'payments':f.get('receipt_journal',[])+[body.get('receipt')],'external_payments_attested':body.get('external_payments_attested')}
                return self.action(deal_id,'import',payload,actor)
            if command in ('import','simulate'):
                base=e._version(d,d['accepted_version'])
                if e.now()[:10]<=max(base['terms']['rooms']):raise ValueError('Advance to checkout before importing the final folio.')
                if d['status']!='confirmed':raise ValueError('Resolve pending amendments before importing a folio.')
                f=d.get('folio') or {}
                if f.get('document'):raise ValueError('The final folio is issued and immutable; use a separate approved adjustment.')
                if pending(e,d):raise ValueError('An existing final payment is unresolved. Check it before changing the bill.')
                if command=='simulate':
                    old=f.get('snapshot');rev=(old or {}).get('source_revision',0)+1
                    body={'reservation_ref':reference(e,d),'source_id':'DEMO-'+reference(e,d),'source_revision':rev,'currency':'GBP',
                        'lines':[{'posting_id':'reservation','description':'Agreed reservation package','category':'accommodation','payer':'master','amount':base['pricing']['total']},
                                 {'posting_id':'minibar','description':'Minibar (demo)','category':'minibar','payer':'guest','guest_ref':'guest-001','amount':50000},
                                 {'posting_id':'incidental','description':'Additional event services (demo)','category':'incidentals','payer':'master','amount':150000}], 'payments':[]}
                snap=normalize(e,d,body);old=f.get('snapshot')
                if old and snap['hash']==old['hash']:return self.view(deal_id)
                if old and (snap['source_id']!=old['source_id'] or snap['source_revision']<=old['source_revision']):raise ValueError('Use the same PMS folio ID with a newer revision; a changed snapshot cannot reuse its revision.')
                # Validate the entire import before touching receipts or cash.
                journal=copy.deepcopy(f.get('receipt_journal',[]));by_id={r['payment_id']:r for r in journal}
                known_refs={r['reference']:r for r in journal}
                for r in snap['payments']:
                    if r['payment_id'] in by_id:
                        if r!=by_id[r['payment_id']]:raise ValueError('A recorded PMS receipt cannot change amount or identity.')
                        continue
                    if r['reference'] in known_refs:raise ValueError('A payment reference cannot be credited twice under different PMS receipt IDs.')
                    if any(x['id']!=d['id'] and any(p['reference']==r['reference'] for p in (x.get('folio') or {}).get('receipt_journal',[])) for x in e.s['deals'].values()):raise ValueError('This receipt reference is allocated to another reservation.')
                    if r['method']=='airwallex':
                        match=next((x for x in e.s['ledger'] if x['deal_id']==d['id'] and x['provider_id']==r['reference'] and x['type'] in ('deposit_received','folio_received') and x['amount']==r['amount']),None)
                        if not match or r['payer']!='master':raise ValueError('Airwallex receipts must match a verified payment on this reservation.')
                    else:
                        if body.get('external_payments_attested') is not True:
                            raise ValueError('Hotel management must attest external PMS receipts; they are not Airwallex-verified payments.')
                        if any(x['provider_id']==r['reference'] for x in e.s['ledger']):
                            raise ValueError('This payment reference is already credited or belongs to another reservation.')
                    journal.append(r);by_id[r['payment_id']]=r;known_refs[r['reference']]=r
                for r in journal:
                    if r['method']=='external' and r['payer']=='master':
                        e._ledger('pms:'+digest([e.s['run_id'],reference(e,d),r['payment_id']]),d,'folio_external_received',r['amount'],{'id':'pms:'+r['payment_id']},r['reference'])
                d['folio']={**f,'reservation_ref':snap['reservation_ref'],'snapshot':snap,'receipt_journal':journal,
                    'approved_hash':None,'imported_at':e.now(),'imported_by':actor,'history':f.get('history',[])+([copy.deepcopy(old)] if old else [])}
                e.log(d['id'],actor,'folio_imported',f"Imported PMS folio {snap['source_id']} revision {snap['source_revision']}; payer allocations need review.")
            elif command=='approve':
                f=d.get('folio') or {};snap=f.get('snapshot')
                if not snap or body.get('snapshot_hash')!=snap['hash']:raise ValueError('Approval must name the current PMS snapshot.')
                if f.get('document'):raise ValueError('This final folio has already been issued.')
                f.update(approved_hash=snap['hash'],agreement_hash=e._version(d,d['accepted_version'])['hash'],approved_at=e.now(),approved_by=actor)
                e.log(d['id'],actor,'folio_approved','Hotel manager approved the final charges and master/guest allocation.')
            elif command=='checkout':self.checkout(d)
            elif command=='cancel_checkout':self.cancel_checkout(d)
            elif command=='check':self.check(d)
            elif command=='finalize':self.finalize(d)
            else:raise ValueError('Unknown folio action.')
            e._save();return self.view(deal_id)

    def checkout(self,d):
        e=self.e;v=self.view(d['id'])
        if v['blocks']:raise ValueError('; '.join(v['blocks']))
        if v['balance_due']<=0:raise ValueError('No master balance remains to collect.')
        live=pending(e,d)
        if live:
            self.check(d);return
        # Re-read old failed/cancelled intents as well before opening a new obligation.
        self.check(d);v=self.view(d['id'])
        if v['blocks'] or not v['balance_due']:raise ValueError('Balance changed while verifying previous receipts. Refresh the folio.')
        oid=e._id('op_');f=d['folio'];op={'id':oid,'kind':'folio_payment','deal_id':d['id'],'run_id':e.s['run_id'],
            'version':d['accepted_version'],'folio_hash':f['snapshot']['hash'],'amount':v['balance_due'],'currency':'GBP',
            'request_id':f"{e.s['run_id']}-{oid}",'merchant_order_id':f"{e.s['run_id']}-{d['ref']}-folio-{oid}",
            'payment_intent_id':None,'status':'submitting','created_at':e.now(),'provider_status':None}
        e.s['operations'][oid]=op;e._save()
        try:
            pi=e.provider.create_intent(op['request_id'],op['amount'],'GBP',op['merchant_order_id'],
                {'run_id':e.s['run_id'],'operation_id':oid,'obligation':'post-stay folio balance','reservation_ref':reference(e,d),'folio_hash':op['folio_hash']},
                return_url=self.url(d,'return'),connected_account_id=e.hotel_account)
            op.update(payment_intent_id=pi['id'],client_secret=pi.get('client_secret'),status='created',provider_status=pi['status'])
        except AmbiguousResult as exc:
            op.update(status='unknown',note=str(exc));e._recover(op)
        except AirwallexError as exc:
            op.update(status='failed',note=str(exc));e._save();raise ValueError('Provider refused the final payment: '+str(exc))
        except Exception as exc:
            op.update(status='unknown',note=str(exc));e._save();raise ValueError('Payment creation is unresolved. Check provider status before retrying.')
        e.log(d['id'],'hotel_manager','folio_checkout','Created a consent-based checkout for the outstanding master folio balance; no card charged.')

    def cancel_checkout(self,d):
        e=self.e;self.check(d)
        for op in pending(e,d):
            if op['status']!='created' or not op.get('payment_intent_id'):
                raise ValueError('Recover the unresolved payment before cancelling or revising its bill.')
            op.setdefault('cancel_request_id',op['request_id']+'-cancel');e._save()
            try:
                pi=e.provider.cancel_intent(op['cancel_request_id'],op['payment_intent_id'],'Reconcile the revised final folio before collecting')
                if pi['status']=='CANCELLED':op['status']='cancelled'
                else:self.check(d)
            except Exception as exc:
                op.update(status='unknown',note='Cancellation unresolved: '+str(exc));e._save()
                raise ValueError('Cancellation is unresolved; check the provider before changing charges.')
        e._save()

    def check(self,d):
        e=self.e
        for op in operations(e,d):
            if op['status']=='succeeded':continue
            if op['status'] in ('submitting','unknown'):e._recover(op)
            if not op.get('payment_intent_id'):continue
            try:pi=e.provider.get_intent(op['payment_intent_id'])
            except AirwallexError as exc:
                op['note']='Provider verification pending: '+str(exc);continue
            op['provider_status']=pi['status']
            if pi['status'] in ('CANCELLED','FAILED'):op['status']=pi['status'].lower();continue
            if pi['status']!='SUCCEEDED':continue
            if (pi['amount']!=op['amount'] or pi['currency']!='GBP' or pi.get('merchant_order_id')!=op['merchant_order_id']
                or (pi.get('metadata') or {}).get('operation_id')!=op['id'] or (pi.get('metadata') or {}).get('run_id')!=op['run_id']):
                op.update(status='mismatch',note='Provider payment does not match this folio obligation. Reconcile manually.');continue
            e._ledger('receipt:'+pi['id'],d,'folio_received',op['amount'],op,pi['id'])
            e.fx_journal.process({'id':'card:'+pi['id'],'account':e.hotel_account or 'platform','deal_id':e.s['run_id']+':'+d['id'],
                'currency':op['currency'],'amount':str(Decimal(op['amount'])/100),'reference':op['merchant_order_id'],'verified':True},e.fx_provider)
            op['status']='succeeded'
            e.log(d['id'],'system','folio_payment_verified','Verified final folio payment with Airwallex; receipt recorded once.')
        e._settle_payouts(d)
        self.settle_payouts(d)
        v=self.view(d['id'])
        if v['status']=='settled' and not v['blocks']:self.finalize(d)
        e._save()

    def settle_payouts(self,d):
        e=self.e
        if not e.hotel_account:return
        for receipt in operations(e,d):
            if receipt['status']!='succeeded':continue
            payouts=[o for o in e._payout_ops(d,'payout') if o['payment_intent_id']==receipt['payment_intent_id'] and o['status']!='superseded']
            if any(o['status']=='failed' for o in payouts):continue
            payout=payouts[-1] if payouts else e._create_payout(d,receipt)
            if payout['status'] in ('submitting','unknown'):e._recover_payout(payout)

    def finalize(self,d):
        e=self.e;v=self.view(d['id'])
        if d['folio'].get('document'):return
        if v['status']!='settled' or v['blocks']:raise ValueError('All master/guest balances and credits must be reconciled before issuing the final folio.')
        d['folio']['document']={'number':'FOL-'+digest([e.s['run_id'],d['id'],v['snapshot_hash']])[:14].upper(),
            'reservation_ref':reference(e,d),'company':d['company'],'issued_at':e.now(),'currency':'GBP',
            'reservation_total':v['reservation_total'],'final_master_total':v['master_total'],'paid':v['paid'],'balance':0,
            'lines':[copy.deepcopy(l) for l in v['snapshot']['lines'] if l['payer']=='master'],
            'grand_total':v['final_total'],'guest_total':v['guest_total'],'guest_lines':[copy.deepcopy(l) for l in v['snapshot']['lines'] if l['payer']=='guest'],
            'airwallex_paid':v['airwallex_paid'],'external_paid':v['external_paid'],
            'receipt_ids':[x['provider_id'] for x in e.s['ledger'] if x['deal_id']==d['id'] and x['type'] in RECEIPTS]}
        e.log(d['id'],'system','folio_finalized','Issued immutable, settled final master folio '+d['folio']['document']['number'])


def document_html(doc, internal=False):
    """Standalone printable bill; public copies include only the master payer's charges."""
    from html import escape
    from .calc import gbp
    lines=doc['lines']+(doc.get('guest_lines',[]) if internal else [])
    rows=''.join('<tr><td>'+escape(l['description'])+(' · guest '+escape(l['guest_ref']) if internal and l['payer']=='guest' else '')+'</td><td>'+gbp(l['amount'])+'</td></tr>' for l in lines)
    total=doc.get('grand_total',doc['final_master_total']) if internal else doc['final_master_total']
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Groupy · Final folio</title><style>body{font:16px system-ui;margin:32px auto;padding:16px;max-width:760px}table{width:100%;border-collapse:collapse}td{padding:10px;border-bottom:1px solid #ddd}td:last-child{text-align:right}@media print{button{display:none}}</style>'
        '<h1>Groupy · '+('Reconciled PMS folio (hotel copy)' if internal else 'Final master folio')+'</h1><p>'+escape(doc['number'])+' · Reservation '+escape(doc['reservation_ref'])+'</p><p>'+escape(doc['company'])+' · Issued '+escape(doc['issued_at'])+'</p><table>'+rows+'</table><p>Final total (GBP, tax-inclusive): <b>'+gbp(total)+'</b></p><p>Master Airwallex receipts less refunds: '+gbp(doc['airwallex_paid'])+'</p><p>Master external receipts (staff attested): '+gbp(doc['external_paid'])+'</p><p>Master balance: '+gbp(doc['balance'])+'</p>'+('<p>Guest folios are separately reconciled in the PMS.</p>' if internal else '')+'<button onclick="window.print()">Print / save PDF</button></html>')

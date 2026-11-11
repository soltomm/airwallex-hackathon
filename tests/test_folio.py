"""Post-stay bill/receipt reconciliation and idempotent incremental collection."""
import copy
import json
import pytest
from agentbank.groupsales.engine import Engine,Refused
from agentbank.groupsales.provider import SimulatedProvider
from agentbank.groupsales.folio import operations,reference
from test_group_sales import server,_req

@pytest.fixture
def eng(tmp_path):return Engine(tmp_path/'hotel',SimulatedProvider(tmp_path/'provider.json'))

def booking(e,checked_out=True):
    did=e.ingest_email('Organizer <buyer@folio.example>','Five-night group','Folio test inquiry',wake=False)['deal_id']
    e.record_requirements(did,{'room_type':'king','rooms':5,'guests':10,'arrival':'2027-03-10','departure':'2027-03-15','breakfast':False,'payer':'Folio Example Ltd','budget_includes_taxes':True})
    v=e.propose_offer(did,20000,30,10,'Reservation £5,000')['version']
    d=e.deal(did);e.buyer_action(did,d['buyer_token'],'accept',payload={'version':v})
    e.open_checkout(did);op=e._deposit_op(d)
    e.provider.pay(op['payment_intent_id'],'4035 5010 0000 0008');e.check_payment(did)
    assert e.finance(d)['total']==500000
    if checked_out:e.advance_clock(to='2027-03-15T12:00:00+00:00')
    return d

def payload(e,d,extra=200000,external=350000,guest=False):
    return {'reservation_ref':reference(e,d),'source_id':'PMS-FOLIO-100','source_revision':1,'currency':'GBP',
        'lines':[{'posting_id':'stay','description':'5 rooms × 5 nights','category':'accommodation','payer':'master','amount':500000},
                 {'posting_id':'extras','description':'Minibar and incidentals','category':'incidentals','payer':'guest' if guest else 'master','guest_ref':'guest-001' if guest else None,'amount':extra}],
        'payments':[{'payment_id':'paid-reservation-balance','amount':external,'method':'external','reference':'BANK-PAID-001','payer':'master'}] if external else [],'external_payments_attested':True}

def approve(e,d):return e.folio_action(d['id'],'approve',{'snapshot_hash':e.folio_view(d['id'])['snapshot_hash']})
def collect(e,d):
    e.folio_action(d['id'],'checkout',{});op=operations(e,d)[-1]
    e.provider.pay(op['payment_intent_id'],'4035 5010 0000 0008');e.check_folio_payment(d['id']);return op


def test_5000_paid_7000_final_collects_only_2000_and_issues_folio(eng):
    e=eng;d=booking(e);p=payload(e,d)
    v=e.folio_action(d['id'],'import',p)
    assert (v['reservation_total'],v['final_total'],v['paid'],v['balance_due'])==(500000,700000,500000,200000)
    assert (v['airwallex_paid'],v['external_paid'])==(150000,350000)
    with pytest.raises(Refused,match='approve'):e.folio_action(d['id'],'checkout',{})
    approve(e,d);op=collect(e,d)
    assert op['amount']==200000 and op['status']=='succeeded'
    v=e.folio_view(d['id']);assert v['status']=='finalized' and v['document']['balance']==0
    assert v['document']['final_master_total']==700000 and v['paid']==700000
    assert e.finance(d)['still_due']==0 and e.finance(d)['total']==700000
    ledger=copy.deepcopy(e.s['ledger']);count=len(e.provider.db['intents'])
    e.check_folio_payment(d['id']);e.check_folio_payment(d['id'])
    assert e.s['ledger']==ledger and len(e.provider.db['intents'])==count
    e._settle_payouts(d)
    # Only online receipts are split; the external receipt already reached the hotel outside this platform.
    payout=e.payout_view(d)
    assert payout['hotel_net']+payout['platform_fee']==350000
    restored=Engine(e.var,e.provider)
    assert restored.folio_view(d['id'])['document']==v['document']


def test_reservation_price_is_not_a_receipt_and_guest_charges_are_not_master_debt(eng):
    d=booking(eng);v=eng.folio_action(d['id'],'import',payload(eng,d,external=0,guest=True))
    assert v['paid']==150000 and v['balance_due']==350000 and v['guest_balances']=={'guest-001':200000}
    approve(eng,d);op=collect(eng,d)
    assert op['amount']==350000 and eng.folio_view(d['id'])['status']=='guest_balances_open'
    with pytest.raises(Refused,match='master/guest'):eng.folio_action(d['id'],'finalize',{})
    eng.folio_action(d['id'],'receipt',{'receipt':{'payment_id':'guest-paid','amount':200000,'payer':'guest','guest_ref':'guest-001','method':'external','reference':'GUEST-RECEIPT'},'external_payments_attested':True})
    approve(eng,d);eng.folio_action(d['id'],'finalize',{})
    pub=eng.folio_view(d['id'],public=True)
    assert pub['master_total']==500000 and 'guest-001' not in json.dumps(pub)


def test_snapshot_and_receipt_retries_do_not_double_credit(eng):
    d=booking(eng);p=payload(eng,d);v=eng.folio_action(d['id'],'import',p)
    eng.folio_action(d['id'],'import',p);assert eng.folio_view(d['id'])['paid']==500000
    same=copy.deepcopy(p);same['lines'][1]['amount']=210000
    with pytest.raises(Refused,match='revision'):eng.folio_action(d['id'],'import',same)
    approve(eng,d);same['source_revision']=2
    v=eng.folio_action(d['id'],'import',same)
    assert not v['approved'] and v['paid']==500000 and v['balance_due']==210000
    with pytest.raises(Refused,match='current'):eng.folio_action(d['id'],'approve',{'snapshot_hash':'stale'})
    duplicate=copy.deepcopy(same);duplicate['source_revision']=3;duplicate['payments'][0]['payment_id']='another-id'
    with pytest.raises(Refused,match='twice'):eng.folio_action(d['id'],'import',duplicate)


def test_uncertain_creation_is_recovered_and_existing_checkout_reused(eng):
    d=booking(eng);eng.folio_action(d['id'],'import',payload(eng,d));approve(eng,d)
    eng.provider.knobs['timeout_next_create']=True
    eng.folio_action(d['id'],'checkout',{});op=operations(eng,d)[-1]
    assert op['status']=='created'
    count=len(eng.provider.db['intents'])
    eng.folio_action(d['id'],'checkout',{});assert len(eng.provider.db['intents'])==count
    changed=payload(eng,d);changed['source_revision']=2
    with pytest.raises(Refused,match='unresolved'):eng.folio_action(d['id'],'import',changed)
    eng.provider.pay(op['payment_intent_id'],'4035 5010 0000 0008')
    event={'id':'folio-event','name':'payment_intent.succeeded','data':{'object':{'id':op['payment_intent_id'],'metadata':{'run_id':eng.s['run_id']}}}}
    eng.handle_event(event,verified=True);eng.handle_event(event,verified=True)
    assert eng.folio_view(d['id'])['status']=='finalized'
    assert sum(x['type']=='folio_received' for x in eng.s['ledger'])==1


@pytest.mark.parametrize('change',['amount','currency','merchant_order_id','metadata'])
def test_wrong_provider_payment_never_settles_the_folio(eng,change):
    d=booking(eng);eng.folio_action(d['id'],'import',payload(eng,d));approve(eng,d)
    eng.folio_action(d['id'],'checkout',{});op=operations(eng,d)[-1]
    eng.provider.pay(op['payment_intent_id'],'4035 5010 0000 0008')
    pi=eng.provider.db['intents'][op['payment_intent_id']]
    pi[change]={'amount':1,'currency':'USD','merchant_order_id':'wrong','metadata':{}}[change]
    eng.check_folio_payment(d['id'])
    assert op['status']=='mismatch' and not any(x['type']=='folio_received' for x in eng.s['ledger'])
    assert not eng.folio_view(d['id'])['document']


def test_overpayment_requires_credit_review_and_no_second_collection(eng):
    d=booking(eng);p=payload(eng,d,extra=-100000,external=350000);p['lines'][1]['category']='adjustment'
    v=eng.folio_action(d['id'],'import',p);assert v['credit_due']==100000
    approve(eng,d)
    with pytest.raises(Refused,match='Credit due'):eng.folio_action(d['id'],'checkout',{})
    assert not operations(eng,d)


def test_bad_import_is_atomic_and_pms_reference_is_bound(eng):
    d=booking(eng);before=copy.deepcopy(eng.s['ledger'])
    for mutation in ('reference','duplicate','missing-base','unattested','duplicate-online'):
        p=payload(eng,d)
        if mutation=='reference':p['reservation_ref']='another-reservation'
        if mutation=='duplicate':p['lines'][1]['posting_id']='stay'
        if mutation=='missing-base':p['lines']=p['lines'][1:]
        if mutation=='unattested':p['external_payments_attested']='yes'
        if mutation=='duplicate-online':p['payments'][0]['reference']=eng._deposit_op(d)['payment_intent_id']
        with pytest.raises(Refused):eng.folio_action(d['id'],'import',p)
        assert eng.s['ledger']==before and not d.get('folio')
    with pytest.raises(Refused,match='Only'):eng.folio_action(d['id'],'import',payload(eng,d),actor='seller')


def test_verified_airwallex_receipt_is_matched_not_credited_again(eng):
    d=booking(eng);p=payload(eng,d,external=0);op=eng._deposit_op(d)
    p['payments']=[{'payment_id':'matched-deposit','amount':150000,'method':'airwallex','reference':op['payment_intent_id'],'payer':'master'}]
    v=eng.folio_action(d['id'],'import',p)
    assert v['paid']==150000 and v['external_paid']==0 and v['balance_due']==550000


def test_hotel_folio_api_and_document_are_access_controlled(server,monkeypatch):
    monkeypatch.setattr(server,'ACCESS','folio-access')
    did=next(iter(server.engine.s['deals']))
    for path in (f'/api/deals/{did}/folio','/assets/folio.js'):
        assert _req(server,path)[0]==401
    assert _req(server,f'/api/deals/{did}/folio',{'command':'simulate'})[0]==401
    assert _req(server,'/folio/GS-1001/pay?t=bad')[0]==403


def test_unpaid_checkout_can_be_cancelled_before_correcting_the_pms_bill(eng):
    d=booking(eng);p=payload(eng,d);eng.folio_action(d['id'],'import',p);approve(eng,d)
    eng.folio_action(d['id'],'checkout',{});op=operations(eng,d)[-1]
    eng.folio_action(d['id'],'cancel_checkout',{})
    assert op['status']=='cancelled'
    p['source_revision']=2;p['lines'][1]['amount']=100000
    v=eng.folio_action(d['id'],'import',p)
    assert not v['approved'] and v['balance_due']==100000
    approve(eng,d);collect(eng,d)
    assert eng.folio_view(d['id'])['document']['paid']==600000
    assert len(operations(eng,d))==2


def test_card_decline_does_not_mark_the_folio_paid_or_create_a_second_intent(eng):
    d=booking(eng);eng.folio_action(d['id'],'import',payload(eng,d));approve(eng,d)
    eng.folio_action(d['id'],'checkout',{});op=operations(eng,d)[-1]
    eng.provider.pay(op['payment_intent_id'],'4000000000000002');eng.check_folio_payment(d['id'])
    assert op['status']=='created' and not eng.folio_view(d['id'])['document']
    eng.folio_action(d['id'],'checkout',{})
    assert len(operations(eng,d))==1 and eng.folio_view(d['id'])['balance_due']==200000


def test_internal_folio_includes_guest_allocations_but_payer_copy_does_not(eng):
    from agentbank.groupsales.folio import document_html
    d=booking(eng);eng.folio_action(d['id'],'import',payload(eng,d,guest=True));approve(eng,d)
    # All master charges are already paid; settle guest charges through an attested PMS receipt.
    eng.folio_action(d['id'],'receipt',{'receipt':{'payment_id':'g1','amount':200000,'payer':'guest','guest_ref':'guest-001','method':'external','reference':'guest-001-private-receipt'},'external_payments_attested':True})
    approve(eng,d);eng.folio_action(d['id'],'finalize',{})
    private=eng.folio_view(d['id']);public=eng.folio_view(d['id'],public=True)
    assert private['document']['grand_total']==700000
    assert 'guest-001' in document_html(private['document'],internal=True)
    assert 'guest-001' not in json.dumps(public)
    assert '£7,000' in document_html(private['document'],internal=True)
    assert '£5,000' in document_html(public['document'])


def test_final_folio_cannot_reopen_or_mutate_the_accepted_booking(eng):
    d=booking(eng);eng.folio_action(d['id'],'import',payload(eng,d))
    with pytest.raises(Refused,match='Post-stay'):eng.prepare_reduction(d['id'],rooms_per_night=4)
    approve(eng,d);collect(eng,d)
    p=payload(eng,d);p['source_revision']=2
    with pytest.raises(Refused,match='immutable'):eng.folio_action(d['id'],'import',p)
    count=len(operations(eng,d))
    with pytest.raises(Refused,match='No master balance'):eng.folio_action(d['id'],'checkout',{})
    assert len(operations(eng,d))==count


def test_final_import_requires_checkout_and_demo_advance_is_explicit(eng):
    d=booking(eng,checked_out=False)
    assert not eng.folio_view(d['id'])['checkout_reached']
    with pytest.raises(Refused,match='checkout'):eng.folio_action(d['id'],'import',payload(eng,d))
    assert not d.get('folio')
    eng.folio_action(d['id'],'demo_checkout',{})
    assert eng.folio_view(d['id'])['checkout_reached']
    eng.folio_action(d['id'],'import',payload(eng,d))


def test_buyer_checkout_and_printable_document_escape_pms_content(server,monkeypatch):
    e=server.engine;d=booking(e);p=payload(e,d)
    p['lines'][1]['description']='Incidental </script><script>window.injected=1</script>'
    e.folio_action(d['id'],'import',p);approve(e,d);e.folio_action(d['id'],'checkout',{})
    monkeypatch.setattr(server,'ACCESS','hotel-only-folio')
    path=f"/folio/{d['ref']}"
    code,_,body=_req(server,path+'/pay?t='+d['buyer_token'])
    assert code==200 and 'window.injected=1</script>' not in body
    assert '\\u003c/script>' in body
    assert _req(server,path+'/document?t='+d['buyer_token'])[0]==409
    assert _req(server,path+'/pay?t=wrong')[0]==403
    op=operations(e,d)[-1];e.provider.pay(op['payment_intent_id'],'4035 5010 0000 0008')
    assert _req(server,path+'/return?t='+d['buyer_token'])[0]==200
    code,_,body=_req(server,path+'/document?t='+d['buyer_token'])
    assert code==200 and '&lt;script&gt;' in body and '<script>window.injected' not in body
    private=f"/api/deals/{d['id']}/folio/report"
    assert _req(server,private)[0]==401
    assert _req(server,private,headers={'Cookie':'gs_k=hotel-only-folio'})[0]==200


def test_post_stay_collection_cannot_bypass_the_accepted_contract_price(eng):
    d=booking(eng);p=payload(eng,d,extra=-100000,external=0);p['lines'][1]['category']='adjustment'
    eng.folio_action(d['id'],'import',p);approve(eng,d)
    assert eng.folio_view(d['id'])['balance_due']==250000
    with pytest.raises(Refused,match='below the accepted contract'):eng.folio_action(d['id'],'checkout',{})
    assert not operations(eng,d)

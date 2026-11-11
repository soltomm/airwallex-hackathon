"""Revenue review extends the offer flow without weakening hotel authority."""
import copy
import json
import pytest
from agentbank.groupsales.engine import Engine,Refused
from agentbank.groupsales.provider import SimulatedProvider
from agentbank.groupsales.seller import RulesSeller
from test_group_sales import server,_req

@pytest.fixture
def eng(tmp_path):return Engine(tmp_path/'hotel',SimulatedProvider(tmp_path/'provider.json'))

def inquiry(e):
    did=e.ingest_email('Revenue <sales@demo.example>','Price discussion','Group pricing review',wake=False)['deal_id']
    e.record_requirements(did,{'rooms':5,'guests':5,'room_type':'king','arrival':'2027-03-10','departure':'2027-03-13','payer':'Demo Ltd','breakfast':False,'budget_includes_taxes':True})
    e.propose_offer(did,19000,30,10,'Initial offer')
    return e.deal(did)

def request(e,d,rate=17900):return e.request_pricing_review(d['id'],'Buyer requested a lower rate; sales needs a pricing decision.',rate)
def preview(e,d,rate=17900):return e.pricing_review_action(d['id'],'preview',{'review_id':d['pricing_review']['id'],'rate':rate})
def approve(e,d,p,**extra):return e.pricing_review_action(d['id'],'approve',{'review_id':d['pricing_review']['id'],'rate':p['terms']['rate'],'preview_hash':p['hash'],**extra})

def test_revenue_review_to_requote_and_booking_uses_existing_checkout(eng):
    e=eng;d=inquiry(e);r=request(e,d)
    assert r['owner']=='Revenue team' and r['requested_rate']==17900 and r['latest_rate']==19000
    assert e.waiting_on(d)=='Revenue team: pricing review'
    assert d['versions'][0]['status']=='superseded'
    with pytest.raises(Refused):e.buyer_action(d['id'],d['buyer_token'],'accept',payload={'version':1})
    with pytest.raises(Refused,match='Awaiting revenue'):e.propose_offer(d['id'],19000,30,10,'Bypass')
    p=preview(e,d)
    assert p['terms']['deposit_pct']==40 and not p['problems']
    assert p['pricing']['total']==268500
    approve(e,d,p,note='Agree the lower rate with a 40% deposit.')
    assert d['pricing_review']['status']=='approved' and d['handoff'] is None
    assert d['status']=='approved_to_send'
    version=d['versions'][-1];assert version['status']=='approved'
    e.send_offer(d['id'],version['n'])
    assert d['status']=='offer_sent'
    assert 'Agree the lower rate' not in d['thread'][-1]['text']
    e.buyer_action(d['id'],d['buyer_token'],'accept',payload={'version':version['n']})
    e.open_checkout(d['id']);op=e._deposit_op(d)
    e.provider.pay(op['payment_intent_id'],'4035 5010 0000 0008');e.check_payment(d['id'])
    assert d['booking']['status']=='confirmed'
    restored=Engine(e.var,e.provider)
    assert restored.deal(d['id'])['pricing_review']['rate']==17900


def test_hard_floor_and_margin_cannot_be_approved(eng):
    d=inquiry(eng);request(eng,d,10000)
    p=preview(eng,d,10000)
    assert any('absolute minimum' in x for x in p['problems'])
    before=copy.deepcopy(d['versions'])
    with pytest.raises(Refused,match='Cannot approve'):approve(eng,d,p)
    assert d['versions']==before and d['pricing_review']['status']=='pending'
    eng.scenario['rms']['min_contribution_margin_pct']=99
    p=preview(eng,d,19000)
    assert any('margin' in x for x in p['problems'])
    with pytest.raises(Refused,match='Cannot approve'):approve(eng,d,p)


def test_counteroffer_preview_and_exact_decision_cannot_replay(eng):
    d=inquiry(eng);request(eng,d,16000)
    p=preview(eng,d,18500)
    with pytest.raises(Refused,match='Preview'):eng.pricing_review_action(d['id'],'approve',{'review_id':d['pricing_review']['id'],'rate':19000,'preview_hash':p['hash']})
    approve(eng,d,p)
    assert d['versions'][-1]['terms']['rate']==18500
    with pytest.raises(Refused,match='already'):approve(eng,d,p)


@pytest.mark.parametrize('change',['requirements','buyer','policy','vendor'])
def test_approval_revalidates_latest_terms_and_rules(eng,change):
    d=inquiry(eng);request(eng,d);p=preview(eng,d)
    if change=='requirements':eng.record_requirements(d['id'],{'rooms':6})
    if change=='buyer':eng.buyer_action(d['id'],d['buyer_token'],'reply',text='Actually we need different dates.')
    if change=='policy':eng.scenario['policy']['attrition']['standard_pct']=5
    if change=='vendor':eng.vendor_action(d['id'],'configure',{'enabled':True})
    # Scope changes invalidate the request; changed commercial rules invalidate its preview.
    with pytest.raises(Refused):approve(eng,d,p)


def test_review_dedup_role_notes_rejection_and_buyer_privacy(eng):
    d=inquiry(eng);r=request(eng,d);assert request(eng,d)['id']==r['id']
    with pytest.raises(Refused,match='Only'):eng.pricing_review_action(d['id'],'reject',{'review_id':r['id'],'note':'No'},actor='seller')
    eng.pricing_review_action(d['id'],'note',{'review_id':r['id'],'note':'Internal sales context'})
    view=eng.deal_view(d['id'],'buyer')
    assert 'pricing_review' not in view and 'Internal sales context' not in json.dumps(view)
    eng.pricing_review_action(d['id'],'reject',{'review_id':r['id'],'note':'Keep the current minimum price.'})
    assert d['pricing_review']['status']=='rejected' and d['status']=='handoff'
    assert request(eng,d)['id']==r['id'] # automatic agent retries do not loop rejected tasks
    renewed=eng.request_pricing_review(d['id'],'Sales proposes another review',17500,actor='hotel staff')
    assert renewed['id']!=r['id'] and d['pricing_review_history'][-1]['status']=='rejected'


def test_rules_seller_routes_below_authority_counter_to_revenue(eng):
    d=inquiry(eng)
    # King: absolute minimum £170, prepayment floor £175, seller's floor £180. Below the prepayment floor is revenue's call.
    result=RulesSeller(eng)._counter(d['id'],{'rate':17200})
    assert 'revenue' in result and d['pricing_review']['requested_rate']==17200
    assert d['status']=='handoff' and len(d['versions'])==1


def test_rules_seller_trades_prepayment_between_the_prepay_floor_and_its_own(eng):
    d=inquiry(eng)
    result=RulesSeller(eng)._counter(d['id'],{'rate':17900})
    assert 'prepayment' in result and not d.get('pricing_review')
    t=d['versions'][-1]['terms']
    assert (t['rate'],t['deposit_pct'])==(17900,eng.policy['prepay']['min_deposit_pct'])


def test_revenue_routes_are_protected_and_preview_safe(server,monkeypatch):
    monkeypatch.setattr(server,'ACCESS','pricing-demo')
    did=next(iter(server.engine.s['deals']))
    path=f'/api/deals/{did}/pricing_review'
    assert _req(server,path)[0]==401
    assert _req(server,path,{'action':'request'})[0]==401
    assert _req(server,'/assets/revenue.js')[0]==401


def test_review_cannot_bypass_inventory_or_be_handed_back_undecided(eng):
    d=inquiry(eng);request(eng,d)
    with pytest.raises(Refused,match='Resolve'):eng.hand_back(d['id'],'Ignore pricing review')
    for night in ('2027-03-10','2027-03-11','2027-03-12'):
        eng._inv(night,create=True)['king']['booked']=16
    p=preview(eng,d,19000)
    assert not p['fits_inventory']
    with pytest.raises(Refused,match='inventory'):approve(eng,d,p)
    eng.close_deal(d['id'],'Client withdrew')
    assert d['pricing_review']['status']=='withdrawn' and d['status']=='lost'
    with pytest.raises(Refused):request(eng,d)


def test_sales_can_route_a_generic_pricing_handoff_via_protected_api(server):
    e=server.engine;d=inquiry(e)
    e.escalate(d['id'],'Price could not be agreed')
    path=f"/api/deals/{d['id']}/pricing_review"
    code,_,body=_req(server,path,{'action':'request','requested_rate':17900})
    assert code==200 and json.loads(body)['requested_rate']==17900
    code,_,body=_req(server,path);r=json.loads(body)
    assert code==200 and r['link'].endswith('/'+r['id'])
    code,_,body=_req(server,path,{'action':'preview','payload':{'review_id':r['id'],'rate':17900}})
    p=json.loads(body);assert code==200 and not p['problems']
    code,_,body=_req(server,path,{'action':'approve','payload':{'review_id':r['id'],'rate':17900,'preview_hash':p['hash']}})
    assert code==200 and json.loads(body)['status']=='approved'


@pytest.mark.parametrize('who',['buyer','seller'])
def test_declining_the_inquiry_withdraws_revenue_task(eng,who):
    d=inquiry(eng);request(eng,d)
    if who=='buyer':eng.buyer_action(d['id'],d['buyer_token'],'decline',text='We have booked elsewhere.')
    else:eng.decline(d['id'],'Unable to meet the request','We cannot offer this package.')
    assert d['pricing_review']['status']=='withdrawn'
    assert eng.waiting_on(d)!='Revenue team: pricing review'

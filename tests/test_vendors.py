"""Third-party requests are real persisted children; deliveries/contacts are demo fixtures."""
import copy
import json
from datetime import datetime, timedelta
import pytest
from agentbank.groupsales.engine import Engine, Refused
from agentbank.groupsales.provider import SimulatedProvider
from agentbank.groupsales import vendors, calc
from agentbank.groupsales.seller import ClaudeSeller
from test_group_sales import server, _req

@pytest.fixture
def eng(tmp_path):
    e=Engine(tmp_path/'hotel',SimulatedProvider(tmp_path/'provider.json'))
    return e

def deal(e, rooms=5, meeting=None, unsupported=None):
    did=e.ingest_email('Casey <casey@new.example>','Group '+str(len(e.s['deals'])),'Unique group '+str(len(e.s['deals'])),wake=False)['deal_id']
    e.record_requirements(did,{'room_type':'king','rooms':rooms,'guests':rooms,'arrival':'2027-03-10','departure':'2027-03-13','breakfast':False,'meeting':meeting,'payer':'Example Demo Ltd','unsupported':unsupported or []})
    return e.deal(did)

def configure(e,d,host=None,meetings=0):
    return e.vendor_action(d['id'],'configure',{'enabled':True,'host_rooms':host if host is not None else d['requirements']['rooms'],'external_meetings':meetings})

def request(e,d,category='av',quantity=1,vendor='sound',covers=None):
    e.vendor_action(d['id'],'request',{'vendor_id':vendor,'category':category,'title':category+' package','start':'2027-03-10','end':'2027-03-13','quantity':quantity,'covers':covers or []})
    return d['third_party']['requests'][-1]['id']

def quote(e,d,rid,cost=10000,markup=100,kind='percent'):
    e.vendor_action(d['id'],'quote',{'request_id':rid,'unit_cost':cost,'markup_type':kind,'markup':markup,'valid_until':(datetime.fromisoformat(e.now())+timedelta(days=2)).isoformat()})
    return next(r for r in d['third_party']['requests'] if r['id']==rid)['quote']['hash']

def confirmed(e,d,rid,cost=10000):
    h=quote(e,d,rid,cost)
    e.vendor_action(d['id'],'approve',{'request_id':rid,'quote_hash':h})
    e.vendor_action(d['id'],'confirm',{'request_id':rid,'quote_hash':h})
    return h

def propose(e,d):return e.propose_offer(d['id'],19000,30,10,'Master group proposal')


def test_full_master_booking_and_delivery_tracking(eng):
    e=eng;d=deal(e);configure(e,d);rid=request(e,d);confirmed(e,d,rid)
    result=propose(e,d);assert result['status']=='pending_approval'
    v=d['versions'][-1];assert v['pricing']['total']==5*3*19000+20000
    assert v['pricing']['deposit']==91500
    a=e.s['approvals'][result['approval_id']];e.decide_approval(a['id'],'approve','hotel_manager');e.send_offer(d['id'],v['n'])
    e.buyer_action(d['id'],d['buyer_token'],'accept',payload={'version':v['n']})
    checkout=e.open_checkout(d['id']);op=e._deposit_op(d)
    e.provider.pay(op['payment_intent_id'],'4035 5010 0000 0008');e.check_payment(d['id'])
    assert d['booking']['status']=='confirmed'
    e.vendor_action(d['id'],'execution',{'request_id':rid,'status':'delivered','note':'Equipment tested and handed over.'})
    e.vendor_action(d['id'],'payment_status',{'request_id':rid,'status':'paid'})
    restored=Engine(e.var,e.provider).deal(d['id'])
    assert restored['third_party']['requests'][0]['payment_status']=='paid'
    assert restored['third_party']['requests'][0]['execution_note']=='Equipment tested and handed over.'
    assert all(op['kind']!='vendor_payout' for op in e.s['operations'].values())
    with pytest.raises(Refused,match='amendment'):quote(e,d,rid,12000)


def test_provisional_is_not_a_bookable_offer_and_omits_private_costs(eng):
    d=deal(eng);configure(eng,d);rid=request(eng,d)
    with pytest.raises(Refused,match='Master contract not ready'):propose(eng,d)
    eng.vendor_action(d['id'],'provisional',{})
    assert d['versions']==[]
    draft=d['third_party']['drafts'][0];assert draft['not_bookable'] and draft['unpriced_services']==['av package']
    message=d['thread'][-1]['text'];assert 'PROVISIONAL' in message and 'Unpriced services excluded' in message
    quote(eng,d,rid);eng.vendor_action(d['id'],'provisional',{})
    assert 'unit_cost' not in json.dumps(d['thread'][-1])
    with pytest.raises(Refused):propose(eng,d)


def test_quote_changes_invalidate_vendor_and_master_approvals(eng):
    d=deal(eng);configure(eng,d);rid=request(eng,d);old=confirmed(eng,d,rid)
    result=propose(eng,d);oldterms=copy.deepcopy(d['versions'][-1]['terms'])
    quote(eng,d,rid,12000)
    assert eng.s['approvals'][result['approval_id']]['status']=='stale'
    assert d['versions'][-1]['status']=='superseded'
    assert oldterms==d['versions'][-1]['terms']
    with pytest.raises(Refused,match='stale'):eng.vendor_action(d['id'],'approve',{'request_id':rid,'quote_hash':old})
    with pytest.raises(Refused):eng.decide_approval(result['approval_id'],'approve','hotel_manager')


def test_partner_capacity_does_not_become_hotel_inventory(eng):
    d=deal(eng,100,meeting={'attendees':100,'layout':'classroom','date':'2027-03-11','hours':'09:00-17:00'})
    before=copy.deepcopy(eng.s['inventory'])
    configure(eng,d,host=5,meetings=3)
    room=request(eng,d,'rooms',95,'riverside');confirmed(eng,d,room,10000)
    meeting=request(eng,d,'meeting',3,'cityspaces');confirmed(eng,d,meeting,10000)
    result=propose(eng,d);assert result['status']=='pending_approval'
    t=d['versions'][-1]['terms'];assert sum(t['rooms'].values())==15 and t['meeting'] is None
    assert sum(l['quantity'] for l in t['vendor_lines'] if l['category']=='rooms')==285
    assert eng.s['inventory']==before
    result=eng.vendor_workspace(d['id'])
    assert not result['problems'] and result['preview']['fits_hotel_inventory']


def test_shared_partner_capacity_is_checked_across_inquiries(eng):
    d=deal(eng,100);configure(eng,d,host=5);rid=request(eng,d,'rooms',95,'riverside');confirmed(eng,d,rid,10000)
    other=deal(eng,50);configure(eng,other,host=5);r=request(eng,other,'rooms',45,'riverside');h=quote(eng,other,r)
    eng.vendor_action(other['id'],'approve',{'request_id':r,'quote_hash':h})
    with pytest.raises(Refused,match='capacity'):eng.vendor_action(other['id'],'confirm',{'request_id':r,'quote_hash':h})
    assert other['third_party']['requests'][0]['status']=='awaiting_confirmation'


def test_coverage_and_margin_are_enforced(eng):
    d=deal(eng,100);configure(eng,d,host=5);r=request(eng,d,'rooms',90,'riverside');confirmed(eng,d,r,10000)
    with pytest.raises(Refused,match='exactly cover'):propose(eng,d)
    other=deal(eng);configure(eng,other);r=request(eng,other)
    h=quote(eng,other,r,1000000,markup=0);eng.vendor_action(other['id'],'approve',{'request_id':r,'quote_hash':h});eng.vendor_action(other['id'],'confirm',{'request_id':r,'quote_hash':h})
    with pytest.raises(Refused,match='margin'):propose(eng,other)


def test_unsupported_services_need_explicit_quoted_coverage(eng):
    d=deal(eng,unsupported=['group dinner']);configure(eng,d);rid=request(eng,d,'catering',5,'table')
    confirmed(eng,d,rid)
    with pytest.raises(Refused,match='outside supported'):propose(eng,d)
    eng.vendor_action(d['id'],'cancel',{'request_id':rid})
    rid=request(eng,d,'catering',5,'table',covers=['group dinner']);confirmed(eng,d,rid)
    assert propose(eng,d)['status']=='pending_approval'


def test_quote_review_roles_expiry_and_cross_inquiry_identity(eng):
    d=deal(eng);configure(eng,d);r=request(eng,d);h=quote(eng,d,r)
    with pytest.raises(Refused,match='Only hotel'):eng.vendor_action(d['id'],'approve',{'request_id':r,'quote_hash':h},actor='seller')
    with pytest.raises(Refused,match='reason'):eng.vendor_action(d['id'],'reject',{'request_id':r,'quote_hash':h})
    eng.vendor_action(d['id'],'reject',{'request_id':r,'quote_hash':h,'reason':'Need projector included.'})
    assert d['third_party']['requests'][0]['status']=='revision_requested'
    h=confirmed(eng,d,r);eng.advance_clock(hours=49)
    with pytest.raises(Refused,match='expired'):propose(eng,d)
    other=deal(eng);configure(eng,other)
    with pytest.raises(Refused,match='not found'):eng.vendor_action(other['id'],'cancel',{'request_id':r})


def test_public_master_lines_never_expose_supplier_cost_or_markup(eng):
    d=deal(eng);configure(eng,d);rid=request(eng,d);confirmed(eng,d,rid);result=propose(eng,d)
    eng.decide_approval(result['approval_id'],'approve','hotel_manager');eng.send_offer(d['id'],1)
    view=eng.deal_view(d['id'],for_='buyer');text=json.dumps(view)
    assert 'unit_cost' not in text and 'markup' not in text and 'quote_hash' not in text and 'vendor_id' not in text
    assert view['versions'][0]['terms']['vendor_lines'][0]['total']==20000
    assert 'third_party' not in view


def test_invalid_mutations_are_transactional(eng):
    d=deal(eng);configure(eng,d);r=request(eng,d);before=copy.deepcopy(d['third_party'])
    for payload in [{'unit_cost':-1,'markup_type':'percent','markup':20},{'unit_cost':1000,'markup_type':'percent','markup':-10}]:
        with pytest.raises(Refused):eng.vendor_action(d['id'],'quote',{'request_id':r,**payload})
        assert d['third_party']==before
    with pytest.raises(Refused):eng.vendor_action(d['id'],'execution',{'request_id':r,'status':'delivered'})
    assert d['third_party']==before


def test_flat_markup_is_per_subitem_not_per_unit(eng):
    d=deal(eng);configure(eng,d);r=request(eng,d,quantity=3)
    quote(eng,d,r,cost=1000,markup=500,kind='flat')
    line=vendors.snapshots(d)[0]
    assert (line['cost'],line['total'])==(3000,3500)


def test_vendor_routes_are_hotel_only(server,monkeypatch):
    monkeypatch.setattr(server,'VENDORS',True)
    monkeypatch.setattr(server,'ACCESS','vendor-demo-token')
    did=next(iter(server.engine.s['deals']))
    for path in ['/api/vendor_directory',f'/api/deals/{did}/vendors','/assets/vendors.js','/api/vendors/sound/history']:
        assert _req(server,path)[0]==401
    assert _req(server,f'/api/deals/{did}/vendors',{'command':'configure','payload':{'enabled':True}})[0]==401
    headers={'Cookie':'gs_k=vendor-demo-token','Content-Type':'application/json'}
    code,_,body=_req(server,'/api/vendor_directory',headers=headers);assert code==200
    directory=json.loads(body);assert directory['simulated'] and len(directory['team'])==4
    assert all('.example' in p['email'] for p in directory['providers'])
    code,_,body=_req(server,f'/api/deals/{did}/vendors',{'command':'configure','payload':{'enabled':True,'host_rooms':0}},headers)
    assert code==200 and json.loads(body)['workspace']['enabled']


def test_enabling_before_intake_does_not_silently_allocate_zero_host_rooms(eng):
    did=eng.ingest_email('Pending <pending@example.test>','Pending inquiry','Not parsed yet',wake=False)['deal_id']
    eng.vendor_action(did,'configure',{'enabled':True,'host_rooms':None})
    eng.record_requirements(did,{'room_type':'king','rooms':5,'guests':5,'arrival':'2027-03-10','departure':'2027-03-13','breakfast':False,'payer':'Example Ltd'})
    view=eng.vendor_workspace(did)
    assert view['workspace']['host_rooms'] is None
    assert view['preview']['pricing']['room_nights']==15


def test_inquiry_date_change_invalidates_vendor_quote(eng):
    d=deal(eng);configure(eng,d);rid=request(eng,d);h=confirmed(eng,d,rid)
    eng.record_requirements(d['id'],{'arrival':'2027-04-10','departure':'2027-04-13'})
    with pytest.raises(Refused,match='requirements changed'):propose(eng,d)


def test_quote_expiry_blocks_customer_acceptance_before_payment(eng):
    d=deal(eng);configure(eng,d);rid=request(eng,d)
    eng.vendor_action(d['id'],'quote',{'request_id':rid,'unit_cost':1000,'markup_type':'percent','markup':20,'valid_until':(datetime.fromisoformat(eng.now())+timedelta(hours=1)).isoformat()})
    h=d['third_party']['requests'][0]['quote']['hash'];eng.vendor_action(d['id'],'approve',{'request_id':rid,'quote_hash':h});eng.vendor_action(d['id'],'confirm',{'request_id':rid,'quote_hash':h})
    response=propose(eng,d);eng.decide_approval(response['approval_id'],'approve','hotel_manager');eng.send_offer(d['id'],1)
    eng.advance_clock(hours=2)
    with pytest.raises(Refused,match='expired'):eng.buyer_action(d['id'],d['buyer_token'],'accept',payload={'version':1})
    assert not d['accepted_version'] and eng._deposit_op(d) is None


def test_seller_tools_refuse_incomplete_requests_without_type_errors(eng):
    seller=object.__new__(ClaudeSeller);seller.e=eng
    with pytest.raises(Refused,match='Supply'):seller._do('request_vendor_quote',{'deal_id':next(iter(eng.s['deals']))})


def test_external_breakfast_replaces_hotel_capacity_without_double_billing(eng):
    d=deal(eng,100);eng.record_requirements(d['id'],{'breakfast':True,'guests':200})
    configure(eng,d,host=5)
    rid=request(eng,d,'rooms',95,'riverside');confirmed(eng,d,rid,10000)
    rid=request(eng,d,'catering',200,'table',covers=['breakfast']);confirmed(eng,d,rid,4500)
    before=copy.deepcopy(eng.s['breakfast'])
    response=propose(eng,d)
    assert response['status']=='pending_approval'
    terms=d['versions'][-1]['terms']
    assert terms['breakfast']=={}
    assert not any(line['item']=='Breakfast (guests x mornings, guaranteed)' for line in d['versions'][-1]['pricing']['lines'])
    assert eng.s['breakfast']==before


def test_unavailable_provider_invalidates_price_and_requires_alternative(eng):
    d=deal(eng);configure(eng,d);rid=request(eng,d);confirmed(eng,d,rid)
    master=propose(eng,d)
    eng.vendor_action(d['id'],'unavailable',{'request_id':rid,'reason':'Equipment already reserved.'})
    assert eng.s['approvals'][master['approval_id']]['status']=='stale'
    assert not vendors.snapshots(d)
    assert d['third_party']['requests'][0]['quote_versions']
    with pytest.raises(Refused,match='awaiting vendor quote'):propose(eng,d)


def test_multiple_meeting_rooms_are_extracted_for_master_coverage():
    from agentbank.groupsales.extract import extract
    for count in ('3','three'):
        assert extract('Offsite',f'100 king rooms and {count} meeting rooms')['meeting_count']==3


def test_provider_history_is_supplier_cost_upserted_and_survives_reset(eng):
    d=deal(eng);configure(eng,d);rid=request(eng,d,category='rooms',quantity=2,vendor='riverside')
    quote(eng,d,rid,cost=12000,markup=50)
    rows=eng.vendor_history('riverside')['transactions']
    assert len(rows)==1 and rows[0]['cost']==72000 and rows[0]['quantity']==6
    assert rows[0]['status']=='quoted' and rows[0]['payment_status']=='unpaid'
    assert eng.vendor_history('riverside',exclude_deal=d['id'])['transactions']==[]
    quote(eng,d,rid,cost=13000)
    assert len(eng.vendor_history('riverside')['transactions'])==1
    assert eng.vendor_history('riverside')['transactions'][0]['cost']==78000
    eng.reset()
    restored=Engine(eng.var,eng.provider)
    row=restored.vendor_history('riverside')['transactions'][0]
    assert row['revision']==2 and not row['current_run']
    with pytest.raises(Refused,match='Provider not found'):eng.vendor_history('unknown')


def test_hypothetical_quote_requires_normal_review_and_resets_stale_approval(eng):
    d=deal(eng);configure(eng,d);rid=request(eng,d)
    eng.vendor_action(d['id'],'simulate_quote',{'request_id':rid})
    r=d['third_party']['requests'][0]
    assert r['status']=='quoted' and r['approval_hash'] is None
    assert r['quote']['unit_cost']==25000
    with pytest.raises(Refused,match='approval'):eng.vendor_action(d['id'],'confirm',{'request_id':rid,'quote_hash':r['quote']['hash']})
    h=r['quote']['hash'];eng.vendor_action(d['id'],'approve',{'request_id':rid,'quote_hash':h})
    eng.vendor_action(d['id'],'confirm',{'request_id':rid,'quote_hash':h})
    eng.vendor_action(d['id'],'simulate_quote',{'request_id':rid})
    r=d['third_party']['requests'][0]
    assert r['status']=='quoted' and r['approval_hash'] is None and r['quote']['revision']==2


@pytest.mark.parametrize('response',[True,False])
def test_new_inquiry_simulation_waits_for_dates_and_creates_one_request(eng,response):
    options={'category':'av','quote_response':response}
    did=eng.ingest_email('new@demo.example','New third party','New simulation',wake=False,third_party=options)['deal_id']
    assert eng.deal(did)['third_party']['simulation_pending']==options
    eng.record_requirements(did,{'rooms':5})
    assert not eng.deal(did)['third_party']['requests']
    req={'arrival':'2027-03-10','departure':'2027-03-13','rooms':5,'room_type':'king','guests':5}
    eng.record_requirements(did,req);eng.record_requirements(did,req)
    d=eng.deal(did);assert len(d['third_party']['requests'])==1
    r=d['third_party']['requests'][0]
    assert r['status']==('quoted' if response else 'awaiting_quote')
    assert not r['approval_hash']
    duplicate=eng.ingest_email('new@demo.example','New third party','New simulation',wake=False,third_party=options)
    assert duplicate['duplicate'] and len(d['third_party']['requests'])==1


def test_invalid_simulation_does_not_create_inquiry(eng):
    n=len(eng.s['deals'])
    with pytest.raises(Refused):eng.ingest_email('x@example.com','Invalid','Invalid simulation',third_party={'category':'bad'})
    assert len(eng.s['deals'])==n


def test_generated_inquiry_can_include_vendor_quote(server,monkeypatch):
    monkeypatch.setattr(server,'VENDORS',True)
    from agentbank.groupsales import generator
    preset=next(iter(generator.PRESETS))
    code,_,body=_req(server,'/api/simulate/rfp',{'preset':preset,'seed':872,'third_party':{'category':'av','quote_response':True}})
    assert code==200,body
    did=json.loads(body)['deal_id']
    d=server.engine.deal(did)
    server.engine.record_requirements(did,{'arrival':'2027-03-10','departure':'2027-03-13','rooms':5,'room_type':'king'})
    assert d['third_party']['requests'][0]['status']=='quoted'
    code,_,body=_req(server,'/api/vendors/sound/history')
    assert code==200 and json.loads(body)['transactions'][0]['unit_cost']==25000


def test_rules_seller_can_format_bundled_vendor_quote_without_exposing_cost(eng):
    from agentbank.groupsales.seller import RulesSeller
    d=deal(eng);configure(eng,d);rid=request(eng,d);confirmed(eng,d,rid)
    priced=eng.price_terms(d['id'],19000,30,10)
    message=RulesSeller(eng)._offer_message(d,priced,[])
    line=next(line for line in message.splitlines() if 'av package' in line)
    assert '£200' in line and ' x ' not in line
    assert '£100' not in line and 'markup' not in message


def test_disabling_pending_simulation_prevents_later_auto_creation(eng):
    did=eng.ingest_email('cancel@example.com','Cancelled simulation','Wait for dates',wake=False,third_party={'category':'av','quote_response':True})['deal_id']
    eng.vendor_action(did,'configure',{'enabled':False})
    eng.record_requirements(did,{'arrival':'2027-03-10','departure':'2027-03-13','rooms':5})
    assert not eng.deal(did)['third_party']['requests']
    assert 'simulation_pending' not in eng.deal(did)['third_party']


def test_vendor_management_is_hidden_unless_enabled(server):
    """GS_VENDORS is off by default: no Directory, no vendor script, no vendor routes."""
    assert server.VENDORS is False
    code,_,page=_req(server,'/');assert code==200
    assert 'vendorDirectoryNav' not in page and '/assets/vendors.js' not in page
    did=next(iter(server.engine.s['deals']))
    for path in ['/api/vendor_directory',f'/api/deals/{did}/vendors','/assets/vendors.js','/api/vendors/sound/history']:
        assert _req(server,path)[0]==404
    assert _req(server,f'/api/deals/{did}/vendors',{'command':'configure','payload':{'enabled':True}})[0]==404


def test_third_party_options_are_ignored_while_disabled(server):
    from agentbank.groupsales import generator
    preset=next(iter(generator.PRESETS))
    code,_,body=_req(server,'/api/simulate/rfp',{'preset':preset,'seed':873,'third_party':{'category':'av','quote_response':True}})
    assert code==200,body
    assert not (server.engine.deal(json.loads(body)['deal_id']).get('third_party') or {}).get('requests')
    code,_,body=_req(server,'/api/inbox',{'from':'Pat <pat@flag.example>','body':'Ten rooms in March','third_party':{'category':'av'}})
    assert code==200,body
    assert not (server.engine.deal(json.loads(body)['deal_id']).get('third_party') or {}).get('requests')


def test_vendor_management_shows_when_enabled(server,monkeypatch):
    monkeypatch.setattr(server,'VENDORS',True)
    page=_req(server,'/')[2]
    assert 'vendorDirectoryNav' in page and '/assets/vendors.js' in page
    assert _req(server,'/assets/vendors.js')[0]==200

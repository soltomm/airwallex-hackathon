"""FX policy is directional; money execution is verified-receipt and sandbox scoped."""
from datetime import date
from decimal import Decimal
import copy
import json
from types import SimpleNamespace

import pytest

from agentbank.groupsales import fx
from agentbank.groupsales.engine import Engine, Refused
from agentbank.groupsales.fx_provider import SandboxFX, SimulatedFX
from agentbank.groupsales.provider import SimulatedProvider
from agentbank.config import SANDBOX_API
from test_group_sales import server, _req, run_to_confirmed, deal_by


@pytest.fixture
def engine(tmp_path):
    return Engine(tmp_path/'hotel', SimulatedProvider(tmp_path/'payments.json'))


def test_catalog_covers_iso_countries_and_multicurrency():
    c = fx.directory()
    countries = {r['code']: r for r in c['countries']}
    assert len(countries) == 250
    assert countries['US']['currencies'][0]['code'] == 'USD'
    assert countries['GB']['currencies'][0]['code'] == 'GBP'
    assert {x['code'] for x in countries['PA']['currencies']} == {'PAB', 'USD'}
    assert {x['code'] for x in countries['ZW']['currencies']} == {'ZWG', 'USD'}
    assert countries['AQ']['currencies'] == []
    assert countries['XK']['currencies'][0]['code'] == 'EUR'
    # Dollar-using countries share USD's risk tag, irrespective of their own inflation.
    assert next(x for x in countries['ZW']['currencies'] if x['code']=='USD')['tier'] == 'stable'
    assert c['macro_source']['latest_year'] == 2023


def test_directional_policy_has_reason_and_expiry():
    assert fx.risk('USD', date(2026,11,8))['tier'] == 'stable'
    assert fx.risk('NGN', date(2026,11,8))['tier'] == 'unstable'
    assert fx.risk('???')['tier'] == 'review'
    assert fx.risk('NGN', date(2028,1,1))['tier'] == 'review'
    assert fx.risk('NGN')['reason']


def test_payer_profile_is_explicit_not_inferred(engine):
    d=deal_by(engine,'Northstar')
    assert engine.payer_view(d)['origin'] is None
    profile=engine.update_payer(d['id'],'US')
    assert profile['origin']['local_currency']=='USD'
    with pytest.raises(Refused):engine.update_payer(d['id'],'US','NGN')
    with pytest.raises(Refused):engine.update_payer(d['id'],'PA')
    with pytest.raises(Refused):engine.update_payer(d['id'],'AQ')
    assert engine.update_payer(d['id'],'PA','USD')['origin']['local_currency']=='USD'


def test_real_gbp_card_receipt_is_held_even_for_unstable_payer(engine):
    d=deal_by(engine,'Northstar');engine.update_payer(d['id'],'NG')
    _, d=run_to_confirmed(engine)
    receipt=engine.payer_view(d)['latest']
    assert receipt['currency']=='GBP' and receipt['status']=='held'
    assert receipt['reason']=='Already received in hotel currency.'
    assert not engine.fx_provider.state['conversions']
    assert not any('fx' in r['type'] for r in engine.s['ledger'])
    assert 'payer_fx' not in engine.deal_view(d['id'],for_='buyer')


def test_foreign_receipts_are_automatic_and_separate_from_booking(engine):
    d=deal_by(engine,'Northstar');engine.update_payer(d['id'],'NG')
    reference=d['fx_reference'];account=engine.hotel_account
    engine.fx_provider.add_receipt(account,reference,'NGN','1000')
    engine.scan_fx();view=engine.payer_view(d)
    assert view['latest']['status']=='converted'
    assert len(engine.fx_provider.state['conversions'])==1
    assert d['booking'] is None and not engine.s['ledger']
    # Polling, a fresh engine, or a scenario reset cannot resubmit a completed conversion.
    engine.scan_fx()
    assert len(engine.fx_provider.state['conversions'])==1
    restored=Engine(engine.var,engine.provider);restored.scan_fx()
    assert len(restored.fx_provider.state['conversions'])==1
    restored.reset();restored.scan_fx()
    assert restored.payer_view(deal_by(restored,'Northstar'))['latest'] is None
    assert len(restored.fx_provider.state['conversions'])==1


def test_received_currency_drives_decision_not_country(engine):
    d=deal_by(engine,'Northstar');engine.update_payer(d['id'],'NG')
    engine.fx_provider.add_receipt(engine.hotel_account,d['fx_reference'],'USD','1000')
    engine.scan_fx()
    assert engine.payer_view(d)['latest']['status']=='held'
    assert not engine.fx_provider.state['conversions']
    engine.fx_provider.add_receipt(engine.hotel_account,d['fx_reference'],'NGN','1000')
    engine.scan_fx()
    count = len([a for a in engine.s['activity'] if a['kind']=='fx_decision'])
    engine.scan_fx()
    assert len([a for a in engine.s['activity'] if a['kind']=='fx_decision']) == count


def test_mismatched_receipt_reference_does_nothing(engine):
    d=deal_by(engine,'Northstar');engine.update_payer(d['id'],'NG')
    engine.fx_provider.add_receipt(engine.hotel_account,'unrelated-deposit','NGN','1000')
    engine.scan_fx()
    assert engine.payer_view(d)['latest'] is None
    assert not engine.fx_provider.state['conversions']


def receipt():
    return {'id':'bank_1','account':'hotel_1','deal_id':'deal_1','reference':'HH-abc','currency':'NGN','amount':'1000','verified':True}


@pytest.fixture
def journal_provider(tmp_path):
    j=fx.FXJournal(tmp_path);p=SimulatedFX(tmp_path)
    p.state['receipts'].append(receipt())
    return j,p


def test_ambiguous_conversion_is_recovered_without_resubmitting(journal_provider):
    j,p=journal_provider
    original=p.convert
    calls=[]
    def timeout(row):
        calls.append(row['request_id'])
        assert j.store.read()['receipts']['hotel_1:bank_1']['status']=='submitting'
        original(row)
        raise TimeoutError('response lost')
    p.convert=timeout
    assert j.process(receipt(),p)['status']=='unknown'
    assert j.process(receipt(),p)['status']=='converted'
    assert len(calls)==1


def test_unresolved_conversion_never_resubmits(journal_provider):
    j,p=journal_provider;calls=[]
    def timeout(row):calls.append(row);raise TimeoutError('response lost')
    p.convert=timeout
    assert j.process(receipt(),p)['status']=='unknown'
    for _ in range(3):assert j.process(receipt(),p)['status']=='unknown'
    assert len(calls)==1


def test_receipt_replay_validates_identity_and_money(journal_provider):
    j,p=journal_provider
    j.process(receipt(),p)
    assert j.process({**receipt(),'amount':'1000.00'},p)['status']=='converted'
    with pytest.raises(ValueError):j.process({**receipt(),'deal_id':'other'},p)
    with pytest.raises(ValueError):j.process({**receipt(),'amount':'2000'},p)
    with pytest.raises(ValueError):j.process({**receipt(),'verified':False},p)
    for val in ['-1','NaN','Infinity','0']:
        with pytest.raises(ValueError):fx.amount(val)


def test_wallet_or_quote_failure_does_not_submit(journal_provider):
    j,p=journal_provider;p.available=lambda *_:'0'
    assert j.process(receipt(),p)['status']=='review'
    assert not p.state['conversions']


def test_bad_conversion_response_never_claims_success(journal_provider):
    j,p=journal_provider
    p.convert=lambda row:{'id':'wrong','request_id':row['request_id'],'status':'SETTLED','sell_currency':'USD','buy_currency':'GBP','sell_amount':'1000'}
    assert j.process(receipt(),p)['status']=='unknown'


class FakeClient:
    def __init__(self, base=SANDBOX_API):
        self.config=SimpleNamespace(api_base=base);self.calls=[]
    def request(self,method,path,body=None,**kwargs):
        self.calls.append((method,path,body,kwargs))
        if path=='/global_accounts':return {'items':[{'id':'ga_1','status':'ACTIVE'}]}
        if path.endswith('/transactions'):
            return {'items':[
                {'id':'tx_1','status':'SETTLED','type':'DEPOSIT','reference':'expected','currency':'NGN','amount':'1000'},
                {'id':'tx_2','status':'PENDING','type':'DEPOSIT','reference':'expected','currency':'NGN','amount':'1000'},
                {'id':'tx_3','status':'SETTLED','type':'DEBIT','reference':'expected','currency':'NGN','amount':'1000'},
                {'id':'tx_4','status':'SETTLED','type':'DEPOSIT','reference':'wrong','currency':'NGN','amount':'1000'}]}
        if path=='/fx/quotes/create':return {'id':'quote_1',**body}
        return {'id':'conv_1',**(body or {})}


def test_sandbox_adapter_scopes_all_calls_and_matches_provider_receipts():
    c=FakeClient();p=SandboxFX(c)
    assert [r['id'] for r in p.receipts('acct_hotel','expected')]==['tx_1']
    q=p.quote('acct_hotel','NGN','1000')
    p.convert({**receipt(),'request_id':'request_1','quote_id':q['id']})
    assert all(call[3]['on_behalf_of']=='acct_hotel' or call[3]['on_behalf_of']=='hotel_1' for call in c.calls)
    method,path,body,scope=c.calls[-1]
    assert path=='/fx/conversions/create' and body['sell_amount']=='1000' and body['buy_currency']=='GBP'
    for base in ['https://api.airwallex.com/api/v1','http://example.test']:
        bad=FakeClient(base)
        with pytest.raises(ValueError):SandboxFX(bad).receipts('acct_hotel','expected')
        assert bad.calls==[]
    with pytest.raises(ValueError):p.receipts('platform','expected')


def test_fx_endpoints_use_hotel_access_gate(server,monkeypatch):
    monkeypatch.setattr(server,'ACCESS','fx-test-token')
    assert _req(server,'/api/fx/countries')[0]==401
    assert _req(server,'/assets/fx.js')[0]==401
    assert _req(server,'/api/fx/scan',{})[0]==401
    d=deal_by(server.engine,'Northstar')
    assert _req(server,f"/api/deals/{d['id']}/payer",{'country_code':'US'})[0]==401
    headers={'Cookie':'gs_k=fx-test-token','Content-Type':'application/json'}
    code,_,body=_req(server,'/api/fx/countries',headers=headers)
    assert code==200 and len(json.loads(body)['countries'])==250
    assert _req(server,f"/api/deals/{d['id']}/payer",{'country_code':'US'},headers)[0]==200
    assert _req(server,f"/api/deals/{d['id']}/payer",{'country_code':'US','currency':'NGN'},headers)[0]==400


def test_inbox_accepts_structured_origin_but_no_currency_policy_override(server,monkeypatch):
    monkeypatch.setattr(server,'ACCESS','')
    code,_,body=_req(server,'/api/inbox',{'from':'Test <test@example.test>','subject':'New group','body':'Please quote rooms.','payer_country':'US'})
    assert code==200
    d=server.engine.deal(json.loads(body)['deal_id'])
    assert server.engine.payer_view(d)['origin']['local_currency']=='USD'
    count=len(server.engine.s['deals'])
    assert _req(server,'/api/inbox',{'from':'Test <test@example.test>','body':'Anything','payer_country':'US','payer_currency':'NGN'})[0]==400
    assert len(server.engine.s['deals'])==count


def test_unknown_payer_country_does_not_prevent_verifying_actual_currency(engine):
    d=deal_by(engine,'Northstar')
    assert not d.get('payer_origin')
    engine.fx_provider.add_receipt(engine.hotel_account,d['fx_reference'],'USD','50')
    engine.scan_fx()
    view=engine.payer_view(d)
    assert view['origin'] is None
    assert view['latest']['currency']=='USD' and view['latest']['status']=='held'


def test_existing_state_gets_a_reference_without_inventing_origin(engine):
    d=deal_by(engine,'Northstar');del d['fx_reference'];engine._save()
    restored=Engine(engine.var,engine.provider)
    result=restored.payer_view(deal_by(restored,'Northstar'))
    assert result['reference'].startswith('HH-')
    assert result['origin'] is None


def test_simulated_receipts_are_refused_on_real_provider(engine):
    engine.fx_provider=SimpleNamespace(simulated=False)
    with pytest.raises(Refused,match='only available'):
        engine.simulate_fx_receipt('GS-1001')

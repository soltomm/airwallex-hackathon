"""Harbour House is the hotel (seller, property, payment descriptor); Groupy is the software provider."""
from unittest.mock import Mock
from agentbank.groupsales.engine import Engine
from agentbank.groupsales.provider import AirwallexProvider,SimulatedProvider
from agentbank.groupsales.seller import RulesSeller,system_prompt


def test_hotel_and_seller_messages_use_harbour_house(tmp_path):
    e=Engine(tmp_path/'hotel',SimulatedProvider(tmp_path/'provider.json'))
    e.ingest_email('demo@example.com','Group with dinner','Please quote 5 king rooms for 5 guests arriving 10 March and departing 13 March 2027, with a group dinner.',wake=False)
    RulesSeller(e).run()
    assert e.hotel['name']=='Harbour House'
    messages=[m['text'] for d in e.s['deals'].values() for m in d['thread'] if m['from']=='seller']
    assert any('Group Sales, Harbour House' in m for m in messages)
    assert all('Groupy' not in m for m in messages)
    prompt=system_prompt(e)
    assert 'Group Sales, Harbour House' in prompt and 'Groupy' not in prompt


def test_payment_descriptor_names_the_hotel_without_changing_request_routing():
    client=Mock();client.post.return_value={'id':'intent_demo','amount':'49.20','currency':'GBP'}
    p=AirwallexProvider(client)
    p.create_intent('stable-request',4920,'GBP','stable-order',{'deal_id':'stable-deal'},return_url='https://example.test/return',connected_account_id='acct_existing')
    _,body=client.post.call_args.args
    assert body=={'request_id':'stable-request','amount':'49.20','currency':'GBP','merchant_order_id':'stable-order','metadata':{'deal_id':'stable-deal'},'descriptor':'Harbour House deposit','return_url':'https://example.test/return','connected_account_id':'acct_existing'}

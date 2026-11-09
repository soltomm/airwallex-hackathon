"""Sandbox-only treasury adapter. Nothing here changes card booking obligations.

Global-account receipt field mappings are deliberately strict: incomplete or unfamiliar
provider records are not money-movement authority. Verify this adapter on Render staging.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from urllib.parse import quote
import uuid

from ..config import SANDBOX_API
from ..storage import doc
from .fx import amount


class SandboxFX:
    simulated = False

    def __init__(self, client):
        self.client = client

    def request(self, method, path, account, body=None, params=None):
        if self.client.config.api_base.rstrip('/') != SANDBOX_API:
            raise ValueError('FX execution is restricted to the Airwallex sandbox.')
        if not account or account == 'platform':
            raise ValueError('A hotel connected account is required for foreign-currency treasury operations.')
        return self.client.request(method, path, body, params=params, on_behalf_of=account)

    def pages(self, path, account, params=None):
        for page in range(100):
            result = self.request('GET', path, account, params={**(params or {}), 'page_num': page, 'page_size': 100})
            items = result.get('items') if isinstance(result, dict) else None
            if not isinstance(items, list):
                raise ValueError('Unrecognized provider list response; review required.')
            yield from items
            if result.get('has_more') is False or (result.get('has_more') is not True and len(items) < 100):
                return
        raise ValueError('Provider pagination limit reached; scan incomplete.')

    def receipts(self, account, reference):
        found = []
        references = set(reference) if isinstance(reference, list) else {reference}
        for ga in self.pages('/global_accounts', account):
            if ga.get('status') != 'ACTIVE' or not ga.get('id'):
                continue
            path = '/global_accounts/' + quote(ga['id'], safe='') + '/transactions'
            for tx in self.pages(path, account):
                matched = tx.get('reference') or tx.get('payment_reference')
                if matched not in references:
                    continue
                if tx.get('status') not in ('SETTLED', 'COMPLETED', 'POSTED') or (tx.get('type') or tx.get('transaction_type')) not in ('DEPOSIT', 'CREDIT'):
                    continue
                if not tx.get('id') or not tx.get('currency'):
                    continue
                amount(tx.get('amount'))  # reject debits, zero, NaN and unsupported money shapes
                found.append({'id': tx['id'], 'account': account, 'currency': tx['currency'],
                              'amount': str(tx['amount']), 'reference': matched, 'verified': True})
        return found

    def available(self, account, currency):
        rows = self.request('GET', '/balances/current', account)
        if not isinstance(rows, list):
            raise ValueError('Unrecognized provider balance response.')
        return next((str(r['available_amount']) for r in rows if r.get('currency') == currency), '0')

    def quote(self, account, currency, value):
        body = {'sell_currency': currency, 'buy_currency': 'GBP', 'sell_amount': value, 'validity': 'MIN_1'}
        result = self.request('POST', '/fx/quotes/create', account, body)
        if result.get('sell_currency') != currency or result.get('buy_currency') != 'GBP' or amount(result.get('sell_amount')) != amount(value):
            raise ValueError('Firm quote is not bound to the verified receipt amount and currencies.')
        return result

    def convert(self, row):
        return self.request('POST', '/fx/conversions/create', row['account'], {
            'request_id': row['request_id'], 'quote_id': row['quote_id'], 'sell_currency': row['currency'],
            'buy_currency': 'GBP', 'sell_amount': row['amount']})

    def find_conversion(self, row):
        if row.get('conversion_id'):
            return self.request('GET', '/fx/conversions/' + quote(row['conversion_id'], safe=''), row['account'])
        return next((x for x in self.pages('/fx/conversions', row['account'], {'request_id': row['request_id']})
                     if x.get('request_id') == row['request_id']), None)


class SimulatedFX:
    simulated = True

    def __init__(self, var):
        self.store = doc(Path(var) / 'groupsales_fx_sim.json')
        self.state = self.store.read() or {'receipts': [], 'conversions': {}}

    def add_receipt(self, account, reference, currency, value):
        amount(value)
        row = {'id': 'fxsim_' + uuid.uuid4().hex, 'account': account, 'reference': reference,
               'currency': currency, 'amount': str(value), 'verified': True}
        self.state['receipts'].append(row)
        self.store.write(self.state)
        return row

    def receipts(self, account, reference):
        references = set(reference) if isinstance(reference, list) else {reference}
        return [dict(r) for r in self.state['receipts'] if r['account'] == account and r['reference'] in references]

    def available(self, account, currency):
        received = sum((Decimal(r['amount']) for r in self.state['receipts'] if r['account'] == account and r['currency'] == currency), Decimal(0))
        sold = sum((Decimal(r['sell_amount']) for r in self.state['conversions'].values() if r['account'] == account and r['sell_currency'] == currency), Decimal(0))
        return str(received - sold)

    def quote(self, account, currency, value):
        return {'id': 'fxquote_sim_' + uuid.uuid4().hex, 'sell_currency': currency, 'buy_currency': 'GBP', 'sell_amount': value}

    def convert(self, row):
        # Illustrative fixed rate only. Never presented as a market quote.
        record = {'id': 'fxconversion_sim_' + uuid.uuid4().hex, 'request_id': row['request_id'], 'account': row['account'],
                  'sell_currency': row['currency'], 'buy_currency': 'GBP', 'sell_amount': row['amount'],
                  'buy_amount': str((Decimal(row['amount']) * Decimal('0.001')).quantize(Decimal('0.01'))), 'status': 'SETTLED'}
        result = self.state['conversions'].setdefault(row['request_id'], record)
        self.store.write(self.state)
        return result

    def find_conversion(self, row):
        return self.state['conversions'].get(row['request_id'])

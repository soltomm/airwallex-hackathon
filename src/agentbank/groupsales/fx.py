"""Directional FX policy and an at-most-once, verified-receipt treasury journal.

Country is awareness metadata, never proof of the denomination of received funds.
Only provider-verified receipts reach process(); booking/payout ledgers are separate.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import copy
import json
from pathlib import Path
import threading
import uuid

from ..storage import doc

DATA = Path(__file__).resolve().parents[3] / 'data' / 'groupsales'


@lru_cache(maxsize=1)
def catalog():
    return json.loads((DATA / 'fx_countries.json').read_text())


@lru_cache(maxsize=1)
def policy():
    return json.loads((DATA / 'fx_policy.json').read_text())


def risk(currency: str, today: date | None = None) -> dict:
    p = policy()
    known = {c['code'] for row in catalog()['countries'] for c in row['currencies']}
    today = today or datetime.now(timezone.utc).date()
    if currency not in known:
        tier, reason = 'review', 'Unknown or unsupported currency classification.'
    elif today > date.fromisoformat(p['reviewed_on']) + timedelta(days=p['valid_days']):
        tier, reason = 'review', 'The directional policy needs a fresh review before automatic FX.'
    elif currency == p['base_currency']:
        tier, reason = 'stable', 'Hotel base currency; no foreign-currency exposure.'
    elif currency in p['unstable']:
        tier, reason = 'unstable', p['unstable'][currency]
    else:
        tier, reason = 'stable', p['stable_reason']
    return {'currency': currency, 'tier': tier, 'reason': reason, 'policy_version': p['version'], 'reviewed_on': p['reviewed_on']}


def directory() -> dict:
    out = copy.deepcopy(catalog())
    for row in out['countries']:
        for currency in row['currencies']:
            currency.update(risk(currency['code']))
    return {**out, 'policy': policy()}


def payer_profile(country_code: str, currency: str | None = None) -> dict:
    country = next((c for c in catalog()['countries'] if c['code'] == country_code), None)
    if not country:
        raise ValueError('Choose a country from the country directory.')
    currencies = [c['code'] for c in country['currencies']]
    if currency is None and len(currencies) == 1:
        currency = currencies[0]
    if currency not in currencies:
        raise ValueError('Choose one of the country’s listed currencies. Countries with no recorded tender need review.')
    return {'country_code': country_code, 'country': country['name'], 'local_currency': currency,
            'classification': risk(currency), 'source': 'Hotel-declared payer origin; not payment verification'}


def amount(value) -> Decimal:
    try:
        value = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError('Receipt amount must be a positive decimal.')
    if not value.is_finite() or value <= 0:
        raise ValueError('Receipt amount must be a positive decimal.')
    return value


class FXJournal:
    def __init__(self, var):
        self.store = doc(Path(var) / 'groupsales_fx_journal.json')
        self.lock = threading.RLock()
        self.state = self.store.read() or {'receipts': {}, 'last_scan': None, 'scan_error': None}

    def save(self):
        self.store.write(self.state)

    def view(self, deal_id):
        with self.lock:
            rows = [copy.deepcopy(x) for x in self.state['receipts'].values() if x['deal_id'] == deal_id]
            return {'receipts': rows, 'latest': rows[-1] if rows else None, 'last_scan': self.state['last_scan'],
                    'scan_error': self.state['scan_error'], 'base_currency': policy()['base_currency']}

    def process(self, receipt: dict, provider) -> dict:
        """Receipt is read from the scoped provider, never from an incoming API body."""
        required = ['id', 'account', 'deal_id', 'currency', 'amount', 'reference']
        if any(not receipt.get(k) for k in required) or receipt.get('verified') is not True:
            raise ValueError('A verified, account-scoped receipt with a matching reference is required.')
        value = amount(receipt['amount'])
        key = receipt['account'] + ':' + receipt['id']
        with self.lock:
            old = self.state['receipts'].get(key)
            if old:
                if any(str(old[k]) != str(receipt[k]) for k in required if k != 'amount') or Decimal(old['amount']) != value:
                    raise ValueError('Receipt identity was reused with different facts; review required.')
                if old['status'] in ('submitting', 'unknown', 'pending'):
                    self._recover(old, provider)
                return copy.deepcopy(old)
            classification = risk(receipt['currency'])
            action = 'hold' if receipt['currency'] == policy()['base_currency'] or classification['tier'] == 'stable' else 'convert' if classification['tier'] == 'unstable' else 'review'
            row = {k: receipt[k] for k in required}
            row.update(amount=str(value), classification=classification, action=action, status='held' if action == 'hold' else 'review' if action == 'review' else 'prepared',
                       reason='Already received in hotel currency.' if receipt['currency'] == policy()['base_currency'] else classification['reason'],
                       request_id=str(uuid.uuid5(uuid.NAMESPACE_URL, 'harbour-fx:' + key)),
                       recorded_at=datetime.now(timezone.utc).isoformat(), simulated=provider.simulated)
            self.state['receipts'][key] = row
            self.save()  # durable identity before any provider quote/conversion
            if action != 'convert':
                return copy.deepcopy(row)
            try:
                # Require an available wallet balance and a firm, amount-bound quote.
                available = Decimal(str(provider.available(row['account'], row['currency'])))
                if not available.is_finite() or available < value:
                    raise ValueError('Insufficient verified spendable wallet balance; no conversion submitted.')
                quote = provider.quote(row['account'], row['currency'], str(value))
                if not quote.get('id'):
                    raise ValueError('The provider did not return a firm quote ID.')
                row['quote_id'] = quote['id']
                row['status'] = 'submitting'
                self.save()
                result = provider.convert(row)
                self._apply(row, result)
            except Exception as exc:
                # Once submitted, every failure is treated as ambiguous. Never blindly retry.
                row['status'] = 'unknown' if row['status'] == 'submitting' else 'review'
                row['reason'] = str(exc)[:400]
            self.save()
            return copy.deepcopy(row)

    def _apply(self, row, result):
        if not isinstance(result, dict) or not result.get('id') or result.get('request_id') != row['request_id']:
            raise ValueError('Conversion response does not match its request ID.')
        if result.get('sell_currency') != row['currency'] or result.get('buy_currency') != policy()['base_currency'] or Decimal(str(result.get('sell_amount'))) != Decimal(row['amount']):
            raise ValueError('Conversion currencies or amount do not match the verified receipt.')
        if result.get('status') in ('SETTLED', 'COMPLETED'):
            amount(result.get('buy_amount'))
        row['conversion_id'] = result.get('id')
        status = result.get('status')
        row['status'] = 'converted' if status in ('SETTLED', 'COMPLETED') else 'review' if status in ('FAILED', 'CANCELLED') else 'pending'
        row['reason'] = 'Provider confirmed conversion to GBP.' if row['status'] == 'converted' else 'Waiting for a confirmed provider outcome; no second conversion will be submitted.'
        if result.get('buy_amount') is not None:
            row['received_gbp'] = str(result['buy_amount'])

    def _recover(self, row, provider):
        try:
            found = provider.find_conversion(row)
            if found:
                self._apply(row, found)
        except Exception as exc:
            row['reason'] = 'Recovery lookup failed; no resubmission: ' + str(exc)[:300]
        self.save()

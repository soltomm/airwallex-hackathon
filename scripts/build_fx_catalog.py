#!/usr/bin/env python3
"""Regenerate the country/current-tender directory from pinned ISO/Unicode datasets."""
from datetime import date
import csv
import io
import json
from pathlib import Path
from urllib.request import urlopen

AS_OF = date(2026, 11, 8)
SOURCES = {
    'countries': 'https://raw.githubusercontent.com/pycountry/pycountry/4c6a69927a25326a69bf753671fb571bb437cd7d/src/pycountry/databases/iso3166-1.json',
    'tender': 'https://raw.githubusercontent.com/unicode-org/cldr-json/7d2b9cb11cec08f4d333ad73209deb3df0711c3b/cldr-json/cldr-core/supplemental/currencyData.json',
    'names': 'https://raw.githubusercontent.com/unicode-org/cldr-json/7d2b9cb11cec08f4d333ad73209deb3df0711c3b/cldr-json/cldr-numbers-full/main/en/currencies.json',
}
MACRO_URL = 'https://raw.githubusercontent.com/datasets/inflation/b8ece3f0b169a7fbcee162d5427be5ae4d0ee57e/data/inflation-consumer.csv'


def build():
    raw = {name: json.load(urlopen(url, timeout=30)) for name, url in SOURCES.items()}
    countries = raw['countries']['3166-1'] + [{'alpha_2': 'XK', 'name': 'Kosovo'}]
    tender = raw['tender']['supplemental']['currencyData']['region']
    names = raw['names']['main']['en']['numbers']['currencies']
    macro = {}
    with urlopen(MACRO_URL, timeout=30) as response:
        for row in csv.DictReader(io.StringIO(response.read().decode())):
            code = row['Country Code']
            if row['Inflation'] and (code not in macro or int(row['Year']) > macro[code]['year']):
                macro[code] = {'year': int(row['Year']), 'consumer_inflation_pct': round(float(row['Inflation']), 2)}
    rows = []
    for country in sorted(countries, key=lambda c: c.get('common_name', c['name'])):
        currencies = []
        for entry in tender.get(country['alpha_2'], []):
            for code, validity in entry.items():
                if validity.get('_tender') == 'false' or validity.get('_from', '0001-01-01') > AS_OF.isoformat() or validity.get('_to', '9999-12-31') < AS_OF.isoformat():
                    continue
                if code not in [c['code'] for c in currencies]:
                    currencies.append({'code': code, 'name': names.get(code, {}).get('displayName', code)})
        entry = {'code': country['alpha_2'], 'name': country.get('common_name', country['name']), 'currencies': currencies}
        if country.get('alpha_3') in macro:
            entry['historical_macro'] = macro[country['alpha_3']]
        rows.append(entry)
    return {'as_of': AS_OF.isoformat(), 'coverage': '249 ISO 3166-1 countries and territories, plus Kosovo (XK); no tender is recorded explicitly.',
            'sources': SOURCES, 'countries': rows,
            'macro_source': {'url': MACRO_URL, 'upstream': 'World Bank FP.CPI.TOTL.ZG',
                             'latest_year': max(m['year'] for m in macro.values()),
                             'note': 'Historical context only, not a live inflation feed or an automatic currency rating.'}}


if __name__ == '__main__':
    dest = Path(__file__).resolve().parents[1] / 'data/groupsales/fx_countries.json'
    dest.write_text(json.dumps(build(), indent=2, ensure_ascii=False) + '\n')
    print(dest)

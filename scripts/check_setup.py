"""Verify credentials, list Global Accounts and show non-zero balances (guide steps 3-5)."""
from _bootstrap import connect

from agentbank.config import ConfigError

try:
    client, api = connect()
    client.token()
except ConfigError as e:
    raise SystemExit(f"config: {e}")
print("login: ok")

accounts = api.global_accounts()
print(f"global accounts: {len(accounts)}")
for a in accounts:
    feats = sorted({f"{f['currency']}/{f['transfer_method']}" for f in a.get("required_features", [])})
    print(f"  {a['id']}  {a['status']}  {a['country_code']}  {', '.join(feats)}  {a.get('nick_name', '')}")

print("balances (non-zero):")
for cur, row in sorted(api.balances().items()):
    if row.get("total_amount"):
        print(f"  {cur}: available {row['available_amount']:,}  pending {row['pending_amount']:,}")

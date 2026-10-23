#!/usr/bin/env bash
# The traveller's side of Metal: a wallet their Claude can pay hotel deposits from, within limits
# Metal enforces on chain. Creates the traveller's key and smart account (testnet faucet pays gas),
# funds it, writes a mandate capped at the budget whose only payees are the hotels in
# data/hotels.json, and binds it.
#   scripts/metal_traveller_setup.sh [budget-usdz]          (default 0.50: EUR 500 at the demo rate)
#   scripts/metal_traveller_setup.sh --renew [budget-usdz]  fresh budget: replaces the bound mandate
# Then give Claude the wallet:  claude mcp add metal-wallet -- <repo>/scripts/metal_wallet_mcp.sh
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
M=./metal/node_modules/.bin/metal-agent
RENEW=no; [ "${1:-}" = "--renew" ] && { RENEW=yes; shift; }
BUDGET=${1:-0.50}
KEY=var/metal/traveller-key.json
MANDATE=var/metal/traveller-mandate.json

[ -f "$KEY" ] || $M key create --out "$KEY"
ACCOUNT=$($M account create --key "$KEY" --network testnet | tee /dev/stderr | awk '/^account / {print $2}')
echo "traveller account $ACCOUNT"
[ "$RENEW" = yes ] || $M fund "$ACCOUNT" --network testnet   # the account still holds ~99 *USDZ

PAYEES=()
while IFS=$'\t' read -r addr name; do PAYEES+=(--payee "$addr,$name"); done < <(
  .venv/bin/python -c "import json; [print(h['metal']['payee'], h['name'], sep='\t') for h in json.load(open('data/hotels.json')) if h.get('metal')]")

create() {
  $M mandate credential create --principal "$ACCOUNT" --cap "$BUDGET" --budget "$BUDGET" "${PAYEES[@]}" \
    --expires-in 604800 --description "Paris trip: hotel deposits within the traveller's budget" \
    --key "$KEY" --network testnet --out "$1"
}
if [ "$RENEW" = yes ] && [ -f "$MANDATE" ]; then
  # Budgets never reset: bind a fresh mandate and revoke the old one in one transaction,
  # then move it into place (credential and its key) so the wallet MCP picks it up.
  NEW=var/metal/traveller-mandate.next.json
  rm -f "$NEW" "${NEW%.json}.key.json"
  create "$NEW"
  $M mandate renew --credential "$NEW" --replaces "$MANDATE" --key "$KEY" --network testnet
  mv "$NEW" "$MANDATE"; mv "${NEW%.json}.key.json" "${MANDATE%.json}.key.json"
  rm -f "${MANDATE%.json}.pulls"
else
  create "$MANDATE"
  $M mandate bind --credential "$MANDATE" --key "$KEY" --network testnet
fi
$M mandate status --credential "$MANDATE" --network testnet

#!/usr/bin/env bash
# Kit 11 smoke test: an agent pays from the owner's Metal account under on-chain limits.
# Needs METAL_RPC_URL in .env, a funded account, and a bound mandate (see README "Metal testnet").
#   scripts/metal_limits_test.sh <mandate credential> <allowed payee> <other address>
set -uo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
M=./metal/node_modules/.bin/metal-agent
CRED=$1 PAYEE=$2 OTHER=$3 GAS_KEY=${GAS_KEY:-var/metal/agent-key.json}  # key that submits and pays gas

run() { echo "--- $1"; shift; "$@" 2>&1 | grep -E "^(status|outcome|amount|transaction|error|payment|mandate_|refus)" || true; }

run "within cap, allowed payee (expect paid)"       $M mandate pull --credential "$CRED" --to "$PAYEE" --amount 0.05 --network testnet --key "$GAS_KEY"
run "over per-payment cap (expect refusal)"         $M mandate pull --credential "$CRED" --to "$PAYEE" --amount 0.06 --network testnet --key "$GAS_KEY" --record-refusal
run "payee not on the list (expect refusal)"        $M mandate pull --credential "$CRED" --to "$OTHER" --amount 0.01 --network testnet --key "$GAS_KEY" --record-refusal
echo "--- mandate status"; $M mandate status --credential "$CRED" --network testnet 2>&1 | head -12

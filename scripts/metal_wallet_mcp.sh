#!/usr/bin/env bash
# MCP server for the traveller's Claude: Metal's pay/fund/status tools, bound to the traveller's
# mandate, so the model can pay only the listed hotels and never more than the budget.
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
exec ./metal/node_modules/.bin/metal-agent mcp --mandate var/metal/traveller-mandate.json

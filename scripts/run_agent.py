"""Run the example agent against your sandbox.

    pip install -e ".[agent]"
    python scripts/seed_beneficiaries.py        # copy a beneficiary id
    python scripts/run_agent.py <beneficiary_id>

Policy below: keep a 9,990,000 USD reserve floor (so cash is genuinely scarce on the
10M sandbox balance) and ask a person for anything over 2,500 USD.
"""
import sys

from _bootstrap import ROOT, connect

from agentbank.agent.loop import run_agent
from agentbank.agent.tools import AgentContext
from agentbank.approvals import ApprovalStore, ConsoleApprover
from agentbank.audit import AuditLog
from agentbank.ledger import OperationLedger
from agentbank.money import Money
from agentbank.policy import PaymentPolicy

if len(sys.argv) < 2:
    raise SystemExit(__doc__)
beneficiary_id = sys.argv[1]

client, api = connect()
ctx = AgentContext(
    api=api,
    policy=PaymentPolicy(
        reserve_floor={"USD": Money.of(9_990_000, "USD")},
        autonomous_limit={"USD": Money.of(2_500, "USD")},
    ),
    approvals=ApprovalStore(),
    approver=ConsoleApprover(),
    ledger=OperationLedger(ROOT / "var" / "ledger.sqlite3"),
    audit=AuditLog(ROOT / "var" / "audit.jsonl"),
)

goal = f"""Three supplier invoices are due this week, all payable to beneficiary {beneficiary_id}
(Acme Freight Inc, US, LOCAL):
- INV-1001: 1,800.00 USD, shipping. If unpaid, our shipments stop on Friday.
- INV-1002: 4,200.00 USD, warehouse rent, due Monday.
- INV-1003: 30,000.00 USD, equipment, the supplier accepts payment next month.
Decide what to pay now and what to defer, then act."""

run_agent(goal, ctx)
print(f"\naudit log: {ctx.audit.path}")

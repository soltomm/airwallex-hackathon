"""The hotel's side after booking: what front desk and revenue staff do.

    python scripts/hotel_ops.py list
    python scripts/hotel_ops.py <booking_ref> status|cancel|check-out|no-show

Works on var/bookings.json and the sandbox, alongside a running hotel_mcp.py.
"""
import json
import sys

from _bootstrap import ROOT, connect

from agentbank.audit import AuditLog
from agentbank.bookings import BookingError, BookingStore, HotelPayments
from agentbank.ledger import OperationLedger

store = BookingStore(ROOT / "var/bookings.json")


def main(argv: list[str]) -> int:
    if argv[:1] == ["list"] or not argv:
        for b in store.all().values():
            print(f"{b['booking_ref']}  {b['hotel']:<30} {b['check_in']}  {b['total']:>8} {b['currency']}"
                  f"  {b['method']:<5}  {b['status']}")
        return 0
    if len(argv) != 2:
        print(__doc__)
        return 2
    ref, action = argv
    _, api = connect()
    payments = HotelPayments(store, OperationLedger(ROOT / "var/ledger.sqlite3"), AuditLog(ROOT / "var/audit.jsonl"), api)
    ops = {"status": payments.refresh, "cancel": payments.cancel, "check-out": payments.check_out,
           "no-show": payments.no_show}
    if action not in ops:
        print(__doc__)
        return 2
    try:
        b = ops[action](ref)
    except BookingError as e:
        print(f"error: {e}")
        return 1
    print(json.dumps({k: b.get(k) for k in ("booking_ref", "status", "total", "deposit", "currency",
                                            "payment_intent_id", "metal")}, indent=2))
    if action != "status" and api:
        pi = b.get("payment_intent_id") and api.get_payment_intent(b["payment_intent_id"])
        if pi:
            print(f"Airwallex: {pi['status']}, captured {pi.get('captured_amount')} {pi['currency']}")
    print("history:", *(f"  {h['at']}  {h['event']}" for h in b.get("history", [])), sep="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

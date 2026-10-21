"""After paying on the local checkout (scripts/checkout_server.py), exercise kits 4 and 12:

- DISPUTE-* intents: stage a dispute at RFI on each, accept the small one, challenge the
  large one with an uploaded evidence PDF, then simulate the issuer rejecting it.
- HOLD-* intents (pre-auth): increment the first hold, capture it, cancel the second.

    python scripts/payments_followup.py
"""
import datetime
import json
import urllib.request
import uuid

from _bootstrap import ROOT, connect

from agentbank.airwallex import AirwallexError

client, api = connect()
intents = json.loads((ROOT / "var" / "checkout_intents.json").read_text())


def show(label, fn):
    try:
        out = fn()
        keep = ("id", "status", "stage", "amount", "captured_amount", "reason_code")
        print(f"PASS {label}: {json.dumps({k: out.get(k) for k in keep if k in out} if isinstance(out, dict) else out)[:300]}")
        return out
    except AirwallexError as e:
        print(f"FAIL {label}: [{e.status}] {e.code}: {e.message} {json.dumps(e.body)[:300]}")


def upload_evidence(text: bytes) -> str:
    """Multipart upload of a one-page PDF to the files host (JPG or PDF only; PNG is rejected)."""
    stream = b"BT /F1 12 Tf 72 720 Td (" + text + b") Tj ET"
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    pdf, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n%s\nendobj\n" % (i, o)
    xref = len(pdf)
    pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)

    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"evidence.pdf\"\r\n"
            f"Content-Type: application/pdf\r\n\r\n").encode() + pdf + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(client.config.files_base + "/files/upload", data=body, method="POST", headers={
        "Authorization": "Bearer " + client.token(), "Content-Type": f"multipart/form-data; boundary={boundary}",
        "User-Agent": "agentbank/0.1"})  # the files host refuses urllib's default user agent
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())["file_id"]


due = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S+0000")
reason_codes = {"DISPUTE-FRAUD": "10.4", "DISPUTE-NOT-RECEIVED": "13.1", "DISPUTE-CREDIT": "13.6"}
disputes, holds = {}, []

for row in intents:
    pi = client.get(f"/pa/payment_intents/{row['id']}")
    status = pi.get("status")
    print(f"     {row['ref']:<22} {row['id']}  {status}")
    if row["ref"] in reason_codes and status == "SUCCEEDED" and row["ref"] not in disputes:
        d = show(f"stage dispute {row['ref']} (RFI, {reason_codes[row['ref']]})",
                 lambda: client.post("/simulation/pa/payment_disputes/create", {
                     "payment_intent_id": row["id"], "reason_code": reason_codes[row["ref"]],
                     "stage": "RFI", "due_at": due}))
        if d:
            disputes[row["ref"]] = d["id"]
    if row.get("preauth") and status == "REQUIRES_CAPTURE":
        holds.append(row)

if "DISPUTE-NOT-RECEIVED" in disputes:
    show("accept small dispute",
         lambda: client.post(f"/pa/payment_disputes/{disputes['DISPUTE-NOT-RECEIVED']}/accept", {"request_id": str(uuid.uuid4())}))
if "DISPUTE-FRAUD" in disputes:
    fraud = disputes["DISPUTE-FRAUD"]
    file_id = upload_evidence(b"Synthetic evidence: signed proof of delivery, order DISPUTE-FRAUD")
    print(f"     uploaded evidence {file_id[:24]}...")
    # A challenge needs at least one evidence file; delivered_at must end in Z, not +0000.
    show("challenge large dispute with evidence", lambda: client.post(f"/pa/payment_disputes/{fraud}/challenge", {
        "request_id": str(uuid.uuid4()),
        "delivery_info": {"address": "1 Market St, San Francisco, CA 94105", "delivered_at": "2026-10-15T15:00:00Z"},
        "supporting_documents": {"documents": [{"type": "PRIMARY", "file_ids": [file_id],
                                                "description": "Signed proof of delivery"}]}}))
    # The issuer rejects the evidence: RFI -> CHARGEBACK, back to REQUIRES_RESPONSE. Needs due_at.
    show("simulate issuer rejection (escalate)",
         lambda: client.post(f"/simulation/pa/payment_disputes/{fraud}/escalate", {"due_at": due}))

if holds:
    first = holds[0]
    show("increment hold +$30", lambda: client.post(f"/pa/payment_intents/{first['id']}/increment_authorization",
                                                    {"request_id": str(uuid.uuid4()), "amount": first["amount"] + 30}))
    show("capture $72 of the hold", lambda: client.post(f"/pa/payment_intents/{first['id']}/capture",
                                                        {"request_id": str(uuid.uuid4()), "amount": 72}))
if len(holds) > 1:
    show("cancel second hold", lambda: client.post(f"/pa/payment_intents/{holds[1]['id']}/cancel",
                                                   {"request_id": str(uuid.uuid4()), "cancellation_reason": "Job failed"}))
if not disputes and not holds:
    print("Nothing paid yet: pay on http://localhost:8008 first.")

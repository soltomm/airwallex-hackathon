"""The hotel back office: shared state and every operation an agent, buyer or manager can perform.

Agents never change state directly. They call these methods, which enforce pricing, authority,
inventory and payment rules in code and refuse anything outside them (`Refused`). State is one
JSON document saved after every change, so a refresh or restart picks up where the demo was.

Invariants enforced here:
- inventory: available = capacity - held - booked, never negative; a hold converts to a booking in
  one operation, and an accepted reduction releases rooms exactly once.
- versions: offers and amendments are immutable versions with a hash of their terms; approvals and
  acceptance name a version and its hash, so a changed offer needs fresh approval.
- money: every provider call is preceded by a persisted operation with its own request_id; a call
  whose outcome is unknown blocks any new call for the same purpose until it is resolved; ledger
  entries carry a unique key, so a repeated notification can't credit twice.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from . import bookable, calc, rules
from .calc import gbp
from .extract import extract, missing_info, stay_nights
from .policy import buyer_check, hotel_check
from .provider import AirwallexError, AmbiguousResult
from . import fx
from .fx_provider import SandboxFX, SimulatedFX
from ..storage import doc

ROOT = Path(__file__).resolve().parents[3]
SCENARIO = ROOT / "data" / "groupsales" / "scenario.json"
REFUND_RANK = {"RECEIVED": 1, "ACCEPTED": 2, "SETTLED": 3, "FAILED": 9}


FREE_MAIL = {"gmail", "googlemail", "outlook", "hotmail", "live", "yahoo", "icloud", "me", "aol", "proton", "protonmail"}


def _company_from(sender: str, domain: str) -> str:
    """A readable name for a sender the CRM doesn't know: the domain's name, or the person for free mail."""
    label = domain.split(".")[0] if domain else ""
    person = sender.split("<")[0].strip().strip('"') if "<" in sender else ""
    if label.lower() in FREE_MAIL or not label:
        return person or sender.strip() or "Unknown sender"
    return label[:1].upper() + label[1:]


class Refused(Exception):
    """The operation is outside the caller's authority or the hotel's hard limits."""


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def cap_first(t: str) -> str:
    return t[:1].upper() + t[1:]


def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:12]


class Engine:
    def __init__(self, var_dir: str | Path, provider, scenario_path: str | Path = SCENARIO,
                 public_base: str = "http://localhost:8010"):
        self.var = Path(var_dir)
        self.var.mkdir(parents=True, exist_ok=True)
        self.provider = provider
        self.scenario_path = Path(scenario_path)
        self.public_base = public_base.rstrip("/")
        self.lock = threading.RLock()
        # One JSON document: a file under var/, or a Postgres row when DATABASE_URL is set (storage.py),
        # so a hosted instance keeps its state across deploys and idle spin-downs.
        self.store = doc(self.var / "groupsales_state.json")
        self.runs = doc(self.var / "groupsales_runs.json")
        self.followups = doc(self.var / "groupsales_followups.json")
        # Hotel staff's changes to the seller's rules (rules.EDITABLE), kept apart from the scenario
        # state so they survive a reset.
        self.rule_store = doc(self.var / "groupsales_rules.json")
        # The hotel's Airwallex connected account, once onboarded from the dashboard. Kept apart from the
        # scenario state so a reset never forgets it (Airwallex doesn't forget it either).
        self.account_store = doc(self.var / "groupsales_hotel_account.json")
        self._onboarding = None   # the background onboarding thread, while it runs
        self.fx_journal = fx.FXJournal(self.var)
        self.fx_provider = SimulatedFX(self.var) if provider.name == 'simulated' else SandboxFX(provider.c)
        self.fx_scan_lock = threading.Lock()
        self.scenario = self._load_scenario()
        self.listeners: list = []          # called after state changes that should wake an agent
        saved = self.store.read()
        if saved:
            self.s = saved
            migrated = False
            for d in self.s['deals'].values():
                if not d.get('fx_reference'):
                    d['fx_reference'] = 'HH-' + uuid.uuid5(uuid.NAMESPACE_URL, self.s['run_id'] + ':' + d['id']).hex[:24]
                    migrated = True
            if migrated:
                self._save()
        else:
            self.s = {}
            self.reset(reason="first start", archive=False)

    # ================================================================ basics
    @property
    def hotel(self):
        return self.scenario["hotel"]

    @property
    def policy(self):
        return self.scenario["policy"]

    @property
    def rms(self):
        return self.scenario["rms"]

    def now(self) -> str:
        return self.s["clock"]

    def _save(self):
        self.store.write(json.loads(json.dumps(self.s, default=str)))

    def _id(self, prefix: str) -> str:
        self.s["seq"] += 1
        return f"{prefix}{self.s['seq']:04d}"

    def log(self, deal_id, actor: str, kind: str, text: str, data=None):
        self.s["activity"].append({"at": self.now(), "real_at": _iso(datetime.now(timezone.utc)), "deal_id": deal_id,
                                   "actor": actor, "kind": kind, "text": text, "data": data})

    def _wake(self, reason: str):
        for fn in list(self.listeners):
            try:
                fn(reason)
            except Exception:
                pass

    def deal(self, deal_id: str) -> dict:
        d = self.s["deals"].get(deal_id)
        if not d:
            for x in self.s["deals"].values():
                if x["ref"].lower() == str(deal_id).lower():
                    return x
            raise Refused(f"no deal {deal_id}")
        return d

    # ================================================================ reset / clock
    def reset(self, reason: str = "presenter reset", archive: bool = True) -> dict:
        with self.lock:
            followups = []
            if archive and self.s:
                for op in self.s.get("operations", {}).values():
                    if op["status"] in ("submitting", "unknown", "created", "RECEIVED"):
                        followups.append({**op, "client_secret": None, "marked_at": _iso(datetime.now(timezone.utc)),
                                          "note": "in flight at reset: needs follow-up against the provider"})
                # Keep the last few runs whole, for reconciling provider records against them later.
                self.runs.write(((self.runs.read() or []) + [{"run_id": self.s["run_id"], "state": self.s}])[-5:])
                if followups:
                    self.followups.write((self.followups.read() or []) + followups)
            self.scenario = self._load_scenario()
            h = self.hotel
            prior_events = self.s.get("events_seen", []) if self.s else []
            self.s = {
                "run_id": "run_" + uuid.uuid4().hex[:8],
                "clock": self.scenario["start"],
                "seq": 1000,
                # Sparse: only nights, mornings and meeting days something has touched are stored.
                # _inv / _bf / _slot fill in the rest from the scenario's bookable calendar.
                "inventory": {}, "meeting": {room: {} for room in h["meeting_rooms"]}, "breakfast": {},
                "emails": {}, "deals": {}, "holds": {}, "approvals": {}, "operations": {}, "ledger": [],
                "activity": [], "agent_runs": [], "events_seen": prior_events[-500:], "delayed_notifications": [],
                "reconcile_log": [],
                "sim": {"delay_refund_notifications": True, "fail_next_commit": False},
                "followups_at_reset": len(followups),
            }
            self.log(None, "system", "reset", f"Scenario reset ({reason}). New run {self.s['run_id']}; "
                     f"clock {self.s['clock']}." + (f" {len(followups)} in-flight provider operations marked for "
                                                    "follow-up." if followups else ""))
            for e in self.scenario["inbox"]:
                self.ingest_email(e["from"], e["subject"], e["body"], message_id=e["message_id"],
                                  received_at=e["received_at"], wake=False)
            self._save()
        self._wake("scenario reset: new inquiries in the inbox")   # the seller starts on its own
        return {"run_id": self.s["run_id"], "followups": len(followups)}

    # ================================================================ seller rules
    def update_payer(self, deal_id, country_code, currency=None, actor='hotel staff'):
        try:
            profile = fx.payer_profile(country_code, currency)
        except (ValueError, TypeError) as exc:
            raise Refused(str(exc))
        with self.lock:
            d = self.deal(deal_id)
            d['payer_origin'] = profile
            d['fx_reference'] = 'HH-' + uuid.uuid5(uuid.NAMESPACE_URL, self.s['run_id'] + ':' + d['id']).hex[:24]
            self.log(d['id'], actor, 'payer_origin', f"Payer origin recorded: {profile['country']} · {profile['local_currency']}. This does not establish received currency.")
            self._save()
            return self.payer_view(d)

    def payer_view(self, d):
        origin = copy.deepcopy(d.get('payer_origin'))
        if origin:
            origin['classification'] = fx.risk(origin['local_currency'])
        return {'origin': origin, 'reference': d.get('fx_reference'), **self.fx_journal.view(self.s['run_id'] + ':' + d['id'])}

    def scan_fx(self):
        """Automatic sandbox treasury scan. Network calls happen outside the booking lock."""
        if not self.fx_scan_lock.acquire(blocking=False):
            return {'busy': True}
        try:
            with self.lock:
                account = self.hotel_account
                run_id = self.s['run_id']
                watched = [(d['id'], d['fx_reference']) for d in self.s['deals'].values() if d.get('fx_reference')]
            processed = 0
            if account:
                by_reference = {reference: did for did, reference in watched}
                for receipt in self.fx_provider.receipts(account, list(by_reference)) if by_reference else []:
                    if receipt.get('reference') in by_reference:
                        did = by_reference[receipt['reference']]
                        receipt['deal_id'] = run_id + ':' + did
                        with self.fx_journal.lock:
                            before = copy.deepcopy(self.fx_journal.state['receipts'].get(receipt['account'] + ':' + receipt['id']))
                        row = self.fx_journal.process(receipt, self.fx_provider)
                        if not before or before['status'] != row['status']:
                            with self.lock:
                                if self.s['run_id'] == run_id:
                                    self.log(did, 'FX agent', 'fx_decision', f"{row['currency']} {row['amount']}: {row['action']} · {row['status']}. {row['reason']}")
                                    self._save()
                        processed += 1
            with self.fx_journal.lock:
                self.fx_journal.state.update(last_scan=_iso(datetime.now(timezone.utc)), scan_error=None if account else 'Connect the hotel account to monitor foreign bank receipts.')
                self.fx_journal.save()
            return {'processed': processed, 'account': account}
        except Exception as exc:
            with self.fx_journal.lock:
                self.fx_journal.state.update(last_scan=_iso(datetime.now(timezone.utc)), scan_error=str(exc)[:400])
                self.fx_journal.save()
            return {'error': str(exc)[:400]}
        finally:
            self.fx_scan_lock.release()

    def simulate_fx_receipt(self, deal_id):
        if not self.fx_provider.simulated:
            raise Refused('Local FX receipt fixtures are only available with simulated payments.')
        with self.lock:
            d = self.deal(deal_id)
            origin = d.get('payer_origin')
            if not origin or not d.get('fx_reference'):
                raise Refused('Record the payer country and local currency first.')
            if not self.hotel_account:
                raise Refused('Connect a simulated hotel account first.')
            self.fx_provider.add_receipt(self.hotel_account, d['fx_reference'], origin['local_currency'], '1000')
        self.scan_fx()
        return self.payer_view(d)

    def _load_scenario(self) -> dict:
        return bookable.expand(rules.apply(json.loads(self.scenario_path.read_text()), self.rule_store.read() or {}))

    def lead_days(self, arrival: str) -> int:
        """Whole days from the scenario clock's date to `arrival`."""
        return (calc.d(arrival) - calc.d(self.now()[:10])).days

    def rules_view(self) -> dict:
        with self.lock:
            base = json.loads(self.scenario_path.read_text())
            return rules.describe(self.scenario, base)

    def update_rules(self, changes: dict, actor: str = "hotel manager") -> dict:
        """Change the seller's editable rules. Applies to pricing from now on; versions already
        created keep the terms (and hash) they were made with."""
        with self.lock:
            unknown = [k for k in changes if k not in rules.PATHS]
            if unknown:
                raise Refused(f"not an editable rule: {', '.join(unknown)}")
            try:
                changes = {k: int(v) for k, v in changes.items()}
            except (TypeError, ValueError):
                raise Refused("rule values must be whole numbers (pence for money)")
            base = json.loads(self.scenario_path.read_text())
            overrides = {**(self.rule_store.read() or {}), **changes}
            candidate = rules.apply(base, overrides)
            problems = rules.validate(candidate)
            if problems:
                raise Refused("; ".join(problems))
            diff = [(p, rules.get(self.scenario, p), rules.get(candidate, p)) for p in changes
                    if rules.get(self.scenario, p) != rules.get(candidate, p)]
            # Store only what differs from the scenario file.
            self.rule_store.write({p: v for p, v in overrides.items() if rules.get(base, p) != v})
            self.scenario = bookable.expand(candidate)
            if diff:
                text = "; ".join(f"{rules.PATHS[p][0]} {rules._fmt(rules.PATHS[p][1], a)} → "
                                 f"{rules._fmt(rules.PATHS[p][1], b)}" for p, a, b in diff)
                self.log(None, actor, "rules_changed", f"Seller rules changed: {text}. Applies to new offers and "
                         "changes; versions already made keep their terms.")
                self._save()
        if diff:
            self._wake("seller rules changed")
        return {**self.rules_view(), "saved": len(diff)}

    def reset_rules(self, actor: str = "hotel manager") -> dict:
        with self.lock:
            current = self.rule_store.read() or {}
            base = json.loads(self.scenario_path.read_text())
            return self.update_rules({p: rules.get(base, p) for p in current}, actor=actor) if current \
                else {**self.rules_view(), "saved": 0}

    # ================================================================ seller pause
    def _pause_seller(self, d: dict, what: str, error) -> None:
        """A provider refused a money operation. Stop retrying it on every wake: hotel staff look at
        the error and retry once it's fixed."""
        d["seller_paused"] = {"at": self.now(), "what": what, "error": str(error)}
        self.log(d["id"], "system", "seller_paused", f"Seller paused on {d['ref']}: {what} failed ({error}). "
                 "Hotel staff can retry once the cause is fixed.")

    def retry_seller(self, deal_id: str, actor: str = "hotel staff") -> dict:
        with self.lock:
            d = self.deal(deal_id)
            if not d.get("seller_paused"):
                raise Refused("the seller isn't paused on this deal")
            p = d.pop("seller_paused")
            self.log(d["id"], actor, "seller_retry", f"{cap_first(actor)} asked the seller to retry: {p['what']}.")
            self._save()
        self._wake(f"{actor} asked the seller to retry {p['what']} on {d['ref']}")
        return {"retrying": p["what"]}

    @staticmethod
    def _mornings(nights: list[str]) -> list[str]:
        return [(calc.d(n) + timedelta(days=1)).isoformat() for n in nights]

    def advance_clock(self, hours: float | None = None, to: str | None = None) -> str:
        with self.lock:
            cur = _dt(self.now())
            new = _dt(to) if to else cur + timedelta(hours=hours or 0)
            if new < cur:
                raise Refused("the scenario clock only moves forward")
            self.s["clock"] = _iso(new)
            self.log(None, "presenter", "clock", f"Scenario clock moved to {self.s['clock']}.")
            self._sweep()
            self._save()
        self._wake("clock")
        return self.s["clock"]

    def set_sim(self, **knobs):
        with self.lock:
            for k, v in knobs.items():
                if k in self.s["sim"]:
                    self.s["sim"][k] = bool(v)
                elif hasattr(self.provider, "knobs") and k in self.provider.knobs:
                    self.provider.knobs[k] = bool(v)
            self.log(None, "presenter", "sim", f"Simulation settings: {knobs}")
            self._save()

    # ================================================================ intake
    def ingest_email(self, sender: str, subject: str, body: str, message_id: str | None = None,
                     in_reply_to: str | None = None, received_at: str | None = None, wake: bool = True) -> dict:
        with self.lock:
            norm = re.sub(r"\s+", " ", body.strip().lower())
            key = message_id or "h:" + hashlib.sha256(f"{sender.lower()}|{norm}".encode()).hexdigest()[:20]
            content_key = "c:" + hashlib.sha256(f"{sender.lower()}|{norm}".encode()).hexdigest()[:20]
            for e in self.s["emails"].values():
                if e["key"] == key or e["content_key"] == content_key:
                    self.log(e["deal_id"], "system", "duplicate",
                             f"Duplicate email ignored ({subject!r}); already filed under {self.s['deals'][e['deal_id']]['ref']}.")
                    self._save()
                    return {"deal_id": e["deal_id"], "duplicate": True}
            domain = sender.split("@")[-1].strip(" >").lower()
            deal = None
            if in_reply_to:
                deal = next((self.s["deals"][e["deal_id"]] for e in self.s["emails"].values()
                             if e["key"] == in_reply_to), None)
            m = re.search(r"\b(GS-\d{4})\b", subject)
            if not deal and m:
                deal = next((x for x in self.s["deals"].values() if x["ref"] == m.group(1)), None)
            base = re.sub(r"^\s*((re|fw|fwd)\s*:\s*)+", "", subject, flags=re.I).strip().lower()
            if not deal and base != subject.strip().lower():
                deal = next((x for x in self.s["deals"].values()
                             if x["subject"].strip().lower() == base and x["domain"] == domain), None)
            eid = self._id("em")
            at = received_at or self.now()
            if deal:
                self.s["emails"][eid] = {"id": eid, "key": key, "content_key": content_key, "deal_id": deal["id"],
                                         "from": sender, "subject": subject, "body": body, "at": at}
                deal["thread"].append({"id": eid, "at": at, "from": "buyer", "channel": "email", "text": body})
                deal["unread"] = True
                self.log(deal["id"], "buyer", "email", f"Reply threaded to {deal['ref']}: {body[:140]}")
                self._save()
                if wake:
                    self._wake("email")
                return {"deal_id": deal["id"], "duplicate": False, "threaded": True}

            did = self._id("deal_")
            ref = f"GS-{1000 + len(self.s['deals']) + 1}"
            preview = extract(subject, body, sender)
            crm = self.scenario["crm"].get(domain)
            deal = {
                "id": did, "ref": ref, "domain": domain,
                "company": (crm or {}).get("account") or _company_from(sender, domain),
                "sender": sender, "subject": subject, "received_at": at,
                "reply_by": preview.get("reply_by"), "status": "new", "requirements": None, "missing": [],
                "thread": [{"id": eid, "at": at, "from": "buyer", "channel": "email", "text": body,
                            "subject": subject}],
                "records": [], "versions": [], "hold_id": None, "accepted_version": None, "booking": None,
                "finance": {"refund_owed": 0, "refund_owed_version": None}, "flags": [],
                "buyer_token": secrets.token_urlsafe(12), "unread": True, "handoff": None,
                "buyer_profile": domain if domain in self.scenario["buyers"] else None,
                "fx_reference": 'HH-' + uuid.uuid5(uuid.NAMESPACE_URL, self.s['run_id'] + ':' + did).hex[:24],
                "pending_acceptance": None,
            }
            self.s["deals"][did] = deal
            self.s["emails"][eid] = {"id": eid, "key": key, "content_key": content_key, "deal_id": did,
                                     "from": sender, "subject": subject, "body": body, "at": at}
            self.log(did, "system", "intake", f"New inquiry {ref} from {sender}: {subject}")
            self._save()
        if wake:
            self._wake("email")
        return {"deal_id": did, "duplicate": False, "threaded": False}

    # ================================================================ seller: reading
    def queue(self) -> list[dict]:
        with self.lock:
            self._sweep()
            out = []
            for d in self.s["deals"].values():
                cur = self._current_version(d)
                hours_left = None
                if d.get("reply_by"):
                    hours_left = round((_dt(d["reply_by"]) - _dt(self.now())).total_seconds() / 3600, 1)
                value = cur["pricing"]["total"] if cur else None
                out.append({
                    "deal_id": d["id"], "ref": d["ref"], "company": d["company"], "status": d["status"],
                    "reply_by": d.get("reply_by"), "hours_to_deadline": hours_left,
                    "unread_buyer_input": d.get("unread", False),
                    "seller_paused": bool(d.get("seller_paused")),
                    "waiting_on": self.waiting_on(d), "next_step": self.next_step(d),
                    "current_version": cur["n"] if cur else None,
                    "package_value": gbp(value) if value is not None else None,
                    "margin_pct": cur["pricing"]["margin_all_rooms"]["margin_pct"] if cur else None,
                })
            out.sort(key=lambda x: (x["reply_by"] or "9999"))
            return out

    def deal_view(self, deal_id: str, for_: str = "seller", mark_read: bool = False) -> dict:
        with self.lock:
            d = self.deal(deal_id)
            if mark_read and for_ == "seller":
                d["unread"] = False
                self._save()
            if for_ not in ("seller", "buyer"):
                raise Refused("deal views exist for the seller or the buyer only")
            # Each side sees only its own private constraints. The seller never gets the buyer's mandate
            # or the organizer's reasons; the buyer never gets floors, costs, margins, hotel checks,
            # records, holds, flags or the hotel manager's approval summaries. Thread messages are what
            # each side chose to send. The presenter dashboard (snapshot) shows both sides.
            view = {k: copy.deepcopy(d[k]) for k in ("id", "ref", "company", "sender", "subject", "status",
                                                      "reply_by", "requirements", "missing", "thread",
                                                      "accepted_version")}
            view["finance"] = self.finance(d)
            view["booking"] = copy.deepcopy(d.get("booking"))
            if for_ == "seller":
                view["flags"] = list(d["flags"])
                view["waiting_on"] = self.waiting_on(d)
                view["versions"] = [self._version_public(v, for_) for v in d["versions"]]
                view["next_step"] = self.next_step(d)
                view["records_consulted"] = [{"system": r["system"], "checked_at": r["checked_at"],
                                              "summary": r["summary"]} for r in d["records"]]
                view["hold"] = self._hold_public(d)
                view["approvals"] = [self._approval_for_seller(a) for a in self.s["approvals"].values()
                                     if a["deal_id"] == d["id"]]
                view["key_dates"] = self._key_dates(d)
                view["payouts"] = self.payout_view(d)
                view["clock"] = self.now()
                view["staff_instruction"] = copy.deepcopy(d.get("staff_instruction"))
                view["group_calendar"] = {"first_night": self.hotel["nights"][0], "last_night": self.hotel["nights"][-1],
                                          "min_days_before_arrival": self.policy["min_days_before_arrival"]}
            else:
                w = self.waiting_on(d) or ""
                view["waiting_on"] = w if w.startswith(("buyer", "buyer's organizer")) else ("hotel" if w else None)
                # Drafts and versions awaiting the hotel manager are internal until the hotel sends them.
                view["versions"] = [self._version_public(v, for_) for v in d["versions"] if v.get("sent_at")]
            return view

    @staticmethod
    def _approval_for_seller(a: dict) -> dict:
        """The hotel's own approvals in full; buyer-side (organizer) approvals only as a pending/decided marker."""
        if a["approver"] != "organizer":
            return copy.deepcopy(a)
        return {"id": a["id"], "deal_id": a["deal_id"], "ref": a["ref"], "approver": "organizer",
                "subject": a["subject"], "version": a["version"], "status": a["status"],
                "requested_at": a["requested_at"], "decided_at": a["decided_at"],
                "summary": f"Acceptance of v{a['version']} is pending buyer-side authorization"
                if a["status"] == "pending" else f"Buyer-side authorization {a['status']} for v{a['version']}"}

    def _version_public(self, v: dict, for_: str) -> dict:
        out = {k: v[k] for k in ("n", "kind", "status", "hash", "terms", "created_at", "sent_at", "message",
                                 "explanation", "unmet", "expires_at") if k in v}
        p = v["pricing"]
        out["pricing"] = {k: p[k] for k in ("lines", "total", "deposit", "deposit_pct", "attrition_pct",
                                            "attrition_allowance", "allowance_remaining", "room_nights",
                                            "contracted_room_nights", "released_room_nights",
                                            "per_room_night_incl_breakfast", "accommodation")}
        if for_ == "seller":
            out["pricing"]["margin_all_rooms"] = p["margin_all_rooms"]
            out["pricing"]["margin_full_release"] = p["margin_full_release"]
            if "displacement" in p:
                out["pricing"]["displacement"] = p["displacement"]
            out["hotel_check"] = v.get("hotel_check")
            out["reduction"] = v.get("reduction")
        elif v.get("reduction"):
            r = v["reduction"]
            out["reduction"] = {k: r[k] for k in ("requested_room_nights", "allowance", "remaining_after",
                                                  "revised_deposit", "refund_due", "refund_mode")}
        return out

    def _hold_public(self, d):
        h = self.s["holds"].get(d.get("hold_id") or "")
        return h and {k: h[k] for k in ("id", "status", "expires_at", "rooms", "room_type", "meeting")}

    # ================================================================ seller: requirements & records
    def record_requirements(self, deal_id: str, req: dict, actor: str = "seller") -> dict:
        with self.lock:
            d = self.deal(deal_id)
            base = d["requirements"] or {}
            merged = {**base, **{k: v for k, v in req.items() if v is not None}}
            if isinstance(req.get("meeting"), dict) and isinstance(base.get("meeting"), dict):
                merged["meeting"] = {**base["meeting"], **{k: v for k, v in req["meeting"].items() if v is not None}}
            if req.get("meeting") is None and "meeting" in req and base.get("meeting") is None:
                merged["meeting"] = None
            rt = merged.get("room_type")
            if rt and rt not in self.hotel["room_types"]:
                merged.setdefault("unsupported", []).append(f"{rt} rooms")
            # Unsupported services go to a person. Dates the hotel can't sell are not a hand-off: the seller
            # offers the buyer dates that are available (suggest_dates / offer_dates).
            problems = list(merged.get("unsupported") or [])
            merged["date_issue"] = self._date_issue(merged)
            merged["out_of_scope"] = problems
            d["requirements"] = merged
            d["missing"] = missing_info(merged)
            if d["status"] == "new" or (d["status"] == "dates_proposed" and not merged["date_issue"]
                                        and merged.get("arrival")):
                d["status"] = "intake"
            self.log(d["id"], actor, "requirements",
                     "Requirements recorded" + (f"; missing: {', '.join(d['missing'])}" if d["missing"] else "; complete")
                     + (f"; out of scope: {', '.join(problems)}" if problems else "")
                     + (f"; {merged['date_issue']}" if merged["date_issue"] else ""), merged)
            self._save()
            return {"requirements": merged, "missing": d["missing"], "out_of_scope": problems,
                    "date_issue": merged["date_issue"]}

    def _date_issue(self, req: dict) -> str | None:
        if not (req.get("arrival") and req.get("departure")):
            return None
        try:
            nights = stay_nights(req["arrival"], req["departure"])
        except (TypeError, ValueError):
            return f"dates {req['arrival']} to {req['departure']} could not be read"
        if not nights:
            return f"departure {req['departure']} is not after arrival {req['arrival']}"
        if any(n not in self.hotel["capacity"] for n in nights):
            return (f"requested dates {req['arrival']} to {req['departure']} are not available for groups "
                    f"(group calendar: {bookable.window_text(self.hotel)})")
        if self.lead_days(req["arrival"]) < self.policy["min_days_before_arrival"]:
            return (f"arrival {req['arrival']} is less than {self.policy['min_days_before_arrival']} days away: "
                    "groups need more notice")
        return None

    # ---- other dates, when the requested ones can't be sold -------------------------------------
    def _windows(self, length: int) -> list[list[str]]:
        """Runs of consecutive nights in the group calendar, `length` long (or the longest available),
        arriving with at least the policy's minimum notice."""
        min_lead = self.policy["min_days_before_arrival"]
        cal = [n for n in sorted(self.hotel["nights"]) if self.lead_days(n) >= min_lead]
        runs, cur = [], []
        for n in cal:
            if cur and date.fromisoformat(n) - date.fromisoformat(cur[-1]) != timedelta(days=1):
                runs.append(cur)
                cur = []
            cur.append(n)
        if cur:
            runs.append(cur)
        out = []
        for r in runs:
            size = min(length, len(r))
            out += [r[i:i + size] for i in range(len(r) - size + 1)]
        return out

    def _window_option(self, d: dict, nights: list[str], room_type: str | None) -> dict:
        req = d["requirements"] or {}
        types = [room_type] if room_type else list(self.hotel["room_types"])
        avail = {t: min(self._available(n, t) for n in nights) for t in types}
        want = int(req.get("rooms") or 0)
        rt = room_type or req.get("room_type")
        last = date.fromisoformat(nights[-1]) + timedelta(days=1)
        opt = {"arrival": nights[0], "departure": last.isoformat(), "nights": len(nights), "rooms_free": avail,
               "fits_rooms": bool(rt and want and avail.get(rt, 0) >= want)}
        if req.get("meeting"):
            free = [n for n in nights + [last.isoformat()] if (self._slot(n) or {}).get("status") == "free"]
            opt["meeting_room_free_on"] = free
        return opt

    def suggest_dates(self, deal_id: str, room_type: str | None = None, actor: str = "seller") -> dict:
        """Dates in the group calendar with rooms free for this request, nearest to what was asked first."""
        with self.lock:
            self._sweep()
            d = self.deal(deal_id)
            req = d["requirements"]
            if not req or not (req.get("arrival") and req.get("departure")):
                raise Refused("record the requested dates first")
            if room_type and room_type not in self.hotel["room_types"]:
                raise Refused(f"unknown room type {room_type}")
            rt = room_type or (req.get("room_type") if req.get("room_type") in self.hotel["room_types"] else None)
            try:
                length = max(1, len(stay_nights(req["arrival"], req["departure"])))
            except (TypeError, ValueError):
                length = 1
            asked = date.fromisoformat(req["arrival"]) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(req["arrival"])) else None
            opts = [self._window_option(d, w, rt) for w in self._windows(length)]
            opts = [o for o in opts if any(v > 0 for v in o["rooms_free"].values())]
            opts.sort(key=lambda o: (not o["fits_rooms"], o["nights"] != length,
                                     abs((date.fromisoformat(o["arrival"]) - asked).days) if asked else 0))
            cal = sorted(self.hotel["nights"])
            rec = {"system": "PMS (other dates)", "checked_at": self.now(), "query": d["ref"],
                   "summary": ("; ".join(f"{o['arrival']} to {o['departure']}: " + ", ".join(f"{v} {t}" for t, v in o["rooms_free"].items())
                                         + " free" for o in opts[:5]) or "no group rooms free in the calendar"),
                   "data": {"requested": {"arrival": req["arrival"], "departure": req["departure"]}, "options": opts[:5]}}
            d["records"] = [rec] + d["records"][:15]
            self.log(d["id"], actor, "records", "Checked the group calendar for other dates.", rec["summary"])
            self._save()
            return {"requested": {"arrival": req["arrival"], "departure": req["departure"], "nights": length,
                                  "rooms": req.get("rooms"), "room_type": rt},
                    "date_issue": req.get("date_issue"),
                    "group_calendar": {"first_night": cal[0], "last_night": cal[-1]},
                    "options": opts[:5]}

    def offer_dates(self, deal_id: str, options: list[dict], message: str, actor: str = "seller") -> dict:
        """Email the buyer other dates. Each option must be in the group calendar with the rooms free now:
        the seller can't offer inventory that doesn't exist."""
        with self.lock:
            self._sweep()
            d = self.deal(deal_id)
            req = d["requirements"]
            if not req:
                raise Refused("record the requirements first")
            if d.get("booking") or d.get("accepted_version"):
                raise Refused("this deal already has an agreement; changes go through prepare_reduction")
            if not options:
                raise Refused("give at least one option")
            if not (message or "").strip():
                raise Refused("write the message to the buyer")
            clean = []
            for o in options[:5]:
                try:
                    nights = stay_nights(o["arrival"], o["departure"])
                except (KeyError, TypeError, ValueError):
                    raise Refused(f"option {o} needs valid arrival and departure dates")
                if not nights:
                    raise Refused(f"option {o['arrival']} to {o['departure']}: departure must be after arrival")
                if any(n not in self.hotel["capacity"] for n in nights):
                    raise Refused(f"option {o['arrival']} to {o['departure']} is outside the group calendar "
                                  f"({bookable.window_text(self.hotel)})")
                if self.lead_days(o["arrival"]) < self.policy["min_days_before_arrival"]:
                    raise Refused(f"option {o['arrival']} to {o['departure']}: arrival is less than "
                                  f"{self.policy['min_days_before_arrival']} days away")
                rt = o.get("room_type") or req.get("room_type")
                if rt not in self.hotel["room_types"]:
                    raise Refused(f"option {o['arrival']} to {o['departure']}: say which room type")
                rooms = int(o.get("rooms_per_night") or req.get("rooms") or 0)
                if rooms <= 0:
                    raise Refused(f"option {o['arrival']} to {o['departure']}: say how many rooms a night")
                short = [n for n in nights if self._available(n, rt) < rooms]
                if short:
                    raise Refused(f"option {o['arrival']} to {o['departure']}: only "
                                  f"{min(self._available(n, rt) for n in nights)} {rt} rooms free on {', '.join(short)}")
                clean.append({"arrival": o["arrival"], "departure": o["departure"], "room_type": rt,
                              "rooms_per_night": rooms})
            d["thread"].append({"id": self._id("msg"), "at": self.now(), "from": "seller", "channel": "email",
                                "text": message.strip(), "date_options": clean})
            d["status"] = "dates_proposed"
            d["handoff"] = None
            self._seller_acted(d)
            self.log(d["id"], actor, "dates_offered", "Offered other dates: " + "; ".join(
                f"{c['arrival']} to {c['departure']} ({c['rooms_per_night']} {c['room_type']})" for c in clean))
            self._save()
            return {"sent": True, "status": d["status"], "options": clean}

    def check_systems(self, deal_id: str, actor: str = "seller") -> dict:
        """Consult PMS, meeting space, breakfast, RMS, CRM and policy records for this deal."""
        with self.lock:
            self._sweep()
            d = self.deal(deal_id)
            req = d["requirements"]
            if not req:
                raise Refused("record the requirements first")
            now = self.now()
            nights = stay_nights(req["arrival"], req["departure"]) if req.get("arrival") and req.get("departure") else []
            rt = req.get("room_type")
            own_hold = self.s["holds"].get(d.get("hold_id") or "")
            pms = {}
            for n in nights:
                inv = self._inv(n)
                if not inv:
                    pms[n] = "outside the bookable dates"
                    continue
                pms[n] = {t: {"available": self._available(n, t), "held": inv[t]["held"], "booked": inv[t]["booked"]}
                          for t in inv}
            meeting = None
            if req.get("meeting"):
                day = req["meeting"].get("date")
                slot = self._slot(day)
                mr = self.hotel["meeting_rooms"]["thames"]
                meeting = {"room": "Thames Room", "date": day, "status": slot["status"] if slot else "not available",
                           "hours_available": mr["available"].get(day), "capacity": mr["capacity"],
                           "day_rate": gbp(mr["day_rate"]), "includes": mr["includes"]}
            bf = {m: {"capacity": self.hotel["breakfast"]["group_capacity_per_day"],
                      "committed": v["held"] + v["booked"]}
                  for m, v in ((m, self._bf(m)) for m in self._mornings(nights)) if v}
            rates = self.rms["rates"].get(rt) if rt else self.rms["rates"]
            req_dates = calc.key_dates(req["arrival"], self.policy) if req.get("arrival") else None
            crm = self.scenario["crm"].get(d["domain"], {"account": None, "type": "unknown sender", "credit_terms": False})
            records = [
                {"system": "PMS", "summary": "; ".join(
                    f"{n}: {v[rt]['available'] if isinstance(v, dict) and rt in v else v} {rt or 'rooms'} free"
                    for n, v in pms.items()) + (f" (own hold {own_hold['id']} {own_hold['status']})" if own_hold else ""),
                 "data": pms},
                {"system": "Meeting space", "summary": (f"Thames Room {meeting['date']}: {meeting['status']}, "
                                                        f"{meeting['hours_available']}, classroom {meeting['capacity'].get('classroom')}")
                 if meeting else "No meeting space requested", "data": meeting},
                {"system": "F&B (breakfast)", "summary": "; ".join(f"{m}: {v['committed']}/{v['capacity']} group covers committed"
                                                                 for m, v in bf.items()), "data": bf},
                {"system": "RMS", "summary": (f"{rt}: target {gbp(rates['target'])}, seller floor {gbp(rates['autonomous_floor'])}, "
                                              f"absolute min {gbp(rates['absolute_min'])}; min margin {self.rms['min_contribution_margin_pct']}%")
                 if rt else "room type unknown", "data": {"rates": rates, "delivery_costs": self.rms["delivery_costs"],
                                                                   "displacement": self.rms.get("displacement", {})}},
                {"system": "CRM", "summary": f"{crm.get('account') or d['domain']}: {crm.get('type')}, "
                                             f"credit terms {'yes' if crm.get('credit_terms') else 'no'}", "data": crm},
                {"system": "Policy", "summary": "Deposit 30% standard (up to 40%; 20-29% needs manager); attrition 10% "
                                                "standard (to 20% with manager, needs 40% deposit); quotes/holds 24h",
                 "data": {"deposit": self.policy["deposit"], "attrition": self.policy["attrition"],
                          "key_dates": req_dates}},
            ]
            for r in records:
                r["checked_at"] = now
                r["query"] = d["ref"]
            d["records"] = records + [r for r in d["records"] if r["checked_at"] != now][:12]
            self.log(d["id"], actor, "records", "Checked PMS, meeting space, breakfast, RMS, CRM and policy records.",
                     [{"system": r["system"], "summary": r["summary"]} for r in records])
            self._save()
            return {"checked_at": now, "pms": pms, "meeting": meeting, "breakfast": bf, "rms": rates,
                    "crm": crm, "key_dates": req_dates}

    def ask_buyer(self, deal_id: str, questions: list[str], message: str, actor: str = "seller") -> dict:
        with self.lock:
            d = self.deal(deal_id)
            d["thread"].append({"id": self._id("msg"), "at": self.now(), "from": "seller", "channel": "email",
                                "text": message, "questions": questions})
            d["status"] = "needs_info"
            self._seller_acted(d)
            self.log(d["id"], actor, "clarification", f"Asked {d['company']}: {'; '.join(questions)}")
            self._save()
            return {"sent": True, "status": d["status"]}

    def note(self, deal_id: str | None, text: str, actor: str = "seller"):
        with self.lock:
            self.log(deal_id and self.deal(deal_id)["id"], actor, "reasoning", text)
            self._save()

    # ---- agent trace (see trace.py): presenter-side record of each seller run and its tool calls
    def agent_run_start(self, agent: str, trigger: str = "", model: str | None = None) -> dict:
        # The run joins the record only with its first step: a wake-up that finds nothing to do leaves no line
        # (and nothing that a save in between could persist and a later removal leave behind).
        return {"id": "ar_" + uuid.uuid4().hex[:8], "agent": agent, "model": model, "trigger": trigger,
                "at": self.now(), "real_at": _iso(datetime.now(timezone.utc)), "steps": []}

    def agent_step(self, run: dict, step: dict):
        with self.lock:
            runs = self.s.setdefault("agent_runs", [])
            if not any(r is run for r in runs):
                runs.append(run)
                del runs[:-60]
            run["steps"].append(step)

    def agent_run_end(self, run: dict):
        with self.lock:
            if any(r is run for r in self.s.get("agent_runs", [])):
                self._save()

    # ================================================================ offers
    def build_terms(self, d: dict, rate: int, deposit_pct: int, attrition_pct: int,
                    rooms_per_night: int | None = None, room_type: str | None = None) -> dict:
        req = d["requirements"] or {}
        if req.get("out_of_scope"):
            raise Refused("request is outside supported dates/services: " + "; ".join(req["out_of_scope"])
                          + ". Hand it to a person (escalate).")
        if req.get("date_issue"):
            raise Refused(req["date_issue"] + ". Call suggest_dates and send the buyer available dates with "
                          "offer_dates; do not hand this to a person.")
        if not (req.get("arrival") and req.get("departure") and (room_type or req.get("room_type"))):
            raise Refused("requirements incomplete: need dates and room type")
        nights = stay_nights(req["arrival"], req["departure"])
        if self.lead_days(req["arrival"]) < self.policy["min_days_before_arrival"]:
            raise Refused(f"arrival {req['arrival']} is less than {self.policy['min_days_before_arrival']} days away: "
                          "groups need more notice. Call suggest_dates and offer the buyer later dates with offer_dates")
        n_rooms = int(rooms_per_night or req.get("rooms") or 0)
        if n_rooms <= 0:
            raise Refused("need a number of rooms")
        guests = int(req.get("guests") or n_rooms * int(req.get("occupancy") or 1))
        terms = {
            "room_type": room_type or req["room_type"], "rate": int(rate),
            "rooms": {n: n_rooms for n in nights}, "released": {n: 0 for n in nights},
            "breakfast": {m: guests for m in self._mornings(nights)} if req.get("breakfast") else {},
            "meeting": None, "attrition_pct": int(attrition_pct), "deposit_pct": int(deposit_pct),
            "payer": req.get("payer"), "currency": "GBP",
        }
        if req.get("meeting"):
            m = req["meeting"]
            terms["meeting"] = {"room": "thames", "date": m.get("date"), "layout": m.get("layout"),
                                "attendees": int(m.get("attendees") or guests), "hours": m.get("hours")}
        return terms

    def price_terms(self, deal_id: str, rate: int, deposit_pct: int, attrition_pct: int,
                    rooms_per_night: int | None = None, room_type: str | None = None) -> dict:
        """Dry run: what these terms cost, earn, and whether the seller may offer them."""
        with self.lock:
            d = self.deal(deal_id)
            terms = self.build_terms(d, rate, deposit_pct, attrition_pct, rooms_per_night, room_type)
            pricing = calc.price(terms, self.hotel, self.rms)
            check = hotel_check(terms, pricing, self.scenario)
            return {"terms": terms, "pricing": pricing, "hotel_check": check,
                    "fits_inventory": self._can_hold(terms, d)}

    def propose_offer(self, deal_id: str, rate: int, deposit_pct: int, attrition_pct: int, message: str,
                      explanation: str = "", unmet: list[str] | None = None, rooms_per_night: int | None = None,
                      room_type: str | None = None, justification: str = "", actor: str = "seller") -> dict:
        with self.lock:
            self._sweep()
            d = self.deal(deal_id)
            if d.get("accepted_version") and d["status"] not in ("expired",):
                raise Refused("an agreement is already accepted; changes go through prepare_reduction")
            if d["missing"]:
                raise Refused("still missing: " + "; ".join(d["missing"]) + ". Ask the buyer first.")
            terms = self.build_terms(d, rate, deposit_pct, attrition_pct, rooms_per_night, room_type)
            pricing = calc.price(terms, self.hotel, self.rms)
            check = hotel_check(terms, pricing, self.scenario)
            if not check["ok"]:
                raise Refused("outside hotel limits: " + "; ".join(check["hard"]))
            if not self._can_hold(terms, d):
                raise Refused("inventory cannot be held for these terms (rooms, meeting space or breakfast capacity); "
                              "offer a feasible alternative or explain why you cannot quote")
            for v in d["versions"]:
                if v["status"] in ("draft", "pending_approval", "approved", "sent"):
                    v["status"] = "superseded"
            for a in self.s["approvals"].values():
                if a["deal_id"] == d["id"] and a["status"] == "pending" and a["subject"] == "offer":
                    a["status"] = "stale"
            n = len(d["versions"]) + 1
            v = {"n": n, "kind": "offer", "terms": terms, "pricing": pricing, "hotel_check": check,
                 "hash": _hash({"kind": "offer", "terms": terms}), "status": "draft", "created_at": self.now(),
                 "sent_at": None, "message": message, "explanation": explanation, "unmet": unmet or [],
                 "expires_at": None}
            d["versions"].append(v)
            self.log(d["id"], actor, "offer_drafted",
                     f"Drafted v{n}: {terms['room_type']} {gbp(terms['rate'])}, {terms['attrition_pct']}% attrition, "
                     f"{terms['deposit_pct']}% deposit; total {gbp(pricing['total'])}, deposit {gbp(pricing['deposit'])}, "
                     f"margin {pricing['margin_all_rooms']['margin_pct']}% / {pricing['margin_full_release']['margin_pct']}%.")
            if check["approvals"]:
                v["status"] = "pending_approval"
                d["status"] = "pending_approval"
                summary = (f"{d['company']} v{n}: {'; '.join(check['approvals'])}. Total {gbp(pricing['total'])}, "
                           f"deposit {terms['deposit_pct']}% = {gbp(pricing['deposit'])}, attrition allowance "
                           f"{pricing['attrition_allowance']} room-nights. Margin {pricing['margin_all_rooms']['margin_pct']}% "
                           f"(all rooms) / {pricing['margin_full_release']['margin_pct']}% (full release)."
                           + (f" Seller's case: {justification}" if justification else ""))
                appr = self._request_approval(d, "hotel_manager", "offer", summary, check["approvals"],
                                              version=n, obj_hash=v["hash"])
                self._save()
                return {"version": n, "status": "pending_approval", "approval_id": appr["id"], "pricing": pricing,
                        "note": "Paused: hotel manager approval required before this version can be sent."}
            res = self._send_version(d, v, actor)
            self._save()
            return res

    def send_offer(self, deal_id: str, version: int, actor: str = "seller") -> dict:
        with self.lock:
            self._sweep()
            d = self.deal(deal_id)
            v = self._version(d, version)
            if v is not d["versions"][-1]:
                raise Refused(f"v{version} is not the latest version")
            if v["status"] == "sent":
                return {"version": version, "status": "sent", "note": "already sent"}
            if v["status"] != "approved":
                raise Refused(f"v{version} is {v['status']}; it needs a valid hotel manager approval first")
            if not self._approval_valid(d, "offer", version, v["hash"]):
                raise Refused("the approval on file does not match this version's terms")
            res = self._send_version(d, v, actor)
            self._save()
            return res

    def _send_version(self, d: dict, v: dict, actor: str) -> dict:
        hold = self.s["holds"].get(d.get("hold_id") or "")
        if v["kind"] == "offer":
            if hold and hold["status"] == "active":
                if not self._hold_covers(hold, v["terms"]):
                    self._release_hold(hold, "replaced by a new package")
                    hold = None
            if not hold or hold["status"] != "active":
                hold = self._create_hold(d, v["terms"])
                if not hold:
                    v["status"] = "draft"
                    raise Refused("could not hold the rooms/meeting space; this offer cannot be sent as bookable")
            v["expires_at"] = hold["expires_at"]
        v["status"] = "sent"
        v["sent_at"] = self.now()
        d["status"] = "offer_sent" if v["kind"] == "offer" else "amendment_sent"
        self._seller_acted(d)
        d["thread"].append({"id": self._id("msg"), "at": self.now(), "from": "seller", "channel": "email",
                            "text": v["message"], "version": v["n"]})
        p = v["pricing"]
        self.log(d["id"], actor, "offer_sent" if v["kind"] == "offer" else "amendment_sent",
                 f"Sent v{v['n']} to {d['company']}: total {gbp(p['total'])}, deposit {gbp(p['deposit'])}"
                 + (f"; held until {v['expires_at']}" if v.get("expires_at") else ""))
        return {"version": v["n"], "status": "sent", "expires_at": v.get("expires_at"), "total": gbp(p["total"]),
                "deposit": gbp(p["deposit"])}

    def _version(self, d, n) -> dict:
        for v in d["versions"]:
            if v["n"] == int(n):
                return v
        raise Refused(f"no version {n}")

    def _current_version(self, d):
        if d.get("accepted_version"):
            return self._version(d, d["accepted_version"])
        live = [v for v in d["versions"] if v["status"] not in ("superseded", "rejected")]
        return live[-1] if live else None

    # ================================================================ approvals
    def _request_approval(self, d, approver: str, subject: str, summary: str, reasons: list[str], version=None,
                          obj_hash=None, amount=None, extra=None) -> dict:
        aid = self._id("ap")
        a = {"id": aid, "deal_id": d["id"], "ref": d["ref"], "approver": approver, "subject": subject,
             "summary": summary, "reasons": reasons, "version": version, "hash": obj_hash, "amount": amount,
             "status": "pending", "requested_at": self.now(), "decided_at": None, "note": None, **(extra or {})}
        self.s["approvals"][aid] = a
        if approver == "hotel_manager":
            self.log(d["id"], "system", "approval_requested", f"Hotel manager approval requested: {summary}")
        else:
            # The organizer's reasons are the buyer's private mandate: they stay on the approval itself.
            self.log(d["id"], "system", "approval_requested",
                     f"Acceptance of v{version} is pending buyer-side authorization.")
        return a

    def _approval_valid(self, d, subject, version, obj_hash, approver="hotel_manager") -> bool:
        return any(a["deal_id"] == d["id"] and a["subject"] == subject and a["approver"] == approver
                   and a["version"] == version and a["hash"] == obj_hash and a["status"] == "approved"
                   for a in self.s["approvals"].values())

    def decide_approval(self, approval_id: str, decision: str, actor: str, note: str = "") -> dict:
        """Only the named approver can decide; the decision binds to the exact version/amount on file."""
        with self.lock:
            self._sweep()
            a = self.s["approvals"].get(approval_id)
            if not a:
                raise Refused("no such approval")
            if actor != a["approver"]:
                raise Refused(f"only the {a['approver'].replace('_', ' ')} can decide this")
            if a["status"] != "pending":
                raise Refused(f"approval is already {a['status']}")
            if decision not in ("approve", "reject"):
                raise Refused("decision must be approve or reject")
            d = self.deal(a["deal_id"])
            # A request whose object changed since it was raised is stale, whatever the decision.
            if a["version"] is not None:
                v = self._version(d, a["version"])
                # An acceptance can only be authorized while that offer is still the one on the table;
                # checked here, before anything changes, so a refusal leaves no half-made decision.
                lapsed_offer = a["subject"] == "acceptance" and (v["status"] != "sent" or v is not d["versions"][-1])
                if v["hash"] != a["hash"] or v["status"] in ("superseded",) or lapsed_offer:
                    a["status"] = "stale"
                    self.log(d["id"], "system", "approval_stale", f"Approval {a['id']} is stale: v{a['version']} changed.")
                    self._save()
                    raise Refused("this request is stale: the version it names has changed")
            a["status"] = "approved" if decision == "approve" else "rejected"
            a["decided_at"] = self.now()
            a["note"] = note
            who = "Hotel manager" if actor == "hotel_manager" else "Organizer"
            self.log(d["id"], actor, "approval_" + a["status"], f"{who} {a['status']} {a['subject']}"
                     + (f" v{a['version']}" if a["version"] else "") + (f" {gbp(a['amount'])}" if a["amount"] else "")
                     + (f": {note}" if note else ""))
            if a["subject"] == "offer":
                v = self._version(d, a["version"])
                v["status"] = "approved" if decision == "approve" else "rejected"
                d["status"] = "approved_to_send" if decision == "approve" else "revise"
            elif a["subject"] == "reduction":
                v = self._version(d, a["version"])
                v["status"] = "approved" if decision == "approve" else "rejected"
                if decision == "approve":
                    d["status"] = "amendment_approved"
                else:
                    d["status"] = "confirmed"
                    d["flags"].append(f"reduction v{a['version']} declined by manager; existing agreement stands")
            elif a["subject"] == "acceptance":
                if decision == "approve":
                    self._record_acceptance(d, self._version(d, a["version"]), "buyer (organizer-authorized)")
                else:
                    d["pending_acceptance"] = None
                    d["status"] = "offer_sent"
                    d["thread"].append({"id": self._id("msg"), "at": self.now(), "from": "buyer", "channel": "portal",
                                        "text": f"Our organizer did not authorize v{a['version']}."})
            elif a["subject"] == "refund":
                if decision == "reject":
                    d["flags"].append(f"refund {gbp(a['amount'])} not authorized: amount remains owed to the buyer; "
                                      "discrepancy escalated for manager review")
                    d["handoff"] = {"at": self.now(), "reason": "refund authorization withheld while the buyer is owed "
                                    f"{gbp(d['finance']['refund_owed'])}"}
            d["unread"] = True
            self._save()
        self._wake("approval")
        return {"id": a["id"], "status": a["status"]}

    # ================================================================ buyer interface
    def buyer_action(self, deal_id: str, token: str, action: str, text: str = "", payload: dict | None = None,
                     actor: str = "buyer") -> dict:
        """Requests, counteroffers, acceptance and change requests, from the authenticated buyer only."""
        payload = payload or {}
        with self.lock:
            self._sweep()
            d = self.deal(deal_id)
            if not secrets.compare_digest(token or "", d["buyer_token"]):
                raise Refused("not authenticated as this deal's buyer")
            msg = {"id": self._id("msg"), "at": self.now(), "from": "buyer", "channel": "portal", "text": text,
                   "action": action, "payload": payload}
            result: dict = {"recorded": True}
            if action == "accept":
                n = int(payload.get("version") or 0)
                v = self._version(d, n)
                result = self._buyer_accept(d, v)
            elif action == "change_request":
                if not d.get("booking") or d["booking"]["status"] not in ("confirmed",):
                    raise Refused("there is no confirmed booking to change")
            elif action not in ("reply", "counter", "decline", "message"):
                raise Refused(f"unknown action {action}")
            d["thread"].append(msg)
            d["unread"] = True
            if action == "decline":
                d["status"] = "lost"
            self.log(d["id"], actor, "buyer_" + action, (text or action)[:200], payload or None)
            self._save()
        self._wake("buyer")
        return result

    def _buyer_accept(self, d: dict, v: dict) -> dict:
        if v["status"] != "sent" or v is not d["versions"][-1]:
            raise Refused(f"v{v['n']} is not the current offer (status {v['status']})")
        if v["kind"] == "offer":
            hold = self.s["holds"].get(d.get("hold_id") or "")
            if not hold or hold["status"] != "active" or _dt(hold["expires_at"]) <= _dt(self.now()):
                raise Refused("this offer has expired; the hotel must re-check availability and renew it")
        mandate = self.scenario["buyers"].get(d.get("buyer_profile") or "")
        reasons = buyer_check(v["terms"], v["pricing"], mandate) if mandate and v["kind"] == "offer" else []
        if reasons and not self._approval_valid(d, "acceptance", v["n"], v["hash"], approver="organizer"):
            pending = next((a for a in self.s["approvals"].values() if a["deal_id"] == d["id"]
                            and a["subject"] == "acceptance" and a["version"] == v["n"] and a["status"] == "pending"), None)
            if not pending:
                pending = self._request_approval(
                    d, "organizer", "acceptance",
                    f"Buyer agent asks to accept {d['company']} v{v['n']} ({gbp(v['pricing']['total'])}, deposit "
                    f"{v['terms']['deposit_pct']}% = {gbp(v['pricing']['deposit'])}): " + "; ".join(reasons),
                    reasons, version=v["n"], obj_hash=v["hash"])
            d["pending_acceptance"] = {"version": v["n"], "hash": v["hash"], "approval_id": pending["id"]}
            d["status"] = "acceptance_pending_organizer"
            return {"accepted": False, "status": "awaiting organizer authorization", "approval_id": pending["id"],
                    "reasons": reasons}
        self._record_acceptance(d, v, "buyer")
        return {"accepted": True, "version": v["n"]}

    def _record_acceptance(self, d, v, who):
        if v["status"] != "sent" or v is not d["versions"][-1]:
            raise Refused(f"v{v['n']} is no longer the current offer")
        v["status"] = "accepted"
        v["accepted_at"] = self.now()
        d["pending_acceptance"] = None
        if v["kind"] == "offer":
            d["accepted_version"] = v["n"]
            d["status"] = "accepted"
            self.log(d["id"], "buyer", "accepted", f"{who} accepted v{v['n']} (hash {v['hash']}).")
        else:
            self._apply_amendment(d, v)

    # ================================================================ holds & inventory
    # State stores only what has been touched. Reads of an untouched bookable night/morning/day return a
    # fresh, unsaved entry; writes pass create=True so the entry is stored. Outside the calendar: None.
    def _inv(self, night: str, create: bool = False) -> dict | None:
        inv = self.s["inventory"].get(night)
        if inv is None and night in self.hotel["capacity"]:
            inv = {rt: {"capacity": cap, "held": 0, "booked": 0} for rt, cap in self.hotel["capacity"][night].items()}
            if create:
                self.s["inventory"][night] = inv
        return inv

    def _bf(self, morning: str, create: bool = False) -> dict | None:
        b = self.s["breakfast"].get(morning)
        if b is None and (calc.d(morning) - timedelta(days=1)).isoformat() in self.hotel["capacity"]:
            b = {"held": 0, "booked": 0}
            if create:
                self.s["breakfast"][morning] = b
        return b

    def _slot(self, day: str, create: bool = False) -> dict | None:
        slots = self.s["meeting"].setdefault("thames", {})
        slot = slots.get(day)
        if slot is None and day in self.hotel["meeting_rooms"]["thames"]["available"]:
            slot = {"status": "free", "deal_id": None}
            if create:
                slots[day] = slot
        return slot

    def _set_slot(self, day: str, status: str, deal_id: str | None):
        self.s["meeting"].setdefault("thames", {})[day] = {"status": status, "deal_id": deal_id}

    def inventory_at(self, night: str) -> dict | None:
        """Rooms on one night by type ({capacity, held, booked}); None outside the bookable calendar."""
        with self.lock:
            return copy.deepcopy(self._inv(night))

    def _available(self, night: str, rt: str) -> int:
        inv = self._inv(night)[rt]
        return inv["capacity"] - inv["held"] - inv["booked"]

    def _can_hold(self, terms: dict, d: dict) -> bool:
        own = self.s["holds"].get(d.get("hold_id") or "")
        own_active = own if own and own["status"] == "active" else None
        for night, n in terms["rooms"].items():
            if self._inv(night) is None:
                return False
            extra = own_active["rooms"].get(night, 0) if own_active and own_active["room_type"] == terms["room_type"] else 0
            if n > self._available(night, terms["room_type"]) + extra:
                return False
        cap = self.hotel["breakfast"]["group_capacity_per_day"]
        for m, n in terms["breakfast"].items():
            b = self._bf(m)
            extra = own_active["breakfast"].get(m, 0) if own_active else 0
            if b is None or b["held"] + b["booked"] - extra + n > cap:
                return False
        if terms.get("meeting"):
            slot = self._slot(terms["meeting"]["date"])
            if not slot or (slot["status"] != "free" and slot["deal_id"] != d["id"]):
                return False
        return True

    def _hold_covers(self, hold, terms) -> bool:
        return (hold["room_type"] == terms["room_type"] and hold["rooms"] == terms["rooms"]
                and hold["breakfast"] == terms["breakfast"]
                and (hold["meeting"] or None) == ((terms.get("meeting") or {}).get("date")))

    def _create_hold(self, d, terms) -> dict | None:
        if not self._can_hold(terms, d):
            return None
        hid = self._id("hold_")
        expires = _iso(_dt(self.now()) + timedelta(hours=self.policy["quote_validity_hours"]))
        rt = terms["room_type"]
        for night, n in terms["rooms"].items():
            self._inv(night, create=True)[rt]["held"] += n
        for m, n in terms["breakfast"].items():
            self._bf(m, create=True)["held"] += n
        mdate = (terms.get("meeting") or {}).get("date")
        if mdate:
            self._set_slot(mdate, "held", d["id"])
        hold = {"id": hid, "deal_id": d["id"], "room_type": rt, "rooms": dict(terms["rooms"]),
                "breakfast": dict(terms["breakfast"]), "meeting": mdate, "created_at": self.now(),
                "expires_at": expires, "status": "active"}
        self.s["holds"][hid] = hold
        d["hold_id"] = hid
        self.log(d["id"], "system", "hold", f"PMS hold {hid}: {sum(terms['rooms'].values())} {rt} room-nights"
                 + (f", Thames Room {mdate}" if mdate else "") + f"; expires {expires}.")
        return hold

    def _release_hold(self, hold, why: str):
        if hold["status"] != "active":
            return
        for night, n in hold["rooms"].items():
            self._inv(night, create=True)[hold["room_type"]]["held"] -= n
        for m, n in hold["breakfast"].items():
            self._bf(m, create=True)["held"] -= n
        if hold["meeting"]:
            self._set_slot(hold["meeting"], "free", None)
        hold["status"] = "released"
        self.log(hold["deal_id"], "system", "hold_released", f"Hold {hold['id']} released: {why}.")

    def _commit_booking(self, d, v) -> bool:
        """Convert the hold to a booking in one inventory operation. False if it can't be done."""
        if self.s["sim"].get("fail_next_commit"):
            self.s["sim"]["fail_next_commit"] = False
            return False
        terms = v["terms"]
        hold = self.s["holds"].get(d.get("hold_id") or "")
        rt = terms["room_type"]
        if hold and hold["status"] in ("active", "expired_payment_pending"):
            covered = self._hold_covers(hold, terms)
            if not covered:
                return False
            for night, n in hold["rooms"].items():
                inv = self._inv(night, create=True)[rt]
                inv["held"] -= n
                inv["booked"] += n
            for m, n in hold["breakfast"].items():
                b = self._bf(m, create=True)
                b["held"] -= n
                b["booked"] += n
            if hold["meeting"]:
                self._set_slot(hold["meeting"], "booked", d["id"])
            hold["status"] = "converted"
            return True
        # No live hold (released at expiry): book from free inventory only if every unit is still there.
        tmp = {**terms}
        d_no_hold = {**d, "hold_id": None}
        if not self._can_hold(tmp, d_no_hold):
            return False
        for night, n in terms["rooms"].items():
            self._inv(night, create=True)[rt]["booked"] += n
        for m, n in terms["breakfast"].items():
            self._bf(m, create=True)["booked"] += n
        if terms.get("meeting"):
            self._set_slot(terms["meeting"]["date"], "booked", d["id"])
        return True

    def _sweep(self):
        """Hold expiry. Reconcile any payment first; keep inventory held while an outcome is unknown."""
        now = _dt(self.now())
        for hold in list(self.s["holds"].values()):
            if hold["status"] != "active" or _dt(hold["expires_at"]) > now:
                continue
            d = self.s["deals"][hold["deal_id"]]
            op = self._deposit_op(d)
            if op and op["status"] not in ("failed", "cancelled"):
                self._verify_deposit(d, op, source="hold-expiry reconciliation")
                if hold["status"] != "active":
                    continue
                if op["status"] in ("unknown", "submitting") or op.get("provider_status") in (
                        "PENDING", "PENDING_REVIEW", "REQUIRES_CAPTURE", "REQUIRES_CUSTOMER_ACTION"):
                    hold["status"] = "expired_payment_pending"
                    d["flags"].append("hold expired while payment outcome unknown: inventory kept, manager review")
                    d["handoff"] = {"at": self.now(), "reason": "payment outcome unknown at hold expiry"}
                    self.log(d["id"], "system", "hold_expiry", "Hold expired during checkout with payment outcome "
                             "unknown: inventory kept provisionally held, flagged for manager review.")
                    continue
                if op.get("payment_intent_id") and op["status"] == "created":
                    try:
                        self.provider.cancel_intent(op["request_id"] + "-cancel", op["payment_intent_id"],
                                                    "hold expired")
                        op["status"] = "cancelled"
                    except Exception as e:
                        op["note"] = f"cancel at expiry failed: {e}"
            self._release_hold(hold, f"quote validity ({self.policy['quote_validity_hours']} scenario hours) ended")
            hold["status"] = "expired"
            for v in d["versions"]:
                if v["status"] in ("sent", "approved", "pending_approval") and v["kind"] == "offer":
                    v["status"] = "expired"
            if d["status"] not in ("confirmed", "lost", "handoff", "declined"):
                d["status"] = "expired"
                if not d.get("booking"):
                    d["accepted_version"] = None   # an unpaid acceptance lapses with its hold
            self.log(d["id"], "system", "expired", f"Offer and hold expired after {self.policy['quote_validity_hours']} scenario hours; checkout "
                     "disabled. A renewed offer needs a fresh availability check and new acceptance.")
            d["unread"] = True

    # ================================================================ deposit payment
    def _deposit_op(self, d):
        ops = [o for o in self.s["operations"].values() if o["deal_id"] == d["id"] and o["kind"] == "deposit"]
        return ops[-1] if ops else None

    def open_checkout(self, deal_id: str, actor: str = "seller") -> dict:
        with self.lock:
            self._sweep()
            d = self.deal(deal_id)
            if d.get("seller_paused"):
                raise Refused(f"paused after {d['seller_paused']['what']} failed: hotel staff must retry first")
            if not d.get("accepted_version"):
                raise Refused("no accepted agreement")
            v = self._version(d, d["accepted_version"])
            if v is not d["versions"][-1] or v["status"] != "accepted":
                raise Refused("the accepted version is not current")
            hold = self.s["holds"].get(d.get("hold_id") or "")
            if not hold or hold["status"] != "active" or _dt(hold["expires_at"]) <= _dt(self.now()):
                raise Refused("the hold has expired: run a fresh availability check, renew the offer and get acceptance")
            op = self._deposit_op(d)
            if op and op["status"] not in ("failed", "cancelled"):
                if op["version"] != v["n"]:
                    raise Refused("an earlier checkout exists for another version; reconcile it first")
                if op["status"] in ("submitting", "unknown"):
                    self._recover(op)
                return {"payment_url": self.pay_url(d), "operation": op["id"], "status": op["status"],
                        "amount": gbp(op["amount"]), "note": "existing checkout reused"}
            amount = v["pricing"]["deposit"]
            oid = self._id("op_")
            op = {"id": oid, "kind": "deposit", "deal_id": d["id"], "run_id": self.s["run_id"], "version": v["n"],
                  "version_hash": v["hash"], "amount": amount, "currency": "GBP",
                  "request_id": f"{self.s['run_id']}-{oid}", "merchant_order_id": f"{self.s['run_id']}-{d['ref']}-v{v['n']}",
                  "payment_intent_id": None, "status": "submitting", "created_at": self.now(), "provider_status": None}
            self.s["operations"][oid] = op
            self._save()   # persist the operation before calling the provider
            meta = {"run_id": self.s["run_id"], "deal": d["ref"], "agreement_version": str(v["n"]),
                    "agreement_hash": v["hash"], "operation_id": oid, "obligation": "signing deposit"}
            try:
                pi = self.provider.create_intent(op["request_id"], amount, "GBP", op["merchant_order_id"], meta,
                                                 return_url=self.pay_url(d, "return"),
                                                 connected_account_id=self.hotel_account)
                op.update(payment_intent_id=pi["id"], client_secret=pi.get("client_secret"), status="created",
                          provider_status=pi["status"])
            except AmbiguousResult as e:
                op["status"] = "unknown"
                op["note"] = str(e)
                self._recover(op)
            except AirwallexError as e:
                op["status"] = "failed"
                op["note"] = str(e)
                self._pause_seller(d, "opening checkout", e)
                self._save()
                raise Refused(f"provider refused the payment intent: {e}")
            d["status"] = "awaiting_payment"
            self.log(d["id"], actor, "checkout", f"Deposit due {gbp(amount)} for v{v['n']}; payment page opened "
                     f"(intent {op.get('payment_intent_id') or 'unknown'}).")
            self._save()
            return {"payment_url": self.pay_url(d), "operation": oid, "status": op["status"], "amount": gbp(amount)}

    def pay_url(self, d, suffix: str = "") -> str:
        return f"{self.public_base}/pay/{d['ref']}{'/' + suffix if suffix else ''}?t={d['buyer_token']}"

    def _recover(self, op):
        """An op whose creation outcome is unknown: look it up by our own references, never resubmit."""
        try:
            if op["kind"] == "deposit":
                found = [i for i in self.provider.find_intents(op["merchant_order_id"])
                         if i.get("request_id") in (None, op["request_id"])
                         and (i.get("metadata") or {}).get("operation_id") in (None, op["id"])]
                if found:
                    op.update(payment_intent_id=found[0]["id"], client_secret=found[0].get("client_secret"),
                              status="created", provider_status=found[0]["status"])
                    self.log(op["deal_id"], "system", "recovered", f"Recovered intent {found[0]['id']} for {op['id']} "
                             "from provider records; no new intent created.")
            else:
                found = [r for r in self.provider.find_refunds(op["payment_intent_id"])
                         if r.get("request_id") == op["request_id"]
                         or (r.get("metadata") or {}).get("operation_id") == op["id"]]
                if found:
                    op.update(provider_id=found[0]["id"], status="created")
                    self._apply_refund_status(op, found[0])
                    self.log(op["deal_id"], "system", "recovered", f"Recovered refund {found[0]['id']} for {op['id']} "
                             "from provider records; no new refund created.")
        except Exception as e:
            op["note"] = f"recovery lookup failed: {e}; left unresolved for review"

    def check_payment(self, deal_id: str, source: str = "seller") -> dict:
        with self.lock:
            d = self.deal(deal_id)
            op = self._deposit_op(d)
            if not op:
                raise Refused("no checkout opened")
            before = (d["status"], op["status"])
            self._verify_deposit(d, op, source)
            self._save()
            changed = (d["status"], op["status"]) != before
            out = {"operation": op["id"], "status": op["status"], "provider_status": op.get("provider_status"),
                   "booking": d.get("booking"), "deal_status": d["status"]}
        if changed:
            self._wake("payment")   # a confirmed booking (or an exception) is new input for the seller
        return out

    def _verify_deposit(self, d, op, source: str):
        if op["status"] in ("submitting", "unknown"):
            self._recover(op)
            if op["status"] in ("submitting", "unknown"):
                return
        if op["status"] in ("failed", "cancelled") or not op.get("payment_intent_id"):
            return
        if op["status"] == "succeeded" and d.get("booking"):
            return
        pi = self.provider.get_intent(op["payment_intent_id"])
        op["provider_status"] = pi["status"]
        if pi["status"] != "SUCCEEDED":
            return
        problems = []
        if pi["amount"] != op["amount"] or (pi["currency"] or "").upper() != op["currency"]:
            problems.append(f"amount/currency {pi['amount']} {pi['currency']} != obligation {op['amount']} {op['currency']}")
        if pi.get("merchant_order_id") and pi["merchant_order_id"] != op["merchant_order_id"]:
            problems.append("merchant_order_id mismatch")
        if problems:
            d["flags"].append("payment mismatch: " + "; ".join(problems))
            self.log(d["id"], "system", "payment_mismatch", "; ".join(problems))
            return
        op["status"] = "succeeded"
        new = self._ledger(f"receipt:{pi['id']}", d, "deposit_received", op["amount"], op, pi["id"])
        self.fx_journal.process({'id': 'card:' + pi['id'], 'account': self.hotel_account or 'platform',
                                 'deal_id': self.s['run_id'] + ':' + d['id'], 'currency': op['currency'],
                                 'amount': str(Decimal(op['amount']) / 100),
                                 'reference': op['merchant_order_id'], 'verified': True}, self.fx_provider)
        if not new and d.get("booking"):
            return
        v = self._version(d, op["version"])
        if d.get("booking"):
            return
        if self._commit_booking(d, v):
            d["booking"] = {"status": "confirmed", "confirmed_at": self.now(), "version": v["n"],
                            "room_type": v["terms"]["room_type"], "rooms": dict(v["terms"]["rooms"]),
                            "payment_intent_id": pi["id"], "deposit_op": op["id"], "applied_amendments": []}
            d["status"] = "confirmed"
            self.log(d["id"], "system", "confirmed", f"Payment {pi['id']} verified with the provider ({source}); "
                     f"{gbp(op['amount'])} received; hold converted; booking confirmed.")
            self._settle_payouts(d)
        else:
            d["booking"] = {"status": "exception", "confirmed_at": None, "version": v["n"],
                            "room_type": v["terms"]["room_type"], "rooms": dict(v["terms"]["rooms"]),
                            "payment_intent_id": pi["id"], "deposit_op": op["id"], "applied_amendments": []}
            d["status"] = "payment_received_booking_exception"
            d["handoff"] = {"at": self.now(), "reason": "payment received but inventory could not be committed"}
            self.log(d["id"], "system", "booking_exception", "Payment received - booking exception: inventory could "
                     "not be committed. Receipt kept; routed to the hotel manager. No second collection.")
        d["unread"] = True

    def _ledger(self, key: str, d, typ: str, amount: int, op, provider_id) -> bool:
        if any(e["key"] == key for e in self.s["ledger"]):
            self.log(d["id"], "system", "ledger_dedupe", f"Ledger already has {key}; not recorded twice.")
            return False
        self.s["ledger"].append({"id": self._id("le"), "key": key, "deal_id": d["id"], "type": typ, "amount": amount,
                                 "currency": "GBP", "operation": op["id"], "provider_id": provider_id,
                                 "version": op.get("version"), "at": self.now()})
        return True

    # ================================================================ amendments
    def prepare_reduction(self, deal_id: str, rooms_per_night: int | None = None, release: dict | None = None,
                          message: str = "", actor: str = "seller") -> dict:
        with self.lock:
            d = self.deal(deal_id)
            if not d.get("booking") or d["booking"]["status"] != "confirmed":
                raise Refused("no confirmed booking")
            base = self._version(d, d["accepted_version"])
            if release is None:
                release = {n: int(rooms_per_night or 0) for n in base["terms"]["rooms"]}
            release = {k: int(v) for k, v in release.items() if int(v)}
            if not release:
                raise Refused("nothing to release")
            fin = self.finance(d)
            # Measure against what the hotel keeps: receipts net of refunds made AND refunds already owed
            # (pending or not yet started), so a second reduction never refunds the first one again.
            try:
                ev = calc.evaluate_reduction(base["terms"], release, self.now(), self.hotel, self.policy, self.rms,
                                             fin["net_received"] - fin["refund_owed"])
            except ValueError as err:   # e.g. more rooms than are booked, or a night not in the booking
                raise Refused(f"this change cannot be applied to the booking: {err}") from None
            for v in d["versions"]:
                if v["kind"] == "amendment" and v["status"] in ("draft", "pending_approval", "approved", "sent"):
                    v["status"] = "superseded"
            n = len(d["versions"]) + 1
            terms = ev.pop("new_terms")
            pricing = ev.pop("new_pricing")
            v = {"n": n, "kind": "amendment", "base_version": base["n"], "terms": terms, "pricing": pricing,
                 "hotel_check": {"hard": [], "approvals": ev["review_reasons"], "ok": True},
                 "hash": _hash({"kind": "amendment", "terms": terms, "base": base["hash"]}),
                 "status": "draft", "created_at": self.now(), "sent_at": None, "reduction": ev,
                 "message": message or self._reduction_message(d, ev, pricing), "explanation": "", "unmet": [],
                 "release": release}
            d["versions"].append(v)
            self.log(d["id"], actor, "amendment_drafted",
                     f"Reduction v{n}: release {ev['requested_room_nights']} room-nights (allowance {ev['allowance']}, "
                     f"already used {ev['already_released']}); new total {gbp(pricing['total'])}, revised deposit "
                     f"{gbp(ev['revised_deposit'])}, refund {gbp(ev['refund_due'])} ({ev['refund_mode']}).")
            if ev["needs_manager_review"]:
                v["status"] = "pending_approval"
                d["status"] = "amendment_review"
                a = self._request_approval(d, "hotel_manager", "reduction",
                                           f"{d['company']} reduction v{n} needs review: " + "; ".join(ev["review_reasons"])
                                           + ". The existing agreement remains in effect until a change is accepted.",
                                           ev["review_reasons"], version=n, obj_hash=v["hash"])
                self._save()
                return {"version": n, "status": "pending_approval", "approval_id": a["id"], "evaluation": ev}
            res = self._send_version(d, v, actor)
            self._save()
            return {**res, "evaluation": ev}

    def send_amendment(self, deal_id: str, version: int, actor: str = "seller") -> dict:
        with self.lock:
            d = self.deal(deal_id)
            v = self._version(d, version)
            if v["kind"] != "amendment" or v is not d["versions"][-1]:
                raise Refused("not the current amendment")
            if v["status"] != "approved" or not self._approval_valid(d, "reduction", v["n"], v["hash"]):
                raise Refused("this amendment needs a valid manager review approval first")
            res = self._send_version(d, v, actor)
            self._save()
            return res

    def _reduction_message(self, d, ev, pricing) -> str:
        return (f"We can release {ev['requested_room_nights']} room-nights under your attrition allowance "
                f"({ev['remaining_after']} room-night(s) remain for later release without penalty). Breakfast and "
                f"meeting charges stay guaranteed. Revised total {gbp(pricing['total'])}; revised deposit "
                f"{gbp(ev['revised_deposit'])}" + (f"; we will refund {gbp(ev['refund_due'])} to your original card "
                                                    "once you accept." if ev["refund_due"] else "."))

    @staticmethod
    def _manual_amendment_flag(n: int, ev: dict) -> str:
        """Why an accepted reduction got no automatic refund, from calc.evaluate_reduction's own result."""
        reasons = list(ev.get("review_reasons") or [])            # over the allowance, after the release deadline
        if not ev.get("before_first_installment", True):
            reasons.append("after the first installment date")
        if not reasons:
            reasons.append("outside the automatic refund terms")
        return (f"amendment v{n} accepted with no automatic refund ({'; '.join(reasons)}): payments to be "
                "reconciled manually by the manager")

    def _apply_amendment(self, d, v):
        """Accepted reduction: update booking, release inventory exactly once, record refund owed."""
        b = d["booking"]
        if v["n"] in b["applied_amendments"]:
            return
        rt = b["room_type"]
        for night, n in v["release"].items():
            inv = self._inv(night, create=True)[rt]
            if inv["booked"] < n:
                raise Refused("inventory inconsistency: cannot release more than booked")
            inv["booked"] -= n
            b["rooms"][night] -= n
        b["applied_amendments"].append(v["n"])
        prev = d["accepted_version"]
        v["status"] = "accepted"
        v["accepted_at"] = self.now()
        self._version(d, prev)["status"] = "superseded_by_amendment"
        d["accepted_version"] = v["n"]
        ev = v["reduction"]
        if ev["refund_due"]:
            d["finance"]["refund_owed"] += ev["refund_due"]
            d["finance"]["refund_owed_version"] = v["n"]
            d["status"] = "refund_due"
        elif ev["refund_mode"] != "automatic":
            d["status"] = "confirmed"
            d["flags"].append(self._manual_amendment_flag(v["n"], ev))
        else:
            d["status"] = "confirmed"
        self.log(d["id"], "buyer", "amendment_accepted",
                 f"Amendment v{v['n']} accepted: {sum(v['release'].values())} {rt} room-nights released to inventory "
                 f"once; booking now {sorted(set(b['rooms'].values()))} {rt}s per night"
                 + (f"; refund owed {gbp(ev['refund_due'])}" if ev["refund_due"] else "") + ".")

    # ================================================================ refunds
    def _refund_ops(self, d):
        return [o for o in self.s["operations"].values() if o["deal_id"] == d["id"] and o["kind"] == "refund"]

    def refund_excess(self, deal_id: str, justification: str = "", actor: str = "seller") -> dict:
        """Refund what the buyer is owed after an accepted reduction, within authority and refundable funds."""
        with self.lock:
            d = self.deal(deal_id)
            if d.get("seller_paused"):
                raise Refused(f"paused after {d['seller_paused']['what']} failed: hotel staff must retry first")
            owed = d["finance"]["refund_owed"]
            if owed <= 0:
                raise Refused("no refund is owed")
            for o in self._refund_ops(d):
                # Anything not yet completed or failed is unresolved, including a refund stored as created
                # whose status was never applied (e.g. the process stopped right after the provider call).
                if o["status"] in ("submitting", "unknown", "created", "pending") or o.get("provider_status") == "RECEIVED":
                    raise Refused(f"refund {o.get('provider_id') or o['id']} is unresolved "
                                  f"({o.get('provider_status') or o['status']}): check it with check_refund before "
                                  "any new refund")
            dep = self._deposit_op(d)
            if not dep or dep["status"] != "succeeded":
                raise Refused("no verified payment to refund against")
            fin = self.finance(d)
            refundable = fin["received"] - fin["refunded"] - fin["refund_pending"]
            if owed > refundable:
                raise Refused(f"owed {gbp(owed)} exceeds remaining refundable {gbp(refundable)}; manager reconciliation")
            limit = self.policy["seller_refund_limit"]
            cumulative = fin["refunded"] + fin["refund_pending"] + owed
            ref_obj = {"amount": owed, "currency": "GBP", "payment_intent_id": dep["payment_intent_id"],
                       "amendment_version": d["finance"]["refund_owed_version"], "deal": d["ref"]}
            ref_hash = _hash(ref_obj)
            if cumulative > limit:
                ok = any(a["deal_id"] == d["id"] and a["subject"] == "refund" and a["hash"] == ref_hash
                         and a["status"] == "approved" and not a.get("used") for a in self.s["approvals"].values())
                if not ok:
                    rejected = any(a["deal_id"] == d["id"] and a["subject"] == "refund" and a["hash"] == ref_hash
                                   and a["status"] == "rejected" for a in self.s["approvals"].values())
                    if rejected:
                        raise Refused("the manager withheld authorization for this refund; the amount stays owed and "
                                      "the discrepancy is escalated for review")
                    pending = next((a for a in self.s["approvals"].values() if a["deal_id"] == d["id"]
                                    and a["subject"] == "refund" and a["hash"] == ref_hash and a["status"] == "pending"), None)
                    if not pending:
                        pending = self._request_approval(
                            d, "hotel_manager", "refund",
                            f"Refund {gbp(owed)} GBP to the original card (intent {dep['payment_intent_id']}) for "
                            f"{d['company']} amendment v{ref_obj['amendment_version']}: cumulative refunds "
                            f"{gbp(cumulative)} exceed the seller's {gbp(limit)} limit. Contractually owed under the "
                            "accepted reduction." + (f" {justification}" if justification else ""),
                            [f"refund above the seller's {gbp(limit)} authority"], obj_hash=ref_hash, amount=owed,
                            extra={"refund": ref_obj})
                        self._save()
                    d["status"] = "refund_awaiting_approval"
                    return {"status": "pending_approval", "approval_id": pending["id"],
                            "note": "Paused: hotel manager must authorize this exact refund."}
            for a in self.s["approvals"].values():
                if a["deal_id"] == d["id"] and a["subject"] == "refund" and a["hash"] == ref_hash and a["status"] == "approved":
                    a["used"] = True
            oid = self._id("op_")
            op = {"id": oid, "kind": "refund", "deal_id": d["id"], "run_id": self.s["run_id"],
                  "version": ref_obj["amendment_version"], "amount": owed, "currency": "GBP",
                  "payment_intent_id": dep["payment_intent_id"], "request_id": f"{self.s['run_id']}-{oid}",
                  "provider_id": None, "status": "submitting", "provider_status": None, "created_at": self.now(),
                  "ref_hash": ref_hash}
            self.s["operations"][oid] = op
            self._save()   # persisted before the provider call
            meta = {"run_id": self.s["run_id"], "deal": d["ref"], "agreement_version": str(op["version"]),
                    "operation_id": oid}
            try:
                r = self.provider.create_refund(op["request_id"], dep["payment_intent_id"], owed,
                                                "Group block reduction under attrition allowance", meta)
                op.update(provider_id=r["id"], status="created")
                self._save()   # the refund id is stored before anything else happens
                self._apply_refund_status(op, r, notify=False)
            except AmbiguousResult as e:
                op["status"] = "unknown"
                op["note"] = str(e)
                self._recover(op)
            except AirwallexError as e:
                op["status"] = "failed"
                op["note"] = str(e)
                self.log(d["id"], "system", "refund_failed", f"Provider rejected refund: {e}. Amount stays owed.")
                self._pause_seller(d, "the refund", e)
                self._save()
                raise Refused(f"provider rejected the refund: {e}")
            d["status"] = "refund_pending"
            self.log(d["id"], actor, "refund_initiated", f"Refund {gbp(owed)} initiated to the original payment "
                     f"({op.get('provider_id') or 'id unknown'}), status {op.get('provider_status') or op['status']}.")
            if op.get("provider_id"):
                self._queue_notification(op)
            self._save()
            return {"operation": oid, "refund_id": op.get("provider_id"), "status": op.get("provider_status") or op["status"],
                    "note": "Completion notification not yet received; verify with check_refund."}

    def _queue_notification(self, op):
        """The provider's completion notice. In the demo it is held back so the agent sees an unresolved refund."""
        evt = {"id": f"evt_{op['provider_id']}_done", "name": "refund.accepted", "held": True,
               "data": {"object": {"id": op["provider_id"], "status": "ACCEPTED", "amount": op["amount"],
                                   "metadata": {"run_id": self.s["run_id"], "operation_id": op["id"]}}}}
        if self.s["sim"].get("delay_refund_notifications"):
            self.s["delayed_notifications"].append(evt)

    def release_notifications(self) -> dict:
        """Presenter: deliver held provider notifications now (they are deduplicated like any webhook)."""
        with self.lock:
            evts, self.s["delayed_notifications"] = self.s["delayed_notifications"], []
            out = [self.handle_event(e, verified=True) for e in evts]
            self._save()
            return {"delivered": len(evts), "results": out}

    def check_refund(self, deal_id: str, actor: str = "seller") -> dict:
        with self.lock:
            d = self.deal(deal_id)
            ops = self._refund_ops(d)
            if not ops:
                raise Refused("no refund on file")
            results = []
            for op in ops:
                if op["status"] in ("submitting", "unknown"):
                    self._recover(op)
                if op.get("provider_id") and op.get("provider_status") not in ("SETTLED", "FAILED"):
                    r = self.provider.get_refund(op["provider_id"])
                    self._apply_refund_status(op, r)
                results.append({"operation": op["id"], "refund_id": op.get("provider_id"), "amount": gbp(op["amount"]),
                                "provider_status": op.get("provider_status"), "status": op["status"]})
            self.log(d["id"], actor, "refund_checked", "Checked refund status with the provider: "
                     + ", ".join(f"{r['refund_id']} {r['provider_status']}" for r in results))
            self._save()
            return {"refunds": results, "finance": self.finance(d)}

    def _apply_refund_status(self, op, r: dict, notify: bool = True):
        """Monotonic: an older status can't overwrite a newer one, and the ledger is credited once."""
        new = r.get("status")
        old = op.get("provider_status")
        if old and REFUND_RANK.get(new, 0) <= REFUND_RANK.get(old, 0):
            return   # never move backwards, and a repeat changes nothing
        if r.get("amount") is not None and r["amount"] != op["amount"]:
            self.log(op["deal_id"], "system", "refund_mismatch", f"Refund {r.get('id')} amount {r['amount']} != {op['amount']}")
            return
        op["provider_status"] = new
        d = self.s["deals"][op["deal_id"]]
        if new in ("ACCEPTED", "SETTLED"):
            op["status"] = "completed"
            if self._ledger(f"refund:{op['provider_id']}", d, "refund_completed", -op["amount"], op, op["provider_id"]):
                d["finance"]["refund_owed"] = max(0, d["finance"]["refund_owed"] - op["amount"])
                if d["status"] in ("refund_pending", "refund_due"):
                    d["status"] = "confirmed"
                self.log(d["id"], "system", "refund_completed",
                         f"Refund {op['provider_id']} {new} at the provider; ledger updated once. (Provider status does "
                         "not prove the buyer's bank has posted the money.)")
                self._settle_payouts(d)
        elif new == "FAILED":
            op["status"] = "failed"
            d["status"] = "refund_failed"
            d["handoff"] = {"at": self.now(), "reason": f"refund {op['provider_id']} failed; {gbp(op['amount'])} still owed"}
            d["flags"].append(f"refund {op['provider_id']} failed: {gbp(op['amount'])} still owed, follow up")
            self.log(d["id"], "system", "refund_failed", f"Refund {op['provider_id']} FAILED; amount stays owed.")
        elif new == "RECEIVED":
            op["status"] = "pending"

    # ================================================================ hotel payouts (Payments for Platforms)
    # The platform collects the deposit for the hotel ("collect payments directly", intent tagged with the
    # hotel's connected account). Once the deposit is verified and booked, a funds split pays the hotel its
    # share and the commission stays with the platform. A refund is paid from the platform; a funds split
    # reversal then takes the hotel's share of it back. Payouts are backend bookkeeping: no agent tool
    # moves them, and the buyer's view never shows them.
    @property
    def hotel_account(self) -> str | None:
        """The hotel's Airwallex connected account (Payments for Platforms): deposits are collected by the
        platform for it, and its share is paid out by funds split. GS_HOTEL_ACCOUNT wins; otherwise the
        account onboarded from the dashboard, once it accepts cards (an intent tagged with an account that
        doesn't would fail); otherwise the simulated provider's stand-in. None = no payouts."""
        env = os.environ.get("GS_HOTEL_ACCOUNT")
        if env:
            return env
        rec = self.account_store.read() or {}
        if rec.get("status") == "ready" and rec.get("account"):
            return rec["account"]
        return getattr(self.provider, "default_account", None)

    def hotel_account_view(self, check: bool = False) -> dict:
        """What the dashboard shows about the hotel's connected account; check=True asks the provider."""
        rec = dict(self.account_store.read() or {})
        out = {"account": self.hotel_account, "source": "GS_HOTEL_ACCOUNT" if os.environ.get("GS_HOTEL_ACCOUNT")
               else "onboarded" if rec.get("status") == "ready" and rec.get("account")
               else "simulated" if self.hotel_account else None,
               "onboarding": rec or None, "running": bool(self._onboarding and self._onboarding.is_alive()),
               "provider": self.provider.name, "platform": self.platform.get("name")}
        if check and self.hotel_account:
            try:
                out["provider_record"] = self.provider.account_status(self.hotel_account)
            except Exception as e:
                out["provider_error"] = str(e)
        return out

    def onboard_hotel_account(self, actor: str = "hotel staff") -> dict:
        """Create (or resume) the hotel's connected account under the platform, in the background: the
        sandbox takes a minute or two to activate it and enable card payments. Progress is saved after
        every step, so a retry resumes the same account instead of opening a second one."""
        with self.lock:
            if os.environ.get("GS_HOTEL_ACCOUNT"):
                raise Refused("GS_HOTEL_ACCOUNT is set, so that account is used; unset it to onboard from here")
            if self._onboarding and self._onboarding.is_alive():
                raise Refused("onboarding is already running")
            rec = self.account_store.read() or {}
            if rec.get("status") == "ready":
                raise Refused(f"the hotel's account {rec['account']} is already onboarded")
            rec = {**rec, "status": "starting", "error": None, "started_at": _iso(datetime.now(timezone.utc)),
                   "request_id": rec.get("request_id") or str(uuid.uuid4()), "steps": rec.get("steps") or []}
            self.account_store.write(rec)
            self.log(None, actor, "account_onboarding", "Started onboarding the hotel's Airwallex connected account.")
            self._save()
            self._onboarding = threading.Thread(target=self._run_onboarding, args=(rec["request_id"], rec.get("account")),
                                                daemon=True)
            self._onboarding.start()
        return self.hotel_account_view()

    def _onboarding_step(self, status: str, info: dict):
        with self.lock:
            rec = self.account_store.read() or {}
            rec["steps"] = (rec.get("steps") or []) + [{"at": _iso(datetime.now(timezone.utc)), "status": status}]
            rec["status"] = status
            if info.get("connected_account"):
                rec["account"] = info["connected_account"]
            self.account_store.write(rec)

    def _run_onboarding(self, request_id: str, account: str | None):
        h = self.hotel
        try:
            ok = self.provider.onboard_account(h["name"], h.get("email") or "groups@harbourhouse.example",
                                               request_id, account, self._onboarding_step)
            final, error = ("ready", None) if ok else ("capabilities_pending",
                                                       "card payments not enabled yet; retry in a minute")
        except Exception as e:
            final, error = "failed", str(e)
        with self.lock:
            rec = self.account_store.read() or {}
            rec.update(status=final, error=error, finished_at=_iso(datetime.now(timezone.utc)))
            self.account_store.write(rec)
            self.log(None, "system", "account_onboarding",
                     f"Hotel connected account {rec.get('account') or '(not created)'}: "
                     + ("ready, accepts card payments; hotel payouts will use it." if final == "ready" else error))
            self._save()

    @property
    def platform(self) -> dict:
        return self.scenario.get("platform") or {"name": "platform", "commission_bps": 0}

    def commission(self, amount: int) -> int:
        """The platform's commission on an amount in pence, rounded half up."""
        return (amount * int(self.platform.get("commission_bps", 0)) + 5000) // 10000

    def _payout_ops(self, d, kind: str):
        return [o for o in self.s["operations"].values() if o["deal_id"] == d["id"] and o["kind"] == kind]

    def _settle_payouts(self, d):
        """Idempotent reconciler: the hotel's share of the verified deposit paid out once, and its share of
        each completed refund taken back once. Safe to call again after any change or failure."""
        b = d.get("booking")
        if not b or b.get("status") != "confirmed":
            return
        dep = self.s["operations"].get(b.get("deposit_op") or "")
        if not dep or dep["status"] != "succeeded":
            return
        if not self.hotel_account:
            if not any(f.startswith("no hotel connected account") for f in d["flags"]):
                d["flags"].append("no hotel connected account configured (GS_HOTEL_ACCOUNT): deposit held by the platform, "
                                  "hotel payout pending")
                self.log(d["id"], "system", "payout_pending", "No hotel connected account is configured, so the "
                         "deposit stays with the platform until one is set and the payout is retried.")
            return
        payouts = self._payout_ops(d, "payout")
        live = [o for o in payouts if o["status"] not in ("failed", "superseded")]
        if not live:
            if any(o["status"] == "failed" for o in payouts):
                return   # a refused payout waits for a person to retry it
            live = [self._create_payout(d, dep)]
        payout = live[-1]
        if payout["status"] in ("submitting", "unknown"):
            self._recover_payout(payout)
        if payout["status"] != "paid":
            return
        for r in self._refund_ops(d):
            if r["status"] != "completed":
                continue
            revs = [o for o in self._payout_ops(d, "payout_reversal")
                    if o.get("refund_op") == r["id"] and o["status"] != "superseded"]
            if any(o["status"] == "failed" for o in revs):
                continue   # refused: waits for a person to retry
            rev = revs[-1] if revs else self._create_reversal(d, payout, r)
            if rev and rev["status"] in ("submitting", "unknown"):
                self._submit_reversal(d, rev)

    def _create_payout(self, d, dep) -> dict:
        gross = dep["amount"]
        fee = self.commission(gross)
        oid = self._id("op_")
        op = {"id": oid, "kind": "payout", "deal_id": d["id"], "run_id": self.s["run_id"], "gross": gross,
              "commission": fee, "amount": gross - fee, "currency": "GBP", "request_id": f"{self.s['run_id']}-{oid}",
              "payment_intent_id": dep["payment_intent_id"], "destination": self.hotel_account, "provider_id": None,
              "status": "submitting", "created_at": self.now(), "provider_status": None}
        self.s["operations"][oid] = op
        self._save()   # persist the operation before calling the provider
        meta = {"run_id": self.s["run_id"], "deal": d["ref"], "operation_id": oid,
                "obligation": "hotel share of signing deposit", "commission": calc.gbp(fee)}
        try:
            sp = self.provider.create_funds_split(op["request_id"], op["payment_intent_id"], op["amount"],
                                                  op["destination"], meta)
            self._apply_payout(d, op, sp)
        except AmbiguousResult as e:
            op["status"] = "unknown"
            op["note"] = str(e)
            self._recover_payout(op)
        except AirwallexError as e:
            op["status"] = "failed"
            op["error"] = str(e)
            d["flags"].append(f"hotel payout refused by the provider: {e}")
            self.log(d["id"], "system", "payout_failed", f"Funds split to the hotel refused: {e}. The booking stands; "
                     "the deposit stays with the platform until the payout is retried.")
        return op

    def _apply_payout(self, d, op, sp: dict):
        op.update(provider_id=sp["id"], provider_status=sp.get("status"), status="paid")
        if self._payout_ledger(f"payout:{sp['id']}", d, "hotel_payout", op["amount"], op, sp["id"]):
            self.log(d["id"], "system", "payout_paid",
                     f"Paid {gbp(op['amount'])} to the hotel's account {op['destination']} by funds split {sp['id']}; "
                     f"{gbp(op['commission'])} platform commission kept.")

    def _recover_payout(self, op):
        """Outcome unknown: look the split up by our own references, never send a second one."""
        d = self.s["deals"][op["deal_id"]]
        try:
            found = [x for x in self.provider.find_funds_splits(op["payment_intent_id"])
                     if x.get("request_id") == op["request_id"]
                     or (x.get("metadata") or {}).get("operation_id") == op["id"]]
        except Exception as e:
            op["note"] = f"recovery lookup failed: {e}; left unresolved"
            return
        if found:
            self._apply_payout(d, op, found[0])
            self.log(d["id"], "system", "recovered", f"Recovered funds split {found[0]['id']} for {op['id']} from "
                     "provider records; no second split sent.")
        else:
            op["status"] = "unknown"
            if not any(f.startswith(f"hotel payout {op['id']}") for f in d["flags"]):
                d["flags"].append(f"hotel payout {op['id']} outcome unknown: check the provider before retrying")

    def _create_reversal(self, d, payout, refund_op) -> dict:
        fee_back = self.commission(refund_op["amount"])
        taken = sum(o["amount"] for o in self._payout_ops(d, "payout_reversal") if o["status"] not in ("failed", "superseded"))
        claw = min(refund_op["amount"] - fee_back, payout["amount"] - taken)
        oid = self._id("op_")
        op = {"id": oid, "kind": "payout_reversal", "deal_id": d["id"], "run_id": self.s["run_id"],
              "refund_op": refund_op["id"], "refund": refund_op["amount"], "commission_returned": fee_back,
              "amount": claw, "currency": "GBP", "request_id": f"{self.s['run_id']}-{oid}",
              "funds_split_id": payout["provider_id"], "provider_id": None, "status": "submitting",
              "created_at": self.now(), "provider_status": None}
        self.s["operations"][oid] = op
        self._save()
        return op

    def _submit_reversal(self, d, op):
        """Send (or, after a timeout, resend with the same request_id) the reversal. Airwallex refuses a reused
        request_id, so a resend can never take the money back twice."""
        meta = {"run_id": self.s["run_id"], "deal": d["ref"], "operation_id": op["id"],
                "reason": "hotel share of a refund to the buyer"}
        try:
            r = self.provider.reverse_funds_split(op["request_id"], op["funds_split_id"], op["amount"], meta)
            op.update(provider_id=r.get("id"), provider_status=r.get("status"), status="reversed")
        except AmbiguousResult as e:
            op["status"] = "unknown"
            op["note"] = str(e)
            return
        except AirwallexError as e:
            if e.code == "duplicate_request_id":
                op.update(status="reversed", note="already created by an earlier attempt (request_id reused)")
            else:
                op["status"] = "failed"
                op["error"] = str(e)
                d["flags"].append(f"clawback of the hotel's share of a refund refused: {e}")
                self.log(d["id"], "system", "payout_reversal_failed", f"Funds split reversal refused: {e}. The buyer's "
                         "refund stands; the platform has paid it until the reversal is retried.")
                return
        if self._payout_ledger(f"reversal:{op['refund_op']}", d, "payout_reversal", -op["amount"], op,
                               op.get("provider_id")):
            self.log(d["id"], "system", "payout_reversed",
                     f"Took {gbp(op['amount'])} back from the hotel's account for the {gbp(op['refund'])} refund; "
                     f"{gbp(op['commission_returned'])} of commission returned with it.")

    def _payout_ledger(self, key: str, d, typ: str, amount: int, op, provider_id) -> bool:
        """Platform-to-hotel money, kept apart from the buyer ledger (which is buyer-to-hotel money)."""
        book = self.s.setdefault("payout_ledger", [])
        if any(e["key"] == key for e in book):
            return False
        book.append({"id": self._id("pl"), "key": key, "deal_id": d["id"], "type": typ, "amount": amount,
                     "currency": "GBP", "operation": op["id"], "provider_id": provider_id, "at": self.now()})
        return True

    def retry_payout(self, deal_id: str, actor: str = "hotel staff") -> dict:
        """A person retries a refused or unknown payout or clawback once the cause is fixed."""
        with self.lock:
            d = self.deal(deal_id)
            for o in self._payout_ops(d, "payout") + self._payout_ops(d, "payout_reversal"):
                if o["status"] == "failed":
                    o["status"] = "superseded"
            d["flags"] = [f for f in d["flags"] if not f.startswith(("hotel payout", "clawback of the hotel",
                                                                     "no hotel connected account"))]
            self.log(d["id"], actor, "payout_retry", f"{cap_first(actor)} retried the hotel payout.")
            self._settle_payouts(d)
            self._save()
            return self.payout_view(d)

    def payout_view(self, d) -> dict:
        pays = [o for o in self._payout_ops(d, "payout") if o["status"] != "superseded"]
        revs = [o for o in self._payout_ops(d, "payout_reversal") if o["status"] != "superseded"]
        paid = sum(o["amount"] for o in pays if o["status"] == "paid")
        fee = sum(o["commission"] for o in pays if o["status"] == "paid")
        clawed = sum(o["amount"] for o in revs if o["status"] == "reversed")
        fee_back = sum(o["commission_returned"] for o in revs if o["status"] == "reversed")
        states = [o["status"] for o in pays + revs]
        if not d.get("booking") or d["booking"].get("status") != "confirmed":
            status = "not_due"
        elif not self.hotel_account:
            status = "no_account"
        elif "failed" in states:
            status = "failed"
        elif "unknown" in states or "submitting" in states:
            status = "unknown"
        elif paid:
            status = "paid"
        else:
            status = "pending"
        return {"platform": self.platform.get("name"), "commission_pct": int(self.platform.get("commission_bps", 0)) / 100,
                "connected_account": self.hotel_account, "status": status, "hotel_paid": paid,
                "hotel_clawed_back": clawed, "hotel_net": paid - clawed, "platform_fee": fee - fee_back,
                "operations": [{k: o.get(k) for k in ("id", "kind", "status", "amount", "commission",
                                                      "commission_returned", "provider_id", "created_at", "error")}
                               for o in pays + revs]}

    # ================================================================ webhooks
    def handle_event(self, event: dict, verified: bool) -> dict:
        """A provider notification (already signature-verified by the server). Used only as a trigger."""
        with self.lock:
            if not verified:
                raise Refused("unverified event")
            eid = str(event.get("id"))
            if eid in self.s["events_seen"]:
                self.s["reconcile_log"].append({"at": self.now(), "event": eid, "result": "duplicate ignored"})
                self._save()
                return {"event": eid, "result": "duplicate ignored"}
            self.s["events_seen"].append(eid)
            obj = (event.get("data") or {}).get("object") or {}
            meta = obj.get("metadata") or {}
            name = event.get("name", "")
            run = meta.get("run_id")
            result = "ignored"
            if run and run != self.s["run_id"]:
                result = f"belongs to earlier run {run}: kept for reconciliation, not applied to current hotel state"
            elif name.startswith("payment_intent."):
                op = next((o for o in self.s["operations"].values() if o.get("payment_intent_id") == obj.get("id")), None)
                if op:
                    self._verify_deposit(self.s["deals"][op["deal_id"]], op, source=f"webhook {name}")
                    result = f"re-read intent: {op.get('provider_status')}"
            elif name.startswith("refund."):
                op = next((o for o in self.s["operations"].values() if o.get("provider_id") == obj.get("id")), None)
                if op:
                    before = op["status"]
                    self._apply_refund_status(op, self.provider.get_refund(op["provider_id"]))
                    result = f"re-read refund: {op.get('provider_status')}" + (" (already reconciled; ledger unchanged)"
                                                                               if before == "completed" else "")
            self.s["reconcile_log"].append({"at": self.now(), "event": eid, "name": name, "result": result})
            self.log(None, "system", "webhook", f"Provider event {name} {eid}: {result}")
            self._save()
        if result != "ignored" and "earlier run" not in result:
            self._wake("provider event")
        return {"event": eid, "result": result}

    # ================================================================ escalation / handoff
    def escalate(self, deal_id: str, reason: str, message: str = "", actor: str = "seller") -> dict:
        with self.lock:
            d = self.deal(deal_id)
            d["handoff"] = {"at": self.now(), "reason": reason}
            if d["status"] not in ("confirmed", "refund_due", "refund_pending"):
                d["status"] = "handoff"
            if not message and not d.get("booking") and not any(m["from"] == "seller" for m in d["thread"]):
                # An inquiry nobody has answered yet: the buyer gets an acknowledgement, never silence.
                message = ("Thank you for your inquiry. A member of our group sales team will review it and reply "
                           "to you directly.")
            self._seller_acted(d)
            if message:
                d["thread"].append({"id": self._id("msg"), "at": self.now(), "from": "seller", "channel": "email",
                                    "text": message})
            self.log(d["id"], actor, "handoff", f"Handed to a person: {reason}")
            self._save()
            return {"status": d["status"]}

    def _seller_acted(self, d: dict) -> None:
        """A staff instruction stays in front of the seller until it next writes to the buyer or hands off."""
        if d.get("staff_instruction"):
            d.setdefault("staff_instructions_done", []).append({**d.pop("staff_instruction"), "done_at": self.now()})

    def hand_back(self, deal_id: str, instruction: str = "", actor: str = "hotel staff") -> dict:
        """Hotel staff return a handed-off inquiry to the seller, optionally with an instruction it follows."""
        with self.lock:
            d = self.deal(deal_id)
            if d["status"] != "handoff" or d.get("booking"):
                raise Refused("only an inquiry handed to staff (with no booking) can be handed back")
            text = (instruction or "").strip()[:1000]
            reason = (d.get("handoff") or {}).get("reason", "")
            d["handoff"] = None
            d["status"] = "intake" if d["requirements"] else "new"
            if d["status"] == "intake" and d["thread"] and any(m.get("date_options") for m in d["thread"]):
                d["status"] = "dates_proposed"
            if text:
                d["staff_instruction"] = {"at": self.now(), "by": actor, "text": text}
            d["unread"] = True
            self.log(d["id"], actor, "handed_back", "Handed back to the seller" + (f": {text}" if text else ".")
                     + (f" (was: {reason})" if reason else ""))
            self._save()
        self._wake("staff")
        return {"status": d["status"]}

    def close_deal(self, deal_id: str, note: str = "", actor: str = "hotel staff") -> dict:
        """Hotel staff close a handed-off inquiry without a reply from the seller."""
        with self.lock:
            d = self.deal(deal_id)
            if d["status"] != "handoff" or d.get("booking"):
                raise Refused("only an inquiry handed to staff (with no booking) can be closed here")
            hold = self.s["holds"].get(d.get("hold_id") or "")
            if hold and hold["status"] == "active":
                self._release_hold(hold, "inquiry closed by hotel staff")
            d["status"] = "lost"
            d["closed"] = {"at": self.now(), "by": actor, "note": (note or "").strip()[:500]}
            self.log(d["id"], actor, "closed", "Inquiry closed by hotel staff" + (f": {note.strip()}" if note.strip() else "."))
            self._save()
            return {"status": d["status"]}

    def decline(self, deal_id: str, reason: str, message: str, actor: str = "seller") -> dict:
        with self.lock:
            d = self.deal(deal_id)
            hold = self.s["holds"].get(d.get("hold_id") or "")
            if hold:
                self._release_hold(hold, "declined")
            d["status"] = "declined"
            self._seller_acted(d)
            d["thread"].append({"id": self._id("msg"), "at": self.now(), "from": "seller", "channel": "email",
                                "text": message})
            self.log(d["id"], actor, "declined", reason)
            self._save()
            return {"status": "declined"}

    # ================================================================ views
    def finance(self, d) -> dict:
        received = sum(e["amount"] for e in self.s["ledger"] if e["deal_id"] == d["id"] and e["type"] == "deposit_received")
        refunded = -sum(e["amount"] for e in self.s["ledger"] if e["deal_id"] == d["id"] and e["type"] == "refund_completed")
        pending = sum(o["amount"] for o in self._refund_ops(d) if o["status"] in ("submitting", "unknown", "created", "pending"))
        cur = self._version(d, d["accepted_version"]) if d.get("accepted_version") else None
        out = {"received": received, "refunded": refunded, "refund_pending": pending, "net_received": received - refunded,
               "refund_owed": d["finance"]["refund_owed"], "total": cur["pricing"]["total"] if cur else None}
        if cur:
            # While a refund is owed, what is still due is measured against receipts net of that refund.
            out["still_due"] = cur["pricing"]["total"] - (out["net_received"] - out["refund_owed"])
            signing = self._version(d, d["booking"]["version"])["accepted_at"] if d.get("booking") else None
            if out["refund_owed"] or pending:
                out["projected_after_refund"] = calc.schedule(cur["pricing"]["total"], cur["terms"]["deposit_pct"],
                                                              out["net_received"] - out["refund_owed"], calc.arrival_of(cur["terms"]),
                                                              self.policy, signing)
                out["collection_blocked"] = "refund unresolved: reconcile before any further collection"
                out["schedule"] = out["projected_after_refund"]
            else:
                out["schedule"] = calc.schedule(cur["pricing"]["total"], cur["terms"]["deposit_pct"],
                                                out["net_received"], calc.arrival_of(cur["terms"]), self.policy,
                                                signing)
        return out

    def _key_dates(self, d) -> dict | None:
        """Deadlines for this deal's own stay: the accepted contract, else the latest version, else the
        requested arrival. None until an arrival is known."""
        v = self._version(d, d["accepted_version"]) if d.get("accepted_version") else (
            d["versions"][-1] if d["versions"] else None)
        arrival = calc.arrival_of(v["terms"]) if v else (d.get("requirements") or {}).get("arrival")
        return calc.key_dates(arrival, self.policy) if arrival else None

    def waiting_on(self, d) -> str | None:
        pend = [a for a in self.s["approvals"].values() if a["deal_id"] == d["id"] and a["status"] == "pending"]
        if pend:
            a = pend[-1]
            return ("hotel manager" if a["approver"] == "hotel_manager" else "buyer's organizer") + f": {a['subject']} approval"
        if d.get("seller_paused"):
            return "hotel staff: retry after a payment-provider error"
        st = d["status"]
        if d.get("unread") and st not in ("new",):
            return "seller: new input to read"
        return {"new": "seller: read inquiry", "intake": "seller: check records", "needs_info": "buyer: answers",
                "offer_sent": "buyer: response to offer", "approved_to_send": "seller: send approved offer",
                "revise": "seller: revise offer", "accepted": "seller: open checkout",
                "awaiting_payment": "buyer: pay deposit on hosted page", "refund_due": "seller: refund",
                "refund_pending": "provider: refund completion (check status)", "amendment_sent": "buyer: accept amendment",
                "amendment_approved": "seller: send amendment", "expired": "seller: renew offer",
                "handoff": "hotel staff", "dates_proposed": "buyer: choose from the dates offered",
                "payment_received_booking_exception": "hotel manager: booking exception",
                "refund_awaiting_approval": "hotel manager: refund approval",
                "refund_failed": "hotel manager: failed refund follow-up (amount still owed)"}.get(st)

    def next_step(self, d) -> str:
        st = d["status"]
        if d.get("seller_paused"):
            return (f"paused: {d['seller_paused']['what']} failed at the payment provider; wait for hotel staff "
                    "to retry (do not call it again)")
        refund_appr = [a for a in self.s["approvals"].values() if a["deal_id"] == d["id"] and a["subject"] == "refund"]
        if refund_appr and d["finance"]["refund_owed"] and not any(
                o["status"] in ("submitting", "unknown", "created", "pending") for o in self._refund_ops(d)):
            last = refund_appr[-1]
            if last["status"] == "approved" and not last.get("used"):
                return f"manager approved the {gbp(last['amount'])} refund: call refund_excess now to execute it"
            if last["status"] == "rejected":
                return "manager withheld refund authorization: escalate (the buyer is still owed the money)"
        if st == "refund_pending":
            return "a refund exists: call check_refund (never create another)"
        if st == "refund_due":
            return "call refund_excess for the amount owed"
        if st == "expired":
            return ("buyer asked after the lapse: re-check availability and renew" if d.get("unread")
                    and d["thread"] and d["thread"][-1]["from"] == "buyer" else
                    "lapsed: do not renew unless the buyer asks")
        instr = d.get("staff_instruction")
        if instr and st in ("new", "intake", "needs_info", "dates_proposed", "offer_sent", "expired"):
            return (f"hotel staff handed this back with an instruction: \"{instr['text']}\". Act on it with your "
                    "tools, within the hotel's rules, and reply to the buyer")
        if st == "new":
            return "read the inquiry and record requirements"
        req = d.get("requirements") or {}
        if st == "intake" and req.get("date_issue"):
            return ("the requested dates can't be sold: call suggest_dates, then offer_dates with the available "
                    "options (do not escalate for dates)")
        if st == "intake":
            return "ask for missing details" if d["missing"] else "check systems and quote"
        if st == "dates_proposed":
            return ("read the buyer's reply: record_requirements with the dates they chose, then check_systems and "
                    "quote" if d.get("unread") else "blocked: waiting for the buyer to choose dates")
        if st == "needs_info":
            return "read answers and quote" if d.get("unread") else "blocked: waiting for the buyer's answers"
        if st in ("offer_sent", "amendment_sent"):
            return "respond to the buyer's message" if d.get("unread") else "blocked: waiting for the buyer"
        return self.waiting_on(d) or st

    def ops_instructions(self, d) -> dict | None:
        b = d.get("booking")
        if not b or b["status"] != "confirmed":
            return None
        v = self._version(d, d["accepted_version"])
        t = v["terms"]
        rooms = b["rooms"]
        per_night = sorted(set(rooms.values()))
        occ = 2 if t["room_type"] == "twin" else 1
        dates = calc.key_dates(calc.arrival_of(t), self.policy)
        return {
            "front_office": f"{', '.join(f'{n}: {r} {t['room_type']}' for n, r in rooms.items())}; expected guests "
                            f"{max(per_night) * occ if per_night else 0}; company master bill to {t.get('payer')}; "
                            "guests pay incidentals. Rooming list due " + dates["rooming_list_due"] + ".",
            "events": (f"Thames Room {t['meeting']['date']} {t['meeting'].get('hours') or ''}, {t['meeting'].get('layout')} "
                       f"for {t['meeting']['attendees']}; projector, microphone, Wi-Fi.") if t.get("meeting") else "No meeting space.",
            "food_and_beverage": "Guaranteed group breakfast: " + ", ".join(f"{m}: {n}" for m, n in t["breakfast"].items())
                                 if t["breakfast"] else "No group breakfast.",
            "finance": f"Release deadline {dates['release_deadline']}; allowance remaining "
                       f"{v['pricing']['allowance_remaining']} room-nights.",
        }

    def _calendar(self) -> dict:
        """Every bookable night, morning and meeting day, stored entries merged over the scenario's defaults
        (plus any stored entry outside the window, so nothing held or booked is ever hidden). New dicts
        throughout: callers can't mutate state through it."""
        h, st = self.hotel, self.s
        cap, inv = h["capacity"], {}
        for n in sorted(set(cap) | set(st["inventory"])):
            x = st["inventory"].get(n) or {rt: {"capacity": c, "held": 0, "booked": 0} for rt, c in cap[n].items()}
            inv[n] = {rt: {**v, "available": v["capacity"] - v["held"] - v["booked"]} for rt, v in x.items()}
        free = {"held": 0, "booked": 0}
        bf = {m: dict(st["breakfast"].get(m) or free)
              for m in sorted(set(h["mornings"]) | set(st["breakfast"]))}
        slots = st["meeting"].get("thames", {})
        meeting = {x: dict(slots.get(x) or {"status": "free", "deal_id": None})
                   for x in sorted(set(h["meeting_rooms"]["thames"]["available"]) | set(slots))}
        return {"inventory": inv, "breakfast": bf, "meeting": {"thames": meeting}, "bookable": {**h["bookable"],
                                                                          "min_days_before_arrival": self.policy["min_days_before_arrival"],
                                                                          "allocation": dict(h["availability"])}}

    def snapshot(self) -> dict:
        with self.lock:
            self._sweep()
            deals = []
            for d in self.s["deals"].values():
                x = copy.deepcopy(d)
                x['payer_fx'] = self.payer_view(d)
                x["waiting_on"] = self.waiting_on(d)
                x["next_step"] = self.next_step(d)
                x["finance_view"] = self.finance(d)
                x["payout_view"] = self.payout_view(d)
                x["ops"] = self.ops_instructions(d)
                x["hold"] = self._hold_public(d)
                x["pay_url"] = self.pay_url(d) if self._deposit_op(d) else None
                deals.append(x)
            ops = [{k: v for k, v in o.items() if k != "client_secret"} for o in self.s["operations"].values()]
            cal = self._calendar()
            return {"run_id": self.s["run_id"], "clock": self.now(), "hotel": self.hotel["name"], **cal,
                    "breakfast_capacity": self.hotel["breakfast"]["group_capacity_per_day"], "deals": deals,
                    "approvals": list(self.s["approvals"].values()), "operations": ops, "ledger": self.s["ledger"],
                    "activity": self.s["activity"][-200:],
                    "agent_runs": copy.deepcopy(self.s.get("agent_runs", [])[-40:]), "sim": {**self.s["sim"], **getattr(self.provider, "knobs", {})},
                    "delayed_notifications": len(self.s["delayed_notifications"]),
                    "reconcile_log": self.s["reconcile_log"][-30:], "provider": self.provider.name}

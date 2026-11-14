"""A record of what the seller agent did, for the dashboard's AI Agent tab.

Both sellers act only through Engine methods. During a run, `Traced` stands in for the engine and
records each of those calls as a step: the tool, its inputs, a short result, whether the engine
refused it, and the activity entries the call produced. Recording observes; it never changes what
the engine allows. The record is presenter-side only (it sits next to the activity log).
"""
from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone

from .calc import gbp
from .engine import Refused, _iso

# engine method -> tool name the dashboard shows (the Claude seller's own tool names)
TOOLS = {
    'vendor_workspace': 'vendor_workspace', 'request_vendor_quote': 'request_vendor_quote',
    "queue": "list_queue", "deal_view": "read_deal", "record_requirements": "record_requirements",
    "check_systems": "check_systems", "ask_buyer": "ask_buyer", "suggest_dates": "suggest_dates", "offer_dates": "offer_dates", "price_terms": "price_terms",
    "propose_offer": "propose_offer", "send_offer": "send_offer", "open_checkout": "open_checkout",
    "check_payment": "check_payment", "prepare_reduction": "prepare_reduction", "send_amendment": "send_amendment",
    "refund_excess": "refund_excess", "check_refund": "check_refund", "escalate": "escalate", "decline": "decline",
    "request_balance": "request_balance", "check_balance_payment": "check_balance_payment",
    "note": "log_reasoning",
}
READS = {"list_queue", "read_deal", "vendor_workspace"}
MAX_RUNS = 60
_PENCE = {"rate", "budget_total", "target_per_room_night"}


def _short(v, n=1500):
    if isinstance(v, str):
        return v if len(v) <= n else v[:n] + "…"
    try:
        s = json.dumps(v, default=str)
    except (TypeError, ValueError):
        s = str(v)
    return v if len(s) <= n else s[:n] + "…"


def _clean(v):
    """Drop empty values inside nested inputs, so details show what was actually set."""
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items() if x not in (None, "", [], {})}
    return v


def _args(bound: dict) -> dict:
    out = {}
    for k, v in bound.items():
        if k in ("self", "deal_id", "actor", "source", "mark_read", "for_") or v in (None, "", [], {}):
            continue
        if k in _PENCE and isinstance(v, int):
            v = gbp(v)
        elif isinstance(v, dict):
            v = _clean({kk: gbp(vv) if kk in _PENCE and isinstance(vv, int) else vv for kk, vv in v.items()})
        out[k] = _short(v)
    return out


def _result(tool: str, res) -> dict | None:
    if res is None or tool == "log_reasoning":
        return None
    if tool == "read_deal":
        return {"status": res.get("status")} if isinstance(res, dict) else None
    if tool == "list_queue":
        return {"inquiries": len(res)}
    if tool == "price_terms":
        p, hc = res["pricing"], res["hotel_check"]
        return {"total": gbp(p["total"]), "deposit": gbp(p["deposit"]),
                "margin": f"{p['margin_all_rooms']['margin_pct']}% / {p['margin_full_release']['margin_pct']}% after releases",
                "authority": ("over a hard limit: " + "; ".join(hc["hard"])) if hc["hard"] else
                             ("needs the manager: " + "; ".join(hc["approvals"])) if hc["approvals"] else "within the seller's own authority",
                "fits_inventory": res["fits_inventory"]}
    if isinstance(res, dict):
        keep = {k: v for k, v in res.items() if k not in ("finance", "records", "pms", "meeting", "breakfast", "rms",
                                                          "crm", "policy", "payment_url", "client_secret",
                                                          "pricing", "terms", "hotel_check", "lines", "requirements")}
        return {k: _short(v, 300) for k, v in keep.items() if v not in (None, "", [], {})} or None
    return {"result": _short(res, 300)}


def _summary(tool, step, res) -> str:
    if not step["ok"]:
        return "Refused: " + step["error"]
    own = [x["text"] for x in step["effects"] if x["actor"] == "seller"]
    if own:
        return own[-1]
    if tool == "price_terms" and step.get("result"):
        r = step["result"]
        return f"Dry run {step['args'].get('rate', '')}: total {r['total']}, deposit {r['deposit']}; {r['authority']}."
    if tool == "read_deal":
        return "Read the deal"
    if tool == "list_queue":
        return f"Looked at the queue ({len(res or [])} inquiries)"
    if step["effects"]:
        return step["effects"][0]["text"]
    if tool == "check_payment" and isinstance(res, dict):
        return f"Checked the deposit with the provider: {res.get('status') or res.get('provider_status') or 'no change'}"
    if tool == "check_balance_payment" and isinstance(res, dict):
        return f"Checked the instalment with the provider: {res.get('status') or res.get('provider_status') or 'no change'}"
    return "Done"


class Traced:
    """Engine stand-in for one seller run. Attribute access passes through; tool calls are recorded."""

    def __init__(self, engine, agent: str, trigger: str = "", model: str | None = None, reads: bool = True):
        self._e = engine
        self._reads = reads
        self._run = engine.agent_run_start(agent, trigger, model)

    def __getattr__(self, name):
        attr = getattr(self._e, name)
        tool = TOOLS.get(name)
        if tool is None or not callable(attr) or (tool in READS and not self._reads):
            return attr

        def call(*a, **k):
            return self._step(tool, attr, a, k)
        return call

    def finish(self):
        self._e.agent_run_end(self._run)

    def _step(self, tool, fn, a, k):
        e = self._e
        try:
            bound = inspect.signature(fn).bind(*a, **k).arguments
        except TypeError:
            bound = {}
        state, n0 = e.s, len(e.s["activity"])
        step = {"at": e.now(), "real_at": _iso(datetime.now(timezone.utc)), "tool": tool, "deal_id": None,
                "ref": None, "args": _args(bound), "ok": True}
        try:
            d = e.deal(bound["deal_id"]) if bound.get("deal_id") else None
            step["deal_id"], step["ref"] = (d["id"], d["ref"]) if d else (None, None)
        except Exception:      # unknown deal: the call itself will say so
            pass
        res = None
        try:
            res = fn(*a, **k)
            step["result"] = _result(tool, res)
            return res
        except Refused as err:
            step["ok"], step["error"] = False, str(err)
            raise
        except Exception as err:
            step["ok"], step["error"] = False, f"{type(err).__name__}: {err}"
            raise
        finally:
            produced = state["activity"][n0:] if e.s is state else []
            step["effects"] = [{"kind": x["kind"], "actor": x["actor"], "text": x["text"]} for x in produced
                               if x["actor"] in ("seller", "system") and x["deal_id"] in (step["deal_id"], None)]
            try:
                step["summary"] = _summary(tool, step, res)
            except Exception:
                step["summary"] = tool.replace("_", " ")
            e.agent_step(self._run, step)

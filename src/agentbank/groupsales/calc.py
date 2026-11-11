"""Pure arithmetic for group packages: pricing, deposits, attrition, margin, refunds, schedules.

All amounts are integer pence. Percentages are whole numbers (30 means 30%).
Nothing here reads state; the engine passes in terms, hotel data and receipts.
"""
from __future__ import annotations

from datetime import date, timedelta


def pct_of(amount: int, pct: int | float) -> int:
    """Round half up to the penny."""
    return int((amount * pct + 50) // 100) if isinstance(pct, int) else int(round(amount * pct / 100))


def gbp(p: int) -> str:
    sign = "-" if p < 0 else ""
    p = abs(int(p))
    pounds, pence = divmod(p, 100)
    return f"{sign}£{pounds:,}" + (f".{pence:02d}" if pence else "")


def d(s: str) -> date:
    return date.fromisoformat(s[:10])


# ---- terms ---------------------------------------------------------------------------------
# terms = {
#   "room_type": "twin", "rate": 22000,
#   "rooms": {"2027-03-10": 12, ...},            # contracted rooms per night
#   "released": {"2027-03-10": 0, ...},          # cumulative rooms released per night (amendments)
#   "charged_room_nights": 0,                    # released room-nights still paid at the rate (beyond the
#                                                #   allowance or after the release deadline), cumulative
#   "waived": 0,                                 # pence of those charges waived by the manager, cumulative
#   "breakfast": {"2027-03-11": 24, ...},        # guests per morning (guaranteed)
#   "meeting": {"room": "thames", "date": "2027-03-11", "layout": "classroom", "attendees": 24,
#               "hours": "09:00-17:00"} | None,
#   "attrition_pct": 10, "deposit_pct": 30, "payer": "...", "currency": "GBP"
# }

def contracted_room_nights(terms: dict) -> int:
    return sum(int(n) for n in terms.get("rooms", {}).values())


def released_room_nights(terms: dict) -> int:
    return sum(int(n) for n in (terms.get("released") or {}).values())


def charged_room_nights(terms: dict) -> int:
    return int(terms.get("charged_room_nights") or 0)


def free_released_room_nights(terms: dict) -> int:
    """Released room-nights that used the attrition allowance (released and not charged)."""
    return released_room_nights(terms) - charged_room_nights(terms)


def attrition_allowance(terms: dict) -> int:
    """Original contracted room-nights x approved attrition %, rounded down."""
    return contracted_room_nights(terms) * int(terms.get("attrition_pct", 0)) // 100


def price(terms: dict, hotel: dict, rms: dict, firm: dict | None = None) -> dict:
    """Itemised price, deposit, attrition allowance and contribution for a set of terms.

    Service prices come from hotel data, never from the terms: an agent can only choose the room
    rate, deposit % and attrition %. `firm` is competing demand the engine found for these nights
    (see displacement); it changes only the margin, never the price.
    """
    rt = terms["room_type"]
    rate = int(terms["rate"])
    contracted = contracted_room_nights(terms)
    released = released_room_nights(terms)
    used_rn = contracted - released
    charged = charged_room_nights(terms)
    waived = int(terms.get("waived") or 0)
    attrition_charge = charged * rate
    bf_unit = hotel["breakfast"]["price_per_person"]
    bf_count = sum(int(n) for n in (terms.get("breakfast") or {}).values())
    meeting = terms.get("meeting")
    meeting_price = hotel["meeting_rooms"][meeting["room"]]["day_rate"] if meeting else 0

    lines = [{"item": f"{hotel['room_types'][rt]['label']} room-nights", "qty": used_rn, "unit": rate,
              "amount": used_rn * rate}]
    if released:
        lines[0]["note"] = f"{contracted} contracted, {released} released"
    if charged:
        lines.append({"item": "Attrition charge (released room-nights beyond the allowance or after the release "
                              "deadline, at the contracted rate)", "qty": charged, "unit": rate,
                      "amount": attrition_charge})
    if waived:
        lines.append({"item": "Attrition charge waived", "qty": 1, "unit": -waived, "amount": -waived})
    if bf_count:
        lines.append({"item": "Breakfast (guests x mornings, guaranteed)", "qty": bf_count, "unit": bf_unit,
                      "amount": bf_count * bf_unit})
    if meeting:
        mr = hotel["meeting_rooms"][meeting["room"]]
        lines.append({"item": f"{mr['label']} day hire ({meeting['date']}, {meeting.get('layout') or 'layout tbc'}; "
                              f"{', '.join(mr['includes'])})", "qty": 1, "unit": meeting_price,
                      "amount": meeting_price})
    accommodation = used_rn * rate
    charges_net = attrition_charge - waived
    vendor_lines = terms.get('vendor_lines', [])
    vendor_total = sum(line['total'] for line in vendor_lines)
    vendor_cost = sum(line['cost'] for line in vendor_lines)
    for line in vendor_lines:
        lines.append({'item': line['label'] + ' (third-party service)', 'qty': line['quantity'],
                      'unit': None, 'amount': line['total'], 'note': f"{line['start']} to {line['end']}; {line['location']}; bundled price"})
    total = accommodation + charges_net + bf_count * bf_unit + meeting_price + vendor_total
    deposit = pct_of(total, int(terms["deposit_pct"]))

    costs = rms["delivery_costs"]
    disp = displacement(terms, hotel, rms, firm)
    def contribution(room_nights: int) -> dict:
        # Delivery costs follow the rooms used. Displacement is carried on every contracted room-night;
        # released rooms get the resale share back (actual releases night by night, the rest of the
        # allowance at the average for the stay). Third-party vendor services pass through at their cost.
        revenue = room_nights * rate + charges_net + bf_count * bf_unit + meeting_price + vendor_total
        more = used_rn - room_nights
        resold = disp["resold_released"] + (disp["blocked"] * more * disp["resale_pct"]) // (contracted * 100 or 1)
        cost = (room_nights * costs[rt] + disp["blocked"] - resold + bf_count * costs["breakfast"]
                + (costs["meeting_day"] if meeting else 0) + vendor_cost)
        contrib = revenue - cost
        return {"revenue": revenue, "costs": cost, "contribution": contrib,
                "margin_pct": round(100 * contrib / revenue, 1) if revenue else 0.0}

    allowance = attrition_allowance(terms)
    remaining_allowance = max(0, allowance - (released - charged))
    return {
        "currency": "GBP",
        "lines": lines,
        "room_nights": used_rn,
        "contracted_room_nights": contracted,
        "released_room_nights": released,
        "charged_room_nights": charged,
        "attrition_charge": attrition_charge,
        "waived": waived,
        "breakfasts": bf_count,
        "accommodation": accommodation,
        "breakfast_total": bf_count * bf_unit,
        "meeting_total": meeting_price,
        "total": total,
        "deposit_pct": int(terms["deposit_pct"]),
        "deposit": deposit,
        "attrition_pct": int(terms["attrition_pct"]),
        "attrition_allowance": allowance,
        "allowance_remaining": remaining_allowance,
        # Breakfast per room-night counts at most a full room (max occupancy): guests beyond the block's beds
        # don't make the room itself dearer.
        "per_room_night_incl_breakfast": rate + (min(bf_unit * bf_count // used_rn,
                                                     bf_unit * int(hotel["room_types"][rt].get("max_occupancy") or 2))
                                                 if used_rn else 0),
        "displacement": disp,
        "margin_all_rooms": contribution(used_rn),
        "margin_full_release": contribution(used_rn - remaining_allowance),
    }


def demand_pct(night: str, room_type: str, hotel: dict, rms: dict) -> int:
    """Demand for a night (0-100): the chance a room blocked for a group would otherwise have sold.

    It is read from the same calendar the availability page shows: the share of the group allocation
    other business has already taken (allocation minus what is still on sale), so a sold-out or nearly
    full night carries high displacement and a quiet one little. Nights the RMS states explicitly
    (demand_by_night_pct, the demo stay whose allocation is set by hand) use that figure instead.
    """
    cfg = rms.get("displacement")
    if not cfg:
        return 0
    stated = (cfg.get("demand_by_night_pct") or {}).get(night)
    if stated is not None:
        return int(stated)
    alloc = int((hotel.get("availability") or {}).get(room_type, 0))
    on_sale = (hotel.get("capacity") or {}).get(night, {}).get(room_type)
    if not alloc or on_sale is None:
        return 0
    return max(0, min(100, (alloc - int(on_sale)) * 100 // alloc))


def displacement(terms: dict, hotel: dict, rms: dict, firm: dict | None = None) -> dict:
    """Opportunity cost of the rooms a group blocks, night by night (integer pence).

    blocked: displacement on every contracted room-night; resold_released: what comes back on rooms
    already released (they are released before the cutoff and the hotel resells resale_pct of that).

    firm: {"rooms": {night: n}, "groups": [...]}, rooms another group has asked for on these nights and
    can't have if this block is held (the engine works it out from the open inquiries). Those rooms
    would have sold for certain, so they carry the full-demand value instead of that night's demand.
    Released rooms still come back at the usual resale share: a release comes weeks later, after the
    waiting group has gone elsewhere.
    """
    cfg = rms.get("displacement") or {}
    rt = terms["room_type"]
    full = (cfg.get("full_demand") or {}).get(rt, 0)
    resale = int(cfg.get("resale_pct", 0))
    released = terms.get("released") or {}
    firm_rooms = (firm or {}).get("rooms") or {}
    nights, blocked, back = [], 0, 0
    for night in sorted(terms.get("rooms", {})):
        pct = demand_pct(night, rt, hotel, rms)
        per_room = full * pct // 100
        rooms, rel = int(terms["rooms"][night]), int(released.get(night, 0))
        sure = min(rooms, max(0, int(firm_rooms.get(night, 0))))
        blocked += sure * full + (rooms - sure) * per_room
        back += rel * per_room * resale // 100
        row = {"night": night, "demand_pct": pct, "per_room": per_room, "rooms": rooms}
        if sure:
            row["competing_rooms"] = sure
        nights.append(row)
    out = {"nights": nights, "blocked": blocked, "resold_released": back, "resale_pct": resale}
    if firm and firm_rooms:
        out["firm"] = firm
    return out


# ---- dates ---------------------------------------------------------------------------------

def arrival_of(terms: dict) -> str:
    """A contract's arrival date: its first contracted night. Deadlines count back from this."""
    return min(terms["rooms"])


def key_dates(arrival: str, policy: dict) -> dict:
    """Release, rooming-list and installment deadlines for a stay arriving on `arrival` (YYYY-MM-DD)."""
    arrival = d(arrival)
    inst = policy["installments"]
    return {
        "arrival": arrival.isoformat(),
        "release_deadline": (arrival - timedelta(days=policy["attrition"]["release_days_before_arrival"])).isoformat(),
        "rooming_list_due": (arrival - timedelta(days=policy["rooming_list_days_before_arrival"])).isoformat(),
        "first_installment_due": (arrival - timedelta(days=inst[0]["days_before_arrival"])).isoformat(),
        "final_installment_due": (arrival - timedelta(days=inst[-1]["days_before_arrival"])).isoformat(),
    }


def schedule(total: int, deposit_pct: int, net_received: int, arrival: str, policy: dict,
             signing_date: str | None = None) -> list[dict]:
    """Remaining payment obligations, counting all earlier payments toward each cumulative target."""
    arrival = d(arrival)
    milestones = [("Signing deposit", signing_date, deposit_pct)]
    for m in policy["installments"]:
        milestones.append((f"{m['cumulative_pct']}% paid", (arrival - timedelta(days=m["days_before_arrival"])).isoformat(),
                           m["cumulative_pct"]))
    covered = net_received
    out = []
    for label, due, pct in milestones:
        target = pct_of(total, pct) if pct < 100 else total
        amount = max(0, target - covered)
        covered += amount
        out.append({"label": label, "due": due, "cumulative_target": target, "amount": amount})
    return out


def cancellation_terms(policy: dict) -> str:
    return ("Cancelling the whole booking: 30% of the total more than 30 days before arrival, 50% from 30 to 8 days, "
            "100% within 7 days; payments already received are credited against the charge.")


# ---- reductions ----------------------------------------------------------------------------

def evaluate_reduction(terms: dict, release: dict, today: str, hotel: dict, policy: dict, rms: dict,
                       net_received: int, waive: int = 0) -> dict:
    """What happens if the buyer releases `release` ({night: rooms}) on `today` under `terms`.

    The contract prices every outcome, so no release is refused or left to judgement: room-nights
    released within the attrition allowance and before the release deadline come off the total;
    any others are released to inventory but still charged at the contracted rate (the attrition
    charge). `waive` (pence) gives back part of this change's charge; only the hotel manager can grant
    it, so a waiver is the one thing that makes a reduction need review.
    """
    dates = key_dates(arrival_of(terms), policy)
    new_released = dict(terms.get("released") or {n: 0 for n in terms["rooms"]})
    for night, n in release.items():
        if night not in terms["rooms"]:
            raise ValueError(f"{night} is not a contracted night")
        new_released[night] = int(new_released.get(night, 0)) + int(n)
        if new_released[night] > int(terms["rooms"][night]):
            raise ValueError(f"cannot release more rooms than contracted on {night}")
    requested = sum(int(n) for n in release.values())
    allowance = attrition_allowance(terms)
    already = released_room_nights(terms)
    free_used = free_released_room_nights(terms)
    before_deadline = today[:10] <= dates["release_deadline"]
    before_installment = today[:10] < dates["first_installment_due"]
    free = min(requested, max(0, allowance - free_used)) if before_deadline else 0
    charged = requested - free
    rate = int(terms["rate"])
    charge = charged * rate
    waive = int(waive or 0)
    if waive < 0 or waive > charge:
        raise ValueError(f"a waiver must be between £0 and this change's attrition charge ({gbp(charge)})")
    new_terms = {**terms, "released": new_released,
                 "charged_room_nights": charged_room_nights(terms) + charged,
                 "waived": int(terms.get("waived") or 0) + waive}
    old_price = price(terms, hotel, rms)
    new_price = price(new_terms, hotel, rms)
    new_deposit = new_price["deposit"]
    refund = max(0, net_received - new_deposit) if before_installment else 0
    charge_reasons = []
    if charged and before_deadline:
        charge_reasons.append(f"{charged} room-night(s) beyond the {allowance} room-night allowance "
                              f"({free_used} already used)")
    if charged and not before_deadline:
        charge_reasons.append(f"released after the room-release deadline ({dates['release_deadline']})")
    review = [f"waives {gbp(waive)} of the {gbp(charge)} attrition charge"] if waive else []
    total_change = new_price["total"] - old_price["total"]
    return {
        "requested_room_nights": requested,
        "allowance": allowance,
        "already_released": already,
        "free_room_nights": free,
        "charged_room_nights": charged,
        "attrition_charge": charge,
        "waived": waive,
        "charge_reasons": charge_reasons,
        "remaining_after": max(0, allowance - free_used - free),
        "within_allowance": charged == 0,
        "before_release_deadline": before_deadline,
        "before_first_installment": before_installment,
        "needs_manager_review": bool(review),
        "review_reasons": review,
        "new_terms": new_terms,
        "new_pricing": new_price,
        "accommodation_change": new_price["accommodation"] - old_price["accommodation"],
        "total_change": total_change,
        "revised_deposit": new_deposit,
        "refund_due": refund,
        "refund_mode": ("automatic" if before_installment else
                        "none" if total_change == 0 else "manager reconciliation"),
    }

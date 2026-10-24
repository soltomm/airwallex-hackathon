"""Sandbox-only market events, behind one interface (the guide's rule for simulations).

The demo's "new information" comes from here: rooms sold through another channel (so terms a
traveller confirmed may no longer be available at booking), and a hotel overbooking a night (so
the platform refunds guests, recovers the hotel's share and raises its reserve). Replace this with
real channel-manager and hotel feeds without touching the decision logic in offer_desk.py.
"""
from __future__ import annotations

from datetime import date


class MarketSimulator:
    def __init__(self, desk):
        self.desk = desk

    def sell_rooms(self, hotel_id: str, room_code: str, night: str, rooms: int) -> dict:
        """Another channel sells `rooms` of this type on `night` (never more than are left)."""
        h = self.desk.hotels[hotel_id]
        n = date.fromisoformat(night)
        left = h.rooms[room_code].total_rooms - h.booked.get((room_code, n), 0)
        sold = max(0, min(rooms, left))
        h.booked[(room_code, n)] = h.booked.get((room_code, n), 0) + sold
        self.desk.demand.record("simulated_sale", self.desk.now(), hotel_id=hotel_id, room=room_code,
                                night=night, rooms=sold)
        return {"sold": sold, "occupancy": str(h.occupancy(room_code, n)), "left": left - sold}

    def overbook(self, hotel_id: str, night: str, reason: str = "overbooked") -> dict:
        """The hotel can't honour stays covering `night`: hand them to the platform's incident flow."""
        return self.desk.hotel_incident(hotel_id, night, reason)

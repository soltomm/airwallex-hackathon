// In-chat negotiation panel (MCP App). The host renders it when search_hotels runs; it then polls
// negotiation_feed and shows each round live: the traveller's agent, the hotel agent's steps as they
// happen, offers with Book buttons, and bookings as their deposits are held or paid.
// Build: node build.mjs  ->  ../web/negotiation_app.html
import { App } from "@modelcontextprotocol/ext-apps/app-with-deps";

type Offer = {
  offer_id: string; hotel: string; room: string; check_in: string; check_out: string; guests: number;
  extras: string[]; cancellation: string; deposit: string; deposit_pct: string; total: string; expires_at: string;
};
type Booking = {
  booking_ref: string; hotel: string; status: string; total: string; deposit: string; currency: string;
  method: string; next_step?: string; metal?: { usdz: string; transaction?: string };
};
type Event = { seq: number; kind: string; [k: string]: any };

const app = new App({ name: "negotiation-room", version: "1.0.0" });
const $ = (id: string) => document.getElementById(id)!;
const esc = (s: string) => s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const words = (s: string) => s.replaceAll("_", " ");

// Seller replies are short markdown: bold, line breaks and "- " bullets.
const md = (s: string) =>
  esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/^[-•] (.*)$/gm, "• $1").replace(/\n/g, "<br>");

const hotels = new Map<string, { el: HTMLElement; published?: number; rounds: Map<string, HTMLElement> }>();
const offers = new Map<string, { offer: Offer; el: HTMLElement }>();
const bookings = new Map<string, HTMLElement>();
let since = 0;
let room = "";   // the traveller's session, from search_hotels; the feed is scoped to it

function hotelSection(id: string, name: string): HTMLElement {
  let h = hotels.get(id);
  if (!h) {
    const el = document.createElement("section");
    el.className = "hotel";
    el.innerHTML = `<div class="hotel-head"><h2>${esc(name)}</h2><span class="published"></span></div>
                    <div class="note"></div><div class="timeline"></div>`;
    $("hotels").appendChild(el);
    h = { el, rounds: new Map() };
    hotels.set(id, h);
  }
  return h.el;
}

function clampable(html: string): string {
  return `<div class="clamp">${html}</div><button class="more" type="button" hidden>more</button>`;
}

// Show "more" only where the three-line clamp actually cuts text off.
function revealMore(root: ParentNode) {
  for (const btn of root.querySelectorAll<HTMLButtonElement>("button.more[hidden]")) {
    const body = btn.previousElementSibling as HTMLElement;
    if (body.scrollHeight > body.clientHeight + 1) btn.hidden = false;
  }
}

document.addEventListener("click", (e) => {
  const t = e.target as HTMLElement;
  if (t.classList.contains("more")) {
    const body = t.previousElementSibling as HTMLElement;
    const open = body.classList.toggle("clamp");
    t.textContent = open ? "more" : "less";
  }
});

function onSearch(ev: Event) {
  const r = ev.request;
  const wants = [...r.extras.map(words), words(r.cancellation)].join(", ");
  $("request").textContent = `${r.guests} guests · ${r.check_in} → ${r.check_out} · ${wants}`;
  for (const res of ev.results) {
    const el = hotelSection(res.hotel_id, res.hotel);
    const h = hotels.get(res.hotel_id)!;
    const price = parseFloat(res.published_total);
    if (h.published === undefined || price < h.published) {
      h.published = price;
      el.querySelector(".published")!.innerHTML = `published from <s>${esc(res.published_total)}</s>`;
    }
  }
  // Warn only about hotels where no room can do the request as published.
  const byHotel = new Map<string, any[]>();
  for (const res of ev.results) byHotel.set(res.hotel_id, [...(byHotel.get(res.hotel_id) ?? []), res]);
  for (const [id, rooms] of byHotel) {
    const blocked = rooms.every((r) => !r.bookable_as_requested);
    hotels.get(id)!.el.querySelector(".note")!.textContent = blocked ? rooms.flatMap((r) => r.notes).join("; ") : "";
  }
}

function roundBlock(ev: Event): HTMLElement {
  const el = hotelSection(ev.hotel_id, ev.hotel);
  const h = hotels.get(ev.hotel_id)!;
  const key = `${ev.negotiation_id}:${ev.round}`;
  let block = h.rounds.get(key);
  if (!block) {
    block = document.createElement("div");
    block.innerHTML = `<div class="round">Round ${ev.round}</div><div class="slot-buyer"></div>
      <div class="bubble seller"><div class="who">Hotel agent</div><ul class="activity">
      <li class="working">working…</li></ul><div class="reply"></div><div class="offers"></div></div>`;
    el.querySelector(".timeline")!.appendChild(block);
    h.rounds.set(key, block);
    block.dataset.neg = ev.negotiation_id;
  }
  return block;
}

const current = new Map<string, HTMLElement>();   // negotiation_id -> round in progress

function onBuyer(ev: Event) {
  const block = roundBlock(ev);
  block.querySelector(".slot-buyer")!.innerHTML =
    `<div class="bubble buyer"><div class="who">Your agent</div>${clampable(md(ev.message))}</div>`;
  current.set(ev.negotiation_id, block);
}

function onActivity(ev: Event) {
  const block = current.get(ev.negotiation_id);
  if (!block) return;
  const list = block.querySelector("ul.activity")!;
  const li = document.createElement("li");
  li.textContent = ev.text;
  list.insertBefore(li, list.querySelector("li.working"));
}

function offerCard(o: Offer, hotelId: string): HTMLElement {
  const el = document.createElement("div");
  el.className = "offer";
  const total = parseFloat(o.total);
  const published = hotels.get(hotelId)?.published;
  const saving = published && published > total ? `<span class="saving">−${Math.round(published - total)}</span>` : "";
  const hold = o.cancellation === "non_refundable" ? "charged at booking" : `${Math.round(parseFloat(o.deposit_pct) * 100)}% held on card`;
  el.innerHTML = `<div><span class="total">${esc(o.total)}</span>${saving}</div>
    <div class="chips">${[...o.extras.map(words), words(o.cancellation), hold].map((c) => `<span class="chip">${esc(c)}</span>`).join("")}</div>
    <div class="meta">${esc(o.room)} · ${esc(o.offer_id)} · <span class="expiry"></span></div>
    <div class="actions"><button class="act" data-pay="card" type="button">Book · card</button>
    <button class="act alt" data-pay="Metal wallet" type="button">Book · Metal</button></div>`;
  for (const btn of el.querySelectorAll<HTMLButtonElement>("button.act")) {
    btn.onclick = async () => {
      for (const b of el.querySelectorAll<HTMLButtonElement>("button.act")) b.disabled = true;
      await app.sendMessage({ role: "user", content: [{ type: "text",
        text: `I'd like to book offer ${o.offer_id} (${o.hotel}, ${o.total}) and pay the deposit by ${btn.dataset.pay}.` }] });
    };
  }
  return el;
}

function onSeller(ev: Event) {
  const block = current.get(ev.negotiation_id) ?? roundBlock(ev);
  block.querySelector("li.working")?.remove();
  block.querySelector(".reply")!.innerHTML = clampable(md(ev.message));
  const grid = block.querySelector(".offers")!;
  for (const o of ev.offers as Offer[]) {
    const card = offerCard(o, ev.hotel_id);
    grid.appendChild(card);
    offers.set(o.offer_id, { offer: o, el: card });
  }
  current.delete(ev.negotiation_id);
}

const STATUS: Record<string, [string, string]> = {
  awaiting_payment: ["awaiting payment", "warn"], deposit_held: ["deposit held", "ok"], paid: ["paid", "ok"],
  checked_out: ["checked out", "ok"], cancelled: ["cancelled", "bad"], cancelled_refund_due: ["cancelled · refund due", "bad"],
  cancelled_fee_charged: ["cancelled · deposit kept", "bad"], no_show: ["no-show", "bad"],
};

function onBooking(ev: Event) {
  const b: Booking = ev.booking;
  $("bookings").hidden = false;
  let row = bookings.get(b.booking_ref);
  if (!row) {
    row = document.createElement("div");
    row.className = "booking";
    $("booking-list").appendChild(row);
    bookings.set(b.booking_ref, row);
  }
  const [label, tone] = STATUS[b.status] ?? [words(b.status), "warn"];
  const paid = b.method === "metal"
    ? `${b.metal?.usdz} *USDZ on Metal${b.metal?.transaction ? ` · tx ${b.metal.transaction.slice(0, 10)}…` : ""}`
    : `deposit ${b.deposit} ${b.currency} by card`;
  const link = b.status === "awaiting_payment" && b.method === "card" ? b.next_step?.match(/https?:\/\/\S+/)?.[0] : undefined;
  row.innerHTML = `<div><b>${esc(b.booking_ref)}</b> · ${esc(b.hotel)} · ${esc(b.total)} ${esc(b.currency)}
      <div class="meta">${esc(paid)}</div></div>
    <div class="actions" style="margin:0"><span class="pill ${tone}">${esc(label)}</span>
      ${link ? `<button class="act" type="button" data-link="${esc(link)}">Open checkout</button>` : ""}</div>`;
  row.querySelector<HTMLButtonElement>("button[data-link]")?.addEventListener("click", (e) =>
    app.openLink({ url: (e.target as HTMLElement).dataset.link! }));
}

const handlers: Record<string, (ev: Event) => void> = {
  search: onSearch, buyer: onBuyer, activity: onActivity, seller: onSeller, booking: onBooking,
};

function tick() {   // offer expiry countdowns
  const now = Date.now();
  for (const { offer, el } of offers.values()) {
    const left = Math.round((Date.parse(offer.expires_at) - now) / 1000);
    el.querySelector(".expiry")!.textContent = left > 0 ? `expires in ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}` : "expired";
    if (left <= 0 && !el.classList.contains("expired")) {
      el.classList.add("expired");
      for (const b of el.querySelectorAll<HTMLButtonElement>("button.act")) b.disabled = true;
    }
  }
}

function parse(result: any): any {
  if (result?.structuredContent) return result.structuredContent;
  const text = result?.content?.find((c: any) => c.type === "text")?.text;
  return text ? JSON.parse(text) : undefined;
}

let failures = 0;
async function poll() {
  try {
    const feed = parse(await app.callServerTool({ name: "negotiation_feed", arguments: { room, since } }));
    for (const ev of feed.events as Event[]) {
      handlers[ev.kind]?.(ev);
      since = ev.seq;
    }
    if (feed.events.length) revealMore(document);
    failures = 0;
    $("live").classList.remove("off");
    $("live").textContent = "live";
  } catch {
    if (++failures > 3) { $("live").classList.add("off"); $("live").textContent = "reconnecting"; }
  }
  setTimeout(poll, failures > 3 ? 5000 : 1000);
}

function applyTheme() {
  const theme = app.getHostContext()?.theme;
  if (theme) document.documentElement.dataset.theme = theme;
}

app.onhostcontextchanged = applyTheme;
app.ontoolresult = (result) => {   // the search result names the room; the feed carries the rest
  const data = parse(result);
  if (data?.room) room = data.room;
};
await app.connect();
applyTheme();
setInterval(tick, 1000);
poll();

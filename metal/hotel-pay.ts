// Hotel deposits paid on Metal: x402 endpoint the traveller's agent pays with its mandate.
//   npx tsx hotel-pay.ts          (from metal/; scripts/hotel_mcp.py must be running)
// GET /bookings/:ref/deposit - priced per booking in *USDZ, paid to that hotel's address.
// Bookings, prices and payees come from the hotel MCP server's local API on :8009; after
// settlement this reports the transaction back so the booking is marked paid.
import express from "express";
import { HTTPFacilitatorClient, x402ResourceServer } from "@x402/core/server";
import { paymentMiddleware } from "@x402/express";
import { METAL_NETWORKS, registerMetalScheme } from "@metalntwx/metal-pay";
import { formatUnits } from "viem";

const DESK = "http://127.0.0.1:8009";
const NETWORK = "eip155:63825";

type MetalBooking = { status: string; usdz: string; pay_to: string };
const booking = async (ref: string): Promise<MetalBooking> => {
  const res = await fetch(`${DESK}/api/metal/${ref}`);
  if (!res.ok) throw new Error((await res.json()).error ?? `booking ${ref}: HTTP ${res.status}`);
  return res.json();
};
const refFrom = (path: string) => path.match(/^\/bookings\/([\w-]+)\/deposit/)?.[1] ?? "";

const facilitator = new HTTPFacilitatorClient({ url: METAL_NETWORKS.testnet.facilitatorUrl });
const server = registerMetalScheme(new x402ResourceServer(facilitator), { networks: ["testnet"] });

server.onAfterSettle(async ({ result, paymentPayload, requirements }) => {
  const ref = refFrom(new URL(paymentPayload.resource?.url ?? "http://x/").pathname);
  const usdz = formatUnits(BigInt(result.amount ?? requirements.amount), 18);
  console.log(`settled ${ref} success=${result.success} ${usdz} *USDZ payer=${result.payer} tx=${result.transaction}`);
  if (!result.success || !ref) return;
  const res = await fetch(`${DESK}/api/metal/${ref}/paid`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ transaction: result.transaction, payer: result.payer, usdz }),
  });
  if (!res.ok) console.error(`could not record payment for ${ref}: ${await res.text()}`);
});

const app = express();

// Refuse unknown or already-paid bookings before quoting a price.
app.use("/bookings/:ref/deposit", async (req, res, next) => {
  try {
    const b = await booking(req.params.ref);
    if (b.status !== "awaiting_payment") return res.status(409).json({ error: `booking is ${b.status}` });
    next();
  } catch (e) {
    res.status(404).json({ error: (e as Error).message });
  }
});

app.use(paymentMiddleware({
  "GET /bookings/:ref/deposit": {
    accepts: {
      scheme: "exact", network: NETWORK,
      price: async (ctx) => (await booking(refFrom(ctx.path))).usdz,
      payTo: async (ctx) => (await booking(refFrom(ctx.path))).pay_to,
    },
    description: "Hotel booking deposit",
  },
}, server));

app.get("/bookings/:ref/deposit", (req, res) =>
  res.json({ booking_ref: req.params.ref, deposit: "paid", note: "receipt recorded with the hotel" }));

app.listen(4022, "127.0.0.1", () => console.log("hotel deposits on http://127.0.0.1:4022/bookings/<ref>/deposit"));

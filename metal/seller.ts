// x402 seller on Metal testnet for kits 11 and 12.
//   npx tsx seller.ts            (from metal/; reads ../.env for METAL_RPC_URL)
// GET /check  - 0.25 *USDZ per call, `exact` scheme (kit 11's paid API)
// GET /chunk  - 0.001 by clearing (`bajillion`) or 0.01 by `exact` (kit 12's provider)
import express from "express";
import { HTTPFacilitatorClient, x402ResourceServer } from "@x402/core/server";
import { paymentMiddleware } from "@x402/express";
import { ClearingServerScheme, METAL_NETWORKS, registerMetalScheme } from "@metalntwx/metal-pay";
import { openClearingWallet } from "@metalntwx/metal-pay/node";
import { readFileSync } from "node:fs";

const PAY_TO = JSON.parse(readFileSync("../var/metal/owner-key.json", "utf8")).address as string;
const NETWORK = "eip155:63825";
const sellerWallet = await openClearingWallet("../var/metal/clearing-seller");

const facilitator = new HTTPFacilitatorClient({ url: METAL_NETWORKS.testnet.facilitatorUrl });
// Offer clearing only when the facilitator lists a clearing (`bajillion`) deployment.
// As of 2026-10-21 the testnet facilitator lists `exact` only.
const supported = await (await fetch(`${METAL_NETWORKS.testnet.facilitatorUrl}/supported`)).json();
const clearing = (supported.kinds ?? []).some((k: any) => k.scheme === "bajillion" && k.network === NETWORK);
const server = registerMetalScheme(new x402ResourceServer(facilitator), { networks: ["testnet"] });
if (clearing) server.register(NETWORK, new ClearingServerScheme(NETWORK));

server.onAfterSettle(async ({ result }) => {
  const s = result.extra?.settlement as { id: string; first: boolean } | undefined;
  console.log(`settled success=${result.success} first=${s?.first} id=${s?.id ?? "-"} tx=${result.transaction || "(clearing, off chain)"}`);
});

const app = express();
app.use(paymentMiddleware({
  "GET /check": {
    accepts: { scheme: "exact", network: NETWORK, payTo: PAY_TO, price: "0.25" },
    description: "VetCheck: one supplier background check",
  },
  "GET /chunk": {
    accepts: [
      ...(clearing ? [{ scheme: "bajillion", network: NETWORK, payTo: sellerWallet.signer.publicKey, price: "0.001" }] : []),
      { scheme: "exact", network: NETWORK, payTo: PAY_TO, price: "0.01" },
    ],
    description: "Inference provider: one chunk of an answer",
  },
}, server));

app.get("/check", (_req, res) => res.json({ supplier: "Acme Freight Inc", sanctions: "clear", checkedAt: new Date().toISOString() }));
let chunk = 0;
app.get("/chunk", (_req, res) => res.json({ chunk: ++chunk, text: "stand-in model output" }));

app.listen(4021, "127.0.0.1", () => console.log(`seller on http://127.0.0.1:4021  exact payTo=${PAY_TO}  clearing ${clearing ? `payTo=${sellerWallet.signer.publicKey}` : "not offered (facilitator has no clearing deployment)"}`));

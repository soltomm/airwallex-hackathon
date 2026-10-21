// Kit 12 clearing test: deposit once, pay per request off chain, check what's left, queue a withdrawal.
//   npx tsx clearing-payer.ts [deposit-usdz]     (seller.ts must be running)
import { readFileSync } from "node:fs";
import { privateKeyToAccount } from "viem/accounts";
import {
  METAL_NETWORKS, USDZ, clearingSpendable, createClearingClient, createMetalPaymentClient,
  depositToClearing, registerClearingClient, usdzBaseUnits, withdrawFromClearing,
} from "@metalntwx/metal-pay";
import { openClearingWallet } from "@metalntwx/metal-pay/node";

const env = Object.fromEntries(readFileSync("../.env", "utf8").split("\n").filter(l => l.includes("=")).map(l => [l.slice(0, l.indexOf("=")), l.slice(l.indexOf("=") + 1)]));
const rpcUrl = env.METAL_RPC_URL;
const account = privateKeyToAccount(JSON.parse(readFileSync("../var/metal/agent-key.json", "utf8")).privateKey);
const wallet = await openClearingWallet("../var/metal/clearing-payer");
const NETWORK = "eip155:63825";

// The facilitator's /supported names the Clearing contract and the clearing service.
const supported = await (await fetch(`${METAL_NETWORKS.testnet.facilitatorUrl}/supported`)).json();
const kind = (supported.kinds ?? []).find((k: any) => k.scheme === "bajillion" && k.network === NETWORK);
if (!kind) throw new Error("facilitator lists no clearing (bajillion) deployment on testnet");
const { settlement, serviceUrl } = kind.extra;
console.log(`clearing contract ${settlement}\nclearing service  ${serviceUrl}`);

const fmt = (base: bigint) => (Number(base) / 1e18).toFixed(6) + " USDZ";
const spendable = async () => fmt(BigInt(await clearingSpendable({ rpcUrl, settlement, serviceUrl, account: wallet.signer.publicKey })));

const amount = process.argv[2] ?? "0.05";
console.log(`spendable before: ${await spendable()}\ndepositing ${amount} USDZ (waits for the window between epochs)...`);
await depositToClearing({ rpcUrl, chainId: 63825, from: account, settlement, asset: USDZ.address,
  account: wallet.signer.publicKey, amount: BigInt(usdzBaseUnits(amount)) });
console.log(`spendable after deposit: ${await spendable()}`);

const { client, fetchWithPayment } = createMetalPaymentClient({ account, network: "testnet" });
registerClearingClient(client, NETWORK, createClearingClient({
  serviceUrl, rpcUrl, network: NETWORK, settlement, asset: USDZ.address,
  signer: wallet.signer, store: wallet.store, maxAmount: BigInt(usdzBaseUnits("0.005")),
}));

// A fresh deposit pays from the next epoch; a payment between epochs fails, so retry briefly.
for (let i = 1; i <= 5; i++) {
  for (let attempt = 1; attempt <= 10; attempt++) {
    const res = await fetchWithPayment("http://127.0.0.1:4021/chunk");
    if (res.ok) { console.log(`chunk ${i}: ${res.status} ${await res.text()}`); break; }
    console.log(`chunk ${i} attempt ${attempt}: ${res.status} ${res.headers.get("payment-required") ? "(402)" : ""} ${(await res.text()).slice(0, 120)}`);
    await new Promise(r => setTimeout(r, 3000));
  }
}
console.log(`spendable after 5 chunks: ${await spendable()}`);

const w = await withdrawFromClearing({ rpcUrl, chainId: 63825, from: account, settlement, serviceUrl, signer: wallet.signer });
console.log("withdraw:", JSON.stringify(w, (_k, v) => typeof v === "bigint" ? v.toString() : v).slice(0, 400));

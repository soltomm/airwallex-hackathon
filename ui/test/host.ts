// Test host for the negotiation panel: embeds it like a chat client, completes the MCP Apps
// handshake, replays feed.json one event per poll (a live negotiation), and records what the
// panel sends back. Build + screenshot: node test/run.mjs
import { AppBridge, PostMessageTransport } from "@modelcontextprotocol/ext-apps/app-bridge";

declare const PANEL_HTML: string, FEED: any[];
const log = (m: string) => { (window as any).hostLog.push(m); console.log("[host]", m); };
(window as any).hostLog = [];

const iframe = document.getElementById("panel") as HTMLIFrameElement;
const bridge = new AppBridge(null, { name: "test-host", version: "1.0.0" }, { openLinks: {}, serverTools: {}, logging: {} });
let served = 0;
bridge.oncalltool = async (params) => {
  if (params.name !== "negotiation_feed") throw new Error(`unexpected tool ${params.name}`);
  const since = Number(params.arguments?.since ?? 0);
  // ALL_AT_ONCE: the whole feed in the first poll (for screenshots); otherwise one event per poll (live).
  served = (window as any).ALL_AT_ONCE ? FEED.length : Math.min(served + 1, FEED.length);
  const events = FEED.slice(since, served);
  log(`feed since=${since} -> ${events.map((e) => e.kind).join(",") || "none"}`);
  return { content: [{ type: "text", text: JSON.stringify({ events, last: since + events.length }) }] };
};
bridge.onmessage = async (params) => { log("message: " + JSON.stringify(params.content)); return {}; };
bridge.onopenlink = async (params) => { log("open link: " + params.url); return {}; };
bridge.oninitialized = () => log("initialized");
bridge.onsizechange = (p) => log(`size ${p.width}x${p.height}`);   // iframe height stays fixed for screenshots

iframe.srcdoc = PANEL_HTML;
iframe.onload = async () => {
  await bridge.connect(new PostMessageTransport(iframe.contentWindow!, iframe.contentWindow!));
  bridge.setHostContext({ theme: (window as any).THEME ?? "light" });
  await bridge.sendToolResult({ content: [{ type: "text", text: "[]" }] });
  // Click like a traveller once the feed has rendered (srcdoc iframes share our origin).
  setTimeout(() => {
    const doc = iframe.contentDocument!;
    doc.querySelector<HTMLButtonElement>('.offer button[data-pay="Metal wallet"]')?.click();
    doc.querySelector<HTMLButtonElement>("button[data-link]")?.click();
    log(`clicked; first offer buttons disabled: ${doc.querySelector<HTMLButtonElement>(".offer button.act")?.disabled}`);
  }, 3000);
};

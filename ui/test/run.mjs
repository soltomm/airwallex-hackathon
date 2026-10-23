// Builds the test host around web/negotiation_app.html and screenshots it in headless Chrome.
import { build } from "esbuild";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { execFileSync } from "node:child_process";

const panel = readFileSync("../web/negotiation_app.html", "utf8");
const feed = readFileSync("test/feed.json", "utf8");
const out = await build({ entryPoints: ["test/host.ts"], bundle: true, format: "esm", target: "es2022", write: false });
const js = out.outputFiles[0].text.replaceAll("</script", "<\\/script");
mkdirSync("test/out", { recursive: true });
const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
for (const theme of ["light", "dark"]) {
  const html = `<!doctype html><meta charset="utf-8"><body style="margin:0;background:${theme === "dark" ? "#1c1917" : "#fff"}">
<iframe id="panel" style="width:100%;height:2300px;border:0"></iframe>
<script>window.THEME=${JSON.stringify(theme)};window.ALL_AT_ONCE=true;const PANEL_HTML=${JSON.stringify(panel).replaceAll("</", "<\\/")};const FEED=${feed.replaceAll("</", "<\\/")};</script>
<script type="module">${js}</script>`;
  writeFileSync(`test/out/host-${theme}.html`, html);
  const page = new URL(`out/host-${theme}.html`, import.meta.url.replace("run.mjs", "")).href;
  const args = ["--headless=new", "--disable-gpu", "--hide-scrollbars", "--window-size=760,2300", "--force-prefers-reduced-motion",
                "--virtual-time-budget=30000", `--screenshot=test/out/panel-${theme}.png`, page];
  execFileSync(CHROME, args, { stdio: "ignore" });
  console.log(theme, "screenshot test/out/panel-" + theme + ".png");
}

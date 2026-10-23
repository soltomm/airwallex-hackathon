// Bundles negotiation.ts into one self-contained HTML file (the chat iframe loads no external scripts).
import { build } from "esbuild";
import { readFileSync, writeFileSync } from "node:fs";

const out = await build({ entryPoints: ["negotiation.ts"], bundle: true, format: "esm", target: "es2022",
                          minify: true, write: false, legalComments: "none" });
const js = out.outputFiles[0].text.replaceAll("</script", "<\\/script");
const html = readFileSync("negotiation.html", "utf8").replace("<script>/*__BUNDLE__*/</script>",
  () => `<script type="module">${js}</script>`);
writeFileSync("../web/negotiation_app.html", html);
console.log(`web/negotiation_app.html ${(html.length / 1024).toFixed(0)} KB`);

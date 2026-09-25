import fs from "node:fs";

const src = fs.readFileSync(new URL("../src/index.js", import.meta.url), "utf8");
const body = src
  .replace(/export \{[\s\S]*?\};\nexport default api;\n/, "")
  .replace(/^export default api;\n?/m, "");
const banner = "/* Senda Argus Browser Hooks v0.2.0 | Apache-2.0 */\n";
fs.writeFileSync(
  new URL("../dist/senda-argus-browser-hooks.js", import.meta.url),
  `${banner}(() => {\n${body}\n})();\n`
);

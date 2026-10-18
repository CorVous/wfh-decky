// Bundles the preview harness (dev only): node preview/build.mjs <outdir>
// esbuild + React 19 come from .devdeps (see scripts/preview-pages.sh); @decky/ui and
// @decky/api are aliased to the mocks in this folder. The plugin build (rollup) never sees it.
import { createRequire } from "node:module";
import { copyFileSync, mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.dirname(here);
const dev = path.join(root, ".devdeps", "node_modules");
const require = createRequire(path.join(root, ".devdeps", "package.json"));
const esbuild = require("esbuild");
const out = path.resolve(process.argv[2] || path.join(root, "out", "previews", ".static"));
mkdirSync(out, { recursive: true });

await esbuild.build({
  entryPoints: [path.join(here, "entry.tsx")],
  outdir: out,
  bundle: true,
  splitting: true,
  format: "esm",
  platform: "browser",
  target: "chrome120",
  jsx: "automatic",
  sourcemap: "inline",
  logLevel: "warning",
  define: { "process.env.NODE_ENV": '"development"' },
  alias: {
    "@decky/ui": path.join(here, "mock-ui.tsx"),
    "@decky/api": path.join(here, "mock-api.ts"),
    react: path.join(dev, "react"),
    "react-dom": path.join(dev, "react-dom"),
  },
  nodePaths: [path.join(root, "node_modules"), dev],
});
copyFileSync(path.join(here, "index.html"), path.join(out, "index.html"));
console.log(`preview bundle -> ${out}`);

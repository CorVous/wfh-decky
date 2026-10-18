// Screenshot runner for the preview harness (dev only); started by scripts/preview-pages.sh.
//   node preview/shoot.mjs --base http://127.0.0.1:PORT --out DIR [target...]
// target: a page id (src/registry.tsx), "qam:<section>", "pages" (all pages), "qam" (all
// QAM sections). No targets = all pages and all QAM sections.
// Writes <page>.png (full length: the frame is grown until nothing scrolls), <page>-viewport.png
// (the 1280x760 area under Steam's header, what the Deck shows without scrolling) and
// qam-<section>.png; prints console errors, RPC errors and d-pad reachability per target.
import { createRequire } from "node:module";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.dirname(here);
const require = createRequire(import.meta.url);

function loadPlaywright() {
  const tries = [process.env.PLAYWRIGHT_MODULE, "playwright", "/opt/node-tools/node_modules/playwright"].filter(Boolean);
  for (const t of tries) {
    try {
      return require(t);
    } catch {
      // next
    }
  }
  throw new Error("playwright not found (set PLAYWRIGHT_MODULE to its package dir)");
}

const args = process.argv.slice(2);
let base = "";
let out = path.join(root, "out", "previews");
const targets = [];
for (let i = 0; i < args.length; i++) {
  if (args[i] === "--base") base = args[++i];
  else if (args[i] === "--out") out = args[++i];
  else targets.push(args[i]);
}
if (!base) throw new Error("--base is required");
mkdirSync(out, { recursive: true });

const PAGES = [...readFileSync(path.join(root, "src", "registry.tsx"), "utf8").matchAll(/\{\s*id:\s*"([^"]+)",\s*title:/g)].map((m) => m[1]);
const QAMS = [...readFileSync(path.join(root, "src", "qam", "QamRoot.tsx"), "utf8").matchAll(/\{\s*id:\s*"([^"]+)",\s*title:\s*"[^"]*",\s*Qam:/g)].map((m) => m[1]);

let jobs = [];
for (const t of targets.length ? targets : ["pages", "qam"]) {
  if (t === "pages") jobs.push(...PAGES.map((p) => ({ kind: "page", id: p })));
  else if (t === "qam") jobs.push(...QAMS.map((q) => ({ kind: "qam", id: q })));
  else if (t.startsWith("qam:")) jobs.push({ kind: "qam", id: t.slice(4) });
  else jobs.push({ kind: "page", id: t });
}
jobs = jobs.filter((j, i) => jobs.findIndex((k) => k.kind === j.kind && k.id === j.id) === i);
for (const j of jobs) {
  const known = j.kind === "page" ? PAGES : QAMS;
  if (!known.includes(j.id)) console.warn(`warning: unknown ${j.kind} id "${j.id}" (known: ${known.join(", ")})`);
}

const HEADER = 40;
const W = 1280;
const H = 800;
const QAM_W = 310;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function settle(page, timeoutMs = 25000) {
  // Idle = no RPC in flight for 1s and no "Loading…" text left in the frame.
  const t0 = Date.now();
  let quietSince = 0;
  while (Date.now() - t0 < timeoutMs) {
    const st = await page.evaluate(() => {
      const w = window.__wfh;
      const frame = document.getElementById("pv-frame");
      const text = frame ? frame.innerText : "";
      return { ready: !!frame, pending: w ? w.pending : 1, loading: /Loading…|Loading\.\.\./.test(text) };
    });
    if (st.ready && st.pending === 0 && !st.loading) {
      if (!quietSince) quietSince = Date.now();
      if (Date.now() - quietSince > 1000) return { ok: true, ms: Date.now() - t0 };
    } else quietSince = 0;
    await sleep(150);
  }
  const why = await page.evaluate(() => ({ pending: window.__wfh?.pending, loading: /Loading…/.test(document.getElementById("pv-frame")?.innerText || "") }));
  return { ok: false, ms: Date.now() - t0, why };
}

async function overflowExtra(page) {
  return page.evaluate(() => {
    const frame = document.getElementById("pv-frame");
    let extra = 0;
    for (const el of frame ? frame.querySelectorAll("*") : []) {
      const s = getComputedStyle(el);
      if (!/(auto|scroll)/.test(s.overflowY) || el.clientHeight === 0) continue;
      extra = Math.max(extra, el.scrollHeight - el.clientHeight);
    }
    return extra;
  });
}

async function run(browser, job) {
  const isPage = job.kind === "page";
  const ctx = await browser.newContext({ viewport: { width: isPage ? W : QAM_W, height: H }, deviceScaleFactor: 1 });
  const page = await ctx.newPage();
  const consoleErrors = [];
  const pageErrors = [];
  page.on("console", (m) => {
    const t = m.text();
    if (m.type() === "error") consoleErrors.push(t);
    else if (m.type() === "warning" && !t.startsWith("[rpc-error]")) consoleErrors.push("warning: " + t);
  });
  page.on("pageerror", (e) => pageErrors.push(String(e?.stack || e)));
  const extra = process.env.WFH_PREVIEW_QUERY ? `&${process.env.WFH_PREVIEW_QUERY.replace(/^[?&]/, "")}` : "";
  const url = `${base}/?${isPage ? "page" : "qam"}=${encodeURIComponent(job.id)}${extra}`;
  const name = isPage ? job.id : `qam-${job.id}`;
  const res = { target: name, url };
  try {
    await page.goto(url, { waitUntil: "load" });
    const st = await settle(page);
    res.settled = st;
    // stop event polling so later screenshots are stable
    await page.evaluate(() => (window.__wfhNoEvents = true));
    res.nav = await page.evaluate(() => window.__nav?.reachability());
    if (isPage) {
      await page.screenshot({ path: path.join(out, `${name}-viewport.png`), clip: { x: 0, y: HEADER, width: W, height: H - HEADER } });
      let extra = await overflowExtra(page);
      res.contentHeight = H - HEADER + extra;
      for (let i = 0; i < 3 && extra > 0; i++) {
        const h = Math.min(30000, page.viewportSize().height + extra);
        await page.setViewportSize({ width: W, height: h });
        await sleep(250);
        extra = await overflowExtra(page);
      }
      res.contentHeight = page.viewportSize().height - HEADER + extra;
      await page.screenshot({ path: path.join(out, `${name}.png`), fullPage: true });
    } else {
      await page.screenshot({ path: path.join(out, `${name}.png`), fullPage: true });
      res.contentHeight = await page.evaluate(() => document.getElementById("pv-frame")?.scrollHeight || 0);
    }
    const w = await page.evaluate(() => window.__wfh);
    res.rpcCalls = w?.calls ?? 0;
    const seen = new Set();
    res.rpcErrors = (w?.rpcErrors ?? []).filter((e) => {
      const k = e.method + e.error;
      return !seen.has(k) && seen.add(k);
    });
    res.text = await page.evaluate(() => (document.getElementById("pv-frame")?.innerText || "").replace(/\s+/g, " ").slice(0, 160));
  } catch (e) {
    res.fatal = String(e?.stack || e);
  }
  res.consoleErrors = [...new Set(consoleErrors)];
  res.pageErrors = pageErrors;
  await ctx.close();
  return res;
}

function report(r) {
  const lines = [];
  const screens = r.contentHeight ? (r.contentHeight / (r.target.startsWith("qam-") ? H : H - HEADER)).toFixed(1) : "?";
  lines.push(`== ${r.target}  (${r.contentHeight ?? "?"}px tall, ${screens} screens; ${r.rpcCalls ?? 0} RPCs; ${r.settled?.ok ? `settled in ${r.settled.ms}ms` : `NOT settled: ${JSON.stringify(r.settled?.why)}`})`);
  if (r.fatal) lines.push(`   FATAL: ${r.fatal}`);
  if (r.nav) {
    lines.push(`   d-pad: ${r.nav.reached}/${r.nav.total} controls reachable from the first`);
    for (const u of r.nav.unreachable.slice(0, 12)) lines.push(`     unreachable: ${u}`);
    if (r.nav.unreachable.length > 12) lines.push(`     ... ${r.nav.unreachable.length - 12} more`);
    if (r.nav.blocked?.length) {
      lines.push(`   d-pad: ${r.nav.blocked.length} press(es) go nowhere although a control lies that way (missing flow-children="horizontal"?):`);
      for (const b of r.nav.blocked.slice(0, 8)) lines.push(`     ${b}`);
    }
    if (r.nav.wrongWay?.length) {
      lines.push(`   d-pad: ${r.nav.wrongWay.length} press(es) jump the wrong way (e.g. down moves sideways):`);
      for (const b of r.nav.wrongWay.slice(0, 8)) lines.push(`     ${b}`);
    }
  }
  for (const e of r.rpcErrors ?? []) lines.push(`   rpc error: ${e.method}: ${e.error}`);
  for (const e of r.pageErrors) lines.push(`   page error: ${e.split("\n")[0]}`);
  for (const e of r.consoleErrors.slice(0, 10)) lines.push(`   console: ${e.split("\n")[0].slice(0, 300)}`);
  if (r.consoleErrors.length > 10) lines.push(`   ... ${r.consoleErrors.length - 10} more console messages`);
  return lines.join("\n");
}

const { chromium } = loadPlaywright();
const browser = await chromium.launch({ args: ["--font-render-hinting=none"] });
const results = [];
const conc = Math.max(1, Number(process.env.WFH_PREVIEW_JOBS || 3));
let next = 0;
await Promise.all(
  Array.from({ length: Math.min(conc, jobs.length) }, async () => {
    while (next < jobs.length) {
      const job = jobs[next++];
      const r = await run(browser, job);
      results.push(r);
      console.log(report(r));
    }
  }),
);
await browser.close();
const order = (r) => jobs.findIndex((j) => (j.kind === "page" ? j.id : `qam-${j.id}`) === r.target);
results.sort((a, b) => order(a) - order(b));
writeFileSync(path.join(out, "report.json"), JSON.stringify(results, null, 1));
const bad = results.filter((r) => r.fatal || r.pageErrors.length || !r.settled?.ok);
console.log(`\n${results.length} target(s) -> ${out}${bad.length ? `; problems: ${bad.map((r) => r.target).join(", ")}` : ""}`);
process.exit(results.some((r) => r.fatal) ? 1 : 0);

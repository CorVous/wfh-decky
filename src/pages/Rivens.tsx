// Rivens: the live riven scanner (both cards with grades, rescan / close, similar
// listings on warframe.market, scanner settings) and the owned rivens decoded from
// the inventory with their grades. QAM: the last scan, Rescan / Close.
// Backend: py_modules/wfh/features/riven_scanner.py (riven.*), menus/rivens.py (rivens.*).
import {
  ButtonItem,
  DialogButton,
  Dropdown,
  DropdownItem,
  Field,
  Focusable,
  ModalRoot,
  Navigation,
  PanelSection,
  PanelSectionRow,
  TextField,
  ToggleField,
  showModal,
} from "@decky/ui";
import { toaster } from "@decky/api";
import { FC, ReactNode, useEffect, useState } from "react";

import { Badge, Empty, ErrorText, Grid, Loading, Page, Paged, Row, Section, Stat, chip, colors } from "../components/ui";
import { openTab } from "../nav";
import { ROUTE } from "../nav";
import { call, useEvent, useRpc } from "../rpc";

// ---- types (mirror features/riven_scanner.py, riven_grading.py, riven_market.py) ----------

interface ScanStat {
  name: string;
  positive: boolean;
  displayPositive?: boolean;
  value: number | null;
  multiplier?: boolean;
  grade?: string;
  rollFloat?: number;
}

interface GradeResult {
  stats: ScanStat[];
  overallGrade: string;
  attributeGrade: string;
  disposition?: number;
  rank?: number;
}

interface Side {
  stats: ScanStat[];
  grade: GradeResult | null;
  status: string | null;
  error: string | null;
}

/** The previous session's cards, kept by the backend after the Cycle screen closes. */
interface LastResult {
  weapon: string | null;
  layout: "reroll" | "chat";
  rollCount: number;
  totalKuvaSpent: number;
  left: Side;
  right: Side | null;
  at: number;
}

interface RivenState {
  active: boolean;
  visible: "none" | "both" | "left";
  layout: "reroll" | "chat";
  weapon: string | null;
  weaponSource: string | null;
  weaponMissing: boolean;
  rollCount: number;
  kuvaPerRoll: number;
  totalKuvaSpent: number;
  left: Side;
  right: Side;
  best: { positives: string[]; negatives: string[] } | null;
  chosen: string | null;
  lastScan: { kind: string; stats: number; outcome: string; elapsed_ms: number; low_confidence: boolean; at: number } | null;
  lastResult: LastResult | null;
  listingsCount: number | null;
  slug: string | null;
  updatedAt: number;
}

interface ListingStat {
  name: string;
  value: number;
  positive: boolean;
  matched?: boolean;
}

interface Listing {
  id: string;
  seller: string;
  sellerStatus: string | null;
  price: number;
  match: number;
  rerolls: number;
  isDirectSell: boolean;
  stats: ListingStat[];
  url: string;
  name: string;
}

interface SimilarResult {
  rows: Listing[];
  auctionsShown: boolean;
  error: string | null;
  loaded?: boolean;
  slug?: string | null;
}

interface OwnedStat {
  tag: string;
  name: string;
  displayValue: number;
  maxRankValue: number;
  rollFloat: number;
  grade: string;
  positive: boolean;
  multiplier: boolean;
}

interface OwnedRiven {
  itemId: string;
  weaponName: string;
  rivenName: string;
  masteryReq: number;
  currentRank: number;
  maxRank: number;
  rerolls: number;
  polarity: string | null;
  disposition: number | null;
  overallGrade: string;
  attributeGrade: string;
  statPerfectness: number;
  rivenType: string;
  familySlug: string | null;
  stats: OwnedStat[];
}

interface Veiled {
  label: string;
  challengeDesc?: string;
  challengeGroup?: string;
  challengeProgress?: number;
  challengeRequired?: number;
}

interface OwnedResult {
  inventory: { origin: string; syncTime: number | null; fetchedAt: number } | null;
  data: { version: string | null; weapons: number; types: number };
  sheet: { rows: number; updatedAt: number | null; stale: boolean; error: string | null };
  total: number;
  rivens: OwnedRiven[];
  veiled: Veiled[];
  veiledUnseen: { label: string; count: number }[];
}

interface ScannerSettings {
  overlays_enabled?: boolean;
  riven_scanner?: {
    enabled?: boolean;
    auctions_shown?: boolean;
    ui_scale_auto?: boolean;
    ui_scale?: number;
    canvas?: string;
    debug_dumps?: boolean;
    compare_arrows?: boolean;
  };
}

interface ScannerStatus {
  enabled: boolean;
  ocr_available: boolean;
  data: { version: string | null; weapons: number };
  goodrolls: { rows: number; updatedAt: number | null; stale: boolean; error: string | null };
  overlay: { connected: boolean; headless: boolean };
  realtime_active: boolean;
  ui_scale: number;
  canvas: string;
  last_capture_ms: number | null;
}

// ---- formatting ------------------------------------------------------------------------------

const GRADE_COLOR: Record<string, string> = {
  S: "#4ade80",
  "A+": "#6aab7a",
  A: "#6aab7a",
  "A-": "#6aab7a",
  "B+": "#facc15",
  B: "#facc15",
  "B-": "#facc15",
  "C+": "#f97316",
  C: "#f97316",
  "C-": "#f97316",
  F: "#ef4444",
};
const ATTR_COLOR: Record<string, string> = { Great: "#4ade80", Good: "#6aab7a", OK: "#facc15", Bad: "#ef4444" };

function num(v: number): string {
  return Number.isInteger(v) ? String(v) : String(Math.round(v * 100) / 100);
}

function scanValue(s: ScanStat): string {
  if (s.value == null) return "?";
  if (s.multiplier) return `x${num(s.value)}`;
  const shown = s.displayPositive ?? s.positive;
  return `${shown ? "+" : "−"}${num(s.value)}%`;
}

function ownedValue(s: OwnedStat, v: number): string {
  if (s.multiplier) return `x${num(v)}`;
  return `${v < 0 ? "−" : "+"}${num(Math.abs(v))}%`;
}

function listingValue(s: ListingStat): string {
  if (/^Damage Vs /i.test(s.name)) return `x${s.value.toFixed(2)}`;
  return `${s.value < 0 ? "−" : "+"}${Math.abs(Math.round(s.value))}%`;
}

function matchColor(pct: number): string {
  return pct >= 75 ? colors.good : pct >= 40 ? colors.warn : colors.bad;
}

function barColor(f: number, positive: boolean): string {
  const p = positive ? f : 1 - f;
  return p >= 0.85 ? "#48c774" : p >= 0.6 ? "#4f9eff" : p >= 0.35 ? "#f5a623" : "#ff5a5a";
}

const GradeBadge: FC<{ grade?: string | null }> = ({ grade }) =>
  grade && grade !== "?" ? <Badge text={grade} color={GRADE_COLOR[grade] ?? colors.muted} /> : null;

const AttrBadge: FC<{ grade?: string | null }> = ({ grade }) =>
  grade && grade !== "?" ? <Badge text={grade} color={ATTR_COLOR[grade] ?? colors.muted} /> : null;

const RollBar: FC<{ f?: number; positive: boolean }> = ({ f, positive }) =>
  f == null ? null : (
    <div style={{ height: "3px", background: "rgba(255,255,255,0.1)", marginTop: "2px" }}>
      <div style={{ width: `${Math.round(Math.max(0, Math.min(1, f)) * 100)}%`, height: "3px", background: barColor(f, positive) }} />
    </div>
  );

async function patchScanner(patch: Record<string, unknown>): Promise<void> {
  await call("settings.update", { patch: { riven_scanner: patch } });
}

// ---- live session ------------------------------------------------------------------------------

/** Scanner action with feedback: a backend error becomes a toast instead of an unhandled rejection. */
async function scannerAction(method: "riven.rescan" | "riven.close"): Promise<void> {
  try {
    const r = await call<{ started?: string | null; reason?: string } | null>(method);
    if (method === "riven.rescan" && r && r.started == null && r.reason === "disabled") {
      toaster.toast({ title: "Riven scanner", body: "The riven overlay is turned off in settings." });
    }
  } catch (e) {
    toaster.toast({ title: method === "riven.rescan" ? "Rescan failed" : "Close failed", body: String((e as Error)?.message ?? e) });
  }
}

function minutesAgo(at: number, now: number): string {
  const m = Math.round((now - at) / 60000);
  return m < 1 ? "just now" : m < 60 ? `${m} min ago` : `${Math.round(m / 60)} h ago`;
}

function useRivenState() {
  const st = useRpc<RivenState>("riven.state", {}, []);
  const [live, setLive] = useState<RivenState>();
  useEvent("riven.state", (s: RivenState) => setLive(s));
  return { ...st, data: live ?? st.data };
}

const StatLines: FC<{ side: Side; compact?: boolean }> = ({ side, compact }) => {
  const stats = side.grade?.stats ?? side.stats;
  if (side.status && stats.length === 0) return <div style={{ color: colors.muted, fontSize: "13px" }}>{side.status}</div>;
  if (side.error && stats.length === 0) return <div style={{ color: colors.bad, fontSize: "12px" }}>{side.error}</div>;
  if (stats.length === 0) return <div style={{ color: colors.muted, fontSize: "13px" }}>No card read yet.</div>;
  return (
    <div>
      {stats.map((s, i) => (
        <div key={i} style={{ padding: "2px 0" }}>
          <div style={{ display: "flex", alignItems: "center", gap: "6px", fontSize: compact ? "13px" : "14px" }}>
            <span style={{ fontWeight: 700, minWidth: compact ? "62px" : "76px", textAlign: "right" }}>{scanValue(s)}</span>
            <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: s.positive ? colors.text : "#f4a3a3" }}>{s.name}</span>
            <GradeBadge grade={s.grade} />
          </div>
          {!compact && <RollBar f={s.grade && s.grade !== "?" ? s.rollFloat : undefined} positive={s.positive} />}
        </div>
      ))}
    </div>
  );
};

const CardHeader: FC<{ title: string; grade: GradeResult | null; right?: ReactNode }> = ({ title, grade, right }) => (
  <div style={{ display: "flex", alignItems: "center", gap: "4px", marginBottom: "4px" }}>
    <span style={{ fontSize: "11px", textTransform: "uppercase", color: colors.muted, fontWeight: 700 }}>{title}</span>
    <span style={{ flex: 1 }} />
    <GradeBadge grade={grade?.overallGrade} />
    <AttrBadge grade={grade?.attributeGrade} />
    {right}
  </div>
);

const ListingRow: FC<{ l: Listing }> = ({ l }) => (
  <Focusable
    focusClassName="gpfocus"
    onActivate={() => showModal(<ListingModal l={l} />)}
    onClick={() => showModal(<ListingModal l={l} />)}
    style={{ padding: "4px 2px", borderBottom: `1px solid ${colors.border}` }}
  >
    <div style={{ display: "flex", gap: "10px", alignItems: "baseline" }}>
      <span style={{ color: matchColor(l.match), fontWeight: 700, minWidth: "42px" }}>{l.match}%</span>
      <span style={{ color: colors.plat, fontWeight: 600, minWidth: "56px" }}>{l.price}p</span>
      <span style={{ color: colors.muted, fontSize: "12px" }}>Rolls: {l.rerolls}</span>
      <span style={{ color: colors.muted, fontSize: "12px" }}>{l.isDirectSell ? "Sale" : "Auction"}</span>
      <span style={{ flex: 1 }} />
      <span style={{ color: colors.muted, fontSize: "12px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{l.seller}</span>
    </div>
    <div style={{ fontSize: "12px", display: "flex", flexWrap: "wrap", gap: "2px 10px" }}>
      {l.stats.map((s, i) => (
        <span key={i} style={{ color: s.positive ? colors.text : "#f4a3a3", opacity: s.matched ? 1 : 0.5, textDecoration: s.matched ? undefined : "line-through" }}>
          {listingValue(s)} {s.name}
        </span>
      ))}
    </div>
  </Focusable>
);

const ListingModal: FC<{ l: Listing; closeModal?: () => void }> = ({ l, closeModal }) => (
  <ModalRoot closeModal={closeModal} onCancel={closeModal}>
    <div style={{ color: colors.text, minWidth: "520px" }}>
      <div style={{ fontSize: "18px", fontWeight: 700, marginBottom: "6px" }}>
        {l.name || "Riven"} <span style={{ color: matchColor(l.match), fontSize: "14px" }}>{l.match}% match</span>
      </div>
      <div style={{ marginBottom: "8px" }}>
        <Stat label="Price" value={`${l.price}p`} color={colors.plat} />
        <Stat label="Rolls" value={l.rerolls} />
        <Stat label="Type" value={l.isDirectSell ? "Direct sale" : "Auction"} />
        <Stat label="Seller" value={`${l.seller}${l.sellerStatus ? ` (${l.sellerStatus})` : ""}`} />
      </div>
      {l.stats.map((s, i) => (
        <div key={i} style={{ color: s.positive ? colors.text : "#f4a3a3" }}>
          {listingValue(s)} {s.name}
        </div>
      ))}
      <div style={{ color: colors.muted, fontSize: "12px", margin: "10px 0", wordBreak: "break-all" }}>{l.url}</div>
      <Focusable flow-children="horizontal" style={{ display: "flex", gap: "8px", justifyContent: "flex-end" }}>
        <DialogButton
          style={{ width: "200px" }}
          onClick={() => {
            Navigation.NavigateToExternalWeb(l.url);
            closeModal?.();
          }}
        >
          Open in browser
        </DialogButton>
        <DialogButton style={{ width: "120px" }} onClick={closeModal}>
          Close
        </DialogButton>
      </Focusable>
    </div>
  </ModalRoot>
);

const SimilarSection: FC<{ title: string; method: string; params: Record<string, unknown>; reloadKey?: unknown; auctions: boolean }> = ({ title, method, params, reloadKey, auctions }) => {
  const st = useRpc<SimilarResult>(method, { ...params, auctions }, [reloadKey]);
  const d = st.data;
  return (
    <Section title={title} right={d ? <span style={{ color: colors.muted, fontSize: "12px" }}>{d.rows.length} listings</span> : undefined}>
      {st.error && !d && <ErrorText error={st.error} />}
      {!d && !st.error && <Loading />}
      {d && d.loaded === false && <Empty text="Listings load once the weapon is known." />}
      {d && d.loaded !== false && d.rows.length === 0 && <Empty text={d.error ? `warframe.market unavailable (${d.error})` : "No similar rivens found"} />}
      {d && <Paged items={d.rows} pageSize={10} render={(l) => <ListingRow key={l.id} l={l} />} />}
    </Section>
  );
};

const WeaponModal: FC<{ closeModal?: () => void }> = ({ closeModal }) => {
  const [q, setQ] = useState("");
  const list = useRpc<string[]>("riven.weapons", { query: q, limit: 24 }, []);
  const [err, setErr] = useState<string>();
  return (
    <ModalRoot closeModal={closeModal} onCancel={closeModal}>
      <div style={{ color: colors.text, minWidth: "480px" }}>
        <div style={{ fontWeight: 700, marginBottom: "6px" }}>Set the riven's weapon</div>
        <TextField label="Search" value={q} onChange={(e) => setQ(e.target.value)} />
        {err && <ErrorText error={err} />}
        <Focusable style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "4px", marginTop: "8px", maxHeight: "360px", overflowY: "auto" }}>
          {(list.data ?? []).map((n) => (
            <DialogButton
              key={n}
              style={{ minWidth: 0, padding: "4px 8px" }}
              onClick={async () => {
                try {
                  await call("riven.set_weapon", { name: n });
                  closeModal?.();
                } catch (e: any) {
                  setErr(String(e?.message ?? e));
                }
              }}
            >
              {n}
            </DialogButton>
          ))}
        </Focusable>
      </div>
    </ModalRoot>
  );
};

const LastResultCards: FC<{ r: LastResult; compact?: boolean }> = ({ r, compact }) => {
  const title = `${r.weapon ?? "Unknown weapon"}${r.layout === "reroll" && r.rollCount > 0 ? ` · ${r.rollCount} roll${r.rollCount === 1 ? "" : "s"}` : ""}`;
  const cards = (
    <>
      <Focusable focusClassName="gpfocus" onActivate={() => {}} style={{ width: "100%" }}>
        <CardHeader title="Kept" grade={r.left.grade} />
        <StatLines side={r.left} compact={compact} />
      </Focusable>
      {r.right && (
        <Focusable focusClassName="gpfocus" onActivate={() => {}} style={{ width: "100%" }}>
          <CardHeader title={r.rollCount ? `Last roll ${r.rollCount}` : "Last roll"} grade={r.right.grade} />
          <StatLines side={r.right} compact={compact} />
        </Focusable>
      )}
    </>
  );
  return (
    <div style={{ marginTop: "8px" }}>
      <div style={{ fontSize: "12px", color: colors.muted, marginBottom: "4px" }}>
        Last scan: {title} · {minutesAgo(r.at, Date.now())}
        {r.totalKuvaSpent > 0 ? ` · ${r.totalKuvaSpent.toLocaleString()} kuva` : ""}
      </div>
      {compact ? (
        <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>{cards}</div>
      ) : (
        <Focusable flow-children="horizontal" style={{ display: "grid", gridTemplateColumns: r.right ? "1fr 1fr" : "1fr", gap: "16px" }}>{cards}</Focusable>
      )}
    </div>
  );
};

const LiveSession: FC<{ auctions: boolean }> = ({ auctions }) => {
  const st = useRivenState();
  const s = st.data;
  if (st.error && !s) return <ErrorText error={st.error} />;
  if (!s) return <Loading />;
  const weapon = s.weapon ?? "Unknown weapon";
  return (
    <>
      <Section
        title={s.active ? `Riven scanner: ${weapon}` : "Riven scanner"}
        right={
          <Focusable flow-children="horizontal" style={{ display: "flex", gap: "6px" }}>
            <DialogButton style={chip(false)} onClick={() => scannerAction("riven.rescan")}>
              Rescan
            </DialogButton>
            <DialogButton style={chip(false)} onClick={() => showModal(<WeaponModal />)}>
              Weapon…
            </DialogButton>
            <DialogButton style={chip(false)} disabled={!s.active} onClick={() => scannerAction("riven.close")}>
              Close
            </DialogButton>
          </Focusable>
        }
      >
        {!s.active && <Empty text="Open a riven's Cycle screen (or a riven linked in chat) and the scan starts on its own. Rescan reads the card on screen now." />}
        {!s.active && s.lastResult && <LastResultCards r={s.lastResult} />}
        {s.active && (
          <>
            <div style={{ marginBottom: "8px" }}>
              <Stat label="Weapon" value={weapon} />
              <Stat label="Source" value={s.weaponSource ?? "–"} />
              {s.layout === "reroll" && <Stat label="Rolls" value={s.rollCount} />}
              {s.totalKuvaSpent > 0 && <Stat label="Kuva spent" value={s.totalKuvaSpent.toLocaleString()} />}
              {s.lastScan && <Stat label="Last scan" value={`${s.lastScan.kind} ${s.lastScan.elapsed_ms} ms`} />}
            </div>
            {s.weaponMissing && <div style={{ color: colors.warn, fontSize: "13px", marginBottom: "6px" }}>Couldn't read the weapon name, so there are no grades. Pick it with Weapon… or Rescan.</div>}
            <Focusable flow-children="horizontal" style={{ display: "grid", gridTemplateColumns: s.visible === "both" ? "1fr 1fr" : "1fr", gap: "16px" }}>
              <Focusable focusClassName="gpfocus" onActivate={() => {}}>
                <CardHeader title="Current" grade={s.left.grade} right={s.chosen === "left" ? <Badge text="kept" color={colors.good} /> : undefined} />
                <StatLines side={s.left} />
              </Focusable>
              {s.visible === "both" && (
                <Focusable focusClassName="gpfocus" onActivate={() => {}}>
                  <CardHeader title={s.rollCount ? `New roll ${s.rollCount}` : "New roll"} grade={s.right.grade} />
                  <StatLines side={s.right} />
                </Focusable>
              )}
            </Focusable>
            {s.best && (
              <div style={{ fontSize: "12px", marginTop: "8px", color: colors.muted }}>
                <div>
                  <span style={{ color: colors.good }}>Best: </span>
                  {s.best.positives.join(", ") || "–"}
                </div>
                <div>
                  <span style={{ color: colors.bad }}>Accepted negatives: </span>
                  {s.best.negatives.join(", ") || "–"}
                </div>
              </div>
            )}
          </>
        )}
      </Section>
      {/* Similar listings line up under their card: side by side when both cards are read. */}
      {s.active && (s.left.stats.length > 0 || s.right.stats.length > 0) && (
        <Focusable
          flow-children="horizontal"
          style={{ display: "grid", gridTemplateColumns: s.left.stats.length > 0 && s.right.stats.length > 0 ? "repeat(2, minmax(0, 1fr))" : "minmax(0, 1fr)", gap: "0 12px", alignItems: "start" }}
        >
          {s.left.stats.length > 0 && (
            <SimilarSection title="Similar on WFM (current)" method="riven.similar" params={{ side: "left" }} reloadKey={`${s.slug}|${s.listingsCount}|${s.left.stats.length}|${s.updatedAt > 0 ? s.left.stats.map((x) => x.name).join() : ""}`} auctions={auctions} />
          )}
          {s.right.stats.length > 0 && (
            <SimilarSection title={`Similar on WFM (roll ${s.rollCount})`} method="riven.similar" params={{ side: "right" }} reloadKey={`${s.slug}|${s.listingsCount}|${s.right.stats.map((x) => x.name).join()}`} auctions={auctions} />
          )}
        </Focusable>
      )}
    </>
  );
};

// ---- owned rivens ----------------------------------------------------------------------------------

const SORTS = [
  { data: "grade", label: "Best grade" },
  { data: "perfect", label: "Best rolls" },
  { data: "weapon", label: "Weapon" },
  { data: "rerolls", label: "Most cycled" },
];
let lastSort = "grade";

const OwnedModal: FC<{ r: OwnedRiven; auctions: boolean; closeModal?: () => void }> = ({ r, auctions, closeModal }) => {
  const [showSimilar, setShowSimilar] = useState(false);
  return (
    <ModalRoot closeModal={closeModal} onCancel={closeModal}>
      <div style={{ color: colors.text, minWidth: "640px" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "6px", marginBottom: "6px" }}>
          <span style={{ fontSize: "20px", fontWeight: 700 }}>{r.rivenName}</span>
          <GradeBadge grade={r.overallGrade} />
          <AttrBadge grade={r.attributeGrade} />
        </div>
        <div style={{ marginBottom: "8px" }}>
          <Stat label="Type" value={r.rivenType} />
          <Stat label="Rank" value={`${r.currentRank}/${r.maxRank}`} />
          <Stat label="MR" value={r.masteryReq} />
          <Stat label="Cycles" value={r.rerolls} />
          {r.disposition != null && <Stat label="Disposition" value={r.disposition} />}
          <Stat label="Rolls" value={`${Math.round(r.statPerfectness * 100)}%`} />
        </div>
        <Focusable style={{ display: "flex", flexDirection: "column" }}>
          {r.stats.map((s) => (
            <Focusable key={s.tag} focusClassName="gpfocus" onActivate={() => {}} style={{ padding: "3px 2px", borderBottom: `1px solid ${colors.border}` }}>
              <div style={{ display: "grid", gridTemplateColumns: "90px 90px 1fr 50px", gap: "8px", alignItems: "center" }}>
                <span style={{ fontWeight: 700, textAlign: "right" }}>{ownedValue(s, s.displayValue)}</span>
                <span style={{ color: colors.muted, fontSize: "12px" }}>max {ownedValue(s, s.maxRankValue)}</span>
                <span style={{ color: s.positive ? colors.text : "#f4a3a3" }}>{s.name}</span>
                <span style={{ textAlign: "right" }}>
                  <GradeBadge grade={s.grade} />
                </span>
              </div>
              <RollBar f={s.rollFloat} positive={s.positive} />
            </Focusable>
          ))}
        </Focusable>
        {showSimilar ? (
          <div style={{ maxHeight: "300px", overflowY: "auto" }}>
            <SimilarSection title="Similar on WFM" method="rivens.similar" params={{ id: r.itemId }} auctions={auctions} />
          </div>
        ) : (
          <Focusable flow-children="horizontal" style={{ display: "flex", justifyContent: "flex-end", gap: "8px", marginTop: "10px" }}>
            <DialogButton style={{ width: "200px" }} onClick={() => setShowSimilar(true)}>
              Similar on WFM
            </DialogButton>
            <DialogButton style={{ width: "120px" }} onClick={closeModal}>
              Close
            </DialogButton>
          </Focusable>
        )}
      </div>
    </ModalRoot>
  );
};

const OwnedCard: FC<{ r: OwnedRiven; auctions: boolean }> = ({ r, auctions }) => {
  const open = () => showModal(<OwnedModal r={r} auctions={auctions} />);
  return (
    <Focusable
      focusClassName="gpfocus"
      onActivate={open}
      onClick={open}
      style={{ background: colors.panel, border: `1px solid ${colors.border}`, borderRadius: "6px", padding: "8px 10px", minWidth: 0 }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: "4px" }}>
        <span style={{ fontWeight: 700, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1 }}>{r.rivenName}</span>
        <GradeBadge grade={r.overallGrade} />
        <AttrBadge grade={r.attributeGrade} />
      </div>
      <div style={{ fontSize: "12px", color: colors.muted }}>
        {r.rivenType} · rank {r.currentRank}/{r.maxRank} · {r.rerolls} cycles
      </div>
      {r.stats.map((s) => (
        <div key={s.tag} style={{ display: "flex", gap: "6px", fontSize: "13px" }}>
          <span style={{ minWidth: "64px", textAlign: "right", fontWeight: 600 }}>{ownedValue(s, s.maxRankValue)}</span>
          <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: s.positive ? colors.text : "#f4a3a3" }}>{s.name}</span>
          <span style={{ color: GRADE_COLOR[s.grade] ?? colors.muted, fontWeight: 700 }}>{s.grade}</span>
        </div>
      ))}
    </Focusable>
  );
};

const OwnedRivens: FC<{ auctions: boolean }> = ({ auctions }) => {
  const [sort, setSort] = useState(lastSort);
  const st = useRpc<OwnedResult>("rivens.owned", { sort }, []);
  useEvent("rivens.updated", () => st.reload());
  const d = st.data;
  const groups = new Map<string, number>();
  for (const v of d?.veiled ?? []) {
    const k = `${v.label}: ${v.challengeGroup ?? "Unknown challenge"}`;
    groups.set(k, (groups.get(k) ?? 0) + 1);
  }
  return (
    <>
      <Section
        title={d ? `Owned rivens (${d.total})` : "Owned rivens"}
        right={
          <Focusable style={{ display: "flex", alignItems: "center", gap: "8px" }}>
            <span style={{ fontSize: "11px", color: colors.muted, textTransform: "uppercase" }}>Sort</span>
            <div style={{ width: "180px" }}>
              <Dropdown
                rgOptions={SORTS}
                selectedOption={sort}
                onChange={(o) => {
                  lastSort = o.data;
                  setSort(o.data);
                }}
              />
            </div>
          </Focusable>
        }
      >
        {st.error && !d && <ErrorText error={st.error} />}
        {!d && !st.error && <Loading />}
        {d && !d.inventory && <Empty text="No inventory yet. Sync it from the Inventory page." />}
        {d && d.inventory && d.rivens.length === 0 && <Empty text="No unveiled rivens in the inventory." />}
        {d && d.data.weapons === 0 && <ErrorText error="Riven data is missing (run scripts/fetch-data.sh); rivens cannot be graded." />}
        {d && d.rivens.length > 0 && (
          <Grid items={d.rivens} cols={3} rows={4} label={`${d.rivens.length} rivens`} render={(r) => <OwnedCard key={r.itemId} r={r} auctions={auctions} />} />
        )}
      </Section>
      {d && (groups.size > 0 || d.veiledUnseen.length > 0) && (
        <Section title={`Veiled rivens (${d.veiled.length + d.veiledUnseen.reduce((a, b) => a + b.count, 0)})`}>
          {[...groups.entries()].map(([k, n]) => (
            <Row key={k} label={k} value={<span style={{ color: colors.muted }}>×{n}</span>} />
          ))}
          {d.veiledUnseen.map((v) => (
            <Row key={`u-${v.label}`} label={`${v.label}: not yet unveiled`} value={<span style={{ color: colors.muted }}>×{v.count}</span>} />
          ))}
        </Section>
      )}
    </>
  );
};

// ---- settings and data --------------------------------------------------------------------------

const UI_SCALES = [0.5, 0.6, 0.7, 0.8, 0.9, 0.99, 1.0].map((v) => ({ data: v, label: v === 0.99 ? "100% (default)" : `${Math.round(v * 100)}%` }));

const ScannerSettingsSection: FC<{ settings: ScannerSettings | undefined; reload: () => void }> = ({ settings, reload }) => {
  const rs = settings?.riven_scanner ?? {};
  const status = useRpc<ScannerStatus>("riven.status", {}, []);
  const [busy, setBusy] = useState<string>();
  const st = status.data;
  const set = async (patch: Record<string, unknown>) => {
    await patchScanner(patch);
    reload();
    status.reload();
  };
  return (
    <Section title="Scanner settings">
      {/* Two settings per row: full-width rows leave the label and its switch 1200px apart. */}
      <Grid
        cols={2}
        gap="0 32px"
        rowGap="0"
        render={(el) => el}
        items={[
            <ToggleField key="en" label="Riven overlay" description="Scan the Cycle screen and chat-linked rivens, and show the grade panels." checked={rs.enabled !== false} onChange={(b) => set({ enabled: b })} />,
            <ToggleField key="auc" label="Include auctions in similar listings" checked={rs.auctions_shown !== false} onChange={(b) => set({ auctions_shown: b })} />,
            <ToggleField key="arr" label="Better / worse marks on the new roll" checked={rs.compare_arrows !== false} onChange={(b) => set({ compare_arrows: b })} />,
            <ToggleField key="dbg" label="Save scan crops for debugging" checked={rs.debug_dumps !== false} onChange={(b) => set({ debug_dumps: b })} />,
            <DropdownItem
              key="canvas"
              label="Menu area"
              description="Where Warframe draws its menus on the 16:10 screen. Switch if the scans read nothing."
              rgOptions={[
                { data: "16:9", label: "16:9" },
                { data: "full", label: "Full" },
              ]}
              selectedOption={rs.canvas ?? "16:9"}
              onChange={(o) => set({ canvas: o.data })}
            />,
            <Focusable key="scale" style={{ display: "flex", flexDirection: "column" }}>
              <ToggleField label="Interface scale from EE.cfg" checked={rs.ui_scale_auto !== false} onChange={(b) => set({ ui_scale_auto: b })} />
              {rs.ui_scale_auto === false && (
                <DropdownItem label="Interface scale" rgOptions={UI_SCALES} selectedOption={rs.ui_scale ?? 0.99} onChange={(o) => set({ ui_scale: o.data })} />
              )}
            </Focusable>,
        ]}
      />
      {st && (
        <div style={{ fontSize: "12px", color: colors.muted, marginTop: "6px" }}>
          OCR {st.ocr_available ? "ready" : "models missing"} · overlay {st.overlay.connected ? "connected" : st.overlay.headless ? "headless" : "not connected (toasts)"} · real-time log{" "}
          {st.realtime_active ? "on" : "off (add PROTON_LOG=1 %command% to Warframe's launch options for faster scans)"} · UI scale {st.ui_scale}
          {st.last_capture_ms != null ? ` · capture ${st.last_capture_ms} ms` : ""}
        </div>
      )}
      {st && (
        <div style={{ fontSize: "12px", color: colors.muted }}>
          Riven data {st.data.version ?? "missing"} ({st.data.weapons} weapons) · good-roll sheet {st.goodrolls.rows} weapons
          {st.goodrolls.updatedAt ? `, ${new Date(st.goodrolls.updatedAt).toLocaleDateString()}` : ""}
          {st.goodrolls.stale ? " (stale)" : ""}
          {st.goodrolls.error ? ` · ${st.goodrolls.error}` : ""}
        </div>
      )}
      <Focusable flow-children="horizontal" style={{ display: "flex", gap: "8px", marginTop: "8px" }}>
        <DialogButton
          style={chip(false)}
          disabled={!!busy}
          onClick={async () => {
            setBusy("sheet");
            try {
              await call("riven.goodrolls_refresh");
            } finally {
              setBusy(undefined);
              status.reload();
            }
          }}
        >
          {busy === "sheet" ? "Refreshing…" : "Refresh good-roll sheet"}
        </DialogButton>
        <DialogButton
          style={chip(false)}
          disabled={!!busy}
          onClick={async () => {
            setBusy("data");
            try {
              await call("riven.data_refresh", { force: false });
            } finally {
              setBusy(undefined);
              status.reload();
            }
          }}
        >
          {busy === "data" ? "Updating…" : "Update riven data"}
        </DialogButton>
      </Focusable>
    </Section>
  );
};

// ---- page --------------------------------------------------------------------------------------------

const Rivens: FC = () => {
  const settings = useRpc<ScannerSettings>("settings.get", {}, []);
  const auctions = settings.data?.riven_scanner?.auctions_shown !== false;
  return (
    <Page>
      <LiveSession auctions={auctions} />
      <OwnedRivens auctions={auctions} />
      <ScannerSettingsSection settings={settings.data} reload={settings.reload} />
    </Page>
  );
};

export default Rivens;

// ---- QAM section ----------------------------------------------------------------------------------

export const Qam: FC = () => {
  const st = useRivenState();
  const s = st.data;
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 30000);
    return () => clearInterval(t);
  }, []);
  const age = s?.lastScan ? Math.round((now - s.lastScan.at) / 60000) : null;
  return (
    <PanelSection title="Riven scanner">
      {st.error && !s && (
        <PanelSectionRow>
          <Field label="Error" description={st.error} />
        </PanelSectionRow>
      )}
      {s && !s.active && !s.lastResult && (
        <PanelSectionRow>
          <Field label="No riven on screen" description="Open a riven's Cycle screen or a chat link; Rescan reads the card on screen now." focusable />
        </PanelSectionRow>
      )}
      {s && !s.active && s.lastResult && (
        <PanelSectionRow>
          <LastResultCards r={s.lastResult} compact />
        </PanelSectionRow>
      )}
      {s && s.active && (
        <>
          <PanelSectionRow>
            <Field
              focusable
              label={
                <span>
                  {s.weapon ?? "Unknown weapon"}
                  {s.layout === "reroll" && s.rollCount > 0 && <span style={{ color: colors.muted, fontSize: "12px" }}> · roll {s.rollCount}</span>}
                </span>
              }
              description={age != null ? `Last scan ${age < 1 ? "just now" : `${age} min ago`}` : undefined}
              bottomSeparator="none"
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <div style={{ width: "100%" }}>
              <CardHeader title="Current" grade={s.left.grade} />
              <StatLines side={s.left} compact />
            </div>
          </PanelSectionRow>
          {s.visible === "both" && (s.right.stats.length > 0 || s.right.status || s.right.error) && (
            <PanelSectionRow>
              <div style={{ width: "100%" }}>
                <CardHeader title={s.rollCount ? `New roll ${s.rollCount}` : "New roll"} grade={s.right.grade} />
                <StatLines side={s.right} compact />
              </div>
            </PanelSectionRow>
          )}
        </>
      )}
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={() => scannerAction("riven.rescan")}>
          Rescan riven
        </ButtonItem>
      </PanelSectionRow>
      {s?.active && (
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => scannerAction("riven.close")}>
            Close riven overlay
          </ButtonItem>
        </PanelSectionRow>
      )}
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          onClick={() => {
            openTab("rivens");
            Navigation.Navigate(ROUTE);
            Navigation.CloseSideMenus();
          }}
        >
          Open Rivens
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
};

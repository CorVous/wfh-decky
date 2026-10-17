// World: cycles, fissures (incl. Steel Path and Void Storms), sortie/archon, invasions, Nightwave,
// Baro/Varzia/Darvo, Steel Path honors + Circuit, the arbitration schedule and a dailies tracker
// (docs/port/menus.md §10). Data: world.view / world.qam / world.dailies (py_modules/wfh/menus/world.py),
// world.arbi_schedule (data/arbi_schedule.py). Every date comes as epoch ms; countdowns tick locally.
import { ButtonItem, DialogButton, DropdownItem, Field, Focusable, Navigation, PanelSection, PanelSectionRow, SliderField } from "@decky/ui";
import { ComponentProps, CSSProperties, FC, ReactNode, useEffect, useRef, useState } from "react";
import { FaBell, FaRegBell, FaRegStar, FaStar } from "react-icons/fa";

import { Badge, Ducats, Empty, ErrorText, Guard, Loading, Page, Paged, Row, Section, colors } from "../components/ui";
import { openTab } from "../nav";
import { ROUTE } from "../nav";
import { call, useEvent, useRpc } from "../rpc";

// ---- types (mirror menus/world.py) ---------------------------------------------------------------

export interface Timer {
  key: string;
  label: string;
  expiryMs: number | null;
  activationMs: number | null;
  urgent?: boolean;
}

export interface Cycle {
  key: string;
  label: string;
  state: string;
  next: string | null;
  expiryMs: number;
  activationMs: number | null;
  urgent: boolean;
}

export interface Fissure {
  id: string;
  tier: string;
  tierCls: string;
  missionType: string;
  node: string;
  nodeId: string;
  planet: string;
  isHard: boolean;
  isStorm: boolean;
  sourceMode: "normal" | "steel" | "railjack";
  expiryMs: number;
  activationMs: number | null;
}

export type FissureMode = "all" | "normal" | "steel" | "railjack";
export type TierCounts = Record<string, number> & { total: number };

export interface WorldPrefs {
  fissureMode: FissureMode;
  fissureTiers: string[];
  qamLimit: number;
}

export interface Baro {
  active: boolean;
  location: string;
  activationMs: number | null;
  expiryMs: number | null;
  targetMs: number | null;
  itemCount: number;
  inventory?: { item: string; uniqueName: string; ducats?: number; credits?: number }[];
}

interface SortieLike {
  boss: string;
  expiryMs: number | null;
  missions: { node: string; mission: string; modifier?: string }[];
}

interface Invasion {
  id: string;
  node: string;
  attacker: string;
  defender: string;
  attackerReward: string;
  defenderReward: string;
  vsInfestation: boolean;
  completion: number;
}

interface Alert {
  id: string;
  node: string;
  mission: string;
  faction: string;
  minLevel: number;
  maxLevel: number;
  credits: number;
  items: { name: string; count: number }[];
  expiryMs: number | null;
}

interface NightwaveAct {
  id: string;
  title: string;
  description: string;
  standing: number | null;
  kind: "daily" | "weekly" | "elite";
  expiryMs: number | null;
}

interface CircuitCat {
  choices: string[];
  next: string[][];
  known: boolean;
}

export interface Circuit {
  normal: CircuitCat;
  hard: CircuitCat;
}

interface WorldView {
  now: number;
  fetchedAt: number | null;
  stale: boolean;
  error: string | null;
  refreshing: boolean;
  timers: Timer[];
  cycles: Cycle[];
  fissures: Fissure[];
  fissureCounts: Record<FissureMode, TierCounts>;
  sortie: SortieLike | null;
  archon: SortieLike | null;
  invasions: Invasion[];
  alerts: Alert[];
  nightwave: { season: number; phase: number; expiryMs: number | null; challenges: NightwaveAct[] } | null;
  baro: Baro | null;
  varzia: { active: boolean; expiryMs: number | null; inventory: { item: string; aya?: number; regalAya?: number }[] } | null;
  darvo: { item: string; discount: number; originalPrice: number; salePrice: number; total: number; sold: number; expiryMs: number | null }[];
  steelPath: {
    current: { name: string; cost: number } | null;
    expiryMs: number | null;
    upcoming: { name: string; cost: number; inDays: number | null; activationMs: number | null }[];
    evergreens: { name: string; cost: number }[];
  } | null;
  circuit: Circuit;
  prefs: WorldPrefs;
}

export interface ArbiEntry {
  epochMs: number;
  nodeId: string;
  node: string;
  mission: string;
  faction: string;
  key: string;
}

interface ArbiSchedule {
  entries: ArbiEntry[];
  fetchedAt: number | null;
  stale: boolean;
  error: string | null;
  alerts: { occurrences: string[]; favoriteNodes: string[]; minutesBefore: number };
  current: ArbiEntry | null;
  next: ArbiEntry | null;
  now: number;
}

interface DailyTask {
  id: string;
  label: string;
  group: "daily" | "weekly" | "vendors";
  target: number;
  count: number;
  manual: number;
  auto: boolean;
  detail: string | null;
  done: boolean;
  hidden: boolean;
  resetMs: number | null;
}

interface Dailies {
  now: number;
  tasks: DailyTask[];
  inventory: boolean;
  doneCount: number;
  visibleCount: number;
}

// ---- shared helpers (Dashboard imports these) -----------------------------------------------------

export const TIERS = ["Lith", "Meso", "Neo", "Axi", "Requiem", "Omnia"];
export const TIER_COLORS: Record<string, string> = {
  lith: "#c8a27a",
  meso: "#9fb4c8",
  neo: "#d6d6e0",
  axi: "#f0cf6a",
  requiem: "#e07070",
  omnia: "#b48cf0",
};
export const MODE_LABELS: Record<FissureMode, string> = {
  normal: "Normal",
  steel: "Steel Path",
  railjack: "Void Storms",
  all: "All",
};
export const MODES: FissureMode[] = ["normal", "steel", "railjack", "all"];
const FACTION_COLORS: Record<string, string> = {
  Grineer: "#e0905a",
  Corpus: "#6eaaff",
  Infested: "#7fd67f",
  Corrupted: "#f0cf6a",
  Orokin: "#f0cf6a",
  Murmur: "#b48cf0",
  Narmer: "#e07070",
};

// A ticking clock; every countdown re-renders from it.
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(t);
  }, [intervalMs]);
  return now;
}

const pad = (n: number) => String(n).padStart(2, "0");

// format.ts countdown: > 24 h "Dd Hh", else "Hh Mm Ss"; <= 0 "Refreshing...".
export function fmtCountdown(toMs: number | null | undefined, now: number): string {
  if (!toMs) return "–";
  let s = Math.floor((toMs - now) / 1000);
  if (s <= 0) return "Refreshing...";
  const d = Math.floor(s / 86400);
  s -= d * 86400;
  const h = Math.floor(s / 3600);
  s -= h * 3600;
  const m = Math.floor(s / 60);
  if (d > 0) return `${d}d ${h}h`;
  return h > 0 ? `${h}h ${pad(m)}m ${pad(s % 60)}s` : `${m}m ${pad(s % 60)}s`;
}

// Compact form for the QAM: "2d 6h", "6h 12m", "12m 03s".
export function fmtShort(toMs: number | null | undefined, now: number): string {
  if (!toMs) return "–";
  let s = Math.floor((toMs - now) / 1000);
  if (s <= 0) return "now";
  const d = Math.floor(s / 86400);
  s -= d * 86400;
  const h = Math.floor(s / 3600);
  s -= h * 3600;
  const m = Math.floor(s / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${pad(m)}m`;
  return `${m}m ${pad(s % 60)}s`;
}

export function isUrgent(activationMs: number | null | undefined, expiryMs: number | null | undefined, now: number): boolean {
  if (!expiryMs || !activationMs || expiryMs <= activationMs) return false;
  return (expiryMs - now) / (expiryMs - activationMs) < 0.2;
}

export function fmtClock(ms: number): string {
  const d = new Date(ms);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function fmtDay(ms: number): string {
  return new Date(ms).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

export const TierTag: FC<{ tier: string }> = ({ tier }) => (
  <span style={{ color: TIER_COLORS[tier.toLowerCase()] ?? colors.text, fontWeight: 600 }}>{tier}</span>
);

export const FissureLabel: FC<{ f: Fissure }> = ({ f }) => (
  <>
    <TierTag tier={f.tier} /> {f.missionType}
    {f.isHard && <Badge text="SP" color={colors.bad} />}
    {f.isStorm && <Badge text="Storm" color={colors.accent} />}
  </>
);

export const Countdown: FC<{ to: number | null | undefined; now: number; activation?: number | null; short?: boolean }> = ({ to, now, activation, short }) => (
  <span style={{ color: isUrgent(activation, to, now) ? colors.warn : colors.text, fontVariantNumeric: "tabular-nums" }}>
    {short ? fmtShort(to, now) : fmtCountdown(to, now)}
  </span>
);

export function cycleText(c: Cycle, now: number): string {
  return `${c.state} · ${fmtShort(c.expiryMs, now)}`;
}

// Opens the full-screen page on a tab (from the QAM). FullPage only reads the requested tab when it
// mounts, so this cannot switch tabs while the full page is already showing another tab: in-page
// rows therefore never link to other FullPage tabs (only World's own sub-tabs, via openWorld).
export function openPage(tab: string): void {
  openTab(tab);
  Navigation.CloseSideMenus();
  Navigation.Navigate(ROUTE);
}

let lastWorldTab: WorldTab = "overview";
const worldTabListeners = new Set<(t: WorldTab) => void>();

// Opens World on a sub-tab; a mounted World page switches to it immediately.
export function openWorld(tab: WorldTab = "overview"): void {
  lastWorldTab = tab;
  worldTabListeners.forEach((l) => l(tab));
  openPage("world");
}

// Fissure filters saved from the QAM reach a mounted World page right away.
const prefsListeners = new Set<(p: WorldPrefs) => void>();

function prefsSaved(p: WorldPrefs): void {
  prefsListeners.forEach((l) => l(p));
}

// A focus target for display-only rows: Steam's nav tree skips a Focusable with no onActivate,
// which would leave long lists unreachable / unscrollable with the D-pad.
export const noop = (): void => {};

export const Item: FC<ComponentProps<typeof Row>> = (p) => <Row {...p} onActivate={p.onActivate ?? noop} />;

// Gamepad tab strip: one row of buttons, the active one highlighted.
export function Segmented<T extends string>({ value, options, onChange, style }: {
  value: T;
  options: { id: T; label: ReactNode }[];
  onChange: (v: T) => void;
  style?: CSSProperties;
}) {
  return (
    <Focusable flow-children="horizontal" style={{ display: "flex", gap: "6px", flexWrap: "wrap", ...style }}>
      {options.map((o) => (
        <DialogButton
          key={o.id}
          onClick={() => onChange(o.id)}
          style={{
            minWidth: 0,
            width: "auto",
            padding: "6px 14px",
            flex: "0 0 auto",
            background: o.id === value ? colors.accent : undefined,
            color: o.id === value ? "#0e121e" : undefined,
            fontWeight: o.id === value ? 700 : 400,
          }}
        >
          {o.label}
        </DialogButton>
      ))}
    </Focusable>
  );
}

const Grid: FC<{ cols?: number; children: ReactNode }> = ({ cols = 3, children }) => (
  <div style={{ display: "grid", gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`, gap: "0 14px", alignItems: "start" }}>{children}</div>
);

const Muted: FC<{ children: ReactNode }> = ({ children }) => <span style={{ color: colors.muted }}>{children}</span>;

const StatusLine: FC<{ v: { stale: boolean; error: string | null; refreshing?: boolean; fetchedAt?: number | null } }> = ({ v }) => {
  if (!v.stale && !v.refreshing) return null;
  const text = v.refreshing
    ? "Refreshing world state…"
    : v.fetchedAt
      ? `Offline: showing data from ${fmtClock(v.fetchedAt)}; clocks are computed locally.`
      : "World state unavailable; only clock-based timers are shown.";
  return (
    <div style={{ color: v.stale ? colors.warn : colors.muted, fontSize: "13px", margin: "6px 0" }}>
      {text}
      {v.error && <span style={{ color: colors.muted }}> ({v.error})</span>}
    </div>
  );
};

// ---- sections ---------------------------------------------------------------------------------

export const TimersSection: FC<{ timers: Timer[]; now: number }> = ({ timers, now }) => (
  <Section title="Resets and timers">
    {timers.map((t) => (
      <Item key={t.key} label={t.label} value={<Countdown to={t.expiryMs} activation={t.activationMs} now={now} />} />
    ))}
  </Section>
);

export const CyclesSection: FC<{ cycles: Cycle[]; now: number }> = ({ cycles, now }) => (
  <Section title="Cycles">
    {cycles.length === 0 && <Muted>No cycle data yet.</Muted>}
    {cycles.map((c) => (
      <Item
        key={c.key}
        label={
          <>
            {c.label} <span style={{ color: colors.accent }}>{c.state}</span>
          </>
        }
        sub={c.next ? `then ${c.next}` : undefined}
        value={<Countdown to={c.expiryMs} activation={c.activationMs} now={now} />}
      />
    ))}
  </Section>
);

const CountsTable: FC<{ counts: Record<FissureMode, TierCounts>; onPick?: (m: FissureMode) => void }> = ({ counts, onPick }) => (
  <div>
    <div style={{ display: "grid", gridTemplateColumns: "110px repeat(6, 1fr) 40px", fontSize: "12px", color: colors.muted, padding: "0 2px" }}>
      <span />
      {TIERS.map((t) => (
        <span key={t} style={{ textAlign: "center" }}>
          {t.slice(0, 3)}
        </span>
      ))}
      <span style={{ textAlign: "right" }}>All</span>
    </div>
    {(["normal", "steel", "railjack"] as FissureMode[]).map((m) => (
      <Focusable
        key={m}
        onActivate={onPick ? () => onPick(m) : undefined}
        focusClassName="gpfocus"
        style={{ display: "grid", gridTemplateColumns: "110px repeat(6, 1fr) 40px", padding: "4px 2px", borderBottom: `1px solid ${colors.border}` }}
      >
        <span>{MODE_LABELS[m]}</span>
        {TIERS.map((t) => (
          <span key={t} style={{ textAlign: "center", color: counts[m][t] ? TIER_COLORS[t.toLowerCase()] : colors.muted }}>
            {counts[m][t] || "·"}
          </span>
        ))}
        <span style={{ textAlign: "right", fontWeight: 600 }}>{counts[m].total}</span>
      </Focusable>
    ))}
  </div>
);

export const CircuitBlock: FC<{ circuit: Circuit; showNext?: boolean }> = ({ circuit, showNext }) => (
  <>
    {(["normal", "hard"] as const).map((k) => {
      const c = circuit[k];
      return (
        <div key={k}>
          <Item label={k === "normal" ? "Circuit (Normal)" : "Circuit (Steel Path)"} sub={c.choices.length ? c.choices.join(", ") : "Not known yet"} />
          {showNext &&
            c.next.map((w, i) => (
              <Item key={i} label={<Muted>{i === 0 ? "Next week" : `In ${i + 1} weeks`}</Muted>} sub={w.join(", ")} />
            ))}
        </div>
      );
    })}
  </>
);

export const BaroRow: FC<{ baro: Baro | null; now: number; onActivate?: () => void }> = ({ baro, now, onActivate }) =>
  baro ? (
    <Item
      onActivate={onActivate}
      label={baro.active ? `Baro is at ${baro.location}` : `Baro arrives at ${baro.location}`}
      sub={baro.active ? `${baro.itemCount} items · leaves in ${fmtCountdown(baro.expiryMs, now)}` : `in ${fmtCountdown(baro.activationMs, now)}`}
      value={<Countdown to={baro.targetMs} now={now} />}
      color={baro.active ? colors.ducat : undefined}
    />
  ) : (
    <Item label="Baro Ki'Teer" sub="No schedule in the world state" />
  );

const Overview: FC<{ v: WorldView; now: number; go: (t: WorldTab) => void; setPrefs: (p: Partial<WorldPrefs>) => void; arbi?: ArbiSchedule }> = ({
  v,
  now,
  go,
  setPrefs,
  arbi,
}) => (
  <Grid>
    <div>
      <TimersSection timers={v.timers} now={now} />
    </div>
    <div>
      <CyclesSection cycles={v.cycles} now={now} />
      <Section title="Fissures" right={<Muted>A to open</Muted>}>
        <CountsTable
          counts={v.fissureCounts}
          onPick={(m) => {
            setPrefs({ fissureMode: m });
            go("fissures");
          }}
        />
      </Section>
    </div>
    <div>
      <Section title="Arbitration">
        {arbi?.current && (
          <Item label={arbi.current.node} sub={`${arbi.current.mission} · ${arbi.current.faction} · now`} value={<Countdown to={arbi.current.epochMs + 3600000} now={now} />} onActivate={() => go("arbis")} />
        )}
        {arbi?.next && (
          <Item label={`Next: ${arbi.next.node}`} sub={`${arbi.next.mission} · ${arbi.next.faction} · ${fmtClock(arbi.next.epochMs)}`} value={<Countdown to={arbi.next.epochMs} now={now} />} onActivate={() => go("arbis")} />
        )}
        {!arbi?.current && !arbi?.next && <Muted>{arbi?.error ? "Schedule unavailable" : "No schedule yet"}</Muted>}
      </Section>
      <Section title="Baro Ki'Teer">
        <BaroRow baro={v.baro} now={now} onActivate={() => go("baro")} />
      </Section>
      <Section title="Steel Path honors">
        {v.steelPath?.current ? (
          <Item label={v.steelPath.current.name} sub={`${v.steelPath.current.cost} Steel Essence`} value={<Countdown to={v.steelPath.expiryMs} now={now} />} onActivate={() => go("steel")} />
        ) : (
          <Muted>Unknown</Muted>
        )}
        <CircuitBlock circuit={v.circuit} />
      </Section>
    </div>
  </Grid>
);

const FissuresTab: FC<{ v: WorldView; now: number; prefs: WorldPrefs; setPrefs: (p: Partial<WorldPrefs>) => void }> = ({ v, now, prefs, setPrefs }) => {
  const tiers = new Set(prefs.fissureTiers);
  const rows = v.fissures.filter((f) => modeMatches(f, prefs.fissureMode) && tiers.has(f.tier));
  const groups = TIERS.map((t) => ({ tier: t, rows: rows.filter((f) => f.tier === t) })).filter((g) => g.rows.length > 0);
  const toggleTier = (t: string) => {
    const next = tiers.has(t) ? prefs.fissureTiers.filter((x) => x !== t) : TIERS.filter((x) => x === t || tiers.has(x));
    setPrefs({ fissureTiers: next });
  };
  return (
    <>
      <Segmented
        value={prefs.fissureMode}
        options={MODES.map((m) => ({ id: m, label: `${MODE_LABELS[m]} (${v.fissureCounts[m].total})` }))}
        onChange={(m) => setPrefs({ fissureMode: m })}
        style={{ margin: "8px 0" }}
      />
      <Focusable flow-children="horizontal" style={{ display: "flex", gap: "6px", flexWrap: "wrap", marginBottom: "4px" }}>
        {TIERS.map((t) => (
          <DialogButton
            key={t}
            onClick={() => toggleTier(t)}
            style={{
              minWidth: 0,
              width: "auto",
              padding: "4px 12px",
              flex: "0 0 auto",
              opacity: tiers.has(t) ? 1 : 0.45,
              borderBottom: `3px solid ${tiers.has(t) ? TIER_COLORS[t.toLowerCase()] : "transparent"}`,
            }}
          >
            {t} {v.fissureCounts[prefs.fissureMode][t] ?? 0}
          </DialogButton>
        ))}
      </Focusable>
      <div style={{ maxWidth: "420px" }}>
        <SliderField
          label="Quick Access fissure rows"
          value={prefs.qamLimit}
          min={1}
          max={20}
          step={1}
          showValue
          onChange={(n) => setPrefs({ qamLimit: n })}
        />
      </div>
      {groups.length === 0 && <Empty text="No fissures match these filters." />}
      <Grid cols={2}>
        {groups.map((g) => (
          <Section key={g.tier} title={`${g.tier} (${g.rows.length})`}>
            {g.rows.map((f) => (
              <Item key={f.id} label={<FissureLabel f={f} />} sub={f.node} value={<Countdown to={f.expiryMs} activation={f.activationMs} now={now} />} />
            ))}
          </Section>
        ))}
      </Grid>
    </>
  );
};

export function modeMatches(f: Fissure, mode: FissureMode): boolean {
  if (mode === "railjack") return f.isStorm;
  if (f.isStorm) return false;
  if (mode === "steel") return f.isHard;
  if (mode === "normal") return !f.isHard;
  return true;
}

const SortieSection: FC<{ title: string; s: SortieLike | null; now: number }> = ({ title, s, now }) => (
  <Section title={title} right={s ? <Countdown to={s.expiryMs} now={now} /> : undefined}>
    {!s && <Muted>Not in the world state.</Muted>}
    {s && (
      <>
        <div style={{ color: colors.muted, marginBottom: "4px" }}>{s.boss}</div>
        {s.missions.map((m, i) => (
          <Item key={i} label={`${i + 1}. ${m.mission}`} sub={m.modifier ? `${m.node} · ${m.modifier}` : m.node} />
        ))}
      </>
    )}
  </Section>
);

const InvasionRow: FC<{ i: Invasion }> = ({ i }) => {
  const pct = Math.max(0, Math.min(100, i.completion));
  return (
    <Focusable onActivate={noop} focusClassName="gpfocus" style={{ padding: "6px 2px", borderBottom: `1px solid ${colors.border}` }}>
      <div style={{ display: "flex", justifyContent: "space-between" }}>
        <span>{i.node}</span>
        <Muted>{pct.toFixed(1)}%</Muted>
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: "13px" }}>
        <span style={{ color: FACTION_COLORS[i.attacker] ?? colors.text }}>
          {i.attacker}
          {i.attackerReward && `: ${i.attackerReward}`}
        </span>
        <span style={{ color: FACTION_COLORS[i.defender] ?? colors.text, textAlign: "right" }}>
          {i.defender}: {i.defenderReward || "–"}
        </span>
      </div>
      <div style={{ height: "4px", background: FACTION_COLORS[i.defender] ?? colors.border, borderRadius: "2px", marginTop: "3px" }}>
        <div style={{ width: `${pct}%`, height: "100%", background: FACTION_COLORS[i.attacker] ?? colors.accent, borderRadius: "2px" }} />
      </div>
    </Focusable>
  );
};

const MissionsTab: FC<{ v: WorldView; now: number }> = ({ v, now }) => (
  <Grid>
    <div>
      <SortieSection title="Sortie" s={v.sortie} now={now} />
      <SortieSection title="Archon Hunt" s={v.archon} now={now} />
    </div>
    <div>
      <Section title={`Invasions (${v.invasions.length})`}>
        {v.invasions.length === 0 && <Muted>No running invasions.</Muted>}
        {v.invasions.map((i) => (
          <InvasionRow key={i.id} i={i} />
        ))}
      </Section>
    </div>
    <div>
      <Section title={`Alerts (${v.alerts.length})`}>
        {v.alerts.length === 0 && <Muted>No alerts.</Muted>}
        {v.alerts.map((a) => (
          <Item
            key={a.id}
            label={`${a.mission} · ${a.node}`}
            sub={[a.items.map((x) => (x.count > 1 ? `${x.count}x ${x.name}` : x.name)).join(", "), a.credits ? `${a.credits.toLocaleString()} cr` : "", `${a.faction} ${a.minLevel}-${a.maxLevel}`].filter(Boolean).join(" · ")}
            value={<Countdown to={a.expiryMs} now={now} />}
          />
        ))}
      </Section>
    </div>
  </Grid>
);

const NightwaveTab: FC<{ v: WorldView; now: number }> = ({ v, now }) => {
  const nw = v.nightwave;
  if (!nw) return <Empty text="No Nightwave season in the world state." />;
  const kinds: { id: NightwaveAct["kind"]; title: string }[] = [
    { id: "daily", title: "Daily" },
    { id: "weekly", title: "Weekly" },
    { id: "elite", title: "Elite weekly" },
  ];
  return (
    <>
      <div style={{ margin: "8px 0", color: colors.muted }}>
        Season {nw.season}
        {nw.expiryMs && ` · ends in ${fmtCountdown(nw.expiryMs, now)}`}
      </div>
      <Grid>
        {kinds.map((k) => {
          const acts = nw.challenges.filter((c) => c.kind === k.id);
          return (
            <Section key={k.id} title={`${k.title} (${acts.length})`}>
              {acts.length === 0 && <Muted>None</Muted>}
              {acts.map((c) => (
                <Item
                  key={c.id}
                  label={c.title}
                  sub={c.description}
                  value={
                    <div>
                      {c.standing != null && <div style={{ color: colors.good }}>{c.standing.toLocaleString()}</div>}
                      <div style={{ fontSize: "12px" }}>
                        <Countdown to={c.expiryMs} now={now} short />
                      </div>
                    </div>
                  }
                />
              ))}
            </Section>
          );
        })}
      </Grid>
    </>
  );
};

const BaroTab: FC<{ v: WorldView; now: number }> = ({ v, now }) => (
  <Grid cols={2}>
    <div>
      <Section title="Baro Ki'Teer">
        <BaroRow baro={v.baro} now={now} />
        {v.baro?.active && v.baro.inventory && (
          <Paged
            items={v.baro.inventory}
            pageSize={14}
            render={(it, i) => (
              <Item
                key={i}
                label={it.item}
                value={
                  <span>
                    <Ducats value={it.ducats ?? null} />
                    {it.credits != null && <span style={{ color: colors.muted, marginLeft: "8px" }}>{Math.round(it.credits / 1000)}k cr</span>}
                  </span>
                }
              />
            )}
          />
        )}
      </Section>
    </div>
    <div>
      <Section title="Darvo's deal">
        {v.darvo.length === 0 && <Muted>No deal right now.</Muted>}
        {v.darvo.map((d, i) => (
          <Item
            key={i}
            label={d.item}
            sub={`${d.discount}% off · ${Math.max(0, d.total - d.sold)} / ${d.total} left`}
            value={
              <span>
                <span style={{ color: colors.plat }}>{d.salePrice}p</span> <s style={{ color: colors.muted }}>{d.originalPrice}p</s>
                <div style={{ fontSize: "12px" }}>
                  <Countdown to={d.expiryMs} now={now} short />
                </div>
              </span>
            }
          />
        ))}
      </Section>
      <Section title="Varzia (Prime Resurgence)" right={v.varzia ? <Countdown to={v.varzia.expiryMs} now={now} /> : undefined}>
        {!v.varzia && <Muted>Not in the world state.</Muted>}
        {v.varzia && (
          <Paged
            items={v.varzia.inventory}
            pageSize={10}
            render={(it, i) => (
              <Item key={i} label={it.item} value={<Muted>{it.aya != null ? `${it.aya} Aya` : it.regalAya != null ? `${it.regalAya} Regal Aya` : ""}</Muted>} />
            )}
          />
        )}
      </Section>
    </div>
  </Grid>
);

const SteelTab: FC<{ v: WorldView; now: number }> = ({ v, now }) => (
  <Grid cols={2}>
    <div>
      <Section title="Steel Path honors (Teshin)">
        {!v.steelPath && <Muted>Unknown</Muted>}
        {v.steelPath?.current && (
          <Item label={v.steelPath.current.name} sub={`${v.steelPath.current.cost} Steel Essence · this week`} value={<Countdown to={v.steelPath.expiryMs} now={now} />} color={colors.ducat} />
        )}
        {v.steelPath?.upcoming.map((u, i) => (
          <Item key={i} label={u.name} sub={`${u.cost} Steel Essence`} value={<Muted>in {u.inDays} day{u.inDays === 1 ? "" : "s"}</Muted>} />
        ))}
      </Section>
      {v.steelPath && (
        <Section title="Always available">
          {v.steelPath.evergreens.map((e, i) => (
            <Item key={i} label={e.name} value={<Muted>{e.cost} SE</Muted>} />
          ))}
        </Section>
      )}
    </div>
    <div>
      <Section title="The Circuit">
        <CircuitBlock circuit={v.circuit} showNext />
      </Section>
    </div>
  </Grid>
);

// ---- arbitration schedule ---------------------------------------------------------------------

const ARBI_PAGE = 12;
const LEAD_MINUTES = [5, 10, 15, 30];

const ArbisTab: FC<{ now: number }> = ({ now }) => {
  const [days, setDays] = useState(3);
  const [favOnly, setFavOnly] = useState(false);
  const s = useRpc<ArbiSchedule>("world.arbi_schedule", { days }, [], 5 * 60000);
  const [busy, setBusy] = useState(false);
  useEvent("arbi.schedule_alert", () => s.reload());
  const toggle = async (method: string, params: Record<string, unknown>) => {
    await call(method, params);
    s.reload();
  };
  return (
    <>
      <Focusable flow-children="horizontal" style={{ display: "flex", gap: "8px", alignItems: "center", margin: "8px 0", flexWrap: "wrap" }}>
        <Segmented value={String(days)} options={[1, 3, 7, 14].map((d) => ({ id: String(d), label: `${d} day${d > 1 ? "s" : ""}` }))} onChange={(d) => setDays(Number(d))} />
        <DialogButton style={{ minWidth: 0, width: "auto", padding: "6px 14px" }} onClick={() => setFavOnly(!favOnly)}>
          {favOnly ? "Favourites only" : "All nodes"}
        </DialogButton>
        <DialogButton
          style={{ minWidth: 0, width: "auto", padding: "6px 14px" }}
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            try {
              await call("world.arbi_refresh", { days });
              s.reload();
            } finally {
              setBusy(false);
            }
          }}
        >
          Refresh
        </DialogButton>
        <Muted>Run analysis: Arbitrations tab (R1)</Muted>
      </Focusable>
      <Guard state={s}>
        {(d) => {
          const favs = new Set(d.alerts.favoriteNodes);
          const bells = new Set(d.alerts.occurrences);
          const entries = d.entries.filter((e) => !favOnly || favs.has(e.nodeId));
          return (
            <>
              {d.error && <div style={{ color: colors.warn, fontSize: "13px" }}>Schedule refresh failed ({d.error}); showing the cached list.</div>}
              <Focusable flow-children="horizontal" style={{ display: "flex", gap: "8px", alignItems: "center", flexWrap: "wrap", marginBottom: "6px" }}>
                <Muted>Alert lead time</Muted>
                <Segmented
                  value={String(d.alerts.minutesBefore)}
                  options={LEAD_MINUTES.map((m) => ({ id: String(m), label: `${m} min` }))}
                  onChange={(m) => toggle("world.arbi_set_lead", { minutes: Number(m) })}
                />
              </Focusable>
              <div style={{ color: colors.muted, fontSize: "13px", marginBottom: "6px" }}>
                Star = alert for every arbitration on that node · Bell = alert for this one ({d.alerts.minutesBefore} min before)
              </div>
              {entries.length === 0 && <Empty text={favOnly ? "No favourite node in this window." : "No arbitrations in this window."} />}
              <Paged
                items={entries}
                pageSize={ARBI_PAGE}
                render={(e, i) => {
                  const live = e.epochMs <= now && now < e.epochMs + 3600000;
                  // the first row of every page repeats its day so page 2+ is never undated
                  const newDay = i % ARBI_PAGE === 0 || fmtDay(entries[i - 1].epochMs) !== fmtDay(e.epochMs);
                  return (
                    <div key={e.key}>
                      {newDay && <div style={{ color: colors.accent, fontWeight: 600, margin: "8px 0 2px" }}>{fmtDay(e.epochMs)}</div>}
                      <Focusable flow-children="horizontal" style={{ display: "flex", alignItems: "center", gap: "8px", padding: "3px 0", borderBottom: `1px solid ${colors.border}` }}>
                        <span style={{ width: "52px", color: live ? colors.good : colors.muted, fontVariantNumeric: "tabular-nums" }}>{fmtClock(e.epochMs)}</span>
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div style={{ color: favs.has(e.nodeId) ? colors.ducat : colors.text }}>{e.node}</div>
                          <div style={{ fontSize: "12px", color: colors.muted }}>
                            {e.mission} · <span style={{ color: FACTION_COLORS[e.faction] ?? colors.muted }}>{e.faction}</span>
                          </div>
                        </div>
                        <span style={{ width: "100px", textAlign: "right" }}>{live ? <span style={{ color: colors.good }}>now</span> : <Countdown to={e.epochMs} now={now} short />}</span>
                        <DialogButton style={{ minWidth: 0, width: "48px", padding: "4px" }} onClick={() => toggle("world.arbi_set_occurrence", { key: e.key, enabled: !bells.has(e.key) })}>
                          {bells.has(e.key) ? <FaBell color={colors.warn} /> : <FaRegBell />}
                        </DialogButton>
                        <DialogButton style={{ minWidth: 0, width: "48px", padding: "4px" }} onClick={() => toggle("world.arbi_set_favorite", { node_id: e.nodeId, enabled: !favs.has(e.nodeId) })}>
                          {favs.has(e.nodeId) ? <FaStar color={colors.ducat} /> : <FaRegStar />}
                        </DialogButton>
                      </Focusable>
                    </div>
                  );
                }}
              />
            </>
          );
        }}
      </Guard>
    </>
  );
};

// ---- dailies ----------------------------------------------------------------------------------

const DailiesTab: FC<{ now: number }> = ({ now }) => {
  const r = useRpc<Dailies>("world.dailies", {}, [], 60000);
  const [data, setData] = useState<Dailies>();
  const [edit, setEdit] = useState(false);
  useEvent("inventory.updated", () => r.reload());
  useEffect(() => setData(r.data), [r.data]);
  const d = data ?? r.data;
  if (!d) return r.error ? <ErrorText error={r.error} /> : <Loading />;
  const run = async (method: string, params: Record<string, unknown>) => setData(await call<Dailies>(method, params));
  const groups: { id: DailyTask["group"]; title: string }[] = [
    { id: "daily", title: "Daily" },
    { id: "weekly", title: "Weekly" },
    { id: "vendors", title: "Vendors" },
  ];
  return (
    <>
      <Focusable flow-children="horizontal" style={{ display: "flex", gap: "8px", alignItems: "center", margin: "8px 0" }}>
        <span style={{ fontSize: "16px", fontWeight: 600 }}>
          {d.doneCount} / {d.visibleCount} done
        </span>
        <DialogButton style={{ minWidth: 0, width: "auto", padding: "6px 14px" }} onClick={() => setEdit(!edit)}>
          {edit ? "Done editing" : "Show / hide tasks"}
        </DialogButton>
        <Muted>{d.inventory ? "Ticks marked auto come from your inventory." : "No inventory yet: tick tasks by hand."}</Muted>
      </Focusable>
      <Grid>
        {groups.map((g) => {
          const tasks = d.tasks.filter((t) => t.group === g.id && (edit || !t.hidden));
          return (
            <Section key={g.id} title={g.title}>
              {tasks.map((t) => (
                <Item
                  key={t.id}
                  onActivate={() =>
                    edit
                      ? run("world.dailies_hide", { id: t.id, hidden: !t.hidden })
                      : run("world.dailies_set", { id: t.id, count: t.target === 1 ? (t.manual >= 1 ? 0 : 1) : (t.count + 1) % (t.target + 1) })
                  }
                  color={edit && t.hidden ? colors.muted : t.done ? colors.good : colors.text}
                  label={
                    <>
                      {edit ? (t.hidden ? "◌ " : "● ") : t.done ? "✔ " : "☐ "}
                      {t.label}
                      {t.auto && <Badge text="auto" color={colors.good} />}
                    </>
                  }
                  sub={[t.detail, t.resetMs ? `resets in ${fmtShort(t.resetMs, now)}` : null].filter(Boolean).join(" · ") || undefined}
                  value={t.target > 1 ? <span>{t.count}/{t.target}</span> : undefined}
                />
              ))}
            </Section>
          );
        })}
      </Grid>
    </>
  );
};

// ---- page -------------------------------------------------------------------------------------

export type WorldTab = "overview" | "fissures" | "missions" | "nightwave" | "baro" | "steel" | "arbis" | "dailies";
const TABS: { id: WorldTab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "fissures", label: "Fissures" },
  { id: "missions", label: "Sortie & Invasions" },
  { id: "nightwave", label: "Nightwave" },
  { id: "baro", label: "Baro & Vendors" },
  { id: "steel", label: "Steel Path & Circuit" },
  { id: "arbis", label: "Arbitrations" },
  { id: "dailies", label: "Dailies" },
];

const World: FC = () => {
  const [tab, setTabState] = useState<WorldTab>(lastWorldTab);
  const setTab = (t: WorldTab) => {
    lastWorldTab = t;
    setTabState(t);
  };
  useEffect(() => {
    // openWorld(tab) while this page is mounted (QAM rows, Overview links) switches the sub-tab now
    setTabState(lastWorldTab);
    worldTabListeners.add(setTabState);
    return () => {
      worldTabListeners.delete(setTabState);
    };
  }, []);
  const now = useNow(1000);
  const view = useRpc<WorldView>("world.view", {}, [], 30000);
  const arbi = useRpc<ArbiSchedule>("world.arbi_schedule", { days: 1 }, [], 5 * 60000);
  // Local copy of the fissure filters for instant feedback; the saved settings stay the source of
  // truth: re-seeded from every world.view result and every QAM save while no page save is pending.
  const [prefs, setPrefsState] = useState<WorldPrefs>();
  const pending = useRef(0);
  const seq = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const latest = useRef<WorldPrefs | undefined>(undefined);
  useEffect(() => {
    if (view.data && pending.current === 0) setPrefsState(view.data.prefs);
  }, [view.data]);
  useEffect(() => {
    const l = (p: WorldPrefs) => {
      if (pending.current === 0) setPrefsState(p);
    };
    prefsListeners.add(l);
    return () => {
      prefsListeners.delete(l);
      if (timer.current && pending.current && latest.current) {
        // leaving the page within the debounce window still saves the last change
        clearTimeout(timer.current);
        const w = latest.current;
        void call("world.ui_set", { fissure_mode: w.fissureMode, fissure_tiers: w.fissureTiers, qam_limit: w.qamLimit }).catch(() => undefined);
      }
    };
  }, []);
  const setPrefs = (p: Partial<WorldPrefs>) => {
    const merged = { ...(latest.current && pending.current ? latest.current : prefs ?? view.data!.prefs), ...p };
    latest.current = merged;
    setPrefsState(merged);
    pending.current = 1;
    // debounced so slider steps and quick tier toggles write the settings file once; a response
    // for an older request than the latest change is ignored (sequence number)
    const mine = ++seq.current;
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(async () => {
      timer.current = undefined;
      const want = latest.current!;
      try {
        const saved = await call<WorldPrefs>("world.ui_set", {
          fissure_mode: want.fissureMode,
          fissure_tiers: want.fissureTiers,
          qam_limit: want.qamLimit,
        });
        if (mine === seq.current) setPrefsState(saved);
      } finally {
        if (mine === seq.current) pending.current = 0;
      }
    }, 300);
  };
  const [refreshing, setRefreshing] = useState(false);
  return (
    <Page>
      <Focusable flow-children="horizontal" style={{ display: "flex", gap: "8px", alignItems: "center", marginTop: "8px" }}>
        <Segmented value={tab} options={TABS} onChange={setTab} style={{ flex: 1 }} />
        <DialogButton
          style={{ minWidth: 0, width: "auto", padding: "6px 14px" }}
          disabled={refreshing}
          onClick={async () => {
            setRefreshing(true);
            try {
              await call("world.view", { force: true });
              view.reload();
            } finally {
              setRefreshing(false);
            }
          }}
        >
          {refreshing ? "…" : "Refresh"}
        </DialogButton>
      </Focusable>
      {tab === "arbis" ? (
        <ArbisTab now={now} />
      ) : tab === "dailies" ? (
        <DailiesTab now={now} />
      ) : (
        <Guard state={view}>
          {(v) => (
            <>
              <StatusLine v={v} />
              {tab === "overview" && <Overview v={v} now={now} go={setTab} setPrefs={setPrefs} arbi={arbi.data} />}
              {tab === "fissures" && <FissuresTab v={v} now={now} prefs={prefs ?? v.prefs} setPrefs={setPrefs} />}
              {tab === "missions" && <MissionsTab v={v} now={now} />}
              {tab === "nightwave" && <NightwaveTab v={v} now={now} />}
              {tab === "baro" && <BaroTab v={v} now={now} />}
              {tab === "steel" && <SteelTab v={v} now={now} />}
            </>
          )}
        </Guard>
      )}
    </Page>
  );
};

export default World;

// ---- QAM (primary QAM page: resets, cycles, fissure counts + top rows, arbitration, Baro, Circuit) ----

interface QamView {
  now: number;
  stale: boolean;
  error: string | null;
  resets: Timer[];
  cycles: Cycle[];
  fissureCounts: Record<FissureMode, TierCounts>;
  fissures: Fissure[];
  prefs: WorldPrefs;
  arbitration: { current: ArbiEntry | null; next: ArbiEntry | null; nextFavorite: ArbiEntry | null; error: string | null };
  baro: Baro | null;
  circuit: Circuit;
}

const RESET_SHORT: Record<string, string> = { daily: "Daily", weekly: "Weekly", sortie: "Sortie" };

export const Qam: FC = () => {
  const q = useRpc<QamView>("world.qam", {}, [], 30000);
  const now = useNow(1000);
  const d = q.data;
  const counts = d ? d.fissureCounts[d.prefs.fissureMode] : undefined;
  return (
    <>
      <PanelSection title="World">
        {q.error && !d && (
          <PanelSectionRow>
            <Field label="Error" description={q.error} />
          </PanelSectionRow>
        )}
        {!d && !q.error && (
          <PanelSectionRow>
            <Field label="Loading…" />
          </PanelSectionRow>
        )}
        {d && (
          <>
            {d.stale && (
              <PanelSectionRow>
                <div style={{ color: colors.warn, fontSize: "12px" }}>Offline: cached world state</div>
              </PanelSectionRow>
            )}
            <PanelSectionRow>
              <Field
                focusable
                onActivate={() => openWorld("overview")}
                label="Resets"
                description={d.resets.map((r) => `${RESET_SHORT[r.key] ?? r.label} ${fmtShort(r.expiryMs, now)}`).join(" · ")}
              />
            </PanelSectionRow>
            {d.cycles.map((c) => (
              <PanelSectionRow key={c.key}>
                <Field focusable padding="compact" label={c.label} onActivate={() => openWorld("overview")}>
                  <span style={{ color: isUrgent(c.activationMs, c.expiryMs, now) ? colors.warn : colors.text, whiteSpace: "nowrap", fontVariantNumeric: "tabular-nums" }}>
                    {cycleText(c, now)}
                  </span>
                </Field>
              </PanelSectionRow>
            ))}
          </>
        )}
      </PanelSection>
      {d && (
        <PanelSection title="Fissures">
          <PanelSectionRow>
            <DropdownItem
              label="Mode"
              rgOptions={MODES.map((m) => ({ data: m, label: `${MODE_LABELS[m]} (${d.fissureCounts[m].total})` }))}
              selectedOption={d.prefs.fissureMode}
              onChange={async (o) => {
                prefsSaved(await call<WorldPrefs>("world.ui_set", { fissure_mode: o.data }));
                q.reload();
              }}
            />
          </PanelSectionRow>
          {counts && (
            <PanelSectionRow>
              <Focusable onActivate={() => openWorld("fissures")} focusClassName="gpfocus" style={{ display: "flex", justifyContent: "space-between", fontSize: "13px", padding: "2px 0" }}>
                {TIERS.filter((t) => t !== "Omnia" || counts[t]).map((t) => (
                  <span key={t} style={{ color: counts[t] ? TIER_COLORS[t.toLowerCase()] : colors.muted }}>
                    {t.slice(0, 3)} {counts[t] ?? 0}
                  </span>
                ))}
              </Focusable>
            </PanelSectionRow>
          )}
          {d.fissures.length === 0 && (
            <PanelSectionRow>
              <Field label="No fissures match" description="Change the tiers on the World page." />
            </PanelSectionRow>
          )}
          {d.fissures.map((f) => (
            <PanelSectionRow key={f.id}>
              <Field focusable padding="compact" label={<FissureLabel f={f} />} description={f.node} onActivate={() => openWorld("fissures")}>
                <span style={{ fontVariantNumeric: "tabular-nums", whiteSpace: "nowrap" }}>{fmtShort(f.expiryMs, now)}</span>
              </Field>
            </PanelSectionRow>
          ))}
        </PanelSection>
      )}
      {d && (
        <PanelSection title="Also">
          {(d.arbitration.current || d.arbitration.next) && (
            <PanelSectionRow>
              {d.arbitration.current ? (
                <Field
                  focusable
                  onActivate={() => openWorld("arbis")}
                  label={`Arbitration: ${d.arbitration.current.node}`}
                  description={`${d.arbitration.current.mission} · ${d.arbitration.current.faction} · ends in ${fmtShort(d.arbitration.current.epochMs + 3600000, now)}`}
                />
              ) : (
                <Field
                  focusable
                  onActivate={() => openWorld("arbis")}
                  label={`Next arbi ${fmtClock(d.arbitration.next!.epochMs)}`}
                  description={`${d.arbitration.next!.node} · ${d.arbitration.next!.mission} · ${d.arbitration.next!.faction}`}
                />
              )}
            </PanelSectionRow>
          )}
          {d.arbitration.nextFavorite && d.arbitration.nextFavorite.key !== d.arbitration.next?.key && (
            <PanelSectionRow>
              <Field
                focusable
                onActivate={() => openWorld("arbis")}
                label={`★ ${d.arbitration.nextFavorite.node}`}
                description={`${fmtDay(d.arbitration.nextFavorite.epochMs)} ${fmtClock(d.arbitration.nextFavorite.epochMs)} · in ${fmtShort(d.arbitration.nextFavorite.epochMs, now)}`}
              />
            </PanelSectionRow>
          )}
          {d.baro && (
            <PanelSectionRow>
              <Field
                focusable
                onActivate={() => openWorld("baro")}
                label={d.baro.active ? "Baro is here" : "Baro arrives"}
                description={`${d.baro.location} · ${d.baro.active ? "leaves" : "in"} ${fmtShort(d.baro.targetMs, now)}`}
              />
            </PanelSectionRow>
          )}
          {d.circuit.hard.choices.length > 0 && (
            <PanelSectionRow>
              <Field focusable onActivate={() => openWorld("steel")} label="Circuit SP" description={d.circuit.hard.choices.join(", ")} />
            </PanelSectionRow>
          )}
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={() => openWorld("overview")}>
              Open World
            </ButtonItem>
          </PanelSectionRow>
        </PanelSection>
      )}
    </>
  );
};

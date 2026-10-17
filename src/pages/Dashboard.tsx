// Dashboard: one-glance summary (docs/port/menus.md §2). Data: dashboard.summary
// (py_modules/wfh/menus/dashboard.py); widget settings via dashboard.configure.
// Full page only: the QAM already has World (cycles, fissures, Baro), Foundry, Missions and
// Arbitrations sections, so Dashboard exports no QAM section. Rows are focus targets for D-pad
// scrolling but do not link: FullPage cannot switch to another tab while it is showing (use L1/R1).
import { Focusable, SliderField, ToggleField } from "@decky/ui";
import { FC, useEffect, useRef, useState } from "react";

import { Badge, Ducats, Guard, Page, Plat, Section, colors } from "../components/ui";
import { call, useEvent, useRpc } from "../rpc";
import {
  ArbiEntry,
  Baro,
  BaroRow,
  Countdown,
  Cycle,
  CyclesSection,
  Fissure,
  FissureLabel,
  FissureMode,
  MODE_LABELS,
  TIERS,
  TIER_COLORS,
  TierCounts,
  Timer,
  WorldPrefs,
  fmtClock,
  Item as Row,
  useNow,
} from "./World";

interface FoundryRow {
  name: string;
  status: "claimable" | "in-progress" | "ready-to-build";
  endMs: number | null;
  count: number | null;
  chainOnly: boolean;
}

interface LastMission {
  id: string;
  endedAt: number;
  nodeLabel: string | null;
  missionTypeLabel: string | null;
  missionCount: number | null;
  platinum: number;
  ducats: number;
  credits: number | null;
  endo: number | null;
  itemCount: number;
  unpriced: number;
  rows: { name: string; count: number; platinum: number | null; ducats: number | null }[];
}

interface Summary {
  now: number;
  stale: boolean;
  error: string | null;
  prefs: { fissureLimit: number; foundryLimit: number; hidden: string[] };
  worldPrefs: WorldPrefs;
  cycles: Cycle[];
  resets: Timer[];
  fissures: Fissure[];
  fissureCounts: Record<FissureMode, TierCounts>;
  baro: Baro | null;
  baroItems: string[];
  // ready = recipes ready to build; null when only the raw PendingRecipes fallback was available
  foundry: { claimable: number; building: number; ready: number | null; rows: FoundryRow[]; nextReadyMs: number | null } | null;
  lastMission: LastMission | null;
  inventory: { available: boolean; syncMs: number | null };
  lastReward: { at: number; best: string | null; bestPlat: number | null; items: { name: string; plat: number | null; ducats: number | null; owned: number | null }[] } | null;
  lastArbi: {
    id: string;
    startedAt: number;
    node: string | null;
    missionName: string | null;
    missionType: string | null;
    durationSec: number | null;
    rotations: number | null;
    drones: number | null;
    vitusActual: number | null;
    expectedVitusMean: number | null;
  } | null;
  arbiRunCount: number;
  arbitration: { current: ArbiEntry | null; next: ArbiEntry | null; nextFavorite: ArbiEntry | null; error: string | null };
  dailies: { done: number; total: number; sortieDone: boolean | null; archonDone: boolean | null } | null;
  sortie: { boss: string } | null;
  archon: { boss: string } | null;
  steelPath: { name: string; cost: number } | null;
}

const WIDGETS: { id: string; label: string }[] = [
  { id: "cycles", label: "Planet cycles" },
  { id: "resets", label: "Resets and dailies" },
  { id: "fissures", label: "Void fissures" },
  { id: "baro", label: "Baro Ki'Teer" },
  { id: "foundry", label: "Foundry ready" },
  { id: "lastMission", label: "Last mission" },
  { id: "lastReward", label: "Last relic reward" },
  { id: "lastArbi", label: "Last arbitration run" },
  { id: "arbitration", label: "Next arbitration" },
];

function ago(sec: number, nowMs: number): string {
  const m = Math.max(0, Math.round((nowMs / 1000 - sec) / 60));
  if (m < 1) return "just now";
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  return h < 48 ? `${h} h ago` : `${Math.round(h / 24)} days ago`;
}

function duration(sec: number | null): string {
  if (!sec) return "–";
  const m = Math.floor(sec / 60);
  return m >= 60 ? `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m` : `${m}m ${String(Math.round(sec % 60)).padStart(2, "0")}s`;
}

function arbiLine(a: NonNullable<Summary["lastArbi"]>): string {
  const parts = [a.rotations != null ? `${a.rotations} rotations` : null, a.drones != null ? `${a.drones} drones` : null, duration(a.durationSec)];
  if (a.vitusActual != null) parts.push(`${a.vitusActual} Vitus`);
  else if (a.expectedVitusMean != null) parts.push(`~${Math.round(a.expectedVitusMean)} Vitus expected`);
  return parts.filter(Boolean).join(" · ");
}

function foundryHeader(f: NonNullable<Summary["foundry"]>): string {
  const parts = [`${f.claimable} to claim`];
  if (f.ready != null) parts.push(`${f.ready} to build`);
  return parts.join(" · ");
}

// ---- widgets ----------------------------------------------------------------------------------

const ResetsWidget: FC<{ s: Summary; now: number }> = ({ s, now }) => (
  <Section title="Resets" right={s.dailies ? <span style={{ color: colors.muted }}>Dailies {s.dailies.done}/{s.dailies.total}</span> : undefined}>
    {s.resets.map((t) => {
      const done = t.key === "sortie" ? s.dailies?.sortieDone : t.key === "archon" ? s.dailies?.archonDone : null;
      const who = t.key === "sortie" ? s.sortie?.boss : t.key === "archon" ? s.archon?.boss : t.key === "steelPath" ? s.steelPath?.name : null;
      return (
        <Row
          key={t.key}
          label={
            <>
              {t.label}
              {done === true && <Badge text="done" color={colors.good} />}
            </>
          }
          sub={who ?? undefined}
          value={<Countdown to={t.expiryMs} activation={t.activationMs} now={now} />}
        />
      );
    })}
  </Section>
);

const FissuresWidget: FC<{ s: Summary; now: number }> = ({ s, now }) => {
  const counts = s.fissureCounts[s.worldPrefs.fissureMode];
  return (
    <Section title={`Fissures · ${MODE_LABELS[s.worldPrefs.fissureMode]}`}>
      <div style={{ display: "flex", gap: "10px", fontSize: "13px", marginBottom: "4px" }}>
        {TIERS.map((t) => (
          <span key={t} style={{ color: counts[t] ? TIER_COLORS[t.toLowerCase()] : colors.muted }}>
            {t} {counts[t] ?? 0}
          </span>
        ))}
      </div>
      {s.fissures.length === 0 && <div style={{ color: colors.muted }}>No fissures match the World filters.</div>}
      {s.fissures.map((f) => (
        <Row key={f.id} label={<FissureLabel f={f} />} sub={f.node} value={<Countdown to={f.expiryMs} activation={f.activationMs} now={now} />} />
      ))}
    </Section>
  );
};

const BaroWidget: FC<{ s: Summary; now: number }> = ({ s, now }) => (
  <Section title="Baro Ki'Teer">
    <BaroRow baro={s.baro} now={now} />
    {s.baro?.active && s.baroItems.map((name, i) => <Row key={i} label={<span style={{ color: colors.muted }}>{name}</span>} />)}
  </Section>
);

// foundryReadyRows: finished builds (CLAIM), running builds (countdown), then recipes ready to build (BUILD).
const FoundryWidget: FC<{ s: Summary; now: number }> = ({ s, now }) => (
  <Section title="Foundry" right={s.foundry ? <span style={{ color: s.foundry.claimable || s.foundry.ready ? colors.good : colors.muted }}>{foundryHeader(s.foundry)}</span> : undefined}>
    {!s.foundry && <div style={{ color: colors.muted }}>No inventory yet. It is read from the game after a mission or from Settings.</div>}
    {s.foundry && s.foundry.rows.length === 0 && <div style={{ color: colors.muted }}>Nothing to claim or build.</div>}
    {s.foundry?.rows.map((r, i) => (
      <Row
        key={i}
        label={
          <span style={{ color: r.status === "in-progress" ? colors.muted : colors.text }}>
            {r.name}
            {r.count != null && r.count > 1 && <span style={{ color: colors.muted }}> x{r.count}</span>}
            {r.chainOnly && <Badge text="chain" color={colors.muted} />}
          </span>
        }
        value={
          r.status === "claimable" ? (
            <span style={{ color: colors.good, fontWeight: 600 }}>CLAIM</span>
          ) : r.status === "ready-to-build" ? (
            <span style={{ color: colors.accent, fontWeight: 600 }}>BUILD</span>
          ) : (
            <Countdown to={r.endMs} now={now} />
          )
        }
      />
    ))}
    {s.foundry?.ready == null && s.foundry && <div style={{ color: colors.muted, fontSize: "12px" }}>Ready-to-build recipes need the item data; only builds are shown.</div>}
  </Section>
);

const MissionWidget: FC<{ s: Summary; now: number }> = ({ s, now }) => {
  const m = s.lastMission;
  return (
    <Section title="Last mission" right={m ? <span style={{ color: colors.muted }}>{ago(m.endedAt / 1000, now)}</span> : undefined}>
      {!m && <div style={{ color: colors.muted }}>Mission rewards appear here after the inventory is read at the end of a mission.</div>}
      {m && (
        <>
          <Row
            label={m.nodeLabel ?? m.missionTypeLabel ?? "Mission"}
            sub={[m.nodeLabel ? m.missionTypeLabel : null, fmtClock(m.endedAt), m.missionCount && m.missionCount > 1 ? `${m.missionCount} missions` : null].filter(Boolean).join(" · ")}
            value={
              <span style={{ whiteSpace: "nowrap" }}>
                {m.platinum > 0 && <span style={{ color: colors.plat }}>+{m.platinum}p </span>}
                {m.ducats > 0 && <span style={{ color: colors.ducat }}>+{m.ducats}d </span>}
                {m.credits ? <span style={{ color: colors.muted }}>+{Math.round(m.credits / 1000)}k cr</span> : null}
              </span>
            }
          />
          {m.rows.map((r, i) => (
            <Row
              key={i}
              label={
                <span style={{ color: colors.muted }}>
                  {r.count > 1 ? `${r.count}x ` : ""}
                  {r.name}
                </span>
              }
              value={
                <span>
                  {r.platinum != null && <Plat value={r.platinum} />} {r.ducats != null && <Ducats value={r.ducats} />}
                </span>
              }
            />
          ))}
          {m.itemCount > m.rows.length && <div style={{ color: colors.muted, fontSize: "12px" }}>+{m.itemCount - m.rows.length} more on the Missions page</div>}
        </>
      )}
    </Section>
  );
};

const RewardWidget: FC<{ s: Summary; now: number }> = ({ s, now }) => (
  <Section title="Last relic reward" right={s.lastReward ? <span style={{ color: colors.muted }}>{ago(s.lastReward.at, now)}</span> : undefined}>
    {!s.lastReward && <div style={{ color: colors.muted }}>Open a relic in a fissure to see its rewards here.</div>}
    {s.lastReward?.items.map((it, i) => (
      <Row
        key={i}
        label={
          <span style={{ color: it.name === s.lastReward?.best ? colors.plat : colors.text }}>
            {it.name}
            {it.owned != null && it.owned > 0 && <Badge text={`owned ${it.owned}`} color={colors.muted} />}
          </span>
        }
        value={
          <span>
            <Plat value={it.plat} /> <Ducats value={it.ducats} />
          </span>
        }
      />
    ))}
  </Section>
);

const ArbiWidget: FC<{ s: Summary; now: number; showRun: boolean; showNext: boolean }> = ({ s, now, showRun, showNext }) => {
  const a = s.arbitration;
  return (
    <Section title="Arbitration">
      {showNext && a.current && (
        <Row label={a.current.node} sub={`${a.current.mission} · ${a.current.faction} · running now`} value={<Countdown to={a.current.epochMs + 3600000} now={now} />} />
      )}
      {showNext && a.next && (
        <Row label={`Next ${fmtClock(a.next.epochMs)}: ${a.next.node}`} sub={`${a.next.mission} · ${a.next.faction}`} value={<Countdown to={a.next.epochMs} now={now} />} />
      )}
      {showNext && a.nextFavorite && a.nextFavorite.key !== a.next?.key && (
        <Row label={<span style={{ color: colors.ducat }}>Favourite: {a.nextFavorite.node}</span>} sub={`${a.nextFavorite.mission} · ${fmtClock(a.nextFavorite.epochMs)}`} value={<Countdown to={a.nextFavorite.epochMs} now={now} />} />
      )}
      {showNext && !a.current && !a.next && <div style={{ color: colors.muted }}>{a.error ? "Schedule unavailable." : "Schedule not loaded yet."}</div>}
      {showRun &&
        (s.lastArbi ? (
          <Row
            label={`Last run: ${s.lastArbi.node ?? s.lastArbi.missionName ?? "Arbitration"}`}
            sub={`${arbiLine(s.lastArbi)} · ${ago(s.lastArbi.startedAt / 1000, now)}`}
            value={<span style={{ color: colors.muted }}>{s.arbiRunCount} runs</span>}
          />
        ) : (
          <Row label={<span style={{ color: colors.muted }}>No tracked runs yet</span>} />
        ))}
    </Section>
  );
};

// Local state changes at once; the save is debounced (a slider fires on every D-pad step) and the
// summary reloads only after it. Server prefs re-seed the controls only while nothing is pending.
const Customize: FC<{ s: Summary; onSaved: () => void }> = ({ s, onSaved }) => {
  const [p, setP] = useState(s.prefs);
  const pending = useRef<Record<string, unknown>>({});
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const seq = useRef(0);
  useEffect(() => {
    if (!timer.current && Object.keys(pending.current).length === 0) setP(s.prefs);
  }, [s.prefs]);
  useEffect(
    () => () => {
      // leaving the tab within the debounce window still saves the last change
      if (timer.current) clearTimeout(timer.current);
      if (Object.keys(pending.current).length) void call("dashboard.configure", pending.current).catch(() => undefined);
    },
    [],
  );
  const save = (patch: Record<string, unknown>, local: Partial<Summary["prefs"]>) => {
    setP((cur) => ({ ...cur, ...local }));
    pending.current = { ...pending.current, ...patch };
    const mine = ++seq.current;
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(async () => {
      timer.current = undefined;
      const body = pending.current;
      pending.current = {};
      try {
        const saved = await call<Summary["prefs"]>("dashboard.configure", body);
        if (mine === seq.current) setP(saved);
      } finally {
        if (mine === seq.current) onSaved();
      }
    }, 400);
  };
  return (
    <Section title="Customize">
      <Focusable style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0 24px" }}>
        <div>
          {WIDGETS.map((w) => (
            <ToggleField
              key={w.id}
              label={w.label}
              checked={!p.hidden.includes(w.id)}
              onChange={(on) => {
                const hidden = on ? p.hidden.filter((x) => x !== w.id) : [...p.hidden, w.id];
                save({ hidden }, { hidden });
              }}
            />
          ))}
        </div>
        <div>
          <SliderField label="Fissure rows" value={p.fissureLimit} min={1} max={20} step={1} showValue onChange={(v) => save({ fissure_limit: v }, { fissureLimit: v })} />
          <SliderField label="Foundry rows" value={p.foundryLimit} min={1} max={20} step={1} showValue onChange={(v) => save({ foundry_limit: v }, { foundryLimit: v })} />
          <div style={{ color: colors.muted, fontSize: "13px", marginTop: "8px" }}>Fissure mode and tiers follow the filters on the World page.</div>
        </div>
      </Focusable>
    </Section>
  );
};

// ---- page -------------------------------------------------------------------------------------

const Dashboard: FC = () => {
  const r = useRpc<Summary>("dashboard.summary", {}, [], 30000);
  const now = useNow(1000);
  useEvent("inventory.updated", () => r.reload());
  useEvent("missions.updated", () => r.reload());
  useEvent("rewards.result", () => r.reload());
  useEvent("arbi.run_saved", () => r.reload());
  return (
    <Page>
      <Guard state={r}>
        {(s) => {
          const show = (id: string) => !s.prefs.hidden.includes(id);
          return (
            <>
              <div style={{ color: colors.muted, fontSize: "12px", marginTop: "8px" }}>L1 / R1 switch to World, Foundry, Missions and the other pages for details.</div>
              {s.stale && <div style={{ color: colors.warn, fontSize: "13px", marginTop: "8px" }}>World state is offline or stale; clock-based timers are still live.</div>}
              <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) minmax(0,1fr)", gap: "0 16px", alignItems: "start" }}>
                <div>
                  {show("cycles") && <CyclesSection cycles={s.cycles} now={now} />}
                  {show("fissures") && <FissuresWidget s={s} now={now} />}
                  {show("baro") && <BaroWidget s={s} now={now} />}
                </div>
                <div>
                  {show("resets") && <ResetsWidget s={s} now={now} />}
                  {show("foundry") && <FoundryWidget s={s} now={now} />}
                  {show("lastMission") && <MissionWidget s={s} now={now} />}
                  {show("lastReward") && <RewardWidget s={s} now={now} />}
                  {(show("arbitration") || show("lastArbi")) && <ArbiWidget s={s} now={now} showNext={show("arbitration")} showRun={show("lastArbi")} />}
                </div>
              </div>
              <Customize s={s} onSaved={r.reload} />
            </>
          );
        }}
      </Guard>
    </Page>
  );
};

export default Dashboard;

// ---- QAM: none. World (cycles, fissures, Baro), Foundry, Missions and Arbitrations have their own
// QAM sections, which cover everything this summary would repeat (see qam/QamRoot.tsx).
export const Qam = null;

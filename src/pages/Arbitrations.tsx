// Arbitrations: tracked run history with per-run analysis (vitus luck, saturation,
// rotations, drone cadence, spawn map), plus the upcoming schedule with favourite
// and one-shot alerts. QAM: current / next arbitration, live capture, last run.
// Backend: py_modules/wfh/menus/arbitrations.py (arbi.*), features/arbitration.py,
// data/arbi_schedule.py (world.arbi_*).
import {
  ButtonItem,
  ConfirmModal,
  DialogButton,
  Dropdown,
  Field,
  Focusable,
  Navigation,
  PanelSection,
  PanelSectionRow,
  TextField,
  ToggleField,
  showModal,
} from "@decky/ui";
import { CSSProperties, Children, FC, ReactNode, useEffect, useMemo, useState } from "react";

import { Badge, Empty, ErrorText, FilterCell, Loading, Page, Paged, Row, Section, Segmented, Stat, chip, colors, countdown } from "../components/ui";
import { openTab } from "../nav";
import { ROUTE } from "../nav";
import { call, useEvent, useRpc } from "../rpc";

// ---- types (mirror menus/arbitrations.py, features/arbi_*.py) -------------------------

type MissionType = "defense" | "interception" | "disruption" | "other";

interface RunSlim {
  id: string;
  startedAt: number;
  endedAt: number;
  missionName: string;
  node: string;
  missionType: MissionType;
  missionTypeRaw: string | null;
  solNode: string | null;
  durationSec: number;
  rotations: number;
  drones: number;
  totalEnemies: number;
  vitusActual: number | null;
  logFile: string | null;
  logSizeBytes: number;
  endReason: string;
  source: "live" | "imported";
  players?: string[];
  tags?: string[];
  notes?: string;
  duplicateOf?: string;
  expectedVitusMean?: number;
  expectedVitusStd?: number;
  hasStats: boolean;
}

interface ActiveRun {
  id: string;
  node: string;
  missionType: MissionType;
  rotations: number;
  drones: number;
  hostTelemetry: boolean;
  startedAt: number;
}

interface Skip {
  node: string;
  reason: string;
  rotations: number;
  hostTelemetry: boolean;
  at: number;
}

interface TrackerStatus {
  tracking: boolean;
  initialized: boolean;
  active: ActiveRun | null;
  lastSkip: Skip | null;
  runCount: number;
  eeLog: string | null;
  summaryVisible: boolean;
  lastSummaryId: string | null;
}

interface RunsPayload {
  runs: RunSlim[];
  total: number;
  diskUsageBytes: number;
  status: TrackerStatus;
}

interface Scenario {
  prob: string;
  key: string;
  label: string;
  total: number;
}

interface Luck {
  key: string;
  label: string;
  percentile: number;
  text: string;
  color: string;
}

interface Bucket {
  minCount: number;
  label: string;
  seconds: number;
  pct: number;
}

interface RotCell {
  index: number;
  durationSec: number;
  saturationPct: number | null;
  drones: number;
  dpm: number;
}

interface Wave {
  index: number;
  durationSec: number;
  saturationPct?: number;
}

interface Cadence {
  buckets: { label: string; seconds: number; pct: number }[];
  totalWaitSec: number;
  dryPct: number;
  peak: { drones: number; atSec: number } | null;
}

interface PB {
  value: number;
  rank: number;
  poolSize: number;
  isPb: boolean;
  vsBestPct: number | null;
}

interface Analysis {
  typeLabel: string;
  dronesPerMin: number | null;
  enemiesPerMin: number | null;
  dronesPerRotation: number | null;
  hasStats: boolean;
  scenarios?: Scenario[];
  luck?: Luck | null;
  vitusPerMinActual?: number | null;
  expectedVitusPerMin?: number | null;
  saturationAbovePct?: number;
  saturationBuckets?: Bucket[];
  rotations?: RotCell[];
  waves?: Wave[] | null;
  cadence?: Cadence | null;
  spawns?: { total: number; cold: number; top: { id: string; label: string; count: number }[] };
  spawnMap?: { cx: number; cy: number; r: number; hue: number; count: number }[];
  pauseSec?: number;
  idleSec?: number;
  personalBest?: Record<string, PB>;
}

interface RunFull extends Omit<RunSlim, "hasStats"> {
  stats: {
    expectedVitusMean: number;
    expectedVitusStd: number;
    avgDroneIntervalSec: number | null;
    killsPerDrone: number;
    wavesPerRotation: number;
    droneCount: number;
  } | null;
}

interface Detail {
  run: RunFull;
  analysis: Analysis;
  prevId: string | null;
  nextId: string | null;
  hasLog: boolean;
}

interface SchedEntry {
  epochMs: number;
  nodeId: string;
  node: string;
  mission: string;
  faction: string;
  key: string;
}

interface Alerts {
  occurrences: string[];
  favoriteNodes: string[];
  minutesBefore: number;
}

interface Schedule {
  entries: SchedEntry[];
  fetchedAt: number | null;
  stale: boolean;
  error: string | null;
  alerts: Alerts;
  current: SchedEntry | null;
  next: SchedEntry | null;
  now: number;
}

interface QamView {
  current: SchedEntry | null;
  next: SchedEntry | null;
  nextFavorite: SchedEntry | null;
  favorites: string[];
  scheduleError: string | null;
  lastRun: RunSlim | null;
  status: TrackerStatus;
  lastSummary: { id: string } | null;
}

// ---- formatting ---------------------------------------------------------------------------

const RAW_LABELS: Record<string, string> = {
  MT_SURVIVAL: "Survival",
  MT_EXCAVATE: "Excavation",
  MT_EVACUATION: "Defection",
  MT_PURIFY: "Infested Salvage",
  MT_ALCHEMY: "Alchemy",
};
const TYPE_LABELS: Record<MissionType, string> = { defense: "Defense", interception: "Interception", disruption: "Disruption", other: "Other" };

function typeLabel(t: MissionType, raw: string | null): string {
  if (t !== "other") return TYPE_LABELS[t] ?? t;
  if (raw) return RAW_LABELS[raw] ?? raw.replace(/^MT_/, "");
  return "Other";
}

function fmtDuration(sec: number | null | undefined): string {
  const s = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h > 0 ? `${h}h ${m}m ${s % 60}s` : `${m}m ${s % 60}s`;
}

function fmtClock(sec: number): string {
  const t = Math.max(0, Math.round(sec || 0));
  return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`;
}

function fmtDate(ms: number): string {
  if (!ms || ms <= 0) return "—";
  const d = new Date(ms);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getDate())}.${p(d.getMonth() + 1)}.${d.getFullYear()} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function fmtBytes(b: number): string {
  if (b <= 0) return "0 B";
  if (b >= 1024 * 1024) return `${(b / 1024 / 1024).toFixed(1)} MB`;
  if (b >= 1024) return `${(b / 1024).toFixed(1)} KB`;
  return `${b} B`;
}

function num(v: number | null | undefined, dp = 0): string {
  return v == null || !isFinite(v) ? "–" : v.toFixed(dp);
}

const END_LABELS: Record<string, string> = {
  "mission-end": "Completed",
  aborted: "Aborted",
  "new-mission": "Next mission",
  "log-truncated": "Game restarted",
  "app-quit": "Plugin stopped",
  inactivity: "Inactive 10 min",
  imported: "Imported (log end)",
};

const FACTION_COLORS: Record<string, string> = {
  grineer: "#e08a5a",
  corpus: "#6eaaff",
  infested: "#8fd16a",
  corrupted: "#e2c45a",
  other: colors.muted,
};

function factionKey(f: string): string {
  const l = (f || "").toLowerCase();
  if (l === "grineer" || l === "corpus") return l;
  if (l.startsWith("infest")) return "infested";
  if (l.startsWith("corrupt") || l === "orokin") return "corrupted";
  return "other";
}

function hue(h: number): string {
  return `hsl(${Math.round(h)}, 70%, 55%)`;
}

// A focus stop so the D-pad can scroll through chart blocks (they have nothing to press).
const Block: FC<{ children: ReactNode }> = ({ children }) => (
  <Focusable onActivate={() => {}} focusClassName="gpfocus" noFocusRing={false} style={{ padding: "2px 0" }}>
    {children}
  </Focusable>
);

const ButtonRow: FC<{ children: ReactNode }> = ({ children }) => (
  <Focusable flow-children="horizontal" style={{ display: "flex", gap: "8px", flexWrap: "wrap", margin: "6px 0" }}>
    {children}
  </Focusable>
);

const Btn: FC<{ onClick: () => void; disabled?: boolean; children: ReactNode; width?: string; on?: boolean }> = ({ onClick, disabled, children, width, on }) => (
  <DialogButton style={{ ...chip(!!on), ...(width ? { width } : {}) }} disabled={disabled} onClick={onClick}>
    {children}
  </DialogButton>
);

function confirm(title: string, body: string, ok: string, onOK: () => void): void {
  showModal(<ConfirmModal strTitle={title} strDescription={body} strOKButtonText={ok} bDestructiveWarning onOK={onOK} />);
}

// Set by the QAM "Open last run" before navigating here.
let pendingRunId: string | null = null;
let lastView: "runs" | "schedule" = "runs";
// A mounted page applies QAM deep links (view + run) live, since navigating to the
// already-active tab does not remount it.
type ArbiJump = (view: "runs" | "schedule", runId: string | null) => void;
const jumpListeners = new Set<ArbiJump>();

// ---- run list -------------------------------------------------------------------------------

interface Filters {
  type: "all" | MissionType;
  source: "all" | "live" | "imported";
  minRot: number;
  dups: boolean;
}
let savedFilters: Filters = { type: "all", source: "all", minRot: 0, dups: false };

const StatusNotice: FC<{ s: TrackerStatus }> = ({ s }) => (
  <Section title="Tracker">
    {!s.tracking && <div style={{ color: colors.warn }}>Run tracking is off. Turn it on below to record arbitrations.</div>}
    {s.tracking && !s.eeLog && <div style={{ color: colors.warn }}>EE.log not found yet; start Warframe once.</div>}
    {s.active ? (
      <div>
        Recording <b>{s.active.node}</b> · {s.active.rotations} rotations · {s.active.drones} drones
        {!s.active.hostTelemetry && <span style={{ color: colors.warn }}> · no AI data yet (client?)</span>}
      </div>
    ) : (
      s.tracking && <div style={{ color: colors.muted }}>Waiting for an arbitration in EE.log.</div>
    )}
    {s.lastSkip && (
      <div style={{ fontSize: "13px", color: colors.muted, marginTop: "4px" }}>
        Last run not saved ({s.lastSkip.node}): {s.lastSkip.reason}.
        {!s.lastSkip.hostTelemetry && " Only the squad host's EE.log has enemy spawns; host or play solo to track a run."}
      </div>
    )}
  </Section>
);

const RunList: FC<{ onOpen: (id: string) => void }> = ({ onOpen }) => {
  const runs = useRpc<RunsPayload>("arbi.runs", {}, [], 30000);
  const settings = useRpc<{ arbitration?: { tracking?: boolean; enabled?: boolean } }>("settings.get", {}, []);
  const [f, setF] = useState<Filters>(savedFilters);
  const [msg, setMsg] = useState<string>();
  const [busy, setBusy] = useState(false);
  useEvent("arbi.run_saved", () => runs.reload());
  useEvent("arbi.run_skipped", () => runs.reload());
  const update = (patch: Partial<Filters>) => {
    savedFilters = { ...f, ...patch };
    setF(savedFilters);
  };
  const list = useMemo(
    () =>
      (runs.data?.runs ?? []).filter(
        (r) =>
          (f.type === "all" || r.missionType === f.type) &&
          (f.source === "all" || r.source === f.source) &&
          r.rotations >= f.minRot &&
          (f.dups || !r.duplicateOf),
      ),
    [runs.data, f],
  );
  const d = runs.data;
  const arb = settings.data?.arbitration;
  return (
    <>
      {d && <StatusNotice s={d.status} />}
      <Section title="Runs" right={d && <span style={{ color: colors.muted, fontSize: "12px" }}>{d.total} runs · logs {fmtBytes(d.diskUsageBytes)}</span>}>
        {/* Compact filter bar: dropdowns side by side, actions at the end. */}
        <Focusable
          flow-children="horizontal"
          style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr)) auto auto auto", gap: "8px", alignItems: "end", marginBottom: "6px" }}
        >
          <FilterCell label="Mission type">
            <Dropdown
              rgOptions={[
                { data: "all", label: "All types" },
                { data: "defense", label: "Defense" },
                { data: "interception", label: "Interception" },
                { data: "disruption", label: "Disruption" },
                { data: "other", label: "Other" },
              ]}
              selectedOption={f.type}
              onChange={(o) => update({ type: o.data })}
            />
          </FilterCell>
          <FilterCell label="Source">
            <Dropdown
              rgOptions={[
                { data: "all", label: "Live and imported" },
                { data: "live", label: "Live" },
                { data: "imported", label: "Imported" },
              ]}
              selectedOption={f.source}
              onChange={(o) => update({ source: o.data })}
            />
          </FilterCell>
          <FilterCell label="Min rotations">
            <Dropdown
              rgOptions={[0, 2, 4, 6, 8, 10, 15, 20].map((n) => ({ data: n, label: n === 0 ? "Any" : `${n}+` }))}
              selectedOption={f.minRot}
              onChange={(o) => update({ minRot: o.data })}
            />
          </FilterCell>
          <Btn on={f.dups} onClick={() => update({ dups: !f.dups })}>
            {f.dups ? "Duplicates shown" : "Duplicates hidden"}
          </Btn>
            <Btn
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                try {
                  await call("arbi.refresh");
                  runs.reload();
                } finally {
                  setBusy(false);
                }
              }}
            >
              Refresh
            </Btn>
            <Btn
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                setMsg("Importing the current EE.log…");
                try {
                  const r = await call<{ imported: RunSlim[]; skipped: number; error?: string }>("arbi.import_log", {});
                  setMsg(r.error ?? `Imported ${r.imported.length} run(s), skipped ${r.skipped}.`);
                  runs.reload();
                } catch (e) {
                  setMsg(String(e));
                } finally {
                  setBusy(false);
                }
              }}
            >
              Import current EE.log
            </Btn>
        </Focusable>
        {msg && <div style={{ color: colors.muted, fontSize: "13px", marginBottom: "4px" }}>{msg}</div>}
        {runs.error && !d && <ErrorText error={runs.error} />}
        {!d && !runs.error && <Loading />}
        {d && list.length === 0 && (
          <Empty text={d.total === 0 ? "No arbitrations recorded yet. Runs are saved after 2+ rotations as host or solo." : "No runs match the filters."} />
        )}
        {d && list.length > 0 && (
          <Paged
            items={list}
            pageSize={20}
            render={(r) => (
              <Row
                key={r.id}
                onActivate={() => onOpen(r.id)}
                label={
                  <span>
                    {r.node}
                    <Badge text={typeLabel(r.missionType, r.missionTypeRaw)} />
                    {r.source === "imported" && <Badge text="imported" color={colors.muted} />}
                    {r.duplicateOf && <Badge text="duplicate" color={colors.warn} />}
                  </span>
                }
                sub={`${fmtDate(r.startedAt)} · ${fmtDuration(r.durationSec)} · ${END_LABELS[r.endReason] ?? r.endReason}${r.tags?.length ? ` · ${r.tags.join(", ")}` : ""}`}
                value={
                  <span style={{ fontSize: "13px" }}>
                    {r.rotations} rot · {r.drones} drones ·{" "}
                    <span style={{ color: colors.accent }}>
                      {r.vitusActual != null ? `${r.vitusActual}` : `~${num(r.expectedVitusMean)}`} vitus
                    </span>
                  </span>
                }
              />
            )}
          />
        )}
      </Section>
      <Section title="Settings">
        <ToggleField
          label="Track arbitration runs"
          description="Reads EE.log; nothing is sent anywhere."
          checked={arb?.tracking ?? true}
          onChange={async (v) => {
            await call("settings.update", { patch: { arbitration: { tracking: v } } });
            settings.reload();
            runs.reload();
          }}
        />
        <ToggleField
          label="End-of-mission summary overlay"
          checked={arb?.enabled ?? true}
          onChange={async (v) => {
            await call("settings.update", { patch: { arbitration: { enabled: v } } });
            settings.reload();
          }}
        />
        <SummarySettings />
      </Section>
    </>
  );
};

const SummarySettings: FC = () => {
  const s = useRpc<{ arbitration?: { summary_position?: string; summary_scale?: number; duration_s?: number } }>("settings.get", {}, []);
  const a = s.data?.arbitration;
  const set = async (patch: Record<string, unknown>) => {
    await call("settings.update", { patch: { arbitration: patch } });
    s.reload();
  };
  return (
    <Focusable flow-children="horizontal" style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: "8px", marginTop: "8px" }}>
      <FilterCell label="Summary position">
        <Dropdown
          rgOptions={[
            { data: "top-center", label: "Top center" },
            { data: "top-right", label: "Top right (over minimap)" },
            { data: "top-left", label: "Top left" },
            { data: "left-middle", label: "Left middle" },
          ]}
          selectedOption={a?.summary_position ?? "top-center"}
          onChange={(o) => set({ summary_position: o.data })}
        />
      </FilterCell>
      <FilterCell label="Summary size">
        <Dropdown
          rgOptions={[0.75, 0.9, 1, 1.1, 1.25].map((v) => ({ data: v, label: `${Math.round(v * 100)}%` }))}
          selectedOption={a?.summary_scale ?? 1}
          onChange={(o) => set({ summary_scale: o.data })}
        />
      </FilterCell>
      <FilterCell label="Summary shows for">
        <Dropdown
          rgOptions={[10, 15, 25, 40, 60].map((v) => ({ data: v, label: `${v} s` }))}
          selectedOption={a?.duration_s ?? 25}
          onChange={(o) => set({ duration_s: o.data })}
        />
      </FilterCell>
    </Focusable>
  );
};

// ---- run detail ------------------------------------------------------------------------------

const Bars: FC<{ rows: { label: string; pct: number; color?: string; right?: string }[] }> = ({ rows }) => (
  <div>
    {rows.map((r, i) => (
      <div key={i} style={{ display: "flex", alignItems: "center", gap: "8px", fontSize: "13px", margin: "2px 0" }}>
        <span style={{ width: "64px", color: colors.muted, flexShrink: 0 }}>{r.label}</span>
        <div style={{ flex: 1, height: "12px", background: "rgba(255,255,255,0.06)", borderRadius: "3px" }}>
          <div style={{ width: `${Math.max(0, Math.min(100, r.pct))}%`, height: "100%", background: r.color ?? colors.accent, borderRadius: "3px" }} />
        </div>
        <span style={{ width: "72px", textAlign: "right", flexShrink: 0 }}>{r.right ?? `${r.pct.toFixed(1)}%`}</span>
      </div>
    ))}
  </div>
);

const Grid: FC<{ head: string[]; rows: ReactNode[][] }> = ({ head, rows }) => (
  <div style={{ display: "grid", gridTemplateColumns: `repeat(${head.length}, 1fr)`, fontSize: "13px", rowGap: "2px" }}>
    {head.map((h) => (
      <div key={h} style={{ color: colors.muted, fontSize: "11px", textTransform: "uppercase" }}>{h}</div>
    ))}
    {rows.map((r, i) => r.map((c, j) => <div key={`${i}-${j}`}>{c}</div>))}
  </div>
);

// Two sections side by side at equal width (or one at full width when the other is
// absent). The row is a horizontal Focusable so left/right moves between them.
const PAIR: CSSProperties = { margin: 0, height: "100%", boxSizing: "border-box", minWidth: 0 };
const Pair: FC<{ children: ReactNode[] }> = ({ children }) => {
  const parts = Children.toArray(children);
  if (parts.length === 0) return null;
  if (parts.length === 1) return <div style={{ margin: "12px 0" }}>{parts[0]}</div>;
  return (
    <Focusable flow-children="horizontal" style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: "12px", margin: "12px 0", alignItems: "stretch" }}>
      {/* Each half is its own vertical group, so up/down stays inside it. */}
      {parts.map((part, i) => (
        <Focusable key={i} style={{ minWidth: 0 }}>
          {part}
        </Focusable>
      ))}
    </Focusable>
  );
};

// Long wave lists wrap into side-by-side columns of up to 10 rows instead of one tall table.
const WaveColumns: FC<{ waves: Wave[] }> = ({ waves }) => {
  const cols = Math.min(4, Math.ceil(waves.length / 10));
  const per = Math.ceil(waves.length / cols);
  const chunks: Wave[][] = [];
  for (let i = 0; i < waves.length; i += per) chunks.push(waves.slice(i, i + per));
  return (
    <div style={{ display: "grid", gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`, gap: "24px" }}>
      {chunks.map((c) => (
        <Grid key={c[0].index} head={["#", "Clear", "15+ enemies"]} rows={c.map((w) => [w.index, fmtClock(w.durationSec), w.saturationPct == null ? "–" : `${w.saturationPct.toFixed(0)}%`])} />
      ))}
    </div>
  );
};

const RunDetail: FC<{ id: string; onBack: () => void; onOpen: (id: string) => void }> = ({ id, onBack, onOpen }) => {
  const det = useRpc<Detail>("arbi.run", { id }, [id]);
  const [vitus, setVitus] = useState("");
  const [tags, setTags] = useState("");
  const [notes, setNotes] = useState("");
  const [msg, setMsg] = useState<string>();
  useEffect(() => {
    const r = det.data?.run;
    setVitus(r?.vitusActual != null ? String(r.vitusActual) : "");
    setTags((r?.tags ?? []).join(", "));
    setNotes(r?.notes ?? "");
    setMsg(undefined);
  }, [det.data?.run.id]);
  if (det.error && !det.data) return <ErrorText error={det.error} />;
  if (det.data === undefined) return <Loading />;
  if (!det.data)
    return (
      <>
        <ButtonRow>
          <Btn onClick={onBack}>‹ Runs</Btn>
        </ButtonRow>
        <Empty text="Run not found." />
      </>
    );
  const { run, analysis: a, prevId, nextId, hasLog } = det.data;
  const st = run.stats;
  const pb = a.personalBest ?? {};
  const pbText = (k: string, label: string) => {
    const p = pb[k];
    if (!p || p.poolSize < 2) return null;
    return (
      <div>
        {label}: {p.isPb ? <b style={{ color: colors.good }}>Personal best</b> : `#${p.rank} of ${p.poolSize}`}
        {p.vsBestPct != null && <span style={{ color: colors.muted }}> ({p.vsBestPct >= 0 ? "+" : ""}{p.vsBestPct.toFixed(1)}% vs best other)</span>}
      </div>
    );
  };
  return (
    <>
      <ButtonRow>
        <Btn onClick={onBack}>‹ Runs</Btn>
        <Btn disabled={!nextId} onClick={() => nextId && onOpen(nextId)}>Newer</Btn>
        <Btn disabled={!prevId} onClick={() => prevId && onOpen(prevId)}>Older</Btn>
        <Btn
          disabled={!st}
          onClick={async () => {
            const r = await call("arbi.summary_show", { id: run.id });
            setMsg(r ? "Summary shown over the game." : "This run has no summary (needs 2+ rotations and stats).");
          }}
        >
          Show summary overlay
        </Btn>
      </ButtonRow>
      <Section title={run.node} right={<span style={{ color: colors.muted }}>{fmtDate(run.startedAt)}</span>}>
        <div style={{ color: colors.muted, fontSize: "13px", marginBottom: "6px" }}>
          {a.typeLabel} · {END_LABELS[run.endReason] ?? run.endReason} · {run.source}
          {run.players && run.players.length > 0 && ` · Squad (${run.players.length}): ${run.players.join(", ")}`}
        </div>
        <Block>
          <Stat label="Duration" value={fmtDuration(run.durationSec)} />
          <Stat label="Rotations" value={run.rotations} />
          <Stat label="Drones" value={run.drones} />
          <Stat label="Enemies" value={run.totalEnemies} />
          <Stat label="Drones/min" value={num(a.dronesPerMin, 2)} />
          <Stat label="Enemies/min" value={num(a.enemiesPerMin, 1)} />
          {st && <Stat label="Kills/drone" value={num(st.killsPerDrone, 2)} />}
          {st && <Stat label="Drone every" value={st.avgDroneIntervalSec != null ? `${st.avgDroneIntervalSec.toFixed(1)}s` : "–"} />}
          {a.saturationAbovePct != null && <Stat label="Time 15+" value={`${a.saturationAbovePct.toFixed(1)}%`} />}
        </Block>
        {(pb.dronesPerMin || pb.expectedVitusPerMin) && (
          <div style={{ fontSize: "13px", marginTop: "6px" }}>
            {pbText("dronesPerMin", "Drones/min on this node")}
            {pbText("expectedVitusPerMin", "Vitus/min on this node")}
          </div>
        )}
        {!st && <div style={{ color: colors.muted, marginTop: "6px" }}>No detailed stats for this mission type.</div>}
      </Section>

      <Pair>
      {st && a.scenarios && (
        <Section title="Vitus essence" style={PAIR}>
          <Block>
            <div>
              Expected <b style={{ color: colors.accent }}>{st.expectedVitusMean.toFixed(1)}</b> ± {st.expectedVitusStd.toFixed(1)}
              {a.expectedVitusPerMin != null && <span style={{ color: colors.muted }}> · {a.expectedVitusPerMin.toFixed(2)}/min</span>}
            </div>
            <div style={{ height: "6px" }} />
            <Grid head={a.scenarios.map((s) => s.label)} rows={[a.scenarios.map((s) => <b key={s.key}>{s.total}</b>), a.scenarios.map((s) => <span key={s.key} style={{ color: colors.muted }}>{s.prob}</span>)]} />
          </Block>
          <Focusable flow-children="horizontal" style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) auto", gap: "8px", alignItems: "end" }}>
            <TextField label="Actual vitus" description="What you got (opens the keyboard)" mustBeNumeric value={vitus} onChange={(e) => setVitus(e.target.value)} />
            <Btn
              onClick={async () => {
                const v = vitus.trim() === "" ? null : Number(vitus);
                const r = await call("arbi.set_vitus", { id: run.id, vitus: v });
                setMsg(r ? "Saved." : "Enter a whole number from 0 to 10,000,000.");
                det.reload();
              }}
            >
              Save vitus
            </Btn>
          </Focusable>
          {a.luck && (
            <div style={{ color: a.luck.color, fontWeight: 600 }}>
              {a.luck.text}
              {a.vitusPerMinActual != null && <span style={{ color: colors.muted, fontWeight: 400 }}> · {a.vitusPerMinActual.toFixed(2)}/min</span>}
            </div>
          )}
        </Section>
      )}

      {a.rotations && a.rotations.length > 0 && (
        <Section title="Rotations" style={PAIR}>
          <Block>
            <Grid
              head={["#", "Clear", "Drones", "Drones/min", "15+ enemies"]}
              rows={a.rotations.map((r) => [
                r.index,
                fmtClock(r.durationSec),
                r.drones,
                r.dpm.toFixed(2),
                r.saturationPct == null ? "–" : <span style={{ color: hue(Math.min(120, r.saturationPct * 1.2)) }}>{r.saturationPct.toFixed(0)}%</span>,
              ])}
            />
          </Block>
        </Section>
      )}

      </Pair>

      <Pair>
      {a.saturationBuckets && a.saturationBuckets.length > 0 && (
        <Section title="Enemies alive (share of fight time)" style={PAIR}>
          <Block>
            <Bars rows={a.saturationBuckets.map((b, i) => ({ label: b.label, pct: b.pct, color: `color-mix(in srgb, ${colors.good} ${Math.max(0, 100 - 15 * i)}%, ${colors.bad})` }))} />
            <div style={{ color: colors.muted, fontSize: "12px", marginTop: "4px" }}>
              Reward screens {fmtClock(a.pauseSec ?? 0)} · load/stall gaps {fmtClock(a.idleSec ?? 0)} excluded
            </div>
          </Block>
        </Section>
      )}

      {a.cadence && (
        <Section title="Waiting between drones" style={PAIR}>
          <Block>
            <Bars rows={a.cadence.buckets.map((b) => ({ label: b.label, pct: b.pct, color: b.label === "12s+" ? colors.bad : colors.accent }))} />
            <div style={{ color: colors.muted, fontSize: "12px", marginTop: "4px" }}>
              Dry spells (12s+): {a.cadence.dryPct.toFixed(1)}% of wait time
              {a.cadence.peak && ` · busiest 10 s: ${a.cadence.peak.drones} drones at ${fmtClock(a.cadence.peak.atSec)}`}
            </div>
          </Block>
        </Section>
      )}

      </Pair>

      {a.waves && a.waves.length > 0 && (
        <Section title={run.missionType === "disruption" ? "Rounds" : "Waves"}>
          <Block>
            <WaveColumns waves={a.waves} />
          </Block>
        </Section>
      )}

      <Pair>
      {a.spawns && a.spawns.total > 0 && (
        <Section title="Spawn points" style={PAIR}>
          <Block>
            <div style={{ display: "flex", gap: "16px", alignItems: "flex-start" }}>
              <svg viewBox="0 0 100 100" style={{ width: "220px", height: "220px", background: "rgba(255,255,255,0.03)", borderRadius: "4px", flexShrink: 0 }}>
                {(a.spawnMap ?? []).map((p, i) => (
                  <circle key={i} cx={p.cx} cy={p.cy} r={p.r} fill={hue(p.hue)} fillOpacity={0.8} />
                ))}
              </svg>
              <div style={{ fontSize: "13px", flex: 1 }}>
                <div style={{ color: colors.muted }}>
                  {a.spawns.total} points · {a.spawns.cold} cold (≤2 spawns)
                </div>
                {a.spawns.top.map((p) => (
                  <div key={p.id}>
                    #{p.label}: <b>{p.count}</b>
                  </div>
                ))}
              </div>
            </div>
          </Block>
        </Section>
      )}

      <Section title="Notes" style={PAIR}>
        <Focusable flow-children="horizontal" style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: "12px" }}>
          <TextField label="Tags" description="Comma separated, up to 12" value={tags} onChange={(e) => setTags(e.target.value)} />
          <TextField label="Notes" value={notes} onChange={(e) => setNotes(e.target.value)} />
        </Focusable>
        <ButtonRow>
          <Btn
            onClick={async () => {
              await call("arbi.set_tags", { id: run.id, tags: tags.split(",").map((t) => t.trim()).filter(Boolean) });
              await call("arbi.set_notes", { id: run.id, notes });
              setMsg("Saved.");
              det.reload();
            }}
          >
            Save tags and notes
          </Btn>
        </ButtonRow>
      </Section>

      </Pair>

      <Section title="Log">
        <div style={{ color: colors.muted, fontSize: "13px" }}>
          {hasLog ? `Stored capture ${fmtBytes(run.logSizeBytes)}.` : "No stored capture."}
        </div>
        <ButtonRow>
          <Btn
            disabled={!hasLog}
            onClick={async () => {
              const r = await call<{ ok: boolean; path?: string; error?: string }>("arbi.export_log", { id: run.id });
              setMsg(r.ok ? `Saved to ${r.path}` : r.error);
            }}
          >
            Export to Downloads
          </Btn>
          <Btn
            disabled={!hasLog}
            onClick={() =>
              confirm("Delete stored log?", "The run stays in the list; its raw capture is removed.", "Delete log", async () => {
                await call("arbi.delete_log", { id: run.id });
                det.reload();
              })
            }
          >
            Delete log
          </Btn>
          <Btn
            onClick={() =>
              confirm("Delete this run?", `${run.node}, ${fmtDate(run.startedAt)}. This cannot be undone.`, "Delete run", async () => {
                await call("arbi.delete_run", { id: run.id });
                onBack();
              })
            }
          >
            Delete run
          </Btn>
        </ButtonRow>
      </Section>
      {msg && <div style={{ color: colors.muted, margin: "4px 0 12px" }}>{msg}</div>}
    </>
  );
};

// ---- schedule ---------------------------------------------------------------------------------

let savedDays = 14;
let savedFavOnly = false;

function dayLabel(ms: number): string {
  return new Date(ms).toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "short" });
}

function timeLabel(ms: number): string {
  const d = new Date(ms);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function schedCountdown(ms: number, now: number): string {
  return ms - now <= 0 ? "NOW" : countdown(ms, now);
}

const ScheduleView: FC = () => {
  const [days, setDays] = useState(savedDays);
  const [favOnly, setFavOnly] = useState(savedFavOnly);
  const sched = useRpc<Schedule>("world.arbi_schedule", { days }, [], 60000);
  const [alerts, setAlerts] = useState<Alerts>();
  const [now, setNow] = useState(Date.now());
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  useEffect(() => setAlerts(sched.data?.alerts), [sched.data]);
  const s = sched.data;
  const al = alerts ?? s?.alerts;
  const favs = new Set(al?.favoriteNodes ?? []);
  const bells = new Set(al?.occurrences ?? []);
  const entries = (s?.entries ?? []).filter((e) => !favOnly || favs.has(e.nodeId));
  const favNodes = useMemo(() => {
    const m = new Map<string, SchedEntry>();
    (s?.entries ?? []).forEach((e) => favs.has(e.nodeId) && !m.has(e.nodeId) && m.set(e.nodeId, e));
    return [...m.values()];
  }, [s, al]);
  const toggleFav = async (e: SchedEntry) => {
    const r = await call<Alerts | null>("world.arbi_set_favorite", { node_id: e.nodeId, enabled: !favs.has(e.nodeId) });
    if (r) setAlerts(r);
  };
  const toggleBell = async (e: SchedEntry) => {
    const r = await call<Alerts | null>("world.arbi_set_occurrence", { key: e.key, enabled: !bells.has(e.key) });
    if (r) setAlerts(r);
  };
  return (
    <>
      <Section
        title="Arbitration schedule"
        right={s?.fetchedAt ? <span style={{ color: s.stale ? colors.warn : colors.muted, fontSize: "12px" }}>updated {fmtDate(s.fetchedAt)}</span> : undefined}
      >
        {s?.error && <div style={{ color: colors.warn, fontSize: "13px" }}>Offline: {s.error}. Showing the cached schedule.</div>}
        {s?.current && (
          <Row
            label={
              <span>
                Now: <b>{s.current.node}</b>
                {favs.has(s.current.nodeId) && <span style={{ color: colors.accent }}> ★</span>}
              </span>
            }
            sub={`${s.current.mission} · ${s.current.faction}`}
            value={<span style={{ color: colors.muted }}>ends in {countdown(s.current.epochMs + 3600000, now)}</span>}
          />
        )}
        <Focusable
          flow-children="horizontal"
          style={{ display: "flex", flexWrap: "wrap", gap: "8px", alignItems: "flex-end", margin: "8px 0 6px" }}
        >
          <FilterCell label="Days ahead" width="240px">
            <Dropdown
              rgOptions={[7, 14, 30, 60].map((d) => ({ data: d, label: `${d} days` }))}
              selectedOption={days}
              onChange={(o) => {
                savedDays = o.data;
                setDays(o.data);
              }}
            />
          </FilterCell>
          <FilterCell label="Alert lead time" width="240px">
            <Dropdown
              rgOptions={[1, 2, 5, 10, 15, 30, 60, 120].map((m) => ({ data: m, label: `${m} min before` }))}
              selectedOption={al?.minutesBefore ?? 5}
              onChange={async (o) => {
                const r = await call<Alerts | null>("world.arbi_set_lead", { minutes: o.data });
                if (r) setAlerts(r);
              }}
            />
          </FilterCell>
          <Btn
            on={favOnly}
            onClick={() => {
              savedFavOnly = !favOnly;
              setFavOnly(!favOnly);
            }}
          >
            Favourite nodes only
          </Btn>
            <Btn
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                try {
                  await call("world.arbi_refresh", { days });
                  sched.reload();
                } finally {
                  setBusy(false);
                }
              }}
            >
              Refresh schedule
            </Btn>
        </Focusable>
        {favNodes.length > 0 && (
          <div style={{ fontSize: "13px", color: colors.muted }}>
            Favourites: {favNodes.map((e) => e.node).join(", ")}. Alerts arrive as Steam notifications.
          </div>
        )}
      </Section>
      <Section title="Upcoming" right={<span style={{ color: colors.muted, fontSize: "12px" }}>★ every time on this node · Alert: this one only</span>}>
        {sched.error && !s && <ErrorText error={sched.error} />}
        {!s && !sched.error && <Loading />}
        {s && entries.length === 0 && <Empty text={favOnly ? "No favourite nodes in this window." : "No schedule data yet."} />}
        {entries.length > 0 && (
          <Paged
            items={entries}
            pageSize={24}
            render={(e, i) => {
              const prev = i > 0 ? entries[i - 1] : null;
              const newDay = !prev || new Date(prev.epochMs).toDateString() !== new Date(e.epochMs).toDateString();
              const fk = factionKey(e.faction);
              return (
                <div key={e.key}>
                  {newDay && <div style={{ color: colors.accent, fontWeight: 600, margin: "8px 0 2px" }}>{dayLabel(e.epochMs)}</div>}
                  <Focusable
                    flow-children="horizontal"
                    style={{ display: "flex", alignItems: "center", gap: "8px", padding: "3px 0", borderBottom: `1px solid ${colors.border}` }}
                  >
                    <span style={{ width: "48px", color: colors.muted }}>{timeLabel(e.epochMs)}</span>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                        {favs.has(e.nodeId) && <span style={{ color: colors.warn }}>★ </span>}
                        {e.node}
                      </div>
                      <div style={{ fontSize: "12px", color: colors.muted }}>
                        {e.mission} · <span style={{ color: FACTION_COLORS[fk] }}>{e.faction}</span>
                      </div>
                    </div>
                    <span style={{ width: "90px", textAlign: "right", fontSize: "13px" }}>{schedCountdown(e.epochMs, now)}</span>
                    <DialogButton style={{ minWidth: 0, width: "64px", padding: "4px" }} onClick={() => toggleFav(e)}>
                      {favs.has(e.nodeId) ? "★" : "☆"}
                    </DialogButton>
                    <DialogButton style={{ minWidth: 0, width: "84px", padding: "4px" }} onClick={() => toggleBell(e)} disabled={e.epochMs <= now}>
                      {bells.has(e.key) ? "Alert ✓" : "Alert"}
                    </DialogButton>
                  </Focusable>
                </div>
              );
            }}
          />
        )}
      </Section>
    </>
  );
};

// ---- page --------------------------------------------------------------------------------------

const Arbitrations: FC = () => {
  const [view, setView] = useState<"runs" | "schedule">(lastView);
  const [runId, setRunId] = useState<string | null>(pendingRunId);
  useEffect(() => {
    // Pick up a request made between the first render and this effect, then consume it.
    setView(lastView);
    if (pendingRunId !== null) setRunId(pendingRunId);
    pendingRunId = null;
    const onJump: ArbiJump = (v, id) => {
      setView(v);
      if (v === "runs") setRunId(id);
    };
    jumpListeners.add(onJump);
    return () => {
      jumpListeners.delete(onJump);
    };
  }, []);
  const go = (v: "runs" | "schedule") => {
    lastView = v;
    setView(v);
  };
  return (
    <Page>
      <Segmented
        value={view}
        onChange={(v) => {
          go(v);
          if (v === "runs") setRunId(null);
        }}
        options={[
          { id: "runs", label: "Runs" },
          { id: "schedule", label: "Schedule" },
        ]}
        style={{ marginTop: "8px" }}
      />
      {view === "schedule" ? (
        <ScheduleView />
      ) : runId ? (
        <RunDetail id={runId} onBack={() => setRunId(null)} onOpen={setRunId} />
      ) : (
        <RunList onOpen={setRunId} />
      )}
    </Page>
  );
};

export default Arbitrations;

// ---- QAM section ---------------------------------------------------------------------------------

function openPage(view: "runs" | "schedule", runId: string | null = null): void {
  lastView = view;
  if (jumpListeners.size > 0) {
    // Already mounted: apply now so no stale run lingers for the next mount.
    pendingRunId = null;
    jumpListeners.forEach((l) => l(view, runId));
  } else {
    pendingRunId = runId;
  }
  openTab("arbitrations");
  Navigation.CloseSideMenus();
  Navigation.Navigate(ROUTE);
}

export const Qam: FC = () => {
  const q = useRpc<QamView>("arbi.qam", {}, [], 30000);
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 15000);
    return () => clearInterval(t);
  }, []);
  useEvent("arbi.run_saved", () => q.reload());
  useEvent("arbi.summary", () => q.reload());
  const d = q.data;
  const last = d?.lastRun;
  return (
    <PanelSection title="Arbitration">
      {q.error && !d && (
        <PanelSectionRow>
          <Field label="Error" description={q.error} />
        </PanelSectionRow>
      )}
      {d?.current && (
        <PanelSectionRow>
          <Field
            focusable
            label={d.current.node}
            description={`${d.current.mission} · ${d.current.faction} · ends in ${countdown(d.current.epochMs + 3600000, now)}`}
            onActivate={() => openPage("schedule")}
          />
        </PanelSectionRow>
      )}
      {d?.next && (
        <PanelSectionRow>
          <Field
            focusable
            label={`Next: ${d.next.node}`}
            description={`${d.next.mission} · in ${countdown(d.next.epochMs, now)}`}
            onActivate={() => openPage("schedule")}
          />
        </PanelSectionRow>
      )}
      {d?.nextFavorite && d.nextFavorite.key !== d.next?.key && (
        <PanelSectionRow>
          <Field
            focusable
            label={`★ ${d.nextFavorite.node}`}
            description={`in ${countdown(d.nextFavorite.epochMs, now)}`}
            onActivate={() => openPage("schedule")}
          />
        </PanelSectionRow>
      )}
      {d?.status.active && (
        <PanelSectionRow>
          <Field
            focusable
            label="Recording"
            description={`${d.status.active.node}: ${d.status.active.rotations} rot · ${d.status.active.drones} drones`}
          />
        </PanelSectionRow>
      )}
      {last && (
        <PanelSectionRow>
          <Field
            focusable
            label={`Last: ${last.node}`}
            description={`${last.rotations} rot · ${last.drones} drones · ${last.vitusActual ?? `~${num(last.expectedVitusMean)}`} vitus · ${fmtDuration(last.durationSec)}`}
            onActivate={() => openPage("runs", last.id)}
          />
        </PanelSectionRow>
      )}
      {!last && d && (
        <PanelSectionRow>
          <Field focusable label="No runs yet" description={d.status.lastSkip ? `Not saved: ${d.status.lastSkip.reason}` : "Host or solo, 2+ rotations"} />
        </PanelSectionRow>
      )}
      {d?.status.summaryVisible ? (
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={async () => {
              await call("arbi.summary_dismiss");
              q.reload();
            }}
          >
            Dismiss summary
          </ButtonItem>
        </PanelSectionRow>
      ) : (
        d?.lastSummary && (
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              onClick={async () => {
                await call("arbi.summary_show");
                q.reload();
              }}
            >
              Show last summary
            </ButtonItem>
          </PanelSectionRow>
        )
      )}
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={() => openPage("runs", d?.status.summaryVisible ? d.lastSummary?.id ?? null : null)}>
          Open Arbitrations
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
};

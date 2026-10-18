// Missions: rewards of each mission with platinum / ducat value, from inventory diffs taken
// after every mission end (docs/port/menus.md §7). Data: missions.* RPCs
// (py_modules/wfh/menus/missions.py); event missions.updated after each recorded mission.
import { ButtonItem, DialogButton, Dropdown, Field, Focusable, PanelSection, PanelSectionRow } from "@decky/ui";
import { FC, useState } from "react";

import { Ducats, Empty, ErrorText, FilterCell, Grid, Loading, Page, Pager, Plat, Row, Section, Segmented, Stat, card, chip, colors } from "../components/ui";
import { call, useEvent, useRpc } from "../rpc";
import { openPage, useThrottled } from "./Mastery";

// ---- types (mirror menus/missions.py) ---------------------------------------------------------

interface RewardRow {
  uniqueName: string;
  name: string;
  count: number;
  slug: string | null;
  vaulted: boolean;
  platinum: number | null;
  each: number | null;
  ducats: number | null;
  unpriced: boolean;
  tradable: boolean;
}

interface Summary {
  id: string;
  endedAt: number;
  readAt: number;
  missionCount: number;
  missionType: string | null;
  node: string | null;
  missionTypeLabel: string | null;
  nodeLabel: string | null;
  credits: number;
  endo: number;
  platinum: number;
  ducats: number;
  unpriced: number;
  itemCount: number;
  rows?: RewardRow[];
}

interface Status {
  enabled: boolean;
  phase: "idle" | "waiting";
  pendingMissions: number;
  recorded: number;
  blocked?: "tracking-off" | "source-not-memory" | "source-none" | "mission-scan-off";
  lastFailure?: string;
  inventorySource?: string;
  baseline?: boolean;
}

interface PageView {
  period: Period;
  matched: number;
  offset: number;
  limit: number;
  summaries: Summary[];
  totals: { missions: number; credits: number; endo: number; platinum: number; ducats: number; unpriced: number; items: RewardRow[]; itemTypes: number };
  missionTypes: { id: string; label: string }[];
  recorded: number;
  today: number;
  latest: Summary | null;
  status: Status;
}

interface QamView {
  latest: Summary | null;
  today: { missions: number; platinum: number; ducats: number };
  status: Status;
}

type Period = "today" | "7d" | "30d" | "all";

const fmt = (n: number | null | undefined) => (n == null ? "–" : Math.round(n).toLocaleString("en-US"));
const localMidnight = () => {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d.getTime();
};
const timeText = (ms: number) => {
  const d = new Date(ms);
  const today = d.toDateString() === new Date().toDateString();
  const hm = d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  return today ? hm : `${d.toLocaleDateString("en-GB", { day: "numeric", month: "short" })} ${hm}`;
};
const title = (s: Summary) => {
  const where = s.nodeLabel ?? "Mission";
  return s.missionTypeLabel ? `${where} - ${s.missionTypeLabel}` : where;
};
const BLOCKED: Record<string, string> = {
  "tracking-off": "Mission tracking is off.",
  "source-not-memory": "Inventory source is a file: rewards are recorded only when the file updates after a mission.",
  "source-none": "No inventory source is set (Settings > Inventory), so mission rewards cannot be read.",
  "mission-scan-off": "The post-mission inventory read is off (Settings > Inventory).",
};

export function openMissions(): void {
  openPage("missions");
}

const Value: FC<{ s: Summary }> = ({ s }) => (
  <span style={{ whiteSpace: "nowrap", fontVariantNumeric: "tabular-nums" }}>
    {s.platinum > 0 && (
      <>
        <Plat value={s.platinum} />{" "}
      </>
    )}
    {s.ducats > 0 && <Ducats value={s.ducats} />}
    {s.platinum <= 0 && s.ducats <= 0 && <span style={{ color: colors.muted }}>{s.itemCount} items</span>}
  </span>
);

const RewardList: FC<{ rows: RewardRow[] }> = ({ rows }) => (
  <>
    {rows.length === 0 && <Empty text="No items (credits/endo only)." />}
    {rows.map((r) => (
      <Row
        key={r.uniqueName}
        color={r.unpriced ? colors.muted : undefined}
        label={`${r.name}${r.count > 1 ? `  x${fmt(r.count)}` : ""}`}
        sub={r.unpriced ? "no cached price yet" : r.each != null && r.count > 1 ? `${r.each}p each` : undefined}
        value={
          <span style={{ whiteSpace: "nowrap" }}>
            {r.platinum != null && <Plat value={r.platinum} />} {r.ducats != null && <Ducats value={r.ducats} />}
          </span>
        }
      />
    ))}
  </>
);

// Top rewards as a card grid: one horizontal Focusable per row inside a vertical one,
// so the d-pad moves in all four directions.
const GRID_COLS = 4;
const TOP = 12;

const RewardCell: FC<{ r: RewardRow }> = ({ r }) => (
  <Focusable
    focusClassName="gpfocus"
    onActivate={() => {}}
    style={card}
  >
    <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: r.unpriced ? colors.muted : colors.text }}>
      {r.name}
      {r.count > 1 && <span style={{ color: colors.muted }}> x{fmt(r.count)}</span>}
    </div>
    <div style={{ display: "flex", justifyContent: "space-between", gap: "8px", fontSize: "13px" }}>
      <span style={{ color: colors.muted, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {r.unpriced ? "no cached price yet" : r.each != null && r.count > 1 ? `${r.each}p each` : ""}
      </span>
      <span style={{ whiteSpace: "nowrap", flexShrink: 0 }}>
        {r.platinum != null && <Plat value={r.platinum} />} {r.ducats != null && <Ducats value={r.ducats} />}
      </span>
    </div>
  </Focusable>
);

const MissionItem: FC<{ s: Summary; open: boolean; toggle: () => void }> = ({ s, open, toggle }) => (
  <div>
    <Row
      onActivate={toggle}
      label={title(s)}
      sub={
        <span>
          {timeText(s.endedAt)}
          {s.missionCount > 1 && ` · ${s.missionCount} missions`}
          {s.credits > 0 && ` · +${fmt(s.credits)} cr`}
          {s.endo > 0 && ` · +${fmt(s.endo)} endo`}
          {` · ${s.itemCount} item types`}
          {s.unpriced > 0 && ` · ${s.unpriced} unpriced`}
        </span>
      }
      value={<Value s={s} />}
    />
    {open && s.rows && (
      <div style={{ paddingLeft: "16px", borderLeft: `2px solid ${colors.accent}`, margin: "4px 0 8px" }}>
        <RewardList rows={s.rows} />
      </div>
    )}
  </div>
);

let lastPeriod: Period = "7d";
const PAGE = 20;

const Missions: FC = () => {
  const [period, setPeriodS] = useState<Period>(lastPeriod);
  const [type, setType] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);
  const [open, setOpen] = useState<string | null>(null);
  const setPeriod = (p: Period) => ((lastPeriod = p), setPeriodS(p), setOffset(0));
  const q = useRpc<PageView>("missions.page", { period, since_ms: localMidnight(), mission_type: type, offset, limit: PAGE });
  useEvent("missions.updated", () => q.reload());
  // Prices this page queued land one by one; reload at most every 2 s.
  const reloadSoon = useThrottled(q.reload, 2000);
  useEvent("market.price", reloadSoon);
  const d = q.data;
  const top = d ? d.totals.items.slice(0, TOP) : [];
  return (
    <Page>
      {/* Compact filter bar on top; everything below gets the full width. */}
      <Focusable flow-children="horizontal" style={{ display: "flex", gap: "12px", alignItems: "flex-end", flexWrap: "wrap", marginTop: "8px" }}>
        <FilterCell label="Period">
          <Segmented
            value={period}
            onChange={setPeriod}
            options={[
              { id: "today", label: "Today" },
              { id: "7d", label: "7 days" },
              { id: "30d", label: "30 days" },
              { id: "all", label: "All" },
            ]}
          />
        </FilterCell>
        {d && d.missionTypes.length > 0 && (
          <FilterCell label="Mission type" width="240px">
            <Dropdown
              rgOptions={[{ data: null, label: "All types" }, ...d.missionTypes.map((t) => ({ data: t.id, label: t.label }))]}
              selectedOption={type}
              onChange={(o) => {
                setType(o.data);
                setOffset(0);
              }}
            />
          </FilterCell>
        )}
        {d && (
          <FilterCell label="Tracking">
            <DialogButton
              style={{ ...chip(d.status.enabled), width: "auto" }}
              onClick={async () => {
                await call("missions.set_enabled", { enabled: !d.status.enabled });
                q.reload();
              }}
            >
              {d.status.enabled ? "Recording rewards" : "Recording off"}
            </DialogButton>
          </FilterCell>
        )}
      </Focusable>
      {q.error && !d && <ErrorText error={q.error} />}
      {!d && !q.error && <Loading />}
      {d && (
        <>
          {d.status.blocked && <div style={{ color: colors.warn, margin: "8px 0 0" }}>{BLOCKED[d.status.blocked] ?? d.status.blocked}</div>}
          {d.status.pendingMissions > 0 && (
            <div style={{ color: colors.muted, margin: "8px 0 0" }}>
              Reading rewards of {d.status.pendingMissions} mission{d.status.pendingMissions > 1 ? "s" : ""}…{d.status.lastFailure ? ` (last read: ${d.status.lastFailure})` : ""}
            </div>
          )}
          <Section
            title="Period totals"
            right={d.totals.unpriced > 0 ? <span style={{ fontSize: "12px", color: colors.muted }}>{d.totals.unpriced} item types not priced yet</span> : undefined}
          >
            <Focusable style={{ display: "flex", flexWrap: "wrap", gap: "6px 12px" }} focusClassName="gpfocus" onActivate={() => {}}>
              <Stat label="Missions" value={fmt(d.totals.missions)} />
              <Stat label="Platinum" value={`${fmt(d.totals.platinum)}p`} color={colors.plat} />
              <Stat label="Ducats" value={`${fmt(d.totals.ducats)}d`} color={colors.ducat} />
              <Stat label="Credits" value={fmt(d.totals.credits)} />
              <Stat label="Endo" value={fmt(d.totals.endo)} />
            </Focusable>
          </Section>
          {top.length > 0 && (
            <Section title={`Top rewards (${d.totals.itemTypes})`} right={d.totals.itemTypes > top.length ? <span style={{ fontSize: "12px", color: colors.muted }}>top {top.length} by value</span> : undefined}>
              <Grid items={top} cols={GRID_COLS} render={(r) => <RewardCell key={r.uniqueName} r={r} />} />
            </Section>
          )}
          <Section title={`${d.matched} recorded`} right={d.summaries.length > 0 ? <span style={{ fontSize: "12px", color: colors.muted }}>Press A on a mission for its rewards</span> : undefined}>
            {d.summaries.length === 0 && <Empty text={d.recorded === 0 ? "No missions recorded yet. Finish a mission with Warframe running; rewards appear a few seconds after the end screen." : "No missions in this period."} />}
            {d.summaries.map((s) => (
              <MissionItem key={s.id} s={s} open={open === s.id} toggle={() => setOpen(open === s.id ? null : s.id)} />
            ))}
            {d.matched > PAGE && <Pager page={Math.floor(offset / PAGE)} pages={Math.ceil(d.matched / PAGE)} onPage={(n) => setOffset(n * PAGE)} label={`${d.matched} missions`} />}
          </Section>
        </>
      )}
    </Page>
  );
};

export default Missions;

// ---- QAM --------------------------------------------------------------------------------------

export const Qam: FC = () => {
  const q = useRpc<QamView>("missions.qam", { since_ms: localMidnight(), rows: 4 }, [], 60000);
  useEvent("missions.updated", () => q.reload());
  const d = q.data;
  const s = d?.latest;
  return (
    <PanelSection title="Last mission">
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
      {d && !s && (
        <PanelSectionRow>
          <Field label="No mission recorded yet" description={d.status.blocked ? BLOCKED[d.status.blocked] : d.status.pendingMissions ? "Reading rewards…" : "Rewards appear a few seconds after a mission ends."} />
        </PanelSectionRow>
      )}
      {s && (
        <>
          <PanelSectionRow>
            <Field
              focusable
              padding="compact"
              label={title(s)}
              description={[s.platinum > 0 ? `+${fmt(s.platinum)}p` : "", s.ducats > 0 ? `+${s.ducats}d` : "", s.credits > 0 ? `+${fmt(s.credits)} cr` : "", s.endo > 0 ? `+${fmt(s.endo)} endo` : ""].filter(Boolean).join("  ") || `${s.itemCount} items`}
              onActivate={openMissions}
            >
              <span style={{ color: colors.muted, whiteSpace: "nowrap" }}>{timeText(s.endedAt)}</span>
            </Field>
          </PanelSectionRow>
          {(s.rows ?? []).map((r) => (
            <PanelSectionRow key={r.uniqueName}>
              <Field focusable padding="compact" label={<span style={{ color: r.unpriced ? colors.muted : colors.text }}>{r.name}</span>} onActivate={openMissions}>
                <span style={{ whiteSpace: "nowrap" }}>
                  x{fmt(r.count)} {r.platinum != null && <Plat value={r.platinum} />}
                </span>
              </Field>
            </PanelSectionRow>
          ))}
          {s.itemCount > (s.rows?.length ?? 0) && (
            <PanelSectionRow>
              <div style={{ fontSize: "12px", color: colors.muted }}>+{s.itemCount - (s.rows?.length ?? 0)} more</div>
            </PanelSectionRow>
          )}
        </>
      )}
      {d && (
        <>
          {d.status.pendingMissions > 0 && s && (
            <PanelSectionRow>
              <div style={{ fontSize: "12px", color: colors.muted }}>Reading the latest mission…</div>
            </PanelSectionRow>
          )}
          <PanelSectionRow>
            <Field focusable padding="compact" label="Today" onActivate={openMissions}>
              <span style={{ whiteSpace: "nowrap" }}>
                {d.today.missions} missions <Plat value={d.today.platinum} />
              </span>
            </Field>
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={openMissions}>
              Open missions
            </ButtonItem>
          </PanelSectionRow>
        </>
      )}
    </PanelSection>
  );
};

// Mastery: MR progress, collection by category/status, the "easy mastery" roadmap and pinned
// goals (docs/port/menus.md §5). Data: mastery.* RPCs (py_modules/wfh/menus/mastery.py), which
// compute everything from the inventory snapshot; this file only keeps tab/filter state.
import { ButtonItem, DialogButton, DropdownItem, Field, Focusable, ModalRoot, Navigation, PanelSection, PanelSectionRow, ToggleField, showModal } from "@decky/ui";
import { CSSProperties, FC, ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { FaRegStar, FaStar } from "react-icons/fa";

import { Badge, Empty, ErrorText, Loading, Page, Plat, Row, Section, Stat, colors } from "../components/ui";
import { openTab } from "../nav";
import { ROUTE } from "../nav";
import { call, useEvent, useRpc } from "../rpc";

// ---- types (mirror menus/mastery.py) ---------------------------------------------------------

type Status = "missing" | "progress" | "mastered";

export interface Profile {
  rank: number;
  percentToNext: number | null;
  totalXp: number | null;
  xpIntoRank: number | null;
  xpForNext: number | null;
  testReady: boolean;
}

interface Pair {
  done: number;
  total: number;
}

interface Summary {
  mastered: number;
  inProgress: number;
  missing: number;
  total: number;
  percent: number;
  byCategory?: { category: string; total: number; mastered: number; inProgress: number; missing: number }[];
  profile: Profile | null;
  completion?: {
    starChart: { normal: Pair; junctions: Pair; steelPath: Pair; steelPathJunctions: Pair };
    intrinsics: { railjack: Pair; drifter: Pair };
  };
  mrLine: string | null;
  projection: { bankedRank: number; projectedRank: number; readyXp: number; text: string | null } | null;
}

interface DataStatus {
  data: boolean;
  dataVersion: string | null;
  inventory: boolean;
  hideFounder?: boolean;
}

interface SummaryView extends DataStatus {
  summary: Summary | null;
  categories?: string[];
}

export interface MasteryRow {
  name: string;
  uniqueName: string;
  category: string;
  status: Status;
  rank: number;
  maxRank: number;
  currentlyOwned: boolean;
  masteryXp: number;
  masteryXpRemaining: number;
  isPrime: boolean;
  vaulted: boolean;
  marketBuyable: boolean;
  needsGilding?: boolean;
  partsOwned: number;
  partsTotal: number;
  foundryState: "claimable" | "building" | null;
  progressPct: number;
  platinum: number | null;
  estimatedCost: number | null;
  masteryReq: number;
  pinned: boolean;
  // roadmap
  access?: string;
  accessLabel?: string;
  relicProbability?: number;
  relevantRelicCount?: number;
  xpPerPlatinum?: number;
  missingParts?: string[];
}

interface Part {
  uniqueName: string;
  name: string;
  need: number;
  owned: number;
  state: "owned" | "blueprint" | "building" | "missing";
  building: boolean;
  missing: number;
}

interface ItemsView extends DataStatus {
  rows: MasteryRow[];
  matched: number;
  offset: number;
  limit: number;
}

interface RoadmapView extends DataStatus {
  easy: MasteryRow[];
  relics: MasteryRow[];
  platinum: MasteryRow[];
  counts: { easy: number; relics: number; platinum: number };
  easyXp: number;
  easyRank?: number | null;
}

interface PlannedView extends DataStatus {
  rows: (MasteryRow & { parts: Part[]; completeness: number; craftableNow: boolean })[];
  limit: number;
}

interface QamView extends DataStatus {
  summary: Summary | null;
  easy: MasteryRow[];
  easyCount: number;
}

// ---- helpers --------------------------------------------------------------------------------

const fmt = (n: number | null | undefined) => (n == null ? "–" : Math.round(n).toLocaleString("en-US"));
const mrLabel = (r: number) => (r > 30 ? `L${r - 30}` : String(r));
const STATUS_COLOR: Record<Status, string> = { mastered: colors.good, progress: colors.warn, missing: colors.muted };
const STATUS_LABEL: Record<Status, string> = { mastered: "Mastered", progress: "In progress", missing: "Missing" };
const PART_COLOR: Record<Part["state"], string> = { owned: colors.good, blueprint: colors.accent, building: colors.warn, missing: colors.bad };
const PART_LABEL: Record<Part["state"], string> = { owned: "Owned", blueprint: "Blueprint", building: "Building", missing: "Missing" };

// Returns a stable callback that runs fn at most once per `ms` (leading call delayed to the
// window, trailing call kept), for event storms such as market.price while a prefetch drains.
export function useThrottled(fn: () => void, ms: number): () => void {
  const ref = useRef(fn);
  ref.current = fn;
  const st = useRef<{ timer: ReturnType<typeof setTimeout> | null; last: number }>({ timer: null, last: 0 });
  useEffect(
    () => () => {
      if (st.current.timer) clearTimeout(st.current.timer);
    },
    [],
  );
  return useCallback(() => {
    const s = st.current;
    if (s.timer) return;
    s.timer = setTimeout(() => {
      s.timer = null;
      s.last = Date.now();
      ref.current();
    }, Math.max(0, s.last + ms - Date.now()));
  }, [ms]);
}

export function openPage(tab: string): void {
  openTab(tab);
  Navigation.CloseSideMenus();
  Navigation.Navigate(ROUTE);
}

// Gamepad tab strip (one row of buttons, the active one highlighted). Shared by the
// Mastery / Syndicates / Missions pages.
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

export const Bar: FC<{ value: number; color?: string; width?: string | number }> = ({ value, color, width = "100%" }) => (
  <div style={{ width, height: "6px", background: "rgba(255,255,255,0.10)", borderRadius: "3px", overflow: "hidden" }}>
    <div style={{ width: `${Math.max(0, Math.min(100, value * 100))}%`, height: "100%", background: color ?? colors.accent }} />
  </div>
);

const NoData: FC<{ s: DataStatus }> = ({ s }) =>
  !s.data ? (
    <Empty text="Mastery data is not installed (scripts/fetch-data.sh builds data/mastery.json.gz)." />
  ) : (
    <Empty text="No inventory yet. Start Warframe (or import an inventory file in Settings) to see mastery progress." />
  );

function mrText(s: Summary): string {
  const p = s.profile;
  if (!p) return "MR –";
  return `MR ${mrLabel(p.rank)}${s.mrLine ? `  ·  ${s.mrLine}` : ""}`;
}

// ---- summary strip ---------------------------------------------------------------------------

const SummaryStrip: FC<{ s: Summary }> = ({ s }) => {
  const p = s.profile;
  return (
    <Section>
      <Focusable style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "4px 0" }} focusClassName="gpfocus" onActivate={() => {}}>
        <Stat label="Mastery rank" value={p ? mrLabel(p.rank) : "–"} color={colors.accent} />
        <Stat label="To next" value={p?.testReady ? "Test ready" : p?.xpForNext != null && p.xpIntoRank != null ? `${fmt(p.xpForNext - p.xpIntoRank)} XP` : "–"} color={p?.testReady ? colors.good : undefined} />
        <Stat label="Mastered" value={`${fmt(s.mastered)} / ${fmt(s.total)}`} color={colors.good} />
        <Stat label="In progress" value={fmt(s.inProgress)} color={colors.warn} />
        <Stat label="Missing" value={fmt(s.missing)} />
        <Stat label="Complete" value={`${s.percent}%`} />
      </Focusable>
      {p?.percentToNext != null && (
        <div style={{ marginTop: "8px" }}>
          <Bar value={p.percentToNext / 100} color={p.testReady ? colors.good : colors.accent} />
        </div>
      )}
      {s.projection?.text && <div style={{ marginTop: "6px", fontSize: "13px", color: colors.muted }}>{s.projection.text}</div>}
    </Section>
  );
};

const CompletionPanel: FC<{ s: Summary }> = ({ s }) => {
  const c = s.completion;
  if (!c) return null;
  const line = (label: string, p: Pair) => <Row key={label} label={label} value={<span>{fmt(p.done)} / {fmt(p.total)}</span>} />;
  return (
    <Section title="Account">
      {line("Star chart", c.starChart.normal)}
      {line("Junctions", c.starChart.junctions)}
      {line("Steel Path", c.starChart.steelPath)}
      {line("Railjack intrinsics", c.intrinsics.railjack)}
      {line("Drifter intrinsics", c.intrinsics.drifter)}
      {s.profile?.totalXp != null && <Row label="Mastery XP (reconstructed)" value={fmt(s.profile.totalXp)} />}
    </Section>
  );
};

// ---- rows -----------------------------------------------------------------------------------

const ItemRow: FC<{ r: MasteryRow; right?: ReactNode; sub?: ReactNode; onActivate?: () => void }> = ({ r, right, sub, onActivate }) => (
  <Row
    onActivate={onActivate}
    label={
      <span>
        {r.name}
        {r.vaulted && <Badge text="Vaulted" color={colors.muted} />}
        {r.needsGilding && <Badge text="Gild" color={colors.warn} />}
        {r.pinned && <FaStar style={{ marginLeft: "6px", color: colors.ducat, fontSize: "11px" }} />}
      </span>
    }
    sub={
      sub ?? (
        <span>
          <span style={{ color: STATUS_COLOR[r.status] }}>{STATUS_LABEL[r.status]}</span>
          {" · "}
          {r.category} · rank {r.rank}/{r.maxRank}
          {r.partsTotal > 0 && r.status !== "mastered" && ` · parts ${r.partsOwned}/${r.partsTotal}`}
          {r.foundryState === "claimable" && " · claim in foundry"}
          {r.foundryState === "building" && " · building"}
          {r.masteryReq > 0 && ` · MR ${r.masteryReq}`}
        </span>
      )
    }
    value={
      right ?? (
        <span style={{ fontVariantNumeric: "tabular-nums" }}>
          {r.masteryXpRemaining > 0 ? <span style={{ color: colors.good }}>+{fmt(r.masteryXpRemaining)}</span> : <span style={{ color: colors.muted }}>done</span>}
          {r.platinum != null && (
            <div style={{ fontSize: "12px" }}>
              <Plat value={r.platinum} />
            </div>
          )}
        </span>
      )
    }
  />
);

// Item detail in a modal: B closes only the modal and the tab underneath (page, filters,
// focus) stays as it was.
const ItemDetail: FC<{ unique: string; closeModal?: () => void; onPinned: () => void }> = ({ unique, closeModal, onPinned }) => {
  const d = useRpc<(MasteryRow & { parts: Part[] }) | null>("mastery.item", { unique_name: unique }, [unique]);
  const it = d.data;
  const onClose = () => closeModal?.();
  return (
    <ModalRoot closeModal={closeModal} onCancel={closeModal}>
      <Section title={it?.name ?? "Item"} right={<span style={{ color: colors.muted, fontSize: "12px" }}>B to close</span>}>
        {d.error && <ErrorText error={d.error} />}
        {!it && !d.error && <Loading />}
        {it && (
          <>
            <div style={{ color: colors.muted, fontSize: "13px", marginBottom: "6px" }}>
              {STATUS_LABEL[it.status]} · {it.category} · rank {it.rank}/{it.maxRank} · {fmt(it.masteryXpRemaining)} mastery XP left
              {it.estimatedCost != null && <> · buy missing ≈ <Plat value={it.estimatedCost} /></>}
            </div>
            {it.parts.length === 0 && <Empty text="No crafted parts (bought or built from resources)." />}
            {it.parts.map((p) => (
              <Row
                key={p.uniqueName}
                label={p.name}
                sub={p.need > 1 ? `${p.owned}/${p.need}` : undefined}
                value={<span style={{ color: PART_COLOR[p.state] }}>{PART_LABEL[p.state]}</span>}
              />
            ))}
            <Focusable flow-children="horizontal" style={{ display: "flex", gap: "8px", marginTop: "10px" }}>
              <DialogButton
                style={{ width: "auto", minWidth: 0, padding: "6px 16px" }}
                onClick={async () => {
                  try {
                    await call("mastery.pin", { unique_name: it.uniqueName, pinned: !it.pinned });
                  } catch (e) {
                    console.error("mastery.pin", e);
                  }
                  d.reload();
                  onPinned();
                }}
              >
                {it.pinned ? <FaStar /> : <FaRegStar />} {it.pinned ? "Unpin" : "Pin to Planned"}
              </DialogButton>
              <DialogButton style={{ width: "auto", minWidth: 0, padding: "6px 16px" }} onClick={onClose}>
                Close
              </DialogButton>
            </Focusable>
          </>
        )}
      </Section>
    </ModalRoot>
  );
};

// ---- tabs -----------------------------------------------------------------------------------

type CatStatus = "all" | Status;
type Sort = "name" | "owned" | "parts_owned" | "mastery_xp" | "platinum";
const SORTS: { data: Sort; label: string }[] = [
  { data: "mastery_xp", label: "Mastery XP left" },
  { data: "name", label: "Name" },
  { data: "owned", label: "Owned first" },
  { data: "parts_owned", label: "Parts owned" },
  { data: "platinum", label: "Platinum" },
];
const PAGE = 20;

let lastCat = "all";
let lastStatus: CatStatus = "missing";
let lastSort: Sort = "mastery_xp";
let lastOffset = 0;

const CollectionTab: FC<{ categories: string[]; byCategory: Summary["byCategory"]; open: (u: string) => void; tick: number }> = ({ categories, byCategory, open, tick }) => {
  const [cat, setCatS] = useState(lastCat);
  const [status, setStatusS] = useState<CatStatus>(lastStatus);
  const [sort, setSortS] = useState<Sort>(lastSort);
  const [offset, setOffsetS] = useState(lastOffset);
  const setOffset = (o: number) => ((lastOffset = o), setOffsetS(o));
  const setCat = (c: string) => ((lastCat = c), setCatS(c), setOffset(0));
  const setStatus = (s: CatStatus) => ((lastStatus = s), setStatusS(s), setOffset(0));
  const setSort = (s: Sort) => ((lastSort = s), setSortS(s), setOffset(0));
  const q = useRpc<ItemsView>("mastery.items", { category: cat, status, sort, offset, limit: PAGE }, [tick]);
  const count = (c: string) => {
    const b = byCategory?.find((x) => x.category === c);
    if (!b) return "";
    const n = status === "all" ? b.total : status === "progress" ? b.inProgress : b[status];
    return ` ${n}`;
  };
  return (
    <>
      <Segmented value={cat} onChange={setCat} options={[{ id: "all", label: "All" }, ...categories.map((c) => ({ id: c, label: `${c}${count(c)}` }))]} />
      <div style={{ display: "flex", gap: "12px", alignItems: "center", marginTop: "8px", flexWrap: "wrap" }}>
        <Segmented
          value={status}
          onChange={setStatus}
          options={[
            { id: "missing", label: "Missing" },
            { id: "progress", label: "In progress" },
            { id: "mastered", label: "Mastered" },
            { id: "all", label: "All" },
          ]}
        />
        <div style={{ minWidth: "260px", flex: 1 }}>
          <DropdownItem label="Sort" rgOptions={SORTS} selectedOption={sort} onChange={(o) => setSort(o.data)} />
        </div>
      </div>
      {q.error && <ErrorText error={q.error} />}
      {!q.data && !q.error && <Loading />}
      {q.data && (
        <Section title={`${q.data.matched} items`}>
          {q.data.rows.length === 0 && <Empty text="Nothing here." />}
          {q.data.rows.map((r) => (
            <ItemRow key={r.uniqueName} r={r} onActivate={() => open(r.uniqueName)} />
          ))}
          {q.data.matched > PAGE && (
            <Focusable flow-children="horizontal" style={{ display: "flex", gap: "8px", alignItems: "center", marginTop: "8px" }}>
              <DialogButton style={{ minWidth: 0, width: "90px" }} disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
                Prev
              </DialogButton>
              <span style={{ color: colors.muted }}>
                {Math.floor(offset / PAGE) + 1} / {Math.ceil(q.data.matched / PAGE)}
              </span>
              <DialogButton style={{ minWidth: 0, width: "90px" }} disabled={offset + PAGE >= q.data.matched} onClick={() => setOffset(offset + PAGE)}>
                Next
              </DialogButton>
            </Focusable>
          )}
        </Section>
      )}
    </>
  );
};

type RoadList = "easy" | "relics" | "platinum";
let lastRoad: RoadList = "easy";

const RoadmapTab: FC<{ open: (u: string) => void; tick: number; profile: Profile | null }> = ({ open, tick, profile }) => {
  const [list, setListS] = useState<RoadList>(lastRoad);
  const setList = (l: RoadList) => ((lastRoad = l), setListS(l));
  const q = useRpc<RoadmapView>("mastery.roadmap", { limit: 100 }, [tick]);
  // mastery.roadmap queues unpriced set/part slugs with notify: reload as they land (throttled).
  const reloadSoon = useThrottled(q.reload, 3000);
  useEvent("market.price", reloadSoon);
  const d = q.data;
  return (
    <>
      <Segmented
        value={list}
        onChange={setList}
        options={[
          { id: "easy", label: `Easy mastery${d ? ` ${d.counts.easy}` : ""}` },
          { id: "relics", label: `From my relics${d ? ` ${d.counts.relics}` : ""}` },
          { id: "platinum", label: `Best XP per plat${d ? ` ${d.counts.platinum}` : ""}` },
        ]}
      />
      {q.error && <ErrorText error={q.error} />}
      {!d && !q.error && <Loading />}
      {d && list === "easy" && (
        <Section
          title="Owned, claimable or buildable now"
          right={
            <span style={{ color: colors.muted, fontSize: "13px" }}>
              +{fmt(d.easyXp)} XP{d.easyRank ? ` → MR ${mrLabel(d.easyRank)}` : profile ? "" : ""}
            </span>
          }
        >
          {d.easy.length === 0 && <Empty text="Nothing easy left: everything you own is mastered." />}
          {d.easy.map((r) => (
            <ItemRow
              key={r.uniqueName}
              r={r}
              onActivate={() => open(r.uniqueName)}
              sub={
                <span>
                  <span style={{ color: colors.accent }}>{r.accessLabel}</span> · {r.category}
                  {r.status === "progress" && ` · rank ${r.rank}/${r.maxRank}`}
                  {r.access === "marketBlueprint" && " · Market blueprint (credits)"}
                </span>
              }
            />
          ))}
        </Section>
      )}
      {d && list === "relics" && (
        <Section title="Chance your owned relics finish the missing parts">
          {d.relics.length === 0 && <Empty text="No unmastered item can be completed from relics you own." />}
          {d.relics.map((r) => (
            <ItemRow
              key={r.uniqueName}
              r={r}
              onActivate={() => open(r.uniqueName)}
              sub={`${r.relevantRelicCount} relics · missing ${r.missingParts?.join(", ")}`}
              right={
                <span>
                  <span style={{ color: colors.good }}>{Math.round((r.relicProbability ?? 0) * 100)}%</span>
                  <div style={{ fontSize: "12px", color: colors.muted }}>+{fmt(r.masteryXpRemaining)} XP</div>
                </span>
              }
            />
          ))}
        </Section>
      )}
      {d && list === "platinum" && (
        <Section title="Cheapest mastery to buy (cached warframe.market prices)">
          {d.platinum.length === 0 && <Empty text="No priced items yet; prices load in the background, check back in a minute." />}
          {d.platinum.map((r) => (
            <ItemRow
              key={r.uniqueName}
              r={r}
              onActivate={() => open(r.uniqueName)}
              sub={`${r.category} · missing ${r.missingParts?.join(", ") || "set"}`}
              right={
                <span>
                  <Plat value={r.estimatedCost} />
                  <div style={{ fontSize: "12px", color: colors.muted }}>{fmt(r.xpPerPlatinum)} XP/p</div>
                </span>
              }
            />
          ))}
        </Section>
      )}
    </>
  );
};

const PlannedTab: FC<{ open: (u: string) => void; tick: number }> = ({ open, tick }) => {
  const q = useRpc<PlannedView>("mastery.planned", {}, [tick]);
  const d = q.data;
  if (q.error) return <ErrorText error={q.error} />;
  if (!d) return <Loading />;
  return (
    <Section title={`Pinned ${d.rows.length} / ${d.limit}`}>
      {d.rows.length === 0 && <Empty text="Nothing pinned. Open an item in Collection or Roadmap and pin it." />}
      {d.rows.map((r) => (
        <ItemRow
          key={r.uniqueName}
          r={r}
          onActivate={() => open(r.uniqueName)}
          sub={
            <span>
              {r.craftableNow ? <span style={{ color: colors.good }}>Craftable now</span> : `${Math.round(r.completeness * 100)}% of parts`}
              {r.parts.filter((p) => p.state === "missing").length > 0 &&
                ` · missing ${r.parts
                  .filter((p) => p.state === "missing")
                  .map((p) => p.name)
                  .join(", ")}`}
            </span>
          }
        />
      ))}
    </Section>
  );
};

// ---- page -----------------------------------------------------------------------------------

type Tab = "collection" | "roadmap" | "planned" | "account";
let lastTab: Tab = "roadmap";
const masteryTabListeners = new Set<(t: Tab) => void>();

// Opens Mastery on a sub-tab; a mounted Mastery page switches to it immediately.
export function openMastery(tab: Tab = "roadmap"): void {
  lastTab = tab;
  masteryTabListeners.forEach((l) => l(tab));
  openPage("mastery");
}

const Mastery: FC = () => {
  const [tab, setTabS] = useState<Tab>(lastTab);
  const [tick, setTick] = useState(0);
  const setTab = (t: Tab) => ((lastTab = t), setTabS(t));
  useEffect(() => {
    masteryTabListeners.add(setTabS);
    return () => {
      masteryTabListeners.delete(setTabS);
    };
  }, []);
  const setDetail = (u: string) => showModal(<ItemDetail unique={u} onPinned={() => setTick((n) => n + 1)} />);
  const s = useRpc<SummaryView>("mastery.summary", {}, [tick]);
  useEvent("mastery.updated", () => setTick((n) => n + 1));
  const d = s.data;
  return (
    <Page>
      {s.error && !d && <ErrorText error={s.error} />}
      {!d && !s.error && <Loading />}
      {d && !d.summary && <NoData s={d} />}
      {d?.summary && (
        <>
          <SummaryStrip s={d.summary} />
          <Segmented
            value={tab}
            onChange={setTab}
            options={[
              { id: "roadmap", label: "Roadmap" },
              { id: "collection", label: "Collection" },
              { id: "planned", label: "Planned" },
              { id: "account", label: "Account" },
            ]}
            style={{ marginBottom: "8px" }}
          />
          {tab === "collection" && <CollectionTab categories={d.categories ?? []} byCategory={d.summary.byCategory} open={setDetail} tick={tick} />}
          {tab === "roadmap" && <RoadmapTab open={setDetail} tick={tick} profile={d.summary.profile} />}
          {tab === "planned" && <PlannedTab open={setDetail} tick={tick} />}
          {tab === "account" && (
            <>
              <CompletionPanel s={d.summary} />
              <Section title="Options">
                <ToggleField
                  label="Hide Founder items"
                  description="Leave Excalibur Prime, Lato Prime and Skana Prime (no longer obtainable) out of the totals and lists."
                  checked={!!d.hideFounder}
                  onChange={async (v) => {
                    try {
                      await call("mastery.configure", { hide_founder: v });
                    } catch (e) {
                      console.error("mastery.configure", e);
                    }
                    setTick((n) => n + 1);
                  }}
                />
              </Section>
              <Section title="By category">
                {(d.summary.byCategory ?? []).map((c) => (
                  <Row
                    key={c.category}
                    label={c.category}
                    sub={<Bar value={c.total ? c.mastered / c.total : 0} color={colors.good} width="220px" />}
                    value={
                      <span>
                        {fmt(c.mastered)} / {fmt(c.total)}
                      </span>
                    }
                    onActivate={() => {
                      lastCat = c.category;
                      lastOffset = 0;
                      setTab("collection");
                    }}
                  />
                ))}
              </Section>
            </>
          )}
        </>
      )}
    </Page>
  );
};

export default Mastery;

// ---- QAM ------------------------------------------------------------------------------------

export const Qam: FC = () => {
  const q = useRpc<QamView>("mastery.qam", { limit: 5 }, [], 120000);
  useEvent("mastery.updated", () => q.reload());
  const d = q.data;
  const s = d?.summary;
  return (
    <PanelSection title="Mastery">
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
          <Field label="No mastery data" description={!d.data ? "data/mastery.json.gz missing" : "Waiting for the inventory"} />
        </PanelSectionRow>
      )}
      {s && (
        <>
          <PanelSectionRow>
            <Field focusable padding="compact" label={mrText(s)} description={s.projection?.text ?? undefined} onActivate={() => openMastery("roadmap")} bottomSeparator="none" />
          </PanelSectionRow>
          {s.profile?.percentToNext != null && (
            <PanelSectionRow>
              <Bar value={s.profile.percentToNext / 100} color={s.profile.testReady ? colors.good : colors.accent} />
            </PanelSectionRow>
          )}
          <PanelSectionRow>
            <Field focusable padding="compact" label="Mastered" onActivate={() => openMastery("collection")}>
              <span style={{ whiteSpace: "nowrap" }}>
                {fmt(s.mastered)} / {fmt(s.total)} ({s.percent}%)
              </span>
            </Field>
          </PanelSectionRow>
          {d.easy.length > 0 && (
            <PanelSectionRow>
              <div style={{ fontSize: "12px", color: colors.muted, textTransform: "uppercase", marginTop: "4px" }}>Easy mastery ({d.easyCount})</div>
            </PanelSectionRow>
          )}
          {d.easy.map((r) => (
            <PanelSectionRow key={r.uniqueName}>
              <Field focusable padding="compact" label={r.name} description={r.accessLabel} onActivate={() => openMastery("roadmap")}>
                <span style={{ color: colors.good, whiteSpace: "nowrap" }}>+{fmt(r.masteryXpRemaining)}</span>
              </Field>
            </PanelSectionRow>
          ))}
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={() => openMastery("roadmap")}>
              Open mastery
            </ButtonItem>
          </PanelSectionRow>
        </>
      )}
    </PanelSection>
  );
};

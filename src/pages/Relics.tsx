// Relics: owned relics with refinement counts, squad EV (platinum / ducats), rewards,
// filters and sort; plus a QAM section with the best relics to crack and planner controls.
// Backend: py_modules/wfh/menus/relics.py (relics.view/detail/top/push_filters) and
// py_modules/wfh/features/relic_planner.py (relics.planner_*).
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
import { FC, ReactNode, useEffect, useState } from "react";

import { Badge, Ducats, Empty, ErrorText, FilterCell, Loading, Page, Plat, Section, Stat, chip, colors } from "../components/ui";
import { openTab } from "../nav";
import { ROUTE } from "../nav";
import { call, useEvent, useRpc } from "../rpc";

// ---- types (mirror menus/relics.py) ------------------------------------------------

type Quality = "intact" | "exceptional" | "flawless" | "radiant";
type QualityMode = "owned" | Quality;
type SortMode = "tier" | "name" | "ev" | "ducat" | "ducatonator" | "owned";

interface RewardRow {
  name: string;
  uniqueName: string | null;
  slug: string | null;
  rarity: string;
  chance: number;
  platinum: number | null;
  ducats: number | null;
  owned: number | null;
  needed: boolean | null;
}

interface RelicGroup {
  key: string;
  name: string;
  tier: string;
  code: string;
  vaulted: boolean;
  owned: Record<Quality, number>;
  ownedTotal: number;
  quality: Quality;
  qualityLabel: string;
  count: number;
  label: string;
  plat: number | null;
  ducat: number | null;
  ratio: number | null;
  state: "has-value" | "no-data";
  rewards?: RewardRow[];
}

interface RelicView {
  stale: boolean;
  generatedAt: number;
  hasInventory: boolean;
  inventory: { origin: string; syncTime: number | null; fetchedAt: number } | null;
  squad: number;
  quality: QualityMode;
  total: number;
  offset: number;
  limit: number;
  groups: RelicGroup[];
  tierCounts: Record<string, number>;
  totals: { relics: number; groups: number; plat: number | null; ducat: number | null };
  pricesMissing: number;
}

interface RefinementInfo {
  quality: Quality;
  label: string;
  owned: number;
  plat: number | null;
  ducat: number | null;
  gainOverIntact: number | null;
  traces: number;
  platPer100Traces: number | null;
}

interface RelicDetail {
  name: string;
  tier: string;
  vaulted: boolean;
  owned: Record<Quality, number>;
  ownedTotal: number;
  hasInventory: boolean;
  squad: number;
  bestOwned: Quality | null;
  refinementTable: RefinementInfo[];
  evBySquad: Record<string, Record<Quality, { plat: number | null; ducat: number | null }>>;
  rewards: Record<Quality, RewardRow[]>;
  pricesMissing: number;
}

interface TopResult {
  hasInventory: boolean;
  squad: number;
  total: number;
  pricesMissing?: number;
  rows: RelicGroup[];
}

// relics.planner event: compact mirror of the overlay (top rows without rewards).
interface PlannerPayload {
  state: "scanning" | "rows" | "error" | "closed";
  era?: string | null;
  count?: number;
  rows: Pick<RelicGroup, "key" | "name" | "quality" | "count">[];
  message?: string;
  pricesMissing?: number;
  at: number;
}

// ---- constants ------------------------------------------------------------------------

const QUALITIES: Quality[] = ["intact", "exceptional", "flawless", "radiant"];
const Q_SHORT: Record<Quality, string> = { intact: "Int", exceptional: "Exc", flawless: "Fla", radiant: "Rad" };
const Q_LABEL: Record<Quality, string> = { intact: "Intact", exceptional: "Exceptional", flawless: "Flawless", radiant: "Radiant" };
const TIERS = ["all", "Lith", "Meso", "Neo", "Axi", "Requiem"];
const SORTS: { data: SortMode; label: string }[] = [
  { data: "ev", label: "Platinum EV" },
  { data: "ducat", label: "Ducat EV" },
  { data: "ducatonator", label: "Ducats per plat" },
  { data: "owned", label: "Copies owned" },
  { data: "tier", label: "Tier" },
  { data: "name", label: "Name" },
];
const DEFAULT_DESC: SortMode[] = ["ev", "ducat", "ducatonator", "owned"];
const RARITY_COLOR: Record<string, string> = { Rare: "#ffd666", Uncommon: "#c8d0dc", Common: "#cd966e" };
const PAGE_SIZE = 20;

interface ViewState {
  tier: string;
  ownership: "owned" | "all";
  quality: QualityMode;
  squad: number;
  sort: SortMode;
  direction: "asc" | "desc";
  vaulted: "all" | "vaulted" | "unvaulted";
  ownedAbove: number;
  search: string;
  needed: boolean;
  pinned: Record<string, Quality>;
}

const DEFAULT_STATE: ViewState = {
  tier: "all",
  ownership: "owned",
  quality: "owned",
  squad: 1,
  sort: "ev",
  direction: "desc",
  vaulted: "all",
  ownedAbove: 0,
  search: "",
  needed: false,
  pinned: {},
};

const STORE_KEY = "wfh.relics.view";

function loadState(): ViewState {
  try {
    const raw = localStorage.getItem(STORE_KEY);
    if (raw) return { ...DEFAULT_STATE, ...JSON.parse(raw) };
  } catch {
    /* private storage unavailable */
  }
  return DEFAULT_STATE;
}

function saveState(s: ViewState): void {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(s));
  } catch {
    /* ignore */
  }
}

// ---- small pieces --------------------------------------------------------------------

const fmt1 = (v: number | null | undefined, unit: string): string => (v == null ? "–" : `${(Math.round(v * 10) / 10).toFixed(1)}${unit}`);

const Chance: FC<{ value: number }> = ({ value }) => <span>{value >= 10 ? value.toFixed(0) : value.toFixed(1)}%</span>;

const OwnedMark: FC<{ r: RewardRow }> = ({ r }) => {
  if (r.owned == null) return null;
  if (r.needed) return <span style={{ color: colors.good, fontSize: "11px" }}>need</span>;
  return <span style={{ color: colors.muted, fontSize: "11px" }}>own {r.owned}</span>;
};

const VaultBadge: FC<{ vaulted: boolean }> = ({ vaulted }) =>
  vaulted ? <Badge text="Vaulted" color={colors.bad} /> : <Badge text="Unvaulted" color="#60a5fa" />;

const OwnedCounts: FC<{ owned: Record<Quality, number>; selected?: Quality }> = ({ owned, selected }) => (
  <span style={{ fontSize: "12px" }}>
    {QUALITIES.filter((q) => owned[q] > 0).map((q) => (
      <span key={q} style={{ marginRight: "8px", color: q === selected ? colors.accent : colors.muted, fontWeight: q === selected ? 600 : 400 }}>
        {Q_SHORT[q]} ×{owned[q]}
      </span>
    ))}
  </span>
);

// Segmented 1-4 squad picker (D-pad left/right).
const SquadPicker: FC<{ value: number; onChange: (n: number) => void; compact?: boolean }> = ({ value, onChange, compact }) => (
  <Focusable flow-children="horizontal" style={{ display: "flex", gap: "4px" }}>
    {[1, 2, 3, 4].map((n) => (
      <DialogButton
        key={n}
        onClick={() => onChange(n)}
        style={{
          minWidth: 0,
          width: compact ? "48px" : "44px",
          padding: "4px 0",
          background: n === value ? colors.accent : undefined,
          color: n === value ? "#0e121e" : undefined,
          fontWeight: n === value ? 700 : 400,
        }}
      >
        {n}
      </DialogButton>
    ))}
  </Focusable>
);

// ---- detail modal ----------------------------------------------------------------------

const DetailModal: FC<{ name: string; squad: number; pin?: PinControl; closeModal?: () => void }> = ({
  name,
  squad: initialSquad,
  pin,
  closeModal,
}) => {
  const [squad, setSquad] = useState(initialSquad);
  const [pinned, setPinned] = useState<Quality | undefined>(pin?.current);
  const [q, setQ] = useState<Quality | null>(null);
  const st = useRpc<RelicDetail | null>("relics.detail", { name, squad }, []);
  const d = st.data;
  const quality: Quality = q ?? d?.bestOwned ?? "intact";
  return (
    <ModalRoot closeModal={closeModal} onCancel={closeModal}>
      <div style={{ color: colors.text, minWidth: "640px" }}>
        {st.error && <ErrorText error={st.error} />}
        {d === undefined && !st.error && <Loading />}
        {d === null && <Empty text="Relic not found." />}
        {d && (
          <>
            <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "8px" }}>
              <span style={{ fontSize: "20px", fontWeight: 700 }}>{d.name}</span>
              <VaultBadge vaulted={d.vaulted} />
              <span style={{ flex: 1 }} />
              {d.hasInventory && <OwnedCounts owned={d.owned} selected={quality} />}
            </div>
            <Focusable style={{ display: "flex", gap: "12px", alignItems: "center", marginBottom: "8px" }} flow-children="horizontal">
              <span style={{ color: colors.muted }}>Squad</span>
              <SquadPicker value={squad} onChange={setSquad} />
              <span style={{ color: colors.muted, marginLeft: "12px" }}>Refinement</span>
              <Focusable flow-children="horizontal" style={{ display: "flex", gap: "4px" }}>
                {QUALITIES.map((x) => (
                  <DialogButton
                    key={x}
                    onClick={() => setQ(x)}
                    style={{ minWidth: 0, width: "64px", padding: "4px 0", background: x === quality ? colors.accent : undefined, color: x === quality ? "#0e121e" : undefined }}
                  >
                    {Q_SHORT[x]}
                  </DialogButton>
                ))}
              </Focusable>
            </Focusable>
            {/* EV per refinement */}
            <div style={{ display: "grid", gridTemplateColumns: "110px repeat(4, 1fr)", gap: "2px 10px", fontSize: "13px", marginBottom: "10px" }}>
              <span style={{ color: colors.muted }}>EV (squad {squad})</span>
              {d.refinementTable.map((r) => (
                <span key={r.quality} style={{ color: r.quality === quality ? colors.accent : colors.muted, fontWeight: 600 }}>
                  {r.label}
                  {r.quality === d.bestOwned ? " ★" : ""}
                  {r.quality === pinned ? " (pinned)" : ""}
                </span>
              ))}
              <span style={{ color: colors.muted }}>Platinum</span>
              {d.refinementTable.map((r) => (
                <Plat key={r.quality} value={r.plat} />
              ))}
              <span style={{ color: colors.muted }}>Ducats</span>
              {d.refinementTable.map((r) => (
                <span key={r.quality} style={{ color: colors.ducat }}>{fmt1(r.ducat, "d")}</span>
              ))}
              <span style={{ color: colors.muted }}>vs Intact</span>
              {d.refinementTable.map((r) => (
                <span key={r.quality} style={{ color: (r.gainOverIntact ?? 0) > 0 ? colors.good : colors.muted }}>
                  {r.quality === "intact" ? "–" : `${(r.gainOverIntact ?? 0) >= 0 ? "+" : ""}${fmt1(r.gainOverIntact, "p")} / ${r.traces} traces`}
                </span>
              ))}
            </div>
            {/* rewards */}
            <Focusable style={{ display: "flex", flexDirection: "column" }}>
              {(d.rewards[quality] ?? []).map((r) => (
                <Focusable
                  key={r.name}
                  focusClassName="gpfocus"
                  onActivate={() => {}}
                  style={{ display: "grid", gridTemplateColumns: "1fr 60px 60px 60px 60px", gap: "8px", padding: "4px 2px", borderBottom: `1px solid ${colors.border}`, alignItems: "center" }}
                >
                  <span style={{ color: RARITY_COLOR[r.rarity] ?? colors.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.name}</span>
                  <span style={{ color: colors.muted, textAlign: "right" }}>
                    <Chance value={r.chance} />
                  </span>
                  <span style={{ textAlign: "right" }}>
                    <Plat value={r.platinum} />
                  </span>
                  <span style={{ textAlign: "right" }}>
                    <Ducats value={r.ducats} />
                  </span>
                  <span style={{ textAlign: "right" }}>
                    <OwnedMark r={r} />
                  </span>
                </Focusable>
              ))}
            </Focusable>
            {d.pricesMissing > 0 && (
              <div style={{ color: colors.muted, fontSize: "12px", marginTop: "6px" }}>
                {d.pricesMissing} reward price(s) not cached yet; EV counts them as 0 until they load.
              </div>
            )}
            <Focusable style={{ display: "flex", justifyContent: "flex-end", gap: "8px", marginTop: "10px" }} flow-children="horizontal">
              {pin && d.hasInventory && (pinned === quality || (d.owned[quality] ?? 0) > 0) && (
                <DialogButton
                  style={{ width: "220px" }}
                  onClick={() => {
                    const next = pinned === quality ? undefined : quality;
                    setPinned(next);
                    pin.set(next ?? null);
                  }}
                >
                  {pinned === quality ? `Unpin ${Q_LABEL[quality]}` : `Pin ${Q_LABEL[quality]}`}
                </DialogButton>
              )}
              <DialogButton style={{ width: "140px" }} onClick={closeModal}>
                Close
              </DialogButton>
            </Focusable>
          </>
        )}
      </div>
    </ModalRoot>
  );
};

// Pin: the page's per-relic refinement choice (used when Refinement = "Best owned";
// pushed to the overlay as pinnedQualities). The QAM opens the modal without one.
interface PinControl {
  current?: Quality;
  set: (q: Quality | null) => void;
}

function openDetail(name: string, squad: number, pin?: PinControl): void {
  showModal(<DetailModal name={name} squad={squad} pin={pin} />);
}

// ---- relic card --------------------------------------------------------------------------

const RelicCard: FC<{ g: RelicGroup; squad: number; showOwned: boolean; pin: PinControl }> = ({ g, squad, showOwned, pin }) => (
  <Focusable
    focusClassName="gpfocus"
    onActivate={() => openDetail(g.name, squad, pin)}
    style={{
      background: colors.panel,
      border: `1px solid ${colors.border}`,
      borderRadius: "6px",
      padding: "8px 10px",
      display: "flex",
      flexDirection: "column",
      gap: "4px",
      minWidth: 0,
    }}
  >
    <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
      <span style={{ fontWeight: 700, fontSize: "15px" }}>{g.name}</span>
      <span style={{ color: colors.muted, fontSize: "12px" }}>{g.qualityLabel}</span>
      <VaultBadge vaulted={g.vaulted} />
    </div>
    {showOwned && (g.ownedTotal > 0 ? <OwnedCounts owned={g.owned} selected={g.quality} /> : <span style={{ fontSize: "12px", color: colors.muted }}>Not owned</span>)}
    <div style={{ display: "flex", gap: "12px", fontSize: "14px", alignItems: "baseline" }}>
      <span style={{ color: colors.muted, fontSize: "12px" }}>EV</span>
      {g.state === "no-data" ? <span style={{ color: colors.muted }}>no prices</span> : <span style={{ color: colors.plat, fontWeight: 600 }}>{fmt1(g.plat, "p")}</span>}
      <span style={{ color: colors.ducat, fontWeight: 600 }}>{fmt1(g.ducat, "d")}</span>
      {g.ratio != null && <span style={{ color: colors.muted, fontSize: "12px" }}>{g.ratio.toFixed(1)} d/p</span>}
    </div>
    <div style={{ display: "flex", flexDirection: "column", fontSize: "12px" }}>
      {(g.rewards ?? []).map((r) => (
        <div key={r.name} style={{ display: "grid", gridTemplateColumns: "1fr 44px 40px 40px", gap: "6px" }}>
          <span style={{ color: RARITY_COLOR[r.rarity] ?? colors.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.name}</span>
          <span style={{ color: colors.muted, textAlign: "right" }}>
            <Chance value={r.chance} />
          </span>
          <span style={{ color: colors.plat, textAlign: "right" }}>{r.platinum == null ? "–" : `${Math.round(r.platinum)}p`}</span>
          <span style={{ textAlign: "right" }}>
            <OwnedMark r={r} />
          </span>
        </div>
      ))}
    </div>
  </Focusable>
);

// ---- full page ------------------------------------------------------------------------------

const GRID_COLUMNS = 3;

function chunk<T>(items: T[], n: number): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < items.length; i += n) out.push(items.slice(i, i + n));
  return out;
}

const OverlayFiltersModal: FC<{ closeModal?: () => void; onPush: () => Promise<void>; onClear: () => Promise<void> }> = ({ closeModal, onPush, onClear }) => (
  <ModalRoot onCancel={closeModal} onEscKeypress={closeModal}>
    <div style={{ color: colors.text }}>
      <div style={{ fontSize: "18px", fontWeight: 700, marginBottom: "6px" }}>Relic planner overlay</div>
      <div style={{ color: colors.muted, marginBottom: "12px" }}>The planner overlay ranks relics by EV unless you give it this page's filters.</div>
      <Focusable flow-children="horizontal" style={{ display: "flex", gap: "8px" }}>
        <DialogButton onClick={() => void onPush().then(() => closeModal?.())}>Use these filters</DialogButton>
        <DialogButton onClick={() => void onClear().then(() => closeModal?.())}>Rank by EV again</DialogButton>
        <DialogButton onClick={closeModal}>Close</DialogButton>
      </Focusable>
    </div>
  </ModalRoot>
);

const Relics: FC = () => {
  const [s, setS] = useState<ViewState>(loadState);
  const [page, setPage] = useState(0);
  const [searchDraft, setSearchDraft] = useState(s.search);
  const [pushed, setPushed] = useState<string>();
  const update = (patch: Partial<ViewState>) => {
    setS((prev) => {
      const next = { ...prev, ...patch };
      saveState(next);
      return next;
    });
    setPage(0);
  };
  // TextField opens the Steam keyboard; debounce like WFHelper (250 ms).
  useEffect(() => {
    const t = setTimeout(() => searchDraft !== s.search && update({ search: searchDraft }), 250);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchDraft]);

  const params = {
    tier: s.tier,
    ownership: s.ownership,
    quality: s.quality,
    squad: s.squad,
    sort: s.sort,
    direction: s.direction,
    vaulted: s.vaulted,
    ownedAbove: s.ownedAbove,
    search: s.search,
    needed: s.needed,
    pinned: s.pinned,
    offset: page * PAGE_SIZE,
    limit: PAGE_SIZE,
  };
  const view = useRpc<RelicView>("relics.view", params, []);
  useEvent("inventory.updated", () => view.reload());
  const v = view.data;
  const pages = v ? Math.max(1, Math.ceil(v.total / PAGE_SIZE)) : 1;

  const pushToOverlay = async () => {
    const f = await call<Record<string, unknown>>("relics.push_filters", {
      push: {
        squadSize: s.squad,
        search: s.search,
        containsNeededReward: s.needed,
        vaultedMode: s.vaulted,
        qualityMode: s.quality,
        ownedAbove: s.ownedAbove,
        sortMode: s.sort,
        sortDirection: s.direction,
        tierFilter: s.tier === "all" ? null : s.tier,
        neededRewardKeys: null,
        pinnedQualities: s.pinned,
      },
    });
    setPushed(`Overlay uses these filters (squad ${f.squadSize}).`);
  };

  const noInv = v ? !v.hasInventory : false;

  // Compact filter bar above the relics: two rows of small controls instead of
  // a column of full-width Steam rows, so the cards get the whole width.
  const filters = (
    <Focusable style={{ display: "flex", flexDirection: "column", gap: "8px", marginTop: "8px" }}>
      <Focusable flow-children="horizontal" style={{ display: "grid", gridTemplateColumns: "repeat(6, minmax(0, 1fr))", gap: "8px" }}>
        <FilterCell label="Tier">
          <Dropdown
            rgOptions={TIERS.map((t) => ({ data: t, label: `${t === "all" ? "All tiers" : t}${v?.tierCounts?.[t] != null ? ` (${v.tierCounts[t]})` : ""}` }))}
            selectedOption={s.tier}
            onChange={(o) => update({ tier: o.data })}
          />
        </FilterCell>
        <FilterCell label="Sort">
          <Dropdown rgOptions={SORTS} selectedOption={s.sort} onChange={(o) => update({ sort: o.data, direction: DEFAULT_DESC.includes(o.data) ? "desc" : "asc" })} />
        </FilterCell>
        <FilterCell label="Show">
          <Dropdown
            rgOptions={[
              { data: "owned", label: "Owned relics" },
              { data: "all", label: "All relics" },
            ]}
            selectedOption={s.ownership}
            onChange={(o) => update({ ownership: o.data, quality: o.data === "owned" ? "owned" : "intact" })}
          />
        </FilterCell>
        <FilterCell label="Refinement">
          <Dropdown
            rgOptions={[{ data: "owned", label: "Best owned" }, ...QUALITIES.map((q) => ({ data: q, label: Q_LABEL[q] }))]}
            selectedOption={s.quality}
            onChange={(o) => update({ quality: o.data })}
          />
        </FilterCell>
        <FilterCell label="Vaulted">
          <Dropdown
            rgOptions={[
              { data: "all", label: "Any" },
              { data: "vaulted", label: "Vaulted only" },
              { data: "unvaulted", label: "Unvaulted only" },
            ]}
            selectedOption={s.vaulted}
            onChange={(o) => update({ vaulted: o.data })}
          />
        </FilterCell>
        <FilterCell label="Copies owned">
          <Dropdown
            disabled={noInv}
            rgOptions={[0, 2, 4, 6, 8, 10].map((n) => ({ data: n, label: n === 0 ? "Any" : `More than ${n}` }))}
            selectedOption={s.ownedAbove}
            onChange={(o) => update({ ownedAbove: o.data })}
          />
        </FilterCell>
      </Focusable>
      <Focusable flow-children="horizontal" style={{ display: "flex", gap: "8px", alignItems: "flex-end", flexWrap: "wrap" }}>
        <FilterCell label="Squad">
          <SquadPicker value={s.squad} onChange={(n) => update({ squad: n })} />
        </FilterCell>
        <DialogButton style={chip(s.direction === "desc")} onClick={() => update({ direction: s.direction === "desc" ? "asc" : "desc" })}>
          {s.direction === "desc" ? "High → low" : "Low → high"}
        </DialogButton>
        <DialogButton style={chip(s.needed)} disabled={noInv} onClick={() => update({ needed: !s.needed })}>
          Has a reward I need
        </DialogButton>
        <div style={{ width: "220px" }}>
          <TextField label="" value={searchDraft} onChange={(e) => setSearchDraft(e.target.value)} />
        </div>
        <DialogButton style={chip(false)} onClick={() => showModal(<OverlayFiltersModal onPush={pushToOverlay} onClear={async () => { await call("relics.clear_filters"); setPushed("Overlay ranks by EV again."); }} />)}>
          Overlay…
        </DialogButton>
        <DialogButton
          style={chip(false)}
          onClick={async () => {
            await call("relics.prefetch_prices");
            setTimeout(() => view.reload(), 4000);
          }}
        >
          Fetch prices
        </DialogButton>
        <DialogButton
          style={chip(false)}
          onClick={() => {
            setSearchDraft("");
            update({ ...DEFAULT_STATE });
          }}
        >
          Reset
        </DialogButton>
      </Focusable>
      {pushed && <div style={{ color: colors.good, fontSize: "12px" }}>{pushed}</div>}
    </Focusable>
  );

  let body: ReactNode;
  if (view.error && !v) body = <ErrorText error={view.error} />;
  else if (!v) body = <Loading />;
  else if (!v.hasInventory && s.ownership === "owned")
    body = <Empty text="No inventory yet. Sync it in the Inventory tab, or choose Show: All relics." />;
  else if (v.groups.length === 0) body = <Empty text="No relics match these filters." />;
  else
    body = (
      <>
        <Focusable style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
          {chunk(v.groups, GRID_COLUMNS).map((row) => (
            // One horizontal group per row: left/right moves within it,
            // up/down moves to the card above or below.
            <Focusable key={row[0].key} flow-children="horizontal" style={{ display: "grid", gridTemplateColumns: `repeat(${GRID_COLUMNS}, minmax(0, 1fr))`, gap: "8px" }}>
              {row.map((g) => (
                <RelicCard
                  key={g.key}
                  g={g}
                  squad={s.squad}
                  showOwned={v.hasInventory}
                  pin={{
                    current: s.pinned[g.key],
                    set: (q) => {
                      const next = { ...s.pinned };
                      if (q) next[g.key] = q;
                      else delete next[g.key];
                      update({ pinned: next });
                    },
                  }}
                />
              ))}
            </Focusable>
          ))}
        </Focusable>
        {pages > 1 && (
          <Focusable style={{ display: "flex", gap: "8px", alignItems: "center", marginTop: "10px" }} flow-children="horizontal">
            <DialogButton style={{ minWidth: 0, width: "100px" }} disabled={page === 0} onClick={() => setPage(page - 1)}>
              Prev
            </DialogButton>
            <span style={{ color: colors.muted }}>
              {page + 1} / {pages} · {v.total} relics
            </span>
            <DialogButton style={{ minWidth: 0, width: "100px" }} disabled={page >= pages - 1} onClick={() => setPage(page + 1)}>
              Next
            </DialogButton>
          </Focusable>
        )}
      </>
    );

  return (
    <Page>
      <Focusable style={{ display: "flex", flexDirection: "column" }}>
        {filters}
        {v && (
          <Section
            title={s.ownership === "owned" ? "Owned relics" : "All relics"}
            right={view.loading ? <span style={{ color: colors.muted, fontSize: "12px" }}>Updating…</span> : undefined}
          >
            {v.hasInventory && (
              <>
                <Stat label="Relics" value={v.totals.relics} />
                <Stat label="Kinds" value={v.totals.groups} />
                {v.totals.plat != null && <Stat label="Crack all (EV)" value={`${Math.round(v.totals.plat)}p`} color={colors.plat} />}
                {v.totals.ducat != null && <Stat label="Ducat EV" value={`${Math.round(v.totals.ducat)}d`} color={colors.ducat} />}
              </>
            )}
            <Stat label="Squad" value={v.squad} />
            {v.pricesMissing > 0 && (
              <div style={{ color: colors.muted, fontSize: "12px", marginTop: "4px" }}>
                {v.pricesMissing} reward prices not cached yet (counted as 0). Use “Fetch prices”.
              </div>
            )}
          </Section>
        )}
        {body}
      </Focusable>
    </Page>
  );
};

export default Relics;

// ---- QAM section ----------------------------------------------------------------------------

let qamEra = "auto";

interface PlannerSettings {
  relic_planner?: { show_rewards?: boolean; enabled?: boolean; squad_size?: number; filters?: { squadSize?: number } | null };
}

export const Qam: FC = () => {
  const settings = useRpc<PlannerSettings>("settings.get", {}, []);
  const rp = settings.data?.relic_planner;
  // The QAM squad is the overlay's squad (relic_planner.squad_size) unless the page pushed filters.
  const [squadPick, setSquadPick] = useState<number>();
  const squad = squadPick ?? rp?.filters?.squadSize ?? rp?.squad_size ?? 4;
  const [era, setEra] = useState(qamEra);
  // "auto": the list shows all tiers and the overlay detects the era; "all": all tiers in both;
  // a tier filters the list and forces that era on the overlay.
  const listEra = era === "auto" || era === "all" ? null : era;
  const top = useRpc<TopResult>("relics.top", { limit: 8, squad, era: listEra }, [], 60000);
  const [showStatus, setShowStatus] = useState<string>();
  const [planner, setPlanner] = useState<PlannerPayload>();
  useEvent("relics.planner", (p: PlannerPayload) => setPlanner(p));
  useEvent("inventory.updated", () => top.reload());
  const t = top.data;
  return (
    <PanelSection title="Relics to crack">
      <PanelSectionRow>
        <Field label="Squad" childrenLayout="below" focusable={false}>
          <SquadPicker
            compact
            value={squad}
            onChange={async (n) => {
              setSquadPick(n);
              const patch: Record<string, unknown> = { squad_size: n };
              if (rp?.filters) patch.filters = { ...rp.filters, squadSize: n };
              await call("settings.update", { patch: { relic_planner: patch } });
              settings.reload();
            }}
          />
        </Field>
      </PanelSectionRow>
      <PanelSectionRow>
        <DropdownItem
          label="Tier"
          description={era === "auto" ? "List: all tiers · overlay: detect era" : era === "all" ? "List and overlay: all tiers" : "Overlay forced to this tier"}
          rgOptions={[
            { data: "auto", label: "Auto-detect" },
            { data: "all", label: "All tiers" },
            ...TIERS.slice(1).map((x) => ({ data: x, label: x })),
          ]}
          selectedOption={era}
          onChange={(o) => {
            qamEra = o.data;
            setEra(o.data);
          }}
        />
      </PanelSectionRow>
      {top.error && !t && (
        <PanelSectionRow>
          <Field label="Error" description={top.error} />
        </PanelSectionRow>
      )}
      {t && !t.hasInventory && (
        <PanelSectionRow>
          <Field label="No inventory yet" description="Sync it from the Inventory page." focusable />
        </PanelSectionRow>
      )}
      {t?.rows.map((r) => (
        <PanelSectionRow key={r.key}>
          <Field
            focusable
            onActivate={() => openDetail(r.name, squad)}
            label={
              <span>
                {r.name} <span style={{ color: colors.muted, fontSize: "12px" }}>{Q_SHORT[r.quality]} ×{r.count}</span>
                {r.vaulted && <span style={{ color: colors.bad, fontSize: "10px", marginLeft: "4px" }}>V</span>}
              </span>
            }
            childrenContainerWidth="min"
            bottomSeparator="none"
          >
            <span style={{ whiteSpace: "nowrap", fontSize: "13px" }}>
              <span style={{ color: colors.plat }}>{fmt1(r.plat, "p")}</span> <span style={{ color: colors.ducat }}>{r.ducat == null ? "" : `${Math.round(r.ducat)}d`}</span>
            </span>
          </Field>
        </PanelSectionRow>
      ))}
      {t && t.total > t.rows.length && (
        <PanelSectionRow>
          <div style={{ color: colors.muted, fontSize: "12px" }}>+{t.total - t.rows.length} more owned</div>
        </PanelSectionRow>
      )}
      {planner && planner.state === "rows" && (
        <PanelSectionRow>
          <div style={{ color: colors.muted, fontSize: "12px" }}>
            Overlay: {planner.era ? planner.era.toUpperCase() : "all eras"} · {planner.count ?? planner.rows.length} relics
            {planner.message ? ` · ${planner.message}` : ""}
          </div>
        </PanelSectionRow>
      )}
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          onClick={async () => {
            try {
              const r = await call<{ token: number | null; enabled: boolean }>("relics.planner_show", { era });
              if (!r.enabled) setShowStatus("Relic planner is turned off in Settings.");
              else if (r.token == null) setShowStatus("Planner did not open (try again in a moment).");
              else setShowStatus(era === "auto" ? "Shown; detecting the era…" : `Shown: ${era === "all" ? "all tiers" : era}.`);
            } catch (e) {
              setShowStatus(`Failed: ${String((e as Error)?.message ?? e)}`);
            }
          }}
        >
          Show planner overlay
        </ButtonItem>
      </PanelSectionRow>
      {showStatus && (
        <PanelSectionRow>
          <div style={{ color: colors.muted, fontSize: "12px" }}>{showStatus}</div>
        </PanelSectionRow>
      )}
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={() => call("relics.planner_hide")}>
          Hide planner overlay
        </ButtonItem>
      </PanelSectionRow>
      {rp && (
        <PanelSectionRow>
          <ToggleField
            label="Rewards on overlay"
            checked={!!rp.show_rewards}
            onChange={async (b) => {
              await call("settings.update", { patch: { relic_planner: { show_rewards: b } } });
              settings.reload();
            }}
          />
        </PanelSectionRow>
      )}
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          onClick={() => {
            openTab("relics");
            Navigation.CloseSideMenus();
            Navigation.Navigate(ROUTE);
          }}
        >
          Open Relics
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
};

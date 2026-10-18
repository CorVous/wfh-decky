// Foundry: claimable builds, builds in progress (countdown), blueprints ready to build (including
// whole crafting chains from owned blueprints) and blueprints missing parts; pinned blueprints
// with summed credits / resources. QAM: counts, the actionable rows, pinned shortfall.
// Backend: py_modules/wfh/menus/foundry.py (foundry.view/pin/qam).
import { ButtonItem, DialogButton, Dropdown, Field, Focusable, ModalRoot, Navigation, PanelSection, PanelSectionRow, TextField, showModal } from "@decky/ui";
import { FC, ReactNode, useEffect, useState } from "react";

import { Badge, Empty, ErrorText, FilterCell, Loading, Page, Pager, Section, Segmented, chip, colors, countdown } from "../components/ui";
import { openTab } from "../nav";
import { ROUTE } from "../nav";
import { call, useEvent, useRpc } from "../rpc";
import { InvStatus, NoInventory, ago, fmtNum } from "./Inventory";

// ---- types (mirror menus/foundry.py) ----------------------------------------------------------

type Status = "claimable" | "in-progress" | "ready-to-build" | "not-ready";

interface Ingredient {
  uniqueName: string;
  name: string;
  count: number;
  owned: number;
}

interface FoundryRow {
  key: string;
  source: "building" | "blueprint";
  name: string;
  uniqueName: string | null;
  productUniqueName: string | null;
  category: string;
  status: Status;
  count: number | null;
  endMs: number | null;
  time: number | null;
  buildPrice: number;
  buildTime: number;
  ingredients: Ingredient[];
  isIngredient: boolean;
  isPrime: boolean;
  vaulted: boolean;
  chainOnly?: boolean;
  productOwned: number;
  pinned: boolean;
}

interface PinnedTotals {
  count: number;
  credits: number;
  resources: { uniqueName: string; name: string; needed: number; owned: number }[];
  missing: { uniqueName: string; name: string; needed: number; owned: number }[];
  names: string[];
}

interface Counts {
  claimable: number;
  building: number;
  ready: number;
  missing: number;
}

interface FoundryView {
  hasInventory: boolean;
  status?: InvStatus;
  home?: string;
  inventory?: { origin: string; syncTime: number | null; fetchedAt: number } | null;
  now?: number;
  tab?: string;
  sort?: string;
  direction?: "asc" | "desc";
  tabs?: { id: string; label: string; count: number }[];
  counts?: Counts;
  total: number;
  rows: FoundryRow[];
  pinned?: string[];
  pinnedTotals?: PinnedTotals;
  recipeData?: boolean;
}

interface QamRow {
  key: string;
  name: string;
  status: Status;
  endMs: number | null;
  count: number | null;
  chainOnly?: boolean;
}

interface FoundryQam {
  hasInventory: boolean;
  counts?: Counts;
  rows?: QamRow[];
  nextReadyMs?: number | null;
  pinnedTotals?: PinnedTotals;
}

// ---- constants / helpers ------------------------------------------------------------------------

const STATUS_COLOR: Record<Status, string> = {
  claimable: colors.accent,
  "in-progress": "#60a5fa",
  "ready-to-build": colors.good,
  "not-ready": colors.muted,
};

const STATES = [
  { data: "all", label: "Any state" },
  { data: "claimable", label: "Claimable" },
  { data: "buildable", label: "Ready to build" },
  { data: "buildable_sets", label: "Ready: whole items" },
  { data: "not_ready", label: "Not ready" },
];
const SORTS = [
  { data: "count", label: "Blueprints owned" },
  { data: "time", label: "Time left" },
  { data: "name", label: "Name" },
];
const PAGE_SIZE = 40;

function statusLabel(r: { status: Status; endMs: number | null; chainOnly?: boolean }, now: number): string {
  switch (r.status) {
    case "claimable":
      return "CLAIM";
    case "in-progress":
      return countdown(r.endMs, now);
    case "ready-to-build":
      return r.chainOnly ? "CHAIN" : "BUILD";
    default:
      return "missing";
  }
}

function openPage(id: string): void {
  openTab(id);
  Navigation.Navigate(ROUTE);
  Navigation.CloseSideMenus();
}

// One-second clock for countdowns between server refreshes.
function useNow(): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  return now;
}

const hours = (s: number): string => (s >= 86400 ? `${Math.round(s / 3600)}h` : s >= 3600 ? `${Math.round(s / 360) / 10}h` : `${Math.round(s / 60)}m`);

const IngredientGrid: FC<{ ings: Ingredient[]; compact?: boolean }> = ({ ings, compact }) => (
  <div style={{ display: "grid", gridTemplateColumns: compact ? "repeat(auto-fill, minmax(150px, 1fr))" : "1fr 120px", gap: compact ? "0 12px" : "2px 12px", fontSize: compact ? "12px" : "14px" }}>
    {ings.map((i, n) =>
      compact ? (
        <span key={`${i.uniqueName}:${n}`} style={{ color: i.owned >= i.count ? colors.muted : colors.bad, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {i.name} {fmtNum(i.owned)}/{fmtNum(i.count)}
        </span>
      ) : (
        [
          <span key={`${i.uniqueName}:${n}n`}>{i.name}</span>,
          <span key={`${i.uniqueName}:${n}c`} style={{ textAlign: "right", color: i.owned >= i.count ? colors.good : colors.bad }}>
            {fmtNum(i.owned)} / {fmtNum(i.count)}
          </span>,
        ]
      ),
    )}
  </div>
);

// ---- detail modal ------------------------------------------------------------------------------

const RowModal: FC<{ r: FoundryRow; onPin: (r: FoundryRow, pinned: boolean) => Promise<void>; closeModal?: () => void }> = ({ r, onPin, closeModal }) => {
  const now = useNow(); // the modal renders outside the page tree, so it keeps its own clock
  const [pinned, setPinned] = useState(r.pinned);
  return (
    <ModalRoot closeModal={closeModal} onCancel={closeModal}>
      <div style={{ color: colors.text, minWidth: "560px" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "6px" }}>
          <span style={{ fontSize: "20px", fontWeight: 700 }}>{r.name}</span>
          {r.count != null && r.count > 1 && <span style={{ color: colors.muted }}>×{r.count}</span>}
          <Badge text={statusLabel(r, now)} color={STATUS_COLOR[r.status]} />
          {r.vaulted && <Badge text="Vaulted" color={colors.bad} />}
          {r.isIngredient && <Badge text="Used in crafting" color={colors.warn} />}
        </div>
        <div style={{ color: colors.muted, marginBottom: "8px" }}>
          {r.category}
          {r.buildPrice ? ` · ${fmtNum(r.buildPrice)} credits` : ""}
          {r.buildTime ? ` · ${hours(r.buildTime)} build` : ""}
          {r.productOwned > 0 ? ` · ${r.productOwned} built` : ""}
          {r.status === "in-progress" && r.endMs ? ` · ready ${new Date(r.endMs).toLocaleString()}` : ""}
        </div>
        {r.chainOnly && <div style={{ color: colors.good, fontSize: "13px", marginBottom: "6px" }}>Buildable by first crafting the missing parts from blueprints you own.</div>}
        {r.ingredients.length > 0 ? <IngredientGrid ings={r.ingredients} /> : <Empty text="No recipe data for this blueprint." />}
        <Focusable flow-children="horizontal" style={{ display: "flex", justifyContent: "flex-end", gap: "8px", marginTop: "12px" }}>
          {r.source === "blueprint" && r.uniqueName && (
            <DialogButton
              style={{ width: "160px" }}
              onClick={async () => {
                await onPin(r, !pinned);
                setPinned(!pinned);
              }}
            >
              {pinned ? "Unpin" : "Pin"}
            </DialogButton>
          )}
          <DialogButton style={{ width: "140px" }} onClick={closeModal}>
            Close
          </DialogButton>
        </Focusable>
      </div>
    </ModalRoot>
  );
};

// ---- rows ------------------------------------------------------------------------------------------

const Row: FC<{ r: FoundryRow; now: number; onOpen: () => void }> = ({ r, now, onOpen }) => (
  <Focusable
    focusClassName="gpfocus"
    onActivate={onOpen}
    onClick={onOpen}
    style={{ display: "grid", gridTemplateColumns: "84px 1fr", gap: "10px", alignItems: "start", padding: "6px 4px", borderBottom: `1px solid ${colors.border}` }}
  >
    <span style={{ color: STATUS_COLOR[r.status], fontWeight: 700, fontSize: "13px", paddingTop: "2px" }}>{statusLabel(r, now)}</span>
    <div style={{ minWidth: 0 }}>
      <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {r.pinned && <span style={{ color: colors.warn, marginRight: "4px" }}>★</span>}
        {r.name}
        {r.count != null && r.count > 1 && <span style={{ color: colors.muted }}> ×{r.count}</span>}
        <span style={{ color: colors.muted, fontSize: "12px" }}> · {r.category}</span>
        {r.isIngredient && <span style={{ color: colors.warn, fontSize: "11px", marginLeft: "6px" }}>crafting part</span>}
        {r.vaulted && <span style={{ color: colors.bad, fontSize: "11px", marginLeft: "6px" }}>V</span>}
      </div>
      {r.source === "blueprint" && r.status !== "ready-to-build" && r.ingredients.length > 0 && <IngredientGrid ings={r.ingredients.filter((i) => i.owned < i.count)} compact />}
    </div>
  </Focusable>
);

const PinnedSummary: FC<{ t: PinnedTotals }> = ({ t }) =>
  t.count === 0 ? null : (
    <Section title={`Pinned (${t.count})`} right={<span style={{ color: colors.muted, fontSize: "12px" }}>{fmtNum(t.credits)} credits</span>}>
      {t.missing.length === 0 ? (
        <div style={{ color: colors.good }}>Everything for the pinned builds is in stock.</div>
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))", gap: "2px 12px", fontSize: "13px" }}>
          {t.missing.map((m) => (
            <span key={m.uniqueName} style={{ color: colors.bad }}>
              {fmtNum(m.needed - m.owned)} × {m.name} <span style={{ color: colors.muted }}>({fmtNum(m.owned)}/{fmtNum(m.needed)})</span>
            </span>
          ))}
        </div>
      )}
    </Section>
  );

// ---- full page -----------------------------------------------------------------------------------

interface ViewState {
  tab: string;
  search: string;
  prime: string;
  state: string;
  vaulted: string;
  sort: string;
  direction: "asc" | "desc";
}

const DEFAULT_STATE: ViewState = { tab: "all", search: "", prime: "all", state: "all", vaulted: "all", sort: "count", direction: "desc" };
const STORE_KEY = "wfh.foundry.view";

function loadState(): ViewState {
  try {
    const raw = localStorage.getItem(STORE_KEY);
    if (raw) return { ...DEFAULT_STATE, tab: JSON.parse(raw).tab ?? "all" };
  } catch {
    /* ignore */
  }
  return DEFAULT_STATE;
}

const Foundry: FC = () => {
  const [s, setS] = useState<ViewState>(loadState);
  const [page, setPage] = useState(0);
  const [searchDraft, setSearchDraft] = useState("");
  const now = useNow();
  const update = (patch: Partial<ViewState>) => {
    setS((prev) => {
      const next = { ...prev, ...patch };
      try {
        localStorage.setItem(STORE_KEY, JSON.stringify({ tab: next.tab }));
      } catch {
        /* ignore */
      }
      return next;
    });
    setPage(0);
  };
  useEffect(() => {
    const t = setTimeout(() => searchDraft !== s.search && update({ search: searchDraft }), 300);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchDraft]);
  const view = useRpc<FoundryView>("foundry.view", { ...s, offset: page * PAGE_SIZE, limit: PAGE_SIZE }, [], 60000);
  useEvent("inventory.updated", () => view.reload());
  const v = view.data;

  if (view.error && !v) return <Page><ErrorText error={view.error} /></Page>;
  if (!v) return <Page><Loading /></Page>;
  if (!v.hasInventory)
    return (
      <Page>
        <NoInventory status={v.status} home={v.home} onDone={() => view.reload()} />
      </Page>
    );

  const onPin = async (r: FoundryRow, pinned: boolean) => {
    await call("foundry.pin", { unique_name: r.uniqueName, pinned });
    view.reload();
  };
  const pages = Math.max(1, Math.ceil(v.total / PAGE_SIZE));
  const c = v.counts!;

  const tabStrip = (
    <Segmented
      style={{ margin: "8px 0" }}
      value={v.tab ?? s.tab}
      options={(v.tabs ?? []).map((t) => ({ id: t.id, label: <>{t.label} <span style={{ opacity: 0.7, fontSize: "12px" }}>{t.count}</span></> }))}
      onChange={(id) => update({ tab: String(id) })}
    />
  );

  // Compact filter bar above the list so rows get the full width.
  const filters = (
    <Focusable flow-children="horizontal" style={{ display: "flex", gap: "8px", alignItems: "flex-end", flexWrap: "wrap", marginTop: "4px" }}>
      <FilterCell label="Search" width="220px">
        <TextField label="" value={searchDraft} onChange={(e) => setSearchDraft(e.target.value)} />
      </FilterCell>
      <FilterCell label="State" width="170px">
        <Dropdown rgOptions={STATES} selectedOption={s.state} onChange={(o) => update({ state: o.data })} />
      </FilterCell>
      <FilterCell label="Sort" width="200px">
        <Dropdown rgOptions={SORTS} selectedOption={s.sort} onChange={(o) => update({ sort: o.data, direction: o.data === "count" ? "desc" : "asc" })} />
      </FilterCell>
      <DialogButton style={chip(false)} onClick={() => update({ direction: s.direction === "desc" ? "asc" : "desc" })}>
        {s.direction === "desc" ? "▼ Descending" : "▲ Ascending"}
      </DialogButton>
      <FilterCell label="Prime" width="140px">
        <Dropdown
          rgOptions={[
            { data: "all", label: "Any" },
            { data: "prime", label: "Prime only" },
            { data: "non_prime", label: "Non-prime" },
          ]}
          selectedOption={s.prime}
          onChange={(o) => update({ prime: o.data })}
        />
      </FilterCell>
      <FilterCell label="Vaulted" width="140px">
        <Dropdown
          rgOptions={[
            { data: "all", label: "Any" },
            { data: "yes", label: "Vaulted" },
            { data: "no", label: "Not vaulted" },
          ]}
          selectedOption={s.vaulted}
          onChange={(o) => update({ vaulted: o.data })}
        />
      </FilterCell>
      <DialogButton style={chip(false)} onClick={() => { setSearchDraft(""); update({ ...DEFAULT_STATE, tab: s.tab }); }}>
        Reset
      </DialogButton>
    </Focusable>
  );
  const notes = (
    <>
      {v.inventory && <div style={{ color: colors.muted, fontSize: "12px", padding: "4px 0" }}>Inventory synced {ago(v.inventory.syncTime ?? v.inventory.fetchedAt)}</div>}
      {v.recipeData === false && <div style={{ color: colors.warn, fontSize: "12px" }}>Recipe data missing (run scripts/fetch-data.sh): credits and build times are unknown.</div>}
    </>
  );

  let body: ReactNode;
  if (v.rows.length === 0) body = <Empty text="Nothing in the foundry matches." />;
  else
    body = (
      <>
        <Focusable style={{ display: "flex", flexDirection: "column" }}>
          {v.rows.map((r) => (
            <Row key={r.key} r={r} now={now} onOpen={() => showModal(<RowModal r={r} onPin={onPin} />)} />
          ))}
        </Focusable>
        {pages > 1 && <Pager page={page} pages={pages} onPage={setPage} label={`${v.total} rows`} />}
      </>
    );

  return (
    <Page>
      {tabStrip}
      <Focusable style={{ display: "flex", flexDirection: "column" }}>
        {filters}
        {notes}
        <div>
          <Focusable flow-children="horizontal" style={{ display: "flex", gap: "10px", alignItems: "center", flexWrap: "wrap", margin: "10px 0 4px" }}>
            <span style={{ color: colors.muted, fontSize: "11px", textTransform: "uppercase" }}>Quick filter</span>
            <Segmented
              options={[
                { id: "claimable", label: `Claimable ${c.claimable}` },
                { id: "in-progress", label: `Building ${c.building}` },
                { id: "buildable", label: `Ready ${c.ready}` },
                { id: "not_ready", label: `Missing parts ${c.missing}` },
              ]}
              value={s.state === "all" && v.tab === "status:in-progress" ? "in-progress" : s.state}
              onChange={(x) => (x === "in-progress" ? update({ tab: "status:in-progress", state: "all" }) : update({ state: s.state === x ? "all" : x, tab: v.tab === "status:in-progress" ? "all" : s.tab }))}
            />
          </Focusable>
          {v.pinnedTotals && <PinnedSummary t={v.pinnedTotals} />}
          {view.loading && <div style={{ color: colors.muted, fontSize: "12px" }}>Updating…</div>}
          {body}
        </div>
      </Focusable>
    </Page>
  );
};

export default Foundry;

// ---- QAM section ------------------------------------------------------------------------------

export const Qam: FC = () => {
  const st = useRpc<FoundryQam>("foundry.qam", { limit: 6 }, [], 60000);
  useEvent("inventory.updated", () => st.reload());
  const now = useNow();
  const q = st.data;
  const c = q?.counts;
  const pt = q?.pinnedTotals;
  return (
    <PanelSection title="Foundry">
      {st.error && !q && (
        <PanelSectionRow>
          <Field label="Error" description={st.error} />
        </PanelSectionRow>
      )}
      {q && !q.hasInventory && (
        <PanelSectionRow>
          <Field label="No inventory yet" description="Sync it from the Inventory page." focusable />
        </PanelSectionRow>
      )}
      {c && (
        <PanelSectionRow>
          <div style={{ display: "flex", gap: "10px", fontSize: "13px" }}>
            <span style={{ color: STATUS_COLOR.claimable }}>Claim {c.claimable}</span>
            <span style={{ color: STATUS_COLOR["in-progress"] }}>Building {c.building}</span>
            <span style={{ color: STATUS_COLOR["ready-to-build"] }}>Ready {c.ready}</span>
          </div>
        </PanelSectionRow>
      )}
      {(q?.rows ?? []).map((r) => (
        <PanelSectionRow key={r.key}>
          <Field
            focusable
            onActivate={() => openPage("foundry")}
            label={<span style={{ fontSize: "13px" }}>{r.name}{r.count != null && r.count > 1 ? <span style={{ color: colors.muted }}> ×{r.count}</span> : null}</span>}
            childrenContainerWidth="min"
            bottomSeparator="none"
          >
            <span style={{ color: STATUS_COLOR[r.status], fontWeight: 700, fontSize: "12px", whiteSpace: "nowrap" }}>{statusLabel(r, now)}</span>
          </Field>
        </PanelSectionRow>
      ))}
      {q?.hasInventory && (q.rows ?? []).length === 0 && (
        <PanelSectionRow>
          <div style={{ color: colors.muted, fontSize: "12px" }}>Nothing to claim or build.</div>
        </PanelSectionRow>
      )}
      {pt && pt.count > 0 && (
        <PanelSectionRow>
          <Field label={`Pinned (${pt.count}) · ${fmtNum(pt.credits)} cr`} focusable bottomSeparator="none" description={
            pt.missing.length === 0 ? "All resources in stock" : `Missing: ${pt.missing.slice(0, 3).map((m) => `${fmtNum(m.needed - m.owned)} ${m.name}`).join(", ")}${pt.missing.length > 3 ? ` +${pt.missing.length - 3}` : ""}`
          } />
        </PanelSectionRow>
      )}
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={() => openPage("foundry")}>
          Open Foundry
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
};

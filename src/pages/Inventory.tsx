// Inventory: tabs (prime parts, relics, mods, arcanes, full sets, equipment, resources, misc,
// everything), value strip, filters and sort, item details with the order book on demand.
// Backend: py_modules/wfh/menus/inventory.py (inventory.view/item/qam/prefetch_prices/ui_set) and
// py_modules/wfh/inventory (inventory.status/refresh/import_file).
import { openFilePicker } from "@decky/api";
import {
  DialogButton,
  Dropdown,
  Field,
  Focusable,
  ModalRoot,
  PanelSection,
  PanelSectionRow,
  TextField,
  ToggleField,
  showModal,
} from "@decky/ui";
import { FC, ReactNode, useEffect, useState } from "react";

import { Badge, Ducats, Empty, ErrorText, FilterCell, Loading, Page, Pager, Plat, QamButtons, QamMessage, QamNote, QamRow, Section, Segmented, Stat, chip, colors, ellipsis } from "../components/ui";
import { call, useEvent, useRpc } from "../rpc";

// ---- types (mirror menus/inventory.py) -----------------------------------------------------

type Tab = "everything" | "all_parts" | "relics" | "mods" | "arcanes" | "full_sets" | "equipment" | "resources" | "misc";
type YesNo = "all" | "yes" | "no";

interface InvRow {
  key: string;
  name: string;
  internalName: string;
  category: string;
  categoryLabel: string;
  inventoryGroup: string;
  rank: number;
  maxRank: number | null;
  amount: number;
  combinedAmount: number | null;
  isPrime: boolean;
  vaulted: boolean;
  tradable: boolean;
  favorite: boolean;
  equipped: boolean;
  leveledUp: boolean;
  marketSlug: string | null;
  subtype: string | null;
  platinum: number | null;
  ducats: number | null;
  ducatonator: number | null;
  equippedSummary: string | null;
  completeSets: number | null;
  missingParts: number | null;
  ownedPartTypes: number | null;
  totalPartTypes: number | null;
  setCategory?: string;
}

interface ValueTotals {
  platinum: number;
  ducats: number;
  platinumUnpriced: number;
  ducatsUnpriced: number;
  unpriced: number;
  counted: number;
}

interface Prefs {
  valueScope: "prime" | "tradable";
  valueMinPlat: number;
  hideFoundryClaims: boolean;
  autoPrefetch: boolean;
}

export interface InvStatus {
  source?: string | null;
  file?: string | null;
  found?: boolean;
  running?: boolean;
  last_run_at?: number | null;
  last_run_ok?: boolean | null;
  last_reason?: string | null;
  last_error?: { message?: string } | null;
  warframe_running?: boolean;
}

interface InvMeta {
  origin: string;
  syncTime: number | null;
  fetchedAt: number;
  path: string | null;
}

interface InvView {
  hasInventory: boolean;
  status?: InvStatus;
  home?: string;
  prefs?: Prefs;
  inventory?: InvMeta | null;
  tab: Tab;
  sort?: string;
  direction?: "asc" | "desc";
  sorts?: string[];
  total: number;
  offset?: number;
  limit?: number;
  rows: InvRow[];
  tabs?: { id: Tab; label: string; count: number }[];
  everythingHidden?: string[];
  setCategories?: { id: string; count: number }[];
  value?: { scope: string; minPlat: number; inView: ValueTotals; inventory: ValueTotals; rows: ("inView" | "inventory")[] };
  pricesMissing?: number;
}

interface ItemDetail extends InvRow {
  equippedIn: string[];
  modularParts: string[];
  priceRank: number | null;
  components?: { name: string; uniqueName: string; needed: number; owned: number; ducats: number | null }[];
  parentSet?: { key: string; name: string; completeSets: number | null; missingParts: number | null; platinum: number | null };
  recipe?: { product: string; credits: number; buildTime: number; ingredients: { name: string; needed: number; owned: number }[] };
}

interface OrderRow {
  user?: string;
  ingame_name?: string;
  platinum?: number;
  quantity?: number;
  rank?: number | null;
  status?: string;
}

interface MarketItem {
  slug: string;
  name: string;
  price: { median: number | null; low_sell: number | null; high_buy: number | null; volume: number | null } | null;
  orders: { sell: OrderRow[]; buy: OrderRow[] };
}

interface QamView {
  hasInventory: boolean;
  scope: "prime" | "tradable";
  minPlat: number;
  presets: number[];
  totals?: ValueTotals;
  top?: { name: string; amount: number; platinum: number; ducats: number | null }[];
  inventory?: InvMeta | null;
}

// ---- constants ------------------------------------------------------------------------------

const SORT_LABEL: Record<string, string> = {
  name: "Name",
  platinum: "Platinum",
  ducats: "Ducats",
  amount: "Amount",
  ducatonator: "Ducats per plat",
  complete_sets: "Complete sets",
  missing_parts: "Missing parts",
};
const DESC_DEFAULT = ["platinum", "ducats", "amount", "ducatonator", "complete_sets"];
const SOURCES: { id: string; label: string }[] = [
  { id: "all_parts", label: "Prime parts" },
  { id: "relics", label: "Relics" },
  { id: "mods", label: "Mods" },
  { id: "arcanes", label: "Arcanes" },
  { id: "full_sets", label: "Full sets" },
  { id: "equipment", label: "Equipment" },
  { id: "resources", label: "Resources" },
  { id: "misc", label: "Misc" },
];
const MIN_PLAT_OPTIONS = [0, 5, 10, 20, 50, 100];
const PAGE_SIZE = 30;

interface Filters {
  vaulted: YesNo;
  partType: "all" | "normal" | "prime";
  favorite: YesNo;
  equipped: YesNo;
  leveledUp: YesNo;
  minimumPlatinum: number;
  minimumAmount: number;
}

interface ViewState {
  tab: Tab;
  sort: string | null;
  direction: "asc" | "desc" | null;
  search: string;
  filters: Filters;
  everythingHidden: string[];
  fullSetsHidden: string[];
  showIncomplete: boolean;
}

const DEFAULT_FILTERS: Filters = { vaulted: "all", partType: "all", favorite: "all", equipped: "all", leveledUp: "all", minimumPlatinum: 0, minimumAmount: 0 };
const DEFAULT_STATE: ViewState = {
  tab: "all_parts",
  sort: null,
  direction: null,
  search: "",
  filters: DEFAULT_FILTERS,
  everythingHidden: ["full_sets"],
  fullSetsHidden: [],
  showIncomplete: false,
};
const STORE_KEY = "wfh.inventory.view";

function loadState(): ViewState {
  try {
    const raw = localStorage.getItem(STORE_KEY);
    if (raw) {
      const s = JSON.parse(raw);
      // search and filters are session-only (WFHelper keeps them out of storage)
      return { ...DEFAULT_STATE, tab: s.tab ?? DEFAULT_STATE.tab, everythingHidden: s.everythingHidden ?? DEFAULT_STATE.everythingHidden, fullSetsHidden: s.fullSetsHidden ?? [] };
    }
  } catch {
    /* storage unavailable */
  }
  return DEFAULT_STATE;
}

function saveState(s: ViewState): void {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify({ tab: s.tab, everythingHidden: s.everythingHidden, fullSetsHidden: s.fullSetsHidden }));
  } catch {
    /* ignore */
  }
}

// ---- helpers ----------------------------------------------------------------------------------

export const fmtNum = (n: number | null | undefined): string => (n == null ? "–" : Math.round(n).toLocaleString("en-US"));

export function ago(ms: number | null | undefined): string {
  if (!ms) return "never";
  const s = Math.max(0, Math.floor((Date.now() - ms) / 1000));
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

// "No inventory yet": refresh from the game or import an exported JSON file.
export const NoInventory: FC<{ status?: InvStatus; home?: string; onDone: () => void }> = ({ status, home, onDone }) => {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string>();
  const run = async (fn: () => Promise<unknown>, ok: string) => {
    setBusy(true);
    setMsg(undefined);
    try {
      await fn();
      setMsg(ok);
      onDone();
    } catch (e) {
      setMsg(String((e as Error)?.message ?? e));
    } finally {
      setBusy(false);
    }
  };
  const importFile = async () => {
    let path: string;
    try {
      const res = await openFilePicker(0 as unknown as Parameters<typeof openFilePicker>[0], home || "/home/deck", true, true, undefined, ["json"]);
      path = res.realpath || res.path;
    } catch {
      return; // picker closed
    }
    await run(() => call("inventory.import_file", { path }), "Inventory imported.");
  };
  const err = status?.last_error?.message ?? status?.last_reason;
  return (
    <Section title="No inventory yet">
      <div style={{ color: colors.muted, marginBottom: "8px", lineHeight: 1.4 }}>
        {status?.source === "file"
          ? `Source: file${status.file ? ` (${status.file})` : ""}.`
          : status?.warframe_running
            ? "Warframe is running: refresh to read your inventory (after you log in)."
            : "Start Warframe and log in, then refresh. Or import an inventory JSON you exported."}
        {err && <div style={{ color: colors.warn, marginTop: "4px" }}>Last attempt: {err}</div>}
      </div>
      <Focusable flow-children="horizontal" style={{ display: "flex", gap: "8px" }}>
        <DialogButton style={{ width: "220px" }} disabled={busy} onClick={() => run(() => call("inventory.refresh"), "Inventory refreshed.")}>
          {busy ? "Working…" : "Refresh from game"}
        </DialogButton>
        <DialogButton style={{ width: "220px" }} disabled={busy} onClick={importFile}>
          Import JSON file…
        </DialogButton>
      </Focusable>
      {msg && <div style={{ marginTop: "8px", color: colors.muted }}>{msg}</div>}
    </Section>
  );
};

// ---- value strip ------------------------------------------------------------------------------

const ValueLine: FC<{ label: string; t: ValueTotals; scope: string }> = ({ label, t, scope }) => (
  <div style={{ display: "flex", gap: "14px", alignItems: "baseline", flexWrap: "wrap" }}>
    <span style={{ color: colors.muted, width: "84px" }}>{label}</span>
    <span style={{ color: colors.plat, fontWeight: 700, fontSize: "16px" }}>
      {t.platinumUnpriced > 0 ? "≥ " : ""}
      {fmtNum(t.platinum)}p
    </span>
    {scope === "prime" && (
      <span style={{ color: colors.ducat, fontWeight: 600 }}>
        {t.ducatsUnpriced > 0 ? "≥ " : ""}
        {fmtNum(t.ducats)}d
      </span>
    )}
    <span style={{ color: colors.muted, fontSize: "12px" }}>
      {t.counted} items{t.unpriced > 0 ? ` · ${t.unpriced} unpriced` : ""}
    </span>
  </div>
);

const ValueStrip: FC<{ v: NonNullable<InvView["value"]>; prefs: Prefs; onPrefs: (p: Partial<{ value_scope: string; value_min_plat: number }>) => void; onFetch: () => void; fetchMsg?: string }> = ({ v, prefs, onPrefs, onFetch, fetchMsg }) => (
  <Section title="Inventory value" right={<span style={{ fontSize: "12px", color: colors.muted }}>median 48 h trade price × amount</span>}>
    {v.rows.length === 0 && <div style={{ color: colors.muted }}>Nothing sellable in this scope.</div>}
    {v.rows.includes("inView") && <ValueLine label="In view" t={v.inView} scope={v.scope} />}
    {v.rows.includes("inventory") && <ValueLine label="Inventory" t={v.inventory} scope={v.scope} />}
    <Focusable flow-children="horizontal" style={{ display: "flex", gap: "16px", marginTop: "8px", alignItems: "center", flexWrap: "wrap" }}>
      <Segmented
        options={[
          { id: "prime", label: "Prime parts" },
          { id: "tradable", label: "All tradables" },
        ]}
        value={prefs.valueScope}
        onChange={(s) => onPrefs({ value_scope: s })}
      />
      <span style={{ color: colors.muted, fontSize: "12px" }}>Min per unit</span>
      <Segmented options={[0, 5, 10, 15].map((n) => ({ id: n, label: n === 0 ? "Any" : `${n}p` }))} value={prefs.valueMinPlat} onChange={(n) => onPrefs({ value_min_plat: n })} />
      <DialogButton style={chip(false)} onClick={onFetch}>
        Fetch prices
      </DialogButton>
      {fetchMsg && <span style={{ color: colors.muted, fontSize: "12px" }}>{fetchMsg}</span>}
    </Focusable>
  </Section>
);

// ---- detail modal -----------------------------------------------------------------------------

const OrderBook: FC<{ slug: string; rank: number | null }> = ({ slug, rank }) => {
  const st = useRpc<MarketItem>("market.item", { slug, rank }, []);
  if (st.error) return <ErrorText error={st.error} />;
  if (!st.data) return <Loading />;
  const m = st.data;
  const col = (title: string, rows: OrderRow[], color: string) => (
    <div style={{ flex: 1, minWidth: 0 }}>
      <div style={{ color, fontWeight: 600, marginBottom: "2px" }}>{title}</div>
      {rows.length === 0 && <div style={{ color: colors.muted }}>none online</div>}
      {rows.slice(0, 5).map((o, i) => (
        <div key={i} style={{ display: "flex", justifyContent: "space-between", fontSize: "13px", gap: "8px" }}>
          <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: colors.muted }}>{o.ingame_name ?? o.user ?? "?"}</span>
          <span>
            <span style={{ color: colors.plat }}>{o.platinum}p</span>
            {o.quantity && o.quantity > 1 ? <span style={{ color: colors.muted }}> ×{o.quantity}</span> : null}
          </span>
        </div>
      ))}
    </div>
  );
  return (
    <div>
      {m.price && (
        <div style={{ marginBottom: "6px" }}>
          <Stat label="Median" value={<Plat value={m.price.median} />} />
          <Stat label="Lowest sell" value={<Plat value={m.price.low_sell} />} />
          <Stat label="Best buy" value={<Plat value={m.price.high_buy} />} />
          <Stat label="Volume 48h" value={m.price.volume ?? "–"} />
        </div>
      )}
      <div style={{ display: "flex", gap: "16px" }}>
        {col("Sellers", m.orders.sell, colors.good)}
        {col("Buyers", m.orders.buy, colors.accent)}
      </div>
    </div>
  );
};

const ItemModal: FC<{ itemKey: string; closeModal?: () => void }> = ({ itemKey, closeModal }) => {
  const st = useRpc<ItemDetail | null>("inventory.item", { key: itemKey }, []);
  const [book, setBook] = useState(false);
  const d = st.data;
  return (
    <ModalRoot closeModal={closeModal} onCancel={closeModal}>
      <div style={{ color: colors.text, minWidth: "620px" }}>
        {st.error && <ErrorText error={st.error} />}
        {d === undefined && !st.error && <Loading />}
        {d === null && <Empty text="Item no longer in the inventory." />}
        {d && (
          <>
            <div style={{ display: "flex", alignItems: "center", gap: "6px", marginBottom: "6px" }}>
              <span style={{ fontSize: "20px", fontWeight: 700 }}>{d.name}</span>
              <span style={{ color: colors.muted }}>{d.categoryLabel}</span>
              {d.vaulted && <Badge text="Vaulted" color={colors.bad} />}
              {d.equipped && <Badge text="Equipped" color={colors.good} />}
              {d.favorite && <Badge text="Favorite" color={colors.warn} />}
            </div>
            <div style={{ marginBottom: "8px" }}>
              <Stat label="Owned" value={d.combinedAmount != null && d.combinedAmount !== d.amount ? `${d.amount} (${d.combinedAmount} all ranks)` : d.amount} />
              {d.maxRank != null && d.maxRank > 0 && d.maxRank !== 30 && <Stat label="Rank" value={`${d.rank}/${d.maxRank}`} />}
              <Stat label="Platinum" value={<Plat value={d.platinum} />} />
              {d.ducats != null && <Stat label="Ducats" value={<Ducats value={d.ducats} />} />}
              {d.ducatonator != null && <Stat label="d/p" value={d.ducatonator.toFixed(2)} />}
              {d.tradable === false && <Stat label="Trade" value="untradable" color={colors.muted} />}
            </div>
            {d.equippedIn.length > 0 && <div style={{ color: colors.muted, fontSize: "13px" }}>Equipped in {d.equippedIn.join(", ")}</div>}
            {d.modularParts.length > 0 && <div style={{ color: colors.muted, fontSize: "13px" }}>Parts: {d.modularParts.join(", ")}</div>}
            {d.components && (
              <Section title="Set parts">
                {d.components.map((c) => (
                  <div key={c.uniqueName} style={{ display: "flex", justifyContent: "space-between", fontSize: "14px" }}>
                    <span>{c.name}</span>
                    <span style={{ color: c.owned >= c.needed ? colors.good : colors.bad }}>
                      {c.owned}/{c.needed}
                    </span>
                  </div>
                ))}
              </Section>
            )}
            {d.parentSet && (
              <div style={{ fontSize: "13px", margin: "6px 0" }}>
                Part of <b>{d.parentSet.name}</b>:{" "}
                {d.parentSet.missingParts != null ? `${d.parentSet.missingParts} part type(s) missing` : `${d.parentSet.completeSets ?? 0} complete set(s)`}
                {d.parentSet.platinum != null && <span style={{ color: colors.plat }}> · set {Math.round(d.parentSet.platinum)}p</span>}
              </div>
            )}
            {d.recipe && (
              <Section title={`Builds ${d.recipe.product}`} right={<span style={{ color: colors.muted, fontSize: "12px" }}>{fmtNum(d.recipe.credits)} cr</span>}>
                {d.recipe.ingredients.map((i) => (
                  <div key={i.name} style={{ display: "flex", justifyContent: "space-between", fontSize: "14px" }}>
                    <span>{i.name}</span>
                    <span style={{ color: i.owned >= i.needed ? colors.text : colors.bad }}>
                      {fmtNum(i.owned)}/{fmtNum(i.needed)}
                    </span>
                  </div>
                ))}
              </Section>
            )}
            {d.marketSlug && (
              <Section title="warframe.market">
                {book ? (
                  <OrderBook slug={d.marketSlug} rank={d.priceRank} />
                ) : (
                  <Focusable>
                    <DialogButton style={{ width: "220px" }} onClick={() => setBook(true)}>
                      Load order book
                    </DialogButton>
                  </Focusable>
                )}
              </Section>
            )}
            <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "10px" }}>
              <DialogButton style={{ width: "140px" }} onClick={closeModal}>
                Close
              </DialogButton>
            </div>
          </>
        )}
      </div>
    </ModalRoot>
  );
};

// ---- rows --------------------------------------------------------------------------------------

const ItemRow: FC<{ r: InvRow; tab: Tab }> = ({ r, tab }) => {
  const open = () => showModal(<ItemModal itemKey={r.key} />);
  const ranked = r.inventoryGroup === "mods" || r.inventoryGroup === "arcanes";
  const isSet = r.inventoryGroup === "full_sets" || r.inventoryGroup === "incomplete_sets";
  return (
    <Focusable
      focusClassName="gpfocus"
      onActivate={open}
      onClick={open}
      style={{ display: "grid", gridTemplateColumns: "1fr 64px 70px 56px 52px", gap: "8px", alignItems: "center", padding: "5px 4px", borderBottom: `1px solid ${colors.border}` }}
    >
      <div style={{ minWidth: 0 }}>
        <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {r.name}
          {ranked && r.maxRank ? <span style={{ color: colors.muted, fontSize: "12px" }}> R{r.rank}/{r.maxRank}</span> : null}
          {r.vaulted && <span style={{ color: colors.bad, fontSize: "11px", marginLeft: "6px" }}>V</span>}
          {r.equipped && <span style={{ color: colors.good, fontSize: "11px", marginLeft: "6px" }}>E</span>}
          {r.favorite && <span style={{ color: colors.warn, fontSize: "11px", marginLeft: "6px" }}>★</span>}
        </div>
        <div style={{ fontSize: "11px", color: colors.muted, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {tab === "everything" || tab === "misc" ? r.categoryLabel : null}
          {isSet && r.missingParts != null ? `${r.ownedPartTypes}/${r.totalPartTypes} parts · ${r.missingParts} missing` : null}
          {isSet && r.setCategory ? ` ${r.setCategory}` : null}
          {r.equippedSummary ? ` ${r.equippedSummary}` : null}
          {r.tradable === false && r.inventoryGroup !== "resources" ? " untradable" : null}
        </div>
      </div>
      <span style={{ textAlign: "right" }}>×{fmtNum(r.amount)}</span>
      <span style={{ textAlign: "right" }}>
        <Plat value={r.platinum} />
      </span>
      <span style={{ textAlign: "right" }}>{r.ducats != null ? <Ducats value={r.ducats} /> : ""}</span>
      <span style={{ textAlign: "right", color: colors.muted, fontSize: "12px" }}>{r.ducatonator != null ? `${r.ducatonator.toFixed(1)}` : ""}</span>
    </Focusable>
  );
};

// ---- full page --------------------------------------------------------------------------------

const yesNoOptions = [
  { data: "all", label: "Any" },
  { data: "yes", label: "Yes" },
  { data: "no", label: "No" },
];


const Inventory: FC = () => {
  const [s, setS] = useState<ViewState>(loadState);
  const [page, setPage] = useState(0);
  const [searchDraft, setSearchDraft] = useState(s.search);
  const [showMore, setShowMore] = useState(false);
  const [fetchMsg, setFetchMsg] = useState<string>();
  const update = (patch: Partial<ViewState>) => {
    setS((prev) => {
      const next = { ...prev, ...patch };
      saveState(next);
      return next;
    });
    setPage(0);
  };
  const setFilter = (patch: Partial<Filters>) => update({ filters: { ...s.filters, ...patch } });
  useEffect(() => {
    const t = setTimeout(() => searchDraft !== s.search && update({ search: searchDraft }), 300);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchDraft]);

  const view = useRpc<InvView>(
    "inventory.view",
    {
      tab: s.tab,
      search: s.search,
      sort: s.sort,
      direction: s.direction,
      filters: s.filters,
      offset: page * PAGE_SIZE,
      limit: PAGE_SIZE,
      everything_hidden: s.everythingHidden,
      full_sets_hidden: s.fullSetsHidden,
      show_incomplete: s.showIncomplete,
    },
    [],
  );
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

  const prefs = v.prefs!;
  const pages = Math.max(1, Math.ceil(v.total / PAGE_SIZE));
  const sorts = v.sorts ?? ["name"];
  const setPrefs = async (patch: Record<string, unknown>) => {
    await call("inventory.ui_set", patch);
    view.reload();
  };
  const fetchPrices = async () => {
    const r = await call<{ queued: number }>("inventory.prefetch_prices", { tab: s.tab === "everything" ? "all_parts" : s.tab });
    setFetchMsg(r.queued > 0 ? `Fetching ${r.queued} prices…` : "Prices are cached.");
    if (r.queued > 0) {
      setTimeout(() => view.reload(), 8000);
      setTimeout(() => view.reload(), 30000);
    }
  };

  const tabStrip = (
    <Segmented
      style={{ margin: "8px 0" }}
      value={s.tab}
      options={(v.tabs ?? []).map((t) => ({ id: t.id, label: <>{t.label} <span style={{ opacity: 0.7, fontSize: "12px" }}>{t.count}</span></> }))}
      onChange={(id) => {
        setSearchDraft("");
        update({ tab: id, sort: null, direction: null, search: "" });
      }}
    />
  );

  const refreshInventory = async () => {
    setFetchMsg("Refreshing inventory…");
    try {
      await call("inventory.refresh");
      setFetchMsg("Inventory refreshed.");
    } catch (e) {
      setFetchMsg(String((e as Error)?.message ?? e));
    }
    view.reload();
  };

  // Compact filter bar above the list; the rarely used filters fold out
  // below it (as more horizontal rows) so the rows keep the full width.
  const toggleIn = (list: string[], id: string, on: boolean) => (on ? list.filter((x) => x !== id) : [...list, id]);
  const filters = (
    <Focusable style={{ display: "flex", flexDirection: "column", gap: "8px", marginTop: "4px" }}>
      <Focusable flow-children="horizontal" style={{ display: "flex", gap: "8px", alignItems: "flex-end", flexWrap: "wrap" }}>
        <FilterCell label="Search" width="220px">
          <TextField label="" value={searchDraft} onChange={(e) => setSearchDraft(e.target.value)} />
        </FilterCell>
        <FilterCell label="Sort" width="190px">
          <Dropdown
            rgOptions={sorts.map((k) => ({ data: k, label: SORT_LABEL[k] ?? k }))}
            selectedOption={v.sort ?? "name"}
            onChange={(o) => update({ sort: o.data, direction: DESC_DEFAULT.includes(o.data) ? "desc" : "asc" })}
          />
        </FilterCell>
        <DialogButton style={chip(false)} onClick={() => update({ sort: v.sort ?? "name", direction: v.direction === "desc" ? "asc" : "desc" })}>
          {v.direction === "desc" ? "▼ Descending" : "▲ Ascending"}
        </DialogButton>
        <DialogButton style={chip(s.filters.minimumAmount >= 2)} onClick={() => setFilter({ minimumAmount: s.filters.minimumAmount >= 2 ? 0 : 2 })}>
          Spares only
        </DialogButton>
        <DialogButton style={chip(showMore)} onClick={() => setShowMore(!showMore)}>
          {showMore ? "Fewer filters" : "More filters"}
        </DialogButton>
        <DialogButton style={chip(false)} onClick={() => { setSearchDraft(""); update({ search: "", filters: DEFAULT_FILTERS, sort: null, direction: null }); }}>
          Reset
        </DialogButton>
        <DialogButton style={chip(false)} onClick={() => void refreshInventory()}>
          Refresh inventory
        </DialogButton>
      </Focusable>
      {showMore && (
        <Focusable flow-children="horizontal" style={{ display: "flex", gap: "8px", alignItems: "flex-end", flexWrap: "wrap" }}>
          {s.tab !== "resources" && (
            <>
              <FilterCell label="Min platinum" width="150px">
                <Dropdown rgOptions={MIN_PLAT_OPTIONS.map((n) => ({ data: n, label: n === 0 ? "Any" : `${n}p or more` }))} selectedOption={s.filters.minimumPlatinum} onChange={(o) => setFilter({ minimumPlatinum: o.data })} />
              </FilterCell>
              <FilterCell label="Vaulted" width="130px">
                <Dropdown rgOptions={yesNoOptions} selectedOption={s.filters.vaulted} onChange={(o) => setFilter({ vaulted: o.data })} />
              </FilterCell>
              <FilterCell label="Part type" width="140px">
                <Dropdown
                  rgOptions={[
                    { data: "all", label: "Any" },
                    { data: "prime", label: "Prime" },
                    { data: "normal", label: "Non-prime" },
                  ]}
                  selectedOption={s.filters.partType}
                  onChange={(o) => setFilter({ partType: o.data })}
                />
              </FilterCell>
              <FilterCell label="Equipped" width="130px">
                <Dropdown rgOptions={yesNoOptions} selectedOption={s.filters.equipped} onChange={(o) => setFilter({ equipped: o.data })} />
              </FilterCell>
              <FilterCell label="Favorite" width="130px">
                <Dropdown rgOptions={yesNoOptions} selectedOption={s.filters.favorite} onChange={(o) => setFilter({ favorite: o.data })} />
              </FilterCell>
              <FilterCell label="Leveled up" width="130px">
                <Dropdown rgOptions={yesNoOptions} selectedOption={s.filters.leveledUp} onChange={(o) => setFilter({ leveledUp: o.data })} />
              </FilterCell>
            </>
          )}
          <DialogButton style={chip(prefs.hideFoundryClaims)} onClick={() => setPrefs({ hide_foundry_claims: !prefs.hideFoundryClaims })}>
            Hide foundry blueprints
          </DialogButton>
        </Focusable>
      )}
      {showMore && s.tab === "everything" && (
        <FilterCell label="Sources">
          <Focusable flow-children="horizontal" style={{ display: "flex", gap: "6px", flexWrap: "wrap" }}>
            {SOURCES.map((src) => {
              const on = !s.everythingHidden.includes(src.id);
              return (
                <DialogButton key={src.id} style={chip(on)} onClick={() => update({ everythingHidden: toggleIn(s.everythingHidden, src.id, !on) })}>
                  {src.label}
                </DialogButton>
              );
            })}
          </Focusable>
        </FilterCell>
      )}
      {showMore && s.tab === "full_sets" && (
        <FilterCell label="Sets">
          <Focusable flow-children="horizontal" style={{ display: "flex", gap: "6px", flexWrap: "wrap" }}>
            <DialogButton style={chip(s.showIncomplete)} onClick={() => { const b = !s.showIncomplete; update({ showIncomplete: b, sort: b ? "missing_parts" : null, direction: b ? "asc" : null }); }}>
              Incomplete sets too
            </DialogButton>
            {(v.setCategories ?? []).map((c) => {
              const on = !s.fullSetsHidden.includes(c.id);
              return (
                <DialogButton key={c.id} style={chip(on)} onClick={() => update({ fullSetsHidden: toggleIn(s.fullSetsHidden, c.id, !on) })}>
                  {c.id} <span style={{ opacity: 0.7, fontSize: "12px" }}>{c.count}</span>
                </DialogButton>
              );
            })}
          </Focusable>
        </FilterCell>
      )}
      {v.inventory && (
        <div style={{ color: colors.muted, fontSize: "12px" }}>
          Synced {ago(v.inventory.syncTime ?? v.inventory.fetchedAt)} · {v.inventory.origin}
        </div>
      )}
    </Focusable>
  );

  let body: ReactNode;
  if (v.rows.length === 0) body = <Empty text={s.search ? "Nothing matches this search." : "Nothing in this tab with these filters."} />;
  else
    body = (
      <>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 64px 70px 56px 52px", gap: "8px", padding: "0 4px 4px", color: colors.muted, fontSize: "11px", textTransform: "uppercase" }}>
          <span>Item</span>
          <span style={{ textAlign: "right" }}>Owned</span>
          <span style={{ textAlign: "right" }}>Plat</span>
          <span style={{ textAlign: "right" }}>Ducats</span>
          <span style={{ textAlign: "right" }}>d/p</span>
        </div>
        <Focusable style={{ display: "flex", flexDirection: "column" }}>
          {v.rows.map((r) => (
            <ItemRow key={r.key} r={r} tab={s.tab} />
          ))}
        </Focusable>
        {pages > 1 && <Pager page={page} pages={pages} onPage={setPage} label={`${v.total} items`} />}
        {(v.pricesMissing ?? 0) > 0 && (
          <div style={{ color: colors.muted, fontSize: "12px", marginTop: "6px" }}>{v.pricesMissing} prices not cached yet. Use “Fetch prices”.</div>
        )}
      </>
    );

  return (
    <Page>
      {tabStrip}
      <Focusable style={{ display: "flex", flexDirection: "column" }}>
        {filters}
        <div>
          {v.value && s.tab !== "resources" && <ValueStrip v={v.value} prefs={prefs} onPrefs={setPrefs} onFetch={fetchPrices} fetchMsg={fetchMsg} />}
          {view.loading && <div style={{ color: colors.muted, fontSize: "12px" }}>Updating…</div>}
          {body}
        </div>
      </Focusable>
    </Page>
  );
};

export default Inventory;

// ---- QAM section ----------------------------------------------------------------------------

export const Qam: FC = () => {
  const st = useRpc<QamView>("inventory.qam", {}, [], 120000);
  const [msg, setMsg] = useState<string>();
  useEvent("inventory.updated", () => st.reload());
  const q = st.data;
  const setPrefs = async (patch: Record<string, unknown>) => {
    await call("inventory.ui_set", patch);
    st.reload();
  };
  const presets = q?.presets ?? [0, 5, 10, 15];
  const refresh = async () => {
    setMsg("Refreshing…");
    try {
      await call("inventory.refresh");
      setMsg(undefined);
    } catch (e) {
      setMsg(String((e as Error)?.message ?? e));
    }
    st.reload();
  };
  const fetchPrices = async (scope: string) => {
    const r = await call<{ queued: number }>("inventory.prefetch_prices", { tab: scope === "prime" ? "prime" : "everything" });
    setMsg(r.queued > 0 ? `Fetching ${r.queued} prices…` : "Prices are cached.");
    if (r.queued > 0) setTimeout(() => st.reload(), 15000);
  };
  return (
    <PanelSection>
      {st.error && !q && <QamMessage label="Error" sub={st.error} />}
      {q && !q.hasInventory && (
        <>
          <QamMessage focusable label="No inventory yet" sub="Log in to Warframe, or import a JSON file on the Inventory page." />
          <QamButtons buttons={[{ label: "Refresh inventory", onClick: refresh }]} />
        </>
      )}
      {q?.hasInventory && q.totals && (
        <>
          <QamRow
            label={q.scope === "prime" ? "Prime parts" : "All tradables"}
            sub={`${q.totals.counted} items${q.totals.unpriced ? ` · ${q.totals.unpriced} unpriced` : ""}`}
            separator={(q.top ?? []).length === 0}
            value={
              <>
                <span style={{ display: "block", color: colors.plat, fontWeight: 700 }}>
                  {q.totals.platinumUnpriced ? "≥" : ""}
                  {fmtNum(q.totals.platinum)}p
                </span>
                {q.scope === "prime" && (
                  <span style={{ display: "block", color: colors.ducat, fontSize: "13px" }}>
                    {q.totals.ducatsUnpriced ? "≥" : ""}
                    {fmtNum(q.totals.ducats)}d
                  </span>
                )}
              </>
            }
          />
          {/* The most valuable stacks, as a small list under the total (not separate rows). */}
          {(q.top ?? []).length > 0 && (
            <PanelSectionRow>
              <div style={{ padding: "0 0 6px", borderBottom: `1px solid ${colors.border}`, fontSize: "13px", lineHeight: "18px" }}>
                {(q.top ?? []).map((t) => (
                  <div key={t.name} style={{ display: "flex", justifyContent: "space-between", gap: "8px" }}>
                    <span style={{ color: colors.muted, ...ellipsis }}>
                      {t.name} ×{t.amount}
                    </span>
                    <span style={{ color: colors.plat, flexShrink: 0, fontVariantNumeric: "tabular-nums" }}>{Math.round(t.platinum)}p</span>
                  </div>
                ))}
              </div>
            </PanelSectionRow>
          )}
          <PanelSectionRow>
            <ToggleField label="Count all tradables" checked={q.scope === "tradable"} onChange={(b) => setPrefs({ value_scope: b ? "tradable" : "prime" })} />
          </PanelSectionRow>
          {/* Min price as chips on one line: the notched slider took two rows. */}
          <PanelSectionRow>
            <Field label="Min price" padding="compact" focusable={false}>
              <Focusable flow-children="horizontal" style={{ display: "flex", gap: "4px" }}>
                {presets.map((p) => (
                  <DialogButton key={p} onClick={() => setPrefs({ value_min_plat: p })} style={{ ...chip(p === q.minPlat), padding: "4px 8px", fontWeight: p === q.minPlat ? 700 : 400 }}>
                    {p === 0 ? "Any" : `${p}p`}
                  </DialogButton>
                ))}
              </Focusable>
            </Field>
          </PanelSectionRow>
          <QamButtons buttons={[{ label: "Fetch missing prices", onClick: () => fetchPrices(q.scope) }]} />
        </>
      )}
      {msg && <QamNote>{msg}</QamNote>}
    </PanelSection>
  );
};

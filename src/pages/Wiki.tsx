// Wiki: where does an item drop (relic tables bundled with the plugin) and what is in a relic
// (backend: py_modules/wfh/menus/wiki.py).
import { DialogButton, Focusable, ModalRoot, Navigation, TextField, showModal } from "@decky/ui";
import { FC, useEffect, useRef, useState } from "react";

import { Badge, Ducats, Empty, ErrorText, FilterCell, Grid, Loading, Page, Section, Segmented, card, chip, colors } from "../components/ui";
import { call, useRpc } from "../rpc";

// Compact section for the Quick Access Menu home; null when the page has none.
export const Qam: FC | null = null;

type Mode = "item" | "relic";
type Chances = Record<"intact" | "exceptional" | "flawless" | "radiant", number>;

interface DropRow {
  item: string;
  relic: string;
  tier: string;
  vaulted: boolean;
  rarity: "Common" | "Uncommon" | "Rare";
  chances: Chances;
  owned: number;
}

interface ItemSummary {
  item: string;
  ducats: number | null;
  relics: number;
  unvaulted: number;
  bestIntact: number;
  bestRadiant: number;
  wikiUrl: string;
}

interface SearchResult {
  query: string;
  mode: Mode;
  rows: DropRow[];
  total: number;
  items: ItemSummary[];
  recent?: Recent[];
}

interface Recent {
  q: string;
  mode: Mode;
}

interface RelicDetail {
  name: string;
  tier: string;
  vaulted: boolean;
  owned: Record<string, number>;
  ownedTotal: number;
  wikiUrl: string;
  rewards: { item: string; rarity: DropRow["rarity"]; ducats: number | null; chances: Chances }[];
}

const RARITY_COLOR: Record<string, string> = { Common: "#c8a070", Uncommon: "#c8d2e6", Rare: "#f0d060" };
const TIERS = ["Lith", "Meso", "Neo", "Axi", "Requiem"];
const REFS: (keyof Chances)[] = ["intact", "exceptional", "flawless", "radiant"];

function pct(v: number): string {
  return `${Number.isInteger(v) ? v : v.toFixed(2).replace(/0$/, "")}%`;
}

function openWeb(url: string): void {
  Navigation.NavigateToExternalWeb(url);
  Navigation.CloseSideMenus();
}

const RelicModal: FC<{ name: string; closeModal?: () => void }> = ({ name, closeModal }) => {
  const d = useRpc<RelicDetail | null>("wiki.relic", { name });
  return (
    <ModalRoot closeModal={closeModal} onCancel={closeModal}>
      {d.error && <ErrorText error={d.error} />}
      {d.data === undefined && !d.error && <Loading />}
      {d.data === null && <Empty text={`No relic named ${name}.`} />}
      {d.data && (
        <>
          <div style={{ fontSize: "20px", fontWeight: 600, marginBottom: "4px" }}>
            {d.data.name} Relic
            {d.data.vaulted ? <Badge text="Vaulted" color={colors.warn} /> : <Badge text="Available" color={colors.good} />}
          </div>
          <div style={{ color: colors.muted, marginBottom: "8px", fontSize: "13px" }}>
            Owned: {d.data.ownedTotal ? REFS.filter((r) => d.data!.owned[r]).map((r) => `${d.data!.owned[r]} ${r}`).join(", ") : "none"}
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr repeat(4, 64px) 50px", fontSize: "12px", color: colors.muted, padding: "2px" }}>
            <span>Reward</span>
            <span>Intact</span>
            <span>Exc.</span>
            <span>Flaw.</span>
            <span>Rad.</span>
            <span>Ducats</span>
          </div>
          {d.data.rewards.map((r) => (
            <Focusable
              key={r.item}
              focusClassName="gpfocus"
              style={{ display: "grid", gridTemplateColumns: "1fr repeat(4, 64px) 50px", padding: "4px 2px", borderBottom: `1px solid ${colors.border}` }}
            >
              <span style={{ color: RARITY_COLOR[r.rarity] }}>{r.item}</span>
              {REFS.map((q) => (
                <span key={q}>{pct(r.chances[q])}</span>
              ))}
              <Ducats value={r.ducats} />
            </Focusable>
          ))}
          <Focusable style={{ display: "flex", gap: "10px", marginTop: "12px" }} flow-children="horizontal">
            <DialogButton style={{ width: "200px", minWidth: 0 }} onClick={() => openWeb(d.data!.wikiUrl)}>
              Open wiki
            </DialogButton>
            <DialogButton style={{ width: "140px", minWidth: 0 }} onClick={closeModal}>
              Close
            </DialogButton>
          </Focusable>
        </>
      )}
    </ModalRoot>
  );
};

function showRelic(name: string): void {
  showModal(<RelicModal name={name} />);
}

const DropCard: FC<{ r: DropRow; mode: Mode }> = ({ r, mode }) => (
  <Focusable focusClassName="gpfocus" onActivate={() => showRelic(r.relic)} style={{ ...card, display: "flex", justifyContent: "space-between", alignItems: "center", gap: "8px" }}>
    <div style={{ minWidth: 0 }}>
      <div style={{ display: "flex", alignItems: "center", minWidth: 0 }}>
        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: mode === "item" ? colors.text : RARITY_COLOR[r.rarity] }}>
          {mode === "item" ? `${r.relic} Relic` : r.item}
        </span>
        {r.vaulted ? <Badge text="Vaulted" color={colors.muted} /> : <Badge text="Available" color={colors.good} />}
        {r.owned > 0 && <Badge text={`Owned ${r.owned}`} color={colors.accent} />}
      </div>
      <div style={{ fontSize: "12px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: mode === "item" ? RARITY_COLOR[r.rarity] : colors.muted }}>
        {mode === "item" ? `${r.item} · ${r.rarity}` : `${r.relic} Relic · ${r.rarity}`}
      </div>
    </div>
    <span style={{ flexShrink: 0, fontVariantNumeric: "tabular-nums", fontSize: "13px" }}>
      {pct(r.chances.intact)} <span style={{ color: colors.muted }}>→</span> {pct(r.chances.radiant)}
    </span>
  </Focusable>
);

const ItemCard: FC<{ s: ItemSummary }> = ({ s }) => (
  <Focusable
    focusClassName="gpfocus"
    onActivate={() => openWeb(s.wikiUrl)}
    style={{ ...card, display: "flex", justifyContent: "space-between", alignItems: "center", gap: "8px" }}
  >
    <div style={{ minWidth: 0 }}>
      <div style={{ fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{s.item}</div>
      <div style={{ fontSize: "12px", color: s.unvaulted ? colors.good : colors.warn }}>
        {s.unvaulted
          ? `In ${s.unvaulted} of ${s.relics} relics now · best ${pct(s.bestIntact)} intact / ${pct(s.bestRadiant)} radiant`
          : `Vaulted: ${s.relics} relics, none dropping now`}
      </div>
    </div>
    <div style={{ textAlign: "right", fontSize: "12px", flexShrink: 0 }}>
      <Ducats value={s.ducats} />
      
    </div>
  </Focusable>
);

const TierBrowser: FC<{ unvaultedOnly: boolean }> = ({ unvaultedOnly }) => {
  const [tier, setTier] = useState("Lith");
  const list = useRpc<{ name: string; vaulted: boolean; owned: number }[]>("wiki.relics", { tier, unvaulted_only: unvaultedOnly });
  return (
    <Section
      title="Browse relics"
      right={list.data ? <span style={{ color: colors.muted, fontSize: "12px" }}>{list.data.length} {tier} relics · A: rewards</span> : undefined}
    >
      <Segmented value={tier} options={TIERS.map((t) => ({ id: t, label: t }))} onChange={setTier} style={{ marginBottom: "8px" }} />
      {list.error && <ErrorText error={list.error} />}
      {!list.data && !list.error && <Loading />}
      {list.data && list.data.length === 0 && <Empty text={`No ${unvaultedOnly ? "unvaulted " : ""}${tier} relics.`} />}
      {list.data && list.data.length > 0 && (
        <Grid
          key={`${tier}|${unvaultedOnly}`}
          items={list.data}
          cols={5}
          rows={7}
          render={(r) => (
            <Focusable
              key={r.name}
              focusClassName="gpfocus"
              onActivate={() => showRelic(r.name)}
              style={{ ...card, display: "flex", flexDirection: "column", gap: "2px" }}
            >
              <span style={{ fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.name}</span>
              <span style={{ fontSize: "12px" }}>
                <span style={{ color: r.vaulted ? colors.muted : colors.good }}>{r.vaulted ? "Vaulted" : "Available"}</span>
                {r.owned > 0 && <span style={{ color: colors.accent }}> · Owned {r.owned}</span>}
              </span>
            </Focusable>
          )}
        />
      )}
    </Section>
  );
};

let lastQuery = "";
let lastMode: Mode = "item";

const Wiki: FC = () => {
  const [draft, setDraft] = useState(lastQuery);
  const [query, setQuery] = useState(lastQuery);
  const [mode, setMode] = useState<Mode>(lastMode);
  const [unvaultedOnly, setUnvaultedOnly] = useState(false);
  const [recent, setRecent] = useState<Recent[]>([]);
  const [res, setRes] = useState<SearchResult>();
  const [err, setErr] = useState<string>();
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    call<Recent[]>("wiki.recent").then(setRecent).catch(() => undefined);
  }, []);

  // Debounced search while typing; Search / recent buttons search at once and are remembered.
  useEffect(() => {
    const t = setTimeout(() => setQuery(draft.trim()), 450);
    return () => clearTimeout(t);
  }, [draft]);

  // Only the newest request may set results: a slow answer for an older query is dropped.
  const seq = useRef(0);
  // The search an explicit Search / recent click just ran, so the [query, mode] effect
  // does not repeat it once those states catch up.
  const ranKey = useRef("");
  const keyOf = (q: string, m: Mode, u: boolean) => `${m}|${u}|${q}`;

  const run = async (q: string, m: Mode, record: boolean) => {
    lastQuery = q;
    lastMode = m;
    const id = ++seq.current;
    ranKey.current = keyOf(q, m, unvaultedOnly);
    if (q.length < 2) {
      setRes(undefined);
      setBusy(false);
      return;
    }
    setBusy(true);
    try {
      const r = await call<SearchResult>("wiki.search", { query: q, mode: m, limit: 300, unvaulted_only: unvaultedOnly, record });
      if (id !== seq.current) return;
      setRes(r);
      setErr(undefined);
      if (r.recent) setRecent(r.recent);
    } catch (e: any) {
      if (id === seq.current) setErr(String(e?.message ?? e));
    } finally {
      if (id === seq.current) setBusy(false);
    }
  };

  // Search / recent: show results at once, skipping the typing debounce.
  const runNow = (q: string, m: Mode) => {
    setDraft(q);
    setQuery(q);
    setMode(m);
    void run(q, m, true);
  };

  useEffect(() => {
    if (ranKey.current === keyOf(query, mode, unvaultedOnly)) return;
    void run(query, mode, false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, mode, unvaultedOnly]);

  return (
    <Page>
      <Section
        title="Where does it drop?"
        right={<span style={{ color: colors.muted, fontSize: "12px" }}>Relic drop tables bundled with the plugin (weekly); no mission, bounty or enemy drops</span>}
      >
        <Focusable style={{ display: "flex", gap: "12px", alignItems: "flex-end", flexWrap: "wrap" }} flow-children="horizontal">
          <div style={{ flex: "1 1 320px", minWidth: 0 }}>
            <FilterCell label={mode === "item" ? "Item name, e.g. Ash Prime or Forma" : "Relic name, e.g. Lith A1 or Axi"}>
              <TextField label="" value={draft} onChange={(e) => setDraft(e.target.value)} />
            </FilterCell>
          </div>
          <FilterCell label="Search in">
            <Focusable style={{ display: "flex", gap: "6px" }} flow-children="horizontal">
              <DialogButton style={chip(mode === "item")} onClick={() => setMode("item")}>
                Items: where it drops
              </DialogButton>
              <DialogButton style={chip(mode === "relic")} onClick={() => setMode("relic")}>
                Relics: what is inside
              </DialogButton>
            </Focusable>
          </FilterCell>
          <FilterCell label="Relics">
            <DialogButton style={chip(unvaultedOnly)} onClick={() => setUnvaultedOnly(!unvaultedOnly)}>
              Available only
            </DialogButton>
          </FilterCell>
          <DialogButton style={{ ...chip(false), minWidth: "100px" }} disabled={busy || draft.trim().length < 2} onClick={() => runNow(draft.trim(), mode)}>
            Search
          </DialogButton>
        </Focusable>
        {recent.length > 0 && (
          <Focusable style={{ display: "flex", gap: "8px", flexWrap: "wrap", alignItems: "center", marginTop: "8px" }} flow-children="horizontal">
            <span style={{ fontSize: "11px", color: colors.muted, textTransform: "uppercase" }}>Recent</span>
            {recent.map((r) => (
              <DialogButton
                key={`${r.mode}:${r.q}`}
                style={{ ...chip(false), fontSize: "13px" }}
                onClick={() => runNow(r.q, r.mode)}
              >
                {r.mode === "relic" ? `Relic: ${r.q}` : r.q}
              </DialogButton>
            ))}
            <DialogButton
              style={{ ...chip(false), fontSize: "13px" }}
              onClick={async () => setRecent(await call<Recent[]>("wiki.recent_clear"))}
            >
              Clear
            </DialogButton>
          </Focusable>
        )}
      </Section>
      {err && <ErrorText error={err} />}
      {res && query.length >= 2 && (
        <>
          {res.items.length > 0 && (
            <Section title="Items" right={<span style={{ color: colors.muted, fontSize: "12px" }}>A: open the wiki</span>}>
              <Grid items={res.items.slice(0, 6)} cols={3} rows={2} render={(s) => <ItemCard key={s.item} s={s} />} />
            </Section>
          )}
          <Section title={`${res.total} result${res.total === 1 ? "" : "s"}`} right={<span style={{ color: colors.muted, fontSize: "12px" }}>chance intact → radiant · A: relic</span>}>
            {res.total === 0 ? (
              <Empty text={`Nothing matches "${res.query}".`} />
            ) : (
              <Grid key={`${res.mode}|${res.query}`} items={res.rows} cols={2} rows={10} render={(r, i) => <DropCard key={`${r.relic}|${r.item}|${i}`} r={r} mode={res.mode} />} />
            )}
          </Section>
        </>
      )}
      {(!res || query.length < 2) && <TierBrowser unvaultedOnly={unvaultedOnly} />}
    </Page>
  );
};

export default Wiki;

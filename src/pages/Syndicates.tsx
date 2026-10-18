// Syndicates: rank, standing to next rank, today's remaining standing per pool and rank-up
// goals with their credits / items / days (docs/port/menus.md §11). Data: syndicates.* RPCs
// (py_modules/wfh/menus/syndicates.py).
import { ButtonItem, DialogButton, Dropdown, Field, Focusable, PanelSection, PanelSectionRow } from "@decky/ui";
import { FC, ReactNode, useState } from "react";

import { Bar, Empty, ErrorText, Grid, Loading, Page, Section, Stat, card, colors, ellipsis } from "../components/ui";
import { call, useEvent, useRpc } from "../rpc";
import { openPage } from "./Mastery";

// ---- types (mirror menus/syndicates.py) -------------------------------------------------------

interface SynRow {
  tag: string;
  name: string;
  kind: "normal" | "openWorld" | "other";
  bin: string;
  level: number;
  title: string;
  standing: number;
  nextLevel: number | null;
  nextTitle: string;
  standingToNext: number;
  tierStart: number;
  tierEnd: number;
  progress: number;
  initiated: boolean;
  joined: boolean;
  dailyRemaining: number | null;
  maxLevel: number;
  goal: number | null;
  goalOptions: { level: number; title: string }[];
}

interface Pool {
  bin: string;
  label: string;
  kind: string;
  remaining: number | null;
  cap: number;
  tags: string[];
}

interface Step {
  level: number;
  title: string;
  standingNeeded: number;
  credits: number;
  items: { itemType: string; count: number; name: string }[];
  initiation: boolean;
}

interface Needs {
  credits: { needed: number; owned: number; missing: number };
  items: { itemType: string; name: string; needed: number; owned: number; missing: number }[];
  standing: { bin: string; label: string; tags: string[]; needed: number; remainingToday: number | null; dailyCap: number; daysEstimate: number }[];
}

interface View {
  inventory: boolean;
  mr: number | null;
  dailyCap: number;
  pools: Pool[];
  groups: { kind: string; label: string; rows: SynRow[] }[];
  plans: { tag: string; name: string; targetLevel: number; steps: Step[] }[];
  needs: Needs | null;
  conflicts: { a: string; b: string; aName: string; bName: string; factor: number }[];
}

interface QamView {
  inventory: boolean;
  dailyCap: number;
  pools: Pool[];
  rows: Pick<SynRow, "tag" | "name" | "level" | "title" | "standing" | "standingToNext" | "progress" | "nextLevel" | "goal" | "dailyRemaining" | "maxLevel">[];
  more: number;
}

const fmt = (n: number | null | undefined) => (n == null ? "–" : Math.round(n).toLocaleString("en-US"));
const short = (n: number | null | undefined) => (n == null ? "–" : n >= 1000 ? `${Math.round(n / 100) / 10}k` : String(n));
const rankText = (r: { level: number; title: string }) => `Rank ${r.level}${r.title ? ` ${r.title}` : ""}`;

export function openSyndicates(): void {
  openPage("syndicates");
}

// ---- page -------------------------------------------------------------------------------------

// One syndicate: name and rank, progress to the next rank and the goal picker (the card's only
// d-pad stop; max-rank cards keep a focusable label so every card can be reached).
const SyndicateCard: FC<{ r: SynRow; onGoal: (tag: string, level: number | null) => void }> = ({ r, onGoal }) => {
  const top = r.nextLevel == null;
  return (
    <div style={{ ...card, display: "flex", flexDirection: "column", gap: "6px" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: "8px" }}>
        <span style={{ ...ellipsis, fontWeight: 600, color: r.joined ? colors.text : colors.muted }}>{r.name}</span>
        <span style={{ fontSize: "12px", flexShrink: 0, color: colors.muted }}>{fmt(r.standing)} st</span>
      </div>
      <div style={{ ...ellipsis, fontSize: "12px", color: r.level < 0 ? colors.bad : r.joined ? colors.text : colors.muted }}>{r.joined ? rankText(r) : "Not joined"}</div>
      <Bar value={top ? 1 : r.joined ? r.progress : 0} color={top ? colors.good : r.level < 0 ? colors.bad : colors.accent} />
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: "8px" }}>
        <span style={{ ...ellipsis, fontSize: "12px", color: colors.muted, minWidth: 0 }}>
          {top ? "" : `${fmt(r.standingToNext)} to rank ${r.nextLevel}${r.nextTitle ? ` ${r.nextTitle}` : ""}`}
        </span>
        <div style={{ width: "150px", flexShrink: 0 }}>
          {top ? (
            <Focusable focusClassName="gpfocus" onActivate={() => {}} style={{ textAlign: "center", fontSize: "13px", color: colors.good, padding: "6px 0" }}>
              Max rank
            </Focusable>
          ) : (
            <Dropdown
              menuLabel={`${r.name} goal`}
              strDefaultLabel="No goal"
              rgOptions={[{ data: null, label: "No goal" }, ...r.goalOptions.map((o) => ({ data: o.level, label: rankText(o) }))]}
              selectedOption={r.goal}
              renderButtonValue={() => (r.goal == null ? "No goal" : `Goal: rank ${r.goal}`)}
              onChange={(o) => onGoal(r.tag, o.data)}
            />
          )}
        </div>
      </div>
    </div>
  );
};

// Small focusable card for one goal requirement.
const NeedCard: FC<{ label: ReactNode; sub?: ReactNode; value: ReactNode }> = ({ label, sub, value }) => (
  <Focusable focusClassName="gpfocus" onActivate={() => {}} style={{ ...card, padding: "6px 10px", display: "flex", justifyContent: "space-between", alignItems: "center", gap: "8px" }}>
    <div style={{ minWidth: 0 }}>
      <div style={ellipsis}>{label}</div>
      {sub && <div style={{ ...ellipsis, fontSize: "12px", color: colors.muted }}>{sub}</div>}
    </div>
    <div style={{ flexShrink: 0, textAlign: "right" }}>{value}</div>
  </Focusable>
);

const NeedsPanel: FC<{ v: View; onClear: () => void }> = ({ v, onClear }) => {
  const n = v.needs;
  // no goal yet: the hint sits in the standing section above, so the page starts with the cards
  if (!n) return null;
  const cards: { key: string; label: ReactNode; sub?: ReactNode; value: ReactNode }[] = [
    ...n.standing.map((p) => ({
      key: `st:${p.bin}`,
      label: `${p.label} standing`,
      sub: `${fmt(p.remainingToday)} left today · cap ${fmt(p.dailyCap)}/day`,
      value: (
        <span>
          {fmt(p.needed)}
          <div style={{ fontSize: "12px", color: p.daysEstimate === 0 ? colors.good : colors.muted }}>{p.daysEstimate === 0 ? "today" : `~${p.daysEstimate} day${p.daysEstimate > 1 ? "s" : ""}`}</div>
        </span>
      ),
    })),
    {
      key: "credits",
      label: "Credits",
      sub: `${fmt(n.credits.owned)} owned`,
      value: <span style={{ color: n.credits.missing > 0 ? colors.bad : colors.good }}>{fmt(n.credits.needed)}</span>,
    },
    ...n.items.map((it) => ({
      key: `it:${it.itemType}`,
      label: it.name,
      sub: `${fmt(it.owned)} owned`,
      value: <span style={{ color: it.missing > 0 ? colors.bad : colors.good }}>{it.missing > 0 ? `${fmt(it.missing)} missing` : `${fmt(it.needed)} ✓`}</span>,
    })),
  ];
  return (
    <Section title="Goals" right={<DialogButton style={{ width: "auto", minWidth: 0, padding: "4px 12px" }} onClick={onClear}>Clear</DialogButton>}>
      {v.conflicts.length > 0 && (
        <div style={{ color: colors.warn, fontSize: "13px", margin: "4px 0 8px" }}>
          Opposed: {v.conflicts.map((c) => `${c.aName} / ${c.bName}`).join(", ")} (gaining one costs the other)
        </div>
      )}
      <Grid cols={3} items={cards} render={(c) => <NeedCard key={c.key} label={c.label} sub={c.sub} value={c.value} />} />
      {v.plans.map((p) => (
        <div key={p.tag} style={{ marginTop: "10px" }}>
          <div style={{ color: colors.accent, fontSize: "13px", marginBottom: "4px" }}>
            {p.name} → rank {p.targetLevel}
          </div>
          <Grid cols={3}
            items={p.steps}
            render={(s) => (
              <NeedCard
                key={s.level}
                label={`Rank ${s.level}${s.title ? ` ${s.title}` : ""}${s.initiation ? " (+ initiation)" : ""}`}
                sub={s.items.map((i) => `${i.count}x ${i.name}`).join(", ") || undefined}
                value={
                  <span style={{ fontSize: "13px" }}>
                    {fmt(s.standingNeeded)} st
                    <div style={{ fontSize: "12px", color: colors.muted }}>{short(s.credits)} cr</div>
                  </span>
                }
              />
            )}
          />
        </div>
      ))}
    </Section>
  );
};

const Syndicates: FC = () => {
  const q = useRpc<View>("syndicates.view", {});
  const [override, setOverride] = useState<View | null>(null);
  useEvent("syndicates.updated", () => {
    setOverride(null);
    q.reload();
  });
  const [actionError, setActionError] = useState<string | null>(null);
  const v = override ?? q.data;
  const run = async (method: string, params: Record<string, unknown> = {}) => {
    try {
      setOverride(await call<View>(method, params));
      setActionError(null);
    } catch (e) {
      setActionError(String((e as Error)?.message ?? e));
    }
  };
  const setGoal = (tag: string, level: number | null) => run("syndicates.set_goal", { tag, level });
  return (
    <Page>
      {q.error && !v && <ErrorText error={q.error} />}
      {actionError && <ErrorText error={actionError} />}
      {!v && !q.error && <Loading />}
      {v && (
        <>
          {!v.inventory && <Empty text="No inventory yet: ranks and standing appear once Warframe has been read." />}
          <Section title={`Standing left today (cap ${fmt(v.dailyCap)}${v.mr != null ? ` at MR ${v.mr}` : ""})`}>
            <Focusable style={{ display: "flex", flexWrap: "wrap", gap: "6px 0" }} focusClassName="gpfocus" onActivate={() => {}}>
              {v.pools
                .filter((p) => p.remaining != null)
                .map((p) => (
                  <Stat key={p.bin} label={p.label} value={short(p.remaining)} color={p.remaining === 0 ? colors.muted : colors.good} />
                ))}
              {v.pools.every((p) => p.remaining == null) && <span style={{ color: colors.muted }}>Not reported by the inventory</span>}
            </Focusable>
            {!v.needs && (
              <div style={{ color: colors.muted, fontSize: "13px", marginTop: "6px" }}>Pick a goal rank on a syndicate below to see the standing, credits and items it takes.</div>
            )}
          </Section>
          <NeedsPanel v={v} onClear={() => run("syndicates.clear_goals")} />
          {v.groups.map((g) => (
            <Section key={g.kind} title={g.label}>
              <Grid cols={3} items={g.rows} render={(r) => <SyndicateCard key={r.tag} r={r} onGoal={setGoal} />} />
            </Section>
          ))}
        </>
      )}
    </Page>
  );
};

export default Syndicates;

// ---- QAM --------------------------------------------------------------------------------------

export const Qam: FC = () => {
  const q = useRpc<QamView>("syndicates.qam", {}, [], 120000);
  useEvent("syndicates.updated", () => q.reload());
  const d = q.data;
  return (
    <PanelSection title="Syndicates">
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
      {d && !d.inventory && (
        <PanelSectionRow>
          <Field label="Waiting for the inventory" />
        </PanelSectionRow>
      )}
      {d && d.inventory && (
        <>
          {d.pools.length > 0 && (
            <PanelSectionRow>
              <Field focusable padding="compact" label="Left today" description={d.pools.map((p) => `${p.label} ${short(p.remaining)}`).join(" · ")} onActivate={openSyndicates} />
            </PanelSectionRow>
          )}
          {d.rows.map((r) => (
            <PanelSectionRow key={r.tag}>
              <Field
                focusable
                padding="compact"
                label={r.name}
                description={
                  <div style={{ width: "260px", maxWidth: "100%" }}>
                    <div style={{ display: "flex", justifyContent: "space-between", gap: "6px" }}>
                      <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {rankText(r)}
                        {r.goal != null && <span style={{ color: colors.accent }}> → {r.goal}</span>}
                      </span>
                      <span style={{ flexShrink: 0 }}>{r.nextLevel == null ? "Max" : `${short(r.standingToNext)} to go`}</span>
                    </div>
                    <div style={{ marginTop: "3px" }}>
                      <Bar value={r.nextLevel == null ? 1 : r.progress} color={r.nextLevel == null ? colors.good : r.level < 0 ? colors.bad : colors.accent} />
                    </div>
                  </div>
                }
                onActivate={openSyndicates}
              />
            </PanelSectionRow>
          ))}
          {d.rows.length === 0 && (
            <PanelSectionRow>
              <Field label="No syndicate joined yet" />
            </PanelSectionRow>
          )}
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={openSyndicates}>
              Open syndicates{d.more > 0 ? ` (+${d.more})` : ""}
            </ButtonItem>
          </PanelSectionRow>
        </>
      )}
    </PanelSection>
  );
};

// Syndicates: rank, standing to next rank, today's remaining standing per pool and rank-up
// goals with their credits / items / days (docs/port/menus.md §11). Data: syndicates.* RPCs
// (py_modules/wfh/menus/syndicates.py).
import { ButtonItem, DialogButton, DropdownItem, Field, Focusable, PanelSection, PanelSectionRow } from "@decky/ui";
import { FC, useState } from "react";

import { Empty, ErrorText, Loading, Page, Row, Section, Stat, colors } from "../components/ui";
import { call, useEvent, useRpc } from "../rpc";
import { Bar, openPage } from "./Mastery";

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

const SyndicateRow: FC<{ r: SynRow; onGoal: (tag: string, level: number | null) => void }> = ({ r, onGoal }) => {
  const top = r.nextLevel == null;
  return (
    <div style={{ borderBottom: `1px solid ${colors.border}`, padding: "4px 0" }}>
      <Focusable style={{ display: "grid", gridTemplateColumns: "minmax(0, 1.4fr) minmax(0, 1fr) 260px", gap: "12px", alignItems: "center" }} flow-children="horizontal">
        <Focusable focusClassName="gpfocus" onActivate={() => {}} style={{ minWidth: 0 }}>
          <div style={{ color: r.joined ? colors.text : colors.muted, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{r.name}</div>
          <div style={{ fontSize: "12px", color: r.level < 0 ? colors.bad : colors.muted }}>
            {r.joined ? rankText(r) : "Not joined"} · {fmt(r.standing)} standing
          </div>
        </Focusable>
        <div>
          <Bar value={r.progress} color={top ? colors.good : r.level < 0 ? colors.bad : colors.accent} />
          <div style={{ fontSize: "12px", color: colors.muted, marginTop: "2px" }}>
            {top ? "Max rank" : `${fmt(r.standingToNext)} to rank ${r.nextLevel}${r.nextTitle ? ` ${r.nextTitle}` : ""}`}
          </div>
        </div>
        <DropdownItem
          label="Goal"
          strDefaultLabel="None"
          disabled={top}
          rgOptions={[{ data: null, label: "None" }, ...r.goalOptions.map((o) => ({ data: o.level, label: `${o.level}${o.title ? ` ${o.title}` : ""}` }))]}
          selectedOption={r.goal}
          onChange={(o) => onGoal(r.tag, o.data)}
        />
      </Focusable>
    </div>
  );
};

const NeedsPanel: FC<{ v: View; onClear: () => void }> = ({ v, onClear }) => {
  const n = v.needs;
  if (!n) {
    return (
      <Section title="Goals">
        <Empty text="Pick a target rank on a syndicate to see the standing, credits and items it takes." />
      </Section>
    );
  }
  return (
    <Section title="Goals" right={<DialogButton style={{ width: "auto", minWidth: 0, padding: "4px 12px" }} onClick={onClear}>Clear</DialogButton>}>
      {v.conflicts.length > 0 && (
        <div style={{ color: colors.warn, fontSize: "13px", margin: "4px 0 8px" }}>
          Opposed: {v.conflicts.map((c) => `${c.aName} / ${c.bName}`).join(", ")} (gaining one costs the other)
        </div>
      )}
      {n.standing.map((p) => (
        <Row
          key={p.bin}
          label={`${p.label} standing`}
          sub={`${fmt(p.remainingToday)} left today · cap ${fmt(p.dailyCap)}/day`}
          value={
            <span>
              {fmt(p.needed)}
              <div style={{ fontSize: "12px", color: p.daysEstimate === 0 ? colors.good : colors.muted }}>{p.daysEstimate === 0 ? "today" : `~${p.daysEstimate} day${p.daysEstimate > 1 ? "s" : ""}`}</div>
            </span>
          }
        />
      ))}
      <Row
        label="Credits"
        sub={`${fmt(n.credits.owned)} owned`}
        value={<span style={{ color: n.credits.missing > 0 ? colors.bad : colors.good }}>{fmt(n.credits.needed)}</span>}
      />
      {n.items.map((it) => (
        <Row
          key={it.itemType}
          label={it.name}
          sub={`${fmt(it.owned)} owned`}
          value={<span style={{ color: it.missing > 0 ? colors.bad : colors.good }}>{it.missing > 0 ? `${fmt(it.missing)} missing` : `${fmt(it.needed)} ✓`}</span>}
        />
      ))}
      {v.plans.map((p) => (
        <div key={p.tag} style={{ marginTop: "8px" }}>
          <div style={{ color: colors.accent, fontSize: "13px" }}>
            {p.name} → rank {p.targetLevel}
          </div>
          {p.steps.map((s) => (
            <Row
              key={s.level}
              label={`Rank ${s.level}${s.title ? ` ${s.title}` : ""}${s.initiation ? " (+ initiation)" : ""}`}
              sub={s.items.map((i) => `${i.count}x ${i.name}`).join(", ") || undefined}
              value={
                <span style={{ fontSize: "13px" }}>
                  {fmt(s.standingNeeded)} st · {short(s.credits)} cr
                </span>
              }
            />
          ))}
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
          </Section>
          <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) 360px", gap: "16px", alignItems: "start" }}>
            <div>
              {v.groups.map((g) => (
                <Section key={g.kind} title={g.label}>
                  {g.rows.map((r) => (
                    <SyndicateRow key={r.tag} r={r} onGoal={setGoal} />
                  ))}
                </Section>
              ))}
            </div>
            <NeedsPanel v={v} onClear={() => run("syndicates.clear_goals")} />
          </div>
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
                  <div>
                    <div>
                      {rankText(r)}
                      {r.goal != null && <span style={{ color: colors.accent }}> → {r.goal}</span>}
                    </div>
                    <div style={{ margin: "3px 0" }}>
                      <Bar value={r.progress} color={r.nextLevel == null ? colors.good : r.level < 0 ? colors.bad : colors.accent} />
                    </div>
                    <div>{r.nextLevel == null ? "Max rank" : `${fmt(r.standingToNext)} to next`}</div>
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

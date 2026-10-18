// Settings: every overlay/tracking option (generic form from settings.view), the PROTON_LOG
// launch option helper, data refresh and diagnostics (backend: py_modules/wfh/menus/settings_view.py).
import { ButtonItem, ConfirmModal, DialogButton, DropdownItem, Field, Focusable, PanelSection, PanelSectionRow, SliderField, TextField, ToggleField, showModal } from "@decky/ui";
import { toaster } from "@decky/api";
import { FC, useEffect, useRef, useState } from "react";

import { Badge, Columns, ErrorText, Guard, Loading, Page, Section, chip, colors } from "../components/ui";
import { call, useEvent, useRpc } from "../rpc";

interface FieldDef {
  key: string;
  label: string;
  kind: "toggle" | "slider" | "dropdown" | "text";
  description?: string;
  min?: number;
  max?: number;
  step?: number;
  unit?: string;
  options?: { value: string; label: string }[];
  check?: "ee_log" | "inventory_file" | null;
  value: any;
  default: any;
}

interface SectionDef {
  id: string;
  title: string;
  fields: FieldDef[];
}

interface PathCheck {
  path: string;
  exists: boolean;
  isFile: boolean;
  readable: boolean;
  size: number | null;
  mtime: number | null;
  error?: string;
  auto?: boolean;
}

interface EelogStatus {
  ee_log: string | null;
  ee_log_found: boolean;
  lines_seen: number;
  proton_log: string;
  proton_log_exists: boolean;
  proton_log_env: boolean | null;
  launch_options: string | null;
  launch_option_set: boolean;
  realtime_active: boolean;
  realtime_lines: number;
  launch_option_hint: string;
}

interface Diagnostics {
  core: any;
  eelog: EelogStatus & { error?: string };
  inventory: any;
  data: any;
  ms: number;
}

interface ProtonPlan {
  current: string;
  proposed: string;
  alreadySet: boolean;
  source: "steam" | "localconfig";
  appId: number;
  hint: string;
  valid: boolean;
}

const WARFRAME_APPID = 230410;

function fmtSize(n: number | null | undefined): string {
  if (n == null) return "–";
  if (n >= 1 << 20) return `${(n / (1 << 20)).toFixed(1)} MB`;
  if (n >= 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${n} B`;
}

function fmtTime(sec: number | null | undefined): string {
  if (!sec) return "never";
  return new Date(sec * 1000).toLocaleString();
}

type SetFn = (key: string, value: any, delay?: number, onFail?: (err: string) => void) => void;

function errText(e: any): string {
  return String(e?.message ?? e);
}

// Debounced writes so a slider held on the D-pad does not write the file per notch.
// onFail lets the control roll back to the saved value; failures are also toasted.
function useDebouncedSet(): SetFn {
  const timers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  // Values whose debounce timer has not fired yet; flushed on unmount (closing
  // the QAM or switching tabs unmounts this) so the last slider value is saved.
  const pending = useRef<Record<string, any>>({});
  useEffect(
    () => () => {
      Object.values(timers.current).forEach(clearTimeout);
      timers.current = {};
      for (const [key, value] of Object.entries(pending.current)) {
        call<FieldDef>("settings.set", { key, value }).catch(() => undefined);
      }
      pending.current = {};
    },
    [],
  );
  return (key, value, delay = 0, onFail) => {
    clearTimeout(timers.current[key]);
    delete timers.current[key];
    delete pending.current[key];
    const go = async () => {
      delete timers.current[key];
      delete pending.current[key];
      try {
        await call<FieldDef>("settings.set", { key, value });
      } catch (e: any) {
        const msg = errText(e);
        onFail?.(msg);
        toaster.toast({ title: "Setting not saved", body: msg });
      }
    };
    if (delay) {
      pending.current[key] = value;
      timers.current[key] = setTimeout(go, delay);
    }
    else void go();
  };
}

const PathField: FC<{ f: FieldDef }> = ({ f }) => {
  const [draft, setDraft] = useState<string>(f.value ?? "");
  const [saved, setSaved] = useState<string>(f.value ?? "");
  const [check, setCheck] = useState<PathCheck>();
  const [err, setErr] = useState<string>();
  const verify = async (p: string) => {
    if (!f.check) return;
    try {
      setCheck(await call<PathCheck>("settings.check_path", { kind: f.check, path: p }));
    } catch {
      setCheck(undefined);
    }
  };
  // A reload (section reset, another writer) replaces both the saved value and the draft.
  useEffect(() => {
    setSaved(f.value ?? "");
    setDraft(f.value ?? "");
    verify(f.value ?? "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [f.value]);
  const save = async (v: string) => {
    try {
      const r = await call<FieldDef>("settings.set", { key: f.key, value: v });
      const nv = r?.value ?? "";
      setErr(undefined);
      setSaved(nv);
      setDraft(nv); // the backend may expand ~ to the player's home
      await verify(nv);
    } catch (e: any) {
      setErr(errText(e));
    }
  };
  const dirty = draft.trim() !== saved;
  return (
    <>
      <TextField label={f.label} description={f.description} value={draft} onChange={(e) => setDraft(e.target.value)} />
      <Focusable style={{ display: "flex", gap: "8px", alignItems: "center", margin: "4px 0 8px" }} flow-children="horizontal">
        <DialogButton style={{ width: "110px", minWidth: 0, padding: "6px" }} disabled={!dirty} onClick={() => void save(draft.trim())}>
          Save
        </DialogButton>
        <DialogButton
          style={{ width: "110px", minWidth: 0, padding: "6px" }}
          disabled={!draft}
          onClick={() => void save("")}
        >
          Clear
        </DialogButton>
        {check && (
          <span style={{ fontSize: "12px", color: check.exists && check.readable ? colors.good : colors.warn, wordBreak: "break-all" }}>
            {check.auto ? (check.path ? `Found automatically: ${check.path}` : "Not found automatically") : check.exists ? (check.readable ? `OK · ${fmtSize(check.size)}` : "Not readable") : check.path ? `Missing: ${check.error ?? "not found"}` : ""}
          </span>
        )}
      </Focusable>
      {err && <ErrorText error={err} />}
    </>
  );
};

const FieldView: FC<{ f: FieldDef; set: SetFn }> = ({ f, set }) => {
  const [local, setLocal] = useState(f.value);
  useEffect(() => setLocal(f.value), [f.value]);
  const revert = () => setLocal(f.value);
  switch (f.kind) {
    case "toggle":
      return <ToggleField label={f.label} description={f.description || undefined} checked={!!local} onChange={(v) => { setLocal(v); set(f.key, v, 0, revert); }} />;
    case "slider":
      return (
        <SliderField
          label={f.label}
          description={f.description || undefined}
          value={Number(local)}
          min={f.min}
          max={f.max}
          step={f.step}
          showValue
          valueSuffix={f.unit ? ` ${f.unit}` : undefined}
          resetValue={f.default}
          onChange={(v) => {
            setLocal(v);
            set(f.key, v, 400, revert);
          }}
        />
      );
    case "dropdown":
      return (
        <DropdownItem
          label={f.label}
          description={f.description || undefined}
          rgOptions={(f.options ?? []).map((o) => ({ data: o.value, label: o.label }))}
          selectedOption={local}
          onChange={(o) => {
            setLocal(o.data);
            set(f.key, o.data, 0, revert);
          }}
        />
      );
    case "text":
      return <PathField f={f} />;
    default:
      return null;
  }
};

async function currentLaunchOptions(): Promise<string | undefined> {
  // The Steam client's own copy is authoritative; localconfig.vdf (backend) is the fallback.
  try {
    const d = window.appDetailsStore?.GetAppDetails?.(WARFRAME_APPID);
    if (d && typeof d.strLaunchOptions === "string") return d.strLaunchOptions;
  } catch {
    /* not available in this client build */
  }
  try {
    return await new Promise<string | undefined>((resolve) => {
      let done = false;
      const reg = SteamClient.Apps.RegisterForAppDetails(WARFRAME_APPID, (d: any) => {
        if (done) return;
        done = true;
        reg?.unregister?.();
        resolve(typeof d?.strLaunchOptions === "string" ? d.strLaunchOptions : undefined);
      });
      setTimeout(() => {
        if (done) return;
        done = true;
        reg?.unregister?.();
        resolve(undefined);
      }, 1500);
    });
  } catch {
    return undefined;
  }
}

async function offerProtonLog(onDone: (msg: string) => void): Promise<void> {
  const current = await currentLaunchOptions();
  const plan = await call<ProtonPlan>("settings.proton_plan", current === undefined ? {} : { current });
  if (plan.alreadySet) {
    onDone("PROTON_LOG is already in Warframe's launch options. Restart the game if real-time lines are not active yet.");
    return;
  }
  if (!plan.valid) {
    onDone(`Your launch options could not be parsed safely. Add ${plan.hint} by hand in Warframe > Properties > Launch options.`);
    return;
  }
  showModal(
    <ConfirmModal
      strTitle="Change Warframe's launch options?"
      strDescription={
        <div>
          <div style={{ marginBottom: "8px" }}>
            PROTON_LOG=1 makes Proton write the game's debug output to a log the plugin reads at once, so reward and relic overlays appear without EE.log's 10-25 s delay. The log grows while you play (Proton recreates it each launch).
          </div>
          <div style={{ fontSize: "13px", color: colors.muted }}>Current{plan.source === "localconfig" ? " (from Steam's config file)" : ""}:</div>
          <div style={{ fontFamily: "monospace", marginBottom: "6px", wordBreak: "break-all" }}>{plan.current || "(empty)"}</div>
          <div style={{ fontSize: "13px", color: colors.muted }}>New:</div>
          <div style={{ fontFamily: "monospace", wordBreak: "break-all" }}>{plan.proposed}</div>
          <div style={{ marginTop: "8px", fontSize: "13px" }}>Takes effect the next time Warframe starts.</div>
        </div>
      }
      strOKButtonText="Set launch options"
      onOK={() => {
        try {
          SteamClient.Apps.SetAppLaunchOptions(plan.appId, plan.proposed);
          onDone("Launch options updated. Restart Warframe to turn on real-time lines.");
        } catch (e: any) {
          onDone(`Steam refused the change: ${String(e?.message ?? e)}. Add ${plan.hint} by hand.`);
        }
      }}
    />,
  );
}

const ProtonSection: FC<{ eelog?: EelogStatus; reload: () => void }> = ({ eelog, reload }) => {
  const [msg, setMsg] = useState<string>();
  return (
    <Section title="Real-time EE.log (PROTON_LOG)">
      <div style={{ fontSize: "13px", color: colors.muted, marginBottom: "6px" }}>
        Warframe writes EE.log lazily, so overlays triggered from it can be 10-25 s late. With <code>PROTON_LOG=1 %command%</code> in Warframe's launch options the plugin reads the game's debug output as it happens.
      </div>
      {eelog && (
        <Focusable focusClassName="gpfocus" style={{ fontSize: "13px", lineHeight: 1.6 }}>
          <div>
            Launch option:{" "}
            {eelog.launch_option_set ? <span style={{ color: colors.good }}>set</span> : <span style={{ color: colors.warn }}>not set</span>}
            {eelog.launch_options ? <span style={{ color: colors.muted }}> · {eelog.launch_options}</span> : null}
          </div>
          <div>
            Real-time feed:{" "}
            {eelog.realtime_active ? <span style={{ color: colors.good }}>active ({eelog.realtime_lines} lines)</span> : <span style={{ color: colors.muted }}>{eelog.proton_log_env === false ? "game running without PROTON_LOG" : "inactive"}</span>}
          </div>
          <div style={{ color: colors.muted, wordBreak: "break-all" }}>
            {eelog.proton_log} {eelog.proton_log_exists ? "" : "(not created yet)"}
          </div>
        </Focusable>
      )}
      <Focusable style={{ display: "flex", gap: "10px", marginTop: "8px" }} flow-children="horizontal">
        <DialogButton
          style={{ width: "auto", minWidth: 0, padding: "8px 18px" }}
          disabled={eelog?.launch_option_set}
          onClick={() =>
            offerProtonLog((m) => {
              setMsg(m);
              setTimeout(reload, 1500);
            }).catch((e) => setMsg(String(e?.message ?? e)))
          }
        >
          {eelog?.launch_option_set ? "PROTON_LOG is set" : "Add PROTON_LOG=1 to launch options…"}
        </DialogButton>
      </Focusable>
      {msg && <div style={{ marginTop: "6px", color: colors.accent }}>{msg}</div>}
    </Section>
  );
};

const KV: FC<{ k: string; v: any; color?: string }> = ({ k, v, color }) => (
  <div style={{ display: "flex", gap: "8px", fontSize: "13px", padding: "1px 0" }}>
    <span style={{ color: colors.muted, minWidth: "170px" }}>{k}</span>
    <span style={{ color: color ?? colors.text, wordBreak: "break-all" }}>{v === null || v === undefined || v === "" ? "–" : String(v)}</span>
  </div>
);

const DataSection: FC<{ data: any; reload: () => void }> = ({ data, reload }) => {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string>();
  const active = data?.active === "runtime" ? data?.runtime : data?.bundle;
  return (
    <Section title="Game data">
      {data?.error ? (
        <ErrorText error={data.error} />
      ) : (
        <Focusable focusClassName="gpfocus">
          <KV k="Active copy" v={data?.active} />
          <KV k="Version" v={active?.version ?? data?.items?.version} />
          <KV k="Built from" v={active?.source_time ?? active?.built_at} />
          <KV k="Counts" v={active?.counts ? Object.entries(active.counts).map(([k, n]) => `${n} ${k}`).join(", ") : undefined} />
          <KV k="Last online check" v={fmtTime(data?.last_check)} />
        </Focusable>
      )}
      <Focusable style={{ display: "flex", gap: "10px", alignItems: "center", marginTop: "8px" }} flow-children="horizontal">
        <DialogButton
          style={{ width: "260px", minWidth: 0 }}
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            setMsg("Checking WFCD for new item and relic data…");
            try {
              const r = await call<{ status: string; counts?: Record<string, number> }>("data.refresh", { force: true });
              setMsg(
                {
                  updated: "Updated to the newest data.",
                  unchanged: "Already up to date.",
                  busy: "A refresh is already running.",
                  disabled: "Runtime refresh is not available.",
                  fresh: "Checked recently.",
                }[r.status] ?? r.status,
              );
              reload();
            } catch (e: any) {
              setMsg(`Refresh failed: ${String(e?.message ?? e)}`);
            } finally {
              setBusy(false);
            }
          }}
        >
          {busy ? "Refreshing…" : "Check for data update"}
        </DialogButton>
        {msg && <span style={{ fontSize: "13px", color: colors.accent }}>{msg}</span>}
      </Focusable>
    </Section>
  );
};

const DiagnosticsSection: FC<{ d: Diagnostics; reload: () => void }> = ({ d, reload }) => {
  const [raw, setRaw] = useState(false);
  const core = d.core ?? {};
  const inv = d.inventory ?? {};
  const ee = d.eelog ?? ({} as EelogStatus);
  return (
    <Section
      title="Diagnostics"
      right={
        <DialogButton style={{ width: "110px", minWidth: 0, padding: "4px" }} onClick={reload}>
          Refresh
        </DialogButton>
      }
    >
      <Focusable focusClassName="gpfocus">
        <KV k="Plugin version" v={core.version} />
        <KV k="Warframe" v={core.warframe_running ? "running" : "not running"} />
        <KV k="Display" v={core.display} />
        <KV k="Overlay window" v={core.overlay ? (core.overlay.connected ? "connected" : "not connected") : undefined} color={core.overlay?.connected ? colors.good : colors.warn} />
        {core.failed_modules?.length > 0 && <KV k="Failed modules" v={core.failed_modules.join(", ")} color={colors.bad} />}
        <KV k="EE.log" v={ee.ee_log ?? "not found"} color={ee.ee_log_found ? undefined : colors.warn} />
        <KV k="EE.log lines" v={ee.lines_seen} />
        <KV k="Inventory source" v={inv.source} />
        <KV k="Inventory" v={inv.found === false ? "none yet" : inv.last_run_ok === false ? `last read failed (${inv.last_reason ?? "?"})` : inv.origin ?? (inv.last_run_ok ? "ok" : undefined)} />
        <KV k="Last inventory read" v={fmtTime(inv.last_run_at)} />
        <KV k="Next automatic read" v={inv.next_auto_at ? fmtTime(inv.next_auto_at) : undefined} />
        {inv.last_error && <KV k="Inventory error" v={inv.last_error.message ?? JSON.stringify(inv.last_error)} color={colors.warn} />}
        <KV k="Collected in" v={`${d.ms} ms`} />
      </Focusable>
      <ToggleField label="Show raw status" checked={raw} onChange={setRaw} />
      {raw && (
        <Focusable focusClassName="gpfocus">
          <pre style={{ fontSize: "11px", whiteSpace: "pre-wrap", wordBreak: "break-all", color: colors.muted, maxHeight: "400px", overflowY: "auto" }}>
            {JSON.stringify(d, null, 2)}
          </pre>
        </Focusable>
      )}
    </Section>
  );
};

// Rough height of a settings section (in toggle rows), to split the sections into two
// balanced columns. Sections keep their order: the left column reads first.
const FIELD_WEIGHT: Record<FieldDef["kind"], number> = { toggle: 1, dropdown: 1.1, slider: 2.2, text: 3.2 };
function sectionWeight(s: SectionDef): number {
  let w = 1.6 + s.fields.reduce((a, f) => a + FIELD_WEIGHT[f.kind] + (f.description ? 0.4 : 0), 0);
  if (s.id === "inventory") w += 1;
  if (s.id === "game") w += 6;
  return w;
}
function splitColumns(sections: SectionDef[], extraRight: number): [SectionDef[], SectionDef[]] {
  const total = sections.reduce((a, s) => a + sectionWeight(s), 0) + extraRight;
  const left: SectionDef[] = [];
  let acc = 0;
  for (const s of sections) {
    const w = sectionWeight(s);
    // Stop when adding this section moves the left column further past half than leaving it out.
    if (left.length > 0 && Math.abs(acc + w - total / 2) > Math.abs(acc - total / 2)) break;
    left.push(s);
    acc += w;
  }
  return [left, sections.slice(left.length)];
}

const Settings: FC = () => {
  const view = useRpc<{ sections: SectionDef[] }>("settings.view");
  const diag = useRpc<Diagnostics>("settings.diagnostics");
  const [invBusy, setInvBusy] = useState(false);
  const [invMsg, setInvMsg] = useState<string>();
  const set = useDebouncedSet();
  return (
    <Page>
      <Section title="Quick actions" style={{ marginBottom: 0 }}>
        <Focusable style={{ display: "flex", gap: "10px", flexWrap: "wrap" }} flow-children="horizontal">
          <DialogButton style={chip(false)} onClick={() => call("overlay.test", { seconds: 6 })}>
            Test overlay
          </DialogButton>
          <DialogButton style={chip(false)} onClick={() => call("overlay.clear")}>
            Hide overlays now
          </DialogButton>
          <DialogButton
            style={chip(false)}
            disabled={invBusy}
            onClick={async () => {
              setInvBusy(true);
              setInvMsg("Reading inventory…");
              try {
                const s = await call<any>("inventory.refresh");
                setInvMsg(s?.last_run_ok ? "Inventory updated." : `Inventory read failed: ${s?.last_reason ?? "unknown"}`);
                diag.reload();
              } catch (e: any) {
                setInvMsg(String(e?.message ?? e));
              } finally {
                setInvBusy(false);
              }
            }}
          >
            Refresh inventory
          </DialogButton>
        </Focusable>
        {invMsg && <div style={{ marginTop: "6px", color: colors.accent, fontSize: "13px" }}>{invMsg}</div>}
      </Section>
      <Guard state={view}>
        {(v) => {
          const renderSection = (s: SectionDef) => (
            <Section
              key={s.id}
              title={s.title}
              right={
                <DialogButton
                  style={{ width: "100px", minWidth: 0, padding: "6px 4px", fontSize: "13px" }}
                  onClick={() =>
                    showModal(
                      <ConfirmModal
                        strTitle={`Reset ${s.title}?`}
                        strDescription="Restores the default value of every option in this section."
                        strOKButtonText="Reset"
                        onOK={async () => {
                          await call("settings.reset", { section: s.id });
                          view.reload();
                        }}
                      />,
                    )
                  }
                >
                  Defaults
                </DialogButton>
              }
            >
              {s.id === "inventory" && (
                <div style={{ fontSize: "12px", color: colors.muted }}>
                  "Game" reads your inventory from Warframe while it runs. "JSON file" uses an inventory you exported (e.g. from AlecaFrame); paths are on the Deck, ~ is the deck user's home.
                </div>
              )}
              {s.fields.map((f) => (
                <FieldView key={f.key} f={f} set={set} />
              ))}
              {s.id === "game" && <ProtonSection eelog={diag.data?.eelog} reload={diag.reload} />}
            </Section>
          );
          // Game data + Diagnostics close the right column (about 16 rows together).
          const [left, right] = splitColumns(v.sections, 16);
          return (
            <Columns
              cols={[
                left.map(renderSection),
                <>
                  {right.map(renderSection)}
                  {diag.data ? <DataSection data={diag.data.data} reload={diag.reload} /> : <Loading />}
                  {diag.error && <ErrorText error={diag.error} />}
                  {diag.data && <DiagnosticsSection d={diag.data} reload={diag.reload} />}
                </>,
              ]}
            />
          );
        }}
      </Guard>
      <div style={{ color: colors.muted, fontSize: "12px", margin: "8px 0" }}>
        WFHelper for Decky ports WFHelper (MIT). Hotkeys are not available in game mode; use the Quick Access Menu buttons instead.
        <Badge text="Gamescope" color={colors.muted} />
      </div>
    </Page>
  );
};

export default Settings;

interface QuickSettings {
  overlays_enabled: boolean;
  overlay_opacity: number;
  relic_rewards: { enabled: boolean };
  relic_planner: { enabled: boolean };
  riven_scanner: { enabled: boolean };
  arbitration: { enabled: boolean; tracking: boolean };
}

const QAM_TOGGLES: { key: string; label: string; get: (s: QuickSettings) => boolean }[] = [
  { key: "relic_rewards.enabled", label: "Relic reward prices", get: (s) => s.relic_rewards?.enabled },
  { key: "relic_planner.enabled", label: "Relic recommendations", get: (s) => s.relic_planner?.enabled },
  { key: "arbitration.enabled", label: "Arbitration summary", get: (s) => s.arbitration?.enabled },
];

// QAM: per-overlay switches (the riven switch lives in the Rivens section), opacity and the
// manual reward scan that replaces WFHelper's F8. settings.get only reads in-memory settings;
// "settings.changed" (any writer, e.g. the master Overlays switch) refreshes the snapshot.
export const Qam: FC = () => {
  const s = useRpc<QuickSettings>("settings.get");
  useEvent("settings.changed", () => s.reload());
  const set = useDebouncedSet();
  const [opacity, setOpacity] = useState<number>();
  const [scanMsg, setScanMsg] = useState<string>();
  const [scanBusy, setScanBusy] = useState(false);
  const d = s.data;
  if (!d)
    return (
      <PanelSection title="Overlays">
        <PanelSectionRow>
          <Field label={s.error ? "Error" : "Loading…"} description={s.error} />
        </PanelSectionRow>
      </PanelSection>
    );
  return (
    <PanelSection title="Overlays">
      {QAM_TOGGLES.map((t) => (
        <PanelSectionRow key={t.key}>
          <ToggleField
            label={t.label}
            checked={!!t.get(d)}
            disabled={!d.overlays_enabled}
            onChange={async (v) => {
              try {
                await call("settings.set", { key: t.key, value: v });
              } catch (e: any) {
                toaster.toast({ title: "Setting not saved", body: errText(e) });
              }
              s.reload();
            }}
          />
        </PanelSectionRow>
      ))}
      <PanelSectionRow>
        <SliderField
          label="Opacity"
          value={opacity ?? d.overlay_opacity}
          min={0.3}
          max={1}
          step={0.05}
          showValue
          onChange={(v) => {
            setOpacity(v);
            set("overlay_opacity", v, 400, () => setOpacity(undefined));
          }}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          disabled={scanBusy}
          description={scanMsg}
          onClick={async () => {
            setScanBusy(true);
            try {
              const r = await call<{ started: boolean }>("rewards.scan_now");
              setScanMsg(r?.started ? "Scanning the reward screen…" : "Relic reward prices are turned off.");
            } catch (e: any) {
              setScanMsg(`Scan failed: ${errText(e)}`);
            } finally {
              setScanBusy(false);
            }
          }}
        >
          Scan rewards now
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={() => call("overlay.clear")}>
          Hide overlays now
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
};

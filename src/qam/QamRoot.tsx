import { ButtonItem, DialogButton, Dropdown, Field, Focusable, PanelSection, PanelSectionRow, ToggleField } from "@decky/ui";
import { Component, FC, ReactNode, useState } from "react";
import { FaExpand } from "react-icons/fa";

import { colors } from "../components/ui";
import { openPage } from "../nav";
import { PAGES } from "../registry";
import { call, useEvent, useRpc } from "../rpc";
import { Qam as ArbitrationsQam } from "../pages/Arbitrations";
import { Qam as FoundryQam } from "../pages/Foundry";
import { Qam as InventoryQam } from "../pages/Inventory";
import { Qam as MarketQam, openMarket } from "../pages/Market";
import { Qam as MasteryQam, openMastery } from "../pages/Mastery";
import { Qam as MissionsQam } from "../pages/Missions";
import { Qam as RelicsQam } from "../pages/Relics";
import { Qam as RivensQam } from "../pages/Rivens";
import { Qam as SettingsQam } from "../pages/Settings";
import { Qam as StatsQam } from "../pages/Stats";
import { Qam as SyndicatesQam } from "../pages/Syndicates";
import { Qam as WikiQam } from "../pages/Wiki";
import { Qam as WorldQam, openWorld } from "../pages/World";

// Compact summaries the QAM can show, one at a time (the panel is ~300px wide,
// so stacking them all made a long scroll). Each loads its own small cached
// view-model (world.qam, arbi.qam, ...). `open` is the header's full-screen
// button (default: the page with the same id, on whatever sub-tab it last had).
// Titles stay short: they are the picker's label next to that button.
const SECTIONS: { id: string; title: string; Qam: FC | null; open?: () => void }[] = [
  { id: "world", title: "World", Qam: WorldQam, open: () => openWorld("overview") },
  { id: "arbitrations", title: "Arbitrations", Qam: ArbitrationsQam },
  { id: "relics", title: "Relics", Qam: RelicsQam },
  { id: "market", title: "Market", Qam: MarketQam, open: () => openMarket("browse") },
  { id: "foundry", title: "Foundry", Qam: FoundryQam },
  { id: "missions", title: "Missions", Qam: MissionsQam },
  { id: "inventory", title: "Inventory", Qam: InventoryQam },
  { id: "mastery", title: "Mastery", Qam: MasteryQam, open: () => openMastery("roadmap") },
  { id: "syndicates", title: "Syndicates", Qam: SyndicatesQam },
  { id: "rivens", title: "Rivens", Qam: RivensQam },
  { id: "stats", title: "Stats", Qam: StatsQam },
  { id: "wiki", title: "Wiki", Qam: WikiQam },
  { id: "settings", title: "Settings", Qam: SettingsQam },
].filter((x) => x.Qam);

const ICONS: Record<string, FC> = Object.fromEntries(PAGES.map((p) => [p.id, p.icon]));

const SECTION_KEY = "wfhelper.qamSection";

function loadSection(): string {
  try {
    const v = localStorage.getItem(SECTION_KEY);
    if (v && SECTIONS.some((x) => x.id === v)) return v;
  } catch {
    // storage unavailable: fall back to the default
  }
  return "world";
}

interface Status {
  version: string;
  warframe_running: boolean;
  ee_log: string | null;
  display: string | null;
  overlay: { connected: boolean; enabled: boolean; layers: string[] };
  failed_modules: string[];
}

// One broken page section must not blank the whole Quick Access Menu.
class SectionBoundary extends Component<{ id: string; children: ReactNode }, { error: string | null }> {
  state = { error: null as string | null };
  static getDerivedStateFromError(e: unknown) {
    return { error: String((e as Error)?.message ?? e) };
  }
  componentDidCatch(e: unknown) {
    console.error("[WFHelper] QAM section failed", this.props.id, e);
  }
  render() {
    if (this.state.error) {
      return (
        <PanelSection title={this.props.id}>
          <PanelSectionRow>
            <Field label="Section failed" description={this.state.error} />
          </PanelSectionRow>
        </PanelSection>
      );
    }
    return this.props.children;
  }
}

// The overlay's state in one short line under the master switch: what is missing first.
function statusLine(s: Status): { text: string; color: string } {
  if (!s.warframe_running) return { text: "Warframe not running", color: colors.muted };
  if (!s.ee_log) return { text: "Warframe running · no log yet", color: colors.warn };
  if (!s.overlay.connected) return { text: "Waiting for the game display", color: colors.warn };
  return { text: "Drawing over the game", color: colors.good };
}

const QamRoot: FC = () => {
  const status = useRpc<Status>("core.status", {}, [], 5000);
  // Overlay switches elsewhere (Settings page, other sections) change overlay.enabled too.
  useEvent("settings.changed", () => status.reload());
  const [busy, setBusy] = useState(false);
  const [section, setSection] = useState(loadSection);
  const s = status.data;
  const current = SECTIONS.find((x) => x.id === section) ?? SECTIONS[0];
  const Qam = current?.Qam;
  const line = s ? statusLine(s) : null;
  const Icon = current ? ICONS[current.id] : undefined;
  return (
    <>
      <PanelSection>
        {status.error && (
          <PanelSectionRow>
            <Field label="Backend" description={status.error} />
          </PanelSectionRow>
        )}
        {s && line && (
          <>
            <PanelSectionRow>
              <ToggleField
                label="Overlays"
                description={
                  <span style={{ whiteSpace: "nowrap" }}>
                    <span style={{ color: line.color }}>●</span> {line.text}
                  </span>
                }
                checked={s.overlay.enabled}
                onChange={async (v) => {
                  await call("settings.update", { patch: { overlays_enabled: v } });
                  status.reload();
                }}
              />
            </PanelSectionRow>
            {s.failed_modules.length > 0 && (
              <PanelSectionRow>
                <Field label="Failed to load" description={s.failed_modules.join(", ")} />
              </PanelSectionRow>
            )}
          </>
        )}
        {/* Section picker and its full-screen button on one d-pad row. */}
        <PanelSectionRow>
          <Focusable flow-children="horizontal" style={{ display: "flex", gap: "8px", alignItems: "stretch", padding: "8px 0", borderBottom: `1px solid ${colors.border}` }}>
            <div style={{ flex: "1 1 auto", minWidth: 0 }}>
              <Dropdown
                menuLabel="Show in Quick Access"
                rgOptions={SECTIONS.map((x) => ({ data: x.id, label: x.title }))}
                selectedOption={current?.id}
                renderButtonValue={(el) => (
                  <span style={{ display: "inline-flex", alignItems: "center", gap: "8px", minWidth: 0, verticalAlign: "middle" }}>
                    {Icon && (
                      <span style={{ display: "inline-flex", color: colors.accent, flexShrink: 0 }}>
                        <Icon />
                      </span>
                    )}
                    {el}
                  </span>
                )}
                onChange={(o) => {
                  const id = String(o.data);
                  setSection(id);
                  try {
                    localStorage.setItem(SECTION_KEY, id);
                  } catch {
                    // not persisted; still switches for this session
                  }
                }}
              />
            </div>
            <DialogButton
              style={{ flex: "0 0 auto", width: "auto", minWidth: 0, padding: "0 14px", display: "flex", alignItems: "center", justifyContent: "center", gap: "6px", whiteSpace: "nowrap" }}
              onClick={() => (current?.open ? current.open() : openPage(current?.id ?? "dashboard"))}
            >
              <FaExpand style={{ flexShrink: 0 }} /> Open
            </DialogButton>
          </Focusable>
        </PanelSectionRow>
      </PanelSection>
      {Qam && current && (
        <SectionBoundary key={current.id} id={current.id}>
          <Qam />
        </SectionBoundary>
      )}
      <PanelSection>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              try {
                await call("overlay.test", { seconds: 6 });
              } finally {
                setBusy(false);
              }
            }}
          >
            Show test overlay
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
};

export default QamRoot;

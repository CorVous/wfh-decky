import { ButtonItem, DropdownItem, Field, Navigation, PanelSection, PanelSectionRow, ToggleField } from "@decky/ui";
import { Component, FC, ReactNode, useState } from "react";

import { openTab } from "../nav";
import { PAGES, ROUTE } from "../registry";
import { call, useEvent, useRpc } from "../rpc";
import { Qam as ArbitrationsQam } from "../pages/Arbitrations";
import { Qam as FoundryQam } from "../pages/Foundry";
import { Qam as InventoryQam } from "../pages/Inventory";
import { Qam as MarketQam } from "../pages/Market";
import { Qam as MasteryQam } from "../pages/Mastery";
import { Qam as MissionsQam } from "../pages/Missions";
import { Qam as RelicsQam } from "../pages/Relics";
import { Qam as RivensQam } from "../pages/Rivens";
import { Qam as SettingsQam } from "../pages/Settings";
import { Qam as StatsQam } from "../pages/Stats";
import { Qam as SyndicatesQam } from "../pages/Syndicates";
import { Qam as WikiQam } from "../pages/Wiki";
import { Qam as WorldQam } from "../pages/World";

// Compact summaries the QAM can show, one at a time (the panel is ~300px wide,
// so stacking them all made a long scroll). Each loads its own small cached
// view-model (world.qam, arbi.qam, ...).
const SECTIONS: { id: string; title: string; Qam: FC | null }[] = [
  { id: "world", title: "World", Qam: WorldQam },
  { id: "arbitrations", title: "Arbitrations", Qam: ArbitrationsQam },
  { id: "relics", title: "Relics", Qam: RelicsQam },
  { id: "market", title: "Market", Qam: MarketQam },
  { id: "foundry", title: "Foundry", Qam: FoundryQam },
  { id: "missions", title: "Last mission", Qam: MissionsQam },
  { id: "inventory", title: "Inventory", Qam: InventoryQam },
  { id: "mastery", title: "Mastery", Qam: MasteryQam },
  { id: "syndicates", title: "Syndicates", Qam: SyndicatesQam },
  { id: "rivens", title: "Rivens", Qam: RivensQam },
  { id: "stats", title: "Stats", Qam: StatsQam },
  { id: "wiki", title: "Wiki", Qam: WikiQam },
  { id: "settings", title: "Overlay settings", Qam: SettingsQam },
].filter((x) => x.Qam);

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

function openPage(id: string): void {
  openTab(id);
  Navigation.CloseSideMenus();
  Navigation.Navigate(ROUTE);
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
  const statusLine = s
    ? `${s.warframe_running ? "Warframe running" : "Warframe not running"} · ${s.ee_log ? "log found" : "no log yet"}`
    : "";
  return (
    <>
      <PanelSection>
        {status.error && (
          <PanelSectionRow>
            <Field label="Backend" description={status.error} />
          </PanelSectionRow>
        )}
        {s && (
          <>
            <PanelSectionRow>
              <ToggleField
                label="Overlays"
                description={`${s.overlay.connected ? "Drawing over the game" : "Waiting for the game display"}. ${statusLine}`}
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
        <PanelSectionRow>
          <DropdownItem
            label="Show"
            rgOptions={SECTIONS.map((x) => ({ data: x.id, label: x.title }))}
            selectedOption={current?.id}
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
        </PanelSectionRow>
      </PanelSection>
      {Qam && current && (
        <SectionBoundary key={current.id} id={current.id}>
          <Qam />
        </SectionBoundary>
      )}
      <PanelSection>
        <PanelSectionRow>
          <DropdownItem
            label="Full screen"
            strDefaultLabel="Open…"
            rgOptions={PAGES.map((p) => ({ data: p.id, label: p.title }))}
            selectedOption={null}
            onChange={(o) => openPage(String(o.data))}
          />
        </PanelSectionRow>
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

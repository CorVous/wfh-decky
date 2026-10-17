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

// Page sections shown under the status block, in this order (null = the page has none).
// World first (the primary QAM page), then Arbitrations and Relics; the remaining pages
// except Dashboard (whose summary repeats World/Foundry) follow; Foundry, Missions and Market
// close the list, then the overlay switches from Settings. Each section loads its own small
// view-model (world.qam, arbi.qam, ...), which the backend serves from caches.
const SECTIONS: { id: string; Qam: FC | null }[] = [
  { id: "world", Qam: WorldQam },
  { id: "arbitrations", Qam: ArbitrationsQam },
  { id: "relics", Qam: RelicsQam },
  { id: "inventory", Qam: InventoryQam },
  { id: "mastery", Qam: MasteryQam },
  { id: "syndicates", Qam: SyndicatesQam },
  { id: "rivens", Qam: RivensQam },
  { id: "stats", Qam: StatsQam },
  { id: "wiki", Qam: WikiQam },
  { id: "foundry", Qam: FoundryQam },
  { id: "missions", Qam: MissionsQam },
  { id: "market", Qam: MarketQam },
  { id: "settings", Qam: SettingsQam },
];

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
  const s = status.data;
  return (
    <>
      <PanelSection title="Status">
        {status.error && (
          <PanelSectionRow>
            <Field label="Backend" description={status.error} />
          </PanelSectionRow>
        )}
        {s && (
          <>
            <PanelSectionRow>
              <Field label="Warframe" focusable>
                {s.warframe_running ? "Running" : "Not running"}
              </Field>
            </PanelSectionRow>
            <PanelSectionRow>
              <Field label="EE.log" focusable description={s.ee_log ?? "Not found yet"} />
            </PanelSectionRow>
            <PanelSectionRow>
              <ToggleField
                label="Overlays"
                description={s.overlay.connected ? "Drawing over the game" : "Waiting for the game display"}
                checked={s.overlay.enabled}
                onChange={async (v) => {
                  await call("settings.update", { patch: { overlays_enabled: v } });
                  status.reload();
                }}
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
            {s.failed_modules.length > 0 && (
              <PanelSectionRow>
                <Field label="Failed to load" description={s.failed_modules.join(", ")} />
              </PanelSectionRow>
            )}
          </>
        )}
      </PanelSection>
      {/* One focusable row near the top so the full-screen pages are reachable without
          scrolling past every section; the full list stays at the bottom. */}
      <PanelSection>
        <PanelSectionRow>
          <DropdownItem
            label="Open full screen"
            strDefaultLabel="Choose a page…"
            rgOptions={PAGES.map((p) => ({ data: p.id, label: p.title }))}
            selectedOption={null}
            onChange={(o) => openPage(String(o.data))}
          />
        </PanelSectionRow>
      </PanelSection>
      {SECTIONS.map(({ id, Qam }) =>
        Qam ? (
          <SectionBoundary key={id} id={id}>
            <Qam />
          </SectionBoundary>
        ) : null,
      )}
      <PanelSection title="Open">
        {PAGES.map((p) => (
          <PanelSectionRow key={p.id}>
            <ButtonItem
              layout="below"
              onClick={() => openPage(p.id)}
            >
              <span style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                <p.icon />
                {p.title}
              </span>
            </ButtonItem>
          </PanelSectionRow>
        ))}
      </PanelSection>
    </>
  );
};

export default QamRoot;

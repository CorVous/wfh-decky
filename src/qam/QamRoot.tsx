import { ButtonItem, Field, Navigation, PanelSection, PanelSectionRow, ToggleField } from "@decky/ui";
import { FC, useState } from "react";

import { openTab } from "../FullPage";
import { PAGES, ROUTE } from "../registry";
import { call, useRpc } from "../rpc";

interface Status {
  version: string;
  warframe_running: boolean;
  ee_log: string | null;
  display: string | null;
  overlay: { connected: boolean; enabled: boolean; layers: string[] };
  failed_modules: string[];
}

const QamRoot: FC = () => {
  const status = useRpc<Status>("core.status", {}, [], 5000);
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
      <PanelSection title="Open">
        {PAGES.map((p) => (
          <PanelSectionRow key={p.id}>
            <ButtonItem
              layout="below"
              onClick={() => {
                openTab(p.id);
                Navigation.CloseSideMenus();
                Navigation.Navigate(ROUTE);
              }}
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

// Preview harness views (dev only):
//   ?page=<id>    one full-screen page: the real src/FullPage.tsx (Steam Tabs wrapper) in a
//                 1280-wide frame under a 40px stand-in for Steam's header.
//   ?qam=<id>     the Quick Access Menu (src/qam/QamRoot.tsx) in a 310px panel with that
//                 section selected (localStorage wfhelper.qamSection).
//   (none)        an index of links.
import { FC } from "react";
import { createRoot } from "react-dom/client";
import { staticClasses } from "@decky/ui";

import FullPage from "../src/FullPage";
import QamRoot from "../src/qam/QamRoot";
import { openTab } from "../src/nav";
import { PAGES } from "../src/registry";
import { startEvents } from "../src/rpc";

export const QAM_SECTIONS = ["world", "arbitrations", "relics", "market", "foundry", "missions", "inventory", "mastery", "syndicates", "rivens", "stats", "wiki", "settings"];

const SteamHeader: FC = () => {
  const now = new Date();
  return (
    <div className="pv-steamheader">
      <span style={{ fontWeight: 700, letterSpacing: "0.5px" }}>WFHELPER</span>
      <span style={{ marginLeft: "auto", display: "flex", gap: "14px", alignItems: "center" }}>
        <span>▮▮▮</span>
        <span>{now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}</span>
      </span>
    </div>
  );
};

const PageView: FC = () => (
  <div className="pv-screen" id="pv-frame">
    <SteamHeader />
    <FullPage />
  </div>
);

const QamView: FC = () => (
  <div className="pv-qam" id="pv-frame">
    <div className="pv-qam-title">
      <div className={staticClasses.Title}>WFHelper</div>
    </div>
    <div data-nav-root="1" data-nav-group="default">
      <QamRoot />
    </div>
  </div>
);

const IndexView: FC = () => (
  <div style={{ padding: "24px", color: "#dcdedf", fontFamily: "sans-serif", lineHeight: 1.8 }}>
    <h2>WFHelper preview</h2>
    <div>Pages: {PAGES.map((p) => <a key={p.id} href={`?page=${p.id}`} style={{ color: "#6eaaff", marginRight: "12px" }}>{p.id}</a>)}</div>
    <div>QAM: {QAM_SECTIONS.map((s) => <a key={s} href={`?qam=${s}`} style={{ color: "#6eaaff", marginRight: "12px" }}>{s}</a>)}</div>
    <p style={{ color: "#8b929a" }}>Keys: arrows = d-pad, Enter = A, Esc = B, Q/E = L1/R1.</p>
  </div>
);

export function mount(el: HTMLElement): void {
  const q = new URLSearchParams(location.search);
  const page = q.get("page");
  const qam = q.get("qam");
  startEvents();
  let view = <IndexView />;
  if (page) {
    if (!PAGES.some((p) => p.id === page)) console.error(`[preview] unknown page id ${page}`);
    openTab(page);
    document.body.dataset.mode = "page";
    view = <PageView />;
  } else if (qam) {
    try {
      localStorage.setItem("wfhelper.qamSection", qam);
    } catch {
      // storage blocked
    }
    document.body.dataset.mode = "qam";
    view = <QamView />;
  }
  createRoot(el).render(view);
}

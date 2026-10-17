import { Tabs } from "@decky/ui";
import { FC, useState } from "react";

import { PAGES } from "./registry";

// Remembers the last tab across openings of the page.
let lastTab = "dashboard";

export function openTab(id: string): void {
  lastTab = id;
}

const FullPage: FC = () => {
  const [tab, setTab] = useState(lastTab);
  return (
    <div style={{ marginTop: "40px", height: "calc(100% - 40px)", background: "#0e121e" }}>
      <Tabs
        activeTab={tab}
        onShowTab={(id: string) => {
          lastTab = id;
          setTab(id);
        }}
        tabs={PAGES.map((p) => ({ id: p.id, title: p.title, content: <p.component /> }))}
      />
    </div>
  );
};

export default FullPage;

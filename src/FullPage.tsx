import { Tabs } from "@decky/ui";
import { FC, useEffect, useState } from "react";

import { PageBoundary } from "./components/ui";
import { getLastTab, openTab, registerTabSetter } from "./nav";
import { PAGES } from "./registry";

const FullPage: FC = () => {
  const [tab, setTab] = useState(getLastTab());
  useEffect(() => {
    registerTabSetter(setTab);
    return () => registerTabSetter(null);
  }, []);
  return (
    <div style={{ marginTop: "40px", height: "calc(100% - 40px)", background: "#0e121e" }}>
      <Tabs
        activeTab={tab}
        onShowTab={(id: string) => {
          openTab(id);
          setTab(id);
        }}
        tabs={PAGES.map((p) => ({
          id: p.id,
          title: p.title,
          content: (
            <PageBoundary key={p.id} id={p.id}>
              <p.component />
            </PageBoundary>
          ),
        }))}
      />
    </div>
  );
};

export default FullPage;

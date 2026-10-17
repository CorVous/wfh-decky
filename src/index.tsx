import { definePlugin, routerHook, toaster } from "@decky/api";
import { staticClasses } from "@decky/ui";
import { GiCrystalGrowth } from "react-icons/gi";

import FullPage from "./FullPage";
import QamRoot from "./qam/QamRoot";
import { ROUTE } from "./registry";
import { onEvent, startEvents, stopEvents } from "./rpc";

export default definePlugin(() => {
  startEvents();
  const offToast = onEvent("toast", (d: { title: string; body?: string }) => {
    toaster.toast({ title: d.title, body: d.body ?? "" });
  });
  routerHook.addRoute(ROUTE, FullPage, { exact: true });

  return {
    name: "WFHelper",
    titleView: <div className={staticClasses.Title}>WFHelper</div>,
    content: <QamRoot />,
    icon: <GiCrystalGrowth />,
    onDismount() {
      offToast();
      stopEvents();
      routerHook.removeRoute(ROUTE);
    },
  };
});

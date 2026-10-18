// WFHelper's own entry points outside Decky's tab: an icon in the Quick Access
// bar and an item in the Steam main menu. Neither has a Decky API, so both
// patch Steam's UI the way Decky (tabs-hook.tsx) and DeckWebBrowser/Deckcord do.
// Every step is wrapped so a Steam update that breaks a patch only removes the
// shortcut; the plugin's normal Decky entry keeps working.
import { ErrorBoundary, Patch, afterPatch, createReactTreePatcher, findInReactTree, findModuleByExport, getReactRoot } from "@decky/ui";
import { FC, ReactElement, useEffect, useState } from "react";

const TAG = "[WFHelper] shortcuts:";
// Clear of Steam's tabs (0-7) and Decky's (999).
const QAM_KEY = 0x5746;
const MENU_KEY = "wfhelper-menu";

// ---- QAM visibility -----------------------------------------------------------------
// Tabs stay mounted while the QAM is closed. The panel only renders while it
// is open so its polling stops with it.
let qamVisible = false;
const visibleListeners = new Set<(v: boolean) => void>();

function setQamVisible(v: boolean): void {
  if (v === qamVisible) return;
  qamVisible = v;
  // Called from inside Steam's render; notify after it.
  setTimeout(() => visibleListeners.forEach((f) => f(v)), 0);
}

const VisibleGate: FC<{ children: ReactElement }> = ({ children }) => {
  const [v, setV] = useState(qamVisible);
  useEffect(() => {
    visibleListeners.add(setV);
    setV(qamVisible);
    return () => {
      visibleListeners.delete(setV);
    };
  }, []);
  return v ? children : null;
};

// ---- Quick Access bar icon ------------------------------------------------------------

function installQamTab(title: string, icon: ReactElement, panel: ReactElement): (() => void) | null {
  const named = (n: string) => (e: any) => e?.type?.toString?.()?.includes(n);
  const mod = findModuleByExport(named("QuickAccessMenuBrowserView"));
  if (!mod) return null;
  const renderers = Object.values(mod).filter((e) => named("QuickAccessMenuBrowserView")(e) || named("QuickAccessMenuEmbedded")(e));
  if (renderers.length === 0) return null;

  let enabled = true;
  // Built once: a fresh panel element each render would remount it.
  const entry = {
    key: QAM_KEY,
    title,
    tab: icon,
    panel: (
      <ErrorBoundary>
        <VisibleGate>{panel}</VisibleGate>
      </ErrorBoundary>
    ),
  };
  const handler = createReactTreePatcher(
    [(tree: any) => findInReactTree(tree, (n: any) => n?.props?.onFocusNavDeactivated)],
    (args: any[], ret: any) => {
      try {
        const tabs: any[] | undefined = findInReactTree(ret, (x: any) => Array.isArray(x?.props?.tabs))?.props?.tabs;
        if (!tabs) return ret;
        const at = tabs.findIndex((t) => t?.key === QAM_KEY);
        if (!enabled) {
          if (at >= 0) tabs.splice(at, 1);
          return ret;
        }
        setQamVisible(!!args?.[0]?.visible);
        if (at < 0) {
          // Sit just before Decky's plug. Never mark it `decky`: Decky
          // counts those to decide whether to re-add its own tabs.
          const decky = tabs.findIndex((t) => t?.decky);
          tabs.splice(decky < 0 ? tabs.length : decky, 0, entry);
        }
      } catch (e) {
        console.warn(TAG, "QAM tab render failed", e);
      }
      return ret;
    },
    "WFHelperQamTab",
  );
  const patches: Patch[] = renderers.map((r) => afterPatch(r, "type", handler));

  // A QAM that is already mounted cached the unpatched type on its fiber.
  try {
    const root = getReactRoot(document.getElementById("root") as any);
    const node = root && findInReactTree(root, (n: any) => renderers.includes(n?.elementType));
    if (node?.elementType?.type) {
      node.type = node.elementType.type;
      if (node.alternate) node.alternate.type = node.type;
    }
  } catch (e) {
    console.warn(TAG, "could not adopt the mounted QAM", e);
  }

  return () => {
    enabled = false;
    patches.forEach((p) => p.unpatch());
  };
}

// ---- Steam main menu item ------------------------------------------------------------

function installMainMenuItem(route: string, label: string, icon: ReactElement): (() => void) | null {
  const root = getReactRoot(document.getElementById("root") as any);
  const node = root && findInReactTree(root, (n: any) => n?.memoizedProps?.navID === "MainNavMenuContainer");
  const parent = node?.return;
  // Only a plain function component can be wrapped by calling it.
  if (typeof parent?.type !== "function") return null;

  const orig = parent.type;
  let enabled = true;
  let inner: any;
  const isItem = (e: any) => e?.props?.label && e.props.onFocus && e.props.route;
  const wrapper = (props: any) => {
    const ret = orig(props);
    try {
      const slot = ret?.props?.children?.props?.children?.[0];
      if (!slot?.type) return ret;
      if (inner) {
        slot.type = inner;
        return ret;
      }
      afterPatch(slot, "type", (_a: any[], r: any) => {
        try {
          const items: any[] | undefined = findInReactTree(r, (n: any) => Array.isArray(n) && n.some(isItem));
          if (!items) return r;
          const at = items.findIndex((e) => e?.key === MENU_KEY);
          if (!enabled) {
            if (at >= 0) items.splice(at, 1);
            return r;
          }
          if (at >= 0) return r;
          const ref = items.find(isItem);
          const Item = ref.type;
          const extra = ref.props.icon !== undefined ? { icon } : { children: icon };
          // After the last Steam entry that has a route (Library, Store, ...).
          const last = items.map(isItem).lastIndexOf(true);
          items.splice(last + 1, 0, <Item key={MENU_KEY} route={route} label={label} onFocus={ref.props.onFocus} {...extra} />);
        } catch (e) {
          console.warn(TAG, "main menu render failed", e);
        }
        return r;
      });
      inner = slot.type;
    } catch (e) {
      console.warn(TAG, "main menu patch failed", e);
    }
    return ret;
  };
  parent.type = wrapper;
  if (parent.alternate) parent.alternate.type = wrapper;

  return () => {
    enabled = false;
    parent.type = orig;
    if (parent.alternate) parent.alternate.type = orig;
  };
}

// ---- install with retries -------------------------------------------------------------

export function installShortcuts(opts: { title: string; route: string; icon: ReactElement; panel: ReactElement }): () => void {
  let removeQam: (() => void) | null = null;
  let removeMenu: (() => void) | null = null;
  let tries = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const attempt = () => {
    tries++;
    if (!removeQam) {
      try {
        removeQam = installQamTab(opts.title, opts.icon, opts.panel);
      } catch (e) {
        console.warn(TAG, "QAM tab install failed", e);
      }
    }
    if (!removeMenu) {
      try {
        removeMenu = installMainMenuItem(opts.route, opts.title, opts.icon);
      } catch (e) {
        console.warn(TAG, "main menu install failed", e);
      }
    }
    // Steam's UI may still be loading right after boot.
    if ((!removeQam || !removeMenu) && tries < 20) timer = setTimeout(attempt, 1000);
    else if (!removeQam || !removeMenu) console.warn(TAG, "gave up:", { qam: !!removeQam, menu: !!removeMenu });
  };
  attempt();
  return () => {
    if (timer) clearTimeout(timer);
    removeQam?.();
    removeMenu?.();
  };
}

// Navigation state shared by the QAM and pages. Kept out of FullPage and
// registry so pages can link to each other without import cycles.
import { Navigation } from "@decky/ui";

export const ROUTE = "/wfhelper";

let lastTab = "dashboard";
let setter: ((id: string) => void) | null = null;

export function getLastTab(): string {
  return lastTab;
}

// FullPage registers its tab setter while mounted so links switch tabs live.
export function registerTabSetter(fn: ((id: string) => void) | null): void {
  setter = fn;
}

export function openTab(id: string): void {
  lastTab = id;
  setter?.(id);
}

// Closes the side menus and shows the full-screen page on the given tab.
export function openPage(id: string): void {
  openTab(id);
  Navigation.CloseSideMenus();
  Navigation.Navigate(ROUTE);
}

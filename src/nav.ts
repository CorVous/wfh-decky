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

// Shows the full-screen page on the given tab, then closes the side menus.
// Navigate first, like Decky's own settings/store buttons: closing the QAM
// while a game runs hands the controller back to the game, so a page opened
// after that is shown but never gets d-pad input.
export function openPage(id: string): void {
  openTab(id);
  Navigation.Navigate(ROUTE);
  Navigation.CloseSideMenus();
}

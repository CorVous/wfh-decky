// Full-screen pages, opened from the Quick Access Menu. Each page lives in its
// own file under pages/ and may also export a compact `Qam` section.
import { FC } from "react";
import { FaBoxOpen, FaChartLine, FaCog, FaCubes, FaGem, FaGlobe, FaHammer, FaBook, FaShieldAlt, FaStar, FaStore, FaTachometerAlt, FaUsers, FaFlagCheckered } from "react-icons/fa";

import Dashboard from "./pages/Dashboard";
import World from "./pages/World";
import Arbitrations from "./pages/Arbitrations";
import Market from "./pages/Market";
import Inventory from "./pages/Inventory";
import Relics from "./pages/Relics";
import Foundry from "./pages/Foundry";
import Mastery from "./pages/Mastery";
import Rivens from "./pages/Rivens";
import Stats from "./pages/Stats";
import Wiki from "./pages/Wiki";
import Settings from "./pages/Settings";
import Syndicates from "./pages/Syndicates";
import Missions from "./pages/Missions";

export interface PageDef {
  id: string;
  title: string;
  icon: FC;
  component: FC;
}

export const PAGES: PageDef[] = [
  { id: "dashboard", title: "Dashboard", icon: FaTachometerAlt, component: Dashboard },
  { id: "world", title: "World", icon: FaGlobe, component: World },
  { id: "arbitrations", title: "Arbitrations", icon: FaShieldAlt, component: Arbitrations },
  { id: "market", title: "Market", icon: FaStore, component: Market },
  { id: "inventory", title: "Inventory", icon: FaBoxOpen, component: Inventory },
  { id: "relics", title: "Relics", icon: FaCubes, component: Relics },
  { id: "missions", title: "Missions", icon: FaFlagCheckered, component: Missions },
  { id: "foundry", title: "Foundry", icon: FaHammer, component: Foundry },
  { id: "mastery", title: "Mastery", icon: FaStar, component: Mastery },
  { id: "syndicates", title: "Syndicates", icon: FaUsers, component: Syndicates },
  { id: "rivens", title: "Rivens", icon: FaGem, component: Rivens },
  { id: "stats", title: "Stats", icon: FaChartLine, component: Stats },
  { id: "wiki", title: "Wiki", icon: FaBook, component: Wiki },
  { id: "settings", title: "Settings", icon: FaCog, component: Settings },
];

export const ROUTE = "/wfhelper";

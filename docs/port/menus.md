# Port spec: app menus and views (Quick Access Menu + full-screen routes)

Source: WFHelper (MIT), `/home/claude/wfhelper-src`. All `file:line` references are into that tree.
Target: Decky plugin on Steam Deck LCD (1280x800, game mode, gamescope). Menus are React
(`@decky/ui`) in the Quick Access Menu (QAM, ~300 px wide content column, D-pad/stick focus
navigation, A = activate, B = back) plus full-screen routes (`routerHook.addRoute`, 1280x800
minus Steam header, which leaves about 1280x740). Python backend (root) does all data work and
returns plain JSON to the frontend through `callable()`.

Sibling specs this file relies on (do not duplicate):
- `data-sources.md`: warframe.market (WFM) endpoints, auth, rate limits; `api.wfhelper.com`
  snapshot; world state fetch/merge; drop tables; item database.
- `relic-planner.md`, `relic-rewards.md`: relic data model, EV math used by overlays, EE.log
  triggers, EE.cfg interface-scale parsing.
- `riven-scanner.md`: riven decode and grading (`DecodedRiven`, grades).
- `arbitration.md`: arbitration and Profit-Taker run parsing, `ArbiRunRecord`, arbys.txt schedule.
- `game-integration-linux.md`: EE.log path, memory inventory read on Linux, root vs `deck` home.

This spec covers: what each view shows, how its rows are filtered and sorted, which pure
functions compute them (port these to Python or keep in TS), a QAM layout, a full-screen layout,
a value ranking for game mode, and the Settings that change overlays.

---

## 0. Architecture of the WFHelper renderer (what to replace)

| WFHelper piece | Where | Port target |
|---|---|---|
| View registry, 16 routable ids | `src/types/views.ts:4-21` (`VIEW_NAMES`), `src/lib/viewRegistry.ts:15-31` (lazy loaders), `:41-58` (labels) | One QAM panel with a view picker + one full-screen route per view (`/wfh/<view>`) |
| Svelte stores (reactive derived data) | `src/stores/*.ts` | Backend computes view-models; frontend holds only UI state (tab, sort, search) |
| Persistence | `localStorage` via `src/lib/persistence.ts` (`readStorage`, `persistedString`, `persistedBoolean`, `persistedStringList`) | Backend JSON file `~deck/homebrew/settings/wfhelper/ui-state.json` (Decky `DECKY_PLUGIN_SETTINGS_DIR`), keys kept identical so behaviour matches |
| Main-process data files | Electron `app.getPath("userData")` = `~/.config/WFHelper/` on Linux (`services/userDataPath.ts:3-5`) | `DECKY_PLUGIN_RUNTIME_DIR` / settings dir. NOTE backend runs as root, so never use `os.homedir()`; use `/home/deck` or `DECKY_USER_HOME` |
| IPC (`invoke("name", ...)`) | `src/lib/ipc.ts`, handlers in `ipc/*.ts` | `callable("name")` Python methods with the same names and payload shapes |
| Layout editor (drag sections, spans, popouts) | `src/lib/layout/*`, `src/stores/layout.ts`, `EditLayoutBar.svelte`, `LayoutGrid.svelte` | Drop. Fixed layouts on the Deck. Keep only "hidden sections" as Settings toggles |
| Theme/custom CSS/sidebar tabs/workspaces | `src/stores/theme.ts`, `src/lib/customCss/*`, `src/stores/workspaces.ts`, `sidebarTabs.ts` | Drop, except overlay opacity (section 17) |

View ids in default order (`src/types/views.ts:4-21`): `setup, dashboard, inventory, foundry,
mastery, stats, world, syndicates, market, analytics, relics, wiki, rivens, arbi, missions,
settings`. "Resources" is not a view: it is the `resources` tab of Inventory
(`src/views/ResourcesView.svelte` is a child component). "Arbitrations" is two things: the
`arbis` tab of World (schedule) and the `arbi` view ("Run analysis", `ArbiAnalyzeView.svelte`).
"MarketAnalysis" is view id `analytics`.

### 0.1 Core shared data every view reads

| Name | Type | Produced by | Notes |
|---|---|---|---|
| `inventoryData` | raw DE inventory JSON (`RawInventoryData`) | game memory read or helper/import (`services/gameMemoryInventory.ts:291-318`, Linux branch `scanGameMemoryLinux`; `services/apiHelperRunner.ts:252` writes `inventory.json`) | see `game-integration-linux.md`. Source enum `"helper" | "manual" | "aleca" | "none"` (`config/shared/inventorySource.ts:2`) |
| `inventoryModifiedAt` | epoch ms | mtime of the inventory file | Dailies auto-detect uses it (section 10.4) |
| `itemDb` | `Record<uniqueName, ItemDbEntry>` | `services/itemDatabase.ts` (DE PublicExport, WFCD fallback; images rewritten to `https://assets.wfhelper.com`) | fields: `src/types/inventory.ts:44-75` |
| `wfmItems` | WFM catalog keyed by normalized name / gameRef | `services/wfmCatalog.ts` | `url_name`, `item_name`, `gameRef`, `thumb`, `icon`, `maxRank` |
| `relicDb` | `RelicDatabase` (`src/types/relics.ts`) | `invoke("getRelicDatabase")` | see `relic-planner.md` §5 |
| `masteryData` | `MasteryData {items, stats}` (`src/types/inventory.ts:276-279`) | `services/masteryHelper.ts:919` `computeMasteryProgress(inventory)` | section 5.1 |
| `worldData` | `WorldState` (`src/types/world.ts:218-239`) | `invoke("getWorldState")` | see `data-sources.md` §3 |
| prices | median per `slug` (+rank) | price cache, warmed from `api.wfhelper.com/v1/snapshot` (fresh 2 h, `src/lib/wfm/snapshotLoader.ts:17`) then WFM `/v1/items/{slug}/statistics` | basis `closed-volume-average-48h-v1` (`config/shared/wfmStats.ts:4`). Price = volume-weighted mean of 48 h closed `wa_price` sell rows, excluding rows > 10x a reference price (live sell-book median, else 90-day closed median), rounded (`config/shared/wfmStats.ts:69-115`) |

Derived stores in `src/stores/data.ts` (port as backend functions, cache by input identity, they
cost ~1 s on large accounts):

- `usableInventory` (`data.ts:19-25`): if the preference `hideFoundryClaims` (default `true`,
  `src/stores/preferences.ts:13`) is on, `withoutFoundryPending(inv, isReusableBlueprint)` removes
  blueprints already handed to the foundry (`config/shared/foundryPending.ts`).
- `componentOwnership` (`data.ts:28-31`): `aggregateComponentOwnership(inv)` -> `Map<uniqueName, count>`
  (stacked items plus built gear) (`config/shared/componentOwnership.ts`).
- `parsedItems` (`data.ts:58-71`): `parseInventory(inv, itemDb, marketGameRefs)` (`src/lib/inventory.ts:46`),
  where `marketGameRefs` = set of `gameRefKey(item.gameRef)` over the WFM catalog (`data.ts:39-49`).
- `foundryData` (`data.ts:79-89`): `parseFoundry(inv, itemDb)` (`src/lib/inventory/foundryResources.ts:148`).
- `relicOwnedCounts` (`src/stores/relics.ts:24-27`): `parseOwnedRelics(inv, relicDb)`
  (`src/lib/relic/relicInventory.ts:60`).

`parseInventory` summary (`src/lib/inventory.ts:46-310`): iterate the equipment collections
(`CATEGORIES`, `src/lib/inventory/itemClassification.ts:39`), modular collections, and
`SUPPLEMENTAL_COLLECTIONS` (`itemClassification.ts:57-65`, verbatim):

```
{ key: "MiscItems", cat: "misc", label: "Misc" },
{ key: "FusionTreasures", cat: "misc", label: "Misc" },
{ key: "Recipes", cat: "misc", label: "Recipe" },
{ key: "LevelKeys", cat: "relics", label: "Relic" },
{ key: "RawUpgrades", cat: "misc", label: "Misc" },
{ key: "Upgrades", cat: "mods", label: "Mod" },
{ key: "Arcanes", cat: "arcanes", label: "Arcane" },
```

Each entry gets `inventoryGroup` from `deriveGroup` (`itemClassification.ts:496-537`: equipment
collections -> `equipment`; `LevelKeys` -> `relics`/`misc`; `Arcanes` -> `arcanes`; `Upgrades`/
`RawUpgrades` -> `arcanes`/`mods`/`misc`; then category/type words; build parts -> `all_parts`;
else `misc`). Mods and arcanes are keyed per rank: `${uniqueName}#r${rank}m${maxRank}`
(`inventory.ts:57-67`). A recipe path (`/Types/Recipes/`) not listed on WFM is demoted to `misc`
(`inventory.ts:91-95`). Group union: `all_parts | relics | mods | arcanes | full_sets |
incomplete_sets | equipment | misc` (`src/types/inventory.ts:129-137`). Full sets are synthesised
by `buildFullSetItems` (`src/lib/inventory/fullSets.ts`).

---

## 1. Value ranking for a player in game mode

Ranked by how often a Deck player in Warframe would open it mid-session, and whether it works
with gamepad only (no typing).

| Rank | View | Why | QAM? |
|---|---|---|---|
| 1 | World (fissures, cycles, resets, Baro, Circuit, arbitration schedule) | Answers "what should I run now" between missions; needs no inventory; refreshes itself | Yes, primary |
| 2 | Relics (owned-relic planner, squad EV, needed rewards, push filters to overlay) | Directly drives fissure runs and the relic overlays | Yes |
| 3 | Dashboard (cycles, fissures, foundry ready, last mission, alerts) | Best QAM home page: one glance summary | Yes, home |
| 4 | Missions (last mission rewards with platinum/ducat value) | Instant feedback after each mission; memory read, root OK | Yes (last mission only) |
| 5 | Foundry (claimable now, ready to build, pinned totals) | Short list, actionable at the orbiter | Yes |
| 6 | Market (my orders, close one trade, presence) | Trades happen in game; "sold 1" after a trade is one button | QAM: orders + presence; route: everything |
| 7 | Syndicates (rank, standing to next, today's remaining pool) | Daily checklist | Yes (compact) |
| 8 | Mastery (MR progress, "easy mastery" roadmap, pinned goals) | Planning; long lists | Route; QAM shows MR + top 5 easy |
| 9 | Inventory (value strip, search item price, sets) | Mostly a trading-prep tool; search needs keyboard | Route; QAM shows value totals |
| 10 | Wiki / drop search | "Where does X drop" mid-farm; needs on-screen keyboard | Route |
| 11 | Arbitrations run analysis (+ Profit-Taker) | Post-run review; charts | Route; QAM shows last run line |
| 12 | Rivens (owned rivens, grades, finder) | Desk activity | Route |
| 13 | Stats (daily resource deltas, charts) | Low in-session value | Route; QAM shows "today" deltas |
| 14 | Market analysis (trade ledger, FIFO margin) | Desk activity | Route only |
| 15 | Settings | Required, not "value" | QAM: overlay toggles; route: all |

---

## 2. Dashboard (`src/views/DashboardView.svelte`, 70 lines)

What it shows: a grid of widgets. Two clocks are owned by the view and passed down: `now` (1 s)
and `coarse` (`COARSE_CLOCK_MS = 5_000`, `src/lib/world/useWorldView.ts:29`)
(`DashboardView.svelte:29-36`). World polling is mounted refcounted (`mountWorldPolling`,
`useWorldView.ts:96-112`: fetch now, then every `WORLD_POLL_MS = 30_000`; a fetch is skipped when
the last one is younger than `WORLD_REFRESH_MS = 120_000` unless forced, `useWorldView.ts:27-28,69-90`).

Widget registry, verbatim ids and settings (`src/lib/widgets/registry.ts:12-94`):

| id | label | default span | settings | home view |
|---|---|---|---|---|
| `widget.cycles` | Planet cycles | 1 | none | world |
| `widget.fissures` | Void fissures | 1 | `limit:number`, `lith, meso, neo, axi, requiem, omnia: boolean` | world |
| `widget.foundryReady` | Foundry ready | 1 | `limit` | foundry |
| `widget.marketAlerts` | Market alerts | 1 | `limit` | market |
| `widget.goals` | Pinned goals (mastery planner) | 1 | `limit` | mastery |
| `widget.baro` | Baro Ki'Teer | 1 | none | world |
| `widget.inventoryValue` | Inventory value | 1 | `allTradables:boolean` | inventory |
| `widget.tradeSummary` | Trade summary | 1 | none | analytics |
| `widget.lastMission` | Last mission | 1 | none | missions (hidden in layouts saved before it existed) |
| `widget.recentRuns` | Arbitration runs | `full` | `limit` | arbi |

Defaults and ranges (`registry.ts:124-137`): `limit: 5` (range 1..20, rounded and clamped),
`allTradables: false`, all tier booleans `true`. Persisted as `wf_dashboard_v1`
(`src/lib/widgets/types.ts:27`), `{version:1, widgets:[{id, span, hidden, settings}]}`; loading
validates every setting by type and drops wrong types instead of coercing (`src/stores/dashboard.ts:48-112`).
String settings longer than 120 chars are rejected (`dashboard.ts:24`).

Per-widget data (all in `src/components/widgets/*.svelte`):

- Cycles: `buildWorldTimes` + `buildCycleRows` (section 10.2), same as World.
- Fissures: `buildFissureRows(wd.fissures, worldFissureMode, now, coarse)` (section 10.2), then
  drop rows whose tier class setting is `false` (`settingBoolean(settings, row.tierCls, true)`), take
  `limit`. Shows the current fissure mode label (`all|normal|steel|railjack`, persisted key
  `wf_fissure_mode`, default `normal`, `src/stores/world.ts:8-12`).
- Foundry ready: `foundryReadyRows(foundry, owned, chainBuildable, coarse)`
  (`src/lib/widgets/rows.ts:28-48`): finished builds first (`claimable: true`, key
  `building:<productUniqueName|name>`), then recipes where `isFoundryRecipeReady` (key
  `recipe:<uniqueName|name>`); repeated keys get `#2`, `#3` suffixes (`withRowKeys`, `rows.ts:13-25`).
- Market alerts: `invoke("marketAlertsGetHits")` + `invoke("marketAlertsStatus")`, refresh on
  event `market-alerts:changed`. Health label: not running -> "engine stopped";
  `scheduler.state == "backoff"` / `"degraded"` -> those; else OK. Each hit: time HH:MM, title,
  platinum.
- Goals: `buildMasteryPlan(pins, itemDb, ownership)` (section 5.4), rows sorted by
  `masteryXpRemaining` desc, take `limit`.
- Baro: `voidTrader`; active iff `activation <= now < expiry` (`activeWindow`, `src/lib/format.ts:85-93`).
  Shows countdown to expiry when active, else to activation; first 4 manifest items.
- Inventory value: `computeInventoryValueTotals` over `all_parts` (prime scope) or `everything`
  (tradable scope) (section 3.3).
- Trade summary: ledger events from the 1st of the current local month (cap 2000), `computeFlow`
  (section 15).
- Last mission: `invoke` of mission-rewards payload (recent 10 summaries), newest selected by
  default; rows by `buildRewardRows` (section 7).
- Recent runs: `arbiRuns` sorted `startedAt` desc, take `limit`; date as "Mon D".

### QAM layout (home page)
```
[ View picker  v ]  (DropdownItem: Dashboard/World/Relics/...)  [Open full screen >]
-- Cycles ----------------------------------
 Cetus   Night  12m 03s        (urgent = amber when <20% remains)
 Vallis  Warm    3m 40s
 Cambion Vome   44m
 Duviri  Joy    1h 10m
 Earth   Day    2h 01m
-- Fissures (Normal) [mode v] ------------
 Lith  Capture  Hepit (Void)   21m
 ... (limit 5)
-- Foundry ready -------------------------
 * Ember Prime          CLAIM
   Forma                BUILD
-- Last mission ---------------------------
 Hepit (Void)  14:32  +38p  +45d  +12k cr
-- Alerts (2 new) ------------------------
```
Each block is a `PanelSection`; rows are `Focusable` so D-pad scrolls. Selecting a row opens the
matching full-screen view at that item.

### Full-screen route
Two-column grid (each 620 px): left Cycles, Fissures, Baro; right Foundry ready, Last mission,
Alerts, Goals; bottom full-width Recent runs. No layout editor; per-widget settings
(limit, tier toggles, allTradables) in a gear `ModalRoot`.

---

## 3. Inventory (`src/views/InventoryView.svelte`, 931 lines)

### 3.1 Tabs (`src/lib/inventoryMarket.ts:98-109`, verbatim order)
`everything, all_parts, relics, mods, arcanes, full_sets, equipment, pets, resources, misc`.
Persisted `wf_inventory_tab`, default `all_parts` (`InventoryView.svelte:139,151-155`).

- Everything tab sources (chip row) `EVERYTHING_SOURCES = all_parts, relics, mods, arcanes,
  full_sets, equipment, resources, misc` (`inventoryMarket.ts:25-34`); default enabled = all except
  `full_sets` (`:36-38`). Persisted as the HIDDEN list, comma-joined, key
  `wf_inventory_everything_hidden_sources` (`InventoryView.svelte:142,161-173`).
- Full Sets tab: category chips from `EQUIPMENT_CATEGORY_ORDER` (`foundryResources.ts:38-49`:
  `Warframe, Primary, Secondary, Melee, Archwing, Companion, Appearance, Gear, Modular, Misc`)
  present in the tab, hidden list persisted `wf_inventory_full_sets_hidden_categories`. Category of a
  set = `classifyForFoundry(setRoot, setRoot, itemDb)`. Toggle "show incomplete sets" adds
  `incomplete_sets` rows and switches sort to `missing_parts` asc (`:268-274`).
- Pets tab: `parsePetGenetics(inventoryData)` (`src/lib/inventory/petGenetics.ts`) rendered by
  `PetsInventory`.
- Resources tab: `parseResources(inv, itemDb)` (`foundryResources.ts:258`), search + `minimumAmount`,
  sort `name` or `amount` (`InventoryView.svelte:663-679`). Card: image, name, `formatNumber(count)`.

### 3.2 Row model (port these three functions)
1. `buildBaseInventoryItems(parsedItems, tab, wfmLookup, orderedNames, orderedSlugs, relicDb, orderedSubtypes)`
   (`inventoryMarket.ts:346-500`). Per item: resolve group (fallback by category label,
   `:191-200`); relic rows renamed to `"<Group> Relic (<Quality>)"` and their WFM subtype = relic
   quality; slug resolution order: snapshot set slug -> catalog by gameRef -> catalog by name (with
   name aliases, `lookupNameCandidates` `:111-167`: `X Blueprint` <-> `X`, `Zariman Ship` ->
   `Parallax`, `Prime Archwing` -> `Odonata Prime`, `Bane of` <-> `Cleanse`, `Orokin` <-> `Corrupted`,
   `Helmet` <-> `Neuroptics`, punctuation folding) -> generated slug (`toMarketSlug`; sets get
   `_set`). Ranked groups (mods/arcanes) never get a generated slug. Modular builds and excluded
   ranked items get `marketSlug = null`. `rank` clamped to `maxRank` (fallback cap mods 10, arcanes
   5, else 30). `orderPlaced` true when any of my WFM orders matches by normalized name or slug and
   (for ranked items) rank matches or the order is rankless, and (for relics) subtype matches.
2. `buildOrderLookups(myOrders)` (`:311-344`): maps name/slug -> list of order ranks (`-1` rankless)
   and subtypes.
3. `buildInventoryViewItems(base, metricsByKey)` (`:502-589`): attaches `platinum`, `platinumR0`,
   `platinumRmax`, `wtsR0/wtbR0/wtsRmax/wtbRmax` (best sell/buy for rank 0 / max rank), `ducats`,
   `ducatonator = round2(ducats / platinum)` (only when platinum > 0), `displayImageUrl`,
   `equippedSummary = "Equipped in A, B +"` (first 2 contexts). For ranked items the price used is
   the max-rank price when the item is at max rank, else rank 0.

Metric hydration (`src/stores/inventoryHydration.ts`, `InventoryView.svelte:130-133,226-237`):
first `METRIC_VISIBLE_PREFETCH_LIMIT = 42` filtered rows fetch price (+ducats, + order book for
ranked) with network; the next `METRIC_BACKGROUND_PREFETCH_LIMIT = 210` fetch price only from
cache. A "ranked hotset" refresh re-fetches up to `HOTSET_REFRESH_LIMIT = 12` recently viewed
ranked items `HOTSET_REFRESH_DELAY_MS = 4_000` after the list settles. On the Deck: do the
prefetch in the backend for the whole tab from the snapshot cache and only hit WFM for the item
the user selects (rate limits are in `data-sources.md` §1.3).

### 3.3 Value strip (`src/lib/inventory/valueTotals.ts`, port verbatim)
- Scope `prime` (default) counts only `inventoryGroup == "all_parts"` and prime rows; `tradable`
  counts every sellable row (`isCountedForValue`, `:66-71`). Sellable = `tradable !== false`, not
  `full_sets`/`incomplete_sets` (would double count), has a `marketSlug`, slug not WFM-excluded
  (`:45-51`).
- Per-unit platinum floor presets `VALUE_MIN_PLATINUM_PRESETS = [0, 5, 10, 15]` (`:35`); rows with
  a known median below the floor are dropped entirely (ducats too); unpriced rows stay counted.
- `stackSize`: `amount == null` -> 1; `<= 0` -> 0; else `max(1, floor(amount))`.
- Totals: `platinum += median * qty` (else `platinumUnpriced++`), prime rows `ducats += ducats * qty`
  (else `ducatsUnpriced++`); `unpriced` counts rows with either hole; result rounded (`:84-134`).
- Which rows to show (`selectValueStripRows`, `:140-147`): if the visible list counted 0 rows show
  only "inventory" (if >0); if visible >= whole show only "in view"; else both.
- Persisted: `wf_inventory_value_all_tradables` (false), `wf_inventory_value_min_plat` (0)
  (`src/stores/preferences.ts:26-34`).

### 3.4 Filters and sorting (shared engine, port verbatim: `src/lib/filters.ts`)
`SharedFiltersState` (`src/types/filters.ts`): `search, primeMode(all|prime|non_prime),
masteredMode(all|mastered|not_mastered), sortBy, sortDirection, orderPlaced, mastered, spares,
vaulted, favorite, equipped, leveledUp, subsumed (all|yes|no), partType(all|normal|prime),
minimumPlatinum(number), minimumAmount(0|2), foundryState(all|claimable|not_ready|buildable|buildable_sets)`.
Defaults: all `"all"`, `sortBy "name" asc`, foundry scope `sortBy "count" desc`
(`src/stores/filters.ts:4-38`). Not persisted (session only).

- `matchesSearch` (`filters.ts:117-130`): lower-case substring on `name`, `displayName`,
  `category`, `categoryLabel`, `internalName`, then any `keywords[]`.
- `matchesSharedFilters` (`:214-273`): exact semantics, including "strict tri-state" for
  `mastered` and `subsumed` (rows lacking the flag drop out when the filter is active),
  `minimumAmount` reads `combinedAmount ?? amount`, `not_ready` drops claimable and buildable,
  `buildable_sets` requires buildable and not `looseComponent`.
- `compareSharedFilterSort` (`:283-299`): `name` uses a shared `Intl.Collator`; numeric keys put
  nulls last regardless of direction (`compareNullableNumber`, `:202-212`), ties by name. Metrics:
  direct fields `platinum, ducats, amount, count, time, disposition, rerolls`; `grade` (S=6 A=5 B=4
  C=3 D=2 F=1 or `gradeRank`), `attr_grade` (`attrGradeRank`), `owned`, `ducatonator`
  (field or ducats/platinum), `complete_sets`, `missing_parts`, `parts_owned`, `mastery_xp`.
- Default direction: desc for `platinum, ducats, amount, count, owned, disposition, rerolls,
  grade, attr_grade, ducatonator, complete_sets, mastery_xp, parts_owned, ev, ducat`; else asc
  (`defaultSortDirection`, `:13-33`).
- Sort options per tab (`InventoryView.svelte:177-199`): `all_parts`/`full_sets`: name, platinum,
  ducats, amount, ducatonator, complete_sets, missing_parts; `resources`: name, amount; every other
  tab: name, platinum, amount.
- Inventory advanced controls (`FILTER_CONTROL_SUPPORT.inventory`, `filters.ts:395-408`): search,
  sort, orderPlaced, mastered, spares, vaultedChips, partType, favorite, minPlatinum, minAmount,
  equipped, leveledUp.

"Spares"/safe-to-sell comes from `src/lib/inventory/safetyRules.ts` (`safeToList`, `:580`; rule ids
`lock, built, equipped, lastCopy, spare, pinnedGoal, unmasteredRecipe, setKeep`, highest floor
wins, `:15-28`). Port only if bulk sell is ported (not recommended for v1).

### 3.5 Selection / bulk sell / order book
Selection mode, saved selections and Bulk Sell (`src/components/workbench/BulkSellModal.svelte`,
1109 lines, `src/lib/tradeWorkbench/*`) automate WFM listing. Skip in v1 (high risk, typing).
Order book side panel (`InventoryOrderBookPanel.svelte`) shows top WTS/WTB for the selected item
(WFM `/v2/orders/item/{slug}`, `data-sources.md`).

### QAM layout
```
Inventory value  [Prime parts | All tradables]
  In view   12,340p   3,210d   (14 unpriced)
Min price per unit: [0 5 10 15]  (SliderField with notches)
[Search item price...]  -> opens OSK, shows top 5 matches with median plat/ducats
[Open inventory >]
```
### Full-screen route
Header: tab strip (`Tabs` from @decky/ui; L1/R1 switch tabs). Left 840 px: virtualised list (row
64 px: image 48, name, rank, xN, median plat, ducats, d/p, badges M/O/V for
mastered/owned-parent/vaulted, "listed" dot). Right 400 px: details of the focused row (order book
top 5 sell/buy, drops). Y = sort menu, X = filter menu, search via OSK. Page in 120-row chunks
(`GRID_PAGE_SIZE = 120`, `InventoryView.svelte:635`).

---

## 4. Foundry (`src/views/FoundryView.svelte`, 671 lines)

Data: `foundryData = {building[], recipes[]}` from `parseFoundry` (`foundryResources.ts:148-256`):
- `building` from `inv.PendingRecipes[]`: product resolved via a reverse map
  `recipe.blueprintUniqueName -> productUniqueName` built from every itemDb entry; `endDate =
  CompletionDate` (Mongo `{$date:{$numberLong}}`), category, ingredients, buildPrice.
- `recipes` from `inv.Recipes[]`: `count = ItemCount ?? 1`, `isIngredient = product is used in
  another recipe and is not a componentOf`, `buildTime` (seconds). A blueprint with no product drops a
  trailing " Blueprint" from its name.

Status per row (`FoundryView.svelte:171-176`), sort rank (`:269-274`):
| status | condition | rank | colour |
|---|---|---|---|
| `claimable` | building and `endDate <= now` | 0 | accent |
| `in-progress` | building, not done | 1 | info; label = `formatTimeRemaining(endDate)` |
| `ready-to-build` | `isFoundryRecipeReady(recipe, owned, chainBuildable)` | 2 | success |
| `not-ready` | otherwise | 3 | muted |

`isFoundryRecipeReady` (`foundryResources.ts:358-369`): false if no ingredients; true if every
ingredient `owned >= count`; else true if the blueprint is in `chainBuildable`.
`chainBuildableBlueprints` (`:289-352`): for each recipe with count>0, ingredients>0, and product
not a `componentOf`, try to satisfy ingredients from one shared pool (copy of `owned`): take what is
owned, then for a deficit look up a recipe row that builds that ingredient; `builds = ceil(deficit /
(recipe.num || 1))`; unless the blueprint is reusable, need `builds` blueprint copies (from a shared
`blueprints` map seeded with that row's `count`); recurse, depth limit `CHAIN_MAX_DEPTH = 4`
(`:283`), cycle guard via stack.

Sort: pinned blueprints first, then status rank, then shared sort (`count` desc by default; options
`count`, `time` (ms remaining for building rows), `name`) (`FoundryView.svelte:281-298`).
Tabs (`:93-115`): `all`, `status:in-progress` (in-progress + claimable), and `cat:<Category>` for
each `EQUIPMENT_CATEGORY_ORDER` entry; persisted `foundryView.filter`. Shared filters for scope
`foundry`: search (also matches recipe material names, `collectRecipeMaterialNames`), prime,
mastery, foundryState, vaulted, subsumed, sort.

Pinned blueprints (`foundry.pinnedRecipes`, max 60, `src/stores/pinnedRecipes.ts:4`) produce a
summary (`computePinnedTotals`, `src/lib/pinnedSummary.ts:24-50`): one build per pinned blueprint,
sum credits and ingredients, `resources` sorted by needed desc, `missing = owned < needed`.

Card fields: image, name `xN` (blueprint count), status/countdown, "Used in crafting" badge when
`isIngredient`, ingredient grid (icon, `owned/needed`, red when short), mastery state (mastered / in
progress / not mastered / n/a via `inheritedMasteryStatus`), credits.

### QAM layout
```
Foundry  [Claimable 2] [Building 3] [Ready 5]
 CLAIM  Ember Prime
 CLAIM  Forma
 4h 12m Khora Prime Chassis
 BUILD  Lex Prime   (all parts owned)
-- Pinned (3) 75,000 cr --------------
 Missing: 2x Neurodes, 400 Plastids
```
### Full-screen
List with status sections (Claimable, Building, Ready to build, Missing parts), category tabs on
L1/R1, focus row shows ingredient grid on the right. A = pin/unpin.

---

## 5. Mastery (`src/views/MasteryView.svelte`, 1220 lines)

### 5.1 Data
`masteryData` from `services/masteryHelper.ts:919` `computeMasteryProgress(inventory)`. Key
constants (`masteryHelper.ts:163-170`): weapon affinity `500 * rank^2`, suit affinity
`1000 * rank^2`; item rank = `min(maxRank, floor(sqrt(xp / perRankSquared)))`; account mastery per
gear rank = affinityPerRankSquared/5 (weapons 100, suits 200); junction 1000, intrinsic rank 1500
(max 10). Items sold still count via `inv.XPInfo`. Founder items `Excalibur Prime, Lato Prime,
Skana Prime` can be hidden (`wf_hide_founder_mastery_items`, default false).
Account rank math (`config/shared/masteryXp.ts`, verbatim):
```
xp(rank) = 2500 * rank^2                (rank <= 30)
xp(rank) = 2_250_000 + (rank-30)*147_500 (legendary)
rank(xp) = xp >= 2_250_000 ? 30 + floor((xp-2_250_000)/147_500) : floor(sqrt(xp/2500))
```
Item: `{name, uniqueName, category, rank, maxRank, status: mastered|progress|missing,
masteryXpRemaining, currentlyOwned, needsGilding, components[], isPrime, vaulted, marketBuyable}`.
Stats: totals, `byCategory`, `profileMastery {rank, percentToNext, totalXp, xpIntoRank, xpForNext,
testReady}`, `completion {starChart{normal,junctions,steelPath,steelPathJunctions}, intrinsics{railjack,drifter}}`
each a `{done,total}`.

### 5.2 View tabs
`collection | roadmap | planned | codex | incarnon` (persisted `wf_mastery_view_tab`,
`MasteryView.svelte:150-156`). Category tabs: `CAT_ORDER = Warframes, Primary, Secondary, Melee,
Companions, Archwing, Amps, Necramech, Misc` then any extra categories alphabetically, plus an
"Incomplete sets" pseudo-tab inserted before Misc (`:120-130,609-615`). Status tabs `all | missing |
progress | mastered` (`wf_mastery_status_tab`). Sort options: name, owned, parts_owned, mastery_xp,
platinum (`:131-137`).

Summary strip (`buildMasterySummary`, `:262-321`): Mastered / In progress / Missing / Total, and an
MR row: `MR <rank>` with label "test ready" / "N XP to MR <rank+1>" (`remaining = xpForNext -
xpIntoRank`) / percent; subtext from `masteryProjectionSubtext` (`src/lib/masteryProjection.ts`):
banked rank = `max(rank, rankFromXp(totalXp))`; `readyXp` = sum of `masteryXpRemaining` of
unmastered items whose foundry build is claimable; projected = `rankFromXp(totalXp + readyXp)`.

Collection row hydration (`hydrateMasteryItems`, `:407-474`): progress pct
`floor(rank/maxRank*100)`; foundry status (claimable/in-progress by productUniqueName or lower-case
name); `subsumed` only for base Warframes (`isSubsumableFrame`: name does not end in prime/umbra/dex,
`src/lib/helminth.ts:20-22`; families from `InfestedFoundry.ConsumedSuits`); `partsOwned`;
`buildable` = not owned and every part owned or blueprint held; `platinum` = root set median;
`estimatedCost = estimateMasteryPurchaseCost(root, components, price)` (`src/lib/masteryRoadmap.ts:106-127`:
null when nothing missing or any missing part unpriced; else `min(rootPrice, sum(price*count))`).

### 5.3 Roadmap (`buildMasteryRoadmap`, `masteryRoadmap.ts:348-421`, port verbatim)
Skip mastered or `masteryXpRemaining <= 0`. Three lists:
- `easy`: access is the first of `gild` (needsGilding) > `owned` > `claimable` > `building` >
  `buildable` (foundryState buildable and not craftParts) > `foundryParts` (only missing parts are
  in the foundry) > `craftParts` (all parts owned or blueprint held, some blueprint) >
  `marketBlueprint` (marketBuyable and only "... Blueprint" rows missing). Sorted by
  `ACCESS_PRIORITY` (owned 0, gild 1, claimable 2, building 3, buildable 4, foundryParts 5,
  craftParts 6, marketBlueprint 7), then XP desc, then name.
- `relics`: probability that the player's owned relics complete the missing parts
  (`calculateOwnedRelicCompletion`, `:263-346`): pools = (count, rewards) per owned relic quality;
  per pool sum chances (as 0..1) per missing component (matched by uniqueName alias or normalized
  "<item> <part>" name; normalise if total > 1); all missing parts must be obtainable; then a
  capped joint distribution over states (mixed-radix counters, one reward per relic copy) gives
  P(all missing counts reached). Sorted by probability desc, XP desc, name.
- `platinum`: items with `estimatedCost > 0`, `xpPerPlatinum = masteryXpRemaining /
  estimatedCost`, sorted desc, then cost asc, then name.

### 5.4 Planned (pins)
Pins persisted `mastery.pinnedItems` (max 40), keep-after-mastered list
`mastery.pinnedKeepMastered` (`src/stores/masteryPins.ts:7,16,32`). Mastered pins are auto-unpinned
with an undo toast. `buildMasteryPlan(pins, itemDb, ownership)` (`src/lib/masteryPlanner.ts:233-280`):
walks each pin's crafting tree (`src/lib/craftingTree.ts`) with one shared budget so two pins
cannot claim the same owned part; per item: components (needed/owned/missing/state), raw
resources (needed/owned/missing), credits, `completeness = satisfied/topLevel`, `craftableNow`;
totals summed across pins. Display order (`sortPlannedItems`, `:311-325`): craftable first, then
by `mastery_xp | completeness | name` (persisted `wf_mastery_planner_sort`).

### 5.5 Codex and Incarnon tabs
- Codex (`src/lib/codexScans.ts`): rows from profile scan counts joined with bundled
  requirements; wildlife required = 20. Needs `getProfileViewingData.php` (section 14.2).
- Incarnon (`src/lib/incarnon.ts`): weapons with status `unlocked | adapter | missing`, evolution
  tier up to `INCARNON_MAX_EVOLUTION = 5`.
- Archon shards (`src/lib/inventory/archonShards.ts`): per-frame 5 sockets, colours
  `crimson amber azure emerald topaz violet`, tauforged variants, loose stock counts.

### QAM layout
```
MR 27  ->  45,210 XP to MR 28
  Foundry raises you to MR 28 (12,000 XP ready)
Mastered 1,203 / 1,512 (79.6%)
-- Easy mastery (top 5) ------------
 Level it      Kuva Bramma    +6,000
 Claim         Lex Prime      +3,000
 Build         Phantasma      +3,000
[Open mastery >]
```
### Full-screen
Tabs: Collection / Roadmap / Planned / Codex / Incarnon. Collection is a list (not a card grid)
with category on L1/R1 and status filter on Y.

---

## 6. Relics (`src/views/RelicsView.svelte`, 820 lines)

Relic data model and EV math are specified in `relic-planner.md` §5 and §7. This view adds:

State (`src/stores/relics.ts`, `config/shared/relicPlannerView.ts:56-79`):
```
tierFilter: "all"|"Lith"|"Meso"|"Neo"|"Axi"|"Requiem"   (persisted "wf_relics_tab")
ownershipMode: "owned"|"all"                             (default "owned")
squadSize: 1..4 (default 1)          search: string (max 200)
containsNeededReward: bool (false)   vaultedMode: all|vaulted|unvaulted
qualityMode: owned|intact|exceptional|flawless|radiant (default owned)
ownedAbove: 0|2|4|6|8|10 (hide relics with total copies <= N)
sortMode: tier|name|ev|ducat|ducatonator|owned (default tier)  sortDirection asc|desc
```
Changing ownership to `owned` sets quality `owned`; to `all` sets `intact`
(`RelicsView.svelte:271-277`). Without inventory, `ownedAbove` is forced to 0.

Pipeline (`computeFilteredRelicGroups`, `RelicsView.svelte:332-379`):
1. All groups; if inventory and ownership `owned`: keep groups with any owned copy.
2. Tier filter.
3. Row per group: `plat = cached squad EV` (`getCachedEv(key, squad, quality)`), `ducat =
   computeGroupDucatEv` (EV with ducat values), `ratio = ducat/plat` (null if plat<=0);
   quality for `owned` mode = the grade pinned on that card if still owned, else the highest owned
   grade (`relicQualityForMode`, `relicPlannerView.ts:117-125`).
4. `selectRelicPlannerRows` (`relicPlannerView.ts:176-191`): vaulted filter, ownedAbove on
   `ownedTotal`, search hook, needed-reward hook, then sort. Tier order Lith 0, Meso 1, Neo 2, Axi 3,
   Requiem 4 (else 99); metric sorts put nulls last, tie by tier then name; `owned` sort uses
   `ownedCount` for the selected quality mode.

Squad EV (`src/lib/relic/relicMath.ts:1-33`, verbatim algorithm): for N=1, `sum(p_i * price_i)`;
for N>1 (everyone opens the same relic, you pick the best of N), sort by price asc, merge equal
prices, `EV = sum(price_g * (CDF_g^N - CDF_{g-1}^N))` with `p = chance/100`.

Needed reward (`src/lib/relic/rewardNeed.ts:32-46`): not needed if owned by name/gameRef; needed if
no uniqueName; if mastery data present: needed iff recipe claims (`recipeCopiesFor`) > owned; if
some build consumes it -> not needed; else needed unless held, building in foundry, or mastered.

Search (`relicGroupMatchesSearch`): relic name, tier+code, reward names, quality labels, owned
counts.

Card (`src/components/relics/RelicCompactCard.svelte`): tier icon, name, vaulted badge, owned
count per quality (Int/Ex/Fl/Rad; click a quality to pin it in owned mode), selected EV
platinum/ducats/ratio with state `has-value | loading | no-data`, up to 6 reward icons
(`RELIC_PREVIEW_REWARD_LIMIT`, intact rewards sorted by `sortRelicRewards`, last 6), reward marks
(owned, mastered, parent owned). Click opens the relic modal (rewards table with chance/price).

Warm-up (`src/lib/relic/relicPriceCache.ts:16-28`, `relicWarmupController.ts:9-14`):
EV batch 30 groups, 4 workers, 50 ms pause between batches, no-data TTL 2 min (transient 30 s),
card price no-data TTL 5 min, EV warmup starts 2 s after mount, UI debounce 800 ms, visible card
warm limit 120. On the Deck compute EVs in the backend for all owned relics once per price-cache
refresh.

"Push to overlay" (`RelicsView.svelte:219-235`): sends IPC `overlay:push-relic-filters` with
`RelicOverlayFilterPush` = planner filters + `tierFilter` (null for all) + `neededRewardKeys`
(group keys passing the needed-reward engine, max 4000, key <= 64 chars; null when that filter is
off) + `pinnedQualities` (`{groupKey: quality}`). Validated by `normalizeRelicOverlayFilterPush`
(`relicPlannerView.ts:274-283`). On the Deck this becomes a backend call that stores the filters the
relic recommendation overlay reads (see `relic-planner.md`).

### QAM layout
```
Relics (owned)   Squad [1 2 3 4]   Tier [All v]
Sort [Platinum v]   Needed rewards only [toggle]
 Axi  A16  Rad x2   EV 34.5p  41d
 Neo  N9   Int x7   EV 21.0p  65d
 ... (10 rows, D-pad)
[Use these filters in overlay]   [Open relics >]
```
### Full-screen
Left filter column (240 px: tier, ownership, quality, squad, min owned, vaulted, needed, sort),
right list of compact cards two per row (500 px each). A on a card opens the reward table modal.

---

## 7. Missions (`src/views/MissionsView.svelte`, 427 lines)

Data source: after each mission end line in EE.log the backend waits `READ_DELAY_MS = 3_000`
(retry once after 1 s) and reads the game's resident inventory from memory, then diffs it against
the previous snapshot (`services/missionRewards.ts:27-41`, `services/missionRewardsDiff.ts`).
`PendingRecipes`, `PendingTrades`, `PersonalTechProjects` are ignored. Credits/endo deltas kept.
Opt-in: setting `missionTrackingEnabled` (default false). Status `phase idle|waiting|reading`,
`blocked: "tracking-off"`, `lastFailure: game-not-running | access-denied | no-fresh-copy |
inventory-unreadable | error` (`config/shared/missionRewardsTypes.ts`).

Types (verbatim shape): `MissionRewardSummary {id, endedAt, readAt, missionCount, missionType?,
node?, items:[{uniqueName,count}], credits, endo, baselineAt?}`. Query `{offset, limit (50, max
200), since?, todaySince?, missionType?, uniqueNames?}`; page `{summaries, matched, totals{missions,
credits, endo, items}, latest, recorded, today, missionTypes, itemTypes, status}`.

Rows (`buildRewardRows`, `src/lib/missionRewardRows.ts:53-87`): name from itemDb; if tradable,
slug by gameRef then name; unit price = rank-0 median, else rankless median; `platinum = each *
count`, `ducats = itemDb.ducats * count`; `unpriced` when tradable and no price; sort platinum desc,
ducats desc, name. Totals sum platinum, ducats, unpriced count.

Periods `today | 7d | 30d | all` (persisted `wf_missions_period`, default `7d`); `today` = local
midnight; 7d/30d = now minus N days (`missionPeriodStart`, `:154-158`). Search debounce 250 ms,
matched against item names (`matchRewardItemTypes`). Mission type label: `MISSION_TYPE_LABELS[type]`
else title-case of `type.slice(3)` (strip `MT_`). Event `mission-rewards-updated` triggers a merge
reload (`mergeFirstPage`, `appendPage` keep scroll position).

### QAM layout
```
Last mission  Hepit (Void) - Capture   14:32
 +38p  +45d  +12,400 cr  +320 endo
  Lith G1 Relic          x1
  Neurodes               x2   (unpriced items grey)
Today: 7 missions  +210p
[Open missions >]
```
### Full-screen
Period tabs on L1/R1, mission type dropdown, list of missions (node, time, plat, ducats, credits,
endo; A expands rewards), period totals panel on the right.

---

## 8. Rivens (`src/views/RivensView.svelte`, 769 lines)

Decode and grading: `riven-scanner.md` (`DecodedRiven`, `config/shared/rivenTypes.ts:16-34`:
`itemId, weaponName, weaponUniqueName, rivenName, masteryReq, currentRank, maxRank, rerolls,
polarity, disposition, stats[{tag,name,displayValue,maxRankValue,rankValues,rollFloat,grade,positive,multiplier}],
overallGrade, attributeGrade, statPerfectness, rivenType}`). Data: `invoke("getRivens")` ->
`{unveiled, veiled, veiledUnseen}`; reload on `inventory-updated`.

Tabs: `unveiled | veiled | finder` (persisted `wf_rivens_tab`). Filters: type `all, Rifle,
Shotgun, Pistol, Melee, Archgun, Kitgun, Zaw`; grade letter family `all, S, A, B, C, F` (prefix
match on `overallGrade`, which can carry +/-); attribute grade `all, Great, Good, OK, Bad`. Sort:
name (weapon), disposition, rerolls, grade, attr_grade (Great 4, Good 3, OK 2, Bad 1; "?" null)
(`src/lib/rivenLabels.ts:27-32`). Search keywords include `rivenName` and stat names.

Per card: weapon + generated name, overall grade colour, attribute grade, stats (`+x%` or `xN` for
multipliers, green/red), MR, polarity, rerolls, listed badge when a WFM contract matches
(`matchRivenListings`), dissolve hint (`src/lib/rivens/dissolve.ts:17-24`: only for attribute
grade Bad/OK, and only if rerolls >= 5 or endo >= 3000). Endo formula
(`config/shared/rivenEndo.ts:19-31`, verbatim):
```
endo = 100*(MR-8) + floor(22.5 * 2^rank) + 200*rerolls - 7   (MR clamp 8..16, rank 0..8, >= 0)
```
Context menu: copy chat tag `"[<weapon> <suffix>]"` and WTS line `"WTS [<tag>] <plat>p"`
(`src/lib/rivenChatTag.ts:10-22`). Veiled tab: grouped by challenge (`groupVeiledRivens`).
Finder (`src/components/RivenFinder.svelte`): weapon picker, required positives, negative required,
price/reroll bounds, min similarity, online/ingame only, hide 1p; backed by WFM
`/auctions/search?type=riven&weapon_url_name=...&sort_by=price_asc` (`services/wfmRivenSearch.ts:165`).

Deck: route only. QAM shows count by grade ("S 2 / A 5 / B 8"). Clipboard in game mode: copying to
clipboard works through `SteamClient` text input only partially; prefer showing the WTS line large
so the player can type it, or use the gamescope clipboard via `xclip -selection clipboard` on
`DISPLAY=:0` from the backend (untested, flag as risk).

---

## 9. Market (`src/views/MarketView.svelte`, 1380 lines)

Endpoints, auth (email/password sign-in with CSRF/JWT cookie), presence websocket and rate
limits: `data-sources.md` §1.4-1.7. Base `https://api.warframe.market/v1` and `/v2`, headers
`Accept: application/json`, `Content-Type: application/json`, `Platform: pc`, `Language: en`
(`services/wfmClient.ts:39-53`).

Tabs `sell | buy | rivens | browse | alerts` (persisted `wf_market_tab`, default `sell`).

Constants (`MarketView.svelte:139-147`): `ORDERS_STALE_MS 30_000`, `ORDERS_POLL_MS 30_000` (poll
while signed in, view visible, no order modal open), `CONTRACTS_STALE_MS 60_000`,
`CONTRACTS_PAGE_SIZE 40`, `CONTRACT_GRADE_BATCH 50`, grade retry 30 s x3.

Sell/Buy rows (`WfmOrder`, `src/types/market.ts:21-35`: `id, orderType, platinum, quantity,
perTrade?, visible, modRank, subtype?, itemId, itemName, itemUrlName, itemThumb`). Filter row adds
`count = owned copies` (`ownedCountForMarketOrder`), search over name and keywords `[orderType,
visible|hidden]`. Sort: name, platinum, amount (listed quantity), count (owned). Actions:
- Close one trade (`wfmCloseOrder(id, tradeSize)`, `POST /v2/order/{id}/close`): tradeSize =
  `normalizePerTrade(perTrade)`; confirm text uses count when perTrade > 1; locally subtract then
  background refetch (`:718-754`). This is the highest-value action on the Deck.
- Delete (`DELETE /v2/order/{id}`), bulk visible/invisible (`wfmSetVisible`), bulk delete,
  inline edit price/quantity (`PATCH /v2/order/{id}`), reprice with strategies
  (`RepriceOrdersModal`, `src/lib/market/repriceOrders.ts`, skip reasons `no-book | no-price |
  unchanged | not-sell`), sync quantities to inventory (`planQuantitySync`).
- Per-order write lock: no two writes to the same order overlap (`tryLockOrders`,
  `src/stores/market.ts:160-174`).

Presence: status `ingame | online | invisible`, optional auto "In Game" while Warframe runs,
hold minutes, away rules (settings, section 17.3).

Rivens tab: my contracts (`/profile/auctions` etc.), graded in batches, remove/close, toggle visible.

Browse (`src/components/market/MarketBrowseView.svelte`): search an item (max 8 suggestions),
sell/buy side, rank filter all|maxed, top 50 orders, auto refresh 45 s; Stats sub-view charts
48 h / 90 d (`MarketBrowseStats.svelte`, merged history `marketStatsHistoryMerge`).

Alerts (`config/shared/marketAlertTypes.ts`): rules `riven | item | baro` (baro reserved, never
fires). Limits: 100 rules, name 60 chars, cooldown 5..1440 min (default 60), history 500.
Item rule: `{itemUrlName, side sell|buy, min/maxPlatinum, minQuantity, statuses[ingame|online],
ownedBelow, ownedAbove}`. Riven rule: weapon slug, required positives, allowed/excluded negatives,
`hasNegative`, `positiveCount`, `minSimilarityPct`, `includeBidOnly`, stat bounds (normalised to
rank 8), MR/mod-rank/plat/reroll bounds, `minEndoPerPlat`, statuses. Engine (`services/marketAlerts.ts:48-57`):
tick 60 s, first after 30 s, each rule evaluated every 3 min, max 4 WFM requests per tick, failure
backoff 5 min doubling to 60 min, seen ids kept 7 days (500/rule), rules file
`market-alert-rules.json`. Hits fire a notification and appear in the Dashboard widget.

### QAM layout
```
WFM: signed in as Foo   Status [In game v]
My sell orders (12)
 Ash Prime Set      65p  x2   [Sold 1]
 Arcane Energize R5 55p  x1   [Sold 1]
 ...
Alerts: 1 new hit  "Rubico riven < 200p"
[Open market >]
```
`[Sold 1]` is a `ButtonItem` with a `ConfirmModal`. Sign-in lives in the full-screen route
(email/password fields use the OSK; store the session cookie, never the password).
### Full-screen
Tabs Sell/Buy/Rivens/Browse/Alerts; list left, selected order's live order book right.

---

## 10. World (`src/views/WorldView.svelte`, 1155 lines) and World > Arbitrations

### 10.1 Tabs and sections
World tabs `world | arbis | dailies | baro` (persisted `localStorage["world-tab"]`,
`WorldView.svelte:173-185`). Sections of the `world` tab (`:4-90`, ids verbatim):
`world.weekOverview, world.cycles, world.timers, world.resurgence, world.circuit,
world.steelPath, world.fissures, world.fissureAlerts, world.invasions, world.darvo, world.vendors,
world.nightwaveShop, world.baro, world.bounties`. Conditional: steelPath (if honors present),
invasions (if any not completed), darvo (if deals), baro (if active with inventory or arrival
known), bounties (if any) (`:394-409`). Collapsed sections persisted `world-collapsed-sections`
(bounty job collapses are not saved).

### 10.2 Pure functions to port (`src/lib/world/useWorldView.ts`)
- `buildWorldTimes` (`:204-255`): strings for baro (expiry if active else activation), varzia,
  daily reset (next 00:00 UTC), weekly reset (next Monday 00:00 UTC), sortie expiry (fallback daily),
  steel path expiry (fallback weekly), duviri (strict), earth/cetus/vallis/cambion via
  `cycleTimeDisplay` (prefer expiry; else API `timeLeft` unless it reads as zero).
- Duration formatting (`src/lib/format.ts:11-49`, verbatim rules): countdown `>24h -> "Dd Hh"`,
  else `"Hh Mm Ss"`; strict `"Hh Mm Ss"` or `"Mm Ss"`; remaining `>24h -> "Dd Hh"` else `"Hh Mm"`;
  `<= 0` -> "Refreshing..." (countdown) / "Ready!" (remaining). `formatNumber`: `>= 999,950 ->
  "x.xM"`, `>= 10,000 -> "x.xK"` (one decimal, trailing ".0" removed), else locale string.
- `buildFissureRows(fissures, mode, now, coarse)` (`:257-289`): drop expired and those expiring
  within `FISSURE_EXPIRY_GUARD_MS = 1_500`; mode `railjack` = `isStorm` only; other modes exclude
  storms; `all` = normal + steel; `steel` = `isHard`, `normal` = not `isHard`. Sort by tier
  (`lith 0, meso 1, neo 2, axi 3, requiem 4, omnia 5`, else 99) then expiry asc. Adds `timeStr`
  (strict), `tierCls`, `sourceMode`.
- `buildCycleRows` (`:291-382`): rows earth (day/night), cetus (day/night), vallis (warm/cold),
  cambion (fass/vome), duviri (mood from `duviri.state`, next = `nextState`); a row is dropped when it
  has no expiry. `urgent` = remaining/total < `URGENCY_RATIO = 0.2` (total = expiry-activation).
- `buildResetUrgency` (`:394-407`), `buildBountyGroups` (`:384-392`, order Ostrons 0, Solaris 1,
  Entrati 2, Holdfasts 3, Cavia 4, Hex 5), `buildBountyTimers` (urgent with fallback total 9,000,000 ms).
- Circuit (`src/lib/world.ts:226-300`): `CIRCUIT_NORMAL_ROTATION` (11 weeks of 3 frames) and
  `CIRCUIT_HARD_ROTATION` (9 weeks of 5 weapons), verbatim lists in source; `circuitRotationIndex`
  = week with most matches, accepted only if hits >= ceil(len/2); `circuitWeeksFrom` rotates so
  index 0 is this week. Choices resolved to items with owned/subsumed flags and incarnon status.
- Featured primes (Varzia, `buildFeaturedPrimes`), Baro owned set (`buildBaroOwnedSet`), Baro
  planner (`src/lib/world/baroPlanner.ts`: catalog from history `baro-history-v1` + current
  manifest + wishlist `baro-wishlist-v1`; `baroBudget` vs ducats/credits held).
- Steel Path honors upcoming: "in N days" = `max(1, ceil(ms/86_400_000))`.

### 10.3 Arbitrations tab (schedule)
Data `https://browse.wf/arbys.txt`, lines `<epochSeconds>,<nodeId>` (`services/arbiSchedule.ts:23,65-79`;
node id regex `^[A-Za-z0-9_./:-]{2,120}$`), labels from region translation, window = from one hour
ago to one year ahead, refresh hourly. Full detail in `arbitration.md`. UI
(`src/components/world/ArbiSchedule.svelte`, `src/lib/world/arbiScheduleData.ts`): node search,
selected nodes (`arbi-sched-selected`), presets (`arbi-sched-presets`), days to show
(`arbi-sched-days`), grouped by day, countdown, faction badge, bell per occurrence or favourite
node (alerts lead 1..120 min, default 5).

### 10.4 Dailies tab (`src/components/world/DailiesTracker.svelte`, `src/lib/world/dailies.ts`)
Built-in tasks (id, period, target) (`dailies.ts:56-229`): sortie(sortie,1), spIncursions(daily,5),
simaris(daily,1), syndicateStanding(daily,1), dailyFocus(daily,1), archonHunt(archon,1),
circuitNormal(weekly,1), circuitSteelPath(weekly,1), netracells(weekly,5), deepArchimedea(weekly,1),
temporalArchimedea(weekly,1), kahl(weekly,1), clem(weekly,1), ayatanHunt(weekly,1),
descendiaNormal/SteelPath(descendia,1), calendar1999(calendar1999,1), steelPathHonors(steelPath,1),
vendors palladino/acrithis/bird3/yonta(weekly), tenetMelee(tenet), codaWeapons(coda), baro(baro),
varzia(varzia), darvo(darvo); plus dynamic Nightwave and alert tasks and up to 30 custom tasks
(label <= 60, target <= 99). Persisted `world-dailies`.
Auto-detection from inventory (`src/lib/world/dailiesAuto.ts:199-260`): sortie/archon done when
`LastSortieReward`/`LastLiteSortieReward` contains the live world-state id; netracells and
Archimedea from counters; Simaris, standing pool (`DailyAffiliation`) and focus (`DailyFocus`)
only when the inventory mtime is inside today's UTC day; Clem `GetClem*`, Ayatan `TreasureHunt*`
in `PeriodicMissionCompletions` since week start; incursions `HardDaily*` since day start (max
5); Kahl weeks counted from `Date.UTC(2014,1,10)`; Nightwave acts. Rules only add doneness.

### 10.5 Notifications driven from world state
`ipc/worldStateIpc.ts:490-578`: cycle transition alerts per `cycleAlerts.<key>` (titles "Earth
Cycle", "Cetus Cycle", "Orb Vallis Cycle", "Cambion Drift Cycle", "Duviri Cycle"), lead-time
alerts `cycleAlertMinutesBefore` (0..120, default 3), Baro arrival, Varzia rotation, and fissure
rules `{tier any|Lith..Omnia, missionType, steelPath any|normal|steel, planet}`: match on new
fissure id `tier|node|expiry|isHard`, planet by `node` containing `"(<planet>)"`; body
`"<tier> <missionType>[ (Steel Path)] - <node>"`. On the Deck, send through Decky `toaster.toast`
from the frontend (backend emits an event) instead of desktop notifications.

### QAM layout (this is the primary QAM page)
```
[World | Arbis | Dailies | Baro]  (Tabs scroll horizontally)
Daily 6h 12m   Weekly 2d 6h   Sortie 6h 12m
Cycles: Cetus Night 12m | Vallis Warm 3m | Cambion Vome 44m | Duviri Joy 1h
Fissures [Normal v]  Lith Capture Hepit 21m ...
Next arbitration: 15:00  Casta (Ceres) Defense  Grineer  [bell]
Baro: arrives in 3d 4h  (Strata Relay, Earth)
Circuit SP: Braton, Lato, Skana, Paris, Kunai
```
### Full-screen
Tabs World/Arbitrations/Dailies/Baro; World uses a 3-column masonry (cycles+timers, fissures,
invasions/bounties); fissure mode as `Dropdown`.

---

## 11. Syndicates (`src/views/SyndicatesView.svelte`, 359 lines; `src/lib/syndicates/rankup.ts`)

Table `src/data/syndicateRanks.json` (generated from DE `ExportSyndicates`): tag, name, kind
(`normal` = the six aligned, shared pool; `openWorld`; `other` = Conclave), `dailyBin`,
`dailyField`, `alignments` (negative = enemy), `initiation`, `titles[{level, name, minStanding,
maxStanding, sacrifice{credits, items[{itemType,count,name}]}}]`.
Syndicates and pool fields: Arbiters/Suda/New Loka/Red Veil/Steel Meridian/Perrin ->
`NORMAL`/`DailyAffiliation`; Cavia `DailyAffiliationCavia`; Entrati `DailyAffiliationEntrati`;
Necraloid; Ostron `DailyAffiliationCetus`; Solaris `DailyAffiliationSolaris`; Hex; Holdfasts
`DailyAffiliationZariman`; Quills; Ventkids; Vox; Conclave `DailyAffiliationPvp`.

Algorithms (port verbatim):
- `syndicateStatus(inv, meta)` (`rankup.ts:136-160`): affiliation from `inv.Affiliations[]` by
  `Tag`; `level = Title ?? 0`, `standing = Standing ?? 0`; `rankThreshold(level)` = title's
  `minStanding`, else previous title's `maxStanding`, else 0 for level 0; `standingToNext =
  max(0, threshold(next) - standing)`; bar = `(standing - tierStart)/(tierEnd - tierStart)`;
  `dailyRemaining = inv[meta.dailyField]`.
- Daily cap `16000 + 500 * MR` (`PlayerLevel`) (`:9-10,105-109`).
- `planSteps(inv, meta, target)` (`:173-213`): one step per level from current+1 to target;
  initiation cost added on level 1 if not `Initiated`; sacrifice of the title reached (negative
  levels use the title below); `standingNeeded` = threshold minus previous threshold (first step
  from live standing).
- `aggregateNeeds(plans, inv, owned)` (`:216-279`): sum credits (vs `RegularCredits`), items (vs
  `componentOwnership`), standing per pool with `daysEstimate = ceil(max(0, needed - remainingToday)/dailyCap)`.
- `alignmentConflicts` (`:283-300`): pairs of selected goals with negative factor.
Goals persisted `wf_syndicate_goals_v1` (`{tag: targetLevel}`).

### QAM layout
```
Standing left today: Normal 41,500 / Cetus 32,000 / Holdfasts 32,000 ...
 Steel Meridian  Rank 4 General  ######--- 62%  92,000 to next
 Cavia           Rank 3  ...
[Open syndicates >]
```
### Full-screen
Groups Normal / Open world / Other; each syndicate row with target picker; totals panel (credits,
items missing, standing days) on the right.

---

## 12. Stats (`src/views/StatsView.svelte`, 1003 lines)

Two tabs: `tracking | personal` (persisted `stats-tab`).
Tracking data (`services/statsTracker.ts`): on every inventory payload, read resources and keep a
per-day baseline; delta = current - first value of the local day; relics opened = sum of decreases
in total `LevelKeys` count; trades counted per day. History 365 days, file `stats-history.json`,
schema v2 (local-time day keys). Resource catalog (`config/shared/statsTypes.ts:122-146`, verbatim
ids and sources): `plat PremiumCredits`, `ducats MiscItems/PrimeBucks`, `aya MiscItems/SchismKey`,
`credits RegularCredits (compact)`, `endo FusionPoints (compact)`, `vitus MiscItems/Elitium`, `kuva
MiscItems/Kuva (compact)`, `regalAya PrimeTokens`, `voidTraces VoidTearDrop`, `steelEssence`,
`rivenSliver RivenFragment`, `vosfor DistillPoints (compact)`, `nitain Alertium`, `forma`,
`argonCrystal`, `orokinCell`, `tellurium`, `somaticFibers MemoryCryptoFragment`, `hexenon
ConcentratedGas`, `narmerIsoplast NarmerBountyResource`, `cetusWisp
/Lotus/Types/Gameplay/Eidolon/Resources/CetusWispItem`, `pathosClamp
/Lotus/Types/Gameplay/Duviri/Resource/DuviriDragonDropItem` (misc paths prefixed
`/Lotus/Types/Items/MiscItems/`). Default charts: plat, ducats, aya, credits, endo, vitus, kuva
(persisted `wf_stats_chart_resources`). Plus charts relicsOpened and dailyTrades. Timeframes from
`TIMEFRAME_OPTIONS` (`src/lib/stats/chartData.ts`), default 30 days. Session strip: current
balance and "+delta today" per resource.

Personal tab: `getPersonalProfile` -> `https://api.warframe.com/cdn/getProfileViewingData.php?playerId=<24-hex accountId>`
(`services/codexProfile.ts:360`), manual refresh, min interval 60 s, timeout 20 s, max 40 MB;
career stats, equipment usage, enemies, abilities, missions, appearance. Account id comes from the
memory authz read (`services/gameMemoryAuthz.ts`).

Deck: QAM shows "today" deltas for plat/ducats/credits/endo/kuva; route shows bar charts (use a
simple SVG bar component; `chartData.ts` gives bars).

---

## 13. Market analysis (view id `analytics`, `src/views/MarketAnalysisView.svelte`, 786 lines)

Data: trade ledger (`TradeEvent`, `config/shared/statsTypes.ts:22-38`: `id, date ISO, type
sale|purchase|trade, platChange >= 0, items[{internalName, displayName, count, direction
given|received, wfmSlug?}], partner?, wfmClosed?, source live|gdpr, credits?, tradeTax?`). Trades
come from the EE.log trade dialog ("Are you sure you want to accept this trade?",
`services/eeLogMonitor.ts:211`) and are persisted by `services/tradeTracker.ts` (live log 2000
rows, rest archived per year; max 12 items per trade; dedupe window 30 s). Paged IPC `ledgerQuery`
(page 50, analytics cap 6000 rows).

Sections: summary, top traded, time charts, top items, by type, worth today, partners, year
compare, ledger. Pure functions (`src/lib/stats/tradeAnalytics.ts`, port verbatim):
`resolveRangePreset` (`all, 30d, 90d, 365d, ytd, lastYear, custom`), `filterEvents`, `computeFlow`
(`:426-460`: platIn from sales, platOut from purchases, swaps, net, volume, distinct active days),
`topItems` (`:517-534`, multi-item trade splits plat per unit), `typeRollup` (kinds `riven, set,
prime, mod, arcane, relic, resource, other`), `partnerRollup`, `monthlyFlow`, `recentDailyFlow`,
`todayFlow`, `yearComparison` (Jan 1..today vs same span last year), `fifoCostBasis` (`:1023-1132`:
oldest first, purchase sorts before a sale at the same timestamp, lots per base item key, sales
consume lots FIFO, swaps consume without revenue, unmatched units reported as `unpricedRevenue`,
result flagged `estimated: true`), `worthToday` (`:1136-1203`: sold units valued at today's median;
unpriced stays null, not 0).

Deck: route only, low priority. QAM: "This month: +4,210p net (38 sales)" (same as Dashboard).

---

## 14. Wiki (`src/views/WikiView.svelte`, 500 lines)

Drop search over flattened drop tables (`services/dropData.ts`): source
`https://drops.warframestat.us/data/info.json` (hash) and `.../all.json`, cached with
`CACHE_VERSION = 2`; refresh only when hash changes. Flattening (`dropData.ts:155-222`): mission
rewards (`"<node> (<planet>)"`), relics (intact only, `"<tier> <name> Relic"`), transient rewards,
sortie, key (quest) rewards, bounties (`cetusBountyRewards, solarisBountyRewards, deimosRewards,
zarimanRewards, entratiLabRewards, hexRewards`), mod/blueprint locations by enemy, enemy tables,
byAvatar tables, syndicates; plus bundled dojo research rows (chance 100). Dedup key
`item|place|rarity|chance` (enemy row wins). Row: `{item, place, rarity, chance, kind}`.

`searchDrops(query, mode item|place|enemy, limit 300)` (`dropData.ts:341-372`): lower-case
substring on `item` (item mode) or place field; score 0 = match at start, 1 = after whitespace, 2 =
elsewhere; sort score, chance desc, item, place. Returns `{rows, total}`.

View extras: saved searches per mode (`wiki:item|place|enemy`), live bounty job name for
"Level X - Y ... Bounty" rows (map location <-> syndicate key), enemy spawn info (planets by
tileset/faction) for place/enemy searches, links to `https://wiki.warframe.com/w/<Name_With_Underscores>`
(`src/lib/wikiUrl.ts`). Clicking a relic row opens the relic modal; item row opens item detail.

Deck: route only. Search box = `TextField` (OSK). Results list 60 px rows: item, place, rarity
colour, chance %. Links open with `Navigation.NavigateToExternalWeb(url)`.

---

## 15. Arbitrations run analysis (view id `arbi`, `src/views/ArbiAnalyzeView.svelte`, 760 lines)

Tabs `arbitrations | profitTaker`. Data from `getArbiRuns`/`getPtRuns`, events `arbi-run-saved`;
records `ArbiRunRecord` (`config/shared/arbiTypes.ts:107-145`: mission, node, type
defense|interception|disruption|other, duration, rotations, drones, enemies, `vitusActual`, stats
{killsPerDrone, avgDroneIntervalSec, expectedVitusMean/Std, vitusPerMin, saturation buckets, waves,
drone/reward timestamps, spawn points}, tags, players, notes). Actions: import past EE.log
(`importArbiLog`, `importPtLog`), delete run / raw log, set vitus, tags, notes, compare up to
`ARBI_COMPARE_MAX` runs. Charts: DPM, drone cadence, saturation, timeline, rotation grid, spawn
map. Parsing and math: `arbitration.md`.

Deck: QAM shows the last run summary (node, duration, drones, vitus/min, expected vitus) and a
"tracking on/off" toggle; route lists runs with a detail page using simple SVG charts.

---

## 16. Settings (`src/views/SettingsView.svelte`, 1560 lines)

Categories (`src/stores/preferences.ts:36-50`): `general, notifications, inventory, overlay,
appearance, about`. Everything overlay-related is persisted by the main process in
`overlay-settings.json` (`config/runtime/overlaySettings.ts:121`); the rest in localStorage.
Saves are queued and debounced; "Restore defaults" resets the overlay form.

### 16.1 General
Language (UI), game language (`auto` or explicit), keep running on close (`keepRunningOnClose`,
false; N/A on Deck), Warframe lifecycle (`warframeLifecycleEnabled`, false: launch/close helpers),
auto-focus search (`wf_auto_focus_search`).
### 16.2 Inventory
Inventory source (`helper | manual | aleca | none`), auto inventory sync (`autoInventorySyncEnabled`,
true; helper source only), export inventory, item list options: hide foundry-pending blueprints
(`wf_hide_foundry_claims`, true), hide founder items, badges (`wf_show_mastered_badges`,
`wf_show_owned_parent_badges`, `wf_show_foundry_ready_badges`, `wf_show_vaulted_badges`, all true).
### 16.3 Notifications
Notify only while game running, WFM DM notifications (`wfmNotificationsEnabled` false), in-game
message notifications (`messageNotificationsEnabled` true, while focused false), Windows
notification seconds (Windows only), sound (`notificationSoundEnabled` true, volume 1, use system
sound - Windows only), trades: toast seconds (`tradeNotificationSeconds` 5), desktop notification
on trade (false), unlist on trade (`autoCloseWfmOrders` true), +rep keybind (`tradeRepHotkey` F9),
webhook channels (Discord / generic, Discord ping), channel routing native|webhook per source,
fissure alert rules, cycle alerts. WFM presence: `wfmAutoIngameEnabled` false,
`wfmStatusHoldMinutes` 0, `wfmAwayIdleEnabled` false + minutes, `wfmAwayWhenClosedEnabled` false.
### 16.4 Advanced
Block third-party injection (Windows only), OCR debug images (`ocrDebugImagesEnabled` true), track
arbitration runs (`arbiTrackingEnabled` true), track mission rewards (`missionTrackingEnabled`
false), reset defaults, open scan-debug / log folder.

---

## 17. Settings that affect overlays (the important part for the Deck)

All keys and defaults verbatim from `config/runtime/overlaySettings.ts:44-191`.

### 17.1 Overlay on/off (unset means ON; only explicit `false` disables, `:196-212`)
| Key | Default | Overlay |
|---|---|---|
| `relicRewardsOverlayEnabled` | true | relic reward screen prices (`relic-rewards.md`) |
| `relicRecommendationOverlayEnabled` | true | relic planner/selection overlay (`relic-planner.md`) |
| `rivenOverlayEnabled` | true | riven scan panels (`riven-scanner.md`) |
| `rivenSimilarAuctionsShown` | true | "Similar on WFM" list also shows bid-only auctions |
| `arbiSummaryOverlayEnabled` | true | post-arbitration summary card (`arbitration.md`) |
| `tradeNotificationOverlayEnabled` | true | in-game trade toast |
| `arbiTrackingEnabled` | true | arbitration parser at all |
| `missionTrackingEnabled` | false | post-mission memory read (Missions view, Last mission widget) |

### 17.2 Detection and geometry
| Key | Default | Meaning / Deck note |
|---|---|---|
| `autoTriggerEnabled` | true | trigger reward scan from EE.log automatically |
| `warframeUiScale` | `0.99` (`REFERENCE_WARFRAME_UI_SCALE`) | in-game interface scale; crop ratios were measured at 0.99 so this default is a no-op |
| `warframeUiScaleAuto` | true | read scale from `EE.cfg` next to EE.log: only when `Flash.FlashDrawScaleMode=DSM_CUSTOM` (or `MSM_CUSTOM`), value = last `Flash.FlashDrawScale=`, rounded 2 dp, clamped 0.5..1 (`services/eeLogPath.ts:221-233`). On the Deck EE.cfg is under `.../compatdata/230410/pfx/drive_c/users/steamuser/AppData/Local/Warframe/` |
| `overlayScale` | 1 | global overlay zoom |
| `overlayWindowScales` | `{}` | per-window scale for `reward, planner, rivenLeft, rivenRight, arbiSummary` (`tradeNotification` is fixed size) |
| `overlayWindowBounds` | `{}` | saved `{x, y, displayId?, width?, height?}` per window (content px before zoom) |
| `overlayDragHintDismissed` | false | UI hint only |
| `linuxOverlaysInteractive` | false | Linux only: overlays open taking clicks. On the Deck the X11 overlay under gamescope gets no input; drop and use QAM buttons |
| `ocrDebugImagesEnabled` | true | save failed-scan crops |

Overlay layout editor (`config/shared/overlayLayout.ts`): per overlay kind a canvas and field list;
each field `{x, y, scale, color|null, hidden, cards?}` (defaults 0,0,1,null,false). Canvases
(verbatim, `:543-641`): `reward 980x236` (4 cards; `vaulted` hidden by default), `planner 460x640`
(per-reward rows hidden by default), `rivenLeft 420x640`, `rivenRight 420x640`, `arbiSummary
420x252` (hidden by default: actual vitus, vitus rate, kill rate, kills/drone, drone interval,
squad, end reason), `tradeNotification 370x104`. Reward fields: `slotLabel, itemName, rarity,
platinumIcon, platinumValue, ducatIcon, ducatValue, pricePlaceholder, owned, mastery, foundry,
setOwned, setPrice, vaulted, part0Icon..part2Count` (`:204-224`). Stored as `overlayLayouts[kind]`
and `rewardLayout` (`{version:1, fields}`).
Deck port: keep the field schema so the Pillow renderer honours `hidden`, `scale`, `color`, offset;
expose only "hide field" toggles and a per-overlay scale slider in the full-screen Settings route.
The live drag editor is not portable (no pointer input on the overlay).

Overlay opacity (theme effects, `src/lib/theme/themeStorage.ts:250-267`,
`config/shared/overlayOpacity.ts:1-2`): global `overlayOpacity` default 1, clamped 0.3..1, plus
`overlayOpacityOverrides[kind]`. Port as alpha multiplier on the ARGB window.

### 17.3 Hotkeys (Windows/X11 global hotkeys; not available in gamescope)
`hotkeyEnabled` true + `hotkey` "F8" (manual reward scan), `interactionHotkeyEnabled` true +
`interactionHotkey` "F7" (toggle click-through; legacy default "Control+Tab" migrated),
`rivenRescanHotkeyEnabled` true + `rivenRescanHotkey` "F6", `tradeRepHotkeyEnabled` true +
`tradeRepHotkey` "F9". Deck replacement: QAM buttons "Scan rewards now", "Rescan riven",
"Hide overlays"; optionally a Steam Input chord bound to a key that the backend reads via evdev
(root can open `/dev/input/event*`), flag as risk.

### 17.4 Non-overlay settings that change overlay content
- Relic filter push from the Relics view (section 6) sets what the relic recommendation overlay lists.
- Badge preferences (mastered, owned parent, foundry ready, vaulted) decide marks on overlay rows.
- `hideFoundryClaims` changes `componentOwnership`, which feeds "owned" counts on reward cards.
- Inventory safety settings (`normalizeSafetySettings`, `safetyRules.ts:155`) and mastery pins
  decide "needed" rewards.
- `tradeNotificationSeconds` (5) and the +rep keybind control the trade toast lifetime.

### QAM Settings page
```
Overlays
 [x] Relic reward prices      [x] Relic recommendations
 [x] Riven scanner            [x] Arbitration summary
 [x] Trade toast
 Overlay opacity  [=====-----] 0.8      Overlay size [1.0 v]
 [Scan rewards now] [Rescan riven] [Hide overlays 10 s]
Tracking
 [x] Arbitration runs   [ ] Mission rewards (memory read)
Interface scale: Auto (detected 100%)   [Manual...]
[All settings >]
```

---

## 18. Gamepad and Decky UI conventions (all views)

- QAM width: design for 300 px content; one `PanelSection` per block, max ~10 focusable rows per
  block, long lists behind "Open full screen". Use `ToggleField`, `SliderField` (notched for preset
  lists), `DropdownItem`, `ButtonItem`, `Field` with `focusable`.
- Full-screen routes: register `routerHook.addRoute("/wfh/:view", ...)`; open with
  `Navigation.Navigate("/wfh/relics")` then `Navigation.CloseSideMenus()`. B returns. Use
  `Tabs` (L1/R1 switch) for view-internal tabs, `Focusable` with `flow-children="row"` for grids,
  `onSecondaryButton` (X) for filter menus and `onOptionsButton` (Y/menu) for sort.
- Text input only through `TextField` (opens the Steam OSK); debounce 250 ms like WFHelper.
- Never block on network in the frontend: the backend returns cached view-models immediately with a
  `stale` flag and pushes updates via `decky.emit` events (`world-updated`, `inventory-updated`,
  `mission-rewards-updated`, `market-alerts:changed`, `arbi-run-saved`) mirroring the WFHelper
  event names.
- Clocks: the frontend ticks one 1 s timer for countdowns, formatting from absolute expiry epochs
  the backend sends (do not send preformatted strings for timers).

---

## 19. Suggested backend API (callable names, mirror WFHelper IPC where one exists)

| Callable | Returns | Built from |
|---|---|---|
| `get_dashboard()` | `{cycles[], fissures[], foundryReady[], lastMission, alerts{hits[],health}, goals[], baro, inventoryValue, tradeMonth, recentRuns[]}` | sections 2, 7, 9, 10 |
| `get_world_state()` | `WorldState` + `fetchedAt` | `getWorldState` |
| `get_arbi_schedule(days)` | `{entries[{epochMs,nodeId,node,mission,faction}], fetchedAt, alerts}` | `arbiSchedule.ts` |
| `get_inventory_view(tab, filters, offset, limit)` | `{rows[], total, totals{inView,inventory}}` | section 3 |
| `get_foundry_view(filterKey, filters)` | `{rows[], pinnedTotals}` | section 4 |
| `get_mastery_view(tab, cat, status, filters)` | `{summary, rows[] or roadmap or plan}` | section 5 |
| `get_relic_view(state)` | `{groups[{key,name,tier,vaulted,owned{q:n},ev{plat,ducat,ratio,state},rewards[]}]}` | section 6 |
| `push_relic_overlay_filters(push)` | ok | `RelicOverlayFilterPush` |
| `get_mission_rewards_page(query)` | `MissionRewardsPage` + priced rows | section 7 |
| `get_rivens()` | `{unveiled[], veiled[], veiledUnseen[]}` | `getRivens` |
| `wfm_get_session()`, `wfm_sign_in(email,pw)`, `wfm_sign_out()`, `wfm_get_orders()`, `wfm_close_order(id, qty)`, `wfm_delete_order(id)`, `wfm_set_visible(ids, v)`, `wfm_update_order(id, patch)`, `wfm_set_status(s)` | WFM | `data-sources.md` |
| `market_alerts_list/get_hits/status/save/delete/set_enabled` | alerts | section 9 |
| `get_syndicates()` / `set_syndicate_goal(tag, level)` | cards + totals | section 11 |
| `get_stats()` / `get_personal_profile(force)` | history + session | section 12 |
| `ledger_query(range, offset, limit)` | `TradeEvent[]` page | section 13 |
| `search_drops(q, mode)` | `{rows, total}` | section 14 |
| `get_arbi_runs()` / `get_pt_runs()` | runs | section 15 |
| `get_settings()` / `set_settings(patch)` | `OverlaySettings` + UI prefs | sections 16-17 |

Response example (`get_relic_view`):
```json
{"stale": false, "generatedAt": 1792252800000,
 "groups": [{"key": "Axi A16", "name": "Axi A16", "tier": "Axi", "vaulted": true,
   "owned": {"intact": 0, "exceptional": 0, "flawless": 0, "radiant": 2},
   "selectedQuality": "radiant",
   "ev": {"plat": 34.5, "ducat": 41.2, "ratio": 1.19, "state": "has-value"},
   "rewards": [{"name": "Ash Prime Systems Blueprint", "rarity": "Rare", "chance": 10,
                "platinum": 22, "ducats": 100, "owned": false, "needed": true}]}]}
```

---

## 20. Porting risks (summary)

1. Data volume and CPU: `parseInventory`, `parseFoundry`, mastery computation and relic EV warm-up
   are ~1 s each on large accounts in V8; in CPython on the Deck APU expect several seconds. Run
   them in the worker process, cache by inventory hash, and serve stale results.
2. Many views depend on WFM prices and on signed-in WFM features. Respect the global WFM rate
   limiter (`data-sources.md` §1.3); never let QAM rendering trigger per-row network fetches.
3. Text entry (WFM sign-in, searches) needs the Steam OSK; keep all core flows gamepad-only.
4. Hotkeys, click-through toggling, overlay drag editor and desktop notifications do not exist
   under gamescope; replace with QAM buttons, Decky toasts and fixed layout fields.
5. Root backend: `os.homedir()` is `/root`; Steam/compatdata/EE.log/EE.cfg lookups and data dirs must
   target `/home/deck` (see `game-integration-linux.md`).
6. Mission rewards, personal profile (account id) and auto inventory rely on reading game memory;
   failures must degrade to "unavailable" states exactly like WFHelper's status enums.
7. Windows-only items to drop: Windows toast seconds, system sound, injection blocking, DXGI
   capture, tray/keep-running.

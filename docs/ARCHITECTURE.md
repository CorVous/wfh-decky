# WFHelper for Decky: architecture and module contracts

This is the contract every module is built against. The port specs in
`docs/port/*.md` describe *what* WFHelper does; this file fixes *where* it goes
and *how* modules talk to each other in this repo.

## Runtime layout

```
Decky Loader (root)
 └─ main.py                    thin relay, Decky's embedded Python (stdlib only)
     └─ bin/python/bin/python3 -m wfh      worker, bundled CPython 3.12
          numpy, onnxruntime, Pillow, python-xlib (requirements-runtime.txt)
          py_modules/wfh/...   all backend logic
 frontend: src/ (React, @decky/ui) → dist/index.js
```

- Frontend → backend: `call(method, params)` in `src/rpc.ts` → `main.py rpc()` →
  worker JSON-lines RPC (`wfh/rpc.py`). Methods are registered with
  `@rpc.method("area.name")` and take keyword params. Return JSON-able values
  (dicts, lists, numbers, strings). Names use `area.verb` (e.g. `world.state`).
- Backend → frontend: `app.emit(name, data)` → `useEvent(name, fn)` in React.
  `app.toast(title, body)` shows a Steam toast.
- The worker runs as **root**. The player's home is `session.deck_user().pw_dir`
  (normally `/home/deck`). Never use `~` / `os.path.expanduser`.
- Long or blocking work (HTTP, OCR, file parsing) runs off the event loop:
  `await app.run_blocking(fn, *args)` inside async RPC handlers, or
  `app.submit(fn, *args)` from EE.log handlers (which run on the tailer thread
  and must return quickly).

## Module map (py_modules/wfh)

| Module | Owns | Public API (others may rely on exactly this) |
|---|---|---|
| `app.py` | worker context | `App` (settings, overlays, eelog, emit, toast, run_blocking, submit, services dict, plugin_dir, runtime_dir, settings_dir, models_dir, data_dir), `get_app()`, `MODULES` load order |
| `rpc.py` | RPC plumbing | `@method(name)`, `emit`, `call_local` |
| `settings.py` | settings JSON | `Settings.get("a.b")`, `.update(patch)`, `.all()`, `.on_change(fn)`; defaults in `DEFAULTS` (add your section there) |
| `net.py` | HTTP + disk cache | `fetch(url, headers, timeout, data, method) -> bytes`, `get_json(url, max_age, headers)`, `HttpError`, per-host throttle table `_HOST_INTERVAL` |
| `session.py` | process/session discovery | `deck_user()`, `find_session_env()`, `find_warframe_pid()`, `warframe_running()` |
| `capture.py` | screenshots | `grab(timeout) -> PIL.Image (RGB, game resolution)`, `CaptureError` |
| `eelog.py` | EE.log + Proton debug log tail | `EELogTailer.subscribe(fn(line), source="merged")` (merged: Proton real-time lines at once + file lines minus their echoes; `"file"`: every EE.log line once, for run trackers; `"realtime"`), `.source` (`"file"`/`"realtime"` of the line being dispatched), `.realtime_active()`, `.status()`, `.feed(line)` / `.feed_realtime(payload)` (tests), `find_ee_log()`, `steam_libraries(home)`; RPC `eelog.status`; `protonlog.py`: Proton log parse/gate; `eecfg.py`: `ui_scale()` / `ui_scale_info()` from EE.cfg, `is_cfg_saved_line(line)` |
| `overlay/manager.py` | the overlay window | `app.overlays.show(name, rgba_img, x, y, ttl=None, z=0)`, `.clear(name)`, `.size` (w, h) |
| `overlay/draw.py` | look and feel | `Card`, `Cell`, `badge`, palette constants, `font(size, bold)`, `fmt_plat` |
| `ocr/paddle.py` | text recognition | `PaddleRecognizer.get(models_dir)` singleton; `.recognize(crops: list[np.ndarray HxWx3 uint8]) -> list[(text, confidence)]` |
| `ocr/yolo.py` | riven stat line detector | `StatLineDetector.get(models_dir)`; `.detect(img_rgb) -> list[Box]` |
| `ocr/image.py` | shared image ops | otsu threshold, row splitting, resize helpers used by scanners |
| `data/items.py` | item database | `items()` → `ItemDb` with `.by_unique(uniqueName)`, `.by_name(name)`, `.prime_parts()`, `.match_name(ocr_text) -> (Item, score)`; `Item` has `unique_name, name, category, ducats, vaulted, tradable, wfm_slug, components, mastery_req, image_name` |
| `data/relics.py` | relic database | `relics()` → `RelicDb` with `.get(name)`, `.all()`, `Relic(name, tier, vaulted, rewards=[Reward(item_unique, item_name, rarity, chance_by_refinement)])` |
| `data/names.py` | OCR reward-name matching (WFHelper rewardScannerMatch port) | `rank_candidates(ocr_text, items, limit) -> [Candidate(item, confidence, score, mode)]`, `match_single(ocr_text, items)`, `SLOT_ACCEPT_CONFIDENCE`, `normalize_wfm_slug`, `normalize_for_slug`; items are objects with `.name` or dicts with "name" |
| `data/market.py` | warframe.market | `price(slug, rank=None) -> Price|None` (cached, fresh 30 min), `prices(slugs) -> dict`, `prefetch(slugs)` (background), `search(query)`, `orders(slug)`; `Price(median, low_sell, volume, fetched_at)`; respects the WFM rate limit |
| `data/worldstate.py` | world state | `state(max_age=60) -> dict` (cycles, fissures, invasions, sortie, archon, nightwave, baro, steel path, circuit), `arbi_schedule() -> list`; WFHelper `WorldState` shape with ISO dates plus `resets`, `circuit`, `zarimanCycle`, `fetchedAt`, `stale`, `error`, `source`; never raises (stale-on-error); `parse_raw(raw, now)` for tests; RPC `world.state(force)`; setting `world.enrich` |
| `data/arbi_schedule.py` | arbitration schedule + alerts | `get() -> ArbiSchedule` (`.payload(days)`, `.upcoming(days) -> [Entry]`, `.alerts()`, `.set_occurrence/.set_favorite/.set_lead`), `schedule(days) -> [dict]`; disk cache `<runtime>/arbi-sched-cache.json`, alerts `arbi-sched-alerts.json`, favourites mirrored to `arbitration.favorite_nodes`; RPCs `world.arbi_schedule(days)`, `world.arbi_refresh`, `world.arbi_set_occurrence(key, enabled)`, `world.arbi_set_favorite(node_id, enabled)`, `world.arbi_set_lead(minutes)`; event `arbi.schedule_alert` + toast |
| `data/solnodes.py` | node/mission/challenge labels | `node(id) -> Node(name, system, mission_type, mission, faction, min_level, max_level, label)`, `node_label(id)` ("Casta (Ceres)"), `node_mission(id)`, `node_faction(id)`, `faction_label(fc)`, `mission_type_label(mt, node_id)`, `challenge(path)`, `title_case`; generated from warframe-public-export-plus (`python -m wfh.data.solnodes build <pkg>`) |
| `inventory/__init__.py` | the player's inventory | `inventory() -> Inventory|None` (latest snapshot), `refresh()`, `owned_count(unique_name) -> int`, `owned_relics() -> dict[(relic_name, refinement), int]`, `status() -> dict`, `on_update(fn(Inventory))`, `import_file(path)`; `Inventory` has `data` (raw DE JSON), `origin`, `sync_time`, `owned_count()`, `relic_counts()`; sources: game memory (root) or a JSON file; RPCs `inventory.status`, `inventory.refresh`, `inventory.import_file(path)`; event `inventory.updated` |
| `features/*.py` | overlays and trackers | each has `setup(app)`, subscribes to `app.eelog`, draws with `app.overlays` |
| `relic_value.py` | relic EV / refinement / planner rows (pure) | `squad_ev(chances, values, n)`, `Valuer(price_of, ducats_of, owned_of)` / `default_valuer()` (cached prices only, never network), `relic_ev(relic, q, squad, valuer)`, `pick_best_owned_quality`, `quality_for_mode`, `refinement_table`, `build_rows(db, owned_relics, ...)`, `select_rows(rows, filters)`, `rank_by_ev`, `normalize_filters(push, prev)`, `tag_era` / `normalize_era` / `era_matches` |
| `features/relic_planner.py` | relic picker overlay (layer `planner`) | `planner()` → `RelicPlanner` (`.trigger(source, era)`, `.squad_size()`, `.state()`); RPCs `relics.planner_state`, `relics.planner_show(era)`, `relics.planner_hide`, `relics.prefetch_prices`; event `relics.planner` (scanning/rows/closed payload); settings `relic_planner.*` (`filters` = pushed page filters) |
| `features/relic_rewards.py` (+ `reward_geometry`, `reward_scan`, `reward_enrich`, `reward_overlay`) | relic reward overlay (layer `rewards`) | `controller()` → `RewardController` (`.trigger(source)`, `.dispatch`, `.scan_file(path, ui_scale, show)`, `.last`, `.status()`); `reward_scan.RewardScanner(models_dir).scan(img, ui_scale) -> ScanResult`; `reward_geometry.card_rects(n, ui, size)` / `confident_layouts(px, ui)`; RPCs `rewards.last`, `rewards.status`, `rewards.scan_file(path, ui_scale, show)`, `rewards.scan_now`, `rewards.clear`; event `rewards.result` (items with plat/ducats/owned/set, `best_plat`, `best_ducats`, `failure`); settings `relic_rewards.*` |
| `features/arbitration.py` (+ `arbi_parser`, `arbi_tracker`, `arbi_analysis`, `arbi_overlay`) | arbitration run tracker + end-of-mission summary (layer `arbi`) | `controller()` → `ArbiController` (`.tracker`, `.show(payload)`, `.show_run(id)`, `.dismiss()`, `.status()`, `.last_summary`), also `app.services["arbitration"]`; `arbi_parser.ArbiParser` (`feed_line`, `finalize`), `compute_vitus_model`, `scenario_table`, `luck`; `arbi_tracker.ArbiRunTracker(base_dir)` (`process_line`, `runs`, `import_ee_log(path)`, `set_vitus/tags/notes`, `delete_run/log`); files `<runtime>/arbi-runs.json` + `arbi-logs/`; RPCs in `menus/arbitrations.py`: `arbi.runs`, `arbi.refresh`, `arbi.run(id)`, `arbi.status`, `arbi.qam`, `arbi.set_vitus(id, vitus)`, `arbi.set_tags(id, tags)`, `arbi.set_notes(id, notes)`, `arbi.delete_run(id)`, `arbi.delete_log(id)`, `arbi.export_log(id)`, `arbi.import_log(path?)`, `arbi.summary_show(id?)`, `arbi.summary_dismiss`; events `arbi.run_saved`, `arbi.run_skipped`, `arbi.summary`; settings `arbitration.tracking/enabled/duration_s/summary_position/summary_scale` |
| `features/riven_scanner.py` (+ `riven_state`, `riven_scan`, `riven_text`, `riven_grading`, `riven_data`, `riven_market`, `riven_overlay`) | riven overlay (layers `riven` left / `riven_right` right) | `controller()` → `RivenController` (`.on_line`, `.rescan()`, `.close()`, `.state()`, `.scan_file(path, profile)`); `riven_state.RivenLogStateMachine` (EE.log cycle/roll/choice/close/chat-link); `riven_scan.RivenScanner(models_dir, capture).scan(profile)` / `FitsInReader.read(rgb)`; `riven_text.parse_riven_stats(text)`; `riven_grading.grade_riven(weapon, stats)`, `goodrolls()` (sheet, 7-day cache in runtime dir), `decode_all_rivens(inventory)`; `riven_data.db()` (data/riven/riven.json.gz from warframe-public-export-plus; `python -m wfh.features.riven_data build <pkg> <out>`); RPCs `riven.state`, `riven.status`, `riven.rescan`, `riven.close`, `riven.set_weapon(name)`, `riven.weapons(query)`, `riven.similar(side, auctions)`, `riven.scan_file(path, profile, source_type)`, `riven.data_status`, `riven.data_refresh(force)`, `riven.goodrolls_refresh`; event `riven.state`; settings `riven_scanner.*` |
| `menus/rivens.py` | Rivens page | RPCs `rivens.owned(sort, weapon)`, `rivens.similar(id, auctions)`, `rivens.sheet(weapon)`; event `rivens.updated` |
| `menus/*.py` | view-model RPCs for pages | each has `setup(app)`; listed in `menus.SUBMODULES` |
| `menus/world.py` | World page/QAM view-models + dailies tracker | pure: `fissure_rows(fissures, now)` (sorted tier/expiry, `sourceMode` normal/steel/railjack, `expiryMs`), `filter_fissures(rows, mode, tiers, limit)`, `fissure_counts(rows)`, `cycle_rows(state, now)` (urgent < 20 %), `timers(state, now)`, `baro_view`, `world_view`, `qam_view`, `auto_tracker(inv, state, now, inv_ms)`, `dailies_view`, `DailiesStore` (`<runtime>/world-dailies.json`); RPCs `world.view(force)`, `world.qam`, `world.ui_set(fissure_mode?, fissure_tiers?, qam_limit?)`, `world.dailies`, `world.dailies_set(id, count)`, `world.dailies_hide(id, hidden)`; settings `world_ui.*` |
| `data/wfm_auth.py` | warframe.market account (header JWT auth) | `WfmAuth(settings_dir, transport, platform, ws_factory)`: `.sign_in(email, password)`, `.sign_out()`, `.state()`, `.request(method, path, body, v)`, `.refresh_me()`, `.refresh_status()`, `.set_status(status, hold_min)` (one-shot stdlib websocket), `.my_orders(item_by_id)`, `.close_order/.delete_order/.update_order`; `WfmError(code)`, `WfmUnauthorized` (401 signs out); token in `<settings>/wfm.session` (0600), password never stored |
| `menus/market.py` | Market page/QAM view-models | pure: `whisper(side, entry, item_text)`, `item_text`, `detail(market, slug, rank)`, `RecentStore` (`<runtime>/market-recent.json`), `recent_rows` (cache only + prefetch), `order_counts`, `close_quantity`, `apply_close`; `auth()` / `set_auth()`; RPCs `market.detail(slug, rank?, record)`, `market.recent`, `market.recent_remove(slug)`, `market.recent_clear`, `market.qam`, `market.account(refresh)`, `market.sign_in(email, password)`, `market.sign_out`, `market.set_status(status, hold_min?)`, `market.my_orders(force)`, `market.order_close(id, quantity?)`, `market.order_delete(id)`, `market.order_visible(id, visible)`, `market.orders_visible(visible, type?)`, `market.order_update(id, platinum?, quantity?)`; events `market.orders`, `market.account`; settings `market.qam_recent`, `market.status_hold_min` |
| `menus/dashboard.py` | Dashboard summary | `foundry_from_qam(q, limit)` (menus/foundry.qam_view: claim / building / ready to build; `foundry_summary(inv, now, limit)` PendingRecipes fallback), `mission_summary` (last mission from menus/missions), `build_summary(...)`; RPCs `dashboard.summary`, `dashboard.configure(fissure_limit?, foundry_limit?, hidden?)`; settings `dashboard.*` |
| `inventory_view.py` | parseInventory / parseFoundry ports (pure) | `get_db()` → `Db` (WFHelper itemDb view over `data.items` + `data/foundry/foundry.json.gz`: `.get(u) -> Entry`, `.name_of(u)`, `.recipe_by_product`, `.blueprint_to_product`), `extra()`; `parse_inventory(data, db, market_refs)`, `build_full_set_items`, `parse_resources`, `parse_foundry`, `classify_for_foundry`, `chain_buildable_blueprints`, `is_foundry_recipe_ready`, `compute_pinned_totals`, `compute_value_totals(rows, scope, min_plat)`, `select_value_strip_rows`, `matches_shared_filters`, `sort_rows`, `MarketIndex(catalog)`, `build_base_items`, `attach_prices(rows, price_of)`, `market_price_of()` (cache only), `foundry_entries(foundry, owned, db, now_ms)`; `snapshot(inv, market_index, hide_claims, relic_db) -> Snapshot` cached per inventory sync; `python -m wfh.inventory_view build <pep pkg> <out>` (fetch-data.sh) |
| `menus/inventory.py` | Inventory page + QAM value | `build_view`, `build_item`, `qam_view`, `current_snapshot()`, `market_index()`; RPCs `inventory.view(tab, search, sort, direction, filters, offset, limit, everything_hidden, full_sets_hidden, show_incomplete)`, `inventory.item(key)`, `inventory.qam`, `inventory.prefetch_prices(tab?, limit?)`, `inventory.ui_set(value_scope?, value_min_plat?, hide_foundry_claims?, auto_prefetch?)`; settings `inventory_ui.*`; queues prime part prices after each sync |
| `menus/foundry.py` | Foundry page + QAM | `build_view`, `qam_view`; RPCs `foundry.view(tab, search, prime, state, vaulted, sort, direction, offset, limit)`, `foundry.pin(unique_name, pinned)`, `foundry.qam(limit)`; setting `foundry.pinned` |
| `menus/stats.py` | Stats page + QAM "Today" | `StatsTracker(runtime_dir, clock)` (`.on_inventory(data, synced_at)`, `.add_trade(parsed)`, `.today()`, `.series(ids, days)`, `.trade_page`), `read_amounts`, `parse_trade_dialog(lines)`, `TradeDialogWatcher(on_trade)` (EE.log file lines), `build_view`, `qam_view`, `tracker()`; files `<runtime>/stats-history.json`, `stats-trades.json`; RPCs `stats.view(days)`, `stats.qam`, `stats.trades(offset, limit)`, `stats.trade_delete(id)`, `stats.configure(chart?, days?, trade_tracking?)`, `stats.status`; event `stats.updated`; settings `stats.*` |
| `menus/wiki.py` | Wiki page (relic drop search) | pure `build_rows(relic_db)`, `search_rows(rows, query, mode, limit, unvaulted_only)`, `item_summaries`, `relic_detail`, `wiki_url`; RPCs `wiki.search(query, mode item|relic, limit, unvaulted_only, record)`, `wiki.relic(name)`, `wiki.relics(tier, unvaulted_only)`, `wiki.recent`, `wiki.recent_clear`; setting `wiki.recent` |
| `menus/settings_view.py` | Settings page form + diagnostics | `SCHEMA` / `FIELDS` (every user-facing setting with kind/range/options), `validate`, `set_value`, `reset_section`, `check_path`, `compose_launch_options(current)`; RPCs `settings.view`, `settings.set(key, value)`, `settings.reset(section)`, `settings.check_path(kind, path)`, `settings.diagnostics`, `settings.proton_plan(current?)` (a new user-facing setting should get a SCHEMA field) |
| `menus/mastery.py` | Mastery page + QAM (MR, collection, easy/relic/platinum roadmap, pins) | data `data/mastery.json.gz` (`python -m wfh.menus.mastery build <pep> <wfcd json> <out> [label]`, run by fetch-data.sh): `data()` → `MasteryData` (`.items` masterable rows `{u,n,cat,max,rate,...}`, `.blueprints`, `.regions`); pure `rank_to_xp`, `xp_to_rank`, `compute_progress(inv, md)`, `part_states`, `easy_access`, `relic_completion`, `estimate_purchase_cost`, `build_roadmap`, `summary`; `MasteryService`; RPCs `mastery.summary`, `mastery.items(category, status, sort, offset, limit, query)`, `mastery.item(unique_name)`, `mastery.roadmap(limit)`, `mastery.planned`, `mastery.pin(unique_name, pinned)`, `mastery.qam(limit)`, `mastery.configure(hide_founder?)`; event `mastery.updated`; settings `mastery.hide_founder`, `mastery.pinned` |
| `menus/syndicates.py` | Syndicates page + QAM | table `menus/syndicate_ranks.json` (WFHelper syndicateRanks.json, MIT); pure `syndicate_status`, `daily_cap`, `plan_steps`, `aggregate_needs`, `alignment_conflicts`, `view_model`, `qam_model`; RPCs `syndicates.view`, `syndicates.qam(limit?)`, `syndicates.set_goal(tag, level)`, `syndicates.clear_goals`; event `syndicates.updated`; settings `syndicates.goals` (`[[tag, level]]`), `syndicates.qam_limit` |
| `menus/missions.py` | Missions page + QAM (rewards per mission from inventory diffs) | `MissionTracker` (EE.log end/type/node + `inventory.on_update` diff vs pre-mission baseline, `<runtime>/mission-rewards.json`), pure `snapshot`, `diff`, `reward_rows`, `page`; `tracker()`; RPCs `missions.page(period, since_ms?, mission_type?, offset, limit)`, `missions.last`, `missions.qam(since_ms?, rows)`, `missions.status`, `missions.set_enabled(enabled)`, `missions.set_toast(enabled)`, `missions.delete(id)`, `missions.clear`; event `missions.updated`; settings `missions.enabled`, `missions.toast` |

Bundled data lives in `data/` at the plugin root (`app.data_dir`), produced at
build time by `scripts/fetch-data.sh` from the `@wfcd/items` npm package
(trimmed JSON: `items.json.gz`, `relics.json.gz`, `manifest.json`, ~330 KB). Runtime
refreshes (weekly, from WFCD's GitHub JSON, setting `data_auto_refresh`) go to
`<runtime_dir>/data`; the copy built from newer source data wins. RPCs: `data.status`,
`data.refresh`, `items.search`, `relics.get`, `market.search`, `market.item`, `market.prices`.
`data/foundry/foundry.json.gz` (~160 KB, from warframe-public-export-plus, bundle only, no
runtime refresh) holds foundry recipes, DE export categories, productCategory, mod/arcane max
rank and names for `wfh.inventory_view`.

Models live in `models/` (`app.models_dir`): `paddle/ch_PP-OCRv3_rec_infer.onnx`,
`paddle/ch_dict.txt`, `yolo/stat_line_detector.onnx`.

## Frontend map (src/)

- `index.tsx` registers the QAM panel (`qam/QamRoot.tsx`) and the full-screen
  route `/wfhelper` (`FullPage.tsx`, tabs from `registry.tsx`).
- Each page is `src/pages/<Name>.tsx` with a default export (full page) and an
  optional named export `Qam` (compact section shown in the QAM home).
- Shared UI bits go in `src/components/`. Data comes from `useRpc(method,
  params, deps, pollMs)`; no network calls from the frontend.
- Gamepad first: every interactive element must be focusable (`Focusable`,
  `DialogButton`, `ButtonItem`, `ToggleField`, `DropdownItem`, `TextField`).
  Lists longer than ~30 rows are paged or virtualised.

## Overlay conventions

- Screen space is the game output (1280x800 on Steam Deck LCD). Scanners map
  capture coordinates to overlay coordinates with one uniform scale
  `min(ow/cw, oh/ch)` plus centring offsets (gamescope letterboxes a buffer of
  another aspect; render-size screenshots come at the buffer's size). Use
  `reward_overlay.capture_to_overlay`.
- Layer names: `rewards`, `planner`, `riven`, `riven_right`, `arbi`, `test`.
- Every overlay has a setting section with `enabled` and honours
  `overlays_enabled`. Overlays never block: they are click-through and timed.
- When the overlay window is unavailable, fall back to a short `app.toast`.
- The window connects only to a gamescope session (`GAMESCOPE_WAYLAND_DISPLAY`
  set); in Desktop mode the manager stays disconnected and toasts are used.
- gamescope shows one external overlay at a time, so the Steam performance
  overlay (mangoapp) is hidden while one of our layers is up; hiding the
  window clears `GAMESCOPE_EXTERNAL_OVERLAY` so mangoapp gets the slot back.

## Tests

`scripts/test.sh` runs pytest on the bundled runtime with `WFH_HEADLESS=1`.
Fixtures from WFHelper (MIT) are copied into `tests/fixtures/`. Network is not
available in CI-like runs: data modules must accept recorded JSON fixtures.

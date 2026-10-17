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
| `menus/*.py` | view-model RPCs for pages | each has `setup(app)`; listed in `menus.SUBMODULES` |

Bundled data lives in `data/` at the plugin root (`app.data_dir`), produced at
build time by `scripts/fetch-data.sh` from the `@wfcd/items` npm package
(trimmed JSON: `items.json.gz`, `relics.json.gz`, `manifest.json`, ~330 KB). Runtime
refreshes (weekly, from WFCD's GitHub JSON, setting `data_auto_refresh`) go to
`<runtime_dir>/data`; the copy built from newer source data wins. RPCs: `data.status`,
`data.refresh`, `items.search`, `relics.get`, `market.search`, `market.item`, `market.prices`.

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
  capture coordinates to overlay coordinates by `overlays.size / capture.size`.
- Layer names: `rewards`, `planner`, `riven`, `arbi`, `test`.
- Every overlay has a setting section with `enabled` and honours
  `overlays_enabled`. Overlays never block: they are click-through and timed.
- When the overlay window is unavailable, fall back to a short `app.toast`.

## Tests

`scripts/test.sh` runs pytest on the bundled runtime with `WFH_HEADLESS=1`.
Fixtures from WFHelper (MIT) are copied into `tests/fixtures/`. Network is not
available in CI-like runs: data modules must accept recorded JSON fixtures.

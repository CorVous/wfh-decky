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
| `eelog.py` | EE.log + Proton debug log tail | `EELogTailer.subscribe(fn(line))`, `.feed(line)` (tests), `find_ee_log()`, `steam_libraries(home)`; `eecfg.py`: `ui_scale()` from EE.cfg |
| `overlay/manager.py` | the overlay window | `app.overlays.show(name, rgba_img, x, y, ttl=None, z=0)`, `.clear(name)`, `.size` (w, h) |
| `overlay/draw.py` | look and feel | `Card`, `Cell`, `badge`, palette constants, `font(size, bold)`, `fmt_plat` |
| `ocr/paddle.py` | text recognition | `PaddleRecognizer.get(models_dir)` singleton; `.recognize(crops: list[np.ndarray HxWx3 uint8]) -> list[(text, confidence)]` |
| `ocr/yolo.py` | riven stat line detector | `StatLineDetector.get(models_dir)`; `.detect(img_rgb) -> list[Box]` |
| `ocr/image.py` | shared image ops | otsu threshold, row splitting, resize helpers used by scanners |
| `data/items.py` | item database | `items()` → `ItemDb` with `.by_unique(uniqueName)`, `.by_name(name)`, `.prime_parts()`, `.match_name(ocr_text) -> (Item, score)`; `Item` has `unique_name, name, category, ducats, vaulted, tradable, wfm_slug, components, mastery_req, image_name` |
| `data/relics.py` | relic database | `relics()` → `RelicDb` with `.get(name)`, `.all()`, `Relic(name, tier, vaulted, rewards=[Reward(item_unique, item_name, rarity, chance_by_refinement)])` |
| `data/market.py` | warframe.market | `price(slug, rank=None) -> Price|None` (cached, fresh 30 min), `prices(slugs) -> dict`, `prefetch(slugs)` (background), `search(query)`, `orders(slug)`; `Price(median, low_sell, volume, fetched_at)`; respects the WFM rate limit |
| `data/worldstate.py` | world state | `state(max_age=60) -> dict` (cycles, fissures, invasions, sortie, archon, nightwave, baro, steel path, circuit), `arbi_schedule() -> list` |
| `inventory/__init__.py` | the player's inventory | `inventory() -> Inventory|None` (latest snapshot), `refresh()`, `owned_count(unique_name) -> int`, `owned_relics() -> dict[(relic_name, refinement), int]`, `status() -> dict`; sources: game memory (root) or a JSON file |
| `features/*.py` | overlays and trackers | each has `setup(app)`, subscribes to `app.eelog`, draws with `app.overlays` |
| `menus/*.py` | view-model RPCs for pages | each has `setup(app)`; listed in `menus.SUBMODULES` |

Bundled data lives in `data/` at the plugin root (`app.data_dir`), produced at
build time by `scripts/fetch-data.sh` from the `@wfcd/items` npm package
(trimmed JSON). Runtime refreshes go to `app.runtime_dir`.

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

# WFHelper for Decky

A [Decky Loader](https://decky.xyz) plugin that brings
[WFHelper](https://github.com/WFHelper/WFHelper)'s Warframe overlays and menus to
SteamOS game mode on the Steam Deck.

## What you get

In-game overlays, drawn on top of Warframe by gamescope:

- **Relic rewards**: prices every reward card when the squad's relics crack,
  with ducats, owned counts, vaulted status and the best pick highlighted.
- **Relic planner**: ranks the relics you own by expected value on the relic
  selection screen.
- **Riven scanner**: reads the rolls while you reroll and compares the old and
  new stats with grades and market value.
- **Arbitration summary**: your run stats as soon as the mission ends.

In the Quick Access Menu (one summary at a time, picked from a dropdown) and as
full-screen pages: Dashboard, World (cycles, fissures, invasions, sortie,
archon, nightwave, Baro, Circuit), Arbitrations (schedule, favourite-node
alerts, run history), Market (price search, orders, optional sign-in),
Inventory, Relics, Foundry, Mastery, Syndicates, Missions, Rivens, Stats, Wiki
(drop search) and Settings.

## Install

1. Download `WFHelper.zip` from the latest build (Actions artifact or release).
2. In game mode open Decky's settings, turn on **Developer mode**, then use
   **Developer → Install plugin from ZIP** and pick the file.
3. Open WFHelper in the Quick Access Menu and press **Show test overlay**.
   You should see a card at the top of the screen.

Recommended: add `PROTON_LOG=1 %command%` to Warframe's launch options
(Settings page has a button for it). Without it the game writes its log
late and overlays can appear several seconds after the screen they belong to.

Set Warframe's interface language to English: the scanners read English names.

## How it works

- `main.py` runs inside Decky and only relays calls to a worker process.
- The worker (`py_modules/wfh`) runs on a bundled CPython 3.12 with numpy,
  onnxruntime, Pillow and python-xlib, because Decky's embedded Python cannot
  load those.
- Overlays are an X11 window flagged `GAMESCOPE_EXTERNAL_OVERLAY` (the same
  mechanism MangoHud's mangoapp uses), so gamescope composites them over the
  game. They never take input.
- Scanners take gamescope screenshots of the game plane only
  (`gamescopectl screenshot`), then run WFHelper's PaddleOCR and YOLO models.
- Game events come from `EE.log` and the Proton debug log in the game prefix.
- Inventory comes from the running game (the plugin runs as root, so no
  `ptrace_scope` change is needed) or from an imported JSON file.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the module map and
[docs/port/](docs/port) for the per-feature port notes.

## What differs from WFHelper on the Deck

| WFHelper (desktop) | Here | Why |
|---|---|---|
| Global hotkeys, draggable overlay editor, click-through toggle | Quick Access Menu buttons and fixed layouts sized for 1280x800 | gamescope has no global hotkeys and overlays cannot take input |
| Desktop notifications | Steam toasts | Game mode has no notification daemon |
| DXGI / portal / compositor screen capture | gamescope screenshots | The only capture path in game mode |
| WFHelper's own price snapshot service | warframe.market's public API directly, cached and rate limited | The snapshot service belongs to WFHelper |
| Windows-only settings (tray, sounds, injection blocking) | Dropped | Not applicable |

Known limits: the screen crop positions for 16:10 were derived from WFHelper's
16:9 geometry and checked on scaled test images, not yet on real Deck
captures. `rewards.scan_file` and the debug capture setting help tune them.

## Building

```
pnpm install
./scripts/package.sh      # bundles runtime, models, game data → out/WFHelper.zip
./scripts/test.sh         # Python tests on the bundled runtime
pnpm run typecheck
```

## License

MIT. Ported logic and the OCR models come from WFHelper (MIT); see LICENSE.

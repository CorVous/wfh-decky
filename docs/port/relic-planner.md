# Port spec: Relic Planner overlay

Source: WFHelper (MIT), cloned at `/home/claude/wfhelper-src`. Every `file:line` below is relative to that root.

**What it does.** When the player opens the in-game relic picker (the void fissure "select a relic" screen, or the orbiter relic screen), WFHelper shows a panel of the relics the player **owns**. The panel is limited to the era the game is asking for (Lith/Meso/Neo/Axi/Requiem, or every era except Requiem for Omnia fissures). Each relic gets the expected platinum and ducat value of opening it at the best owned refinement, for a given squad size, and the list is ranked by that value. The panel shows the relic count, name, refinement, vaulted tag, expected plat, expected ducats, and (hidden by default) up to 6 rewards with chance and owned count.

It does **not** OCR relic names or read the grid. It reads only:
1. EE.log lines, to detect the picker opening and closing and the fissure tier (`activeMissionTag`).
2. The parsed inventory, for owned relic counts per refinement and owned reward parts.
3. Optionally, an OCR read of the era label on the picker screen. This only runs when the EE.log tag is missing (orbiter, stale tag) or needs a cross-check.
4. Relic data (@wfcd/items Relics.json, which can be refreshed through the WFHelper backend) and prices/ducats (backend snapshot cache plus warframe.market statistics).

---

## 1. Pipeline overview

```
EE.log line ──► picker-open pattern ──(300 ms debounce, 3 s cooldown)──► onRelicSelectionTrigger("eelog")
                                                                           │
   activeMissionTag=VoidT<n> ─► logMissionTier (lith..omnia)               │
   EOM / Abort line ─► clears logMissionTier                               ▼
                                                    show overlay in "scanning" state (auto-hide 20 s)
                                                    if a cached era exists → send fallback rows at once
                                                    after 100 ms → runRefinement():
                                                         decide era (log tag / cache / desktop hint / OCR)
                                                         buildRecommendations(era) → rows
                                                         send rows; auto-hide 120 s (rows) or 4.5 s (none)
EE.log InitMapping line (≥800 ms after open) ──► onRelicSelectionClose → hide overlay, clear the OCR-era cache
```

Main files:

| Concern | File |
|---|---|
| Controller: era decision, ranking, EV, cache | `ipc/overlay/relicSelection.ts` (1–1082) |
| Wiring, window, close, filter push, price IPC | `ipc/rewardOverlayIpc.ts` (98–124, 189–199, 239–272, 287–357) |
| EE.log patterns and debounce | `services/eeLogMonitor.ts` (55–123, 179–193, 335–398, 438–535) |
| Era OCR | `services/rewardScannerEra.ts`, `services/rewardScannerSupport.ts:27–53`, `services/rewardScannerMatch.ts:11–17, 563–704`, `services/rewardScannerImage.ts:183–209, 233–296, 343–363` |
| Relic DB | `services/relicService.ts` |
| Relic DB refresh | `services/relicDataUpdate.ts`, `config/shared/wfcdRelicRules.ts`, `backend/worker/src/services/wfcdRelics.ts`, `backend/worker/src/routes/public.ts:610–628` |
| Filters and sort shared with the desktop planner | `config/shared/relicPlannerView.ts` |
| Search | `config/shared/relicSearch.ts` |
| Reward order | `config/shared/relicRewardOrder.ts` |
| Rarity fix | `services/relicRarity.ts` |
| Inventory counts | `config/shared/relicCounts.ts` |
| Overlay UI | `renderer/overlay.js:384–650`, `renderer/overlay.css:150–410`, `config/shared/overlayLayout.ts:365–447, 560–574` |

---

## 2. Detection trigger (EE.log)

### 2.1 EE.log location

These paths are in `services/eeLogPath.ts:13–200`.

- **Windows:** `%LOCALAPPDATA%\Warframe\EE.log`.
- **Linux (Proton, Steam):** for each Steam library, the path is:
  `<lib>/steamapps/compatdata/230410/pfx/drive_c/users/steamuser/AppData/Local/Warframe/EE.log` (`eeLogPath.ts:39–53`).
  - Steam roots are probed in this order: `~/.local/share/Steam`, `~/.steam/steam`, `~/.steam/root`, the Flatpak root `~/.var/app/com.valvesoftware.Steam/.local/share/Steam`, and the Snap root.
  - Libraries come from `steamapps/libraryfolders.vdf`, parsed with the regex `/"path"\s+"([^"]*)"/g` (`eeLogPath.ts:31`).
  - Every `compatdata/*` id is also scanned, for the non-Steam-shortcut case (`eeLogPath.ts:171–174`).
  - When nothing is verified, the path is re-probed after `LINUX_REPROBE_MS`.
- **Decky:** the backend runs as root, so `~` is `/root`. Resolve the deck user's home instead (`/home/deck`, or `DECKY_USER_HOME`). On a Deck, the usual hits are `/home/deck/.local/share/Steam/steamapps/compatdata/230410/...` or `/run/media/<sd>/steamapps/compatdata/230410/...`.

### 2.2 Line sources

There are two sources (`eeLogMonitor.ts:799–814`):

- **File poll.**
  - The file is polled every `POLL_INTERVAL_MS = 500` ms, plus a chokidar `change` event.
  - Each read is at most 256 KiB, with up to 8 loops per tick.
  - Lines are split on `\n`.
  - The engine flushes EE.log lazily, so a line can reach the file **10–15 s late**. The real-time source exists because of this.
- **Real-time source.**
  - Windows uses DBWIN (OutputDebugString).
  - Linux uses `protonDebugstrMonitor`, which tails `~/steam-230410.log` (override: `WFHELPER_PROTON_LOG`) and parses Wine lines matching `/(?:trace|warn):(?:debugstr|seh):OutputDebugString[AW]\s+(L?)"/`.
  - The Proton log only exists when the user adds `PROTON_LOG=1 %command%` to the launch options. The source counts as active only if the log grew in the last 30 s.
  - While a real-time source is active, picker open/close lines **from the file poll are ignored**, because they would be delayed echoes (`eeLogMonitor.ts:438–444, 516–520`).

**Decky recommendation:** support both sources.
- Default to the file poll. Optionally offer a toggle that tells the user to add `PROTON_LOG=1 %command%`.
- Note that `PROTON_LOG` writes to `$HOME/steam-230410.log` of the **game's** user (`/home/deck`).

### 2.3 Exact patterns (`services/eeLogMonitor.ts`)

```ts
// Picker OPEN (any match) — eeLogMonitor.ts:66-72
/\bThemedProjectionManager\.lua:\s*LoadingCompleteEnd\b/i
/\bProjection[A-Za-z_]*\.lua:\s*LoadingCompleteEnd\b/i
/\bThemedProjectionManager\.lua:\s*PopulateInventoryGrid\b/i
/\bProjectionManager\.lua:\s*PopulateInventoryGrid\b/i
/\bProjection[A-Za-z_]*\.lua:\s*PopulateInventoryGrid\b/i

// Picker CLOSE — eeLogMonitor.ts:74-76
/\bInitMapping for all devices with bindings\b/i

// Fissure tier — eeLogMonitor.ts:179-185 (key=value and JSON forms)
/(?:^|["\s])activeMissionTag["\s]*[=:]\s*"?([A-Za-z0-9_]+)/
//   only tested when line.includes("activeMissionTag"); capture group 1 = tag
//   examples: "    activeMissionTag=VoidT6", '    "activeMissionTag" : "VoidT5",', '{"activeMissionTag":"VoidT2"}'

// Mission end — clears the tier — eeLogMonitor.ts:189
/Sys \[Info\]: EOM missionLocationUnlocked=|TopMenu\.lua: Abort:/
//   example: "234.503 Sys [Info]: EOM missionLocationUnlocked=1"
//   example: "234.503 Script [Info]: TopMenu.lua: Abort: host/no session"
//   → callback("EndOfMission"), which is not a void tag and so clears logMissionTier
```

The tag-to-era mapping (`ipc/overlay/relicSelection.ts:203–210`) is applied to `tag.trim().toUpperCase()`:

| Tag | Era |
|---|---|
| VOIDT1 | lith |
| VOIDT2 | meso |
| VOIDT3 | neo |
| VOIDT4 | axi |
| VOIDT5 | requiem |
| VOIDT6 | omnia |
| anything else | clears `logMissionTier` (logged as "mission tier cleared (non-fissure tag …)") |

### 2.4 Timing constants and state machine (`eeLogMonitor.ts:105–123, 335–398, 515–535`)

| Constant | Value | Meaning |
|---|---|---|
| `RELIC_TRIGGER_DELAY_MS` | 300 | Debounce between the pattern match and dispatch, so the UI can render |
| `RELIC_PICKER_COOLDOWN_MS` | 3000 | Pattern lines within 3 s of the last dispatch are ignored |
| `RELIC_PICKER_CLOSE_COOLDOWN_MS` | 500 | Minimum gap between two close dispatches |
| `RELIC_PICKER_ENTRY_WINDOW_MS` | 800 | An InitMapping line within 800 ms of open is part of *entry*, not a close (it can fire twice, about 3 ms apart) |
| `REWARD_AFTER_PICKER_SUPPRESS_MS` | 3000 | Reward-screen triggers are suppressed for 3 s after any picker pattern line, because the picker shows reward-preview cards |

Open:
1. On a pattern match, always set `lastRelicPickerPatternAt = now`.
2. If `now - lastRelicPickerAt < 3000`, return.
3. If a timer is pending, return.
4. Otherwise, after 300 ms: `lastRelicPickerAt = now`, `relicPickerSessionOpen = true`, call `onRelicSelectionOpen()`.

Close: when an InitMapping line arrives and all of the following hold, call `onRelicSelectionClose()` and set `relicPickerSessionOpen = false`:
- no riven session is active,
- `relicPickerSessionOpen` is true,
- `now - lastRelicPickerCloseAt >= 500`,
- `now - lastRelicPickerAt >= 800`.

### 2.5 Controller entry: `onRelicSelectionTrigger(source)` (`relicSelection.ts:940–1010`)

- `source = "eelog"`:
  - Return if `overlayDismissedUntilMs > now`. When the user closes the panel, reopen is suppressed for `REOPEN_SUPPRESS_AFTER_CLOSE_MS = 3000`.
  - Return if `now - lastEelogTriggerAt < MIN_EELOG_TRIGGER_GAP_MS (900)`.
  - Return if `overlaySettings.autoTriggerEnabled` is false.
- The overlay is gated by the setting `relicRecommendationOverlayEnabled` (`rewardOverlayIpc.ts:244`).
- `activeScanToken += 1`. Every async step checks `scanToken === activeScanToken` and drops stale work. A new trigger replaces an in-flight scan.
- Show the window, send `relic-planner-trigger {source}` so the renderer shows the scanning state, and schedule auto-hide at 20 s (`OVERLAY_AUTO_HIDE_DETECTING_MAX_MS`).
- `cachedEra = logMissionTier || (activeMissionTier if younger than RELIC_MISSION_TIER_CACHE_TTL_MS = 25 min)` (`config/runtime/cacheConfig.ts:14`). If it is set, call `sendFallbackRows(cachedEra)` immediately, so rows appear without OCR.
- After `ERA_DETECTION_START_DELAY_MS = 100`, call `runRefinement()`.

`onRelicSelectionClose()` (`rewardOverlayIpc.ts:263–272`):
- Calls `resetMissionTier()`. This clears only the OCR-derived `activeMissionTier`. `logMissionTier` survives, because the EE.log tag fires only on mission load.
- Hides the window and clears the auto-hide timer.

The close button (`OVERLAY_CLOSE`, `rewardOverlayIpc.ts:287–303`) calls `suppressReopenForClose()`, which sets `overlayDismissedUntilMs = now + 3000`, and hides the window.

---

## 3. Era decision (`runRefinement`, `relicSelection.ts:741–938`)

Constants (`relicSelection.ts:28–41`):

```
RECOMMENDATION_SQUAD_SIZE        = 4      // squad size used before the desktop planner ever pushes filters
RECOMMENDATION_CACHE_TTL_MS      = 10_000
MIN_EELOG_TRIGGER_GAP_MS         = 900
ERA_DETECTION_TIMEOUT_MS         = 2400
ERA_DETECTION_LABEL_TIMEOUT_MS   = 1500
ERA_DETECTION_TOTAL_TIMEOUT_MS   = 3000
ERA_DETECTION_RETRY_DELAY_MS     = 700
ERA_DETECTION_MIN_RETRY_MS       = 600
ERA_DETECTION_START_DELAY_MS     = 100
REOPEN_SUPPRESS_AFTER_CLOSE_MS   = 3_000
OVERLAY_AUTO_HIDE_SUCCESS_MS     = 120_000
OVERLAY_AUTO_HIDE_FAILURE_MS     = 4_500
OVERLAY_AUTO_HIDE_DETECTING_MAX_MS = 20_000
```

Algorithm:

1. Start with `era = logMissionTier || (activeMissionTier if age < 25 min)`. If an era is set, `conf = 1.0`.
2. **If `era` came from the EE.log tag:**
   - Refresh `activeMissionTierSetAt`.
   - Run OCR in **labelOnly** mode with a 1500 ms budget. Only the filter-tab label is read.
   - If the label read gives `labelEra` with `conf >= 0.9` and `candidateId === "filter-label"`, and `labelEra != era` and `labelEra != "requiem"`, the label **overrides** the tag: set `era = labelEra` and cache it as `activeMissionTier`.
   - The tag can be wrong in the orbiter after a mission. A Requiem label never overrides, because only a Requiem fissure can offer Requiem relics.
3. **Else if `era` came only from the 25-min cache:** do no OCR. WFHelper calls `captureSourceMeta` only to learn which display to anchor on; this is not needed on Deck.
4. **Else if the desktop planner pushed a tier hint:** set `era = hint`, `conf = 0.75`, and do no OCR.
5. **Else (no knowledge):**
   - Run full OCR detection with a 2400 ms budget.
   - If the read is empty and `retryBudget = 3000 - elapsed - 700 >= 600`, wait 700 ms and retry once with timeout `min(2400, retryBudget)`.
   - If the result has `era`, `conf >= 0.9` and `candidateId === "filter-label"`, cache it: set `activeMissionTier` and `activeMissionTierSetAt = now`.
6. Set `effectiveEra = (era && conf >= 0.9) ? era : desktopTierHint`. The hint may be null, which means all eras.
   - The 0.75 hint path in step 4 therefore still resolves to the hint through this fallback.
7. Build the recommendations for `effectiveEra`.
   - If the user closed the overlay during the scan (`overlayDismissedUntilMs > scanDismissBaselineMs`), drop the result.
8. Send `relic-recommendations`. Auto-hide after 120 s if there are rows, otherwise after 4.5 s.
9. On an exception, send `{source, era:null, rows:[], ocrUnavailable}` and auto-hide after 4.5 s.

`normalizeEra(value)` (`relicSelection.ts:183–195`):
1. Lowercase and trim the value.
2. Check substrings **in this order**: `requiem`, `lith`, `meso`, `neo`, `axi`, `omnia`.
3. Return the first match, or null.

**Self-read guard** (`relicSelection.ts:197–200, 688–706`):
- After each send, remember a signature for each row: `normalizeOcrPhrase(row.label)`, kept only when it has 3 or more words. The label format is `"<count>x <Relic Name> <Quality>"`.
- If an OCR `textPreview` (normalised) contains any remembered signature, the read is of our own overlay. Discard it by setting `era = null` and `confidence = 0`.
- Keep this on Deck if the screenshot can include the external overlay plane.

---

## 4. Era OCR (`services/rewardScannerEra.ts`)

On Deck, the capture is the gamescope screenshot (1280×800 PNG). WFHelper's Linux capture returns the game window content (`screenCapture.ts:479–495`).

### 4.1 Crop geometry (`services/rewardScannerSupport.ts:27–53`)

Normalized to the **captured image**:

```ts
// 1) Filter-tab label above the relic grid — tried first; the only source allowed to cache/override
RELIC_ERA_FILTER_LABEL_RECTS = [{ id: "filter-label", x: 0.02, y: 0.085, width: 0.19, height: 0.058 }]

// 2) Relic tile labels (5 columns, lower half)
RELIC_ROW_TILE_LABEL_RECTS = [
  { id: "slot-1", x: 0.02, y: 0.5, width: 0.18, height: 0.42 },
  { id: "slot-2", x: 0.20, y: 0.5, width: 0.18, height: 0.42 },
  { id: "slot-3", x: 0.38, y: 0.5, width: 0.18, height: 0.42 },
  { id: "slot-4", x: 0.56, y: 0.5, width: 0.18, height: 0.42 },
  { id: "slot-5", x: 0.74, y: 0.5, width: 0.18, height: 0.42 },
]   // candidateId = "tile-slot-N"

// 3) Full-width header bands
RELIC_ERA_BANDS = [ { top: 0.12, height: 0.12 }, { top: 0.16, height: 0.13 }, { top: 0.20, height: 0.14 } ]
// candidateId = "header-band"
```

How each crop is scaled (`services/rewardScannerImage.ts`):

- **`cropRect` (label and tiles, `:194–209`)** has **no aspect correction**. It uses plain fractions of the full frame:
  - `x = floor(W*x)`, `y = floor(H*y)`
  - `w = max(24, floor(W*w))`, `h = max(24, floor(H*h))`
  - Each ratio is clamped: x and y to [0, 0.98], width and height to [0.02, 1-x] and [0.02, 1-y].
- **`cropBand` (header bands, `:183–192`)** uses the **16:9-centred canvas** rule:
  - `REFERENCE_ASPECT = 16/9` (`:343`).
  - `scaleY = (W/(16/9)) / H` if that is less than 1, else 1.
  - `top = floor(H*(0.5 + (top-0.5)*scaleY))`
  - `h = max(18, floor(H*height*scaleY))`
  - The band is always full width.

**16:10 on the Deck (1280×800).**
- `scaleY = 720/800 = 0.9`.
- The 16:9 canvas is 1280×720, centred at y=40.
- WFHelper's ratios were measured on 16:9. Whether Warframe's relic-picker UI at 16:10 is anchored to the full 800-px frame or to a centred 720-px canvas is **unverified**. Calibrate from a real Deck screenshot.

Pixel boxes at 1280×800 (x, y, w, h):

| Crop | Full-frame (WFHelper's cropRect behaviour) | 16:9-canvas variant (y offset 40, H=720) |
|---|---|---|
| filter-label | 25, 68, 243, 46 | 25, 101, 243, 41 |
| tile slot-1..5 | x = 25, 256, 486, 716, 947; y=400, w=230, h=336 | same x, y=400, w=230, h=302 |
| band 1 | 0, 126, 1280, 86 (WFHelper already applies scaleY) | – |
| band 2 | 0, 155, 1280, 93 | – |
| band 3 | 0, 184, 1280, 100 | – |

**Recommendation:** for the filter label, crop both the full-frame box and the canvas box and keep the higher-confidence read. The label box is small, so the extra OCR cost is low.

### 4.2 Variants (`rewardScannerImage.ts:233–296, 1217–1230`)

Each crop is OCR'd as:
- `raw`: the crop as-is.
- `enhanced`:
  1. Upscale 2× (Lanczos/"best"), capped at 4096 in each dimension.
  2. Convert to luminance: `lum = (114*B + 587*G + 299*R + 500)/1000`.
  3. Apply the LUT `out = round(255 * clamp((lum-72)/(214-72), 0, 1) ^ 0.9)`.
  4. Output grey RGB with alpha 255.

### 4.3 Budget (`rewardScannerEra.ts:50–77, 307–367`)

- `timeoutMs` is clamped to [600, 12000], default 4500. The callers pass 2400, 1500, or the retry budget.
- `ocrTimeoutMs = max(900, 15000)`.
- Each OCR attempt gets `min(ocrTimeoutMs, remaining)`. After the first attempt, an attempt is skipped when `remaining < 900` (`MIN_ERA_OCR_ATTEMPT_MS`).

Order of work:
1. Filter label (raw, then enhanced).
2. If best confidence < 0.9 and not labelOnly: tiles 1 to 5.
3. If still < 0.9 and not labelOnly: bands.

Every stage stops early at confidence ≥ 0.99. The overall best (highest confidence) wins.

The result is `{era, confidence, textPreview (whitespace-collapsed, ≤240 chars), candidateId, bandTopRatio, bandHeightRatio, ocrVariant, elapsedMs}`.

### 4.4 OCR engine

- Windows uses Windows.Media.Ocr through `scripts/ocr.ps1`, with ONNX as the fallback. This is **Windows-only**.
- Linux uses only the ONNX path, `recognizeRewardStripOnnx` (`services/rewardOcrOnnx.ts:118–210`). It uses the PaddleOCR CH PP-OCRv3 recognizer at `<RIVEN_OCR_ASSET_DIR>/paddle/ch_PP-OCRv3_rec_infer.onnx` plus `ch_dict.txt` (`services/rivenOcrOnnx.ts:25–27`). The steps:
  1. Resize to width `min(srcW*3, 720)` with Lanczos.
  2. Convert to greyscale, normalise, apply an Otsu threshold, and treat pixels where `gray >= threshold` as ink (bright text on dark).
  3. Split rows by horizontal ink projection: `minInk = max(3, 1.5% of width)`, gap tolerance 2, maximum line height 72 (taller segments are split at the ink valley in the middle 50% if valley < 0.8×peak), minimum line height 10.
  4. Pad each row by 4 px vertically and 8 px horizontally, then recognise the RGB crop.
  5. Drop rows with confidence < 0.55.
  6. Clean each row: insert a space at lowercase-to-uppercase boundaries, then `[^A-Za-z0-9 &'-]` → space.
  7. Join the rows with spaces.
  - A timeout makes `boundedStrip` resolve to "" (`services/rewardScannerOcr.ts:259–273`).
- `ocrUnavailable` in the payload is `!windowsOcr && !onnxAvailable`. On Linux this is true only when the model files are missing.

**Deck:** reuse the shared reward-OCR ONNX worker. A 1280-wide crop becomes ≤720 px wide, which is a downscale, so the band OCR is cheap.

### 4.5 Text-to-era matching (`services/rewardScannerMatch.ts`)

Normalisers:
- `normalizeOcrPhrase(s)` (`config/shared/ocrPhrase.ts:4–10`): `s.toUpperCase().replace(/[^A-Z0-9\s]+/g," ").replace(/\s+/g," ").trim()`.
- `normalizeForOcr(s)` (`config/shared/textNormalize.ts:25–33`): `s.toUpperCase().replace(/[1|!]/g,"I").replace(/0/g,"O").replace(/5/g,"S").replace(/[^A-Z]/g,"").trim()`.

Era tokens (`:11–17`): `LITH→lith, MESO→meso, NEO→neo, AXI→axi, REQUIEM→requiem`.

`matchEraToken(word)` (`:563–582`):

| Condition | Confidence |
|---|---|
| Exact match | 1.0 |
| Otherwise, if `len >= 3` and either word is a prefix of the other | 0.82 |
| Otherwise, Levenshtein distance ≤ 1 | 0.76 |
| Otherwise | null |

`detectRelicEraFromText` (`:598–621`):
1. Split the phrase into words, apply `normalizeForOcr` to each, and keep words with length ≥ 2.
2. The first exact hit returns 1.0. Otherwise keep the highest-confidence hit.

Each of the three readers then adds its own rules:

- **Filter label** (`:626–647`):
  1. For each word: `ALL` or `OMNIA` → `omnia @ 1.0`.
  2. A 3-letter word that equals `ALL` after replacing `I` with `L` (for example "AII" or "A11") → `omnia @ 0.95`.
  3. Otherwise use `detectRelicEraFromText`, and keep the higher of the two results.
  - The selected tab reads "All" on Omnia screens, and every grid opens Lith-first.
- **Band** (`:653–662`): if `distinctEraMentions` finds more than one era, return null. Otherwise use `detectRelicEraFromText`.
- **Tile** (`:664–704`):
  1. Apply the same multi-era guard.
  2. Take the base confidence from `detectRelicEraFromText`.
  3. Add 0.08 if the phrase starts with `"LITH "`, `"MESO "`, `"NEO "`, `"AXI "` or `"REQUIEM "`.
  4. Add 0.06 if the phrase contains `" RELIC"`.
  5. Cap at 1.0.

`distinctEraMentions` (`:584–596`): the set of tokens hit by `matchEraToken`, plus `OMNIA`.

---

## 5. Relic data

### 5.1 Database model (`services/relicService.ts:23–52, 164–236`)

```ts
RelicDatabase = {
  groups: Record<groupKey, RelicGroup>,                       // groupKey = "Lith A1" (name minus refinement)
  byUniqueName: Record<relicUniqueName, { groupKey, quality: "intact"|"exceptional"|"flawless"|"radiant" }>
}
RelicGroup = { key, name /* "Lith A1" */, tier /* "Lith" */, code /* "A1" */, vaulted, imageUrl,
               qualities: { intact?: {uniqueName, rewards: RelicReward[]}, exceptional?, flawless?, radiant? } }
RelicReward = { name, uniqueName|null, imageUrl|null, rarity, chance /* percent, e.g. 25.33 */,
                urlName /* WFM slug */|null, wfmId|null, ducats|null }
```

Build, for each row of @wfcd `Relics.json`:

1. Split `name` on spaces. Skip the row if it has fewer than 3 parts.
2. `quality` = the last part, which must be one of `Intact`, `Exceptional`, `Flawless`, `Radiant`.
3. `tier` = the first part, which must be one of `Lith`, `Meso`, `Neo`, `Axi`, `Requiem`, `Vanguard`.
4. `baseName = parts[:-1].join(" ")` and `code = parts[1:-1].join(" ")`.
5. Set `group.vaulted` to the AND over its refinements of `isRelicVaulted(row)`, where `isRelicVaulted = row.vaulted && (row.dropCount ?? row.drops?.length ?? 0) == 0`.
   - This works around wfcd 1.1276.6, which flags Citrine relics as vaulted while still listing their drops.
6. Image: `toIconMirrorUrl("https://cdn.warframestat.us/img/" + imageName)`. The Intact image is preferred.
7. For each reward:
   - `name = r.item.name || "Unknown"`.
   - `slug = r.item.warframeMarket.urlName || .url_name`, then `normalizeWfmSlug(slug)`.
   - `uniqueName = r.item.uniqueName` unless it starts with `/Lotus/Types/Game/Projections/`. In that case (most wfcd rows carry the relic's own uniqueName), resolve it through the item DB by name or slug.
   - `rarity = relicRewardRarity(quality, chance, r.rarity)`.
   - `ducats = normalizeDucats(r.item.ducats)`, which rounds the value and returns null if it is < 0.
8. Map the relic `uniqueName` to `{groupKey, quality}`.

**Rarity correction** (`services/relicRarity.ts:5–36`): DE mislabels commons. A reward gets the table's rarity if `|chance - expected| < 0.5`; otherwise the fallback is kept.

| Refinement | Common | Uncommon | Rare |
|---|---|---|---|
| intact | 25.33 | 11 | 2 |
| exceptional | 23.33 | 13 | 4 |
| flawless | 20 | 17 | 6 |
| radiant | 16.67 | 20 | 10 |

Relic inventory uniqueNames end in the refinement suffix (`config/shared/wfcdRelicRules.ts:21–26`):

| Refinement | Suffix |
|---|---|
| Intact | `Bronze` |
| Exceptional | `Silver` |
| Flawless | `Gold` |
| Radiant | `Platinum` |

Example: `/Lotus/Types/Game/Projections/T1VoidProjection…Bronze`.

### 5.2 Source and refresh

1. **Bundled:** `node_modules/@wfcd/items/data/json/Relics.json` (package `^1.1276.6`; `services/bundledGameData.ts:125–146`). The version comes from that package's `package.json`.
2. **Downloaded:** `services/relicDataUpdate.ts`.
   - **Request:** `GET https://api.wfhelper.com/v1/wfcd-relics`.
     - The base can be overridden with `VITE_WFM_BACKEND_URL`.
     - No bootstrap token is needed (`backend/worker/src/routes/public.ts:610–628`).
     - Headers:
       ```
       Accept: application/json
       x-wfhelper-client: WFHelper/<appVersion>
       If-None-Match: <etag>        (when known)
       ```
   - **Response 200:**
     - Headers: `ETag: "wfcd-<version>-1"` and `Cache-Control: public, max-age=3600`.
     - Body (trimmed wfcd, about 4.9 MB):
       ```json
       {"ok":true,"version":"1.1276.6","publishedAt":"2026-…Z","generatedAt":"…",
        "relics":[{"uniqueName":"/Lotus/Types/Game/Projections/T1VoidProjectionXBronze","name":"Lith A1 Intact",
                   "vaulted":true,"imageName":"lith-relic.png","dropCount":0,
                   "rewards":[{"chance":25.33,"rarity":"Common",
                               "item":{"uniqueName":"…","name":"Akstiletto Prime Barrel","imageName":"…png","ducats":15,
                                       "warframeMarket":{"id":"…","urlName":"akstiletto_prime_barrel"}}}]}]}
       ```
   - **Other statuses:**
     - `304`: unchanged.
     - `404 {"ok":false,"error":"wfcd_relics_not_ready"}`: revert to the bundled data.
     - Anything else: ignore.
   - **Rate limit:** `PUBLIC_API_RATE_LIMITER`, 200 requests per 60 s (`backend/worker/wrangler.jsonc:55`).
   - **Timing:**
     - Fetch timeout is 60 s and covers the body.
     - The body is capped at `WFCD_RELICS_MAX_BYTES = 20 MiB`.
     - Refresh runs on startup (after applying the cache) and then every **6 h** (`REFRESH_INTERVAL_MS`).
   - **Validation** (`validateRelicDataDoc`, `relicDataUpdate.ts:88–157`; rules in `config/shared/wfcdRelicRules.ts`):
     - Version must match `^\d{1,9}\.\d{1,9}\.\d{1,9}$`.
     - Each row needs:
       - a uniqueName that starts with `/Lotus/Types/Game/Projections/` and ends with the suffix matching its refinement,
       - a name that matches `^(Lith|Meso|Neo|Axi|Requiem|Vanguard) \S+ (Intact|Exceptional|Flawless|Radiant)$`,
       - a boolean `vaulted`,
       - an integer `dropCount >= 0`,
       - 1–12 rewards.
     - Each reward needs:
       - `chance` in (0, 100],
       - `rarity` one of Common, Uncommon or Rare,
       - an item name of 1–64 characters,
       - every string ≤ 256 characters.
     - A row with any invalid reward is **dropped** (`readRelicRow`).
     - The whole document is **rejected** if the valid row count is outside [2500, 10000] or there are more than 2000 distinct reward names.
   - **Version rule:** the download is used only if it has the **same major version** as the bundled package and is **strictly newer** (`notNewerReason`, `:169–174`). Otherwise the app reverts to the bundled data.
   - **Cache:**
     - Stored at `<userData>/wfcd-relics.json` as `{etag, doc}`, written atomically. `doc: null` means "keep only the etag".
     - On start: read it (skip if larger than 20 MiB), validate it, and apply it if it is newer than the bundled data.
   - **After a change:** call `setDownloadedRelics()`, rebuild the DB lazily, and call `onRelicDatabaseChanged()`, which clears the planner row cache (`main.ts:715–722`).

**Decky:** ship a pre-trimmed Relics.json (or the full one) in the plugin. Use the same endpoint with ETag and 6 h polling, and cache it in `DECKY_PLUGIN_SETTINGS_DIR` or the runtime dir. Python port notes:
- Parse with `json` and apply the same validation.
- The 5 MB document parses in about 100 ms in CPython, so do it in the worker.

---

## 6. Owned relic counts (`config/shared/relicCounts.ts`, `relicSelection.ts:221–244`)

Input: the parsed inventory JSON (`ctx.currentInventoryData`, from the inventory sync; see the inventory spec).

1. For the collections `LevelKeys`, `MiscItems` and `Recipes`:
   - Sum `ItemCount` per `ItemType` **within** each collection. A missing `ItemCount` counts as 1; a non-finite or ≤ 0 value counts as 0; values are floored.
   - Across collections, take the **max** per ItemType.
2. If nothing was found, scan **every** top-level array the same way.
3. Keep only ItemTypes found in `db.byUniqueName`. Add each count to `owned[groupKey][quality]`.

The result is `owned: Record<groupKey, {intact, exceptional, flawless, radiant}>`.

Owned **reward** counts (`relicSelection.ts:663–686`): the per-reward "Owned N" uses this pipeline (details in the components spec):
1. `withoutFoundryPending(inventory, isReusableBlueprint)` removes blueprints already consumed by pending foundry recipes (`config/shared/foundryPending.ts:33–55`).
2. `aggregateComponentOwnership`:
   - `MiscItems` and `Recipes` are summed by `ItemCount` (default 1).
   - Each row of a built-gear collection counts as 1.
3. `ownedComponentCount(uniqueName)` takes the max over `componentUniqueNameAliases`.

---

## 7. Ranking algorithm

### 7.1 Prices and ducats (`relicSelection.ts:281–366, 531–549, 591–615`)

**Price per reward slug.** The slug is `normalizeWfmSlugKey`: lowercase, strip `'`, `’` and `‘`, replace `[^a-z0-9]+` with `_`, then trim `_` (`config/shared/textNormalize.ts:14–23`). Lookup order:
1. `snapshot-cache.json` → `prices[slug].median`, when `status` (default "ok") is `"ok"` and `median > 0`.
2. Otherwise `orderSummaries[slug]`: `wts` if > 0, else `wtb` if > 0, when the status is ok.
3. Otherwise the in-memory warframe.market statistics cache (`services/wfmStatsPrice.ts`). This is a 5-minute TTL with at most 5000 entries, filled by `GET /items/{slug}/statistics` elsewhere in the app. The planner never fetches.
4. Otherwise null.

**Ducats per reward:**
1. `reward.ducats` from wfcd, if > 0.
2. Otherwise `snapshot-cache.json.meta[slug].ducats`, if > 0.
3. Otherwise null.

Snapshot file details:
- Path: `<userData>/snapshot-cache.json`. It is written by the desktop renderer from backend `GET /v1/snapshot?client=closed-average-v3`; see the market spec.
- Shape: `{version:1, generatedAt, prices:{slug:{status,median,timestamp,priceBasis}}, meta:{slug:{slug,ducats,setRoot,thumb,icon,timestamp}}, orderSummaries:{slug:{status,wts,wtb,timestamp}}}`.
- Parsed maps are cached by file mtime.
- **Note:** the planner uses snapshot prices **regardless of age**. The 48 h `SNAPSHOT_PRICE_MAX_AGE_MS` gate applies only to `getSnapshotPrice()`, which serves the overlay's single-price IPC.

### 7.2 Expected value (`computeSquadExpected`, `relicSelection.ts:246–279`)

Inputs:
- `rewards[i].chance`, as a percent.
- `values[i]`, the price or ducats. A null value counts as **0**.
- `squadSize` n, from 1 to 4.

Steps:
1. `p_i = clamp(chance_i/100, 0, 1)` and `v_i = value_i ?? 0`.
2. If n ≤ 1: `EV = Σ p_i·v_i`.
3. If n > 1, compute the expected **best** reward among n independent openings of the same relic (radshare, where everyone picks the best):
   1. Sort the items by value ascending and merge equal values by summing their probabilities.
   2. Walk the merged list with `cdf_prev = 0`. For each group: `cdf = min(1, cdf_prev + p)`, `EV += v·(cdf^n − cdf_prev^n)`, `cdf_prev = cdf`.

Worked example (Intact, solo):
- Rewards: commons 2p, 3p and 5p at 25.33% each; uncommons 10p and 15p at 11% each; rare 50p at 2%.
- `EV = .2533·10 + .11·25 + .02·50 = 6.283`.

Python:

```python
def squad_ev(rewards, values, n):
    items = [(min(max((r.get("chance") or 0)/100, 0), 1), v or 0) for r, v in zip(rewards, values)]
    if n <= 1: return sum(p*v for p, v in items)
    grouped = []
    for p, v in sorted(items, key=lambda t: t[1]):
        if grouped and grouped[-1][1] == v: grouped[-1][0] += p
        else: grouped.append([p, v])
    ev = cdf_prev = 0.0
    for p, v in grouped:
        cdf = min(1.0, cdf_prev + p); ev += v*(cdf**n - cdf_prev**n); cdf_prev = cdf
    return ev
```

### 7.3 Row per relic (`makeQualityRowBuilder`, `relicSelection.ts:374–438`)

For `(group, quality, count)`:

1. Take `rewards = group.qualities[quality].rewards`. If the list is empty, return null.
2. Normalise each reward's `chance` to [0, 100].
3. Compute `platEv`, which is null if **no** reward has a price. Otherwise use `squad_ev` with the missing prices as 0. Compute `ducatEv` the same way with ducats.
4. Return the row:
   - `label = "<count>x <group.name> <Intact|Exceptional|Flawless|Radiant>"`
   - `relicName`, `quality`, `count`, `platEv`, `ducatEv`, `vaulted`
   - `rewards`: sorted **rare-first** and cut to the first 6. Each is `{uniqueName, name, imageUrl, urlName, rarity, chance, ownedCount}`.

Rare-first order (`config/shared/relicRewardOrder.ts`):
- Rank by rarity: rare=0, uncommon=1, common=2, unknown=3.
- Break ties by `chance` descending.
- The UI slot order is therefore Rare, Uncommon ×2, Common ×3 (`overlayLayout.ts:423–431`).

### 7.4 Refinement choice (`relicSelection.ts:442–474, 115–125, 633–645`)

**No filters pushed (the default overlay behaviour):** `pickBestOwnedQuality`.
- Iterate refinements in the order `radiant, flawless, exceptional, intact`. Skip any with 0 owned copies.
- Pick the highest `platEv` (null counts as −1). On an exact tie, pick the higher `ducatEv`. On a full tie, the earlier one (the higher refinement) wins.

**Filters pushed:** `relicQualityForMode(qualityMode, owned, pinned[groupKey])`.
- If `qualityMode` names a specific refinement, use that one.
- In mode `"owned"`, use the pinned refinement if it is owned. Otherwise use the highest owned (radiant first).
- If the chosen refinement has 0 copies, fall back to `pickBestOwnedQuality`.

The planner never suggests refining a relic. It only ranks refinements the player already owns. There is no void-trace cost model.

### 7.5 Era filter and list (`buildRecommendations`, `relicSelection.ts:578–661`)

1. Load the DB and the `owned` counts.
2. Set `cacheKey = (era || "all") + "|" + fingerprint`.
   - The fingerprint is `"<groupKey>:<i>|<e>|<f>|<r>"` for every owned group, sorted by key and joined with `;`.
   - Return the cached rows if the key matches and the cache is less than 10 s old.
3. For each group:
   - Compute `groupEra = normalizeEra(group.tier)`. Vanguard relics give null, so they show only when the era is null (all).
   - If `era` is set and is not omnia, keep only groups where `groupEra == era`.
   - If `era == "omnia"`, keep all groups except `requiem`.
   - Skip groups with no owned row.
   - Add to `totalOwnedCount` (all refinements).
   - Build the row as in 7.4.
4. Order the rows:
   - **No filters pushed:** sort by `platEv` descending (null = −1), then `ducatEv` descending, then `label` with `localeCompare`.
   - **Filters pushed:** apply `selectRelicPlannerRows` (7.6).
5. Cache the rows. Add the owned-reward counts. Return `{rows, totalOwnedCount}`.

The cache is invalidated when the filters change or the relic DB changes.

### 7.6 Pushed filters (`config/shared/relicPlannerView.ts`)

The desktop planner's "Push to Overlay" button sends `overlay:push-relic-filters` (`src/views/RelicsView.svelte:228–235`). The payload is normalised by `normalizeRelicOverlayFilterPush` (`:274–285`).

```ts
{
  squadSize: 1..4 (int),            search: string (≤200 chars),
  containsNeededReward: boolean,    vaultedMode: "all"|"vaulted"|"unvaulted",
  qualityMode: "owned"|"intact"|"exceptional"|"flawless"|"radiant",
  ownedAbove: 0|2|4|6|8|10,         sortMode: "tier"|"name"|"ev"|"ducat"|"ducatonator"|"owned",
  sortDirection: "asc"|"desc",
  tierFilter: string|null,          // era hint, OCR/log era still wins
  neededRewardKeys: string[]|null,  // ≤4000 group keys, each ≤64 chars
  pinnedQualities: {groupKey: quality}|null
}
```

Defaults are `squadSize 1, search "", containsNeededReward false, vaultedMode "all", qualityMode "owned", ownedAbove 0, sortMode "tier", sortDirection "asc"`.
- **Before any push**, the overlay uses squad size **4** and its own EV sort.
- On the first push, an invalid field falls back to `{...DEFAULT, squadSize: 4}`. On later pushes, it falls back to the previously pushed values.
- In the desktop UI, `ev`, `ducat`, `ducatonator` and `owned` default to "desc" when picked (`src/lib/filters.ts:13–33`).

Filter steps, in order (`selectRelicPlannerRows`, `:176–192`):
1. `vaultedMode`: keep only rows whose `vaulted` matches the mode.
2. `ownedAbove > 0`: drop rows with `ownedTotal <= ownedAbove`. `ownedTotal` counts all refinements.
3. `search`: apply `relicGroupMatchesSearch` (below).
4. `containsNeededReward`: keep rows whose group key is in `neededRewardKeys`.

Sort (`:132–174`), with `dir = desc ? -1 : 1`:
- `name`: `dir * localeCompare(name)`.
- `tier`: `dir * (tierOrder, then name)`, where the tier order is `{Lith:0, Meso:1, Neo:2, Axi:3, Requiem:4}` and anything else is 99.
- `owned`: by `ownedCount` (for the current qualityMode).
- `ev`: by `platEv`.
- `ducat`: by `ducatEv`.
- `ducatonator`: by `ratio = ducatEv/platEv`, which is null if either is null or `platEv <= 0` (`relicDucatonator`, `:127–130`).

For the metric sorts, null always goes **last**, whatever the direction. A tie falls back to tier then name, ascending.

Search (`config/shared/relicSearch.ts:110–142`):
1. Normalise: lowercase, strip `'` and `’`, replace `[^\p{L}\p{N}]+` with a space.
2. Pull refinement words out of the query (`intact`, `exceptional`, `flawless`, `radiant`, or the localised labels). They become a filter: the group must own one of those refinements.
3. The rest of the query matches if, for any term:
   - the term contains the query, or
   - the space-stripped term contains the space-stripped query, or
   - for a multi-token query, every token is in the term's token set.
4. The terms are: the group name; `"tier code"`; every reward name; reward `urlName` with `_` replaced by a space; and each of these with the words `prime` and `blueprint` removed.

**Needed-reward engine.** `src/lib/relic/rewardNeed.ts:32–46` runs only in the renderer. It needs the safety rules, mastery and foundry data.
- **Decky:** run it in the backend if the inventory/mastery spec provides the data. Otherwise use a simplified rule: a reward is needed if its `ownedComponentCount == 0` and its product is not mastered.

---

## 8. IPC payloads (Electron, for reference; on Decky they map to backend → overlay process)

`relic-planner-trigger` → `{source: "eelog"|"manual"}`. The renderer shows "Detecting relic era and ranking owned relics...".

`relic-recommendations`:

```json
{
  "source": "eelog",
  "era": "axi",                      // null when unknown/all
  "rows": [{
    "label": "3x Axi A16 Radiant", "relicName": "Axi A16", "quality": "radiant", "count": 3,
    "platEv": 18.42, "ducatEv": 41.7, "vaulted": true,
    "rewards": [{"rarity":"Rare","chance":10,"name":"Ash Prime Systems Blueprint",
                 "imageUrl":"https://…","ownedCount":0}, …≤6]
  }],
  "totalOwnedCount": 57,
  "ocrUnavailable": false,
  "detection": {"confidence": 1, "textPreview": "", "elapsedMs": 812}
}
```

Banner logic when `rows` is empty (`renderer/overlay.js:620–631`):
- If `ocrUnavailable`, show the OCR-unavailable message. WFHelper's text is Windows-specific; rewrite it for Deck, for example "OCR model missing".
- Else if `era` is set, show "No owned relic recommendations found for the detected era."
- Else show "Could not detect relic era yet (OCR {elapsed}ms, confidence {confidence})." The banner gets the `info` style.

`overlay:push-relic-filters`: see 7.6. `overlay:close`: see 2.5.

---

## 9. Overlay layout (`renderer/overlay.js:463–650`, `renderer/overlay.css:150–410`, `config/shared/overlayLayout.ts:560–574`)

**Window:**
- Canvas 460×640 CSS px, placed **top-right** of the work area with margins: horizontal 16, top 8, bottom 18 (`ipc/overlay/windows.ts:28–38, 414–421`).
- Transparent and click-through unless interactive mode is on (hotkey F7 by default).
- The user can drag it, and the bounds are saved per `windowStateKey "planner"`.
- On Electron the window is scaled by the display zoom factor.

**Layout:**
- `#planner-grid` is a **2-column** CSS grid: `repeat(2, minmax(0,1fr))`, gap 5, padding 8/10/6, `overflow-y: auto` (6 px scrollbar). The top padding is 30 when the header is visible.
- The footer `#planner-hint` (9.5 px, muted, top border) reads "Press {hotkey} to interact with the overlay" in passive mode.

**Card `.plan-card`:**
- Surface background, 1 px border, radius 6, padding 6/8, min-height 48, flex column, gap 2.
- The card with the highest `platEv` (≥ 0) gets `.best`: accent border `#d4a843` mixed with 16% white, a 1 px glow, and a 10% accent tint.

Rows inside the card:
1. **Title** (`.plan-title`, Rajdhani 12 px, weight 600, wraps):
   - `relicCount` ("3x"), `relicName`, `refinement` (localised).
   - Vault tag (8 px uppercase pill, weight 700): VAULTED in red `#f87171` (16% background, 45% border) or UNVAULTED in blue `#60a5fa`.
2. **Profit** (`.plan-profit`, 10 px):
   - Muted label "E. profits:".
   - Platinum icon (14 px) with `platEv.toFixed(1)` in `#f0c95c`, weight 600.
   - Ducat icon with `ducatEv.toFixed(1)` in the accent mixed with `#f7d47a`. A null value shows "-".
3. **Rewards** (`.plan-rewards`, hidden by default because every `reward{0..5}*` field starts hidden; `overlayLayout.ts:565`):
   - Up to 6 rows. Each row is a 22 px icon spanning two lines; the name (10 px, ellipsis); then chance `xx.x%` (9 px, muted, rare in `#d4a843`, uncommon in `#94a3b8`) and "Owned N" (or "?"), right-aligned.

**Theme tokens:**

| Token | Value |
|---|---|
| bg | `#060a12` |
| surface | `#111827` |
| border | `rgba(212,168,67,0.25)` |
| text | `#e8e4dc` |
| muted | `#8b93a5` |
| accent | `#d4a843` |
| font | Barlow 0.82 rem |

**English strings** (`src/i18n/en.json:1153–1163, 985`): `overlay.planner.scanning`, `expectedProfits` "E. profits:", `expectedPlatinum`, `expectedDucats`, `noRecommendations`, `eraUnknown`, `market.ownedCount` "Owned {count}", `common.vaulted` and `common.unvaulted`.

**Decky adaptation** (Pillow on an ARGB X11 window, 1280×800):
- 460×640 covers about 36% of the width.
- Suggested panel: about 380×720 at x = 1280−380−8, y = 40. Keep the left/centre relic grid and the selected-relic reward preview clear.
- Use 1 column, or 2 columns with rewards hidden, at about 46 px per card. That fits about 14 rows in 1 column or 28 in 2 columns.
- The overlay cannot take input under gamescope. Move scroll, filters, squad size, refinement mode and "show rewards" to the QAM panel.
- Add QAM "page up/down" buttons, or show the top N and say "+K more".
- Render the fonts (Rajdhani/Barlow TTF in the plugin) at 1:1. The Deck has no zoom factor.

---

## 10. Windows-only pieces and their Linux equivalents

| WFHelper | Windows | Linux path in WFHelper | Decky plan |
|---|---|---|---|
| Real-time log | DBWIN worker | `~/steam-230410.log` via `PROTON_LOG=1` | Same, using the deck user's home; file poll as the default |
| OCR | Windows.Media.Ocr (`scripts/ocr.ps1`) | ONNX Paddle CH rec | ONNX in the worker (onnxruntime) |
| Capture | GDI window capture / desktopCapturer | getDisplayMedia stream, cropped to the game | `gamescopectl screenshot` / `GAMESCOPECTRL_REQUEST_SCREENSHOT` → `/tmp/gamescope.png` |
| Overlay window | Electron BrowserWindow, always-on-top | Electron, or a layer-shell surface on Wayland | X11 ARGB window with `GAMESCOPE_EXTERNAL_OVERLAY` |
| Interactive hotkey F7 | globalShortcut | not on native Wayland | QAM controls |

---

## 11. Porting risks and open questions

1. **Trigger latency.** Without `PROTON_LOG=1`, the picker lines arrive through the file poll and can be 10+ s late because of the engine's lazy flush. Mitigations:
   - Prompt the user to enable `PROTON_LOG`.
   - Optionally poll the gamescope screenshot for the picker UI (a template on the "filter tab" region) while in the orbiter or a fissure.
   - **Unverified:** whether reading game memory as root could expose the UI state.
2. **16:10 geometry.** The era-label crops are 16:9 full-frame fractions. Validate them on a Deck screenshot of the relic picker (both the fissure picker and the orbiter "Void Relics" screen) and adjust the rects. The EE.log tag path needs no OCR, so the risk only affects the orbiter, the label cross-check and Omnia.
3. **OCR cost on the Deck CPU.** The worst case is label (2 variants) + 5 tiles × 2 + 3 bands × 2, which is 18 recognitions inside a 2.4–3 s budget. Prefer the log tag, keep the budget, and warm up the ONNX session at plugin load.
4. **Price coverage.** The planner reads the backend snapshot cache file written by the desktop renderer. The plugin must itself fetch `/v1/snapshot` (bootstrap token flow, 2 requests/min limit; see the market spec) and persist the same shape, or EVs will be null.
5. **Needed-reward filter.** It depends on renderer-only engines (safety rules, mastery, foundry). Simplify it or port it with the inventory spec.
6. **Self-read.** If gamescope screenshots include the external overlay plane, keep the self-read guard, or hide the overlay for the capture frame.
7. **Root/home paths.** EE.log, the Proton log and Steam libraries must be resolved for `/home/deck` and SD cards (`/run/media/*`), not `/root`.

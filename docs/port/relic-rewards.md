# Port spec: Relic rewards overlay

Source: WFHelper at `/home/claude/wfhelper-src` (commit `06fa689`). All `file:line` references are
to that tree. Target: a Decky plugin on Steam Deck LCD (1280x800, gamescope, Proton, English UI).

This spec covers the whole path: an EE.log line fires, the screen is captured, the reward
cards are found and read with OCR, the reads are matched to item names, the items are
enriched with owned counts and prices, and an overlay is drawn. It also lists the fixtures
that can be reused as tests.

---

## 0. End-to-end flow

```
EE.log tail (500 ms poll)  ─┐
Proton log tail (250 ms)   ─┴─> handleLine() ──"Pause countdown done"/"Got rewards"──> scheduleTrigger("reward")
                                   (250 ms debounce, 2.5 s cooldown, picker suppression, staleness drop)
     ──> onRelicRewardTrigger("eelog", stalenessMs)
         eelogTriggerAt = now - stalenessMs
         sleep until eelogTriggerAt + 650 ms
         runRewardScanWithRetries(): up to 10 attempts, 450 ms apart, within 5 s
             scanRewardsDetailed():
               capture full frame -> trim bars -> frame-hash dedup (5 s)
               detect slot layouts (card-bar counter, else activity scoring, else column projection)
               for each layout (best first): crop each card's title strip -> paddle OCR -> rank item names
               choose best layout; merge donors; rescue near misses
         enrichRewardItems(): uniqueName, ducats, owned/required, set progress, mastered, foundry, vaulted
         overlay shows 4 columns (one per on-screen slot), then fetches WFM price per slot + set price
         auto-hide: at trigger + 14.5 s (10 s in omnia), never < 5 s visible; or on
         "Relic reward screen shut down" + 1.5 s grace
```

---

## 1. Trigger detection

### 1.1 EE.log location (`services/eeLogPath.ts`)

- Override: env `WFHELPER_EE_LOG` (eeLogPath.ts:206). Keep an equivalent (e.g. a plugin setting).
- Windows: `%LOCALAPPDATA%\Warframe\EE.log` (eeLogPath.ts:209-213). **Windows-only.**
- Linux (`discoverLinuxEeLog`, eeLogPath.ts:154-186):
  1. Steam roots (eeLogPath.ts:15-26), each relative to `$HOME`:
     `.local/share/Steam`, `.steam/steam`, `.steam/root`,
     `.var/app/com.valvesoftware.Steam/.local/share/Steam`, `snap/steam/common/.local/share/Steam`.
  2. For each existing root, add the root plus every `"path"` in `<root>/steamapps/libraryfolders.vdf`.
     The regex is `/"path"\s+"([^"]*)"/g`, and each match has `\\` replaced by `\` (eeLogPath.ts:29-37).
  3. For every library, for **every** directory under `steamapps/compatdata/*` (non-Steam shortcuts get
     random ids), look for `<prefix>/drive_c/users/*/AppData/Local/Warframe/EE.log` **and**
     `<prefix>/pfx/drive_c/users/*/AppData/Local/Warframe/EE.log` (eeLogPath.ts:134-144).
  4. Also look in launcher prefixes: `~/.wine`, Heroic `GamesConfig/*.json` `winePrefix` values,
     `~/Games/Heroic/Prefixes/default/*`, Lutris `games/*.yml` `prefix:` lines, and Bottles bottles
     (eeLogPath.ts:95-131). On a Deck, Steam alone is enough.
  5. If several logs are found, **the newest mtime wins**, and the result is marked verified.
  6. If none is found but `<lib>/steamapps/compatdata/230410` exists, return the expected path
     `<lib>/steamapps/compatdata/230410/pfx/drive_c/users/steamuser/AppData/Local/Warframe/EE.log`
     as unverified (eeLogPath.ts:39-54, 181-184).
  7. Cache: a verified path is cached forever. An unverified one is re-probed every 60 s
     (`LINUX_REPROBE_MS = 60_000`, eeLogPath.ts:188-202).
- Warframe's Steam app id is `230410` (eeLogPath.ts:13).
- **Deck note:** the backend runs as root, so `os.homedir()` / `~` would be `/root`. Use the
  `deck` user's home (`/home/deck`, or `decky.DECKY_USER_HOME`). SD-card libraries appear in
  `libraryfolders.vdf` (for example `/run/media/mmcblk0p1`), and their compatdata sits on the card.
  On a Deck the expected file is usually
  `/home/deck/.local/share/Steam/steamapps/compatdata/230410/pfx/drive_c/users/steamuser/AppData/Local/Warframe/EE.log`.

### 1.2 Interface scale from EE.cfg (`eeLogPath.ts:221-273`)

The file is `EE.cfg`, in the same directory as EE.log. It is read fresh on **every scan attempt**.

```
if not re.search(r'^\s*Flash\.FlashDrawScaleMode\s*=\s*[DM]SM_CUSTOM\s*$', text, re.M): return None
vals = re.findall(r'^\s*Flash\.FlashDrawScale\s*=\s*([0-9.]+)\s*$', text, re.M)   # last one wins
v = float(vals[-1]); if not finite or v <= 0: return None
return min(1, max(0.5, round(v, 2)))
```
- `DSM_MATCH_SCREEN` is the Legacy menu scale: a fixed pixel size, which returns `None`.
- If it returns `None`, use the manual setting `warframeUiScale` (default `0.99`). The setting
  `warframeUiScaleAuto` (default `true`) turns the EE.cfg read on or off (scan.ts:385-387).
- `REFERENCE_WARFRAME_UI_SCALE = 0.99` (config/runtime/overlaySettings.ts:133). All crop ratios were
  measured at 99 %, so 0.99 makes the scale correction a no-op.
- The line `/Saved package: \/Configs\/EE\.cfg\s*$/` (eeLogMonitor.ts:170-172) signals that EE.cfg
  changed. WFHelper only uses it to refresh the settings UI.

### 1.3 File tail (`services/eeLogMonitor.ts`)

- Start at the **current EOF**: `lastSize = stat.size` (eeLogMonitor.ts:774-779). Old lines are never replayed.
- Poll every `POLL_INTERVAL_MS = 500` (l.125), and also on chokidar change/add events.
  Read up to `MAX_READ_BYTES = 256*1024` per read and up to `MAX_READ_LOOPS_PER_TICK = 8` reads per tick
  (l.126-127, 279-327).
- Split on `/\r?\n/` and keep the trailing partial line as a remainder (l.628-636).
- Truncation check every 2 s (`TRUNCATION_CHECK_INTERVAL_MS`, l.128). If `size < lastSize`, reset
  the offset to 0, clear the remainder, and reset the uptime tracker (l.288-304). An unlink also resets (l.793-800).
- Decode as UTF-8.

### 1.4 Line staleness from the uptime prefix (`services/eeUptime.ts`)

EE.log lines start with game uptime in seconds, for example `1289.269 Sys [Info]: Pause countdown done`.
The engine buffers file writes, so a line can land 10-15 s late. Staleness is estimated like this:
```
m = re.match(r'^(\d+)\.(\d{3}) ', line)    # if no match, return the previous staleness
uptime_ms = int(m[1])*1000 + int(m[2])
if uptime_ms < last_uptime_ms - 5000: offset = None      # the game restarted
last_uptime_ms = uptime_ms
off = now_ms - uptime_ms
if offset is None or off < offset: offset = off          # the minimum offset is the freshest line ever seen
staleness = max(0, off - offset)
```
Real-time lines (the Proton source) always have staleness 0 (eeLogMonitor.ts:404).

### 1.5 Real-time source: Proton debugstr (`services/protonDebugstrMonitor.ts`)

WFHelper's Linux real-time path tails the **Proton log**, not EE.log. On Windows, the same role is
played by a DBWIN worker that reads OutputDebugString (`services/dbwinMonitor.ts`, **Windows-only**).

- Path: env `WFHELPER_PROTON_LOG`, else `$HOME/steam-230410.log` (protonDebugstrMonitor.ts:18-22).
  The log exists only when the user sets the Steam launch option `PROTON_LOG=1 %command%`
  (l.245-249). On a Deck this is `/home/deck/steam-230410.log`.
- Poll every 250 ms with the same 256 KiB x 8 read loop. On first attach, start at EOF. A smaller
  size means the log restarted, so begin at 0. A missing file means the next appearance is a fresh launch (l.126-217).
- The source counts as "active" only if the log grew in the last 30 s (`ACTIVE_WINDOW_MS`, l.16, 140-142).
- Payload regex (l.25): `/(?:trace|warn):(?:debugstr|seh):OutputDebugString[AW]\s+(L?)"/`.
  The payload runs from the end of that match to the **last** `"` of the right-trimmed line.
  Text after the closing quote that equals `...` means Wine truncated the message (about 290 chars), so the line is **dropped** (l.111-123, 237).
  - ANSI (`A`, no `L`): unescape `\n \r \t \" \\` and `\xHH` as **bytes**, then decode UTF-8 (l.39-71).
  - Wide (`L"`): `\xHHHH` (or `\xHH`) are UTF-16 code units (l.74-108). A stray backslash is kept literally.
  - The payload usually ends in `\n`. Strip it before matching.
  - Example lines (tests/main/protonDebugstrMonitor.test.ts):
    `1234.567:0048:004c:warn:seh:OutputDebugStringA "Sys [Info]: ThemedProjectionManager.lua: LoadingCompleteEnd\n"`,
    `0048:004c:trace:debugstr:OutputDebugStringA "Pause countdown done"`.
- Prefilter `DebugLineGate` (services/debugLineFilter.ts:34-103): forward a line only if its lowercase form
  contains one of these substrings. For this feature the relevant ones are `"pause countdown done"`,
  `"got rewards"`, `"projectionrewardchoice.lua"`, `"loadingcompleteend"`, `"populateinventorygrid"`,
  `"initmapping"`, `"eom missionlocationunlocked="`, `"topmenu.lua: abort:"`. Picker lines
  (`loadingcompleteend`/`populateinventorygrid`) are suppressed for 7.5 s after one passes.
- Real-time lines are handled with `source = "dbwin"`, and file lines with `source = "file"`.
- **Decky alternative:** the backend is root and can tail EE.log directly. Real time only saves the
  engine's flush lag. Check on hardware whether EE.log flushes quickly enough on a Deck. If it does
  not, ask the user to set `PROTON_LOG=1` (the log grows quickly).

### 1.6 Patterns (copied verbatim, eeLogMonitor.ts)

| Purpose | Regex | Line |
|---|---|---|
| Reward screen open (trigger) | `/\bPause countdown done\b/i`, `/\bGot rewards\b/i` | 55-58 |
| Reward cards rendering (log-only "UI ready" signal) | `/ProjectionRewardChoice\.lua:\s*Missing icon data!/i` | 61 |
| Reward screen closed | `/ProjectionRewardChoice\.lua:\s*Relic reward screen shut down/i` | 64 |
| Relic picker open (suppresses reward triggers) | `/\bThemedProjectionManager\.lua:\s*LoadingCompleteEnd\b/i`, `/\bProjection[A-Za-z_]*\.lua:\s*LoadingCompleteEnd\b/i`, `/\bThemedProjectionManager\.lua:\s*PopulateInventoryGrid\b/i`, `/\bProjectionManager\.lua:\s*PopulateInventoryGrid\b/i`, `/\bProjection[A-Za-z_]*\.lua:\s*PopulateInventoryGrid\b/i` | 66-72 |
| Relic picker close | `/\bInitMapping for all devices with bindings\b/i` | 74-76 |
| Fissure tier tag | `/(?:^\|["\s])activeMissionTag["\s]*[=:]\s*"?([A-Za-z0-9_]+)/` (the line must contain `activeMissionTag`) | 179-185 |
| Mission end (clears tag, emits `"EndOfMission"`) | `/Sys \[Info\]: EOM missionLocationUnlocked=\|TopMenu\.lua: Abort:/` | 189 |

Tag to era mapping (ipc/overlay/relicSelection.ts:203-210): `VOIDT1 lith, VOIDT2 meso, VOIDT3 neo, VOIDT4 axi,
VOIDT5 requiem, VOIDT6 omnia`. The tag is uppercased before lookup. A non-fissure tag clears the tier. Only `omnia` changes reward behavior: a shorter auto-hide.

### 1.7 Reward trigger scheduling (`scheduleTrigger`, eeLogMonitor.ts:335-397; `handleLine` 414-424)

Constants (l.103-131): `TRIGGER_DELAY_MS 250`, `REWARD_TRIGGER_COOLDOWN_MS 2500`,
`REWARD_FILE_ECHO_WINDOW_MS 30000`, `REWARD_AFTER_PICKER_SUPPRESS_MS 3000`, `REWARD_STALE_SUPPRESS_MS 20000`,
`RELIC_TRIGGER_DELAY_MS 300`, `RELIC_PICKER_COOLDOWN_MS 3000`.

```
on line matching a reward pattern:
  if source == "file" and realtime_source_active() and now - lastRewardAt < 30000: ignore   # flush echo
  else scheduleTrigger("reward", source, staleness)

scheduleTrigger("reward"):
  if now - lastRewardAt < 2500: return
  if a reward timer is already pending: return
  after 250 ms:
     if now - lastRelicPickerPatternAt < 3000: log "suppressed - relic picker"; return  # lastRewardAt is NOT updated
     if staleness > 20000: log "dropped - stale"; return
     lastRewardAt = now; fire onRewardTrigger(staleness)
any relic-picker pattern line (even one inside its cooldown) sets lastRelicPickerPatternAt = now.
Picker patterns and InitMapping close lines are ignored from the file source while a real-time source is active.
```

---

## 2. Scan orchestration (`ipc/overlay/scan.ts`, `ipc/rewardOverlayIpc.ts`)

Constants (scan.ts:20-53):

| Name | Value | Meaning |
|---|---|---|
| `EELOG_REWARD_SCAN_DELAY_MS` | 650 | first capture at `triggerAt + 650 ms` (the AlecaFrame value) |
| `SCAN_RETRY_WINDOW_MS` | 5000 | total retry window |
| `SCAN_RETRY_INTERVAL_MS` | 450 | sleep between attempts |
| `SCAN_MAX_ATTEMPTS` | 10 | |
| `NO_LAYOUT_MAX_ATTEMPTS` | 3 | 3 consecutive attempts with `layoutCount == 0` mean "not the reward screen" |
| `PARTIAL_LAYOUT_BONUS_ATTEMPTS` | 1 | one extra attempt when a read is partial |
| `MAX_REWARD_ITEMS` | 4 | |
| `REWARD_VOTE_WINDOW_MS` | 14500 | hide at trigger + 14.5 s (the vote lasts 15 s) |
| `OMNIA_REWARD_VOTE_WINDOW_MS` | 10000 | in an omnia fissure |
| `REWARD_MIN_VISIBLE_MS` | 5000 | minimum time the result stays visible |
| `REWARD_CLOSE_GRACE_MS` | 1500 | after the "shut down" line |
| `REWARD_CLOSE_STALE_MAX_MS` | 5000 | ignore close lines staler than this |
| `OVERLAY_AUTO_HIDE_SUCCESS_MS` | 8500 | non-EE.log triggers (hotkey or manual) |
| `OVERLAY_AUTO_HIDE_FAILURE_MS` | 3500 | |
| `OVERLAY_AUTO_HIDE_OCR_UNAVAILABLE_MS` | 12000 | capture or OCR unavailable message |
| `OVERLAY_AUTO_HIDE_DETECTING_MAX_MS` | 20000 | a manual trigger shows "scanning" for at most this long |

Flow:
1. `onRelicRewardTrigger(source, staleness)` (scan.ts:620-637). If `source == "eelog"` and
   `autoTriggerEnabled` is false, return. Clear the hide timer and reset the overlay to "scanning".
   An EE.log trigger keeps the overlay **hidden** until its own result arrives (rewardOverlayIpc.ts:223-227).
   This stops the previous round's cards from being shown and then captured. A manual trigger
   (hotkey `F8` by default) shows "scanning" immediately.
2. `dispatchRewardScan` (scan.ts:484-618) refuses to run while a scan is in flight.
   `eelogTriggerAt = now - stalenessMs` and `rewardScreenClosedAt = 0`.
   Then it checks that Warframe is open and focused, and skips the scan if the game is closed. On a
   Deck, check that the game is running, for example the `Warframe.x64.exe` process (an assumption).
   Then it sleeps until `eelogTriggerAt + 650`.
3. `runRewardScanWithRetries` (scan.ts:365-482):
   ```
   countedCards = 0; best = None; partialAttempts = 0; noLayout = 0
   while attempts < 10 and elapsed < 5000:
     r = scan(uiScale = EE.cfg scale ?? setting ?? 0.99)
     countedCards = r.meta.cardCount or countedCards       # sticky
     best = chooseBetter(best, r, countedCards)
     n = len(r.items)
     if n > 0:
       geometryComplete = r.meta.slotCount == 3 and n >= 3
       layoutKnown = max(slotCount, layoutCount) > 0
       partial = (n != countedCards) if countedCards > 0 else (layoutKnown and n < 4 and not geometryComplete)
       if not partial or partialAttempts >= 1:
         if countedCards > 0 and len(best.items) != countedCards: return {items: [], partial: ...}  # show nothing
         return best
       partialAttempts += 1
     noLayout = 0 if r.meta.layoutCount > 0 else noLayout + 1
     if noLayout >= 3: notRewardScreen = True; break
     sleep(min(450, remaining))
   # timeout: if best has items but their count != countedCards: return empty with partial
   ```
   `chooseBetterScanResult` (scan.ts:299-319): when `countedCards > 0`, a result whose item count
   equals it wins. Otherwise the result with more items wins. If counts are equal, the higher `meta.score` wins.
4. `enrichRewardItems` (§9). Then `presentResult` sends the items to the overlay and schedules auto-hide:
   - success with `source == "eelog"`: `max(5000, triggerAt + (omnia ? 10000 : 14500) - now)`.
     If the close line already arrived during the scan, use 5000 (scan.ts:341-348).
   - no items: 3500. If `notRewardScreen`, the overlay is not revealed at all (scan.ts:548-556).
   - Linux capture failure: payload `{items: [], failureReason: "capture-unavailable"}`, shown for 12000 ms.
5. `notifyRewardScreenClosed(staleness)` (scan.ts:352-363): ignored if there was no trigger, if staleness is
   over 5000, or if a close was already handled. Otherwise hide after `max(1500, triggerAt + 5000 - now)`.
   If a scan is still in flight, the success path uses 5000 instead.

---

## 3. Screen capture

WFHelper's Linux path (`services/screenCapture.ts:478-512`, `services/linuxStreamCapture.ts`) uses an
Electron `getDisplayMedia` portal stream, then crops to the game window (KWin, niri or X11 geometry) or falls back to
letterbox trimming. **None of that applies to a Deck.** Windows uses GDI BitBlt plus a client-rect crop (**Windows-only**).

Deck: `gamescopectl screenshot <path>` (or the `GAMESCOPECTRL_REQUEST_SCREENSHOT` root property, which writes
`/tmp/gamescope.png`) gives a 1280x800 PNG of the game plane. That is already "the game window",
so it is equivalent to WFHelper's `isGameWindow: true` case: **do not run bar trimming**. If you do want
to guard against letterboxing (for example at an external-display resolution), port `detectGameContentRect`:

- `detectGameContentRect` (rewardScannerImage.ts:21-97): `BAR_LUMA_THRESHOLD 12`,
  `BAR_SAMPLE_COUNT 32`, `BAR_BLACK_RATIO 0.85`. A row or column is "black" when at least 85 % of 32
  evenly spaced samples have `(R+G+B)/3 <= 12`. Scan inward from each edge (top and left up to 25 %,
  bottom and right from 75 %). Pillar width = `min(left, W-right)` and letterbox height =
  `min(top, H-bottom)`, because bars come in equal pairs. Content =
  `{x: pillar, y: letter, w: max(24, W-2*pillar), h: max(24, H-2*letter)}`.
- `trimToGameContent` (screenCapture.ts:130-150) crops only when the content rect is smaller than the
  frame by more than `EDGE_SLACK_PX` and larger than the `MIN_CONTENT_*` constants.

Pixel format: the WFHelper bitmap is **BGRA** (Electron `toBitmap`). The math below is written per
channel, so convert carefully. In Python, use RGB numpy arrays and swap where the formula names B/G/R.

---

## 4. Geometry (`services/rewardScannerImage.ts`)

### 4.1 Base card layouts (16:9, normalized to the frame, measured at 99 % UI scale), l.320-339 (verbatim)

```ts
1: [{ x: 0.439,  y: 0.225, width: 0.122, height: 0.225 }],
2: [{ x: 0.3755, ... }, { x: 0.5025, ... }],
3: [{ x: 0.312, ... }, { x: 0.439, ... }, { x: 0.566, ... }],
4: [{ x: 0.245, ... }, { x: 0.372, ... }, { x: 0.499, ... }, { x: 0.626, ... }],
// every card: y 0.225, width 0.122, height 0.225
```
Card pitch is 0.127 W. Title strip of a card (`buildFixedSlots`, l.876-892):
`title = {x: c.x, y: c.y + c.h*0.70, w: c.w, h: c.h*0.18}`.

### 4.2 UI-scale correction (l.386-400)

`s = clamp(uiScale, 0.25, 1) / 0.99`. Every rect scales about the centre (0.5, 0.5):
`x' = 0.5 + (x-0.5)*s`, `y' = 0.5 + (y-0.5)*s`, `w' = w*s`, `h' = h*s`.

### 4.3 Aspect correction (l.341-383), **important for 16:10**

```
refW = H*16/9 ; refH = W/(16/9)
scaleX = refW/W if refW < W else 1      # wider than 16:9 (21:9)
scaleY = refH/H if refH < H else 1      # taller than 16:9 (16:10, 4:3)
if scaleX >= 1 and scaleY >= 1: unchanged
x' = 0.5 + (x-0.5)*scaleX ; y' = 0.5 + (y-0.5)*scaleY ; w' = w*scaleX ; h' = h*scaleY
```
The menus sit on a centred 16:9 canvas. At **1280x800, scaleX = 1 and scaleY = 0.9** (canvas 1280x720,
centred with 40 px above and below). Order: UI-scale correction first, then aspect correction
(`aspectCorrectLayout(uiScaleCorrectLayout(layout, ui), aspectScaleFor(img))`, l.904, 940-945).

### 4.4 Crop rounding (`cropRect`, l.194-209)

x and y are clamped to [0, 0.98]. w and h are clamped to [0.02, 1-x] and [0.02, 1-y].
`px = floor(W*x)`, `py = floor(H*y)`, `pw = max(24, floor(W*w))`, `ph = max(24, floor(H*h))`.

### 4.5 Precomputed Deck geometry (1280x800, UI scale 0.99)

Computed with the formulas above (validation script output, see §12.4):

| Cards | Card rects (x, y, w, h px) | Title strips (x, y, w, h px) |
|---|---|---|
| 1 | (561,201,156,162) | (561,315,156,29) |
| 2 | (480,201,156,162) (643,201,156,162) | (480,315,156,29) (643,315,156,29) |
| 3 | (399,201,156,162) (561,…) (724,…) | x = 399, 561, 724; y 315; 156x29 |
| 4 | (313,201,156,162) (476,…) (638,…) (801,…) | x = 313, 476, 638, 801; y 315; 156x29 |

Normalized title y at 16:10 = 0.5 + (0.225 + 0.225*0.7 - 0.5)*0.9 = 0.39425, and h = 0.225*0.18*0.9 = 0.03645.
At 1080p 16:9 the title strip is 234x43 px. On the Deck it is **156x29 px**, smaller than any real fixture
except 1080x607 (131x24).

### 4.6 Other bands (only used on fallback or non-ONNX paths)

- `SLOT_LAYOUT_REGION = {x: 0.03, y: 0.37, w: 0.94, h: 0.34}` (l.317), for column projection.
- `CROP_PRESETS.balanced = [{top .38,h .36},{top .36,h .40},{top .40,h .34}]` (rewardScannerSupport.ts:138-144). The
  text fallback uses `balanced[1]` (pipeline l.187), but **it is skipped when the reader is ONNX** (pipeline l.327-337).
- `cropRewardBand` maps top and height with scaleY the same way as 4.3 (l.166-181).

---

## 5. Slot-count / player-count detection

The number of reward cards equals the squad size (1-4). WFHelper never asks the game for it. It
**reads it off the frame**: `detectRewardSlotLayoutCandidates` (l.927-964) tries three methods in order.

### 5.1 Card-bar counter (preferred; `countRewardCardsInBitmap`, l.402-536)

Every reward card draws a thin bar under its title. The outermost card of an N-card row exists only if
N cards are shown, so the counter probes windows from N = 4 down to 1.

```
CARD_COUNTER_BAND = {top: 0.431, height: 0.027}           # 16:9 frame ratios
CARD_COUNTER_WINDOW_WIDTH = 0.064                         # x row width
CARD_COUNTER_WINDOWS = [(4,[0.01,0.91]), (3,[0.15,0.78]), (2,[0.28,0.665]), (1,[0.4,0.535])]  # starts as fractions of the 4-card row
CARD_ROW_LEFT  = 0.245 ; CARD_ROW_WIDTH = 0.626+0.122-0.245 = 0.503
row = aspect(uiScale({x:0.245, y:0.431, w:0.503, h:0.027}))
rowLeft = round(row.x*W); rowWidth = round(row.w*W); top = max(0, round(row.y*H))
bandH = min(H-top, round(row.h*H)); win = round(rowWidth*0.064)
for (n, starts) in WINDOWS: for s in starts:
   left = rowLeft + round(rowWidth*s); if left < 0 or left+win > W: continue
   if windowHasCardBar(left, top, win, bandH): return n
return 0
```
Deck at UI 0.99: `rowLeft 314, rowWidth 644, top 350, bandH 19, win 41`.

`windowHasCardBar(left, top, width, height, thin=False)` (l.429-473):
```
if width < 12 or height < 6: False
mid = top + height//2 ; frm = left + width//6 ; to = left + (5*width)//6
run = count of x in (frm+1 .. to-1) before the first x where dist(px[mid][x], px[mid][x-1]) >= 45
if run/(width*0.6) < 0.9: False
for each column x in [left, left+width): tall = number of rows going up from mid-1 and down from mid+1
     until dist(px[y], px[y±1]) > 32   (dist = Euclidean RGB distance)
avg = mean(tall); trimmed = sorted(tall, desc)[3:]; var = mean((h-avg)^2 for h in trimmed)
normAvg = avg/height ; normStd = 5*sqrt(var)/height
if count(h < (1 if thin else 2)) > len/3: False
return (avg >= 1 if thin else normAvg >= 0.05) and normAvg <= 0.27 and normStd <= 0.36
```

If the configured scale finds nothing, `findRewardCardsInBitmap` (l.671-733) **searches UI scales**:
- `rowAt(step)` with candidate = step/100: build the band at that scale, find `barRowNear` (the row within
  ±max(2, round(bandH/8)) of the band middle with the largest `thinBarShare`, using
  `reach = max(2, ceil(bandH/4))`, sampled every `max(1, rowWidth//256)` px; l.597-625). Then
  `cardRowVerified` for n = 4, 3, 2, 1 (l.629-665): bars near both edges of every card (inset
  `round(cardW*0.08)`, thin mode), `barEndsAt` at the row's outer edges (≥ 2/3 share inside, ≤ 1/3 outside,
  over `[edge±round(cw*.03), edge±round(cw*.1)]`), and no bar in the slot beyond either end.
- Legacy probe: `legacy = 885/(H*scaleY)` (`LEGACY_UI_SCALE_TIMES_HEIGHT = 885`). Probe steps around
  `round(legacy*100)` in the order 0, -1, +1, -2, +2, -3, +3, within [25, 100]. Grow the run of equal
  counts and accept it if both neighbours outside the run are 0.
  **On a Deck, legacy = 885/720 = 1.23, which is out of range, so this branch never fires.**
- Sweep: every step from `round(min(0.5, legacy*0.9)*100)` to 100. All hits must agree on the count.
  Return the middle hit.
- A hit gives a single layout `{count, confidence: 1, counted: True}` built at the **found** uiScale.
  Every other layout is skipped.

### 5.2 Activity-scored fixed layouts (fallback; l.832-925)

For each N in 1..4: `activity` of each corrected card rect (l.832-874):
```
crop the card rect; if w < 30 or h < 30: 0
stepX = max(1, w//80); stepY = max(1, h//80); luma = (77R + 150G + 29B) >> 8
for y in range(stepY, h, stepY): for x in range(stepX, w, stepX):
    bright += luma >= 86 ; texture += |luma - luma(x-stepX)| ; total += 1
score = round(0.45*min(1, bright/total/0.24) + 0.55*min(1, texture/total/42), 3)
```
`active = #(score >= 0.22)`, `avg = mean(scores)`,
`confidence = round(0.58*min(1, active/N) + 0.42*min(1, avg/0.7), 3)`.
Keep the layout if `active >= min(2, N) or avg >= 0.28`. Sort by confidence descending; if two
confidences differ by less than 0.08, the larger N comes first.

### 5.3 Column projection (last resort; l.968-1062)

This runs only when no fixed layout qualified. It counts bright columns in `SLOT_LAYOUT_REGION`
(`luma >= 108 or (luma >= 86 and sat <= 0.38)`, every 2nd row), smooths them with a 3-tap filter,
and thresholds at `max(3, mean + 0.38*std)`. Runs must be at least `max(18, 5 % of width)` wide and
are padded by `max(8, 1 %)`; at most 4 are kept. The title rect is at y + 0.56h with height 0.16h.
`confidence = .45*min(1,runs/4) + .30*min(1,coverage/.72) + .25*min(1,std/36)`.

### 5.4 Layout gate (`hasConfidentSlotLayout`, rewardScannerSupport.ts:152-158)

A counted layout always passes. N = 1 needs confidence ≥ 0.55. N ≥ 2 needs confidence ≥ 0.38.
Keep at most 6 layouts (SlotScan l.416-418).

### 5.5 What WFHelper does with the count

- `meta.cardCount` = the counted N (0 if the layout was searched). The retry loop treats it as the
  truth: a read whose item count differs from it **is not shown**.
- The overlay always has 4 columns, and each item goes into the column of its **on-screen slot index**
  (§11). The count does not resize the panel.

---

## 6. OCR: PaddleOCR CH PP-OCRv3 recognizer (ONNX)

The Windows OCR path (`services/ocrServer.ts`, `scripts/ocr.ps1`, `rewardScannerOcr.ts`,
`binarizeRewardRegion` with 3 bands `[0,.58]`, `[.42,.58]`, `[0,1]`) is **Windows-only**. Off Windows,
WFHelper pins `reader = "onnx"` (rewardScanner.ts:80). Only the ONNX path needs porting.

### 6.1 Assets (`resources/riven-ocr/paddle/`)

| File | sha256 |
|---|---|
| `ch_PP-OCRv3_rec_infer.onnx` (10,690,752 B) | `897a3ededb38fee0dae2c1ccee38241f37df202c9509e3abca02e9217c5ee615` |
| `ch_dict.txt` (6624 lines, last line is a single space `" "`) | `c084fa990ecf0ce7fcb6bd5a3b689382645ec454e6f6af712b3d6be0addfdaf5` |

- Model I/O (checked with onnxruntime 1.29): input `x` float32 `[N, 3, ?, ?]`, output
  `softmax_5.tmp_0` `[N, T, 6625]`. **The output is already softmaxed.**
- Dictionary (rivenOcrOnnx.ts:94-101): split on `/\r?\n/`, drop one trailing empty line, and prepend `"blank"`.
  The result has 6625 entries, which matches the class count. Index 0 is the CTC blank. Do not strip the last `" "` entry: it is the space class.
- Session (rivenOcrOnnx.ts:13-22): CPU EP, graph optimization "all", `inter_op_num_threads = 1`,
  `intra_op_num_threads = min(4, max(2, cores-4))`. On the Deck's 8 threads that gives 4. Consider 2 so the game does not stutter.
- Warm-up (rewardOcrOnnx.ts:24-31): run one 160x48 all-white RGB crop at startup. The first load costs about 1.4 s.

### 6.2 Strip preprocessing (`recognizeRewardStripOnnx`, rewardOcrOnnx.ts:119-189)

Input: the title-strip crop as a PNG (RGBA).
1. `targetW = max(8, round(min(srcW*3, 720)))` (`ocrStripTargetWidth`, rewardScannerImage.ts:140-146).
   Deck: 156 px becomes **468 px**. 1080p: 234 px becomes 702 px.
2. `gray`: resize to width targetW with **Lanczos3** (keep aspect), convert to grayscale, drop alpha, then
   `normalise()`. Sharp's normalise is a percentile stretch, by default 1 % → 0 and 99 % → 255.
3. `thr = otsu(gray)` (rewardScannerImage.ts:1142-1167: standard Otsu over a 256-bin histogram, starting at
   127, keeping the max between-class variance `wB*wF*(mB-mF)^2`, strict `>`).
   `mono[i] = 0 (ink) if gray[i] >= thr else 255`. **Bright pixels are ink**: game text is light on dark.
   (Windows binarize instead treats the majority class as background.)
4. `splitStripRows(mono, W, H)` (l.39-95):
   ```
   ink[y] = count(mono[y][x] == 0); minInk = max(3, floor(W*0.015))
   a segment starts at the first row with ink >= minInk; it ends when > 2 consecutive rows fall below
       (ROW_GAP_TOLERANCE 2): seg = (start, y - gap)
   any segment taller than MAX_LINE_HEIGHT 72: valley = argmin ink[y] for y in [y1+25%, y1+75%];
       if valley < 0.8*peak, split into (y1, v-1) and (v+1, y2) and re-queue at the front
   keep segments with height >= MIN_LINE_HEIGHT 10, sorted by y1
   ```
5. RGB at targetW: the same Lanczos resize, alpha removed, **not** normalised.
6. For each segment (`segmentToRgbCrop`, l.192-228): `y1 = max(0, seg.y1-4)`, `y2 = min(H-1, seg.y2+4)`.
   The ink column range is taken over those rows. `x1 = max(0, minX-8)`, `x2 = min(W-1, maxX+8)`.
   Crop **the raw RGB** (the row was found on mono, but the RGB keeps thin glyph strokes).
7. Recognize all crops (§6.3). Drop rows with `confidence < 0.55` (`MIN_ROW_CONFIDENCE`).
   Clean each row (`cleanOnnxRowText`, l.98-104):
   ```
   re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', t)      # CH model drops spaces: "XakuPrime" -> "Xaku Prime"
   re.sub(r"[^A-Za-z0-9 &'-]", ' ', ...) ; collapse whitespace ; strip
   ```
   Strip text = the rows joined with spaces, top to bottom. Return `None` if no row survives.

### 6.3 Recognizer (`recognizeCropsBatch` / `recognizeChunk`, rivenOcrOnnx.ts:404-532)

- Height `REC_IMG_HEIGHT = 48`. Each crop gets `w48 = ceil(48*w/h)`.
- Batching: sort crops by w48. Keep a chunk while it has fewer than `REC_MAX_CHUNK = 6` crops,
  `max(w48) <= narrowest*2` (`REC_MAX_PAD_STRETCH`), and
  `count*ceil(imgW/8)*6625*4 <= 48 MiB` (`REC_OUTPUT_BUDGET`). A single crop over the budget is skipped.
- Per chunk: `imgW = ceil(48 * max(w/h))`. The tensor is `float32 [N, 3, 48, imgW]`, zero-filled.
  For each crop: `rw = min(imgW, ceil(48*w/h))`; resize to `(rw, 48)` **bilinear** (sharp "linear"), then write CHW,
  **RGB order**, `v/127.5 - 1.0` into `[:, :, :, :rw]`. The right padding stays 0.0. There is no mean/std beyond that.
- CTC greedy decode (l.342-379): for each t, `best = argmax`. Emit `dict[best]` if `best != 0 and best != prev`
  (and `best < len(dict)`). Then `prev = best`, so a blank resets the repeat suppression.
  `confidence = mean(prob of emitted chars)`, which is 0 if nothing was emitted.

### 6.4 Validation (Python reimplementation, see §12.4)

PIL Lanczos/bilinear and a numpy percentile stretch stand in for sharp. This reproduced the expected text exactly:
`real-full-2p`: "Khora Prime Systems Blueprint", "Fang Prime Handle".
`real-full-4p-fps`: "Xaku Prime Chassis Blueprint", "Bronco Prime Receiver", "Paris Prime Lower Limb", "Vadarya Prime Barrel".
`real-full-4p-16x10` downscaled to **1280x800**: 4x "2 XForma Blueprint" ("2 XFoima Blueprint" on one slot).
`1080x607`: "Caliban Prme Bluerint" and "Grende Prme Blueprint". The fuzzy matcher has to recover these.

---

## 7. Per-slot reading and layout selection (`services/rewardScannerSlotScan.ts`, `rewardScannerPipeline.ts`)

### 7.1 Pipeline wrapper (rewardScannerPipeline.ts:242-385, rewardScanner.ts:22-27)

- Settings: `ocrPasses 2`, `matchThreshold 0.74`, `ocrTimeoutMs 15000`. Budget =
  `max(1800, min(5000, 800 + passes*500 + clamp(ocrTimeoutMs, 500, 2000)))` = **3800 ms** (l.78-85).
  Slot reads start only while the remaining budget is above 0.
- `detectConsoleOpen` (l.1086-1139) only logs. It is a bright, low-saturation bottom 4 % strip at least 55 luma brighter than the frame.
- **Frame dedup** (l.57-76, 265-280): sample the green channel every 8 px in x and every 2 px in y over the
  bounding box of the corrected 4-card layout (Deck: x 313-958, y 201-364). Key = `sha1(sample) + ":" + uiScale`.
  The same key within `REWARD_FRAME_DEDUP_TTL_MS = 5000` returns the cached result. Only non-empty results are cached.
- Result meta keys: `captureWidth, captureHeight, score, exactCount, strategy` (`slot-primary` | `slot-merged`
  | `slot-rescued` | `text-fallback` | `none`), `layoutCount, slotCount, cardCount, bandTopRatio,
  bandBottomRatio, elapsedMs, hadOcrSuccess`.

### 7.2 Reading one slot (`readSlotTitle`, l.286-391)

1. Crop the title rect (cropRect) and encode it as PNG. Run ONNX on the strip (§6). Cache by rect key
   `x:y:w:h` with 4 decimals within one scan, because the 1- and 3-card layouts share their centre card (l.431-446).
2. Remove 1-character tokens (`cleanRewardOcrText`, l.261-267): keep a whitespace token if it is `&` or
   has more than 1 `[a-z0-9]` char (case-insensitive).
3. For each distinct candidate text (with ONNX only, just the ONNX text), call
   `rankRewardCandidatesDetailed(text, items, limit=4)` (§8).
4. Usability gate (`isUsableSlotCandidate`, l.229-239):
   - name has ≤ 1 word and fewer than 5 chars: only `exact` with confidence ≥ 0.99
   - `exact` ≥ 0.98; `substring` ≥ 0.92 (`SUBSTRING_SLOT_GATE`); `fuzzy` ≥ 0.86
   The rejected candidate with the highest confidence is kept as `nearMiss`.
5. Sort the usable candidates by `score desc`, then confidence desc, then (if both are exact) longer name first.

### 7.3 Choosing the layout (`scanRewardSlotsFallback`, l.393-605)

For each layout in order: read slots `0..min(count,4)-1` in parallel, and collect the top candidate per slot.
Skip a layout with zero hits. Score it (`buildLayoutResult`, l.130-170):
```
score = avg(candidate.score) + 44*matched + 35*exact + 20*avgConf + 12*layoutConf
        + 18*min(matched, 4)/4 - 30*emptySlots          # expectedCount = MAX_REWARD_SLOTS = 4
```
The best layout has the most matched slots. On a tie, the higher score wins, or when the scores are
within 12, fewer empty slots wins. Each item gets `slotIndex` = its index **within that layout**.
**Early exit**: if `matched >= 2`, every slot matched, and every match is exact, skip the remaining (smaller) layouts.

After choosing:
- **Donor merge** (l.177-203, 548-560): if the best layout has empty slots, take hits from losing layouts.
  Each donor maps to the best-layout slot its title rect overlaps most in x. It needs at least 50 %
  overlap of the narrower rect, and the slot must be empty. Per slot, the donor with the highest score wins.
  The strategy becomes `slot-merged`.
- **Near-miss rescue** (l.101-128, 562-577): if empty slots remain and `exactCount >= 1`, fill an empty slot with its
  `nearMiss` if `confidence + 0.06` passes the usability gate. A substring candidate sitting at the 0.88 floor never qualifies.
  Names already used are skipped. The strategy becomes `slot-rescued`.
- Debug dumps go to `userData/scan-debug/<stamp>/` (strip PNGs plus JSON), capped at 25 bundles (`rewardScanDebug.ts`).
  Optional in the port.

---

## 8. Fuzzy name matching (`services/rewardScannerMatch.ts`)

### 8.1 Normalization

- `normalizeForSearch(v)` = lowercase, collapse whitespace, strip (config/shared/textNormalize.ts:7-12).
- `normalizeRewardWord(w)` (l.225-229) = `normalizeForSearch(w)` with everything except `[a-z0-9]` removed, then
  `REWARD_TOKEN_ALIASES.get(w, w)`.
- `normalizeRewardText(t)` (l.235-243) = split on whitespace, map `normalizeRewardWord`, drop empties, join with
  `" "`, then `re.sub(r'^(\d+)\s+x(?=\s|$)', r'\1x', ...)`. This turns "2 x forma blueprint" into "2x forma blueprint".
- Quantity prefix (config/shared/quantityPrefix.ts): `QUANTITY_PREFIX = /^\d+\s*x\s+/i`; `hasQuantityPrefix`, `stripQuantityPrefix`.
- `foldOcrConfusions(v)` (l.249-262): lowercase, `8→b, 0→o, 1→l, 5→s`, then remove all `[^a-z0-9]` (spaces removed too).
- Item-name normalizations are cached (max 8000 entries; clear when full).

`REWARD_TOKEN_ALIASES` (l.74-147, verbatim; keys are compared after lowercasing, so the keys with uppercase
letters `bIueprint`, `voIt`, `Iimbo`, `bIade` can never match. Copy them anyway for parity):
```
bluedrint blueorint blueprlnt blueprini bluepnnt blueprint bIueprint biueprint lueprint -> blueprint
svst svstems systerns syst syslems -> systems
neurootics neurotics neuroptlcs neurcptics neuropiics neuraptics eurobtic europtics -> neuroptics
chassls chassi chassl chassi5 hassis -> chassis
recelver recelvar recei rece1ver -> receiver
wukon->wukong rhln->rhino rhin->rhino sarv->saryn nek->nekros nekr->nekros obero->oberon obenon->oberon
trinlty->trinity trini->trinity trinit->trinity bans->banshee bansh->banshee equlnox->equinox equln->equinox
voIt->volt hy->hydroid hyd->hydroid ivar->ivara llmbo->limbo Iimbo->limbo
prlme->prime pnme->prime prix->prime priime->prime barre->barrel banel->barrel bIade->blade bilade->blade
stoc->stock slock->stock grlp->grip forrna->forma forna->forma
```
Note that "prme" (seen on the 1080x607 strips) is not an alias. The fuzzy word pass (similarity ≥ 0.78 for words under 7 chars: prme/prime = 0.8) covers it.

### 8.2 `similarityScore(a, b)` = `max(0, 1 - lev(a,b)/max(len a, len b, 1))`; 1 if a == b. Levenshtein is the standard version (rewardScannerUtils.ts:41-57).

### 8.3 `rankRewardCandidatesDetailed(ocrText, items, limit)` (l.405-547)

```
text = normalizeRewardText(ocrText); if not text: return [none]
textWords = [w for w in text.split(' ') if len(w) > 1]
foldedText = fold(text); textHasQty = hasQuantityPrefix(text)
for item in items:
  name = normalizeRewardText(item.name)
  1. text == name                       -> exact,     conf 1,    score 1000
  2. foldedText == fold(name)           -> exact,     conf 0.99, score 990
  3. phrase(text contains name) or (useful(text) and phrase(name contains text))
                                        -> substring, conf max(0.88, sim(text, name)),
                                           score 400 + conf*92 + min(8, len(name)/4)
     where phrase(h, n) = re.search(r'(^|[^a-z0-9])' + re.escape(n) + r'(?=$|[^a-z0-9])', h)
           useful(t)    = (#words with len > 1) >= 2 or len(t) >= 5
  4. no quantity prefix in either, len(foldedText) >= 12 and len(fold(name)) >= 12,
     sim(foldedText, fold(name)) >= 0.94 -> substring, conf = sim, score as in 3
  5. fuzzy: skip if len(name) < 0.45*len(text)
     itemWords = [w for w in name.split(' ') if len(w) > 1]
     wordsEq(t, n) = t == n or sim(t, n) >= (0.70 if len(n) >= 7 else 0.78)
     matched = #itemWords equivalent to any textWord; explained = textWords that matched some itemWord
     wordRatio = matched/len(itemWords); skip if < 0.45
     unexplained = #textWords not explained with len >= 3
     coverage = min(wordRatio, explained/(explained+unexplained))
     span = max(sim(text, name), max over windows of len(itemWords) textWords: sim(' '.join(window), name))
     conf = min(0.97, 0.6*coverage + 0.4*span); score = conf*100 + 2*matched -> fuzzy
survivors = dropQuantityPrefixMismatches(ranked, text)
boostUniqueStructuralCandidate(survivors, text, textWords)
sort by score desc, conf desc, len(name) desc; return the first max(1, limit)
```
- `dropQuantityPrefixMismatches` (l.290-319): this applies only when the candidates include both a counted name
  (for example "2X Forma Blueprint") and its bare sibling ("Forma Blueprint"). An `exact` candidate is always
  kept. Otherwise a candidate is kept only if `hasQuantityPrefix(text) == candidate is counted`.
- `boostUniqueStructuralCandidate` (l.359-403): this applies when there are at least 2 textWords and no exact candidate.
  Tiers, in order: a substring candidate whose name starts with `text + " "` (prefix), a substring
  candidate whose name is contained in the text (contains), and a non-substring candidate where the text words
  are an ordered subsequence of more name words (wordsEq). The first non-empty tier is used, and it must hold
  exactly one name. A subsequence covering less than 60 % of the name never wins. If that single candidate has
  confidence below 0.93, raise it to **0.93**, with score `400 + 0.93*92 + min(8, len/4)`.

### 8.4 `matchItemsDetailed` (l.149-216)

This multi-item matcher over a whole band is used only by the text fallback, which is disabled for ONNX. It
finds exact phrases, then (if there are fewer than 3 exact matches) word-overlap matches with ratio ≥ max(threshold, 0.86) and at least 2 words.
The port can skip it.

### 8.5 Item list (`services/relicService.ts:97-123`)

`setRelicItems(getRelicRewardItems())` is called at startup and when relic data refreshes (main.ts:707-719).
The scanner pre-sorts the list by name length, longest first (rewardScanner.ts:39-43).
- Source: `@wfcd/items` `data/json/Relics.json` (package `^1.1276.6`), or the backend document `GET https://api.wfhelper.com/v1/wfcd-relics`
  (ETag cached, refreshed every 6 h, about 4.9 MB; services/relicDataUpdate.ts:32-36). The backend is optional.
  Fetching Relics.json from the @wfcd/items npm tarball or GitHub works too.
- A relic row is used only if its name is `"<Tier> <Code> <Quality>"` with Tier in
  `{Lith, Meso, Neo, Axi, Requiem, Vanguard}` and Quality in `{Intact, Exceptional, Flawless, Radiant}` (l.17-21, 171-182).
- For each reward `r` of each quality: `name = r.item.name`, `urlName = slug(r.item.warframeMarket.urlName ||
  .url_name)`, `ducats = r.item.ducats`, `rarity = relicRewardRarity(quality, r.chance, r.rarity)`.
  Rarity uses the per-quality chance table in `services/relicRarity.ts:5-34`: intact
  25.33/11/2, exceptional 23.33/13/4, flawless 20/17/6, radiant 16.67/20/10 for Common/Uncommon/Rare, each ±0.5.
- De-duplicate by name. **The first occurrence's rarity, ducats and urlName win.** `vaulted` = AND over every
  relic group that drops the item. A group is vaulted only if `relic.vaulted` is true **and** the relic has 0 drop locations
  (`isRelicVaulted`, l.147-150, working around @wfcd mis-flagging Citrine relics).
- Output item: `{name, uniqueName, urlName, rarity, ducats, vaulted}`, sorted by name.
- Slug normalisation (`normalizeWfmSlug`, config/shared/wfm.ts:96-98): keep a valid WFM slug
  (`/^[\p{Ll}\p{N}](?:[\p{Ll}\p{M}\p{N}_()'’-]{0,118}[\p{Ll}\p{N})])?$/u` after lowercasing). Otherwise slugify:
  lowercase, remove `'’‘`, replace runs of non-`[a-z0-9]` with `_`, and trim `_`.

---

## 9. Enrichment: owned counts, set progress, vaulted (`ipc/overlay/scan.ts:139-293`)

The input is a scanned item plus the player's inventory JSON (DE `inventory.php` shape: `MiscItems`, `Recipes`,
`PendingRecipes`, equipment collections). WFHelper gets the inventory from its inventory-sync feature (outside
this area). The Decky backend can get it from game memory or the API.
- `uniqueName` = lookup by display name (lowercased) or WFM slug in the item DB, else the item's own uniqueName.
- `parent` = `itemDB[uniqueName].componentOf`. `ducats` = the item's ducats if it is a positive integer, else the DB value.
- Owned counts (config/shared/componentOwnership.ts): sum `ItemCount` (default 1) over `MiscItems` and `Recipes`
  after removing foundry-pending copies (`withoutFoundryPending`, config/shared/foundryPending.ts:38-55). Add +1 per row in
  `Suits, LongGuns, Pistols, Melee, Sentinels, SentinelWeapons, SpaceSuits, SpaceGuns, SpaceMelee, OperatorAmps,
  MechSuits, Hoverboards, MoaPets, KubrowPets, SpecialItems`.
- Alias rule (config/shared/componentNames.ts:7-16): `X…Blueprint` ↔ `X…Component`, and any `/Types/Recipes/` path without
  `Blueprint` also matches `<path>Blueprint`. The owned count is the max over aliases.
- `partRequiredCount` = the parent's component `itemCount` (default 1). `partOwnedCount` = owned(uniqueName).
- Set progress (scan.ts:166-205), over parent components with `tradable !== false`:
  `required += need; owned += min(count, need); completeSets = min(floor(count/need))`. Each part reports
  `{name, imageUrl, ownedCount, requiredCount, isReward, building}`.
- `setName = parentName + " Set"` only if there are at least 2 tradable parts. `setUrlName = slug(setName)`.
- `mastered` comes from mastery progress of the parent (absent for non-masterables).
  `building = true` if the item's blueprint is in `PendingRecipes`.
- `vaulted` is passed through only as a boolean, and never for "Forma Blueprint" (including a quantity-prefixed one) (scan.ts:231-266).

---

## 10. Pricing

### 10.1 Which price

- **Slot price** = the WFM item statistics price for `item.urlName`. **Set price** = the same for `item.setUrlName`.
  Both are fetched from the overlay (renderer/overlay.js:708-728), in parallel for every slot.
  Items without a `urlName` get price 0 (shown as N/A); the set price is still fetched.
- Lookup order in the main process (rewardOverlayIpc.ts:318-330): in-memory stats cache (5 min TTL) → the
  "snapshot" file `userData/snapshot-cache.json` (if its mtime is under 48 h old; `prices[slug].median` with
  status ok, else `orderSummaries[slug].wts`, else `.wtb`; relicSelection.ts:280-345, 542-549) → a live WFM call.
- The renderer rounds the price: `round(abs(raw))`. A value of 0 or below, or not finite, is treated as missing (overlay.js:83-98).
- **Ducats**: from the item list and enrichment (no network). They are shown next to platinum.
- **Best pick** = the slot with the highest platinum price. Ties go to the lower index. Ducats are ignored (overlay.js:345-378).
  The crown is updated again once all prices arrive, or after at most `BEST_PICK_SETTLE_CAP_MS = 4000`.

### 10.2 WFM statistics request (`services/wfmStatsPrice.ts`, `services/wfmClient.ts`)

```
GET https://api.warframe.market/v1/items/{slug}/statistics
Accept: application/json
Content-Type: application/json
Platform: pc
Language: en
(+ a browser User-Agent; WFHelper may add Cloudflare clearance cookies, see wfmClient.ts:105-240)
```
Example response, trimmed to the fields that are read:
```json
{"payload": {
  "statistics_closed": {
    "48hours": [{"datetime":"2026-10-16T13:00:00.000+00:00","volume":3,"wa_price":41.2,"median":40,"order_type":"sell"}],
    "90days":  [{"datetime":"2026-09-01T00:00:00.000+00:00","median":38}]
  },
  "statistics_live": {
    "48hours": [{"datetime":"…","median":45,"order_type":"sell"}, {"…":"…","order_type":"buy"}]
  }
}}
```
Price algorithm (`extractAverageFromStatsPayload`, config/shared/wfmStats.ts:69-116; basis id `closed-volume-average-48h-v1`):
```
rows = statistics_closed["48hours"] or ["48_hours"]          # none -> None
keep rows with order_type in (None, "sell"), rank pool (rank None: rows with no mod_rank or mod_rank 0),
     datetime in [now-48h, now], wa_price > 0, volume > 0; de-duplicate on (datetime, rank)
reference = median(statistics_live 48h sell medians)  or, if there are none,
            median(statistics_closed 90days medians with datetime < now-48h, sell or untyped)
drop samples with wa_price > 10 * reference (when reference > 0)       # fake closes
price = round(sum(wa_price*volume)/sum(volume)); None if volume == 0 or price <= 0
```
- Cache: up to 5000 slugs, TTL `WFM_STATS_CACHE_TTL_MS = 5 min`, with concurrent requests for the same slug de-duplicated (wfmStatsPrice.ts:10-78).
- **Rate limit** (services/wfmScheduler.ts:19-46): a token bucket of **2.5 req/s sustained, burst 5**, at most 3 concurrent,
  queue depth 64. Retry GET up to 3 times with backoff 750 ms × 2^n, capped at 2500 ms plus 0-25 % jitter. On a 429,
  stop all requests for `Retry-After` (clamped to 1 s - 5 min), or 30 s if the header is missing.
  A 4-slot crack needs 8 requests (4 parts plus 4 sets), so with the burst of 5 the last prices land after about 1.2 s.
- **Risk:** WFM v1 is deprecated in favour of v2, and v2 has no statistics endpoint. Prefetch and cache prices for every
  reward slug (a few hundred) in the background, or use WFHelper's snapshot
  (`GET https://api.wfhelper.com/v1/snapshot?client=closed-average-v3`, header `x-wfhelper-client: <product>/<version>`,
  ETag/304, considered fresh for 2 h; body `{version, generatedAt, prices{slug:{status,median,timestamp,priceBasis}},
  orderSummaries{slug:{status,wts,wtb,timestamp}}, meta{slug:{slug,ducats,setRoot,thumb,icon,timestamp}}}`;
  src/lib/wfm/snapshotLoader.ts:17-140). Using WFHelper's backend from another client should be cleared with its author first.

---

## 11. Overlay presentation

### 11.1 Payload sent to the overlay (`RELIC_REWARD_ITEMS`)

Either an array of enriched items, or `{items: [], failureReason: "ocr-unavailable" | "capture-unavailable"}`.
Item fields that are used: `name, slotIndex, rarity, urlName, ducats, vaulted?, partOwnedCount, partRequiredCount,
mastered?, building?, setOwnedCount, setRequiredCount, setParts[≤6], setName, setUrlName`.

### 11.2 How WFHelper lays it out

WFHelper does **not** draw over each in-game card. It draws **one panel** (renderer/overlay.html, overlay.css,
overlay.js):
- Canvas `980x236` logical px (config/shared/overlayLayout.ts:542-548). It is horizontally centred on the display
  work area. Its top edge is at `y = area.h * anchorRatio`: `anchorRatio = clamp(bandBottomRatio + 0.04, 0.32, 0.82)`
  when the scan reported a band (only the text fallback does), else **0.56** (ipc/overlay/windows.ts:28-38, 325-358, 371-426).
  The height grows to fit the content (`RELIC_REWARD_CONTENT_HEIGHT`). Margins: x 16, top 8, bottom 18.
  Zoom = `baseZoomForDisplay` (shortest edge ≤ 720 → 0.8, ≤ 900 → 0.9, ≤ 1200 → 1, ≤ 1600 → 1.15, else 1.3;
  config/runtime/uiScale.ts:17-28) × the user's `overlayScale` (0.75-1.5).
- `#slots-grid` always has **4 equal columns** (gap 8 px, padding `8px 10px 6px`). Items go to column `slotIndex` when every
  item has a unique, valid slotIndex, else in read order (overlay.js:660-673). Columns without an item are drawn at 42 % opacity with "-".
- A card (`.reward-slot`, min height 92 px, radius 8, bg `rgba(255,255,255,.04)`, border `rgba(255,255,255,.08)`,
  with an item `rgba(212,168,67,.28)`):
  1. `Slot N` (9 px, uppercase, muted)
  2. item name (14 px, at most 2 lines, shrunk to fit)
  3. rarity label (Rare `#d4a843`, Uncommon `#94a3b8`, Common `#64748b`) + values: platinum icon + `round(price)`, ducat icon + ducats.
     Placeholder: `...` while the price is null, `N/A` when it is 0 or missing.
  4. chips: `Own {owned}/{required}`; `Mastered` / `Not Mastered`; `In foundry`; `Set {owned}/{required}`;
     `Set {value}` (`...` / `{n}p` / `N/A`); `Vaulted` / `Unvaulted` (**hidden by default** via layout
     `hiddenByDefault: ["vaulted"]`); then up to 6 set-part chips (icon + `owned/required`; green when owned ≥ required,
     orange outline when building, highlighted when it is this reward).
  - The best slot gets an accent border `#d4a843` and a tinted background. The footer reads `Best: <name> - <plat icon> <price>`.
    Before prices arrive it shows `Detecting...` or `No priced rewards yet`.
  - Panel colours: bg `#060a12` at 92 %, surface `#111827`, text `#e8e4dc`, muted `#8b93a5`, accent `#d4a843`/`#f0c95c`.
- Strings (src/i18n/en.json): scanning "Reading reward screen...", failure "OCR failed to detect reward items from the
  current screen. …", short "OCR failed", capture failure "WFHelper got no screen capture. …".
- The per-field layout editor (renderer/reward-layout.js, `rewardLayout` setting: `{version:1, fields:{<field>:{x,y,scale,color,hidden,cards?}}}`)
  lets users move or hide fields. This is optional for the port.

### 11.3 Recommended Deck mapping

The Pillow overlay can do better than WFHelper here because the card rects are known exactly (§4.5):
- Draw one value block per detected slot, directly **below each card**, at the card's x and width
  (156 px wide at UI 0.99; use the UI scale the scan actually used, which `findRewardCardsInBitmap` can change). The cards
  end at y ≈ 363. The in-game timer, buttons and party icons sit below that, so leave room. Alternatively, use WFHelper's
  single panel scaled to 1280 px wide (0.8 zoom × 980 = 784 px, centred, top at 0.56 × 800 = 448).
- Each block needs: name (or just rarity colour), platinum, ducats, `Own x/y`, and set progress. Mark the best plat.
- Keep the overlay hidden until the first result arrives (an EE.log trigger never shows "scanning"). Clear it on the close line
  or when the auto-hide timer from §2 fires.
- At 1280x800 the 156 px card width fits about 20 characters at 12 px. Abbreviate "Blueprint" → "BP" if needed.

---

## 12. Test fixtures (`scripts/reward-scan-e2e/`)

### 12.1 Public full frames (`fixtures/public/`, manifest.json verbatim values)

| File | Size | sha256 | Expected slot 0..N-1 | Gated meta |
|---|---|---|---|---|
| `real-full-2p.png` | 1920x1080 | `9d94b90522d67ad598b28272d2902e53011790833dd26dfbf0fa595e9359ce24` | Khora Prime Systems Blueprint, Fang Prime Handle | cardCount 2, layoutCount 1 |
| `real-full-4p-fps.png` | 1920x1080 | `609dcdbc621a7ae5e3bc7e164963f446c71994e1904d1a10300a08a95ec3536b` | Xaku Prime Chassis Blueprint, Bronco Prime Receiver, Paris Prime Lower Limb, Vadarya Prime Barrel | cardCount 4, layoutCount 1 |
| `real-full-4p-16x10.png` | 1899x1199 | `7e0ce9089016a4796e47367670786eb47f3a658d03d7b8d24ce562ee7c198647` | manifest says 4x "2 X Forma Blueprint". run-check accepts **"Forma Blueprint" or "2X Forma Blueprint"** for each slot | - |
| `real-full-4p-1080x607.png` | 1080x607 | `7c9e07be5ea4fce9a423e35932e2a6bdd8426999fb8c3b9bcc899e7922bb98ad` | Okina Prime Handle, Velox Prime Barrel, Caliban Prime Blueprint, Grendel Prime Blueprint | - |

Masks are opaque rectangles over names and icons, outside the cards: 2p `(94,43,96,50)`, `(1047,494,96,39)`; fps
`(93,43,191,49)`, `(510,496,870,34)`; 16x10 `(73,43,192,50)`, `(715,557,481,61)`, `(123,1156,291,27)`; 1080x607
`(51,23,110,29)`, `(540,277,144,72)`, `(149,545,278,40)`. Capture dates, game versions and UI scale are unknown.
16x10 is the **only real taller-than-16:9 frame**. All four of its slots hold the same item, so it proves the
y-correction reads text, but it cannot detect swapped slots (README.md).

### 12.2 Slot-title crops (`fixtures/slot-crops/`) and reconstructed frames (`build-screens.cjs`)

The file names encode the original 1080p screen position: `<N>_players_slot<i>_title_<x>x<y>_<w>x<h>.png`
(RGBA). There are 2p (326x44 at x 710/1037), 3p (288x44 at 557/845/1133) and 4p (234x44 at 470/714/958/1202), all at y 413.
`build-screens.cjs` builds `real-{2,3,4}p.png`: a 1920x1080 background `rgb(24,16,16)`, card scaffolds
`rgb(146,138,138)` 234 px wide from y 243 to 410 at x = 2p `721, 965`; 3p `599, 843, 1087`; 4p `470, 714, 958, 1202`,
with the crops composited at their file-name coordinates.
Expected (run-check.cjs:38-216):
- `real-4p.png`: Epitaph Prime Receiver, Forma Blueprint, Zephyr Prime Neuroptics Blueprint, Wukong Prime Chassis Blueprint
- `real-3p.png`: Forma Blueprint, Caliban Prime Neuroptics Blueprint, Nautilus Prime Systems
- `real-2p.png`: Braton Prime Stock, Trumna Prime Blueprint
- Derived from real-4p (same expected values): `sim-aspect-16x10.png` (padded to 1920x1200 with `rgb(24,16,16)`),
  `sim-aspect-4x3.png` (1920x1440), `sim-aspect-21x9.png` (2560x1080), `sim-4k-4p.png` (Lanczos 3840x2160).
  **Port tip:** also generate a `sim-deck` frame, real-4p padded to 1920x1200 and then resized to 1280x800.
- Synthetic frames (`make-synthetic-screens.ps1`, PowerShell/GDI, **Windows-only**; expected Vadarya Prime Stock,
  Perigale Prime Blueprint, Pangolin Prime Handle, Yareli Prime Chassis Blueprint). Rebuild them with Pillow if needed.
- Local-only fixtures that are not in the repo (`fixtures/screens/`: real-full-1p-windowed*, real-full-4p-oldui90) are skipped when absent.

### 12.3 Test harness semantics

`run-check.cjs` checks the manifest sha256 values, scans each frame with a reset frame dedup, and checks
`items[k].slotIndex == slot → name`, plus `expectMeta` keys against `result.meta`. Unit-test equivalents exist for
the card counter (tests/main/rewardCardCounter.test.ts, which draws synthetic card bars: cards `0.122W` on a `0.127W` pitch,
bar at y `0.443` with height `max(2, round(0.004H))`, Legacy bar 3 px at `round(H/2)-53`), the matcher, and the log parsers.

### 12.4 Results from the throwaway Python check (not in the repo)

Running the §4-§6 algorithms with numpy, Pillow and onnxruntime against the public frames:
- 2p and fps: the card-bar counter at UI 0.99 returns 2 and 4, and every OCR read is exact.
- 1080x607: the counter misses at 0.99, but the scale sweep (§5.1) finds 4. The reads need fuzzy matching ("Prme", "Bluerint").
- **16x10 (and 16x10 resized to 1280x800): the card-bar counter finds nothing at any scale.** The activity layouts
  (§5.2) rank 4-card (conf 0.94; 0.916 at 1280x800) first, and those reads give "2 XForma Blueprint" per slot.
  So on the Deck, expect the **activity fallback, not the counter, to set the slot count**.

---

## 13. Windows-only parts and their Linux/Deck replacements

| WFHelper piece | Windows | Linux path in WFHelper | Deck plan |
|---|---|---|---|
| EE.log location | `%LOCALAPPDATA%` | Steam/compatdata/launcher prefix search | compatdata/230410 under `/home/deck` or SD |
| Real-time log | DBWIN OutputDebugString (`dbwinMonitor.ts`) | Proton log `~/steam-230410.log` (`PROTON_LOG=1`) | same, or EE.log only |
| Capture | GDI BitBlt + client rect | getDisplayMedia portal + window geometry | gamescope screenshot |
| OCR | Windows.Media.Ocr via `ocrServer`/`ocr.ps1`, `binarizeRewardRegion` 3 bands | ONNX paddle only | ONNX paddle only |
| Text fallback (`matchItemsDetailed` over a band) | yes | skipped (reader `onnx`) | skip |
| Focus check | Win32 foreground window | display-backend specific | check that the game process is running |
| Synthetic test screens | `make-synthetic-screens.ps1` | - | rebuild with Pillow |

---

## 14. Porting risks (summary)

1. **16:10 slot count:** the card-bar counter fails on the only real 16:10 frame, so slot count depends on activity
   scoring. A 2-card screen can tie with the 4-card layout; the empty-slot penalty decides. Test with real Deck captures
   for 1, 2, 3 and 4 players before trusting it.
2. **Small strips:** Deck title strips are 156x29 px (`targetW 468`). The 1080x607 fixture (131x24) already shows misreads
   that only the fuzzy matcher recovers. Sharp vs Pillow resampling and the `normalise` percentile stretch can move Otsu thresholds.
3. **Trigger latency:** without `PROTON_LOG=1`, EE.log flush lag can push the capture past the reward screen. Lines staler than 20 s
   are dropped. Measure EE.log flush latency on a Deck.
4. **WFM v1 statistics deprecation and rate limits:** prefetch prices or use a snapshot. Respect 2.5 req/s and Retry-After.
5. **Root vs deck user paths:** use `/home/deck`, not `~`, for EE.log, EE.cfg and the Proton log.
6. **UI scale:** read EE.cfg `Flash.FlashDrawScale`. Legacy mode (`DSM_MATCH_SCREEN`) has no ratio model at 800 px
   (legacy 1.23 is out of range), so only the sweep can find it.
7. **ONNX CPU contention with the game:** use 2 intra-op threads and warm up the session at plugin load.

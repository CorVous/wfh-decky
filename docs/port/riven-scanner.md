# Port spec: Riven scanner overlay

Source: WFHelper at `/home/claude/wfhelper-src` (commit `06fa689`, MIT). All `file:line` references are relative to that tree.
Target: Decky plugin on Steam Deck LCD (1280x800, gamescope, Proton, English game UI). A Python worker does OCR and draws the overlay with Pillow onto an X11 ARGB window that has `GAMESCOPE_EXTERNAL_OVERLAY` set. The Quick Access Menu (QAM, React with @decky/ui) handles control and detail views.

Sections 1-15 describe how WFHelper works today. Section 16 gives the Deck layout. Section 18 lists the porting risks. Appendices A-D copy the parser, the OCR post-processing and the stat tables verbatim. They are the parts where an approximation would visibly lower accuracy.

---

## 0. Source map

| Concern | File | Notes |
|---|---|---|
| EE.log riven state machine | `services/rivenLogStateMachine.ts` (459 lines) | Regexes, cooldowns, callbacks |
| Feed of lines into it | `services/eeLogMonitor.ts:399,510,758-767,810-813` | `processRivenPatterns(line, source, realtimeSourceActive())` |
| Orchestrator (timers, weapon, grading, enrichment) | `ipc/rivenOverlayIpc.ts` (904 lines) | Main logic of the feature |
| Session counters and event fan-out | `ipc/overlay/rivenSession.ts` | rollCount, kuva |
| Scan profiles and retry | `ipc/overlay/rivenScan.ts` | `scanInitialCard`, `scanNewRoll`, `scanChoiceRescan` |
| Crop geometry, readiness gate, frame hash | `ipc/overlay/rivenScanImage.ts` | |
| 16:9 canvas and letterbox detection | `services/rewardScannerImage.ts:11-120,213-231` | Shared with the reward scanner |
| Recognition loop (retries, completeness, confidence) | `ipc/overlay/rivenScanOcr.ts` | |
| YOLO and PaddleOCR | `services/rivenOcrOnnx.ts` | Models in `resources/riven-ocr/` |
| OCR text to stats parser | `ipc/overlay/rivenScanText.ts` (750 lines) | Copied verbatim in Appendix A |
| FITS IN weapon label reader | `ipc/overlay/rivenWeaponLabel.ts` | Uses the reward strip OCR (`services/rewardOcrOnnx.ts`) |
| Weapon and stat database | `services/rivenData.ts` | Built from `warframe-public-export-plus` |
| Grading (roll float, letter grade) | `services/rivenGrading.ts`, `services/rivenConstants.ts` | |
| Good-roll sheet (best attributes, attribute grade) | `services/rivenBestAttributes.ts`, `config/shared/rivenGoodRolls.ts` | Google Sheet CSV |
| Inventory fingerprint decode | `services/rivenFingerprint.ts` | Not used by the overlay. It shares the formulas. |
| WFM similar listings | `services/wfmRivenSearch.ts`, `services/wfmClient.ts`, `services/wfmScheduler.ts` | |
| Shared tables | `config/shared/rivenStatDisplayNames.ts`, `wfmRivenVocabulary.ts`, `rivenTypes.ts`, `rivenEndo.ts`, `weaponVariants.ts` | |
| Overlay UI | `renderer/riven-overlay.{html,js,css}`, `renderer/riven-similarity.js`, `preload-riven.ts` | Two windows, `?side=left/right` |
| Panel canvas | `config/shared/overlayLayout.ts:576-609` | 420x640 CSS px each |
| Fixtures | `tests/fixtures/riven/*`, `assets/setup/overlay-demo-riven.jpg` | See §17 |

---

## 1. End-to-end flow

```
EE.log line ──► state machine (§2) ──callbacks──► orchestrator (§3)
                                                   │
         ┌─────────────────────────────────────────┤
         ▼                                         ▼
 capture + crop (§5) ─► YOLO+Paddle (§7) ─► parser (§8) ─► stats[]
                                                   │
 weapon id (§9: EE.log dialog / diorama path / card title / FITS IN plate)
                                                   │
 grading (§11) + good-roll sheet (§12) + WFM similar listings (§13)
                                                   │
                        overlay events (§4) ─► two panels (§16)
```

How the game behaves (English UI):
1. The player opens a riven's **Cycle** screen. The game logs `Created /Lotus/Interface/OmegaRerollSelection.swf`. **Session open**: both panels show and the **initial scan** runs after 200 ms. One centered card is visible.
2. The player presses CYCLE FOR N kuva. The game logs the confirm dialog `Are you sure you want to cycle <Weapon> for <N>?`, which gives the weapon family and the cost (`rollPending`). The player clicks OK: `Dialog::SendResult(4)` (`rollConfirmed`). The **roll scan** runs 2850 ms later on the `rollCard` crop, while the new card is revealed.
3. The player picks a card. The game logs the dialog `Cycle Riven into current selection?`, then `SendResult(4)` (`choiceConfirmed`). The **choice rescan** reads the single remaining card 1200 ms later, and similarity scoring decides which side was kept.
4. The player leaves the screen: `NpcManager::ClearAgents() ReadyToCreateAgents = false` or `...ytes of recycled effects`. **Session close**.
5. A riven linked in chat (the item preview dialog) is a separate, left-panel-only flow. It starts with `HudVis` going up and a `PopulateInfo->/Lotus/StoreItems/Upgrades/Mods/Randomized/` line within 2 s (§2.4).

---

## 2. EE.log state machine (`services/rivenLogStateMachine.ts`)

### 2.1 Regexes (verbatim, `:33-54`)

```ts
sessionOpen:  /Sys \[Info\]: Created \/Lotus\/Interface\/OmegaRerollSelection\.swf/
sessionClose: /NpcManager::ClearAgents\(\) ReadyToCreateAgents = false/
hudVis:       /ThemedDetailedPurchaseDialog\.lua: DBG: HudVis (\d+)/
populateRiven:/ThemedDetailedPurchaseDialog\.lua: PopulateInfo->\/Lotus\/StoreItems\/Upgrades\/Mods\/Randomized\//
cycleConfirmEn:  /Dialog::CreateOkCancel\(description=Are you sure you want to cycle (.+?) for ([\d,. ]+)\?/
choiceConfirmEn: /Dialog::CreateOkCancel\(description=Cycle Riven into current selection\?/
genericDialog:   /Dialog::CreateOkCancel\(/
genericDialogNonInteractive: /leftItem=nil/
sendResult:      /Dialog\.lua:\s*Dialog::SendResult\((\d+)\)/
dioramaSetup:    /OmegaRerollSelection\.lua.*Diorama setup/i
dioramaWeaponLoad: /(?:ResourceLoader|Resloader|Resource load completed) 0x[0-9A-Fa-f]+ \((\/Lotus\/Weapons\/[^)]+)\)/
recycledEffects: /ytes of recycled effects/
```

Real sample lines from `tests/main/rivenOverlay.test.ts`:
```
Sys [Info]: Created /Lotus/Interface/OmegaRerollSelection.swf @ 0x12345678 of class OmegaRerollSelectionScreen
Script [Info]: Dialog.lua: Dialog::CreateOkCancel(description=Are you sure you want to cycle Burston Sati-critades for 3.500?, leftItem=/Menu/Confirm_Item_Yes, rightItem=/Menu/Confirm_Item_No)
Dialog::CreateOkCancel(description=Cycle Riven into current selection?, leftItem=/Menu/Confirm_Item_Yes, rightItem=/Menu/Confirm_Item_No)
Script [Info]: Dialog.lua: Dialog::CreateOkCancel(description=/Lotus/Language/Menu/NavBar_QuickMatchPleaseWait, leftItem=nil, rightItem=nil)   <- non-interactive, ignored
Dialog.lua: Dialog::SendResult(4)          <- 4 = OK; anything else = cancel
NpcManager::ClearAgents() ReadyToCreateAgents = false
530.304 Script [Info]: ThemedDetailedPurchaseDialog.lua: DBG: HudVis 1
530.357 Sys [Info]: ResourceLoader 0x000001ABBE845340 (/Lotus/Weapons/Tenno/Shotgun/PrimeBoar) Found 1,081 items to load
530.415 Script [Info]: ThemedDetailedPurchaseDialog.lua: PopulateInfo->/Lotus/StoreItems/Upgrades/Mods/Randomized/LotusShotgunRandomModRare
48.096 Sys [Info]: Resource load completed 0x00000245D4699890 (/Lotus/Weapons/Grineer/KuvaLich/LongGuns/Sobek/KuvaSobek) in one pass and 0.1s (...)
```
The cycle dialog's weapon group is the **riven name or the weapon family**, e.g. `Burston Sati-critades` or `Arca Plasmor`, and never the exact variant. The cost has thousands separators `4,000`, `3.500`, `4 000`, and `parseInt(cost.replace(/[,. ]/g,""))` gives 4000. A comment at `ipc/rivenOverlayIpc.ts:632` notes that "the cycle dialog logs a language key these days". So `cycleConfirmEn` may stop matching, and the generic fallback (§2.3 step 9) then produces `rollPending("", 0)`. The port needs both paths.

### 2.2 Constants (`:87-135`)

| Name | Value | Meaning |
|---|---|---|
| RIVEN_SESSION_IDLE_TIMEOUT_MS | 600 000 | Backstop close when no riven event arrives for 10 min (timer is reset by riven events) |
| RIVEN_SESSION_MAX_MS | 30 min | Hard cap. Checked at the top of each processed line |
| RIVEN_SEND_RESULT_COOLDOWN_MS | 400 | Debounce for a confirmed SendResult(4) |
| RIVEN_GENERIC_DIALOG_COOLDOWN_MS | 600 | Generic dialog ignored within 600 ms after the last accepted SendResult |
| RIVEN_CHOICE_DIALOG_COOLDOWN_MS | 2000 | Throttles only the log line |
| RIVEN_SESSION_OPEN_COOLDOWN_MS | 2000 | Ignore a re-open within 2 s |
| RIVEN_CLOSE_MARKER_SPACING_MS | 2000 | Trailing close markers ignored (only when real-time) |
| RIVEN_DIORAMA_DEDUP_MS | 2000 | |
| RIVEN_WEAPON_PATH_WINDOW_MS | 15 000 | Accept a diorama weapon load ≤15 s after open, once per session |
| RIVEN_FORCE_END_COOLDOWN_MS | 5000 | After a force-end, cycle/choice dialogs cannot resurrect a session for 5 s |
| CHAT_RIVEN_POPULATE_WINDOW_MS | 2000 | PopulateInfo must follow a HudVis increase within 2 s |

### 2.3 `processRivenPatterns(line, source, dbwinActive)` step by step (`:179-411`)

`source` is `"dbwin"` for real-time lines and `"file"` for EE.log file-poll lines. `skip = dbwinActive && source==="file"`: when a real-time feed is live, file lines are echoes. They are ignored for every riven pattern **except** diorama setup and diorama weapon load (accepted from either source), and SendResult is still marked consumed.

State: `pendingDialog ∈ {null,"roll_confirm","choice"}`, `sessionActive`, `sessionStartedAt`, `nextDialog ∈ {"cycle","choice"}`, `chatViewActive`, `chatHudVisLevel`, `chatWeaponPath`, `dioramaReady`, `weaponPathSent`, `lastHudVis`, `lastHudVisIncreaseAt`, plus the timestamps above.

1. `forceEndIfExpired()`: if active and `now - startedAt ≥ 30 min`, run endSessionState, set `forceEndedAt = now` and fire `onSessionClose`.
2. **Open**: if `!skip && sessionOpen` matches: if `!active && now - lastOpenAt ≥ 2000`, set active, startedAt = now, reset chat state, `nextDialog="cycle"`, `pendingDialog=null`, `weaponPathSent=false`, reset the idle timer and fire **`onSessionOpen`**.
3. **Weapon path**: if `dioramaWeaponLoad` matches and active, `!weaponPathSent` and `now - startedAt < 15 s`, set `weaponPathSent=true` and fire **`onWeaponPath(path)`**.
4. **Diorama ready**: if active and `dioramaSetup` matches with ≥2 s dedup, set `dioramaReady=true`, reset the idle timer and fire `onDioramaSetup` (a no-op in the orchestrator).
5. **Close**: if `!skip`, active, `dioramaReady`, and `sessionClose` or `recycledEffects` matches: `trailing = dbwinActive && now-lastCloseMarker < 2000 && now-lastCloseDispatch < 4000`. Always set `lastCloseMarker = now`. If not trailing: `lastCloseDispatch = now`, endSessionState, fire **`onSessionClose`**. *Close is gated on diorama readiness because these lines also fire during screen loading.*
6. **HudVis** (only when `!skip`, the line matches and **no** reroll session is active): `v = int`. If `chatViewActive && v < chatHudVisLevel`, the chat view closes: reset the chat state and `lastHudVisIncreaseAt=0`, then fire `onSessionClose`. Else if `v > lastHudVis`: clear `chatWeaponPath` unless the chat view is active, and set `lastHudVisIncreaseAt = now`. Else if `!chatViewActive && v < lastHudVis`: clear the path and the increase timestamp. Always set `lastHudVis = v`.
7. While not in a session or chat view and within 2 s of a HudVis increase, a `dioramaWeaponLoad` match is remembered as `chatWeaponPath`.
8. **Chat view confirm**: `!skip`, no session, no chat view, `lastHudVisIncreaseAt>0`, `populateRiven` matches and within 2 s: set `chatViewActive=true` and `chatHudVisLevel = lastHudVis`, fire **`onChatView`**, then `onWeaponPath(chatWeaponPath)` if one was remembered.
9. **Dialogs** (`handled = skip` initially):
   - `cycleConfirmEn` matches (when `!skip`), unless (`!active && now-forceEndedAt < 5 s`): set `handled`, `active=true`, reset idle, `pendingDialog="roll_confirm"`, fire **`onRollPending(weapon.trim(), cost)`**. Note that this can *start* a session without an open line.
   - Else `choiceConfirmEn` (same force-end guard): set `handled`, `active=true`, reset idle, `pendingDialog="choice"`.
   - Else the generic fallback: `!handled && active && pendingDialog===null && now-lastSendResultAt ≥ 600 && genericDialog && !genericDialogNonInteractive`. Then if `nextDialog==="cycle"`, set `pendingDialog="roll_confirm"` and fire `onRollPending("",0)`. Otherwise set `pendingDialog="choice"`.
10. **SendResult**: if it matches while active and `skip`, it is consumed (return true). If it matches with `!skip` and (`pendingDialog!==null || active`): consumed; reset idle if active. If `pendingDialog !== null`: code `"4"` with ≥400 ms since the last accept: for `roll_confirm`, set `nextDialog="choice"` and fire **`onRollConfirmed`**; for `choice`, set `nextDialog="cycle"` and fire **`onChoiceConfirmed`**. Any other code is a cancel. Then `pendingDialog=null`.
11. Return `consumed`. The EE.log monitor uses this so the relic-picker close handler does not also act on the same SendResult.

Other entry points: `resumeRivenSession()` (`:418`) re-arms when the user presses the manual rescan and no open marker is present. It sets active, `dioramaReady=true` and clears the force-end. `forceEndRivenSession()` (`:432`) resets everything, including HudVis tracking, and sets `forceEndedAt`. The orchestrator calls it on close.

### 2.4 Linux/Deck line sources
- File: `<SteamLibrary>/steamapps/compatdata/230410/pfx/drive_c/users/steamuser/AppData/Local/Warframe/EE.log` (`services/eeLogPath.ts:38-53`). WFHelper scans every `compatdata/*` folder and picks the newest EE.log, because a non-Steam shortcut gets a random id. Library roots come from `steamapps/libraryfolders.vdf`, matched with `/"path"\s+"([^"]*)"/g`. On a Deck these are `/home/deck/.local/share/Steam` and SD cards under `/run/media/...`. WFHelper polls the file every 500 ms. The engine flushes EE.log lazily, **10 s or more** at quiet moments.
- Real-time on Linux (`services/protonDebugstrMonitor.ts`): tail `~/steam-230410.log` (env `WFHELPER_PROTON_LOG` overrides). It exists only when the Warframe launch options contain `PROTON_LOG=1 %command%`. Lines match `/(?:trace|warn):(?:debugstr|seh):OutputDebugString[AW]\s+(L?)"/` and are C-unescaped. The monitor polls every 250 ms and counts as "active" only if the file grew within 30 s. These lines are tagged `"dbwin"`.
- On Windows, DBWIN (OutputDebugString shared memory) is the real-time source. It is **Windows-only**.
- The backend runs as root, so it must resolve `/home/deck` explicitly rather than `~`.

---

## 3. Orchestrator (`ipc/rivenOverlayIpc.ts`)

### 3.1 State
`hasRollResult`, `initialStats[]`, `newRollStats[]`, `weaponName` (`""` or `"Riven"` means unknown), `weaponSource ∈ {"" ,"dialog","ocr","diorama","label"}`, `weaponLabelExact`, `scanLayout ∈ {"reroll","chat"}`, `sessionToken` (incremented on every open, close or rescan), `rollScanGeneration` (begin, invalidate, isCurrent; `rivenSession.ts:30-47`), and timers `initialScanTimer` and `rollScanTimer`.

### 3.2 Timing constants (`:347-353`)
`INITIAL_SCAN_DELAY_MS=200`, `ROLL_SCAN_DELAY_MS=2850`, `CHOICE_RESCAN_DELAY_MS=1200`, `ROLL_STALE_RESCAN_DELAY_MS=1100`, `MAX_ROLL_STALE_RESCANS=2`, `SIMILAR_LISTING_COUNT=30`.

### 3.3 Handlers
- **onSessionOpen** (`:741`): return if the setting is off. Reset state, invalidate the roll generation, `sessionToken++`, create or show both panels, emit `SESSION_START("Riven", 0)`, then `triggerInitialScan("reroll")`.
- **onChatView** (`:681`): ignored if `hasRollResult` is set. Reset as above, show the **left panel only**, hide the right, emit `SESSION_START`, then `triggerInitialScan("chat")`.
- **triggerInitialScan(layout)** (`:575`): after 200 ms, run `scanInitialCard(layout)` (§6). Then `initialStats = stats`, `maybeDetectWeaponFromText(titleText || rawText)`, emit `INITIAL_STATS(stats, lowConfidence)`, emit `GRADING_INITIAL` if the stats can be graded, and if a capture exists run `detectFitsInWeapon(capture)` asynchronously (§9.3).
- **onRollPending(weapon, kuva)** (`:762`): `hasRollResult=false`. `isFirstReveal = weaponName in ("","Riven")`. `keepLabelVariant = !isFirstReveal && weaponSource==="label" && (labelExact || familySlug(weapon)===familySlug(weaponName))`. If `weapon` is non-empty and not keepLabelVariant: set `weaponName=weapon`, `source="dialog"`, emit `WEAPON_UPDATE(weapon)`; on the first reveal also emit graded initial stats and start enrichment (§3.5).
  - The orchestrator ignores `kuvaPerRoll` after this point. `rivenSession.startSession` is always called with 0, so `totalKuvaSpent` stays 0. That looks like a latent bug. The port may wire the cost through.
- **onRollConfirmed** (`:791`): emit `ROLL_SCANNING`, then `triggerRollScan(2850)`.
- **triggerRollScan** (`:600`): `serial = gen.begin()`. After the delay, return if the serial is stale. `knownCards = [initialStats, newRollStats]`. `panels = scanNewRoll()`. Then up to 2 rescans: `reason = rollRescanReason(panels.right, knownCards)`, which is `"read nothing"` if empty or `"matches a pre-roll card"` if `looksLikeStaleCardRead` (§8.6). For each, sleep 1100 ms and rescan. If the result is still stale, `newRollStats=[]` and emit `ROLL_RESULT(left=initialStats, right=[])`, which is treated as failure. Otherwise `maybeDetectWeaponFromText(rawText)`, `newRollStats=right`; if `right` is empty, report failure. Else `hasRollResult=true`, emit `ROLL_RESULT{rollCount,totalKuvaSpent,left,right}` and `GRADING_ROLL{left:grade(left), right:grade(right)}`.
- **onChoiceConfirmed** (`:803`): skip if no panel is visible. Invalidate, abort scans, clear timers. Snapshot `pre=initialStats` and `new=newRollStats`, then `newRollStats=[]`. Emit `CHOICE_MADE("unknown")`. After 1200 ms run `scanChoiceRescan()`. If the result, `pre` and `new` are all non-empty, `L=score(stats,pre)` and `R=score(stats,new)`. If `max(L,R) ≥ 12 && |L-R| ≥ 6`, the chosen side is `R>L ? "right" : "left"`. Then `initialStats` becomes `new` (right), `pre` (left) or the raw scan (unknown with a non-empty scan). If non-empty, emit `CHOICE_MADE(side)`, `INITIAL_STATS(initialStats)` and `GRADING_INITIAL`. *SendResult(4) fires for both "take new" and "keep old", so the rescan is the only way to tell which was chosen.*
- **onSessionClose** (`:662`): invalidate, `sessionToken++`, abort scans, `forceEndRivenSession()`, clear timers and all state, emit `SESSION_END`, hide panels and clear the replay buffer.
- **onWeaponPath(path)** (`:471`): `name = getWeaponNameByUniqueName(path)`. Return if unknown. If a weapon is already known: return if it is the same. If it is the same family and the current source is not `"label"`, apply it as `"diorama"`; with a label source, return. A different family logs a warning and keeps the first. If no weapon is known yet, apply it.
- **Manual rescan** (hotkey F6 by default, or the panel button; `:716-739`): `resumeRivenSession()`. If a panel is visible, `rescanVisibleRivenCard()`: invalidate, abort, clear, `hasRollResult=false`, `newRollStats=[]`, emit `RESCAN`, then `triggerInitialScan(scanLayout)`. Otherwise run `onSessionOpen()`.
- **Close button / Esc**: like session close, but without `forceEndRivenSession`.
- **Open auction**: open `https://warframe.market/auction/<id>`, where `id` is the auction id with characters outside `[a-zA-Z0-9]` stripped.

### 3.4 Choice similarity score (`scoreRivenStatSimilarity`, `:374-405`)
```
if either empty -> 0
score = 0; byName = right indexed by lower(name)
for s in left:
   m = byName[lower(s.name)]
   if !m: score -= 4; continue
   score += (s.positive == m.positive) ? 12 : 2
   if both values non-null:
       base = max(5, |s.value|, |m.value|); score += max(0, 8 - (|s.value-m.value|/base)*24)
   elif s.value === m.value: score += 2
score -= max(0, len(right)-len(left)) * 3
```

### 3.5 Weapon enrichment (`sendWeaponEnrichment`, `:414`)
Runs when a weapon becomes known (and is not `"Riven"`).
1. `ensureRivenGoodRollsLoaded()`, then `getBestAttributes(weapon, isMelee)`, then emit `BEST_ATTRIBUTES{positives[],negatives[]}` (§12).
2. `slug = getRivenFamilySlug(weapon)`, then `searchSimilarRivens(slug,{limit:2000})`, then `similarListingPool(listings,30)`, then emit `SIMILAR_LISTINGS(pool)` if non-empty (§13).

`applyDetectedWeapon(name, source, via, exact)` sets the weapon, emits `WEAPON_UPDATE`, re-runs enrichment and re-emits `GRADING_INITIAL` when initial stats exist.

`tryGradeStats(stats)` returns null if the weapon is unknown or `"Riven"`. Otherwise it returns `gradeRiven(weapon, correctScannedStats(weapon, stats).stats)`.

---

## 4. Backend to overlay event protocol

These are the payloads to reproduce. Every event is also kept in a "last event per channel" buffer (insertion-ordered, delete-then-set). A panel created mid-session replays the buffer once it reports ready.

| Event | Payload | Panel behavior (`renderer/riven-overlay.js`) |
|---|---|---|
| `SESSION_START` | `(weapon:"Riven", kuvaPerRoll:0)` | Reset. Left: spinner "Scanning current stats...". Right: "Waiting for roll..." |
| `INITIAL_STATS` | `(stats: RivenStat[], lowConfidence: bool)` | Left only. Empty: error banner `textTooSmall` if lowConfidence, else `readFailed` |
| `ROLL_SCANNING` | – | Both: spinner "Scanning riven stats..." |
| `ROLL_RESULT` | `{rollCount, totalKuvaSpent, left: RivenStat[], right: RivenStat[]}` | Badge "Roll N". Each side renders its own list. Empty: banner `rollReadFailed` |
| `CHOICE_MADE` | `"left"\|"right"\|"unknown"` | Chosen panel gets a green border for 2 s. Right clears to "Waiting for roll..." after 2 s (right chosen) or 0 s. Left shows spinner |
| `SESSION_END` | – | Clear |
| `WEAPON_UPDATE` | `name` | Header weapon text. Hides the weapon warning |
| `WEAPON_MISSING` | – | Left shows "Couldn't read the weapon name, so there are no grades. Use the rescan button to retry." |
| `RESCAN` | – | Left: spinner "Rescanning...". Right: clear |
| `GRADING_INITIAL` | `RivenGradeResult` | Left re-renders stats with grades |
| `GRADING_ROLL` | `{left: RivenGradeResult\|null, right: ...}` | Each side applies its own |
| `BEST_ATTRIBUTES` | `{positives: string[], negatives: string[]}` | Chips. Buffered until the panel has stats |
| `SIMILAR_LISTINGS` | `WfmRivenListing[]` (≤ ~60) | Buffered until the panel has stats |
| `SIMILAR_AUCTIONS` | `bool` | Toggle "Auctions" chip (setting `rivenSimilarAuctionsShown`, default true) |

```ts
interface RivenStat { name: string; positive: boolean; displayPositive?: boolean; value: number|null; multiplier?: boolean }
// positive = good for the player. displayPositive = the sign printed on the card, present only when it differs (Weapon Recoil).
interface GradedStat extends RivenStat { grade: "S"|"A+"|"A"|"A-"|"B+"|"B"|"B-"|"C+"|"C"|"C-"|"F"|"?"; rollFloat: number }
interface RivenGradeResult { stats: GradedStat[]; overallGrade: string; attributeGrade: "Great"|"Good"|"OK"|"Bad"|"?" }
interface WfmRivenListing { id; seller; sellerStatus: string|null; platinum; stats: {name,value,positive}[]; rerolls; startingPrice|null; buyoutPrice|null; isDirectSell }
```
Example `ROLL_RESULT`:
```json
{"rollCount":3,"totalKuvaSpent":0,
 "left":[{"name":"Multishot","positive":true,"value":159},{"name":"Damage","positive":true,"value":276.2},{"name":"Projectile Speed","positive":false,"value":90.9}],
 "right":[{"name":"Weapon Recoil","positive":true,"displayPositive":false,"value":44.3},{"name":"Damage to Corpus","positive":false,"value":0.84,"multiplier":true}]}
```
Values are always unsigned magnitudes. Multipliers keep the printed factor (`x0.84`), with `positive = value ≥ 1`.

---

## 5. Capture and crop geometry

### 5.1 Capture
WFHelper's Linux capture uses an xdg-desktop-portal/PipeWire stream (`services/screenCapture.ts:479-505`, `linuxStreamCapture.ts`), and Windows uses DXGI/WGC with `sourceType ∈ {"window","screen"}`. **On the Deck, replace this with the gamescope screenshot** (`gamescopectl screenshot <path>`, or the root property, giving `/tmp/gamescope.png`). That is the game plane only at 1280x800, so treat it as `sourceType="screen"`. It should not contain our overlay; verify this. If it does, the FITS IN retries hide the right panel and capture again (§9.3).

Capture latency matters. WFHelper's readiness gate polls every **40 ms** and needs 3 stable frames (§5.6). A gamescope screenshot round trip is likely 100-300 ms. Measure it, and treat the gate's time budget as wall-clock.

### 5.2 Content rect and 16:9 canvas (`services/rewardScannerImage.ts`)
1. `detectGameContentRect(img)` (`:25-99`): letterbox detection. A row or column is "black" when ≥85% of 32 evenly spaced samples (`step=floor(dim/32)`) have mean RGB ≤ 12. Scan inward from each edge, at most 25% of the dimension. Bars must be symmetric: `pillar=min(left, W-right)`, `letter=min(top, H-bottom)`. Content is `(pillar, letter, max(24,W-2·pillar), max(24,H-2·letter))`. Images smaller than 120x80 are returned whole.
2. `centerGameCanvas(c)` (`:105-114`), with the comment *"Menus sit on a centred 16:9 canvas"*:
   ```
   cw = min(c.w, round(c.h*16/9)); ch = min(c.h, round(c.w*9/16))
   x = c.x + floor((c.w-cw)/2); y = c.y + floor((c.h-ch)/2)
   ```
   `sourceType="window"` skips bar detection (`frameCanvasContentRect`). `"screen"` runs it.
3. `cropRectContent(img, rect, content)` (`:213-231`): clamp `x,y ∈ [0,0.98]`, `w ∈ [0.02, 1-x]`, `h ∈ [0.02, 1-y]`. Then `X = max(0, content.x + floor(content.w*x))`, `Y` likewise, `W = max(24, floor(content.w*w))`, `H = max(24, floor(content.h*h))`.

**16:10 (Deck 1280x800)**: no bars, so content is (0,0,1280,800) and the canvas is **(0, 40, 1280, 720)**. Every normalized crop below is relative to that 1280x720 band, with 40 px dead strips at the top and bottom. WFHelper asserts this 16:9 menu canvas but never tested a 16:10 frame. **Verify on a real Deck screenshot of the Cycle screen first.** If Warframe instead stretches its menus to the full 800 px height, set the canvas to (0,0,1280,800).

### 5.3 Crops (`ipc/overlay/rivenScanImage.ts:18-25`), normalized to the canvas
```ts
singleCard: { x: 0.22, y: 0.43,  width: 0.56, height: 0.45 }   // initial + choice rescan (centered card)
chatCard:   { x: 0.22, y: 0.3,   width: 0.56, height: 0.45 }   // chat-link preview
rollCard:   { x: 0.36, y: 0.416, width: 0.28, height: 0.434 }  // post-roll new card
```
Deck pixel values (computed with the formulas above; canvas (0,40,1280,720)):

| Crop | Rough crop x,y,w,h | Aspect-trimmed width | Stat band | Upscale |
|---|---|---|---|---|
| singleCard | 281, 349, 716, 324 | 267 | 224x162 at y 110-272 | x2 |
| chatCard | 281, 256, 716, 324 | 267 | 224x162 | x2 |
| rollCard | 460, 339, 358, 312 | 257 | 216x156 at y 106-262 | x3 |
| FITS IN name | 998, 558, 268, 158 | – | – | – |
| FITS IN panel | 896, 436, 384, 324 | – | – | – |
| FITS IN wide (small UI) | 742, 385, 537, 338 | – | – | – |

The 224x162 band matches the `*-angstrum-stats.png` fixtures exactly. Those were saved from a 1278x768 game window, so the Deck is in the regime WFHelper already handles: a 162 px band upscaled x2, where the curse line is lost at x1.

### 5.4 Stat-area trim (`cropRivenStatArea`, `:44-81`)
Tuning: `cardAspectRatio=287/433`, `maxAspectOverflow=1.1`, `trimWidthMargin=1.25`, `maxCenterShift=0.2`, `statTop=0.34`, `statBottom=0.84`, `statLeft=0.08`, `statRight=0.92`, min band 30x30.
```
if w<50 or h<50: return rough
expectedW = floor(h*287/433); trimmed=rough; tw=w
if w > expectedW*1.1:
    keepW = min(w, floor(expectedW*1.25)); centerX = w/2
    b = analyzeTextMetrics(rough).bounds          # default minBoundsWidthFraction 0.2
    if b and b.width <= keepW and |b.left+b.width/2 - w/2| <= 0.2*w: centerX = b.left+b.width/2
    x1 = min(max(0, round(centerX-keepW/2)), max(0, w-keepW)); trimmed = rough[x1:x1+keepW, :]; tw = keepW
sy0=floor(h*.34); sy1=floor(h*.84); sx0=floor(tw*.08); sx1=floor(tw*.92)
if (sx1-sx0)<30 or (sy1-sy0)<30: return rough
return trimmed[sy0:sy1, sx0:sx1]
```
`round` is JS `Math.round`, i.e. `floor(x+0.5)`. **Do not use Python's banker's `round`.** This applies throughout.

### 5.5 Upscale and fallback crop
- `statCropUpscaleFactor(h) = min(3, max(1, ceil(320/h)))` (h≤0 gives 1). Tests: 162→2, 228→2, 400→1. Applied with a **Lanczos3** resize before OCR on attempt 0.
- `cropRivenStatAreaFallback(cardCrop)` (`:111-135`) is used on retry attempts. It runs `analyzeTextMetrics(cardCrop, minBoundsWidthFraction=0.08)` to get bounds, pads by `round(0.06*bounds.w)` and `round(0.06*bounds.h)`, and clamps to the image. It rejects results under 30x30. `upscale = min(3, max(1, round(400/height)))`.

### 5.6 Text metrics, readiness gate, frame hash
`analyzeRivenTextMetrics(img, minBoundsWidthFraction=0.2)` (`:274-379`). Input is BGRA in Electron; adapt the channel order.
```
sampleCols = max(48, min(W,320)); sampleRows = max(32, min(H,160))
stepX = W/sampleCols; stepY = H/sampleRows        # fractional (comment: integer steps clipped bounds)
for sy,sx: px = img[min(H-1,floor(sy*stepY)), min(W-1,floor(sx*stepX))]
   textLike = max(r,g,b) >= 146 and (max-min)/max <= 0.42
   count active, rowScores[sy]++, colScores[sx]++
smooth = 3-tap mean (edges duplicate themselves); skip if len<=2
thr = max(2, mean + 0.45*std)   # population std, per axis
rowBounds/colBounds = first..last index >= thr
activeRows/Cols = count >= thr
rowGroups = runs of >=thr with length >= max(2, floor(sampleRows*0.03))
coverage = active/(cols*rows)
bounds (if both): padX=max(2,floor(cols*.04)), padY=max(2,floor(rows*.03))
   left=max(0,floor((cs-padX)*stepX)); top=max(0,floor((rs-padY)*stepY))
   right=min(W-1,floor((ce+padX+1)*stepX)); bottom=min(H-1,floor((re+padY+1)*stepY))
   keep if width >= max(24,floor(W*minFrac)) and height >= max(24,floor(H*0.12))
score = round3(0.28*clamp01(coverage/0.08) + 0.24*clamp01(activeRows/max(8,rows*.16))
             + 0.24*clamp01(activeCols/max(24,cols*.2)) + 0.24*clamp01(rowGroups/3))
```
Images smaller than 24x24 give score 0 and no bounds.

Readiness gate `waitForRivenUiReady(rect, mode)` (`:407-520`). Production uses it only for the `initial` and `chat` profiles. Budget: initial and chat 1800 ms (roll 500, choice 1800, both unused). Loop: capture, take the rough crop, compute metrics and hash, and track the best score. `stable = score ≥ 0.2`. If the previous frame had bounds, it also requires `(Δcoverage ≤ 0.025 or coverage > 0.25)`, `Δleft/W ≤ 0.05`, `Δtop/H ≤ 0.05`, `Δwidth/W ≤ 0.08` and `Δheight/H ≤ 0.08`. Three consecutive stable frames return ready with that frame. Three null captures in a row abort. Sleep 40 ms between polls. On timeout, return the last screenshot (or the best one) and scanning continues anyway.

Frame hash (`:381-405`): grid `cols=max(12,min(W,24))`, `rows=max(8,min(H,16))`, `step=max(1,floor(dim/n))`. For each sample, `hex(round((b+g+r)/32))` concatenated (rounding with floor(x+0.5)). It is used to skip a retry on an identical frame.

---

## 6. Scan profiles and retry (`ipc/overlay/rivenScan.ts`)

| Profile | Crop | Ready gate | Capture timeout | Retry delay | Retry accepted when | rollReveal |
|---|---|---|---|---|---|---|
| initial (`initial-card`) | singleCard | initial (1800 ms) | – | 650 ms | `>` stats | no |
| chat (`chat-card`) | chatCard | initial | – | 650 ms | `>` | no |
| roll (`roll-right`) | rollCard | none | 100 ms (DXGI fresh frame) | 800 ms | `>=` stats | **yes** |
| choice (`choice-rescan`) | singleCard | none | 100 ms | 500 ms | `>` | no |

`runRivenScanAttempt` (`:290-353`): capture (with the gate when configured), then `recognizeRivenCardStats` (§7.1). If `stats ≥ 2` (`MIN_ACCEPTABLE_RIVEN_STATS`) or the scan is stale, return. Otherwise sleep for the retry delay, capture again, and skip if the frame hash is unchanged. Recognize again and keep the retry if it is better by the profile rule. Each public scan increments a global generation, so stale work is dropped. `abortRivenScans()` sets an abort flag that the gate and the OCR loop check.

---

## 7. OCR pipeline

### 7.1 Recognition loop (`ipc/overlay/rivenScanOcr.ts:143-355`)
Constants: `MIN_ACCEPTABLE_RIVEN_STATS=2`, `MIN_ACCEPTABLE_RIVEN_BUFFS=2`, `MAX_RIVEN_STATS=4`, `MAX_LOW_CONFIDENCE_RETRIES=1` (so attempts 0 and 1).

```
cardCrop, statCrop = crop (§5.3/5.4)
for attempt in 0..1:
   if stale: return empty
   if attempt==0: img=statCrop; up=statCropUpscaleFactor(statCrop.h)
   else: up=1; fb=fallbackCrop(cardCrop); if fb: img=fb.image; up=fb.upscale
   rgba = lanczos3 resize(img, w*up, h*up) if up>1 else img
   ocr = recognizeStatArea(rgba)                       # §7.2
   diag = {droppedLines:[], titleSeen}
   stats = parseRivenStats(ocr.text, diag)             # §8
   droppedWhole = any(looksLikeWholeStatLine(l) for l in diag.droppedLines)
   titleSeen = diag.titleSeen != false
   incomplete = isIncomplete(stats, droppedWhole, titleSeen)
   blocking   = isIncomplete(stats, droppedWhole, titleSeen or not rollReveal)
   nulls = count(value is null)
   betterTie = best exists and len(stats)>0 and len==len(best) and (nulls<bestNulls or (== and ocr.minConf > best.minConf))
   completes = bestIncomplete and not incomplete and (len(best)>4 or (len>len(best) if bestDroppedWhole else len>=len(best)))
   sameComp  = incomplete==bestIncomplete or len(best)==0
   if completes or (sameComp and (len>len(best) or betterTie)): best := this (also store droppedAny, incomplete, blocking, bestDroppedWhole = incomplete and droppedWhole)
   if len(stats)>=2 and not hasLowConfidenceLine(ocr) and no nulls and not incomplete: break
lowConf = best and len(bestStats)>=2 and hasLowConfidenceLine(bestOcr)
dump debug crops (outcome empty|failed|dropped|ok)
if lowConf:   return {stats:[], lowConfidence:true}
if bestBlocking: return {text, stats:[], lowConfidence:true}
return {text, stats:bestStats, lowConfidence:false}
```
`isIncompleteRivenRead(stats, droppedWhole=false, titleSeen=true)` (`:34-49`):
```
if len==0: False
if len<2: True
if count(positive)<2: True
if len>4: True            # crop spanned both roll cards
if len==4: False
return droppedWhole or not titleSeen
```
Debug dumps go to `<userData>/riven-scan-debug/<ISO-stamp>-<outcome>-<label>-{card,stats}.png`, keeping 10 per outcome (`ok`: 4). Fits-in dumps keep 6. The setting is on unless the user opts out. On the Deck, put them under the plugin's log or data directory.

### 7.2 `recognizeStatArea(rgba, W, H)` (`services/rivenOcrOnnx.ts:695-744`)
1. Run YOLO detection (§7.3). With no boxes, return `{lines:[], text:"", minConfidence:-1, yoloBoxCount:0}`.
2. Extract and upscale the crops (§7.4), then recognize them (§7.5).
3. For each result: `t=text.trim()`, skip empty, `p=postprocessOcrText(t)` (Appendix B), and keep `{text:p.trim(), confidence: trimmedLineConfidence(result)}`.
4. Apply `mergeSplitLines` (Appendix B) and join the texts with `\n`.
5. `minConfidence` is the minimum confidence over lines matching `/^[+\-x×]/i`; if there are none, over all lines; if there are no lines, -1.
6. `hasLowConfidenceLine(r)`: lines matching `/^[+-]|^[x×]\s*\d/i` (after trim). If any has `confidence < 0.8` (`LOW_CONFIDENCE_THRESHOLD`), return true. With no stat lines, return false.

`trimmedLineConfidence` (`:388-398`) drops the per-character confidences of leading and trailing spaces. When `ICON_GLYPH_AFTER_VALUE` matches the trimmed text, it also removes that single non-ASCII icon glyph's confidence:
```
ICON_GLYPH_AFTER_VALUE = /^([+\-–x×]?\s*\d(?:[\d.,]*\d)?(?:%\s*|\s+))([\u0080-￿])(?=\s*[A-Za-z])/
```
It returns the mean of what is kept, and falls back to the full mean if `charConfidences.length != text.length`.

### 7.3 YOLO stat-line detector
- File `resources/riven-ocr/yolo/stat_line_detector.onnx` (12.3 MB). Input `images` float32 `[1,3,640,640]`. Output `output0` `[1,5,8400]`: rows `cx,cy,w,h,conf` in 640-letterbox pixels, a single class, no NMS in the graph.
- ORT options: CPU, graph optimization `all`, `inter_op=1`, `intra_op = min(4, max(2, cores-4))`. On the Deck (8 threads) that is 4. Consider 2 to avoid stutter.
- Preprocess: `scale=min(640/H,640/W)`, `newW=round(W*scale)`, `newH=round(H*scale)`, `padLeft=floor((640-newW)/2)`, `padTop=floor((640-newH)/2)`. Resize the RGB image (alpha dropped) with **linear** (bilinear) to newW x newH. Fill the 640x640 blob with `114/255`, paste `pixel/255`, CHW, RGB order.
- Decode: for each i with `conf ≥ 0.25`, `x1=(cx-w/2-padLeft)/scale` etc., clamp to [0,W]/[0,H], and drop boxes with `x2-x1<10` or `y2-y1<3`. Round the coordinates (JS rounding).
- NMS: greedy. Sort by conf descending. Suppress a box when IoU with any kept box is `> 0.5`, where `union = areaA+areaB-inter` and boxes with `union>0` are skipped. Sort the kept boxes by `y1` ascending. Lines include the title, stats, wrapped fragments and the MR badge.

### 7.4 Crop extraction (`:255-318`)
Pad `padX=padY=8`, clamp to the image, and skip a crop if `w<20` or `h<5` or `h>80` (`MAX_STAT_CROP_HEIGHT`). With `maxW` the widest crop, if `maxW < 1200` (`MIN_OCR_WIDTH`), scale **every** crop by the integer `ceil(1200/maxW)` with linear resize, capping each dimension at 6000. Output is RGB.

### 7.5 PaddleOCR CH v3 recognizer
- File `resources/riven-ocr/paddle/ch_PP-OCRv3_rec_infer.onnx` (10.7 MB). Input `x` `[N,3,48,W]`. Output `softmax_5.tmp_0` `[N, T, 6625]`, already softmax probabilities.
- Dict `resources/riven-ocr/paddle/ch_dict.txt`: 6624 lines. Split on `\r?\n` and drop the trailing empty entry; `dict = ["blank", *lines]` (6625). Strip the CR **explicitly**, or decoding breaks.
- Chunking (`recognitionChunks`, `:419-451`) bounds memory. `w48(c)=ceil(48*c.w/c.h)`; `outputBytes(n,w)=n*ceil(w/8)*len(dict)*4`; budget is 48 MiB. Sort crops by w48 ascending. Drop any single crop whose `outputBytes(1,w48) > budget` (detector artifact). Start a new chunk when the current one has 6 crops, or `max(curW,w) > 2*narrowestInChunk`, or the budget would be exceeded.
- Per chunk: `imgW = ceil(48*max(w/h))`. Each crop is resized (linear) to `min(imgW, ceil(48*w/h)) x 48`, normalized `px/127.5-1`, CHW RGB, and **zero-padded** (0.0) on the right to imgW.
- CTC greedy decode (`:342-379`): per timestep, argmax with ties going to the lowest index (strict `>`). Emit the character when `idx≠0 && idx≠prev` and `idx<len(dict)`, and record `prob[idx]`. `prev=idx` every step, including blanks. Confidence is the mean of the recorded probabilities (0 if none). The dict contains `' '` (space), so decoded text often has edge spaces.
- This recognizer is shared with the FITS IN caption reader (§9.3) and the reward scanner.

A Python prototype of §7.2-7.5 (numpy, Pillow, onnxruntime 1.29) on `chat-card-angstrum-stats.png` at x2 reproduced the expected lines: `Angstrum Sati-`, `visidra`, `+159% Multishot`, `+276,2% Damage`, `+93,2% Fire Rate`, `-90,9% Projectile Speed` (conf 0.89-0.95). At x1 the curse line came out garbled, matching the TS test. Pillow bilinear and sharp `linear` differ slightly; this is within tolerance.

---

## 8. Text parser (`ipc/overlay/rivenScanText.ts`), copied verbatim in Appendix A

Port it **line for line**. The replay fixture (§17) exercises many of its branches.

### 8.1 Pipeline
`parseRivenStats(text, diag)`:
1. `cleaned = preprocessOcrText(text)`: an ordered list of about 90 regex repairs (Appendix A lines 130-252). It repairs padlock tails, rejoins wrapped names, converts the decimal comma to a dot, maps digit look-alikes (`g q→9, l I→1, O→0, S→5, B→8`), applies the alias table, strips qualifiers like `(x2 for Bows)` and `(x2 for Heavy Attacks)`, strips element-icon junk, and splits on each sign or `x` multiplier into separate lines.
2. `diag.titleSeen = hasTitleAboveStats(cleaned)`. Before the first stat-like line, any line containing a 4+ letter word that is not one of the stat-name words (plus `bows` and `attacks`) counts as a title.
3. `lineResults = parseStatsFromLines(cleaned)`. If it is non-empty and some value is non-null, return it. Otherwise also try the blob (all `\n` replaced with spaces) and keep the one with the higher score (10 per valued stat, 3 per null-valued stat; ties go to lines).

### 8.2 `parseStatsFromLines`
- `collapseOrphanValueLines`: a line that is only a value (`/^[+\-–x\d\s.,% ]+$/i`, has a value, has no known stat, and is not a bare `^\d{1,4}$`) is queued and prepended to the next stat-name line that lacks its own value. Leftover values are appended at the end.
- For each line: find every `KNOWN_RIVEN_STATS` substring (case-insensitive, Appendix C).
  - If there is none and the line starts with `[+\-–x×]`, strip the value prefix to get `namePart`. Try `factionMultiplierStat` (an `x` value plus a trailing grineer/corpus/infested word gives `Damage to X`), then `completeTruncatedStatName` (a unique-prefix completion, with one edit allowed when the fragment is ≥8 characters), then Levenshtein fuzzy matching (fragment trimmed to `stat.length+2`; max distance 2 when the name is ≥7 characters, else 1).
  - If there is still none, record the line in `droppedLines` when it looks stat-like (`/[+\-–]\s*\d/` or `/\bx\s*\d/i`).
- Remove overlapping hits (sort by idx, then longer first). Extend short hits to a longer stat when the tail is unambiguous. Resolve garbled head words (`C...ical Damage` becomes Critical Damage). Drop a bare `Damage` hit when letters precede it after the value.
- Value: try `extractSignAndValue(prefix)`, then the suffix. A **multiplier on a stat other than `Damage to …` is discarded** (qualifier junk).
- Dedupe by name: a valued duplicate replaces a null-valued earlier hit (riven titles can spell a stat). An integer value can be refined by a later decimal one with the same floor.
- An adjacent elemental with a null value inherits the previous elemental's value (combined damage types).
- **Recoil inversion**: for `weapon recoil` and `recoil`, `positive = !parsedPositive` and `displayPositive` keeps the printed sign.

### 8.3 `extractSignAndValue(fragment)`
The sign comes from the last `[+\-–](?=\s*\d)` match: `-` and `–` mean negative, no sign means positive. Value order: the last `(\d+\.?\d*)\s*%`, then the last `X_VALUE` = `/x\s*(\d+\.?\d*)(?!\.?\d|[A-Za-z](?![A-Za-z]))/gi`, which gives `{positive: v≥1, value:v, multiplier:true}`, then the last `SIGNED_VALUE` = `/[+\-–]\s*(\d+\.?\d*)(?!\.?\d|[A-Za-z](?![A-Za-z]))/g`. With a sign but no value, return `{positive, value:null}`. Otherwise return null.

`sanitiseValue(v)`: an integer `v>500` and `≥100` gets a decimal point inserted before its last digit (`1593`→`159.3`). A non-integer `>1000` whose integer part has more than 3 digits has its first digit dropped when the result is in `(0,500]`.

### 8.4 Python translation gotchas
- JS replacement strings: `"$11$2"` means group 1, then a literal `1`, then group 2. In Python write `r"\g<1>1\g<2>"`. This appears in `postprocessOcrText` lines 561 and 567. Write every `$n` as `\g<n>`.
- Use `re.ASCII` to match JS semantics for `\b`, `\w` and `\d`. JS `\s` also matches Unicode spaces (U+3000 and so on); if needed, use an explicit class `[\s 　]`.
- JS `String.replace(regex_with_g)` replaces all matches; without `g`, only the first. All the parser regexes use `g`. The `m` flag maps to `re.M`, `i` to `re.I`.
- Replacement callbacks (`VALUE_WITH_LOOKALIKE`) map to `re.sub` with a function.
- `[...new Set(...)]` keeps insertion order. Use `dict.fromkeys`.
- `TRAIT_LOCK_TAIL` is built from the last word of each known stat: `\b(<ends>)[^\s%)]$` with flags `gim`.

### 8.5 OCR post-processing (`postprocessOcrText`, `mergeSplitLines`), Appendix B
These run before the parser. Use the `SPLIT_STAT_HEADS` and `SPLIT_STAT_TAILS` tables verbatim. A head may be right-truncated (length ≥4 and a prefix of a head). Glyph-junk lines matching `/^[^A-Za-z0-9%+\-–×]*$/` between a head and its tail are skipped. The merged confidence is the min of the two.

### 8.6 Stale-card check
`looksLikeStaleCardRead(scanned, knownCards)`: true when `len(scanned) ≥ 2` and some non-empty known card has **≥2** stats with the same name (case-insensitive), the same `positive`, and value within 0.05 (0.005 when either side is a multiplier). A reroll never repeats exact values.

---

## 9. Weapon identification

Sources, in increasing order of authority:
1. **Cycle dialog** (`rollPending`), source `dialog`. It gives the family and riven name and is overridden by later sources. It does not override a `label` weapon of the same family, or an exact label.
2. **Card title OCR**, source `ocr`: `findWeaponInText(titleText || rawText)`. It runs only while the weapon is unknown, on the initial scan and on every roll scan.
3. **Diorama load path** (`/Lotus/Weapons/...`), source `diorama`: an exact `uniqueName` lookup. It refines a same-family name unless the source is `label`.
4. **FITS IN plate** on the bottom right of the Cycle screen, source `label`. It names the exact variant, for example "Kuva Sobek" on a "Sobek" riven.

### 9.1 `findWeaponInText(text)` (`services/rivenData.ts:463-546`)
- Exact: over all lowercased weapon names with length ≥3, find the longest name such that some trimmed, lowercased line **starts with** it and the next character is not `[a-z0-9]`.
- Fuzzy: the first 4 lines normalized (lowercase, `[^a-z0-9]+`→space, keep only lines of length ≥4). An alias table returns immediately on a line or a leading phrase: `{euphona:"Euphona Prime", gotva:"Gotva Prime", reaper:"Reaper Prime"}`. Then, for leading phrases of 1-4 words (each word ≥2 characters, phrase ≥4), compare to every normalized weapon whose word count differs by ≤1, with `maxDist = len≥14 ? 3 : len≥8 ? 2 : 1`. The best candidate has the lowest distance; a candidate within +1 distance wins if it has more words.
- The result is `bestCandidate` when there is no exact match or when it has more words than the exact match; otherwise the exact match.

### 9.2 Diorama path
`getWeaponNameByUniqueName(path)` does an exact match against `ExportWeapons` keys. Example: `/Lotus/Weapons/Grineer/KuvaLich/LongGuns/Sobek/KuvaSobek` gives "Kuva Sobek".

### 9.3 FITS IN label (`ipc/overlay/rivenWeaponLabel.ts`)
Crops are normalized to the 16:9 canvas:
```ts
RIVEN_FITS_IN_NAME_CROP  = { x: 0.78, y: 0.72, width: 0.21, height: 0.22 }
RIVEN_FITS_IN_PANEL_CROP = { x: 0.7,  y: 0.55, width: 0.3,  height: 0.45 }
RIVEN_FITS_IN_WIDE_CROP  = { x: 0.58, y: 0.48, width: 0.42, height: 0.47 }  // UI scale < 0.98
REFERENCE_CONTENT_HEIGHT = 1080
CAPTION_TOP_OFFSET=110, CAPTION_HEIGHT=60, CAPTION_HALF_WIDTH=150, CAPTION_UPSCALE=4      // 1080p px * unit
CAPTION_FIXED_OFFSET_X=738, CAPTION_FIXED_OFFSET_Y=366, CAPTION_FIXED_HALF_WIDTH=85, CAPTION_FIXED_HALF_HEIGHT=18
```
- The UI scale comes from `EE.cfg`, next to EE.log (`services/eeLogPath.ts:221-273`). It applies only if `/^\s*Flash\.FlashDrawScaleMode\s*=\s*[DM]SM_CUSTOM\s*$/m` matches. Then the last `/^\s*Flash\.FlashDrawScale\s*=\s*([0-9.]+)\s*$/gm` value, rounded to 2 decimals and clamped to [0.5,1]. Otherwise the setting `warframeUiScale` applies, default `REFERENCE_WARFRAME_UI_SCALE=0.99`. `smallUi = scale < 0.98`.
- Normal UI (`readFitsInWeapon`): name crop, then the reward-strip reader (`recognizeRewardStripOnnx`: grayscale, normalise, Otsu, row split, Paddle; specified in the reward-scanner port spec). Then the inverted name crop. Then the panel crop, upscaled so the content height maps to 1080. Each stage takes the OCR rows into `findWeaponByLabelLine`.
- Small UI (`readFitsInWeaponSmallUi`): hide the right panel, wait 50 ms, then try up to 3 times (600 ms apart) on a fresh capture. Each attempt tries, in order: the wide crop at scale `(1080/contentH)/uiScale`; the same with CLAHE (64x64 tiles); a caption band under a "FITS IN" heading row (a row whose letters contain `tsin` or `fitsi`) at `unit=(contentH/1080)*clamp(uiScale,0.5,1)`; a band at a fixed offset from the screen center; and finally the inverted image. A caption band is cropped raw, Lanczos-upscaled x4, normalised, then sent to the Paddle recognizer directly.
- `findWeaponByLabelLine(lines)` (`services/rivenData.ts:560-600`) needs a **whole line** to equal the normalized weapon name. A fuzzy match needs the line to be at least 5 characters, with distance ≤1 (≤2 when ≥14 characters), and it is rejected when two names tie. An exact match beats any fuzzy one; among exact matches, the longest wins.
- `shouldApplyLabelWeapon(match, cur, curSource, sameFamily)`: true if the current weapon is unknown; false if the names are equal; true if `match.exact || sameFamily`; otherwise true only when `curSource === "ocr"`.
- If no match is found and no weapon is known, emit `WEAPON_MISSING`.
- With an 1280x720 canvas the plate text is about 13 px tall (20 px at 1080p), so expect weaker reads. The diorama path is the most reliable source on the Deck.

---

## 10. Riven data (`services/rivenData.ts`)

Source data is the npm package `warframe-public-export-plus` (^0.6.8): `ExportWeapons.json`, `ExportUpgrades.json` and `dict.en.json` from the package root. The Deck plugin must bundle these files or fetch them from the same package (they are not in this repo's node_modules).

Index build (`:215-307`):
- Weapons: for each entry with a string `name` (resolve `/Lotus/Language/...` keys through dict.en) and a numeric `omegaAttenuation` (the disposition). Store `uniqueName`, `omegaAttenuation`, `productCategory`, `holsterCategory`, `compatibilityTags[]` and `shotgunFamily` (any ancestor in the `parentName` chain matches `/\/Shotgun\//i`). On a display-name collision, a `DrifterMelee` entry never replaces a non-Drifter one; other ties keep the later entry.
- Riven types: `ExportUpgrades` keys containing `Randomized` that have non-empty `upgradeEntries`. Each entry has `{tag, canBeBuff, canBeCurse, baseValue: upgradeValues[0].value, displayName: cleanLocTag(dict[upgradeValues[0].locTag]), prefix: dict[prefixTag], suffix: dict[suffixTag]}`. `cleanLocTag` removes `/\|[^|]*\|[%s]?\s*/g`, then `<...>` tags, then parentheticals.

`resolveRivenType(weapon)` (`:333-365`):
```
LongGuns and (holster=="SHOTGUN" or "SHOTGUN" in compatTags or shotgunFamily) -> .../LotusShotgunRandomModRare
uniqueName ~ /ModularMelee/i -> LotusModularMeleeRandomModRare (Zaw)
uniqueName ~ /SUModularSecondary|InfKitGun/i -> LotusModularPistolRandomModRare (Kitgun)
category: LongGuns->LotusRifleRandomModRare, Pistols->LotusPistolRandomModRare,
          Melee|DrifterMelee->PlayerMeleeWeaponRandomModRare, SpaceGuns->LotusArchgunRandomModRare
else if "SENTINEL_WEAPON" in compatTags: holster (or "PISTOL" if uniqueName ends /Pistol$/i)
          RIFLE|SNIPER|BOW->Rifle, SHOTGUN->Shotgun, PISTOL->Pistol, MELEE->Melee
else null
```
All keys are prefixed `/Lotus/Upgrades/Mods/Randomized/`.
`isMeleeWeapon`: uniqueName matches `/ModularMelee/i`, or the category is Melee, SpaceMelee or DrifterMelee, or the holster is `MELEE`.
`findUpgradeEntry(type, tag)`: exact tag, then `WeaponFaction*` mapped to `WeaponMeleeFaction*`, then `WeaponDamageAmountMod` mapped to `WeaponMeleeDamageMod`.
`getRivenFamilySlug(name)`: strip one suffix from `[" Prime"," Wraith"," Vandal"]`, then one prefix from `["MK1-","Kuva ","Tenet ","Prisma ","Vaykor ","Rakta ","Secura ","Sancti ","Synoid ","Telos "]`. Each strip applies **only if the base weapon exists**. Then replace `&` with ` and `, lowercase, replace `[^a-z0-9]+` with `_`, and trim `_`. Examples: Boar Prime→`boar`, Silva & Aegis→`silva_and_aegis`, Kuva Bramma→`kuva_bramma`.
`getFamilyVariants(name)`: every weapon with the same slug, with its disposition.
`STAT_NAME_TO_TAG` (the OCR name to tag map) is in Appendix C.

---

## 11. Grading (`services/rivenGrading.ts`, `services/rivenConstants.ts`)

### 11.1 Constants (verbatim)
```ts
NUM_BUFFS_ATTEN       = [0, 1, 0.66000003, 0.5, 0.40000001, 0.34999999];
NUM_BUFFS_CURSE_ATTEN = [0, 1, 0.33000001, 0.5, 1.25, 1.5];   // indexed by number of BUFFS
SPECIFIC_FIT_ATTEN = 1.5; BASE_DRAIN = 10;
NON_PERCENTAGE_TAGS = {WeaponFactionDamageGrineer, WeaponFactionDamageCorpus, WeaponFactionDamageInfested,
  WeaponMeleeFactionDamageGrineer, WeaponMeleeFactionDamageCorpus, WeaponMeleeFactionDamageInfested,
  WeaponMeleeComboInitialBonusMod, ComboDurationMod, WeaponMeleeRangeIncMod, WeaponPunctureDepthMod}
isMultiplierTag(t) = t in NON_PERCENTAGE_TAGS and ("FactionDamage" in t or t == "WeaponMeleeComboInitialBonusMod")
MAX_RIVEN_MOD_RANK = 8
GRADE_THRESHOLDS on score=lerp(-10,10,f): S>=9.5, A+>=7.5, A>=5.5, A->=3.5, B+>=1.5, B>=-1.5, B->=-3.5, C+>=-5.5, C>=-7.5, C->=-9.5, else F
```
`floatToGrade(f, isCurse)`: if the stat is a curse, use `1-f`. Test vectors: 0.975→S, 0.974→A+, 0.875→A+, 0.874→A, 0.5→B, 0.024→F; curse 0.0→S.

### 11.2 Forward formulas (from browse.wf RivenParser.js, as in `rivenFingerprint.ts:181-212`)
```
att = 1.5 * disposition * 10
buff  = base * att * 1.25^numCurses * lerp(0.9,1.1,roll) * NUM_BUFFS_ATTEN[min(nb,5)] * (lvl+1)
curse = |base| * att * lerp(0.9,1.1,roll) * NUM_BUFFS_CURSE_ATTEN[min(nb,5)] * NUM_BUFFS_ATTEN[min(nc,5)] * (lvl+1)
display: percentage tags -> round(raw*1000)/10 (%), non-percentage -> round(raw*100)/100
multiplier tags: buff shows x(1+v) rounded to 2 dp; curse shows x(1-v)
curse sign: base>0 -> shown negative; base<0 (recoil) -> shown positive
```

### 11.3 Inverse (`unparseBuff` / `unparseCurse`, `:68-135`)
`v = displayed` for a non-percentage tag, else `displayed/100` (curse: `|displayed|`). Return 0.5 if any factor is 0. Otherwise divide by `(lvl+1)`, the attenuation tables (curse: `NUM_BUFFS_ATTEN[nc]` and `NUM_BUFFS_CURSE_ATTEN[nb]`), `att` and `|base|`, and return `(v-0.9)/0.2`. Test: `unparseBuff(157.5, 0.016666, 0.7, 1, 0, "WeaponCritChanceMod") ≈ 0.5`, `173.2 → 1.0`, `141.7 → 0.0`.

### 11.4 `correctScannedStats(weapon, stats)` (`:183-310`)
Fixes OCR name confusion before grading.
- `dispositions = [base, *familyVariants]`. The counts come from `plausibleStatCounts(nb,nc)`: every `buffs ∈ [nb..max(nb,3)]` × `curses ∈ [nc..max(nc,1)]`.
- On melee, rename `WeaponDamageAmountMod` to `WeaponMeleeDamageMod`, and the reverse on non-melee.
- `violationFor(tag, stat, v)`: null if the type lacks the tag or the buff/curse capability does not match. Otherwise the min over dispositions, counts and `lvl 0..8` of `max(0,f-1)+max(0,-f)`.
- A stat **misfits** when its violation is null or > 0.1. If more than one stat misfits, keep the scanned names (the weapon is probably wrong). Otherwise, for each misfit, try the siblings:
  ```
  WeaponDamageAmountMod: [WeaponMeleeDamageMod, WeaponCritDamageMod]
  WeaponMeleeDamageMod:  [WeaponDamageAmountMod, WeaponCritDamageMod]
  WeaponCritDamageMod:   [WeaponMeleeDamageMod, WeaponDamageAmountMod]
  WeaponCritChanceMod:   [SlideAttackCritChanceMod, WeaponStunChanceMod]
  SlideAttackCritChanceMod: [WeaponCritChanceMod]
  WeaponStunChanceMod:   [WeaponCritChanceMod]
  ```
  Rename to the sibling only if exactly one fits within `FIT_TOLERANCE=0.02`. Multiplier and null-valued stats are left alone.

### 11.5 `gradeRiven(weapon, stats, modRank?)` (`:393-673`)
1. Return null if the disposition or riven type is unknown, the stats are empty, or there are more than 3 buffs or more than 1 curse.
2. Prepare each stat. A multiplier gives `fraction = positive ? v-1 : 1-v`, and `displayed` is the fraction for non-percentage tags, else `fraction*100`. `halfStep` is 0.005 or 0.5 for multipliers, else 0.05.
3. `fitsAt(p,disp,lvl,b,c)`: the interval from `[displayed-halfStep, displayed+halfStep]` unparsed must intersect `[-0.02, 1.02]`.
4. If not every gradeable stat fits at `(baseDisp, assumedLvl=modRank??8, nb, nc)`, refit:
   - Candidates are `[{weapon, base}, *familyVariants]`. The levels to search are `8..0` only when ≥2 stats are gradeable; a stated rank goes first.
   - For each `(b,c)` from `plausibleStatCounts`, for each level in order, pick the candidate that fits all stats and whose disposition is closest to base. The first hit wins and sets disposition, level and counts.
   - With no hit, minimize the summed violation over counts × candidates at the assumed level (a tie goes to the closest disposition).
5. Per stat: an unknown tag or entry gives grade `B` with rollFloat 0.5 and counts as score 0. A null value gives grade `?`. Otherwise `rollFloat = clamp01(unparse(...))`, `grade = floatToGrade`, and `score = lerp(-10,10, curse ? 1-f : f)`.
6. If a rank was stated and every scored buff is clamped at 0 or 1, re-grade without the rank.
7. `overallGrade = floatToGrade(inverseLerp(-10,10, mean(score)), false)`. `attributeGrade = computeAttributeGrade(stats, weapon)` (§12.3).

---

## 12. Good-roll sheet ("best attributes", attribute grade)

### 12.1 Source
Google Sheet `1LJ83e4x_xIVgjZg049PKrDgTex2MIuaiX0FFZi9mH-Q` (44bananas). Fetch
`GET https://docs.google.com/spreadsheets/d/<ID>/export?format=csv&gid=<gid>`, following redirects, with a 20 s timeout per tab. There are no special headers.

| gid | class |
|---|---|
| 0 | primary |
| 1505239276 | secondary |
| 1413904270 | melee |
| 289737427 | archgun |
| 965095749 | robotic |

### 12.2 CSV parsing (`config/shared/rivenGoodRolls.ts`)
RFC-style quotes (`""` escapes a quote). `\r` is ignored. A leading BOM is stripped. The header columns start with `weapon`, `positive` and `negative`, case-insensitive. The key is `lower(trim(weapon))`; the first row per name wins across tabs.
- Positives cell: collapse whitespace, then split on `/\s+or\s+/i` into alternatives. In each alternative, a whitespace token containing `/` contributes its parts as **optional** tags, and other tokens are **mandatory**. A tag that is mandatory is removed from optional.
- Negatives cell: split on `/[/\s]+/`.
- Abbreviations:
```
CC Crit Chance, CD Crit Dmg, MS Multishot, FR/AS FireRate, RLS Reload, SC Status Chance, SD Status Duration,
PT PunchThrough, MAG Magazine, AMMO AmmoMax, REC Recoil, ZOOM Zoom, PFS ProjectileSpeed, IMP Impact,
PUNC Puncture, SLASH Slash, COLD, HEAT, ELEC, TOX, DTG/DTC/DTI faction, RANGE, IC InitialCombo,
EFF HeavyAtkEfficiency, SLIDE, FIN Finisher; DMG -> WeaponMeleeDamageMod (melee tab) else WeaponDamageAmountMod;
ELEMENT -> [Heat, Cold, Electricity, Toxin]
```
(The exact tag strings are in Appendix C.)

### 12.3 Lookup, caching and grading
- Cache file `<userData>/riven-good-rolls-cache.json` = `{updatedAt: ISO, data: {name: {goodAttrs:[{mandatory[],optional[]}], acceptedBadAttrs[]}}}`. Max age 7 days. Refetch cooldown 10 min. A failed refresh keeps the cache, even when stale. `ensureLoaded()` waits for the fetch only if nothing is loaded.
- `lookupName(weapon)`: `lc=lower(collapsed ws)`, then strip one of `["Coda ","Dex ","Mara ","Carmine ","Ceti ","Prime "]`, then `getRivenFamilySlug(folded)` against an index of `normalizeForSlug(key.replace(&," and "))`.
- `getBestAttributes`: unique display names of all mandatory tags (in order), then the optional ones, then the negatives. Melee uses "Attack Speed" for `WeaponFireRateMod`.
- `computeAttributeGrade` (`:312-389`): positives are `Decisive` (in any mandatory list), `Good` (in any optional list) or `NotHelping`. Negatives are `Good` (in acceptedBad), `Bad` (in any mandatory or optional list) or `NotHelping`. `flag` means some goodAttrs group has all of its mandatory tags among the positives and every positive is in that group. `num` counts Decisive plus Good. Overall:
  - with a Bad negative: `(flag && num≥2) || num≥3` gives `OK`, else `Bad`;
  - else with a NotHelping negative: `flag||num≥2` gives `Good`, `num≥1` gives `OK`, else `Bad`;
  - else if `flag`: `num≥2 && hasAnyNeg` gives `Great`, else `Good`;
  - else `num≥2` gives `Good`, `num≥1` gives `OK`, else `Bad`.
  - An unknown weapon gives `?`.

---

## 13. warframe.market similar listings

### 13.1 Requests
Base `https://api.warframe.market/v1`. These calls need no auth. Headers:
```
Accept: application/json
Content-Type: application/json
Platform: pc
Language: en
User-Agent: (browser-like UA; WFHelper uses Chromium's or "Mozilla/5.0 (X11; Linux x86_64) ...")
```
1. `GET /auctions/search?type=riven&weapon_url_name=<slug>&sort_by=price_asc`, plus `&positive_stats=a,b&negative_stats=c` when filtering. WFM honors only the **first** repeated key, so filters are comma lists. The overlay passes no stat filter.
2. If step 1 returned >0 auctions, also `...&sort_by=price_desc`. (Full coverage with ≥490 results would fan out to `&polarity=madurai|naramon|vazarin` × asc/desc; the overlay does not use it.)
3. Dedupe by `id` in insertion order.

Response (`unwrapWfmResponse` takes `.data`, else `.payload`):
```json
{"payload":{"auctions":[{"id":"64f...","owner":{"ingame_name":"Foo","status":"ingame"},
  "buyout_price":450,"starting_price":300,"is_direct_sell":false,
  "item":{"name":"croni-zetican","weapon_url_name":"sobek","re_rolls":12,
   "attributes":[{"url_name":"critical_chance","value":153.4,"positive":true},
                 {"url_name":"damage_vs_corpus","value":0.98,"positive":false}]}}]}}
```
Parsed listing: the stat name comes from `url_name`, with `_` replaced by space and each word title-cased ("Damage Vs Corpus"). `platinum = buyout ?? starting ?? 0`. `sellerStatus = lower(owner.status)`.

### 13.2 Caching and rate limits
- In-memory cache keyed `slug,quick|full,sortedPos...,|,sortedNeg...`, TTL 10 min, at most 50 entries (oldest evicted).
- Global WFM scheduler (`services/wfmScheduler.ts:19-46`): token bucket **2.5 req/s**, burst 5, at most 3 concurrent (2 for background), queue 64. Retries up to 3 with backoff `750·2^(n-1)` ms capped at 2500 ms, plus 0-25% jitter. A 429 gates all calls for `Retry-After` seconds, clamped to [1 s, 5 min], or 30 s when absent. Calls that would wait more than 10 s fail immediately. Only GET and HEAD are retried on 5xx or transport errors.
- Cloudflare challenge (`cf-mitigated: challenge` or a 403 with "just a moment"): WFHelper solves it in a hidden Electron window. **That is Electron-only.** On the Deck, use plain `requests` with a UA and treat a challenge as a soft failure that leaves the panel without listings.

### 13.3 Pool and similarity (renderer)
- `similarListingPool(listings, 30)`: the first 30, plus the cheapest (by `buyout ?? platinum`) direct sales among the rest, enough to bring direct sales up to 30.
- Display: if the "Auctions" toggle is off, filter to `isDirectSell !== false`. Show the first 30.
- `computeRivenStatSimilarity(myNamesLc, listingStats)`: `match` is the first listing name that equals, contains or is contained in my name. `pct = round(100*|matched| / (|mine|+|listing|-|matched|))`. Sort by pct descending. Colors: ≥75 green, ≥40 amber, otherwise red. Non-matching listing stat lines are drawn with strikethrough at 50% opacity.
- Value format: a name matching `^Damage Vs ` shows `x{value.toFixed(2)}`; otherwise `{value<0?"−":"+"}{|round(value)|}%`.
- Price is `buyoutPrice || startingPrice || platinum`. Rolls is `re_rolls`.
- Clicking a listing opens `https://warframe.market/auction/<id>`. On the Deck, show it in the QAM or open it in the Steam browser.

---

## 14. Inventory fingerprint decode (`services/rivenFingerprint.ts`)

The overlay does not use this. It powers the Rivens view and shares the grading math, so it is included here. The inventory comes from `inventory.php` via the API helper, which is outside this area.
- Look at `inventory.Upgrades[]` where `ItemType` contains `Randomized` or `RandomMod`. `UpgradeFingerprint` is JSON, sometimes double-stringified: `{compat, lim, lvlReq, lvl, rerolls, pol, buffs:[{Tag,Value}], curses:[{Tag,Value}], challenge?}`. The riven is veiled when `challenge` is present or `compat` is missing.
- `rollFloat = Value / 0x3FFFFFFF`, or 0 if outside [0,1].
- `displayValue` is computed at `lvl`, `maxRankValue` at 8, and `rankValues` at 0..8 (§11.2).
- `overallGrade = floatToGrade(mean(buff f, 1 - curse f))`.
- Riven name = `weapon + " " + generateRivenSuffix`. Sort buffs by fingerprint `Value` descending (ties go to ascending baseValue). One buff gives `Title(prefix)+lower(suffix)`. Otherwise `Title(prefix1)`, then `-prefix_k` for the middle buffs, then `lower(suffix_last)`.
- `RawUpgrades[]` give unseen veiled counts per type label. The type labels and the challenge and complication description tables are in Appendix D.

---

## 15. Settings (from `config/runtime/overlaySettings.ts`)
| Key | Default | Deck port |
|---|---|---|
| `rivenOverlayEnabled` | true | QAM toggle |
| `rivenRescanHotkeyEnabled` / `rivenRescanHotkey` | true / `F6` | Replace with a QAM "Rescan riven" button. Optionally add a Steam Input chord. |
| `rivenSimilarAuctionsShown` | true | QAM toggle |
| `warframeUiScaleAuto` / `warframeUiScale` | auto / 0.99 | Read EE.cfg in the prefix. Fall back to a slider. |
| OCR debug dumps | on | Plugin data directory, with rotation as in §7.1 |

---

## 16. Overlay layout: WFHelper today and Deck target

### 16.1 WFHelper today
There are two transparent, frameless windows, `rivenLeft` ("Current") and `rivenRight` ("New roll"). The canvas is **420x640 CSS px** each, zoomed by `baseZoomForDisplay`, which uses the shortest screen edge: ≤720→0.8, ≤900→0.9, ≤1200→1, ≤1600→1.15, else 1.3. The left window is at top-left `(16, 80)` and the right at top-right (`x = W - 16 - w`). Users can drag them, and the position is saved.

Top to bottom (`renderer/riven-overlay.html`):
1. Header: panel label (10 px uppercase, accent on the right panel), weapon name, "Roll N" badge, rescan ↻ (left panel, interactive mode only), close ×.
2. Overall attribute grade badge (`Great/Good/OK/Bad`), colored `#4ade80/#6aab7a/#facc15/#ef4444`.
3. Scanning spinner and text.
4. Stat rows. Each row has a grade-colored dot, the value (`+153.4%`, `−44.3%` using U+2212, or `x0.84`; Rajdhani 16 px bold, right-aligned, min 72 px), the stat name, a grade badge, and a roll bar whose width is `rollFloat%`. The bar color uses `pct = curse ? 1-f : f`: ≥0.85 ok `#48c774`, ≥0.6 accent `#4f9eff`, ≥0.35 warn `#f5a623`, else bad `#ff5a5a`.
   - Grade badge colors: S `#4ade80`; A+/A/A- `#6aab7a`; B+/B/B- `#facc15`; C+/C/C- `#f97316`; F `#ef4444`.
   - Dot colors: S `#d4a017`, A+ `#2ecc40`, A `#27ae60`, A- `#1e8449`, B+ `#3498db`, B `#2980b9`, B- `#2471a3`, C+ `#e67e22`, C `#d35400`, C- `#a04000`, F `#e74c3c`.
5. Error banner and weapon warning (strings in §16.3).
6. Best attributes: a "Best" row and a "Neg" row of abbreviated chips. A chip is highlighted when the current stats contain that canonical stat with the matching sign. Abbreviations:
   `critical chance CritCh, critical damage CritDmg, multishot Multi, damage/melee damage Dmg, status chance Status, attack speed AtkSpd, electricity Elec, toxin Toxin, heat Heat, cold Cold, range Range, zoom Zoom, ammo maximum Ammo, weapon recoil Recoil, projectile speed ProjSpd, finisher damage Finisher, heavy attack efficiency HvyAtk, combo duration Combo, slide attack Slide, reload speed Reload, fire rate FireRate`.
   Canonical names for matching: `attack speed→fire rate, melee damage→damage, critical chance for slide attack→slide attack, slide→slide attack, recoil→weapon recoil, magazine→magazine capacity, heavy attack→heavy attack efficiency`.
7. "Similar on WFM" list with an "Auctions" toggle. Each card shows the match %, `N p`, "Rolls: N" and the stat lines.

Background is `#060a12` and surface `#111827`, at the user's overlay opacity. Text is `#e8edf5`, muted `#a0aec0`, border `rgba(79,158,255,0.25)`.

### 16.2 Deck target (Pillow-drawn, one ARGB window)
At zoom 0.9 the WFHelper panels would be 378x576 at x=16 and x=886. The left one would cover the left edge of the 716 px card band (x 281-997), and the right one would cover the FITS IN plate (x≥998, y≥558). Recommended layout on the 1280x800 plane (adjust after the §5.2 check):
- **Left panel "CURRENT"** at x=8, y=48, width 264. **Right panel "NEW ROLL"** at x=1008, y=48, width 264. Height fits the content, at most about 500 px, so it ends above the FITS IN plate (y≈558) on the right. Both stay clear of the card column (x 281-997 at most; the actual card is about x 545-735).
- Content per panel: header (weapon, Roll N, overall grade badge); 2-4 stat rows (value 15-16 px bold, name 13 px, grade badge, roll bar 3 px); best-attribute chips (one line each, 11 px); the **top 5** similar listings (match %, price, rolls). The QAM detail page shows the full 30 with the toggle.
- **Comparison cues** (old vs new): when both sides are graded, outline in green the panel with the better `overallGrade`, comparing by score. Optionally mark per-stat arrows ▲/▼ for the same stat name between sides. This goes beyond WFHelper; keep it optional.
- **Chat view**: the left panel only.
- **Input**: gamescope external overlays get no input, so close, rescan, the auctions toggle and listing clicks all move to the QAM. Esc does nothing. Hide the panels on `SESSION_END`, and when the QAM or Steam overlay is open if the overlay spec requires it.
- **Rendering**: redraw on every event. Font: Rajdhani Bold for values and labels, or another bundled TTF. Respect the "last event" replay semantics so the overlay process can restart mid-session.

### 16.3 English strings (`src/i18n/en.json:1151-1200,1294-1309`)
```
overlay.riven.current            Current
overlay.riven.newRoll            New roll
overlay.riven.roll               Roll {count}
overlay.riven.scanning           Scanning...
overlay.riven.scanningCurrent    Scanning current stats...
overlay.riven.scanningRoll       Scanning riven stats...
overlay.riven.rescanning         Rescanning...
overlay.riven.waitingForRoll     Waiting for roll...
overlay.riven.waitingForScan     Waiting for scan...
overlay.riven.readFailed         Couldn't read the riven. Keep the card visible and set Warframe's UI language to English.
overlay.riven.rollReadFailed     Couldn't read the new roll. Keep both cards visible and set Warframe's UI language to English.
overlay.riven.textTooSmall       The riven text is too small to read reliably. Increase Warframe's interface scale or window size.
overlay.riven.weaponMissing      Couldn't read the weapon name, so there are no grades. Use the rescan button to retry.
overlay.riven.bestPositives      Best
overlay.riven.bestNegatives      Neg
overlay.riven.matchPercent       {percent}% match
overlay.riven.rolls              Rolls: {count}
rivens.detail.similarOnWfm       Similar on WFM
rivens.detail.noSimilarFound     No similar rivens found
rivens.detail.auctionsChip       Auctions
```
On the Deck, `textTooSmall` should say "set Interface Scale to 100%".

---

## 17. Fixtures (`tests/fixtures/riven/`) and test vectors

| File | Size | What it is | Expected (test) |
|---|---|---|---|
| `chat-card-angstrum-stats.png` | 224x162 RGB | Stat band of a chat-linked card from a **1278x768** window (Deck geometry) | At x2 (`statCropUpscaleFactor(162)`): `+159 Multishot, +276.2 Damage, +93.2 Fire Rate, -90.9 Projectile Speed`. At x1 the curse line is lost (`rivenCurseLineFixtures.test.ts`) |
| `mod-card-angstrum-stats.png` | 224x162 | The same riven on the mod screen | Same 4 stats at x1 and x2 |
| `mod-card-soma-trait-locked-stats.png` | 337x243 | 1080p U44 Cycle screen, Critical Chance trait-locked (grey, padlocks, wrapped) | `+1.5 Damage to Grineer, +167.2 Critical Chance`. The first is the multiplier `x1.5` (`positive` because ≥1); the test prints sign and value only |
| `choice-card-soma-trait-locked-stats.png` | 326x234 | Dimmed choice card | `+133.7 Puncture, +167.2 Critical Chance` |
| `chat-card-arum-heat-icon-stats.png` | 337x243 | Heat line with a fire icon glyph | `hasLowConfidenceLine == false`; values `86.5 Heat, 86.3 Critical Damage, 140.9 Melee Damage` |
| `roll-card-corufell-trait-locked.png` | 537x468 | **Rough** rollCard crop from 1920x1080 | Paste at `(floor(1920*.36), floor(1080*.416))` into a black 1920x1080 frame, crop with `sourceType="window"`, then `+110.1 Finisher Damage, +88.1 Heat, +166.4 Critical Chance, -101.5 Puncture` |
| `roll-card-boar-argi.png` | 507x442 | Rough rollCard crop from a 1811x1019 window | Lines include `Boar Argi-visitak`, `x1,49 Damage to Grineer`, `+49,2% Reload Speed` |
| `initial-card-sobek-small-ui.png` | 1012x457 RGBA | Rough singleCard crop from 1808x1017 at a small UI scale | All 4 stats, but only through the fallback text-bounds crop (attempt 1). Used by `scripts/riven-scan-e2e`, not by unit tests |
| `roll-scan-replay.json` | 50 scans | A field session (1920x1080), OCR lines per capture attempt: `{scan, note, expected?[], recapture?, stale?, logged[], captures:[{label, attempts:[{yoloBoxes, lines:[[text,conf]]}]}]}` | Replay with OCR mocked through §7.1. The result must never be wrong or short. **36 of 37** `expected` cards are read (scan 7 fails: the padlock line at 0.799 is below 0.8). The 11 `recapture` scans return `[]`. The `stale` scans 19 and 43 return `[]` or a card that `looksLikeStaleCardRead` matches against scans 11 and 20 |
| `wfm-riven-weapons.json` | 418 entries `{slug, gameRef}` | WFM riven weapon slugs with game uniqueNames | `getRivenFamilySlug(nameOf(gameRef)) == slug` (`rivenFamilySlugWfm.test.ts`) |

Also `assets/setup/overlay-demo-riven.jpg` (1920x1080 Cycle screen): `+72.7 Fire Rate, -66.2 Weapon Recoil (buff), +85.7 Multishot, -65.1 Status Duration (curse)`. The FITS IN plate reads "Kuva Sobek", while the title says "Sobek Croni-zetican".

Recommended Deck additions: capture real 1280x800 gamescope screenshots of (a) the Cycle screen, (b) the two-card roll result, (c) the choice result and (d) a chat-linked preview. Use them to pin down §5.2 (16:9 band or not) and the crop fractions.

---

## 18. Windows-only parts and porting risks

Windows-only pieces and their replacements:
- DBWIN real-time log: use the Proton debugstr tail (needs `PROTON_LOG=1 %command%`) or EE.log polling.
- DXGI/WGC capture and its 100 ms "fresh frame" timeout: use the gamescope screenshot.
- Z-order, focus and click-through window management: use the gamescope external overlay.
- The Cloudflare solver window: drop it.
- The Electron `NativeImage` BGRA bitmap: use Pillow RGB. Swap the channel order in §5.6.
- sharp resizing: use Pillow. Lanczos3 maps to `Image.LANCZOS` and sharp `linear` to `Image.BILINEAR`.
- `fs`/`userData` paths: use the plugin settings and data directories.

Risks, highest first:
1. **The 16:10 menu canvas assumption** (§5.2). The crops assume Warframe draws its menus in a centered 1280x720 band. If that is wrong, every crop is off by up to 40 px vertically. Validate with real screenshots before tuning anything else.
2. **Text size at 720p canvas**: the card is about 191 px wide and the stat band 224x162. The existing fixtures show this works only with the x2 upscale (x3 for the roll crop), and only at interface scale ~100%. A smaller UI scale yields `textTooSmall` or the fallback path. The FITS IN plate text is about 13 px. Expect weak plate reads and rely on the diorama path.
3. **Event latency without PROTON_LOG**: EE.log file flushes can lag 10 s or more. The roll scan is a fixed 2850 ms after `SendResult(4)`, so a late line means a late scan. The stale-card check and the choice similarity then guard correctness, but the UX degrades. Prompt the user in the QAM to add the launch option.
4. **Capture latency**: the readiness gate expects 40 ms polls and 3 stable frames within 1.8 s. Gamescope screenshots are slower and touch disk (`/tmp/gamescope.png`). Use a shared-memory or pipe path if available, and measure.
5. **CPU contention**: YOLO (640²) plus 4-8 Paddle crops run 2-3 times per roll on the Deck's 4-core APU while the game renders. Cap ORT intra-op threads at 2, run at nice 10, and load the sessions once in the persistent worker.
6. **English-only parsing** and the possibly localized cycle dialog: `cycleConfirmEn` may no longer match (language key). The generic dialog fallback must work. The weapon then comes from the title, diorama or plate.
7. **Data dependencies**: `warframe-public-export-plus` (about 10-20 MB of JSON) for dispositions and stat bases; the Google Sheet; WFM (Cloudflare). Bundle a PEP snapshot with an update path, and cache everything to disk.
8. **JS/Python semantic drift** in the parser (rounding, `$11`, `\b` and Unicode, `parseFloat`). Port Appendix A literally and run `roll-scan-replay.json` as a CI test. The expected result is 36/37.
9. **Overlay occlusion**: with no input to the overlay, every action moves to the QAM. The panels must not cover the cards or the FITS IN plate (§16.2).

---
## Appendix A: `ipc/overlay/rivenScanText.ts`, verbatim (MIT, WFHelper)

This is the whole parser. `levenshteinDistance` is the standard DP edit distance (`services/rewardScannerUtils.ts:41`).

```ts
import { levenshteinDistance } from "../../services/rewardScannerUtils";

const RIVEN_STAT_ALIAS_REPLACEMENTS: ReadonlyArray<[RegExp, string]> = Object.freeze([
  [/Dannage/gi, "Damage"],
  [/Darnage/gi, "Damage"],
  [/Darnoge/gi, "Damage"],
  [/\bDamage\s+to\s+Corpu\b/gi, "Damage to Corpus"],
  [/\bDamage\s+to\s+Infeste\b/gi, "Damage to Infested"],
  [/Crit\s*ical/gi, "Critical"],
  [/Cri tical/gi, "Critical"],
  [/Critica\b/gi, "Critical"],
  [/Multi\s*shot/gi, "Multishot"],
  [/Sta tus/gi, "Status"],
  [/Statuc/gi, "Status"],
  [/Re load/gi, "Reload"],
  [/Elec tricity/gi, "Electricity"],
  [/Punc ture/gi, "Puncture"],
  [/Pi[uo]ncture/gi, "Puncture"],
  [/Puincture/gi, "Puncture"],
  [/\bReload\s+Spe[de]\b/gi, "Reload Speed"],
  [/Maga zine/gi, "Magazine"],
  [/Capaclty/gi, "Capacity"],
  [/Maxinnunn/gi, "Maximum"],
  [/Maximu[rn]/gi, "Maximum"],
  [/Annnno/gi, "Ammo"],
  [/Mel[ae]e/gi, "Melee"],
  [/Fini sher/gi, "Finisher"],
  [/Finlsher/gi, "Finisher"],
  [/[>]?[lh]mpact/gi, "Impact"],
  [/\bG[Ll]ash\b/gi, "Slash"],
  [/\b\(Glash\b/gi, "Slash"],
  [/\bY\s*Puncture\b/gi, "Puncture"],
  [/\bA\s*Slash\b/gi, "Slash"],
  [/\bO\s*Cold\b/gi, "Cold"],
  [/\bO\s*Heat\b/gi, "Heat"],
  [/\bl\s*eat\b/gi, "Heat"],
  [/\bQ\s*Toxin\b/gi, "Toxin"],
  [/\bQ\s*Electricity\b/gi, "Electricity"],
  // The unique tail recovers "Additional" when PaddleOCR garbles or drops it.
  [/\b(?:[A-Za-z]{2,12}\s+)?Combo\s+Count\s+Chance\b/gi, "Additional Combo Count Chance"],
]);

const KNOWN_RIVEN_STATS: ReadonlyArray<string> = Object.freeze([
  "Additional Combo Count Chance",
  "Chance to Gain Combo Count",
  "Critical Chance for Slide Attack",
  "Heavy Attack Efficiency",
  "Magazine Capacity",
  "Damage to Grineer",
  "Damage to Corpus",
  "Damage to Infested",
  "Critical Chance",
  "Critical Damage",
  "Finisher Damage",
  "Melee Damage",
  "Weapon Recoil",
  "Status Duration",
  "Status Chance",
  "Projectile Speed",
  "Reload Speed",
  "Attack Speed",
  "Flight Speed",
  "Fire Rate",
  "Punch Through",
  "Combo Duration",
  "Initial Combo",
  "Ammo Maximum",
  "Heavy Attack",
  "Channeling Damage",
  "Channeling Efficiency",
  "Multishot",
  "Electricity",
  "Corrosive",
  "Radiation",
  "Magnetic",
  "Cold",
  "Heat",
  "Toxin",
  "Viral",
  "Blast",
  "Gas",
  "Impact",
  "Puncture",
  "Slash",
  "Magazine",
  "Recoil",
  "Damage",
  "Range",
  "Slide",
  "Zoom",
]);

export interface RivenStat {
  name: string;
  positive: boolean;
  displayPositive?: boolean;
  value: number | null;
  multiplier?: boolean;
}

const MAX_REASONABLE_VALUE = 500;

// Recoil alone displays buffs with a minus sign, so its parsed polarity must flip.
const INVERTED_POLARITY_STATS = new Set(["weapon recoil", "recoil"]);

const STAT_END_WORDS = [
  ...new Set(KNOWN_RIVEN_STATS.map((stat) => stat.slice(stat.lastIndexOf(" ") + 1))),
].join("|");
// The padlock closing a trait-locked stat reads as one glued character ("Chancee").
const TRAIT_LOCK_TAIL = new RegExp(`\\b(${STAT_END_WORDS})[^\\s%)]$`, "gim");

const DIGIT_LOOKALIKES: Readonly<Record<string, string>> = {
  g: "9",
  q: "9",
  l: "1",
  I: "1",
  O: "0",
  S: "5",
  B: "8",
};
// A signed or x value read with a digit as a letter ("x1,4g", "+l,7"). A letter
// right after the sign stays icon junk unless it is the whole integer part.
const VALUE_WITH_LOOKALIKE =
  /([+\-\u2013]\s*|\bx\s*)((?:\d[\dgqlIOSB]*|[gqlIOSB](?=\.))(?:\.[\dgqlIOSB]+)?)(?=[\s%]|$)/gm;
// A number must not end on the digit before an unread glyph ("x1.4k" is not x1.4).
const NUMBER_END = String.raw`(?!\.?\d|[A-Za-z](?![A-Za-z]))`;
const X_VALUE = new RegExp(String.raw`x\s*(\d+\.?\d*)` + NUMBER_END, "gi");
const SIGNED_VALUE = new RegExp(String.raw`[+\-\u2013]\s*(\d+\.?\d*)` + NUMBER_END, "g");

function preprocessOcrText(raw: string): string {
  let text = raw.replace(TRAIT_LOCK_TAIL, "$1");
  // The padlock leading a locked stat can read as a digit ("8 +99,5%"); glued to a
  // multiplier, "8x1,51" collapsed to "81.51" and the stat lost its value.
  text = text.replace(/^\d(?=x\s*\d)/gim, "");

  // Colored stat icons make WinRT split two-word names; rejoin before other repairs.
  text = text.replace(/\bFinisher\s*\n+\s*(?=Damage\b)/gi, "Finisher ");
  text = text.replace(/\bMelee\s*\n+\s*(?=Damage\b)/gi, "Melee ");
  text = text.replace(/\bCritical?\s*\n+\s*(?=(?:Chance|Damage)\b)/gi, "Critical ");
  text = text.replace(/\bStatus\s*\n+\s*(?=(?:Chance|Duration)\b)/gi, "Status ");
  text = text.replace(/\bAttack\s*\n+\s*(?=Speed\b)/gi, "Attack ");
  text = text.replace(/\bReload\s*\n+\s*(?=Speed\b)/gi, "Reload ");
  text = text.replace(/\bFlight\s*\n+\s*(?=Speed\b)/gi, "Flight ");
  text = text.replace(/\bProjectile\s*\n+\s*(?=Speed\b)/gi, "Projectile ");
  text = text.replace(/\bFire\s*\n+\s*(?=Rate\b)/gi, "Fire ");
  // Allow leading punctuation junk on the wrapped line: PaddleOCR emits the
  // second line of "Additional Combo / Count Chance" as ". Count Chance".
  text = text.replace(/\bCombo\s*\n+[^\w\n]*(?=(?:Duration|Count)\b)/gi, "Combo ");
  text = text.replace(/\bAdditional\s*\n+\s*(?=Combo\b)/gi, "Additional ");
  text = text.replace(/\bAmmo\s*\n+\s*(?=Maximum\b)/gi, "Ammo ");
  text = text.replace(/\bPunch\s*\n+\s*(?=Through\b)/gi, "Punch ");
  text = text.replace(/\bChanneling\s*\n+\s*(?=(?:Damage|Efficiency)\b)/gi, "Channeling ");
  text = text.replace(/\bWeapon\s*\n+\s*(?=Recoil\b)/gi, "Weapon ");
  text = text.replace(/\bHeavy\s*\n+\s*(?=Attack\b)/gi, "Heavy ");
  text = text.replace(/\bInitial\s*\n+\s*(?=Combo\b)/gi, "Initial ");
  text = text.replace(/\bMagazine\s*\n+\s*(?=Capacity\b)/gi, "Magazine ");
  text = text.replace(/\bDamage\s*\n+\s*(?=to\s+(?:Grineer|Corpus|Infested)\b)/gi, "Damage ");
  text = text.replace(/\bto\s*\n+\s*(?=(?:Grineer|Corpus|Infested)\b)/gi, "to ");
  text = text.replace(/0\/0/g, "%");
  text = text.replace(/O\/O/gi, "%");
  text = text.replace(/o\/o/g, "%");
  text = text.replace(/(\d)\s*Z\b/g, "$1%");
  // PaddleOCR can read the x multiplier glyph as a leading angle bracket:
  // "<1,32 Damage to Infeste" -> "x1.32 Damage to Infested".
  text = text.replace(/[<‹]\s*(\d+[.,]\d+)\s+(?=Damage\s+to\s+)/gi, "x$1 ");
  text = text.replace(/\bx\s*O([.,]\d)/gi, "x0$1");
  // WinRT reads x1 as xl or xI and may separate the glyphs.
  text = text.replace(/\bx\s+[lI1]\s*[,.]\s*(\d+)/gi, "x1.$1");
  text = text.replace(/\bx\s+[lI1]\b/gi, "x1");
  text = text.replace(/\bx[lI]([,.]?\d)/g, "x1$1");
  text = text.replace(/\bx[lI]\b/g, "x1");
  // Collapse spaced decimal on multiplier: "x1 , 44" or "x1 ,44" -> "x1.44".
  text = text.replace(/\bx(\d)\s*,\s*(\d+)/g, "x$1.$2");
  // Repair spaced decimal commas before the general comma conversion.
  text = text.replace(/([+\-\u2013]?\d+),\s+(\d+)\s*%/g, "$1.$2%");
  text = text.replace(/,(\d)/g, ".$1");
  // Rejoin multiplier decimals that WinRT splits across a line boundary.
  text = text.replace(/(x\d+)\n(\.\d+)/g, "$1$2");
  // Also rejoin when the decimal is on the same line with a space:
  // "x1 .3 Damage" -> "x1.3 Damage" / "x1 .36 Damage" -> "x1.36 Damage"
  text = text.replace(/\b(x\d+)\s+\.(\d+)/g, "$1.$2");
  // Attach a trailing orphan decimal to the preceding multiplier.
  text = text.replace(/\b(x\d+)(\s+(?:Damage\s+to\s+\w+|[A-Z][a-z]+))\n\.(\d+)/g, "$1.$3$2");
  // Handle WinRT emitting an integer x-multiplier followed by isolated decimal on next line:
  // "x1\n3 Damage" -> "x1.3 Damage" (when digit after newline is 1-9 and followed by space+stat)
  text = text.replace(/(x\d+)\n([1-9]\d?\s+(?:Damage|[A-Z]))/g, "$1.$2");
  // Fix spaced decimal point: "+151 .4%" -> "+151.4%".
  // WinRT OCR sometimes inserts a space before the decimal point.
  text = text.replace(/(\d)\s+\.(\d)/g, "$1.$2");
  text = text.replace(/(\d)\s([1-9])\s*%/g, "$1.$2%");

  for (let pass = 0; pass < 5; pass++) {
    text = text.replace(/([+\-\u2013]\s*\d+)\s+(\d)/g, "$1$2");
  }

  for (let pass = 0; pass < 5; pass++) {
    text = text.replace(/(\d)\s+(\d)/g, "$1$2");
  }

  // Combo Duration prints a seconds unit, so its S is that unit, not a 5.
  text = text.replace(/(\d)S(?=\s+Combo\s+Dur)/g, "$1s");
  text = text.replace(VALUE_WITH_LOOKALIKE, (match, lead: string, value: string) =>
    /\d/.test(value) ? lead + value.replace(/[gqlIOSB]/g, (c) => DIGIT_LOOKALIKES[c]) : match,
  );
  text = text.replace(/(\d)[A-Za-z](\d)/g, "$1$2");
  for (let pass = 0; pass < 3; pass++) {
    text = text.replace(/(\d)\s+(\d)/g, "$1$2");
  }

  for (const [pattern, replacement] of RIVEN_STAT_ALIAS_REPLACEMENTS) {
    text = text.replace(pattern, replacement);
  }

  text = text.replace(/\(x\d+\s*(?:for\s*)?Heavy\s*Attack[a-z]*\)/gi, "");
  // A clipped band cuts the wrapped qualifier to "(x2 for Hea)" + "Attacks)".
  text = text.replace(/\(\s*x\d+\s*fo[a-z]*\s*Hea[a-z]*\s*\)?(?:\s*Attack[a-z]*\s*\)?)?/gi, "");
  // Fire-rate qualifier "(x2 for Bows)": OCR wraps it across lines and clips
  // letters ("(x2 fol" + "Bows)"), so match loosely and allow the unclosed head.
  text = text.replace(/\(\s*x\d+\s*fo[a-z]*\s*Bows?\s*\)?/gi, "");
  text = text.replace(/\(\s*x\d+\s*fo[a-z]*\s*$/gim, "");
  text = text.replace(/\(\s*(\d+[.,]\d+)/g, "x$1");
  text = text.replace(/[*()[\]{}|\\<>^~°©®™•→←↑↓↗↘►◄▸▾▲▼■□●○]+\s*/g, " ");
  text = text.replace(/\bx\d+\s*(?:for\s*)?Heavy\s*Attack[a-z]*\b/gi, "");
  text = text.replace(/%\s+[A-Z0-9]\s+(?=[A-Z])/g, "% ");
  // Strip isolated uppercase letter (element-icon artifact) between sign and digits.
  // e.g. "+ A0,58 Damage to Grineer" -> "+0,58 Damage to Grineer"
  text = text.replace(/([+\-\u2013]\s*)[A-Z]\s*(\d)/g, "$1$2");
  // Strip 1-2 letters of element-icon junk BEFORE the sign at line start:
  // "Ao-102.5% Status Dura" / "A -34.6% Reload Spe" -> "-102.5% ..." / "-34.6% ...".
  text = text.replace(/^[A-Za-z]{1,2}\s*(?=[+\-\u2013]\s*\d)/gm, "");
  text = text.replace(
    /[0-9'"`]\s*(?=(?:Slash|Cold|Heat|Electricity|Toxin|Impact|Puncture|Radiation|Viral|Corrosive|Blast|Magnetic|Gas)\b)/gi,
    "",
  );
  text = text.replace(
    /\b[A-Z]\s+(?=(?:Slash|Cold|Heat|Electricity|Toxin|Impact|Puncture|Radiation|Viral|Corrosive|Blast|Magnetic|Gas)\b)/g,
    "",
  );
  text = text.replace(
    /[^\w\s+.%\-x]{1,3}\s+(?=(?:Slash|Cold|Heat|Electricity|Toxin|Impact|Puncture|Radiation|Viral|Corrosive|Blast|Magnetic|Gas)\b)/gi,
    "",
  );
  text = text.replace(
    /Critical\s+Chance[^a-zA-Z]{0,20}for\s+Slide\s+Attack/gi,
    "Critical Chance for Slide Attack",
  );
  text = text.replace(/(\d)s(?=\s|$)/g, "$1");
  text = text.replace(/\s+([+\-\u2013]\d)/g, "\n$1");
  text = text.replace(/\s+(x\d)/gi, "\n$1");

  return text;
}

function sanitiseValue(value: number): number {
  if (value > MAX_REASONABLE_VALUE && Number.isInteger(value) && value >= 100) {
    const str = String(value);
    const corrected = parseFloat(str.slice(0, -1) + "." + str.slice(-1));
    if (Number.isFinite(corrected)) return corrected;
  }
  // Merged OCR strips can prepend a digit; strip it only when the result is plausible.
  if (value > 1000 && !Number.isInteger(value)) {
    const str = String(Math.round(value * 10) / 10);
    const dotIdx = str.indexOf(".");
    const intPart = dotIdx >= 0 ? str.slice(0, dotIdx) : str;
    const decPart = dotIdx >= 0 ? str.slice(dotIdx + 1) : "";
    if (intPart.length > 3) {
      const corrected = parseFloat(intPart.slice(1) + (decPart ? "." + decPart : ""));
      if (Number.isFinite(corrected) && corrected > 0 && corrected <= MAX_REASONABLE_VALUE)
        return corrected;
    }
  }
  return value;
}

function extractSignAndValue(
  fragment: string,
): { positive: boolean; value: number | null; multiplier?: boolean } | null {
  const signMatches = [...fragment.matchAll(/[+\-\u2013](?=\s*\d)/g)];
  const lastSign = signMatches[signMatches.length - 1];
  const positive = !lastSign || (lastSign[0] !== "-" && lastSign[0] !== "\u2013");

  const percentMatches = [...fragment.matchAll(/(\d+\.?\d*)\s*%/g)];
  if (percentMatches.length > 0) {
    const parsed = parseFloat(percentMatches[percentMatches.length - 1][1]);
    if (Number.isFinite(parsed)) return { positive, value: sanitiseValue(parsed) };
  }

  const xMultiplier = [...fragment.matchAll(X_VALUE)];
  if (xMultiplier.length > 0) {
    const parsed = parseFloat(xMultiplier[xMultiplier.length - 1][1]);
    if (Number.isFinite(parsed)) return { positive: parsed >= 1, value: parsed, multiplier: true };
  }

  const numAfterSign = [...fragment.matchAll(SIGNED_VALUE)];
  if (numAfterSign.length > 0) {
    const parsed = parseFloat(numAfterSign[numAfterSign.length - 1][1]);
    if (Number.isFinite(parsed)) return { positive, value: sanitiseValue(parsed) };
  }

  if (signMatches.length > 0 || xMultiplier.length > 0) return { positive, value: null };
  return null;
}

export interface RivenParseDiagnostics {
  /** Preprocessed lines that carried a signed value but produced no stat. */
  droppedLines: string[];
  /** A name line (the riven title) sits above the first stat line. */
  titleSeen?: boolean;
}

// Words a wrapped stat or qualifier spills onto its own line ("Bows)", "Capacity").
const STAT_NAME_WORDS: ReadonlySet<string> = new Set([
  ...KNOWN_RIVEN_STATS.flatMap((stat) => stat.toLowerCase().split(" ")),
  "bows",
  "attacks",
]);

function hasTitleAboveStats(cleaned: string): boolean {
  for (const line of cleaned.split(/\r?\n/)) {
    if (looksStatLike(line)) return false;
    const words = line.toLowerCase().match(/[a-z]{4,}/g) ?? [];
    if (words.some((word) => !STAT_NAME_WORDS.has(word))) return true;
  }
  return false;
}

export function parseRivenStats(text: string, diagnostics?: RivenParseDiagnostics): RivenStat[] {
  if (!text) return [];

  const cleaned = preprocessOcrText(text);
  if (diagnostics) diagnostics.titleSeen = hasTitleAboveStats(cleaned);
  const lineDropped: string[] = [];
  const lineResults = parseStatsFromLines(cleaned, lineDropped);
  if (lineResults.length > 0 && lineResults.some((stat) => stat.value !== null)) {
    diagnostics?.droppedLines.push(...lineDropped);
    return lineResults;
  }

  const blob = cleaned.replace(/\r?\n/g, " ");
  const blobDropped: string[] = [];
  const blobResults = parseStatsFromLines(blob, blobDropped);
  const lineScore = lineResults.reduce((score, stat) => score + (stat.value !== null ? 10 : 3), 0);
  const blobScore = blobResults.reduce((score, stat) => score + (stat.value !== null ? 10 : 3), 0);
  const useBlob = blobScore > lineScore;
  diagnostics?.droppedLines.push(...(useBlob ? blobDropped : lineDropped));
  return useBlob ? blobResults : lineResults;
}

function lineContainsKnownStat(line: string): boolean {
  const lineLower = line.toLowerCase();
  return KNOWN_RIVEN_STATS.some((stat) => lineLower.includes(stat.toLowerCase()));
}

/** Edit distance from the fragment to the closest prefix of the stat name. */
function prefixDistance(fragment: string, stat: string): number {
  let prev = Array.from({ length: stat.length + 1 }, (_, j) => j);
  for (let i = 1; i <= fragment.length; i += 1) {
    const cur = [i];
    for (let j = 1; j <= stat.length; j += 1) {
      const substitution = prev[j - 1] + (fragment[i - 1] === stat[j - 1] ? 0 : 1);
      cur.push(Math.min(prev[j] + 1, cur[j - 1] + 1, substitution));
    }
    prev = cur;
  }
  return Math.min(...prev);
}

function shortestOfChain(candidates: string[]): string | null {
  if (candidates.length === 0) return null;
  const sorted = [...candidates].sort((a, b) => a.length - b.length);
  const shortest = sorted[0].toLowerCase();
  return sorted.every((stat) => stat.toLowerCase().startsWith(shortest)) ? sorted[0] : null;
}

function normalizeStatFragment(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z]+$/, "")
    .replace(/\s+/g, " ")
    .trim();
}

// Complete crop-truncated stat names only when every candidate has one shared prefix.
function completeTruncatedStatName(fragment: string, allowSlip = true): string | null {
  const frag = normalizeStatFragment(fragment);
  if (frag.length < 5) return null;
  const whole = KNOWN_RIVEN_STATS.find((stat) => stat.toLowerCase() === frag);
  if (whole) return whole;
  // A lone word that ends other stats ("Chance") lost its head, not its tail.
  if (!frag.includes(" ") && KNOWN_RIVEN_STATS.some((s) => s.toLowerCase().endsWith(` ${frag}`))) {
    return null;
  }
  const exact = KNOWN_RIVEN_STATS.filter((stat) => {
    const statLower = stat.toLowerCase();
    return statLower.length > frag.length && statLower.startsWith(frag);
  });
  if (exact.length > 0) return shortestOfChain(exact);
  // One slip on a long fragment: a half-clipped last glyph ("Status Char") or a
  // lost first letter ("tatus Chan").
  if (!allowSlip || frag.length < 8) return null;
  return shortestOfChain(
    KNOWN_RIVEN_STATS.filter((stat) => prefixDistance(frag, stat.toLowerCase()) <= 1),
  );
}

const FACTION_TAIL = /\b(grineer|corpus|infested)\W*$/i;

// Only faction damage takes a multiplier, so its faction word alone names the stat.
function factionMultiplierStat(line: string, namePart: string): string | null {
  const faction = FACTION_TAIL.exec(namePart);
  if (!faction || !extractSignAndValue(line)?.multiplier) return null;
  const word = faction[1].toLowerCase();
  return `Damage to ${word[0].toUpperCase()}${word.slice(1)}`;
}

// Heads of wrapped names; no riven stat is called only this.
const WRAP_HEAD_NAMES: ReadonlySet<string> = new Set(["Magazine", "Heavy Attack"]);

// Stats whose name ends another stat's name, keyed by that shorter name.
const LONGER_STATS_BY_TAIL = new Map(
  KNOWN_RIVEN_STATS.map((tail) => [
    tail.toLowerCase(),
    KNOWN_RIVEN_STATS.filter((stat) => stat.toLowerCase().endsWith(` ${tail.toLowerCase()}`)),
  ]),
);

/** Resolves a garbled head word before a short stat ("C...ical Damage").
 *  Returns the longer stat, null when the head names none, or undefined when
 *  there is no head word to resolve. */
function resolveGarbledHead(
  line: string,
  hit: { stat: string; idx: number },
  prefixStart: number,
): { stat: string; idx: number } | null | undefined {
  const longer = LONGER_STATS_BY_TAIL.get(hit.stat.toLowerCase()) ?? [];
  if (longer.length === 0) return undefined;
  const head = /([A-Za-z]{3,})[^A-Za-z\d]*$/.exec(line.slice(prefixStart, hit.idx));
  if (!head) return undefined;
  const frag = head[1].toLowerCase();
  const matches = longer.filter((stat) => {
    const words = stat.toLowerCase().split(" ");
    const word = words[words.length - hit.stat.split(" ").length - 1] ?? "";
    if (word.endsWith(frag)) return true;
    return frag.length >= 4 && levenshteinDistance(frag, word.slice(-frag.length)) <= 1;
  });
  if (matches.length !== 1) return null;
  return { stat: matches[0], idx: prefixStart + head.index };
}

function collapseOrphanValueLines(lines: string[]): string[] {
  const collapsed: string[] = [];
  // Queue orphan values so interleaved name noise cannot consume their matching stat.
  const pendingValues: string[] = [];

  for (let index = 0; index < lines.length; index += 1) {
    const current = lines[index].trim();
    if (!current) continue;

    const extracted = extractSignAndValue(current);
    const looksLikeValueOnly =
      !!extracted &&
      extracted.value !== null &&
      !lineContainsKnownStat(current) &&
      // Allow trailing comma so "+62," (integer part of a split "+62.2%") is
      // treated as a value-only orphan and paired with the following stat name.
      /^[+\-\u2013x\d\s.,% ]+$/i.test(current) &&
      // Bare integers are edge UI artifacts, not stat values.
      !/^\d{1,4}$/.test(current.trim());

    if (looksLikeValueOnly) {
      pendingValues.push(current);
      continue;
    }

    if (pendingValues.length > 0 && lineContainsKnownStat(current)) {
      // Do not prepend an orphan when this stat line already carries its own value.
      const lineOwnValue = extractSignAndValue(current);
      if (lineOwnValue === null || lineOwnValue.value === null) {
        const prefix = pendingValues.shift()!;
        collapsed.push(`${prefix} ${current}`.trim());
      } else {
        collapsed.push(current);
      }
    } else {
      collapsed.push(current);
    }
  }

  // Flush any remaining orphan values so they are at least visible to the blob-parse fallback.
  for (const pending of pendingValues) {
    collapsed.push(pending);
  }

  return collapsed;
}

// Combined damage types share one displayed value, so the second may inherit the first.
const DAMAGE_TYPE_STAT_NAMES: ReadonlySet<string> = new Set([
  "electricity",
  "corrosive",
  "radiation",
  "magnetic",
  "cold",
  "heat",
  "toxin",
  "viral",
  "blast",
  "gas",
  "impact",
  "puncture",
  "slash",
]);

// A dropped line is worth reporting when it plainly carried a stat value.
function looksStatLike(line: string): boolean {
  return /[+\-–]\s*\d/.test(line) || /\bx\s*\d/i.test(line);
}

// A value with no name is a glare-split piece of a line the read kept, not a lost stat.
const MIN_DROPPED_STAT_NAME_CHARS = 3;

export function looksLikeWholeStatLine(line: string): boolean {
  if (!looksStatLike(line)) return false;
  // "x2 for Hea Attacks" is a wrapped qualifier, not a stat of its own.
  if (/\bx\d+\s*fo/i.test(line) && !/[+\-–]\s*\d/.test(line)) return false;
  return (line.match(/[A-Za-z]/g)?.length ?? 0) >= MIN_DROPPED_STAT_NAME_CHARS;
}

function parseStatsFromLines(text: string, dropped?: string[]): RivenStat[] {
  const lines = collapseOrphanValueLines(text.split(/\r?\n/));
  const results: RivenStat[] = [];
  const seen = new Set<string>();

  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line) continue;
    const lineLower = line.toLowerCase();

    const hits: Array<{ stat: string; idx: number }> = [];
    for (const stat of KNOWN_RIVEN_STATS) {
      const idx = lineLower.indexOf(stat.toLowerCase());
      if (idx !== -1) hits.push({ stat, idx });
    }

    // Fuzzy-match signed lines to recover small OCR errors in known stat names.
    if (hits.length === 0) {
      if (/^[+\-\u2013x\xd7]/i.test(line)) {
        // Strip sign+value prefix to isolate the stat name portion
        const namePart = line.replace(/^[+\-\u2013x\xd7]?[\d.,\s%]*/, "").trim();
        // Right-truncation first: "+152.3% Critical Cha" is beyond the fuzzy
        // distance budget but is a clean prefix of "Critical Chance".
        const completed =
          factionMultiplierStat(line, namePart) ?? completeTruncatedStatName(namePart);
        if (completed) {
          const idx = lineLower.indexOf(namePart.toLowerCase().slice(0, 4));
          if (idx >= 0) hits.push({ stat: completed, idx });
        }
        // The padlock or an element icon leaves glyph junk before the name (":Toxir").
        const fuzzyName = namePart.replace(/^[^A-Za-z]+/, "").toLowerCase();
        if (hits.length === 0 && fuzzyName.length >= 3) {
          // Two edits on a short word turn "+5pact" into Impact; keep them for long names.
          const maxDist = fuzzyName.length >= 7 ? 2 : 1;
          let bestStat = "";
          let bestDist = Infinity;
          let bestIdx = -1;
          const namePartLower = namePart.toLowerCase();
          for (const stat of KNOWN_RIVEN_STATS) {
            const statLower = stat.toLowerCase();
            // Compare the name portion (trimmed to stat length + slack) against the stat
            const dist = levenshteinDistance(fuzzyName.slice(0, statLower.length + 2), statLower);
            if (dist < bestDist && dist <= maxDist) {
              bestDist = dist;
              bestStat = stat;
              bestIdx = lineLower.indexOf(namePartLower);
            }
          }
          if (bestStat && bestIdx >= 0) {
            hits.push({ stat: bestStat, idx: bestIdx });
          }
        }
      }
      if (hits.length === 0) {
        if (dropped && looksStatLike(line)) dropped.push(line);
        continue;
      }
    }

    hits.sort((a, b) => a.idx - b.idx || b.stat.length - a.stat.length);
    const filtered: typeof hits = [];
    let lastEnd = -1;
    for (const hit of hits) {
      if (hit.idx >= lastEnd) {
        filtered.push(hit);
        lastEnd = hit.idx + hit.stat.length;
      }
    }

    // Extend short matches only when the cleaned tail is an unambiguous longer stat.
    for (let index = 0; index < filtered.length; index++) {
      const hit = filtered[index];
      const tailEnd = index + 1 < filtered.length ? filtered[index + 1].idx : line.length;
      const cleanTail = normalizeStatFragment(line.slice(hit.idx, tailEnd));
      if (cleanTail.length <= hit.stat.length) continue;
      // A lone junk letter past a whole name ("Critical Chance f") is no start of a
      // longer stat; a wrap head is no stat of its own, so one letter decides there.
      const extension = cleanTail.slice(hit.stat.length).replace(/[^a-z]/g, "");
      if (extension.length < 2 && !WRAP_HEAD_NAMES.has(hit.stat)) continue;
      const completed = completeTruncatedStatName(
        cleanTail,
        cleanTail.length - hit.stat.length > 3,
      );
      if (completed && completed.toLowerCase().startsWith(hit.stat.toLowerCase())) {
        filtered[index] = { stat: completed, idx: hit.idx };
      }
    }

    // Base Damage and the four *-Damage stats differ, so a head word that names
    // none of them voids the hit, as does any letter between the value and a
    // base Damage, which has no icon; Recoil and Weapon Recoil are one stat.
    for (let index = filtered.length - 1; index >= 0; index--) {
      const hit = filtered[index];
      const prefixStart = index > 0 ? filtered[index - 1].idx + filtered[index - 1].stat.length : 0;
      const resolved = resolveGarbledHead(line, hit, prefixStart);
      if (resolved) filtered[index] = resolved;
      else if (
        hit.stat === "Damage" &&
        (resolved === null || /[A-Za-z][^\d%]*$/.test(line.slice(prefixStart, hit.idx)))
      ) {
        filtered.splice(index, 1);
      }
    }
    if (filtered.length === 0) {
      if (dropped && looksStatLike(line)) dropped.push(line);
      continue;
    }

    for (let index = 0; index < filtered.length; index++) {
      const { stat, idx } = filtered[index];
      const key = stat.toLowerCase();

      // Compute prefix/value before the seen-check so the deduplication logic
      // below can compare the new value against the existing one.
      const prefixStart = index > 0 ? filtered[index - 1].idx + filtered[index - 1].stat.length : 0;
      const prefix = line.slice(prefixStart, idx);
      let extracted = extractSignAndValue(prefix);

      if (!extracted || extracted.value === null) {
        const suffixEnd = index + 1 < filtered.length ? filtered[index + 1].idx : line.length;
        const suffix = line.slice(idx + stat.length, suffixEnd);
        const suffixExtracted = extractSignAndValue(suffix);
        if (suffixExtracted && suffixExtracted.value !== null) {
          extracted = suffixExtracted;
        }
      }

      const positive = extracted?.positive ?? true;
      let value = extracted?.value ?? null;
      let effectivePositive = positive;
      const displayPositive = positive;
      const multiplier = extracted?.multiplier ?? false;

      // Multipliers belong only to faction damage; elsewhere they are qualifier OCR junk.
      if (multiplier && !/^Damage\b/.test(stat)) continue;

      if (seen.has(key)) {
        const existingIdx =
          value === null ? -1 : results.findIndex((r) => r.name.toLowerCase() === key);
        const existingValue = existingIdx >= 0 ? results[existingIdx].value : undefined;
        // A riven title can spell a stat ("Hexa-toxinok"); its valueless hit must not
        // shadow the stat line, which keeps its own place in card order.
        if (value !== null && existingValue === null) {
          results.splice(existingIdx, 1);
        } else {
          // Prefer a duplicate with decimal precision when its integer part still matches.
          if (
            value !== null &&
            existingValue != null &&
            Number.isInteger(existingValue) &&
            !Number.isInteger(value) &&
            Math.floor(value) === existingValue
          ) {
            results[existingIdx] = {
              name: stat,
              positive: effectivePositive,
              ...(displayPositive !== effectivePositive && { displayPositive }),
              value,
              ...(multiplier && { multiplier: true }),
            };
          }
          continue;
        }
      }
      seen.add(key);

      // Carry values only across adjacent damage types; noisy signs mark separate OCR rows.
      const hasNoisySignInPrefix = /[+\-\u2013]\s*\S/.test(prefix);
      if (value === null && index > 0 && DAMAGE_TYPE_STAT_NAMES.has(key) && !hasNoisySignInPrefix) {
        const prev = results[results.length - 1];
        // Multipliers and non-damage stats cannot start combined elemental rolls.
        const prevIsDamageType = prev && DAMAGE_TYPE_STAT_NAMES.has(prev.name.toLowerCase());
        if (prev && prev.value !== null && !prev.multiplier && prevIsDamageType) {
          value = prev.value;
          effectivePositive = prev.positive;
        }
      }

      // Recoil displays buffs with a minus sign, opposite the parsed polarity.
      if (INVERTED_POLARITY_STATS.has(key)) {
        effectivePositive = !effectivePositive;
      }

      results.push({
        name: stat,
        positive: effectivePositive,
        ...(displayPositive !== effectivePositive && { displayPositive }),
        value,
        ...(multiplier && { multiplier: true }),
      });
    }
  }

  return results;
}

function countExactValueMatches(scanned: RivenStat[], known: RivenStat[]): number {
  const knownByName = new Map(known.map((stat) => [stat.name.toLowerCase(), stat] as const));
  let count = 0;
  for (const stat of scanned) {
    const match = knownByName.get(stat.name.toLowerCase());
    if (!match || stat.positive !== match.positive) continue;
    if (stat.value == null || match.value == null) continue;
    // Multipliers print two decimals: x1.21 is a different roll from x1.22.
    const tolerance = stat.multiplier || match.multiplier ? 0.005 : 0.05;
    if (Math.abs(stat.value - match.value) <= tolerance) count += 1;
  }
  return count;
}

/** A reroll never repeats exact stat values; two value-exact matches against a
 *  known card mean the scan caught that card (mid-animation), not the new roll. */
export function looksLikeStaleCardRead(scanned: RivenStat[], knownCards: RivenStat[][]): boolean {
  if (scanned.length < 2) return false;
  return knownCards.some((card) => card.length > 0 && countExactValueMatches(scanned, card) >= 2);
}

export function rollRescanReason(scanned: RivenStat[], knownCards: RivenStat[][]): string | null {
  if (scanned.length === 0) return "read nothing";
  if (looksLikeStaleCardRead(scanned, knownCards)) return "matches a pre-roll card";
  return null;
}
```

## Appendix B: OCR post-processing, verbatim (`services/rivenOcrOnnx.ts:534-677`)

```ts
/** Deterministic corrections for known PaddleOCR CH misreads (ports postprocess_ocr_text). */
function postprocessOcrText(text: string): string {
  // Strip asterisk-minus artifact: "*-74,2%" -> "-74,2%"
  text = text.replace(/\*-/g, "-");
  // Strip > before uppercase: ">Impact" -> "Impact"
  text = text.replace(/>([A-Z])/g, "$1");

  // Insert spaces before CamelCase boundaries
  text = text.replace(/%([A-Z])/g, (m) => m[0] + " " + m[1]);
  text = text.replace(/([a-z])([A-Z])/g, "$1 $2");

  // Word join fixes
  text = text.replace(/Damageto/g, "Damage to");
  text = text.replace(/\bfor([A-Z])/g, "for $1");
  text = text.replace(/(\d)for\b/g, "$1 for");

  // Recover dropped 'x' prefix on multiplier lines
  text = text.replace(
    /(?:^|\n)(\*?)[A-Za-z]?(\d[,.]?\d*)\s*Damage\s+to\b/gm,
    (_, _star, num) => `x${num} Damage to`,
  );

  // Common letter misreads
  text = text.replace(/Mmpact/g, "Impact");
  text = text.replace(/%\s*mpact/g, "% Impact");

  // Sign + 'i' + digits -> sign + '1' + digits: "+i29,1%" -> "+129,1%"
  text = text.replace(/([+-])i(\d)/g, "$11$2");

  // x-multiplier: xi/xl -> x1
  text = text.replace(/\bx[il]([,.])/g, "x1$1");

  // Double-1: digit + i before separator
  text = text.replace(/(\d)i([,.])/g, "$11$2");

  // Spurious dots: "197.,9%" -> "197,9%"
  text = text.replace(/(\d)\.,(\d)/g, "$1,$2");
  text = text.replace(/(\d)\.\.(\d)/g, "$1.$2");

  // Space between digits: "+1 56,2%" -> "+156,2%"
  text = text.replace(/(\d) (\d)/g, "$1$2");

  // OCR misspelling
  text = text.replace(/\bAditional\b/g, "Additional");

  return text;
}

const SPLIT_STAT_TAILS: Record<string, string> = {
  "slide attack": "Critical Chance for Slide Attack",
  "for slide attack": "Critical Chance for Slide Attack",
  "for slide": "Critical Chance for Slide Attack",
  "count chance": "Additional Combo Count Chance",
  "combo count chance": "Additional Combo Count Chance",
  "combo count": "Chance to Gain Combo Count",
  "gain combo count": "Chance to Gain Combo Count",
  damage: "Finisher Damage",
  efficiency: "Heavy Attack Efficiency",
  "attack efficiency": "Heavy Attack Efficiency",
  capacity: "Magazine Capacity",
  "heavy attacks": "Critical Chance",
  "for heavy attacks": "Critical Chance",
  "x2 for heavy attacks": "Critical Chance",
  "for bows": "Fire Rate",
  "x2 for bows": "Fire Rate",
  duration: "Status Duration",
  speed: "Reload Speed",
  maximum: "Ammo Maximum",
  recoil: "Weapon Recoil",
  chance: "Status Chance",
};

const SPLIT_STAT_HEADS = new Set([
  "critical chance",
  "critical chance for",
  "additional combo",
  "additional combo count",
  "aditional combo",
  "aditional combo count",
  "chance to gain",
  "chance to gain combo",
  "finisher",
  "melee",
  "heavy attack",
  "heavy",
  "magazine",
  "fire rate",
  "fire rate x2",
  "fire rate x2 for",
  "critical chance x2",
  "critical chance x2 for",
  "status",
  "reload",
  "ammo",
  "weapon",
]);

function normForMerge(s: string): string {
  let n = s.toLowerCase().trim();
  n = n.replace(/[()]/g, "");
  n = n.replace(/\s+/g, " ");
  return n;
}

// A lone CJK glyph at 0.04-0.46 confidence (263 benchmark screenshots) is a mark,
// not text, and can sit between the two halves of a wrapped name.
const GLYPH_JUNK_LINE = /^[^A-Za-z0-9%+\-\u2013\u00d7]*$/;

/** Merges consecutive fragments of a known multi-word stat. */
export function mergeSplitLines(lines: OcrLineResult[]): OcrLineResult[] {
  const merged: OcrLineResult[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const lineNorm = normForMerge(line.text);
    let statPart = lineNorm.replace(/^[+\-x]?[\d.,]+%?\s*/, "").trim();
    statPart = statPart.replace(/[^a-z0-9 ]/g, "").trim();

    // Tolerate a right-truncated head ("magazin" for "magazine") - the stat
    // crop clips the card edge, so the head fragment may be missing chars.
    const headMatches =
      SPLIT_STAT_HEADS.has(statPart) ||
      (statPart.length >= 4 && [...SPLIT_STAT_HEADS].some((head) => head.startsWith(statPart)));
    let next = i + 1;
    while (headMatches && next < lines.length && GLYPH_JUNK_LINE.test(lines[next].text)) next += 1;
    if (next < lines.length && headMatches) {
      const tail = lines[next];
      const nextClean = normForMerge(tail.text)
        .replace(/[^a-z0-9 ]/g, "")
        .trim();
      if (nextClean in SPLIT_STAT_TAILS) {
        merged.push({
          text: line.text.trimEnd() + " " + tail.text.trimStart(),
          confidence: Math.min(line.confidence, tail.confidence),
        });
        i = next + 1;
        continue;
      }
    }
    merged.push(line);
    i += 1;
  }
  return merged;
}
```

## Appendix C: Stat tables, verbatim

### C.1 OCR stat name to upgrade tag (`services/rivenData.ts:62-104`)
```ts
const STAT_NAME_TO_TAG: Record<string, string> = {
  // Shared ranged stats
  "critical chance": "WeaponCritChanceMod",
  "critical damage": "WeaponCritDamageMod",
  multishot: "WeaponFireIterationsMod",
  "fire rate": "WeaponFireRateMod",
  damage: "WeaponDamageAmountMod",
  "reload speed": "WeaponReloadSpeedMod",
  "status chance": "WeaponStunChanceMod",
  "status duration": "WeaponProcTimeMod",
  "punch through": "WeaponPunctureDepthMod",
  "magazine capacity": "WeaponClipMaxMod",
  "ammo maximum": "WeaponAmmoMaxMod",
  "weapon recoil": "WeaponRecoilReductionMod",
  recoil: "WeaponRecoilReductionMod",
  zoom: "WeaponZoomFovMod",
  "projectile speed": "WeaponProjectileSpeedMod",
  // Physical damage
  impact: "WeaponImpactDamageMod",
  puncture: "WeaponArmorPiercingDamageMod",
  slash: "WeaponSlashDamageMod",
  // Elemental damage
  cold: "WeaponFreezeDamageMod",
  heat: "WeaponFireDamageMod",
  electricity: "WeaponElectricityDamageMod",
  toxin: "WeaponToxinDamageMod",
  // Faction damage (ranged)
  "damage to grineer": "WeaponFactionDamageGrineer",
  "damage to corpus": "WeaponFactionDamageCorpus",
  "damage to infested": "WeaponFactionDamageInfested",
  // Melee-specific
  "melee damage": "WeaponMeleeDamageMod",
  "attack speed": "WeaponFireRateMod",
  range: "WeaponMeleeRangeIncMod",
  "combo duration": "ComboDurationMod",
  "critical chance for slide attack": "SlideAttackCritChanceMod",
  "slide attack": "SlideAttackCritChanceMod",
  "finisher damage": "WeaponMeleeFinisherDamageMod",
  "heavy attack efficiency": "WeaponMeleeComboEfficiencyMod",
  "initial combo": "WeaponMeleeComboInitialBonusMod",
  "chance to gain combo count": "WeaponMeleeComboPointsOnHitMod",
  "additional combo count chance": "WeaponMeleeComboBonusOnHitMod",
};
```

### C.2 Tag to display name (`config/shared/rivenStatDisplayNames.ts`)
```ts
const STAT_TAG_TO_DISPLAY_NAME: Record<string, string> = {
  WeaponCritChanceMod: "Critical Chance",
  WeaponCritDamageMod: "Critical Damage",
  WeaponFireIterationsMod: "Multishot",
  WeaponFireRateMod: "Fire Rate",
  WeaponDamageAmountMod: "Damage",
  WeaponMeleeDamageMod: "Melee Damage",
  WeaponReloadSpeedMod: "Reload Speed",
  WeaponStunChanceMod: "Status Chance",
  WeaponProcTimeMod: "Status Duration",
  WeaponPunctureDepthMod: "Punch Through",
  WeaponClipMaxMod: "Magazine Capacity",
  WeaponAmmoMaxMod: "Ammo Maximum",
  WeaponRecoilReductionMod: "Weapon Recoil",
  WeaponZoomFovMod: "Zoom",
  WeaponProjectileSpeedMod: "Projectile Speed",
  WeaponImpactDamageMod: "Impact",
  WeaponArmorPiercingDamageMod: "Puncture",
  WeaponSlashDamageMod: "Slash",
  WeaponFreezeDamageMod: "Cold",
  WeaponFireDamageMod: "Heat",
  WeaponElectricityDamageMod: "Electricity",
  WeaponToxinDamageMod: "Toxin",
  WeaponFactionDamageGrineer: "Damage to Grineer",
  WeaponFactionDamageCorpus: "Damage to Corpus",
  WeaponFactionDamageInfested: "Damage to Infested",
  WeaponMeleeRangeIncMod: "Range",
  ComboDurationMod: "Combo Duration",
  SlideAttackCritChanceMod: "Slide Attack",
  WeaponMeleeFinisherDamageMod: "Finisher Damage",
  WeaponMeleeComboEfficiencyMod: "Heavy Attack Efficiency",
  WeaponMeleeComboInitialBonusMod: "Initial Combo",
  WeaponMeleeComboPointsOnHitMod: "Chance to Gain Combo Count",
  WeaponMeleeComboBonusOnHitMod: "Additional Combo Count Chance",
};

/** Resolves an upgrade tag to its display label. */
export function statTagToDisplayName(tag: string, melee = false): string {
  if (melee && tag === "WeaponFireRateMod") return "Attack Speed";
  return STAT_TAG_TO_DISPLAY_NAME[tag] ?? tag;
}
```

### C.3 Tag to WFM attribute url_name (`config/shared/wfmRivenVocabulary.ts:6-43`)
```ts
export const TAG_TO_WFM_URL_NAME: Record<string, string> = {
  WeaponCritChanceMod: "critical_chance",
  WeaponCritDamageMod: "critical_damage",
  WeaponFireIterationsMod: "multishot",
  WeaponFireRateMod: "fire_rate_/_attack_speed",
  WeaponDamageAmountMod: "base_damage_/_melee_damage",
  WeaponReloadSpeedMod: "reload_speed",
  WeaponStunChanceMod: "status_chance",
  WeaponProcTimeMod: "status_duration",
  WeaponPunctureDepthMod: "punch_through",
  WeaponClipMaxMod: "magazine_capacity",
  WeaponAmmoMaxMod: "ammo_maximum",
  WeaponRecoilReductionMod: "recoil",
  WeaponZoomFovMod: "zoom",
  WeaponProjectileSpeedMod: "projectile_speed",
  WeaponImpactDamageMod: "impact_damage",
  WeaponArmorPiercingDamageMod: "puncture_damage",
  WeaponSlashDamageMod: "slash_damage",
  WeaponFreezeDamageMod: "cold_damage",
  WeaponFireDamageMod: "heat_damage",
  WeaponElectricityDamageMod: "electric_damage",
  WeaponToxinDamageMod: "toxin_damage",
  WeaponFactionDamageGrineer: "damage_vs_grineer",
  WeaponFactionDamageCorpus: "damage_vs_corpus",
  WeaponFactionDamageInfested: "damage_vs_infested",
  WeaponMeleeDamageMod: "base_damage_/_melee_damage",
  WeaponMeleeRangeIncMod: "range",
  ComboDurationMod: "combo_duration",
  SlideAttackCritChanceMod: "critical_chance_on_slide_attack",
  WeaponMeleeFinisherDamageMod: "finisher_damage",
  WeaponMeleeComboEfficiencyMod: "channeling_efficiency",
  WeaponMeleeComboInitialBonusMod: "channeling_damage",
  WeaponMeleeComboPointsOnHitMod: "chance_to_gain_combo_count",
  WeaponMeleeComboBonusOnHitMod: "chance_to_gain_extra_combo_count",
  WeaponMeleeFactionDamageGrineer: "damage_vs_grineer",
  WeaponMeleeFactionDamageCorpus: "damage_vs_corpus",
  WeaponMeleeFactionDamageInfested: "damage_vs_infested",
};
```

### C.4 Good-roll sheet abbreviations (`config/shared/rivenGoodRolls.ts:50-90`)
```ts
const TAG: Record<string, string> = {
  CC: "WeaponCritChanceMod",
  CD: "WeaponCritDamageMod",
  MS: "WeaponFireIterationsMod",
  FR: "WeaponFireRateMod",
  AS: "WeaponFireRateMod",
  RLS: "WeaponReloadSpeedMod",
  SC: "WeaponStunChanceMod",
  SD: "WeaponProcTimeMod",
  PT: "WeaponPunctureDepthMod",
  MAG: "WeaponClipMaxMod",
  AMMO: "WeaponAmmoMaxMod",
  REC: "WeaponRecoilReductionMod",
  ZOOM: "WeaponZoomFovMod",
  PFS: "WeaponProjectileSpeedMod",
  IMP: "WeaponImpactDamageMod",
  PUNC: "WeaponArmorPiercingDamageMod",
  SLASH: "WeaponSlashDamageMod",
  COLD: "WeaponFreezeDamageMod",
  HEAT: "WeaponFireDamageMod",
  ELEC: "WeaponElectricityDamageMod",
  TOX: "WeaponToxinDamageMod",
  DTG: "WeaponFactionDamageGrineer",
  DTC: "WeaponFactionDamageCorpus",
  DTI: "WeaponFactionDamageInfested",
  RANGE: "WeaponMeleeRangeIncMod",
  IC: "WeaponMeleeComboInitialBonusMod",
  EFF: "WeaponMeleeComboEfficiencyMod",
  SLIDE: "SlideAttackCritChanceMod",
  FIN: "WeaponMeleeFinisherDamageMod",
};

const ELEMENT_TAGS = [TAG.HEAT, TAG.COLD, TAG.ELEC, TAG.TOX];

function tagsFor(abbrev: string, klass: RivenGoodRollClass): string[] {
  const key = abbrev.toUpperCase().trim();
  if (!key) return [];
  if (key === "DMG") return [klass === "melee" ? "WeaponMeleeDamageMod" : "WeaponDamageAmountMod"];
  if (key === "ELEMENT") return ELEMENT_TAGS.slice();
  return TAG[key] ? [TAG[key]] : [];
}
```

## Appendix D: Fingerprint label and challenge tables, verbatim (`services/rivenFingerprint.ts:34-127`)

```ts
const RIVEN_TYPE_LABELS: Record<string, string> = {
  // Lotus (unveiled / individual-veiled in Upgrades)
  LotusRifleRandomModRare: "Rifle",
  LotusShotgunRandomModRare: "Shotgun",
  LotusPistolRandomModRare: "Pistol",
  PlayerMeleeWeaponRandomModRare: "Melee",
  LotusArchgunRandomModRare: "Archgun",
  LotusModularPistolRandomModRare: "Kitgun",
  LotusModularMeleeRandomModRare: "Zaw",
  // Raw (stackable veiled in RawUpgrades)
  RawRifleRandomMod: "Rifle",
  RawShotgunRandomMod: "Shotgun",
  RawPistolRandomMod: "Pistol",
  RawMeleeRandomMod: "Melee",
  RawArchgunRandomMod: "Archgun",
  RawModularPistolRandomMod: "Kitgun",
  RawModularMeleeRandomMod: "Zaw",
};

// Keys match exact challenge path suffixes; {n} is replaced with the Required count.

const CHALLENGE_DESCS: Record<string, string> = {
  RandomizedKill: "Kill {n} Enemies",
  RandomizedKillPassengers: "Kill {n} Enemies that are on a Dropship",
  RandomizedKillFallingPilots: "Kill {n} Enemies with Headshots",
  RandomizedFinisherKill: "Kill {n} Enemies with Finishers",
  RandomizedHeadshot: "Kill {n} Enemies with Headshots",
  RandomizedHeadshotGlide: "Get {n} Headshot kills in a single Aim Glide",
  RandomizedStyleKill: "Kill {n} Enemies while Sliding",
  RandomizedWallClingKillstreak:
    "Get {n} kills in a row while Wall Dashing or Wall Latching without touching the floor",
  RandomizedHeadshotUnawareBallistas: "Kill {n} unalerted Tusk Ballistas with a Headshot",
  RandomizedKillSentients: "Get the killing blow on {n} Sentients",
  RandomizedLongRangeSniper: "Kill {n} Enemies with Headshots from at least 75m away",
  RandomizedSkiffArcher: "Destroy {n} Dargyns in flight using a bow",
  RandomizedAntiAntiAir:
    "Destroy {n} Vruush Turrets while in Archwing without dying or becoming downed",
  RandomizedFlyingHeadshotSeries:
    "Land {n} consecutive headshots while in Archwing in the Plains of Eidolon",
  RandomizedFindCaches: "Find {n} Caches",
  RandomizedFindRareMedallions: "Pick up {n} Syndicate Medallions",
  RandomizedFisherman: "Catch {n} fish without missing a throw",
  SustainMeleeComboThree: "Sustain a 6x Melee Combo Multiplier for 30s",
  HighSurvivalPacifist:
    "Complete a Survival mission with level 30 or higher enemies without killing anyone",
  HighExterminationUndetected:
    "Complete an Extermination mission with level 30 or higher enemies without being detected",
  HighPerfectDefense:
    "Complete a Defense mission with level 30 or higher enemies with the defense objective taking no damage",
  HighSoloInterceptionHobbled:
    "Complete a Solo Interception mission with level 30 or higher enemies and a Hobbled Dragon Key equipped",
  LimitedSynthesis:
    "Synthesize a Simaris target without using Traps or Abilities while having a Hobbled Dragon Key equipped",
  PlainsTimedVariety: "Catch one fish, mine one gem or metal, and kill one enemy in 30 seconds",
  KahlMissions: "Complete {n} Kahl missions",
  // DJRandomized is the same challenge set from a different pool.
  DJRandomizedKill: "Kill {n} Enemies",
  DJRandomizedFinisherKill: "Kill {n} Enemies with Finishers",
  DJRandomizedHeadshot: "Kill {n} Enemies with Headshots",
  DJRandomizedStyleKill: "Kill {n} Enemies while Sliding",
  DJRandomizedWallClingKillstreak:
    "Get {n} kills in a row while Wall Dashing or Wall Latching without touching the floor",
  DJRandomizedKillPassengers: "Kill {n} Enemies that are on a Dropship",
  DJRandomizedHeadshotUnawareBallistas: "Kill {n} unalerted Tusk Ballistas with a Headshot",
  DJRandomizedFindCaches: "Find {n} Caches",
};

const COMPLICATION_DESCS: Record<string, string> = {
  ResetOnDamageTaken: "without taking damage",
  ResetOnDowned: "without dying or becoming downed",
  ResetOnMissionFailure: "without failing a mission",
  ResetOnAlarmRaised: "without raising any alarms",
  ResetOnAllyDowned: "without an ally becoming downed",
  ResetOnDisrupt: "without being disrupted by a Magnetic Status Effect",
  ResetOnGearAirSupport: "without using air support",
  ResetOnGearAmmoRestores: "without using ammo consumables",
  ResetOnGearCipher: "without using ciphers",
  ResetOnGearEnergyRestores: "without using energy consumables",
  ResetOnGearHealthRestores: "without using health consumables",
  ResetOnGearShieldRestores: "without using shield-restoring consumables",
  ResetOnProc: "without getting afflicted by a Status Effect",
  ResetOnNewDay: "in one day",
  EquippedDamageDebuffKey: "with an Extinguished Dragon Key equipped",
  EquippedHealthDebuffKey: "with a Bleeding Dragon Key equipped",
  EquippedShieldDebuffKey: "with a Decaying Dragon Key equipped",
  EquippedSpeedDebuffKey: "with a Hobbled Dragon Key equipped",
  SoloPlayer: ", while alone or in Solo Mode",
  PetPresent: "with an active pet present",
  SentinelPresent: "with an active sentinel present",
  Invisible: "while invisible",
  AimGliding: "during Aim Glide",
  Sliding: "while sliding",
  Undetected: "while undetected",
};
```

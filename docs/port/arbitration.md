# Port spec: Arbitration tracking, schedule, summary overlay (+ generic run tracker, Profit-Taker brief)

Source: WFHelper at `/home/claude/wfhelper-src`. All `file:line` refs are relative to that root.
Target: Decky plugin (Python backend running as root, X11 ARGB overlay on gamescope `:0`, React QAM + full-screen routes), Steam Deck LCD 1280x800.

**No screen capture or OCR is used anywhere in this feature.** Arbitration tracking reads EE.log only.
The overlay is a fixed-size panel, so the only geometry is panel placement (section 8). The feature has
four independent parts:

| Part | WFHelper files | Decky home |
|---|---|---|
| A. EE.log run parser (arbitration) | `services/arbiRunParser.ts` | Python backend, pure function module |
| B. Generic run tracker (capture, gzip, index, dedupe, salvage) | `services/runTracker.ts`, `services/runAnnotations.ts`, `services/arbiRunTracker.ts` | Python backend |
| C. EE.log importer (split a whole log into runs) | `services/eeLogSegmentImporter.ts`, `services/arbiLogImporter.ts` | Python backend, called from a route |
| D. Arbitration schedule + bell/favorite alerts | `services/arbiSchedule.ts`, `services/regionNames.ts`, `ipc/arbiScheduleIpc.ts`, `src/lib/world/arbiScheduleData.ts` | Python backend (fetch + timers), React view, Decky toasts |
| E. Post-run summary overlay | `config/shared/arbiSummary.ts`, `ipc/arbiOverlayIpc.ts`, `preload-arbi.ts`, `renderer/arbi-overlay.{html,js,css}` | X11 overlay drawn with Pillow |
| F. Analysis UI (run list, detail, charts, compare, spawn map) | `src/views/ArbiAnalyzeView.svelte`, `src/components/arbi/*`, `src/lib/arbi/*`, `config/shared/arbiMath.ts` | React full-screen route |

Profit-Taker (PT) uses the same B and C machinery with its own parser (section 12).

---

## 1. EE.log input

### 1.1 Locating EE.log on Linux
`services/eeLogPath.ts:12-54`. Steam Proton path:

```
<steamLibrary>/steamapps/compatdata/230410/pfx/drive_c/users/steamuser/AppData/Local/Warframe/EE.log
```

- Warframe Steam app id: `230410`.
- Steam roots checked (`eeLogPath.ts:15-26`): `~/.local/share/Steam`, `~/.steam/steam`, `~/.steam/root`, Flatpak `~/.var/app/com.valvesoftware.Steam/.local/share/Steam`, Snap `~/snap/steam/common/.local/share/Steam`.
- Extra libraries: regex over `<root>/steamapps/libraryfolders.vdf`: `/"path"\s+"([^"]*)"/g` (`eeLogPath.ts:29-37`). Unescape `\\\\` to `\\`.
- Override: env `WFHELPER_EE_LOG`.
- **Decky risk:** the backend runs as root, so `os.homedir()` would be `/root`. Use `decky.DECKY_USER_HOME` (normally `/home/deck`) for every `~` above. SD card libraries live under `/run/media/<label>/` and are listed in `libraryfolders.vdf`, so parse it.
- Windows-only: `%LOCALAPPDATA%\Warframe\EE.log` and the DBWIN real-time feed. Neither applies.

### 1.2 Tailing
`services/eeLogMonitor.ts:125-140, 280-325, 628-632, 780-805`.

| Constant | Value |
|---|---|
| `POLL_INTERVAL_MS` | 500 |
| `MAX_READ_BYTES` per read | 256 KiB |
| `MAX_READ_LOOPS_PER_TICK` | 8 (so at most 2 MiB per tick) |
| `TRUNCATION_CHECK_INTERVAL_MS` | 2000 |

Algorithm:
1. At start, `lastSize = stat(EE_LOG).size`. History is not replayed; only new bytes are read.
2. Every 500 ms, plus on inotify change (chokidar): if 2 s have passed since the last check and `size < lastSize`, the log was truncated (game restart). Close the fd, set `lastSize = 0`, clear the line remainder, and call `notifyEeLogReset()` on the arbi and PT trackers.
3. Read from offset `lastSize` in chunks. Decode as UTF-8. Prepend the carried remainder, split on `/\r?\n/`, and keep the last partial line as the new remainder.
4. Feed every complete line to `processArbiLine(line, "file")` (`eeLogMonitor.ts:513`) and `processProfitTakerLine`.
5. If the file is unlinked, do the same reset as step 2 (`eeLogMonitor.ts:790-799`).

The run trackers **ignore any line whose source is not `"file"`** (`runTracker.ts:450`). WFHelper also has a Proton debugstr real-time feed tagged `"dbwin"`, and the trackers ignore it. Decky only needs the file tail.

The EE.log file is written with lazy buffering, so a line can reach disk 10-26 s after the event. That is fine for run tracking. It only delays the summary overlay.

### 1.3 Line timestamp
`arbiRunParser.ts:16`:
```
EE_LOG_LINE_TS = /^[^\d]*(\d+\.\d+)/
```
The value is seconds since the game process started, as a float. A line without a match gets `ts = 0`. Many rules check `ts > 0`.
Example line: `16796.750 Script [Info]: ThemedSquadOverlay.lua: Mission name: Stöfler (Lua) - Arbitration`.

JS `line.match(re)` means "search anywhere" unless the pattern is anchored. In Python use `re.search`, not `re.match`.

---

## 2. Arbitration parser (`services/arbiRunParser.ts`)

### 2.1 Regex table (verbatim, line numbers in arbiRunParser.ts)

```text
16 EE_LOG_LINE_TS        ^[^\d]*(\d+\.\d+)
17 MISSION_NAME          Script \[Info\]: ThemedSquadOverlay\.lua: Mission name: (.*)
18 SPAM_SKIP             Game \[Warning\]:|DamagePct
19 AGENT_CREATED         OnAgentCreated
20 AGENT_FULL            OnAgentCreated.*?\/Npc\/(.+?)(\d+)\s+.*?MonitoredTicking\s+(\d+)
21 AGENT_NPC_NAME        \/Npc\/([A-Za-z0-9_]+)
22 AGENT_EXCLUDE (i)     (Replicant|RJCrew|petavatar|VoidClone|Turret|Dropship|CatbrowPetAgent|AllyAgent)
24 DRONE                 OnAgentCreated.*?CorpusEliteShieldDroneAgent
25 DEFENSE_REWARD        Sys \[Info\]: Created \/Lotus\/Interface\/DefenseReward\.swf
27 SURVIVAL_REWARD       Sys \[Info\]: Created \/Lotus\/Interface\/SurvivalReward\.swf
28 MONITORED_TICKING     AI \[Info\]: .*?MonitoredTicking (\d+)
29 WAVE_START_UNPAUSE    WaveDefend\.lua: Starting wave (\d+)
30 DEFENSE_WAVE          WaveDefend\.lua: Defense wave: (\d+)
31 SLEEP_BETWEEN         WaveDefend\.lua: _SleepBetweenWaves
32 SLEEP_BETWEEN_3       WaveDefend\.lua: _SleepBetweenWaves\(3\)
34 SPAWN_POINT           WaveDefend\.lua: Spawned a .+? spawn point: (\S+) @ Vector\((-?[\d.]+), (-?[\d.]+), (-?[\d.]+)\)
36 SPAWN_POINT_HINT      literal "spawn point:"   (substring pre-filter before SPAWN_POINT)
39 WAVE_COUNTDOWN        \/Lotus\/Interface\/ProjectionsCountdown\.swf
40 TERRITORY             Script \[Info\]: TerritoryMission\.lua
41 TERRITORY_START (i)   TerritoryMission\.lua: .*(control|captured)
44 PENDING_SECTOR_PLAIN  (?:ThemedSquadOverlay\.lua: Pending mission:|MapRedux\.lua: Confirm sector) (\S+)
46 PENDING_SECTOR_JSON   Set squad mission.*?"name":"([^"]+)"
47 ELITE_SECTOR          ^(SolNode\d+)_EliteAlert$
50 CLIENT_MISSION_JOIN   Client (?:joining mission in-progress|loaded)[^{]*\{"name":"([^"]+)"\}
52 CACHED_MISSION_NAME   ThemedSquadOverlay\.lua: Cached mission name=(.+) \((SolNode\d+)\)
55 SYNC_CONSUMABLES      SyncAutoPopulatedConsumables for mission (MT_[A-Z_]+) with location (\S+)
57 STATE_STARTED         Game \[Info\]: OnStateStarted, mission type=(MT_[A-Z_]+)
60 LOADOUT_LOADED        Game \[Info\]: (.+?) loadout loader finished
64 ABORT_CONFIRMED       TopMenu\.lua: Abort:
65 EOM_COMMIT            Sys \[Info\]: EOM missionLocationUnlocked=
76 DISRUPTION_ROUND_START SentientArtifactMission\.lua: Disruption: State change: ARTIFACT_ROUND$
78 DISRUPTION_ROUND_DONE  SentientArtifactMission\.lua: Disruption: State change: ARTIFACT_ROUND_DONE
```
`DISRUPTION_ROUND_START` ends with `$`. In Python, strip `\r` before matching, or use `\Z`. The splitter already removes `\r\n`.

Example lines (from `tests/fixtures/arbi/*.log`):
```
94.588 Script [Info]: MapRedux.lua: Confirm sector SolNode18_EliteAlert
94.588 Net [Info]: Set squad mission: {"name":"SolNode18_EliteAlert"}
99.488 Script [Info]: ThemedSquadOverlay.lua: Mission name: Rhea (Saturn) - Arbitration
102.433 Sys [Info]: SyncAutoPopulatedConsumables for mission MT_TERRITORY with location SolNode18
103.240 Game [Info]: OnStateStarted, mission type=MT_TERRITORY
16786.896 Script [Info]: WaveDefend.lua: Defense wave: 1
16790.100 AI [Info]: OnAgentCreated /Npc/ShieldLancer10 Live 5 Spawned 4 Ticking 5 Paused 0 IgnoredTicking 0 MonitoredTicking 5 AllyLive 4 AllyActive 4 NeutralActive 0
16808.117 AI [Info]: OnAgentCreated /Npc/CorpusEliteShieldDroneAgent67 Live 15 Spawned 53 ... MonitoredTicking 9 ...
16855.934 Sys [Info]: Created /Lotus/Interface/DefenseReward.swf
16783.893 Script [Info]: WaveDefend.lua: _SleepBetweenWaves(3)
133.194 Script [Info]: TerritoryMission.lua: Alpha has is now under Enemy control
19736.723 Sys [Info]: EOM missionLocationUnlocked=1
```
`MonitoredTicking N` on `OnAgentCreated` lines is the live-enemy count. It drives the saturation statistics.

### 2.2 Constants
```
MAX_SPAWN_POINTS = 300                (:38)
FORCED_VALID_AGENTS = {"CorpusEliteShieldDroneAgent"}  (:68)
REWARD_DEBOUNCE_SEC = 30              (:70)
MIRROR_DEFENSE_NODES = ["munio","tyana"]  (:72)  -> 2 waves/rotation
DISRUPTION_CONDUITS_PER_ROUND = 4     (:80)
SATURATION_BUCKET_WIDTH = 3           (:82)
SATURATION_MAX_COUNT = 30             (:83)  -> 10 buckets: 0-2,3-5,...,24-26,27+
SATURATION_MAX_SEGMENT_SEC = 29       (:85)
ARBI_EARLY_WAVE_CAP = 15              (config/shared/arbiTypes.ts:32)
ARBI_SPAWN_DATA_VERSION = 2           (arbiTypes.ts:35)
ARBI_SATURATION_THRESHOLD = 15        (config/shared/arbiMath.ts:7)
```

### 2.3 Parser state
The parser keeps state across runs:
- `pendingSector: str|None`. Holds the last seen sector, e.g. `"SolNode167_EliteAlert"`.
- `cachedMission: {name, solNode}|None`

Per-run `RunState` (`:152-187`): `missionName, node, missionType, missionTypeRaw, solNode, wavesPerRotation, runStartSec, missionStartSec, runEndSec, lastActivitySec, eventCount, hostTelemetry, players[], rotations, lastRewardSec(0), rewardTimestamps[], droneTimestamps[], spawnEvents[{name,tick}], tickSamples[{t,val}], pauseIntervals[{start,end}], currentPauseStart, spawnPoints: dict id -> {x,y,z,count,early[15]}, currentWave(1), waveStarts: dict wave->ts, waveEnds[], waveCountdowns[], roundStarts[], roundEnds[], preciseStartSec`.

Interface: `feed_line(line) -> event|None`, `is_run_active()`, `last_activity_sec()`, `activity_count()` (returns `eventCount`), `finalize() -> ParsedRun|None` (also clears the active run), `reset()` (clears the run, `pendingSector` and `cachedMission`).

Events: `{"type":"run-start", missionName, node, missionType, gameTimeSec}` or `{"type":"run-end", "reason": "mission-end"|"aborted"|"new-mission"}`.

### 2.4 `classifyMission(name)` (`:189-206`)
```
node = name.replace("Arbitration:", "", 1)   # first occurrence only (JS String.replace)
node = re.sub(r"\s+-\s+Arbitration$", "", node).strip()
lower = node.lower()
isMirror = any(m in lower for m in ["munio","tyana"])
missionType = "defense" if ("defense" in lower or isMirror) else "interception" if "interception" in lower else "other"
wavesPerRotation = 2 if isMirror else 3
```
Both name shapes occur: legacy `Arbitration: Casta (Ceres)` and current `Oestrus (Eris) - Arbitration`. Current names usually lack the mission type, so `missionType` normally comes from the `MT_` raw type.

`missionTypeFromRaw` (`:208-213`): `MT_DEFENSE` maps to defense, `MT_TERRITORY` to interception, `MT_ARTIFACT` to disruption, and anything else to "other".
`applyMissionTypeRaw` (`:215-222`) only applies **the first time** (when `missionTypeRaw is None`). It sets raw and type. For disruption it also sets `wavesPerRotation = 4`.

### 2.5 `startRun(name, ts)` (`:279-316`)
- `sector = ELITE_SECTOR.match(pendingSector)` if pendingSector, else None. Then set `pendingSector = None`, which consumes it.
- `solNode = sector[1] if sector else None`. `runStartSec = lastActivitySec = ts`. `currentWave = 1`. Everything else starts at 0 or empty.
- Return a run-start event.

### 2.6 `feedLine(line)`, applied in this exact order (`:318-503`)
1. If the line is empty or `SPAM_SKIP` matches, return None.
2. `ts` = first match of `EE_LOG_LINE_TS`, else 0.
3. If `PENDING_SECTOR_PLAIN` or (fallback) `PENDING_SECTOR_JSON` matches, set `pendingSector = m[1]`.
4. If `CACHED_MISSION_NAME` matches, set `cachedMission = {name: m[1].strip(), solNode: m[2]}`.
5. **Mid-mission join:** when **no run is active** and `CLIENT_MISSION_JOIN` matches, check `ELITE_SECTOR` against `m[1]`. If it matches:
   - set `pendingSector = m[1]`
   - `name = cachedMission.name if cachedMission and cachedMission.solNode == joined[1] else joined[1]`
   - return `startRun(name, ts)`.
6. **Mission name:** if `MISSION_NAME` matches, let `name = m[1].strip()` and `isArbi = "Arbitration" in name or (pendingSector and ELITE_SECTOR matches pendingSector)`.
   - No run active: return `startRun` if isArbi, else None.
   - A run is active and `isArbi and ts>0 and run.lastActivitySec>0 and ts < run.lastActivitySec`: return None. This is the host-migration replay guard.
   - Otherwise: if `ts>0`, set `run.runEndSec = ts`. Return `run-end` with reason `new-mission`. The tracker then re-feeds the same line, which can start the next run (section 3.3).
7. If no run is active, return None.
8. `ABORT_CONFIRMED`: set `runEndSec=ts` (if ts>0) and return run-end `aborted`. `EOM_COMMIT`: same, with reason `mission-end`.
9. `LOADOUT_LOADED`: append `m[1].strip()` to `players` if it is non-empty and not already present. The list includes the local player.
10. `SYNC_CONSUMABLES`: call `applyMissionTypeRaw(m[1])`. If `solNode is None`, set `solNode = m[2]`.
11. `STATE_STARTED`: call `applyMissionTypeRaw(m[1])`. If `missionStartSec is None and ts>0`, set `missionStartSec = ts`.
12. **Pause start:** if `SLEEP_BETWEEN` or `DEFENSE_REWARD` matches: `eventCount++`. If `currentPauseStart is None and ts>0`, set `currentPauseStart = ts`.
13. **Unpause:** set `isUnpause=False`.
    - `WAVE_START_UNPAUSE`: `eventCount++`. If `missionType=="defense"`, set `isUnpause=True`.
    - `TERRITORY`: `eventCount++`. If `missionTypeRaw is None and missionType=="other"`, set `missionType="interception"` and `wavesPerRotation=3`. Then set `isUnpause=True`.
    - If `isUnpause and currentPauseStart is not None and ts>0`, append `{start: currentPauseStart, end: ts}` to `pauseIntervals` and set `currentPauseStart=None`.
14. **Spawn point:** if `"spawn point:" in line` and `SPAWN_POINT` matches, record it (section 2.7).
15. `DEFENSE_WAVE` matches:
    - `eventCount++`.
    - If `missionTypeRaw is None`, set `missionType="defense"`.
    - `wave=int(m[1])`. If `wave>0`, set `currentWave=wave`.
    - If `ts>0`: set `waveStarts[wave]=ts` and `lastActivitySec=max(lastActivitySec,ts)`. If `preciseStartSec is None and wave==1`, set `preciseStartSec=ts`.

    **Else** if `preciseStartSec is None and ts>0 and TERRITORY_START` matches, set `preciseStartSec=ts`.
16. **Disruption** (`missionType=="disruption" and ts>0`):
    - `ROUND_DONE`: `eventCount++`, `rotations++`, append ts to `roundEnds` and `rewardTimestamps`, update `lastActivitySec`. If `currentPauseStart is None`, set it to ts.
    - Else `ROUND_START`: `eventCount++`, append ts to `roundStarts`, update `lastActivitySec`. If `preciseStartSec is None`, set it to ts. If a pause is open, close it at ts and append it.
17. **Rotation reward:** `isSurvivalReward = missionTypeRaw=="MT_SURVIVAL" and SURVIVAL_REWARD matches`. If true, `eventCount++`. If `isSurvivalReward or DEFENSE_REWARD` matches and `ts - lastRewardSec > 30`:
    - `rotations++`
    - set `lastRewardSec=ts` and append ts to `rewardTimestamps`
    - update `lastActivitySec`
    - if `currentPauseStart is None`, set it to ts.
18. **Tick sample:** if `missionType in {defense, interception, disruption}` and `ts>0` and `MONITORED_TICKING` matches: `eventCount++` and append `{t: ts, val: int(m[1])}` to `tickSamples`.
19. **Agents:**
    - If `DRONE` matches: `eventCount++`, `hostTelemetry=True`. If `ts>0`, append ts to `droneTimestamps` and update `lastActivitySec`.
    - Elif `AGENT_CREATED` matches: `eventCount++`, `hostTelemetry=True`. If `AGENT_EXCLUDE` does not match, try `AGENT_FULL`:
      - On a match, append `{name: m[1], tick: int(m[3])}`. The name has its trailing digits stripped, e.g. `ShieldLancer`. The tick is the MonitoredTicking value.
      - Otherwise append `{name: AGENT_NPC_NAME[1] or None, tick: None}`.
20. If `SLEEP_BETWEEN_3` matches and `ts>0`, append ts to `waveEnds`. If `WAVE_COUNTDOWN` matches and `ts>0`, `eventCount++` and append ts to `waveCountdowns`.
21. Return None.

Only the host logs `OnAgentCreated` and spawn points. On a client, `hostTelemetry` stays False and the run is **not saved** (section 3.2).

### 2.7 Spawn points (`:228-262`)
`recordSpawnPoint(r, m)`, with `id=m[1]` (e.g. `/Layer1/Layer1/NpcSpawnPoint37`):
- If the id is known: `count++`. If `1<=currentWave<=15`, `early[currentWave-1]++`.
- Else, if fewer than 300 points are stored, create `{x,y,z = round1(float(m[2..4])), count:1, early:[0]*15}` and tally the wave the same way. `round1(v) = round(v*10)/10` in JS rounding, which rounds half up. Python's `round` uses banker's rounding, so use `math.floor(v*10+0.5)/10`.

Output (`buildSpawnPoints`): a list of `{id,x,y,z,count, early?}`. `early` is included only when some value is greater than 0. Sort by count descending, then by id ascending.

`countFromWave(point, minWave)` (`arbiTypes.ts:54-60`): if there is no `early` or `minWave<=1`, return count. Otherwise subtract `early[0 .. min(minWave-1,15)-1]` from count, floored at 0.

### 2.8 `finalize()` (`:659-736`)
```
drones = len(droneTimestamps)
totalEnemies = countValidEnemies(spawnEvents) + drones
startSec = preciseStartSec ?? droneTimestamps[0] ?? missionStartSec ?? runStartSec
durationSec = max(0, lastActivitySec - startSec)
if durationSec < 1 and runEndSec is not None: durationSec = max(0, runEndSec - startSec)
```
`countValidEnemies` (`:505-527`):
- Let `named` be the spawn events with a non-null name.
- For each consecutive pair `(prev, curr)` in `named` where both ticks are non-null: if `curr.tick > prev.tick`, add `prev.name` to `confirmed`. Otherwise add it to `suspected`.
- `nonTicking = suspected - confirmed - FORCED_VALID_AGENTS`.
- `valid` = the number of events whose name is null or not in `nonTicking`.

Full stats are built only when `missionType in {defense, interception, disruption}`. Otherwise `stats = None`.
- `avgDroneIntervalSec`: the mean of consecutive drone-timestamp gaps when there are more than 1 drones, else None.
- `model = computeVitusModel(rotations, wavesPerRotation, drones)` (section 2.9).
- `pauses = pauseIntervals + [{currentPauseStart, lastActivitySec}]` if a pause is still open.
- `stats` fields:
  - `killsPerDrone = totalEnemies/drones` if drones else 0
  - `avgDroneIntervalSec`
  - `expectedVitusMean`, `expectedVitusStd`
  - `vitusPerMin = mean/(durationSec/60)` if durationSec>0 else 0
  - `wavesPerRotation`, `droneTimestamps`, `rewardTimestamps`, `preciseStartSec`, `lastActivitySec`
  - `saturationBuckets = buildSaturation(startSec, lastActivitySec)`
  - `waves = buildWaves()` for defense, `buildRounds()` for disruption, else None
  - `pauseIntervals = mergeIntervals(pauses, {startSec, lastActivitySec})`
  - `idleIntervals = buildIdleIntervals(startSec, lastActivitySec)`
  - `rotationSaturationPct = buildRotationSaturation(startSec)`
  - `spawnPoints`
  - `spawnDataVersion = 2`

Returned `ParsedRun`: `missionName, node, missionType, missionTypeRaw, solNode, runStartSec, runEndSec, lastActivitySec, durationSec, rotations, drones, totalEnemies, hostTelemetry, players, stats`.

**Saturation segments** (`eachSaturationSegment`, `:531-554`):
- For each consecutive sample pair `(cur, next)`, take the segment `[max(cur.t,start), min(next.t,end)]`.
- Skip it if `dur<=0` or `dur>29`.
- Skip it if any raw (unmerged) pause `p` satisfies `(segStart < p.start and segEnd > p.end) or (p.start <= segStart < p.end)`.
- Otherwise visit `(cur.val, dur)`.

Derived values:
- `saturationPctIn(s,e)`: 100 * (time with `val >= 15`) / total time, or 0 when the total is 0.
- `buildSaturation`: 10 buckets, `idx = min(val // 3, 9)`. Each bucket is `{minCount: i*3, label: "27+" if last else f"{lo}-{lo+2}", seconds, pct}`.
- `buildRotationSaturation`: the window runs from `startSec` to `reward[0]`, then from each reward to the next. Produces one pct per reward.
- `buildIdleIntervals`: gaps between consecutive samples longer than 29 s, merged and clamped to the run window.

**Waves** (`buildWaves`, `:609-639`). Iterate the `waveStarts` keys in sorted order:
- For waves where `wave % 3 != 0`: use the first `waveEnds` value greater than the start, consumed in order. `dur = end - start`. If none remain, skip the wave.
- For waves where `wave % 3 == 0`: use the first `waveCountdowns` value greater than the start. `dur = countdown - 5 - start`. If none remain, skip the wave.
- Emit `{index: wave, durationSec: dur, saturationPct: saturationPctIn(start, start+dur)}`.

**Rounds** (disruption, `:642-657`): pair each `roundStarts[i]` with the next `roundEnds` value greater than it. Emit `{index:i+1, durationSec, saturationPct}`. Stop when no ends remain.

### 2.9 Vitus model (`config/shared/arbiMath.ts:10-41`). This is a port of svesk.github.io/arbi.
```
VITUS_DROP_CHANCE=0.15  VITUS_RETRIEVER_CHANCE=0.18  ROTATION_VITUS_CHANCE=0.1
meanVal = 4*0.18 + 2*0.82                      # 2.36
expectValSq = 16*0.18 + 4*0.82                 # 6.16
varVal = expectValSq - meanVal^2
rotMean = R + R*0.1*W
rotVar  = R*0.1*0.9*W^2
meanDrops = D*0.15 ; varDrops = D*0.15*0.85
mean = rotMean + meanDrops*meanVal
std  = sqrt(max(0, rotVar + meanDrops*varVal + meanVal^2*varDrops))
```
R = rotations, W = wavesPerRotation, D = drones.

Scenario table (`arbiMath.ts:43-69`): for each row, `total = max(0, round(mean + z*std))`.
```
z=-2.326 "99%" worstCase | -1.282 "90%" unlucky | -0.674 "75%" belowAvg | 0 "50%" average
 0.674 "25%" aboveAvg | 1.282 "10%" highRoll | 2.326 "1%" godRoll
```
`normCdf` (`arbiMath.ts:93-102`) uses the Abramowitz-Stegun approximation with constants `0.2316419, 0.3989423, 0.3193815, -0.3565638, 1.781478, -1.821256, 1.330274`. In Python you can use `statistics.NormalDist`, but use the AS form if results must match WFHelper exactly.

Luck label for an entered actual vitus (`src/components/arbi/ArbiVitusPanel.svelte:37-70`). `p = normCdf(actual)`:

| Condition | Label | Color |
|---|---|---|
| p ≥ .99 | godRoll | #ffd700 |
| p ≥ .9 | highRoll | #00e676 |
| p ≥ .75 | aboveAvg | #b2ff59 |
| p > .25 | average | #ccc |
| p > .1 | belowAvg | #ffcc80 |
| p > .01 | unlucky | #ff9100 |
| otherwise | worstCase | #ff5252 |

Text: "top X%" if p>.5 (with X = 100-100p), else "bottom 100p%".

`mergeIntervals(list, bounds?)` (`arbiMath.ts:73-90`): clamp each interval to bounds, drop those with `end<=start`, sort by start, and merge intervals that overlap **or touch** (`start <= last.end`).

---

## 3. Generic run tracker (`services/runTracker.ts`)

The arbi and PT trackers both use it.

### 3.1 Constants
```
INDEX_SCHEMA_VERSION = 1
FLUSH_EVERY_LINES = 200
INACTIVITY_TIMEOUT_MS = 600_000 (10 min); INACTIVITY_CHECK_MS = 60_000
RUN_ID_RE = ^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(?:-\d+)?$
DUPLICATE_START_TOLERANCE_MS = 120_000 ; DUPLICATE_DURATION_TOLERANCE_SEC = 5
```

### 3.2 Files
All files live under Electron `userData`. On Linux that is `~/.config/WFHelper/` (`config/runtime/appIdentity.ts:38-39`, productName `WFHelper`). For Decky, use `decky.DECKY_PLUGIN_RUNTIME_DIR` (or `SETTINGS_DIR`) and chown to the deck user if the UI must read them.
- `arbi-runs.json`: `{"schemaVersion":1,"runs":[ArbiRunRecord, ...]}`, newest first (records are prepended). Written atomically: write a tmp file, fsync, then rename (`services/atomicFile.ts`).
- `arbi-logs/<id>.partial.log`: the live capture, plain text. 200 lines are appended per flush.
- `arbi-logs/<id>.log.gz`: the finished capture, gzipped.
- PT uses `pt-runs.json` and `pt-logs/`.
- Run id: local wall clock `YYYY-MM-DD_HH-mm-ss`. On collision with an existing or reserved id, append `-2`, `-3` and so on (`:172-187`).
- `_storedLogPath`: return a path only if the id matches `RUN_ID_RE`, `logFile == f"{id}.log.gz"`, and the resolved path's directory is the logs dir. This is a path-traversal guard.

### 3.3 `processLine(line, source)` (`:448-477`)
- Ignore the line unless tracking is enabled, `source == "file"` and the tracker is initialized.
- `ev = parser.feed_line(line)`.
- `run-start`: `_startCapture(ev.gameTimeSec, line)`. This sets a new id, `startedAt=now`, `capturedAtSec=gameTimeSec`, `pendingLines=[line]`, `lastCombatAt=now`, and starts the inactivity timer.
- `run-end`: `_finalizeRun(reason, sync=False)`. Then **feed the same line again**. If that returns run-start, start a new capture. This handles back-to-back missions.
- Otherwise, if capturing: append to `pendingLines`. If `parser.activity_count()` changed, set `lastCombatAt=now`. If 200 lines are pending, flush them.
- The inactivity timer runs every 60 s. If `now - lastCombatAt > 10 min`, finalize with reason `"inactivity"`. Orbiter lines do not bump `eventCount`, so they do not keep a run alive.

### 3.4 `_finalizeRun(reason, sync)` (`:369-425`)
1. `parsed = parser.finalize()`. If it is None, release the id.
2. Arbi `skipReason` (`arbiRunTracker.ts:26-30`):
   - `"client-side run, no AI data in the log"` if not `hostTelemetry`
   - `"<n> rotation(s)"` if `rotations < 2`
   - otherwise None.

   If there is a skip reason, delete the partial file and save nothing.
3. Flush pending lines. `startedAt = capture.startedAt + (parsed.runStartSec - capturedAtSec)*1000`.
4. Gzip the partial file to `<id>.log.gz` and delete the partial. The record is built after the gzip; `logSizeBytes` is the gz size, or 0 on failure. App quit uses a synchronous gzip.
5. `_addRecord`: prepend the record, recompute duplicates, save the index, then call `onRunSaved(record)`. For arbi, that callback broadcasts `arbi-run-saved` to the UI and calls `maybeShowArbiSummary` (`main.ts:696-699`).

Arbi `buildRecord` (`arbiRunTracker.ts:32-56`):
```
endSec = parsed.runEndSec ?? parsed.lastActivitySec
endedAt = startedAt + max(0,(endSec - runStartSec)*1000)
logFile = f"{id}.log.gz" if size>0 else None ; source="live" ; vitusActual=None
other fields copied from parsed (missionName,node,missionType,missionTypeRaw,solNode,durationSec,rotations,drones,totalEnemies,stats,players)
```

Other end reasons:
- `notifyEeLogReset`: finalize with `"log-truncated"`.
- `shutdown`: finalize with `"app-quit"`, synchronously.
- `setTrackingEnabled(False)`: discard the active capture and delete its partial file.

### 3.5 Duplicates (`:250-305`)
Two records are the same run when:
- their sources differ (live vs imported; two live runs are never paired),
- `|startedAt diff| <= 120 s`,
- `|durationSec diff| <= 5`, and
- `sameTarget(a,b)` holds (arbi: same `node`; PT: always true).

To pick the winner of a pair, compare in order:
1. `richness`, higher wins (arbi: 1 if stats is not None, else 0).
2. `logSizeBytes`, larger wins.
3. Prefer the `live` record.

The loser gets `duplicateOf = winnerId`. Chains are resolved up to depth 4. The winner adopts the loser's `notes` and `tags` when it has none. For arbi it also adopts `vitusActual` when its own is None. Duplicates are recomputed after every add and delete and on init.

### 3.6 Init and salvage (`:522-563`)
On init:
1. Load the index. Drop entries whose id fails `RUN_ID_RE`. Records whose log file is missing get `logFile=None, logSizeBytes=0`. Re-normalize tags and notes.
2. If the JSON is corrupt, quarantine the file and start empty.
3. Mark duplicates.
4. Salvage every `*.partial.log` left by a crash:
   - Reparse the whole file.
   - If it should be skipped, delete it.
   - Otherwise `startedAt = mtime - (endSec-runStartSec)*1000`, gzip it, and add it with reason `"log-truncated"`.

### 3.7 Mutations
- `setRunTags`, `setRunNotes` normalize first (section 3.8).
- `setRunVitus(id, int|None)`. The IPC layer validates `0 <= v <= 10_000_000`, rounds it, and returns None on bad input (`ipc/arbiIpc.ts:14, 56-69`).
- `deleteRunLog`: unlink the gz and set `logFile=None, logSizeBytes=0`.
- `deleteRun`: unlink the gz, remove the record and recompute duplicates.
- `getDiskUsageBytes`: the sum of `*.log.gz` sizes in the logs dir.

### 3.8 `runAnnotations.ts` (whole file)
```
MAX_TAGS=12 MAX_TAG_LEN=32 MAX_NOTES_LEN=2000
tags: keep only strings; trim; collapse \s+ to " "; cut to 32; trim; drop empty;
      dedupe case-insensitively (keep the first spelling); stop at 12.
notes: non-string -> ""; normalize \r\n and \r to \n; drop code points <32 except \t and \n, and drop 127;
       stop before exceeding 2000 chars (UTF-16 length in JS); trim.
```
An empty result deletes the field from the record.

### 3.9 Backfills (`arbiRunTracker.ts:109-190`)
These run once at init, in a background chain.
- **Players:** for records with `players` undefined, reparse the gz and take only `players`. Records with no log get `[]`.
- **Cadence:** for records with stats and a stored log, where any of these is missing (`pauseIntervals`, `rotationSaturationPct`, or `spawnDataVersion != 2` while there are spawn points), reparse at most 200 records, newest first. Copy only `pauseIntervals`, `idleIntervals`, `rotationSaturationPct`, `spawnPoints` and `spawnDataVersion`, plus `waves` if the wave count is unchanged.

A fresh Decky install has no old records, so both backfills can be skipped. They matter only when importing a WFHelper index.

---

## 4. EE.log importer (`services/eeLogSegmentImporter.ts`, `services/arbiLogImporter.ts`)

This feature splits a whole EE.log (for example the current or a previous `EE.log`) into runs.
- Header regex (`:12`): `Sys \[Diag\]: Current time: (.+?)(?: \[UTC: (.+?)\])?\s*$`. Example: `0.234 Sys [Diag]: Current time: Fri Jul 04 12:34:56 2026 [UTC: Fri Jul 04 10:34:56 2026]`.
- `gameTimeZeroMs = wall - ts*1000`. Use the UTC group parsed as `"<str> GMT"` when present, else the local string. In Python: `datetime.strptime(s, "%a %b %d %H:%M:%S %Y")` with UTC tzinfo.
- If there is no header: `startedAt = file_mtime - max(0, lastTs - runStartSec)*1000`.
- `MAX_SEGMENT_BYTES = 256 MiB` per run. Beyond that the segment is truncated.
- Lines are fed with the same start, end and re-feed logic as the live tracker. Every line is captured while a run is active. A run still open at EOF finalizes with reason `"imported"`.
- `addImportedRunFromFile`:
  - Apply the skip rules.
  - `id = format(startedAt)`, with no suffix. If the id already exists, skip the run.
  - Gzip the segment into the logs dir and set `source="imported"`.
- Returns `{imported: [records], skipped: n}`.

In Decky, start an import from the UI with a path picker (or "import current EE.log") rather than a native dialog.

---

## 5. Arbitration schedule (`services/arbiSchedule.ts`)

### 5.1 Source
- `GET https://browse.wf/arbys.txt` (`:23`). No custom headers. Timeout 15 s (`:24`). Non-2xx counts as failure. A failure keeps the previous or disk-cached data.
- Format: plain text, one `epochSeconds,NodeId` per line. Example from `tests/main/arbiSchedule.test.ts:77`:
  ```
  1751980800,SolNode149
  1751984400,SolNode167
  ```
  Entries are hourly; each arbitration lasts 1 h.
- Parse (`:65-79`): split on `\n` and trim. Split on `,` and use fields 0 and 1. `epoch = float(f0)` must be finite and greater than 0. `nodeId = f1.strip()` must match `^[A-Za-z0-9_./:-]{2,120}$`. Skip any line that fails. If no rows are valid, the fetch counts as failed.
- Response shape: `{epochMs: epoch*1000, nodeId, node, mission, faction}` (`:81-95`).

### 5.2 Labels (`services/regionNames.ts`)
Labels come from the bundled `warframe-public-export-plus` package: `ExportRegions.json` and `dict.en.json`. The Decky plugin must bundle `ExportRegions.json` plus the `dict.en` keys it uses. Alternatively, ship a precomputed `{nodeId: {node, mission, faction}}` map built at package time, which is much smaller.
- `nodeLabel`: `"<dict[region.name]> (<dict[region.systemName]>)"`. Use the raw nodeId if the region is unknown, and `"Unknown"` if the id is empty (`:101-107`).
- `missionLabel`: `titleCase(dict[region.missionName])`. Else, if `region.missionType` starts with `MT_`, `titleCase(mt[3:].replace("_"," "))`. Else `"Unknown"`.
- `titleCase(s) = s.lower()`, then uppercase each character that follows the start, whitespace or `-` (`config/shared/textNormalize.ts:3`).
- `resolveDict`: a value that does not start with `/` passes through unchanged.
- Faction table (`:14-21`): `FC_GRINEER`→Grineer, `FC_CORPUS`→Corpus, `FC_INFESTATION`→Infested, `FC_OROKIN`→Corrupted, `FC_SENTIENT`→Sentient, `FC_NARMER`→Narmer. Any other `FC_X` → `titleCase(X with _→space)`. Otherwise "Unknown".

### 5.3 Caching and timers
```
REFRESH_INTERVAL_MS = 3_600_000   (hourly background fetch)
STALE_AFTER_MS      = 3_300_000   (GET re-fetches if fetchedAt older than 55 min)
FIRST_FETCH_DELAY_MS= 5_000
ALERT_CHECK_MS      = 30_000
PAST_WINDOW_MS      = 3_600_000   (keep the still-active hour)
YEAR_MS             = 365 days    (keep at most a year ahead)
FIRED_RETENTION_MS  = 7_200_000
MIN/MAX/DEFAULT_LEAD_MINUTES = 1 / 120 / 5
```
- Cache file `arbi-sched-cache.json`: `{"fetchedAt": <ms>, "raw": "<arbys.txt text>"}`. It is reparsed on load.
- Concurrent fetches share one in-flight promise.
- `getSchedulePayload()`: fetch first if stale, then return `{entries: window-filtered (epochMs in [now-1h, now+365d]), ascending, fetchedAt, alerts}`.
- The source has no rate limit header. Fetch at most once per 55 min plus on explicit refresh. The file is static future data.

### 5.4 Alerts (favorites + one-shot bells)
Store file `arbi-sched-alerts.json` (pretty JSON, atomic write):
```json
{"schemaVersion":1,"occurrences":["1751980800000:SolNode149"],"favoriteNodes":["SolNode167"],"minutesBefore":5,"firedKeys":[]}
```
- Occurrence key: `f"{epochMs}:{nodeId}"`. Validated with `^\d{10,17}:[A-Za-z0-9_./:-]{2,120}$`.
- Sanitize on load:
  - keep only valid, deduped strings
  - `minutesBefore` must be an integer in 1..120 (floored), else 5.
- `computeDueAlerts` (`:108-127`):
  - `lead = minutesBefore*60000`.
  - An entry is due when `epochMs - lead <= now < epochMs`, its key is not in `firedKeys`, and either the key is in `occurrences` or `nodeId` is in `favoriteNodes`.
- `_checkAlerts` runs every 30 s (`:308-335`):
  1. Prune `occurrences` and `firedKeys` older than now-2h.
  2. If notifications are disabled, return. Bells are not consumed, so they fire later or get pruned.
  3. For each due entry: add the key to `firedKeys` and remove it from `occurrences` (bells are one-shot; favorites keep firing on later occurrences).
  4. Notify with title `"Arbitration Alert"` and body `f"{mission} on {node} vs {faction} - {timing}."`, where `timing = f"starts in ~{m} min"` if `m = round((epochMs-now)/60000) > 0`, else `"starting now"`.
  5. Save the store.
- Setters return the new `alerts` object `{occurrences, favoriteNodes, minutesBefore}`, or None on invalid input:
  - `setOccurrenceAlert(key, enabled)`
  - `setFavoriteNode(nodeId, enabled)`
  - `setLeadMinutes(n)`. The UI clamps to 1..120 (`ArbiSchedule.svelte:228-232`).
- Delivery (`ipc/arbiScheduleIpc.ts`): alerts fire only when some channel can deliver them (native toggle or webhook). In Decky, send them as `toaster.toast` from the frontend over a backend→frontend event. Because the backend fires while the QAM may be closed, the plugin's frontend `definePlugin` must keep an event listener registered globally. Optionally add a Steam notification sound.

### 5.5 Schedule UI (`src/components/world/ArbiSchedule.svelte`, `src/lib/world/arbiScheduleData.ts`)
- Days-ahead options: `[7, 14, 30, 60]`, default 30.
- Node filter: a multi-select of unique nodes sorted by label. Saved presets `{name, nodeIds[], updatedAt}`.
- Search:
  - Lowercase the query and split it on `[\s,]+`.
  - A token "matches" if some node label contains it.
  - An entry passes if any matched token is in its node label, or if the full query is a substring of the node, mission or faction.
  - Report unmatched tokens back to the user.
- Grouping and display:
  - Group entries by local day; label format is weekday long, day numeric, month short.
  - Time is `HH:MM`.
  - Countdown is `"NOW"` once `diff <= 0`, `"{d}d {hh}h {mm}m"` when at least a day remains, else `"{h}h {mm}m {ss}s"`.
- Faction badge key: `grineer` / `corpus` / `infest*` → infested / `corrupt*` or `orokin` → corrupted / other.
- Per-viewer prefs (localStorage keys `arbi-sched-selected`, `arbi-sched-presets`, `arbi-sched-days`). On Decky, use plugin settings or frontend localStorage. CEF localStorage persists.
- "Copy as image" builds a branded PNG card with at most 80 rows and puts it on the clipboard. This is optional and low value on a Deck; drop it or replace it with a text list.

---

## 6. Records and payload shapes (Python backend ↔ frontend API)

`ArbiRunRecord` (`config/shared/arbiTypes.ts:99-130`). Example:
```json
{
  "id": "2026-07-04_21-30-15", "startedAt": 1783193415000, "endedAt": 1783195257000,
  "missionName": "Casta (Ceres) - Arbitration", "node": "Casta (Ceres)",
  "missionType": "defense", "missionTypeRaw": "MT_DEFENSE", "solNode": "SolNode149",
  "durationSec": 1842.3, "rotations": 6, "drones": 184, "totalEnemies": 4238,
  "vitusActual": null, "logFile": "2026-07-04_21-30-15.log.gz", "logSizeBytes": 412345,
  "endReason": "mission-end", "source": "live",
  "stats": { "killsPerDrone": 23.03, "avgDroneIntervalSec": 9.8, "expectedVitusMean": 48.5,
    "expectedVitusStd": 6.2, "vitusPerMin": 1.58, "wavesPerRotation": 3,
    "droneTimestamps": [16808.117], "rewardTimestamps": [16855.934],
    "preciseStartSec": 16786.896, "lastActivitySec": 18629.2,
    "saturationBuckets": [{"minCount":0,"label":"0-2","seconds":12.1,"pct":0.8}],
    "waves": [{"index":1,"durationSec":19.2,"saturationPct":0}],
    "pauseIntervals": [{"start":16855.9,"end":16860.7}], "idleIntervals": [],
    "rotationSaturationPct": [41.2],
    "spawnPoints": [{"id":"/Layer1/Layer1/NpcSpawnPoint37","x":-29.8,"y":4.2,"z":44.5,"count":31,"early":[3,2,0,0,0,0,0,0,0,0,0,0,0,0,0]}],
    "spawnDataVersion": 2 },
  "players": ["Tenno","Volt"], "tags": ["boar run"], "notes": "...", "duplicateOf": "..."
}
```
`endReason` is one of `mission-end | aborted | new-mission | log-truncated | app-quit | inactivity | imported`.

Run arrays can be large: thousands of drone timestamps and up to 300 spawn points per run. Send the list view a slim projection without `stats`, and fetch `stats` per run on demand. Decky `callable` passes JSON over a websocket, so avoid sending megabytes at once.

Suggested backend callables, mapped from the IPC channels in `config/shared/ipcChannels.ts:190-224`:

| WFHelper channel | Args | Returns |
|---|---|---|
| `arbi:get-runs` | — | `{runs, diskUsageBytes}` |
| `arbi:refresh-runs` | — | Forces an EE.log poll, waits up to 5 s (`REFRESH_WAIT_MS`, `ipc/runTrackerIpc.ts:14`) for pending gzips, returns the same shape |
| `arbi:set-vitus` | `id, int\|null` | record or null |
| `arbi:set-tags` | `id, string[]` | record or null |
| `arbi:set-notes` | `id, string` | record or null |
| `arbi:delete-run` | `id` | `{ok}` |
| `arbi:delete-log` | `id` | record or null |
| `arbi:export-log` | `id` | Gunzips to `<Node_Name>_<id>.log`; name = node with `[^a-zA-Z0-9-_ ]` removed and spaces→`_` (`ipc/arbiIpc.ts:17-21`). On Deck, write to `~/Downloads` or skip. |
| `arbi:import-log` | (path) | `{imported, skipped}` |
| `arbi:save-image` | — | Desktop PNG export; drop |
| event `arbi-run-saved` | record | push to UI |
| `arbi-sched:get` | — | `{entries, fetchedAt, alerts}` |
| `arbi-sched:set-occurrence` | `key, bool` | alerts or null |
| `arbi-sched:set-favorite` | `nodeId, bool` | alerts or null |
| `arbi-sched:set-lead` | `minutes` | alerts or null |

Run ids passed in from the UI must be strings of length 1..64 (`asRunId`, `runTrackerIpc.ts:16-18`).

Settings (`config/runtime/overlaySettings.ts:93-94, 179-180`):
- `arbiTrackingEnabled` (default true). It gates **both** the arbi and PT trackers (`main.ts:503-506`).
- `arbiSummaryOverlayEnabled` (default true).
- `worldNotificationsEnabled` (default true) gates schedule alerts.

---

## 7. Analysis UI (Arbitrations view)

The view is `src/views/ArbiAnalyzeView.svelte`. It has two tabs, "arbitrations" and "profitTaker" (persisted under localStorage key `arbi-tab`).

**List filters** (`:146-157`):
- mission type: `all | defense | interception | disruption | other`
- min actual vitus (excludes runs with no actual vitus)
- tag (case-insensitive)
- min rotations
- min duration in minutes
- source: `all | live | imported`
- show duplicates (hidden by default)

List columns: date (`DD.MM.YYYY HH:MM`), node, type, duration, rotations, drones, vitus. The list also supports multi-select for mass delete, mass add-tag and compare (2..4 runs, `ARBI_COMPARE_MAX = 4`). It shows a notice when tracking is disabled.

**Type label** (`src/lib/arbi/arbiChartData.ts:7-21`): when `missionType=="other"`, map the raw type:
```
MT_SURVIVAL: "Survival", MT_EXCAVATE: "Excavation", MT_EVACUATION: "Defection", MT_PURIFY: "Infested Salvage", MT_ALCHEMY: "Alchemy"
```
Any other raw type shows as the raw string with `MT_` stripped.

**Run detail** (`src/components/arbi/ArbiRunDetail.svelte:420-437`). Panels in order:
1. KPI strip.
2. Personal best: rank among usable runs with the same node and mission type, for `dronesPerMin` and `expectedVitusPerMin`. Uses `runPersonalBest` in `src/lib/runPersonalBest.ts`.
3. Vitus panel: scenario table, an input for actual vitus, and the luck label.
4. Saturation histogram.
5. DPM chart: drones per minute per rotation.
6. Rotation grid.
7. If `hasCadenceData`:
   - timeline (active / dry / reward / gap segments)
   - drone-wait histogram
   - clear map (waves or rotations)
8. Spawn map.
9. Tags and notes editors, plus prev/next navigation.

**Formulas** (`src/lib/arbi/*`). All are pure and port directly to TypeScript:
- `formatDuration`: `"{h}h {m}m {s}s"` or `"{m}m {s}s"`. `formatClock`: `m:ss`.
- `dronesPerRotation`: drones with `t <= reward[i]`, counted per window.
- `rotationWindows`:
  - The first start is `preciseStartSec ?? droneTimestamps[0] ?? lastActivitySec - nRewards*300`.
  - If that is at or after `reward[0]`, use `droneTimestamps[0] ?? reward[0]-300` instead.
  - Each later window starts at the previous reward.
- `dpmSeries`: `count / (max(window,10)/60)`.
- `rotationClearCells`: window length minus overlap with pauses.
- `saturationAboveThresholdPct(buckets,15)`.
- `thresholdHue(pct) = max(0, 120 - clamp(pct)/18*120)`.
- `relativePerformanceHue(v,min,max) = (v-min)/(max-min or 1)*120`.
- `bucketSuccessMixPct(i) = max(0,100-15i)`.
- `densestWindow(sorted, w)`: two-pointer scan for the most values in a half-open window `[v, v+w)`; the earliest window wins ties.
- **Cadence** (`arbiCadence.ts`): `MIN_DRY_SEC=45`, `DRY_MEDIAN_FACTOR=3`, `BUSIEST_WINDOW_SEC=60`.
  - `dryThreshold = max(45, 3*median(drone gaps))`.
  - `reward` = merged pauses. `gap` = merged idle intervals minus reward.
  - The free time between consecutive marks `[start, drones..., end]` (with the explained time removed) is "dry" if its total exceeds the threshold, else "active".
  - Adjacent segments of the same kind merge when they are less than 0.001 s apart.
  - Each drone is counted into its segment.
  - `activeShare` = active time / window.
- **Drone wait buckets** (`arbiDroneCadence.ts`): edges `[1,2,3,5,8,12]` plus `12s+`.
  - `wait = gap - overlap(pause∪idle)`.
  - Weight by seconds.
  - `ARBI_DRY_WAIT_SEC=12`.
  - Peak = densest 10 s window.
- **Compare metrics** (`arbiCompare.ts:38-67`). Each entry is `key: better, format`:
  - `durationSec`: high, duration
  - `rotations`: high, int
  - `drones`: high, int
  - `totalEnemies`: high, int
  - `killsPerDrone`: low, 2dp
  - `avgDroneIntervalSec`: low, seconds
  - `dronesPerMin`: high, 2dp
  - `enemiesPerMin`: high, 1dp
  - `dronesPerRotation`: high, 1dp
  - `expectedVitus`: high, int
  - `vitusActual`: high, int
  - `vitusPerMin`: high, 2dp. Uses actual vitus if set, else the model value.
  - `saturationPct`: high, percent

  "Usable" runs exclude `endReason=="log-truncated"` and duplicates. Average scopes are filtered / same missionType / same node / same squad size. The best cell is flagged only when more than one value is present.
- **Spawn map, plain** (`arbiSpawnMap.ts`):
  - Project to the x/z plane in a 100x100 view with 8 padding and uniform scale.
  - Radius `max(1.4, 4.5*sqrt(count/max))`. Hue from relativePerformanceHue (all 120 when counts are equal).
  - Top 10 list. Cold points are those with `count <= 2`.
  - Label is the trailing digits of the id.
- **Spawn map on the tile minimap** (`arbiMinimap.ts`). Optional and heavy.
  - Uses `src/data/arbiMinimaps.json` (210 KB, from arbi.guide by remesis; credit required): 24 layouts and 43 node mappings.
  - Images are at `https://assets.wfhelper.com/arbi-minimaps/<src>.webp`, and file names must match `^[A-Za-z0-9][A-Za-z0-9+~._-]*\.webp$`.
  - Alignment:
    - Try 8 planar transforms (x/z swaps and sign flips).
    - Find offset candidates by binned voting: `BIN=0.05`, up to 64 sampled points, top 24 offsets.
    - Match points greedily to the nearest unused reference within `TOLERANCE=0.25`, using a grid hash.
    - Pick the alignment with the most matches, then the least error.
    - Require at least 12 matched points and score `matched / min(points, refs) >= 0.35`.
  - Pixel position: `px = a*X+b*Z+c`, `py = d*X+e*Z+f` from the layout's `matrix`.
  - Elevation ring: 5 bands at the 20/40/60/80% quantiles of reference y. Colors `#0b4399 #1768c5 #2d91eb #67b7f5 #b9ddff`.
  - Floor filter: `minWave` uses `countFromWave` and then re-matches; otherwise filter by `minY`/`maxY`.
  - Candidate layouts come from `nodes[solNode]`, else from node-name token overlap.
  - Memoize per run, because a big tile costs hundreds of ms.
  - On the Deck CPU, run it in the worker process (Python) or lazily in the frontend. It can be deferred.

**Deck UI note:** 1280x800 in a full-screen route leaves about 1280x740 of usable area. Use a single-column layout with focusable rows (`Focusable`/`PanelSection`). Charts can be simple SVG. Text inputs (vitus, tags, notes) need the on-screen keyboard; `TextField` in @decky/ui triggers it.

---

## 8. Post-run summary overlay

**Trigger:** `onRunSaved`, then `maybeShowArbiSummary(run)` (`ipc/arbiOverlayIpc.ts:92-103`):
1. Return if `arbiSummaryOverlayEnabled` is off.
2. Build the payload with `buildArbiSummaryPayload(run)` (`config/shared/arbiSummary.ts:33-77`). It returns None unless:
   - `source=="live"`,
   - `rotations >= 2`,
   - `endReason in {mission-end, aborted}`, and
   - stats exist.
3. Show the overlay, send the payload, and **auto-hide after 60 s** (`AUTO_HIDE_MS = 60_000`). The overlay also hides whenever Warframe loses focus (zOrder subscriber), which is irrelevant under gamescope.

Payload:
```
id, node, missionType, missionTypeRaw, durationSec, rotations, drones, totalEnemies,
expectedVitusMean, expectedVitusStd, vitusActual (finite>=0 or null),
expectedVitusPerMin = mean/duration*60, killsPerMin = totalEnemies/duration*60   (null if duration falsy/<0)
killsPerDrone = totalEnemies/drones if drones>0 else null, avgDroneIntervalSec,
players = unique(trimmed non-empty)[:4], each name cut to 64 chars ; squadSize = len(players) or null,
endReason, pctTimeAt15Plus = round(sum(bucket.pct for minCount>=15)*10)/10
```

**Geometry** (`config/shared/overlayLayout.ts:610-637`, `ipc/overlay/windows.ts:28-44, 411-417`):
- Canvas **420x252 CSS px**. Placement is `top-right` on the primary display: `x = area.right - 420 - 16`, `y = area.top + 8` (horizontal margin 16, top margin 8, clamped into the display).
- WFHelper scales it by a user "overlay scale" setting. The position can be dragged with the right mouse button and is persisted.
- **On Deck 1280x800 (16:10):** 420x252 is 33% of the width and 31.5% of the height. That fits as-is at 1:1 pixels (top-right, x=844..1264, y=8..260). Use a fixed pixel rectangle; aspect ratio does not matter because the overlay is not tied to game UI. Offer a scale setting from 0.75 to 1.25. The top-right corner in Warframe holds the minimap and objective UI, so consider top-center or left-middle as the Deck default.

**Contents** (`renderer/arbi-overlay.html`, `renderer/arbi-overlay.js:41-102`):
- Header:
  - "◆ ARBITRATION COMPLETE" in uppercase, color `#f5a623`.
  - Node name, large.
  - Right side: type label, duration (`Hh Mm Ss`), "N rotations", and an × button.
- KPI grid, 3 columns of value over label. Fields visible by default:
  - Expected Vitus `mean` with ` ±std` (1 dp)
  - Drone Kills (0 dp)
  - Total Kills (0 dp)
  - Time at 15+ Enemies `x.x%`
- KPI fields hidden by default (`hiddenByDefault`): Actual Vitus, Expected Vitus/min (2 dp), Enemies/min (1 dp), Kills/Drone (1 dp), Avg Drone Interval `x.xs`, squad line `Squad (n): a, b`, end reason line. A missing or negative value renders as `?`.
- Footer: "Right-click drag to move" and a "Detailed Stats" button. The button opens the main window on that run (`ARBI_OPEN_RUN`).
- Colors: bg `#060a12`, surface `#111827`, border `rgba(79,158,255,0.25)`, accent `#4f9eff`, text `#e8edf5`, muted `#a0aec0`. Fonts: Rajdhani for display, Barlow for body. Border radius 10.

**Decky mapping:**
- Render a 420x252 RGBA image with Pillow and blit it into the ARGB X window. Bundle TTFs for Rajdhani and Barlow, which are OFL licensed.
- The overlay is passive. Under gamescope, input cannot be routed to the external overlay, so there is no close button or drag. Instead:
  - auto-hide after 60 s, and
  - QAM "Dismiss" and "Open details" actions. "Open details" navigates to the route with the run id (`Navigation.Navigate("/wfh/arbi/<id>")`).
- Also send a Decky toast as a fallback.

---

## 9. Preload / IPC security (WFHelper-only)

`preload-arbi.ts` exposes `ready, close, openDetails(runId), moveBy(dx,dy), getThemeVars, getMessages, onData, onThemeVars, onMessages`. `handleAuthorized` checks the sender. None of this applies in Decky. Callables are already authenticated to the Steam client frontend.

---

## 10. Windows-only vs Linux

| Item | Windows | Linux path in WFHelper | Decky |
|---|---|---|---|
| EE.log location | `%LOCALAPPDATA%\Warframe\EE.log` | Proton prefix under compatdata/230410 | same as Linux, but resolve `~` to the deck user |
| Real-time lines | DBWIN worker | Proton debugstr tail (`"dbwin"` tag) | not needed; trackers use file lines only |
| Overlay | Electron transparent BrowserWindow | X11/Wayland window (no click-through on X11) | Pillow → X11 ARGB + GAMESCOPE_EXTERNAL_OVERLAY |
| Notifications | native toast + webhooks | libnotify + webhooks | Decky `toaster.toast` |
| File dialogs (import/export/save image) | Electron dialog | Electron dialog | in-UI file browser (`openFilePicker` from @decky/api) or fixed paths |
| Atomic rename retries for AV locks (`atomicFile.ts:4`) | yes | harmless | plain `os.replace` |

---

## 11. Porting risks

1. **Host-only telemetry.** `OnAgentCreated` and spawn-point lines are logged only by the mission host. Client runs are discarded (`skipReason`). Deck players often join public squads, so many runs will produce no record. The UI must explain this, not report "nothing tracked".
2. **Regex fidelity.** Use `re.search` (not `re.match`), apply the rules in the order given in section 2.6, re-feed a run-end line, match JS `String.replace` (first occurrence only), round with half-up in `round1`, and handle `$` with stripped CRs. Port the four fixtures in `tests/fixtures/arbi/` and the expectations in `tests/main/arbiRunParser.test.ts` as golden tests.
3. **Root vs deck user.** Use the deck user's home for EE.log discovery, and chown written data if the frontend reads files directly.
4. **EE.log lazy flush:** the summary appears 10-26 s after mission end. Truncation detection relies on the size shrinking. Warframe recreates EE.log at each launch, so the 2 s stat check is required.
5. **Payload size** over Decky's websocket. Strip `stats` from list responses.
6. **Region labels** need DE's ExportRegions and dict data. Precompute a small node map at build time; otherwise schedule entries show raw `SolNodeNNN`.
7. **Overlay interactivity.** Gamescope external overlays get no input, so the close, drag and "Detailed Stats" buttons must move to the QAM.
8. The **minimap alignment** is CPU-heavy and its assets come from a third party (arbi.guide). Defer it, or run it in the worker.

---

## 12. Profit-Taker tracker (brief)

- Files: `services/profitTakerParser.ts` (459 lines), `services/profitTakerTracker.ts`, `services/profitTakerLogImporter.ts`, `config/shared/profitTakerTypes.ts`, `src/lib/profitTakerStats.ts`, `src/components/profitTaker/*`.
- Uses the same `createRunTracker` with `indexFile="pt-runs.json"`, `logsDir="pt-logs"`, `sameTarget = always True`, `richness = 0`.
- `isRecord`: the record has a `phases` array.
- `skipReason` (`profitTakerTracker.ts:13-18`):
  - `"bounty was never entered"` if `not confirmed`
  - `"client-side run, no Camper telemetry in the log"` if `not hostTelemetry`
  - `"no phase data"` if there are no phases.
- Uses the same `arbiTrackingEnabled` toggle and the same EE.log feed.
- Key substring markers (`profitTakerParser.ts:13-46`):
  - Job: `ThemedSquadOverlay.lua: Active jobId set to ` with `/Lotus/Types/Gameplay/Venus/Jobs/Heists/HeistProfitTakerBountyFour`
  - Mission name: `... Mission name: Fortuna (Venus) - PROFIT-TAKER - PHASE 4`
  - `EidolonMP.lua: Session map string: ` (confirms the run)
  - `EidolonMP.lua: EIDOLONMP: Avatar left the zone` (elevator exit; flight start)
  - `EidolonMissions.lua - Starting job PROFIT-TAKER` (fallback start)
  - Phase starts: `CamperHeistOrbFight.lua: Orb Fight - Starting {first|second|third|final} attack Orb phase`
  - Shield element: `Camper->SwitchShieldVulnerability() - Switching shield damage vulnerability type to DT_*`
  - Legs: `Leg freshly destroyed at part: ARM_RIGHT|ARM_LEFT|LEG_RIGHT|LEG_LEFT`. These map to frontLeft/frontRight/backLeft/backRight; the log names legs from the orb's view.
  - Body: `StartVulnerable() - The Camper can now be damaged!`
  - Pylons: `ArachnoidCamperScript.lua: Pylon launch complete`
  - Shield phase end: `Sys [Info]: ResourceLoader` with one of `DBntyFourInterPrTk0920TheBusiness`, `DBntyFourInterPrTk0890TheBusiness`, `DBntyFourSatelReal0930TheBusiness`
  - Abort: `GameRulesImpl - changing state from SS_STARTED to SS_ENDING`
  - End of mission: `EOM missionLocationUnlocked=`
  - `Landscape - New State: ` with body-kill states {3,5,6}
  - Host migration: `"jobId" : "<BountyFour path>`
- DT→element map:
  - `DT_IMPACT` impact, `DT_PUNCTURE` puncture, `DT_SLASH` slash
  - `DT_FREEZE` cold, `DT_FIRE` heat, `DT_POISON` toxin, `DT_ELECTRICITY` electric
  - `DT_GAS` gas, `DT_VIRAL` viral, `DT_MAGNETIC` magnetic, `DT_RADIATION` radiation, `DT_CORROSIVE` corrosive, `DT_EXPLOSION` blast
- Record: `durationSec, flightSec, shieldSec, legSec, bodySec, pylonSec, phases[{index, totalSec, shieldSec, legSec, bodySec, pylonSec, shields[{element,seconds}], legs[{leg,seconds}]}], players, complete, bugged, aborted, hostMigration?, flightUnreliable?` plus the base fields.
- End reasons: `completed | aborted | mission-end | new-run | log-truncated | app-quit | inactivity | imported`.
- PT needs its own spec pass for the phase timing algorithm (`profitTakerParser.ts:90-459`) before porting.

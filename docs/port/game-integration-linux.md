# Port spec: Linux game integration (EE.log, Proton debugstr, game memory inventory, notifications)

Source: WFHelper at `/home/claude/wfhelper-src`. All `file:line` references are to that tree.
Target: Decky plugin, Python backend running as **root** on SteamOS game mode (Steam Deck LCD), Warframe via Steam Proton.

This spec covers:

1. EE.log / EE.cfg discovery under Proton (`services/eeLogPath.ts`)
2. The Proton `PROTON_LOG` / `OutputDebugString` real-time tail (`services/protonDebugstrMonitor.ts`, `services/debugLineFilter.ts`)
3. How inventory JSON is obtained on Linux: the auth-token memory scan plus the HTTPS fetch (`services/gameMemoryAuthz.ts`, `services/apiHelperRunner.ts`), the resident-inventory memory scan (`services/gameMemoryInventory.ts`), sync scheduling (`services/inventorySync.ts`), and file sources and fallbacks (`ipc/inventoryIpc.ts`, `services/alecaFrameInventory.ts`, `config/shared/inventoryPayload.ts`, `config/shared/inventorySource.ts`)
4. Game locale name tables (`services/gameLocale.ts`)
5. The GDPR trade-export import (`services/gdprImport.ts`), which imports **trade ledger rows, not inventory**
6. Trade and whisper notification sources, summarized (`ipc/tradeNotificationIpc.ts`, `ipc/messageNotificationIpc.ts`, `ipc/tradeWorkflow.ts`, `services/eeLogMonitor.ts`)
7. The Linux display backend (`services/linuxDisplayBackend.ts`, `config/shared/linuxDisplay.ts`). It is desktop-only and **not ported**. See section 9.

---

## 0. Steam Deck / Decky specifics that change WFHelper's assumptions

| WFHelper assumption | Reality in the Decky backend | What to do |
|---|---|---|
| `os.homedir()` is the player's home | The backend runs as root, so `~` is `/root` | Use `decky.DECKY_USER_HOME` (env `DECKY_USER_HOME`, normally `/home/deck`) everywhere WFHelper uses `os.homedir()`. This applies to Steam roots, the Proton log, and launcher prefixes. |
| Reading `/proc/<pid>/mem` needs `ptrace_scope=0` or `cap_sys_ptrace` | Root has `CAP_SYS_PTRACE`, so Yama does not block it | No sysctl change is needed. Keep the `mem-open-<errno>` failure path anyway. |
| The user adds `PROTON_LOG=1 %command%` by hand | Same requirement | Show the string in the QAM settings. Optionally set it from the frontend through `SteamClient.Apps.SetAppLaunchOptions(230410, ...)` after explicit user consent. That is a Decky-side option WFHelper does not have. |
| Files written under Electron `userData` | Decky has `DECKY_PLUGIN_SETTINGS_DIR`, `DECKY_PLUGIN_RUNTIME_DIR` and `DECKY_PLUGIN_LOG_DIR` | Put `inventory.json` and state JSON in the settings or runtime dir. Files are root-owned, which is fine. |
| Default Steam library | `/home/deck/.local/share/Steam`. An SD card library appears in `libraryfolders.vdf` as `/run/media/mmcblk0p1` or `/run/media/deck/<label>` | WFHelper's VDF parse already covers this. |
| Display backend selection (X11, XWayland, Wayland, portal) | gamescope only | Ignore section 9 entirely. |

---

## 1. EE.log discovery (`services/eeLogPath.ts`)

### 1.1 Constants

- `WARFRAME_STEAM_APP_ID = "230410"` (eeLogPath.ts:13)
- Override env var: `WFHELPER_EE_LOG`
# Port spec: external data sources and caches

Source: WFHelper at `/home/claude/wfhelper-src`, commit `06fa689` (2026-09-27). All `file:line`
references are relative to that tree. Target: Decky plugin (Python backend running as root, React
frontend in the QAM), Steam Deck LCD, game mode.

This spec covers every network source WFHelper reads, how it is paced, how it is cached and where it
lands on disk. Screen geometry and 16:10 do not apply here: nothing in this area depends on
resolution. Feature consumers (reward overlay, riven scanner, relic planner, arbitration) have
their own specs in this directory and only *call into* what is described here.

---

## 0. Summary table

| # | Source | Base URL | Auth | Refresh cadence | Disk cache (WFHelper) | Size (approx.) |
|---|---|---|---|---|---|---|
| 1 | warframe.market REST v1 | `https://api.warframe.market/v1` | none / JWT | on demand | none (memory) | 1 KB to 2 MB per call |
| 2 | warframe.market REST v2 | `https://api.warframe.market/v2` | none / Bearer JWT | on demand | none (memory) | `/v2/items` is a few MB (about 4,000 items) |
| 3 | warframe.market websocket | `wss://ws.warframe.market/socket` | JWT in first message | persistent while signed in | none | small JSON frames |
| 4 | warframe.market site (CSRF/CF) | `https://warframe.market/` | cookie | only when a mutation lacks header auth | Electron partition `persist:wfm-clearance` | HTML page |
| 5 | WFHelper backend (Cloudflare Worker) | `https://api.wfhelper.com` | optional bootstrap token | snapshot at startup; per-item on demand | `snapshot-cache.json`, `wfcd-relics.json`, localStorage | snapshot: several MB; relics 4.9 MB |
| 6 | WFHelper icon mirror | `https://assets.wfhelper.com` | none | image loads | browser cache | images |
| 7 | DE world state | `https://api.warframe.com/cdn/worldState.php` (+ fallbacks) | none | every 60 s background, 90 s TTL | memory only | about 100 to 300 KB |
| 8 | browse.wf oracle | `https://oracle.browse.wf/worldState.json`, `/bounty-cycle` | none | with world state | memory only | small / world-state sized |
| 9 | warframestat.us API | `https://api.warframestat.us/pc/...` | none | with world state | memory only | small |
| 10 | DE Public Export | `https://content.warframe.com/PublicExport/...` | none | once per app start, hash-diffed | `public-export-cache.json` | index under 1 KB; manifests MBs each |
| 11 | warframestat drops | `https://drops.warframestat.us/data/...` | none | once per app start, hash-diffed | `drop-data-cache.json` | `all.json` tens of MB |
| 12 | Bundled npm data | `warframe-public-export-plus@^0.6.8`, `@wfcd/items@^1.1276.6` | n/a | per release | shipped files | PEP dicts 62.6 MB total, exports tens of MB |
| 13 | Bundled item-name tables | `src/data/itemNames/<lang>.json` | n/a | per release | shipped files | 340 to 715 KB each, 15 files, 7.6 MB |
| 14 | Riven good-roll sheet | Google Sheets CSV export | none | 7-day cache | `riven-good-rolls-cache.json` | small CSVs |
| 15 | Arbitration schedule | `https://browse.wf/arbys.txt` | none | hourly | `arbi-sched-cache.json` | small text |

All WFHelper disk caches live in Electron `userData`, which WFHelper sets explicitly
(`config/runtime/appIdentity.ts:37-50`) to `<appData>/WFHelper`:

- Windows: `%APPDATA%\WFHelper\`
- Linux: `$XDG_CONFIG_HOME/WFHelper/` (normally `~/.config/WFHelper/`)
- Override: env `WFHELPER_USER_DATA`.

JSON caches are written atomically (`services/atomicFile.ts`, used by `services/jsonCache.ts:69-75`)
and a corrupt file is renamed to `<file>.corrupt-<ms>` (`services/jsonCache.ts:26-36`).

**Decky mapping.** Use `decky.DECKY_PLUGIN_RUNTIME_DIR` (or `DECKY_PLUGIN_SETTINGS_DIR` for the
session token) as the `userData` equivalent. Keep the same file names so a user can copy caches
over. Write atomically: write to `<file>.tmp`, `fsync`, then `os.replace`.

---

## 1. warframe.market (WFM)

### 1.1 Hosts and base headers

| Purpose | URL | Source |
|---|---|---|
| REST v1 | `https://api.warframe.market/v1` | `services/wfmClient.ts:39` |
| REST v2 | `https://api.warframe.market/v2` | `services/wfmClient.ts:40` |
| Websocket | `wss://ws.warframe.market/socket`, subprotocol `wfm`, `Origin: https://warframe.market` | `services/wfmWebSocketCommon.ts:4-6` |
| Static assets | `https://warframe.market/static/assets/` | `config/shared/wfm.ts:42` |
| Website (CSRF page, links) | `https://warframe.market/`, `/items/<slug>`, `/auction/<id>` | `services/wfmClient.ts:111` |

Main-process (authenticated) base headers, `services/wfmClient.ts:47-52`:

```
Accept: application/json
Content-Type: application/json
Platform: pc
Language: en
```
v2 calls add `Crossplay: true` (`services/wfmClient.ts:859`).

Renderer and worker (anonymous reads) use `WFM_HEADERS`, `config/shared/wfm.ts:34-39`:

```
Platform: pc
Language: en
Crossplay: true
Accept: application/json
```

Request timeout 20 s (`wfmClient.ts:41`), response cap 8 MiB (`wfmClient.ts:42`; over the cap the
request fails with `WFM_RESPONSE_TOO_LARGE`). Redirects are **not** followed: a 3xx is returned to
the caller with its `Location` (`wfmClient.ts:420-427`, `wfmClient.ts:749-778`).

Responses: v1 wraps data in `{ "payload": ... }`, v2 in `{ "data": ... }`. `unwrapWfmResponse`
(`services/wfmTypes.ts:97-103`) returns
`obj.data` if the key exists, else `obj.payload`, else the object.

Error detail extraction (`wfmClient.ts:567-601`): use `body.error` if string; else
`body.error.message`; else `body.message`; else flatten the `error` object to at most 4
`"field: code"` strings joined by `"; "` (example `{inputs:{perTrade:"app.field.required"}}` gives
`perTrade: app.field.required`). Several retry rules below regex-match this text.

### 1.2 Slug rules

- WFM slug regex (`config/shared/textNormalize.ts:39`):
  `^[\p{Ll}\p{N}](?:[\p{Ll}\p{M}\p{N}_()'’-]{0,118}[\p{Ll}\p{N})])?$` (Unicode, needs the
  Python `regex` module or an ASCII approximation; WFM slugs are effectively ASCII).
- `sanitizeWfmSlug(v)` = `v.strip().lower()` if it matches, else `None` (`textNormalize.ts:46-50`).
- `normalizeForSlug(name)` (`textNormalize.ts:14-23`): lower, strip `'` `’` `‘`, replace each run of
  `[^a-z0-9]+` with `_`, trim `_`. Example `Medi-Ray` gives `medi_ray`.
- `normalizeWfmSlug(v)` = `sanitizeWfmSlug(v) or normalizeForSlug(v)` (`config/shared/wfm.ts:96-98`).
  A slug WFM minted (for example `zid-an_asheir`-style hyphens) survives verbatim.
- Excluded slugs, never priced (`config/shared/wfmExclusions.ts:4-47`): `vendor_relic`; any slug
  ending `_scene`; ranked-price exclusion also for `blood_for_ammo|energy|life` and
  `(^|_)riven_mod_veiled$`.

### 1.3 Rate limiting (main process): `services/wfmScheduler.ts`

One global scheduler fronts every authenticated WFM call. Constants verbatim
(`wfmScheduler.ts:19-46`):

```
RATE_PER_SECOND: 2.5          # token bucket refill
BURST_TOKENS: 5
MAX_CONCURRENT: 3
MAX_BACKGROUND_CONCURRENT: 2  # background never takes the last slot
MAX_QUEUE_DEPTH: 64           # submitted-but-unsettled; beyond -> WFM_QUEUE_FULL
MAX_RETRIES: 3                # extra sends after the first
RETRY_BASE_MS: 750
RETRY_FACTOR: 2
RETRY_CEILING_MS: 2_500
RETRY_JITTER_RATIO: 0.25      # jitter added upward only
MAX_RETRY_WAIT_MS: 10_000     # longer wait -> fail now
DEFAULT_RATE_LIMIT_GATE_MS: 30_000
MIN_RATE_LIMIT_GATE_MS: 1_000
MAX_RATE_LIMIT_GATE_MS: 300_000
FAILURE_WINDOW_MS: 60_000
DEGRADED_FAILURES: 6
```

Algorithm:

1. Priorities are `interactive` (default) and `background`. Pick the first interactive task; else
   the first background task if fewer than 2 background tasks are in flight (`wfmScheduler.ts:118-129`).
2. Admit while `inFlight < 3` and tokens >= 1; each admission costs 1 token. Tokens refill at
   2.5/s up to 5 (`wfmScheduler.ts:111-116`, `155-182`).
3. On a 429: gate **all** requests for `clamp(Retry-After*1000, 1s, 5min)` (30 s if no header).
   While gated, set `lastRefill = gateEnd` and `tokens = min(tokens, 1)` so the bucket does not
   burst when the gate lifts (`wfmScheduler.ts:215-234`). Interactive requests whose wait would
   exceed 10 s are failed immediately with `WFM_RATE_LIMITED`; background ones wait
   (`wfmScheduler.ts:145-153`).
4. Retry only replayable failures: GET/HEAD on transport error or 5xx; any method on 429
   (except sign-in). Wait = `Retry-After` if given, else `min(750 * 2^(n-1), 2500)`, plus
   `random()*0.25*base`, never less than the remaining gate. Give up if the wait exceeds 10 s or
   after 3 retries (`wfmScheduler.ts:238-314`).
5. Health: `backoff` while gated, `degraded` with 6+ transient failures in the last 60 s, else
   `ok`. One success removes the oldest failure (`wfmScheduler.ts:197-213`, `293-300`).
6. Work that waits on something other than WFM (CSRF page fetch, challenge window) releases its
   slot and re-acquires without paying a second token (`runUnadmitted`, `wfmScheduler.ts:269-283`).

User-facing error text: `Warframe.market rate limit hit. Please wait {ceil(ms/1000)}s before trying again.`
(`wfmScheduler.ts:53-59`).

**Python port:** a single `asyncio` scheduler with the same constants. Route every WFM call,
authenticated or anonymous, through it. WFHelper's renderer has separate, weaker pacing (1.6);
in the plugin there is one backend process, so use one budget.

### 1.4 Anonymous read endpoints

| Endpoint | Used for | Source |
|---|---|---|
| `GET /v1/items/{slug}/statistics` | price (48 h closed-volume average) | `src/lib/wfm/wfmPrice.ts:174-209`, `services/wfmStatsPrice.ts:62`, worker `services/prewarm.ts:163` |
| `GET /v2/orders/item/{slug}` | order book (preferred) | `src/lib/wfm/orderBook.ts:107-109` |
| `GET /v1/items/{slug}/orders` | order book fallback when v2 answers a non-transient error | `src/lib/wfm/orderBook.ts:144-146` |
| `GET /v2/items/{slug}` | item meta: ducats, setRoot, thumb/icon, maxRank, subtypes | `src/lib/wfm/wfmItemMeta.ts:80-98`, `services/wfmCatalog.ts:352-386` |
| `GET /v2/items` | whole item catalog (fallback when backend catalog fails) | `services/wfmCatalog.ts:121` |
| `GET /v2/item/{slug}/set` | set membership (parts, `quantityInSet`, `setRoot`) | `services/wfmCatalog.ts:276-321` |
| `GET /v2/riven/weapons` | riven-eligible weapon list (v1 `/riven/items` is 404 since the 2026 API cut) | `services/wfmRivenItems.ts:9-11,82` |
| `GET /v1/auctions/search?type=riven&weapon_url_name={w}[&positive_stats=a,b][&negative_stats=c][&polarity=p]&sort_by=price_asc|price_desc` | riven auctions | `services/wfmRivenSearch.ts:160-195` |
| `GET /v2/user/{slug}` | public profile (status, slug, ingameName) | `services/wfmSession.ts:354`, `services/wfmProfileSlug.ts:37` |
| `HEAD /v1/profile/{name}/reviews/` | discover a user's real slug from the 3xx `Location` (`/profile/([^/?#]+)/reviews/?$`) | `services/wfmProfileSlug.ts:8,69` |

#### 1.4.1 Statistics payload and the price algorithm (`config/shared/wfmStats.ts`)

Response (v1): `{"payload": {"statistics_closed": {"48hours": [...], "90days": [...]}, "statistics_live": {"48hours": [...], "90days": [...]}}}`.
Each closed row has `datetime` (ISO), `volume`, `wa_price`, `median`, optional `mod_rank`,
optional `order_type`. Accept the key spelling `48_hours` too.

Price basis tag: `WFM_PRICE_BASIS = "closed-volume-average-48h-v1"` (`wfmStats.ts:4`). Algorithm
`extractAverageFromStatsPayload(json, {rank?, now?})` (`wfmStats.ts:69-116`):

1. `rows = payload.statistics_closed["48hours"]`; if not a list return null.
2. Rank pooling (`pooledRank`, `wfmStats.ts:16-22`): with an explicit rank keep rows whose
   `mod_rank ?? rank` equals it; without one keep rows with no rank or rank 0.
3. Keep rows where `order_type` is absent or `"sell"`, `datetime` within `[now-48h, now]`,
   `wa_price > 0`, `volume > 0`. Deduplicate by `"{time}:{rank}"` (last wins).
4. Reference price: median of `statistics_live["48hours"]` sell-row `median` values for the same
   rank pool; if none, median of `statistics_closed["90days"]` rows older than the window.
5. Drop samples with `wa_price > 10 * reference` (`PRICE_OUTLIER_FACTOR = 10`, fake-close filter).
6. `average = round(sum(wa_price*volume) / sum(volume))`; return `{average, timestamp: newest row
   time, volume}` only if volume > 0 and average > 0.

The field is still called `median` everywhere (backend wire format, caches) but holds this average.

#### 1.4.2 Order book normalisation (`config/shared/wfmOrders.ts:146-244`)

- Order list: `data` if array; else `payload.orders`; else `data.orders`; else `orders`.
- Side: v1 `order_type`, v2 `type`. Name: v1 `user.ingame_name`, v2 `user.ingameName`.
  Status: `user.status` lowercased (`ingame`, `online`, `offline`). Platform: `user.platform` or
  top-level `platform`. Rank: `rank` or `mod_rank`. Bulk: v2 `perTrade`, v1 `per_trade`, clamped to
  `[1, quantity]`.
- Drop `visible === false`, rank mismatch, empty name, platinum <= 0. Without a subtype filter
  drop `subtype == "atragraph"`. With a subtype filter (relics) a missing subtype counts as
  `intact`.
- `unitPlatinum = round(platinum/perTrade, 2)`. Sort sell ascending / buy descending by
  unitPlatinum, then quantity descending, then name. Worker caps at 500 per side; renderer keeps all.
- "Active" means status `ingame` or `online`. `bestOrderPrice(entries, side, activeOnly)` =
  `max(1, round(min or max unitPlatinum))` (`wfmOrders.ts:119-129`).
- Ayatan sculpture orders also carry `amberStars` / `cyanStars`.

#### 1.4.3 Catalog item normalisation (`services/wfmCatalog.ts:43-74`)

`slug = slug|url_name|_slug`; name = `i18n.en.name|i18n.en.itemName|i18n.en.item_name|item_name|itemName|name|titleFromSlug(slug)`;
`thumb`/`icon` from `i18n.en.*` then top level, converted with `formatWfmAssetUrl`;
`maxRank = floor(maxRank|max_rank)` if > 0; `gameRef` (the DE uniqueName). Indexes: by id, slug,
lowercase name, lowercase gameRef, plus aliases: slug without `_set` title-cased, and a name with
a trailing `(Key)`/`(Veiled)` stripped (`wfmCatalog.ts:154-176`). `lookupByName` also tries
`name + " set"` and `name - " set"`. Search: case-insensitive startsWith first, then contains,
min query 2 chars (`wfmCatalog.ts:188-208`). On load failure, retry is blocked for 15 s.

Item details (`/v2/items/{slug}`) are cached 5 min in memory; `hasRanks = maxRank > 0` only from
this call; `subtypes` = unique non-empty strings (`wfmCatalog.ts:348-386`).

#### 1.4.4 Asset URLs (`config/shared/wfm.ts:50-83`)

WFM gates `/static/assets/` behind a Cloudflare challenge, so thumbnails go through the WFHelper
mirror. `formatWfmAssetUrl(path)`:

1. Prefix relative paths with `https://warframe.market/static/assets/`.
2. If the path contains a `thumbs/` segment: drop any 32-hex content-hash segment from the file
   name and return `https://assets.wfhelper.com/wfm/<dirs>/<file>`. Example
   `items/images/en/thumbs/ash_prime.1a2b...(32 hex).128x128.png` gives
   `https://assets.wfhelper.com/wfm/items/images/en/thumbs/ash_prime.128x128.png`.
3. Otherwise return the upstream URL. Env `WFHELPER_ICON_MIRROR_URL` / `WFHELPER_ICON_MIRROR_DISABLED=1`.

### 1.5 Authentication (sign-in for orders)

All of this is in the main process: `services/wfmSession.ts`, `services/wfmClient.ts`.

**Sign-in** (`wfmSession.ts:171-234`):

```
POST https://api.warframe.market/v1/auth/signin
Accept: application/json
Content-Type: application/json
Platform: pc
Language: en
Authorization: JWT
auth_type: header

{"email": "...", "password": "...", "device_id": "<uuid>", "auth_type": "header"}
```

- `device_id`: random UUID persisted in `userData/wfm.device-id` (fallback `"wfhelper"`).
- If this fails with anything other than 401, 429 or a message containing `app.account.`, retry
  once in cookie/CSRF mode: same body without `auth_type`, no `Authorization` header, with the
  mutation headers of 1.5.2.
- Never auto-retry sign-in (password); a 429 surfaces immediately.
- Token extraction order: response header `Authorization` (strip leading `JWT `), else
  `Set-Cookie` `JWT=([^;,]+)`, else `body.payload.token` / `body.token`.
- User: `body.payload.user` or `body.user`; `userName = ingame_name || name || email-local-part`,
  `platform = user.platform || "pc"`.

**Authenticated calls** (`wfmClient.ts:657-793`):

- v2: `Authorization: Bearer <jwt>` and `auth_type: header`. v1: `Authorization: JWT <jwt>` and
  `auth_type: header`.
- If header auth answers 401/403, retry once with cookie auth (`Authorization: JWT <jwt>`,
  `Cookie: JWT=<jwt>`, plus CSRF for non-GET). If the cookie retry succeeds, stop using header auth
  for the session (`_headerAuthBroken`).
- 401 after that: error `WFM_UNAUTHORIZED`, and the IPC layer signs the user out
  (`ipc/wfmIpc.ts:62`).
- **Token rotation:** on any 2xx, if the response has an `Authorization` header, strip
  `Bearer `/`JWT ` and, if it differs, adopt it, re-derive CSRF, persist the session and hand it to
  the websocket listener (`wfmClient.ts:550-557`, `wfmSession.ts:69-76`).

**`/v2/me`** returns `{data: {id, ingame_name?, slug, ...}}`; `slug` is the authoritative profile
slug, cached once per session, fallback `normalizeWfmSlug(userName)` (`wfmSession.ts:278-326`).
`/v2/me` has no status; status comes from `GET /v2/user/{slug}` where `offline` maps to
`invisible` (`wfmSession.ts:348-364`).

#### 1.5.1 Session persistence (Windows-only behaviour noted)

`userData/wfm.session` holds `{"token","userName","platform"}` encrypted with Electron
`safeStorage` (DPAPI on Windows, Keychain on macOS, libsecret/kwallet on Linux). On Linux with no
keyring (`basic_text` backend) nothing is written and the session lasts until restart
(`persistable: false`) (`wfmSession.ts:95-163`).

**Decky:** there is no desktop keyring in game mode. Store the token in
`DECKY_PLUGIN_SETTINGS_DIR/wfm.session` with mode `0600` (the backend is root). Optionally encrypt
with a key derived from `/etc/machine-id`; tell the user it is stored locally.

#### 1.5.2 CSRF and Cloudflare (cookie mode only)

Only used when header auth is unavailable. `applyMutationHeaders` (`wfmClient.ts:634-655`) sets
`X-CSRFToken`, `Cookie: JWT=<jwt>`, `Authorization: JWT <jwt>`, `Origin: https://warframe.market`,
`Referer: https://warframe.market/`. The CSRF token must be the `csrf_token` claim of the **same**
JWT sent as cookie (decode the JWT payload, base64url, no verification).

To get an anonymous JWT/CSRF: `GET https://warframe.market/` with `Accept: text/html,...`
and a Chrome UA (`Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/<v> Safari/537.36`),
read `Set-Cookie JWT=`, else `<meta name="csrf-token" content="...">` (ignore values starting `##`).
Second try with UA `Mozilla/5.0 WFHelper/1.0` (`wfmClient.ts:84-140`, `268-290`).

Cloudflare challenge detection: `cf-mitigated: challenge` header, or 403 with body matching
`/just a moment|cf-chl|challenge-platform/i` (`wfmClient.ts:136-139`, `231-237`). WFHelper then
opens a hidden 480x640 Electron window on `https://warframe.market/` for up to 120 s (shown after
8 s), polls cookies every 500 ms for `JWT`, and keeps `cf_clearance`/`__cf*` cookies plus the
window's exact UA for later requests (`wfmClient.ts:148-223`). Re-solve at most once per 5 min
(`wfmClient.ts:228-253`). WFHelper also prefers Chromium's network stack (`electron.net`) for its
browser TLS fingerprint and falls back to Node `https` on `net::ERR_*` (`wfmClient.ts:341-393`).

**Decky:** no Chromium window and no browser TLS fingerprint in the Python backend. Use header
auth only (it skips CSRF entirely); do not implement cookie mode at first. If WFM starts
challenging the Deck's IP, the fallbacks are: (a) send requests from the frontend through the Steam
client CEF (`fetch` from the QAM context has a real Chrome fingerprint but is subject to CORS, and
WFM's API CORS policy is unverified), or (b) show an error. See risk R1.

### 1.6 Authenticated endpoints (orders, contracts, reviews, status)

| Action | Request | Body / notes | Source |
|---|---|---|---|
| My orders | `GET /v2/orders/my` | returns `{data:[order]}`; v2 orders carry only `itemId`, so enrich each from the catalog by id | `services/wfmOrders.ts:108-137` |
| Create order | `POST /v2/order` | `{"itemId","type":"sell"|"buy","platinum":int,"quantity":int,"visible":bool,["perTrade":int],["rank":int],["subtype":str]}` | `wfmOrders.ts:176-268` |
| Update order | `PATCH /v2/order/{id}` | any of `platinum, quantity, visible, rank, subtype` | `wfmOrders.ts:270-299` |
| Delete order | `DELETE /v2/order/{id}` | none | `wfmOrders.ts:301-305` |
| Close (sold) | `POST /v2/order/{id}/close` | `{"quantity": n}`; reply has no remaining count, re-fetch | `wfmOrders.ts:307-321` |
| Set visible (bulk) | sequential `PATCH` per id | | `wfmOrders.ts:323-339` |
| My riven auctions | first that works of `GET /v1/profile/auctions`, `/v1/profile/{slug}/auctions`, `/v2/contracts/my`, `/v2/auctions/my`, `/v2/profile/{slug}/contracts`, `/v2/profile/{slug}/auctions`, `/v1/profile/{slug}/contracts`, each with `?page=&limit=` (default limit 40, 1..100). Skip on 301/302/303/400/404/405; remember the winner | `services/wfmContracts.ts:18-23,245-404` |
| Close auction | `PUT /v1/auctions/entry/{id}/close` (no DELETE; v1 answers 405) | | `wfmContracts.ts:412`, `wfmRivenSearch.ts:282-297` |
| Create auction | `POST /v1/auctions/create` | `{"item":{"type":"riven","name":<lowercase riven name>,"weapon_url_name","attributes":[{"url_name","value","positive"}],"re_rolls","mastery_level","polarity","mod_rank"},"starting_price","minimal_reputation","private","buyout_price":<int or null, key required>,["note"]}` | `wfmRivenSearch.ts:238-276` |
| Update auction | `PUT /v1/auctions/entry/{id}` | `{"minimal_reputation","visible","note",["starting_price"],"buyout_price":<int or null>}` | `wfmRivenSearch.ts:299-335` |
| +1 review | `POST /v1/profile/{slug}/review` | `{"review_type":1,"text":""}`; on 301/302/307/308 to `.../profile/<slug>/review` re-POST there; `app.review.already_exist` means already done | `services/wfmReviews.ts:16-76` |

**Create-order compatibility retries** (`wfmOrders.ts:139-264`), each at most once:

1. Error text matches `/perTrade/i` and (`/required/i` when not sending it, `/notAllowed/i` when
   sending it): flip whether `perTrade` is sent and remember it process-wide. (API 0.24 required
   it, 0.25 rejects it.) `perTrade = clamp(floor(perTrade or 1), 1, quantity)`.
2. No rank given and error matches `\brank\b` and `required`: retry with `rank: 0`.
3. No subtype and error matches `subtype` and `required`: fetch `/v2/items/{slug}` subtypes;
   if `regular` is offered, retry with it; else raise `subtype_required` with the list so the UI
   can ask. Known subtypes: relics `intact|exceptional|flawless|radiant`; mods `regular|atragraph`
   (`config/shared/wfmOrders.ts:3-4`).

Order normalisation (`wfmOrders.ts:33-59`): `orderType = type|order_type`, `modRank = rank|mod_rank`,
`perTrade = perTrade|per_trade`, name from catalog `item_name`.

### 1.7 Websocket (presence and pushes)

`services/wfmWebSocketCommon.ts`, `services/wfmWebSocket.ts`, `services/wfmWebSocketListener.ts`.

- Connect `wss://ws.warframe.market/socket`, subprotocol `wfm`, `Origin: https://warframe.market`,
  max payload 1 MiB, no permessage-deflate, handshake timeout 15 s.
- Frame format (both directions): `{"route": "...", "payload": {...}, "id": "<11 random [A-Za-z0-9]>"}`.
- First message: `{"route":"@wfm|cmd/auth/signIn","payload":{"token":"<jwt>"}}`. Wait for a route
  containing `auth/signIn:ok`. Any route ending `:error` is an error.
- Set status: `{"route":"@wfm|cmd/status/set","payload":{"status":"online|ingame|invisible","duration":<seconds or null>}}`;
  success route contains `status/set:ok` and payload `statusUntil` (ISO or null). `duration` makes
  WFM expire the status server-side. Hold choices (minutes): `[0, 30, 60, 120, 240]`, 0 = forever
  (`config/shared/wfm.ts:8`).
- Listener: ping every 30 s; reconnect with `min(1000*2^n, 60000) + rand(0..500)` ms; reset backoff
  only after the socket held 60 s; after 3 consecutive `auth/signIn:error` give up and tell the UI
  (`listener-auth-failed`).
- Pushed routes handled (`ipc/wfmIpc.ts:75-116`): contains `status/set` -> presence update;
  matches `/order|auction/i` -> refresh my orders; contains `message/new` or `message/create` ->
  whisper toast (`payload.from` or `payload.user.ingame_name`, text `payload.message` or
  `payload.raw_message`).
- Presence rules, persisted in `userData/wfm-presence.json` (`services/wfmPresence.ts:48`): auto
  `ingame` while Warframe runs, away after 1..60 min idle (default 10), restore after 5 s at startup.
  Idle time on the Deck needs a different source than the Windows idle API (see game integration spec).

Python: use `websockets` (pure Python) with `subprotocols=["wfm"]`, `origin="https://warframe.market"`.

### 1.8 Renderer-side direct pacing (for reference)

The renderer hits WFM directly only when the backend has no answer.

- Prices (`src/lib/wfm/wfmPrice.ts:20-38`): priority queue `high|normal|low`, depth 64, adaptive
  spacing 350 ms base, +120 ms per 429 up to 1200 ms, -5 ms per success; a 429 holds the queue
  `max(Retry-After, 30 s)`. Timeout 10 s.
- Order book (`src/lib/wfm/orderBook.ts:29-36`): 4 concurrent, TTL 45 s, not-found TTL 3 min.
- Meta (`src/lib/wfm/wfmItemMeta.ts:17-20`): TTL 7 days, no-data 6 h.

---

## 2. WFHelper backend (`https://api.wfhelper.com`)

A Cloudflare Worker (`backend/worker/`) that caches WFM data in KV and publishes aggregates. Overridable
with `VITE_WFM_BACKEND_URL` (`config/shared/backendConfig.ts:4`). Full architecture:
`backend/worker/ARCHITECTURE.md`.

### 2.1 Headers every client request sends

```
Accept: application/json
x-wfhelper-client: <Product>/<version>      # config/shared/backendConfig.ts:6-13
x-wfhelper-bootstrap: <token>               # only on bootstrap-guarded routes, when enabled
```

`<Product>` must match `^[A-Za-z0-9][A-Za-z0-9 ._-]{0,31}$`, version 1-24 chars starting with a
digit; leading `v` stripped; dev sends `0.0.0`. Production policy is currently `log`
(`wrangler.jsonc` `PUBLIC_CLIENT_POLICY: "log"`, allow `WFHelper`, deny empty); under `enforce` only
`WFHelper` would be served. The plugin should announce its own product name (for example
`WFHelperDecky/0.1.0`) and coordinate with upstream before relying on the backend (risk R2).

### 2.2 Bootstrap token

Production worker has `PUBLIC_BOOTSTRAP_REQUIRED: "1"` and release builds must set
`VITE_WFM_BACKEND_BOOTSTRAP_ENABLED=1` (`scripts/validate-release-env.mjs:30-33`), so the per-item
routes need it.

```
GET /v1/bootstrap
-> 200 {"ok":true,"data":{"token":"<b64url payload>.<b64url HMAC-SHA256>","header":"x-wfhelper-bootstrap","expiresAt":<epoch ms>}}
-> 404 {"ok":false,"error":"disabled"}
```

The token payload is `{v:1, ip, ua: sha256-b64url(User-Agent[:240]), iat, exp}`
(`backend/worker/src/security/bootstrap.ts:68-90`). TTL default 900 s, clamp 60..3600
(`worker/src/config.ts:82`). **It is bound to the caller IP and the exact User-Agent string**, so
the Python client must send the same `User-Agent` on `/v1/bootstrap` and on every guarded call.

Client rules (`src/lib/wfm/backendLite.ts:35-103`): reuse until `expiresAt - 60 s`; single-flight;
after a failure do not retry for 30 s (`BACKEND_BOOTSTRAP_FAILURE_COOLDOWN_MS`,
`config/runtime/cacheConfig.ts:17`); on any 401 drop the token. Guarded routes answer
`401 {"ok":false,"error":"bootstrap_required"}` or `503 bootstrap_misconfigured`.

Request timeout is 3.5 s for per-item routes (`backendLite.ts:29`).

### 2.3 Routes

Envelope for per-item routes (`worker/src/routes/public.ts:179-193`): `200 {"ok":true,"data":{...}}`
with `Cache-Control: public, max-age=60`; `404 {"ok":false,"error":"not_found"}`;
`503 {"ok":false,"error":"unavailable"}`. Excluded slugs answer 404 with `max-age=3600`.

| Route | Bootstrap | Rate limit / IP / min | Edge cache | Response `data` | Client use |
|---|---|---|---|---|---|
| `GET /healthz` | no | 5 | | | |
| `GET /v1/bootstrap` | no | 60 | | see 2.2 | |
| `GET /v1/snapshot?client=closed-average-v3` | no | 2 | `max-age=7200`, ETag | see 2.4 | startup |
| `GET /v1/wfm-items` | no | 2 | `max-age=21600`, ETag | `{"ok":true,"updatedAt":ms,"items":[{"id","slug","name","thumb","icon","maxRank","gameRef"}]}` (raw WFM asset paths) | WFM catalog, preferred over `/v2/items` (timeout 10 s) |
| `GET /v1/prices/{slug}[?rank=n]` | **yes** | 200 | 60 s | `{"slug","priceBasis":"closed-volume-average-48h-v1","median","timestamp","rank"}`; reject if `priceBasis` differs | price |
| `GET /v1/meta/{slug}` | **yes** | 200 | 60 s | `{"slug","tradable","thumb","icon","ducats","setRoot","timestamp"}` | ducats, set root, icons |
| `GET /v1/order-summary/{slug}?rank=n` | **yes** | 200 | 60 s | `{"slug","rank","wts","wtb","timestamp",...}`; `wts` = best active sell else best any, `wtb` likewise | best WTS/WTB |
| `GET /v1/order-summary/{slug}?subtype=intact|exceptional|flawless|radiant` | **yes** | 200 | 60 s | same, slug must end `_relic`, else `400 invalid_subtype`; never send `regular` | relic refinement prices |
| `GET /v1/orders/{slug}` | | | | `410 deprecated` | do not use |
| `GET /v1/price-history/{slug}` | no | 200 | 1 h, ETag | `{"ok":true,"slug","generatedAt","rows":[[date, rank|null, median, volume|null]]}`; 404 `price_history_not_ready`/`not_found`; 503 `catalog_unavailable` | history chart (timeout 10 s) |
| `GET /v1/top-traded` | no | 200 | 1 h, ETag | `{"ok":true,"generatedAt","windowDays":7,"items":[{"slug","name","volume","median","value","thumb?"}],"byValue":[slugs]}`; 404 `top_traded_not_ready` | market browse |
| `GET /v1/baro-history` | no | 200 | 1 h (5 min when empty), ETag | `{"ok":true,"data":{"version","updatedAt","coverageStart","visits","lastSeen"}}`, body cap 8 MiB, timeout 10 s | Baro planner |
| `GET /v1/adversary-vendors` | no | 200 | 1 h | vendor tables | World tab |
| `GET /v1/nightwave-offerings` | no | 200 | 1 h | `{"ok":true,"generatedAt","source","tabs"}` | World tab |
| `GET /v1/supporters` | no | 200 | 1 h | `{"ok":true,"updatedAt","supporters":[{"name","tier"}]}` | credits |
| `GET /v1/wfcd-relics` | no | 200 | 1 h, ETag `"wfcd-<version>-<cacheVer>"` | see 2.5 | relic table refresh |
| `POST /v1/feedback` | no | 2/IP, 10 global | | | opt-in reports |

Other limits: daily request budget 300,000 sampled at 100 (`wrangler.jsonc`). Per-item cache
lifetimes inside the worker: price/meta 24 h, order summary 48 h, stale refresh at 21 h, no-data
markers 900 s; cron every 15 min plus daily 04:00 UTC.

Rank validation: `/v1/prices` and `/v1/order-summary` reject a rank for an item that is not in the
worker's ranked catalog or out of range; order-summary requires a rank for ranked items
(`public.ts:681-743`).

### 2.4 Snapshot (`/v1/snapshot`) and its disk cache

Loader: `src/lib/wfm/snapshotLoader.ts:40-146`. Validation: `config/shared/wfmSnapshotValidation.ts`.

```json
{
  "version": 1,
  "generatedAt": 1790000000000,
  "prices": {
    "ash_prime_set": {"status":"ok","median":95,"timestamp":1790000000000,"priceBasis":"closed-volume-average-48h-v1"},
    "primed_flow:rank-v3:r10": {"status":"ok","median":40,"timestamp":...,"priceBasis":"..."},
    "some_item": {"status":"no_data","median":null,"timestamp":...}
  },
  "meta": {"ash_prime_set": {"slug":"ash_prime_set","ducats":null,"setRoot":true,"thumb":"...","icon":"...","timestamp":...}},
  "orderSummaries": {"primed_flow:r10": {"status":"ok","wts":45,"wtb":30,"timestamp":...,"sourceTimestamp":...}}
}
```

Cache key formats (`config/shared/wfmCacheKeys.ts:1-27`):

| Namespace | Unranked | Ranked |
|---|---|---|
| client price | `{slug}` | `{slug}:rank-v3:r{n}` |
| client order summary | (none) | `{slug}:r{n}` |
| client order book | `{slug}` | `{slug}:r{n}` (+`:st-{subtype}`) |
| worker price | `price:{slug}` | `price:{slug}:r{n}` |
| worker orders summary | `orders-summary:{slug}` | `orders-summary:{slug}:r{n}` / `:s{subtype}` |

Validity: `version == 1`; every timestamp `> 0`, not more than 5 min in the future, not older than
30 days; keys 1..160 chars; price `status` `ok` needs `median >= 0`, `no_data` needs null median;
meta needs `slug`, boolean `setRoot`, nullable non-negative `ducats`; order summaries need nullable
non-negative `wts`/`wtb`.

Algorithm:
1. Read `userData/snapshot-cache.json` (IPC `loadSnapshotCache`, file name in
   `ipc/snapshotCacheIpc.ts:6`; max 64 MiB / 200k keys, `ipc/diskCacheIpcFactory.ts:25-26`).
   If valid, `now - generatedAt < 2 h` and every price has the current basis, use it; else keep its
   `meta` as a stale fallback for the set list.
2. Else `GET /v1/snapshot?client=closed-average-v3` with `Cache-Control: no-cache`, `Pragma: no-cache`,
   `If-None-Match: <etag>` (etag kept only in memory), timeout 20 s. On 304 keep current caches.
3. Validate; save to disk; import into the in-memory caches, keeping an existing entry when it is newer.
4. Import set slugs (keys ending `_set` with `setRoot: true`) only if there are at least 200, so a
   partial snapshot never becomes a deny list (`wfmItemMeta.ts:120-135`).

In-memory TTLs after import: price ok 48 h, price no-data 12 h (`src/lib/wfm/priceCache.ts:5-6`);
order summary fresh 24 h, stale 48 h (`src/lib/wfm/orderSummaryCache.ts:9-10`); meta 7 days.

### 2.5 WFCD relic data (`/v1/wfcd-relics`) — `services/relicDataUpdate.ts`

Lets the app pick up newer relic contents than the bundled `@wfcd/items`.

```json
{"version":"1.1277.0","publishedAt":"2026-09-20T...Z","relics":[
  {"uniqueName":"/Lotus/Types/Game/Projections/T1VoidProjectionA1Bronze","name":"Lith A1 Intact",
   "vaulted":true,"imageName":"...","dropCount":3,
   "rewards":[{"chance":25.33,"rarity":"Common","item":{"uniqueName":"...","name":"...","imageName":"...","ducats":15,
               "warframeMarket":{"id":"...","urlName":"..."}}}]}]}
```

Rules (`config/shared/wfcdRelicRules.ts`): version `^\d{1,9}\.\d{1,9}\.\d{1,9}$`; 2,500..10,000
valid rows; 1..12 rewards; chance in `(0,100]`; rarity `Common|Uncommon|Rare`; reward name 1..64
chars; at most 2,000 distinct reward names; strings at most 256; body at most 20 MiB. Relic
identity: `uniqueName` starts `/Lotus/Types/Game/Projections/`, name matches
`^(Lith|Meso|Neo|Axi|Requiem|Vanguard) \S+ (Intact|Exceptional|Flawless|Radiant)$` and the
uniqueName ends with `Bronze|Silver|Gold|Platinum` respectively. Invalid rows are dropped; a bad
version or row count rejects the whole document.

Update algorithm (`relicDataUpdate.ts:244-354`):
1. 5 s after startup, read `userData/wfcd-relics.json` = `{"etag": str|null, "doc": doc|null}`;
   apply it if its version has the same major as and is newer than the bundled `@wfcd/items`
   version, else rewrite as `{etag, doc:null}`.
2. `GET /v1/wfcd-relics` with `If-None-Match: <etag>`, timeout 60 s (covers body). 304: nothing.
   404: revert to bundled data and clear the cache. Other errors: keep what is applied.
3. On 200: validate; if same version as applied just update the etag; if not newer than bundled,
   revert and store `{etag, doc:null}`; else store and apply, then rebuild relic-dependent indexes.
4. Repeat every 6 h.

The bundled document is about 4.9 MB for `@wfcd/items 1.1276.6` with 3,196 rows.

### 2.6 Price lookup order in the client (`src/lib/wfm/wfmPrice.ts:211-349`)

1. Normalise slug; return `no_slug` for excluded slugs.
2. Memory cache hit (ok, or no-data unless `ignoreNoDataCache`) -> return.
3. Slug in backend-error cooldown (60 s, `WFM_BACKEND_ERROR_COOLDOWN_MS`) -> `transient`.
4. Single-flight per cache key.
5. `GET /v1/prices/{slug}[?rank]`: ok -> cache; 404 -> cache no-data.
6. On error: direct fallback allowed for priority `high` only (`VITE_WFM_BACKEND_DIRECT_FALLBACK`,
   default and release value `high`); otherwise set the 60 s cooldown.
7. Direct `GET /v1/items/{slug}/statistics` and the 1.4.1 algorithm.

By name (`fetchPriceByName`): look up the lowercase name in the WFM catalog map, then
`"<name> set"` for names ending `set`, else fold the name with `normalizeForSlug`; optionally try
`<slug>_set` first.

The main process has its own minimal cache for overlay prices (`services/wfmStatsPrice.ts`): direct
statistics call through the scheduler, memory TTL 5 min (`WFM_STATS_CACHE_TTL_MS`), 5,000-entry
FIFO. In the plugin, merge both into one Python price service: snapshot -> backend -> direct.

### 2.7 Other renderer-held backend documents

All stored in browser `localStorage` in WFHelper; in the plugin store them as JSON files in the
runtime dir.

| Doc | Key | Revalidate | Max age |
|---|---|---|---|
| top-traded | (topTraded.ts) | 1 h, `If-None-Match` | 24 h |
| adversary vendors | `wf_adversary_vendors_v1` | 1 h | 24 h |
| nightwave offerings | `wf_nightwave_offerings_v1` | 1 h | 30 days |
| supporters | `wf_supporters_cache` | 24 h | |

---

## 3. World state

Main process: `ipc/worldStateIpc.ts`, `services/worldStateParser.ts`, `services/worldStateFetch.ts`,
`services/worldStateSteelPath.ts`, `services/regionNames.ts`, constants in
`config/runtime/worldState.ts`.

### 3.1 URLs and timeouts (`config/runtime/worldState.ts`, verbatim)

```ts
fetchUrls: ["https://api.warframe.com/cdn/worldState.php",
            "https://content.warframe.com/dynamic/worldState.php"],
oracleWorldStateUrl: "https://oracle.browse.wf/worldState.json",
oracleBountyCycleUrl: "https://oracle.browse.wf/bounty-cycle",
earthCycleUrl: "https://api.warframestat.us/pc",
warframestatBaseUrl: "https://api.warframestat.us/pc",
fetchTimeoutMs: 20_000,
cycleFetchTimeoutMs: 4_000,
earthCycleFetchTimeoutMs: 4_000,
vallisEpochIso: "November 10, 2018 08:13:48 UTC",
vallisPeriodMs: 1_600_000,
vallisWarmMs: 400_000,
poeNightMs: 3_000_000,
duviriMoodPeriodMs: 7_200_000,
duviriMoods: ["Sorrow", "Fear", "Joy", "Anger", "Envy"],
```

All requests send only `Accept: application/json`. The worker docs note that
`content.warframe.com/dynamic/worldState.php` is 404 everywhere and `api.warframe.com` 403s
Cloudflare IPs but works from a home connection (`ARCHITECTURE.md`, Baro section).

### 3.2 Cadence (`ipc/worldStateIpc.ts:34,667-814`)

- In-memory cache TTL 90 s; IPC `DB_GET_WORLD_STATE` returns the cache if younger, else refreshes.
- Startup seed 3 s after registration; background refresh every 60 s; a 15 s timer re-checks
  cycle-transition notifications against the cache.
- Single-flight refresh. If the refresh fails and nothing is cached, return `emptyWorldState()`
  and broadcast `WORLD_STATE_FETCH_ERROR`.
- No disk cache.

### 3.3 Fetch and merge (`worldStateParser.ts:885-1026`)

1. Try each `fetchUrls` entry in order; accept a JSON object having any of `ActiveMissions`,
   `VoidStorms`, `Invasions`, `VoidTraders`, `SyndicateMissions`, `EndlessXpSchedule`.
2. If both fail, use the browse.wf oracle world state (same DE format). If that fails too, throw.
3. `parseRaw(raw)` (3.4).
4. In parallel:
   - **Cycles** (`fetchAndComputeCycles`, `825-883`): Vallis and Duviri mood by math; oracle
     `bounty-cycle` (gives `expiry` ms, `rot`, `bounties`) -> Cetus/Cambion; earth from
     `api.warframestat.us/pc` (`earthCycle.isDay/expiry`); Duviri circuit choices from
     `/pc/duviriCycle` only if the raw world state had none. Earth falls back to Cetus.
   - **Extras** (`fetchWarframestatExtras`, `629-701`): `/pc/invasions` and `/pc/syndicateMissions`.
5. Merge: raw fields win; invasions from raw, else warframestat; bounties keyed by syndicate: raw
   first, then warframestat (skip display names raw already covers), then oracle seed bounties.
   `steelPath` always recomputed locally.

### 3.4 Cycle math (verbatim logic)

```
deDate(o) = ISO(int(o["$date"]["$numberLong"]))              # all DE dates

Vallis:  elapsed = (now - Date("2018-11-10T08:13:48Z")) % 1_600_000
         isWarm = elapsed < 400_000
         expiry = now + (400_000 - elapsed if isWarm else 1_600_000 - elapsed)

Cetus/Cambion (from oracle bounty-cycle expiry E):
         nightStart = E - 3_000_000
         isDay = now < nightStart
         expiry = nightStart if isDay else E
         cambion.active = "fass" if isDay else "vome"

Duviri mood: i = trunc(now / 7_200_000); state = MOODS[i % 5]; next = MOODS[(i+1) % 5];
             expiry = (i+1) * 7_200_000

Steel Path honors (worldStateSteelPath.ts):
         epoch 2020-11-16T00:00:00Z; ind = floor(((now-epoch)/1000 % 4_838_400) / 604_800)
         rotation[ind] from the 8-entry table; week = Monday 00:00 UTC .. Sunday 23:59:59 UTC
```

Steel Path rotation and evergreen tables: copy verbatim from `services/worldStateSteelPath.ts:5-30`.

### 3.5 `parseRaw` output (`worldStateParser.ts:1155-1437`)

| Key | Source field | Notes |
|---|---|---|
| `fissures[]` | `ActiveMissions` with `Modifier` in `VoidT1..VoidT6[Hard]` + `VoidStorms` (`ActiveMissionTier`) | `{expiry, tier, missionType, node, nodeId, isHard, expired, isStorm?}`; tiers T1 Lith, T2 Meso, T3 Neo, T4 Axi, T5 Requiem, T6 Omnia; drop expired |
| `voidTrader` | `VoidTraders[0]` | `{activation, expiry, location, inventory:[{uniqueName, item, ducats?, credits?, imageOverride}]}`; skip `BaroTreasureBox` items; `uniqueName = storeItemPath(ItemType)` |
| `vaultTrader` | `PrimeVaultTraders[0]` | Varzia |
| `sortie` | active `Sorties` | `{id, activation, expiry, boss, missions:[{node, mission, modifier}]}`; modifier labels table `worldStateParser.ts:243-277` |
| `archonHunt` | active `LiteSorties` | |
| `nightwave` | `SeasonInfo` | challenges with expiry 0 or in the future |
| `alerts` | `Alerts` not expired | |
| `descents`, `duviriCycle.choices` | `Descents`, `EndlessXpSchedule` (`EXC_NORMAL`->normal, `EXC_HARD`->hard; split camelCase) | |
| `bounties` | `SyndicateMissions` with tag `CetusSyndicate`, `SolarisSyndicate`, `EntratiSyndicate` | jobs `{type, enemyLevels, standingStages, minMR}` |
| `invasions` | `Invasions` not completed | completion `round(Count/Goal*1000)/10` clamped 0..100 |
| `calendarSeason` | active `KnownCalendarSeasons` | 1999 calendar |
| `dailyDeals` | `DailyDeals` not expired | Darvo |
| `steelPath` | computed | |
| cycles | null here, filled by 3.3 step 4 | |

Name resolution needs bundled data: node names from PEP `ExportRegions` + `dict.en.json`
("Node, System"); mission types via `ExportMissionTypes`; item names via the PEP export tables
listed at `worldStateParser.ts:118-128`, with recipe -> `"<result> Blueprint"`, relic ->
`"<era> <category> Relic"`, else a prettified path tail (`AvatarImage...` becomes `Glyph ...`).
Dict values have `<UPPER_TAGS>` stripped. Bounty level/standing tiers for Zariman, Cavia and Hex
are hard-coded at `worldStateParser.ts:530-590`. Bounty reward tables come from
`https://drops.warframestat.us/data/{cetusBountyRewards|solarisBountyRewards|deimosRewards|zarimanRewards|hexRewards|entratiLabRewards}.json`
(`src/lib/bountyRewards.ts:7,57-64,137-166`, timeout 15 s, cached per file per session).

### 3.6 Windows-only parts

Desktop toasts with an AUMID Start Menu shortcut (`worldStateIpc.ts:154-210`) are Windows-only.
Linux uses Electron `Notification`. Decky: use `decky.toaster` from the frontend for cycle alerts.

---

## 4. Item database

### 4.1 Bundled packages (`services/bundledGameData.ts`)

| Package | Version range | Files read |
|---|---|---|
| `warframe-public-export-plus` (PEP, calamity-inc) | `^0.6.8` | `Export<Name>.json` (regex `^Export[A-Za-z]+$`), `dict.<xx>.json` (regex `^[a-z]{2}$`). All 15 dicts together are 62.6 MB, 4-6 MB each |
| `@wfcd/items` | `^1.1276.6` | `data/json/<Category>.json`, `package.json` (version) |

WFHelper reads single JSON files, not package entry points, to avoid loading everything
(`bundledGameData.ts:1-3`). Not shipped: WFCD `i18n.json`, `i18n/`, and categories `Enemy, Glyphs,
Node, Quests, Railjack, SentinelWeapons, Sigils, Skins` (`package.json` build files).

WFCD component refs (`{uniqueName, itemCount}` since 1.1276) are expanded against a catalog of all
shipped categories, `Components.json` first (`bundledGameData.ts:155-198`). Result order:
categories in request order, `Components` last, then sorted by `name`, then `uniqueName`.

**Decky:** ship the PEP export files the app uses, `dict.en.json` (and other dicts only if
localisation is wanted), and the WFCD category files under `defaults/data/` in the plugin zip; or
download the npm tarballs on first run
(`https://registry.npmjs.org/<pkg>/-/<name>-<version>.tgz`). Load JSON lazily in the worker
process; on a 16 GB Deck memory is fine but parse time matters (seconds per large file).

### 4.2 DE Public Export overlay (`services/publicExportSource.ts`)

Purpose: fill in items newer than the bundled PEP package.

1. `GET https://content.warframe.com/PublicExport/index_en.txt.lzma` (60 s timeout, follow
   redirects). It is **LZMA-alone** (`.lzma`) format: Python `lzma.decompress(data, format=lzma.FORMAT_ALONE)`.
   Each line is `<Base>_en.json!<hash>`; map base name -> whole line (`publicExportSource.ts:170-187`).
2. Manifests used (`publicExportSource.ts:69-94`): `ExportWarframes_en.json`, `ExportWeapons_en.json`
   (also feeds `ExportRailjackWeapons`), `ExportSentinels_en.json`, `ExportUpgrades_en.json`,
   `ExportResources_en.json`, `ExportRelicArcane_en.json` (items with `relicRewards` go to
   ExportRelics, others to ExportArcanes), `ExportRecipes_en.json`, `ExportGear_en.json`,
   `ExportKeys_en.json`, `ExportDrones_en.json`, `ExportFusionBundles_en.json`,
   `ExportCustoms_en.json`, `ExportFlavour_en.json`. Fetch
   `https://content.warframe.com/PublicExport/Manifest/<whole index line>` (the `!hash` stays in the URL).
3. Before `json.loads`, replace control chars `[\x00-\x08\x0b\x0c\x0e-\x1f]` with a space (DE leaves them in).
4. Skip a manifest whose hashed name equals the cached one (reuse cached entries). Keep only items
   whose `uniqueName` is **not** in the bundled PEP table (gap-fill). Trim each item to
   `uniqueName, name, description, icon, masteryReq, primeSellingPrice, tradable, vaulted,
   productCategory, parentName, era, category, defaultWeapon, excludeFromCodex, codexSecret,
   resultType, buildPrice, buildTime, num, consumeOnUse, ingredients`.
5. Image manifest `ExportManifest.json` (several MB, about 20k rows of
   `{uniqueName, textureLocation}`): fetched when its hash changed or anything changed; keep only
   overlay items; set `icon = textureLocation.split("!")[0]` where missing.
6. Write `userData/public-export-cache.json` =
   `{"updatedAt": ISO, "index": {file: hashedName}, "exports": {ExportX: {uniqueName: item}}, "imagesIndex": str, "images": {uniqueName: texture}}`
   when something changed, a manifest disappeared, or there was no cache.
7. On failure use the cached overlay, else bundled data only.

Cadence: once per app start, in the background after the initial build; if `changed`, rebuild the
item DB and broadcast `ITEM_DB_UPDATED` (`main.ts:597-614`). Note: the DE export names are English
strings already (the `_en` manifests), not `/Lotus/Language` keys.

### 4.3 Database build (`services/itemDatabase.ts:882-914`)

Order:
1. `loadPublicExportPlus` (`241-373`): for each of the 16 export tables below, merge
   `{...overlay, ...bundled}` (bundled wins) and create entries.

   | Export | Category |
   |---|---|
   | ExportWarframes | Warframe |
   | ExportWeapons | Weapon |
   | ExportSentinels | Companion |
   | ExportResources | Resource (VoidProjection paths only in the overlay become Relic) |
   | ExportKeys | Key |
   | ExportRecipes | Recipe |
   | ExportGear | Gear |
   | ExportArcanes | Arcane |
   | ExportUpgrades | Mod |
   | ExportMisc | Misc |
   | ExportRelics | Relic |
   | ExportRailjackWeapons | Railjack |
   | ExportFusionBundles | Fusion |
   | ExportCustoms | Cosmetic |
   | ExportFlavour | Cosmetic |
   | ExportDrones | Gear |

   Name: relics `"<era> <category> Relic"`; recipes without a name `"<result name> Blueprint"`;
   else `dict.en[item.name]` if the name starts with `/`, else the literal; fallback from the
   uniqueName. `nameKey` keeps the `/Lotus/Language/...` key for localisation. `ducats =
   round(primeSellingPrice)`. Icon source `https://browse.wf` + `icon`.
2. `buildRecipeIndex` (`717-780`): `resultType -> {buildPrice, buildTime, num, ingredients, ...}`,
   blueprint -> result, reusable blueprints (`consumeOnUse == false`), market credit prices,
   dojo research blueprints.
3. `loadWfcdItems` (`375-655`): categories `Warframes, Primary, Secondary, Melee, Sentinels, Pets,
   Archwing, Arch-Gun, Arch-Melee, Mods, Resources, Misc, Relics, Fish, Gear, Arcanes`. WFCD adds
   items PEP lacks and supplements existing ones with `vaulted` (WFCD is the only vault source),
   `drops`, `components`, `wikiaUrl`, `exalted`, `masterable`, Incarnon tag, `ducats`, and better
   names for fallback-named entries. Components become their own entries
   (`"<Parent> <Component>"`, with tradability inheritance rules at `114-132`). Relic drop rarities
   are corrected first (`correctDropRarities`, `services/relicRarity.ts`).
4. Vault/tradable fix-ups, blueprint display inheritance, acquisition sources, then
   `resolveAllImages` (`656-715`).

Images: every external image URL goes through the mirror, `toIconMirrorUrl(url)`
(`itemDatabase.ts:39-55`):

```
https://assets.wfhelper.com/icons/<sha256(url).hex[:24]><ext or .png>
```
where `<ext>` is the lowercase path extension if it is at most 8 chars. Priority per item: wiki card
art (mods/arcanes and `WIKI_ITEM_ART` overrides:
`https://assets.wfhelper.com/item-art/<stem>.webp`, `.../mod-art/<stem>.webp`), then browse.wf
icon, then WFCD `wikiaThumbnail`, then `https://cdn.warframestat.us/img/<imageName>`. WFCD's
`blueprint.png` placeholder is skipped for components.

**Decky:** the mirror needs no auth; load images in the React frontend via `<img src>`. For
Pillow overlays, download through the backend and cache under `runtime/icons/<hash>.<ext>`.

Renderer projection (`getRendererLookup`, `itemDatabase.ts:1099-1149`): dictionary keyed by
uniqueName with `name, displayName?, nameIsFallback?, category, imageUrl, cardArt?, isPrime,
tradable, masteryReq, vaulted, exalted, incarnon?, masterable, type, isBuildComponent,
componentOf?, description, productCategory, ducats, components[{name, uniqueName, tradable,
itemCount, drops}], drops (first 20), wikiaUrl, recipe?, reusableBlueprint?, buildsProduct?`.
Built once per database build and game locale. It is about 20k entries; do not ship all of it to the
QAM in one call; page or query by key instead.

Name/slug lookup (`lookupItemByNameOrSlug`, `984-1004`): exact lowercase name or
`normalizeWfmSlug(name)`; prefer the earliest entry that has part metadata (`componentOf`, ducats,
or `isBuildComponent`).

### 4.4 Drop tables (`services/dropData.ts`)

- `GET https://drops.warframestat.us/data/info.json` -> `{"hash": "..."}`; if the hash equals the
  loaded one, stop. Else `GET .../data/all.json` (tens of MB), flatten, write
  `userData/drop-data-cache.json` = `{"version":2,"hash","updatedAt","rows":[{item, place, rarity, chance, kind}]}`.
  No timeout is set on these fetches; add one (60 s) in the port.
- Cadence: load disk cache at startup, then refresh once in the background (`main.ts:616-620`).
- Flatten rules (`dropData.ts:155-222`):
  - `missionRewards[planet][node].rewards` (list or `{rotation: list}`) -> place `"<node> (<planet>)[, Rotation X][ (stage)]"`, kind `mission`.
  - `relics` with `state == "Intact"` only -> place `"<tier> <relicName> Relic"`, kind `relic`.
  - `transientRewards` (`objectiveName`), `sortieRewards` (`Sortie`), `keyRewards` (quest),
    bounty keys `cetusBountyRewards, solarisBountyRewards, deimosRewards, zarimanRewards,
    entratiLabRewards, hexRewards` (place `bountyLevel`), kind accordingly.
  - `modLocations`, `blueprintLocations` (item -> enemies), `enemyModTables`,
    `enemyBlueprintTables`, `resourceByAvatar`, `sigilByAvatar`, `additionalItemByAvatar` -> kind `enemy`.
  - `syndicates` -> place from the reward's own `place`, kind `syndicate`.
  - Reward name `itemName || item || modName`; chance number or 0; rarity corrected.
  - Deduplicate on `item|place|rarity|chance`, enemy rows win ties.
- Extra rows: bundled `src/data/dojoResearch.json` (`{"entries":[{"item","lab"}]}` -> rarity
  Common, chance 100, kind `dojo`) and one hard-coded quest row (Helminth Archon Shard Segment
  Blueprint from Veilbreaker).
- Search: substring, ranked prefix (0) > word start (1) > contains (2), then chance desc, item,
  place; limit 300. `dropsForItem(name)` also matches stack prefixes `^\d+x `.

### 4.5 Other bundled data in `src/data`

`dojoResearch.json` (above), `syndicateRanks.json`, `petTraits.json`, `codexScanRequirements.json`,
`arbiMinimaps.json`: static tables, copy as-is when the consuming feature is ported.

---

## 5. Item name localisation

- Files: `src/data/itemNames/<code>.json` for `en de es fr it ja ko pl pt ru tc th tr uk zh`
  (`services/gameLocale.ts:13-29`). Each is a flat `{"/Lotus/Language/...": "Localized name"}`
  map (for example `de.json` has 5,969 keys; files are 340-715 KB, 7.6 MB in total).
- Built by `scripts/i18n/build-item-names.mjs` from PEP `dict.<lang>.json`, keeping only
  `/Lotus/Language/` values in fields `name, systemName, missionName, stateName, locTag,
  prefixTag, suffixTag, title, tag` of `ExportUpgrades, ExportArcanes, ExportWarframes,
  ExportSentinels, ExportWeapons, ExportRailjackWeapons, ExportResources, ExportRelics, ExportKeys,
  ExportGear, ExportDrones, ExportFusionBundles, ExportRegions, ExportMissionTypes`, plus
  `/Lotus/Language/Items/BlueprintAndItem` (the per-language blueprint pattern).
- `en` loads nothing: English is canonical. English stays the join key for WFM, OCR and every
  lookup; localisation only adds a `displayName` field (`itemDatabase.ts:1066-1078`).
- `localizeName(key, english)` = `table[key] or english`. Blueprints:
  `localizeName("/Lotus/Language/Items/BlueprintAndItem", "|ITEM| Blueprint").replace("|ITEM|", localizedResultName)`
  (`itemDatabase.ts:1050-1064`). Node names use `localizedDictValue`, which needs the full PEP dict
  for that language (`services/regionNames.ts:65-95`).
- The locale is a user setting (`auto` follows UI language, else one of the 15 codes; stored in
  renderer `localStorage["game-language"]`, `src/lib/gameLanguage.ts`). Changing it re-pulls the
  renderer lookup; the DB is not rebuilt.

**Decky:** the target is an English game UI, so this can be deferred. If ported, read the
Steam/Warframe language from `EE.cfg` or a plugin setting and copy the JSON files verbatim.

---

## 6. Smaller sources

| Source | URL | Cadence / cache | Source |
|---|---|---|---|
| Riven good rolls | `https://docs.google.com/spreadsheets/d/1LJ83e4x_xIVgjZg049PKrDgTex2MIuaiX0FFZi9mH-Q/export?format=csv&gid=<gid>` for gids `0` primary, `1505239276` secondary, `1413904270` melee, `289737427` archgun, `965095749` robotic | fetched at startup; cache `userData/riven-good-rolls-cache.json`, max age 7 days, refetch cooldown 10 min, timeout 20 s | `services/rivenBestAttributes.ts:27-29,111-125`, `config/shared/rivenGoodRolls.ts:40-48` |
| Riven weapons | `GET /v2/riven/weapons` -> `data[].slug`, `i18n.en.name` | `userData/wfm-riven-items.json` `{fetchedAt, items:{slug:name}}`, TTL 24 h | `services/wfmRivenItems.ts` |
| Riven auction search | 1.4 | memory, 10 min TTL, 50 entries; full coverage splits by polarity `madurai|naramon|vazarin` x sort when the quick query returns 490+ | `services/wfmRivenSearch.ts:27-28,82,160-195` |
| Arbitration schedule | `https://browse.wf/arbys.txt`, lines `<epochSeconds>,<nodeId>` | hourly, stale after 55 min, first fetch 5 s after start; `userData/arbi-sched-cache.json` | `services/arbiSchedule.ts:23-35,65-80` (see `arbitration.md`) |
| Arbitration minimaps | `https://assets.wfhelper.com/arbi-minimaps/` | images | `src/lib/arbi/arbiMinimap.ts:39` |
| Enemy images | `https://assets.wfhelper.com/enemies/` | images | `src/lib/codexScans.ts:44` |
| Profile (codex) | `https://api.warframe.com/cdn/getProfileViewingData.php?playerId=<accountId>` | on demand | `services/codexProfile.ts:360` |

Out of scope for this spec but present: auto-updater (GitHub releases atom), warframe-api-helper
download from GitHub (inventory; see `game-integration-linux.md`).

---

## 7. Startup sequence to reproduce (`main.ts:597-635`, `827`)

1. Load `public-export-cache.json`, build the item DB synchronously (seconds).
2. Background: DE public export refresh; rebuild the DB if changed.
3. Load `drop-data-cache.json`; background refresh from warframestat.
4. WFM catalog: backend `/v1/wfm-items`, fallback WFM `/v2/items` (background priority).
5. Riven good rolls.
6. 3 s: world state seed, then every 60 s.
7. 5 s: WFCD relic update, then every 6 h.
8. Renderer: snapshot load (disk if < 2 h old, else `/v1/snapshot`), top-traded, etc.
9. If a WFM session was restored: open the websocket listener and resync presence.

**Decky:** run steps 1-7 in the Python worker process after `_main`, never blocking the plugin
load. Only open WFM/world-state polling while the plugin or an overlay needs it, or Warframe runs,
to save battery: world state every 60 s is the only periodic network load.

---

## 8. Porting risks

- **R1. WFM bot protection.** WFHelper leans on Electron's Chromium TLS fingerprint and an
  interactive challenge window. Python `aiohttp`/`requests` has a non-browser fingerprint that WFM's
  Cloudflare can 403 on flagged IPs (`wfmClient.ts:341-346`). Header auth avoids CSRF, but reads can
  still be challenged. Mitigation: route most reads via the WFHelper backend, keep request volume
  low, and use the QAM's CEF `fetch` as an escape hatch if needed.
- **R2. WFHelper backend dependence.** Per-item routes require a bootstrap token bound to IP and
  User-Agent; `PUBLIC_CLIENT_POLICY` may move to `enforce` with an allow list of `WFHelper` only.
  The plugin must send a stable UA and its own product name and should degrade to direct WFM.
  Ask the upstream author for allow-listing. Do not impersonate `WFHelper`.
- **R3. Session secret storage.** No keyring in game mode; store the JWT root-only (`0600`) and handle
  token rotation (the `Authorization` response header) or sessions silently expire.
- **R4. Data volume.** Bundled PEP plus WFCD plus drops (`all.json`) is tens of MB of JSON. Parsing
  on the Deck's CPU takes seconds; do it in the worker process at low priority, cache derived
  indexes (for example pickle or SQLite) keyed by package version plus overlay hash.
- **R5. Upstream API churn.** WFM is mid v1 -> v2 migration (v1 `/riven/items` already 404; order
  creation field drift `perTrade`). Keep the probe-and-remember fallbacks (contracts endpoint list,
  perTrade flip, v2 -> v1 order book).
- **R6. World-state host blocking.** `api.warframe.com` blocks some IP ranges; keep the oracle
  fallback and tolerate warframestat being down (cycles degrade to math).

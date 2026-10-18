Recorded fixtures for tests/test_market.py and tests/test_data.py.

- `wfm/`: warframe.market payloads written by hand from the documented v2/v1 shapes
  (docs/port/data-sources.md §1.4, WFHelper config/shared/wfmOrders.ts and wfmStats.ts),
  because api.warframe.market is not reachable from the build sandbox. Statistics rows are
  dated around 2026-10-17T12:00Z; tests pin the clock there. `top_serration_mixed.json` is a
  `/top` answer without `?rank=` for a rankable mod (R0, R5 and R10 listings mixed);
  `top_lith_a1_relic.json` mixes intact, subtype-less and radiant relic orders.
- `wfcd/`: a subset of @wfcd/items 1.1276.18 `data/json/*.json` (MIT, WFCD) for five relics
  (Lith A1, Meso F2, Neo R1, Axi C12 = Citrine with drops, Requiem I) and the items they
  reference; bulky fields (patchlogs, abilities, drops beyond 2 rows) stripped.

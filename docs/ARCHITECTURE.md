# Architecture

## Goals

1. **Barcode first.** The GTIN/EAN is the only reliable key to say "this Atacadão
   product is the same as that Carrefour product". Every design choice serves
   getting a valid barcode for as many offers as possible, as cheaply as possible.
2. **Fast and boring.** Plain HTTP, JSON APIs where they exist, no headless
   browser in the daily path, no AI, no shared database. One market = one
   process = one Neon database.
3. **Cheap to operate.** Public GitHub repos (free Actions minutes), Neon free
   tier (one project per market, well under 500 MB), no paid services.

## Data flow

```
config.STORES ─► main.py ─► subprocess: python -m markets.<key>.scraper_<key>
                                   │
                                   ├─ resolve store for the CEP  ─► store_info
                                   ├─ walk the catalogue         ─► db.save(offers)  ─► offers (upsert)
                                   │                                              └─► price_history (trigger)
                                   ├─ seed barcodes from offers_legacy (if present)
                                   ├─ enrich(): fetch barcode only for rows still NULL ─► offers.barcode
                                   └─ print coverage
```

`markets/common/runner.py` provides the CLI for every scraper
(`--limit --zip --env --csv --workers --skip-enrich --enrich-only`) and exits
non-zero when nothing was saved so a broken site turns the GitHub run red.

## Shared toolkit (`markets/common/`)

| module | what it gives |
|--------|---------------|
| `http.py` | `make_session()` with browser headers, `request_with_retry()` (429/5xx backoff, Retry-After), `get_json/post_json`, `ThreadSessions` (thread-local sessions), `looks_like_challenge()` (Cloudflare pages) |
| `gtin.py` | `normalize_gtin()` canonical form + check digit, `first_gtin()`, `gtin_from_text()` (image names, paths) |
| `geo.py` | ZIP helpers, ViaCEP (`zip_info`), BrasilAPI/Nominatim (`zip_coords`), haversine |
| `offer.py` | `make_offer()` - the single builder of the offer record; price rules; `norm_name()` |
| `vtex.py` | VTEX catalog client: tree, segments under the 2500 cap, pagination, batch lookup by productId, `vtex_offer()` |
| `vipcommerce.py` | VipCommerce client (Rossi, Davo): login, delivery store, departments, products |
| `applay.py` | applay client (X, Barbosa): token, AES payloads, session, SKU-exclusion pagination |
| `gpa.py` | GPA/Linx client (Extra, Pão de Açúcar) + PDP barcode enrichment |
| `enrich.py` | generic page-based barcode backfill driver + JSON-LD / key extractors |
| `runner.py` | scraper CLI boilerplate |

## Database (per market, Neon PostgreSQL)

```
offers                PK (store_id, product_id)
  market, product_name, brand, category_path
  barcode, barcode_source        -- inline | image | pdp | catalog | legacy | crossfill
  regular_price, promo_price, discount_pct, promo_min_quantity, promo_end_at
  offer_tag, offer_name, app_membership_required
  unit, is_available, stock, product_url, image_url
  scraped_at, updated_at
price_history         append-only; trigger trg_price_history on INSERT / price-changing UPDATE
store_info            one row per store_id (name, address, coords, the CEP that selected it)
barcode_enrich_state  (store_id, product_id) -> found / not_found / error + attempts + timestamp
offers_legacy, price_history_legacy   (renamed old tables, optional, drop when done)
```

Rules enforced by `db_manager.py`:

* `save()` is a pure upsert. `barcode` is only ever filled, never cleared
  (`COALESCE(EXCLUDED.barcode, offers.barcode)`); brand/url/image likewise keep
  the old value when the new scrape has none.
* Rows without a positive `regular_price` are skipped (no price = no offer).
* `promo_price` is stored only when strictly lower than `regular_price`
  (`make_offer()`), `discount_pct` is derived.
* `promo_end_at` decades in the future (cache TTLs some sites send) is dropped.
* Connection uses `prepare_threshold=None` so Neon's transaction pooler works.

Why `(store_id, product_id)` and not the old `market_storehash_barcode` id: the
old id changed when a barcode was learned later (silent duplicates) and it
could not represent a product without barcode cleanly. The native product id is
stable; the barcode is a column, indexed, and the matching key across markets.

## What was removed and why

| old piece | why it is gone |
|-----------|----------------|
| manager DB (Supabase) with `product_catalog`, `known_barcodes`, `match_audit`, `process_timing`, ... | shared-table contention, egress cost, a second provider to babysit. Each market DB is self-contained; the combined view is an export/`sync-hub`. |
| `barcode_ai_matcher.py` (LLM + embeddings + CLIP) | 3h+ per run for a few % of extra matches, with false positives. Replaced by finding real barcode sources per market and a deterministic exact-name cross-fill. |
| Playwright in the daily path (Rossi, Oba fallback, X token) | slow, flaky on CI. Every market now has a plain-HTTP route; Playwright stays only as an optional last-resort token rediscovery. |
| tiers / catalog sync ordering | no cross-market dependency any more, so every market runs fully in parallel. |
| cart builder, HF Space, storage controller, Supabase bootstrap, ML export | out of scope for a scraper repo. |

## Adding a market

1. Find the data source (see docs/LESSONS.md for the reconnaissance recipe).
2. Create `markets/<key>/scraper_<key>.py` exposing `STORE_KEY`, `scrape(db, zip, limit)`
   and optionally `enrich(db, workers, limit)`; build rows with `make_offer()`.
3. Register it in `config.STORES` (db env var, cron, notes) and add
   `DATABASE_URL_<KEY>` to `.env` / `.env.template` / `.github/workflows/scrape.yml`.
4. `python -m markets.<key>.scraper_<key> --limit 50`, then `python deploy/deploy.py --stores <key>`.

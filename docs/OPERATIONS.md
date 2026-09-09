# Operations

## Daily runs (GitHub Actions, one public repo per market)

```bash
python deploy/deploy.py --dry-run        # see what would happen
python deploy/deploy.py                  # create/update every markets-db-<market> repo
python deploy/deploy.py --stores atacadao carrefour
python deploy/deploy.py --secrets-only   # re-push DATABASE_URL_* after rotating credentials
python deploy/deploy.py --force          # force-push code
```

Each market repo receives the code, a `scrape.yml` with the cron from
`config.STORES[key]["cron"]` (every 4 hours; the 16 markets start 15 minutes
apart so no two run at the same time - a full cycle is 6 runs/day per market) and the secrets
`STORE_KEY`, `DATABASE_URL_<MARKET>` and optionally `SCRAPE_ZIP_CODE`. The
workflow runs `python -m main --stores <key> --log --post` (scrape + inline
barcode enrichment + mark stale + prune) and uploads the logs as an artifact.

Repo names kept from the previous generation: `markets-db-oba-hortifruti`,
`markets-db-pao-de-acucar`, `markets-db-sams-club`, `markets-db-sonda-delivery`,
`markets-db-tenda-atacado`; all others are `markets-db-<key>`.

The master repo workflow `.github/workflows/scrape.yml` is a manual "run any
subset" button (needs every `DATABASE_URL_*` as a secret).

## Local runs

```bash
python -m main                                   # everything, parallel
python -m main --parallel 4                      # cap concurrency (slow laptop / Wi-Fi)
python -m main --only-stale --stale-hours 24     # refresh only what fell behind
python -m main --enrich-only                     # barcode backfill only
python -m main --stores nagumo --zip 02401-100   # another region for one market
python -m tools.crossfill_barcodes               # after the scrapes: name-based barcode fill
```

Windows Task Scheduler / cron friendly: every command exits non-zero on failure.

## Store selection (CEP)

`config.SCRAPE_ZIP_CODE` (default `08032-230`, zona leste SP) or `--zip`.
Markets with real per-store prices resolve the nearest store (Atacadão,
Carrefour, Tenda, Nagumo, Higas, X, Barbosa); national-price markets store a
descriptive `store_id` (Swift, Oba, Sam's Club, Giga, Extra, PdA, Sonda) and
VipCommerce markets always serve the delivery hub (Rossi, Davo). `store_info`
keeps what was selected for every `store_id`.

## Several stores per market (several CEPs)

Prices depend on the store for **Atacadão, Carrefour and Higas** only
(`config.STORES[..]["per_store"] = True`; measured 2026-09-09: 4-32% of prices differ
between Atacadão sellers, 13-42% between Carrefour stores). Give a comma-separated CEP
list and each of those markets is scraped once per CEP, each store under its own
`store_id`. The other thirteen markets have national prices - measured, not assumed:
X and Barbosa serve a single e-commerce store, Nagumo prices are identical in every
branch, Tenda has one price per product (only stock is per branch), Sam's Club and
Swift show 0 differences across CEPs, Extra/Pão de Açúcar answer only for one store id,
Rossi/Davo serve the delivery hub, Oba/Giga/Sonda take no CEP. They run once whatever
the list, under a constant `store_id` (`nagumo`, `swift`, `tenda`, `rossi:1:1`, ...).

```bash
python -m main --zip "08032-230,04646-000,02401-100,06290-170"   # leste, sul, norte, oeste
SCRAPE_ZIP_CODES=08032-230,04646-000 python -m main              # same, via env / GitHub secret
python -m markets.atacadao.scraper_atacadao --zip "08032-230,04646-000"
```

What happens with N stores:
* rows are keyed by `(store_id, product_id)`; `store_info.query_zip` says which CEP picked each store;
* barcodes are per product, so a barcode found for one store is copied to the same
  product in the other stores (`fill_barcodes_from_siblings`, instant) and page-based
  enrichment fetches each product only once;
* `mark-stale` works per store: a row is flipped only when its own store's latest run
  did not refresh it, so stores scraped on different days do not disturb each other;
* runtime grows linearly for the per-store markets (Atacadão ~6-9 min per store,
  Carrefour ~12, Higas ~25). With 4 CEPs the daily cycle
  is still well inside the 4-hour schedule slot for every market except Higas, whose
  API pacing makes it ~100 min - give it its own slot or fewer CEPs.
* `python -m db.db_manager stores <market>` lists the stores present;
  `drop-store <market> <store_id>` removes one.
* **Which stores exist?** `python -m tools.list_stores` writes `exports/stores_<market>.csv`
  with every store the market exposes and a CEP that selects it (2026-09: Atacadão 83
  sellers, Carrefour 132 pickup stores, Higas 6; Tenda's 41 branches only differ in stock). One CEP = one store
  per market, so pick the CEPs of the stores you want and put them in `SCRAPE_ZIP_CODES`.

## Maintenance

```bash
python -m db.db_manager stats                        # coverage, freshness, DB size
python -m db.db_manager mark-stale all --hours 48    # offers not seen for 48h -> is_available=false
python -m db.db_manager prune all --days 180         # trim price_history (Neon free tier = 500 MB)
python -m db.db_manager drop-legacy all              # drop offers_legacy tables when no longer needed
python -m db.db_manager stores nagumo                # store_ids in a market DB (rows, barcodes, last update)
python -m db.db_manager drop-store nagumo M_29       # remove a wrongly selected store (all its rows)
python -m tools.crossfill_barcodes --fuzzy 0.8       # name-based barcode fill incl. the Jaccard pass
python -m db.db_manager export atacadao              # offers + price_history CSV
python -m db.db_manager export-all-together          # one barcode-keyed CSV, all markets
python -m db.db_manager sync-hub                     # optional consolidated app_offers in DATABASE_URL_HUB
```

`--post` in `main.py` does mark-stale + prune for the markets that just ran.

## Troubleshooting

| symptom | what to check |
|---------|---------------|
| `DATABASE_URL_X is not set` | `.env` (locally) or the repo secret (Actions). Names are in `config.STORES`. |
| a market saves 0 offers and the run is red | the site changed. Run the scraper directly with `--limit 20`; compare with the endpoint notes in docs/MARKETS.md. |
| Rossi/Davo `login failed` | VipCommerce rotated the shared key: capture it (docs/LESSONS.md, "intercept") and set `VIPCOMMERCE_LOGIN_KEY` or `<MARKET>_API_TOKEN`. |
| X Supermercados `could not obtain an API token` | Next.js action id rotated; the client scans the page/chunks and, if Playwright is installed, the rendered page. Set `XSUPER_TOKEN_ACTION_ID` once found. |
| Higas `Acesso bloqueado` | the IP is banned for hours; the next scheduled run (another runner IP) recovers. Never test from a banned IP; use `--probe` on a runner. |
| Carrefour `captcha page` | pause is automatic; category HTML rarely gets challenged, GraphQL always does (that is why we parse HTML). |
| Extra/PdA `storeId ... empty` | the API silently answers nothing for an unknown storeId; the client falls back through `gpa_store_ids`. |
| Neon DB near 500 MB | `prune` more aggressively, `drop-legacy`, check `stats`. |
| GitHub run over 5h | see per-market runtime notes in docs/MARKETS.md; split with `--parallel` locally or reduce categories. |

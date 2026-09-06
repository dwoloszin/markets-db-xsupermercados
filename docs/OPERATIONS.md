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

Running the same market for two CEPs keeps both stores' rows (PK is
`(store_id, product_id)`).

## Maintenance

```bash
python -m db.db_manager stats                        # coverage, freshness, DB size
python -m db.db_manager mark-stale all --hours 48    # offers not seen for 48h -> is_available=false
python -m db.db_manager prune all --days 180         # trim price_history (Neon free tier = 500 MB)
python -m db.db_manager drop-legacy all              # drop offers_legacy tables when no longer needed
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
| Higas `blocked by Cloudflare challenge` | rate; re-run later or from another IP. The store endpoint (apiv3/store) still works. |
| Carrefour `captcha page` | pause is automatic; category HTML rarely gets challenged, GraphQL always does (that is why we parse HTML). |
| Extra/PdA `storeId ... empty` | the API silently answers nothing for an unknown storeId; the client falls back through `gpa_store_ids`. |
| Neon DB near 500 MB | `prune` more aggressively, `drop-legacy`, check `stats`. |
| GitHub run over 5h | see per-market runtime notes in docs/MARKETS.md; split with `--parallel` locally or reduce categories. |

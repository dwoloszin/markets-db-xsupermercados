# markets_db

Price + **barcode** tracking for Brazilian supermarkets. One scraper per market,
one Neon (PostgreSQL) database per market, automatic price history, and the
barcode (GTIN/EAN) as the key that links the same product across stores.

This is the supermarket sibling of `markets_db_farm` (pharmacies) and follows
the same architecture: no manager database, no AI matching, no Supabase.

| Market | Platform | Barcode source | Offers | Barcode | Run time |
|--------|----------|----------------|-------:|--------:|---------:|
| Atacadão | VTEX FastStore (GraphQL) | VTEX catalog batch lookup | 10,616 | 99.0% | 9 min |
| Barbosa | applay | inline (`material`/`sku`) | 7,178 | 94.7% | 5 min |
| Carrefour Mercado | VTEX FastStore (HTML) | PDP JSON-LD (+ image file name) | 9,948 | 94.8% | 12 min (+16 first enrichment) |
| Davo | VipCommerce | inline `codigo_barras` | 6,151 | 96.8% | 4 min |
| Extra | GPA / Linx API | PDP HTML `"ean"` | 13,813 | 100% | 8 min (+2 enrichment) |
| Giga | VTEX | inline `ean` | 7,363 | 96.2% | 9 min |
| Higas | Instabuy / ibecom v5 | legacy table + cross-market fill | 210* | 35% | ~18 min (paced) |
| Nagumo | Salesforce Commerce Cloud | legacy table + cross-market fill (site exposes none) | 14,405 | 23.3% | 15 min |
| Oba Hortifruti | VTEX | inline `ean` | 5,216 | 71.6% (produce) | 6 min |
| Pão de Açúcar | GPA / Linx API | PDP HTML `"ean"` | 12,053 | 99.2% | 9 min (+5 enrichment) |
| Rossi | VipCommerce | inline `codigo_barras` | 13,283 | 94.1% | 9 min |
| Sam's Club | VTEX | inline `ean` | 4,170 | 99.8% | 7 min |
| Sonda Delivery | ASP.NET HTML | image path + PDP JSON-LD | 9,440 | 97.5% | 19 min (+6 enrichment) |
| Swift | VTEX | inline `ean` | 990 | 100% | 1 min |
| Tenda Atacado | Stoom API | inline `barcode` | 9,322 | 99.6% | 7 min |
| X Supermercados | applay | inline (`material`/`sku`) | 5,040 | 90.7% | 3 min |

Whole set in parallel: **~25 min wall-clock** (the previous pipeline took 7h43m).

\* measured on the first runs after the refactor (2026-09-06, CEP 08032-230); see `python -m db.db_manager stats`.
  Higas: the run was cut short by its API ban ("Acesso bloqueado") - the paced version needs a run from an unblocked IP.

## Quick start

```bash
pip install -r requirements.txt
cp .env.template .env                # fill in DATABASE_URL_<MARKET> (one Neon project per market)

python -m main                       # scrape every market in parallel (default CEP in config.py)
python -m main --stores atacadao     # one market
python -m main --limit 100 --log     # quick test, per-market logs in logs/
python -m main --zip 04646-000       # another region
python -m main --post                # + mark stale offers unavailable, prune price history

python -m markets.atacadao.scraper_atacadao --limit 50   # run a scraper directly (verbose)

python -m db.db_manager stats                    # offers / barcode coverage / freshness per market
python -m db.db_manager export-all-together      # one barcode-keyed CSV with every market
python -m tools.crossfill_barcodes               # fill missing barcodes by exact name across markets
```

## How it works (one paragraph)

`main.py` starts one subprocess per market (`markets/<key>/scraper_<key>.py`).
Each scraper resolves the physical store for the configured CEP, walks the
market's catalogue through the fastest route we found (JSON API when there is
one, server-rendered HTML otherwise), normalises every product into the common
offer record (`markets/common/offer.py`) and upserts it into that market's
`offers` table. A trigger appends to `price_history` only when a price changes.
Markets whose listing has no barcode run an inline enrichment step right after
the scrape, fetching only the products that are still missing a barcode
(barcodes never change, so after the first run this is nearly free).

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) - layout, data flow, database schema, design decisions
- [docs/MARKETS.md](docs/MARKETS.md) - every market: endpoints, store resolution, barcode route, quirks
- [docs/BARCODES.md](docs/BARCODES.md) - why the barcode is the key, validation rules, enrichment, cross-market fill
- [docs/OPERATIONS.md](docs/OPERATIONS.md) - daily runs, GitHub Actions deploy, maintenance, troubleshooting
- [docs/LESSONS.md](docs/LESSONS.md) - what we learned the hard way (read before touching a scraper)

## Deployment

One public GitHub repo per market with its own cron (unlimited Actions minutes):

```bash
python deploy/deploy.py --dry-run
python deploy/deploy.py            # creates/updates markets-db-<market> repos + secrets + schedule
```

## Layout

```
main.py                 parallel runner (one process per market)
config.py               market registry (db env var, schedule, notes) + defaults
db/db_manager.py        StoreDB: schema, upsert, price-history trigger, barcode backfill, exports, CLI
markets/common/         shared toolkit: http, gtin, geo, offer, vtex, vipcommerce, applay, gpa, enrich, runner
markets/<market>/       scraper_<market>.py (+ enrichment inside when needed)
tools/crossfill_barcodes.py   deterministic cross-market barcode fill
deploy/                 deploy.py + store-scrape.yml (per-market workflow template)
docs/                   documentation
```

Legacy note: the previous generation (manager DB on Supabase, AI barcode
matching, tiers) lives in git history before commit `1d182ce`. Its `offers`
tables were renamed to `offers_legacy` in each Neon DB and their barcodes are
reused automatically; drop them with `python -m db.db_manager drop-legacy all`
once you are happy with the new data.

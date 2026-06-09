# Market Price Scraping System — How It Works

## Table of Contents
1. [System Overview](#system-overview)
2. [Phase 1 — Scraping](#phase-1--scraping)
3. [Phase 2 — Barcode Enrichment (Higas API)](#phase-2--barcode-enrichment-higas-api)
4. [Phase 3 — Barcode Inference Pipeline](#phase-3--barcode-inference-pipeline)
5. [Database Architecture](#database-architecture)
6. [Table Reference](#table-reference)
7. [How main.py Orchestrates Everything](#how-mainpy-orchestrates-everything)
8. [Parallel Machine Deployment](#parallel-machine-deployment)
9. [Environment Variables Reference](#environment-variables-reference)

---

## System Overview

The system scrapes product catalogs from 9 Brazilian supermarket chains, stores prices in PostgreSQL, and then runs a multi-stage barcode matching pipeline to link the same physical product across different markets. The end goal is a unified price comparison database keyed by barcode.

```
┌─────────────────────────────────────────────────────────────┐
│  9 SCRAPERS          SHARED DB (manager)   PER-MARKET DBs   │
│                                                             │
│  Rossi    ──────┐   store_mappings         Rossi DB         │
│  Higas    ──────┤   barcode_references  ◄──offers           │
│  Nagumo   ──────┤   barcode_ref_map        price_history    │
│  Atacadão ──────┼──►known_barcodes                          │
│  Sonda    ──────┤   barcode_inf_state   ◄──Higas DB         │
│  Swift    ──────┤   match_audit            offers           │
│  Barbosa  ──────┤   process_timing         price_history    │
│  Carrefour──────┤   product_catalog                         │
│  XSuper   ──────┘   higas_enrich_state  ◄──...other DBs     │
└─────────────────────────────────────────────────────────────┘
```

---

## Phase 1 — Scraping

### What each scraper does

Each `market_scrap_{market}_departamentos.py` file follows the same pattern:

```
1. resolve_store(zip_code)     → find the nearest branch for that ZIP
2. discover departments/slugs  → get list of categories to scrape
3. paginate each department    → collect raw product dicts
4. _standardize_product()      → normalize to the common schema
5. return List[Dict]           → back to main.py
```

### How each market scrapes

| Market | Method | Items/page | Notes |
|---|---|---|---|
| **Rossi** | Playwright (browser) intercepts XHR | ~40 | SPA — API auth handled by browser session |
| **Higas** | REST API (`/apiv3/offers`) | 30 | Instabuy platform; ignores category filter, global pagination |
| **Nagumo** | HTML scraping (SFCC) + JSON fallback | ~15 | Two strategies: HTML grid or `Search-UpdateGrid` JSON endpoint |
| **Atacadão** | GraphQL (`/api/graphql`) | 100 | VTEX GraphQL; prices in centavos (÷100 for BRL) |
| **Sonda** | HTML scraping (server-rendered) | ~15 | Products in `ViewItemAnalytics(price, sku, name)` JS calls |
| **Swift** | Remix context + `getMoreProducts` POST | ~12 | Extracts from `window.__remixContext`; fixed page cursor |
| **Barbosa/XSuper** | Encrypted REST API | variable | CryptoJS AES payload encryption; token via Next.js server action |
| **Carrefour** | HTML JSON-LD + VTEX API fallback | 21 (HTML) / 50 (API) | Tries VTEX catalog API first; falls back to HTML parsing |

### Store resolution

Before scraping, every scraper calls `resolve_store(zip_code)` to find the nearest physical branch. The approach varies:

- **VipCommerce stores (Rossi):** calls `/api-admin/v1/org/63/filial/1/loja/centros_distribuicoes/retiradas`, sorts candidates by haversine distance + ZIP proximity
- **Instabuy stores (Higas):** calls `/apiv3/store?partner_id=...&zip_code=...`
- **VTEX stores (Atacadão):** calls `/api/checkout/pub/regions?postalCode=...`
- **SFCC stores (Nagumo):** calls `StoreLocator-GetNearestStores?postalCode=...`
- **Swift:** derives a store key from ViaCEP city/state lookup + sets a `postalcode` cookie
- **Carrefour:** calls `/action/cep` then `/action/stores-from-pickups` then `/action/set-regionalization`
- **Barbosa/XSuper:** resolves via the Applay API session bootstrap which returns `loja` object

The resolved store ID is cached in `store_mappings` so subsequent runs skip re-resolution.

### The common product schema

Every scraper returns products in this exact shape (23 fields to match the DB INSERT):

```python
{
    "id":                   "rossi_7891234567890",  # market_prefix + barcode or native_id
    "product_name":         "ARROZ BRANCO TIPO 1 5KG",
    "brand":                "Camil",
    "description":          None,
    "regular_price":        22.90,
    "promo_price":          19.90,          # None if no promotion
    "promo_min_quantity":   2,              # None if no minimum
    "unit":                 "5kg",
    "gtin":                 "7891234567890", # raw barcode string from API
    "barcode":              "7891234567890", # normalized (validated GTIN only)
    "product_url":          "https://...",
    "image_url":            "https://...",
    "stock_balance":        None,           # current stock at store
    "stock_general":        None,           # general stock availability
    "sold_quantity":        None,
    "offer_name":           "Semana do Arroz",
    "offer_tag":            "PROMO",
    "app_membership_required": False,       # True for "Clube" prices
    "promo_end_at":         None,
    "last_updated":         "2025-03-20T14:30:00",
    "store_id":             "3",            # resolved branch ID
    "zip_code":             "08032-230",
}
```

### How `offer_id` is built

`db.build_offer_id(market_prefix, native_id, barcode, gtin)` creates the primary key:

```
If valid normalized barcode exists → "{market}_{barcode}"   e.g. "rossi_7891234567890"
Else if valid GTIN exists          → "{market}_{gtin}"
Else                               → "{market}_{native_id}" e.g. "higas_abc123"
```

This means the same physical product (same barcode) always gets the same ID regardless of how the market labels it internally — enabling cross-market deduplication.

### What main.py does after scraping

For each market run, `main.py` does two things with the returned list:

```python
# 1. Save to offers table (with price history tracking)
formatted_offers = self._format_offers_for_db(market_name, offers_data, zip_code)
self.db.save_offers(formatted_offers)

# 2. Save barcode cross-references
barcode_refs = self._collect_barcode_refs(market_name, offers_data)
self.db.save_barcode_references_bulk(barcode_refs)
```

---

## Phase 2 — Barcode Enrichment (Higas API)

Run by `higas_barcode_enrich.py`, triggered as `higas_enrich` mode or as part of `full_sequence_pipeline`.

### What it does

Many products scraped from HTML pages don't have barcodes (the barcode isn't exposed in the page's JSON-LD or analytics calls). This phase calls the **Higas Instabuy search API** using the product's SKU to try to retrieve the EAN/barcode:

```
GET /apiv3/search?search_barcode={sku}&store_id=...
```

It's called "Higas enrichment" but it actually enriches offers from **any market** — it uses Higas as a barcode lookup database because Instabuy exposes barcodes in their API response.

### State tracking

Uses `higas_barcode_enrich_state` table to avoid re-fetching barcodes already processed:
- Status: `found` / `not_found` / `error`
- Tracks last attempt time, HTTP status, hit count
- Runs in batches with burst control to respect rate limits (`HIGAS_ENRICHMENT_BURST_SIZE`, `HIGAS_ENRICHMENT_MAX_CALLS`)

### The loop in full_sequence_pipeline

```python
while rounds < 200:
    result = run_higas_barcode_enrich(max_calls=650, burst_size=140, ...)
    if result["completed"]:
        break
    if result["processed_in_run"] <= 0:
        break          # no more unprocessed barcodes
    time.sleep(30)     # cooldown between rounds
```

---

## Phase 3 — Barcode Inference Pipeline

Run by `BarcodeAIMatcher` in `barcode_ai_matcher.py`. This is the most sophisticated part of the system.

### Goal

For products that still have no barcode after scraping and Higas enrichment, try to infer the barcode by matching against products from other markets that DO have barcodes.

### Step 1: `sync_known_barcodes()`

Builds the reference catalog used for matching. Reads all offers that have a barcode from the 8 trusted source markets and upserts them into `known_barcodes` with normalized text for matching:

```
offers (with barcode) ──► known_barcodes
  - normalized_name    (accent-stripped, lowercase, stopwords removed)
  - normalized_brand
  - measure_token      (e.g. "5kg", "350ml", "12un" extracted from name)
```

### Step 2: `infer_missing_barcodes()`

Targets offers from `["Atacadão", "Nagumo", "Higas"]` (markets that tend to have incomplete barcodes) that have `barcode IS NULL`.

For each barcode-missing offer, the matching pipeline tries three strategies in order:

#### Strategy A — Heuristic matching (fast, no API calls)

Scores each candidate from `known_barcodes` using:

```
score = (0.55 × name_similarity)
      + (0.28 × token_overlap)
      + brand_bonus (0.18–0.22 if brands match, weighted by brand frequency)
      + measure_bonus (0.17 if same unit/weight)
      + measure_penalty (−0.12 if conflicting units)
```

If `score ≥ 0.96` AND the margin over the second-best candidate `≥ 0.08` → accept the match without any API call.

#### Strategy B — Embedding model (optional, local)

If `ENABLE_EMBEDDING_MODEL=1`, uses `intfloat/multilingual-e5-small` (or configured model) to compute semantic similarity between the target and top candidates. Enabled via `embedding_model_infer` mode or `run_embedding_model_inference_only()`.

#### Strategy C — AI matching (optional, cloud)

If `ENABLE_AI_BARCODE_MATCH=1` and an AI provider is configured (`HF_TOKEN`, `OPENROUTER_API_KEY`, or local LM Studio), sends the target product + top candidates to an LLM and asks it to select the matching barcode. Accepts if AI confidence `≥ 0.86`.

Providers tried in order: OpenRouter → HuggingFace Inference API → LM Studio (local).

### Skip logic (efficiency)

Before scoring, the pipeline checks `barcode_inference_state`:
- If the offer's text + catalog snapshot haven't changed since last attempt → **skip** (unchanged)
- If the offer has failed `≥ 5` times without a match → **blacklisted** (skip unless catalog changes)

This means the pipeline gets dramatically faster on subsequent runs — it only processes genuinely new or changed offers.

### When a match is accepted

```python
db.update_offer_barcode_if_null(offer_id, matched_barcode)  # writes to offers
db.save_barcode_reference(barcode, market, offer_id, ...)    # writes to barcode_references
db.upsert_match_audit(...)                                   # writes audit trail
```

---

## Database Architecture

### Two-tier PostgreSQL structure

```
DATABASE_URL_MANAGER  →  shared "manager" DB
                          - barcode_references
                          - barcode_reference_market_map
                          - store_mappings
                          - known_barcodes
                          - match_audit
                          - barcode_inference_state
                          - process_timing
                          - product_catalog
                          - higas_barcode_enrich_state

DATABASE_URL_ROSSI    →  Rossi-only DB        (offers + price_history)
DATABASE_URL_HIGAS    →  Higas-only DB        (offers + price_history)
DATABASE_URL_ATACADAO →  Atacadão-only DB     (offers + price_history)
... etc per market
```

If a market has no dedicated `DATABASE_URL_{MARKET}` set, it falls back to the manager DB.

---

## Table Reference

### `offers` (per-market DB)

The main product catalog. One row per unique offer (market + barcode, or market + native_id).

| Column | Type | Purpose |
|---|---|---|
| `id` | TEXT PK | `{market}_{barcode_or_native_id}` |
| `market_name` | TEXT | e.g. "Rossi", "Higas" |
| `product_name` | TEXT | Full product name |
| `brand` | TEXT | Brand name |
| `regular_price` | DOUBLE | List/regular price in BRL |
| `promo_price` | DOUBLE | Current sale price (= regular if no promo) |
| `promo_min_quantity` | INTEGER | Min qty for promo (null = no minimum) |
| `barcode` | TEXT | Normalized GTIN (8/12/13/14 digits, checksum valid) |
| `gtin` | TEXT | Raw barcode string from source |
| `store_id` | TEXT | Resolved branch/store identifier |
| `zip_code` | TEXT | ZIP code used for this scrape run |
| `last_updated` | TIMESTAMP | When this offer was last scraped |
| `app_membership_required` | BOOLEAN | True for club/app-exclusive prices |

**Unique index:** `(market_name, barcode)` where barcode IS NOT NULL — prevents duplicate entries for the same product.

---

### `price_history` (per-market DB)

Append-only log of price changes. A new row is written whenever `promo_price` changes from the previously stored value.

| Column | Purpose |
|---|---|
| `offer_id` | Links to `offers.id` |
| `market_name` | Market name |
| `regular_price` / `promo_price` | Prices at time of recording |
| `offer_name` / `offer_tag` | Promotion name/tag at time |
| `recorded_at` | When the price change was detected |

---

### `store_mappings` (manager DB)

Cache of ZIP code → store branch resolution. Prevents re-resolving the same ZIP on every run.

| Column | Purpose |
|---|---|
| `zip_code` + `market_name` | Composite PK |
| `store_id` | Resolved branch identifier |
| `store_name/address/city/state` | Human-readable store info |
| `latitude/longitude` | Coordinates for distance sorting |
| `store_payload` | Full raw API response (JSON) |
| `last_successful_update` | When this mapping was last confirmed |

---

### `barcode_references` (manager DB)

Legacy wide-format table with one column per market. Kept for backward compatibility. The newer `barcode_reference_market_map` is the authoritative source.

| Column | Purpose |
|---|---|
| `barcode` | PK — normalized GTIN |
| `rossi_id`, `higas_id`, `atacadao_id`... | Offer IDs per market |
| `product_name`, `brand` | From the first market that added this barcode |

---

### `barcode_reference_market_map` (manager DB)

The modern replacement for `barcode_references`. One row per (barcode, market) pair — cleaner and supports any number of markets without schema changes.

| Column | Purpose |
|---|---|
| `barcode` + `market_name` | Composite PK |
| `market_offer_id` | The offer ID in that market's DB |
| `product_name`, `brand` | Denormalized product info |
| `last_updated` | When this reference was last confirmed |

**This is the primary cross-market linking table.** Given a barcode, you can find all offer IDs across all markets.

---

### `known_barcodes` (manager DB)

The reference catalog used by the barcode inference pipeline. Contains normalized product text from all trusted source markets that have barcodes.

| Column | Purpose |
|---|---|
| `source_market` + `source_market_id` | Composite PK |
| `barcode` | The verified barcode |
| `normalized_name` | Accent-stripped lowercase product name |
| `normalized_brand` | Accent-stripped lowercase brand |
| `measure_token` | Extracted unit/weight e.g. "5kg", "350ml" |
| `last_updated` | When synced |

---

### `barcode_inference_state` (manager DB)

Tracks inference attempts per offer to enable efficient incremental re-runs.

| Column | Purpose |
|---|---|
| `offer_id` | PK — the target offer being matched |
| `offer_signature` | Hash of product name+brand+description at last attempt |
| `catalog_snapshot` | Hash of `known_barcodes` content at last attempt |
| `matched` | Whether a barcode was successfully inferred |
| `no_match_count` | How many consecutive failed attempts |
| `blacklisted` | True after `≥5` failed attempts (skipped until catalog changes) |
| `last_attempted_at` | Timestamp of last attempt |

---

### `match_audit` (manager DB)

Audit trail of every barcode inference decision. Useful for reviewing accuracy and debugging.

| Column | Purpose |
|---|---|
| `target_offer_id` | PK — the offer that received an inferred barcode |
| `inferred_barcode` | The barcode that was matched |
| `source_market` + `source_market_id` | Which product was used as the source |
| `match_method` | e.g. "heuristic", "ai:openrouter", "embedding:multilingual-e5-small" |
| `confidence` | Score (0.0–1.0) |
| `reasoning` | Text explanation from AI, or description of heuristic match |

---

### `process_timing` (manager DB)

Execution time log for every step. Used for performance monitoring and identifying slow markets.

| Column | Purpose |
|---|---|
| `process_name` | e.g. "all_departamentos", "full_sequence_pipeline" |
| `step_name` | e.g. "Rossi departamentos", "Barcode sync + AI match" |
| `market_name` | Which market this step relates to |
| `status` | "success", "failed", "skipped" |
| `duration_seconds` | How long the step took |
| `started_at` / `finished_at` | Wall clock timestamps |

---

### `higas_barcode_enrich_state` (manager DB)

State tracking for the Higas API barcode enrichment loop. Prevents re-querying barcodes that were already processed.

| Column | Purpose |
|---|---|
| `barcode` | PK — the barcode queried |
| `store_id` | Which Higas store was queried |
| `status` | "found" / "not_found" / "error" |
| `http_status` | HTTP response code from last attempt |
| `hits` | How many products were found |
| `last_attempted_at` | When last queried |

---

### `product_catalog` (manager DB)

Canonical product definitions built by `product_normalizer.py`. Aggregates product info across markets into a single canonical record per barcode.

| Column | Purpose |
|---|---|
| `barcode` | PK |
| `canonical_name` | Best product name selected across all markets |
| `canonical_brand` | Best brand name |
| `market_count` | How many markets carry this product |
| `source_market` | Which market provided the canonical data |

---

## How main.py Orchestrates Everything

### CLI modes

```bash
python main.py {mode} [limit]
```

| Mode | What it runs |
|---|---|
| `rossi_departamentos` | Rossi scraper only → save offers → save barcode refs |
| `higas_departamentos` | Higas scraper only → save offers → save barcode refs |
| `atacadao_departamentos` | Atacadão scraper only |
| `nagumo_departamentos` | Nagumo (store-specific mode) |
| `nagumo_departamentos_all` | Nagumo (all-catalog, no store filter) |
| `sonda_departamentos` | Sonda Delivery scraper only |
| `swift_departamentos` | Swift scraper only |
| `barbosa_departamentos` | Barbosa scraper only |
| `carrefour_departamentos` | Carrefour scraper only |
| `xsupermercados_departamentos` | X Supermercados scraper only |
| `higas_enrich` | Higas API barcode enrichment loop only |
| `barcode_pipeline_only` | Barcode sync + inference only (coordinator mode) |
| `embedding_model_infer` | Embedding model inference + audit |
| `all_departamentos` | All 9 scrapers in sequence + barcode pipeline |
| `test_departamentos` | All scrapers with limit=100, skips barcode pipeline |
| `full_sequence_pipeline` | Curated sequence + Higas enrich + AI matching |

### What a single market run does (e.g. `rossi_departamentos`)

```
1. _should_skip_recent_market()    check if last update < SKIP_UPDATED_WITHIN_DAYS
2. RossiDepartamentosScraper()     instantiate scraper
3. scraper.fetch_offers(zip_code)  run full scrape → List[Dict]
4. _format_offers_for_db()         convert to 23-field tuples
5. db.save_offers()                upsert to offers + append price_history if changed
6. _collect_barcode_refs()         extract (barcode, market, offer_id) triples
7. db.save_barcode_references_bulk() write to barcode_references + barcode_reference_market_map
8. print summary
```

### What `all_departamentos` adds

After all 9 market runners complete:

```
9.  barcode_ai_matcher.sync_known_barcodes()       build known_barcodes from offers with barcodes
10. barcode_ai_matcher.infer_missing_barcodes()    heuristic/AI matching for null-barcode offers
11. db.optimize_database()                         ANALYZE (postgres) or VACUUM+ANALYZE (sqlite)
```

### The skip logic

`SKIP_UPDATED_WITHIN_DAYS=1` (default) means: if a market was successfully scraped within the last 24 hours for the same `store_id`, skip it. This prevents re-scraping on accidental double-runs without clearing data.

Set to `0` to always scrape, or `7` to scrape once a week.

---

## Parallel Machine Deployment

Since each market has its own DB for `offers` and `price_history`, scrapers can run fully in parallel:

```bash
# Each machine runs with inference skipped
SKIP_BARCODE_INFERENCE=1 python main.py rossi_departamentos      # Machine 1
SKIP_BARCODE_INFERENCE=1 python main.py higas_departamentos      # Machine 2
SKIP_BARCODE_INFERENCE=1 python main.py atacadao_departamentos   # Machine 3
# ... etc

# After ALL machines finish, one coordinator runs:
python main.py barcode_pipeline_only                              # Coordinator
```

**Safe shared tables (no collision risk):**
- `store_mappings` — keyed by `(zip_code, market_name)`, each machine writes its own rows
- `barcode_references` / `barcode_reference_market_map` — keyed by `(barcode, market_name)`
- `process_timing` — append-only SERIAL PK

**Tables only written by the coordinator:**
- `known_barcodes` — built from all markets' offers
- `barcode_inference_state` — one row per offer being inferred
- `match_audit` — inference decisions
- `higas_barcode_enrich_state` — Higas API query tracking

---

## Environment Variables Reference

```bash
# Required
DATABASE_URL_MANAGER=postgresql://...     # shared tables DB
DATABASE_URL_ROSSI=postgresql://...       # per-market DBs (falls back to manager if unset)
DATABASE_URL_HIGAS=postgresql://...
DATABASE_URL_ATACADAO=postgresql://...
DATABASE_URL_NAGUMO=postgresql://...
DATABASE_URL_SONDA=postgresql://...
DATABASE_URL_SWIFT=postgresql://...
DATABASE_URL_BARBOSA=postgresql://...
DATABASE_URL_CARREFOUR=postgresql://...
DATABASE_URL_XSUPERMERCADOS=postgresql://...

# Scrape behavior
SCRAPE_ZIP_CODE=08032-230                 # default ZIP for auto-runs
SKIP_UPDATED_WITHIN_DAYS=1               # skip market if scraped within N days (0 = always scrape)
SKIP_BARCODE_INFERENCE=0                 # set to 1 to skip AI pipeline (faster, parallel-safe)
NAGUMO_ALL_CATALOG=0                     # set to 1 for Nagumo no-store-filter mode

# Barcode inference
ENABLE_AI_BARCODE_MATCH=0                # set to 1 to enable AI matching
HF_TOKEN=hf_...                          # HuggingFace token for AI provider
OPENROUTER_API_KEY=sk-or-...             # OpenRouter API key
BARCODE_HEURISTIC_THRESHOLD=0.96         # min score for heuristic auto-accept
BARCODE_AI_THRESHOLD=0.86                # min AI confidence to accept
AI_MAX_CALLS_PER_RUN=40                  # max AI API calls per inference run
BARCODE_BLACKLIST_THRESHOLD=5            # failures before blacklisting an offer

# Higas enrichment
HIGAS_ENRICHMENT_MAX_CALLS=650
HIGAS_ENRICHMENT_BURST_SIZE=140
HIGAS_ENRICHMENT_BURST_COOLDOWN_SECONDS=20

# Carrefour
CARREFOUR_ENABLE_PDP_ENRICH=1            # set to 0 to skip PDP barcode enrichment
CARREFOUR_PDP_ENRICH_LIMIT=120           # max PDP fetches per department

# Barbosa/XSuper
BARBOSA_TOKEN_ACTION_ID=...              # Next.js server action ID for token
XSUPER_DEFAULT_CORRIDOR_ID=...          # corridor ID for X Supermercados
```

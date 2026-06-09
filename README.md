# Unified Market Scraping System

This project builds a consolidated offers database from multiple markets.

Main strategy:
1. Scrape all market catalogs.
2. Persist normalized offers.
3. Enrich known barcodes (trusted sources).
4. Match logical products that do not have barcode yet.
5. Keep one consolidated database where barcode is the key identity driver.

## Core Database

PostgreSQL-based database (via Neon Cloud):
- Shared/manager DB connection: `DATABASE_URL_MANAGER` (fallback: `DATABASE_URL`)
- Market DB connections (optional, one per Neon project):
	- `DATABASE_URL_ROSSI`
	- `DATABASE_URL_ATACADAO`
	- `DATABASE_URL_NAGUMO`
	- `DATABASE_URL_HIGAS`
	- `DATABASE_URL_SWIFT`
	- `DATABASE_URL_SONDA`
	- `DATABASE_URL_XSUPERMERCADOS`
	- `DATABASE_URL_BARBOSA`
	- `DATABASE_URL_CARREFOUR`

Routing model:
- Shared tables stay in manager DB (barcode catalogs, store mappings, audit/inference metadata).
- Market tables (`offers`, `price_history`) are routed by `market_name` to each market DB.
- If a market DB variable is empty, that market automatically falls back to manager DB.

Optional shared-table sharding (for manager volume control):
- `DATABASE_URL_COMMON_CATALOG` → `product_catalog`, `barcode_reference_market_map`, `barcode_fingerprint_cache`
- `DATABASE_URL_COMMON_INFERENCE` → `barcode_inference_state`, `*_barcode_enrich_state`
- `DATABASE_URL_COMMON_TIMING` → `process_timing`
- `DATABASE_URL_COMMON_AUDIT` → `match_audit`, `model_inference_audit`
- `DATABASE_URL_COMMON_STORE` → `store_mappings`

If these variables are empty, all shared tables continue using `DATABASE_URL_MANAGER`.

Main table:
- `offers`

Important fields:
- `id` (market-specific offer id)
- `market_name`
- `product_name`
- `regular_price`
- `promo_price`
- `gtin`
- `barcode`
- `store_id`
- `zip_code`
- `last_updated`

Support tables:
- `barcode_references`
- `store_mappings`
- `price_history`
- `process_timing` (stores processing duration and status for each pipeline step)
- barcode inference tables managed by `barcode_ai_matcher.py`

## Run Commands

Run the full departamentos pipeline (recommended):

```bash
python main.py all_departamentos
```

Run mercados in parallel batches locally (faster):

```bash
python main.py all_departamentos --parallel
python main.py all_departamentos --parallel --parallel-workers 6
```

Notes:
- Parallel mode runs markets concurrently in two phases (before and after catalog sync).
- Use `--parallel-workers` or env `SCRAPE_PARALLEL_WORKERS` to tune local concurrency.
- Without `--parallel`, behavior stays sequential (legacy mode).

Run departamentos with a test limit (example: 100 products per market):

```bash
python main.py all_departamentos 100
```

Dedicated responsive test mode (default limit=100):

```bash
python main.py test_departamentos
python main.py test_departamentos 100
python main.py test_departamentos --parallel 100
```

What this mode does automatically:
- skips barcode sync/inference
- disables recent-update skipping (always executes for CEP validation)
- keeps normal DB writes (offers + store_mappings)

Why responsive: `test_departamentos` uses the same internal runner registry as `all_departamentos`. When you add a new market runner in code, both modes include it automatically.

Test multiple CEPs quickly (PowerShell):

```powershell
foreach ($cep in "05707-000","08032-230","07110-000") {
	$env:SCRAPE_ZIP_CODE = $cep
	python main.py test_departamentos 100
}
```

You can also use env var:

```bash
set SCRAPE_LIMIT=100
python main.py all_departamentos
```

Run a single market:

```bash
python main.py rossi_departamentos
python main.py nagumo_departamentos_all
python main.py sonda_departamentos
python main.py atacadao_departamentos
python main.py xsupermercados_departamentos
python main.py swift_departamentos
python main.py higas_departamentos
python main.py carrefour_departamentos
python higas_barcode_enrich.py --max-calls 650 --run-until-done --between-runs-cooldown 30
python main.py full_sequence_pipeline




python main.py higas_enrich

# single market with limit
python main.py nagumo_departamentos_all 100
python main.py higas_enrich 500

```

## Build Store Carts From Shopping List

Generate per-market cart files from your own shopping list so checkout is just login + pay.

Run:

```bash
python main.py build_cart shopping_list.example.json
python main.py build_cart_swift shopping_list.example.json
```

Input JSON supports:
- `items`: required list of products
- `markets`: optional subset of markets to evaluate
- `output_dir`: optional output base folder

Item fields:
- `name` (required)
- `quantity` (optional, default `1`)
- `brand` (optional)
- `max_price` (optional)

Example file: `shopping_list.example.json`

Output:
- `cart_output/<timestamp>/cart_<market>.csv` (one CSV per market)
- `cart_output/<timestamp>/cart_summary.json` (coverage + estimated totals)

Swift-specific extras (when Swift is included):
- `cart_output/<timestamp>/swift_cart_actions.json`
- `cart_output/<timestamp>/swift_quick_buy_links.txt`
- `cart_output/<timestamp>/swift_open_tabs.ps1`
- `cart_output/<timestamp>/swift_auto_cart.ps1`

Notes:
- This MVP builds ready-to-buy lists with product links from each market catalog.
- For Swift, you can run `swift_open_tabs.ps1`, login once, and quickly finalize add-to-cart from the opened product tabs.
- For Swift auto-add by quantity, run:

```bash
python main.py swift_cart_auto cart_output/<timestamp>/swift_cart_actions.json
```

- The automation opens Chromium, waits for your manual login, then adds each product with the requested quantity.
- Cart insertion is not fully automated for all stores yet (site APIs/auth/captcha vary by market).

Run standalone Higas barcode API enrichment (fixed store_id with call budget):

```bash
python higas_barcode_enrich.py
python higas_barcode_enrich.py --store-id 66466cdefafdf200a3352cd5 --zip-code 08032-230 --max-calls 650
python higas_barcode_enrich.py --max-calls 650 --flush-every-calls 120 --flush-cooldown 8
python higas_barcode_enrich.py --max-calls 650 --run-until-done --between-runs-cooldown 30
```

Notes:
- Running only `python higas_barcode_enrich.py` auto-resolves ZIP (SCRAPE_ZIP_CODE or location detection) and auto-resolves Higas store_id from ZIP.
- Uses only barcodes already mapped in DB and skips barcodes already registered for Higas.
- Keeps the same `store_id` in all search calls.
- Saves progress in DB table `higas_barcode_enrich_state` (not local JSON), so next runs resume safely and avoid API blocking after high call volume.
- For better anti-block behavior, script performs periodic DB flush checkpoints (`--flush-every-calls`) and sleeps (`--flush-cooldown`) before the next API batch.
- To process the full dataset, use `--run-until-done`; the script will execute multiple safe rounds of `--max-calls` and continue from DB state until completion.

Notes:
- `nagumo_departamentos` = store-filtered mode
- `nagumo_departamentos_all` = all-catalog mode (no pmid filter)

## Export CSVs

Export standard analysis files:

```bash
python -c "from db.db_manager import DatabaseManager; DatabaseManager().export_to_csv(prefix='analysis')"



# higas enrrich
https://api.instabuy.com.br/apiv3/search?search_barcode=7891150107465&platform=store_android&version=570&store_id=66466cdefafdf200a3352cd5


# sync offer table
python sync_app_offers.py --dry-run
python sync_app_offers.py --truncate

```

# Exporting offer
python -c "from db.db_manager import DatabaseManager; DatabaseManager().export_table_to_csv('app_offers', 'analysis_app_offers.csv')"

```





This exports:
- `analysis_offers.csv`
- `analysis_barcode_references.csv`
- `analysis_store_mappings.csv`
- `analysis_price_history.csv`

Export only one table:

```bash
c:/Users/dwolo/Documents/DARIO/PYTHON/markets_db/.venv/Scripts/python.exe -c "from db_manager import DatabaseManager; DatabaseManager().export_table_to_csv('offers','offers.csv')"
```

## Quick Validation Queries

Example Python check:

```python
from db_manager import DatabaseManager

db = DatabaseManager()
print(db.get_summary())
print(db.query_offers(market_name="Rossi", search_text="coca", limit=5))
print(db.get_offer_by_id("rossi_21534"))
```

Example SQL history query:

```sql
SELECT recorded_at, regular_price, promo_price, offer_name
FROM price_history
WHERE offer_id = 'rossi_12345'
ORDER BY recorded_at;
```

## Optional Runtime Controls

If you get too many rate-limit responses:

```bash
set AI_MAX_CALLS_PER_RUN=20
set AI_CALL_DELAY_SECONDS=1.5
```

Reduce Neon network transfer during heavy usage months:

```bash
# Skip full trusted barcode catalog re-sync if a sync ran recently (default: 6h)
set BARCODE_SYNC_MIN_INTERVAL_HOURS=12

# Optional: choose where local sync state is stored
set BARCODE_SYNC_STATE_FILE=.cache/barcode_sync_state.json

# Startup DB init mode (recommended for GitHub Actions parallel market jobs)
# 0 = init manager DB at startup + lazy-init each market DB only when used
set DB_INIT_ALL_MARKET_DBS_ON_STARTUP=0

# Legacy behavior (eager init of all configured market DBs at startup)
set DB_INIT_ALL_MARKET_DBS_ON_STARTUP=1

# Atacadao barcode lookup budget during scrape
# 0 = unlimited (default behavior)
set ATACADAO_CATALOG_EAN_MAX_CALLS=0

# Optional: cap total fallback EAN lookups per run if needed
set ATACADAO_CATALOG_EAN_MAX_CALLS=200

# Parallel workers for Atacadao fallback EAN lookups
set ATACADAO_CATALOG_EAN_WORKERS=8
```

Notes:
- Catalog sync reads and writes many rows; increasing this interval significantly reduces DB egress/ingress.
- Set `BARCODE_SYNC_MIN_INTERVAL_HOURS=0` to keep legacy behavior (sync every time).
- `DB_INIT_ALL_MARKET_DBS_ON_STARTUP=0` reduces startup traffic when each runner handles only one market.
- Atacadão keeps barcode lookup during scraping; missing EAN fallbacks are now prefetched concurrently per slug to reduce wall-clock time.
- `ATACADAO_CATALOG_EAN_MAX_CALLS=0` means unlimited fallback barcode lookups.
- Lower `ATACADAO_CATALOG_EAN_MAX_CALLS` only if you explicitly want to trade barcode coverage for speed.
- `ATACADAO_CATALOG_EAN_WORKERS` controls concurrency for those fallback EAN lookups.
- For pure scraper validation runs, prefer `python main.py test_departamentos 100` (already skips barcode sync/inference).

## Supabase Bootstrap + Disaster Recovery

You can automatically create/reuse one Supabase project for manager DB + one per market and generate all `DATABASE_URL_*` values used by this project.

Minimal required variable in `.env`:

```bash
set SUPABASE_ACCESS_TOKEN=your_token_here
```

Bootstrap (creates/reuses projects, generates env, and stores recovery artifacts):

```bash
python deploy/supabase_bootstrap.py bootstrap --write-env .env
```

What it generates:
- `deploy/disaster_recovery/<timestamp>/supabase_manifest.json`
- `deploy/disaster_recovery/<timestamp>/supabase.generated.env`
- `deploy/disaster_recovery/<timestamp>/restore_from_manifest.ps1`

Restore quickly after disaster using saved manifest:

```bash
python deploy/supabase_bootstrap.py restore --manifest deploy/disaster_recovery/<timestamp>/supabase_manifest.json --write-env .env
```

Optional Supabase variables:
- `SUPABASE_ORG_ID` (required only if token can access multiple orgs)
- `SUPABASE_REGION` (default: `sa-east-1`)
- `SUPABASE_DB_PASSWORD` (auto-generated if omitted)
- `SUPABASE_PROJECT_PREFIX` (default: `marketsdb`)
- `SUPABASE_DR_DIR` (default: `deploy/disaster_recovery`)

Balanced profile (best value: more matches + good speed):

```bash
set AI_MIN_BEST_SCORE_FOR_CALL=0.88
set AI_MAX_PROVIDER_ATTEMPTS=1
set AI_REMOTE_TIMEOUT_SECONDS=8
set AI_HARD_MAX_CALLS_PER_RUN=120
set AI_MAX_CONSECUTIVE_MISSES=12
set AI_CALL_DELAY_SECONDS=0.25
python main.py full_sequence_pipeline
```

This profile finds ~5-7% more matches than the default while keeping runtime under 2h 30m for 18k offers.

Fast profile (prioritize speed, same recall as current):

```bash
set AI_MIN_BEST_SCORE_FOR_CALL=0.88
set AI_MAX_PROVIDER_ATTEMPTS=1
set AI_REMOTE_TIMEOUT_SECONDS=8
set AI_HARD_MAX_CALLS_PER_RUN=60
set AI_MAX_CONSECUTIVE_MISSES=12
set AI_CALL_DELAY_SECONDS=0.25
python main.py full_sequence_pipeline
```

This profile achieves ~2h 30m runtime for 18k offers with the same match count.

Notes:
- `AI_HARD_MAX_CALLS_PER_RUN` caps AI usage even if `AI_MAX_CALLS_PER_RUN` is higher in `.env`.
- `AI_MAX_CONSECUTIVE_MISSES` auto-disables AI for the current run after repeated misses.
- Default `AI_MIN_BEST_SCORE_FOR_CALL=0.88` is conservative; lower to `0.84` if you want even more recall (slower).
- Balanced profile is recommended for first runs; use Fast profile if time is critical.

Blacklist repeated unmatched offers (skip after N failed runs):

```bash
set BARCODE_BLACKLIST_ENABLED=1
set BARCODE_BLACKLIST_THRESHOLD=5
set BARCODE_BLACKLIST_SKIP=1
```

To force a full retry of blacklisted offers in a new run:

```bash
set BARCODE_BLACKLIST_ENABLED=1
set BARCODE_BLACKLIST_SKIP=0
python main.py full_sequence_pipeline

python main.py all_departamentos
```

Frequency-weighted brand matching (improved brand detection):

The brand matcher now uses a frequency-weighted list of known brands from the catalog. Brands are ranked by how often they appear in the known barcodes database, and only common brands (above a frequency threshold) trigger implicit brand matching (brand found in product name when brand field is empty).

```bash
# Default: require brand to appear at least 2 times in catalog
set BARCODE_MIN_BRAND_FREQUENCY=2

# Strict mode: require 5+ occurrences (fewer false positives)
set BARCODE_MIN_BRAND_FREQUENCY=5

# Loose mode: allow brands appearing just 1 time
set BARCODE_MIN_BRAND_FREQUENCY=1

python main.py full_sequence_pipeline
```

Benefits:
- **Reduces false positives**: Only established brands (appearing in catalog) create matches
- **Weighted confidence**: Common brands (e.g., "Swift", "Nestlé") boost scores more than rare ones  
- **Implicit brand matching**: Catches cases like "Baby Picanha Swift Kg" matching "Baby Picanha Swift" where Swift is in the product name
- **Configurable threshold**: Tune frequency threshold based on your catalog size and precision needs
set BARCODE_BLACKLIST_SKIP=0
python main.py full_sequence_pipeline
```

Skip markets updated too recently (default: 1 day):

```bash
set SKIP_UPDATED_WITHIN_DAYS=1
python main.py full_sequence_pipeline
```

If `SKIP_UPDATED_WITHIN_DAYS` is not set in `.env`, the default is `1` day.

For CEP/storage validation tests, disable recent-update skipping so every CEP run executes:

```bash
set SKIP_UPDATED_WITHIN_DAYS=0
```

Restrict accepted barcode lengths (recommended for this project):

```bash
set BARCODE_ALLOWED_LENGTHS=12,13,14
python main.py all_departamentos
```

Notes:
- Default is `12,13,14` (8-digit GTIN is ignored).
- Existing invalid-length barcodes are cleaned automatically during startup.

## Hugging Face Deployment

This repository now includes a Hugging Face Space app in `hf_space_app/` and an automatic deploy workflow in `.github/workflows/deploy-hf-space.yml`.

Configure GitHub repository secrets:
- `HF_TOKEN`: your Hugging Face token
- `HF_SPACE_ID`: space id in format `username/space-name`

When you push changes under `hf_space_app/` to `main`, GitHub Actions deploys automatically to your Space.

## ML Dataset Export

Export positive labeled pairs from `match_audit` for training/evaluation:

```bash
python ml/export_training_dataset.py --output ml/data/train_pairs_positive.csv --limit 50000
```

Use this file as a starting point and add reviewed negatives/borderline examples before fine-tuning.



# delete postgree fresh restart
c:/Users/dwolo/Documents/DARIO/PYTHON/markets_db/.venv/Scripts/python.exe reset_data.py postgres --yes
python.exe reset_data.py postgres --yes




# testing # hugface

c:/Users/dwolo/Documents/DARIO/PYTHON/markets_db/.venv/Scripts/python.exe -c "import os; from barcode_ai_matcher import BarcodeAIMatcher; m=BarcodeAIMatcher(); os.environ['LM_STUDIO_ENABLED']='0'; os.environ['AI_PROVIDER_ORDER']='huggingface'; os.environ['OPENROUTER_API_KEY']=''; os.environ['XAI_API_KEY']=''; os.environ['GEMINI_API_KEY']=''; target={'id':'t','market_name':'Higas','product_name':'Coca Cola Zero 350ml','brand':'Coca-Cola','description':'Refrigerante lata 350ml','unit':'350ml'}; candidates=[{'barcode':'7894900011517','source_market':'Rossi','source_market_id':'r1','product_name':'Coca Cola Zero Lata 350ml','brand':'Coca-Cola','description':'Refrigerante zero acucar','normalized_name':'coca cola zero lata 350ml','normalized_brand':'coca cola','measure_token':'350ml','score':0.93},{'barcode':'7894900010015','source_market':'Rossi','source_market_id':'r2','product_name':'Coca Cola Original Lata 350ml','brand':'Coca-Cola','description':'Refrigerante tradicional','normalized_name':'coca cola original lata 350ml','normalized_brand':'coca cola','measure_token':'350ml','score':0.89}]; print(m._call_ai_matcher(target, candidates))"

# testing openrouter
c:/Users/dwolo/Documents/DARIO/PYTHON/markets_db/.venv/Scripts/python.exe -c "import os; from barcode_ai_matcher import BarcodeAIMatcher; m=BarcodeAIMatcher(); os.environ['LM_STUDIO_ENABLED']='0'; os.environ['AI_PROVIDER_ORDER']='openrouter'; os.environ['XAI_API_KEY']=''; os.environ['GEMINI_API_KEY']=''; os.environ['HF_TOKEN']=''; target={'id':'t','market_name':'Higas','product_name':'Coca Cola Zero 350ml','brand':'Coca-Cola','description':'Refrigerante lata 350ml','unit':'350ml'}; candidates=[{'barcode':'7894900011517','source_market':'Rossi','source_market_id':'r1','product_name':'Coca Cola Zero Lata 350ml','brand':'Coca-Cola','description':'Refrigerante zero acucar','normalized_name':'coca cola zero lata 350ml','normalized_brand':'coca cola','measure_token':'350ml','score':0.93},{'barcode':'7894900010015','source_market':'Rossi','source_market_id':'r2','product_name':'Coca Cola Original Lata 350ml','brand':'Coca-Cola','description':'Refrigerante tradicional','normalized_name':'coca cola original lata 350ml','normalized_brand':'coca cola','measure_token':'350ml','score':0.89}]; print(m._call_ai_matcher(target, candidates))"


# testing gemini
c:/Users/dwolo/Documents/DARIO/PYTHON/markets_db/.venv/Scripts/python.exe -c "import os; from barcode_ai_matcher import BarcodeAIMatcher; m=BarcodeAIMatcher(); os.environ['LM_STUDIO_ENABLED']='0'; os.environ['AI_PROVIDER_ORDER']='gemini'; os.environ['OPENROUTER_API_KEY']=''; os.environ['XAI_API_KEY']=''; os.environ['HF_TOKEN']=''; target={'id':'t','market_name':'Higas','product_name':'Coca Cola Zero 350ml','brand':'Coca-Cola','description':'Refrigerante lata 350ml','unit':'350ml'}; candidates=[{'barcode':'7894900011517','source_market':'Rossi','source_market_id':'r1','product_name':'Coca Cola Zero Lata 350ml','brand':'Coca-Cola','description':'Refrigerante zero acucar','normalized_name':'coca cola zero lata 350ml','normalized_brand':'coca cola','measure_token':'350ml','score':0.93},{'barcode':'7894900010015','source_market':'Rossi','source_market_id':'r2','product_name':'Coca Cola Original Lata 350ml','brand':'Coca-Cola','description':'Refrigerante tradicional','normalized_name':'coca cola original lata 350ml','normalized_brand':'coca cola','measure_token':'350ml','score':0.89}]; print(m._call_ai_matcher(target, candidates))"

# testing grok
c:/Users/dwolo/Documents/DARIO/PYTHON/markets_db/.venv/Scripts/python.exe -c "import os; from barcode_ai_matcher import BarcodeAIMatcher; m=BarcodeAIMatcher(); os.environ['LM_STUDIO_ENABLED']='0'; os.environ['AI_PROVIDER_ORDER']='xai'; os.environ['OPENROUTER_API_KEY']=''; os.environ['GEMINI_API_KEY']=''; os.environ['HF_TOKEN']=''; target={'id':'t','market_name':'Higas','product_name':'Coca Cola Zero 350ml','brand':'Coca-Cola','description':'Refrigerante lata 350ml','unit':'350ml'}; candidates=[{'barcode':'7894900011517','source_market':'Rossi','source_market_id':'r1','product_name':'Coca Cola Zero Lata 350ml','brand':'Coca-Cola','description':'Refrigerante zero acucar','normalized_name':'coca cola zero lata 350ml','normalized_brand':'coca cola','measure_token':'350ml','score':0.93},{'barcode':'7894900010015','source_market':'Rossi','source_market_id':'r2','product_name':'Coca Cola Original Lata 350ml','brand':'Coca-Cola','description':'Refrigerante tradicional','normalized_name':'coca cola original lata 350ml','normalized_brand':'coca cola','measure_token':'350ml','score':0.89}]; print(m._call_ai_matcher(target, candidates))"

# testing LLM locally
c:/Users/dwolo/Documents/DARIO/PYTHON/markets_db/.venv/Scripts/python.exe -c "import os; from barcode_ai_matcher import BarcodeAIMatcher; m=BarcodeAIMatcher(); os.environ['LM_STUDIO_ENABLED']='1'; os.environ['AI_PROVIDER_ORDER']='lmstudio'; os.environ['OPENROUTER_API_KEY']=''; os.environ['XAI_API_KEY']=''; os.environ['GEMINI_API_KEY']=''; os.environ['HF_TOKEN']=''; target={'id':'t','market_name':'Higas','product_name':'Coca Cola Zero 350ml','brand':'Coca-Cola','description':'Refrigerante lata 350ml','unit':'350ml'}; candidates=[{'barcode':'7894900011517','source_market':'Rossi','source_market_id':'r1','product_name':'Coca Cola Zero Lata 350ml','brand':'Coca-Cola','description':'Refrigerante zero acucar','normalized_name':'coca cola zero lata 350ml','normalized_brand':'coca cola','measure_token':'350ml','score':0.93},{'barcode':'7894900010015','source_market':'Rossi','source_market_id':'r2','product_name':'Coca Cola Original Lata 350ml','brand':'Coca-Cola','description':'Refrigerante tradicional','normalized_name':'coca cola original lata 350ml','normalized_brand':'coca cola','measure_token':'350ml','score':0.89}]; print(m._call_ai_matcher(target, candidates))"



python main.py all_departamentos


#consult barcode
https://verifiedbygs1.gs1br.org/?utm_source=google-pmax&utm_medium=cpc-pmax&utm_campaign=21886016818&utm_term=&utm_content=&gad_source=1&gad_campaignid=21882175418&gbraid=0AAAAA-aMM1S150DZgZsgdVfM2TS-AaAX1&gclid=Cj0KCQjwmunNBhDbARIsAOndKpkuIc7TwPIqAh5Vg88Gl2tNTIWKojJthaNa_JgD3rUJ5LMOKismXXcaArAVEALw_wcB




test mode 

How to use:

1- Default test (100 items/market, fast mode):
python main.py test_departamentos

2- Custom limit:
python main.py test_departamentos 100

3- Multi-CEP test in PowerShell:
$env:SKIP_UPDATED_WITHIN_DAYS = "0"
foreach ($cep in "05707-000","08032-230","07110-000","20930-041") {
$env:SCRAPE_ZIP_CODE = $cep
python main.py test_departamentos rossi_departamentos 100
}






analise

sonda delivery único
atacadão ok
higas ok so em são Paulo
barbosa ok
Nagumo com problemas para selecionar a loja
Rossi ok
SWIFT ok
xsupermercados ok
Carrefour ok


# norte, sul, lest, oete


$ceps = @("02401-100","04646-000","08032-230","06290-170")

foreach ($cep in $ceps) {
$env:SCRAPE_ZIP_CODE = $cep
python main.py
if ($LASTEXITCODE -ne 0) { break }
}





# run all
python main.py all_departamentos
# test all departaments
python main.py test_departamentos 100

# see variables 
Get-ChildItem Env: | Where-Object { $_.Name -match "SCRAPE|SKIP|ZIP" }

# remove variable
Remove-Item Env:\SCRAPE_ZIP_CODE -ErrorAction SilentlyContinue




# To run all markets with a 100-item limit per market, use:
python main.py all_departamentos 100

# Or use the faster test mode (skip barcode inference, always execute):
python main.py test_departamentos 100

Difference:

all_departamentos 100 — Full pipeline with barcode matching/inference (slower)
test_departamentos 100 — Skips barcode sync/inference, just validates scraping works (faster)
Both will run all 9 markets with 100 items each for testing.




new archteture:


# Each machine scrapes its own market — no barcode inference, no shared-table contention
SKIP_BARCODE_INFERENCE=1 python main.py rossi_departamentos
SKIP_BARCODE_INFERENCE=1 python main.py higas_departamentos
SKIP_BARCODE_INFERENCE=1 python main.py atacadao_departamentos
SKIP_BARCODE_INFERENCE=1 python main.py nagumo_departamentos
# ... all 9 markets in parallel

# When all machines are done, coordinator runs once:
python main.py barcode_pipeline_only


# departaments
python main.py higas_departamentos
# enrich higas
python main.py higas_enrich


# export data
python -c "from db.db_manager import DatabaseManager; DatabaseManager().export_to_csv(prefix='analysis')"

# Db MENAGER
barcode_fingerprint_cache
Speeds up barcode inference by caching results so the same product doesn't need to be matched twice. A fingerprint is a short SHA-256 hash of the normalized brand + name + measure token (e.g. "1kg", "500ml") — it's market-agnostic, so if Carrefour and Extra sell the same product, one fingerprint covers both. Stores the inferred barcode, confidence, and method. hit_count tracks how many times the cache was used for each entry.

barcode_inference_state
Tracks every offer that has gone through the barcode matching pipeline. For each offer it records: whether it was matched (matched), how many times the system tried and gave up (no_match_count), whether it's permanently blacklisted (blacklisted), and an offer_signature that changes when a product's name/price changes (so stale entries get re-matched). This is what lets the pipeline skip offers already matched and avoid hammering AI on hopeless ones.

barcode_reference_market_map
The normalized catalog of confirmed barcodes. One row per (barcode, market_name) pair, pointing to which offer in which market confirmed that barcode. This is the cross-market lookup table — when Nagumo or Atacadão (tier 3, no inline barcodes) sells something, the system looks here first to find the barcode from a tier-1 market that already has it.

match_audit
An audit trail for every barcode that was inferred (not scanned from the product directly). Records which offer got the barcode, where it came from (source_market, source_market_id), how confident the match was, and the AI reasoning. One row per offer, updated in place. Useful for reviewing or rolling back bad AI matches.

process_timing
Logs the duration and outcome of every pipeline stage — scraping, barcode inference, DB optimization, storage controller, etc. Each row is one pipeline step: market name, status (success/failed/skipped), how long it took, and when it ran. This is what feeds the timing breakdown table you see in the run output.

product_catalog
The canonical product registry, keyed by barcode. Stores the "best known" name, brand, description, unit, and normalized tokens for each physical product across all markets. market_count says how many markets sell it. Used during barcode matching to enrich tier-3 offers and build the similarity comparisons.

store_mappings
One row per market — stores which store_id is used for that market, plus the store's name, address, city, and coordinates. Cached so the system doesn't need to re-resolve a store from an API on every run. PK is market_name (just changed from (zip_code, market_name) — previously each ZIP had its own row, now it's one row per market since prices are uniform).








# MATRIX
NAGUMO: Av. Jurema, 1065 - Parque Jurema, Guarulhos - SP, 07244-000
ASSAI: Av. Aricanduva, 5555 - Jardim Marília, São Paulo - SP, 03527-900
ATACADAO: Av. Morvan Dias de Figueiredo, 6157 - Vila Maria, São Paulo - SP, 02170-901



# IA MATCH
python main.py barcode_pipeline_only

npx get-shit-done-cc@latest

# export data
python -c "from db.db_manager import DatabaseManager; DatabaseManager().export_to_csv(prefix='analysis')"


# main
python main.py --parallel
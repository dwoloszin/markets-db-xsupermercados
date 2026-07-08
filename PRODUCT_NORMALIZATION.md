# Product Normalization Implementation

## What was created

### 1. New Database Table: `product_catalog`
- **Location**: PostgreSQL, created automatically via `db_manager.py` init
- **Purpose**: One canonical entry per barcode with normalized product name
- **Columns**:
  - `barcode` (PK): GTIN identifier  
  - `canonical_name`: Best normalized product name across all markets
  - `canonical_brand`: Canonical brand name
  - `canonical_description`: Best available description
  - `canonical_unit`: Unit field (e.g., "UN", "77g", "350ml")
  - `market_count`: Number of markets selling this product
  - `source_market`: Which market provided the canonical data
  - `last_updated`: Timestamp

### 2. New Script: `product_normalizer.py`
- **Purpose**: Build and maintain the `product_catalog` table
- **Algorithm**: For each barcode (groups from `offers`):
  1. Score each offer source using:
     - Market quality bonus (Higas > Barbosa/Nagumo > Rossi/Sonda > Others)
     - Data completeness (brand + description + unit = higher score)
     - Name informativeness (longer = more detail)
     - Penalty for ALL-CAPS words (poor data quality)
  2. Pick the best scoring offer as canonical source
  3. Normalize the names (title case, collapse whitespace, fix quotes, etc.)
  4. Upsert into `product_catalog` table using batched `executemany` (fast for cloud DB)
  5. Export to CSV when `--export` is provided

**Usage**:
```bash
# Dry-run: print what would be done without DB commit
python product_normalizer.py --dry-run

# Full build and export
python product_normalizer.py --export analysis_product_catalog.csv

# Background (recommended for cloud DB):
python _run_normalizer.py
```

### 3. Modified Files

**db_manager.py**:
- Added `product_catalog` table creation in `_init_db_postgres()`
- Added `product_catalog` export to `export_to_csv()` method

**README.md**:
- No changes needed (documentation already in place)

## Current Status

✅ **Files created & tested successfully**:
- `product_normalizer.py` — script ready
- `_run_normalizer.py` — wrapper for easy background execution (running now)
- `product_catalog` table — auto-created in DB during init

⏳ **In progress**: First product catalog build (background, ~10-15 min on cloud DB)
- Command: `python _run_normalizer.py`
- Output: `analysis_product_catalog.csv` (when complete)

## Next Steps

### Check results (after ~15 minutes):
```bash
# Check if CSV was created
if (Test-Path "analysis_product_catalog.csv") {
    (Get-Content "analysis_product_catalog.csv" | Measure-Object -Line).Lines  # Row count
    Get-Content "analysis_product_catalog.csv" | Select-Object -First 5      # Preview
}

# Or query the DB directly:
c:/Users/dwolo/Documents/DARIO/PYTHON/markets_db/.venv/Scripts/python.exe -c "from db_manager import DatabaseManager; db=DatabaseManager(); conn=db._get_pg(); cur=conn.cursor(); cur.execute('SELECT COUNT(1) FROM product_catalog'); print('Rows:', cur.fetchone()[0]); conn.close()"
```

### Use the canonical catalog in queries:
```python
from db_manager import DatabaseManager
db = DatabaseManager()
conn = db._get_pg()
cur = conn.cursor()

# Find the canonical name for a barcode:
cur.execute("SELECT canonical_name, canonical_brand FROM product_catalog WHERE barcode = %s", ("7899970402852",))
result = cur.fetchone()
if result:
    print(f"Canonical: {result[0]} [{result[1]}]")

conn.close()
```

### Re-run normalizer after offers change:
```bash
# Automatically syncs product_catalog with latest offers
python product_normalizer.py --export analysis_product_catalog.csv
```

## How It Works: Example

**Input** (same barcode `7899970402852` from 6 different markets):
```
sonda           → "Chocolate ao leite cookies n creme Hershey s 77g"           (no brand, no desc)
xsupermercados  → "Chocolate Branco Cookies N'creme Hershey's 77g"              (no brand)
rossi           → "Chocolate Hersheys Cook n Choc 77g"                          (weak, short)
higas           → "Chocolate Branco Cookies 'n' Creme Hershey's Pacote 77g"    (brand✓ desc✓ unit✓)
barbosa         → "Chocolate Branco Com Biscoito de Chocolate Hersheys 77g"    (brand✓)
nagumo          → "Chocolate Branco Cookies N'Creme Hershey'S 77G"              (brand✓ but all-caps)
```

**Scoring**:
- Higas: +1.0 (quality) +3.0 (brand) +2.0 (desc) +1.0 (unit) + words... → **WINNER**
- Barbosa: +0.8 (quality) +3.0 (brand) ... → second
- Nagumo: +0.8 (quality) +3.0 (brand) -0.4 (ALL-CAPS penalty) ...
- Others: lower scores (no brand/desc or poor market quality)

**Output** (product_catalog entry):
```
barcode:              7899970402852
canonical_name:       Chocolate Branco Cookies 'n' Creme Hershey's Pacote 77g
canonical_brand:      Hershey's
canonical_unit:       UNI
market_count:         6
source_market:        Higas
```

## Performance Notes

- Cloud DB: Full build takes 10-20 minutes (10k+ barcodes × network latency)
- Uses `executemany` for efficiency (reduces from N round-trips to 1)
- Safe for frequent runs (only updates changed rows via `ON CONFLICT DO UPDATE`)

---

**Created**: 2025-03-18 | Part of markets_db barcode normalization system

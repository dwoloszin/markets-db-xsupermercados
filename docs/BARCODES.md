# Barcodes

The barcode (GTIN-13 for almost everything sold in Brazil, GTIN-8 for a few
small items, GTIN-12/UPC for imports, GTIN-14 for cases) is the **main key** of
this project. It is the only attribute that identifies the same physical product
in every store, independent of how each retailer writes the name.

## Validation and canonical form (`markets/common/gtin.py`)

Every barcode written to the database goes through `normalize_gtin()`:

1. digits only, leading zeros stripped
2. re-padded to the shortest standard length (8, 12, 13 or 14)
3. **check digit verified** (GTIN mod-10). Anything failing is rejected.

So `"07894900011517"` (GTIN-14 with indicator 0) and `"7894900011517"` become
the same string, and `"5624968"` (an internal reference id that Atacadão puts in
its `gtin` field) is thrown away.

GTIN-8 is accepted only where the source is an explicit barcode field
(`allow_gtin8=True`): VTEX `ean`, VipCommerce `codigo_barras`, Carrefour image
names / JSON-LD, Sonda, Rossi. It is refused for fields that mix internal ids
with barcodes (applay `material`/`sku`, Atacadão GraphQL `gtin`, Davo sku),
because 1 in 10 random 8-digit numbers passes the check digit.

Real-world example: Heineken 330 ml is GTIN-8 `78936683`; Carrefour exposes it
in the image name (`78936683_1_2_1200_72_RGB.png`) and PDP JSON-LD.

## Where each market's barcode comes from

| market | route | when |
|--------|-------|------|
| swift, giga, oba, samsclub | VTEX `items[0].ean` | inline, listing |
| rossi, davo | VipCommerce `codigo_barras` | inline, listing |
| tenda | Stoom `barcode` | inline, listing |
| xsupermercados, barbosa | applay `material` or `sku` (12-14 digits) | inline, listing |
| atacadao | GraphQL has no usable gtin; **VTEX catalog** `/io/api/catalog_system/pub/products/search?fq=productId:X` (50 ids per call) | enrichment, only NULL rows |
| carrefour | image file name starts with the GTIN for a few products (listing); PDP JSON-LD `gtin` for the rest | inline + enrichment |
| sonda | image path `/sku/<sku>/<size>/<EAN>.png` (~41%); PDP JSON-LD `gtin` | inline + enrichment |
| extra, paodeacucar | nothing in the API; PDP HTML `"ean":"789..."` | enrichment |
| nagumo | nothing on the site any more (`upc` gone in 2026) | legacy + crossfill |
| higas | none since the API moved to ibecom v5 (apiv3 had `barcodes`) | legacy + crossfill |

`barcode_source` records which route produced each value: `inline`, `image`,
`pdp`, `catalog`, `legacy`, `crossfill`.

## Enrichment is incremental

`db.load_missing_barcodes()` returns only rows with `barcode IS NULL` that were
not tried recently (`barcode_enrich_state`; `not_found` is retried after
`config.BARCODE_RETRY_NOT_FOUND_DAYS`, default 14). A barcode never changes, so
the first run pays the full cost (e.g. ~15k PDP fetches for Extra, ~12 threads,
~10 min) and every later run only fetches the handful of new products.

## Legacy seeding

The previous project generation had ~130k barcodes across markets. On first
connect the old `offers` table is renamed `offers_legacy`; after every scrape
`seed_barcodes_from_legacy()` copies barcodes to rows that still lack one,
matching by `product_url` first, then by normalised name (ambiguous names are
never used). This is why Higas keeps ~65% coverage although its new API exposes
no barcode at all.

## Cross-market fill (`tools/crossfill_barcodes.py`)

Deterministic replacement for the old AI matcher:

* build `normalised name -> barcode` from every market that has barcodes,
  keeping only names that map to exactly one barcode and have >= 3 tokens;
* fill NULL rows whose normalised name is exactly equal.

No fuzzy matching, no model, seconds to run, auditable (`barcode_source =
'crossfill'`, revert with one UPDATE). It mainly helps Nagumo and Higas, whose
names are usually the manufacturer's official description.

## Things that are NOT barcodes

* weighed produce and bakery items (Carrefour `gtin: "2062"`, Oba fruit,
  Hortifruti everywhere) - internal PLU codes; expected to stay NULL;
* Atacadão GraphQL `gtin` (`"5624968"`, `"48076932"`) - RefIds;
* applay `material` when it is a 5-digit number - internal SKU.

## Checking coverage

```bash
python -m db.db_manager stats
python -m db.db_manager export-all-together        # only rows with barcode
python -m db.db_manager export-all-together --all-rows
```

"""product_normalizer.py

Builds and maintains a `product_catalog` table with one canonical
(normalized) product entry per barcode, derived from the best available
offer data across all markets.

The table stores:
  - barcode         -- GTIN identifier (PK)
  - canonical_name  -- best normalized product name
  - canonical_brand -- best brand
  - canonical_description -- best description
  - canonical_unit  -- unit (e.g. "UN", "77g", "350ml")
  - market_count    -- number of markets selling this product
  - source_market   -- which market supplied the chosen name
  - last_updated

Usage:
    python product_normalizer.py
    python product_normalizer.py --dry-run
    python product_normalizer.py --export product_catalog.csv
"""

import argparse
import csv
import re
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
import os

# ---------------------------------------------------------------------------
# Market quality priority (higher = name from this market is preferred)
# ---------------------------------------------------------------------------
_MARKET_QUALITY_BONUS: dict[str, float] = {
    "higas": 1.0,
    "barbosa": 0.8,
    "nagumo": 0.8,
    "rossi": 0.5,
    "sonda": 0.5,
    "swift": 0.3,
    "xsupermercados": 0.3,
    "atacadao": 0.3,
}

# Prepositions/conjunctions that stay lowercase inside a title
_LOWERCASE_WORDS = {
    "de", "do", "da", "dos", "das", "di",
    "com", "e", "em", "no", "na", "ao", "aos",
    "por", "para", "a", "o", "n",
}

# Pattern: measurement tokens like 77g, 350ml, 1kg, 2lt etc.
_MEASURE_RE = re.compile(
    r'^\d+(\.\d+)?(g|kg|ml|l|lt|lts|un|und|unid|pct|cx|cp|gr|gramas?|litros?)$',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _score_offer(row: dict) -> float:
    """Score an offer row for its name quality. Higher = better canonical candidate."""
    score = 0.0

    market_key = (row.get("market_name") or "").lower()
    for key, bonus in _MARKET_QUALITY_BONUS.items():
        if key in market_key:
            score += bonus
            break

    brand = (row.get("brand") or "").strip()
    description = (row.get("description") or "").strip()
    unit = (row.get("unit") or "").strip()
    product_name = (row.get("product_name") or "").strip()

    if brand:
        score += 3.0
    if description:
        score += 2.0
    if unit:
        score += 1.0

    # Prefer longer, more informative names (capped to avoid absurd lengths)
    words = product_name.split()
    score += min(len(words), 14) * 0.15

    # Penalise all-caps words (indicates raw/lazy import)
    all_caps = sum(1 for w in words if len(w) > 2 and w.isupper())
    score -= all_caps * 0.4

    return score


# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

def _title_word(word: str) -> str:
    """Capitalise one word intelligently."""
    low = word.lower()

    # Measurement tokens stay lowercase
    if _MEASURE_RE.match(word):
        return low

    # Short prepositions/conjunctions stay lowercase (when not first word)
    if low in _LOWERCASE_WORDS:
        return low

    return word.capitalize()


def _normalize_text(text: str) -> str:
    """Return a cleaned, title-cased version of a product name."""
    if not text:
        return ""

    # Unicode NFC
    text = unicodedata.normalize("NFC", text)

    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()

    # Normalise fancy quotes / apostrophes
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = re.sub(r"'{2,}", "'", text)

    # Title-case, keeping first word always capitalised
    words = text.split()
    if not words:
        return ""
    result = [words[0].capitalize()] + [_title_word(w) for w in words[1:]]
    return " ".join(result)


# ---------------------------------------------------------------------------
# Canonical selection
# ---------------------------------------------------------------------------

def _pick_canonical(offers: list[dict]) -> dict:
    """Pick the best offer from a group sharing the same barcode."""
    best = max(offers, key=_score_offer)

    return {
        "canonical_name": _normalize_text(best.get("product_name") or ""),
        "canonical_brand": _normalize_text(best.get("brand") or "") or None,
        "canonical_description": _normalize_text(best.get("description") or "") or None,
        "canonical_unit": (best.get("unit") or "").strip() or None,
        "market_count": len(offers),
        "source_market": best.get("market_name"),
    }


# ---------------------------------------------------------------------------
# Main build function
# ---------------------------------------------------------------------------

def _connect():
    """Open a direct psycopg connection using DATABASE_URL, without triggering
    the full DatabaseManager init (which runs slow cleanup on every startup)."""
    try:
        import psycopg  # type: ignore
    except ImportError as exc:
        raise RuntimeError("psycopg not installed. Run: pip install psycopg[binary]") from exc

    from env_loader import load_env_file
    load_env_file()
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set. Add it to your .env file.")
    return psycopg.connect(url)


def build_product_catalog(dry_run: bool = False) -> dict:
    """
    Scan all offers with a valid barcode and upsert one canonical entry per
    barcode into the `product_catalog` table.

    Returns a summary dict with keys: upserted, skipped, dry_run.
    """
    conn = _connect()
    cur = conn.cursor()

    # Fetch every offer that has a valid barcode
    cur.execute(
        """
        SELECT id, market_name, product_name, brand, description, unit, barcode
        FROM offers
        WHERE barcode IS NOT NULL AND TRIM(barcode) <> ''
        ORDER BY barcode, market_name
        """
    )
    rows = cur.fetchall()
    cols = ["id", "market_name", "product_name", "brand", "description", "unit", "barcode"]

    # Group by barcode
    by_barcode: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        r = dict(zip(cols, row))
        by_barcode[r["barcode"]].append(r)

    now = datetime.now(timezone.utc)
    upserted = 0
    skipped = 0

    if not dry_run:
        # Safety net – table should already exist via db_manager init
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS product_catalog (
                barcode TEXT PRIMARY KEY,
                canonical_name TEXT NOT NULL,
                canonical_brand TEXT,
                canonical_description TEXT,
                canonical_unit TEXT,
                market_count INTEGER NOT NULL DEFAULT 1,
                source_market TEXT,
                last_updated TIMESTAMP NOT NULL
            )
            """
        )

    for barcode, offers in by_barcode.items():
        canonical = _pick_canonical(offers)
        if not canonical["canonical_name"]:
            skipped += 1
            continue

        if dry_run:
            print(
                f"  {barcode}  ({canonical['market_count']} markets)"
                f"  [{canonical['source_market']}]"
                f"  ->  {canonical['canonical_name']!r}"
            )
            upserted += 1
            continue

        cur.execute(
            """
            INSERT INTO product_catalog
                (barcode, canonical_name, canonical_brand, canonical_description,
                 canonical_unit, market_count, source_market, last_updated)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (barcode) DO UPDATE SET
                canonical_name        = EXCLUDED.canonical_name,
                canonical_brand       = EXCLUDED.canonical_brand,
                canonical_description = EXCLUDED.canonical_description,
                canonical_unit        = EXCLUDED.canonical_unit,
                market_count          = EXCLUDED.market_count,
                source_market         = EXCLUDED.source_market,
                last_updated          = EXCLUDED.last_updated
            """,
            (
                barcode,
                canonical["canonical_name"],
                canonical["canonical_brand"],
                canonical["canonical_description"],
                canonical["canonical_unit"],
                canonical["market_count"],
                canonical["source_market"],
                now,
            ),
        )
        upserted += 1

    if not dry_run:
        conn.commit()
    conn.close()
    if not dry_run:
        # Safety net – table should already exist via db_manager init
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS product_catalog (
                barcode TEXT PRIMARY KEY,
                canonical_name TEXT NOT NULL,
                canonical_brand TEXT,
                canonical_description TEXT,
                canonical_unit TEXT,
                market_count INTEGER NOT NULL DEFAULT 1,
                source_market TEXT,
                last_updated TIMESTAMP NOT NULL
            )
            """
        )
        conn.commit()

    rows_to_upsert = []
    for barcode, offers in by_barcode.items():
        canonical = _pick_canonical(offers)
        if not canonical["canonical_name"]:
            skipped += 1
            continue

        if dry_run:
            print(
                f"  {barcode}  ({canonical['market_count']} markets)"
                f"  [{canonical['source_market']}]"
                f"  ->  {canonical['canonical_name']!r}"
            )
            upserted += 1
            continue

        rows_to_upsert.append((
            barcode,
            canonical["canonical_name"],
            canonical["canonical_brand"],
            canonical["canonical_description"],
            canonical["canonical_unit"],
            canonical["market_count"],
            canonical["source_market"],
            now,
        ))

    if not dry_run and rows_to_upsert:
        cur.executemany(
            """
            INSERT INTO product_catalog
                (barcode, canonical_name, canonical_brand, canonical_description,
                 canonical_unit, market_count, source_market, last_updated)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (barcode) DO UPDATE SET
                canonical_name        = EXCLUDED.canonical_name,
                canonical_brand       = EXCLUDED.canonical_brand,
                canonical_description = EXCLUDED.canonical_description,
                canonical_unit        = EXCLUDED.canonical_unit,
                market_count          = EXCLUDED.market_count,
                source_market         = EXCLUDED.source_market,
                last_updated          = EXCLUDED.last_updated
            """,
            rows_to_upsert,
        )
        upserted = len(rows_to_upsert)
        conn.commit()

    conn.close()

    return {"upserted": upserted, "skipped": skipped, "dry_run": dry_run}

def export_catalog_csv(output_path: str) -> int:
    """Export the product_catalog table to a CSV file. Returns row count."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT barcode, canonical_name, canonical_brand, canonical_description,
               canonical_unit, market_count, source_market, last_updated
        FROM product_catalog
        ORDER BY canonical_name
        """
    )
    rows = cur.fetchall()
    conn.close()

    headers = [
        "barcode", "canonical_name", "canonical_brand", "canonical_description",
        "canonical_unit", "market_count", "source_market", "last_updated",
    ]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)

    return len(rows)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Build/refresh product_catalog table with one canonical name per barcode."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written without committing to the DB.",
    )
    parser.add_argument(
        "--export",
        metavar="FILE",
        help="After building, export the product_catalog table to FILE (CSV).",
    )
    args = parser.parse_args()

    result = build_product_catalog(dry_run=args.dry_run)
    label = "(dry-run)" if args.dry_run else "updated"
    print(
        f"Product catalog {label}: {result['upserted']} entries upserted"
        + (f", {result['skipped']} skipped (no name)." if result["skipped"] else ".")
    )

    if args.export and not args.dry_run:
        n = export_catalog_csv(args.export)
        print(f"Exported {n} rows → {args.export}")

"""
scraper_santaluzia.py - Casa Santa Luzia, Jardins SP (https://www.santaluzia.com.br)

Platform : VTEX IO (account `santaluzia`), public catalog_system API (markets/common/vtex.py).
Listing  : 25 departments -> 44 segments under the 2,500 cap (50/page). ~15.8k products.
Store    : a single online store; VTEX regions returns no sellers for any CEP and the catalog
           answers with seller "1" -> store_id "santaluzia", national prices.
Barcode  : inline items[0].ean on ~95% of products. Many imported goods carry real EAN-8
           codes, so GTIN-8 is accepted (89.8% valid without it, 95.3% with it, 2026-10-27).
Runtime  : ~10 min. The host occasionally resets TLS connections - request_with_retry absorbs it.
"""

from __future__ import annotations

from typing import Dict, Optional

from markets.common.geo import format_zip
from markets.common.http import make_session
from markets.common.vtex import VtexCatalog, vtex_offer

STORE_KEY = "santaluzia"
BASE_URL = "https://www.santaluzia.com.br"
STORE_ID = "santaluzia"  # single online store


def scrape(db, zip_code: str, limit: Optional[int] = None) -> Dict[str, int]:
    session = make_session({"Referer": BASE_URL + "/"})
    db.save_store_info(STORE_ID, query_zip=format_zip(zip_code), name="Casa Santa Luzia (online)",
                       city="São Paulo", state="SP")
    catalog = VtexCatalog(session, BASE_URL, delay=0.2, log_prefix="[santaluzia] ")
    segments = catalog.segments(depth=3) or [(None, "all")]
    print(f"[santaluzia] {len(segments)} segments")
    seen: set = set()
    total = {"upserted": 0, "skipped": 0, "with_barcode": 0}
    for fq, label in segments:
        batch = []
        for product in catalog.iter_products(fq, label=label):
            pid = str(product.get("productId") or "")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            offer = vtex_offer(product, store_id=STORE_ID, base_url=BASE_URL, category_label=label, allow_gtin8=True)
            if offer:
                batch.append(offer)
            if limit and len(seen) >= limit:
                break
        if batch:
            r = db.save(batch)
            for k in total:
                total[k] += r[k]
        print(f"[santaluzia] {str(label)[:45]:<45} +{len(batch):>4} (total {len(seen)})")
        if limit and len(seen) >= limit:
            break
    return total


if __name__ == "__main__":
    from markets.common.runner import run_cli
    run_cli(STORE_KEY, scrape)

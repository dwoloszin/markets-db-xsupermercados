"""
scraper_giga.py - Giga Atacado (https://www.giga.com.vc)

Platform : VTEX (VTEX IO storefront)
Listing  : category tree -> products/search?fq=C:/path/ (50/page); falls back to a
           plain _from/_to sweep when the tree is empty.
Store    : single online store (store_id "giga.com.vc").
Barcode  : inline items[0].ean (~95%).
"""

from __future__ import annotations

from typing import Dict, Optional

from markets.common.geo import format_zip
from markets.common.http import make_session
from markets.common.vtex import VtexCatalog, vtex_offer

STORE_KEY = "giga"
BASE_URL = "https://www.giga.com.vc"
STORE_ID = "giga.com.vc"


def scrape(db, zip_code: str, limit: Optional[int] = None) -> Dict[str, int]:
    session = make_session({"Referer": BASE_URL + "/"})
    db.save_store_info(STORE_ID, query_zip=format_zip(zip_code), name="Giga Atacado (online)")
    catalog = VtexCatalog(session, BASE_URL, delay=0.1, log_prefix="[giga] ")
    segments = catalog.segments(depth=3) or [(None, "all")]
    print(f"[giga] {len(segments)} segments")
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
        print(f"[giga] {str(label)[:45]:<45} +{len(batch):>4} (total {len(seen)})")
        if limit and len(seen) >= limit:
            break
    return total


if __name__ == "__main__":
    from markets.common.runner import run_cli
    run_cli(STORE_KEY, scrape)

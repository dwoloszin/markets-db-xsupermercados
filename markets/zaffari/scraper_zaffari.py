"""
scraper_zaffari.py - Zaffari / Bourbon online (https://www.zaffari.com.br)

Platform : VTEX IO (account `zaffari`), public catalog_system API (see markets/common/vtex.py).
Listing  : category tree (12 departments, 21 segments) -> products/search?fq=C:/dept/sub/
           (50/page). ~15.2k products.
Store    : PRICES ARE NATIONAL (measured 2026-09-22). The catalog answers with the placeholder
           seller "1"; VTEX regions returns 5 store sellers (zaffari1110/1240/1380/1410/1510,
           Porto Alegre) for every CEP, and a checkout simulation gives the same price on all
           of them - only stock differs per store. One run, store_id "zaffari".
Barcode  : inline items[0].ean on every product; ~97.5% valid GTIN (weighed items carry
           "0000020..." PLU codes that fail the check digit and are dropped).
Runtime  : ~8 min.
"""

from __future__ import annotations

from typing import Dict, Optional

from markets.common.geo import format_zip
from markets.common.http import make_session
from markets.common.vtex import VtexCatalog, vtex_offer

STORE_KEY = "zaffari"
BASE_URL = "https://www.zaffari.com.br"
STORE_ID = "zaffari"  # national prices (measured 2026-09-22)


def scrape(db, zip_code: str, limit: Optional[int] = None) -> Dict[str, int]:
    session = make_session({"Referer": BASE_URL + "/"})
    db.save_store_info(STORE_ID, query_zip=format_zip(zip_code), name="Zaffari (online, Porto Alegre)",
                       city="Porto Alegre", state="RS")
    catalog = VtexCatalog(session, BASE_URL, delay=0.1, log_prefix="[zaffari] ")
    segments = catalog.segments(depth=3) or [(None, "all")]
    print(f"[zaffari] {len(segments)} segments")
    seen: set = set()
    total = {"upserted": 0, "skipped": 0, "with_barcode": 0}
    for fq, label in segments:
        batch = []
        for product in catalog.iter_products(fq, label=label):
            pid = str(product.get("productId") or "")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            offer = vtex_offer(product, store_id=STORE_ID, base_url=BASE_URL, category_label=label, allow_gtin8=False)
            if offer:
                batch.append(offer)
            if limit and len(seen) >= limit:
                break
        if batch:
            r = db.save(batch)
            for k in total:
                total[k] += r[k]
        print(f"[zaffari] {str(label)[:45]:<45} +{len(batch):>4} (total {len(seen)})")
        if limit and len(seen) >= limit:
            break
    return total


if __name__ == "__main__":
    from markets.common.runner import run_cli
    run_cli(STORE_KEY, scrape)

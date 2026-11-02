"""
scraper_mambo.py - Mambo Supermercados, Sao Paulo (https://www.mambo.com.br)

Platform : VTEX IO (account `mambodelivery`), public catalog_system API (markets/common/vtex.py).
Listing  : one root category "Mambo" with 14 departments -> 33 segments under the 2,500 cap,
           products/search?fq=C:/1589/<dept>/... (50/page). ~18k products.
Store    : 9 stores (Jardins, Vila Madalena, Santana, Vila Romana, Tatuape, Brooklin,
           Leopoldina, Morumbi, Alphaville) as sellers mambodelivery2xx; regions picks one per
           CEP and some CEPs are not served. PRICES ARE NATIONAL (measured 2026-10-27: checkout
           simulation of 150 SKUs on 3 sellers, 1 differing promo). The catalog answers with
           seller "1", ListPrice/Price carry the promos. One run, store_id "mambo".
Barcode  : inline items[0].ean on every product; ~92% valid GTIN overall, ~100% on packaged
           goods (produce, butcher and bakery carry "2000xxx" PLU codes, dropped).
Runtime  : ~12 min. ~2.9k of the ~17.8k products are out of stock with price 0 and are skipped.
"""

from __future__ import annotations

from typing import Dict, Optional

from markets.common.geo import format_zip
from markets.common.http import make_session
from markets.common.vtex import VtexCatalog, vtex_offer

STORE_KEY = "mambo"
BASE_URL = "https://www.mambo.com.br"
STORE_ID = "mambo"  # national prices (measured 2026-10-27)


def scrape(db, zip_code: str, limit: Optional[int] = None) -> Dict[str, int]:
    session = make_session({"Referer": BASE_URL + "/"})
    db.save_store_info(STORE_ID, query_zip=format_zip(zip_code), name="Mambo (online)",
                       city="São Paulo", state="SP")
    catalog = VtexCatalog(session, BASE_URL, delay=0.1, log_prefix="[mambo] ")
    segments = catalog.segments(depth=3) or [(None, "all")]
    print(f"[mambo] {len(segments)} segments")
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
        print(f"[mambo] {str(label)[:45]:<45} +{len(batch):>4} (total {len(seen)})")
        if limit and len(seen) >= limit:
            break
    return total


if __name__ == "__main__":
    from markets.common.runner import run_cli
    run_cli(STORE_KEY, scrape)

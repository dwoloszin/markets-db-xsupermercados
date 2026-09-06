"""
scraper_rossi.py - Rossi Delivery (https://www.rossidelivery.com.br)

Platform : VipCommerce, organisation 63 (see markets/common/vipcommerce.py).
           The old version drove a headless browser; the JSON API is public after a
           `auth/loja/login` call, ~10x faster and works on GitHub runners.
Store    : delivery hub CD 1 (pickup CDs have no catalogue) -> store_id "rossi:1:1".
Barcode  : inline codigo_barras (~92%).
Runtime  : ~2-3 min for ~15k products (20 per page).
"""

from __future__ import annotations

from typing import Dict, Optional

from markets.common.vipcommerce import VipCommerceMarket

STORE_KEY = "rossi"
MARKET = VipCommerceMarket(store_key=STORE_KEY, domain="rossidelivery.com.br",
                           web_base="https://www.rossidelivery.com.br", org_id=63)


def scrape(db, zip_code: str, limit: Optional[int] = None) -> Dict[str, int]:
    return MARKET.scrape(db, zip_code, limit)


if __name__ == "__main__":
    from markets.common.runner import run_cli
    run_cli(STORE_KEY, scrape)

"""
scraper_spani.py - Spani Atacadista online (https://www.spanionline.com.br)

Platform : VipCommerce, organisation 67 (see markets/common/vipcommerce.py). Same JSON API
           as Rossi/Davo after the `auth/loja/login` call with the shared storefront key.
Store    : delivery hub CD 1 -> store_id "spani:1:1" (national prices, like the other
           VipCommerce tenants).
Barcode  : inline codigo_barras on every product; ~95% valid GTIN (the rest are PLU codes
           of weighed items). Measured 2026-09-22 on a 120-product sample.
Runtime  : ~2-3 min for ~4k products (20 per page, 14 departments).
"""

from __future__ import annotations

from typing import Dict, Optional

from markets.common.vipcommerce import VipCommerceMarket

STORE_KEY = "spani"
MARKET = VipCommerceMarket(store_key=STORE_KEY, domain="spanionline.com.br",
                           web_base="https://www.spanionline.com.br", org_id=67)


def scrape(db, zip_code: str, limit: Optional[int] = None) -> Dict[str, int]:
    return MARKET.scrape(db, zip_code, limit)


if __name__ == "__main__":
    from markets.common.runner import run_cli
    run_cli(STORE_KEY, scrape)

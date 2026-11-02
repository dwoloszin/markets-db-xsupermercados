"""
scraper_violeta.py - Violeta Express, zona oeste/norte SP (https://www.violetaexpress.com.br)

Platform : VipCommerce, organisation 121 (see markets/common/vipcommerce.py). Same JSON API
           and shared storefront login key as Rossi/Davo/Spani/Hirota.
Store    : 3 stores (CD 1 Loja 01 Vila Gomes, CD 2 Loja 06 Vila Palmeiras, CD 11 Loja 10
           Jardim Iris) plus their "CD Express" hubs, all with the SAME prices (measured
           2026-09-22: 0-4 differing products in 75-137 common ones). Scraped from CD 1 ->
           store_id "violeta:1:1", national prices.
Barcode  : inline codigo_barras on every product; ~71% pass the GTIN check digit (the rest
           are internal codes). Measured 2026-09-22.
Runtime  : ~5 min for ~10.4k products (20 per page, 13 departments).
"""

from __future__ import annotations

from typing import Dict, Optional

from markets.common.vipcommerce import VipCommerceMarket

STORE_KEY = "violeta"
MARKET = VipCommerceMarket(store_key=STORE_KEY, domain="violetaexpress.com.br",
                           web_base="https://www.violetaexpress.com.br", org_id=121)


def scrape(db, zip_code: str, limit: Optional[int] = None) -> Dict[str, int]:
    return MARKET.scrape(db, zip_code, limit)


if __name__ == "__main__":
    from markets.common.runner import run_cli
    run_cli(STORE_KEY, scrape)

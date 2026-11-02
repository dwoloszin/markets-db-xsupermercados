"""
scraper_hirota.py - Hirota Food Supermercados online (https://www.lojahirota.com.br)

Platform : VipCommerce, organisation 171 (see markets/common/vipcommerce.py). Same JSON API
           and shared storefront login key as Rossi/Davo/Spani.
Store    : 4 delivery hubs (CD 1 Nazaré/Ipiranga SP, 2 Granja Viana, 3 São Bernardo,
           4 Morumbi) with the SAME prices (measured 2026-09-22: 0-1 differing product in
           130+ common ones) plus 8 "Encomenda" pickup stores with a tiny order-only
           catalogue. Scraped from CD 1 -> store_id "hirota:1:1", national prices.
Barcode  : inline codigo_barras on every product; ~97.6% valid GTIN (rest are PLU codes).
Runtime  : ~5 min for ~10k products (20 per page, 21 departments).
"""

from __future__ import annotations

from typing import Dict, Optional

from markets.common.vipcommerce import VipCommerceMarket

STORE_KEY = "hirota"
MARKET = VipCommerceMarket(store_key=STORE_KEY, domain="lojahirota.com.br",
                           web_base="https://www.lojahirota.com.br", org_id=171)


def scrape(db, zip_code: str, limit: Optional[int] = None) -> Dict[str, int]:
    return MARKET.scrape(db, zip_code, limit)


if __name__ == "__main__":
    from markets.common.runner import run_cli
    run_cli(STORE_KEY, scrape)

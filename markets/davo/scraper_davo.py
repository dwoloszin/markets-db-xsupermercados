"""
scraper_davo.py - Davo Supermercados (https://www.davo.com.br)

Platform : VipCommerce, organisation 399 (see markets/common/vipcommerce.py).
           Davo migrated from its own storefront API (/api/products, gone since
           mid-2026: the URL now redirects to the SPA shell) to VipCommerce.
Store    : delivery hub CD 1 -> store_id "davo:1:1".
Barcode  : inline codigo_barras.
"""

from __future__ import annotations

from typing import Dict, Optional

from markets.common.vipcommerce import VipCommerceMarket

STORE_KEY = "davo"
MARKET = VipCommerceMarket(store_key=STORE_KEY, domain="davo.com.br",
                           web_base="https://www.davo.com.br", org_id=399)


def scrape(db, zip_code: str, limit: Optional[int] = None) -> Dict[str, int]:
    return MARKET.scrape(db, zip_code, limit)


if __name__ == "__main__":
    from markets.common.runner import run_cli
    run_cli(STORE_KEY, scrape)

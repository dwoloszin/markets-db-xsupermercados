"""
scraper_xsupermercados.py - X Supermercados (https://www.xsupermercados.com.br)

Platform : applay (see markets/common/applay.py). Token via Next.js server action
           (ids below rotate on deploys; the client discovers new ones automatically).
Store    : nearest store is picked by the backend from the CEP coordinates.
Listing  : enav/produtos_corredor (corridor = whole catalogue) then enav/produtos.
Barcode  : inline `material`/`sku` (~90%).
"""

from __future__ import annotations

import os
from typing import Dict, Optional

from markets.common.applay import ApplayMarket

STORE_KEY = "xsupermercados"
MARKET = ApplayMarket(
    store_key=STORE_KEY,
    api_base="https://api-xsupermercados.applay.tech/api2/ecommerce",
    web_base="https://www.xsupermercados.com.br",
    corridor_id=os.getenv("XSUPER_DEFAULT_CORRIDOR_ID", "63335f603aa29725e0119211"),
    known_action_ids=[os.getenv("XSUPER_TOKEN_ACTION_ID", ""), "bfb8781927026ab6b741b817d0c1ebd49281b720",
                      "cdd7f568183fa8c8873f4ad115a43ed1ef0a473a", "b5240a22b66e2990db00381bcd0e987be41e7f34"],
    default_position=(-23.506567, -46.601181),
    use_api_auth=True,
)


def scrape(db, zip_code: str, limit: Optional[int] = None) -> Dict[str, int]:
    return MARKET.scrape(db, zip_code, limit, strategy="corridor")


if __name__ == "__main__":
    from markets.common.runner import run_cli
    run_cli(STORE_KEY, scrape)

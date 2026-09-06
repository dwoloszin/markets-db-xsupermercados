"""
scraper_barbosa.py - Barbosa Supermercados (https://www.barbosasupermercados.com.br)

Platform : applay (see markets/common/applay.py). Token via POST /api/auth (simple),
           server-action fallback kept.
Store    : nearest store is picked by the backend from the CEP coordinates.
Listing  : enav/produtos per department (department names come from the same endpoint).
Barcode  : inline `material`/`sku` (~87%).
"""

from __future__ import annotations

import os
from typing import Dict, Optional

from markets.common.applay import ApplayMarket

STORE_KEY = "barbosa"
MARKET = ApplayMarket(
    store_key=STORE_KEY,
    api_base="https://api-barbosa.applay.tech/api2/ecommerce",
    web_base="https://www.barbosasupermercados.com.br",
    corridor_id=os.getenv("BARBOSA_DEFAULT_CORRIDOR_ID", ""),
    known_action_ids=[os.getenv("BARBOSA_TOKEN_ACTION_ID", "")],
    default_position=(-23.506567, -46.601181),
    use_api_auth=True,
)


def scrape(db, zip_code: str, limit: Optional[int] = None) -> Dict[str, int]:
    return MARKET.scrape(db, zip_code, limit, strategy="departments")


if __name__ == "__main__":
    from markets.common.runner import run_cli
    run_cli(STORE_KEY, scrape)

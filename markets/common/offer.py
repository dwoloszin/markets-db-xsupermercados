"""
offer.py - The common offer record every scraper produces.

All scrapers return plain dicts built with `make_offer()`. Keys map 1:1 to the
`offers` table columns (see db/db_manager.py). Keeping one builder means every
market fills the same fields the same way, and the DB layer never needs
per-market special cases.

Price rules (same for every market):
  * regular_price  = the "list"/normal shelf price
  * promo_price    = the effective discounted price, ONLY when strictly lower
                     than regular_price; otherwise None
  * discount_pct   = derived, never taken from the site
  * promo_min_quantity = quantity needed to get promo_price (wholesale tiers,
                     "leve 3 pague 2"); None when the promo applies to 1 unit
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

OFFER_FIELDS = [
    "product_id", "store_id", "product_name", "brand", "category_path",
    "barcode", "barcode_source",
    "regular_price", "promo_price", "discount_pct", "promo_min_quantity", "promo_end_at",
    "offer_tag", "offer_name", "app_membership_required",
    "unit", "is_available", "stock", "product_url", "image_url", "scraped_at",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_brl(value: Any) -> Optional[float]:
    """Parse 'R$ 1.234,56' / '12,90' / 12.9 / '12.90' into float. None when impossible."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    text = str(value).strip().replace("R$", "").replace("\xa0", "").replace(" ", "")
    if not text:
        return None
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        return round(float(text), 2)
    except ValueError:
        return None


def pick_prices(list_price: Any, sale_price: Any) -> Tuple[Optional[float], Optional[float]]:
    """
    Normalise a (list, sale) pair into (regular_price, promo_price).
    promo is kept only when it is a real, strictly lower price.
    """
    reg = parse_brl(list_price)
    sale = parse_brl(sale_price)
    if reg is not None and reg <= 0:
        reg = None
    if sale is not None and sale <= 0:
        sale = None
    if reg is None:
        return sale, None
    if sale is None or sale >= reg:
        return reg, None
    return reg, sale


def clean_text(value: Any, max_len: int = 500) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("name") or value.get("nome") or ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text[:max_len] or None


def slugify(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def norm_name(value: Any) -> str:
    """Normalised product name used for legacy seeding / cross-market matching."""
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def make_offer(
    *,
    product_id: Any,
    store_id: str,
    product_name: Any,
    regular_price: Any = None,
    promo_price: Any = None,
    barcode: Optional[str] = None,
    barcode_source: Optional[str] = None,
    brand: Any = None,
    category_path: Any = None,
    promo_min_quantity: Any = None,
    promo_end_at: Any = None,
    offer_tag: Any = None,
    offer_name: Any = None,
    app_membership_required: Optional[bool] = None,
    unit: Any = None,
    is_available: Optional[bool] = None,
    stock: Any = None,
    product_url: Any = None,
    image_url: Any = None,
) -> Optional[Dict[str, Any]]:
    """Build a normalised offer dict; returns None when the record is unusable."""
    pid = str(product_id or "").strip()
    name = clean_text(product_name, 300)
    if not pid or not name:
        return None
    reg, promo = pick_prices(regular_price, promo_price)
    if reg is None:
        return None  # no usable price -> not an offer
    try:
        min_q = int(float(promo_min_quantity)) if promo_min_quantity not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        min_q = None
    if min_q is not None and min_q <= 1:
        min_q = None
    try:
        stock_val = int(float(stock)) if stock not in (None, "") else None
    except (TypeError, ValueError):
        stock_val = None
    return {
        "product_id": pid,
        "store_id": str(store_id or "").strip(),
        "product_name": name,
        "brand": clean_text(brand, 120),
        "category_path": clean_text(category_path, 300),
        "barcode": barcode or None,
        "barcode_source": (barcode_source if barcode else None),
        "regular_price": reg,
        "promo_price": promo,
        "discount_pct": round((1 - promo / reg) * 100, 1) if promo and reg else None,
        "promo_min_quantity": min_q,
        "promo_end_at": clean_text(promo_end_at, 40),
        "offer_tag": clean_text(offer_tag, 200),
        "offer_name": clean_text(offer_name, 300),
        "app_membership_required": bool(app_membership_required) if app_membership_required is not None else None,
        "unit": clean_text(unit, 40),
        "is_available": is_available,
        "stock": stock_val,
        "product_url": clean_text(product_url, 600),
        "image_url": clean_text(image_url, 600),
        "scraped_at": now_iso(),
    }

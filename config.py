"""
config.py - Non-secret configuration + the market registry.

RULE: secrets (DATABASE_URL_*, tokens) live ONLY in .env / GitHub secrets.
Everything else is a plain value here.

The registry below is the single source of truth used by:
  * main.py            -> which subprocess to launch per market
  * db/db_manager.py   -> which DATABASE_URL_* env var each market uses
  * deploy/deploy.py   -> per-market GitHub repo + cron schedule
"""

from __future__ import annotations

import os
from typing import Dict, Optional

# ZIP (CEP) used to pick the physical store for every market.
# Override per run with SCRAPE_ZIP_CODE=... or `--zip`.
#   zona norte 02401-100 | zona sul 04646-000 | zona leste 08032-230 | zona oeste 06290-170
SCRAPE_ZIP_CODE: str = (os.getenv("SCRAPE_ZIP_CODE") or "08032-230").strip()

# Rows whose barcode enrichment returned "not found" are retried after this many days.
BARCODE_RETRY_NOT_FOUND_DAYS: int = 14

# price_history rows older than this are pruned (keeps Neon under the free-tier size).
PRICE_HISTORY_KEEP_DAYS: int = 180

# Offers not refreshed for this many hours are flipped to is_available=false.
STALE_HOURS: int = 48


def _int_env(key: str, default: Optional[int]) -> Optional[int]:
    raw = (os.getenv(key) or "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        return default
    return val if val > 0 else None


SCRAPE_LIMIT: Optional[int] = _int_env("SCRAPE_LIMIT", None)


# ---------------------------------------------------------------------------
# Market registry
#   key        : store key (folder name under markets/, CLI name, repo suffix)
#   name       : display name
#   db_env     : DATABASE_URL_* variable (one Neon project per market)
#   enrich     : True when the market has an enrich_ean_<key>.py step
#   barcode    : where barcodes come from (documentation only)
#   group      : markets sharing a backend/WAF run one at a time
#   cron       : UTC schedule used by deploy/deploy.py - every 4 h, one market
#                starting every 15 min so no two markets run at the same time
# ---------------------------------------------------------------------------
STORES: Dict[str, Dict[str, object]] = {
    "swift":          {"name": "Swift",           "db_env": "DATABASE_URL_SWIFT",          "enrich": False, "barcode": "inline (VTEX ean)",                    "group": None,     "cron": "45 3,7,11,15,19,23 * * *"},
    "giga":           {"name": "Giga",            "db_env": "DATABASE_URL_GIGA",           "enrich": False, "barcode": "inline (VTEX ean)",                    "group": None,     "cron": "45 1,5,9,13,17,21 * * *"},
    "davo":           {"name": "Davo",            "db_env": "DATABASE_URL_DAVO",           "enrich": False, "barcode": "inline (VipCommerce codigo_barras)",   "group": "vipcommerce", "cron": "0 3,7,11,15,19,23 * * *"},
    "samsclub":       {"name": "Sam's Club",      "db_env": "DATABASE_URL_SAMSCLUB",       "enrich": False, "barcode": "inline (VTEX ean)",                    "group": None,     "cron": "0 2,6,10,14,18,22 * * *"},
    "oba":            {"name": "Oba Hortifruti",  "db_env": "DATABASE_URL_OBA",            "enrich": False, "barcode": "inline (VTEX ean, ~60%)",              "group": None,     "cron": "30 2,6,10,14,18,22 * * *"},
    "rossi":          {"name": "Rossi",           "db_env": "DATABASE_URL_ROSSI",          "enrich": False, "barcode": "inline (VipCommerce codigo_barras)",   "group": "vipcommerce", "cron": "15 1,5,9,13,17,21 * * *"},
    "tenda":          {"name": "Tenda Atacado",   "db_env": "DATABASE_URL_TENDA",          "enrich": False, "barcode": "inline (API barcode)",                 "group": None,     "cron": "15 2,6,10,14,18,22 * * *"},
    "xsupermercados": {"name": "XSupermercados",  "db_env": "DATABASE_URL_XSUPERMERCADOS", "enrich": False, "barcode": "inline (applay material/sku)",         "group": "applay", "cron": "15 3,7,11,15,19,23 * * *"},
    "barbosa":        {"name": "Barbosa",         "db_env": "DATABASE_URL_BARBOSA",        "enrich": False, "barcode": "inline (applay material/sku)",         "group": "applay", "cron": "45 2,6,10,14,18,22 * * *"},
    "atacadao":       {"name": "Atacadao",        "db_env": "DATABASE_URL_ATACADAO",       "enrich": True,  "barcode": "VTEX catalog batch lookup by productId","group": None,     "cron": "0 1,5,9,13,17,21 * * *"},
    "carrefour":      {"name": "Carrefour",       "db_env": "DATABASE_URL_CARREFOUR",      "enrich": True,  "barcode": "image file name (GTIN) + PDP JSON-LD", "group": None,     "cron": "30 0,4,8,12,16,20 * * *"},
    "sonda":          {"name": "Sonda Delivery",  "db_env": "DATABASE_URL_SONDA",          "enrich": True,  "barcode": "image path (EAN) + PDP JSON-LD gtin",  "group": None,     "cron": "0 0,4,8,12,16,20 * * *"},
    "extra":          {"name": "Extra",           "db_env": "DATABASE_URL_EXTRA",          "enrich": True,  "barcode": "PDP HTML \"ean\" (GPA)",               "group": "gpa",    "cron": "30 1,5,9,13,17,21 * * *"},
    "paodeacucar":    {"name": "Pao de Acucar",   "db_env": "DATABASE_URL_PAODEACUCAR",    "enrich": True,  "barcode": "PDP HTML \"ean\" (GPA)",               "group": "gpa",    "cron": "45 0,4,8,12,16,20 * * *"},
    "nagumo":         {"name": "Nagumo",          "db_env": "DATABASE_URL_NAGUMO",         "enrich": False, "barcode": "legacy/crossfill only (no upc any more)", "group": None,     "cron": "15 0,4,8,12,16,20 * * *"},
    "higas":          {"name": "Higas",           "db_env": "DATABASE_URL_HIGAS",          "enrich": False, "barcode": "legacy/crossfill only (API v5 has none)","group": None,   "cron": "30 3,7,11,15,19,23 * * *"},
}


def store_meta(key: str) -> Dict[str, object]:
    try:
        return STORES[key]
    except KeyError:
        raise KeyError(f"unknown store '{key}'. Known: {', '.join(STORES)}") from None

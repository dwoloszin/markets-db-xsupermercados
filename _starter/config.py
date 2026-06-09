"""
config.py — Central configuration for the store scraper system.

RULE:
  - Secrets (API keys, DB connection strings, tokens) come from .env — never hardcoded here.
  - Everything else is hardcoded here. Change behaviour by editing this file.
"""

import os
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# SECRETS — read from environment only
# ─────────────────────────────────────────────────────────────────────────────

def _secret(key: str) -> str:
    return os.getenv(key, "").strip()


def _optional_int_env(key: str, default: Optional[int]) -> Optional[int]:
    raw = os.getenv(key)
    if raw is None:
        return default
    raw = str(raw).strip()
    if raw == "":
        return None
    try:
        parsed = int(raw)
    except ValueError:
        return default
    return parsed if parsed > 0 else None


# Database connection strings
DATABASE_URL:         str = _secret("DATABASE_URL")
DATABASE_URL_MANAGER: str = _secret("DATABASE_URL_MANAGER") or DATABASE_URL

# ── Add your stores here ──────────────────────────────────────────────────────
# Pattern: "Store Name" -> _secret("DATABASE_URL_SUFFIX") or DATABASE_URL
# The suffix must match what you set in .env and in STORE_DB_SUFFIX below.
STORE_DATABASE_URLS: dict = {
    # "Drogaria Example": _secret("DATABASE_URL_EXAMPLE") or DATABASE_URL,
}

# AI provider keys
OPENROUTER_API_KEY: str = _secret("OPENROUTER_API_KEY")
GEMINI_API_KEY:     str = _secret("GEMINI_API_KEY")
XAI_API_KEY:        str = _secret("XAI_API_KEY")
HF_TOKEN:           str = _secret("HF_TOKEN")
HF_SPACE_ID:        str = _secret("HF_SPACE_ID")

# GitHub token for DB archive push
DB_ARCHIVE_GITHUB_TOKEN: str = _secret("DB_ARCHIVE_GITHUB_TOKEN")
DB_ARCHIVE_GITHUB_REPO:  str = _secret("DB_ARCHIVE_GITHUB_REPO")


# ─────────────────────────────────────────────────────────────────────────────
# SCRAPER SETTINGS
# ─────────────────────────────────────────────────────────────────────────────

SCRAPE_ZIP_CODE:          str           = "01310-100"  # default ZIP code
SCRAPE_MODE:              str           = "all_departamentos"
SCRAPE_LIMIT:             Optional[int] = _optional_int_env("SCRAPE_LIMIT", None)
SKIP_UPDATED_WITHIN_DAYS: float         = 0     # 0 = always re-scrape
SKIP_BARCODE_INFERENCE:   bool          = False
IMAGE_MATCH_ENABLED:      bool          = True


# ─────────────────────────────────────────────────────────────────────────────
# DATABASE SETTINGS
# ─────────────────────────────────────────────────────────────────────────────

PG_UPSERT_BATCH_SIZE:               int   = 200
DB_INIT_ALL_MARKET_DBS_ON_STARTUP:  bool  = False
BARCODE_SYNC_MIN_INTERVAL_HOURS:    float = 6.0


# ─────────────────────────────────────────────────────────────────────────────
# STORE DEFINITIONS
# ─────────────────────────────────────────────────────────────────────────────

# Add stores here. The key must match how your scraper identifies itself.
# Tier 1: scraper returns barcodes inline (>=96% coverage)
# Tier 2: partial barcodes
# Tier 3: no inline barcodes — relies on cross-store catalog lookup
STORE_TIER: dict = {
    # "Drogaria Example": 1,
}

# DB URL env-var suffix per store — used by deploy.py and the scrape workflow
STORE_DB_SUFFIX: dict = {
    # "Drogaria Example": "EXAMPLE",
}


# ─────────────────────────────────────────────────────────────────────────────
# BARCODE MATCHING SETTINGS
# ─────────────────────────────────────────────────────────────────────────────

BARCODE_ALLOWED_LENGTHS:     set   = {12, 13, 14}
BARCODE_HEURISTIC_THRESHOLD: float = 0.90
BARCODE_HEURISTIC_MARGIN:    float = 0.04
BARCODE_AI_THRESHOLD:        float = 0.70
BARCODE_PROGRESS_EVERY:      int   = 25
BARCODE_BLACKLIST_THRESHOLD: int   = 10
AI_MIN_BEST_SCORE_FOR_CALL:  float = 0.65


# ─────────────────────────────────────────────────────────────────────────────
# AI / MODEL PROVIDERS
# ─────────────────────────────────────────────────────────────────────────────

ENABLE_AI_BARCODE_MATCH: bool  = True
AI_MAX_CALLS_PER_RUN:    int   = 0
AI_CALL_DELAY_SECONDS:   float = 0.0
AI_BATCH_SIZE:           int   = 10
AI_PROVIDER_ORDER:       list  = ["lmstudio", "openrouter", "xai", "gemini", "huggingface"]

OPENROUTER_BARCODE_MATCH_MODEL: str  = "meta-llama/llama-3.3-70b-instruct:free"
OPENROUTER_SITE_URL:            str  = "https://local.barcode.matcher"
OPENROUTER_SITE_NAME:           str  = "stores-db-barcode-matcher"
GEMINI_BARCODE_MATCH_MODEL:     str  = "gemini-2.5-flash"
XAI_BARCODE_MATCH_MODEL:        str  = "grok-3-mini"
LM_STUDIO_ENABLED:              bool = True
LM_STUDIO_BASE_URL:             str  = "http://127.0.0.1:1234"
LM_STUDIO_BARCODE_MATCH_MODEL:  str  = "deepseek-r1-distill-qwen-7b"
LM_STUDIO_TIMEOUT:              int  = 300
LM_STUDIO_BATCH_SIZE:           int  = 3
AI_REMOTE_TIMEOUT_SECONDS:      int  = 60


# ─────────────────────────────────────────────────────────────────────────────
# DB STORAGE CONTROLLER
# Archives old rows to Parquet when any DB approaches the 500 MB Neon limit
# ─────────────────────────────────────────────────────────────────────────────

DB_ARCHIVE_THRESHOLD_BYTES: int  = 420 * 1024 * 1024
DB_ARCHIVE_TABLES:          list = ["price_history", "barcode_inference_state", "offers"]
DB_ARCHIVE_KEEP_ROWS:       int  = 50_000
DB_ARCHIVE_BRANCH:          str  = "data-archive"
DB_ARCHIVE_TEMP_DIR:        str  = "/tmp/db_archive"

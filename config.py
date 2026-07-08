"""
config.py — Central configuration for the market scraper system.

RULE:
  - Secrets (API keys, DB connection strings, tokens) are read from
    environment variables / .env — they are NEVER hardcoded here.
  - Everything else is hardcoded here as a plain Python value.
    Change behaviour by editing this file directly, not .env.

.env (and GitHub Actions Secrets) should only contain:
  DATABASE_URL, DATABASE_URL_*, HF_TOKEN, HF_SPACE_ID,
  OPENROUTER_API_KEY, GEMINI_API_KEY, XAI_API_KEY,
  DB_ARCHIVE_GITHUB_TOKEN, DB_ARCHIVE_GITHUB_REPO
"""

import os
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# SECRETS — read from environment only, never hardcoded
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

MARKET_DATABASE_URLS: dict = {
    "Rossi":          _secret("DATABASE_URL_ROSSI")          or DATABASE_URL,
    "Atacadão":       _secret("DATABASE_URL_ATACADAO")       or DATABASE_URL,
    "Nagumo":         _secret("DATABASE_URL_NAGUMO")         or DATABASE_URL,
    "Higas":          _secret("DATABASE_URL_HIGAS")          or DATABASE_URL,
    "Swift":          _secret("DATABASE_URL_SWIFT")          or DATABASE_URL,
    "Sonda Delivery": _secret("DATABASE_URL_SONDA")          or DATABASE_URL,
    "XSupermercados": _secret("DATABASE_URL_XSUPERMERCADOS") or DATABASE_URL,
    "Barbosa":        _secret("DATABASE_URL_BARBOSA")        or DATABASE_URL,
    "Carrefour":      _secret("DATABASE_URL_CARREFOUR")      or DATABASE_URL,
    "Oba Hortifruti": _secret("DATABASE_URL_OBA")            or DATABASE_URL,
    "Extra":          _secret("DATABASE_URL_EXTRA")          or DATABASE_URL,
    "Pão de Açúcar":  _secret("DATABASE_URL_PAODEACUCAR")    or DATABASE_URL,
    "Tenda Atacado":  _secret("DATABASE_URL_TENDA")          or DATABASE_URL,
    "Davo":           _secret("DATABASE_URL_DAVO")           or DATABASE_URL,
    "Giga":           _secret("DATABASE_URL_GIGA")           or DATABASE_URL,
}

# AI provider keys
HF_TOKEN:          str = _secret("HF_TOKEN")
HF_SPACE_ID:       str = _secret("HF_SPACE_ID")
OPENROUTER_API_KEY:str = _secret("OPENROUTER_API_KEY")
GEMINI_API_KEY:    str = _secret("GEMINI_API_KEY")
XAI_API_KEY:       str = _secret("XAI_API_KEY")

# GitHub token for DB archive push
DB_ARCHIVE_GITHUB_TOKEN: str = _secret("DB_ARCHIVE_GITHUB_TOKEN")
DB_ARCHIVE_GITHUB_REPO:  str = _secret("DB_ARCHIVE_GITHUB_REPO")


# ─────────────────────────────────────────────────────────────────────────────
# SCRAPER SETTINGS
# ─────────────────────────────────────────────────────────────────────────────
# zona norte: 02401-100
# Zona sul:   04646-000
# Zona leste: 08032-230
# Zona oeste: 06290-170



SCRAPE_ZIP_CODE:          str          = '08032-230' #os.getenv("SCRAPE_ZIP_CODE", "02065-040").strip()  # override via env or edit default
SCRAPE_MODE:              str          = "all_departamentos"
# Same default for local and GitHub Actions. Change this value when you want
# both environments to run test/full mode consistently.
SCRAPE_LIMIT:             Optional[int]= _optional_int_env("SCRAPE_LIMIT", None)
SKIP_UPDATED_WITHIN_DAYS: float        = 0     # 0 = always re-scrape
SKIP_BARCODE_INFERENCE:   bool         = False  # skip offers with existing barcode inference (any state)
IMAGE_MATCH_ENABLED:      bool         = True  # enable CLIP image similarity as a scoring boost


# ─────────────────────────────────────────────────────────────────────────────
# DATABASE SETTINGS
# ─────────────────────────────────────────────────────────────────────────────

PG_UPSERT_BATCH_SIZE: int = 200
# If True, startup initializes schema for every configured market DB URL.
# If False, only manager DB is initialized at startup and market DBs are
# initialized lazily on first use.
DB_INIT_ALL_MARKET_DBS_ON_STARTUP: bool = False
# Minimum interval between full trusted-market catalog syncs.
# Set 0 to sync on every call (legacy behavior).
BARCODE_SYNC_MIN_INTERVAL_HOURS: float = 6.0


# ─────────────────────────────────────────────────────────────────────────────
# MARKET DEFINITIONS
# ─────────────────────────────────────────────────────────────────────────────

# Markets with a fixed store_id (no dynamic store resolution needed)
FIXED_STORE_MARKETS: dict = {
    "Swift":          "swift:sp:sao-paulo",
    "Oba Hortifruti": "obahortifruti",
    "Extra":          "extramercado",
    "Pão de Açúcar":  "paodeacucar",
    "Nagumo":         "ALL_CATALOG",
    "Davo":           "davo.com.br",
    "Giga":           "giga.com.vc",
}

# GPA platform store IDs (Extra / Pão de Açúcar share the GPA API)
GPA_STORE_IDS: dict = {
    "Extra":         483,
    "Pão de Açúcar": 532,
}

# Tier 1: inline barcodes ≥96% — run first to build the catalog
# Tier 2: partial barcodes        — run after Tier 1
# Tier 3: no inline barcodes      — run last, use cross-market catalog lookup
MARKET_TIER: dict = {
    "Swift":          1,  "Carrefour":      1,
    "XSupermercados": 1,  "Barbosa":        1,
    "Rossi":          1,  "Extra":          1,
  "Pão de Açúcar":  1,  "Oba Hortifruti": 1,
  "Davo":           1,
  "Giga":           1,
    "Sonda Delivery": 2,
    "Atacadão":       3,  "Nagumo":         3,  "Higas": 3,
    "Tenda Atacado":  1,  # Has inline barcodes — run early to build the catalog
}


# ─────────────────────────────────────────────────────────────────────────────
# BARCODE MATCHING SETTINGS
# ─────────────────────────────────────────────────────────────────────────────

BARCODE_ALLOWED_LENGTHS:     set   = {12, 13, 14}
BARCODE_HEURISTIC_THRESHOLD: float = 0.90
BARCODE_HEURISTIC_MARGIN:    float = 0.04
BARCODE_AI_THRESHOLD:        float = 0.70
BARCODE_PROGRESS_EVERY:      int   = 25
BARCODE_BLACKLIST_THRESHOLD: int   = 10   # consecutive misses before an offer is blacklisted
AI_MIN_BEST_SCORE_FOR_CALL:  float = 0.65 # min heuristic score before sending to LLM (lower = more AI coverage)


# ─────────────────────────────────────────────────────────────────────────────
# AI / MODEL PROVIDERS
# ─────────────────────────────────────────────────────────────────────────────

ENABLE_AI_BARCODE_MATCH: bool  = True
AI_MAX_CALLS_PER_RUN:    int   = 0    # 0 = unlimited (local LLM — no cost concern)
AI_CALL_DELAY_SECONDS:   float = 0.0  # no delay needed for local inference
AI_BATCH_SIZE:           int   = 10   # remote providers batch size
AI_PROVIDER_ORDER:       list  = ["lmstudio", "openrouter", "xai", "gemini", "huggingface"]

# Model names (not secrets — just identifiers)
OPENROUTER_BARCODE_MATCH_MODEL: str = "meta-llama/llama-3.3-70b-instruct:free"
OPENROUTER_SITE_URL:            str = "https://local.barcode.matcher"
OPENROUTER_SITE_NAME:           str = "markets-db-barcode-matcher"
GEMINI_BARCODE_MATCH_MODEL:     str = "gemini-2.5-flash"
XAI_BARCODE_MATCH_MODEL:        str = "grok-3-mini"
LM_STUDIO_ENABLED:              bool = True
LM_STUDIO_BASE_URL:             str  = "http://127.0.0.1:1234"
LM_STUDIO_BARCODE_MATCH_MODEL:  str  = "deepseek-r1-distill-qwen-7b"
LM_STUDIO_TIMEOUT:              int  = 300   # seconds; reasoning models need time for <think> tokens
LM_STUDIO_BATCH_SIZE:           int  = 3     # small batch for reasoning models — they need tokens for <think>
AI_REMOTE_TIMEOUT_SECONDS:      int  = 60    # seconds; Gemini/OpenRouter timeout for batch requests


# ─────────────────────────────────────────────────────────────────────────────
# HIGAS ENRICHMENT SETTINGS
# ─────────────────────────────────────────────────────────────────────────────

ATACADAO_CATALOG_EAN_MAX_CALLS: int = 0
ATACADAO_CATALOG_EAN_WORKERS: int = 8

HIGAS_ENRICHMENT_BASE_DELAY:       float = 0.06
HIGAS_ENRICHMENT_JITTER_SECONDS:   float = 0.02
HIGAS_ENRICHMENT_BURST_SIZE:       int   = 300
HIGAS_ENRICHMENT_BURST_COOLDOWN:   int   = 8
HIGAS_ENRICHMENT_MAX_CALLS:        int   = 2000
HIGAS_ENRICHMENT_SAFE_CALL_CAP:    int   = 2000
HIGAS_ENRICHMENT_FLUSH_EVERY:      int   = 300
HIGAS_ENRICHMENT_FLUSH_COOLDOWN:   int   = 2
HIGAS_ENRICHMENT_BETWEEN_RUNS_COOLDOWN_SECONDS: int = 8
HIGAS_ENRICHMENT_MAX_429_COOLDOWNS:int   = 8


# ─────────────────────────────────────────────────────────────────────────────
# DB STORAGE CONTROLLER
# Archives old rows to Parquet when any DB approaches the 500 MB Neon limit
# ─────────────────────────────────────────────────────────────────────────────

DB_ARCHIVE_THRESHOLD_BYTES: int  = 420 * 1024 * 1024   # 420 MB — 80 MB headroom before 500 MB limit
DB_ARCHIVE_TABLES:          list = ["price_history", "barcode_inference_state", "offers"]
DB_ARCHIVE_KEEP_ROWS:       int  = 50_000
DB_ARCHIVE_BRANCH:          str  = "data-archive"
DB_ARCHIVE_TEMP_DIR:        str  = "/tmp/db_archive"

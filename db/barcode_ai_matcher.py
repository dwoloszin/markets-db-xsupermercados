import hashlib
import json
import os
import re
import time
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

import requests

import config as _config
from db.db_manager import DatabaseManager
from env_loader import load_env_file

_image_match_enabled: bool = bool(getattr(_config, "IMAGE_MATCH_ENABLED", False))

if _image_match_enabled:
    try:
        from image_matcher import score_candidates_with_images as _score_images
    except ImportError:
        _image_match_enabled = False
        _score_images = None  # type: ignore
else:
    _score_images = None  # type: ignore


class _KnownBarcodesCatalog:
    """
    In-memory inverted index over the product_catalog.
    Eliminates per-offer Postgres round trips during inference.
    Columns expected per row (same order as fetch_all_known_barcodes):
      0 barcode  1 source_market  2 source_market_id  3 canonical_name (product_name)
      4 canonical_brand (brand)  5 canonical_description  6 normalized_name
      7 normalized_brand  8 measure_token  9 image_url
    """

    def __init__(self, rows: List[Tuple[Any, ...]]):
        self._rows = rows
        # Inverted indices: key -> list of row indices
        self._by_brand: Dict[str, List[int]] = {}
        self._by_measure: Dict[str, List[int]] = {}
        self._by_name_token: Dict[str, List[int]] = {}
        for idx, row in enumerate(rows):
            nb = (row[7] or "").strip()
            mt = (row[8] or "").strip()
            nn = (row[6] or "").strip()
            if nb:
                self._by_brand.setdefault(nb, []).append(idx)
            if mt:
                self._by_measure.setdefault(mt, []).append(idx)
            for token in nn.split():
                if len(token) >= 2:
                    self._by_name_token.setdefault(token, []).append(idx)

    def query(
        self,
        target_market: str,
        target_brand: str,
        target_measure: str,
        anchor_token: str,
        extra_tokens: Optional[List[str]] = None,
        limit: int = 250,
    ) -> List[Tuple[Any, ...]]:
        seen: set = set()
        if target_brand:
            seen.update(self._by_brand.get(target_brand, []))
        if target_measure:
            seen.update(self._by_measure.get(target_measure, []))
        if anchor_token:
            seen.update(self._by_name_token.get(anchor_token, []))
        for tok in (extra_tokens or []):
            seen.update(self._by_name_token.get(tok, []))
        results: List[Tuple[Any, ...]] = []
        for idx in seen:
            row = self._rows[idx]
            if row[1] != target_market:
                results.append(row)
            if len(results) >= limit:
                break
        return results


class _KnownBrandsList:
    """
    Frequency-weighted list of known brands from the catalog.
    Tracks brand occurrence counts to prioritize common/trustworthy brands.
    """

    def __init__(self, rows: List[Tuple[Any, ...]]):
        self._brand_counts: Dict[str, int] = {}
        self._total_brands = 0
        
        for row in rows:
            normalized_brand = (row[7] or "").strip()  # normalized_brand is at index 7
            if normalized_brand:
                self._brand_counts[normalized_brand] = self._brand_counts.get(normalized_brand, 0) + 1
                self._total_brands += 1
    
    def get_brand_confidence(self, brand: str, min_frequency: int = 2) -> Optional[float]:
        """
        Get confidence score for a brand (0.0-1.0) based on frequency.
        Returns None if brand is below minimum frequency threshold.
        
        Confidence = (count - 1) / (total / 2) capped at 1.0
        This gives higher scores to more frequent brands while still allowing rare brands.
        """
        count = self._brand_counts.get(brand, 0)
        if count < min_frequency:
            return None
        
        if self._total_brands <= 0:
            return 0.5 if count > 0 else None
        
        # Normalize: more common brands get scores closer to 1.0
        confidence = min(1.0, count / max(5, self._total_brands / 10))
        return confidence
    
    def get_sorted_brands(self, limit: Optional[int] = None) -> List[Tuple[str, int]]:
        """Get brands sorted by frequency (descending)."""
        sorted_brands = sorted(self._brand_counts.items(), key=lambda x: x[1], reverse=True)
        return sorted_brands[:limit] if limit else sorted_brands


class BarcodeAIMatcher:
    STOPWORDS = {
        "com",
        "de",
        "do",
        "da",
        "dos",
        "das",
        "e",
        "em",
        "para",
        "por",
        "redondinha",
    }

    TRUSTED_SOURCE_MARKETS = (
        "Rossi",
        "Nagumo",
        "Higas",
        "Atacadão",
        "Swift",
        "Sonda Delivery",
        "XSupermercados",
        "Barbosa",
        "Carrefour",
        "Extra",
        "Pão de Açúcar",
        "Oba Hortifruti",
        "Tenda Atacado",
    )
    DEFAULT_EMBEDDING_MODEL = "intfloat/multilingual-e5-small"

    def __init__(self):
        load_env_file()
        self.db = DatabaseManager()
        # Backward compatibility: stats payloads still expose this field.
        # Database is PostgreSQL-only now, so this is informational only.
        self.catalog_db_path = "postgres"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                )
            }
        )
        self._init_catalog_db()
        # Circuit-breaker: set to True when the AI provider returns 402/403
        # so we stop wasting calls for the rest of the session.
        self._ai_provider_unavailable = False
        self._disabled_ai_providers = set()
        self._last_batch_provider_failed = False
        self._lmstudio_model_cache: Optional[str] = None
        self._embedding_model_name = (
            os.getenv("BARCODE_EMBEDDING_MODEL", self.DEFAULT_EMBEDDING_MODEL).strip()
            or self.DEFAULT_EMBEDDING_MODEL
        )
        self._embedding_model = None
        self._embedding_util = None
        self._known_brands_list: Optional[_KnownBrandsList] = None

    def _init_catalog_db(self):
        # Tables are initialized by DatabaseManager in the configured backend.
        return

    @staticmethod
    def _sync_state_file_path() -> str:
        raw_path = os.getenv("BARCODE_SYNC_STATE_FILE", ".cache/barcode_sync_state.json").strip()
        return raw_path or ".cache/barcode_sync_state.json"

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _catalog_sync_min_interval_seconds(self) -> float:
        env_value = os.getenv("BARCODE_SYNC_MIN_INTERVAL_HOURS", "").strip()
        if env_value:
            hours = self._safe_float(env_value, default=0.0)
        else:
            hours = self._safe_float(getattr(_config, "BARCODE_SYNC_MIN_INTERVAL_HOURS", 0.0), default=0.0)
        return max(0.0, hours) * 3600.0

    def _read_sync_state(self) -> Dict[str, Any]:
        path = self._sync_state_file_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def _write_sync_state(self, state: Dict[str, Any]) -> None:
        path = self._sync_state_file_path()
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
        except Exception:
            pass

    def _get_last_sync_epoch(self, sync_key: str) -> Optional[float]:
        payload = self._read_sync_state()
        raw_value = payload.get(sync_key)
        if not raw_value:
            return None
        try:
            return float(datetime.fromisoformat(str(raw_value)).timestamp())
        except Exception:
            return None

    def _mark_sync_now(self, sync_key: str) -> None:
        payload = self._read_sync_state()
        payload[sync_key] = datetime.now().isoformat()
        self._write_sync_state(payload)

    @staticmethod
    def _strip_accents(text: str) -> str:
        return "".join(
            char for char in unicodedata.normalize("NFKD", text) if not unicodedata.combining(char)
        )

    @classmethod
    def _normalize_text(cls, value: Optional[str]) -> str:
        if value is None:
            return ""
        text = cls._strip_accents(str(value)).lower()
        text = re.sub(r"[^a-z0-9]+", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def _extract_measure_token(cls, *values: Optional[str]) -> str:
        # Run the regex on the RAW (accent-stripped but comma-preserved) text first,
        # so "1,5L" and "1.5L" are captured before _normalize_text turns commas to spaces.
        raw_combined = " ".join(
            cls._strip_accents(str(v)).lower() for v in values if v
        )
        combined = cls._normalize_text(" ".join(str(v) for v in values if v))
        if not combined and not raw_combined:
            return ""

        match = re.search(r"(\d+[\.,]\d+|\d+)\s*(kg|g|mg|ml|l|lt|un|und)\b", raw_combined) \
             or re.search(r"(\d+[\.,]?\d*)\s*(kg|g|mg|ml|l|lt|un|und)", combined)
        if not match:
            return ""

        amount_text = match.group(1).replace(",", ".")
        unit = match.group(2)
        amount = float(amount_text)

        if unit in {"l", "lt"}:
            return f"{int(round(amount * 1000))}ml"
        if unit == "kg":
            return f"{int(round(amount * 1000))}g"
        if amount.is_integer():
            amount_text = str(int(amount))
        else:
            amount_text = f"{amount:.3f}".rstrip("0").rstrip(".")
        return f"{amount_text}{unit}"

    @classmethod
    def _tokenize(cls, *values: Optional[str]) -> List[str]:
        text = " ".join(cls._normalize_text(value) for value in values if value)
        tokens = []
        for token in text.split():
            if token in cls.STOPWORDS:
                continue
            if len(token) == 1 and not token.isdigit():
                continue
            tokens.append(token)
        return tokens

    @classmethod
    def _pick_anchor_token(cls, product_name: str, brand: str, description: str) -> str:
        # Prefer a specific token from the product name over the brand name —
        # product name words (e.g. "guarana") are better discriminators than brand
        # (e.g. "antarctica") which may match many unrelated products.
        # Fallback to brand tokens if product name yields nothing useful.
        product_tokens = cls._tokenize(product_name)
        brand_tokens = cls._tokenize(brand)
        brand_set = set(brand_tokens)
        # First try: product name token that is NOT part of the brand (more specific)
        for token in product_tokens:
            if token.isdigit():
                continue
            if token not in brand_set:
                return token
        # Second try: any product name token
        for token in product_tokens:
            if not token.isdigit():
                return token
        # Fallback: brand token
        for token in brand_tokens:
            if not token.isdigit():
                return token
        return ""

    @classmethod
    def compute_fingerprint(cls, product_name: Optional[str], brand: Optional[str], unit: Optional[str] = None) -> str:
        """Stable cross-market product fingerprint based on sorted token set.

        Uses SORTED tokens instead of raw text so that word-order differences and
        minor naming variations produce the same fingerprint:
          "Refrigerante Guaraná Antarctica 1,5L" == "Guaraná Antarctica Refrigerante 1.5L"
          "Leite Integral 1L Piracanjuba"        == "Piracanjuba Leite Integral 1000ml"

        Also keeps measure token explicit so size variants never collide:
          "Guaraná 1,5L" != "Guaraná 2L" even if other tokens are the same.
        """
        tokens = set(cls._tokenize(product_name, brand))
        measure = cls._extract_measure_token(product_name, brand, unit)
        if measure:
            tokens.add(measure)
        key = "|".join(sorted(tokens))
        if not key:
            return ""
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    @classmethod
    def _build_offer_signature(cls, target: Dict[str, Any]) -> str:
        return "|".join(
            [
                cls._normalize_text(target.get("product_name")),
                cls._normalize_text(target.get("brand")),
                cls._normalize_text(target.get("description")),
                cls._normalize_text(target.get("unit")),
            ]
        )

    @staticmethod
    def _truncate_text(value: Optional[str], max_chars: int) -> str:
        text = str(value or "").strip()
        if len(text) <= max_chars:
            return text
        return text[: max(0, max_chars - 3)].rstrip() + "..."

    def _build_ai_user_payload(
        self,
        target: Dict[str, Any],
        candidates: List[Dict[str, Any]],
        *,
        max_candidates: int,
        target_name_max: int,
        target_desc_max: int,
        candidate_name_max: int,
        candidate_desc_max: int,
    ) -> Dict[str, Any]:
        trimmed_candidates = candidates[:max_candidates]
        return {
            "target": {
                "market_name": target.get("market_name"),
                "offer_id": target.get("id"),
                "product_name": self._truncate_text(target.get("product_name"), target_name_max),
                "brand": self._truncate_text(target.get("brand"), 60),
                "description": self._truncate_text(target.get("description"), target_desc_max),
                "unit": self._truncate_text(target.get("unit"), 20),
            },
            "candidates": [
                {
                    "barcode": candidate["barcode"],
                    "source_market": candidate["source_market"],
                    "source_market_id": candidate["source_market_id"],
                    "product_name": self._truncate_text(candidate.get("product_name"), candidate_name_max),
                    "brand": self._truncate_text(candidate.get("brand"), 60),
                    "description": self._truncate_text(candidate.get("description"), candidate_desc_max),
                    "heuristic_score": round(candidate["score"], 4),
                }
                for candidate in trimmed_candidates
            ],
            "response_format": {
                "matched": True,
                "selected_barcode": "string or null",
                "source_market": "string or null",
                "source_market_id": "string or null",
                "confidence": 0.0,
                "reasoning": "brief explanation",
            },
        }

    def _load_embedding_matcher(self):
        if self._embedding_model is not None and self._embedding_util is not None:
            return self._embedding_model, self._embedding_util

        from sentence_transformers import SentenceTransformer, util

        self._embedding_model = SentenceTransformer(self._embedding_model_name)
        self._embedding_util = util
        return self._embedding_model, self._embedding_util

    def _build_embedding_text(self, payload: Dict[str, Any], *, prefix: str) -> str:
        name = self._normalize_text(payload.get("product_name"))
        brand = self._normalize_text(payload.get("brand"))
        description = self._normalize_text(payload.get("description"))
        unit = self._normalize_text(payload.get("unit"))
        return f"{prefix}: {name} | {brand} | {description} | {unit}"

    def _call_embedding_matcher(
        self,
        target: Dict[str, Any],
        candidates: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        if not candidates:
            return None

        try:
            model, util = self._load_embedding_matcher()
            target_text = self._build_embedding_text(target, prefix="query")
            candidate_texts = [self._build_embedding_text(candidate, prefix="passage") for candidate in candidates]

            target_embedding = model.encode([target_text], convert_to_tensor=True, normalize_embeddings=True)
            candidate_embeddings = model.encode(candidate_texts, convert_to_tensor=True, normalize_embeddings=True)
            scores = util.cos_sim(target_embedding, candidate_embeddings)[0]

            best_idx = int(scores.argmax().item())
            best_score = float(scores[best_idx].item())
            best = candidates[best_idx]
            sorted_scores = sorted((float(score.item()) for score in scores), reverse=True)
            second_score = sorted_scores[1] if len(sorted_scores) > 1 else 0.0

            target_brand = self._normalize_text(target.get("brand"))
            best_brand = self._normalize_text(best.get("brand"))
            target_measure = self._extract_measure_token(
                target.get("product_name"),
                target.get("brand"),
                target.get("description"),
                target.get("unit"),
            )
            best_measure = self._extract_measure_token(
                best.get("product_name"),
                best.get("brand"),
                best.get("description"),
                best.get("unit"),
            )

            brand_match = bool(target_brand and best_brand and target_brand == best_brand)
            measure_match = bool(target_measure and best_measure and target_measure == best_measure)
            threshold = float(os.getenv("BARCODE_EMBEDDING_THRESHOLD", "0.90"))
            margin = float(os.getenv("BARCODE_EMBEDDING_MARGIN", "0.04"))
            strong_structured_match = brand_match and measure_match
            matched = best_score >= threshold and ((best_score - second_score) >= margin or strong_structured_match)

            if matched:
                reasoning = "Embedding similarity on normalized product text"
            elif best_score < threshold:
                reasoning = "Best candidate below embedding threshold"
            elif strong_structured_match:
                reasoning = "Accepted by brand and measure compatibility"
            else:
                reasoning = "Ambiguous top candidates (low embedding margin)"

            return {
                "matched": matched,
                "selected_barcode": best.get("barcode"),
                "source_market": best.get("source_market"),
                "source_market_id": best.get("source_market_id"),
                "confidence": best_score,
                "second_confidence": second_score,
                "reasoning": reasoning,
                "provider": "embedding",
                "model": self._embedding_model_name,
            }
        except Exception as exc:
            print(f"Barcode embedding matcher failed ({type(exc).__name__}): {exc}")
            return None

    def sync_known_barcodes(self, source_markets: Optional[List[str]] = None) -> Dict[str, Any]:
        markets = source_markets or list(self.TRUSTED_SOURCE_MARKETS)
        sync_key = "|".join(sorted(markets))

        min_interval_seconds = self._catalog_sync_min_interval_seconds()
        if min_interval_seconds > 0:
            last_sync_epoch = self._get_last_sync_epoch(sync_key)
            now_epoch = time.time()
            if last_sync_epoch is not None and (now_epoch - last_sync_epoch) < min_interval_seconds:
                remaining_seconds = max(0.0, min_interval_seconds - (now_epoch - last_sync_epoch))
                return {
                    "catalog_db_path": self.catalog_db_path,
                    "source_markets": markets,
                    "source_rows": 0,
                    "upserted": 0,
                    "skipped": True,
                    "skip_reason": "recent_sync",
                    "min_interval_hours": min_interval_seconds / 3600.0,
                    "remaining_minutes": int(round(remaining_seconds / 60.0)),
                }

        offer_rows = self.db.fetch_offers_with_barcodes(markets)
        reference_rows = self.db.fetch_barcode_reference_catalog(markets)

        upsert_by_key: Dict[Tuple[str, str], Tuple[Any, ...]] = {}

        for market_name, market_id, barcode, product_name, brand, description, image_url in offer_rows:
            normalized_barcode = self.db.normalize_barcode(barcode)
            if not normalized_barcode:
                continue
            upsert_by_key[(market_name, market_id)] = (
                normalized_barcode,
                market_name,
                market_id,
                product_name,
                brand,
                description,
                self._normalize_text(product_name),
                self._normalize_text(brand),
                self._extract_measure_token(product_name, brand, description),
                1,           # market_count
                image_url,
                datetime.now().isoformat(),
            )

        for market_name, market_id, barcode, product_name, brand, last_updated in reference_rows:
            if (market_name, market_id) in upsert_by_key:
                continue
            normalized_barcode = self.db.normalize_barcode(barcode)
            if not normalized_barcode:
                continue
            description = None
            upsert_by_key[(market_name, market_id)] = (
                normalized_barcode,
                market_name,
                market_id,
                product_name,
                brand,
                description,
                self._normalize_text(product_name),
                self._normalize_text(brand),
                self._extract_measure_token(product_name, brand, description),
                1,           # market_count
                None,        # image_url not available from barcode_references
                str(last_updated or datetime.now().isoformat()),
            )

        upsert_rows = list(upsert_by_key.values())
        self.db.upsert_known_barcodes(upsert_rows)
        self._mark_sync_now(sync_key)
        return {
            "catalog_db_path": self.catalog_db_path,
            "source_markets": markets,
            "source_rows": len(offer_rows) + len(reference_rows),
            "upserted": len(upsert_rows),
            "skipped": False,
            "min_interval_hours": min_interval_seconds / 3600.0,
        }

    def has_ai_provider(self) -> bool:
        available = []
        if self._is_lmstudio_enabled() and "lmstudio" not in self._disabled_ai_providers:
            available.append("lmstudio")
        if os.getenv("OPENROUTER_API_KEY") and "openrouter" not in self._disabled_ai_providers:
            available.append("openrouter")
        if os.getenv("XAI_API_KEY") and "xai" not in self._disabled_ai_providers:
            available.append("xai")
        if os.getenv("HF_TOKEN") and "huggingface" not in self._disabled_ai_providers:
            available.append("huggingface")
        if os.getenv("GEMINI_API_KEY") and "gemini" not in self._disabled_ai_providers:
            available.append("gemini")
        return bool(available)

    @staticmethod
    def _get_provider_order() -> List[str]:
        raw_order = os.getenv("AI_PROVIDER_ORDER", "lmstudio,openrouter,xai,gemini,huggingface")
        order = [item.strip().lower() for item in raw_order.split(",") if item.strip()]
        if not order:
            return ["lmstudio", "openrouter", "xai", "gemini", "huggingface"]
        return order

    @staticmethod
    def _is_truthy(value: str) -> bool:
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _is_lmstudio_enabled(self) -> bool:
        return self._is_truthy(os.getenv("LM_STUDIO_ENABLED", "1"))

    @staticmethod
    def _get_lmstudio_base_url() -> str:
        return os.getenv("LM_STUDIO_BASE_URL", "http://127.0.0.1:1234").rstrip("/")

    def _resolve_lmstudio_model(self) -> Optional[str]:
        configured_model = os.getenv("LM_STUDIO_BARCODE_MATCH_MODEL", "").strip()
        if configured_model:
            return configured_model

        if self._lmstudio_model_cache:
            return self._lmstudio_model_cache

        try:
            response = self.session.get(f"{self._get_lmstudio_base_url()}/v1/models", timeout=3)
            if response.status_code != 200:
                return None
            payload = response.json() or {}
            models = payload.get("data") or []
            if not models:
                return None
            model_ids = [str(item.get("id") or "").strip() for item in models]
            model_ids = [item for item in model_ids if item]
            if not model_ids:
                return None

            # Prefer chat/instruct models and avoid embeddings for chat/completions.
            candidates = [
                mid
                for mid in model_ids
                if "embed" not in mid.lower() and "embedding" not in mid.lower()
            ]
            if not candidates:
                candidates = model_ids

            preferred_keywords = [
                "deepseek-r1-distill-qwen",
                "qwen",
                "instruct",
                "chat",
            ]
            ranked = sorted(
                candidates,
                key=lambda mid: (
                    0
                    if any(keyword in mid.lower() for keyword in preferred_keywords)
                    else 1,
                    len(mid),
                ),
            )
            model_id = ranked[0]
            self._lmstudio_model_cache = model_id
            return model_id
        except requests.exceptions.RequestException:
            return None

    def is_ai_matching_enabled(self) -> bool:
        return os.getenv("ENABLE_AI_BARCODE_MATCH", "").strip().lower() in {"1", "true", "yes", "on"}

    @classmethod
    def _extract_brand_from_name(cls, product_name: str, existing_brand: Optional[str]) -> str:
        """
        Extract brand from product name if explicit brand field is missing or empty.
        Looks for brand mentions at the start or in key positions of product name.
        """
        if existing_brand and existing_brand.strip():
            return cls._normalize_text(existing_brand)
        
        if not product_name:
            return ""
        
        normalized = cls._normalize_text(product_name)
        tokens = normalized.split()
        
        # First token is often the brand; return if it's 3+ chars
        if tokens and len(tokens[0]) >= 3:
            return tokens[0]
        
        # Look for branded terms in common positions (2nd-3rd token)
        for token in tokens[1:4]:
            if len(token) >= 3 and token not in {"kg", "ml", "und", "un", "g"}:
                return token
        
        return ""

    def _score_candidate(self, target: Dict[str, Any], candidate: Dict[str, Any]) -> float:
        target_name = self._normalize_text(target.get("product_name"))
        candidate_name = self._normalize_text(candidate.get("normalized_name"))
        name_ratio = SequenceMatcher(None, target_name, candidate_name).ratio()

        target_tokens = set(self._tokenize(target.get("product_name"), target.get("brand"), target.get("description")))
        candidate_tokens = set(self._tokenize(candidate.get("product_name"), candidate.get("brand"), candidate.get("description")))
        token_overlap = 0.0
        if target_tokens or candidate_tokens:
            token_overlap = len(target_tokens & candidate_tokens) / max(len(target_tokens | candidate_tokens), 1)

        # Brand matching with frequency weighting
        target_brand = self._normalize_text(target.get("brand"))
        candidate_brand = self._normalize_text(candidate.get("normalized_brand"))
        brand_bonus = 0.0
        
        # Only consider candidate brand if it's in the known brands list with sufficient frequency
        brand_confidence = None
        if self._known_brands_list and candidate_brand:
            min_brand_frequency = max(1, int(os.getenv("BARCODE_MIN_BRAND_FREQUENCY", "2")))
            brand_confidence = self._known_brands_list.get_brand_confidence(candidate_brand, min_brand_frequency)
        
        # Brand matching logic (explicit match or in-name match)
        if target_brand and candidate_brand and target_brand == candidate_brand:
            # Explicit match: both sides have the same brand
            brand_bonus = 0.18 * (brand_confidence or 1.0)
        elif not target_brand and candidate_brand and brand_confidence:
            # Implicit match: brand appears in target product name (only if it's a known brand)
            target_product_norm = self._normalize_text(target.get("product_name", ""))
            if candidate_brand in target_product_norm:
                # Higher bonus for brands found in product name (0.22 * confidence)
                brand_bonus = 0.22 * brand_confidence

        target_measure = self._extract_measure_token(target.get("product_name"), target.get("brand"), target.get("description"))
        candidate_measure = candidate["measure_token"]
        measure_bonus = 0.17 if target_measure and target_measure == candidate_measure else 0.0
        # Only penalize if both sides have measure data and they conflict (don't penalize missing data)
        measure_penalty = -0.12 if (target_measure and candidate_measure and target_measure != candidate_measure) else 0.0

        score = (0.55 * name_ratio) + (0.28 * token_overlap) + brand_bonus + measure_bonus + measure_penalty
        return max(0.0, min(score, 1.0))

    def _get_candidate_records(
        self,
        target: Dict[str, Any],
        max_candidates: int = 6,
        catalog: Optional["_KnownBarcodesCatalog"] = None,
    ) -> List[Dict[str, Any]]:
        # Use explicit brand from target; don't try to extract from product name for catalog search
        # (improved scoring logic will match brands appearing in product names during _score_candidate)
        target_brand = self._normalize_text(target.get("brand", ""))
        target_measure = self._extract_measure_token(
            target.get("product_name"),
            target.get("brand"),
            target.get("description"),
        )
        anchor_token = self._pick_anchor_token(
            target.get("product_name", ""),
            target.get("brand", ""),
            target.get("description", ""),
        )
        # Use up to 3 additional tokens from the product name for broader retrieval
        all_tokens = self._tokenize(target.get("product_name", ""))
        extra_tokens = [t for t in all_tokens if t != anchor_token and not t.isdigit()][:3]

        if catalog is not None:
            rows = catalog.query(
                target_market=target.get("market_name") or "",
                target_brand=target_brand,
                target_measure=target_measure,
                anchor_token=anchor_token,
                extra_tokens=extra_tokens,
                limit=250,
            )
        else:
            rows = self.db.fetch_known_barcodes_candidates(
                target_market=target.get("market_name"),
                target_brand=target_brand,
                target_measure=target_measure,
                anchor_token=anchor_token,
                limit=250,
            )

        # Score all rows, then deduplicate by barcode keeping the best-scoring name variant.
        # Without this, one barcode can occupy multiple slots in the top-6 (e.g., same product
        # appears in Barbosa, Carrefour, Extra, Pão de Açúcar → 4 slots wasted on same barcode).
        best_by_barcode: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            candidate = {
                "barcode": row[0],
                "source_market": row[1],
                "source_market_id": row[2],
                "product_name": row[3],
                "brand": row[4],
                "description": row[5],
                "normalized_name": row[6],
                "normalized_brand": row[7],
                "measure_token": row[8],
                "image_url": row[9] if len(row) > 9 else None,
            }
            candidate["score"] = self._score_candidate(target, candidate)
            bc = candidate["barcode"]
            if bc not in best_by_barcode or candidate["score"] > best_by_barcode[bc]["score"]:
                best_by_barcode[bc] = candidate

        candidates = sorted(best_by_barcode.values(), key=lambda c: c["score"], reverse=True)
        return candidates[:max_candidates]

    @staticmethod
    def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
        if not text:
            return None

        # Strip <think>...</think> reasoning blocks before searching for JSON
        # (deepseek-r1 and other reasoning models emit these in their content).
        text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None

        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _parse_batch_json_items(content: str) -> List[Dict[str, Any]]:
        """Parse batch response supporting JSON array, NDJSON, or sequential objects."""
        if not content:
            return []

        clean = re.sub(r"<think>[\s\S]*?</think>", "", content, flags=re.IGNORECASE)
        clean = re.sub(r"//[^\n]*", "", clean)
        clean = re.sub(r"```[a-z]*\n?", "", clean, flags=re.IGNORECASE)
        clean = clean.replace("```", "").strip()

        # 1) Preferred shape: a JSON array.
        bracket = clean.find("[")
        if bracket != -1:
            try:
                parsed_list, _ = json.JSONDecoder().raw_decode(clean, bracket)
                if isinstance(parsed_list, list):
                    return [item for item in parsed_list if isinstance(item, dict)]
            except json.JSONDecodeError:
                pass

        # 2) NDJSON shape: one JSON object per line.
        ndjson_items: List[Dict[str, Any]] = []
        for line in clean.splitlines():
            candidate = line.strip().rstrip(",")
            if not candidate.startswith("{"):
                continue
            try:
                obj = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                ndjson_items.append(obj)
        if ndjson_items:
            return ndjson_items

        # 3) Sequential objects in a single blob.
        objects: List[Dict[str, Any]] = []
        decoder = json.JSONDecoder()
        pos = 0
        length = len(clean)
        while pos < length:
            start = clean.find("{", pos)
            if start == -1:
                break
            try:
                obj, end = decoder.raw_decode(clean, start)
            except json.JSONDecodeError:
                pos = start + 1
                continue
            if isinstance(obj, dict):
                objects.append(obj)
            pos = max(end, start + 1)
        return objects

    def _call_ai_matcher(self, target: Dict[str, Any], candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if self._ai_provider_unavailable:
            return None

        provider_configs: List[Dict[str, Any]] = []

        if self._is_lmstudio_enabled():
            lmstudio_model = self._resolve_lmstudio_model()
            if lmstudio_model:
                provider_configs.append(
                    {
                        "provider": "lmstudio",
                        "url": f"{self._get_lmstudio_base_url()}/v1/chat/completions",
                        "headers": {
                            "Content-Type": "application/json",
                        },
                        "model": lmstudio_model,
                    }
                )

        # OpenRouter free tier has no monthly cap - preferred when available.
        if os.getenv("OPENROUTER_API_KEY"):
            provider_configs.append(
                {
                    "provider": "openrouter",
                    "url": "https://openrouter.ai/api/v1/chat/completions",
                    "headers": {
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}",
                        "HTTP-Referer": os.getenv("OPENROUTER_SITE_URL", "https://local.barcode.matcher"),
                        "X-OpenRouter-Title": os.getenv("OPENROUTER_SITE_NAME", "markets-db-barcode-matcher"),
                    },
                    "model": os.getenv("OPENROUTER_BARCODE_MATCH_MODEL", "meta-llama/llama-3.3-70b-instruct:free"),
                }
            )

        if os.getenv("XAI_API_KEY"):
            provider_configs.append(
                {
                    "provider": "xai",
                    "url": "https://api.x.ai/v1/chat/completions",
                    "headers": {
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {os.getenv('XAI_API_KEY')}",
                    },
                    "model": os.getenv("XAI_BARCODE_MATCH_MODEL", "grok-3-mini"),
                }
            )

        if os.getenv("HF_TOKEN"):
            provider_configs.append(
                {
                    "provider": "huggingface",
                    "url": "https://router.huggingface.co/sambanova/v1/chat/completions",
                    "headers": {
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {os.getenv('HF_TOKEN')}",
                    },
                    "model": os.getenv("HF_BARCODE_MATCH_MODEL", "Meta-Llama-3.3-70B-Instruct"),
                }
            )

        if os.getenv("GEMINI_API_KEY"):
            provider_configs.append(
                {
                    "provider": "gemini",
                    "api_key": os.getenv("GEMINI_API_KEY"),
                    "model": os.getenv("GEMINI_BARCODE_MATCH_MODEL", "gemini-2.5-flash"),
                }
            )

        provider_order = self._get_provider_order()
        order_rank = {provider: idx for idx, provider in enumerate(provider_order)}
        provider_configs.sort(key=lambda cfg: order_rank.get(cfg["provider"], len(order_rank)))
        max_provider_attempts = max(1, int(os.getenv("AI_MAX_PROVIDER_ATTEMPTS", "1")))
        remote_timeout = max(5, int(os.getenv("AI_REMOTE_TIMEOUT_SECONDS", "10")))

        provider_configs = [
            cfg for cfg in provider_configs if cfg["provider"] not in self._disabled_ai_providers
        ]
        if not provider_configs:
            self._ai_provider_unavailable = True
            return None

        system_prompt = (
            "You match supermarket products across different stores. "
            "Only match when the physical item is the same barcode-level product. "
            "Pay close attention to brand, volume/weight, flavor, sugar-free/zero variants, pack size, and product category. "
            "Return JSON only."
        )
        for attempt_idx, cfg in enumerate(provider_configs, start=1):
            if attempt_idx > max_provider_attempts:
                break
            provider = cfg["provider"]
            is_local = provider == "lmstudio"
            max_candidates = int(os.getenv("AI_MAX_CANDIDATES_FOR_AI", "4"))
            if is_local:
                max_candidates = int(os.getenv("LM_STUDIO_MAX_CANDIDATES_FOR_AI", "3"))
            max_candidates = max(1, min(max_candidates, len(candidates)))

            user_payload = self._build_ai_user_payload(
                target,
                candidates,
                max_candidates=max_candidates,
                target_name_max=int(os.getenv("AI_TARGET_NAME_MAX_CHARS", "180")),
                target_desc_max=int(os.getenv("AI_TARGET_DESC_MAX_CHARS", "180" if is_local else "260")),
                candidate_name_max=int(os.getenv("AI_CANDIDATE_NAME_MAX_CHARS", "150")),
                candidate_desc_max=int(os.getenv("AI_CANDIDATE_DESC_MAX_CHARS", "160" if is_local else "220")),
            )
            # No rate-limit delay needed for local models
            if is_local:
                effective_delay = 0.0
            else:
                effective_delay = max(0.0, float(os.getenv("AI_CALL_DELAY_SECONDS", "0.75")))
            if effective_delay > 0:
                time.sleep(effective_delay)
            request_timeout = (
                max(5, int(os.getenv("LM_STUDIO_TIMEOUT", str(_config.LM_STUDIO_TIMEOUT))))
                if is_local
                else remote_timeout
            )
            try:
                if provider == "gemini":
                    gemini_model = cfg["model"]
                    if gemini_model.startswith("models/"):
                        gemini_model = gemini_model.split("/", 1)[1]
                    response = self.session.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model}:generateContent",
                        params={"key": cfg["api_key"]},
                        headers={"Content-Type": "application/json"},
                        data=json.dumps(
                            {
                                "contents": [
                                    {
                                        "parts": [
                                            {
                                                "text": (
                                                    f"{system_prompt}\n\n"
                                                    f"INPUT:\n{json.dumps(user_payload, ensure_ascii=False)}\n\n"
                                                    "Return only JSON object."
                                                )
                                            }
                                        ]
                                    }
                                ],
                                "generationConfig": {"temperature": 0.1},
                            }
                        ),
                        timeout=request_timeout,
                    )
                else:
                    response = self.session.post(
                        cfg["url"],
                        headers=cfg["headers"],
                        data=json.dumps(
                            {
                                "model": cfg["model"],
                                "temperature": 0.1,
                                "messages": [
                                    {"role": "system", "content": system_prompt},
                                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                                ],
                            }
                        ),
                        timeout=request_timeout,
                    )
            except requests.exceptions.RequestException as exc:
                print(
                    f"Barcode AI matcher: {provider} request failed ({type(exc).__name__}); "
                    "trying next provider"
                )
                if provider == "lmstudio":
                    self._disabled_ai_providers.add(provider)
                continue

            if response.status_code == 200:
                payload = response.json()
                if provider == "gemini":
                    content = ""
                    candidates_payload = payload.get("candidates", [])
                    if candidates_payload:
                        parts = (
                            candidates_payload[0]
                            .get("content", {})
                            .get("parts", [])
                        )
                        if parts:
                            content = parts[0].get("text", "")
                else:
                    content = (
                        payload.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                    )
                parsed = self._extract_json_object(content)
                if parsed is not None:
                    parsed["provider"] = provider
                return parsed

            if response.status_code == 429 and provider == "openrouter":
                print(
                    "Barcode AI matcher: openrouter returned HTTP 429, "
                    "falling back to next provider and disabling openrouter for this session"
                )
                self._disabled_ai_providers.add(provider)
                continue

            if response.status_code == 429 and provider == "xai":
                print(
                    "Barcode AI matcher: xai returned HTTP 429, "
                    "falling back to next provider and disabling xai for this session"
                )
                self._disabled_ai_providers.add(provider)
                continue

            if response.status_code == 429 and provider == "huggingface":
                print(
                    "Barcode AI matcher: huggingface returned HTTP 429, "
                    "falling back to gemini and disabling huggingface for this session"
                )
                self._disabled_ai_providers.add(provider)
                continue

            if response.status_code == 429 and provider == "gemini":
                print(
                    "Barcode AI matcher: gemini returned HTTP 429. "
                    "Disabling gemini for this session."
                )
                self._disabled_ai_providers.add(provider)
                continue

            if response.status_code in (401, 402, 403):
                print(
                    f"Barcode AI matcher: {provider} returned HTTP {response.status_code} "
                    "(auth/credits/provider issue). Disabling this provider for this session."
                )
                self._disabled_ai_providers.add(provider)
                continue

            if response.status_code == 404 and provider == "lmstudio":
                body_snippet = (response.text or "").replace("\n", " ")[:220]
                print(
                    "Barcode AI matcher: lmstudio returned HTTP 404. "
                    "Likely model id is not loaded for chat/completions. "
                    f"Response: {body_snippet}. Disabling lmstudio for this session."
                )
                self._disabled_ai_providers.add(provider)
                continue

            if response.status_code == 400:
                body_snippet = (response.text or "").replace("\n", " ")[:220]
                if "maximum context length" in body_snippet.lower() or "context" in body_snippet.lower() and "overflow" in body_snippet.lower():
                    print(
                        f"Barcode AI matcher: {provider} payload exceeded context window. "
                        "Disabling this provider for this session to avoid repeated failures."
                    )
                    self._disabled_ai_providers.add(provider)
                    continue
                if provider == "xai" and "Model not found" in body_snippet:
                    print(
                        "Barcode AI matcher: xai model not found. "
                        "Set XAI_BARCODE_MATCH_MODEL to a valid deployed model and retry. "
                        f"Response: {body_snippet}"
                    )
                    self._disabled_ai_providers.add(provider)
                    continue
                print(
                    f"Barcode AI matcher: {provider} returned HTTP 400. "
                    f"Response: {body_snippet}"
                )
                self._disabled_ai_providers.add(provider)
                continue

            print(f"Barcode AI matcher: {provider} returned HTTP {response.status_code}")

        if self._disabled_ai_providers.issuperset({cfg["provider"] for cfg in provider_configs}):
            self._ai_provider_unavailable = True
        return None

    def _call_ai_matcher_batch(
        self,
        batch: List[Dict[str, Any]],
    ) -> List[Optional[Dict[str, Any]]]:
        """Send a batch of products to the LLM in a single call.

        Each item in `batch` is {"target": offer_dict, "candidates": [candidate, ...]}.
        Returns a parallel list of match results (same length as batch), or None per slot
        if the LLM did not return a usable result for that item.

        A single LLM call for 15 products replaces 15 individual calls — 15x cheaper.
        """
        if not batch or self._ai_provider_unavailable:
            return [None] * len(batch)

        # Each candidate is now a unique barcode (deduplicated by _get_candidate_records),
        # so 6 candidates cover 6 distinct products — better signal for the LLM.
        max_candidates = max(1, int(os.getenv("AI_MAX_CANDIDATES_FOR_AI", "6")))
        # Keep payload lean — no "task" or "response_format" keys so reasoning models
        # don't echo the whole prompt back before answering (causes huge token counts + timeouts).
        payload = {
            "targets": [
                {
                    "idx": i,
                    "name": self._truncate_text(item["target"].get("product_name"), 120),
                    "brand": self._truncate_text(item["target"].get("brand"), 60),
                    "unit": self._truncate_text(item["target"].get("unit"), 20),
                    "measure": self._extract_measure_token(
                        item["target"].get("product_name"),
                        item["target"].get("brand"),
                        item["target"].get("unit"),
                    ),
                    "candidates": [
                        {
                            "barcode": c["barcode"],
                            "name": self._truncate_text(c.get("product_name"), 100),
                            "brand": self._truncate_text(c.get("brand"), 50),
                            "measure": c.get("measure_token", ""),
                            "score": round(c["score"], 3),
                        }
                        for c in item["candidates"][:max_candidates]
                    ],
                }
                for i, item in enumerate(batch)
            ],
        }

        batch_size = len(batch)
        system_prompt = (
            "You match Brazilian supermarket products across different stores. "
            "The SAME physical product can have very different names in different stores "
            "(e.g. 'Refrigerante Guaraná Antarctica 1,5L' and 'Guaraná Antart. 1.5L' are the same). "
            "Match if: brand matches, size/measure matches, and the product type is the same. "
            "Do NOT match if size/measure differs (1,5L vs 2L are different products). "
            f"Return ONLY a JSON array of exactly {batch_size} objects, one per target idx, in order. "
            'Each object: {"idx": <int>, "matched": <bool>, "barcode": <string or null>, "confidence": <float 0-1>}. '
            "No explanation, no markdown, just the JSON array."
        )
        user_content = json.dumps(payload, ensure_ascii=False)
        # Cap output tokens for remote providers only (saves cost/quota).
        # For local lmstudio we do NOT cap — reasoning models (deepseek-r1) spend
        # hundreds of tokens on <think> before producing output, so a tight cap
        # causes finish_reason=length with empty content.
        # Each result object is ~300 tokens; add generous headroom.
        _remote_max_tokens = max(1024, batch_size * 300)

        provider_order = self._get_provider_order()
        # Prefer local model for batches (no per-call cost), then remote
        for provider_name in provider_order:
            if provider_name in self._disabled_ai_providers:
                continue

            try:
                if provider_name == "lmstudio" and self._is_lmstudio_enabled():
                    model = self._resolve_lmstudio_model()
                    if not model:
                        continue
                    response = self.session.post(
                        f"{self._get_lmstudio_base_url()}/v1/chat/completions",
                        headers={"Content-Type": "application/json"},
                        data=json.dumps({
                            "model": model, "temperature": 0.1,
                            "messages": [
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_content},
                            ],
                        }),
                        timeout=max(30, int(os.getenv("LM_STUDIO_TIMEOUT", str(_config.LM_STUDIO_TIMEOUT)))),
                    )
                elif provider_name == "gemini" and os.getenv("GEMINI_API_KEY"):
                    gemini_model = os.getenv("GEMINI_BARCODE_MATCH_MODEL", "gemini-2.5-flash")
                    if gemini_model.startswith("models/"):
                        gemini_model = gemini_model.split("/", 1)[1]
                    response = self.session.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model}:generateContent",
                        params={"key": os.getenv("GEMINI_API_KEY")},
                        headers={"Content-Type": "application/json"},
                        data=json.dumps({
                            "contents": [{"parts": [{"text": f"{system_prompt}\n\nINPUT:\n{user_content}"}]}],
                            "generationConfig": {
                                "temperature": 0.1,
                                # Use a generous token budget. gemini-2.5-flash is a
                                # thinking model — thinking tokens count against
                                # maxOutputTokens, so a tight cap truncates the JSON.
                                "maxOutputTokens": max(4096, batch_size * 400),
                            },
                        }),
                        timeout=max(15, int(os.getenv("AI_REMOTE_TIMEOUT_SECONDS", str(_config.AI_REMOTE_TIMEOUT_SECONDS)))),
                    )
                elif provider_name == "openrouter" and os.getenv("OPENROUTER_API_KEY"):
                    response = self.session.post(
                        "https://openrouter.ai/api/v1/chat/completions",
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}",
                        },
                        data=json.dumps({
                            "model": os.getenv("OPENROUTER_BARCODE_MATCH_MODEL", "meta-llama/llama-3.3-70b-instruct:free"),
                            "temperature": 0.1,
                            "max_tokens": _remote_max_tokens,
                            "messages": [
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_content},
                            ],
                        }),
                        timeout=max(15, int(os.getenv("AI_REMOTE_TIMEOUT_SECONDS", str(_config.AI_REMOTE_TIMEOUT_SECONDS)))),
                    )
                else:
                    continue

                if response.status_code == 429 or response.status_code in (401, 402, 403, 404):
                    print(f"Barcode AI batch matcher: {provider_name} HTTP {response.status_code} — disabling. Body: {response.text[:200]}")
                    self._disabled_ai_providers.add(provider_name)
                    continue

                if response.status_code != 200:
                    print(f"Barcode AI batch matcher: {provider_name} HTTP {response.status_code} — Body: {response.text[:200]}")
                    continue

                if provider_name == "gemini":
                    parts = (response.json().get("candidates", [{}])[0].get("content", {}).get("parts", [{}]))
                    content = parts[0].get("text", "") if parts else ""
                else:
                    _resp_json = response.json()
                    _choice = (_resp_json.get("choices") or [{}])[0]
                    _finish = _choice.get("finish_reason", "")
                    if _finish == "length":
                        print(f"Barcode AI batch matcher: {provider_name} finish_reason=length — model hit token limit during reasoning. Reduce batch size or increase LM_STUDIO_TIMEOUT.")
                    content = (_choice.get("message") or {}).get("content", "") or ""

                parsed_list = self._parse_batch_json_items(content)
                if not parsed_list:
                    print(f"Barcode AI batch matcher: {provider_name} — could not parse batch JSON, content={content[:300]!r}")
                    continue

                results: List[Optional[Dict[str, Any]]] = [None] * len(batch)
                for item in parsed_list:
                    if not isinstance(item, dict):
                        continue
                    idx = item.get("idx")
                    if not isinstance(idx, int) or idx < 0 or idx >= len(batch):
                        continue
                    results[idx] = {
                        "matched": bool(item.get("matched")),
                        "selected_barcode": item.get("barcode"),
                        "confidence": float(item.get("confidence", 0.0) or 0.0),
                        "reasoning": "Batch LLM match",
                        "provider": provider_name,
                    }
                return results

            except Exception as exc:
                exc_name = type(exc).__name__
                print(f"Barcode AI batch matcher: {provider_name} error ({exc_name}): {exc}")
                # ConnectionError = lmstudio not running yet (transient) — do NOT permanently disable.
                # Only hard-disable on auth errors (401/402/403) or 404 (wrong model id).
                # Timeouts are also transient.
                if provider_name == "lmstudio":
                    is_connection_error = "ConnectionError" in exc_name or "ConnectionRefused" in str(exc)
                    if not is_connection_error and "Timeout" not in exc_name:
                        self._disabled_ai_providers.add(provider_name)
                continue

        # All providers failed/timed out for this batch — signal the caller
        # so it does NOT count these as heuristic misses or increment no_match_count.
        self._last_batch_provider_failed = True
        # Only mark fully unavailable if hard-disabled (auth/404), not transient failures
        if self._disabled_ai_providers.issuperset(set(provider_order)):
            self._ai_provider_unavailable = True
        return [None] * len(batch)

    def _save_match_audit(
        self,
        target_offer_id: str,
        target_market: str,
        inferred_barcode: str,
        source_market: str,
        source_market_id: str,
        match_method: str,
        confidence: float,
        reasoning: str,
    ):
        self.db.upsert_match_audit(
            target_offer_id=target_offer_id,
            target_market=target_market,
            inferred_barcode=inferred_barcode,
            source_market=source_market,
            source_market_id=source_market_id,
            match_method=match_method,
            confidence=confidence,
            reasoning=reasoning,
            last_updated=datetime.now().isoformat(),
        )

    def infer_missing_barcodes(
        self,
        target_markets: Optional[List[str]] = None,
        limit: Optional[int] = None,
        target_offer_ids: Optional[List[str]] = None,
        heuristic_threshold: float = _config.BARCODE_HEURISTIC_THRESHOLD,
        ai_threshold: float = _config.BARCODE_AI_THRESHOLD,
        enable_embedding_model: bool = False,
        embedding_model_only: bool = False,
        audit_run_id: Optional[str] = None,
        respect_cached_unmatched: bool = True,
    ) -> Dict[str, Any]:
        heuristic_threshold = float(os.getenv("BARCODE_HEURISTIC_THRESHOLD", str(heuristic_threshold)))
        ai_threshold = float(os.getenv("BARCODE_AI_THRESHOLD", str(ai_threshold)))
        heuristic_margin = float(os.getenv("BARCODE_HEURISTIC_MARGIN", str(_config.BARCODE_HEURISTIC_MARGIN)))
        markets = target_markets or ["Atacadão", "Nagumo", "Higas"]
        print(
            "Barcode inference: fetching barcode-missing offers "
            f"for markets={markets} limit={limit if limit is not None else 'ALL'}..."
        )
        fetch_started_at = time.time()
        rows = self.db.fetch_offers_missing_barcode(markets, limit=limit, offer_ids=target_offer_ids)
        fetch_elapsed = time.time() - fetch_started_at
        print(
            "Barcode inference: fetched "
            f"{len(rows)} barcode-missing offers in {fetch_elapsed:.1f}s"
        )
        total_rows = len(rows)
        progress_every = max(1, int(os.getenv("BARCODE_PROGRESS_EVERY", "25")))
        flush_every = max(50, int(os.getenv("BARCODE_FLUSH_EVERY", "500")))
        started_at = time.time()

        # ── Phase 0: re-apply previously inferred barcodes (offer_id cache) ─────────
        # Since offer IDs are now stable (market_storehash_barcode or market_storehash_name),
        # re-scraped offers that had their barcode inferred last run keep the same ID.
        # We can instantly re-apply without repeating any heuristic/LLM work.
        all_offer_ids = [row[0] for row in rows]
        phase0_hits = self.db.bulk_lookup_inferred_barcodes(all_offer_ids)
        phase0_applied = 0
        phase0_skipped_ids: set = set()
        if phase0_hits:
            now_iso = datetime.now().isoformat()
            for offer_id, inferred_barcode in phase0_hits.items():
                updated = self.db.update_offer_barcode_if_null(offer_id, inferred_barcode)
                if updated:
                    phase0_applied += 1
                phase0_skipped_ids.add(offer_id)
            print(
                f"Barcode inference Phase 0 (offer state cache): "
                f"{len(phase0_hits)} hits → {phase0_applied} re-applied, "
                f"{len(phase0_hits) - phase0_applied} already had barcode"
            )

        # ── Phase 1: fingerprint cache (cross-market product lookup) ─────────────────
        # Same physical product (same brand+name+size) in different markets hits the
        # same fingerprint — instant barcode assignment without heuristic or LLM.
        remaining_rows = [r for r in rows if r[0] not in phase0_skipped_ids]
        fingerprints_for_rows = {
            row[0]: self.compute_fingerprint(row[2], row[3], row[5])
            for row in remaining_rows
        }
        all_fingerprints = list(set(fingerprints_for_rows.values()))
        phase1_cache = self.db.bulk_lookup_fingerprint_cache(all_fingerprints) if all_fingerprints else {}
        phase1_applied = 0
        phase1_skipped_ids: set = set()
        if phase1_cache:
            now_iso = datetime.now().isoformat()
            phase1_state_rows = []
            for row in remaining_rows:
                fp = fingerprints_for_rows.get(row[0])
                cache_entry = phase1_cache.get(fp) if fp else None
                if not cache_entry:
                    continue
                inferred_barcode = self.db.normalize_barcode(cache_entry["inferred_barcode"])
                if not inferred_barcode:
                    continue
                updated = self.db.update_offer_barcode_if_null(row[0], inferred_barcode)
                if updated:
                    phase1_applied += 1
                phase1_skipped_ids.add(row[0])
                phase1_state_rows.append((
                    row[0],
                    self._build_offer_signature({"product_name": row[2], "brand": row[3], "description": row[4], "unit": row[5]}),
                    None,  # catalog_snapshot not needed for cache hits
                    True,  # matched
                    0,
                    False,
                    now_iso,
                    inferred_barcode,
                ))
            if phase1_state_rows:
                self.db.upsert_barcode_inference_states(phase1_state_rows)
            print(
                f"Barcode inference Phase 1 (fingerprint cache): "
                f"{len(phase1_cache)} fingerprint hits → {phase1_applied} applied"
            )

        # Rows remaining after both fast-path phases
        rows = [r for r in remaining_rows if r[0] not in phase1_skipped_ids]
        total_rows = len(rows)

        # Pre-load entire known_barcodes catalog into memory (avoids one Postgres
        # round trip per offer — the dominant bottleneck at scale).
        print("Barcode inference: loading catalog into memory...")
        catalog_rows = self.db.fetch_all_known_barcodes()
        in_memory_catalog = _KnownBarcodesCatalog(catalog_rows)
        self._known_brands_list = _KnownBrandsList(catalog_rows)
        print(f"Barcode inference: catalog loaded ({len(catalog_rows)} rows)")
        
        # Log top known brands for debugging
        top_brands = self._known_brands_list.get_sorted_brands(limit=10)
        if top_brands:
            brands_str = ", ".join(f"{b[0]}({b[1]})" for b in top_brands)
            print(f"Barcode inference: top brands by frequency: {brands_str}")

        if total_rows == 0:
            print("Barcode inference progress: 100.0% (0/0) | nothing to process")
        else:
            print(
                "Barcode inference progress: 0.0% "
                f"(0/{total_rows}) | preparing candidates"
            )

        def _log_progress(processed_count: int):
            if total_rows <= 0:
                return

            def _format_seconds(total_seconds: float) -> str:
                seconds = max(0, int(round(total_seconds)))
                hours, rem = divmod(seconds, 3600)
                minutes, secs = divmod(rem, 60)
                if hours > 0:
                    return f"{hours}h {minutes:02d}m {secs:02d}s"
                if minutes > 0:
                    return f"{minutes}m {secs:02d}s"
                return f"{secs}s"

            percent = (processed_count / total_rows) * 100.0
            elapsed = max(0.0, time.time() - started_at)
            if processed_count > 0 and elapsed > 0:
                rows_per_second = processed_count / elapsed
                remaining_rows = max(0, total_rows - processed_count)
                eta_seconds = remaining_rows / rows_per_second if rows_per_second > 0 else 0.0
                eta_text = _format_seconds(eta_seconds)
                finish_text = datetime.fromtimestamp(time.time() + eta_seconds).strftime("%H:%M:%S")
            else:
                eta_text = "estimating..."
                finish_text = "--:--:--"

            print(
                "Barcode inference progress: "
                f"{percent:.1f}% ({processed_count}/{total_rows}) | "
                f"scanned={scanned} matched={matched} ai_calls={ai_calls} embedding_calls={embedding_calls} "
                f"skipped_unchanged={skipped_unchanged} skipped_blacklisted={skipped_blacklisted} elapsed={elapsed:.1f}s "
                f"eta={eta_text} finish~{finish_text}"
            )

        # Re-attempt previously unmatched offers whenever any trusted source
        # market changes in the known-barcode catalog content.
        catalog_snapshot = self.db.get_known_barcodes_snapshot(list(self.TRUSTED_SOURCE_MARKETS))
        existing_states = self.db.fetch_barcode_inference_state([row[0] for row in rows])

        scanned = 0
        matched = 0
        ai_calls = 0
        embedding_calls = 0
        embedding_matched = 0
        ai_skipped_budget = 0
        ai_skipped_low_score = 0
        skipped_unchanged = 0
        inference_state_rows: List[Tuple[str, str, Optional[str], bool, int, bool, str]] = []
        model_audit_rows: List[Tuple[str, str, str, str, bool, bool, Optional[str], Optional[str], Optional[str], float, float, str, str, str]] = []
        # Accumulate confirmed matches to write into product_catalog immediately —
        # so next scrape the heuristic finds them without re-running LLM.
        catalog_match_rows: List[tuple] = []
        # Batch LLM: accumulate offers that need AI; flush in groups of AI_BATCH_SIZE
        # so each LLM call covers multiple products (15x fewer API calls than 1-per-offer).
        # For local lmstudio reasoning models (deepseek-r1 etc.), use a smaller batch —
        # they spend many tokens thinking, so 10 products risks finish_reason=length.
        # Remote providers can handle larger batches.
        _default_batch = str(_config.AI_BATCH_SIZE)
        if self._is_lmstudio_enabled() and "lmstudio" not in self._disabled_ai_providers:
            _default_batch = str(int(os.getenv("LM_STUDIO_BATCH_SIZE", "3")))
        ai_batch_size = max(1, int(os.getenv("AI_BATCH_SIZE", _default_batch)))
        ai_pending: List[Dict[str, Any]] = []  # {target, candidates, offer_signature, prior_no_match_count, prior_blacklisted}
        max_ai_calls_raw = int(os.getenv("AI_MAX_CALLS_PER_RUN", str(_config.AI_MAX_CALLS_PER_RUN)))
        max_ai_calls: Optional[int] = None if max_ai_calls_raw <= 0 else max_ai_calls_raw
        ai_delay_seconds = max(0.0, float(os.getenv("AI_CALL_DELAY_SECONDS", "0.75")))
        ai_min_best_score_for_call = float(os.getenv("AI_MIN_BEST_SCORE_FOR_CALL", str(_config.AI_MIN_BEST_SCORE_FOR_CALL)))
        ai_consecutive_miss_limit = int(os.getenv("AI_MAX_CONSECUTIVE_MISSES", "0"))  # 0 = never auto-disable
        ai_consecutive_misses = 0
        ai_disabled_after_misses = False
        blacklist_enabled = self._is_truthy(os.getenv("BARCODE_BLACKLIST_ENABLED", "1"))
        blacklist_skip = self._is_truthy(os.getenv("BARCODE_BLACKLIST_SKIP", "1"))
        blacklist_threshold = max(1, int(os.getenv("BARCODE_BLACKLIST_THRESHOLD", str(_config.BARCODE_BLACKLIST_THRESHOLD))))
        skipped_blacklisted = 0
        blacklisted_marked = 0

        for index, row in enumerate(rows, start=1):
            # Periodically flush accumulated rows to avoid giant end-of-run batch
            # that can exceed Neon/PG connection timeout on long runs (18k+ offers).
            if index % flush_every == 0:
                if inference_state_rows:
                    self.db.upsert_barcode_inference_states(inference_state_rows)
                    inference_state_rows = []
                if catalog_match_rows:
                    self.db.upsert_known_barcodes(catalog_match_rows)
                    catalog_match_rows = []
                if audit_run_id and enable_embedding_model and model_audit_rows:
                    self.db.upsert_model_inference_audit_rows(model_audit_rows)
                    model_audit_rows = []

            target = {
                "id": row[0],
                "market_name": row[1],
                "product_name": row[2],
                "brand": row[3],
                "description": row[4],
                "unit": row[5],
                "product_url": row[6],
                "image_url": row[7] if len(row) > 7 else None,
            }

            offer_signature = self._build_offer_signature(target)
            prior_state = existing_states.get(target["id"])
            prior_no_match_count = int((prior_state or {}).get("no_match_count", 0) or 0)
            prior_blacklisted = bool((prior_state or {}).get("blacklisted"))

            # If catalog has grown since the offer was blacklisted, unblacklist it
            # so it gets a fresh attempt with the richer catalog.
            if prior_blacklisted and prior_state.get("catalog_snapshot") != catalog_snapshot:
                prior_blacklisted = False
                prior_no_match_count = 0

            if prior_blacklisted and blacklist_skip:
                skipped_blacklisted += 1
                inference_state_rows.append(
                    (
                        target["id"],
                        offer_signature,
                        catalog_snapshot,
                        False,
                        prior_no_match_count,
                        True,
                        datetime.now().isoformat(),
                    )
                )
                if index % progress_every == 0 or index == total_rows:
                    _log_progress(index)
                continue

            if (
                respect_cached_unmatched
                and prior_state
                and not bool(prior_state.get("matched"))
                and prior_state.get("offer_signature") == offer_signature
                and prior_state.get("catalog_snapshot") == catalog_snapshot
            ):
                skipped_unchanged += 1
                next_no_match_count = prior_no_match_count
                is_blacklisted = prior_blacklisted
                if blacklist_enabled:
                    next_no_match_count += 1
                    if not is_blacklisted and next_no_match_count >= blacklist_threshold:
                        is_blacklisted = True
                        blacklisted_marked += 1
                inference_state_rows.append(
                    (
                        target["id"],
                        offer_signature,
                        catalog_snapshot,
                        False,
                        next_no_match_count,
                        is_blacklisted,
                        datetime.now().isoformat(),
                    )
                )
                if audit_run_id and enable_embedding_model:
                    model_audit_rows.append(
                        (
                            audit_run_id,
                            target["id"],
                            target["market_name"],
                            self._embedding_model_name,
                            False,
                            False,
                            None,
                            None,
                            None,
                            0.0,
                            0.0,
                            "Skipped unchanged due to prior unmatched state",
                            offer_signature,
                            datetime.now().isoformat(),
                        )
                    )
                if index % progress_every == 0 or index == total_rows:
                    _log_progress(index)
                continue

            scanned += 1

            candidates = self._get_candidate_records(target, catalog=in_memory_catalog)
            if not candidates:
                next_no_match_count = prior_no_match_count + 1 if blacklist_enabled else prior_no_match_count
                is_blacklisted = prior_blacklisted or (
                    blacklist_enabled and next_no_match_count >= blacklist_threshold
                )
                if blacklist_enabled and not prior_blacklisted and is_blacklisted:
                    blacklisted_marked += 1
                inference_state_rows.append(
                    (
                        target["id"],
                        offer_signature,
                        catalog_snapshot,
                        False,
                        next_no_match_count,
                        is_blacklisted,
                        datetime.now().isoformat(),
                    )
                )
                if audit_run_id and enable_embedding_model:
                    model_audit_rows.append(
                        (
                            audit_run_id,
                            target["id"],
                            target["market_name"],
                            self._embedding_model_name,
                            False,
                            False,
                            None,
                            None,
                            None,
                            0.0,
                            0.0,
                            "No candidates available from known barcode catalog",
                            offer_signature,
                            datetime.now().isoformat(),
                        )
                    )
                if index % progress_every == 0 or index == total_rows:
                    _log_progress(index)
                continue

            best = candidates[0]
            second_score = candidates[1]["score"] if len(candidates) > 1 else 0.0
            chosen = None
            method = None
            confidence = 0.0
            reasoning = ""
            embedding_result: Optional[Dict[str, Any]] = None
            applied_to_offer = False

            # ── Optional image-similarity boost (ambiguous heuristic zone) ──────
            if (
                _image_match_enabled
                and _score_images is not None
                and target.get("image_url")
                and best["score"] < heuristic_threshold
                and best["score"] >= (_config.BARCODE_AI_THRESHOLD if hasattr(_config, "BARCODE_AI_THRESHOLD") else 0.65)
            ):
                candidate_image_urls = {
                    c["barcode"]: c.get("image_url")
                    for c in candidates
                    if c.get("image_url")
                }
                if candidate_image_urls:
                    img_scores = _score_images(target["image_url"], candidate_image_urls)
                    IMAGE_WEIGHT = 0.25
                    TEXT_WEIGHT = 0.75
                    for c in candidates:
                        img_s = img_scores.get(c["barcode"])
                        if img_s is not None:
                            c["score"] = TEXT_WEIGHT * c["score"] + IMAGE_WEIGHT * img_s
                    candidates.sort(key=lambda c: c["score"], reverse=True)
                    best = candidates[0]
                    second_score = candidates[1]["score"] if len(candidates) > 1 else 0.0

            if (
                not embedding_model_only
                and best["score"] >= heuristic_threshold
                and (best["score"] - second_score) >= heuristic_margin
            ):
                chosen = best
                method = "heuristic" if not _image_match_enabled else "heuristic+image"
                confidence = best["score"]
                reasoning = "High-confidence normalized name, brand, and measurement match"
            elif enable_embedding_model:
                embedding_calls += 1
                embedding_result = self._call_embedding_matcher(target, candidates)
                if embedding_result and embedding_result.get("matched"):
                    selected_barcode = self.db.normalize_barcode(embedding_result.get("selected_barcode"))
                    selected_market = embedding_result.get("source_market")
                    selected_market_id = embedding_result.get("source_market_id")
                    chosen = next(
                        (
                            candidate
                            for candidate in candidates
                            if candidate["barcode"] == selected_barcode
                            and candidate["source_market"] == selected_market
                            and candidate["source_market_id"] == selected_market_id
                        ),
                        None,
                    )
                    if chosen is not None:
                        embedding_matched += 1
                        method = f"embedding:{embedding_result.get('model', self._embedding_model_name)}"
                        confidence = float(embedding_result.get("confidence", 0.0) or 0.0)
                        reasoning = str(embedding_result.get("reasoning", "Embedding-assisted semantic match"))
            elif (
                not embedding_model_only
                and not ai_disabled_after_misses
                and self.is_ai_matching_enabled()
                and self.has_ai_provider()
            ):
                if best["score"] < ai_min_best_score_for_call:
                    ai_skipped_low_score += 1
                elif max_ai_calls is not None and ai_calls >= max_ai_calls:
                    ai_skipped_budget += 1
                    if index % progress_every == 0 or index == total_rows:
                        _log_progress(index)
                    continue
                else:
                    # Defer to batch AI phase — accumulate instead of calling per-offer.
                    # The batch is flushed every ai_batch_size items and at end of loop.
                    ai_pending.append({
                        "target": target,
                        "candidates": candidates,
                        "offer_signature": offer_signature,
                        "prior_no_match_count": prior_no_match_count,
                        "prior_blacklisted": prior_blacklisted,
                    })
                    if index % progress_every == 0 or index == total_rows:
                        _log_progress(index)
                    continue  # state will be written after batch flush

            if chosen is None:
                next_no_match_count = prior_no_match_count + 1 if blacklist_enabled else prior_no_match_count
                is_blacklisted = prior_blacklisted or (
                    blacklist_enabled and next_no_match_count >= blacklist_threshold
                )
                if blacklist_enabled and not prior_blacklisted and is_blacklisted:
                    blacklisted_marked += 1
                inference_state_rows.append(
                    (
                        target["id"],
                        offer_signature,
                        catalog_snapshot,
                        False,
                        next_no_match_count,
                        is_blacklisted,
                        datetime.now().isoformat(),
                    )
                )
                if audit_run_id and enable_embedding_model:
                    model_audit_rows.append(
                        (
                            audit_run_id,
                            target["id"],
                            target["market_name"],
                            self._embedding_model_name,
                            bool(embedding_result and embedding_result.get("matched")),
                            False,
                            self.db.normalize_barcode(embedding_result.get("selected_barcode")) if embedding_result else None,
                            embedding_result.get("source_market") if embedding_result else None,
                            embedding_result.get("source_market_id") if embedding_result else None,
                            float(embedding_result.get("confidence", 0.0) or 0.0) if embedding_result else 0.0,
                            float(embedding_result.get("second_confidence", 0.0) or 0.0) if embedding_result else 0.0,
                            str(embedding_result.get("reasoning", "Embedding model did not accept a candidate")) if embedding_result else "Embedding model not used",
                            offer_signature,
                            datetime.now().isoformat(),
                        )
                    )
                if index % progress_every == 0 or index == total_rows:
                    _log_progress(index)
                continue

            updated = self.db.update_offer_barcode_if_null(target["id"], chosen["barcode"])
            applied_to_offer = bool(updated)
            if not updated:
                inference_state_rows.append(
                    (
                        target["id"],
                        offer_signature,
                        catalog_snapshot,
                        True,
                        0,
                        False,
                        datetime.now().isoformat(),
                    )
                )
                if audit_run_id and enable_embedding_model:
                    model_audit_rows.append(
                        (
                            audit_run_id,
                            target["id"],
                            target["market_name"],
                            self._embedding_model_name,
                            bool(embedding_result and embedding_result.get("matched")),
                            False,
                            self.db.normalize_barcode(embedding_result.get("selected_barcode")) if embedding_result else None,
                            embedding_result.get("source_market") if embedding_result else None,
                            embedding_result.get("source_market_id") if embedding_result else None,
                            float(embedding_result.get("confidence", 0.0) or 0.0) if embedding_result else 0.0,
                            float(embedding_result.get("second_confidence", 0.0) or 0.0) if embedding_result else 0.0,
                            str(embedding_result.get("reasoning", "Embedding model accepted an already-populated offer")) if embedding_result else "Embedding model not used",
                            offer_signature,
                            datetime.now().isoformat(),
                        )
                    )
                if index % progress_every == 0 or index == total_rows:
                    _log_progress(index)
                continue
            self.db.save_barcode_reference(
                chosen["barcode"],
                target["market_name"],
                target["id"],
                target.get("product_name"),
                target.get("brand"),
            )
            # Write match into product_catalog immediately so the next scrape's
            # heuristic finds it without re-calling the LLM.
            _norm_name = (target.get("product_name") or "").strip().lower()
            _norm_brand = (target.get("brand") or "").strip().lower()
            _measure = self._extract_measure_token(
                target.get("product_name"), target.get("brand"), target.get("description"), target.get("unit")
            )
            catalog_match_rows.append((
                chosen["barcode"],
                target["market_name"],
                target["id"],
                target.get("product_name"),
                target.get("brand"),
                None,
                _norm_name,
                _norm_brand,
                _measure,
                datetime.now().isoformat(),
            ))
            # Store the inferred barcode in the state row (8th element) so Phase 0
            # can re-apply it instantly on the next run without re-doing inference.
            now_iso = datetime.now().isoformat()
            inference_state_rows.append(
                (
                    target["id"],
                    offer_signature,
                    catalog_snapshot,
                    True,
                    0,
                    False,
                    now_iso,
                    chosen["barcode"],  # inferred_barcode — persisted for Phase 0 re-use
                )
            )
            # Save to fingerprint cache (Phase 1) — same product in any other market
            # will get the barcode instantly on the next run without heuristic or LLM.
            fp = self.compute_fingerprint(
                target.get("product_name"), target.get("brand"), target.get("unit")
            )
            fingerprint_cache_rows = [(
                fp,
                chosen["barcode"],
                confidence,
                method or "unknown",
                chosen.get("source_market"),
                chosen.get("source_market_id"),
                now_iso,
            )]
            self.db.upsert_fingerprint_cache_rows(fingerprint_cache_rows)
            self._save_match_audit(
                target_offer_id=target["id"],
                target_market=target["market_name"],
                inferred_barcode=chosen["barcode"],
                source_market=chosen["source_market"],
                source_market_id=chosen["source_market_id"],
                match_method=method or "unknown",
                confidence=confidence,
                reasoning=reasoning,
            )
            if audit_run_id and enable_embedding_model:
                model_audit_rows.append(
                    (
                        audit_run_id,
                        target["id"],
                        target["market_name"],
                        self._embedding_model_name,
                        bool(embedding_result and embedding_result.get("matched")),
                        applied_to_offer,
                        self.db.normalize_barcode(embedding_result.get("selected_barcode")) if embedding_result else None,
                        embedding_result.get("source_market") if embedding_result else None,
                        embedding_result.get("source_market_id") if embedding_result else None,
                        float(embedding_result.get("confidence", 0.0) or 0.0) if embedding_result else 0.0,
                        float(embedding_result.get("second_confidence", 0.0) or 0.0) if embedding_result else 0.0,
                        str(embedding_result.get("reasoning", "Embedding model accepted candidate")) if embedding_result else "Embedding model not used",
                        offer_signature,
                        datetime.now().isoformat(),
                    )
                )
            matched += 1

            if index % progress_every == 0 or index == total_rows:
                _log_progress(index)

        # ── Phase 3: flush deferred batch AI items ───────────────────────────────
        # Process ai_pending in chunks of ai_batch_size — one LLM call per chunk.
        ai_batch_matched = 0
        if ai_pending and not embedding_model_only and self.is_ai_matching_enabled() and self.has_ai_provider():
            _p3_total_batches = (len(ai_pending) + ai_batch_size - 1) // ai_batch_size
            _p3_total_items = len(ai_pending)
            print(f"Barcode inference Phase 3 (batch LLM): {_p3_total_items} items in {_p3_total_batches} batches of {ai_batch_size}")
            _p3_started = time.time()
            _p3_batch_times: list = []   # rolling window of recent batch durations for ETA
            _p3_done_batches = 0
            _p3_failed_batches = 0
            _p3_skipped_budget = 0
            _p3_progress_every = max(1, int(os.getenv("AI_BATCH_PROGRESS_EVERY", "10")))

            def _p3_log(force: bool = False):
                if not force and _p3_done_batches % _p3_progress_every != 0:
                    return
                elapsed = max(0.01, time.time() - _p3_started)
                done_items = min(_p3_done_batches * ai_batch_size, _p3_total_items)
                pct = done_items / _p3_total_items * 100 if _p3_total_items else 100.0
                # ETA from rolling average of last 20 batch durations
                if _p3_batch_times:
                    avg_sec = sum(_p3_batch_times[-20:]) / len(_p3_batch_times[-20:])
                    remaining = _p3_total_batches - _p3_done_batches - _p3_failed_batches
                    eta_sec = max(0, remaining * avg_sec)
                    h, rem = divmod(int(eta_sec), 3600)
                    m, s = divmod(rem, 60)
                    eta_str = (f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s" if m else f"{s}s")
                    finish = datetime.fromtimestamp(time.time() + eta_sec).strftime("%H:%M:%S")
                    speed = f"{avg_sec:.1f}s/batch"
                else:
                    eta_str, finish, speed = "estimating...", "--:--:--", "?"
                print(
                    f"Phase 3 progress: {pct:.1f}% ({_p3_done_batches}/{_p3_total_batches} batches) | "
                    f"matched={ai_batch_matched} failed={_p3_failed_batches} budget_skip={_p3_skipped_budget} | "
                    f"speed={speed} eta={eta_str} finish~{finish}"
                )

            for batch_start in range(0, len(ai_pending), ai_batch_size):
                chunk = ai_pending[batch_start: batch_start + ai_batch_size]
                if max_ai_calls is not None and ai_calls >= max_ai_calls:
                    for item in chunk:
                        _target = item["target"]
                        _prior_nmc = item["prior_no_match_count"]
                        _prior_bl = item["prior_blacklisted"]
                        nmc = _prior_nmc + 1 if blacklist_enabled else _prior_nmc
                        is_bl = _prior_bl or (blacklist_enabled and nmc >= blacklist_threshold)
                        inference_state_rows.append((_target["id"], item["offer_signature"], catalog_snapshot, False, nmc, is_bl, datetime.now().isoformat()))
                    ai_skipped_budget += len(chunk)
                    _p3_skipped_budget += 1
                    continue
                batch_inputs = [{"target": item["target"], "candidates": item["candidates"]} for item in chunk]
                self._last_batch_provider_failed = False
                _batch_t0 = time.time()
                batch_results = self._call_ai_matcher_batch(batch_inputs)
                _batch_elapsed = time.time() - _batch_t0
                if self._last_batch_provider_failed:
                    _p3_failed_batches += 1
                    batch_num = (batch_start // ai_batch_size) + 1
                    print(f"Barcode AI batch: provider failure on batch {batch_num}, skipping {len(chunk)} offers (will retry)")
                    if self._ai_provider_unavailable:
                        print("Barcode AI batch: all providers permanently disabled — stopping Phase 3 early")
                        break
                    _p3_log()
                    continue
                _p3_batch_times.append(_batch_elapsed)
                ai_calls += 1  # count only successful (non-failed) batches against budget
                _p3_done_batches += 1
                now_iso = datetime.now().isoformat()
                batch_had_match = False
                for item, ai_result in zip(chunk, batch_results):
                    _target = item["target"]
                    _candidates = item["candidates"]
                    _offer_sig = item["offer_signature"]
                    _prior_nmc = item["prior_no_match_count"]
                    _prior_bl = item["prior_blacklisted"]
                    _chosen = None
                    _method = None
                    _confidence = 0.0
                    _reasoning = ""
                    if ai_result and ai_result.get("matched"):
                        sel_bc = self.db.normalize_barcode(ai_result.get("selected_barcode"))
                        ai_conf = float(ai_result.get("confidence", 0.0) or 0.0)
                        if sel_bc and ai_conf >= ai_threshold:
                            _chosen = next((c for c in _candidates if c["barcode"] == sel_bc), None)
                            if _chosen:
                                _method = f"ai_batch:{ai_result.get('provider', 'unknown')}"
                                _confidence = ai_conf
                                _reasoning = str(ai_result.get("reasoning", "Batch AI match"))
                                batch_had_match = True
                    if _chosen is None:
                        nmc = _prior_nmc + 1 if blacklist_enabled else _prior_nmc
                        is_bl = _prior_bl or (blacklist_enabled and nmc >= blacklist_threshold)
                        if blacklist_enabled and not _prior_bl and is_bl:
                            blacklisted_marked += 1
                        inference_state_rows.append((_target["id"], _offer_sig, catalog_snapshot, False, nmc, is_bl, now_iso))
                        continue
                    # Apply the match
                    updated = self.db.update_offer_barcode_if_null(_target["id"], _chosen["barcode"])
                    if updated:
                        matched += 1
                        ai_batch_matched += 1
                    self.db.save_barcode_reference(
                        _chosen["barcode"],
                        _target["market_name"],
                        _target["id"],
                        _target.get("product_name"),
                        _target.get("brand"),
                    )
                    inference_state_rows.append((_target["id"], _offer_sig, catalog_snapshot, True, 0, False, now_iso, _chosen["barcode"]))
                    fp = self.compute_fingerprint(_target.get("product_name"), _target.get("brand"), _target.get("unit"))
                    self.db.upsert_fingerprint_cache_rows([(fp, _chosen["barcode"], _confidence, _method or "unknown", _chosen.get("source_market"), _chosen.get("source_market_id"), now_iso)])
                    self._save_match_audit(target_offer_id=_target["id"], target_market=_target["market_name"], inferred_barcode=_chosen["barcode"], source_market=_chosen["source_market"], source_market_id=_chosen["source_market_id"], match_method=_method or "unknown", confidence=_confidence, reasoning=_reasoning)
                    _norm_name = (_target.get("product_name") or "").strip().lower()
                    _norm_brand = (_target.get("brand") or "").strip().lower()
                    _measure = self._extract_measure_token(_target.get("product_name"), _target.get("brand"), _target.get("description"), _target.get("unit"))
                    catalog_match_rows.append((_chosen["barcode"], _target["market_name"], _target["id"], _target.get("product_name"), _target.get("brand"), None, _norm_name, _norm_brand, _measure, now_iso))
                # Update consecutive miss counter once per batch (not per item within it)
                if batch_had_match:
                    ai_consecutive_misses = 0
                else:
                    ai_consecutive_misses += 1
                    if ai_consecutive_miss_limit > 0 and ai_consecutive_misses >= ai_consecutive_miss_limit and not ai_disabled_after_misses:
                        ai_disabled_after_misses = True
                        print(f"Barcode AI batch: auto-disabling after {ai_consecutive_misses} consecutive batches with no match")
                _p3_log()
            _p3_log(force=True)
            print(f"Barcode inference Phase 3 complete: batch_ai_matched={ai_batch_matched} total_ai_calls={ai_calls}")

        self.db.upsert_barcode_inference_states(inference_state_rows)
        if catalog_match_rows:
            self.db.upsert_known_barcodes(catalog_match_rows)
        if audit_run_id and enable_embedding_model:
            self.db.upsert_model_inference_audit_rows(model_audit_rows)

        return {
            "catalog_db_path": self.catalog_db_path,
            "scanned": scanned,
            "phase0_cache_hits": len(phase0_hits),
            "phase0_applied": phase0_applied,
            "phase1_fingerprint_hits": len(phase1_cache),
            "phase1_applied": phase1_applied,
            "matched": matched,
            "ai_batch_matched": ai_batch_matched,
            "ai_pending_total": len(ai_pending),
            "ai_calls": ai_calls,
            "embedding_calls": embedding_calls,
            "embedding_matched": embedding_matched,
            "ai_skipped_budget": ai_skipped_budget,
            "ai_skipped_low_score": ai_skipped_low_score,
            "ai_disabled_after_misses": ai_disabled_after_misses,
            "blacklist_enabled": blacklist_enabled,
            "blacklist_threshold": blacklist_threshold,
            "blacklisted_marked": blacklisted_marked,
            "skipped_blacklisted": skipped_blacklisted,
            "ai_enabled": self.is_ai_matching_enabled() and self.has_ai_provider(),
            "embedding_model_enabled": enable_embedding_model,
            "embedding_model_only": embedding_model_only,
            "embedding_model_name": self._embedding_model_name if enable_embedding_model else None,
            "audit_run_id": audit_run_id,
            "audit_rows_written": len(model_audit_rows),
            "audit_table": "model_inference_audit" if audit_run_id and enable_embedding_model else None,
            "skipped_unchanged": skipped_unchanged,
        }

    def run_embedding_model_inference(
        self,
        target_markets: Optional[List[str]] = None,
        limit: Optional[int] = None,
        target_offer_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        run_id = f"embedding-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        stats = self.infer_missing_barcodes(
            target_markets=target_markets,
            limit=limit,
            target_offer_ids=target_offer_ids,
            enable_embedding_model=True,
            embedding_model_only=True,
            audit_run_id=run_id,
            respect_cached_unmatched=False,
        )
        stats["audit_run_id"] = run_id
        return stats


if __name__ == "__main__":
    load_env_file()
    matcher = BarcodeAIMatcher()
    sync_stats = matcher.sync_known_barcodes()
    print(f"Barcode driver sync: {sync_stats}")
    infer_stats = matcher.infer_missing_barcodes(limit=200)
    print(f"Barcode inference: {infer_stats}")

"""image_matcher.py — Optional CLIP-based image similarity for barcode matching.

Uses the CLIP ViT-B/32 model loaded via sentence-transformers (already in
requirements-ai.txt).  When IMAGE_MATCH_ENABLED=0 (the default) this module
is imported but the model is never loaded — zero overhead.

Scoring contract
----------------
image_similarity(url_a, url_b) -> float in [0, 1] or None
  None  = one or both images could not be fetched / model unavailable
  0.0   = completely different images
  1.0   = identical images

Caching
-------
Image embeddings are cached in-memory by URL for the duration of the process.
This means the same product image is only downloaded and encoded once per run,
even if it appears in many candidate pairs.
"""

from __future__ import annotations

import io
import logging
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)

# ── module-level caches (process-scoped) ─────────────────────────────────────
_model = None          # SentenceTransformer CLIP instance
_model_tried = False   # avoid repeated import attempts if install is missing
_embed_cache: Dict[str, object] = {}   # url → embedding tensor
_http = requests.Session()


def _load_model():
    global _model, _model_tried
    if _model_tried:
        return _model
    _model_tried = True
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
        _model = SentenceTransformer("clip-ViT-B-32")
        logger.info("image_matcher: CLIP model loaded (clip-ViT-B-32)")
    except Exception as exc:
        logger.warning(
            "image_matcher: CLIP model unavailable (%s). "
            "Install sentence-transformers and torch, or set IMAGE_MATCH_ENABLED=0.",
            exc,
        )
        _model = None
    return _model


def _fetch_image(url: str) -> Optional[object]:
    """Download image URL and return a PIL Image, or None on failure."""
    try:
        from PIL import Image  # type: ignore
        resp = _http.get(url, timeout=8, stream=True)
        if resp.status_code != 200:
            return None
        return Image.open(io.BytesIO(resp.content)).convert("RGB")
    except Exception:
        return None


def _get_embedding(url: str):
    """Return CLIP embedding for an image URL, using in-memory cache."""
    if url in _embed_cache:
        return _embed_cache[url]

    model = _load_model()
    if model is None:
        return None

    img = _fetch_image(url)
    if img is None:
        _embed_cache[url] = None
        return None

    try:
        embedding = model.encode(img, convert_to_tensor=True, normalize_embeddings=True)
        _embed_cache[url] = embedding
        return embedding
    except Exception as exc:
        logger.debug("image_matcher: encode failed for %s: %s", url, exc)
        _embed_cache[url] = None
        return None


def image_similarity(url_a: Optional[str], url_b: Optional[str]) -> Optional[float]:
    """Cosine similarity between two product images.

    Returns
    -------
    float in [0, 1]  — 1.0 = same image, 0.0 = completely different
    None             — one or both images unavailable / model not loaded
    """
    if not url_a or not url_b:
        return None

    emb_a = _get_embedding(url_a)
    emb_b = _get_embedding(url_b)
    if emb_a is None or emb_b is None:
        return None

    try:
        from sentence_transformers import util  # type: ignore
        score = float(util.cos_sim(emb_a, emb_b).item())
        # Clamp to [0, 1] — cosine can return tiny negatives for dissimilar images
        return max(0.0, min(score, 1.0))
    except Exception:
        return None


def score_candidates_with_images(
    target_image_url: Optional[str],
    candidate_image_urls: Dict[str, Optional[str]],  # barcode → image_url
) -> Dict[str, float]:
    """Return image similarity scores for a dict of barcode → image_url pairs.

    Only barcodes with a valid image URL are scored; others are omitted from
    the result dict.  Caller should treat missing barcodes as score=0.
    """
    scores: Dict[str, float] = {}
    if not target_image_url:
        return scores

    for barcode, candidate_url in candidate_image_urls.items():
        sim = image_similarity(target_image_url, candidate_url)
        if sim is not None:
            scores[barcode] = sim
    return scores

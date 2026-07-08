import re
import unicodedata
import json
import os
from typing import Any, Dict, List

import gradio as gr
from sentence_transformers import SentenceTransformer, util


MODEL_NAME = "intfloat/multilingual-e5-small"
model = SentenceTransformer(MODEL_NAME)

MATCH_THRESHOLD = float(os.getenv("SPACE_MATCH_THRESHOLD", "0.90"))
MATCH_MARGIN = float(os.getenv("SPACE_MATCH_MARGIN", "0.04"))


def _extract_measure_token(payload: Dict[str, Any]) -> str:
    combined = " ".join(
        [
            _normalize_text(payload.get("product_name", "")),
            _normalize_text(payload.get("brand", "")),
            _normalize_text(payload.get("description", "")),
            _normalize_text(payload.get("unit", "")),
        ]
    ).strip()
    if not combined:
        return ""

    match = re.search(r"(\d+[\.,]?\d*)\s*(kg|g|mg|ml|l|lt|un|und)", combined)
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


def _normalize_text(value: str) -> str:
    text = "" if value is None else str(value)
    text = "".join(
        char for char in unicodedata.normalize("NFKD", text) if not unicodedata.combining(char)
    )
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _build_text(payload: Dict[str, Any]) -> str:
    name = _normalize_text(payload.get("product_name", ""))
    brand = _normalize_text(payload.get("brand", ""))
    desc = _normalize_text(payload.get("description", ""))
    unit = _normalize_text(payload.get("unit", ""))
    return f"query: {name} | {brand} | {desc} | {unit}"


def _match(target: Dict[str, Any], candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not candidates:
        return {
            "matched": False,
            "selected_barcode": None,
            "confidence": 0.0,
            "reasoning": "No candidates received",
        }

    target_text = _build_text(target)
    candidate_texts = [_build_text(item) for item in candidates]

    target_embedding = model.encode([target_text], convert_to_tensor=True, normalize_embeddings=True)
    candidate_embeddings = model.encode(candidate_texts, convert_to_tensor=True, normalize_embeddings=True)
    scores = util.cos_sim(target_embedding, candidate_embeddings)[0]

    best_idx = int(scores.argmax().item())
    best_score = float(scores[best_idx].item())
    best = candidates[best_idx]
    second_score = 0.0
    if len(candidates) > 1:
        sorted_scores = sorted((float(s.item()) for s in scores), reverse=True)
        second_score = sorted_scores[1]

    target_brand = _normalize_text(target.get("brand", ""))
    best_brand = _normalize_text(best.get("brand", ""))
    target_measure = _extract_measure_token(target)
    best_measure = _extract_measure_token(best)

    brand_match = bool(target_brand and best_brand and target_brand == best_brand)
    measure_match = bool(target_measure and best_measure and target_measure == best_measure)
    margin_ok = (best_score - second_score) >= MATCH_MARGIN
    strong_structured_match = brand_match and measure_match

    is_match = best_score >= MATCH_THRESHOLD and (margin_ok or strong_structured_match)

    return {
        "matched": is_match,
        "selected_barcode": best.get("barcode") if is_match else None,
        "source_market": best.get("source_market") if is_match else None,
        "source_market_id": best.get("source_market_id") if is_match else None,
        "confidence": round(best_score, 4),
        "reasoning": (
            "Embedding similarity on normalized product text"
            if is_match
            else (
                "Best candidate below threshold"
                if best_score < MATCH_THRESHOLD
                else (
                    "Accepted by brand and measure compatibility"
                    if strong_structured_match
                    else "Ambiguous top candidates (low score margin)"
                )
            )
        ),
    }


def match_endpoint(payload_text: str) -> str:
    try:
        payload = json.loads(payload_text)
        target = payload.get("target") or {}
        candidates = payload.get("candidates") or []
        result = _match(target, candidates)
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"matched": False, "error": str(exc)}, ensure_ascii=False)


demo = gr.Interface(
    fn=match_endpoint,
    inputs=gr.Textbox(lines=16, label="Payload dict with target and candidates"),
    outputs=gr.Textbox(lines=8, label="Result"),
    title="Barcode Matcher API Prototype",
    description="Prototype semantic matcher for product-to-barcode selection.",
)


if __name__ == "__main__":
    demo.launch()

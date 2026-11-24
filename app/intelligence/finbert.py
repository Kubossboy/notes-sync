"""Optional local FinBERT news sentiment (ProsusAI/finbert), CPU-only.

Loads lazily on first use. Returns P(positive) - P(negative) in [-1, 1].
Load or inference failures return None so the caller can fall back silently.
"""
from __future__ import annotations

MODEL_ID = "ProsusAI/finbert"

_tokenizer = None
_model = None
_id2label: dict[int, str] = {}
_failed = False


def _load() -> bool:
    global _tokenizer, _model, _id2label, _failed
    if _failed:
        return False
    if _model is not None:
        return True
    try:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        tok = AutoTokenizer.from_pretrained(MODEL_ID)
        model = AutoModelForSequenceClassification.from_pretrained(MODEL_ID)
        model.eval()
        model.to("cpu")
        _tokenizer = tok
        _model = model
        _id2label = {int(k): str(v).lower() for k, v in model.config.id2label.items()}
        return True
    except Exception:
        _failed = True
        _tokenizer = None
        _model = None
        _id2label = {}
        return False


def finbert_score(text: str) -> float | None:
    """Return P(positive) - P(negative) in [-1, 1], or None if unavailable."""
    if not text or not str(text).strip():
        return None
    if not _load():
        return None
    try:
        import torch
        from torch.nn.functional import softmax

        enc = _tokenizer(str(text), return_tensors="pt", truncation=True, max_length=512)
        with torch.no_grad():
            probs = softmax(_model(**enc).logits, dim=-1)[0]
        pos = neg = 0.0
        for i, p in enumerate(probs.tolist()):
            label = _id2label.get(i, "")
            if label == "positive":
                pos = p
            elif label == "negative":
                neg = p
        return round(float(pos - neg), 4)
    except Exception:
        return None

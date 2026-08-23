from __future__ import annotations
import logging
import os
import numpy as np

# Default to cache-only mode so huggingface_hub never retries on startup.
# Without this, a missing model triggers 5 retries × ~10 files = ~60s of SSL spam.
# To download all-MiniLM-L6-v2 on first run:
#   HF_HUB_OFFLINE=0 uv run uvicorn backend.main:app
# After the model is cached it loads instantly regardless of this setting.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

logger = logging.getLogger(__name__)

_sentence_model = None          # sentence-transformers model (primary)
_tfidf_vectorizer = None        # scikit-learn TF-IDF (offline fallback)
_use_tfidf_fallback = False     # flipped on if sentence-transformers unavailable


def _try_init_sentence_transformers() -> bool:
    """Attempt to load sentence-transformers. Returns True on success."""
    global _sentence_model
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
        _sentence_model = SentenceTransformer("all-MiniLM-L6-v2")
        logger.info("Embedder: using sentence-transformers all-MiniLM-L6-v2")
        return True
    except Exception as exc:
        logger.warning(
            "sentence-transformers unavailable (%s). "
            "Falling back to TF-IDF cosine similarity.",
            exc,
        )
        return False


def _tfidf_embed(texts: list[str]) -> np.ndarray:
    """TF-IDF vectorisation with L2 normalisation.

    This is a drop-in fallback for when the sentence-transformer model
    cannot be downloaded (e.g., air-gapped CI or offline sandbox).
    Quality is lower than neural embeddings but sufficient for the
    ~15-chunk policy doc this system uses.
    """
    from sklearn.feature_extraction.text import TfidfVectorizer  # type: ignore
    from sklearn.preprocessing import normalize  # type: ignore

    global _tfidf_vectorizer

    if _tfidf_vectorizer is None:
        _tfidf_vectorizer = TfidfVectorizer(
            analyzer="word",
            ngram_range=(1, 2),
            min_df=1,
            sublinear_tf=True,
        )

    if not hasattr(_tfidf_vectorizer, "vocabulary_"):
        # Vectorizer not fitted yet — fit on the incoming texts
        matrix = _tfidf_vectorizer.fit_transform(texts)
    else:
        matrix = _tfidf_vectorizer.transform(texts)

    dense = matrix.toarray().astype(np.float32)
    return normalize(dense, norm="l2")


def prime_tfidf(corpus: list[str]) -> None:
    """Fit the TF-IDF fallback on the full corpus at startup (call once).

    If sentence-transformers loads successfully this is a no-op.
    """
    global _use_tfidf_fallback
    if _use_tfidf_fallback:
        _tfidf_embed(corpus)   # fits the vectorizer in place


def init_embedder(corpus: list[str]) -> None:
    """Called once at startup to select the embedding backend.

    Tries sentence-transformers first; falls back to TF-IDF and fits it
    on the policy corpus so query-time transforms are consistent.
    """
    global _use_tfidf_fallback
    ok = _try_init_sentence_transformers()
    if not ok:
        _use_tfidf_fallback = True
        prime_tfidf(corpus)


def embed_texts(texts: list[str]) -> np.ndarray:
    """Embed texts → shape (n, dim) float32, L2-normalised.

    Primary: sentence-transformers all-MiniLM-L6-v2 (~80 MB, neural).
    Fallback: TF-IDF + cosine normalisation (no network, instant).

    Both return L2-normalised vectors so dot-product == cosine similarity.
    In production AZ would use Azure OpenAI embeddings for data residency.
    """
    global _use_tfidf_fallback
    if not _use_tfidf_fallback:
        if _sentence_model is None:
            # First call without init_embedder — try to load on demand
            if not _try_init_sentence_transformers():
                _use_tfidf_fallback = True

        if not _use_tfidf_fallback and _sentence_model is not None:
            embeddings = _sentence_model.encode(
                texts,
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            return embeddings.astype(np.float32)

    return _tfidf_embed(texts)

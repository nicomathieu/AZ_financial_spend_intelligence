from __future__ import annotations
import numpy as np


def retrieve(
    query_embedding: np.ndarray,
    chunk_embeddings: np.ndarray,
    chunks: list[dict],
    top_k: int = 3,
) -> list[dict]:
    """Return top_k chunks ranked by cosine similarity.

    Both embedding arrays must be L2-normalised (embed_texts does this), so
    cosine similarity == dot product — O(n_chunks) with a single matrix multiply.
    n_chunks ~ 15 for the current policy doc: numpy is more than sufficient.
    """
    similarities: np.ndarray = chunk_embeddings @ query_embedding   # (n_chunks,)
    top_indices = np.argsort(similarities)[::-1][:top_k]
    return [
        {**chunks[i], "similarity_score": float(similarities[i])}
        for i in top_indices
    ]

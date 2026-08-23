from __future__ import annotations
import numpy as np
import duckdb
from fastapi import Request

from backend.rag.embedder import embed_texts
from backend.rag.retriever import retrieve


class RAGEngine:
    """In-memory RAG index built once at startup from the policy document.

    ~15 chunks for the current policy — numpy cosine similarity is O(15),
    instantaneous. No vector DB dependency needed at this scale.
    """

    def __init__(self, chunks: list[dict], embeddings: np.ndarray) -> None:
        self.chunks = chunks
        self.embeddings = embeddings   # shape (n_chunks, dim), L2-normalised

    def retrieve(self, query: str, top_k: int = 3) -> list[dict]:
        query_emb = embed_texts([query])[0]
        return retrieve(query_emb, self.embeddings, self.chunks, top_k)


def get_db(request: Request) -> duckdb.DuckDBPyConnection:
    return request.app.state.db


def get_rag(request: Request) -> RAGEngine:
    return request.app.state.rag

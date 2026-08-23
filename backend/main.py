from __future__ import annotations
import os
from contextlib import asynccontextmanager

import litellm
import duckdb
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Load .env file if present (development convenience — no-op in prod)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from backend.rag.chunker import chunk_policy
from backend.rag.embedder import embed_texts, init_embedder
from backend.dependencies import RAGEngine
from backend.routes import ask, quality, invoices


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: open DB, init Anthropic client, build RAG index.
    Shutdown: close DB connection.
    """
    db_path = os.environ.get("DB_PATH", "data/spend_intelligence.duckdb")
    policy_path = os.environ.get("POLICY_PATH", "data/procurement_policy.md")

    # Pipeline owns writes; API gets a read-only connection — safe for concurrency
    app.state.db = duckdb.connect(db_path, read_only=True)

    # Configure litellm to route through the LiteLLM proxy.
    # All calls to litellm.completion() will use these settings automatically.
    litellm_host = os.environ.get("LITELLM_HOST", "").rstrip("/")
    litellm_key = os.environ.get("LITELLM_MASTER_KEY", "")
    if litellm_host:
        litellm.api_base = litellm_host
    if litellm_key:
        litellm.api_key = litellm_key
    print(f"LiteLLM configured: api_base={litellm.api_base or '(default)'}")

    # Select embedding backend and build index once at startup
    # init_embedder tries sentence-transformers; falls back to TF-IDF if unavailable
    chunks = chunk_policy(policy_path)
    texts = [f"{c['section_id']} {c['title']}\n\n{c['content']}" for c in chunks]
    init_embedder(texts)       # selects backend and fits TF-IDF corpus if needed
    embeddings = embed_texts(texts)
    app.state.rag = RAGEngine(chunks=chunks, embeddings=embeddings)

    print(f"RAG index ready: {len(chunks)} policy chunks embedded.")
    print(f"DB: {db_path} (read-only)")

    yield

    app.state.db.close()


app = FastAPI(
    title="Finance Spend Intelligence Assistant",
    description="RAG-powered Q&A over AstraZeneca procurement policy and invoice data",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ask.router)
app.include_router(quality.router)
app.include_router(invoices.router)

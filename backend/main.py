from __future__ import annotations
import os
from contextlib import asynccontextmanager

import litellm
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Load .env file if present (development convenience — no-op in prod)
try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass

from backend.exceptions import APIError, api_error_handler, unhandled_error_handler
from backend.rag.chunker import chunk_policy
from backend.rag.embedder import embed_texts, init_embedder
from backend.dependencies import RAGEngine
from backend.routes import ask, quality, invoices


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: store DB path, init LiteLLM, build RAG index."""
    db_path = os.environ.get("DB_PATH", "data/spend_intelligence.duckdb")
    policy_path = os.environ.get("POLICY_PATH", "data/procurement_policy.md")

    app.state.db_path = db_path

    # Configure litellm to route through the LiteLLM proxy.
    # All calls to litellm.completion() will use these settings automatically.
    litellm_host = os.environ.get("LITELLM_HOST", "").rstrip("/")
    litellm_key = os.environ.get("LITELLM_MASTER_KEY", "")
    if litellm_host:
        litellm.api_base = litellm_host
    if litellm_key:
        litellm.api_key = litellm_key
    litellm_model = os.environ.get("LITELLM_MODEL", "")
    print(f"LiteLLM configured: api_base={litellm.api_base or '(default)'}, model={litellm_model or '(NOT SET)'}")

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


def create_app() -> FastAPI:
    application = FastAPI(
        title="Finance Spend Intelligence Assistant",
        description="RAG-powered Q&A over procurement policy and invoice data",
        version="1.0.0",
        lifespan=lifespan,
    )

    # Middleware — order matters: CORS must wrap all routes
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    application.include_router(ask.router)
    application.include_router(quality.router)
    application.include_router(invoices.router)

    # All HTTP error translation lives here — routes raise semantic exceptions only
    application.add_exception_handler(APIError, api_error_handler)
    application.add_exception_handler(Exception, unhandled_error_handler)

    return application


app = create_app()

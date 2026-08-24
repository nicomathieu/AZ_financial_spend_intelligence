from __future__ import annotations
import json
import os
from typing import Optional

import litellm
import duckdb
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.dependencies import get_db, get_rag, RAGEngine
from backend.sql_agent.query_builder import generate_sql

router = APIRouter()

SYSTEM_PROMPT = """You are a finance compliance assistant for AstraZeneca's \
Procurement Controls team. You answer questions about invoice spend using:
1. Retrieved sections from the Global Procurement & Spend Policy
2. Live invoice data from the AP system (SQL query results)

Rules:
- Always cite the policy section (e.g. §2.1) when referencing a rule
- Always show what data evidence supports your answer
- If data is insufficient or unavailable, say so explicitly — do not invent numbers
- Flag when a question requires data you don't have (e.g. approver roles need HR integration)
- Be concise — finance analysts want the answer, then the evidence
- Never invent invoice numbers, vendor names, or amounts

Confidence levels:
- high: both policy and data evidence available
- medium: policy only, or data only
- low: question requires unavailable data (e.g. approver role mapping)
"""

DATA_KEYWORDS: frozenset[str] = frozenset(
    {
        "invoice", "invoices", "vendor", "spend", "amount",
        "total", "count", "which", "list", "how many", "who",
    }
)
POLICY_KEYWORDS: frozenset[str] = frozenset(
    {
        "policy", "rule", "allowed", "required", "threshold",
        "approve", "approval", "must", "should", "what does",
    }
)
APPROVER_ROLE_SIGNALS: frozenset[str] = frozenset(
    {"who can approve", "who is allowed to approve", "authorized to approve", "approver role"}
)

DISCLAIMER = (
    "Data freshness note: results reflect the last pipeline run. "
    "Re-run the pipeline against a new CSV export for up-to-date figures."
)


class AskRequest(BaseModel):
    question: str
    include_evidence: bool = True


class Evidence(BaseModel):
    policy_sections: list[str]
    policy_sources: list[str]
    sql_query: Optional[str]
    sql_results: Optional[list[dict]]
    source: str  # "policy_only" | "data_only" | "hybrid"


class AskResponse(BaseModel):
    answer: str
    evidence: Optional[Evidence]
    confidence: str   # "high" | "medium" | "low"
    disclaimer: str


@router.post("/ask", response_model=AskResponse)
def ask_question(
    body: AskRequest,
    db: duckdb.DuckDBPyConnection = Depends(get_db),
    rag: RAGEngine = Depends(get_rag),
):
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=422, detail="question must not be empty")

    # ── 1. Retrieve top-3 policy chunks ─────────────────────────────────────
    policy_chunks = rag.retrieve(question, top_k=3)
    policy_sections = [
        f"**{c['section_id']} {c['title']}**\n{c['content']}"
        for c in policy_chunks
    ]
    policy_sources = [f"{c['section_id']} {c['title']}" for c in policy_chunks]

    # ── 2. Classify intent ───────────────────────────────────────────────────
    q_lower = question.lower()
    needs_data = any(kw in q_lower for kw in DATA_KEYWORDS)
    needs_policy = any(kw in q_lower for kw in POLICY_KEYWORDS)
    if not needs_data and not needs_policy:
        needs_data = True
        needs_policy = True

    # ── 3. Generate and execute SQL ──────────────────────────────────────────
    sql_query: Optional[str] = None
    sql_results: Optional[list[dict]] = None
    sql_error: Optional[str] = None

    if needs_data:
        sql_query = generate_sql(question)
        if sql_query:
            try:
                df = db.execute(sql_query).fetchdf()
                # Serialise date/datetime objects to strings so JSON round-trips
                sql_results = [
                    {
                        k: (v.isoformat() if hasattr(v, "isoformat") else v)
                        for k, v in row.items()
                    }
                    for row in df.to_dict(orient="records")
                ]
            except Exception as exc:
                sql_error = str(exc)
                sql_query = None   # don't surface a broken query in evidence

    # ── 4. Classify source ───────────────────────────────────────────────────
    if sql_results is not None and needs_policy:
        source = "hybrid"
    elif sql_results is not None:
        source = "data_only"
    else:
        source = "policy_only"

    # ── 5. Confidence ────────────────────────────────────────────────────────
    needs_role_lookup = any(sig in q_lower for sig in APPROVER_ROLE_SIGNALS)
    if needs_role_lookup and not sql_results:
        confidence = "low"
    elif sql_results is not None and policy_chunks:
        confidence = "high"
    else:
        confidence = "medium"

    # ── 6. Build LLM prompt with retrieved context ───────────────────────────
    context_parts: list[str] = []

    if policy_sections:
        context_parts.append(
            "**POLICY CONTEXT (retrieved sections):**\n\n"
            + "\n\n---\n\n".join(policy_sections)
        )

    if sql_query and sql_results is not None:
        # Limit preview to 20 rows to avoid token explosion in LLM context
        preview = sql_results[:20]
        context_parts.append(
            f"**SQL QUERY EXECUTED:**\n```sql\n{sql_query}\n```\n\n"
            f"**RESULTS ({len(sql_results)} rows, showing up to 20):**\n"
            + json.dumps(preview, indent=2, default=str)
        )
    elif sql_error:
        context_parts.append(f"**SQL QUERY FAILED:** {sql_error}")
    elif needs_data and not sql_query:
        context_parts.append(
            "**NOTE:** Could not generate a safe SQL query for the data portion of this question."
        )

    user_prompt = f"Question: {question}\n\n" + "\n\n".join(context_parts)

    # ── 7. Call LLM for answer ────────────────────────────────────────────────
    try:
        llm_response = litellm.completion(
            model=os.environ.get("LITELLM_MODEL", ""),
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=4096,
        )
        answer = llm_response.choices[0].message.content or ""
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"LLM call failed: {exc}. Please retry in a few seconds.",
        ) from exc

    evidence: Optional[Evidence] = None
    if body.include_evidence:
        evidence = Evidence(
            policy_sections=policy_sections,
            policy_sources=policy_sources,
            sql_query=sql_query,
            sql_results=sql_results,
            source=source,
        )

    return AskResponse(
        answer=answer,
        evidence=evidence,
        confidence=confidence,
        disclaimer=DISCLAIMER,
    )

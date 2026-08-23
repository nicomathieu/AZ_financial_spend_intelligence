from __future__ import annotations
import anthropic

SQL_SYSTEM_PROMPT = """You generate safe, read-only SQL queries for a DuckDB database.

Schema:
- invoices_enriched: row_id, invoice_number, invoice_date, date_ambiguous,
  vendor_id, vendor_name_raw, vendor_name_normalized, description, amount,
  currency, cost_center, po_number, approved_by, quarantined, quarantine_reason,
  is_credit_note, vendor_master_name, vendor_country, vendor_category, vendor_status
- compliance_flags: row_id, invoice_number, flag_type, detail, indicative_only
  flag_type values: NO_PO, PENDING_APPROVAL, OVERDUE_APPROVAL, BLOCKED_VENDOR,
  VENDOR_ON_HOLD, APPROVAL_LEVEL_VIOLATION, CREDIT_NOTE, POTENTIAL_DUPLICATE,
  LOGICAL_DUPLICATE_CANDIDATE
- vendors: vendor_id, vendor_name, country, category, status

Allowed tables (query ONLY these):
- invoices_enriched
- compliance_flags
- vendors

Do not query pipeline_log, audit_log, or invoices_clean directly.
These are internal pipeline tables not intended for analyst access.

Rules:
- Return ONLY the SQL query, nothing else — no markdown fences, no explanation
- SELECT statements only — no INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, TRUNCATE
- Use invoices_enriched (not invoices_clean) for all invoice queries
- Filter out quarantined rows unless the question explicitly asks about quarantined: WHERE quarantined = false
- Always LIMIT 100 unless the question asks for totals or aggregations
- Use amount in EUR where possible; note currency when mixing
"""

_DANGEROUS = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "DROP",
        "CREATE",
        "ALTER",
        "TRUNCATE",
        "REPLACE",
        "MERGE",
        "UPSERT",
    }
)


def _is_safe_select(sql: str) -> bool:
    """Two-layer guard: must start with SELECT, must not contain DML keywords."""
    stripped = sql.strip().upper()
    if not stripped.startswith("SELECT"):
        return False
    tokens = set(stripped.split())
    return tokens.isdisjoint(_DANGEROUS)


def generate_sql(question: str, client: anthropic.Anthropic) -> str | None:
    """Ask Claude for a SELECT query. Returns None on failure or unsafe output."""
    try:
        response = client.messages.create(
            model="claude-opus-5",
            max_tokens=1024,
            system=SQL_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": question}],
        )
        raw = response.content[0].text.strip()

        # Strip markdown code fences if the model wrapped the query
        if raw.startswith("```"):
            inner = raw.split("```")[1]
            if inner.lower().startswith("sql"):
                inner = inner[3:]
            raw = inner.strip()

        return raw if _is_safe_select(raw) else None

    except Exception:
        return None

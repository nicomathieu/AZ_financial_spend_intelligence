# Finance Spend Intelligence Assistant

## 1. Overview

A production-grade prototype for AstraZeneca's Finance Procurement Controls team. The **data pipeline** ingests raw invoice CSVs, repairs 9 classes of data quality issues, and flags compliance violations against FIN-POL-014. The **FastAPI backend** serves a RAG endpoint over the policy document and a natural-language-to-SQL agent over DuckDB. The **React frontend** exposes a chat interface and a compliance dashboard with flagged invoice detail. Every transformation is audit-logged for SOX §8.2 lineage.

---

## 2. Quick Start

```bash
# Setup (uv required — https://docs.astral.sh/uv)
uv sync

# Run pipeline (cleans invoices, writes DuckDB, generates quality report)
uv run python main.py

# Or run the pipeline module directly
uv run python -m pipeline.pipeline

# Start backend
uv run uvicorn backend.main:app --reload

# Start frontend
cd frontend && npm install && npm start
```

> **Quality report** is written automatically to `data/quality_report.md` and `data/quality_report.json` on every pipeline run.

---

## 3. Architecture & Key Decisions

### Stack choices

| Choice | Rationale |
|--------|-----------|
| **DuckDB** | In-process OLAP engine — native CSV ingestion, full SQL, columnar storage. SQLite would be the wrong tool for analytical queries over financial data. Clear migration path to Snowflake/Databricks via Parquet export. |
| **No LangChain** | Every component is written by hand and fully defensible. LangChain would abstract the embedding strategy, retrieval logic, and prompt construction — exactly the decisions an interviewer will probe. |
| **Pydantic v2** | Automatic validation, native JSON serialisation, `AuditEntry(frozen=True)` for SOX §8.2 immutability. |
| **difflib cosine vs vector DB** | The policy document produces ≤15 chunks. NumPy cosine similarity is sufficient and introduces zero external infrastructure. A vector DB (Pinecone, ChromaDB) would be the right call at 10,000+ chunks. |
| **uv** | Reproducible lockfile (`uv.lock`), fast installs, Python version pinning — correct default for a prototype that will be handed over. |
| **Single repo** | Appropriate for a prototype. In production: two repos with independent CI/CD pipelines and versioned API contracts between backend and frontend. |

### Pipeline design

**No silent data loss.** Every problematic row is quarantined with an explicit reason and retained in `invoices_clean` with `quarantined=true`. Nothing is ever dropped silently.

**Imperfect key join — 3 layers deliberately.**
1. Malformed key (`V1002`) → regex fix before any join attempt (`cleaners.normalize_vendor_id`)
2. Missing key (empty string) → fuzzy match on `vendor_name` against master (`cleaners.resolve_vendor_id`)
3. Unknown key post-normalization → quarantine with reason rather than producing a compliance blind spot
4. `invoices_enriched` view = `invoices_clean LEFT JOIN vendors` — LEFT JOIN keeps quarantined rows (NULL vendor columns) visible instead of silently dropping them from downstream queries

**Idempotent.** Data tables are dropped and recreated on each run. Same input → same output. `pipeline_log` is append-only to preserve run history.

**`AuditEntry(frozen=True)`.** Pydantic's `frozen=True` enforces immutability at the Python level. Once a transformation is recorded it cannot be mutated — required for SOX §8.2 lineage.

**`reference_date = max(invoice_date)`.** `datetime.now()` would produce different `OVERDUE_APPROVAL` flags on every run — incompatible with auditability. Using the dataset's own time horizon makes the pipeline deterministic and reproducible. Exposed as a parameter; in production it would be set to the accounting period close date by the scheduler.

### Compliance rules — prioritisation

Rules were prioritised by policy reference and immediate financial risk:

- **NO_PO (§2.1)** — maverick spend above EUR 1,000 is the highest-volume control failure in AP processing
- **BLOCKED_VENDOR / VENDOR_ON_HOLD (§4.1)** — payment to a non-authorised vendor is an immediate financial and compliance risk; INACTIVE and ON_HOLD receive distinct flags because policy §4.1 prescribes different remediation paths for each
- **PENDING_APPROVAL / OVERDUE_APPROVAL (§3.1)** — unapproved invoices are a control failure; overdue ones are escalation candidates
- **POTENTIAL_DUPLICATE / LOGICAL_DUPLICATE_CANDIDATE** — duplicate payment is a direct financial loss and a fraud indicator
- **CREDIT_NOTE (§6.1)** — negative amounts require matching to originating invoice; unmatched credits >90 days escalate to Finance Director
- **APPROVAL_LEVEL_VIOLATION (§3)** — added as `indicative_only=true` pending HR/IAM integration. Inferring roles from observed approval behavior would be circular: someone who approved a €60k invoice may have done so incorrectly — mapping them as Finance Director would mask the violation rather than surface it.

### SQL agent — data access controls

The NL-to-SQL agent enforces a two-layer defence against unauthorised data access:

**Layer 1 — system prompt whitelist (`sql_agent/query_builder.py`)**

The prompt Claude receives explicitly names the three business tables analysts are permitted to query:

```
Allowed tables (query ONLY these):
  invoices_enriched, compliance_flags, vendors

Do not query pipeline_log, audit_log, or invoices_clean directly.
```

`pipeline_log` and `audit_log` are internal pipeline metadata — they contain run IDs, transformation history, and raw source values before cleaning. Exposing them to finance analysts would surface implementation details and could reveal intermediate data states that have no meaning in a business context. `invoices_clean` is the raw cleaned table; `invoices_enriched` is the correct analyst surface because it joins vendor master data and is what the compliance rules were validated against.

**Layer 2 — DML keyword blocklist + `read_only=True`**

`_is_safe_select()` in `query_builder.py` validates every generated query before execution: it must start with `SELECT` and must not contain `INSERT`, `UPDATE`, `DELETE`, `DROP`, `CREATE`, `ALTER`, `TRUNCATE`, `REPLACE`, `MERGE`, or `UPSERT`. The DuckDB connection is opened with `read_only=True`, so even if both prompt and code guards were bypassed, the database engine itself would refuse any write operation.

**Defence-in-depth summary:**

| Guard | What it blocks | Where |
|---|---|---|
| Prompt whitelist | LLM querying internal tables | `SQL_SYSTEM_PROMPT` |
| DML keyword blocklist | Write/DDL statements | `_is_safe_select()` |
| `read_only=True` | Any write at DB engine level | `duckdb.connect()` |

The prompt guard is the first line — it handles the 99% case cleanly. The code and DB guards are independent backstops that require no trust in the LLM output.

**Known limitation: `invoice_date` stored as VARCHAR**

`invoice_date` is stored as a `VARCHAR` in the current pipeline. The SQL agent occasionally generates `EXTRACT(YEAR FROM invoice_date)` which fails at runtime on a string column. Mitigation: the SQL system prompt explicitly instructs the LLM to use `invoice_date LIKE '2025%'` or `CAST(invoice_date AS DATE)` instead. Production fix: store as `DATE` type by casting in `_write_to_db` during pipeline ingestion — one line: `inv_df["invoice_date"] = pd.to_datetime(inv_df["invoice_date"]).dt.date`.

---

### What I deliberately did NOT build

- **Authentication / authorisation** — out of scope for prototype; production path is Azure AD SSO with role-based access
- **Real-time pipeline** — batch processing is appropriate for AP invoice workflows; streaming would add infrastructure complexity with no business benefit at this scale
- **Production vector DB** — 15 policy chunks do not warrant Pinecone or ChromaDB; the right trigger is >1,000 chunks or multi-document retrieval
- **CI/CD** — noted as production requirement; would gate on pipeline eval metrics (see §4)
- **Pixel-perfect UI** — functional beats beautiful for an internal finance prototype

---

## 4. Evaluation Strategy

### Pipeline evaluation

```
Data quality
  Completeness    : % rows with each field populated after cleaning
  Fix rate        : % of each issue class successfully resolved
  Quarantine rate : target < 5% on clean production data

Compliance flag accuracy
  Seed known violations into a test dataset
  Measure precision / recall per flag type
  APPROVAL_LEVEL_VIOLATION excluded until IAM integration
```

### Assistant evaluation

**Offline (golden dataset — 10 Q&A pairs, run as CI gate on every deployment):**

| Metric | Method |
|--------|--------|
| Retrieval Hit@3 | Does top-3 chunks include the relevant policy section? |
| Faithfulness | Does the answer contradict retrieved context? (LLM-as-judge) |
| SQL accuracy | Does generated SQL return correct rows on known data? |
| Answer relevance | Is the answer responsive to the question asked? |

**Online (production, sampled):**

| Metric | Signal |
|--------|--------|
| Answer relevance | Claude-as-judge on live queries |
| SQL error rate | Syntax + runtime errors / total SQL queries |
| Policy citation accuracy | % answers citing a policy section |
| User feedback | Thumbs up/down per answer |
| Cost per query | Token tracking (target ~$0.002 at current volume) |

> This mirrors the AgentOps framework being built at AZ for the ARI platform — offline eval as a CI gate on every deployment, online eval as scheduled monitoring in production.

---

## 5. Path to Production

### Security & access control
- Azure AD SSO — role-based access (analyst / controller / auditor)
- Field-level encryption for PII in `approved_by`
- API rate limiting per user

### Data & scale
- Replace CSV ingestion with real AP connector (SAP / Oracle)
- DuckDB → Snowflake or Databricks for 200+ concurrent users
- Real-time streaming for time-sensitive compliance alerts

### Auditability & SOX §8.2
- `pipeline_log` and `audit_log` tables already implement full transformation lineage
- All LLM calls logged (prompt + response + timestamp) for 10-year retention per §8.1
- Immutable audit trail via append-only log table

### Monitoring
- CloudWatch for pipeline health (run duration, quarantine rate, flag counts)
- LangSmith for LLM call tracing (latency, cost, error rate)
- Alert on SQL error rate spike — potential indicator of schema drift

### Cost model
- ~$0.002 / query at current volume (Haiku for embeddings, Sonnet for generation)
- Policy embeddings cached at startup — static document, embed once, reuse
- Acceptable for an internal finance tool at ~50 queries/day

---

## 6. Time spent & what I'd do next

| Component | Time |
|-----------|------|
| Pipeline | Xh |
| Backend | Xh |
| Frontend | Xh |
| README | Xh |
| **Total** | **Xh** |

**What I'd do next with more time:**

1. Implement real approval-level validation with IAM/Azure AD integration — replacing `approver_roles.json` with a live lookup
2. Add eval harness with golden dataset (10 Q&A pairs) as a CI gate before any RAG change
3. Guardrails — refuse or escalate when a question requires data unavailable in the current dataset
4. Incremental pipeline — idempotent ingestion of late-arriving invoices without full reprocessing
5. Production auth — Azure AD SSO with auditor / controller / analyst roles

---

## 7. Data quality report

The pipeline generates a full DQ and compliance report on every run:

- [`data/quality_report.md`](data/quality_report.md) — human-readable summary
- [`data/quality_report.json`](data/quality_report.json) — machine-readable, suitable for CI assertions

The report is derived entirely from `audit_log` and `compliance_flags` in DuckDB — demonstrating the SOX §8.2 lineage from source CSV to reported output.

---

## Project structure

```
.
├── data/
│   ├── invoices_2025.csv          # Source invoice data
│   ├── vendor_master.csv          # Vendor master
│   ├── procurement_policy.md      # FIN-POL-014 (RAG source)
│   ├── approver_roles.json        # Mock IAM config (production: Azure AD)
│   ├── spend_intelligence.duckdb  # Generated by pipeline
│   ├── quality_report.md          # Generated by pipeline
│   └── quality_report.json        # Generated by pipeline
├── pipeline/
│   ├── models.py                  # Pydantic domain models
│   ├── cleaners.py                # Pure DQ fix functions (one per issue)
│   ├── rules.py                   # Compliance rules (one class per flag)
│   ├── pipeline.py                # ETL orchestrator
│   └── quality_report.py         # Report generator (queries DuckDB)
├── backend/
│   ├── main.py                    # FastAPI app
│   ├── routes/ask.py              # POST /ask — RAG + SQL agent
│   ├── routes/quality.py         # GET /quality-report
│   ├── rag/                       # Chunker, embedder, retriever
│   └── sql_agent/                 # NL → SQL over DuckDB
├── frontend/
│   └── src/
│       ├── App.jsx
│       ├── components/Chat.jsx
│       ├── components/QualityDashboard.jsx
│       └── components/EvidencePanel.jsx
├── notebook/
│   └── 01_exploratory_analysis_quality.ipynb
├── main.py                        # Entry point: pipeline + report
└── pyproject.toml
```

"""
API-level tests for the three FastAPI endpoints.
Uses TestClient with dependency overrides and mocks to avoid real LLM dependencies.
Run with: pytest tests/test_api.py -v
"""
from __future__ import annotations
from unittest.mock import MagicMock, patch

import duckdb
import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.dependencies import get_db


@pytest.fixture(scope="module")
def test_db() -> duckdb.DuckDBPyConnection:
    """Minimal in-memory DuckDB with the schema required by all three endpoints."""
    conn = duckdb.connect(":memory:")
    conn.execute("""
        CREATE TABLE pipeline_log (
            run_id VARCHAR, run_timestamp VARCHAR, total_rows INT,
            clean_rows INT, quarantined_rows INT, audit_entries INT, reference_date VARCHAR
        )
    """)
    conn.execute(
        "INSERT INTO pipeline_log VALUES ('run-001', '2025-06-15T10:00:00', 10, 9, 1, 3, '2025-12-26')"
    )
    conn.execute("""
        CREATE TABLE vendors (
            vendor_id VARCHAR, vendor_name VARCHAR,
            country VARCHAR, category VARCHAR, status VARCHAR
        )
    """)
    conn.execute("INSERT INTO vendors VALUES ('V-1001', 'Acme Corp', 'GB', 'IT', 'ACTIVE')")
    conn.execute("""
        CREATE TABLE invoices_clean (
            row_id INT, invoice_number VARCHAR, invoice_date VARCHAR,
            vendor_id VARCHAR, vendor_name_raw VARCHAR, vendor_name_normalized VARCHAR,
            description VARCHAR, amount DOUBLE, currency VARCHAR, cost_center VARCHAR,
            po_number VARCHAR, approved_by VARCHAR, is_credit_note BOOL, quarantined BOOL
        )
    """)
    conn.execute("""
        INSERT INTO invoices_clean VALUES
            (1, 'INV-001', '2025-06-01', 'V-1001', 'Acme Corp', 'Acme Corp',
             'Software', 5000.0, 'EUR', 'CC-01', NULL, NULL, FALSE, FALSE),
            (2, 'INV-002', '2025-06-10', 'V-1001', 'Acme Corp', 'Acme Corp',
             'Consulting', 800.0, 'GBP', 'CC-02', 'PO-001', 'j.smith', FALSE, FALSE)
    """)
    conn.execute("""
        CREATE VIEW invoices_enriched AS
        SELECT ic.*, v.status AS vendor_status
        FROM invoices_clean ic LEFT JOIN vendors v ON ic.vendor_id = v.vendor_id
    """)
    conn.execute("""
        CREATE TABLE compliance_flags (
            row_id INT, flag_type VARCHAR, detail VARCHAR,
            policy_ref VARCHAR, indicative_only BOOL
        )
    """)
    conn.execute(
        "INSERT INTO compliance_flags VALUES (1, 'NO_PO', 'Missing PO above threshold', '§2.1', FALSE)"
    )
    return conn


@pytest.fixture(scope="module")
def client(test_db: duckdb.DuckDBPyConnection):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: test_db
    with TestClient(app) as c:
        yield c


# ── /quality-report ──────────────────────────────────────────────────────────

def test_quality_report_returns_200(client: TestClient):
    assert client.get("/quality-report").status_code == 200


def test_quality_report_shape(client: TestClient):
    data = client.get("/quality-report").json()
    assert data["total_invoices"] == 10
    assert data["quarantined_rows"] == 1
    assert "NO_PO" in data["flags"]
    assert data["flags"]["NO_PO"]["count"] == 1
    assert data["flags"]["NO_PO"]["indicative_only"] is False


# ── /invoices/flagged ─────────────────────────────────────────────────────────

def test_flagged_invoices_returns_200(client: TestClient):
    r = client.get("/invoices/flagged")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_flagged_invoices_contains_expected_row(client: TestClient):
    rows = client.get("/invoices/flagged").json()
    assert len(rows) == 1
    assert rows[0]["invoice_number"] == "INV-001"
    assert "NO_PO" in rows[0]["flags"]


def test_flagged_invoices_filter_by_flag_type(client: TestClient):
    rows = client.get("/invoices/flagged", params={"flag_type": "NO_PO"}).json()
    assert all("NO_PO" in r["flags"] for r in rows)


def test_flagged_invoices_unknown_flag_returns_empty(client: TestClient):
    rows = client.get("/invoices/flagged", params={"flag_type": "NONEXISTENT"}).json()
    assert rows == []


def test_flagged_invoices_pagination(client: TestClient):
    rows_all = client.get("/invoices/flagged").json()
    rows_page = client.get("/invoices/flagged", params={"limit": 1, "offset": 0}).json()
    assert len(rows_page) <= 1
    if rows_all:
        assert rows_page[0] == rows_all[0]


# ── /ask ──────────────────────────────────────────────────────────────────────

def test_ask_empty_question_returns_422(client: TestClient):
    r = client.post("/ask", json={"question": ""})
    assert r.status_code == 422


def _mock_llm_response(*_, **__) -> MagicMock:
    mock = MagicMock()
    mock.choices[0].message.content = "Stub answer from mock LLM."
    return mock


def test_ask_returns_expected_shape(client: TestClient):
    with (
        patch(
            "backend.routes.ask.generate_sql",
            return_value="SELECT invoice_number, amount FROM invoices_enriched LIMIT 5",
        ),
        patch("backend.routes.ask.litellm.completion", side_effect=_mock_llm_response),
    ):
        r = client.post("/ask", json={"question": "Which invoices are missing a PO?"})
    assert r.status_code == 200
    data = r.json()
    assert set(data) >= {"answer", "evidence", "confidence", "disclaimer"}
    assert data["evidence"]["source"] in {"hybrid", "data_only", "policy_only"}
    assert data["confidence"] in {"high", "medium", "low"}


def test_ask_llm_failure_returns_503(client: TestClient):
    with (
        patch("backend.routes.ask.generate_sql", return_value=None),
        patch(
            "backend.routes.ask.litellm.completion",
            side_effect=RuntimeError("LLM unreachable"),
        ),
    ):
        r = client.post("/ask", json={"question": "What is the approval threshold?"})
    assert r.status_code == 503

"""
Unit tests for deterministic pipeline components.
Run with: pytest tests/test_pipeline.py -v
"""
from datetime import date

import pytest

from pipeline.cleaners import (
    normalize_vendor_id,
    normalize_vendor_name,
    parse_amount,
    parse_date,
    resolve_vendor_id,
)
from pipeline.models import CleanInvoice, FlagType, VendorRecord, VendorStatus
from pipeline.rules import (
    CreditNoteRule,
    LogicalDuplicateRule,
    NoPORule,
    OverdueApprovalRule,
)
from backend.sql_agent.query_builder import _is_safe_select


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _invoice(**kwargs) -> CleanInvoice:
    defaults = dict(
        row_id=1,
        invoice_number="INV-TEST-001",
        invoice_date=date(2025, 6, 15),
        date_ambiguous=False,
        vendor_id="V-1001",
        vendor_name_raw="Test Vendor",
        vendor_name_normalized="Test Vendor",
        description="Test",
        amount=1_500.00,
        currency="EUR",
        cost_center="CC-01",
        po_number=None,
        approved_by="j.smith",
        is_credit_note=False,
        quarantined=False,
    )
    defaults.update(kwargs)
    return CleanInvoice(**defaults)


REF_DATE = date(2025, 12, 26)
EMPTY_VENDOR_MAP: dict = {}
EMPTY_APPROVER_ROLES: dict = {}


# ---------------------------------------------------------------------------
# 1. Credit notes must NOT be flagged as NO_PO
# ---------------------------------------------------------------------------

def test_credit_note_not_flagged_no_po():
    invoice = _invoice(amount=-500.00, po_number=None, is_credit_note=True)
    flags = NoPORule().check(invoice, EMPTY_VENDOR_MAP, EMPTY_APPROVER_ROLES, [], REF_DATE)
    assert flags == [], "Credit notes (negative amounts) must not produce NO_PO flags"


# ---------------------------------------------------------------------------
# 2. Non-EUR invoices above 1000 nominal are flagged as indicative (not confirmed)
# ---------------------------------------------------------------------------

def test_no_po_non_eur_is_indicative():
    invoice = _invoice(amount=1_200.00, currency="GBP", po_number=None)
    flags = NoPORule().check(invoice, EMPTY_VENDOR_MAP, EMPTY_APPROVER_ROLES, [], REF_DATE)
    assert len(flags) == 1
    assert flags[0].flag_type == FlagType.NO_PO
    assert "FX conversion" in flags[0].detail, "Non-EUR NO_PO flag must note FX conversion requirement"


# ---------------------------------------------------------------------------
# 3. Logical duplicates across different currencies are NOT flagged
# ---------------------------------------------------------------------------

def test_logical_duplicate_different_currencies_not_flagged():
    inv_eur = _invoice(row_id=1, invoice_number="INV-A", amount=1_000.00, currency="EUR",
                       invoice_date=date(2025, 6, 15))
    inv_gbp = _invoice(row_id=2, invoice_number="INV-B", amount=1_000.00, currency="GBP",
                       invoice_date=date(2025, 6, 16))
    flags = LogicalDuplicateRule().check(inv_eur, EMPTY_VENDOR_MAP, EMPTY_APPROVER_ROLES,
                                         [inv_eur, inv_gbp], REF_DATE)
    assert flags == [], "Same amount in different currencies must not be flagged as logical duplicate"


# ---------------------------------------------------------------------------
# 4. Logical duplicates in same currency ARE flagged
# ---------------------------------------------------------------------------

def test_logical_duplicate_same_currency_flagged():
    inv1 = _invoice(row_id=1, invoice_number="INV-A", amount=1_000.00, currency="EUR",
                    invoice_date=date(2025, 6, 15))
    inv2 = _invoice(row_id=2, invoice_number="INV-B", amount=1_000.00, currency="EUR",
                    invoice_date=date(2025, 6, 16))
    flags = LogicalDuplicateRule().check(inv1, EMPTY_VENDOR_MAP, EMPTY_APPROVER_ROLES,
                                          [inv1, inv2], REF_DATE)
    assert len(flags) == 1
    assert flags[0].flag_type == FlagType.LOGICAL_DUPLICATE_CANDIDATE


# ---------------------------------------------------------------------------
# 5. OVERDUE_APPROVAL is indicative_only (business heuristic, not policy rule)
# ---------------------------------------------------------------------------

def test_overdue_approval_is_indicative_only():
    invoice = _invoice(approved_by=None, invoice_date=date(2025, 1, 1))
    flags = OverdueApprovalRule().check(invoice, EMPTY_VENDOR_MAP, EMPTY_APPROVER_ROLES, [], REF_DATE)
    assert len(flags) == 1
    assert flags[0].indicative_only is True, "OVERDUE_APPROVAL must be indicative_only — threshold not in policy"
    assert "§3.1" not in flags[0].detail, "OVERDUE_APPROVAL must not cite §3.1 — 30-day rule is not policy-derived"


# ---------------------------------------------------------------------------
# 6. Vendor name normalisation preserves commercial casing
# ---------------------------------------------------------------------------

def test_vendor_name_preserves_casing():
    raw = "BioReagent Direct"
    normalized, audit_entry = normalize_vendor_name(raw, row_id=1, invoice_number="INV-001")
    assert normalized == "BioReagent Direct", ".title() must not be applied to vendor names"
    assert audit_entry is None, "No audit entry expected when name already normalised"


def test_vendor_name_collapses_whitespace():
    raw = "BioReagent   Direct"
    normalized, audit_entry = normalize_vendor_name(raw, row_id=1, invoice_number="INV-001")
    assert normalized == "BioReagent Direct"
    assert audit_entry is not None


# ---------------------------------------------------------------------------
# 7. SQL safety: DML keywords are blocked
# ---------------------------------------------------------------------------

def test_sql_rejects_dml_keywords():
    assert _is_safe_select("DROP TABLE invoices_enriched") is False
    assert _is_safe_select("INSERT INTO invoices_enriched VALUES (1)") is False
    assert _is_safe_select("UPDATE invoices_enriched SET amount=0") is False
    assert _is_safe_select("DELETE FROM invoices_enriched") is False


def test_sql_accepts_valid_select():
    assert _is_safe_select("SELECT * FROM invoices_enriched WHERE amount > 1000") is True


# ---------------------------------------------------------------------------
# 8. Vendor ID normalisation: missing dash is inserted
# ---------------------------------------------------------------------------

def test_normalize_vendor_id_inserts_dash():
    fixed, entry = normalize_vendor_id("V1002", row_id=1, invoice_number="INV-001")
    assert fixed == "V-1002"
    assert entry is not None

def test_normalize_vendor_id_idempotent():
    fixed, entry = normalize_vendor_id("V-1002", row_id=1, invoice_number="INV-001")
    assert fixed == "V-1002"
    assert entry is None, "No audit entry when vendor_id already normalised"

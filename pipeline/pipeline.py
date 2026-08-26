"""
Main ETL pipeline:
  1. Load vendor master + approver role config
  2. Read raw CSV → clean each row (DQ fixes + immutable audit log)
  3. Run compliance rules over all cleaned invoices
  4. Write to DuckDB — idempotent (drop-and-recreate data tables each run)
  5. Append a pipeline_log entry (run history preserved for SOX §8.2)
"""
from __future__ import annotations
import json
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

from pipeline.cleaners import (
    normalize_currency,
    normalize_vendor_id,
    normalize_vendor_name,
    parse_amount,
    parse_date,
    resolve_vendor_id,
)
from pipeline.models import (
    AuditEntry,
    CleanInvoice,
    ComplianceFlag,
    PipelineResult,
    RawInvoice,
    VendorRecord,
)
from pipeline.rules import ALL_RULES


def _load_vendors(path: str) -> list[VendorRecord]:
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    return [VendorRecord(**row) for row in df.to_dict(orient="records")]


def _load_approver_roles(path: str) -> dict[str, str]:
    data = json.loads(Path(path).read_text())
    return data["roles"]


def _clean_row(
    raw: RawInvoice,
    row_id: int,
    vendors: list[VendorRecord],
    known_vendor_ids: set[str],
) -> tuple[CleanInvoice, list[AuditEntry], str | None]:
    """
    Returns (clean_invoice, audit_entries, quarantine_reason | None).
    On unrecoverable error, quarantine_reason is set and the row is marked quarantined.
    Data is NEVER dropped — quarantined rows persist in invoices_clean with quarantined=True.
    """
    entries: list[AuditEntry] = []
    quarantine_reason: str | None = None

    # Date
    try:
        invoice_date, date_ambiguous, entry = parse_date(raw.invoice_date, row_id, raw.invoice_number)
        if entry:
            entries.append(entry)
    except ValueError as exc:
        quarantine_reason = f"Unparseable date '{raw.invoice_date}': {exc}"
        invoice_date, date_ambiguous = date(2000, 1, 1), False

    # Amount
    try:
        amount, entry = parse_amount(raw.amount, row_id, raw.invoice_number)
        if entry:
            entries.append(entry)
    except ValueError as exc:
        quarantine_reason = quarantine_reason or f"Unparseable amount '{raw.amount}': {exc}"
        amount = 0.0

    is_credit_note = amount < 0

    # Currency
    currency, entry = normalize_currency(raw.currency, row_id, raw.invoice_number)
    if entry:
        entries.append(entry)

    # Vendor ID — missing → fuzzy match; malformed → add dash
    raw_vid = raw.vendor_id.strip()
    if not raw_vid:
        vendor_id, entry = resolve_vendor_id(raw.vendor_name, vendors, row_id, raw.invoice_number)
        if entry:
            entries.append(entry)
        if not vendor_id:
            quarantine_reason = quarantine_reason or (
                f"Missing vendor_id, no fuzzy match found for name '{raw.vendor_name}'"
            )
            vendor_id = ""
    else:
        vendor_id, entry = normalize_vendor_id(raw_vid, row_id, raw.invoice_number)
        if entry:
            entries.append(entry)

    # Vendor name
    vendor_name_normalized, entry = normalize_vendor_name(raw.vendor_name, row_id, raw.invoice_number)
    if entry:
        entries.append(entry)

    # Post-normalization join validation: vendor_id must exist in the master.
    # A malformed ID that normalizes to an unknown value (e.g. a typo like V-9999)
    # would pass all regex fixes but still fail the join — quarantine explicitly
    # rather than silently producing a compliance blind spot.
    if vendor_id and vendor_id not in known_vendor_ids:
        quarantine_reason = quarantine_reason or (
            f"vendor_id '{vendor_id}' not found in vendor master after normalization"
        )

    clean = CleanInvoice(
        row_id=row_id,
        invoice_number=raw.invoice_number.strip(),
        invoice_date=invoice_date,
        date_ambiguous=date_ambiguous,
        vendor_id=vendor_id,
        vendor_name_raw=raw.vendor_name,
        vendor_name_normalized=vendor_name_normalized,
        description=raw.description.strip(),
        amount=amount,
        currency=currency,
        cost_center=raw.cost_center.strip(),
        po_number=raw.po_number.strip() or None,
        approved_by=raw.approved_by.strip() or None,
        is_credit_note=is_credit_note,
        quarantined=bool(quarantine_reason),
        quarantine_reason=quarantine_reason,
    )
    return clean, entries, quarantine_reason


def _write_to_db(
    conn: duckdb.DuckDBPyConnection,
    clean_invoices: list[CleanInvoice],
    vendors: list[VendorRecord],
    audit_entries: list[AuditEntry],
    flags: list[ComplianceFlag],
    run_id: str,
    run_timestamp: str,
    reference_date: date,
    source_file: str,
) -> None:
    """
    Idempotent write: data tables are dropped and recreated on each run.
    pipeline_log is append-only to preserve the full run history.
    DuckDB's Python integration allows referencing local DataFrames directly in SQL.
    """
    inv_df = pd.DataFrame([i.model_dump() for i in clean_invoices])
    inv_df["invoice_date"] = inv_df["invoice_date"].astype(str)

    ven_df = pd.DataFrame([v.model_dump() for v in vendors])

    _EMPTY_AUDIT = {"row_id": [], "invoice_number": [], "field": [],
                    "original_value": [], "new_value": [], "action": [], "run_timestamp": [], "run_id": []}
    aud_df = pd.DataFrame([a.model_dump() for a in audit_entries]) if audit_entries else pd.DataFrame(_EMPTY_AUDIT)
    aud_df["run_id"] = run_id

    _EMPTY_FLAGS = {"row_id": [], "invoice_number": [], "flag_type": [], "detail": [], "indicative_only": []}
    flags_df = pd.DataFrame([f.model_dump() for f in flags]) if flags else pd.DataFrame(_EMPTY_FLAGS)
    if not flags_df.empty:
        flags_df["flag_type"] = flags_df["flag_type"].astype(str)

    for table, df in [
        ("invoices_clean", inv_df),
        ("vendors", ven_df),
        ("compliance_flags", flags_df),
    ]:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute(f"CREATE TABLE {table} AS SELECT * FROM df")

    # audit_log is append-only — never dropped between runs so the full
    # transformation history is preserved for SOX §8.2 lineage.
    tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
    if "audit_log" in tables:
        # Migrate: add run_id column if it doesn't exist yet (schema evolution)
        cols = {row[0] for row in conn.execute("DESCRIBE audit_log").fetchall()}
        if "run_id" not in cols:
            conn.execute("ALTER TABLE audit_log ADD COLUMN run_id VARCHAR")
        conn.execute("INSERT INTO audit_log SELECT * FROM aud_df")
    else:
        conn.execute("CREATE TABLE audit_log AS SELECT * FROM aud_df")

    # Enriched view: invoices joined to vendor master.
    # LEFT JOIN deliberately — quarantined rows (vendor_id='') remain visible
    # with NULL vendor columns rather than disappearing from the view.
    conn.execute("DROP VIEW IF EXISTS invoices_enriched")
    conn.execute("""
        CREATE VIEW invoices_enriched AS
        SELECT
            ic.*,
            v.vendor_name   AS vendor_master_name,
            v.country       AS vendor_country,
            v.category      AS vendor_category,
            v.status        AS vendor_status
        FROM invoices_clean ic
        LEFT JOIN vendors v ON ic.vendor_id = v.vendor_id
    """)

    log_df = pd.DataFrame([{
        "run_id": run_id,
        "run_timestamp": run_timestamp,
        "source_file": source_file,
        "total_rows": len(clean_invoices),
        "clean_rows": sum(1 for i in clean_invoices if not i.quarantined),
        "quarantined_rows": sum(1 for i in clean_invoices if i.quarantined),
        "flags_raised": len(flags),
        "audit_entries": len(audit_entries),
        "reference_date": str(reference_date),
    }])

    tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
    if "pipeline_log" in tables:
        conn.execute("INSERT INTO pipeline_log SELECT * FROM log_df")
    else:
        conn.execute("CREATE TABLE pipeline_log AS SELECT * FROM log_df")


def run_pipeline(
    invoices_path: str = "data/invoices_2025.csv",
    vendor_master_path: str = "data/vendor_master.csv",
    approver_roles_path: str = "data/approver_roles.json",
    db_path: str = "data/spend_intelligence.duckdb",
    reference_date: date | None = None,
) -> PipelineResult:
    """
    End-to-end pipeline: CSV → clean → flag → DuckDB.
    reference_date defaults to max(invoice_date) in the dataset for deterministic
    OVERDUE_APPROVAL checks (SOX §8.2 — same inputs must produce same outputs).
    """
    run_id = str(uuid.uuid4())
    run_timestamp = datetime.now(timezone.utc).isoformat()

    vendors = _load_vendors(vendor_master_path)
    approver_roles = _load_approver_roles(approver_roles_path)
    vendor_map = {v.vendor_id: v for v in vendors}
    known_vendor_ids = set(vendor_map.keys())

    raw_df = pd.read_csv(invoices_path, dtype=str, keep_default_na=False)

    clean_invoices: list[CleanInvoice] = []
    audit_entries: list[AuditEntry] = []

    for row_id, row in enumerate(raw_df.itertuples(index=False), start=1):
        raw = RawInvoice(
            invoice_number=row.invoice_number,
            invoice_date=row.invoice_date,
            vendor_id=row.vendor_id,
            vendor_name=row.vendor_name,
            description=row.description,
            amount=row.amount,
            currency=row.currency,
            cost_center=row.cost_center,
            po_number=row.po_number,
            approved_by=row.approved_by,
        )
        clean, entries, _ = _clean_row(raw, row_id, vendors, known_vendor_ids)
        clean_invoices.append(clean)
        audit_entries.extend(entries)

    if reference_date is None:
        reference_date = max(
            inv.invoice_date for inv in clean_invoices if not inv.quarantined
        )

    flags: list[ComplianceFlag] = []
    for invoice in clean_invoices:
        for rule in ALL_RULES:
            flags.extend(rule.check(invoice, vendor_map, approver_roles, clean_invoices, reference_date))

    with duckdb.connect(db_path) as conn:
        conn.begin()
        try:
            _write_to_db(
                conn, clean_invoices, vendors, audit_entries, flags,
                run_id, run_timestamp, reference_date, invoices_path,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return PipelineResult(
        total_rows=len(clean_invoices),
        clean_rows=sum(1 for i in clean_invoices if not i.quarantined),
        quarantined_rows=sum(1 for i in clean_invoices if i.quarantined),
        audit_entries=len(audit_entries),
        flags_raised=len(flags),
        reference_date=reference_date,
        db_path=db_path,
    )


if __name__ == "__main__":
    result = run_pipeline()
    print(f"Pipeline complete: {result.total_rows} rows ingested")
    print(f"  Clean: {result.clean_rows} | Quarantined: {result.quarantined_rows}")
    print(f"  Compliance flags: {result.flags_raised}")
    print(f"  Audit entries: {result.audit_entries}")
    print(f"  Reference date: {result.reference_date}")
    print(f"  DB: {result.db_path}")

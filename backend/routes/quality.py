from __future__ import annotations
import duckdb
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.dependencies import get_db
from backend.exceptions import NotFoundError

router = APIRouter()


class FlagSummary(BaseModel):
    count: int
    amounts_by_currency: dict[str, float]
    indicative_only: bool


class QualityReport(BaseModel):
    run_timestamp: str
    run_id: str
    total_invoices: int
    clean_rows: int
    quarantined_rows: int
    audit_entries: int
    flags: dict[str, FlagSummary]
    reference_date: str
    top_flagged_invoices: list[dict]


@router.get("/quality-report", response_model=QualityReport)
def get_quality_report(db: duckdb.DuckDBPyConnection = Depends(get_db)):
    run = db.execute(
        "SELECT run_id, run_timestamp, total_rows, clean_rows, quarantined_rows, "
        "audit_entries, reference_date "
        "FROM pipeline_log ORDER BY run_timestamp DESC LIMIT 1"
    ).fetchone()

    if run is None:
        raise NotFoundError("No pipeline runs found in DB")

    run_id, run_timestamp, total_invoices, clean_rows, quarantined_rows, audit_entries, reference_date = run

    # Flag summary grouped by flag_type AND currency — never sum across currencies
    flags_rows = db.execute("""
        SELECT
            cf.flag_type,
            ic.currency,
            COUNT(*)                                AS count,
            SUM(ic.amount)                          AS currency_amount,
            MAX(cf.indicative_only::INT)::BOOL      AS indicative_only
        FROM compliance_flags cf
        JOIN invoices_clean ic ON cf.row_id = ic.row_id
        GROUP BY cf.flag_type, ic.currency
        ORDER BY cf.flag_type, ic.currency
    """).fetchall()

    flag_acc: dict[str, dict] = {}
    for flag_type, currency, count, currency_amount, indicative_only in flags_rows:
        if flag_type not in flag_acc:
            flag_acc[flag_type] = {"count": 0, "amounts_by_currency": {}, "indicative_only": False}
        flag_acc[flag_type]["count"] += count
        flag_acc[flag_type]["amounts_by_currency"][currency] = round(currency_amount, 2)
        flag_acc[flag_type]["indicative_only"] = indicative_only or flag_acc[flag_type]["indicative_only"]

    flags: dict[str, FlagSummary] = {
        flag_type: FlagSummary(**data)
        for flag_type, data in flag_acc.items()
    }

    # Top 10 flagged invoices by EUR amount (all quarantine states for completeness)
    top_rows = db.execute("""
        SELECT
            ie.invoice_number,
            CAST(ie.invoice_date AS VARCHAR)    AS invoice_date,
            ie.vendor_name_normalized,
            ie.amount,
            ie.currency,
            ARRAY_TO_STRING(LIST_DISTINCT(LIST(cf.flag_type)), ', ') AS flags,
            ie.quarantined
        FROM invoices_enriched ie
        JOIN compliance_flags cf ON ie.row_id = cf.row_id
        GROUP BY
            ie.invoice_number, ie.invoice_date, ie.vendor_name_normalized,
            ie.amount, ie.currency, ie.quarantined
        ORDER BY ie.amount DESC
        LIMIT 10
    """).fetchall()

    top_flagged_invoices = [
        {
            "invoice_number": r[0],
            "invoice_date": r[1],
            "vendor_name": r[2],
            "amount": r[3],
            "currency": r[4],
            "flags": r[5],
            "quarantined": r[6],
        }
        for r in top_rows
    ]

    return QualityReport(
        run_id=run_id,
        run_timestamp=run_timestamp,
        total_invoices=total_invoices,
        clean_rows=clean_rows,
        quarantined_rows=quarantined_rows,
        audit_entries=audit_entries,
        flags=flags,
        reference_date=str(reference_date),
        top_flagged_invoices=top_flagged_invoices,
    )

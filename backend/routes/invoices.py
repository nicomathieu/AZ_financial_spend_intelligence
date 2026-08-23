from __future__ import annotations
from typing import Optional

import duckdb
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from backend.dependencies import get_db

router = APIRouter()


class FlaggedInvoice(BaseModel):
    invoice_number: str
    invoice_date: str
    vendor_name: Optional[str]
    amount: float
    currency: str
    flags: list[str]
    quarantined: bool


@router.get("/invoices/flagged", response_model=list[FlaggedInvoice])
def get_flagged_invoices(
    flag_type: Optional[str] = Query(None, description="Filter by flag type, e.g. NO_PO"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    min_amount: Optional[float] = Query(None, ge=0, description="Minimum invoice amount (EUR)"),
    db: duckdb.DuckDBPyConnection = Depends(get_db),
):
    where_clauses: list[str] = []
    params: list = []

    if flag_type:
        where_clauses.append("cf.flag_type = ?")
        params.append(flag_type)

    if min_amount is not None:
        where_clauses.append("ie.amount >= ?")
        params.append(min_amount)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    # STRING_AGG then split in Python avoids ARRAY_AGG DISTINCT availability issues
    query = f"""
        SELECT
            ie.invoice_number,
            CAST(ie.invoice_date AS VARCHAR)            AS invoice_date,
            ie.vendor_name_normalized,
            ie.amount,
            ie.currency,
            LIST_DISTINCT(LIST(cf.flag_type))           AS flags,
            ie.quarantined
        FROM invoices_enriched ie
        JOIN compliance_flags cf ON ie.row_id = cf.row_id
        {where_sql}
        GROUP BY
            ie.invoice_number, ie.invoice_date, ie.vendor_name_normalized,
            ie.amount, ie.currency, ie.quarantined
        ORDER BY ie.amount DESC
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])

    rows = db.execute(query, params).fetchall()

    return [
        FlaggedInvoice(
            invoice_number=r[0],
            invoice_date=r[1],
            vendor_name=r[2],
            amount=r[3],
            currency=r[4],
            flags=r[5] or [],
            quarantined=r[6],
        )
        for r in rows
    ]

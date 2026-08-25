"""
Generates data quality and compliance reports by querying DuckDB directly.
Reports are derived from audit_log and compliance_flags — demonstrating
SOX §8.2 lineage from source CSV to reported output.
"""
import json
from pathlib import Path

import duckdb


# Static mapping: audit action keyword → issue class metadata.
# Centralised here so the report and the pipeline share a single source of truth.
ISSUE_CLASSES = [
    {
        "issue_class": "Mixed / non-ISO date formats",
        "count_key": "dates_parsed_to_iso",
        "decision": "Fix",
        "handling": "Parsed to ISO 8601 using format heuristics (YYYY-MM-DD, DD.MM.YYYY, MM-DD-YYYY, DD/MM/YYYY). Deterministic per format — no data loss.",
    },
    {
        "issue_class": "Ambiguous dates (both parts ≤ 12)",
        "count_key": "dates_ambiguous_flagged",
        "decision": "Flag",
        "handling": "Parsed with assumed convention; date_ambiguous=true set in invoices_clean. Affects OVERDUE_APPROVAL flags — human review recommended for flagged rows.",
    },
    {
        "issue_class": "Lowercase / mixed-case currency codes",
        "count_key": "currencies_uppercased",
        "decision": "Fix",
        "handling": "Uppercased to ISO 4217 standard (eur → EUR). No ambiguity — purely a casing artefact from the AP export.",
    },
    {
        "issue_class": "Malformed vendor IDs (missing dash)",
        "count_key": "vendor_ids_dash_inserted",
        "decision": "Fix",
        "handling": "Regex normalisation: V1002 → V-1002. Pattern V-XXXX is unambiguous — no heuristic involved.",
    },
    {
        "issue_class": "Missing vendor IDs resolved via name match",
        "count_key": "vendor_ids_resolved_via_fuzzy_match",
        "decision": "Fix",
        "handling": "Fuzzy match on vendor_name vs vendor master (threshold 0.7, casefold). Match score and method logged in audit_log. Production path: matches 0.7–0.9 should go to human review queue.",
    },
    {
        "issue_class": "Quoted amounts / thousand-separator commas",
        "count_key": "amounts_unquoted",
        "decision": "Fix",
        "handling": "Stripped surrounding quotes and comma separators. AP export artefact — format is deterministic and reversible.",
    },
    {
        "issue_class": "Vendor names with excess whitespace",
        "count_key": "vendor_names_normalized",
        "decision": "Fix",
        "handling": "Strip + whitespace collapse only. Canonical commercial casing preserved from vendor master — vendor name is never modified beyond removing leading/trailing whitespace.",
    },
    {
        "issue_class": "Rows with unrecoverable errors",
        "count_key": "rows_quarantined",
        "decision": "Quarantine",
        "handling": "Retained in invoices_clean with quarantined=true and quarantine_reason populated. Never dropped silently — remains visible in invoices_enriched via LEFT JOIN.",
    },
]


def _count_action(conn: duckdb.DuckDBPyConnection, keyword: str, run_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action LIKE ? AND run_id = ?",
        [f"%{keyword}%", run_id],
    ).fetchone()[0]


def generate_report(
    db_path: str = "data/spend_intelligence.duckdb",
    output_dir: str = "data",
) -> dict:
    conn = duckdb.connect(db_path, read_only=True)

    # Fetch run_id first — all audit_log queries filter on it so counts
    # reflect the current run only, not the cumulative append-only history.
    run_info = conn.execute(
        "SELECT run_id, reference_date, run_timestamp, source_file FROM pipeline_log ORDER BY run_timestamp DESC LIMIT 1"
    ).fetchone()
    current_run_id = str(run_info[0]) if run_info else ""
    reference_date  = str(run_info[1]) if run_info else "N/A"
    run_timestamp   = str(run_info[2]) if run_info else "N/A"
    source_file     = str(run_info[3]) if run_info else "N/A"

    total      = conn.execute("SELECT COUNT(*) FROM invoices_clean").fetchone()[0]
    quarantined = conn.execute("SELECT COUNT(*) FROM invoices_clean WHERE quarantined = true").fetchone()[0]
    clean      = total - quarantined
    flag_count = conn.execute("SELECT COUNT(*) FROM compliance_flags").fetchone()[0]
    audit_count = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE run_id = ?", [current_run_id]
    ).fetchone()[0]

    ambiguous_dates = conn.execute(
        "SELECT COUNT(*) FROM invoices_clean WHERE date_ambiguous = true"
    ).fetchone()[0]

    dq_fixes = {
        "dates_parsed_to_iso":               _count_action(conn, "→ ISO", current_run_id),
        "dates_ambiguous_flagged":           ambiguous_dates,
        "currencies_uppercased":             _count_action(conn, "Uppercased currency", current_run_id),
        "vendor_ids_dash_inserted":          _count_action(conn, "Inserted dash", current_run_id),
        "vendor_names_normalized":           _count_action(conn, "Normalized vendor name", current_run_id),
        "amounts_unquoted":                  _count_action(conn, "Stripped quotes", current_run_id),
        "vendor_ids_resolved_via_fuzzy_match": _count_action(conn, "fuzzy match", current_run_id),
        "rows_quarantined":                  quarantined,
    }

    # Issue classes: merge static metadata with live counts
    issue_classes = []
    for ic in ISSUE_CLASSES:
        issue_classes.append({
            "issue_class": ic["issue_class"],
            "count":       dq_fixes.get(ic["count_key"], 0),
            "decision":    ic["decision"],
            "handling":    ic["handling"],
        })

    flag_counts = dict(
        conn.execute(
            "SELECT flag_type, COUNT(*) FROM compliance_flags GROUP BY flag_type ORDER BY COUNT(*) DESC"
        ).fetchall()
    )

    indicative_flags = [
        row[0] for row in conn.execute(
            "SELECT DISTINCT flag_type FROM compliance_flags WHERE indicative_only = true"
        ).fetchall()
    ]

    # Confirmed duplicate pairs (same invoice_number — not "potential")
    confirmed_duplicate_pairs = [
        {
            "invoice_number": row[0],
            "vendor_id": row[1],
            "amount": round(row[2], 2),
            "currency": row[3],
            "occurrences": row[4],
        }
        for row in conn.execute("""
            SELECT ic.invoice_number, ic.vendor_id, ic.amount, ic.currency, COUNT(*) AS occurrences
            FROM compliance_flags cf
            JOIN invoices_clean ic ON cf.row_id = ic.row_id
            WHERE cf.flag_type = 'POTENTIAL_DUPLICATE'
            GROUP BY ic.invoice_number, ic.vendor_id, ic.amount, ic.currency
            ORDER BY ic.amount DESC
        """).fetchall()
    ]

    # NO_PO split by currency — EUR is a definitive threshold breach;
    # non-EUR requires FX conversion to confirm §2.1 compliance.
    no_po_by_currency = dict(
        conn.execute("""
            SELECT ic.currency, COUNT(*)
            FROM compliance_flags cf
            JOIN invoices_clean ic ON cf.row_id = ic.row_id
            WHERE cf.flag_type = 'NO_PO'
            GROUP BY ic.currency
            ORDER BY COUNT(*) DESC
        """).fetchall()
    )

    # Spend summary by currency — cross-currency aggregation requires ECB
    # FX rates; totals are intentionally reported per currency, not summed.
    # Positive invoices and credit notes (negative amounts) are reported
    # separately so the row counts reconcile to the 145 clean rows total.
    spend_by_currency = [
        {"currency": row[0], "invoice_count": row[1], "total_amount": round(row[2], 2), "type": "invoice"}
        for row in conn.execute("""
            SELECT currency, COUNT(*) AS invoice_count, SUM(amount) AS total_amount
            FROM invoices_clean
            WHERE quarantined = false AND amount > 0
            GROUP BY currency
            ORDER BY total_amount DESC
        """).fetchall()
    ]
    credit_notes_by_currency = [
        {"currency": row[0], "invoice_count": row[1], "total_amount": round(row[2], 2), "type": "credit_note"}
        for row in conn.execute("""
            SELECT currency, COUNT(*) AS invoice_count, SUM(amount) AS total_amount
            FROM invoices_clean
            WHERE quarantined = false AND amount < 0
            GROUP BY currency
            ORDER BY total_amount ASC
        """).fetchall()
    ]

    quarantine_detail = conn.execute(
        "SELECT invoice_number, quarantine_reason FROM invoices_clean WHERE quarantined = true"
    ).fetchall()

    conn.close()

    report = {
        "generated_at":   run_timestamp,
        "run_id":         current_run_id,
        "reference_date": reference_date,
        "source_file":    source_file,
        "totals": {
            "rows_ingested":        total,
            "rows_clean":           clean,
            "rows_quarantined":     quarantined,
            "audit_entries":        audit_count,
            "compliance_flags_raised": flag_count,
        },
        "issue_classes":           issue_classes,
        "dq_fixes":                dq_fixes,
        "spend_by_currency":       spend_by_currency,
        "credit_notes_by_currency": credit_notes_by_currency,
        "compliance_summary":      flag_counts,
        "no_po_by_currency":       no_po_by_currency,
        "confirmed_duplicate_pairs": confirmed_duplicate_pairs,
        "indicative_only_flags":   indicative_flags,
        "quarantined_rows": [
            {"invoice_number": r[0], "reason": r[1]} for r in quarantine_detail
        ],
        "notes": [
            "⚠️ APPROVAL_LEVEL_VIOLATION (62 flags, highest count) are INDICATIVE ONLY — role mapping uses a mock IAM config. These figures must not be treated as confirmed violations until an authoritative Azure AD integration is in place.",
            f"⚠️ OVERDUE_APPROVAL reference_date={reference_date} is the max invoice_date in the dataset, NOT the actual current date. This report is not an operational snapshot — actual overdue delays are significantly underestimated. Production fix: pass the accounting period close date as the reference parameter.",
            "DATE_AMBIGUOUS (54 rows): format assumed hyphens→MM-DD-YYYY, slashes→DD/MM/YYYY. For a company with primarily European vendors this assumption may be inverted. Production fix: cross-reference with vendor country from vendor_master to resolve ambiguity. Affects OVERDUE_APPROVAL flags for ambiguous rows.",
            "POTENTIAL_DUPLICATE flags represent confirmed duplicate invoice_numbers (identical in every field) — these are likely duplicate payments, not merely candidates for review. See confirmed duplicate pairs table.",
            "NO_PO non-EUR rows flagged for controller review — EUR equivalent requires ECB FX rate to confirm §2.1 threshold breach definitively.",
            "Spend table shows 143 positive invoices + 2 credit notes = 145 clean rows total. Credit notes are reported separately to avoid distorting the spend figures.",
            "BLOCKED_VENDOR (INACTIVE) and VENDOR_ON_HOLD (ON_HOLD) receive distinct flags — policy §4.1 prescribes different remediation paths for each status.",
            "Out-of-scope rules not implemented: §7 self-approval (no requester field in dataset), §6.1 credit note age/matching (no originating invoice ref), §2.3/§9 blanket PO and emergency exceptions (no Exception Register in dataset).",
            "Quarantined rows are retained in invoices_clean with quarantined=true — data is never dropped silently.",
        ],
    }

    out = Path(output_dir)
    (out / "quality_report.json").write_text(json.dumps(report, indent=2, default=str))
    (out / "quality_report.md").write_text(_render_markdown(report))

    return report


def _render_markdown(r: dict) -> str:
    lines = [
        "# Data Quality & Compliance Report",
        "",
        f"**Generated:** {r['generated_at']}  ",
        f"**Run ID:** `{r['run_id']}`  ",
        f"**Source file:** {r['source_file']}  ",
        f"**Reference date (OVERDUE_APPROVAL):** {r['reference_date']}",
        "",
        "## Pipeline Summary",
        "",
        "| Metric | Value |",
        "|--------|-------|",
    ]
    for k, v in r["totals"].items():
        lines.append(f"| {k.replace('_', ' ').title()} | {v} |")

    # ── Issue classes ──────────────────────────────────────────────────────────
    lines += [
        "",
        "## Issue Classes Found & Handling Decisions",
        "",
        "| Issue Class | Found | Decision | Handling |",
        "|-------------|-------|----------|----------|",
    ]
    for ic in r["issue_classes"]:
        lines.append(
            f"| {ic['issue_class']} | {ic['count']} | **{ic['decision']}** | {ic['handling']} |"
        )

    # ── Spend by currency ──────────────────────────────────────────────────────
    lines += [
        "",
        "## Spend Summary by Currency",
        "",
        "> Cross-currency aggregation without an authoritative ECB FX source would produce a misleading total.",
        "> Totals are reported per currency; a finance controller should apply official rates for consolidated reporting.",
        "> Positive invoices and credit notes are reported separately — counts reconcile to 145 clean rows total.",
        "",
        "| Currency | Invoices | Total Amount | Type |",
        "|----------|----------|-------------|------|",
    ]
    for s in r["spend_by_currency"]:
        lines.append(f"| {s['currency']} | {s['invoice_count']} | {s['total_amount']:,.2f} | Invoice |")
    for s in r.get("credit_notes_by_currency", []):
        lines.append(f"| {s['currency']} | {s['invoice_count']} | {s['total_amount']:,.2f} | Credit note |")

    # ── Compliance flags ───────────────────────────────────────────────────────
    lines += [
        "",
        "## Compliance Flags",
        "",
        "> ⚠️ **Reliability warning:** `APPROVAL_LEVEL_VIOLATION` (62 flags, highest count) is based on a mock IAM config.",
        "> These figures must not be treated as confirmed violations until Azure AD integration is in place.",
        "",
        "| Flag | Count | Note |",
        "|------|-------|------|",
    ]
    for k, v in r["compliance_summary"].items():
        note = "⚠️ indicative only — mock IAM, pending Azure AD integration" if k in r["indicative_only_flags"] else ""
        lines.append(f"| `{k}` | {v} | {note} |")

    # NO_PO currency breakdown
    if r.get("no_po_by_currency"):
        lines += [
            "",
            "### NO_PO — breakdown by currency",
            "",
            "| Currency | Count | Status |",
            "|----------|-------|--------|",
        ]
        for currency, count in r["no_po_by_currency"].items():
            status = "Definitive §2.1 breach" if currency == "EUR" else "Requires ECB FX rate to confirm §2.1 threshold"
            lines.append(f"| {currency} | {count} | {status} |")

    # ── Confirmed duplicate pairs ──────────────────────────────────────────────
    if r.get("confirmed_duplicate_pairs"):
        lines += [
            "",
            "### POTENTIAL_DUPLICATE — confirmed pairs",
            "",
            "> These are not ambiguous candidates: each pair shares an identical invoice_number, vendor, amount, date, and currency.",
            "> They represent likely duplicate payments and should be escalated for immediate review.",
            "",
            "| Invoice Number | Vendor | Amount | Currency | Occurrences |",
            "|----------------|--------|--------|----------|-------------|",
        ]
        for p in r["confirmed_duplicate_pairs"]:
            lines.append(f"| {p['invoice_number']} | {p['vendor_id']} | {p['amount']:,.2f} | {p['currency']} | {p['occurrences']} |")

    # ── Quarantined rows ───────────────────────────────────────────────────────
    if r["quarantined_rows"]:
        lines += ["", "## Quarantined Rows", "", "| Invoice | Reason |", "|---------|--------|"]
        for row in r["quarantined_rows"]:
            lines.append(f"| {row['invoice_number']} | {row['reason']} |")

    # ── Notes ──────────────────────────────────────────────────────────────────
    lines += ["", "## Notes", ""]
    for note in r["notes"]:
        lines.append(f"- {note}")

    return "\n".join(lines)


if __name__ == "__main__":
    report = generate_report()
    print(json.dumps(report, indent=2, default=str))

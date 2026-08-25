"""
Generates data quality and compliance reports by querying DuckDB directly.
This demonstrates SOX §8.2 lineage: reports are derived from the same
immutable audit_log and compliance_flags written by the pipeline.
"""
import json
from pathlib import Path

import duckdb


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

    # Fetch run_id first — all audit_log queries filter on it to report
    # only the current run, not the cumulative append-only history.
    run_info = conn.execute(
        "SELECT run_id, reference_date, run_timestamp, source_file FROM pipeline_log ORDER BY run_timestamp DESC LIMIT 1"
    ).fetchone()
    current_run_id = str(run_info[0]) if run_info else ""
    reference_date = str(run_info[1]) if run_info else "N/A"
    run_timestamp = str(run_info[2]) if run_info else "N/A"
    source_file = str(run_info[3]) if run_info else "N/A"

    total = conn.execute("SELECT COUNT(*) FROM invoices_clean").fetchone()[0]
    quarantined = conn.execute("SELECT COUNT(*) FROM invoices_clean WHERE quarantined = true").fetchone()[0]
    clean = total - quarantined
    flag_count = conn.execute("SELECT COUNT(*) FROM compliance_flags").fetchone()[0]

    ambiguous_dates = conn.execute(
        "SELECT COUNT(*) FROM invoices_clean WHERE date_ambiguous = true"
    ).fetchone()[0]

    dq_fixes = {
        "dates_parsed_to_iso": _count_action(conn, "→ ISO", current_run_id),
        "dates_ambiguous_flagged": ambiguous_dates,
        "currencies_uppercased": _count_action(conn, "Uppercased currency", current_run_id),
        "vendor_ids_dash_inserted": _count_action(conn, "Inserted dash", current_run_id),
        "vendor_names_normalized": _count_action(conn, "Normalized vendor name", current_run_id),
        "amounts_unquoted": _count_action(conn, "Stripped quotes", current_run_id),
        "vendor_ids_resolved_via_fuzzy_match": _count_action(conn, "fuzzy match", current_run_id),
        "rows_quarantined": quarantined,
    }

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

    audit_count = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE run_id = ?", [current_run_id]
    ).fetchone()[0]

    # Quarantine detail
    quarantine_detail = conn.execute(
        "SELECT invoice_number, quarantine_reason FROM invoices_clean WHERE quarantined = true"
    ).fetchall()

    conn.close()

    report = {
        "generated_at": run_timestamp,
        "run_id": current_run_id,
        "reference_date": reference_date,
        "source_file": source_file,
        "totals": {
            "rows_ingested": total,
            "rows_clean": clean,
            "rows_quarantined": quarantined,
            "audit_entries": audit_count,
            "compliance_flags_raised": flag_count,
        },
        "dq_fixes": dq_fixes,
        "compliance_summary": flag_counts,
        "indicative_only_flags": indicative_flags,
        "quarantined_rows": [
            {"invoice_number": r[0], "reason": r[1]} for r in quarantine_detail
        ],
        "notes": [
            "APPROVAL_LEVEL_VIOLATION flags are indicative_only=true — role mapping sourced from approver_roles.json (mock IAM integration point).",
            f"OVERDUE_APPROVAL reference_date={reference_date} (= max invoice_date in dataset, deterministic for SOX §8.2).",
            "DATE_AMBIGUOUS rows (date_ambiguous=true in invoices_clean) used assumed conventions: hyphens→MM-DD-YYYY, slashes→DD/MM/YYYY.",
            "BLOCKED_VENDOR (INACTIVE) and VENDOR_ON_HOLD (ON_HOLD) are distinct flags with different remediation paths per §4.1.",
            "Quarantined rows are retained in invoices_clean with quarantined=true — data is never dropped.",
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
        f"**Run ID:** {r['run_id']}  ",
        f"**Source file:** {r['source_file']}  ",
        f"**OVERDUE_APPROVAL reference date:** {r['reference_date']}",
        "",
        "## Pipeline Summary",
        "",
        "| Metric | Value |",
        "|--------|-------|",
    ]
    for k, v in r["totals"].items():
        lines.append(f"| {k.replace('_', ' ').title()} | {v} |")

    lines += [
        "",
        "## Data Quality Fixes Applied",
        "",
        "| Fix | Count |",
        "|-----|-------|",
    ]
    for k, v in r["dq_fixes"].items():
        lines.append(f"| {k.replace('_', ' ').title()} | {v} |")

    lines += [
        "",
        "## Compliance Flags",
        "",
        "| Flag | Count | Note |",
        "|------|-------|------|",
    ]
    for k, v in r["compliance_summary"].items():
        note = "⚠️ indicative only (mock IAM)" if k in r["indicative_only_flags"] else ""
        lines.append(f"| `{k}` | {v} | {note} |")

    if r["quarantined_rows"]:
        lines += ["", "## Quarantined Rows", "", "| Invoice | Reason |", "|---------|--------|"]
        for row in r["quarantined_rows"]:
            lines.append(f"| {row['invoice_number']} | {row['reason']} |")

    lines += ["", "## Notes", ""]
    for note in r["notes"]:
        lines.append(f"- {note}")

    return "\n".join(lines)


if __name__ == "__main__":
    report = generate_report()
    print(json.dumps(report, indent=2, default=str))

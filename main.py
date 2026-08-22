"""Entry point: run the full ETL pipeline then generate the quality report."""
from pipeline.pipeline import run_pipeline
from pipeline.quality_report import generate_report


def main():
    result = run_pipeline()
    print(f"Pipeline complete")
    print(f"  Rows   : {result.clean_rows} clean | {result.quarantined_rows} quarantined / {result.total_rows} total")
    print(f"  Flags  : {result.flags_raised}")
    print(f"  Audit  : {result.audit_entries} entries")
    print(f"  Ref dt : {result.reference_date}")

    report = generate_report(db_path=result.db_path)
    print(f"\nQuality report written to data/quality_report.json + data/quality_report.md")
    print(f"  DQ fixes : {sum(report['dq_fixes'].values())}")
    print(f"  Flags    : {report['compliance_summary']}")


if __name__ == "__main__":
    main()

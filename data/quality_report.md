# Data Quality & Compliance Report

**Generated:** 2026-08-25T10:57:03.710556+00:00  
**Run ID:** `0e5b9a8c-57d9-4caa-8361-afc813af0e58`  
**Source file:** data/invoices_2025.csv  
**Reference date (OVERDUE_APPROVAL):** 2025-12-26

## Pipeline Summary

| Metric | Value |
|--------|-------|
| Rows Ingested | 145 |
| Rows Clean | 145 |
| Rows Quarantined | 0 |
| Audit Entries | 169 |
| Compliance Flags Raised | 174 |

## Issue Classes Found & Handling Decisions

| Issue Class | Found | Decision | Handling |
|-------------|-------|----------|----------|
| Mixed / non-ISO date formats | 123 | **Fix** | Parsed to ISO 8601 using format heuristics (YYYY-MM-DD, DD.MM.YYYY, MM-DD-YYYY, DD/MM/YYYY). Deterministic per format — no data loss. |
| Ambiguous dates (both parts ≤ 12) | 54 | **Flag** | Parsed with assumed convention; date_ambiguous=true set in invoices_clean. Affects OVERDUE_APPROVAL flags — human review recommended for flagged rows. |
| Lowercase / mixed-case currency codes | 18 | **Fix** | Uppercased to ISO 4217 standard (eur → EUR). No ambiguity — purely a casing artefact from the AP export. |
| Malformed vendor IDs (missing dash) | 10 | **Fix** | Regex normalisation: V1002 → V-1002. Pattern V-XXXX is unambiguous — no heuristic involved. |
| Missing vendor IDs resolved via name match | 4 | **Fix** | Fuzzy match on vendor_name vs vendor master (threshold 0.7, casefold). Match score and method logged in audit_log. Production path: matches 0.7–0.9 should go to human review queue. |
| Quoted amounts / thousand-separator commas | 4 | **Fix** | Stripped surrounding quotes and comma separators. AP export artefact — format is deterministic and reversible. |
| Vendor names with excess whitespace | 10 | **Fix** | Strip + whitespace collapse only. Canonical commercial casing preserved from vendor master — vendor name is never modified beyond removing leading/trailing whitespace. |
| Rows with unrecoverable errors | 0 | **Quarantine** | Retained in invoices_clean with quarantined=true and quarantine_reason populated. Never dropped silently — remains visible in invoices_enriched via LEFT JOIN. |

## Spend Summary by Currency

> Cross-currency aggregation without an authoritative ECB FX source would produce a misleading total.
> Totals are reported per currency; a finance controller should apply official rates for consolidated reporting.
> Positive invoices and credit notes are reported separately — counts reconcile to 145 clean rows total.

| Currency | Invoices | Total Amount | Type |
|----------|----------|-------------|------|
| EUR | 91 | 4,705,163.89 | Invoice |
| GBP | 29 | 1,228,437.15 | Invoice |
| USD | 23 | 1,179,980.08 | Invoice |
| EUR | 1 | -52,749.39 | Credit note |
| USD | 1 | -29,534.55 | Credit note |

## Compliance Flags

> ⚠️ **Reliability warning:** `APPROVAL_LEVEL_VIOLATION` (62 flags, highest count) is based on a mock IAM config.
> These figures must not be treated as confirmed violations until Azure AD integration is in place.

| Flag | Count | Note |
|------|-------|------|
| `APPROVAL_LEVEL_VIOLATION` | 62 | ⚠️ indicative only — mock IAM, pending Azure AD integration |
| `PENDING_APPROVAL` | 26 |  |
| `OVERDUE_APPROVAL` | 24 |  |
| `NO_PO` | 21 |  |
| `BLOCKED_VENDOR` | 17 |  |
| `VENDOR_ON_HOLD` | 12 |  |
| `POTENTIAL_DUPLICATE` | 10 |  |
| `CREDIT_NOTE` | 2 |  |

### NO_PO — breakdown by currency

| Currency | Count | Status |
|----------|-------|--------|
| EUR | 14 | Definitive §2.1 breach |
| USD | 4 | Requires ECB FX rate to confirm §2.1 threshold |
| GBP | 3 | Requires ECB FX rate to confirm §2.1 threshold |

### POTENTIAL_DUPLICATE — confirmed pairs

> These are not ambiguous candidates: each pair shares an identical invoice_number, vendor, amount, date, and currency.
> They represent likely duplicate payments and should be escalated for immediate review.

| Invoice Number | Vendor | Amount | Currency | Occurrences |
|----------------|--------|--------|----------|-------------|
| INV-20107 | V-1001 | 84,170.64 | EUR | 2 |
| INV-20119 | V-1007 | 61,045.65 | EUR | 2 |
| INV-20117 | V-1005 | 32,989.91 | GBP | 2 |
| INV-20015 | V-1004 | 13,472.09 | EUR | 2 |
| INV-20132 | V-1004 | 13,196.22 | GBP | 2 |

## Notes

- ⚠️ APPROVAL_LEVEL_VIOLATION (62 flags, highest count) are INDICATIVE ONLY — role mapping uses a mock IAM config. These figures must not be treated as confirmed violations until an authoritative Azure AD integration is in place.
- ⚠️ OVERDUE_APPROVAL reference_date=2025-12-26 is the max invoice_date in the dataset, NOT the actual current date. This report is not an operational snapshot — actual overdue delays are significantly underestimated. Production fix: pass the accounting period close date as the reference parameter.
- DATE_AMBIGUOUS (54 rows): format assumed hyphens→MM-DD-YYYY, slashes→DD/MM/YYYY. For a company with primarily European vendors this assumption may be inverted. Production fix: cross-reference with vendor country from vendor_master to resolve ambiguity. Affects OVERDUE_APPROVAL flags for ambiguous rows.
- POTENTIAL_DUPLICATE flags represent confirmed duplicate invoice_numbers (identical in every field) — these are likely duplicate payments, not merely candidates for review. See confirmed duplicate pairs table.
- NO_PO non-EUR rows flagged for controller review — EUR equivalent requires ECB FX rate to confirm §2.1 threshold breach definitively.
- Spend table shows 143 positive invoices + 2 credit notes = 145 clean rows total. Credit notes are reported separately to avoid distorting the spend figures.
- BLOCKED_VENDOR (INACTIVE) and VENDOR_ON_HOLD (ON_HOLD) receive distinct flags — policy §4.1 prescribes different remediation paths for each status.
- Out-of-scope rules not implemented: §7 self-approval (no requester field in dataset), §6.1 credit note age/matching (no originating invoice ref), §2.3/§9 blanket PO and emergency exceptions (no Exception Register in dataset).
- Quarantined rows are retained in invoices_clean with quarantined=true — data is never dropped silently.
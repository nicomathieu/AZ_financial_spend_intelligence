"""
Pure cleaning functions — one per DQ issue.
Each returns (cleaned_value, AuditEntry | None).
No side effects, no I/O — fully unit-testable in isolation.
"""
import re
from datetime import date
from difflib import get_close_matches
from typing import Optional

from pipeline.models import AuditEntry, VendorRecord


def _audit(
    row_id: int,
    invoice_number: str,
    field: str,
    original: str,
    new: str,
    action: str,
) -> AuditEntry:
    return AuditEntry(
        row_id=row_id,
        invoice_number=invoice_number,
        field=field,
        original_value=original,
        new_value=new,
        action=action,
    )


def parse_date(
    raw: str, row_id: int, invoice_number: str
) -> tuple[date, bool, Optional[AuditEntry]]:
    """
    Returns (parsed_date, is_ambiguous, audit_entry | None).

    is_ambiguous=True when both non-year parts are ≤ 12 — the format assumption
    could be inverted (e.g. 07-06-2025 could be July 6 or June 7).

    Format conventions (derived from unambiguous rows in the dataset):
      YYYY-MM-DD  →  ISO, never ambiguous
      DD.MM.YYYY  →  dot-separated European
      MM-DD-YYYY  →  hyphen-separated US
      DD/MM/YYYY  →  slash-separated European
    """
    s = raw.strip()

    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return date.fromisoformat(s), False, None

    if re.match(r"^\d{2}\.\d{2}\.\d{4}$", s):
        day, month, year = int(s[:2]), int(s[3:5]), int(s[6:])
        ambiguous = day <= 12
        parsed = date(year, month, day)
        entry = _audit(row_id, invoice_number, "invoice_date", s, parsed.isoformat(), "Parsed DD.MM.YYYY → ISO")
        return parsed, ambiguous, entry

    if re.match(r"^\d{2}-\d{2}-\d{4}$", s):
        month, day, year = int(s[:2]), int(s[3:5]), int(s[6:])
        ambiguous = day <= 12
        parsed = date(year, month, day)
        entry = _audit(row_id, invoice_number, "invoice_date", s, parsed.isoformat(), "Parsed MM-DD-YYYY → ISO")
        return parsed, ambiguous, entry

    if re.match(r"^\d{2}/\d{2}/\d{4}$", s):
        day, month, year = int(s[:2]), int(s[3:5]), int(s[6:])
        ambiguous = day <= 12
        parsed = date(year, month, day)
        entry = _audit(row_id, invoice_number, "invoice_date", s, parsed.isoformat(), "Parsed DD/MM/YYYY → ISO")
        return parsed, ambiguous, entry

    raise ValueError(f"Unrecognised date format: {raw!r}")


def normalize_currency(
    raw: str, row_id: int, invoice_number: str
) -> tuple[str, Optional[AuditEntry]]:
    normalized = raw.strip().upper()
    if normalized == raw.strip():
        return normalized, None
    entry = _audit(row_id, invoice_number, "currency", raw, normalized, "Uppercased currency code")
    return normalized, entry


def normalize_vendor_id(
    raw: str, row_id: int, invoice_number: str
) -> tuple[str, Optional[AuditEntry]]:
    # Regex inserts a dash after the leading V — matches V1002→V-1002, V1010→V-1010, etc.
    fixed = re.sub(r"^V(\d)", r"V-\1", raw)
    if fixed == raw:
        return fixed, None
    entry = _audit(row_id, invoice_number, "vendor_id", raw, fixed, "Inserted dash into malformed vendor ID")
    return fixed, entry


def normalize_vendor_name(
    raw: str, row_id: int, invoice_number: str
) -> tuple[str, Optional[AuditEntry]]:
    """Strip + title-case. Original preserved in CleanInvoice.vendor_name_raw."""
    normalized = raw.strip().title()
    if normalized == raw:
        return normalized, None
    entry = _audit(row_id, invoice_number, "vendor_name", raw, normalized, "Normalized vendor name (strip + title case)")
    return normalized, entry


def parse_amount(
    raw: str, row_id: int, invoice_number: str
) -> tuple[float, Optional[AuditEntry]]:
    cleaned = raw.strip().strip('"').replace(",", "")
    value = float(cleaned)
    if cleaned == raw.strip():
        return value, None
    entry = _audit(row_id, invoice_number, "amount", raw, cleaned, "Stripped quotes and thousand-separator commas")
    return value, entry


def resolve_vendor_id(
    vendor_name_raw: str,
    vendors: list[VendorRecord],
    row_id: int,
    invoice_number: str,
) -> tuple[Optional[str], Optional[AuditEntry]]:
    """
    Fuzzy-matches a missing vendor_id from vendor_name against the vendor master.
    Returns (vendor_id, audit_entry) or (None, None) when no match found.
    """
    normalized = vendor_name_raw.strip().title()
    master = {v.vendor_name.strip().title(): v.vendor_id for v in vendors}

    matches = get_close_matches(normalized, master.keys(), n=1, cutoff=0.7)
    if not matches:
        return None, None

    vendor_id = master[matches[0]]
    entry = _audit(
        row_id, invoice_number, "vendor_id", "",
        vendor_id,
        f"Resolved missing vendor_id via fuzzy match: '{normalized}' → '{matches[0]}' ({vendor_id})",
    )
    return vendor_id, entry

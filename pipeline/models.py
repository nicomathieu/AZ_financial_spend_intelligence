from datetime import date, datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class FlagType(str, Enum):
    NO_PO = "NO_PO"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    OVERDUE_APPROVAL = "OVERDUE_APPROVAL"
    BLOCKED_VENDOR = "BLOCKED_VENDOR"
    VENDOR_ON_HOLD = "VENDOR_ON_HOLD"
    APPROVAL_LEVEL_VIOLATION = "APPROVAL_LEVEL_VIOLATION"
    CREDIT_NOTE = "CREDIT_NOTE"
    POTENTIAL_DUPLICATE = "POTENTIAL_DUPLICATE"
    LOGICAL_DUPLICATE_CANDIDATE = "LOGICAL_DUPLICATE_CANDIDATE"


class VendorStatus(str, Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    ON_HOLD = "ON_HOLD"


class RawInvoice(BaseModel):
    """Direct mapping of a CSV row — all fields as raw strings, no validation yet."""
    invoice_number: str
    invoice_date: str
    vendor_id: str
    vendor_name: str
    description: str
    amount: str
    currency: str
    cost_center: str
    po_number: str
    approved_by: str


class CleanInvoice(BaseModel):
    """Cleaned, normalised invoice ready for compliance checks and persistence."""
    row_id: int
    invoice_number: str
    invoice_date: date
    date_ambiguous: bool
    vendor_id: str
    vendor_name_raw: str
    vendor_name_normalized: str
    description: str
    amount: float
    currency: str
    cost_center: str
    po_number: Optional[str]
    approved_by: Optional[str]
    quarantined: bool = False
    quarantine_reason: Optional[str] = None


class VendorRecord(BaseModel):
    vendor_id: str
    vendor_name: str
    country: str
    category: str
    status: VendorStatus


class AuditEntry(BaseModel):
    """Immutable transformation record — required for SOX §8.2 lineage."""
    model_config = ConfigDict(frozen=True)

    row_id: int
    invoice_number: str
    field: str
    original_value: str
    new_value: str
    action: str
    run_timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class ComplianceFlag(BaseModel):
    row_id: int
    invoice_number: str
    flag_type: FlagType
    detail: str
    indicative_only: bool = False


class PipelineResult(BaseModel):
    total_rows: int
    clean_rows: int
    quarantined_rows: int
    audit_entries: int
    flags_raised: int
    reference_date: date
    db_path: str

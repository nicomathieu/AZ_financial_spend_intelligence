"""
Compliance rules — one class per flag type (Open/Closed principle).
Adding a new rule = new class + append to ALL_RULES. Nothing else changes.

Each rule implements the ComplianceRule protocol:
  check(invoice, vendor_map, approver_roles, all_invoices, reference_date) -> list[ComplianceFlag]
"""
from datetime import date
from typing import Protocol, runtime_checkable

from pipeline.models import CleanInvoice, ComplianceFlag, FlagType, VendorRecord, VendorStatus

# Policy §3 approval thresholds (EUR). Ordered ascending for required_role().
_APPROVAL_THRESHOLDS: list[tuple[float, str]] = [
    (5_000.00,   "cost_center_owner"),
    (50_000.00,  "department_head"),
    (250_000.00, "finance_director"),
]

# Maximum role limit — approver at this tier can approve any amount
_ROLE_LIMITS: dict[str, float] = {
    "cost_center_owner": 5_000.00,
    "department_head":   50_000.00,
    "finance_director":  250_000.00,
    "cfo":               float("inf"),
}


def _required_role(amount: float) -> str:
    for threshold, role in _APPROVAL_THRESHOLDS:
        if amount <= threshold:
            return role
    return "cfo"


@runtime_checkable
class ComplianceRule(Protocol):
    def check(
        self,
        invoice: CleanInvoice,
        vendor_map: dict[str, VendorRecord],
        approver_roles: dict[str, str],
        all_invoices: list[CleanInvoice],
        reference_date: date,
    ) -> list[ComplianceFlag]: ...


class CreditNoteRule:
    """§6.1 — negative amount must be processed as a credit note."""

    def check(self, invoice, vendor_map, approver_roles, all_invoices, reference_date):
        if invoice.amount < 0:
            return [ComplianceFlag(
                row_id=invoice.row_id,
                invoice_number=invoice.invoice_number,
                flag_type=FlagType.CREDIT_NOTE,
                detail=(
                    f"Negative amount {invoice.amount:.2f} {invoice.currency} — "
                    "process as credit note and match to original invoice (§6.1)"
                ),
            )]
        return []


class NoPORule:
    """§2.1 — purchases ≥ EUR 1,000 require an approved PO.

    For non-EUR invoices the EUR 1,000 threshold cannot be applied without FX
    rates. We flag if the nominal amount ≥ 1,000 in the invoice currency and
    note the currency explicitly — a finance controller must confirm whether
    the EUR equivalent crosses the threshold.
    """

    def check(self, invoice, vendor_map, approver_roles, all_invoices, reference_date):
        if invoice.quarantined or invoice.amount < 0:
            return []
        if not invoice.po_number and invoice.amount >= 1_000:
            if invoice.currency == "EUR":
                detail = (
                    f"Amount {invoice.amount:.2f} EUR ≥ EUR 1,000 threshold "
                    "with no PO number (maverick spend, §2.1)"
                )
            else:
                detail = (
                    f"Amount {invoice.amount:.2f} {invoice.currency} ≥ 1,000 nominal "
                    "with no PO number — EUR equivalent requires FX conversion to "
                    "confirm §2.1 threshold breach; flagged for controller review"
                )
            return [ComplianceFlag(
                row_id=invoice.row_id,
                invoice_number=invoice.invoice_number,
                flag_type=FlagType.NO_PO,
                detail=detail,
            )]
        return []


class PendingApprovalRule:
    """§3.1 — missing approver is a workflow state (not an error), but must be held."""

    def check(self, invoice, vendor_map, approver_roles, all_invoices, reference_date):
        if invoice.quarantined:
            return []
        if not invoice.approved_by:
            return [ComplianceFlag(
                row_id=invoice.row_id,
                invoice_number=invoice.invoice_number,
                flag_type=FlagType.PENDING_APPROVAL,
                detail="No approver recorded — invoice held pending approval (§3.1)",
            )]
        return []


class OverdueApprovalRule:
    """Business heuristic — unapproved invoice older than 30 days.

    The policy (§3.1) only states that a missing approver must be held; it does
    not define a 30-day escalation threshold. The 30-day window is a common
    operational convention, not a policy requirement. Flags are therefore
    indicative_only=True and should never be treated as confirmed violations.
    In production, this threshold would come from a configurable SLA parameter,
    not be hardcoded here.
    """

    def check(self, invoice, vendor_map, approver_roles, all_invoices, reference_date):
        if invoice.quarantined or invoice.approved_by:
            return []
        age_days = (reference_date - invoice.invoice_date).days
        if age_days > 30:
            return [ComplianceFlag(
                row_id=invoice.row_id,
                invoice_number=invoice.invoice_number,
                flag_type=FlagType.OVERDUE_APPROVAL,
                detail=(
                    f"No approver + invoice is {age_days} days old "
                    f"(reference_date={reference_date}) — "
                    "business heuristic, 30-day threshold not in policy"
                ),
                indicative_only=True,
            )]
        return []


class BlockedVendorRule:
    """§4.1 — INACTIVE vendor: payment prohibited."""

    def check(self, invoice, vendor_map, approver_roles, all_invoices, reference_date):
        if invoice.quarantined:
            return []
        vendor = vendor_map.get(invoice.vendor_id)
        if vendor and vendor.status == VendorStatus.INACTIVE:
            return [ComplianceFlag(
                row_id=invoice.row_id,
                invoice_number=invoice.invoice_number,
                flag_type=FlagType.BLOCKED_VENDOR,
                detail=(
                    f"Vendor {invoice.vendor_id} ({vendor.vendor_name}) is INACTIVE — "
                    "payment prohibited, escalate to Procurement Excellence (§4.1)"
                ),
            )]
        return []


class VendorOnHoldRule:
    """§4.1 — ON_HOLD vendor: escalation required before processing."""

    def check(self, invoice, vendor_map, approver_roles, all_invoices, reference_date):
        if invoice.quarantined:
            return []
        vendor = vendor_map.get(invoice.vendor_id)
        if vendor and vendor.status == VendorStatus.ON_HOLD:
            return [ComplianceFlag(
                row_id=invoice.row_id,
                invoice_number=invoice.invoice_number,
                flag_type=FlagType.VENDOR_ON_HOLD,
                detail=(
                    f"Vendor {invoice.vendor_id} ({vendor.vendor_name}) is ON_HOLD — "
                    "escalate to Procurement Excellence before processing (§4.1)"
                ),
            )]
        return []


class ApprovalLevelViolationRule:
    """
    §3 — approver tier must cover the invoice amount.
    indicative_only=True: role mapping sourced from approver_roles.json (mock IAM).
    Someone who approved a €60k invoice may have done so incorrectly — we do not
    infer their tier from the observed approval; that would mask the violation.
    """

    def check(self, invoice, vendor_map, approver_roles, all_invoices, reference_date):
        if invoice.quarantined or not invoice.approved_by:
            return []
        role = approver_roles.get(invoice.approved_by)
        if role is None:
            return []  # Unknown approver — cannot assess without IAM data
        limit = _ROLE_LIMITS.get(role, float("inf"))
        if abs(invoice.amount) > limit:
            required = _required_role(abs(invoice.amount))
            currency_note = (
                "" if invoice.currency == "EUR"
                else f" (amount in {invoice.currency} — EUR equivalent requires FX conversion to confirm)"
            )
            return [ComplianceFlag(
                row_id=invoice.row_id,
                invoice_number=invoice.invoice_number,
                flag_type=FlagType.APPROVAL_LEVEL_VIOLATION,
                detail=(
                    f"Amount {invoice.amount:.2f} {invoice.currency} requires '{required}' "
                    f"but '{invoice.approved_by}' (role: {role}, limit: {limit:.0f} EUR) approved (§3)"
                    f"{currency_note}"
                ),
                indicative_only=True,
            )]
        return []


class PotentialDuplicateRule:
    """Same invoice_number appearing more than once — flag all occurrences for human review."""

    def check(self, invoice, vendor_map, approver_roles, all_invoices, reference_date):
        twins = [
            inv for inv in all_invoices
            if inv.invoice_number == invoice.invoice_number and inv.row_id != invoice.row_id
        ]
        if twins:
            twin_ids = ", ".join(str(t.row_id) for t in twins)
            return [ComplianceFlag(
                row_id=invoice.row_id,
                invoice_number=invoice.invoice_number,
                flag_type=FlagType.POTENTIAL_DUPLICATE,
                detail=f"Invoice number also at row_id(s): {twin_ids} — flag for human review",
            )]
        return []


class LogicalDuplicateRule:
    """Different invoice number, same vendor + same amount + date within 7 days."""

    def check(self, invoice, vendor_map, approver_roles, all_invoices, reference_date):
        candidates = [
            inv for inv in all_invoices
            if (
                inv.row_id != invoice.row_id
                and inv.invoice_number != invoice.invoice_number
                and inv.vendor_id == invoice.vendor_id
                and inv.amount == invoice.amount
                and inv.currency == invoice.currency
                and abs((inv.invoice_date - invoice.invoice_date).days) <= 7
            )
        ]
        if candidates:
            ids = ", ".join(str(c.row_id) for c in candidates)
            return [ComplianceFlag(
                row_id=invoice.row_id,
                invoice_number=invoice.invoice_number,
                flag_type=FlagType.LOGICAL_DUPLICATE_CANDIDATE,
                detail=f"Same vendor + amount + date within 7 days as row_id(s): {ids}",
            )]
        return []


ALL_RULES: list = [
    CreditNoteRule(),
    NoPORule(),
    PendingApprovalRule(),
    OverdueApprovalRule(),
    BlockedVendorRule(),
    VendorOnHoldRule(),
    ApprovalLevelViolationRule(),
    PotentialDuplicateRule(),
    LogicalDuplicateRule(),
]

"""Recurring bill and subscription radar engine."""

from __future__ import annotations

import calendar
import re
from datetime import date
from typing import Literal, Optional

from src.database.db_manager import DatabaseManager
from src.engine.categorizer import _boundary_match, extract_canonical_payee
from src.models import RecurringCommitment, RecurringStatus, TransactionRecord

_MONTH_KEY_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_BILL_TAG_RE = re.compile(r"\[bill:(\d+)\]")


def _parse_month(month_key: str) -> tuple[str, int, int]:
    cleaned = month_key.strip()
    m = _MONTH_KEY_RE.match(cleaned)
    if not m:
        raise ValueError(f"Invalid month_key '{month_key}'; expected YYYY-MM")
    return cleaned, int(m.group(1)), int(m.group(2))


def _keyword_specificity(commitment: RecurringCommitment, tx: TransactionRecord) -> Optional[int]:
    """Return match priority score (higher = more specific) or None if tx cannot match commitment."""
    notes = tx.notes or ""
    bill_tags = [int(x) for x in _BILL_TAG_RE.findall(notes)]
    if bill_tags:
        if commitment.id is not None and commitment.id in bill_tags:
            return 1000
        return None

    # Ignore non-payment friction/inflow/transfer rows unless explicitly tagged above
    expected_bucket = commitment.cost_bucket
    if expected_bucket == "INVESTMENT":
        if tx.tx_type != "TRANSFER_MMF":
            return None
    elif tx.tx_type != "DEBIT" or tx.cost_bucket in ("IGNORE", "LEAKAGE", "INFLOW"):
        return None

    kw = commitment.match_keyword.strip()
    if not kw:
        return None

    canon = extract_canonical_payee(tx.payee)
    for idx, candidate in enumerate((tx.merchant_alias or "", canon, tx.payee)):
        if candidate and _boundary_match(kw, candidate):
            return len(kw) * 10 - idx
        if len(kw) >= 5 and candidate and kw.lower() in candidate.lower():
            return len(kw) * 5 - idx
    return None


def _compute_bill_status(
    *,
    is_paid: bool,
    due_date_str: str,
    ref_date: date,
) -> Literal["PAID", "OVERDUE", "DUE_SOON", "UPCOMING"]:
    if is_paid:
        return "PAID"
    due_dt = date.fromisoformat(due_date_str)
    if due_dt < ref_date:
        return "OVERDUE"
    if (due_dt - ref_date).days <= 5:
        return "DUE_SOON"
    return "UPCOMING"


def sync_recurring_status(
    db: DatabaseManager,
    month_key: str,
    reference_date: Optional[date] = None,
    persist_flags: Optional[bool] = None,
) -> list[RecurringStatus]:
    """Match month debits against active recurring commitments (1-to-1) and sync DB flags."""
    norm_month, year, month = _parse_month(month_key)
    ref_date = reference_date or date.today()
    should_persist = (
        persist_flags
        if persist_flags is not None
        else (norm_month == ref_date.strftime("%Y-%m"))
    )

    commitments = db.get_recurring_commitments(active_only=True)
    month_txs = [
        tx
        for tx in db.get_transactions(month_key=norm_month)
        if tx.tx_type in ("DEBIT", "TRANSFER_MMF")
    ]
    month_txs.sort(key=lambda t: (t.tx_date, t.tx_timestamp or "", t.id or 0))
    days_in_month = calendar.monthrange(year, month)[1]

    # Build 1-to-1 assignment candidates sorted by explicit tag -> specificity -> closest amount -> latest date
    candidates: list[tuple[int, float, str, int, int, RecurringCommitment, TransactionRecord]] = []
    for c_idx, item in enumerate(commitments):
        for tx_idx, tx in enumerate(month_txs):
            spec = _keyword_specificity(item, tx)
            if spec is None:
                continue
            # Un-tagged micro-charges (< 35% of expected amount) shouldn't settle a whole bill
            if spec < 1000 and tx.amount < (item.expected_amount * 0.35):
                continue
            amt_diff = abs(tx.amount - item.expected_amount)
            candidates.append(
                (
                    -spec,
                    amt_diff,
                    tx.tx_date,
                    tx.id or tx_idx,
                    c_idx,
                    item,
                    tx,
                )
            )
    candidates.sort(key=lambda row: (row[0], row[1], row[2], row[3], row[4]))

    used_tx_keys: set[tuple[Optional[int], str]] = set()
    claimed_by_commitment: dict[int, list[TransactionRecord]] = {i: [] for i in range(len(commitments))}
    for _, _, _, _, c_idx, _, tx in candidates:
        tx_key = (tx.id, tx.message_id)
        if tx_key in used_tx_keys or claimed_by_commitment[c_idx]:
            continue
        used_tx_keys.add(tx_key)
        claimed_by_commitment[c_idx].append(tx)

    # Also attach additional explicit [bill:{id}] partial installments for the same commitment
    for c_idx, item in enumerate(commitments):
        if item.id is None:
            continue
        tag = f"[bill:{item.id}]"
        for tx in month_txs:
            tx_key = (tx.id, tx.message_id)
            if tx_key in used_tx_keys:
                continue
            if tag in (tx.notes or ""):
                used_tx_keys.add(tx_key)
                claimed_by_commitment[c_idx].append(tx)

    statuses: list[RecurringStatus] = []
    for c_idx, item in enumerate(commitments):
        effective_day = min(item.due_day, days_in_month)
        due_date_str = f"{year:04d}-{month:02d}-{effective_day:02d}"
        matched = sorted(
            claimed_by_commitment.get(c_idx, []),
            key=lambda t: (t.tx_date, t.tx_timestamp or "", t.id or 0),
        )
        paid_amount = round(sum(tx.amount for tx in matched), 2)
        explicit_tagged = bool(
            item.id is not None and any(f"[bill:{item.id}]" in (tx.notes or "") for tx in matched)
        )
        is_paid = bool(
            matched and (explicit_tagged or paid_amount >= round(item.expected_amount * 0.50, 2))
        )
        if is_paid:
            latest = matched[-1]
            paid_date: Optional[str] = latest.tx_date
            matched_tx_id: Optional[int] = latest.id
            if should_persist and item.id is not None:
                db.mark_recurring_paid(item.id, True, norm_month)
            updated_item = item.model_copy(
                update={
                    "is_paid_this_month": True,
                    "last_paid_month": norm_month,
                }
            )
            statuses.append(
                RecurringStatus(
                    commitment=updated_item,
                    is_paid=True,
                    paid_amount=paid_amount,
                    paid_date=paid_date,
                    matched_tx_id=matched_tx_id,
                    due_date=due_date_str,
                    status="PAID",
                )
            )
        else:
            if should_persist and item.id is not None:
                db.mark_recurring_paid(item.id, False, norm_month)
            updated_item = item.model_copy(
                update={
                    "is_paid_this_month": False,
                    "last_paid_month": None
                    if item.last_paid_month == norm_month
                    else item.last_paid_month,
                }
            )
            statuses.append(
                RecurringStatus(
                    commitment=updated_item,
                    is_paid=False,
                    paid_amount=paid_amount,
                    paid_date=None,
                    matched_tx_id=matched[-1].id if matched else None,
                    due_date=due_date_str,
                    status=_compute_bill_status(
                        is_paid=False, due_date_str=due_date_str, ref_date=ref_date
                    ),
                )
            )

    return statuses

"""Recurring bill and subscription radar engine."""

from __future__ import annotations

from datetime import date
from typing import Optional

from src.database.db_manager import DatabaseManager
from src.engine.categorizer import _keyword_matches
from src.models import RecurringCommitment, RecurringStatus, TransactionRecord


def _matches_commitment(commitment: RecurringCommitment, tx: TransactionRecord) -> bool:
    """Match commitment keyword or explicit bill tag against transaction fields."""
    if commitment.id is not None and f"[bill:{commitment.id}]" in (tx.notes or ""):
        return True
    kw = commitment.match_keyword.strip()
    if not kw:
        return False
    for candidate in (tx.payee, tx.merchant_alias or ""):
        if candidate and _keyword_matches(
            kw, candidate, is_user_defined=False, payment_channel=tx.payment_channel
        ):
            return True
    return False


def sync_recurring_status(
    db: DatabaseManager,
    month_key: str,
    reference_date: Optional[date] = None,
    persist_flags: Optional[bool] = None,
) -> list[RecurringStatus]:
    """Match month debits against active recurring commitments and conditionally sync DB flags."""
    ref_date = reference_date or date.today()
    should_persist = (
        persist_flags
        if persist_flags is not None
        else (month_key == ref_date.strftime("%Y-%m"))
    )

    commitments = db.get_recurring_commitments(active_only=True)
    month_debits = [
        tx
        for tx in db.get_transactions(month_key=month_key)
        if tx.tx_type == "DEBIT" and tx.cost_bucket != "IGNORE"
    ]
    # Order chronologically ascending so latest match wins for paid_date / matched_tx_id
    month_debits.sort(key=lambda t: (t.tx_date, t.tx_timestamp or "", t.id or 0))

    statuses: list[RecurringStatus] = []
    for item in commitments:
        matched = [tx for tx in month_debits if _matches_commitment(item, tx)]
        if matched:
            latest = matched[-1]
            paid_amount = round(sum(tx.amount for tx in matched), 2)
            paid_date: Optional[str] = latest.tx_date
            matched_tx_id: Optional[int] = latest.id
            if should_persist and item.id is not None:
                db.mark_recurring_paid(item.id, True, month_key)
            updated_item = item.model_copy(
                update={
                    "is_paid_this_month": True,
                    "last_paid_month": month_key,
                }
            )
            statuses.append(
                RecurringStatus(
                    commitment=updated_item,
                    is_paid=True,
                    paid_amount=paid_amount,
                    paid_date=paid_date,
                    matched_tx_id=matched_tx_id,
                )
            )
        else:
            if should_persist and item.id is not None:
                db.mark_recurring_paid(item.id, False, month_key)
            updated_item = item.model_copy(
                update={
                    "is_paid_this_month": False,
                    "last_paid_month": None
                    if item.last_paid_month == month_key
                    else item.last_paid_month,
                }
            )
            statuses.append(
                RecurringStatus(
                    commitment=updated_item,
                    is_paid=False,
                    paid_amount=0.0,
                    paid_date=None,
                    matched_tx_id=None,
                )
            )

    return statuses

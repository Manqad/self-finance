"""Month 1 Financial Defense & Batwa analytics calculation engine."""

from __future__ import annotations

import calendar
import re
from collections import Counter, defaultdict
from datetime import date
from typing import Literal, Optional

from src.database.db_manager import DatabaseManager
from src.engine.recurring import sync_recurring_status
from src.models import (
    WALLET_CASH,
    WALLET_HABIBMETRO,
    WALLET_MMF,
    WALLET_NAYAPAY,
    CategoryProgress,
    DailyBurnPoint,
    DashboardMetrics,
    MerchantSpend,
    TransactionRecord,
)

_MONTH_KEY_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


def _parse_month_key(month_key: str) -> tuple[int, int]:
    match = _MONTH_KEY_RE.match(month_key.strip())
    if not match:
        raise ValueError(f"Invalid month_key '{month_key}'; expected YYYY-MM")
    return int(match.group(1)), int(match.group(2))


def _previous_month_key(year: int, month: int) -> tuple[str, int, int]:
    if month == 1:
        return f"{year - 1}-12", year - 1, 12
    return f"{year}-{month - 1:02d}", year, month - 1


def _compute_burn_breakdown(
    txs: list[TransactionRecord], envelope_mode: bool
) -> dict[str, object]:
    """Compute fixed, variable, leakage, tax fees, inflow, MMF swept, and daily burn."""
    actual_inflow = 0.0
    actual_fixed = 0.0
    non_envelope_variable = 0.0
    actual_leakage_base = 0.0
    total_tax_fees = 0.0
    monthly_mmf_swept = 0.0
    envelope_topups = 0.0
    envelope_returns = 0.0
    topup_events: list[tuple[int, float]] = []
    daily_burn: dict[int, float] = defaultdict(float)

    for tx in txs:
        day = int(tx.tx_date.split("-")[2])
        tx_type = tx.tx_type
        bucket = tx.cost_bucket

        if tx_type == "CREDIT":
            actual_inflow += tx.amount
            continue

        if tx_type == "TRANSFER_MMF":
            monthly_mmf_swept += tx.amount
            if tx.tax_fee_amount > 0:
                total_tax_fees += tx.tax_fee_amount
                daily_burn[day] += tx.tax_fee_amount
            continue

        if tx_type == "TRANSFER_INTERNAL":
            if envelope_mode and tx.target_account == WALLET_NAYAPAY:
                envelope_topups += tx.amount
                topup_events.append((day, tx.amount))
                total_tax_fees += tx.tax_fee_amount
                daily_burn[day] += tx.amount + tx.tax_fee_amount
            elif (
                envelope_mode
                and tx.account_name == WALLET_NAYAPAY
                and tx.target_account in (WALLET_HABIBMETRO, WALLET_CASH)
            ):
                envelope_returns += tx.amount
                if tx.tax_fee_amount > 0:
                    total_tax_fees += tx.tax_fee_amount
                    daily_burn[day] += tx.tax_fee_amount
            elif tx.tax_fee_amount > 0:
                total_tax_fees += tx.tax_fee_amount
                daily_burn[day] += tx.tax_fee_amount
            continue

        # DEBIT handling
        if envelope_mode and tx.account_name == WALLET_NAYAPAY:
            # Already counted when HabibMetro funded the NayaPay envelope
            continue

        total_tax_fees += tx.tax_fee_amount
        if bucket == "FIXED":
            actual_fixed += tx.amount
            daily_burn[day] += tx.amount + tx.tax_fee_amount
        elif bucket == "VARIABLE":
            non_envelope_variable += tx.amount
            daily_burn[day] += tx.amount + tx.tax_fee_amount
        elif bucket == "LEAKAGE":
            actual_leakage_base += tx.amount
            daily_burn[day] += tx.amount + tx.tax_fee_amount
        else:
            if tx.tax_fee_amount > 0:
                daily_burn[day] += tx.tax_fee_amount

    if envelope_mode:
        net_topups = max(0.0, envelope_topups - envelope_returns)
        reduction_needed = envelope_topups - net_topups
        actual_variable = non_envelope_variable + net_topups
        topup_events.sort(key=lambda item: item[0])
        for topup_day, topup_amt in reversed(topup_events):
            if reduction_needed <= 0:
                break
            used = min(topup_amt, reduction_needed)
            daily_burn[topup_day] -= used
            reduction_needed -= used
    else:
        actual_variable = non_envelope_variable

    actual_leakage = round(actual_leakage_base + total_tax_fees, 2)
    actual_fixed = round(actual_fixed, 2)
    actual_variable = round(actual_variable, 2)
    total_tax_fees = round(total_tax_fees, 2)
    total_monthly_burn = round(actual_fixed + actual_variable + actual_leakage, 2)

    return {
        "actual_inflow": round(actual_inflow, 2),
        "actual_fixed_burn": actual_fixed,
        "actual_variable_burn": actual_variable,
        "actual_leakage_burn": actual_leakage,
        "total_tax_fees": total_tax_fees,
        "total_monthly_burn": total_monthly_burn,
        "monthly_mmf_swept": round(monthly_mmf_swept, 2),
        "daily_burn": daily_burn,
    }


def _status_color(utilization_pct: float) -> Literal["green", "amber", "red"]:
    if utilization_pct < 75.0:
        return "green"
    if utilization_pct <= 95.0:
        return "amber"
    return "red"


def compute_dashboard_metrics(
    db: DatabaseManager,
    month_key: str,
    reference_date: Optional[date] = None,
) -> DashboardMetrics:
    """Compute all Month 1 Financial Defense equations and Batwa analytics for a month."""
    year, month = _parse_month_key(month_key)
    ref_date = reference_date or date.today()
    days_in_month = calendar.monthrange(year, month)[1]

    if (ref_date.year, ref_date.month) == (year, month):
        days_elapsed = min(max(ref_date.day, 1), days_in_month)
        days_remaining = (days_in_month - days_elapsed) + 1
    elif (year, month) < (ref_date.year, ref_date.month):
        days_elapsed = days_in_month
        days_remaining = 0
    else:
        days_elapsed = 0
        days_remaining = days_in_month

    config = db.get_budget_config()
    month_txs = db.get_transactions(month_key=month_key)
    upcoming_bills = sync_recurring_status(db, month_key, reference_date=ref_date)
    unpaid_bills_total = round(
        sum(
            status.commitment.expected_amount
            for status in upcoming_bills
            if not status.is_paid
        ),
        2,
    )

    burn = _compute_burn_breakdown(month_txs, config.envelope_mode)
    actual_inflow = float(burn["actual_inflow"])
    actual_fixed_burn = float(burn["actual_fixed_burn"])
    actual_variable_burn = float(burn["actual_variable_burn"])
    actual_leakage_burn = float(burn["actual_leakage_burn"])
    total_tax_fees = float(burn["total_tax_fees"])
    total_monthly_burn = float(burn["total_monthly_burn"])
    monthly_mmf_swept = float(burn["monthly_mmf_swept"])
    daily_burn: dict[int, float] = burn["daily_burn"]  # type: ignore[assignment]

    # Core Equations (SPEC.md Section 2)
    total_spending_cap = round(
        config.fixed_cost_baseline + config.variable_spending_cap, 2
    )
    remaining_allowance = round(total_spending_cap - total_monthly_burn, 2)
    if total_spending_cap > 0:
        burn_utilization_pct = round(
            (total_monthly_burn / total_spending_cap) * 100.0, 2
        )
    else:
        burn_utilization_pct = 100.0 if total_monthly_burn > 0 else 0.0
    safe_daily_spend = round(
        max(0.0, remaining_allowance / max(1, days_remaining)), 2
    )
    actual_daily_burn_pace = (
        round(total_monthly_burn / max(1, days_elapsed), 2)
        if (days_elapsed > 0 or total_monthly_burn > 0)
        else 0.0
    )

    # Month-over-Month daily burn pace
    prev_key, prev_year, prev_month = _previous_month_key(year, month)
    prev_txs = db.get_transactions(month_key=prev_key)
    prev_burn = _compute_burn_breakdown(prev_txs, config.envelope_mode)
    prev_days_in_month = calendar.monthrange(prev_year, prev_month)[1]
    prev_month_daily_burn_pace = round(
        float(prev_burn["total_monthly_burn"]) / prev_days_in_month, 2
    )
    mom_pace_delta_pct = (
        round(
            (
                (actual_daily_burn_pace - prev_month_daily_burn_pace)
                / prev_month_daily_burn_pace
            )
            * 100.0,
            2,
        )
        if prev_month_daily_burn_pace > 0
        else 0.0
    )

    # Wallets & Emergency Reserve
    wallet_balances = db.get_wallets()
    mmf_wallet = next(
        (w for w in wallet_balances if w.account_name == WALLET_MMF), None
    )
    emergency_current = round(mmf_wallet.current_balance if mmf_wallet else 0.0, 2)
    emergency_target = round(
        config.fixed_cost_baseline * config.emergency_multiplier, 2
    )
    emergency_progress_pct = (
        round((emergency_current / emergency_target) * 100.0, 2)
        if emergency_target > 0
        else 0.0
    )

    effective_income = (
        max(config.monthly_income, actual_inflow)
        if config.monthly_income > 0
        else actual_inflow
    )
    allocated_or_spent = max(total_spending_cap, total_monthly_burn)
    unallocated_cash_surplus = round(
        max(0.0, effective_income - allocated_or_spent - monthly_mmf_swept), 2
    )

    total_liquid_balance = round(
        sum(
            w.current_balance
            for w in wallet_balances
            if w.account_type in ("BANK", "WALLET", "CASH")
        ),
        2,
    )
    total_net_worth = round(sum(w.current_balance for w in wallet_balances), 2)
    needs_review_count = len(db.get_transactions(review_status="NEEDS_REVIEW"))

    # Per-category spend & Top 5 merchants
    category_spend: dict[str, float] = defaultdict(float)
    merchant_totals: dict[str, float] = defaultdict(float)
    merchant_counts: dict[str, int] = defaultdict(int)
    merchant_cats: dict[str, Counter[str]] = defaultdict(Counter)

    for tx in month_txs:
        if tx.tx_type != "DEBIT":
            continue
        if tx.cost_bucket not in ("FIXED", "VARIABLE", "LEAKAGE"):
            continue
        category_spend[tx.category] += tx.amount
        merchant_name = tx.merchant_alias or tx.payee
        merchant_totals[merchant_name] += tx.amount
        merchant_counts[merchant_name] += 1
        merchant_cats[merchant_name][tx.category] += 1

    if total_tax_fees > 0:
        category_spend["Bank & Tax Friction"] += total_tax_fees

    category_progress: list[CategoryProgress] = []
    for cb in db.get_category_budgets(active_only=True):
        if cb.cost_bucket not in ("FIXED", "VARIABLE", "LEAKAGE"):
            continue
        spent = round(category_spend.get(cb.category, 0.0), 2)
        limit = round(cb.monthly_limit, 2)
        remaining = round(limit - spent, 2)
        if limit > 0:
            utilization_pct = round((spent / limit) * 100.0, 2)
        else:
            utilization_pct = 100.0 if spent > 0 else 0.0
        category_progress.append(
            CategoryProgress(
                category=cb.category,
                emoji=cb.emoji,
                cost_bucket=cb.cost_bucket,
                spent=spent,
                monthly_limit=limit,
                remaining=remaining,
                utilization_pct=utilization_pct,
                status_color=_status_color(utilization_pct),
            )
        )
    category_progress.sort(
        key=lambda cp: (-cp.utilization_pct, -cp.spent, cp.category)
    )

    top_merchants = [
        MerchantSpend(
            merchant=name,
            total_spent=round(total, 2),
            tx_count=merchant_counts[name],
            category=merchant_cats[name].most_common(1)[0][0],
        )
        for name, total in sorted(
            merchant_totals.items(),
            key=lambda kv: (-kv[1], -merchant_counts[kv[0]], kv[0]),
        )[:5]
    ]

    daily_cumulative_spend: list[DailyBurnPoint] = []
    running_total = 0.0
    for day in range(1, days_in_month + 1):
        d_spend = round(daily_burn.get(day, 0.0), 2)
        running_total = round(running_total + d_spend, 2)
        daily_cumulative_spend.append(
            DailyBurnPoint(
                day=day,
                tx_date=f"{month_key}-{day:02d}",
                daily_spend=d_spend,
                cumulative_spend=running_total,
            )
        )

    return DashboardMetrics(
        month_key=month_key,
        monthly_income=round(config.monthly_income, 2),
        actual_inflow=actual_inflow,
        fixed_cost_baseline=round(config.fixed_cost_baseline, 2),
        variable_spending_cap=round(config.variable_spending_cap, 2),
        total_spending_cap=total_spending_cap,
        actual_fixed_burn=actual_fixed_burn,
        actual_variable_burn=actual_variable_burn,
        actual_leakage_burn=actual_leakage_burn,
        total_tax_fees=total_tax_fees,
        total_monthly_burn=total_monthly_burn,
        remaining_allowance=remaining_allowance,
        burn_utilization_pct=burn_utilization_pct,
        days_in_month=days_in_month,
        days_elapsed=days_elapsed,
        days_remaining=days_remaining,
        safe_daily_spend=safe_daily_spend,
        actual_daily_burn_pace=actual_daily_burn_pace,
        prev_month_daily_burn_pace=prev_month_daily_burn_pace,
        mom_pace_delta_pct=mom_pace_delta_pct,
        monthly_mmf_swept=monthly_mmf_swept,
        emergency_multiplier=config.emergency_multiplier,
        emergency_target=emergency_target,
        emergency_current=emergency_current,
        emergency_progress_pct=emergency_progress_pct,
        unallocated_cash_surplus=unallocated_cash_surplus,
        needs_review_count=needs_review_count,
        total_liquid_balance=total_liquid_balance,
        total_net_worth=total_net_worth,
        wallet_balances=wallet_balances,
        category_progress=category_progress,
        top_merchants=top_merchants,
        daily_cumulative_spend=daily_cumulative_spend,
        upcoming_bills=upcoming_bills,
        unpaid_bills_total=unpaid_bills_total,
    )

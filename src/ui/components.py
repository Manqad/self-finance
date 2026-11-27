"""Reusable Batwa-style UI components for Personal Treasury OS."""

from __future__ import annotations

import html
from typing import Iterable

import streamlit as st

from src.models import (
    AccountWallet,
    CategoryProgress,
    DashboardMetrics,
    RecurringStatus,
    TransactionRecord,
)

_STATUS_COLORS: dict[str, str] = {
    "green": "#10B981",
    "amber": "#F59E0B",
    "red": "#EF4444",
}

_WALLET_ACCENTS: dict[str, tuple[str, str]] = {
    "BANK": ("#3B82F6", "rgba(59, 130, 246, 0.14)"),
    "WALLET": ("#10B981", "rgba(16, 185, 129, 0.14)"),
    "CASH": ("#F59E0B", "rgba(245, 158, 11, 0.14)"),
    "INVESTMENT": ("#8B5CF6", "rgba(139, 92, 246, 0.14)"),
}


def format_pkr(amount: float, decimals: bool = False) -> str:
    """Format a PKR currency value cleanly with sign and thousands separators."""
    val = float(amount)
    if abs(val) < 0.005:
        val = 0.0
    sign = "-" if val < 0 else ""
    abs_val = abs(val)
    if decimals or abs(abs_val - round(abs_val)) >= 0.005:
        return f"{sign}Rs. {abs_val:,.2f}"
    return f"{sign}Rs. {abs_val:,.0f}"


def get_status_hex(color_name: str) -> str:
    """Return hex color for green/amber/red threshold states."""
    return _STATUS_COLORS.get(color_name, "#10B981")


def _burn_status_meta(
    utilization_pct: float,
    total_spending_cap: float = 1.0,
    total_monthly_burn: float = 0.0,
) -> tuple[str, str]:
    """Return (hex_color, status_label) for burn cap utilization."""
    if total_spending_cap <= 0:
        if total_monthly_burn > 0:
            return "#EF4444", "No Cap Set"
        return "#94A3B8", "No Cap Set • —"
    if utilization_pct < 75.0:
        return "#10B981", "On Track"
    if utilization_pct <= 95.0:
        return "#F59E0B", "Caution Zone"
    if utilization_pct > 100.0:
        return "#EF4444", "Cap Exceeded!"
    return "#EF4444", "Near Cap Limit"


def _join_html(parts: Iterable[str]) -> str:
    """Join HTML fragments flush-left so Streamlit never interprets indentation as Markdown code."""
    return "".join(line.strip() for line in parts if line is not None)


def render_section_header(title: str, subtitle: str = "") -> None:
    """Render a compact mobile-friendly section header."""
    safe_title = html.escape(title)
    sub_html = (
        f'<div class="ptos-section-sub">{html.escape(subtitle)}</div>'
        if subtitle
        else ""
    )
    st.markdown(
        f'<div class="ptos-section-head"><div class="ptos-section-title">{safe_title}</div>{sub_html}</div>',
        unsafe_allow_html=True,
    )


def render_wallet_carousel(
    wallets: list[AccountWallet],
    total_liquid: float,
    total_net_worth: float,
) -> None:
    """Render responsive Batwa-style cards for the 4 wallets plus liquid & net worth totals."""
    summary_html = _join_html(
        [
            '<div class="ptos-summary-strip">',
            '<div class="ptos-summary-pill">',
            '<span class="ptos-pill-label">💧 Liquid Cash</span>',
            f'<span class="ptos-pill-value">{html.escape(format_pkr(total_liquid))}</span>',
            "</div>",
            '<div class="ptos-summary-pill ptos-summary-pill-accent">',
            '<span class="ptos-pill-label">💎 Total Net Worth</span>',
            f'<span class="ptos-pill-value">{html.escape(format_pkr(total_net_worth))}</span>',
            "</div>",
            "</div>",
        ]
    )

    cards_html: list[str] = ['<div class="ptos-wallet-grid">']
    for wallet in wallets:
        accent, tint = _WALLET_ACCENTS.get(
            wallet.account_type, ("#10B981", "rgba(16, 185, 129, 0.14)")
        )
        bal_color = "#EF4444" if wallet.current_balance < 0 else "#F8FAFC"
        cards_html.append(
            _join_html(
                [
                    f'<div class="ptos-wallet-card" style="border-top: 3px solid {accent};">',
                    '<div class="ptos-wallet-top">',
                    f'<span class="ptos-wallet-icon">{html.escape(wallet.icon)}</span>',
                    f'<span class="ptos-wallet-badge" style="color: {accent}; background: {tint};">',
                    html.escape(wallet.account_type),
                    "</span>",
                    "</div>",
                    f'<div class="ptos-wallet-name">{html.escape(wallet.account_name)}</div>',
                    f'<div class="ptos-wallet-bal" style="color: {bal_color};">',
                    html.escape(format_pkr(wallet.current_balance)),
                    "</div>",
                    '<div class="ptos-wallet-sub">',
                    (
                        f"Synced {html.escape(wallet.balance_as_of[5:10])}"
                        if wallet.balance_as_of
                        else f"Base: {html.escape(format_pkr(wallet.opening_balance))}"
                    ),
                    "</div>",
                    "</div>",
                ]
            )
        )
    cards_html.append("</div>")

    st.markdown(summary_html + "".join(cards_html), unsafe_allow_html=True)


def render_burn_hero(metrics: DashboardMetrics) -> None:
    """Render Safe Daily Spend, Burn Cap gauge, and 6-month Emergency Reserve progress."""
    burn_hex, burn_label = _burn_status_meta(
        metrics.burn_utilization_pct,
        total_spending_cap=metrics.total_spending_cap,
        total_monthly_burn=metrics.total_monthly_burn,
    )
    if metrics.total_spending_cap <= 0:
        if metrics.total_monthly_burn > 0:
            burn_bar_w = 100.0
            burn_chip_text = burn_label
            burn_pct_text = "No Cap"
        else:
            burn_bar_w = 0.0
            burn_chip_text = burn_label
            burn_pct_text = "—"
    else:
        burn_bar_w = min(100.0, max(0.0, metrics.burn_utilization_pct))
        burn_chip_text = f"{burn_label} • {metrics.burn_utilization_pct:.1f}%"
        burn_pct_text = f"{metrics.burn_utilization_pct:.1f}%"

    em_bar_w = min(100.0, max(0.0, metrics.emergency_progress_pct))
    rem_color = "#EF4444" if metrics.remaining_allowance < 0 else "#10B981"
    safe_color = "#EF4444" if metrics.remaining_allowance < 0 else "#10B981"

    hero_html = _join_html(
        [
            '<div class="ptos-hero-card">',
            '<div class="ptos-hero-header">',
            '<div>',
            '<div class="ptos-hero-eyebrow">MONTH 1 FINANCIAL DEFENSE</div>',
            f'<div class="ptos-hero-title">Spend Pace &amp; Burn Gauge ({html.escape(metrics.month_key)})</div>',
            "</div>",
            f'<span class="ptos-status-chip" style="color: {burn_hex}; border-color: {burn_hex}55; background: {burn_hex}1A;">',
            html.escape(burn_chip_text),
            "</span>",
            "</div>",
            '<div class="ptos-kpi-grid">',
            '<div class="ptos-kpi-box">',
            '<div class="ptos-kpi-label">Safe Daily Spend</div>',
            f'<div class="ptos-kpi-value" style="color: {safe_color};">{html.escape(format_pkr(metrics.safe_daily_spend))}<span class="ptos-kpi-unit">/day</span></div>',
            f'<div class="ptos-kpi-sub">Pace: {html.escape(format_pkr(metrics.actual_daily_burn_pace))}/day</div>',
            "</div>",
            '<div class="ptos-kpi-box">',
            '<div class="ptos-kpi-label">Days Left in Month</div>',
            f'<div class="ptos-kpi-value">{metrics.days_remaining}<span class="ptos-kpi-unit"> days</span></div>',
            f'<div class="ptos-kpi-sub">Day {metrics.days_elapsed} of {metrics.days_in_month}</div>',
            "</div>",
            '<div class="ptos-kpi-box">',
            '<div class="ptos-kpi-label">Remaining Allowance</div>',
            f'<div class="ptos-kpi-value" style="color: {rem_color};">{html.escape(format_pkr(metrics.remaining_allowance))}</div>',
            f'<div class="ptos-kpi-sub">Surplus: {html.escape(format_pkr(metrics.unallocated_cash_surplus))}</div>',
            "</div>",
            '<div class="ptos-kpi-box">',
            '<div class="ptos-kpi-label">Total Burn vs Cap</div>',
            f'<div class="ptos-kpi-value">{html.escape(format_pkr(metrics.total_monthly_burn))}</div>',
            f'<div class="ptos-kpi-sub">Cap: {html.escape(format_pkr(metrics.total_spending_cap))}</div>',
            "</div>",
            "</div>",
            '<div class="ptos-progress-wrap">',
            '<div class="ptos-progress-meta">',
            f"<span>Monthly Burn Cap ({html.escape(format_pkr(metrics.total_monthly_burn))} / {html.escape(format_pkr(metrics.total_spending_cap))})</span>",
            f'<span style="color: {burn_hex}; font-weight: 700;">{html.escape(burn_pct_text)}</span>',
            "</div>",
            '<div class="ptos-bar-track">',
            f'<div class="ptos-bar-fill" style="width: {burn_bar_w:.1f}%; background: {burn_hex};"></div>',
            "</div>",
            '<div class="ptos-chip-row">',
            f'<span class="ptos-mini-chip">Fixed: {html.escape(format_pkr(metrics.actual_fixed_burn))}</span>',
            f'<span class="ptos-mini-chip">Variable: {html.escape(format_pkr(metrics.actual_variable_burn))}</span>',
            f'<span class="ptos-mini-chip ptos-mini-chip-red">Leakage &amp; Tax: {html.escape(format_pkr(metrics.actual_leakage_burn))}</span>',
            f'<span class="ptos-mini-chip ptos-mini-chip-purple">MMF Swept: {html.escape(format_pkr(metrics.monthly_mmf_swept))}</span>',
            "</div>",
            "</div>",
            '<div class="ptos-progress-wrap ptos-emergency-wrap">',
            '<div class="ptos-progress-meta">',
            f'<span>🛡️ {metrics.emergency_multiplier}-Mo Reserve ({html.escape(format_pkr(metrics.emergency_current))} / {html.escape(format_pkr(metrics.emergency_target))})</span>',
            f'<span style="color: #8B5CF6; font-weight: 700;">{metrics.emergency_progress_pct:.1f}%</span>',
            "</div>",
            '<div class="ptos-bar-track">',
            f'<div class="ptos-bar-fill" style="width: {em_bar_w:.1f}%; background: linear-gradient(90deg, #8B5CF6, #10B981);"></div>',
            "</div>",
            "</div>",
            "</div>",
        ]
    )
    st.markdown(hero_html, unsafe_allow_html=True)


def render_category_budget_bars(category_progress: list[CategoryProgress]) -> None:
    """Render Batwa-style horizontal category budget progress bars."""
    if not category_progress:
        st.info("No active category budgets configured. Manage category limits in 🎯 Budgets & Bills.")
        return

    rows: list[str] = ['<div class="ptos-cat-list">']
    for item in category_progress:
        bar_hex = get_status_hex(item.status_color)
        fill_w = min(100.0, max(0.0, item.utilization_pct))
        has_limit = item.monthly_limit > 0
        if has_limit:
            limit_text = f"{format_pkr(item.spent)} / {format_pkr(item.monthly_limit)}"
            rem_text = (
                f"Over by {format_pkr(abs(item.remaining))}"
                if item.remaining < 0
                else f"{format_pkr(item.remaining)} left"
            )
            if item.utilization_pct > 100.0:
                over_badge = '<span class="ptos-over-badge">Over Budget!</span>'
            elif item.utilization_pct > 95.0:
                over_badge = '<span class="ptos-near-badge">Near Limit</span>'
            else:
                over_badge = ""
            pct_label = f"{item.utilization_pct:.1f}%"
        else:
            limit_text = f"{format_pkr(item.spent)} / No limit"
            rem_text = "Unbudgeted category"
            over_badge = ""
            pct_label = "—"
            if item.spent == 0:
                fill_w = 0.0
                bar_hex = "#475569"

        rows.append(
            _join_html(
                [
                    '<div class="ptos-cat-card">',
                    '<div class="ptos-cat-top">',
                    '<div class="ptos-cat-title">',
                    f'<span class="ptos-cat-emoji">{html.escape(item.emoji)}</span>',
                    f'<span class="ptos-cat-name">{html.escape(item.category)}</span>',
                    f'<span class="ptos-bucket-pill">{html.escape(item.cost_bucket)}</span>',
                    over_badge,
                    "</div>",
                    f'<span class="ptos-cat-pct" style="color: {bar_hex};">{html.escape(pct_label)}</span>',
                    "</div>",
                    '<div class="ptos-bar-track ptos-bar-track-sm">',
                    f'<div class="ptos-bar-fill" style="width: {fill_w:.1f}%; background: {bar_hex};"></div>',
                    "</div>",
                    '<div class="ptos-cat-row-bottom">',
                    f'<span class="ptos-cat-spent">{html.escape(limit_text)}</span>',
                    f'<span class="ptos-cat-rem">{html.escape(rem_text)}</span>',
                    "</div>",
                    "</div>",
                ]
            )
        )
    rows.append("</div>")
    st.markdown("".join(rows), unsafe_allow_html=True)


def render_needs_review_banner(needs_review_count: int) -> None:
    """Render prominent alert banner when uncategorized transactions await review."""
    if needs_review_count > 0:
        banner_html = _join_html(
            [
                '<div class="ptos-review-banner ptos-review-alert">',
                '<div class="ptos-review-left">',
                '<span class="ptos-review-icon">📥</span>',
                '<div>',
                f'<div class="ptos-review-title">Transactions to Review ({needs_review_count})</div>',
                '<div class="ptos-review-sub">Unmatched payees waiting. Tag once to train auto-rules.</div>',
                "</div>",
                "</div>",
                '<span class="ptos-review-pill">Action Needed</span>',
                "</div>",
            ]
        )
    else:
        banner_html = _join_html(
            [
                '<div class="ptos-review-banner ptos-review-ok">',
                '<div class="ptos-review-left">',
                '<span class="ptos-review-icon">✅</span>',
                '<div>',
                '<div class="ptos-review-title">All caught up!</div>',
                '<div class="ptos-review-sub">0 transactions waiting in your Needs Review inbox.</div>',
                "</div>",
                "</div>",
                '<span class="ptos-review-pill ptos-review-pill-ok">Inbox Zero</span>',
                "</div>",
            ]
        )
    st.markdown(banner_html, unsafe_allow_html=True)


def render_upcoming_bills_widget(
    upcoming_bills: list[RecurringStatus],
    unpaid_bills_total: float,
) -> None:
    """Render monthly upcoming bills checklist with paid/unpaid status and unpaid total."""
    paid_count = sum(1 for item in upcoming_bills if item.is_paid)
    total_count = len(upcoming_bills)

    header_html = _join_html(
        [
            '<div class="ptos-bills-summary">',
            f'<span>{paid_count} / {total_count} bills settled</span>',
            f'<span class="ptos-bills-due-total">Unpaid Remaining: {html.escape(format_pkr(unpaid_bills_total))}</span>',
            "</div>",
        ]
    )

    if not upcoming_bills:
        st.markdown(header_html, unsafe_allow_html=True)
        st.info("No active recurring bills configured. Add commitments in Tab 3 (Budgets & Bills).")
        return

    items_html: list[str] = [header_html, '<div class="ptos-bills-list">']
    for status in upcoming_bills:
        c = status.commitment
        short_due = (
            status.due_date[5:]
            if status.due_date and len(status.due_date) >= 10
            else f"Day {c.due_day}"
        )
        if status.is_paid or status.status == "PAID":
            badge_cls = "ptos-bill-paid"
            short_dt = status.paid_date[5:] if status.paid_date and len(status.paid_date) >= 10 else status.paid_date
            badge_txt = f"✅ Paid {format_pkr(status.paid_amount)}" + (
                f" ({short_dt})" if short_dt else ""
            )
        elif status.status == "OVERDUE":
            badge_cls = "ptos-bill-overdue"
            badge_txt = f"⚠️ Overdue ({short_due})"
        elif status.status == "DUE_SOON":
            badge_cls = "ptos-bill-soon"
            badge_txt = f"🔔 Due Soon ({short_due})"
        else:
            badge_cls = "ptos-bill-due"
            badge_txt = f"📅 Due {short_due}"

        items_html.append(
            _join_html(
                [
                    '<div class="ptos-bill-row">',
                    '<div class="ptos-bill-info">',
                    f'<div class="ptos-bill-title">{html.escape(c.title)}</div>',
                    f'<div class="ptos-bill-meta">{html.escape(c.category)} • Expected {html.escape(format_pkr(c.expected_amount))}</div>',
                    "</div>",
                    f'<span class="ptos-bill-badge {badge_cls}">{html.escape(badge_txt)}</span>',
                    "</div>",
                ]
            )
        )
    items_html.append("</div>")
    st.markdown("".join(items_html), unsafe_allow_html=True)


def _build_tx_cards_html(txs: list[TransactionRecord]) -> str:
    """Build mobile-first fintech card markup for a list of transactions."""
    rows_html: list[str] = ['<div class="ptos-tx-feed">']
    for tx in txs:
        label = tx.merchant_alias or tx.payee
        wallet_flow = (
            f"{tx.account_name} → {tx.target_account}"
            if tx.target_account
            else tx.account_name
        )
        if tx.tx_type == "CREDIT":
            amt_str = f"+{format_pkr(tx.amount)}"
            amt_color = "#10B981"
        elif tx.tx_type == "DEBIT":
            amt_str = f"-{format_pkr(tx.amount + tx.tax_fee_amount)}"
            amt_color = "#F8FAFC"
        else:
            amt_str = f"⇄ {format_pkr(tx.amount)}"
            amt_color = "#A78BFA"

        is_pending = tx.review_status == "NEEDS_REVIEW"
        status_pill = (
            '<span class="ptos-tx-pill ptos-tx-pill-warn">⚠️ Needs Review</span>'
            if is_pending
            else ""
        )
        short_date = tx.tx_date[5:] if len(tx.tx_date) >= 10 else tx.tx_date
        tax_pill = (
            f'<span class="ptos-tx-pill">Fee {html.escape(format_pkr(tx.tax_fee_amount))}</span>'
            if tx.tax_fee_amount > 0
            else ""
        )
        rows_html.append(
            _join_html(
                [
                    '<div class="ptos-tx-card">',
                    '<div class="ptos-tx-card-top">',
                    f'<div class="ptos-tx-title">{html.escape(label)}</div>',
                    f'<div class="ptos-tx-amt" style="color: {amt_color};">{html.escape(amt_str)}</div>',
                    "</div>",
                    '<div class="ptos-tx-pills">',
                    f'<span class="ptos-tx-pill">{html.escape(short_date)}</span>',
                    f'<span class="ptos-tx-pill ptos-tx-pill-flow">{html.escape(wallet_flow)}</span>',
                    f'<span class="ptos-tx-pill">{html.escape(tx.category)} · {html.escape(tx.cost_bucket)}</span>',
                    tax_pill,
                    status_pill,
                    "</div>",
                    "</div>",
                ]
            )
        )
    rows_html.append("</div>")
    return "".join(rows_html)


def render_recent_transactions_strip(
    recent_txs: list[TransactionRecord],
    month_key: str,
) -> None:
    """Render a compact mobile-friendly strip of the latest transactions."""
    if not recent_txs:
        st.info(f"No transactions recorded in {month_key} yet.")
        return

    st.markdown(_build_tx_cards_html(recent_txs), unsafe_allow_html=True)


def render_mobile_transaction_cards(
    txs: list[TransactionRecord],
    max_items: int = 30,
) -> None:
    """Render a scrollable mobile-friendly card feed for filtered transactions."""
    if not txs:
        return

    visible = txs[:max_items]
    st.markdown(_build_tx_cards_html(visible), unsafe_allow_html=True)
    if len(txs) > max_items:
        st.caption(
            f"Showing {max_items} of {len(txs)} transactions above. Expand **Full Spreadsheet Table View** below for all rows."
        )

"""Tab 4 (Insights & Leakage): Responsive Plotly Analytics, 6-Month Trend, & Deduplicated Tax Table."""

from __future__ import annotations

import calendar
import html
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.database.db_manager import DatabaseManager
from src.engine.metrics import compute_dashboard_metrics
from src.models import WALLET_NAYAPAY, DashboardMetrics
from src.ui.components import format_pkr, render_section_header
from src.ui.views_common import plotly_base_layout

_PLOTLY_CONFIG = {"displayModeBar": False, "responsive": True}


def render_insights_tab(
    db: DatabaseManager,
    metrics: DashboardMetrics,
    month_key: str,
) -> None:
    """Render Tab 4: Burn split, 6-month trend, top payees, burn trajectory & FBR/FX leakage."""
    render_section_header(
        "📊 Insights & Leakage Analytics",
        f"Burn composition, multi-month trajectory, top payees & FBR/FX friction for {month_key}",
    )

    cfg = db.get_budget_config()
    if cfg.envelope_mode:
        st.info(
            "✉️ **NayaPay Envelope Mode is ON:** Hero Burn & the Fixed/Variable/Leakage split "
            "track `HabibMetro → NayaPay` top-ups (excluding granular NayaPay debits from cap burn & leakage), "
            "while category bars track granular NayaPay purchases."
        )

    with st.container(key="ptos_kpi_grid_t4"):
        k1, k2, k3, k4 = st.columns(4)
        k1.metric(
            "Total Leakage & Tax",
            format_pkr(metrics.actual_leakage_burn),
            delta=f"WHT/FX Fees: {format_pkr(metrics.total_tax_fees)}",
            delta_color="off",
        )
        k2.metric(
            "Daily Burn Pace",
            f"{format_pkr(metrics.actual_daily_burn_pace)}/day",
            delta=(
                f"{metrics.mom_pace_delta_pct:+.1f}% vs prev mo"
                if metrics.prev_month_daily_burn_pace > 0
                else "No prior mo baseline"
            ),
            delta_color="inverse" if metrics.prev_month_daily_burn_pace > 0 else "off",
        )
        k3.metric(
            "Unallocated Cash Surplus",
            format_pkr(metrics.unallocated_cash_surplus),
            delta=f"MMF Swept: {format_pkr(metrics.monthly_mmf_swept)}",
            delta_color="off",
        )
        k4.metric(
            "Actual Inflow",
            format_pkr(metrics.actual_inflow),
            delta=f"Target: {format_pkr(metrics.monthly_income)}",
            delta_color="off",
        )

    # Row 1: Fixed/Variable/Leakage Split + Top 5 Payees
    c_left, c_right = st.columns(2, gap="medium")
    with c_left:
        split_values = [
            metrics.actual_fixed_burn,
            metrics.actual_variable_burn,
            metrics.actual_leakage_burn,
        ]
        if sum(split_values) <= 0:
            st.info(f"No debit burn recorded in {month_key} yet for Fixed/Variable/Leakage split.")
        else:
            fig_pie = go.Figure(
                data=[
                    go.Pie(
                        labels=["Fixed Baseline", "Variable Spend", "Tax & Fee Leakage"],
                        values=split_values,
                        hole=0.58,
                        marker={
                            "colors": ["#3B82F6", "#10B981", "#EF4444"],
                            "line": {"color": "#090E1A", "width": 2},
                        },
                        textinfo="percent",
                        textposition="inside",
                        hovertemplate="%{label}: Rs. %{value:,.0f} (%{percent})<extra></extra>",
                    )
                ]
            )
            pie_layout = plotly_base_layout("Fixed vs Variable vs Leakage Split", height=315)
            pie_layout["margin"] = {"l": 8, "r": 8, "t": 44, "b": 46}
            fig_pie.update_layout(**pie_layout)
            st.plotly_chart(fig_pie, width="stretch", config=_PLOTLY_CONFIG)

    with c_right:
        if not metrics.top_merchants:
            st.info(f"No merchant debit data recorded in {month_key} yet for Top 5 Payees.")
        else:
            ordered_m = list(reversed(metrics.top_merchants))
            short_names = [
                (m.merchant[:14] + "…") if len(m.merchant) > 15 else m.merchant
                for m in ordered_m
            ]
            fig_m = go.Figure(
                data=[
                    go.Bar(
                        x=[m.total_spent for m in ordered_m],
                        y=short_names,
                        customdata=[m.merchant for m in ordered_m],
                        orientation="h",
                        marker={"color": "#10B981", "borderRadius": 6} if False else {"color": "#10B981"},
                        text=[format_pkr(m.total_spent) for m in ordered_m],
                        textposition="auto",
                        hovertemplate="%{customdata}: %{text}<extra></extra>",
                    )
                ]
            )
            m_layout = plotly_base_layout(f"Top {len(ordered_m)} Payees ({month_key})", height=315)
            m_layout["margin"] = {"l": 108, "r": 18, "t": 44, "b": 24}
            m_layout["xaxis"] = {"showgrid": True, "gridcolor": "rgba(148,163,184,0.12)"}
            m_layout["yaxis"] = {"automargin": True}
            fig_m.update_layout(**m_layout)
            st.plotly_chart(fig_m, width="stretch", config=_PLOTLY_CONFIG)

    # Row 2: Cumulative Daily Trajectory + 6-Month Cashflow Trend
    t_left, t_right = st.columns(2, gap="medium")
    month_txs = db.get_transactions(month_key=month_key)

    with t_left:
        year_s, mon_s = month_key.split("-")
        days_n = calendar.monthrange(int(year_s), int(mon_s))[1]
        day_labels = [f"{month_key}-{d:02d}" for d in range(1, days_n + 1)]
        daily_spend = [0.0] * days_n
        for tx in month_txs:
            if tx.tx_type == "DEBIT" or (
                cfg.envelope_mode
                and tx.tx_type == "TRANSFER_INTERNAL"
                and tx.target_account == WALLET_NAYAPAY
            ):
                if cfg.envelope_mode and tx.tx_type == "DEBIT" and tx.account_name == WALLET_NAYAPAY:
                    continue
                if len(tx.tx_date) == 10 and tx.tx_date.startswith(month_key):
                    try:
                        d_i = int(tx.tx_date[8:10]) - 1
                        if 0 <= d_i < days_n:
                            amt = (
                                tx.amount + tx.tax_fee_amount
                                if tx.tx_type == "DEBIT"
                                else tx.amount + tx.tax_fee_amount
                            )
                            daily_spend[d_i] += amt
                    except ValueError:
                        pass
        cum_spend = []
        running = 0.0
        for v in daily_spend:
            running += v
            cum_spend.append(round(running, 2))
        cap_pace = [
            round(metrics.total_spending_cap * ((i + 1) / days_n), 2)
            for i in range(days_n)
        ]

        fig_cum = go.Figure()
        fig_cum.add_trace(
            go.Scatter(
                x=day_labels,
                y=cum_spend,
                mode="lines+markers",
                name="Cumulative Burn",
                line={"color": "#10B981", "width": 3},
                marker={"size": 4},
                fill="tozeroy",
                fillcolor="rgba(16, 185, 129, 0.12)",
            )
        )
        if metrics.total_spending_cap > 0:
            fig_cum.add_trace(
                go.Scatter(
                    x=day_labels,
                    y=cap_pace,
                    mode="lines",
                    name="Linear Cap Pace",
                    line={"color": "#94A3B8", "width": 1.6, "dash": "dash"},
                )
            )
        c_layout = plotly_base_layout("Cumulative Daily Spend vs Pace", height=300)
        c_layout["xaxis"] = {"nticks": 6, "showgrid": False}
        c_layout["yaxis"] = {"gridcolor": "rgba(148,163,184,0.12)"}
        fig_cum.update_layout(**c_layout)
        st.plotly_chart(fig_cum, width="stretch", config=_PLOTLY_CONFIG)

    with t_right:
        month_counts = db.get_month_transaction_counts()
        trend_months = sorted([m for m, cnt in month_counts.items() if cnt > 0])[-6:]
        if month_key not in trend_months:
            trend_months = (trend_months + [month_key])[-6:]
        trend_burns = []
        trend_inflows = []
        for mk in trend_months:
            mm = compute_dashboard_metrics(db, mk)
            trend_burns.append(mm.total_monthly_burn)
            trend_inflows.append(mm.actual_inflow)

        fig_tr = go.Figure()
        fig_tr.add_trace(
            go.Bar(x=trend_months, y=trend_inflows, name="Inflow", marker_color="#3B82F6")
        )
        fig_tr.add_trace(
            go.Bar(x=trend_months, y=trend_burns, name="Total Burn", marker_color="#F59E0B")
        )
        tr_layout = plotly_base_layout("Multi-Month Inflow vs Burn", height=300)
        tr_layout["barmode"] = "group"
        tr_layout["yaxis"] = {"gridcolor": "rgba(148,163,184,0.12)"}
        fig_tr.update_layout(**tr_layout)
        st.plotly_chart(fig_tr, width="stretch", config=_PLOTLY_CONFIG)

    # Row 3: Deduplicated Tax, WHT & Bank Fee Leakage Ledger
    render_section_header(
        "💸 Tax, WHT & Bank Fee Leakage Ledger",
        "Every rupee lost to FBR withholding tax, FX conversion fees, and bank charges",
    )
    leakage_txs = [
        tx
        for tx in month_txs
        if tx.tx_type != "CREDIT"
        and not (
            cfg.envelope_mode
            and tx.tx_type == "DEBIT"
            and tx.account_name == WALLET_NAYAPAY
        )
        and (tx.cost_bucket == "LEAKAGE" or tx.tax_fee_amount > 0.0)
    ]
    if not leakage_txs:
        st.success(f"Zero tax or bank fee leakage recorded in {month_key}!")
        return

    leak_rows = []
    for tx in leakage_txs:
        direct_leak = (
            tx.amount
            if (tx.tx_type == "DEBIT" and tx.cost_bucket == "LEAKAGE")
            else 0.0
        )
        leak_rows.append(
            {
                "tx_date": tx.tx_date,
                "account_name": tx.account_name,
                "payee": tx.merchant_alias or tx.payee,
                "category": tx.category,
                "base_amount": float(tx.amount),
                "tax_fee_amount": float(tx.tax_fee_amount),
                "total_leakage_impact": round(direct_leak + tx.tax_fee_amount, 2),
            }
        )
    leak_sum = round(sum(r["total_leakage_impact"] for r in leak_rows), 2)
    st.caption(
        f"Itemized leakage total for {month_key}: **{format_pkr(leak_sum)}** "
        f"across {len(leak_rows)} transaction(s)."
    )

    card_parts = ['<div class="ptos-tx-feed">']
    for r in leak_rows[:12]:
        payee_esc = html.escape(str(r["payee"]))
        dt_s = str(r["tx_date"])
        date_esc = html.escape(dt_s[5:] if len(dt_s) >= 10 else dt_s)
        wallet_esc = html.escape(str(r["account_name"]))
        cat_esc = html.escape(str(r["category"]))
        amt_esc = html.escape(format_pkr(float(r["total_leakage_impact"])))
        tax_fee = float(r["tax_fee_amount"])
        base_amt = float(r["base_amount"])
        split_note = (
            f'<span class="ptos-tx-pill" style="color: #FCA5A5;">Tax/Fee: {html.escape(format_pkr(tax_fee))}</span>'
            if tax_fee > 0 and base_amt > 0
            else ""
        )
        card_parts.append(
            f'<div class="ptos-tx-card" style="border-left: 3px solid #EF4444;">'
            f'<div class="ptos-tx-card-top">'
            f'<div class="ptos-tx-title">{payee_esc}</div>'
            f'<div class="ptos-tx-amt" style="color: #EF4444;">-{amt_esc}</div>'
            f'</div>'
            f'<div class="ptos-tx-pills">'
            f'<span class="ptos-tx-pill">{date_esc}</span>'
            f'<span class="ptos-tx-pill ptos-tx-pill-flow">{wallet_esc}</span>'
            f'<span class="ptos-tx-pill">{cat_esc}</span>'
            f'{split_note}'
            f'</div>'
            f'</div>'
        )
    card_parts.append("</div>")
    st.markdown("".join(card_parts), unsafe_allow_html=True)

    st.dataframe(
        pd.DataFrame(leak_rows),
        width="stretch",
        hide_index=True,
        column_config={
            "tx_date": st.column_config.TextColumn("Date", width="small"),
            "account_name": st.column_config.TextColumn("Wallet", width="small"),
            "payee": st.column_config.TextColumn("Payee / Merchant", width="medium"),
            "category": st.column_config.TextColumn("Category", width="small"),
            "base_amount": st.column_config.NumberColumn("Base (PKR)", format="accounting"),
            "tax_fee_amount": st.column_config.NumberColumn("Tax/FX Fee (PKR)", format="accounting"),
            "total_leakage_impact": st.column_config.NumberColumn("Leakage Impact (PKR)", format="accounting"),
        },
    )

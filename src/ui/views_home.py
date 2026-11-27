"""Tab 1 (Home): Wallets Overview, Month 1 Burn Hero, Actionable Review, Budgets & Interactive Bills."""

from __future__ import annotations

import html
from typing import Optional

import streamlit as st

from src.database.db_manager import DatabaseManager
from src.models import WALLET_MMF, CostBucket, DashboardMetrics, TxType
from src.ui.components import (
    format_pkr,
    render_burn_hero,
    render_category_budget_bars,
    render_needs_review_banner,
    render_section_header,
    render_wallet_carousel,
)
from src.ui.state import (
    TAB_BUDGETS,
    TAB_TRANSACTIONS,
    _TX_TYPES,
    invalidate_all_caches,
    navigate_to_tab,
    notify_success,
    open_dialog,
)
from src.ui.views_common import (
    apply_pending_widget_sync,
    buckets_for_tx_type,
    categories_for_tx_type,
    default_rule_keyword_for_tx,
    default_transfer_category,
    format_bucket,
    format_tx_type,
    get_category_catalog,
    internal_transfer_targets,
    render_inline_category_creator,
    save_tx_and_maybe_learn,
    sync_bucket_on_cat_change,
)


def render_home_tab(
    db: DatabaseManager,
    metrics: DashboardMetrics,
    month_key: str,
) -> None:
    """Render Tab 1: Executive overview with wallets, Month 1 defense hero, review CTA, budgets, bills, and recents."""
    apply_pending_widget_sync()

    render_wallet_carousel(
        wallets=metrics.wallet_balances,
        total_liquid=metrics.total_liquid_balance,
        total_net_worth=metrics.total_net_worth,
    )
    render_burn_hero(metrics)

    global_pending = db.get_needs_review_count()
    month_pending = metrics.needs_review_count
    render_needs_review_banner(global_pending)

    if global_pending > 0:
        with st.container(key="ptos_compact_t1_rev"):
            c_rev, c_add = st.columns([1.4, 0.8])
            with c_rev:
                scope_label = (
                    f"🔍 Review Pending ({global_pending}) →"
                    if month_pending > 0
                    else f"🔍 Review Earlier Months ({global_pending}) →"
                )
                if st.button(scope_label, key="t1_go_review_btn", type="primary", width="stretch"):
                    navigate_to_tab(
                        TAB_TRANSACTIONS,
                        needs_review_only=True,
                        all_months=(month_pending == 0),
                        quick_filter="Needs Review",
                    )
            with c_add:
                if st.button("➕ Add Transaction", key="t1_home_add_btn", width="stretch"):
                    open_dialog("quick_add")
                    st.rerun()

        # Show concise inline Quick-Tag card for top pending transaction in active month
        pending_txs = db.get_transactions(month_key=month_key, review_status="NEEDS_REVIEW")
        if pending_txs:
            top_tx = pending_txs[0]
            tid = top_tx.id or 0
            wallets = [w.account_name for w in db.get_wallets()]
            categories, bucket_map, _ = get_category_catalog(db)

            with st.expander(
                f"⚡ Quick-Tag: {top_tx.payee[:42]} · {format_pkr(top_tx.amount)}",
                expanded=(month_pending <= 2),
            ):
                qt_acct = top_tx.account_name
                type_opts = [
                    t for t in _TX_TYPES if not (qt_acct == WALLET_MMF and t == "TRANSFER_MMF")
                ]
                qt_type_key = f"t1_qt_type_{tid}"
                if qt_type_key not in st.session_state or st.session_state[qt_type_key] not in type_opts:
                    st.session_state[qt_type_key] = (
                        top_tx.tx_type if top_tx.tx_type in type_opts else type_opts[0]
                    )

                qt_type: TxType = st.selectbox(  # type: ignore[assignment]
                    "Transaction Type",
                    options=type_opts,
                    format_func=format_tx_type,
                    key=qt_type_key,
                )

                qt_target: Optional[str] = None
                if qt_type == "TRANSFER_INTERNAL":
                    tgt_opts = internal_transfer_targets(wallets, qt_acct)
                    qt_tgt_key = f"t1_qt_target_{tid}"
                    if tgt_opts:
                        if qt_tgt_key not in st.session_state or st.session_state[qt_tgt_key] not in tgt_opts:
                            st.session_state[qt_tgt_key] = (
                                top_tx.target_account
                                if top_tx.target_account in tgt_opts
                                else tgt_opts[0]
                            )
                        qt_target = st.selectbox("Target Wallet", options=tgt_opts, key=qt_tgt_key)
                elif qt_type == "TRANSFER_MMF":
                    qt_target = WALLET_MMF

                cat_opts = categories_for_tx_type(
                    categories, bucket_map, qt_type, current_category=top_tx.category
                )
                qt_cat_key = f"t1_qt_cat_{tid}"
                qt_bkt_key = f"t1_qt_bkt_{tid}"
                if qt_cat_key not in st.session_state or st.session_state[qt_cat_key] not in cat_opts:
                    st.session_state[qt_cat_key] = (
                        top_tx.category
                        if top_tx.category in cat_opts
                        else (
                            default_transfer_category(qt_acct, qt_target, cat_opts)
                            if qt_type == "TRANSFER_INTERNAL"
                            else cat_opts[0]
                        )
                    )
                    sync_bucket_on_cat_change(qt_cat_key, qt_bkt_key, bucket_map, qt_type)

                c_a, c_k = st.columns(2)
                with c_a:
                    qt_alias = st.text_input(
                        "Friendly Alias",
                        value=top_tx.merchant_alias or "",
                        placeholder="e.g., Zameen Coffee",
                        key=f"t1_qt_alias_{tid}",
                    )
                with c_k:
                    qt_kw = st.text_input(
                        "Rule Keyword",
                        value=default_rule_keyword_for_tx(db, top_tx),
                        key=f"t1_qt_kw_{tid}",
                    )

                c_c, c_b = st.columns(2)
                with c_c:
                    qt_cat = st.selectbox(
                        "Category",
                        options=cat_opts,
                        key=qt_cat_key,
                        on_change=sync_bucket_on_cat_change,
                        args=(qt_cat_key, qt_bkt_key, bucket_map, qt_type),
                    )
                with c_b:
                    allowed_bkts = buckets_for_tx_type(qt_type)
                    if qt_bkt_key not in st.session_state or st.session_state[qt_bkt_key] not in allowed_bkts:
                        fb_bkt = bucket_map.get(qt_cat, allowed_bkts[0])
                        st.session_state[qt_bkt_key] = (
                            top_tx.cost_bucket
                            if top_tx.cost_bucket in allowed_bkts
                            else (fb_bkt if fb_bkt in allowed_bkts else allowed_bkts[0])
                        )
                    qt_bkt: CostBucket = st.selectbox(  # type: ignore[assignment]
                        "Cost Bucket",
                        options=allowed_bkts,
                        format_func=format_bucket,
                        disabled=(len(allowed_bkts) == 1),
                        key=qt_bkt_key,
                    )

                render_inline_category_creator(
                    db,
                    key_prefix=f"t1_qt_nc_{tid}",
                    tx_type=qt_type,
                    target_cat_key=qt_cat_key,
                    target_bkt_key=qt_bkt_key,
                )

                remember = st.checkbox(
                    "Remember rule for future receipts",
                    value=False,
                    key=f"t1_qt_mem_{tid}",
                )
                with st.container(key=f"ptos_compact_t1_qt_act_{tid}"):
                    b1, b2 = st.columns([1.4, 0.8])
                    with b1:
                        if st.button("✅ Verify & Train", key=f"t1_qt_save_{tid}", type="primary", width="stretch"):
                            try:
                                _, aff = save_tx_and_maybe_learn(
                                    db,
                                    tid,
                                    payee=top_tx.payee,
                                    merchant_alias=qt_alias,
                                    category=qt_cat,
                                    cost_bucket=qt_bkt,
                                    tx_type=qt_type,
                                    target_account=qt_target,
                                    include_target_account=True,
                                    learn_rule=bool(remember),
                                    rule_keyword=qt_kw,
                                    overwrite_verified=False,
                                )
                            except ValueError as exc:
                                st.error(str(exc))
                            else:
                                invalidate_all_caches()
                                notify_success(f"Verified #{tid} ({qt_cat}); {aff} matching row(s) updated.")
                                st.rerun()
                    with b2:
                        if st.button("✏️ Full Edit", key=f"t1_qt_full_{tid}", width="stretch"):
                            open_dialog("edit_tx", tx_id=tid)
                            st.rerun()

    col_cats, col_bills = st.columns([1.25, 1.0], gap="medium")
    with col_cats:
        render_section_header(
            "📊 Category Budgets",
            f"Monthly spending pace for {month_key}",
        )
        render_category_budget_bars(metrics.category_progress[:6])
        if len(metrics.category_progress) > 6:
            with st.expander(f"Show all {len(metrics.category_progress)} categories", expanded=False):
                render_category_budget_bars(metrics.category_progress[6:])
        if st.button("🎯 Manage All Budgets →", key="t1_go_budgets", width="stretch"):
            navigate_to_tab(TAB_BUDGETS)

    with col_bills:
        render_section_header(
            "📅 Bills Radar",
            f"{sum(1 for b in metrics.upcoming_bills if b.is_paid)} / {len(metrics.upcoming_bills)} settled • "
            f"Unpaid: {format_pkr(metrics.unpaid_bills_total)}",
        )
        if not metrics.upcoming_bills:
            st.info("No active recurring bills yet. Add bills in 🎯 Budgets.")
        else:
            for b_status in metrics.upcoming_bills[:6]:
                c = b_status.commitment
                cid = c.id or 0
                short_due = (
                    b_status.due_date[5:]
                    if b_status.due_date and len(b_status.due_date) >= 10
                    else f"Day {c.due_day}"
                )
                if b_status.is_paid or b_status.status == "PAID":
                    pill = f'<span class="ptos-bill-badge ptos-bill-paid">✅ Paid {html.escape(format_pkr(b_status.paid_amount))}</span>'
                elif b_status.status == "OVERDUE":
                    pill = f'<span class="ptos-bill-badge ptos-bill-overdue">⚠️ Overdue ({html.escape(short_due)})</span>'
                elif b_status.status == "DUE_SOON":
                    pill = f'<span class="ptos-bill-badge ptos-bill-soon">🔔 Due {html.escape(short_due)}</span>'
                else:
                    pill = f'<span class="ptos-bill-badge ptos-bill-due">📅 Due {html.escape(short_due)}</span>'

                with st.container(border=True):
                    if not b_status.is_paid and cid > 0:
                        with st.container(key=f"ptos_compact_t1_bill_{cid}"):
                            b_info, b_pay = st.columns([1.65, 0.7], vertical_alignment="center")
                            with b_info:
                                st.markdown(
                                    f"**{html.escape(c.title)}** &nbsp; {pill}<br>"
                                    f"<span style='font-size:0.75rem;color:#94A3B8;'>{html.escape(c.category)} • {html.escape(format_pkr(c.expected_amount))}</span>",
                                    unsafe_allow_html=True,
                                )
                            with b_pay:
                                if st.button("💸 Pay", key=f"t1_bill_pay_{cid}", width="stretch"):
                                    open_dialog("pay_bill", commitment_id=cid, month_key=month_key)
                                    st.rerun()
                    else:
                        st.markdown(
                            f"**{html.escape(c.title)}** &nbsp; {pill}<br>"
                            f"<span style='font-size:0.75rem;color:#94A3B8;'>{html.escape(c.category)} • Expected {html.escape(format_pkr(c.expected_amount))}</span>",
                            unsafe_allow_html=True,
                        )
        if st.button("📅 Manage Recurring Bills →", key="t1_go_bills", width="stretch"):
            navigate_to_tab(TAB_BUDGETS)

    render_section_header(
        "🧾 Recent Activity",
        f"Latest transactions in {month_key} (click ✏️ to edit or train rule)",
    )
    month_txs = db.get_transactions(month_key=month_key)[:6]
    if not month_txs:
        st.info(f"No transactions recorded in {month_key} yet.")
    else:
        for tx in month_txs:
            tid = tx.id or 0
            label = tx.merchant_alias or tx.payee
            flow = f"{tx.account_name} → {tx.target_account}" if tx.target_account else tx.account_name
            if tx.tx_type == "CREDIT":
                amt_txt = f"+{format_pkr(tx.amount)}"
                amt_col = "#10B981"
            elif tx.tx_type == "DEBIT":
                amt_txt = f"-{format_pkr(tx.amount + tx.tax_fee_amount)}"
                amt_col = "#F8FAFC"
            else:
                amt_txt = f"⇄ {format_pkr(tx.amount)}"
                amt_col = "#A78BFA"
            short_dt = tx.tx_date[5:] if len(tx.tx_date) >= 10 else tx.tx_date
            warn_tag = " · ⚠️ Needs Review" if tx.review_status == "NEEDS_REVIEW" else ""

            with st.container(key=f"ptos_compact_t1_rec_{tid}"):
                r_left, r_btn = st.columns([4.2, 0.85], vertical_alignment="center")
                with r_left:
                    st.markdown(
                        f'<div class="ptos-tx-card">'
                        f'<div class="ptos-tx-card-top">'
                        f'<div class="ptos-tx-title">{html.escape(label)}</div>'
                        f'<div class="ptos-tx-amt" style="color:{amt_col};">{html.escape(amt_txt)}</div>'
                        f'</div>'
                        f'<div class="ptos-tx-pills">'
                        f'<span class="ptos-tx-pill">{html.escape(short_dt)}</span>'
                        f'<span class="ptos-tx-pill ptos-tx-pill-flow">{html.escape(flow)}</span>'
                        f'<span class="ptos-tx-pill">{html.escape(tx.category)}{html.escape(warn_tag)}</span>'
                        f'</div>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )
                with r_btn:
                    if st.button("✏️ Edit", key=f"t1_rec_edit_{tid}", width="stretch"):
                        open_dialog("edit_tx", tx_id=tid)
                        st.rerun()

        if st.button("📋 Open Full Ledger →", key="t1_go_ledger", width="stretch"):
            navigate_to_tab(TAB_TRANSACTIONS)

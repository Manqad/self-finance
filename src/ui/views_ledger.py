"""Tab 2 (Ledger): Pill Filters, Rapid Review Queue, Transfer Pair Reconciler, Interactive Table/Cards."""

from __future__ import annotations

import html
from typing import Any, Optional

import pandas as pd
import streamlit as st

from src.database.db_manager import DatabaseManager
from src.models import CostBucket, TransactionRecord, TxType
from src.ui.components import (
    format_pkr,
    render_mobile_transaction_cards,
    render_section_header,
)
from src.ui.state import (
    _COST_BUCKETS,
    invalidate_all_caches,
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
    get_category_catalog,
    internal_transfer_targets,
    month_date_bounds,
    save_tx_and_maybe_learn,
    sync_bucket_on_cat_change,
)

_QUICK_PILLS = ("All", "Needs Review", "Expenses", "Income", "Transfers", "Leakage")


def _apply_pending_ledger_nav(month_key: str) -> None:
    """Apply cross-tab navigation state before Ledger filter widgets instantiate."""
    if "ptos_pending_quick_filter" in st.session_state:
        pill = st.session_state.pop("ptos_pending_quick_filter")
        if pill in _QUICK_PILLS:
            st.session_state["t2_quick_pill"] = pill
    if "ptos_pending_review_only" in st.session_state:
        flag = bool(st.session_state.pop("ptos_pending_review_only"))
        st.session_state["t2_quick_pill"] = "Needs Review" if flag else "All"
    if "ptos_pending_all_months" in st.session_state:
        all_m = bool(st.session_state.pop("ptos_pending_all_months"))
        st.session_state["t2_period_mode"] = "all_months" if all_m else "selected_month"

    # Normalize period mode safely
    raw_pm = st.session_state.get("t2_period_mode")
    valid_pm = ("selected_month", "all_months", "custom")
    if raw_pm not in valid_pm:
        st.session_state["t2_period_mode"] = "selected_month"


def _reset_ledger_filters(month_key: str) -> None:
    """Reset all Ledger filter keys cleanly."""
    st.session_state["t2_quick_pill"] = "All"
    st.session_state["t2_search"] = ""
    st.session_state["t2_wallet"] = "All Wallets"
    st.session_state["t2_category"] = "All Categories"
    st.session_state["t2_bucket"] = "All Buckets"
    st.session_state["t2_period_mode"] = "selected_month"
    m_start, m_end = month_date_bounds(month_key)
    st.session_state[f"t2_d_start_{month_key}"] = m_start
    st.session_state[f"t2_d_end_{month_key}"] = m_end


def render_transactions_tab(db: DatabaseManager, month_key: str) -> None:
    """Render Tab 2: Clean pill filters, rapid review queue, transfer reconciler & click-to-edit ledger."""
    apply_pending_widget_sync()
    _apply_pending_ledger_nav(month_key)

    render_section_header(
        "📋 Transactions & Rapid Review",
        "Click any pill to slice your ledger, resolve transfers, or tap any row to edit",
    )

    wallets = [w.account_name for w in db.get_wallets()]
    categories, bucket_map, _ = get_category_catalog(db)
    all_filter_cats, _, _ = get_category_catalog(db, include_archived=True)

    # 1. Quick filter pills + Reset
    st.session_state.setdefault("t2_quick_pill", "All")
    with st.container(key="ptos_ledger_pills"):
        pill_choice = st.pills(
            "Quick Filter",
            options=list(_QUICK_PILLS),
            selection_mode="single",
            key="t2_quick_pill",
            label_visibility="collapsed",
        )
    active_pill = pill_choice or "All"

    # 2. Compact primary search & scope bar
    s_col, p_col, f_col, r_col = st.columns([1.8, 1.15, 0.9, 0.55], vertical_alignment="bottom")
    with s_col:
        search_q = st.text_input(
            "Search Payee / Alias / Notes",
            placeholder="Search merchant, payee, notes...",
            key="t2_search",
        )
    with p_col:
        period_mode = st.selectbox(
            "Date Scope",
            options=["selected_month", "all_months", "custom"],
            format_func=lambda m: {
                "selected_month": f"Month ({month_key})",
                "all_months": "All Months",
                "custom": "Custom Dates",
            }.get(m, m),
            key="t2_period_mode",
        )
    with f_col:
        with st.popover("🎛️ Filters", width="stretch"):
            wallet_sel = st.selectbox(
                "Wallet",
                options=["All Wallets"] + wallets,
                key="t2_wallet",
            )
            cat_sel = st.selectbox(
                "Category",
                options=["All Categories"] + all_filter_cats,
                key="t2_category",
            )
            bucket_sel = st.selectbox(
                "Cost Bucket",
                options=["All Buckets"] + list(_COST_BUCKETS),
                format_func=lambda b: "All Buckets" if b == "All Buckets" else format_bucket(b),
                key="t2_bucket",
            )
            m_start, m_end = month_date_bounds(month_key)
            d_col1, d_col2 = st.columns(2)
            with d_col1:
                d_start = st.date_input(
                    "From Date",
                    value=m_start,
                    key=f"t2_d_start_{month_key}",
                )
            with d_col2:
                d_end = st.date_input(
                    "To Date",
                    value=m_end,
                    key=f"t2_d_end_{month_key}",
                )
    with r_col:
        has_active_filters = bool(
            active_pill != "All"
            or search_q.strip()
            or st.session_state.get("t2_wallet", "All Wallets") != "All Wallets"
            or st.session_state.get("t2_category", "All Categories") != "All Categories"
            or st.session_state.get("t2_bucket", "All Buckets") != "All Buckets"
            or period_mode != "selected_month"
        )
        if st.button("↺ Reset", key="t2_reset_filters_btn", disabled=not has_active_filters, width="stretch"):
            _reset_ledger_filters(month_key)
            st.rerun()

    wallet_sel = st.session_state.get("t2_wallet", "All Wallets")
    cat_sel = st.session_state.get("t2_category", "All Categories")
    bucket_sel = st.session_state.get("t2_bucket", "All Buckets")

    filter_month: Optional[str] = None
    start_str: Optional[str] = None
    end_str: Optional[str] = None
    if period_mode == "selected_month":
        filter_month = month_key
    elif period_mode == "custom":
        d_s = st.session_state.get(f"t2_d_start_{month_key}", month_date_bounds(month_key)[0])
        d_e = st.session_state.get(f"t2_d_end_{month_key}", month_date_bounds(month_key)[1])
        if d_s > d_e:
            st.warning("From Date is after To Date; swapping bounds for query.")
            d_s, d_e = d_e, d_s
        start_str = d_s.strftime("%Y-%m-%d")
        end_str = d_e.strftime("%Y-%m-%d")

    pill_review = "NEEDS_REVIEW" if active_pill == "Needs Review" else None
    pill_bucket: Optional[CostBucket] = (
        "LEAKAGE"
        if active_pill == "Leakage"
        else ("INFLOW" if active_pill == "Income" else (None if bucket_sel == "All Buckets" else bucket_sel))  # type: ignore[assignment]
    )

    filtered_txs = db.get_transactions(
        month_key=filter_month,
        account_name=None if wallet_sel == "All Wallets" else wallet_sel,
        category=None if cat_sel == "All Categories" else cat_sel,
        cost_bucket=pill_bucket,
        review_status=pill_review,
        search_query=search_q or None,
        start_date=start_str,
        end_date=end_str,
        include_incoming_transfers=(wallet_sel != "All Wallets"),
    )
    if active_pill == "Expenses":
        filtered_txs = [t for t in filtered_txs if t.tx_type == "DEBIT"]
    elif active_pill == "Transfers":
        filtered_txs = [t for t in filtered_txs if t.tx_type in ("TRANSFER_INTERNAL", "TRANSFER_MMF")]

    total_debits = sum(
        tx.amount + tx.tax_fee_amount for tx in filtered_txs if tx.tx_type == "DEBIT"
    )
    total_credits = sum(tx.amount for tx in filtered_txs if tx.tx_type == "CREDIT")
    pending_in_view = [t for t in filtered_txs if t.review_status == "NEEDS_REVIEW"]

    with st.container(key="ptos_kpi_grid_t2"):
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Matching Rows", f"{len(filtered_txs)}")
        m2.metric("Filtered Debits", format_pkr(total_debits))
        m3.metric("Filtered Credits", format_pkr(total_credits))
        m4.metric("Needs Review", f"{len(pending_in_view)}")

    # 3. Internal Transfer Pair Reconciler
    transfer_pairs = db.find_internal_transfer_pairs(day_window=2)
    if transfer_pairs:
        with st.expander(
            f"🔁 Potential Own-Account Transfers Detected ({len(transfer_pairs)})",
            expanded=False,
        ):
            st.caption(
                "Matching outgoing & incoming rows within ±2 days across your wallets. "
                "Link them cleanly so neither side double-counts spend or inflow."
            )
            for pair in transfer_pairs:
                out_tx = pair["out_tx"]
                in_tx = pair["in_tx"]
                mode = pair["mode"]
                conf = pair["confidence"]
                with st.container(border=True):
                    badge = (
                        "Duplicate Mirror Credit"
                        if mode == "MERGE_DUPLICATE_CREDIT"
                        else f"{conf} Match"
                    )
                    st.markdown(
                        f"**{html.escape(out_tx.account_name)} → {html.escape(in_tx.account_name)}** · "
                        f"**{html.escape(format_pkr(out_tx.amount))}** · `{badge}`\n\n"
                        f"• Out (`#{out_tx.id}`, {out_tx.tx_date}): {html.escape(out_tx.payee)}\n"
                        f"• In (`#{in_tx.id}`, {in_tx.tx_date}): {html.escape(in_tx.payee)}"
                    )
                    btn_label = (
                        "🔗 Merge Mirror Credit"
                        if mode == "MERGE_DUPLICATE_CREDIT"
                        else "🔗 Link as Own Transfer"
                    )
                    if st.button(
                        btn_label,
                        key=f"t2_pair_link_{out_tx.id}_{in_tx.id}",
                        width="stretch",
                    ):
                        try:
                            linked = db.link_internal_transfer_pair(
                                out_tx.id or 0,
                                in_tx.id or 0,
                            )
                        except ValueError as exc:
                            st.error(str(exc))
                        else:
                            invalidate_all_caches()
                            notify_success(
                                f"Linked #{out_tx.id} ({linked.account_name} → {linked.target_account}) "
                                f"for {format_pkr(linked.amount)} and removed mirror leg #{in_tx.id}."
                            )
                            st.rerun()

    if not filtered_txs:
        if active_pill == "Needs Review" and filter_month is not None:
            global_pending = db.get_needs_review_count()
            if global_pending > 0:
                st.info(
                    f"0 pending items in {month_key}, but {global_pending} item(s) await review in other months."
                )
                if st.button(f"Switch to All Months ({global_pending})", key="t2_switch_all_months", type="primary"):
                    st.session_state["t2_period_mode"] = "all_months"
                    st.rerun()
                return
        st.info("No transactions match the current filters.")
        return

    # 4. Non-snapping Rapid Review Queue (1-by-1 without dropdown hunting)
    if pending_in_view:
        q_idx = int(st.session_state.get("t2_queue_idx", 0)) % len(pending_in_view)
        st.session_state["t2_queue_idx"] = q_idx
        q_tx = pending_in_view[q_idx]
        q_id = q_tx.id or 0

        with st.container(border=True):
            with st.container(key="ptos_compact_t2_q_hdr"):
                qh_l, qh_prev, qh_skip, qh_edit = st.columns([2.2, 0.55, 0.55, 0.7], vertical_alignment="center")
                with qh_l:
                    st.markdown(
                        f"**⚡ Review Queue ({q_idx + 1} of {len(pending_in_view)})** · "
                        f"`#{q_id}` · **{html.escape(format_pkr(q_tx.amount))}** · {html.escape(q_tx.payee[:48])}"
                    )
                with qh_prev:
                    if st.button("◀", key="t2_q_prev", width="stretch", disabled=(len(pending_in_view) <= 1)):
                        st.session_state["t2_queue_idx"] = (q_idx - 1) % len(pending_in_view)
                        st.rerun()
                with qh_skip:
                    if st.button("Skip ▶", key="t2_q_next", width="stretch", disabled=(len(pending_in_view) <= 1)):
                        st.session_state["t2_queue_idx"] = (q_idx + 1) % len(pending_in_view)
                        st.rerun()
                with qh_edit:
                    if st.button("✏️ Full", key=f"t2_q_full_{q_id}", width="stretch"):
                        open_dialog("edit_tx", tx_id=q_id)
                        st.rerun()

            q_cats = categories_for_tx_type(categories, bucket_map, q_tx.tx_type, current_category=q_tx.category)
            qc_key = f"t2_edit_cat_{q_id}"
            qb_key = f"t2_edit_bkt_{q_id}"
            if qc_key not in st.session_state or st.session_state[qc_key] not in q_cats:
                st.session_state[qc_key] = q_tx.category if q_tx.category in q_cats else q_cats[0]
                sync_bucket_on_cat_change(qc_key, qb_key, bucket_map, q_tx.tx_type)

            qc1, qc2, qc3, qc4 = st.columns([1.15, 1.15, 1.0, 1.15], vertical_alignment="bottom")
            with qc1:
                q_cat = st.selectbox(
                    "Category",
                    options=q_cats,
                    key=qc_key,
                    on_change=sync_bucket_on_cat_change,
                    args=(qc_key, qb_key, bucket_map, q_tx.tx_type),
                )
            with qc2:
                allowed_bkts = buckets_for_tx_type(q_tx.tx_type)
                if qb_key not in st.session_state or st.session_state[qb_key] not in allowed_bkts:
                    st.session_state[qb_key] = (
                        q_tx.cost_bucket
                        if q_tx.cost_bucket in allowed_bkts
                        else bucket_map.get(q_cat, allowed_bkts[0])
                    )
                q_bkt: CostBucket = st.selectbox(  # type: ignore[assignment]
                    "Bucket",
                    options=allowed_bkts,
                    format_func=format_bucket,
                    disabled=(len(allowed_bkts) == 1),
                    key=qb_key,
                )
            with qc3:
                q_kw = st.text_input(
                    "Rule Keyword",
                    value=default_rule_keyword_for_tx(db, q_tx),
                    key=f"t2_edit_kw_{q_id}",
                )
            with qc4:
                q_mem = st.checkbox("Learn Rule", value=True, key=f"t2_edit_mem_{q_id}")

            with st.container(key="ptos_compact_t2_q_btns"):
                qb1, qb2 = st.columns([1.4, 0.9])
                with qb1:
                    if st.button("✅ Verify & Save Next", key=f"t2_q_save_{q_id}", type="primary", width="stretch"):
                        try:
                            _, aff = save_tx_and_maybe_learn(
                                db,
                                q_id,
                                payee=q_tx.payee,
                                merchant_alias=q_tx.merchant_alias,
                                category=q_cat,
                                cost_bucket=q_bkt,
                                target_account=q_tx.target_account,
                                include_target_account=True,
                                learn_rule=bool(q_mem),
                                rule_keyword=q_kw,
                                overwrite_verified=False,
                            )
                        except ValueError as exc:
                            st.error(str(exc))
                        else:
                            # Keep index cleanly clamped on next rerun (remaining shrinks by 1)
                            st.session_state["t2_queue_idx"] = q_idx
                            notify_success(f"Verified #{q_id} ({q_cat}); {aff} matching row(s) updated.")
                            st.rerun()
                with qb2:
                    if st.button("✏️ Open Editor Modal", key=f"t2_q_modal_{q_id}", width="stretch"):
                        open_dialog("edit_tx", tx_id=q_id)
                        st.rerun()

    # 5. Interactive Ledger (Table click-to-edit + Cards view)
    with st.container(key="ptos_compact_t2_view_bar"):
        vh_left, vh_right = st.columns([2.2, 1.0], vertical_alignment="center")
        with vh_left:
            render_section_header(
                "💳 Ledger",
                f"Showing {len(filtered_txs)} transaction(s) — select any row or click ✏️ to edit",
            )
        with vh_right:
            view_mode = st.segmented_control(
                "View Mode",
                options=["📋 Table", "🃏 Cards"],
                default="📋 Table",
                key="t2_view_mode",
                label_visibility="collapsed",
            )
            if not view_mode:
                view_mode = "📋 Table"

    if view_mode == "📋 Table":
        rows = [
            {
                "id": int(tx.id or 0),
                "tx_date": tx.tx_date,
                "account_name": tx.account_name,
                "target_account": tx.target_account or "—",
                "payee": tx.merchant_alias or tx.payee,
                "category": tx.category,
                "cost_bucket": tx.cost_bucket,
                "tx_type": tx.tx_type,
                "amount": float(tx.amount),
                "tax_fee_amount": float(tx.tax_fee_amount),
                "review_status": tx.review_status,
            }
            for tx in filtered_txs
        ]
        df = pd.DataFrame(rows)
        selection_state = st.dataframe(
            df,
            width="stretch",
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key="t2_ledger_df",
            column_config={
                "id": st.column_config.NumberColumn("ID", width="small"),
                "tx_date": st.column_config.TextColumn("Date", width="small"),
                "account_name": st.column_config.TextColumn("Wallet", width="small"),
                "target_account": st.column_config.TextColumn("Target", width="small"),
                "payee": st.column_config.TextColumn("Payee", width="medium"),
                "category": st.column_config.TextColumn("Category", width="medium"),
                "cost_bucket": st.column_config.TextColumn("Bucket", width="small"),
                "tx_type": st.column_config.TextColumn("Type", width="small"),
                "amount": st.column_config.NumberColumn("Amount", format="accounting"),
                "tax_fee_amount": st.column_config.NumberColumn("Tax/Fee", format="accounting"),
                "review_status": st.column_config.TextColumn("Status", width="small"),
            },
        )
        sel_rows = []
        if isinstance(selection_state, dict):
            sel_rows = (selection_state.get("selection") or {}).get("rows") or []
        else:
            sel_obj = getattr(selection_state, "selection", None)
            sel_rows = getattr(sel_obj, "rows", None) or []

        if sel_rows:
            chosen_idx = int(sel_rows[0])
            if 0 <= chosen_idx < len(filtered_txs):
                sel_tx = filtered_txs[chosen_idx]
                with st.container(key="ptos_compact_t2_sel_bar"):
                    sb_l, sb_r = st.columns([2.4, 0.9], vertical_alignment="center")
                    with sb_l:
                        st.info(
                            f"Selected **#{sel_tx.id}** · {sel_tx.merchant_alias or sel_tx.payee} "
                            f"({format_pkr(sel_tx.amount)}, {sel_tx.tx_date})"
                        )
                    with sb_r:
                        if st.button("✏️ Edit Selected", key="t2_edit_selected_btn", type="primary", width="stretch"):
                            open_dialog("edit_tx", tx_id=int(sel_tx.id or 0))
                            st.rerun()
        else:
            # Always keep a compact 1-row quick selector accessible for keyboard / AppTest workflows
            tx_options = [int(t.id or 0) for t in filtered_txs if t.id is not None]
            tx_lookup = {int(t.id or 0): t for t in filtered_txs if t.id is not None}
            if tx_options:
                with st.container(key="ptos_compact_t2_pick"):
                    pk_l, pk_r = st.columns([2.5, 0.9], vertical_alignment="bottom")
                    with pk_l:
                        picked_id = st.selectbox(
                            "Quick Select Row to Edit",
                            options=tx_options,
                            format_func=lambda tid: (
                                f"#{tid} · {tx_lookup[tid].merchant_alias or tx_lookup[tid].payee} "
                                f"({format_pkr(tx_lookup[tid].amount)}, {tx_lookup[tid].tx_date})"
                            ),
                            key="t2_selected_tx_id",
                        )
                    with pk_r:
                        if st.button("✏️ Edit Row", key="t2_edit_picked_btn", width="stretch"):
                            open_dialog("edit_tx", tx_id=int(picked_id))
                            st.rerun()
    else:
        for tx in filtered_txs[:40]:
            tid = tx.id or 0
            with st.container(key=f"ptos_compact_t2_card_{tid}"):
                c_card, c_btn = st.columns([4.3, 0.7], vertical_alignment="center")
                with c_card:
                    render_mobile_transaction_cards([tx], max_items=1)
                with c_btn:
                    if st.button("✏️", key=f"t2_card_edit_{tid}", width="stretch"):
                        open_dialog("edit_tx", tx_id=tid)
                        st.rerun()
        if len(filtered_txs) > 40:
            st.caption(f"Showing first 40 cards of {len(filtered_txs)}. Switch to 📋 Table view for the complete list.")

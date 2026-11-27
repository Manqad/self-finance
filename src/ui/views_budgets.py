"""Tab 3 (Budgets & Bills): Bulk st.data_editor Category Studio, Bill Pay & Edit Dialogs."""

from __future__ import annotations

import html

import pandas as pd
import streamlit as st

from src.database.db_manager import DatabaseManager
from src.models import CostBucket, DashboardMetrics
from src.ui.components import (
    format_pkr,
    render_category_budget_bars,
    render_section_header,
)
from src.ui.state import (
    _COST_BUCKETS,
    _DEBIT_BUCKETS,
    invalidate_all_caches,
    notify_success,
    open_dialog,
)
from src.ui.views_common import (
    apply_pending_widget_sync,
    format_bucket,
)


def _queue_cat_limit_sync(cb: Any, *, remove: bool = False) -> None:
    """Compatibility helper if tests/callers queue category limit updates."""
    pass


def render_budgets_tab(
    db: DatabaseManager,
    metrics: DashboardMetrics,
    month_key: str,
) -> None:
    """Render Tab 3: Category Budget Studio (st.data_editor + bars) and Recurring Bills Manager."""
    apply_pending_widget_sync()

    all_budgets = db.get_category_budgets(active_only=False)
    active_debit = [cb for cb in all_budgets if cb.is_active and cb.cost_bucket in _DEBIT_BUCKETS]
    total_cat_limit = sum(cb.monthly_limit for cb in active_debit if cb.monthly_limit > 0)
    total_cat_spent = sum(
        cp.spent for cp in metrics.category_progress if cp.monthly_limit > 0
    )

    with st.container(key="ptos_kpi_grid_t3"):
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Active Categories", f"{len(active_debit)}")
        k2.metric("Tracked Limits", format_pkr(total_cat_limit))
        k3.metric("Spent Across Limits", format_pkr(total_cat_spent))
        k4.metric("Unpaid Bills", format_pkr(metrics.unpaid_bills_total))

    sub_cats, sub_bills = st.tabs(["📊 Category Budgets", "📅 Recurring Bills"])

    with sub_cats:
        render_section_header(
            "📊 Monthly Category Studio",
            "Edit directly in the table below, or pick any category to edit/create via modal",
        )
        cat_map_id = {int(cb.id): cb for cb in all_budgets if cb.id is not None}
        with st.container(key="ptos_compact_t3_hdr"):
            h_pick, h_edit, h_add = st.columns([1.45, 0.85, 0.9], vertical_alignment="center")
            with h_pick:
                if cat_map_id:
                    picked_cid = st.selectbox(
                        "Pick Category",
                        options=list(cat_map_id.keys()),
                        format_func=lambda cid: f"{cat_map_id[cid].emoji} {cat_map_id[cid].category}",
                        key="t3_cat_pick_id",
                        label_visibility="collapsed",
                    )
                else:
                    picked_cid = None
            with h_edit:
                if picked_cid is not None and st.button("✏️ Edit", key="t3_open_edit_cat_btn", width="stretch"):
                    open_dialog("edit_category", category_id=int(picked_cid))
                    st.rerun()
            with h_add:
                if st.button("➕ New", key="t3_open_new_cat_btn", type="primary", width="stretch"):
                    open_dialog("new_category")
                    st.rerun()

        if all_budgets:
            editor_rows = [
                {
                    "id": int(cb.id or 0),
                    "emoji": cb.emoji,
                    "category": cb.category,
                    "cost_bucket": cb.cost_bucket,
                    "monthly_limit": float(cb.monthly_limit),
                    "is_active": bool(cb.is_active),
                }
                for cb in all_budgets
                if cb.id is not None
            ]
            rev = int(st.session_state.get("ptos_data_rev", 0))
            df_cats = pd.DataFrame(editor_rows)
            edited_df = st.data_editor(
                df_cats,
                width="stretch",
                hide_index=True,
                key=f"t3_cat_studio_editor_{rev}",
                disabled=["id"],
                column_config={
                    "id": st.column_config.NumberColumn("ID", width="small"),
                    "emoji": st.column_config.TextColumn("Emoji", width="small", max_chars=8),
                    "category": st.column_config.TextColumn("Category", width="medium"),
                    "cost_bucket": st.column_config.SelectboxColumn(
                        "Group",
                        options=list(_COST_BUCKETS),
                        required=True,
                        width="small",
                    ),
                    "monthly_limit": st.column_config.NumberColumn(
                        "Monthly Limit (PKR)",
                        min_value=0.0,
                        step=500.0,
                        format="accounting",
                    ),
                    "is_active": st.column_config.CheckboxColumn("Active", width="small"),
                },
            )

            with st.container(key="ptos_compact_t3_save"):
                s_col, d_col = st.columns([1.4, 1.0])
                with s_col:
                    if st.button("💾 Save Category Studio Changes", key="t3_save_studio_btn", type="primary", width="stretch"):
                        orig_map = {int(cb.id): cb for cb in all_budgets if cb.id is not None}
                        updated_n = 0
                        for row in edited_df.to_dict("records"):
                            cid = int(row["id"])
                            orig = orig_map.get(cid)
                            if orig is None:
                                continue
                            new_emoji = str(row.get("emoji") or "").strip() or orig.emoji
                            new_name = " ".join(str(row.get("category") or "").split()).strip()
                            if not new_name:
                                st.error(f"Category #{cid} name cannot be blank.")
                                return
                            new_bkt: CostBucket = str(row.get("cost_bucket") or orig.cost_bucket)  # type: ignore[assignment]
                            new_lim = (
                                round(max(0.0, float(row.get("monthly_limit") or 0.0)), 2)
                                if new_bkt in _DEBIT_BUCKETS
                                else 0.0
                            )
                            new_act = bool(row.get("is_active", True))
                            if (
                                new_emoji != orig.emoji
                                or new_name != orig.category
                                or new_bkt != orig.cost_bucket
                                or abs(new_lim - float(orig.monthly_limit)) >= 0.01
                                or new_act != orig.is_active
                            ):
                                try:
                                    db.update_category_budget_by_id(
                                        cid,
                                        category=new_name,
                                        cost_bucket=new_bkt,
                                        monthly_limit=new_lim,
                                        emoji=new_emoji,
                                        is_active=new_act,
                                    )
                                except ValueError as exc:
                                    st.error(str(exc))
                                    return
                                updated_n += 1
                        if updated_n == 0:
                            notify_success("No category budget changes detected.")
                        else:
                            invalidate_all_caches()
                            notify_success(f"Updated {updated_n} category configuration(s).")
                            st.rerun()

                with d_col:
                    with st.popover("🗑️ Delete Category", width="stretch"):
                        del_map = {int(cb.id): cb for cb in all_budgets if cb.id is not None}
                        del_id = st.selectbox(
                            "Category to Remove",
                            options=list(del_map.keys()),
                            format_func=lambda cid: f"{del_map[cid].emoji} {del_map[cid].category}",
                            key="t3_del_cat_pick",
                        )
                        target_cb = del_map[del_id]
                        if target_cb.category == "Uncategorized":
                            st.caption("Default 'Uncategorized' bucket cannot be deleted.")
                        elif st.button(
                            "Confirm Permanent Delete",
                            key="t3_del_cat_confirm",
                            type="secondary",
                            width="stretch",
                        ):
                            try:
                                moved = db.delete_category_budget(del_id)
                            except ValueError as exc:
                                st.error(str(exc))
                            else:
                                invalidate_all_caches()
                                notify_success(
                                    f"Deleted '{target_cb.category}' ({moved} transaction(s) reassigned)."
                                )
                                st.rerun()

        render_section_header(
            "📈 Live Monthly Category Progress",
            f"Budget burn health across {month_key}",
        )
        render_category_budget_bars(metrics.category_progress)

    with sub_bills:
        paid_cnt = sum(1 for s in metrics.upcoming_bills if s.is_paid)
        total_cnt = len(metrics.upcoming_bills)

        with st.container(key="ptos_compact_t3_bill_hdr"):
            bh_l, bh_add = st.columns([2.2, 0.9], vertical_alignment="center")
            with bh_l:
                render_section_header(
                    "📅 Recurring Bills & Subscriptions",
                    f"{paid_cnt} / {total_cnt} paid in {month_key} • Unpaid remaining: {format_pkr(metrics.unpaid_bills_total)}",
                )
            with bh_add:
                if st.button("➕ Add Bill", key="t3_open_new_bill_btn", type="primary", width="stretch"):
                    open_dialog("edit_bill", commitment_id=None)
                    st.rerun()

        if not metrics.upcoming_bills:
            st.info("No active recurring bills configured. Click ➕ Add Bill above to track rent, fiber, or subscriptions.")
        else:
            for status in metrics.upcoming_bills:
                c = status.commitment
                cid = c.id or 0
                short_due = (
                    status.due_date[5:]
                    if status.due_date and len(status.due_date) >= 10
                    else f"Day {c.due_day}"
                )
                if status.is_paid or status.status == "PAID":
                    badge_html = (
                        f'<span class="ptos-bill-badge ptos-bill-paid">'
                        f'✅ Paid {html.escape(format_pkr(status.paid_amount))} ({html.escape(status.paid_date or month_key)})'
                        f'</span>'
                    )
                elif status.status == "OVERDUE":
                    badge_html = (
                        f'<span class="ptos-bill-badge ptos-bill-overdue">'
                        f'⚠️ Overdue ({html.escape(short_due)}) • {html.escape(format_pkr(c.expected_amount))}'
                        f'</span>'
                    )
                elif status.status == "DUE_SOON":
                    badge_html = (
                        f'<span class="ptos-bill-badge ptos-bill-soon">'
                        f'🔔 Due Soon ({html.escape(short_due)}) • {html.escape(format_pkr(c.expected_amount))}'
                        f'</span>'
                    )
                else:
                    badge_html = (
                        f'<span class="ptos-bill-badge ptos-bill-due">'
                        f'📅 Due {html.escape(short_due)} • {html.escape(format_pkr(c.expected_amount))}'
                        f'</span>'
                    )

                with st.container(border=True):
                    st.markdown(
                        f"**{html.escape(c.title)}** &nbsp; {badge_html}<br>"
                        f"<span style='font-size:0.79rem;color:#94A3B8;'>"
                        f"Keyword: <code>{html.escape(c.match_keyword)}</code> • {html.escape(c.category)} ({html.escape(c.cost_bucket)})"
                        f"</span>",
                        unsafe_allow_html=True,
                    )
                    with st.container(key=f"ptos_compact_t3_bill_{cid}"):
                        if not status.is_paid:
                            b_pay, b_edit, b_pause, b_del = st.columns([1.2, 0.85, 0.85, 0.85])
                            with b_pay:
                                if st.button("💸 Pay", key=f"t3_bill_pay_{cid}", type="primary", width="stretch"):
                                    open_dialog("pay_bill", commitment_id=cid, month_key=month_key)
                                    st.rerun()
                        else:
                            b_edit, b_pause, b_del = st.columns([1.0, 1.0, 1.0])

                        with b_edit:
                            if st.button("✏️ Edit", key=f"t3_bill_edit_{cid}", width="stretch"):
                                open_dialog("edit_bill", commitment_id=cid)
                                st.rerun()
                        with b_pause:
                            if st.button("⏸️ Pause", key=f"t3_bill_pause_{cid}", width="stretch"):
                                db.upsert_recurring_commitment(c.model_copy(update={"is_active": False}))
                                invalidate_all_caches()
                                notify_success(f"Paused recurring bill '{c.title}'.")
                                st.rerun()
                        with b_del:
                            with st.popover("🗑️ Delete", width="stretch"):
                                st.caption(f"Delete '{c.title}'?")
                                if st.button(
                                    "Confirm Delete",
                                    key=f"t3_bill_del_{cid}",
                                    type="secondary",
                                    width="stretch",
                                ):
                                    if c.id is not None:
                                        db.delete_recurring_commitment(c.id)
                                    invalidate_all_caches()
                                    notify_success(f"Deleted recurring commitment '{c.title}'.")
                                    st.rerun()

        paused_bills = [c for c in db.get_recurring_commitments(active_only=False) if not c.is_active]
        if paused_bills:
            with st.expander(f"⏸️ Paused Bills ({len(paused_bills)})", expanded=False):
                for c in paused_bills:
                    cid = c.id or 0
                    with st.container(border=True):
                        with st.container(key=f"ptos_compact_t3_paused_{cid}"):
                            pl, pr1, pr2 = st.columns([2.0, 0.85, 0.75], vertical_alignment="center")
                            with pl:
                                st.markdown(
                                    f"**{html.escape(c.title)}** · {html.escape(format_pkr(c.expected_amount))} · `Day {c.due_day}`"
                                )
                            with pr1:
                                if st.button("▶️ Resume", key=f"t3_bill_resume_{cid}", width="stretch"):
                                    db.upsert_recurring_commitment(c.model_copy(update={"is_active": True}))
                                    invalidate_all_caches()
                                    notify_success(f"Resumed '{c.title}'.")
                                    st.rerun()
                            with pr2:
                                with st.popover("🗑️ Delete", width="stretch"):
                                    if st.button(
                                        "Confirm Delete",
                                        key=f"t3_bill_pdel_{cid}",
                                        type="secondary",
                                        width="stretch",
                                    ):
                                        if c.id is not None:
                                            db.delete_recurring_commitment(c.id)
                                        invalidate_all_caches()
                                        notify_success(f"Deleted paused commitment '{c.title}'.")
                                        st.rerun()

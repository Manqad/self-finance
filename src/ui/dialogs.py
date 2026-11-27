"""Modal @st.dialog flows for Quick Add, Transaction Editor, Gmail/File Sync, Bills, and Categories."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

import streamlit as st

from src.database.db_manager import DatabaseManager
from src.engine.categorizer import apply_rules
from src.models import (
    WALLET_MMF,
    CostBucket,
    ParsedTransaction,
    RecurringCommitment,
    TxType,
)
from src.ui.components import format_pkr
from src.ui.state import (
    _COST_BUCKETS,
    _DEBIT_BUCKETS,
    _TX_TYPES,
    _add_flash,
    close_dialog,
    get_active_dialog,
    invalidate_all_caches,
    notify_success,
)
from src.ui.views_common import (
    apply_pending_widget_sync,
    bill_date_for_month,
    buckets_for_tx_type,
    categories_for_tx_type,
    default_date_for_month,
    default_rule_keyword_for_tx,
    default_transfer_category,
    format_bucket,
    format_tx_type,
    get_category_catalog,
    ingest_parsed_batch,
    internal_transfer_targets,
    month_date_bounds,
    render_inline_category_creator,
    run_gmail_sync,
    save_tx_and_maybe_learn,
    sync_bucket_on_cat_change,
)


@st.dialog("➕ Add Transaction", width="medium", on_dismiss=close_dialog)
def _render_quick_add_dialog(db: DatabaseManager, active_month: str) -> None:
    """Modal Quick Add transaction / wallet transfer form."""
    wallets = [w.account_name for w in db.get_wallets()]
    categories, bucket_map, _ = get_category_catalog(db)

    if st.session_state.get("dlg_qa_month") != active_month:
        st.session_state["dlg_qa_month"] = active_month
        st.session_state["dlg_qa_date"] = default_date_for_month(active_month)

    if st.session_state.pop("dlg_qa_reset", False):
        st.session_state["dlg_qa_amount"] = 0.0
        st.session_state["dlg_qa_tax"] = 0.0
        st.session_state["dlg_qa_payee"] = ""
        st.session_state["dlg_qa_alias"] = ""
        st.session_state["dlg_qa_notes"] = ""
        st.session_state["dlg_qa_category"] = "Auto-Categorize (Rules)"
        st.session_state["dlg_qa_bucket"] = "VARIABLE"
        st.session_state["dlg_qa_date"] = default_date_for_month(active_month)

    col_w, col_t = st.columns(2)
    with col_w:
        if st.session_state.get("dlg_qa_wallet") not in wallets:
            st.session_state["dlg_qa_wallet"] = wallets[0]
        wallet_name = st.selectbox("Wallet", options=wallets, key="dlg_qa_wallet")
    with col_t:
        type_opts = [
            t for t in _TX_TYPES if not (wallet_name == WALLET_MMF and t == "TRANSFER_MMF")
        ]
        if st.session_state.get("dlg_qa_type") not in type_opts:
            st.session_state["dlg_qa_type"] = type_opts[0]
        tx_type: TxType = st.selectbox(  # type: ignore[assignment]
            "Type",
            options=type_opts,
            format_func=format_tx_type,
            key="dlg_qa_type",
        )

    target_acct: Optional[str] = None
    if tx_type == "TRANSFER_INTERNAL":
        tgt_opts = internal_transfer_targets(wallets, wallet_name)
        if not tgt_opts:
            st.error("No valid destination wallet available.")
            return
        if st.session_state.get("dlg_qa_target") not in tgt_opts:
            st.session_state["dlg_qa_target"] = tgt_opts[0]
        target_acct = st.selectbox("Target Wallet", options=tgt_opts, key="dlg_qa_target")
    elif tx_type == "TRANSFER_MMF":
        target_acct = WALLET_MMF
        st.caption(f"Sweep destination: **{WALLET_MMF}** (Investment)")

    col_a, col_tax, col_d = st.columns([1.0, 0.9, 1.0])
    with col_a:
        st.session_state.setdefault("dlg_qa_amount", 0.0)
        amount = st.number_input("Amount (PKR)", min_value=0.0, step=100.0, key="dlg_qa_amount")
    with col_tax:
        st.session_state.setdefault("dlg_qa_tax", 0.0)
        tax_fee = st.number_input(
            "Tax / Fee (PKR)",
            min_value=0.0,
            step=10.0,
            disabled=(tx_type == "CREDIT"),
            key="dlg_qa_tax",
        )
    with col_d:
        m_start, m_end = month_date_bounds(active_month)
        st.session_state.setdefault("dlg_qa_date", default_date_for_month(active_month))
        tx_date_val = st.date_input(
            "Date",
            min_value=m_start,
            max_value=date(max(m_end.year + 1, date.today().year + 1), 12, 31),
            key="dlg_qa_date",
        )

    st.session_state.setdefault("dlg_qa_payee", "")
    payee = st.text_input(
        "Payee / Description",
        placeholder="e.g., Chaiiwala, Salary, NayaPay Top-Up...",
        key="dlg_qa_payee",
    )

    type_cats = categories_for_tx_type(categories, bucket_map, tx_type)
    cat_opts = (
        ["Auto-Categorize (Rules)"] + type_cats
        if tx_type in ("DEBIT", "CREDIT")
        else type_cats
    )
    if st.session_state.get("dlg_qa_category") not in cat_opts:
        if tx_type == "TRANSFER_INTERNAL":
            st.session_state["dlg_qa_category"] = default_transfer_category(
                wallet_name, target_acct, type_cats
            )
        elif tx_type == "TRANSFER_MMF":
            st.session_state["dlg_qa_category"] = type_cats[0]
        else:
            st.session_state["dlg_qa_category"] = "Auto-Categorize (Rules)"

    col_c, col_b = st.columns(2)
    with col_c:
        cat_choice = st.selectbox(
            "Category",
            options=cat_opts,
            key="dlg_qa_category",
            on_change=sync_bucket_on_cat_change,
            args=("dlg_qa_category", "dlg_qa_bucket", bucket_map, tx_type),
        )
    with col_b:
        bkt_opts = buckets_for_tx_type(tx_type)
        if st.session_state.get("dlg_qa_bucket") not in bkt_opts:
            st.session_state["dlg_qa_bucket"] = (
                "INFLOW"
                if tx_type == "CREDIT"
                else ("IGNORE" if tx_type == "TRANSFER_INTERNAL" else ("INVESTMENT" if tx_type == "TRANSFER_MMF" else "VARIABLE"))
            )
        cost_bucket = st.selectbox(
            "Cost Bucket",
            options=bkt_opts,
            format_func=format_bucket,
            disabled=(len(bkt_opts) == 1),
            key="dlg_qa_bucket",
        )

    render_inline_category_creator(
        db,
        key_prefix="dlg_qa_newcat",
        tx_type=tx_type,
        target_cat_key="dlg_qa_category",
        target_bkt_key="dlg_qa_bucket",
    )

    col_al, col_n = st.columns(2)
    with col_al:
        st.session_state.setdefault("dlg_qa_alias", "")
        alias = st.text_input("Merchant Alias (optional)", key="dlg_qa_alias")
    with col_n:
        st.session_state.setdefault("dlg_qa_notes", "")
        notes = st.text_input("Notes (optional)", key="dlg_qa_notes")

    b_save, b_cancel = st.columns([1.4, 0.8])
    with b_save:
        if st.button("💾 Save Transaction", key="dlg_qa_submit", type="primary", width="stretch"):
            clean_payee = payee.strip()
            if float(amount) <= 0.0:
                st.error("Amount must be greater than Rs. 0.")
            elif not clean_payee and not (
                (tx_type == "TRANSFER_INTERNAL" and target_acct) or tx_type == "TRANSFER_MMF"
            ):
                st.error("Payee / description cannot be empty.")
            else:
                if not clean_payee:
                    if tx_type == "TRANSFER_INTERNAL" and target_acct:
                        clean_payee = f"{wallet_name} → {target_acct}"
                    elif tx_type == "TRANSFER_MMF":
                        clean_payee = "Al Meezan MMF Sweep"

                tx_date_str = tx_date_val.strftime("%Y-%m-%d")
                if cat_choice == "Auto-Categorize (Rules)":
                    parsed_tx = ParsedTransaction(
                        message_id=f"MANUAL_PREVIEW_{tx_date_str}",
                        tx_date=tx_date_str,
                        account_name=wallet_name,
                        target_account=target_acct,
                        tx_type=tx_type,
                        amount=round(float(amount), 2),
                        tax_fee_amount=0.0 if tx_type == "CREDIT" else round(float(tax_fee), 2),
                        payee=clean_payee,
                        merchant_alias=alias.strip() or None,
                        source="MANUAL",
                        payment_channel="Cash" if wallet_name == "Cash in Hand" else "Manual",
                        notes=notes.strip() or None,
                    )
                    classified = apply_rules(parsed_tx, db)
                    final_cat = classified.category
                    final_bkt = classified.cost_bucket
                    final_alias = classified.merchant_alias or (alias.strip() or None)
                    final_type = classified.tx_type
                    final_target = classified.target_account
                    rev_stat = classified.review_status
                else:
                    final_cat = cat_choice
                    final_bkt = cost_bucket  # type: ignore[assignment]
                    final_alias = alias.strip() or None
                    final_type = tx_type
                    final_target = target_acct
                    rev_stat = "MANUALLY_VERIFIED"

                created = db.add_manual_transaction(
                    tx_date=tx_date_str,
                    account_name=wallet_name,
                    tx_type=final_type,
                    amount=round(float(amount), 2),
                    tax_fee_amount=0.0 if final_type == "CREDIT" else round(float(tax_fee), 2),
                    payee=clean_payee,
                    target_account=final_target,
                    payment_channel="Cash" if wallet_name == "Cash in Hand" else "Manual",
                    category=final_cat,
                    cost_bucket=final_bkt,
                    review_status=rev_stat,
                    notes=notes.strip() or None,
                )
                if final_alias and created.id is not None:
                    db.update_transaction(created.id, merchant_alias=final_alias)
                    created = created.model_copy(update={"merchant_alias": final_alias})
                st.session_state["dlg_qa_reset"] = True
                invalidate_all_caches()
                notify_success(
                    f"Saved #{created.id}: {format_pkr(created.amount)} at {created.merchant_alias or created.payee} ({created.category})."
                )
                close_dialog()
                st.rerun()
    with b_cancel:
        if st.button("Cancel", key="dlg_qa_cancel", width="stretch"):
            close_dialog()
            st.rerun()


@st.dialog("✏️ Edit Transaction", width="medium", on_dismiss=close_dialog)
def _render_edit_tx_dialog(db: DatabaseManager, tx_id: int) -> None:
    """Modal full transaction editor, rule trainer, and 2-step delete."""
    sel_tx = db.get_transaction_by_id(tx_id)
    if sel_tx is None:
        close_dialog()
        st.rerun()
        return

    wallets = [w.account_name for w in db.get_wallets()]
    categories, bucket_map, _ = get_category_catalog(db)

    st.caption(
        f"ID #{sel_tx.id} • Status: **{sel_tx.review_status}** • Source: **{sel_tx.source}**"
    )
    if sel_tx.raw_subject:
        st.caption(f"Email Subject: `{sel_tx.raw_subject}`")

    m1, m2, m3 = st.columns(3)
    with m1:
        try:
            d_val = datetime.strptime(sel_tx.tx_date, "%Y-%m-%d").date()
        except ValueError:
            d_val = date.today()
        ed_date = st.date_input("Date", value=d_val, key=f"dlg_ed_date_{tx_id}")
    with m2:
        acct_opts = wallets if sel_tx.account_name in wallets else [sel_tx.account_name] + wallets
        ed_account = st.selectbox(
            "Wallet",
            options=acct_opts,
            index=acct_opts.index(sel_tx.account_name),
            key=f"dlg_ed_acct_{tx_id}",
        )
    with m3:
        type_opts = [t for t in _TX_TYPES if not (ed_account == WALLET_MMF and t == "TRANSFER_MMF")]
        t_key = f"dlg_ed_type_{tx_id}"
        if t_key not in st.session_state or st.session_state[t_key] not in type_opts:
            st.session_state[t_key] = sel_tx.tx_type if sel_tx.tx_type in type_opts else type_opts[0]
        ed_type: TxType = st.selectbox(  # type: ignore[assignment]
            "Type",
            options=type_opts,
            format_func=format_tx_type,
            key=t_key,
        )

    ed_target: Optional[str] = None
    if ed_type == "TRANSFER_INTERNAL":
        tgt_opts = internal_transfer_targets(wallets, ed_account)
        tgt_key = f"dlg_ed_target_{tx_id}"
        if tgt_opts:
            if tgt_key not in st.session_state or st.session_state[tgt_key] not in tgt_opts:
                st.session_state[tgt_key] = (
                    sel_tx.target_account if sel_tx.target_account in tgt_opts else tgt_opts[0]
                )
            ed_target = st.selectbox("Target Wallet", options=tgt_opts, key=tgt_key)
    elif ed_type == "TRANSFER_MMF":
        ed_target = WALLET_MMF
        st.caption(f"Sweep destination: **{WALLET_MMF}**")

    a1, a2, a3 = st.columns(3)
    with a1:
        ed_amt = st.number_input(
            "Amount (PKR)",
            min_value=0.01,
            value=max(0.01, float(sel_tx.amount)),
            step=100.0,
            key=f"dlg_ed_amt_{tx_id}",
        )
    with a2:
        ed_tax = st.number_input(
            "Tax / Fee (PKR)",
            min_value=0.0,
            value=0.0 if ed_type == "CREDIT" else float(sel_tx.tax_fee_amount),
            step=10.0,
            disabled=(ed_type == "CREDIT"),
            key=f"dlg_ed_tax_{tx_id}",
        )
    with a3:
        ed_chan = st.text_input(
            "Channel",
            value=sel_tx.payment_channel or "",
            key=f"dlg_ed_chan_{tx_id}",
        )

    p1, p2 = st.columns(2)
    with p1:
        ed_payee = st.text_input("Payee / Narration", value=sel_tx.payee, key=f"dlg_ed_payee_{tx_id}")
    with p2:
        ed_alias = st.text_input(
            "Friendly Alias",
            value=sel_tx.merchant_alias or "",
            key=f"dlg_ed_alias_{tx_id}",
        )

    cat_opts = categories_for_tx_type(
        categories, bucket_map, ed_type, current_category=sel_tx.category
    )
    ed_cat_key = f"dlg_ed_cat_{tx_id}"
    ed_bkt_key = f"dlg_ed_bkt_{tx_id}"
    if ed_cat_key not in st.session_state or st.session_state[ed_cat_key] not in cat_opts:
        st.session_state[ed_cat_key] = (
            sel_tx.category
            if sel_tx.category in cat_opts
            else (
                default_transfer_category(ed_account, ed_target, cat_opts)
                if ed_type == "TRANSFER_INTERNAL"
                else cat_opts[0]
            )
        )
        sync_bucket_on_cat_change(ed_cat_key, ed_bkt_key, bucket_map, ed_type)

    c_col, b_col = st.columns(2)
    with c_col:
        ed_cat = st.selectbox(
            "Category",
            options=cat_opts,
            key=ed_cat_key,
            on_change=sync_bucket_on_cat_change,
            args=(ed_cat_key, ed_bkt_key, bucket_map, ed_type),
        )
    with b_col:
        allowed_bkts = buckets_for_tx_type(ed_type)
        if ed_bkt_key not in st.session_state or st.session_state[ed_bkt_key] not in allowed_bkts:
            fallback_bkt = bucket_map.get(ed_cat, allowed_bkts[0])
            st.session_state[ed_bkt_key] = (
                sel_tx.cost_bucket
                if sel_tx.cost_bucket in allowed_bkts
                else (fallback_bkt if fallback_bkt in allowed_bkts else allowed_bkts[0])
            )
        ed_bucket: CostBucket = st.selectbox(  # type: ignore[assignment]
            "Cost Bucket",
            options=allowed_bkts,
            format_func=format_bucket,
            disabled=(len(allowed_bkts) == 1),
            key=ed_bkt_key,
        )

    render_inline_category_creator(
        db,
        key_prefix=f"dlg_ed_nc_{tx_id}",
        tx_type=ed_type,
        target_cat_key=ed_cat_key,
        target_bkt_key=ed_bkt_key,
    )

    ed_notes = st.text_input("Notes", value=sel_tx.notes or "", key=f"dlg_ed_notes_{tx_id}")

    st.divider()
    r1, r2 = st.columns([1.3, 1.0])
    with r1:
        ed_kw = st.text_input(
            "Rule Keyword",
            value=default_rule_keyword_for_tx(db, sel_tx),
            key=f"dlg_ed_kw_{tx_id}",
        )
    with r2:
        st.write("")
        overwrite_v = st.checkbox(
            "Update verified matches too",
            value=True,
            key=f"dlg_ed_over_{tx_id}",
        )

    btn1, btn2, btn3 = st.columns([1.2, 1.0, 0.8])
    with btn1:
        if st.button("💾 Save & Apply Rule", key=f"dlg_ed_save_rule_{tx_id}", type="primary", width="stretch"):
            try:
                _, affected = save_tx_and_maybe_learn(
                    db,
                    tx_id,
                    payee=ed_payee,
                    merchant_alias=ed_alias,
                    category=ed_cat,
                    cost_bucket=ed_bucket,
                    tx_date=ed_date.strftime("%Y-%m-%d"),
                    account_name=ed_account,
                    tx_type=ed_type,
                    target_account=ed_target,
                    include_target_account=True,
                    payment_channel=ed_chan,
                    amount=float(ed_amt),
                    tax_fee_amount=0.0 if ed_type == "CREDIT" else float(ed_tax),
                    notes=ed_notes,
                    learn_rule=True,
                    rule_keyword=ed_kw,
                    overwrite_verified=bool(overwrite_v),
                )
            except ValueError as exc:
                st.error(str(exc))
            else:
                notify_success(f"Saved #{tx_id} and trained rule '{ed_kw.strip()}' ({affected} updated).")
                close_dialog()
                st.rerun()
    with btn2:
        if st.button("✅ Save Only", key=f"dlg_ed_save_only_{tx_id}", width="stretch"):
            try:
                save_tx_and_maybe_learn(
                    db,
                    tx_id,
                    payee=ed_payee,
                    merchant_alias=ed_alias,
                    category=ed_cat,
                    cost_bucket=ed_bucket,
                    tx_date=ed_date.strftime("%Y-%m-%d"),
                    account_name=ed_account,
                    tx_type=ed_type,
                    target_account=ed_target,
                    include_target_account=True,
                    payment_channel=ed_chan,
                    amount=float(ed_amt),
                    tax_fee_amount=0.0 if ed_type == "CREDIT" else float(ed_tax),
                    notes=ed_notes,
                    learn_rule=False,
                )
            except ValueError as exc:
                st.error(str(exc))
            else:
                notify_success(f"Saved transaction #{tx_id} ({ed_cat}).")
                close_dialog()
                st.rerun()
    with btn3:
        confirm_key = f"dlg_ed_del_confirm_{tx_id}"
        if not st.session_state.get(confirm_key):
            if st.button("🗑️ Delete", key=f"dlg_ed_del_arm_{tx_id}", width="stretch"):
                st.session_state[confirm_key] = True
                st.rerun()
        else:
            if st.button("⚠️ Confirm", key=f"dlg_ed_del_go_{tx_id}", type="secondary", width="stretch"):
                st.session_state.pop(confirm_key, None)
                if db.delete_transaction(tx_id):
                    invalidate_all_caches()
                    notify_success(f"Deleted transaction #{tx_id} and recalculated balances.")
                    close_dialog()
                    st.rerun()
                else:
                    st.error("Transaction could not be deleted.")


@st.dialog("🔄 Sync Gmail & Import Statements", width="medium", on_dismiss=close_dialog)
def _render_sync_dialog(db: DatabaseManager) -> None:
    """Modal quick Sync & Statement Upload dialog accessible from Top Command Bar."""
    import src.ui.views as views_facade

    tab_gmail, tab_file = st.tabs(["📧 Gmail Sync", "📄 PDF / CSV Import"])
    with tab_gmail:
        gmail_status = views_facade.get_gmail_auth_status()
        if gmail_status.get("mode") in ("oauth", "imap"):
            st.success(
                f"Connected ({str(gmail_status.get('mode', '')).upper()}): {gmail_status['detail']}"
            )
        else:
            st.warning(f"Offline / Unconfigured: {gmail_status['detail']}")

        max_emails = st.number_input(
            "Max Recent Emails to Scan",
            min_value=10,
            max_value=500,
            value=100,
            step=10,
            key="dlg_sync_max_emails",
        )
        b1, b2 = st.columns([1.4, 0.8])
        with b1:
            if st.button("🔄 Sync Gmail Now", key="dlg_sync_gmail_btn", type="primary", width="stretch"):
                with st.spinner("Fetching & parsing NayaPay / HabibMetro receipts..."):
                    ok, _ = run_gmail_sync(db, int(max_emails))
                if ok:
                    close_dialog()
                st.rerun()
        with b2:
            if st.button("Close", key="dlg_sync_close_1", width="stretch"):
                close_dialog()
                st.rerun()

    with tab_file:
        nonce = int(st.session_state.get("t5_uploader_nonce", 0))
        files = st.file_uploader(
            "Drop NayaPay or HabibMetro PDF / CSV Statements",
            type=["pdf", "csv"],
            accept_multiple_files=True,
            key=f"dlg_sync_files_{nonce}",
        )
        hint = st.selectbox(
            "Statement Source Hint",
            options=["Auto-Detect", "NayaPay", "HabibMetro"],
            key="dlg_sync_hint",
        )
        if st.button("📥 Import Selected File(s)", key="dlg_sync_import_btn", type="primary", width="stretch"):
            if not files:
                st.warning("Select one or more `.pdf` or `.csv` files first.")
                return
            acct_hint = None if hint == "Auto-Detect" else hint
            all_parsed: list[ParsedTransaction] = []
            errors: list[str] = []
            for uf in files:
                try:
                    all_parsed.extend(
                        views_facade.import_statement_file(
                            uf.getvalue(),
                            uf.name,
                            account_hint=acct_hint,
                        )
                    )
                except Exception as exc:
                    errors.append(f"{uf.name}: {exc}")
            if errors:
                _add_flash("error", "Import errors: " + "; ".join(errors))
            if all_parsed:
                ingest_parsed_batch(db, all_parsed, "Statement Import")
                st.session_state["t5_uploader_nonce"] = nonce + 1
                close_dialog()
                st.rerun()
            elif errors:
                st.error("Import errors: " + "; ".join(errors))
            else:
                st.warning("No parseable transactions found in the uploaded statement(s).")


@st.dialog("💸 Pay Recurring Bill", width="small", on_dismiss=close_dialog)
def _render_pay_bill_dialog(
    db: DatabaseManager,
    commitment_id: int,
    month_key: str,
) -> None:
    """Modal confirmation to record payment for a recurring commitment."""
    bills = [b for b in db.get_recurring_commitments(active_only=False) if b.id == commitment_id]
    if not bills:
        close_dialog()
        st.rerun()
        return
    bill = bills[0]
    wallets = [w.account_name for w in db.get_wallets() if w.account_type != "INVESTMENT"]

    st.markdown(f"**{bill.title}** · Expected `{format_pkr(bill.expected_amount)}` (`{bill.category}`)")
    w_name = st.selectbox(
        "Pay From Wallet",
        options=wallets or ["HabibMetro"],
        key=f"dlg_pb_wallet_{commitment_id}",
    )
    c1, c2 = st.columns(2)
    with c1:
        amt = st.number_input(
            "Amount Paid (PKR)",
            min_value=1.0,
            value=float(bill.expected_amount),
            step=100.0,
            key=f"dlg_pb_amt_{commitment_id}",
        )
    with c2:
        p_date = st.date_input(
            "Payment Date",
            value=bill_date_for_month(month_key, bill.due_day),
            key=f"dlg_pb_date_{commitment_id}",
        )

    b1, b2 = st.columns([1.3, 0.8])
    with b1:
        if st.button("✅ Record Payment", key=f"dlg_pb_save_{commitment_id}", type="primary", width="stretch"):
            tx_date_str = p_date.strftime("%Y-%m-%d")
            payee_str = (
                bill.title
                if bill.match_keyword.lower() in bill.title.lower()
                else f"{bill.title} - {bill.match_keyword}"
            )
            db.add_manual_transaction(
                tx_date=tx_date_str,
                account_name=w_name,
                tx_type="DEBIT",
                amount=round(float(amt), 2),
                payee=payee_str,
                category=bill.category,
                cost_bucket=bill.cost_bucket,
                notes=f"Logged from Bills Radar [bill:{commitment_id}] for {month_key}",
                review_status="MANUALLY_VERIFIED",
            )
            invalidate_all_caches()
            notify_success(f"Logged {format_pkr(amt)} payment for '{bill.title}' from {w_name}.")
            close_dialog()
            st.rerun()
    with b2:
        if st.button("Cancel", key=f"dlg_pb_cancel_{commitment_id}", width="stretch"):
            close_dialog()
            st.rerun()


@st.dialog("📅 Recurring Bill Details", width="small", on_dismiss=close_dialog)
def _render_edit_bill_dialog(db: DatabaseManager, commitment_id: Optional[int]) -> None:
    """Modal dialog to create or edit a recurring bill/commitment."""
    existing: Optional[RecurringCommitment] = None
    if commitment_id is not None:
        matches = [b for b in db.get_recurring_commitments(active_only=False) if b.id == commitment_id]
        existing = matches[0] if matches else None

    categories, bucket_map, _ = get_category_catalog(db)
    bill_cats = categories_for_tx_type(categories, bucket_map, "DEBIT")

    title = st.text_input(
        "Bill / Subscription Title",
        value=existing.title if existing else "",
        placeholder="e.g., PTCL Fiber",
        key=f"dlg_eb_title_{commitment_id}",
    )
    keyword = st.text_input(
        "Match Keyword (case-insensitive)",
        value=existing.match_keyword if existing else "",
        placeholder="e.g., PTCL",
        key=f"dlg_eb_kw_{commitment_id}",
    )
    c1, c2 = st.columns(2)
    with c1:
        exp_amt = st.number_input(
            "Expected Amount (PKR)",
            min_value=1.0,
            value=float(existing.expected_amount) if existing else 3500.0,
            step=100.0,
            key=f"dlg_eb_amt_{commitment_id}",
        )
    with c2:
        due_day = st.number_input(
            "Due Day (1–31)",
            min_value=1,
            max_value=31,
            value=int(existing.due_day) if existing else 10,
            step=1,
            key=f"dlg_eb_day_{commitment_id}",
        )

    cat_opts = bill_cats if not existing or existing.category in bill_cats else [existing.category] + bill_cats
    cat_key = f"dlg_eb_cat_{commitment_id}"
    bkt_key = f"dlg_eb_bkt_{commitment_id}"
    if cat_key not in st.session_state or st.session_state[cat_key] not in cat_opts:
        st.session_state[cat_key] = existing.category if existing else cat_opts[0]
        sync_bucket_on_cat_change(cat_key, bkt_key, bucket_map, "DEBIT")
    if bkt_key not in st.session_state or st.session_state[bkt_key] not in _DEBIT_BUCKETS:
        st.session_state[bkt_key] = (
            existing.cost_bucket
            if existing and existing.cost_bucket in _DEBIT_BUCKETS
            else "FIXED"
        )

    c3, c4 = st.columns(2)
    with c3:
        cat_val = st.selectbox(
            "Category",
            options=cat_opts,
            key=cat_key,
            on_change=sync_bucket_on_cat_change,
            args=(cat_key, bkt_key, bucket_map, "DEBIT"),
        )
    with c4:
        bkt_val = st.selectbox(
            "Cost Bucket",
            options=list(_DEBIT_BUCKETS),
            format_func=format_bucket,
            key=bkt_key,
        )

    is_active = st.checkbox(
        "Active bill reminder",
        value=existing.is_active if existing else True,
        key=f"dlg_eb_act_{commitment_id}",
    )

    b1, b2 = st.columns([1.3, 0.8])
    with b1:
        if st.button("💾 Save Bill", key=f"dlg_eb_save_{commitment_id}", type="primary", width="stretch"):
            if not title.strip() or not keyword.strip():
                st.error("Both Title and Match Keyword are required.")
            else:
                try:
                    db.upsert_recurring_commitment(
                        RecurringCommitment(
                            id=existing.id if existing else None,
                            title=title.strip(),
                            match_keyword=keyword.strip(),
                            expected_amount=float(exp_amt),
                            due_day=int(due_day),
                            category=cat_val,
                            cost_bucket=bkt_val,  # type: ignore[arg-type]
                            is_paid_this_month=existing.is_paid_this_month if existing else False,
                            last_paid_month=existing.last_paid_month if existing else None,
                            is_active=bool(is_active),
                        )
                    )
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    notify_success(f"Saved recurring bill '{title.strip()}'.")
                    close_dialog()
                    st.rerun()
    with b2:
        if st.button("Cancel", key=f"dlg_eb_cancel_{commitment_id}", width="stretch"):
            close_dialog()
            st.rerun()


@st.dialog("🏷️ New Category", width="small", on_dismiss=close_dialog)
def _render_new_category_dialog(db: DatabaseManager) -> None:
    """Modal dialog to create or update a custom category budget."""
    name = st.text_input(
        "Category Name",
        placeholder="e.g., Pet Care, Fitness, Side Hustle",
        key="dlg_nc_name",
    )
    c1, c2 = st.columns([0.7, 1.3])
    with c1:
        emoji = st.text_input("Emoji", value="🏷️", key="dlg_nc_emoji")
    with c2:
        bkt = st.selectbox(
            "Category Group",
            options=list(_COST_BUCKETS),
            format_func=format_bucket,
            index=1,
            key="dlg_nc_bucket",
        )
    lim = 0.0
    if bkt in _DEBIT_BUCKETS:
        lim = st.number_input(
            "Monthly Limit (PKR, 0 = untracked)",
            min_value=0.0,
            value=5000.0,
            step=500.0,
            key="dlg_nc_limit",
        )
    b1, b2 = st.columns([1.3, 0.8])
    with b1:
        if st.button("➕ Save Category", key="dlg_nc_save", type="primary", width="stretch"):
            clean = " ".join(name.split()).strip()
            if not clean:
                st.error("Category Name cannot be empty.")
            else:
                saved = db.upsert_category_budget(
                    category=clean,
                    monthly_limit=float(lim) if bkt in _DEBIT_BUCKETS else 0.0,
                    emoji=emoji.strip() or "🏷️",
                    cost_bucket=bkt,  # type: ignore[arg-type]
                    is_active=True,
                )
                notify_success(f"Saved category '{saved.category}' ({saved.cost_bucket}).")
                close_dialog()
                st.rerun()
    with b2:
        if st.button("Cancel", key="dlg_nc_cancel", width="stretch"):
            close_dialog()
            st.rerun()


@st.dialog("🏷️ Edit Category", width="small", on_dismiss=close_dialog)
def _render_edit_category_dialog(db: DatabaseManager, category_id: int) -> None:
    """Modal dialog to edit, rename, change bucket, archive, or delete an existing category."""
    matches = [
        cb for cb in db.get_category_budgets(active_only=False) if cb.id == category_id
    ]
    if not matches:
        close_dialog()
        st.rerun()
        return
    cb = matches[0]
    is_uncat = cb.category == "Uncategorized"

    name = st.text_input(
        "Category Name",
        value=cb.category,
        disabled=is_uncat,
        key=f"dlg_ec_name_{category_id}",
    )
    c1, c2 = st.columns([0.7, 1.3])
    with c1:
        emoji = st.text_input("Emoji", value=cb.emoji, key=f"dlg_ec_emoji_{category_id}")
    with c2:
        bkt = st.selectbox(
            "Category Group",
            options=list(_COST_BUCKETS),
            index=_COST_BUCKETS.index(cb.cost_bucket) if cb.cost_bucket in _COST_BUCKETS else 1,
            format_func=format_bucket,
            disabled=is_uncat,
            key=f"dlg_ec_bucket_{category_id}",
        )
    lim = 0.0
    if bkt in _DEBIT_BUCKETS:
        lim = st.number_input(
            "Monthly Limit (PKR, 0 = untracked)",
            min_value=0.0,
            value=float(cb.monthly_limit),
            step=500.0,
            key=f"dlg_ec_limit_{category_id}",
        )
    is_act = st.checkbox(
        "Active category",
        value=bool(cb.is_active),
        disabled=is_uncat,
        key=f"dlg_ec_active_{category_id}",
    )

    b1, b2, b3 = st.columns([1.2, 0.85, 0.85])
    with b1:
        if st.button("💾 Save", key=f"dlg_ec_save_{category_id}", type="primary", width="stretch"):
            clean = " ".join(name.split()).strip()
            if not clean:
                st.error("Category Name cannot be empty.")
            else:
                try:
                    updated = db.update_category_budget_by_id(
                        category_id,
                        category=clean,
                        cost_bucket=bkt,  # type: ignore[arg-type]
                        monthly_limit=float(lim) if bkt in _DEBIT_BUCKETS else 0.0,
                        emoji=emoji.strip() or cb.emoji,
                        is_active=bool(is_act),
                    )
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    invalidate_all_caches()
                    notify_success(f"Updated category '{updated.category}' ({updated.cost_bucket}).")
                    close_dialog()
                    st.rerun()
    with b2:
        confirm_key = f"dlg_ec_del_confirm_{category_id}"
        if not is_uncat and not st.session_state.get(confirm_key):
            if st.button("🗑️ Delete", key=f"dlg_ec_del_arm_{category_id}", width="stretch"):
                st.session_state[confirm_key] = True
                st.rerun()
        elif not is_uncat:
            if st.button("⚠️ Confirm", key=f"dlg_ec_del_go_{category_id}", type="secondary", width="stretch"):
                st.session_state.pop(confirm_key, None)
                try:
                    moved = db.delete_category_budget(category_id)
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    invalidate_all_caches()
                    notify_success(f"Deleted '{cb.category}' ({moved} transaction(s) moved).")
                    close_dialog()
                    st.rerun()
    with b3:
        if st.button("Cancel", key=f"dlg_ec_cancel_{category_id}", width="stretch"):
            close_dialog()
            st.rerun()


def dispatch_active_dialog(db: DatabaseManager, active_month: str) -> None:
    """Render whichever modal @st.dialog is queued in session state."""
    dlg = get_active_dialog()
    if dlg is None:
        return
    apply_pending_widget_sync()
    name = dlg["name"]
    payload: dict[str, Any] = dlg.get("payload") or {}
    if name == "quick_add":
        _render_quick_add_dialog(db, active_month)
    elif name == "edit_tx" and payload.get("tx_id") is not None:
        _render_edit_tx_dialog(db, int(payload["tx_id"]))
    elif name == "sync":
        _render_sync_dialog(db)
    elif name == "pay_bill" and payload.get("commitment_id") is not None:
        _render_pay_bill_dialog(
            db,
            int(payload["commitment_id"]),
            str(payload.get("month_key") or active_month),
        )
    elif name == "edit_bill":
        _render_edit_bill_dialog(
            db,
            int(payload["commitment_id"]) if payload.get("commitment_id") is not None else None,
        )
    elif name == "new_category":
        _render_new_category_dialog(db)
    elif name == "edit_category" and payload.get("category_id") is not None:
        _render_edit_category_dialog(db, int(payload["category_id"]))
    else:
        close_dialog()

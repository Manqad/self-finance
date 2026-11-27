"""Tab 5 (Setup): Form-Isolated Wallet Balances & Defense Config, Gmail/PDF Imports, Learned Rules, & Deferred Cloud Backups."""

from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Optional

import streamlit as st

from src.database.db_manager import DatabaseManager
from src.models import ParsedTransaction
from src.security import (
    get_backup_passphrase,
    get_cloud_sync_status,
    get_session_timeout_seconds,
    is_auth_enabled,
    redact_sensitive_text,
)
from src.ui.components import format_pkr, render_section_header
from src.ui.state import (
    _add_flash,
    invalidate_all_caches,
    notify_success,
)
from src.ui.views_common import ingest_parsed_batch, run_gmail_sync


def render_sync_setup_tab(db: DatabaseManager, month_key: str) -> None:
    """Render Tab 5: Sub-tabs for Balances & Defense, Gmail & Imports, Learned Rules, Backups & Cloud."""
    import src.ui.views as views_facade

    render_section_header(
        "⚙️ Sync, Calibration & Security Vault",
        "Manage wallet balance anchors, Defense thresholds, ingestion, learned rules & encrypted backups",
    )

    tab_bal, tab_sync, tab_rules, tab_bak = st.tabs(
        [
            "🏦 Balances & Defense",
            "📧 Gmail & Imports",
            "🧠 Learned Rules",
            "🔐 Backups & Cloud",
        ]
    )

    with tab_bal:
        col_bal, col_cfg = st.columns(2, gap="medium")

        with col_bal:
            wallets = db.get_wallets()
            with st.container(border=True):
                st.subheader("🏦 Calibrate Wallet Balances")
                _bal_mode_labels = {
                    "Match Current Live Balance (As of Now)": "⚡ Live Balance (As of Now — Recommended)",
                    "Set Historical Opening Balance (Before Tracked Txns)": "📜 Historical Starting Balance (Pre-Txns)",
                }
                bal_mode = st.radio(
                    "Balance Entry Mode",
                    options=[
                        "Match Current Live Balance (As of Now)",
                        "Set Historical Opening Balance (Before Tracked Txns)",
                    ],
                    format_func=lambda opt: _bal_mode_labels.get(opt, opt),
                    key="t5_wallet_balance_mode",
                )
                is_live_mode = bal_mode.startswith("Match Current Live")

                if is_live_mode:
                    st.caption(
                        "Enter each wallet's **exact balance right now** and save. We stamp `As of Now` across all 4 wallets "
                        "so earlier statements stay in analytics without double-subtracting."
                    )
                    new_currents: dict[str, tuple[float, float, Optional[str]]] = {}
                    for w in wallets:
                        slug = re.sub(r"[^A-Za-z0-9]+", "_", w.account_name)
                        cur_key = f"t5_cur_bal_{slug}"
                        st.session_state.setdefault(cur_key, float(w.current_balance))
                        anchor_note = (
                            f"Anchored {w.balance_as_of} at {format_pkr(w.opening_balance)}"
                            if w.balance_as_of
                            else f"Unanchored (Opening: {format_pkr(w.opening_balance)})"
                        )
                        val = st.number_input(
                            f"{w.icon} {w.account_name} — Live Balance (PKR)",
                            step=1000.0,
                            help=anchor_note,
                            key=cur_key,
                        )
                        new_currents[w.account_name] = (
                            float(val),
                            float(w.current_balance),
                            w.balance_as_of,
                        )

                    if st.button(
                        "💾 Save Live Balances",
                        key="t5_save_live_bal_btn",
                        type="primary",
                        width="stretch",
                    ):
                        changed = sum(
                            1
                            for nv, ov, cur_as_of in new_currents.values()
                            if abs(nv - ov) >= 0.01 or not cur_as_of
                        )
                        batch_ts = db.calibrate_wallets_live_batch(
                            {acct: vals[0] for acct, vals in new_currents.items()}
                        )
                        invalidate_all_caches()
                        notify_success(
                            f"Anchored {len(new_currents)} wallet(s) as of {batch_ts} ({changed} updated)."
                        )
                        st.rerun()
                else:
                    st.caption(
                        "Advanced: set pre-history starting balances from BEFORE all tracked transactions "
                        "(clears `As of` anchors so full history computes live balances)."
                    )
                    new_openings: dict[str, tuple[float, float, Optional[str]]] = {}
                    for w in wallets:
                        slug = re.sub(r"[^A-Za-z0-9]+", "_", w.account_name)
                        open_key = f"t5_open_bal_{slug}"
                        st.session_state.setdefault(open_key, float(w.opening_balance))
                        val = st.number_input(
                            f"{w.icon} {w.account_name} — Opening Before Txns (Live: {format_pkr(w.current_balance)})",
                            step=1000.0,
                            key=open_key,
                        )
                        new_openings[w.account_name] = (
                            float(val),
                            float(w.opening_balance),
                            w.balance_as_of,
                        )

                    if st.button(
                        "💾 Save Historical Opening Balances",
                        key="t5_save_open_bal_btn",
                        type="primary",
                        width="stretch",
                    ):
                        changed = 0
                        for acct_name, (nv, ov, cur_as_of) in new_openings.items():
                            if abs(nv - ov) >= 0.01 or cur_as_of is not None:
                                db.update_wallet_opening_balance(acct_name, nv)
                                changed += 1
                        invalidate_all_caches()
                        notify_success(
                            f"Updated historical starting balances ({changed} wallet(s) modified)."
                        )
                        st.rerun()

        with col_cfg:
            cfg = db.get_budget_config()
            if st.session_state.pop("t5_cfg_reload", False):
                st.session_state["t5_cfg_income"] = float(cfg.monthly_income)
                st.session_state["t5_cfg_fixed"] = float(cfg.fixed_cost_baseline)
                st.session_state["t5_cfg_var"] = float(cfg.variable_spending_cap)
                st.session_state["t5_cfg_mult"] = int(cfg.emergency_multiplier)
                st.session_state["t5_cfg_env"] = bool(cfg.envelope_mode)
            else:
                st.session_state.setdefault("t5_cfg_income", float(cfg.monthly_income))
                st.session_state.setdefault("t5_cfg_fixed", float(cfg.fixed_cost_baseline))
                st.session_state.setdefault("t5_cfg_var", float(cfg.variable_spending_cap))
                st.session_state.setdefault("t5_cfg_mult", int(cfg.emergency_multiplier))
                st.session_state.setdefault("t5_cfg_env", bool(cfg.envelope_mode))

            with st.container(border=True):
                st.subheader("🛡️ Month 1 Treasury Defense Config")
                cfg_income = st.number_input(
                    "Monthly Income (PKR)", min_value=0.0, step=5000.0, key="t5_cfg_income"
                )
                cfg_fixed = st.number_input(
                    "Fixed Cost Baseline (PKR)", min_value=0.0, step=2500.0, key="t5_cfg_fixed"
                )
                cfg_var = st.number_input(
                    "Variable Spending Cap (PKR)", min_value=0.0, step=2500.0, key="t5_cfg_var"
                )
                cfg_mult = st.number_input(
                    "Emergency Reserve Multiplier (Months)",
                    min_value=1,
                    max_value=24,
                    step=1,
                    key="t5_cfg_mult",
                )
                cfg_env = st.checkbox(
                    "Enable NayaPay Envelope Mode",
                    key="t5_cfg_env",
                    help="Counts HabibMetro → NayaPay sweeps as burn while excluding granular NayaPay debits from burn cap.",
                )
                preview_cap = float(cfg_fixed) + float(cfg_var)
                preview_em = float(cfg_fixed) * int(cfg_mult)
                st.caption(
                    f"Live Preview — Cap: **{format_pkr(preview_cap)}** • Emergency Target: **{format_pkr(preview_em)}**"
                )
                if st.button(
                    "💾 Save Defense Configuration",
                    key="t5_save_budget_btn",
                    type="primary",
                    width="stretch",
                ):
                    db.update_budget_config(
                        monthly_income=float(cfg_income),
                        fixed_cost_baseline=float(cfg_fixed),
                        variable_spending_cap=float(cfg_var),
                        emergency_multiplier=int(cfg_mult),
                        envelope_mode=bool(cfg_env),
                    )
                    st.session_state["t5_cfg_reload"] = True
                    notify_success(
                        f"Updated Defense Config (Cap: {format_pkr(preview_cap)}, Target: {format_pkr(preview_em)})."
                    )
                    st.rerun()

    with tab_sync:
        col_gmail, col_import = st.columns(2, gap="medium")
        with col_gmail:
            with st.container(border=True):
                st.subheader("📧 Gmail Bank Alert Sync")
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
                    key="t5_gmail_max_emails",
                )
                if st.button("🔄 Sync Gmail Now", key="t5_gmail_sync_btn", type="primary", width="stretch"):
                    with st.spinner("Scanning Gmail for NayaPay & HabibMetro alerts..."):
                        run_gmail_sync(db, int(max_emails))
                    st.rerun()

                with st.expander("📖 Gmail OAuth2 & IMAP Setup Guide", expanded=False):
                    st.markdown(
                        "**Option A — Google OAuth2:** Place `credentials.json` in root & click Sync Gmail.\n\n"
                        "**Option B — IMAP App Password:** Set `GMAIL_USER` and `GMAIL_APP_PASSWORD` in `.env`."
                    )

        with col_import:
            with st.container(border=True):
                st.subheader("📄 Multi-Bank PDF & CSV Importer")
                uploader_nonce = int(st.session_state.get("t5_uploader_nonce", 0))
                uploaded_files = st.file_uploader(
                    "Drop NayaPay or HabibMetro PDF / CSV Statements",
                    type=["pdf", "csv"],
                    accept_multiple_files=True,
                    key=f"t5_statement_files_{uploader_nonce}",
                )
                hint_choice = st.selectbox(
                    "Statement Source Hint",
                    options=["Auto-Detect", "NayaPay", "HabibMetro"],
                    key="t5_statement_hint",
                )
                if st.button("📥 Import Statement(s)", key="t5_import_btn", type="primary", width="stretch"):
                    if not uploaded_files:
                        st.warning("Please upload one or more `.pdf` or `.csv` files first.")
                    else:
                        acct_hint = None if hint_choice == "Auto-Detect" else hint_choice
                        all_parsed: list[ParsedTransaction] = []
                        errors: list[str] = []
                        for up_file in uploaded_files:
                            try:
                                all_parsed.extend(
                                    views_facade.import_statement_file(
                                        up_file.getvalue(),
                                        up_file.name,
                                        account_hint=acct_hint,
                                    )
                                )
                            except Exception as exc:
                                errors.append(f"{up_file.name}: {exc}")

                        if errors and all_parsed:
                            _add_flash("error", "Import errors: " + "; ".join(errors))
                            ingest_parsed_batch(db, all_parsed, "Statement Import")
                            st.session_state["t5_uploader_nonce"] = uploader_nonce + 1
                            st.rerun()
                        elif all_parsed:
                            ingest_parsed_batch(db, all_parsed, "Statement Import")
                            st.session_state["t5_uploader_nonce"] = uploader_nonce + 1
                            st.rerun()
                        elif errors:
                            st.error("Import errors: " + "; ".join(errors))
                        else:
                            st.warning("No parseable transactions found in uploaded file(s).")

        with st.container(border=True):
            st.subheader("🧹 Ledger Maintenance & Sample Fixtures")
            with st.container(key="ptos_compact_t5_maint"):
                m1, m2 = st.columns(2)
                with m1:
                    if st.button("🔗 Auto-Link Mirrors & Reconcile Payees", key="t5_reconcile_btn", width="stretch"):
                        linked = db.auto_link_internal_transfer_mirrors(day_window=2)
                        rec_stats = db.reconcile_existing_duplicates_and_payees()
                        invalidate_all_caches()
                        notify_success(
                            f"Linked {linked} transfer pair(s); merged {rec_stats.get('merged', 0)} duplicate(s)."
                        )
                        st.rerun()
                with m2:
                    if st.button("🧪 Load Sample Fixtures", key="t5_load_fixtures_btn", width="stretch"):
                        fixture_txs, loaded_names = views_facade.load_html_fixtures(
                            views_facade._FIXTURES_DIR
                        )
                        if not loaded_names:
                            st.warning("No `.html` fixture files found in `tests/fixtures/` yet.")
                        elif not fixture_txs:
                            st.warning(f"Scanned {', '.join(loaded_names)} but parsed 0 transactions.")
                        else:
                            ingest_parsed_batch(db, fixture_txs, f"Fixtures ({', '.join(loaded_names)})")
                            st.rerun()

    with tab_rules:
        st.subheader("🧠 Learned Categorization Rules")
        st.caption(
            "User-trained priority-100 rules override built-in defaults. Delete outdated or overly broad rules in one tap."
        )
        rc1, rc2 = st.columns([2.0, 1.0], vertical_alignment="bottom")
        with rc1:
            rule_search = st.text_input(
                "Filter Rules",
                placeholder="Search keyword, alias, category...",
                key="t5_rule_search",
            )
        with rc2:
            show_defaults = st.checkbox("Show built-in seed rules too", value=False, key="t5_rule_show_defaults")

        all_rules = db.get_category_rules()
        visible_rules = [
            r
            for r in all_rules
            if (show_defaults or r.is_user_defined)
            and (
                not rule_search.strip()
                or rule_search.strip().lower()
                in f"{r.keyword} {r.merchant_alias or ''} {r.category} {r.cost_bucket}".lower()
            )
        ]
        if not visible_rules:
            st.info("No matching categorization rules found.")
        else:
            for r in visible_rules[:50]:
                rid = r.id or 0
                origin = "User Rule (P100)" if r.is_user_defined else f"Seed Rule (P{r.priority})"
                tgt = f" → {r.target_account}" if r.target_account else ""
                with st.container(border=True):
                    with st.container(key=f"ptos_compact_t5_rule_{rid}"):
                        rl, rr = st.columns([3.5, 0.75], vertical_alignment="center")
                        with rl:
                            st.markdown(
                                f"<code>{html.escape(r.keyword)}</code> → **{html.escape(r.category)}** "
                                f"(`{html.escape(r.cost_bucket)}`) {html.escape(tgt)}<br>"
                                f"<span style='font-size:0.75rem;color:#94A3B8;'>"
                                f"Alias: {html.escape(r.merchant_alias or '—')} • {origin}"
                                f"</span>",
                                unsafe_allow_html=True,
                            )
                        with rr:
                            if r.is_user_defined and rid > 0:
                                if st.button("🗑️", key=f"t5_del_rule_{rid}", help="Delete learned rule", width="stretch"):
                                    db.delete_category_rule(rid)
                                    notify_success(f"Deleted rule '{r.keyword}'.")
                                    st.rerun()

    with tab_bak:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        with st.container(border=True):
            st.subheader("💾 1-Click CSV & JSON Backups")
            st.caption(
                f"On-demand downloads generate only when clicked (active month: {month_key})."
            )
            with st.container(key="ptos_compact_t5_bak"):
                b1, b2, b3 = st.columns(3)
                with b1:
                    if st.button("💾 Save Local Snapshot", key="t5_save_disk_backup", width="stretch"):
                        try:
                            json_p, csv_p = db.save_backup_to_disk(views_facade._BACKUP_DIR)
                        except OSError as exc:
                            st.error(f"Could not write backup files: {exc}")
                        else:
                            notify_success(
                                f"Saved backups to `{json_p.as_posix()}` and `{csv_p.as_posix()}`.",
                                persistent=True,
                            )
                            st.rerun()
                with b2:
                    st.download_button(
                        label="⬇️ JSON Backup",
                        data=lambda: db.export_backup_json(),
                        file_name=f"treasury_backup_{stamp}.json",
                        mime="application/json",
                        on_click="ignore",
                        key="t5_dl_json",
                        width="stretch",
                    )
                with b3:
                    st.download_button(
                        label="⬇️ CSV Ledger",
                        data=lambda: db.export_transactions_csv(),
                        file_name=f"transactions_export_{stamp}.csv",
                        mime="text/csv",
                        on_click="ignore",
                        key="t5_dl_csv",
                        width="stretch",
                    )

        with st.container(border=True):
            st.subheader("🔐 Security, Encrypted Backups & Cloud Vault")
            auth_on = is_auth_enabled()
            timeout_sec = get_session_timeout_seconds()
            cloud_status = get_cloud_sync_status()
            timeout_label = (
                f"{max(1, timeout_sec // 60)} min idle lock" if timeout_sec > 0 else "No idle timeout"
            )
            if auth_on:
                st.success(f"🔒 App Lock Screen: ACTIVE ({timeout_label})")
            else:
                st.info(
                    "🔓 App Lock Screen: OFF — set `TREASURY_APP_PASSWORD_HASH` or `TREASURY_APP_PASSWORD`."
                )

            if cloud_status["configured"]:
                st.success(f"☁️ Encrypted Cloud Vault ({cloud_status['provider']}): {cloud_status['detail']}")
            else:
                st.info(f"☁️ Encrypted Cloud Vault: {cloud_status['detail']}")

            last_cloud = st.session_state.get("ptos_last_cloud_sync")
            if isinstance(last_cloud, tuple) and len(last_cloud) == 2:
                last_ok, last_msg = last_cloud
                st.caption(
                    f"Last Cloud Sync: {'✅' if last_ok else '⚠️'} {redact_sensitive_text(last_msg)}"
                )

            configured_pass = get_backup_passphrase()
            if configured_pass:
                enc_pass = ""
                effective_pass = configured_pass
            else:
                enc_pass = st.text_input(
                    "Vault Encryption Passphrase (required if TREASURY_BACKUP_KEY is not configured)",
                    type="password",
                    key="t5_enc_passphrase",
                )
                effective_pass = (enc_pass or "").strip()

            with st.container(key="ptos_compact_t5_cloud"):
                ev1, ev2, ev3 = st.columns(3)
                with ev1:
                    if effective_pass:
                        st.download_button(
                            label="🔐 Download Encrypted",
                            data=lambda p=effective_pass: db.export_encrypted_backup(passphrase=p),
                            file_name=f"treasury_vault_{stamp}.ptos.enc",
                            mime="application/json",
                            on_click="ignore",
                            key="t5_dl_enc",
                            width="stretch",
                        )
                    else:
                        st.button(
                            "🔐 Download Encrypted",
                            disabled=True,
                            key="t5_dl_enc_disabled",
                            width="stretch",
                            help="Enter passphrase above or set TREASURY_BACKUP_KEY.",
                        )
                with ev2:
                    can_cloud_action = bool(cloud_status.get("backend_ready") and effective_pass)
                    if st.button("☁️ Push to Cloud", key="t5_cloud_push_btn", width="stretch", disabled=not can_cloud_action):
                        ok_push, msg_push = db.sync_to_cloud_vault(passphrase=(enc_pass or "").strip() or None)
                        st.session_state["ptos_synced_changes"] = getattr(db, "change_counter", 0)
                        st.session_state["ptos_last_cloud_sync"] = (ok_push, msg_push)
                        if ok_push:
                            notify_success(msg_push, persistent=True)
                        else:
                            _add_flash("error", msg_push)
                        st.rerun()
                with ev3:
                    if st.button("📥 Pull from Cloud", key="t5_cloud_pull_btn", width="stretch", disabled=not can_cloud_action):
                        ok_pull, msg_pull = db.restore_from_cloud_vault(passphrase=(enc_pass or "").strip() or None)
                        if ok_pull:
                            invalidate_all_caches()
                            st.session_state["ptos_synced_changes"] = getattr(db, "change_counter", 0)
                            notify_success(msg_pull, persistent=True)
                        else:
                            _add_flash("error", msg_pull)
                        st.session_state["ptos_last_cloud_sync"] = (ok_pull, msg_pull)
                        st.rerun()

            restore_file = st.file_uploader(
                "Restore from Encrypted (.ptos.enc / .enc) or Checksummed (.json) Backup",
                type=["enc", "json"],
                key="t5_restore_uploader",
            )
            if restore_file is not None:
                if st.button("♻️ Restore Uploaded Backup", key="t5_restore_vault_btn", type="primary", width="stretch"):
                    raw_up = restore_file.getvalue()
                    if len(raw_up) > 25 * 1024 * 1024:
                        st.error("Backup file exceeds the 25 MB safety limit.")
                    else:
                        text_up = raw_up.decode("utf-8", errors="replace")
                        try:
                            if restore_file.name.lower().endswith(".enc"):
                                summary = db.restore_encrypted_backup(
                                    text_up, passphrase=(enc_pass or "").strip() or None
                                )
                            else:
                                summary = db.restore_backup_json(text_up)
                        except Exception as exc:
                            st.error(f"Restore failed: {redact_sensitive_text(exc)}")
                        else:
                            invalidate_all_caches()
                            st.session_state["ptos_synced_changes"] = getattr(db, "change_counter", 0)
                            notify_success(
                                f"Restored {summary['transactions']} transaction(s) and {summary['accounts']} wallet(s).",
                                persistent=True,
                            )
                            st.rerun()

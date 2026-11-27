"""Slim Streamlit Community Cloud entrypoint for Personal Treasury OS."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st
from dotenv import load_dotenv

from src.database.db_manager import DatabaseManager
from src.engine.metrics import compute_dashboard_metrics
from src.security import (
    get_auth_rate_limiter,
    get_cloud_sync_status,
    get_local_now,
    get_secret,
    get_session_timeout_seconds,
    is_auth_enabled,
    verify_password,
)
from src.ui.dialogs import dispatch_active_dialog
from src.ui.state import (
    ALL_TABS,
    SLUG_TO_TAB,
    TAB_BUDGETS,
    TAB_HOME,
    TAB_INSIGHTS,
    TAB_SETUP,
    TAB_TO_SLUG,
    TAB_TRANSACTIONS,
    drain_toasts,
    maybe_schedule_cloud_push,
    open_dialog,
    pull_background_cloud_status,
    render_flash_banner,
)
from src.ui.styles import inject_styles
from src.ui.views import (
    render_budgets_tab,
    render_home_tab,
    render_insights_tab,
    render_sync_setup_tab,
    render_transactions_tab,
)


@st.cache_resource
def _get_db_manager(db_path: str) -> DatabaseManager:
    """Return a cached thread-safe SQLite DatabaseManager for db_path."""
    import src.database.db_manager as db_mod

    return db_mod.DatabaseManager(db_path=db_path)


def _resolve_default_month(db: DatabaseManager, available_months: list[str]) -> str:
    """Default to current PKT month if populated, else latest month with transactions."""
    current_month = get_local_now().strftime("%Y-%m")
    counts = db.get_month_transaction_counts()
    if counts.get(current_month, 0) > 0:
        return current_month
    for month_key in available_months:
        if counts.get(month_key, 0) > 0:
            return month_key
    return available_months[0] if available_months else current_month


def _enforce_authentication_gate() -> bool:
    """Enforce zero-trust lock screen with PBKDF2 verification, rate limit, and idle timeout."""
    if not is_auth_enabled():
        return True

    timeout_sec = get_session_timeout_seconds()
    if st.session_state.get("ptos_auth_ok"):
        if timeout_sec > 0:
            last_active = float(st.session_state.get("ptos_auth_last_active", 0.0))
            if (time.monotonic() - last_active) > timeout_sec:
                st.session_state["ptos_auth_ok"] = False
                st.session_state["ptos_auth_notice"] = (
                    "Session locked due to inactivity. Please enter your password/PIN to continue."
                )
            else:
                st.session_state["ptos_auth_last_active"] = time.monotonic()
                return True
        else:
            return True

    notice = st.session_state.get("ptos_auth_notice")
    has_cred = bool(
        get_secret("TREASURY_APP_PASSWORD_HASH") or get_secret("TREASURY_APP_PASSWORD")
    )

    with st.container(key="ptos_lock_shell"):
        with st.container(border=True):
            st.markdown(
                '<div class="ptos-lock-hero">'
                '<div class="ptos-lock-icon">🔐</div>'
                '<div class="ptos-lock-title">Personal Treasury OS</div>'
                '<div class="ptos-lock-sub">Authenticate to access your encrypted multi-wallet ledger.</div>'
                '<span class="ptos-lock-badge">Zero-Trust Vault • PBKDF2-SHA256</span>'
                "</div>",
                unsafe_allow_html=True,
            )
            if notice:
                st.info(notice)

            if not has_cred:
                st.error(
                    "Authentication is required (`TREASURY_REQUIRE_AUTH=1`), "
                    "but neither `TREASURY_APP_PASSWORD_HASH` nor `TREASURY_APP_PASSWORD` is configured."
                )
                return False

            limiter = get_auth_rate_limiter()
            locked, remaining_sec = limiter.is_locked()
            if locked:
                st.error(
                    f"Too many failed unlock attempts. Please wait {remaining_sec} second(s) before retrying."
                )

            with st.form(key="ptos_lock_form", clear_on_submit=True):
                candidate = st.text_input(
                    "Vault Password / PIN",
                    type="password",
                    key="ptos_lock_password",
                    disabled=locked,
                )
                submitted = st.form_submit_button(
                    "🔓 Unlock Vault",
                    type="primary",
                    width="stretch",
                    disabled=locked,
                )

            if submitted and not locked:
                if verify_password(candidate):
                    limiter.record_success()
                    st.session_state["ptos_auth_ok"] = True
                    st.session_state["ptos_auth_last_active"] = time.monotonic()
                    st.session_state.pop("ptos_auth_notice", None)
                    st.rerun()
                else:
                    cooldown = limiter.record_failure()
                    if cooldown > 0:
                        st.rerun()
                    else:
                        left = max(0, 5 - limiter.failure_count())
                        st.error(
                            f"Invalid password or PIN ({left} attempt(s) left before cooldown)."
                        )

    return False


def _ensure_fresh_db_manager(db_path: str) -> DatabaseManager:
    """Return cached DatabaseManager, reloading cleanly if code hot-reloaded in dev."""
    db = _get_db_manager(db_path)
    if not hasattr(db, "has_any_transactions") or not hasattr(db, "delete_category_rule"):
        try:
            db.close()
        except Exception:
            pass
        if hasattr(_get_db_manager, "clear"):
            _get_db_manager.clear()
        db = _get_db_manager(db_path)
    return db


def render_app(db: Optional[DatabaseManager] = None) -> None:
    """Render Personal Treasury OS with responsive command bar, segmented nav, and modals."""
    inject_styles()

    if db is None and "PYTEST_CURRENT_TEST" not in os.environ:
        load_dotenv(override=False)

    if not _enforce_authentication_gate():
        return

    if db is None:
        db_path = os.getenv("TREASURY_DB_PATH", "data/treasury.db")
        db = _ensure_fresh_db_manager(db_path)

    pull_background_cloud_status(str(db.db_path))

    if (
        get_cloud_sync_status().get("configured")
        and get_secret("TREASURY_AUTO_RESTORE", "1").lower()
        not in {"0", "false", "no", "off"}
        and not st.session_state.get("ptos_cloud_boot_checked")
    ):
        st.session_state["ptos_cloud_boot_checked"] = True
        try:
            ok_boot = db._auto_restore_from_cloud_if_empty()
            st.session_state["ptos_synced_changes"] = getattr(db, "change_counter", 0)
            if ok_boot:
                st.session_state["ptos_last_cloud_sync"] = (True, "Auto-restored encrypted vault at boot.")
        except Exception as exc:
            db.cloud_boot_restore_failed = True
            st.session_state["ptos_last_cloud_sync"] = (False, f"Boot restore skipped: {exc}")

    st.session_state.setdefault("ptos_synced_changes", getattr(db, "change_counter", 0))

    available_months = db.get_available_months()
    if (
        "ptos_active_month" not in st.session_state
        or st.session_state["ptos_active_month"] not in available_months
    ):
        st.session_state["ptos_active_month"] = _resolve_default_month(db, available_months)

    config = db.get_budget_config()
    auth_active = is_auth_enabled()
    cloud_configured = bool(get_cloud_sync_status().get("configured"))

    # Top Command Bar
    hdr_left, hdr_right = st.columns([1.85, 1.55], vertical_alignment="center")
    with hdr_left:
        env_badge = '<span class="ptos-mode-pill">Envelope Mode</span>' if config.envelope_mode else ""
        cloud_badge = '<span class="ptos-sync-dot">☁️ Vault Synced</span>' if cloud_configured else ""
        st.markdown(
            f'<div class="ptos-brand-wrap">'
            f'<div class="ptos-brand-title">🛡️ Personal Treasury OS</div>'
            f'<div class="ptos-brand-sub">'
            f"<span>Multi-Wallet Treasury • Month 1 Defense</span>"
            f"{env_badge}{cloud_badge}"
            f"</div>"
            f"</div>",
            unsafe_allow_html=True,
        )
    with hdr_right:
        with st.container(key="ptos_compact_hdr_controls"):
            if auth_active:
                c_m, c_add, c_sync, c_lock = st.columns([1.45, 0.85, 0.85, 0.7], vertical_alignment="center")
            else:
                c_m, c_add, c_sync = st.columns([1.55, 0.9, 0.9], vertical_alignment="center")
                c_lock = None
            with c_m:
                active_month = st.selectbox(
                    "Active Month",
                    options=available_months,
                    key="ptos_active_month",
                    label_visibility="collapsed",
                )
            with c_add:
                if st.button("➕ Add", key="ptos_top_add_btn", type="primary", width="stretch"):
                    open_dialog("quick_add")
                    st.rerun()
            with c_sync:
                if st.button("🔄 Sync", key="ptos_top_sync_btn", width="stretch"):
                    open_dialog("sync")
                    st.rerun()
            if c_lock is not None:
                with c_lock:
                    if st.button("🔒", key="ptos_lock_btn", help="Lock Vault", width="stretch"):
                        st.session_state["ptos_auth_ok"] = False
                        st.session_state["ptos_auth_notice"] = "Vault locked manually."
                        st.rerun()

    # Segmented Tab Bar (guarded against clicking active segment -> None)
    pending_tab = st.session_state.pop("ptos_pending_tab", None)
    if pending_tab in ALL_TABS:
        st.session_state["ptos_active_tab"] = pending_tab
    elif (
        "ptos_active_tab" not in st.session_state
        or st.session_state["ptos_active_tab"] not in ALL_TABS
    ):
        qp_tab = st.query_params.get("tab", "")
        if qp_tab in ALL_TABS:
            st.session_state["ptos_active_tab"] = qp_tab
        elif qp_tab in SLUG_TO_TAB:
            st.session_state["ptos_active_tab"] = SLUG_TO_TAB[qp_tab]
        else:
            st.session_state["ptos_active_tab"] = TAB_HOME

    with st.container(key="ptos_nav_container"):
        selected_tab = st.segmented_control(
            "Navigation",
            options=list(ALL_TABS),
            selection_mode="single",
            key="ptos_active_tab",
            width="stretch",
            label_visibility="collapsed",
        )
    active_tab = (
        selected_tab
        if selected_tab in ALL_TABS
        else st.session_state.get("ptos_last_valid_tab", TAB_HOME)
    )
    if active_tab not in ALL_TABS:
        active_tab = TAB_HOME
    st.session_state["ptos_last_valid_tab"] = active_tab
    st.query_params["tab"] = TAB_TO_SLUG.get(active_tab, "home")

    render_flash_banner()

    if active_tab == TAB_HOME:
        metrics = compute_dashboard_metrics(db, active_month)
        render_home_tab(db, metrics, active_month)
    elif active_tab == TAB_TRANSACTIONS:
        render_transactions_tab(db, active_month)
    elif active_tab == TAB_BUDGETS:
        metrics = compute_dashboard_metrics(db, active_month)
        render_budgets_tab(db, metrics, active_month)
    elif active_tab == TAB_INSIGHTS:
        metrics = compute_dashboard_metrics(db, active_month)
        render_insights_tab(db, metrics, active_month)
    elif active_tab == TAB_SETUP:
        render_sync_setup_tab(db, active_month)

    dispatch_active_dialog(db, active_month)
    drain_toasts()
    maybe_schedule_cloud_push(db)


def main() -> None:
    """Streamlit entrypoint configuring page settings and rendering Personal Treasury OS."""
    st.set_page_config(
        page_title="Personal Treasury OS",
        page_icon="🛡️",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    render_app()


if __name__ == "__main__":
    main()

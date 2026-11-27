"""Centralized Streamlit session state, navigation, notifications, dialogs, and non-blocking Cloud Vault push."""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Optional

import streamlit as st

from src.database.db_manager import DatabaseManager
from src.models import CostBucket, TxType
from src.security import get_cloud_sync_status, get_secret

logger = logging.getLogger(__name__)

TAB_HOME = "🏠 Home"
TAB_TRANSACTIONS = "📋 Ledger"
TAB_BUDGETS = "🎯 Budgets"
TAB_INSIGHTS = "📊 Insights"
TAB_SETUP = "⚙️ Setup"
ALL_TABS: tuple[str, ...] = (
    TAB_HOME,
    TAB_TRANSACTIONS,
    TAB_BUDGETS,
    TAB_INSIGHTS,
    TAB_SETUP,
)

TAB_TO_SLUG: dict[str, str] = {
    TAB_HOME: "home",
    TAB_TRANSACTIONS: "ledger",
    TAB_BUDGETS: "budgets",
    TAB_INSIGHTS: "insights",
    TAB_SETUP: "setup",
}
SLUG_TO_TAB: dict[str, str] = {
    "home": TAB_HOME,
    "ledger": TAB_TRANSACTIONS,
    "transactions": TAB_TRANSACTIONS,
    "budgets": TAB_BUDGETS,
    "insights": TAB_INSIGHTS,
    "setup": TAB_SETUP,
}

_COST_BUCKETS: tuple[CostBucket, ...] = (
    "FIXED",
    "VARIABLE",
    "LEAKAGE",
    "INFLOW",
    "INVESTMENT",
    "IGNORE",
)
_DEBIT_BUCKETS: tuple[CostBucket, ...] = ("FIXED", "VARIABLE", "LEAKAGE")
_TX_TYPES: tuple[TxType, ...] = (
    "DEBIT",
    "CREDIT",
    "TRANSFER_INTERNAL",
    "TRANSFER_MMF",
)
_BUCKET_LABELS: dict[str, str] = {
    "FIXED": "FIXED — Fixed Essentials (Bills & Rent)",
    "VARIABLE": "VARIABLE — Flexible Spending",
    "LEAKAGE": "LEAKAGE — Taxes, Fees & Friction",
    "INFLOW": "INFLOW — Income / Refund",
    "INVESTMENT": "INVESTMENT — Money Market Fund",
    "IGNORE": "IGNORE — Internal Wallet Move",
}
_TX_TYPE_LABELS: dict[str, str] = {
    "DEBIT": "DEBIT — Expense",
    "CREDIT": "CREDIT — Income",
    "TRANSFER_INTERNAL": "TRANSFER_INTERNAL — Own Transfer",
    "TRANSFER_MMF": "TRANSFER_MMF — Sweep to MMF",
}
_NON_DEBIT_SYSTEM_CATEGORIES: frozenset[str] = frozenset(
    {
        "Salary & Income",
        "Other Inflow",
        "Money Market Fund",
        "Wallet Top-Up",
        "Cash Withdrawal",
        "Internal Transfer",
        "MMF Redemption",
    }
)
_SYSTEM_CATEGORY_BUCKETS: dict[str, tuple[CostBucket, str]] = {
    "Salary & Income": ("INFLOW", "💰"),
    "Other Inflow": ("INFLOW", "📥"),
    "Money Market Fund": ("INVESTMENT", "📈"),
    "Wallet Top-Up": ("IGNORE", "🔁"),
    "Cash Withdrawal": ("IGNORE", "🏧"),
    "Internal Transfer": ("IGNORE", "🔄"),
    "MMF Redemption": ("IGNORE", "🏦"),
    "Uncategorized": ("VARIABLE", "❓"),
}
_TX_EDITOR_PREFIXES = (
    "t2_edit_",
    "t1_qt_",
    "dlg_ed_",
    "dlg_qa_",
)
_BALANCE_INPUT_PREFIXES = (
    "t5_cur_bal_",
    "t5_open_bal_",
)

_CLOUD_PUSH_LOCK = threading.Lock()
_CLOUD_PUSH_STATE: dict[str, dict[str, Any]] = {}


def invalidate_all_caches() -> None:
    """Clear all widget/editor/balance caches and Bump revision counter after mutations."""
    for key in list(st.session_state.keys()):
        if isinstance(key, str) and (
            key.startswith(_TX_EDITOR_PREFIXES)
            or key.startswith(_BALANCE_INPUT_PREFIXES)
        ):
            st.session_state.pop(key, None)
    st.session_state["ptos_data_rev"] = int(st.session_state.get("ptos_data_rev", 0)) + 1


def notify_success(message: str, *, persistent: bool = False) -> None:
    """Queue a crisp toast notification (and optional persistent banner for imports/restores)."""
    toasts = list(st.session_state.get("ptos_toasts") or [])
    toasts.append(("✅", str(message)))
    st.session_state["ptos_toasts"] = toasts
    if persistent:
        _add_flash("success", message)


def _add_flash(level: str, message: str) -> None:
    """Queue a post-rerun flash message without clobbering earlier messages."""
    flashes = list(st.session_state.get("ptos_flashes") or [])
    flashes.append((level, message))
    st.session_state["ptos_flashes"] = flashes


def _set_flash(level: str, message: str) -> None:
    """Queue a flash banner/toast (routine successes use toast; errors/warnings/info stay visible)."""
    if level == "success":
        notify_success(message, persistent=False)
    else:
        _add_flash(level, message)


def drain_toasts() -> None:
    """Emit all queued toasts once cleanly."""
    queued = st.session_state.pop("ptos_toasts", None)
    if not isinstance(queued, list):
        return
    for item in queued:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            icon, msg = str(item[0]), str(item[1])
            try:
                st.toast(msg, icon=icon)
            except Exception:
                pass


def render_flash_banner() -> None:
    """Display persistent warnings, errors, infos, and explicit persistent banners."""
    flashes: list[tuple[str, str]] = []
    legacy_flash = st.session_state.pop("ptos_flash", None)
    if legacy_flash and isinstance(legacy_flash, (tuple, list)) and len(legacy_flash) == 2:
        flashes.append((str(legacy_flash[0]), str(legacy_flash[1])))
    queued = st.session_state.pop("ptos_flashes", None)
    if isinstance(queued, list):
        for item in queued:
            if isinstance(item, (tuple, list)) and len(item) == 2:
                flashes.append((str(item[0]), str(item[1])))

    for level, message in flashes:
        if level == "success":
            st.success(message)
        elif level == "warning":
            st.warning(message)
        elif level == "error":
            st.error(message)
        else:
            st.info(message)


def open_dialog(name: str, **payload: Any) -> None:
    """Open a named modal dialog with optional payload."""
    st.session_state["ptos_active_dialog"] = {"name": name, "payload": payload}


def close_dialog() -> None:
    """Clear the active modal dialog state and transient modal form keys."""
    st.session_state.pop("ptos_active_dialog", None)
    for k in list(st.session_state.keys()):
        if isinstance(k, str) and k.startswith(
            ("dlg_ed_", "dlg_nc_", "dlg_ec_", "dlg_eb_", "dlg_pb_")
        ):
            st.session_state.pop(k, None)


def get_active_dialog() -> Optional[dict[str, Any]]:
    """Return active dialog descriptor if open, else None."""
    val = st.session_state.get("ptos_active_dialog")
    if isinstance(val, dict) and isinstance(val.get("name"), str):
        return val
    return None


def navigate_to_tab(
    tab_name: str,
    *,
    needs_review_only: Optional[bool] = None,
    all_months: Optional[bool] = None,
    quick_filter: Optional[str] = None,
) -> None:
    """Queue a clean cross-tab navigation for the next rerun without mutating live widget keys."""
    if tab_name in ALL_TABS:
        st.session_state["ptos_pending_tab"] = tab_name
    if needs_review_only is not None:
        st.session_state["ptos_pending_review_only"] = bool(needs_review_only)
    if all_months is not None:
        st.session_state["ptos_pending_all_months"] = bool(all_months)
    if quick_filter is not None:
        st.session_state["ptos_pending_quick_filter"] = quick_filter
    st.rerun()


def pull_background_cloud_status(db_path: str) -> None:
    """Sync completed background cloud push results into Streamlit session state safely."""
    key = os.path.abspath(str(db_path))
    with _CLOUD_PUSH_LOCK:
        entry = _CLOUD_PUSH_STATE.get(key)
        if not entry:
            return
        if "last_result" in entry:
            st.session_state["ptos_last_cloud_sync"] = entry["last_result"]
        if "synced_changes" in entry:
            st.session_state["ptos_synced_changes"] = int(entry["synced_changes"])


def maybe_schedule_cloud_push(db: DatabaseManager) -> None:
    """Push dirty DB snapshots to encrypted Cloud Vault without blocking interactive UI clicks."""
    key = os.path.abspath(str(db.db_path))
    pull_background_cloud_status(key)

    auto_push = get_secret("TREASURY_CLOUD_AUTO_PUSH", "1").lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    current_changes = getattr(db, "change_counter", 0)
    if (
        not auto_push
        or getattr(db, "cloud_boot_restore_failed", False)
        or not get_cloud_sync_status().get("configured")
    ):
        st.session_state["ptos_synced_changes"] = current_changes
        return

    synced_changes = int(st.session_state.get("ptos_synced_changes", current_changes))
    if current_changes <= synced_changes:
        return

    if not db.has_any_transactions():
        st.session_state["ptos_synced_changes"] = current_changes
        return

    # Deterministic synchronous push inside pytest runs so assertions stay synchronous
    if "PYTEST_CURRENT_TEST" in os.environ:
        sync_ok, sync_msg = db.sync_to_cloud_vault()
        final_chg = getattr(db, "change_counter", current_changes)
        st.session_state["ptos_synced_changes"] = final_chg
        st.session_state["ptos_last_cloud_sync"] = (sync_ok, sync_msg)
        return

    with _CLOUD_PUSH_LOCK:
        entry = _CLOUD_PUSH_STATE.setdefault(key, {"running": False, "pending": False})
        if entry["running"]:
            entry["pending"] = True
            return
        entry["running"] = True
        entry["pending"] = False

    def _worker() -> None:
        while True:
            target_chg = getattr(db, "change_counter", current_changes)
            try:
                ok_push, msg_push = db.sync_to_cloud_vault()
            except Exception as exc:
                logger.warning("Background Cloud Vault push failed: %s", exc)
                ok_push, msg_push = False, f"Cloud push failed: {exc}"
            with _CLOUD_PUSH_LOCK:
                entry = _CLOUD_PUSH_STATE[key]
                entry["last_result"] = (ok_push, msg_push)
                entry["synced_changes"] = target_chg
                if entry.get("pending"):
                    entry["pending"] = False
                    continue
                entry["running"] = False
                break

    thread = threading.Thread(
        target=_worker,
        name="ptos-cloud-vault-push",
        daemon=True,
    )
    thread.start()

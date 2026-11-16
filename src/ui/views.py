"""Five-tab Batwa-parity views for Personal Treasury OS."""

from __future__ import annotations

import calendar
import html
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from bs4 import BeautifulSoup

from src.database.db_manager import DatabaseManager, _learned_keyword_matches
from src.engine.categorizer import (
    apply_rules_batch,
    extract_canonical_payee,
    learn_new_rule,
)
from src.ingestion.gmail_service import (
    fetch_emails_with_report,
    get_gmail_auth_status,
    parse_email_payload,
)
from src.ingestion.pdf_importer import import_statement_file
from src.models import (
    WALLET_CASH,
    WALLET_MMF,
    WALLET_NAYAPAY,
    CategoryBudget,
    CostBucket,
    DashboardMetrics,
    ParsedTransaction,
    RecurringCommitment,
    TransactionRecord,
    TxType,
)
from src.security import (
    get_backup_passphrase,
    get_cloud_sync_status,
    get_session_timeout_seconds,
    is_auth_enabled,
    redact_sensitive_text,
)
from src.ui.components import (
    format_pkr,
    render_burn_hero,
    render_category_budget_bars,
    render_mobile_transaction_cards,
    render_needs_review_banner,
    render_recent_transactions_strip,
    render_section_header,
    render_upcoming_bills_widget,
    render_wallet_carousel,
)

TAB_HOME = "🏠 Home & Wallets"
TAB_TRANSACTIONS = "📋 Transactions & Review"
TAB_BUDGETS = "🎯 Budgets & Bills"
TAB_INSIGHTS = "📊 Insights & Leakage"
TAB_SETUP = "⚙️ Sync & Setup"
ALL_TABS: tuple[str, ...] = (
    TAB_HOME,
    TAB_TRANSACTIONS,
    TAB_BUDGETS,
    TAB_INSIGHTS,
    TAB_SETUP,
)

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
_FIXTURES_DIR = Path("tests/fixtures")
_BACKUP_DIR = Path("data/backups")
_TX_EDITOR_PREFIXES = (
    "t2_edit_",
    "t1_qt_alias_",
    "t1_qt_kw_",
    "t1_qt_cat_",
    "t1_qt_bkt_",
    "t1_qt_mem_",
    "t1_qt_type_",
    "t1_qt_target_",
)
_BALANCE_INPUT_PREFIXES = (
    "t5_cur_bal_",
    "t5_open_bal_",
)


def _format_bucket(bucket: str) -> str:
    return _BUCKET_LABELS.get(bucket, bucket)


def _format_tx_type(tx_type: str) -> str:
    return _TX_TYPE_LABELS.get(tx_type, tx_type)


def _navigate_to_tab(
    tab_name: str,
    *,
    needs_review_only: Optional[bool] = None,
) -> None:
    """Queue a safe cross-tab navigation for the next rerun without mutating active widget keys."""
    if tab_name in ALL_TABS:
        st.session_state["ptos_pending_tab"] = tab_name
    if needs_review_only is not None:
        st.session_state["ptos_pending_review_only"] = bool(needs_review_only)
    st.rerun()


def _add_flash(level: str, message: str) -> None:
    """Queue a post-rerun flash message without clobbering earlier messages."""
    flashes = list(st.session_state.get("ptos_flashes") or [])
    flashes.append((level, message))
    st.session_state["ptos_flashes"] = flashes


def _set_flash(level: str, message: str) -> None:
    """Queue a post-rerun flash message."""
    _add_flash(level, message)


def render_flash_banner() -> None:
    """Display and clear all pending post-action flash messages."""
    flashes: list[tuple[str, str]] = []
    legacy_flash = st.session_state.pop("ptos_flash", None)
    if legacy_flash and isinstance(legacy_flash, (tuple, list)) and len(legacy_flash) == 2:
        flashes.append((str(legacy_flash[0]), str(legacy_flash[1])))
    queued = st.session_state.pop("ptos_flashes", None)
    if isinstance(queued, list):
        for item in queued:
            if isinstance(item, (tuple, list)) and len(item) == 2:
                flashes.append((str(item[0]), str(item[1])))

    toast_icons = {
        "success": "✅",
        "warning": "⚠️",
        "error": "❌",
        "info": "ℹ️",
    }
    for level, message in flashes:
        if level == "success":
            st.success(message)
        elif level == "warning":
            st.warning(message)
        elif level == "error":
            st.error(message)
        else:
            st.info(message)
        try:
            st.toast(message, icon=toast_icons.get(level, "ℹ️"))
        except Exception:
            pass


def _invalidate_balance_inputs() -> None:
    """Clear cached Tab 5 balance input keys so live SQLite balances reload cleanly."""
    for key in list(st.session_state.keys()):
        if isinstance(key, str) and key.startswith(_BALANCE_INPUT_PREFIXES):
            st.session_state.pop(key, None)


def _clear_tx_editor_state(
    tx_id: Optional[int] = None,
    *,
    clear_all: bool = False,
    invalidate_balances: bool = False,
) -> None:
    """Clear cached Tab 1 Quick-Tag and Tab 2 Editor widget state so fresh SQLite data loads."""
    if clear_all or tx_id is None:
        stale_keys = [
            k
            for k in list(st.session_state.keys())
            if isinstance(k, str) and k.startswith(_TX_EDITOR_PREFIXES)
        ]
    else:
        suffix = f"_{tx_id}"
        stale_keys = [
            k
            for k in list(st.session_state.keys())
            if isinstance(k, str)
            and k.startswith(_TX_EDITOR_PREFIXES)
            and k.endswith(suffix)
        ]
    for key in stale_keys:
        st.session_state.pop(key, None)
    if invalidate_balances:
        _invalidate_balance_inputs()


def _get_category_catalog(
    db: DatabaseManager,
    *,
    include_archived: bool = False,
) -> tuple[list[str], dict[str, CostBucket], dict[str, str]]:
    """Return ordered category names, default bucket mapping, and emoji mapping."""
    categories: list[str] = []
    bucket_map: dict[str, CostBucket] = {}
    emoji_map: dict[str, str] = {}
    all_budgets = db.get_category_budgets(active_only=False)
    inactive_cats = {cb.category for cb in all_budgets if not cb.is_active}

    for cb in all_budgets:
        if cb.category not in bucket_map and (include_archived or cb.is_active):
            categories.append(cb.category)
        bucket_map[cb.category] = cb.cost_bucket
        emoji_map[cb.category] = cb.emoji

    for cat, (bucket, emoji) in _SYSTEM_CATEGORY_BUCKETS.items():
        if cat not in bucket_map:
            if include_archived or cat not in inactive_cats:
                categories.append(cat)
            bucket_map[cat] = bucket
            emoji_map[cat] = emoji

    for tx in db.get_transactions():
        if tx.category and tx.category not in bucket_map:
            if include_archived or tx.category not in inactive_cats:
                categories.append(tx.category)
            bucket_map[tx.category] = tx.cost_bucket
            emoji_map[tx.category] = "📌"

    return categories, bucket_map, emoji_map


def _categories_for_tx_type(
    categories: list[str],
    bucket_map: dict[str, CostBucket],
    tx_type: str,
    *,
    current_category: Optional[str] = None,
) -> list[str]:
    """Strictly separate Inflow, Outflow, Transfer, and MMF categories by tx_type."""
    if tx_type == "DEBIT":
        opts = [
            c
            for c in categories
            if c not in _NON_DEBIT_SYSTEM_CATEGORIES
            and bucket_map.get(c, "VARIABLE") in _DEBIT_BUCKETS
        ]
        if (
            current_category
            and current_category not in opts
            and current_category not in _NON_DEBIT_SYSTEM_CATEGORIES
            and bucket_map.get(current_category, "VARIABLE") in _DEBIT_BUCKETS
        ):
            opts.append(current_category)
        return opts or ["Uncategorized"]
    if tx_type == "CREDIT":
        opts = [c for c in categories if bucket_map.get(c) == "INFLOW"]
        if (
            current_category
            and current_category not in opts
            and bucket_map.get(current_category) == "INFLOW"
        ):
            opts.append(current_category)
        return opts or ["Salary & Income", "Other Inflow"]
    if tx_type == "TRANSFER_INTERNAL":
        opts = [c for c in categories if bucket_map.get(c) == "IGNORE"]
        if (
            current_category
            and current_category not in opts
            and bucket_map.get(current_category) == "IGNORE"
        ):
            opts.append(current_category)
        return opts or ["Internal Transfer", "Wallet Top-Up", "Cash Withdrawal", "MMF Redemption"]
    if tx_type == "TRANSFER_MMF":
        opts = [c for c in categories if bucket_map.get(c) == "INVESTMENT"]
        if (
            current_category
            and current_category not in opts
            and bucket_map.get(current_category) == "INVESTMENT"
        ):
            opts.append(current_category)
        return opts or ["Money Market Fund"]
    return list(categories)


def _buckets_for_tx_type(tx_type: str) -> list[CostBucket]:
    """Return valid CostBucket options for a transaction type."""
    if tx_type == "CREDIT":
        return ["INFLOW"]
    if tx_type == "TRANSFER_INTERNAL":
        return ["IGNORE"]
    if tx_type == "TRANSFER_MMF":
        return ["INVESTMENT"]
    return list(_DEBIT_BUCKETS)


def _internal_transfer_targets(
    wallet_names: list[str],
    source_wallet: str,
) -> list[str]:
    """Return valid own-account destinations (bidirectional including MMF redemption)."""
    return [
        w
        for w in wallet_names
        if w != source_wallet and (source_wallet == WALLET_MMF or w != WALLET_MMF)
    ]


def _default_transfer_category(
    source_wallet: str,
    target_wallet: Optional[str],
    allowed_cats: list[str],
) -> str:
    """Pick the most appropriate default IGNORE category for an internal wallet transfer."""
    if source_wallet == WALLET_MMF and "MMF Redemption" in allowed_cats:
        return "MMF Redemption"
    if target_wallet == WALLET_CASH and "Cash Withdrawal" in allowed_cats:
        return "Cash Withdrawal"
    if target_wallet == WALLET_NAYAPAY and "Wallet Top-Up" in allowed_cats:
        return "Wallet Top-Up"
    if "Internal Transfer" in allowed_cats:
        return "Internal Transfer"
    return allowed_cats[0] if allowed_cats else "Internal Transfer"


def _render_inline_category_creator(
    db: DatabaseManager,
    *,
    key_prefix: str,
    tx_type: str,
    target_cat_key: str,
    target_bkt_key: Optional[str] = None,
    label: str = "➕ Create New Category",
) -> None:
    """Render an inline category creator scoped to the active transaction type."""
    reset_flag = f"{key_prefix}_reset"
    name_key = f"{key_prefix}_name"
    emoji_key = f"{key_prefix}_emoji"
    bkt_key = f"{key_prefix}_bkt"
    lim_key = f"{key_prefix}_limit"

    allowed_bkts = _buckets_for_tx_type(tx_type)
    default_emoji = (
        "💰"
        if tx_type == "CREDIT"
        else ("🔄" if tx_type == "TRANSFER_INTERNAL" else "🏷️")
    )
    if st.session_state.pop(reset_flag, False):
        st.session_state[name_key] = ""
        st.session_state[emoji_key] = default_emoji
        st.session_state[bkt_key] = allowed_bkts[0]
        st.session_state[lim_key] = 0.0
    else:
        st.session_state.setdefault(name_key, "")
        st.session_state.setdefault(emoji_key, default_emoji)
        if st.session_state.get(bkt_key) not in allowed_bkts:
            st.session_state[bkt_key] = allowed_bkts[0]
        st.session_state.setdefault(lim_key, 0.0)

    with st.expander(label, expanded=False):
        nc_name = st.text_input(
            "New Category Name",
            placeholder=(
                "e.g., Bonus / Rental Income"
                if tx_type == "CREDIT"
                else (
                    "e.g., Savings Vault Move"
                    if tx_type == "TRANSFER_INTERNAL"
                    else "e.g., Fitness & Gym"
                )
            ),
            key=name_key,
        )
        with st.container(key=f"ptos_compact_{key_prefix}_row"):
            c1, c2 = st.columns([0.65, 1.35])
            with c1:
                nc_emoji = st.text_input("Emoji", key=emoji_key)
            with c2:
                nc_bkt = st.selectbox(
                    "Category Group",
                    options=allowed_bkts,
                    format_func=_format_bucket,
                    disabled=(len(allowed_bkts) == 1),
                    key=bkt_key,
                )
        nc_limit = 0.0
        if tx_type == "DEBIT":
            nc_limit = st.number_input(
                "Optional Monthly Cap (PKR, 0 = untracked)",
                min_value=0.0,
                step=500.0,
                key=lim_key,
            )
        if st.button("➕ Save & Select Category", key=f"{key_prefix}_save", width="stretch"):
            clean_name = " ".join(nc_name.split()).strip()
            if not clean_name:
                st.error("Please enter a category name.")
            else:
                existing = {
                    cb.category.strip().lower(): cb
                    for cb in db.get_category_budgets(active_only=False)
                }
                matched = existing.get(clean_name.lower())
                if matched and matched.cost_bucket not in allowed_bkts:
                    st.error(
                        f"'{matched.category}' already exists as a {matched.cost_bucket} category. "
                        "Use a distinct name for this group."
                    )
                else:
                    saved = db.upsert_category_budget(
                        category=matched.category if matched else clean_name,
                        monthly_limit=float(nc_limit) if nc_bkt in _DEBIT_BUCKETS else 0.0,
                        emoji=nc_emoji.strip() or (matched.emoji if matched else default_emoji),
                        cost_bucket=nc_bkt,  # type: ignore[arg-type]
                        is_active=True,
                    )
                    if saved.cost_bucket in _DEBIT_BUCKETS:
                        _queue_cat_limit_sync(saved)
                    pending = st.session_state.setdefault("ptos_pending_widget_sync", {})
                    if isinstance(pending, dict):
                        pending[target_cat_key] = saved.category
                        if target_bkt_key:
                            pending[target_bkt_key] = saved.cost_bucket
                        if target_cat_key == "t1_qa_category" and tx_type in ("DEBIT", "CREDIT"):
                            pending[f"t1_qa_last_cat_{tx_type}"] = saved.category
                    st.session_state[reset_flag] = True
                    _set_flash(
                        "success",
                        f"Created {saved.cost_bucket} category '{saved.category}'.",
                    )
                    st.rerun()


def _sync_bucket_on_cat_change(
    cat_key: str,
    bkt_key: str,
    bucket_map: dict[str, CostBucket],
    tx_type: str = "DEBIT",
) -> None:
    if tx_type == "CREDIT":
        st.session_state[bkt_key] = "INFLOW"
        return
    if tx_type == "TRANSFER_INTERNAL":
        st.session_state[bkt_key] = "IGNORE"
        return
    if tx_type == "TRANSFER_MMF":
        st.session_state[bkt_key] = "INVESTMENT"
        return
    chosen = st.session_state.get(cat_key)
    mapped = bucket_map.get(chosen, "VARIABLE") if chosen else "VARIABLE"
    st.session_state[bkt_key] = mapped if mapped in _DEBIT_BUCKETS else "VARIABLE"


def _sync_qa_bucket(bucket_map: dict[str, CostBucket]) -> None:
    """Keep Tab 1 Quick-Add Cost Bucket synced whenever Type or Category changes."""
    tx_type = st.session_state.get("t1_qa_type", "DEBIT")
    if tx_type == "CREDIT":
        st.session_state["t1_qa_bucket"] = "INFLOW"
        return
    if tx_type == "TRANSFER_INTERNAL":
        st.session_state["t1_qa_bucket"] = "IGNORE"
        return
    if tx_type == "TRANSFER_MMF":
        st.session_state["t1_qa_bucket"] = "INVESTMENT"
        return
    cat_choice = st.session_state.get("t1_qa_category")
    if not cat_choice or cat_choice == "Auto-Categorize (Rules)":
        return
    mapped = bucket_map.get(cat_choice, "VARIABLE")
    st.session_state["t1_qa_bucket"] = mapped if mapped in _DEBIT_BUCKETS else "VARIABLE"


def _default_date_for_month(month_key: str) -> date:
    today = date.today()
    if today.strftime("%Y-%m") == month_key:
        return today
    year_s, mon_s = month_key.split("-")
    return date(int(year_s), int(mon_s), 1)


def _month_date_bounds(month_key: str) -> tuple[date, date]:
    year_s, mon_s = month_key.split("-")
    year, month = int(year_s), int(mon_s)
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def _bill_date_for_month(month_key: str, due_day: int) -> date:
    """Default bill payment date: today when in active current month, else due_day."""
    today = date.today()
    if today.strftime("%Y-%m") == month_key:
        return today
    year_s, mon_s = month_key.split("-")
    year, month = int(year_s), int(mon_s)
    last_day = calendar.monthrange(year, month)[1]
    safe_day = min(max(1, int(due_day)), last_day)
    return date(year, month, safe_day)


def _bill_date_str_for_month(month_key: str, due_day: int) -> str:
    return _bill_date_for_month(month_key, due_day).strftime("%Y-%m-%d")


def _default_rule_keyword_for_tx(
    db: DatabaseManager,
    tx: TransactionRecord,
) -> str:
    """Return existing trained user-rule keyword matching tx if any, else canonical counterparty."""
    for rule in db.get_category_rules():
        if rule.is_user_defined and _learned_keyword_matches(
            rule.keyword, tx.payee, tx.merchant_alias
        ):
            return rule.keyword
    canon = extract_canonical_payee(tx.payee)
    return canon if len(canon.strip()) >= 3 else tx.payee


def _save_tx_and_maybe_learn(
    db: DatabaseManager,
    tx_id: int,
    *,
    payee: str,
    merchant_alias: Optional[str],
    category: str,
    cost_bucket: CostBucket,
    tx_date: Optional[str] = None,
    account_name: Optional[str] = None,
    tx_type: Optional[TxType] = None,
    target_account: Optional[str] = None,
    include_target_account: bool = False,
    payment_channel: Optional[str] = None,
    amount: Optional[float] = None,
    tax_fee_amount: Optional[float] = None,
    notes: Optional[str] = None,
    learn_rule: bool = False,
    rule_keyword: Optional[str] = None,
    overwrite_verified: bool = True,
) -> tuple[Optional[TransactionRecord], int]:
    """Update a transaction as MANUALLY_VERIFIED and optionally train a retroactive rule."""
    clean_payee = payee.strip()
    if not clean_payee:
        raise ValueError("Payee cannot be blank.")
    kw = ""
    if learn_rule:
        kw = (rule_keyword if rule_keyword is not None else clean_payee).strip()
        if len(kw) < 3:
            raise ValueError(
                "Rule Keyword must be at least 3 characters before saving a matching rule."
            )

    clean_alias = merchant_alias.strip() if merchant_alias and merchant_alias.strip() else None
    update_fields: dict[str, Any] = {
        "payee": clean_payee,
        "merchant_alias": clean_alias,
        "category": category.strip() or "Uncategorized",
        "cost_bucket": cost_bucket,
        "review_status": "MANUALLY_VERIFIED",
    }
    if tx_date is not None:
        update_fields["tx_date"] = tx_date
    if account_name is not None:
        update_fields["account_name"] = account_name
    if tx_type is not None:
        update_fields["tx_type"] = tx_type
    if include_target_account:
        update_fields["target_account"] = target_account
    if payment_channel is not None:
        update_fields["payment_channel"] = payment_channel.strip() or None
    if amount is not None:
        update_fields["amount"] = float(amount)
    if tax_fee_amount is not None:
        update_fields["tax_fee_amount"] = float(tax_fee_amount)
    if notes is not None:
        update_fields["notes"] = notes.strip() or None

    updated_tx = db.update_transaction(tx_id, **update_fields)
    affected_count = 0
    if learn_rule:
        affected_count = learn_new_rule(
            db=db,
            payee=clean_payee,
            alias=clean_alias,
            category=updated_tx.category if updated_tx else update_fields["category"],
            cost_bucket=updated_tx.cost_bucket if updated_tx else cost_bucket,
            keyword=kw,
            overwrite_verified=overwrite_verified,
            target_account=updated_tx.target_account if updated_tx else target_account,
            source_tx_type=updated_tx.tx_type if updated_tx else tx_type,
        )
    _clear_tx_editor_state(tx_id, clear_all=learn_rule, invalidate_balances=True)
    return updated_tx, affected_count


def _ingest_parsed_batch(
    db: DatabaseManager,
    parsed: list[ParsedTransaction],
    label: str,
) -> tuple[int, int]:
    """Categorize parsed transactions and upsert idempotently into SQLite."""
    if not parsed:
        return 0, 0
    categorized = apply_rules_batch(parsed, db)
    inserted, skipped = db.upsert_many(categorized)
    if inserted > 0:
        _clear_tx_editor_state(clear_all=True, invalidate_balances=True)
    _set_flash(
        "success",
        f"{label}: parsed {len(parsed)} transaction(s) — inserted {inserted} new, skipped {skipped} duplicate(s).",
    )
    return inserted, skipped


def _extract_fixture_fragments(raw_html: str) -> list[tuple[str, str, str, str]]:
    """Split an HTML fixture file into (fragment_html, subject_hint, sender_hint, date_hint) tuples."""
    # 1. Explicit HTML document boundaries (multiple <!DOCTYPE or multiple <html blocks)
    if len(re.findall(r"(?i)<!DOCTYPE\b", raw_html)) > 1:
        doc_chunks = [
            chunk.strip()
            for chunk in re.split(r"(?i)(?=<!DOCTYPE\b)", raw_html)
            if chunk.strip() and "<" in chunk
        ]
        if len(doc_chunks) > 1:
            return [(chunk, "", "", "") for chunk in doc_chunks]
    elif len(re.findall(r"(?i)<html\b", raw_html)) > 1:
        html_chunks = [
            chunk.strip()
            for chunk in re.split(r"(?i)(?=<html\b)", raw_html)
            if "<html" in chunk.lower()
        ]
        if len(html_chunks) > 1:
            return [(chunk, "", "", "") for chunk in html_chunks]

    # 2. Container tags such as <article>, <section>, or div[data-email] / .email-*
    try:
        soup = BeautifulSoup(raw_html, "lxml")
    except Exception:
        soup = BeautifulSoup(raw_html, "html.parser")

    containers = soup.find_all(
        lambda tag: tag.name in ("article", "section", "div")
        and (
            tag.has_attr("data-email")
            or tag.has_attr("data-subject")
            or any(
                k in " ".join(tag.get("class") or []).lower()
                for k in ("email", "message", "fixture", "receipt", "alert")
            )
        )
    )
    # Keep only outermost matching containers so nested wrappers aren't duplicated
    outer_containers = [
        c
        for c in containers
        if not any(parent in containers for parent in c.parents)
    ]
    if len(outer_containers) > 1:
        results: list[tuple[str, str, str, str]] = []
        for node in outer_containers:
            subj = str(node.get("data-subject") or "").strip()
            sndr = str(node.get("data-sender") or "").strip()
            dt_h = str(node.get("data-date") or "").strip()
            results.append((str(node), subj, sndr, dt_h))
        return results

    # 3. <hr> separated sections inside body
    body_html = str(soup.body) if soup.body else raw_html
    hr_chunks = [
        chunk.strip()
        for chunk in re.split(r"(?i)<hr\b[^>]*/?>", body_html)
        if chunk.strip() and BeautifulSoup(chunk, "html.parser").get_text(strip=True)
    ]
    if len(hr_chunks) > 1:
        return [(chunk, "", "", "") for chunk in hr_chunks]

    title_text = soup.title.get_text(strip=True) if soup.title else ""
    return [(raw_html, title_text, "", "")]


def _load_html_fixtures(fixtures_dir: Path = _FIXTURES_DIR) -> tuple[list[ParsedTransaction], list[str]]:
    """Parse sample HTML email fixtures from tests/fixtures/."""
    if not fixtures_dir.exists():
        return [], []

    html_files = sorted(fixtures_dir.glob("*.html"))
    parsed_all: list[ParsedTransaction] = []
    loaded_names: list[str] = []

    for html_path in html_files:
        raw_html = html_path.read_text(encoding="utf-8", errors="replace").strip()
        if not raw_html:
            continue
        loaded_names.append(html_path.name)
        stem_low = html_path.stem.lower()
        default_sender = (
            "eservice@habibmetro.com"
            if "hmb" in stem_low or "habib" in stem_low
            else ("alerts@nayapay.com" if "nayapay" in stem_low else "")
        )
        default_subject = (
            "HabibMetro Transaction Alert"
            if "hmb" in stem_low or "habib" in stem_low
            else ("NayaPay Transaction Receipt" if "nayapay" in stem_low else "")
        )

        fragments = _extract_fixture_fragments(raw_html)
        file_parsed: list[ParsedTransaction] = []
        for idx, (frag_html, subj_hint, sndr_hint, date_hint) in enumerate(fragments):
            tx = parse_email_payload(
                message_id=f"FIXTURE_{html_path.stem}_{idx}",
                subject=subj_hint or default_subject,
                sender=sndr_hint or default_sender,
                body=frag_html,
                date_hdr=date_hint,
            )
            if tx is not None:
                file_parsed.append(tx)

        if not file_parsed:
            tx = parse_email_payload(
                message_id=f"FIXTURE_{html_path.stem}_0",
                subject=default_subject,
                sender=default_sender,
                body=raw_html,
            )
            if tx is not None:
                file_parsed.append(tx)

        parsed_all.extend(file_parsed)

    return parsed_all, loaded_names


def _plotly_base_layout(title: str = "", height: int = 340) -> dict[str, Any]:
    return {
        "title": {"text": title, "font": {"size": 15, "color": "#F8FAFC"}},
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor": "rgba(0,0,0,0)",
        "font": {"color": "#CBD5E1", "size": 12},
        "height": height,
        "margin": {"l": 18, "r": 18, "t": 44, "b": 28},
        "legend": {
            "orientation": "h",
            "yanchor": "bottom",
            "y": -0.22,
            "xanchor": "center",
            "x": 0.5,
        },
    }


# ----------------------------------------------------------------------
# Tab 1: Home & Wallets
# ----------------------------------------------------------------------


def _apply_pending_widget_sync() -> None:
    """Apply deferred selectbox updates before widgets instantiate on rerun."""
    pending = st.session_state.pop("ptos_pending_widget_sync", None)
    if isinstance(pending, dict):
        for w_key, w_val in pending.items():
            st.session_state[w_key] = w_val


def render_home_tab(
    db: DatabaseManager,
    metrics: DashboardMetrics,
    month_key: str,
) -> None:
    """Render Tab 1: Wallet cards, Hero Burn Gauge, Review banner, Bills & Quick-Add."""
    _apply_pending_widget_sync()
    render_wallet_carousel(
        wallets=metrics.wallet_balances,
        total_liquid=metrics.total_liquid_balance,
        total_net_worth=metrics.total_net_worth,
    )
    render_burn_hero(metrics)
    render_needs_review_banner(metrics.needs_review_count)
    if metrics.needs_review_count > 0:
        if st.button(
            f"📋 Review Pending ({metrics.needs_review_count}) →",
            key="t1_go_review",
            width="stretch",
        ):
            _navigate_to_tab(TAB_TRANSACTIONS, needs_review_only=True)

    # Inline quick-tagger for the top pending transaction when review items exist
    if metrics.needs_review_count > 0:
        pending_txs = db.get_transactions(review_status="NEEDS_REVIEW")
        if pending_txs:
            top_tx = pending_txs[0]
            categories, bucket_map, _ = _get_category_catalog(db)
            qt_wallet_names = [w.account_name for w in db.get_wallets()]
            short_qt_payee = (
                (top_tx.payee[:22] + "…") if len(top_tx.payee) > 24 else top_tx.payee
            )
            with st.expander(
                f"⚡ Quick-Tag: {short_qt_payee} ({format_pkr(top_tx.amount)})",
                expanded=False,
            ):
                st.caption(
                    f"Date: {top_tx.tx_date} • Wallet: {top_tx.account_name} • "
                    f"Channel: {top_tx.payment_channel or '—'}"
                )
                qt_type_opts = [
                    t
                    for t in _TX_TYPES
                    if not (top_tx.account_name == WALLET_MMF and t == "TRANSFER_MMF")
                ]
                qt_type_key = f"t1_qt_type_{top_tx.id}"
                if (
                    qt_type_key not in st.session_state
                    or st.session_state[qt_type_key] not in qt_type_opts
                ):
                    st.session_state[qt_type_key] = (
                        top_tx.tx_type
                        if top_tx.tx_type in qt_type_opts
                        else qt_type_opts[0]
                    )

                c1, c2, c3 = st.columns([1.2, 1.0, 0.9])
                with c1:
                    qt_type = st.selectbox(
                        "Transaction Type",
                        options=qt_type_opts,
                        format_func=_format_tx_type,
                        key=qt_type_key,
                    )
                    qt_target: Optional[str] = None
                    if qt_type == "TRANSFER_INTERNAL":
                        qt_tgt_opts = _internal_transfer_targets(
                            qt_wallet_names, top_tx.account_name
                        )
                        qt_tgt_key = f"t1_qt_target_{top_tx.id}"
                        if qt_tgt_opts:
                            if (
                                qt_tgt_key not in st.session_state
                                or st.session_state[qt_tgt_key] not in qt_tgt_opts
                            ):
                                st.session_state[qt_tgt_key] = (
                                    top_tx.target_account
                                    if top_tx.target_account in qt_tgt_opts
                                    else qt_tgt_opts[0]
                                )
                            qt_target = st.selectbox(
                                "Target Own Account",
                                options=qt_tgt_opts,
                                key=qt_tgt_key,
                            )
                        else:
                            st.warning("No eligible target wallet available.")
                    elif qt_type == "TRANSFER_MMF":
                        qt_target = WALLET_MMF
                        st.caption("Destination: **Money Market Fund** (INVESTMENT).")

                    qt_alias = st.text_input(
                        "Merchant Alias (Optional Friendly Name)",
                        value=top_tx.merchant_alias or "",
                        placeholder=top_tx.payee,
                        key=f"t1_qt_alias_{top_tx.id}",
                    )
                    qt_keyword = st.text_input(
                        "Rule Keyword (3+ chars if saving rule)",
                        value=_default_rule_keyword_for_tx(db, top_tx),
                        key=f"t1_qt_kw_{top_tx.id}",
                    )
                with c2:
                    qt_cats = _categories_for_tx_type(
                        categories,
                        bucket_map,
                        qt_type,
                        current_category=top_tx.category,
                    )
                    qt_buckets = _buckets_for_tx_type(qt_type)
                    if qt_type == "TRANSFER_INTERNAL":
                        default_cat = _default_transfer_category(
                            top_tx.account_name, qt_target, qt_cats
                        )
                    elif top_tx.category in qt_cats:
                        default_cat = top_tx.category
                    else:
                        default_cat = qt_cats[0]

                    qt_cat_key = f"t1_qt_cat_{top_tx.id}"
                    qt_bkt_key = f"t1_qt_bkt_{top_tx.id}"
                    if (
                        qt_cat_key not in st.session_state
                        or st.session_state[qt_cat_key] not in qt_cats
                    ):
                        st.session_state[qt_cat_key] = default_cat
                    mapped_default_bkt = bucket_map.get(default_cat, top_tx.cost_bucket)
                    default_bkt: CostBucket = (
                        qt_buckets[0]
                        if len(qt_buckets) == 1
                        else (
                            mapped_default_bkt
                            if mapped_default_bkt in qt_buckets
                            else (
                                top_tx.cost_bucket
                                if top_tx.cost_bucket in qt_buckets
                                else qt_buckets[0]
                            )
                        )
                    )
                    if (
                        qt_bkt_key not in st.session_state
                        or st.session_state[qt_bkt_key] not in qt_buckets
                    ):
                        st.session_state[qt_bkt_key] = default_bkt
                    qt_cat = st.selectbox(
                        "Assign Category",
                        options=qt_cats,
                        key=qt_cat_key,
                        on_change=_sync_bucket_on_cat_change,
                        args=(qt_cat_key, qt_bkt_key, bucket_map, qt_type),
                    )
                    qt_bucket = st.selectbox(
                        "Cost Bucket",
                        options=qt_buckets,
                        format_func=_format_bucket,
                        disabled=(len(qt_buckets) == 1),
                        key=qt_bkt_key,
                    )
                    _render_inline_category_creator(
                        db,
                        key_prefix=f"t1_qt_newcat_{top_tx.id}",
                        tx_type=qt_type,
                        target_cat_key=qt_cat_key,
                        target_bkt_key=qt_bkt_key,
                    )
                with c3:
                    qt_remember = st.checkbox(
                        "Also save rule for future/similar payees",
                        value=False,
                        key=f"t1_qt_mem_{top_tx.id}",
                        help="Leave unchecked to update ONLY this transaction.",
                    )
                    if st.button(
                        "✅ Approve & Tag",
                        key=f"t1_qt_save_{top_tx.id}",
                        type="primary",
                        width="stretch",
                    ):
                        if qt_type == "TRANSFER_INTERNAL" and (
                            not qt_target
                            or qt_target == top_tx.account_name
                            or (qt_target == WALLET_MMF and top_tx.account_name != WALLET_MMF)
                        ):
                            st.error("Select a valid target own account for transfer.")
                        elif qt_remember and len(qt_keyword.strip()) < 3:
                            st.error(
                                "Rule Keyword must be at least 3 characters (or uncheck 'Also save rule' to tag only this transaction)."
                            )
                        else:
                            try:
                                _, count = _save_tx_and_maybe_learn(
                                    db,
                                    top_tx.id or 0,
                                    payee=top_tx.payee,
                                    merchant_alias=qt_alias,
                                    category=qt_cat,
                                    cost_bucket=qt_bucket,
                                    tx_type=qt_type,
                                    target_account=qt_target,
                                    include_target_account=True,
                                    learn_rule=qt_remember,
                                    rule_keyword=qt_keyword,
                                )
                            except ValueError as exc:
                                st.error(str(exc))
                            else:
                                if qt_remember:
                                    extra = (
                                        f" Saved rule & updated {count} matching transaction(s)."
                                        if count > 0
                                        else " Saved rule for future imports."
                                    )
                                else:
                                    extra = ""
                                transfer_note = (
                                    f" ({top_tx.account_name} → {qt_target})"
                                    if qt_target
                                    else ""
                                )
                                _set_flash(
                                    "success",
                                    f"Tagged '{top_tx.payee}' as {qt_cat}{transfer_note}.{extra}",
                                )
                                st.rerun()

    left_col, right_col = st.columns([1.12, 1.0], gap="medium")

    with left_col:
        render_section_header(
            "📅 Upcoming Bills This Month",
            f"Recurring fixed & subscription tracker for {month_key}",
        )
        render_upcoming_bills_widget(
            metrics.upcoming_bills, metrics.unpaid_bills_total
        )

        render_section_header(
            "🎯 Top Category Sub-Budgets",
            "Highest utilization categories across your budget caps",
        )
        render_category_budget_bars(metrics.category_progress[:5])
        if st.button(
            "🎯 Manage / Edit Categories & Budgets →",
            key="t1_go_budgets",
            width="stretch",
        ):
            _navigate_to_tab(TAB_BUDGETS)

    with right_col:
        wallets = db.get_wallets()
        wallet_map = {w.account_name: w for w in wallets}
        wallet_names = [w.account_name for w in wallets]
        default_wallet_idx = (
            wallet_names.index(WALLET_CASH) if WALLET_CASH in wallet_names else 0
        )
        categories, bucket_map, _ = _get_category_catalog(db)

        if st.session_state.pop("t1_qa_reset", False):
            st.session_state["t1_qa_amount"] = 0.0
            st.session_state["t1_qa_payee"] = ""
            st.session_state["t1_qa_tax"] = 0.0
            st.session_state["t1_qa_notes"] = ""
            st.session_state["t1_qa_category"] = "Auto-Categorize (Rules)"
            st.session_state.pop("t1_qa_last_cat_DEBIT", None)
            st.session_state.pop("t1_qa_last_cat_CREDIT", None)
            st.session_state.pop("t1_qa_transfer_cat", None)
        else:
            st.session_state.setdefault("t1_qa_amount", 0.0)
            st.session_state.setdefault("t1_qa_payee", "")
            st.session_state.setdefault("t1_qa_tax", 0.0)
            st.session_state.setdefault("t1_qa_notes", "")

        with st.expander("➕ Quick-Add Manual / Cash Transaction", expanded=False):
            with st.container(key="ptos_compact_qa_1"):
                qa_row1_a, qa_row1_b = st.columns(2)
                with qa_row1_a:
                    qa_date = st.date_input(
                        "Transaction Date",
                        value=_default_date_for_month(month_key),
                        key=f"t1_qa_date_{month_key}",
                    )
                with qa_row1_b:
                    qa_account = st.selectbox(
                        "Source Wallet",
                        options=wallet_names,
                        index=default_wallet_idx,
                        key="t1_qa_account",
                    )

            tx_type_options = [
                t
                for t in _TX_TYPES
                if not (qa_account == WALLET_MMF and t == "TRANSFER_MMF")
            ]
            if (
                "t1_qa_type" in st.session_state
                and st.session_state["t1_qa_type"] not in tx_type_options
            ):
                st.session_state["t1_qa_type"] = tx_type_options[0]
                _sync_qa_bucket(bucket_map)

            with st.container(key="ptos_compact_qa_2"):
                qa_row2_a, qa_row2_b = st.columns(2)
                with qa_row2_a:
                    qa_tx_type = st.selectbox(
                        "Transaction Type",
                        options=tx_type_options,
                        format_func=_format_tx_type,
                        key="t1_qa_type",
                        on_change=_sync_qa_bucket,
                        args=(bucket_map,),
                    )
                with qa_row2_b:
                    qa_amount = st.number_input(
                        "Amount (PKR)",
                        min_value=0.0,
                        step=100.0,
                        key="t1_qa_amount",
                    )

            src_wallet = wallet_map.get(qa_account)
            if (
                src_wallet
                and src_wallet.balance_as_of
                and qa_date.strftime("%Y-%m-%d") < src_wallet.balance_as_of[:10]
            ):
                st.caption(
                    f"ℹ️ {qa_account} live balance is anchored as of {src_wallet.balance_as_of}. "
                    "Backdated entries appear in monthly spending charts without reducing today's balance."
                )

            qa_target: Optional[str] = None
            if qa_tx_type == "TRANSFER_INTERNAL":
                target_opts = _internal_transfer_targets(wallet_names, qa_account)
                if (
                    target_opts
                    and "t1_qa_target" in st.session_state
                    and st.session_state["t1_qa_target"] not in target_opts
                ):
                    st.session_state["t1_qa_target"] = target_opts[0]
                if not target_opts:
                    st.warning("No eligible target wallet available for internal transfer.")
                else:
                    qa_target = st.selectbox(
                        "Target Wallet",
                        options=target_opts,
                        key="t1_qa_target",
                    )
                    if qa_account == WALLET_MMF:
                        st.caption("Redeeming funds from **Money Market Fund** back into your selected wallet.")
                    else:
                        st.caption(
                            "Moving money between your own accounts (net worth stays neutral). Use **TRANSFER_MMF** above to invest into Money Market Fund."
                        )
            elif qa_tx_type == "TRANSFER_MMF":
                qa_target = WALLET_MMF
                st.caption("Destination locked to **Money Market Fund** (INVESTMENT).")

            qa_payee = st.text_input(
                "Payee / Description",
                placeholder="e.g., Chai & Paratha, ATM Withdrawal, Client Fee",
                key="t1_qa_payee",
            )

            qa_category: Optional[str] = None
            qa_bucket: Optional[CostBucket] = None
            prev_qa_type = st.session_state.get("t1_qa_prev_type", qa_tx_type)
            if qa_tx_type in ("DEBIT", "CREDIT"):
                allowed_cats = _categories_for_tx_type(categories, bucket_map, qa_tx_type)
                cat_options = ["Auto-Categorize (Rules)"] + allowed_cats
                if prev_qa_type != qa_tx_type:
                    if prev_qa_type in ("DEBIT", "CREDIT") and st.session_state.get("t1_qa_category"):
                        st.session_state[f"t1_qa_last_cat_{prev_qa_type}"] = st.session_state[
                            "t1_qa_category"
                        ]
                    restored_cat = st.session_state.get(f"t1_qa_last_cat_{qa_tx_type}")
                    if restored_cat and restored_cat in cat_options:
                        st.session_state["t1_qa_category"] = restored_cat
                    elif (
                        "t1_qa_category" in st.session_state
                        and st.session_state["t1_qa_category"] not in cat_options
                    ):
                        st.session_state["t1_qa_category"] = "Auto-Categorize (Rules)"
                    _sync_qa_bucket(bucket_map)
                elif (
                    "t1_qa_category" in st.session_state
                    and st.session_state["t1_qa_category"] not in cat_options
                ):
                    st.session_state["t1_qa_category"] = "Auto-Categorize (Rules)"
                    _sync_qa_bucket(bucket_map)
                qa_cat_choice = st.selectbox(
                    "Category",
                    options=cat_options,
                    key="t1_qa_category",
                    on_change=_sync_qa_bucket,
                    args=(bucket_map,),
                )
                st.session_state[f"t1_qa_last_cat_{qa_tx_type}"] = qa_cat_choice
                if qa_cat_choice != "Auto-Categorize (Rules)" or qa_tx_type == "CREDIT":
                    allowed_bkts = _buckets_for_tx_type(qa_tx_type)
                    mapped_bkt = bucket_map.get(qa_cat_choice, "VARIABLE")
                    default_bkt: CostBucket = (
                        "INFLOW"
                        if qa_tx_type == "CREDIT"
                        else (mapped_bkt if mapped_bkt in allowed_bkts else "VARIABLE")
                    )
                    if st.session_state.get("t1_qa_bucket") not in allowed_bkts:
                        st.session_state["t1_qa_bucket"] = default_bkt
                    chosen_bkt = st.selectbox(
                        "Cost Bucket",
                        options=allowed_bkts,
                        format_func=_format_bucket,
                        disabled=(len(allowed_bkts) == 1),
                        key="t1_qa_bucket",
                    )
                    if qa_cat_choice != "Auto-Categorize (Rules)":
                        qa_category = qa_cat_choice
                        qa_bucket = chosen_bkt
            st.session_state["t1_qa_prev_type"] = qa_tx_type
            if qa_tx_type in ("DEBIT", "CREDIT"):
                _render_inline_category_creator(
                    db,
                    key_prefix="t1_qa_newcat",
                    tx_type=qa_tx_type,
                    target_cat_key="t1_qa_category",
                    target_bkt_key="t1_qa_bucket",
                )
                if qa_tx_type == "DEBIT":
                    st.caption(
                        "Moving money between wallets or investing? Change **Transaction Type** above."
                    )
            else:
                if qa_tx_type == "TRANSFER_INTERNAL":
                    transfer_cats = _categories_for_tx_type(
                        categories, bucket_map, "TRANSFER_INTERNAL"
                    )
                    if (
                        "t1_qa_transfer_cat" not in st.session_state
                        or st.session_state["t1_qa_transfer_cat"] not in transfer_cats
                    ):
                        st.session_state["t1_qa_transfer_cat"] = _default_transfer_category(
                            qa_account, qa_target, transfer_cats
                        )
                    qa_category = st.selectbox(
                        "Transfer Category",
                        options=transfer_cats,
                        key="t1_qa_transfer_cat",
                    )
                    qa_bucket = "IGNORE"
                    _render_inline_category_creator(
                        db,
                        key_prefix="t1_qa_newcat_tr",
                        tx_type="TRANSFER_INTERNAL",
                        target_cat_key="t1_qa_transfer_cat",
                    )
                else:
                    qa_category = "Money Market Fund"
                    qa_bucket = "INVESTMENT"

            with st.container(key="ptos_compact_qa_3"):
                qa_row3_a, qa_row3_b = st.columns([0.8, 1.2])
                with qa_row3_a:
                    qa_tax = st.number_input(
                        "Tax / Fee (PKR)",
                        min_value=0.0,
                        step=10.0,
                        key="t1_qa_tax",
                    )
                with qa_row3_b:
                    qa_notes = st.text_input(
                        "Notes (Optional)",
                        placeholder="Cash receipt, reference, etc.",
                        key="t1_qa_notes",
                    )

            if st.button(
                "➕ Add Transaction",
                key="t1_qa_submit",
                type="primary",
                width="stretch",
            ):
                if qa_amount <= 0.0:
                    st.error("Please enter an Amount greater than Rs. 0.")
                elif not qa_payee.strip():
                    st.error("Please enter a Payee or description.")
                elif qa_tx_type == "TRANSFER_MMF" and qa_account == WALLET_MMF:
                    st.error("TRANSFER_MMF source wallet cannot be Money Market Fund.")
                elif qa_tx_type == "TRANSFER_INTERNAL" and (
                    not qa_target
                    or qa_target == qa_account
                    or (qa_target == WALLET_MMF and qa_account != WALLET_MMF)
                ):
                    st.error("Please select a valid target wallet (use TRANSFER_MMF to invest into Money Market Fund).")
                else:
                    try:
                        created = db.add_manual_transaction(
                            tx_date=qa_date.strftime("%Y-%m-%d"),
                            account_name=qa_account,
                            tx_type=qa_tx_type,
                            amount=float(qa_amount),
                            payee=qa_payee.strip(),
                            category=qa_category,
                            cost_bucket=qa_bucket,
                            target_account=qa_target,
                            tax_fee_amount=float(qa_tax),
                            payment_channel="Cash" if qa_account == WALLET_CASH else "Manual",
                            notes=qa_notes.strip() or None,
                        )
                    except (ValueError, RuntimeError) as exc:
                        st.error(f"Could not save transaction: {exc}")
                    else:
                        st.session_state["t1_qa_reset"] = True
                        _invalidate_balance_inputs()
                        notes_extra: list[str] = []
                        if created.tx_date[:7] != month_key:
                            notes_extra.append(
                                f"Logged under {created.tx_date[:7]} (switch Active Month to view in {created.tx_date[:7]} dashboard)."
                            )
                        elif created.review_status == "NEEDS_REVIEW":
                            notes_extra.append(
                                "Added to Needs Review (no existing rule matched)."
                            )
                        _set_flash(
                            "success",
                            f"Added {created.tx_type} of {format_pkr(created.amount)} on {created.tx_date} "
                            f"for '{created.payee}' ({created.category} / {created.cost_bucket})."
                            + (" " + " ".join(notes_extra) if notes_extra else ""),
                        )
                        st.rerun()

        render_section_header(
            "🕒 Recent 5 Transactions",
            f"Latest activity in {month_key} across all wallets",
        )
        recent_txs = db.get_transactions(month_key=month_key)[:5]
        render_recent_transactions_strip(recent_txs, month_key)
        if st.button(
            "📋 View Full Ledger & Filters →",
            key="t1_go_ledger",
            width="stretch",
        ):
            _navigate_to_tab(TAB_TRANSACTIONS)


# ----------------------------------------------------------------------
# Tab 2: Transactions & Review Queue
# ----------------------------------------------------------------------


def render_transactions_tab(db: DatabaseManager, month_key: str) -> None:
    """Render Tab 2: Searchable ledger, filters, inline editor, and one-click rule trainer."""
    _apply_pending_widget_sync()
    if "ptos_pending_review_only" in st.session_state:
        st.session_state["t2_needs_review_only"] = bool(
            st.session_state.pop("ptos_pending_review_only")
        )

    render_section_header(
        "📋 Transactions & Review Queue",
        "Filter across wallets/categories, edit any single transaction, or train automatic merchant rules",
    )

    wallets = [w.account_name for w in db.get_wallets()]
    categories, bucket_map, _ = _get_category_catalog(db)
    all_filter_cats, _, _ = _get_category_catalog(db, include_archived=True)

    with st.expander("🔍 Filter & Search Ledger", expanded=False):
        search_q = st.text_input(
            "Search Payee / Alias / Notes",
            placeholder="e.g., Foodpanda, Netflix, Raast, K-Electric...",
            key="t2_search",
        )
        with st.container(key="ptos_compact_t2_f1"):
            f2, f3 = st.columns(2)
            with f2:
                wallet_sel = st.selectbox(
                    "Wallet",
                    options=["All Wallets"] + wallets,
                    key="t2_wallet",
                )
            with f3:
                cat_sel = st.selectbox(
                    "Category",
                    options=["All Categories"] + all_filter_cats,
                    key="t2_category",
                )

        with st.container(key="ptos_compact_t2_f2"):
            f4, f5 = st.columns(2)
            with f4:
                bucket_sel = st.selectbox(
                    "Cost Bucket",
                    options=["All Buckets"] + list(_COST_BUCKETS),
                    format_func=lambda b: "All Buckets" if b == "All Buckets" else _format_bucket(b),
                    key="t2_bucket",
                )
            with f5:
                period_keys = ["selected_month", "all_months", "custom"]
                raw_pm = st.session_state.get("t2_period_mode")
                if isinstance(raw_pm, str) and raw_pm not in period_keys:
                    if raw_pm.startswith("Selected Month"):
                        st.session_state["t2_period_mode"] = "selected_month"
                    elif raw_pm == "All Months":
                        st.session_state["t2_period_mode"] = "all_months"
                    elif raw_pm == "Custom Date Range":
                        st.session_state["t2_period_mode"] = "custom"
                    else:
                        st.session_state["t2_period_mode"] = "selected_month"

                def _format_period_mode(mode_key: str) -> str:
                    return {
                        "selected_month": f"Selected Month ({month_key})",
                        "all_months": "All Months",
                        "custom": "Custom Date Range",
                    }.get(mode_key, mode_key)

                period_mode = st.selectbox(
                    "Date Scope",
                    options=period_keys,
                    format_func=_format_period_mode,
                    key="t2_period_mode",
                )

        needs_review_only = st.checkbox(
            "⚠️ Needs Review Only",
            value=False,
            key="t2_needs_review_only",
        )

        filter_month: Optional[str] = None
        start_str: Optional[str] = None
        end_str: Optional[str] = None
        if period_mode == "selected_month":
            filter_month = month_key
        elif period_mode == "custom":
            m_start, m_end = _month_date_bounds(month_key)
            with st.container(key="ptos_compact_t2_f3"):
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
            if d_start > d_end:
                st.warning("From Date is after To Date; swapping bounds for query.")
                d_start, d_end = d_end, d_start
            start_str = d_start.strftime("%Y-%m-%d")
            end_str = d_end.strftime("%Y-%m-%d")

    filtered_txs = db.get_transactions(
        month_key=filter_month,
        account_name=None if wallet_sel == "All Wallets" else wallet_sel,
        category=None if cat_sel == "All Categories" else cat_sel,
        cost_bucket=None if bucket_sel == "All Buckets" else bucket_sel,  # type: ignore[arg-type]
        review_status="NEEDS_REVIEW" if needs_review_only else None,
        search_query=search_q or None,
        start_date=start_str,
        end_date=end_str,
        include_incoming_transfers=(wallet_sel != "All Wallets"),
    )

    total_debits = sum(
        tx.amount + tx.tax_fee_amount for tx in filtered_txs if tx.tx_type == "DEBIT"
    )
    total_credits = sum(tx.amount for tx in filtered_txs if tx.tx_type == "CREDIT")
    pending_in_view = sum(
        1 for tx in filtered_txs if tx.review_status == "NEEDS_REVIEW"
    )

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Matching Rows", f"{len(filtered_txs)}")
    m2.metric("Filtered Debits", format_pkr(total_debits))
    m3.metric("Filtered Credits", format_pkr(total_credits))
    m4.metric("Needs Review", f"{pending_in_view}")

    transfer_pairs = db.find_internal_transfer_pairs(day_window=2)
    if transfer_pairs:
        with st.expander(
            f"🔁 Potential Own-Account Transfers Detected ({len(transfer_pairs)})",
            expanded=False,
        ):
            st.caption(
                "Detected matching outgoing and incoming entries across two of your own accounts within ±2 days. "
                "Link them as an Internal Transfer so neither side inflates Expense or Inflow totals."
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
                            _clear_tx_editor_state(clear_all=True, invalidate_balances=True)
                            _set_flash(
                                "success",
                                f"Linked #{out_tx.id} ({linked.account_name} → {linked.target_account}) "
                                f"for {format_pkr(linked.amount)} and removed mirror leg #{in_tx.id}.",
                            )
                            st.rerun()

    if not filtered_txs:
        if needs_review_only and filter_month is not None:
            all_pending = len(db.get_transactions(review_status="NEEDS_REVIEW"))
            if all_pending > 0:
                st.info(
                    f"No pending review items in {month_key}, but {all_pending} item(s) await review in other months. "
                    "Switch Date Scope to 'All Months' above to review them."
                )
                return
        st.info("No transactions match the current filters. Try widening the date scope or clearing search.")
        return

    # Prioritize NEEDS_REVIEW rows first while preserving descending date order
    review_first = [t for t in filtered_txs if t.review_status == "NEEDS_REVIEW"]
    verified_rest = [t for t in filtered_txs if t.review_status != "NEEDS_REVIEW"]
    selector_txs = review_first + verified_rest

    tx_map = {tx.id: tx for tx in selector_txs if tx.id is not None}
    tx_ids = list(tx_map.keys())

    def _format_tx_option(tid: int) -> str:
        t = tx_map[tid]
        tag = "⚠️" if t.review_status == "NEEDS_REVIEW" else "✅"
        label_payee = t.merchant_alias or t.payee
        short_date = t.tx_date[5:] if len(t.tx_date) >= 10 else t.tx_date
        return f"{tag} {format_pkr(t.amount)} · {label_payee} ({short_date}, {t.account_name}) #{tid}"

    pending_nav = st.session_state.pop("t2_pending_select_id", None)
    if pending_nav is not None and pending_nav in tx_ids:
        st.session_state["t2_selected_tx_id"] = pending_nav

    with st.expander(
        "🧠 Transaction Editor & Rule Trainer",
        expanded=(pending_in_view > 0),
    ):
        if (
            "t2_selected_tx_id" in st.session_state
            and st.session_state["t2_selected_tx_id"] not in tx_ids
        ):
            st.session_state["t2_selected_tx_id"] = tx_ids[0]
        selected_id = st.selectbox(
            "Select Transaction to Edit / Categorize",
            options=tx_ids,
            format_func=_format_tx_option,
            key="t2_selected_tx_id",
        )
        sel_tx = tx_map[selected_id]

        st.caption(
            f"ID #{sel_tx.id} • Status: **{sel_tx.review_status}** • Source: **{sel_tx.source}**"
        )
        if sel_tx.raw_subject:
            st.caption(f"Email Subject: `{sel_tx.raw_subject}`")

        with st.container(key="ptos_compact_t2_meta"):
            m_col1, m_col2, m_col3, m_col4 = st.columns([0.9, 1.0, 1.1, 0.9])
            with m_col1:
                try:
                    parsed_sel_date = datetime.strptime(sel_tx.tx_date, "%Y-%m-%d").date()
                except ValueError:
                    parsed_sel_date = date.today()
                ed_date = st.date_input(
                    "Date",
                    value=parsed_sel_date,
                    key=f"t2_edit_date_{selected_id}",
                )
            with m_col2:
                acct_opts = wallets if sel_tx.account_name in wallets else [sel_tx.account_name] + wallets
                ed_account = st.selectbox(
                    "Wallet",
                    options=acct_opts,
                    index=acct_opts.index(sel_tx.account_name),
                    key=f"t2_edit_account_{selected_id}",
                )
            with m_col3:
                type_opts = [t for t in _TX_TYPES if not (ed_account == WALLET_MMF and t == "TRANSFER_MMF")]
                ed_type_key = f"t2_edit_type_{selected_id}"
                if ed_type_key not in st.session_state or st.session_state[ed_type_key] not in type_opts:
                    st.session_state[ed_type_key] = (
                        sel_tx.tx_type if sel_tx.tx_type in type_opts else type_opts[0]
                    )
                ed_type = st.selectbox(
                    "Type",
                    options=type_opts,
                    format_func=_format_tx_type,
                    key=ed_type_key,
                )
            with m_col4:
                ed_channel = st.text_input(
                    "Channel",
                    value=sel_tx.payment_channel or "",
                    placeholder="Raast, POS, Cash...",
                    key=f"t2_edit_channel_{selected_id}",
                )

        ed_target: Optional[str] = None
        if ed_type == "TRANSFER_INTERNAL":
            tgt_opts = _internal_transfer_targets(wallets, ed_account)
            ed_tgt_key = f"t2_edit_target_{selected_id}"
            if tgt_opts:
                if ed_tgt_key not in st.session_state or st.session_state[ed_tgt_key] not in tgt_opts:
                    st.session_state[ed_tgt_key] = (
                        sel_tx.target_account if sel_tx.target_account in tgt_opts else tgt_opts[0]
                    )
                ed_target = st.selectbox(
                    "Target Wallet",
                    options=tgt_opts,
                    key=ed_tgt_key,
                )
            else:
                st.warning("No eligible target wallet available for internal transfer.")
        elif ed_type == "TRANSFER_MMF":
            ed_target = WALLET_MMF
            st.caption("Target Wallet locked to **Money Market Fund** (INVESTMENT).")

        with st.container(key="ptos_compact_t2_amts"):
            amt_c1, amt_c2 = st.columns(2)
            with amt_c1:
                ed_amount = st.number_input(
                    "Amount (PKR)",
                    min_value=0.01,
                    value=float(sel_tx.amount),
                    step=50.0,
                    key=f"t2_edit_amt_{selected_id}",
                )
            with amt_c2:
                ed_tax = st.number_input(
                    "Tax / Fee (PKR)",
                    min_value=0.0,
                    value=float(sel_tx.tax_fee_amount),
                    step=10.0,
                    key=f"t2_edit_tax_{selected_id}",
                )

        with st.container(key="ptos_compact_t2_names"):
            nm_c1, nm_c2 = st.columns(2)
            with nm_c1:
                ed_payee = st.text_input(
                    "Payee",
                    value=sel_tx.payee,
                    key=f"t2_edit_payee_{selected_id}",
                )
            with nm_c2:
                ed_alias = st.text_input(
                    "Merchant Alias (Friendly Display Name)",
                    value=sel_tx.merchant_alias or "",
                    placeholder="e.g., Foodpanda, Netflix, K-Electric",
                    key=f"t2_edit_alias_{selected_id}",
                )

        allowed_ed_cats = _categories_for_tx_type(
            categories,
            bucket_map,
            ed_type,
            current_category=sel_tx.category,
        )
        allowed_ed_bkts = _buckets_for_tx_type(ed_type)
        ed_cat_key = f"t2_edit_cat_{selected_id}"
        ed_bkt_key = f"t2_edit_bkt_{selected_id}"
        initial_cat = (
            sel_tx.category
            if sel_tx.category in allowed_ed_cats
            else allowed_ed_cats[0]
        )
        initial_bkt: CostBucket = (
            allowed_ed_bkts[0]
            if len(allowed_ed_bkts) == 1
            else (
                sel_tx.cost_bucket
                if sel_tx.cost_bucket in allowed_ed_bkts
                else (
                    bucket_map[initial_cat]
                    if bucket_map.get(initial_cat) in allowed_ed_bkts
                    else allowed_ed_bkts[0]
                )
            )
        )
        if ed_cat_key not in st.session_state or st.session_state[ed_cat_key] not in allowed_ed_cats:
            st.session_state[ed_cat_key] = initial_cat
        if ed_bkt_key not in st.session_state or st.session_state[ed_bkt_key] not in allowed_ed_bkts:
            st.session_state[ed_bkt_key] = initial_bkt

        with st.container(key="ptos_compact_t2_catbkt"):
            cb_c1, cb_c2 = st.columns(2)
            with cb_c1:
                ed_cat = st.selectbox(
                    "Category",
                    options=allowed_ed_cats,
                    key=ed_cat_key,
                    on_change=_sync_bucket_on_cat_change,
                    args=(ed_cat_key, ed_bkt_key, bucket_map, ed_type),
                )
            with cb_c2:
                ed_bucket = st.selectbox(
                    "Cost Bucket",
                    options=allowed_ed_bkts,
                    format_func=_format_bucket,
                    disabled=(len(allowed_ed_bkts) == 1),
                    key=ed_bkt_key,
                )

        with st.container(key="ptos_compact_t2_kwnotes"):
            kn_c1, kn_c2 = st.columns(2)
            with kn_c1:
                ed_kw = st.text_input(
                    "Rule Keyword (used only by 'Save & Apply Rule')",
                    value=_default_rule_keyword_for_tx(db, sel_tx),
                    key=f"t2_edit_kw_{selected_id}",
                )
            with kn_c2:
                ed_notes = st.text_input(
                    "Notes",
                    value=sel_tx.notes or "",
                    key=f"t2_edit_notes_{selected_id}",
                )

        _render_inline_category_creator(
            db,
            key_prefix=f"t2_newcat_{selected_id}",
            tx_type=ed_type,
            target_cat_key=ed_cat_key,
            target_bkt_key=ed_bkt_key,
        )
        if st.button(
            "🛠️ Manage Categories in Tab 3 →",
            key=f"t2_go_cats_{selected_id}",
            width="stretch",
        ):
            _navigate_to_tab(TAB_BUDGETS)

        st.caption(
            "Use **Save & Apply Rule** to auto-tag all matching payees, or **Save This Only** for one entry."
        )

        if st.button(
            "🧠 Save & Apply Rule to All Similar",
            key=f"t2_btn_learn_{selected_id}",
            type="primary",
            width="stretch",
        ):
            if ed_type == "TRANSFER_INTERNAL" and (
                not ed_target
                or ed_target == ed_account
                or (ed_target == WALLET_MMF and ed_account != WALLET_MMF)
            ):
                st.error("Select a valid Target Wallet for internal transfer.")
            else:
                try:
                    _, count = _save_tx_and_maybe_learn(
                        db,
                        selected_id,
                        payee=ed_payee,
                        merchant_alias=ed_alias,
                        category=ed_cat,
                        cost_bucket=ed_bucket,
                        tx_date=ed_date.strftime("%Y-%m-%d"),
                        account_name=ed_account,
                        tx_type=ed_type,
                        target_account=ed_target,
                        include_target_account=True,
                        payment_channel=ed_channel,
                        amount=ed_amount,
                        tax_fee_amount=ed_tax,
                        notes=ed_notes,
                        learn_rule=True,
                        rule_keyword=ed_kw,
                    )
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    if needs_review_only:
                        next_ids = [tid for tid in tx_ids if tid != selected_id]
                        if next_ids:
                            st.session_state["t2_pending_select_id"] = next_ids[0]
                    _set_flash(
                        "success",
                        f"Saved #{selected_id} and retroactively tagged {count} transaction(s) matching '{ed_kw.strip()}'!",
                    )
                    st.rerun()

        with st.container(key="ptos_compact_t2_subbtns"):
            btn_a, btn_c = st.columns([1.4, 0.8])
            with btn_a:
                if st.button(
                    "💾 Save This Only",
                    key=f"t2_btn_save_{selected_id}",
                    width="stretch",
                ):
                    if ed_type == "TRANSFER_INTERNAL" and (
                        not ed_target
                        or ed_target == ed_account
                        or (ed_target == WALLET_MMF and ed_account != WALLET_MMF)
                    ):
                        st.error("Select a valid Target Wallet for internal transfer.")
                    else:
                        try:
                            _save_tx_and_maybe_learn(
                                db,
                                selected_id,
                                payee=ed_payee,
                                merchant_alias=ed_alias,
                                category=ed_cat,
                                cost_bucket=ed_bucket,
                                tx_date=ed_date.strftime("%Y-%m-%d"),
                                account_name=ed_account,
                                tx_type=ed_type,
                                target_account=ed_target,
                                include_target_account=True,
                                payment_channel=ed_channel,
                                amount=ed_amount,
                                tax_fee_amount=ed_tax,
                                notes=ed_notes,
                                learn_rule=False,
                            )
                        except ValueError as exc:
                            st.error(str(exc))
                        else:
                            if needs_review_only:
                                next_ids = [tid for tid in tx_ids if tid != selected_id]
                                if next_ids:
                                    st.session_state["t2_pending_select_id"] = next_ids[0]
                            _set_flash(
                                "success",
                                f"Saved transaction #{selected_id} only ({ed_cat} / {ed_bucket}).",
                            )
                            st.rerun()

            with btn_c:
                with st.popover(
                    "🗑️ Delete",
                    key=f"t2_pop_del_{selected_id}",
                    width="stretch",
                ):
                    st.caption(
                        f"Permanently delete #{selected_id} "
                        f"({sel_tx.payee}, {format_pkr(sel_tx.amount)})?"
                    )
                    if st.button(
                        "Confirm Delete",
                        key=f"t2_btn_del_{selected_id}",
                        type="secondary",
                        width="stretch",
                    ):
                        deleted = db.delete_transaction(selected_id)
                        if deleted:
                            _clear_tx_editor_state(selected_id, invalidate_balances=True)
                            next_ids = [tid for tid in tx_ids if tid != selected_id]
                            if next_ids:
                                st.session_state["t2_pending_select_id"] = next_ids[0]
                            else:
                                st.session_state.pop("t2_selected_tx_id", None)
                            _set_flash(
                                "warning",
                                f"Deleted transaction #{selected_id} and recalculated wallet balances.",
                            )
                            st.rerun()
                        else:
                            st.error(f"Transaction #{selected_id} could not be deleted.")

    # Mobile-First Card Feed + Expandable Full Spreadsheet Table
    render_section_header(
        "💳 Ledger Feed",
        f"Showing top {min(30, len(filtered_txs))} matching transaction(s)",
    )
    render_mobile_transaction_cards(filtered_txs, max_items=30)

    rows = [
        {
            "tx_date": tx.tx_date,
            "account_name": tx.account_name,
            "target_account": tx.target_account or "—",
            "payee": tx.payee,
            "merchant_alias": tx.merchant_alias or "—",
            "category": tx.category,
            "cost_bucket": tx.cost_bucket,
            "tx_type": tx.tx_type,
            "amount": float(tx.amount),
            "tax_fee_amount": float(tx.tax_fee_amount),
            "review_status": tx.review_status,
            "source": tx.source,
        }
        for tx in filtered_txs
    ]
    df = pd.DataFrame(rows)
    with st.expander("📊 Full Spreadsheet Table View", expanded=False):
        st.dataframe(
            df,
            width="stretch",
            hide_index=True,
            column_config={
                "tx_date": st.column_config.TextColumn("Date", width="small"),
                "account_name": st.column_config.TextColumn("Wallet", width="small"),
                "target_account": st.column_config.TextColumn("Target", width="small"),
                "payee": st.column_config.TextColumn("Payee", width="medium"),
                "merchant_alias": st.column_config.TextColumn("Alias", width="small"),
                "category": st.column_config.TextColumn("Category", width="medium"),
                "cost_bucket": st.column_config.TextColumn("Bucket", width="small"),
                "tx_type": st.column_config.TextColumn("Type", width="small"),
                "amount": st.column_config.NumberColumn("Amount (PKR)", format="accounting"),
                "tax_fee_amount": st.column_config.NumberColumn("Tax/Fee (PKR)", format="accounting"),
                "review_status": st.column_config.TextColumn("Status", width="small"),
                "source": st.column_config.TextColumn("Source", width="small"),
            },
        )


# ----------------------------------------------------------------------
# Tab 3: Budgets & Commitments
# ----------------------------------------------------------------------


def _queue_cat_limit_sync(cb: CategoryBudget, *, remove: bool = False) -> None:
    """Defer updating t3_cat_limit_* until before the next render_budgets_tab widget pass."""
    pending = dict(st.session_state.get("t3_pending_cat_limits") or {})
    key = f"t3_cat_limit_{cb.id}_{cb.category}"
    pending[key] = None if remove else float(cb.monthly_limit)
    st.session_state["t3_pending_cat_limits"] = pending


def render_budgets_tab(
    db: DatabaseManager,
    metrics: DashboardMetrics,
    month_key: str,
) -> None:
    """Render Tab 3: Per-category monthly PKR limits and Recurring Bills Radar."""
    _apply_pending_widget_sync()
    pending_cat_sync = st.session_state.pop("t3_pending_cat_limits", {})
    if isinstance(pending_cat_sync, dict):
        for sync_key, sync_val in pending_cat_sync.items():
            if sync_val is None:
                st.session_state.pop(sync_key, None)
            else:
                st.session_state[sync_key] = float(sync_val)

    render_section_header(
        "🎯 Per-Category Monthly Sub-Budgets & Category Studio",
        f"Edit categories (Variable ↔ Fixed), delete unused ones, and pace budgets for {month_key}",
    )

    cfg = db.get_budget_config()
    if cfg.envelope_mode:
        st.info(
            "✉️ **NayaPay Envelope Mode is ON:** Hero Burn tracks `HabibMetro → NayaPay` "
            "top-ups as variable spend, while the category budget bars below track your "
            "granular NayaPay purchases."
        )

    all_budgets = db.get_category_budgets(active_only=False)
    budgets = [
        cb
        for cb in all_budgets
        if cb.is_active and cb.cost_bucket in _DEBIT_BUCKETS
    ]

    left_col, right_col = st.columns([1.15, 1.0], gap="medium")

    with left_col:
        render_category_budget_bars(metrics.category_progress)

        st.markdown("#### 🛠️ Category Studio (Edit Fixed↔Variable, Add & Delete)")
        with st.expander(
            "✏️ Edit / Switch Variable ↔ Fixed / Delete Category",
            expanded=False,
        ):
            editable_budgets = [cb for cb in all_budgets if cb.id is not None]
            if not editable_budgets:
                st.info("No categories available to edit yet.")
            else:
                cb_by_id = {int(cb.id): cb for cb in editable_budgets if cb.id is not None}
                edit_ids = list(cb_by_id.keys())
                if (
                    "t3_edit_cat_id" not in st.session_state
                    or st.session_state["t3_edit_cat_id"] not in cb_by_id
                ):
                    st.session_state["t3_edit_cat_id"] = edit_ids[0]
                edit_cat_id = int(
                    st.selectbox(
                        "Select Category to Edit or Delete",
                        options=edit_ids,
                        format_func=lambda cid: (
                            f"{cb_by_id[cid].emoji} {cb_by_id[cid].category} ({cb_by_id[cid].cost_bucket})"
                        ),
                        key="t3_edit_cat_id",
                    )
                )
                target_cb = cb_by_id[edit_cat_id]
                force_reload = st.session_state.pop("t3_ec_force_reload", False)
                if (
                    force_reload
                    or st.session_state.get("t3_ec_loaded_id") != edit_cat_id
                ):
                    st.session_state["t3_ec_loaded_id"] = edit_cat_id
                    st.session_state["t3_ec_name"] = target_cb.category
                    st.session_state["t3_ec_emoji"] = target_cb.emoji
                    st.session_state["t3_ec_bucket"] = target_cb.cost_bucket
                    st.session_state["t3_ec_limit"] = float(target_cb.monthly_limit)

                is_system_uncat = target_cb.category == "Uncategorized"
                if is_system_uncat:
                    st.caption("System fallback `'Uncategorized'` cannot be renamed or deleted.")
                ec_name = st.text_input(
                    "Category Name",
                    disabled=is_system_uncat,
                    key="t3_ec_name",
                )
                with st.container(key="ptos_compact_t3_ec"):
                    ec1, ec2 = st.columns([0.55, 1.45])
                    with ec1:
                        ec_emoji = st.text_input("Emoji", key="t3_ec_emoji")
                    with ec2:
                        ec_bucket_opts: list[CostBucket] = [
                            "FIXED",
                            "VARIABLE",
                            "LEAKAGE",
                            "INFLOW",
                            "IGNORE",
                        ]
                        if target_cb.cost_bucket == "INVESTMENT":
                            ec_bucket_opts.append("INVESTMENT")
                        if st.session_state.get("t3_ec_bucket") not in ec_bucket_opts:
                            st.session_state["t3_ec_bucket"] = target_cb.cost_bucket
                        ec_bucket = st.selectbox(
                            "Category Bucket",
                            options=ec_bucket_opts,
                            format_func=_format_bucket,
                            disabled=is_system_uncat,
                            key="t3_ec_bucket",
                        )
                ec_is_outflow = ec_bucket in _DEBIT_BUCKETS
                ec_limit = st.number_input(
                    "Monthly Limit (PKR)",
                    min_value=0.0,
                    step=500.0,
                    disabled=not ec_is_outflow,
                    key="t3_ec_limit",
                )
                with st.container(key="ptos_compact_t3_eb"):
                    eb1, eb2 = st.columns([1.3, 1.0])
                    with eb1:
                        if st.button(
                            "💾 Save Changes",
                            key="t3_ec_save_btn",
                            type="primary",
                            width="stretch",
                        ):
                            try:
                                updated_cb = db.update_category_budget_by_id(
                                    edit_cat_id,
                                    category=target_cb.category if is_system_uncat else ec_name,
                                    emoji=ec_emoji.strip() or target_cb.emoji,
                                    cost_bucket=ec_bucket,  # type: ignore[arg-type]
                                    monthly_limit=float(ec_limit) if ec_is_outflow else 0.0,
                                    is_active=target_cb.is_active,
                                )
                            except ValueError as exc:
                                st.error(str(exc))
                            else:
                                if target_cb.category != updated_cb.category:
                                    _queue_cat_limit_sync(target_cb, remove=True)
                                _queue_cat_limit_sync(
                                    updated_cb,
                                    remove=(
                                        updated_cb.cost_bucket not in _DEBIT_BUCKETS
                                        or not updated_cb.is_active
                                    ),
                                )
                                _clear_tx_editor_state(clear_all=True)
                                st.session_state["t3_ec_force_reload"] = True
                                _set_flash(
                                    "success",
                                    f"Updated category '{updated_cb.category}' ({updated_cb.cost_bucket}).",
                                )
                                st.rerun()
                    with eb2:
                        if not is_system_uncat:
                            with st.popover(
                                "🗑️ Delete",
                                key="t3_ec_pop_del",
                                width="stretch",
                            ):
                                st.caption(
                                    f"Permanently remove '{target_cb.category}'? Transactions move safely to a fallback category."
                                )
                                if st.button(
                                    "Confirm Delete",
                                    key="t3_ec_del_btn",
                                    type="secondary",
                                    width="stretch",
                                ):
                                    try:
                                        _, reassigned = db.delete_category_budget(edit_cat_id)
                                    except ValueError as exc:
                                        st.error(str(exc))
                                    else:
                                        _queue_cat_limit_sync(target_cb, remove=True)
                                        _clear_tx_editor_state(clear_all=True)
                                        st.session_state["t3_ec_force_reload"] = True
                                        _set_flash(
                                            "info",
                                            f"Deleted category '{target_cb.category}' ({reassigned} transaction(s) reassigned).",
                                        )
                                        st.rerun()

        if st.session_state.pop("t3_new_cat_reset", False):
            st.session_state["t3_nc_name"] = ""
            st.session_state["t3_nc_emoji"] = ""
            st.session_state["t3_nc_bucket"] = "VARIABLE"
            st.session_state["t3_nc_limit"] = 5000.0
        else:
            st.session_state.setdefault("t3_nc_name", "")
            st.session_state.setdefault("t3_nc_emoji", "")
            st.session_state.setdefault("t3_nc_bucket", "VARIABLE")
            st.session_state.setdefault("t3_nc_limit", 5000.0)

        with st.expander("➕ Add / Update Custom Category Budget", expanded=False):
            nc_name = st.text_input(
                "Category Name",
                placeholder="e.g., Fitness & Gym, Consulting Income, Savings Vault",
                key="t3_nc_name",
            )
            with st.container(key="ptos_compact_t3_nc"):
                nc1, nc2 = st.columns([0.6, 1.4])
                with nc1:
                    nc_emoji = st.text_input(
                        "Emoji",
                        placeholder="🏷️",
                        key="t3_nc_emoji",
                    )
                with nc2:
                    nc_bucket_opts: list[CostBucket] = [
                        "FIXED",
                        "VARIABLE",
                        "LEAKAGE",
                        "INFLOW",
                        "IGNORE",
                    ]
                    nc_bucket = st.selectbox(
                        "Category Group",
                        options=nc_bucket_opts,
                        format_func=_format_bucket,
                        key="t3_nc_bucket",
                    )
            nc_is_outflow = nc_bucket in _DEBIT_BUCKETS
            if not nc_is_outflow:
                st.caption(
                    "Inflow & Own-Account Transfer categories don't count against Monthly Spending Caps (limit stored as Rs. 0)."
                )
            nc_limit = st.number_input(
                "Monthly Limit (PKR)",
                min_value=0.0,
                step=500.0,
                disabled=not nc_is_outflow,
                key="t3_nc_limit",
            )
            if st.button(
                "Add / Save Category",
                key="t3_nc_submit",
                width="stretch",
            ):
                clean_name = " ".join(nc_name.split()).strip()
                if not clean_name:
                    st.error("Please enter a category name.")
                else:
                    existing_by_lower = {
                        cb.category.strip().lower(): cb for cb in all_budgets
                    }
                    matched_cb = existing_by_lower.get(clean_name.lower())
                    canonical_name = (
                        matched_cb.category if matched_cb else clean_name
                    )
                    default_nc_emoji = (
                        "💰"
                        if nc_bucket == "INFLOW"
                        else ("🔄" if nc_bucket == "IGNORE" else "🏷️")
                    )
                    resolved_emoji = (
                        nc_emoji.strip()
                        or (matched_cb.emoji if matched_cb else default_nc_emoji)
                    )
                    resolved_limit = float(nc_limit) if nc_is_outflow else 0.0
                    saved = db.upsert_category_budget(
                        category=canonical_name,
                        monthly_limit=resolved_limit,
                        emoji=resolved_emoji,
                        cost_bucket=nc_bucket,  # type: ignore[arg-type]
                        is_active=True,
                    )
                    _queue_cat_limit_sync(
                        saved, remove=(saved.cost_bucket not in _DEBIT_BUCKETS)
                    )
                    _clear_tx_editor_state(clear_all=True)
                    st.session_state["t3_ec_force_reload"] = True
                    st.session_state["t3_new_cat_reset"] = True
                    if saved.cost_bucket in _DEBIT_BUCKETS:
                        _set_flash(
                            "success",
                            f"Saved category budget '{saved.category}' ({format_pkr(saved.monthly_limit)}).",
                        )
                    else:
                        _set_flash(
                            "success",
                            f"Saved {saved.cost_bucket} category '{saved.category}'.",
                        )
                    st.rerun()

    with right_col:
        total_allocated = sum(cb.monthly_limit for cb in budgets)
        st.caption(
            f"Allocated outflow limits total **{format_pkr(total_allocated)}** "
            f"vs Month 1 Cap **{format_pkr(metrics.total_spending_cap)}**."
        )

        if not budgets:
            st.info(
                "No active outflow category budgets. Restore an archived category or add a custom category on the left."
            )
        else:
            with st.expander("💰 Edit Monthly Category PKR Caps", expanded=False):
                with st.form("t3_category_budgets_form"):
                    edited_limits: dict[str, tuple[CategoryBudget, float]] = {}
                    for cb in budgets:
                        lim_key = f"t3_cat_limit_{cb.id}_{cb.category}"
                        if lim_key not in st.session_state:
                            st.session_state[lim_key] = float(cb.monthly_limit)
                        new_lim = st.number_input(
                            f"{cb.emoji} {cb.category} · {cb.cost_bucket}",
                            min_value=0.0,
                            step=500.0,
                            key=lim_key,
                        )
                        edited_limits[cb.category] = (cb, float(new_lim))

                    if st.form_submit_button(
                        "💾 Save Category Limits",
                        type="primary",
                        width="stretch",
                    ):
                        updated_n = 0
                        for cat_name, (cb, new_lim) in edited_limits.items():
                            if abs(new_lim - cb.monthly_limit) >= 0.01:
                                saved = db.upsert_category_budget(
                                    category=cat_name,
                                    monthly_limit=new_lim,
                                    emoji=cb.emoji,
                                    cost_bucket=cb.cost_bucket,
                                    is_active=cb.is_active,
                                )
                                _queue_cat_limit_sync(saved)
                                updated_n += 1
                        if updated_n == 0:
                            _set_flash("info", "No category limit changes to save.")
                        else:
                            _set_flash(
                                "success",
                                f"Saved category budget limits ({updated_n} category limit(s) updated).",
                            )
                        st.rerun()

        archived_cnt = sum(1 for cb in all_budgets if not cb.is_active)
        active_cnt = len(all_budgets) - archived_cnt
        with st.expander(
            "🗂️ Manage Active / Archived Categories",
            expanded=False,
        ):
            st.caption(f"{active_cnt} active • {archived_cnt} archived — click ✏️ Edit to change Variable ↔ Fixed, rename, or 🗑️ Delete")
            if not all_budgets:
                st.info("No category budgets configured yet.")
            else:
                groups = [
                    (
                        "💸 Outflow Categories",
                        [cb for cb in all_budgets if cb.cost_bucket in _DEBIT_BUCKETS],
                    ),
                    (
                        "💰 Inflow Categories",
                        [cb for cb in all_budgets if cb.cost_bucket == "INFLOW"],
                    ),
                    (
                        "🔁 Own-Account Transfer Categories",
                        [
                            cb
                            for cb in all_budgets
                            if cb.cost_bucket in ("IGNORE", "INVESTMENT")
                        ],
                    ),
                ]
                for group_title, group_rows in groups:
                    if not group_rows:
                        continue
                    st.markdown(f"**{group_title}**")
                    for cb in group_rows:
                        with st.container(border=True):
                            status_tag = "Active" if cb.is_active else "Archived"
                            lim_text = (
                                f" — {html.escape(format_pkr(cb.monthly_limit))}"
                                if cb.cost_bucket in _DEBIT_BUCKETS
                                else ""
                            )
                            st.markdown(
                                f"{cb.emoji} **{html.escape(cb.category)}** "
                                f"({html.escape(cb.cost_bucket)})"
                                f"{lim_text} · `{status_tag}`"
                            )
                            with st.container(key=f"ptos_compact_cat_{cb.id}"):
                                mc_edit, mc2, mc3 = st.columns([1.0, 1.1, 0.9])
                                with mc_edit:
                                    if cb.id is not None and st.button(
                                        "✏️ Edit",
                                        key=f"t3_cat_pick_{cb.id}",
                                        width="stretch",
                                    ):
                                        pending = st.session_state.setdefault(
                                            "ptos_pending_widget_sync", {}
                                        )
                                        if isinstance(pending, dict):
                                            pending["t3_edit_cat_id"] = int(cb.id)
                                        st.session_state["t3_ec_force_reload"] = True
                                        st.rerun()
                                with mc2:
                                    if cb.is_active:
                                        if st.button(
                                            "⏸️ Archive",
                                            key=f"t3_cat_archive_{cb.id}",
                                            width="stretch",
                                        ):
                                            toggled = db.upsert_category_budget(
                                                category=cb.category,
                                                monthly_limit=cb.monthly_limit,
                                                emoji=cb.emoji,
                                                cost_bucket=cb.cost_bucket,
                                                is_active=False,
                                            )
                                            _queue_cat_limit_sync(toggled, remove=True)
                                            _clear_tx_editor_state(clear_all=True)
                                            _set_flash("info", f"Archived category '{toggled.category}'.")
                                            st.rerun()
                                    else:
                                        if st.button(
                                            "▶️ Restore",
                                            key=f"t3_cat_restore_{cb.id}",
                                            width="stretch",
                                        ):
                                            toggled = db.upsert_category_budget(
                                                category=cb.category,
                                                monthly_limit=cb.monthly_limit,
                                                emoji=cb.emoji,
                                                cost_bucket=cb.cost_bucket,
                                                is_active=True,
                                            )
                                            _queue_cat_limit_sync(
                                                toggled,
                                                remove=(toggled.cost_bucket not in _DEBIT_BUCKETS),
                                            )
                                            _clear_tx_editor_state(clear_all=True)
                                            _set_flash("success", f"Restored category '{toggled.category}'.")
                                            st.rerun()
                                with mc3:
                                    if cb.category != "Uncategorized" and cb.id is not None:
                                        with st.popover(
                                            "🗑️ Del",
                                            key=f"t3_cat_pop_del_{cb.id}",
                                            width="stretch",
                                        ):
                                            st.caption(
                                                f"Permanently delete '{cb.category}'? Existing rows will move safely to a fallback category."
                                            )
                                            if st.button(
                                                "Confirm Delete",
                                                key=f"t3_cat_del_{cb.id}",
                                                type="secondary",
                                                width="stretch",
                                            ):
                                                try:
                                                    _, reassigned = db.delete_category_budget(cb.id)
                                                except ValueError as exc:
                                                    st.error(str(exc))
                                                else:
                                                    _queue_cat_limit_sync(cb, remove=True)
                                                    _clear_tx_editor_state(clear_all=True)
                                                    st.session_state["t3_ec_force_reload"] = True
                                                    _set_flash(
                                                        "info",
                                                        f"Deleted category '{cb.category}' ({reassigned} transaction(s) reassigned).",
                                                    )
                                                    st.rerun()
                                    else:
                                        st.caption("Default")

    st.divider()
    render_section_header(
        "🔄 Recurring Bills & Subscriptions Radar",
        "Auto-detected from monthly debits; log payments or manage active commitments",
    )

    paid_cnt = sum(1 for item in metrics.upcoming_bills if item.is_paid)
    due_cnt = len(metrics.upcoming_bills) - paid_cnt

    r1, r2, r3 = st.columns(3)
    r1.metric("Active Bills", f"{len(metrics.upcoming_bills)}")
    r2.metric("Paid / Due", f"{paid_cnt} / {due_cnt}")
    r3.metric("Unpaid Remaining", format_pkr(metrics.unpaid_bills_total))

    pay_wallets = [
        w.account_name for w in db.get_wallets() if w.account_type != "INVESTMENT"
    ]
    pay_wallet = st.selectbox(
        "Wallet for 'Log Payment'",
        options=pay_wallets or ["HabibMetro"],
        key="t3_bill_pay_wallet",
    )

    if not metrics.upcoming_bills:
        st.info(
            "No active recurring bills or subscriptions configured for this month. "
            "Resume a paused commitment below or add a new recurring bill."
        )
    else:
        for status in metrics.upcoming_bills:
            c = status.commitment
            cid = c.id or 0
            with st.container(border=True):
                if status.is_paid:
                    badge_html = (
                        f'<span style="color:#10B981; font-weight:700;">'
                        f"✅ Paid {html.escape(format_pkr(status.paid_amount))} ({html.escape(status.paid_date or month_key)})"
                        f"</span>"
                    )
                else:
                    badge_html = (
                        f'<span style="color:#F59E0B; font-weight:700;">'
                        f"⏳ Due Day {c.due_day} • {html.escape(format_pkr(c.expected_amount))}"
                        f"</span>"
                    )
                st.markdown(
                    f"**{html.escape(c.title)}** &nbsp;·&nbsp; {badge_html}<br>"
                    f"<span style='font-size:0.8rem; color:#94A3B8;'>"
                    f"Keyword: <code>{html.escape(c.match_keyword)}</code> • "
                    f"{html.escape(c.category)} ({html.escape(c.cost_bucket)})"
                    f"</span>",
                    unsafe_allow_html=True,
                )
                if not status.is_paid:
                    if st.button(
                        "💳 Log Payment",
                        key=f"t3_bill_pay_{cid}",
                        type="primary",
                        width="stretch",
                    ):
                        tx_date_str = _bill_date_str_for_month(month_key, c.due_day)
                        payee_str = (
                            c.title
                            if c.match_keyword.lower() in c.title.lower()
                            else f"{c.title} - {c.match_keyword}"
                        )
                        db.add_manual_transaction(
                            tx_date=tx_date_str,
                            account_name=pay_wallet,
                            tx_type="DEBIT",
                            amount=float(c.expected_amount),
                            payee=payee_str,
                            category=c.category,
                            cost_bucket=c.cost_bucket,
                            notes=f"Logged from Bills Radar [bill:{cid}] for {month_key}",
                            review_status="MANUALLY_VERIFIED",
                        )
                        _invalidate_balance_inputs()
                        _set_flash(
                            "success",
                            f"Logged {format_pkr(c.expected_amount)} payment for '{c.title}' from {pay_wallet} on {tx_date_str}.",
                        )
                        st.rerun()
                else:
                    st.caption("✅ Matched in ledger for this month")
                with st.container(key=f"ptos_compact_bill_actions_{cid}"):
                    b3, b4 = st.columns(2)
                    with b3:
                        if st.button("⏸️ Pause", key=f"t3_bill_pause_{cid}", width="stretch"):
                            db.upsert_recurring_commitment(
                                c.model_copy(update={"is_active": False})
                            )
                            _set_flash("info", f"Paused recurring bill '{c.title}'.")
                            st.rerun()
                    with b4:
                        with st.popover(
                            "🗑️ Delete",
                            key=f"t3_bill_pop_del_{cid}",
                            width="stretch",
                        ):
                            st.caption(f"Delete recurring commitment '{c.title}'?")
                            if st.button(
                                "Confirm Delete",
                                key=f"t3_bill_del_{cid}",
                                type="secondary",
                                width="stretch",
                            ):
                                if c.id is not None:
                                    db.delete_recurring_commitment(c.id)
                                _set_flash(
                                    "warning",
                                    f"Deleted recurring commitment '{c.title}'.",
                                )
                                st.rerun()

    # Inactive / paused commitments
    all_commitments = db.get_recurring_commitments(active_only=False)
    paused_commitments = [c for c in all_commitments if not c.is_active]
    if paused_commitments:
        with st.expander("⏸️ Paused Commitments", expanded=False):
            st.caption(f"{len(paused_commitments)} paused bill(s)")
            for c in paused_commitments:
                with st.container(border=True):
                    st.markdown(
                        f"**{html.escape(c.title)}** — {format_pkr(c.expected_amount)} (Day {c.due_day}, keyword `{c.match_keyword}`)"
                    )
                    with st.container(key=f"ptos_compact_pdel_{c.id}"):
                        pc2, pc3 = st.columns(2)
                        with pc2:
                            if st.button("▶️ Resume", key=f"t3_bill_resume_{c.id}", width="stretch"):
                                db.upsert_recurring_commitment(c.model_copy(update={"is_active": True}))
                                _set_flash("success", f"Resumed '{c.title}'.")
                                st.rerun()
                        with pc3:
                            with st.popover(
                                "🗑️ Delete",
                                key=f"t3_bill_pop_pdel_{c.id}",
                                width="stretch",
                            ):
                                st.caption(f"Delete paused bill '{c.title}'?")
                                if st.button(
                                    "Confirm Delete",
                                    key=f"t3_bill_pdel_{c.id}",
                                    type="secondary",
                                    width="stretch",
                                ):
                                    if c.id is not None:
                                        db.delete_recurring_commitment(c.id)
                                    _set_flash("warning", f"Deleted '{c.title}'.")
                                    st.rerun()

    categories, bucket_map, _ = _get_category_catalog(db)
    bill_cats = _categories_for_tx_type(categories, bucket_map, "DEBIT")
    if st.session_state.pop("t3_new_bill_reset", False):
        st.session_state["t3_rc_title"] = ""
        st.session_state["t3_rc_keyword"] = ""
        st.session_state["t3_rc_amount"] = 3500.0
        st.session_state["t3_rc_due_day"] = 10
        st.session_state["t3_rc_cat"] = bill_cats[0]
        st.session_state["t3_rc_bucket"] = (
            bucket_map.get(bill_cats[0], "FIXED")
            if bucket_map.get(bill_cats[0], "FIXED") in _DEBIT_BUCKETS
            else "FIXED"
        )
    else:
        st.session_state.setdefault("t3_rc_title", "")
        st.session_state.setdefault("t3_rc_keyword", "")
        st.session_state.setdefault("t3_rc_amount", 3500.0)
        st.session_state.setdefault("t3_rc_due_day", 10)
        if "t3_rc_cat" not in st.session_state or st.session_state["t3_rc_cat"] not in bill_cats:
            st.session_state["t3_rc_cat"] = bill_cats[0]
        if "t3_rc_bucket" not in st.session_state or st.session_state["t3_rc_bucket"] not in _DEBIT_BUCKETS:
            mapped_rc_bkt = bucket_map.get(st.session_state["t3_rc_cat"], "FIXED")
            st.session_state["t3_rc_bucket"] = (
                mapped_rc_bkt if mapped_rc_bkt in _DEBIT_BUCKETS else "FIXED"
            )

    with st.expander("➕ Add New Recurring Bill / Subscription", expanded=False):
        rc_title = st.text_input(
            "Bill / Subscription Title",
            placeholder="e.g., PTCL Fiber",
            key="t3_rc_title",
        )
        rc_keyword = st.text_input(
            "Match Keyword (case-insensitive)",
            placeholder="e.g., PTCL",
            key="t3_rc_keyword",
        )
        with st.container(key="ptos_compact_t3_rc1"):
            rc_a, rc_d = st.columns(2)
            with rc_a:
                rc_amount = st.number_input(
                    "Expected Amount (PKR)",
                    min_value=1.0,
                    step=100.0,
                    key="t3_rc_amount",
                )
            with rc_d:
                rc_due_day = st.number_input(
                    "Due Day of Month",
                    min_value=1,
                    max_value=31,
                    step=1,
                    key="t3_rc_due_day",
                )
        with st.container(key="ptos_compact_t3_rc2"):
            rc_c, rc_b = st.columns(2)
            with rc_c:
                rc_cat = st.selectbox(
                    "Category",
                    options=bill_cats,
                    key="t3_rc_cat",
                    on_change=_sync_bucket_on_cat_change,
                    args=("t3_rc_cat", "t3_rc_bucket", bucket_map, "DEBIT"),
                )
            with rc_b:
                rc_bucket = st.selectbox(
                    "Cost Bucket",
                    options=list(_DEBIT_BUCKETS),
                    format_func=_format_bucket,
                    key="t3_rc_bucket",
                )
        if st.button(
            "➕ Save Recurring Commitment",
            key="t3_rc_submit",
            type="primary",
            width="stretch",
        ):
            if not rc_title.strip() or not rc_keyword.strip():
                st.error("Both Title and Match Keyword are required.")
            else:
                db.upsert_recurring_commitment(
                    RecurringCommitment(
                        title=rc_title.strip(),
                        match_keyword=rc_keyword.strip(),
                        expected_amount=float(rc_amount),
                        due_day=int(rc_due_day),
                        category=rc_cat,
                        cost_bucket=rc_bucket,  # type: ignore[arg-type]
                        is_active=True,
                    )
                )
                st.session_state["t3_new_bill_reset"] = True
                _set_flash("success", f"Saved recurring commitment '{rc_title.strip()}'.")
                st.rerun()


# ----------------------------------------------------------------------
# Tab 4: Insights & Leakage Analytics
# ----------------------------------------------------------------------


def render_insights_tab(
    db: DatabaseManager,
    metrics: DashboardMetrics,
    month_key: str,
) -> None:
    """Render Tab 4: Plotly analytics for burn split, cumulative curve, top merchants & leakage."""
    render_section_header(
        "📊 Insights & Leakage Analytics",
        f"Burn composition, daily velocity, merchant concentration & FBR/FX friction for {month_key}",
    )

    cfg = db.get_budget_config()
    if cfg.envelope_mode:
        st.info(
            "✉️ **NayaPay Envelope Mode is ON:** Hero Burn & the Fixed/Variable/Leakage split "
            "track `HabibMetro → NayaPay` top-ups (excluding NayaPay debits from burn & leakage), "
            "while category budgets & Top 5 Payees charts track granular NayaPay purchases."
        )

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

    chart_left, chart_right = st.columns(2, gap="medium")

    with chart_left:
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
                            "line": {"color": "#0B1120", "width": 2},
                        },
                        textinfo="percent",
                        textposition="inside",
                        hovertemplate="%{label}: Rs. %{value:,.0f} (%{percent})<extra></extra>",
                    )
                ]
            )
            pie_layout = _plotly_base_layout("Fixed vs Variable vs Leakage Split", height=320)
            pie_layout["margin"] = {"l": 12, "r": 12, "t": 44, "b": 56}
            pie_layout["legend"] = {
                "orientation": "h",
                "yanchor": "top",
                "y": -0.06,
                "xanchor": "center",
                "x": 0.5,
                "font": {"size": 11},
            }
            fig_pie.update_layout(**pie_layout)
            st.plotly_chart(fig_pie, width="stretch")

    with chart_right:
        if not metrics.top_merchants:
            st.info(f"No merchant debit data recorded in {month_key} yet for Top 5 Payees.")
        else:
            ordered_m = list(reversed(metrics.top_merchants))
            truncated_names = [
                (m.merchant[:13] + "…") if len(m.merchant) > 14 else m.merchant
                for m in ordered_m
            ]
            fig_merchants = go.Figure(
                data=[
                    go.Bar(
                        x=[m.total_spent for m in ordered_m],
                        y=truncated_names,
                        orientation="h",
                        marker={"color": "#10B981", "line": {"width": 0}},
                        text=[format_pkr(m.total_spent) for m in ordered_m],
                        textposition="auto",
                        customdata=[[m.category, m.tx_count, m.merchant] for m in ordered_m],
                        hovertemplate=(
                            "<b>%{customdata[2]}</b><br>"
                            "Spent: Rs. %{x:,.0f}<br>"
                            "Category: %{customdata[0]}<br>"
                            "Transactions: %{customdata[1]}<extra></extra>"
                        ),
                    )
                ]
            )
            layout_m = _plotly_base_layout("Top 5 Payees / Merchants", height=320)
            layout_m["xaxis"] = {
                "title": "PKR Spent",
                "gridcolor": "rgba(148,163,184,0.12)",
                "tickfont": {"size": 10},
            }
            layout_m["yaxis"] = {
                "automargin": True,
                "tickfont": {"size": 11, "color": "#E2E8F0"},
            }
            layout_m["margin"] = {"l": 16, "r": 20, "t": 44, "b": 36}
            fig_merchants.update_layout(**layout_m)
            st.plotly_chart(fig_merchants, width="stretch")

    # Daily Cumulative Spend Curve vs Cap
    pts = metrics.daily_cumulative_spend
    cap = float(metrics.total_spending_cap)
    if not pts or (all(p.daily_spend == 0 for p in pts) and cap <= 0):
        st.info(f"No debit spend or budget cap recorded in {month_key} yet for the daily cumulative curve.")
    else:
        days = [p.tx_date for p in pts]
        cum_vals = [p.cumulative_spend for p in pts]
        daily_vals = [p.daily_spend for p in pts]
        ideal_pace = [
            round((cap / max(1, len(pts))) * (idx + 1), 2)
            for idx in range(len(pts))
        ]

        fig_cum = go.Figure()
        fig_cum.add_trace(
            go.Bar(
                x=days,
                y=daily_vals,
                name="Daily Spend",
                marker={"color": "rgba(59, 130, 246, 0.35)"},
                hovertemplate="%{x}<br>Daily Burn: Rs. %{y:,.0f}<extra></extra>",
            )
        )
        fig_cum.add_trace(
            go.Scatter(
                x=days,
                y=cum_vals,
                mode="lines+markers",
                name="Cumulative Burn",
                line={"color": "#10B981", "width": 3},
                marker={"size": 5},
                fill="tozeroy",
                fillcolor="rgba(16, 185, 129, 0.12)",
                hovertemplate="%{x}<br>Cumulative Burn: Rs. %{y:,.0f}<extra></extra>",
            )
        )
        if cap > 0:
            fig_cum.add_trace(
                go.Scatter(
                    x=days,
                    y=ideal_pace,
                    mode="lines",
                    name="Ideal Linear Pace",
                    line={"color": "#94A3B8", "width": 1.5, "dash": "dash"},
                    hovertemplate="%{x}<br>Ideal Pace: Rs. %{y:,.0f}<extra></extra>",
                )
            )
            fig_cum.add_hline(
                y=cap,
                line_dash="dot",
                line_color="#EF4444",
                annotation_text=f"Monthly Cap ({format_pkr(cap)})",
                annotation_position="top left",
            )

        layout_c = _plotly_base_layout(
            f"Daily Cumulative Spend vs Cap ({month_key})",
            height=360,
        )
        layout_c["xaxis"] = {
            "title": "Date",
            "gridcolor": "rgba(148,163,184,0.1)",
            "nticks": 6,
            "tickfont": {"size": 10},
        }
        layout_c["yaxis"] = {
            "title": "PKR",
            "gridcolor": "rgba(148,163,184,0.12)",
            "tickfont": {"size": 10},
        }
        layout_c["legend"] = {
            "orientation": "h",
            "yanchor": "top",
            "y": -0.24,
            "xanchor": "center",
            "x": 0.5,
            "font": {"size": 10},
        }
        layout_c["margin"] = {"l": 14, "r": 16, "t": 44, "b": 68}
        fig_cum.update_layout(**layout_c)
        st.plotly_chart(fig_cum, width="stretch")

    # Itemized Tax & Bank Fee Leakage Table
    render_section_header(
        "💸 Tax, WHT & Bank Fee Leakage Ledger",
        "Every rupee lost to FBR withholding tax, FX conversion fees, and bank charges",
    )
    month_txs = db.get_transactions(month_key=month_key)
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
    else:
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
        for r in leak_rows[:15]:
            payee_esc = html.escape(str(r["payee"]))
            date_esc = html.escape(str(r["tx_date"])[5:] if len(str(r["tx_date"])) >= 10 else str(r["tx_date"]))
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
                f"</div>"
                f'<div class="ptos-tx-pills">'
                f'<span class="ptos-tx-pill">{date_esc}</span>'
                f'<span class="ptos-tx-pill ptos-tx-pill-flow">{wallet_esc}</span>'
                f'<span class="ptos-tx-pill">{cat_esc}</span>'
                f"{split_note}"
                f"</div>"
                f"</div>"
            )
        card_parts.append("</div>")
        st.markdown("".join(card_parts), unsafe_allow_html=True)

        st.dataframe(
            pd.DataFrame(leak_rows),
            width="stretch",
            hide_index=True,
            column_config={
                "tx_date": st.column_config.TextColumn("Date"),
                "account_name": st.column_config.TextColumn("Wallet"),
                "payee": st.column_config.TextColumn("Payee / Merchant"),
                "category": st.column_config.TextColumn("Category"),
                "base_amount": st.column_config.NumberColumn("Base (PKR)", format="accounting"),
                "tax_fee_amount": st.column_config.NumberColumn("Tax/FX Fee (PKR)", format="accounting"),
                "total_leakage_impact": st.column_config.NumberColumn("Leakage Impact (PKR)", format="accounting"),
            },
        )


# ----------------------------------------------------------------------
# Tab 5: Sync, PDF Import & Setup
# ----------------------------------------------------------------------


def render_sync_setup_tab(db: DatabaseManager, month_key: str) -> None:
    """Render Tab 5: Gmail sync, PDF/CSV import, Month 1 config, balance calibration & backups."""
    render_section_header(
        "⚙️ Ingestion, Month 1 Defense Config & Backups",
        "Sync Gmail receipts, import bank statements, tune your financial defense cap & export data",
    )

    col_sync, col_import = st.columns(2, gap="medium")

    with col_sync:
        with st.container(border=True):
            st.subheader("📧 Gmail Bank Alert Sync")
            auth_status = get_gmail_auth_status()
            mode = auth_status["mode"]
            if mode == "oauth":
                st.success(f"OAuth2 Mode: {auth_status['detail']}")
            elif mode == "imap":
                st.info(f"IMAP Mode: {auth_status['detail']}")
            else:
                st.warning(f"Offline / Unconfigured: {auth_status['detail']}")

            max_emails = st.selectbox(
                "Max Recent Emails to Scan",
                options=[50, 100, 250, 500],
                index=2,
                key="t5_gmail_max",
            )
            if st.button(
                "🔄 Sync Gmail Now",
                key="t5_gmail_sync_btn",
                type="primary",
                width="stretch",
            ):
                with st.spinner("Scanning Gmail for HabibMetro & NayaPay alerts..."):
                    import inspect

                    if "known_message_ids" in inspect.signature(fetch_emails_with_report).parameters:
                        known_ids: set[str] = set()
                        for existing_tx in db.get_transactions():
                            if existing_tx.message_id:
                                known_ids.add(existing_tx.message_id)
                            if existing_tx.notes:
                                for alt_m in re.findall(r"\[alt_id:([^\]]+)\]", existing_tx.notes):
                                    known_ids.add(alt_m)
                                for paired_m in re.findall(r"\[paired:([^\s\]]+)", existing_tx.notes):
                                    known_ids.add(paired_m)
                        parsed_emails, report_msg = fetch_emails_with_report(
                            max_results=int(max_emails),
                            known_message_ids=known_ids,
                        )
                    else:
                        parsed_emails, report_msg = fetch_emails_with_report(
                            max_results=int(max_emails)
                        )
                if parsed_emails:
                    categorized = apply_rules_batch(parsed_emails, db)
                    inserted, skipped = db.upsert_many(categorized)
                    linked_mirrors = db.auto_link_internal_transfer_mirrors(day_window=1)
                    if inserted > 0 or skipped > 0 or linked_mirrors > 0:
                        _clear_tx_editor_state(clear_all=True, invalidate_balances=True)
                    extra_link_note = (
                        f" Auto-linked {linked_mirrors} internal transfer pair(s)."
                        if linked_mirrors > 0
                        else ""
                    )
                    _set_flash(
                        "success",
                        f"{report_msg} Inserted {inserted} new, skipped {skipped} duplicate(s).{extra_link_note}",
                    )
                    st.rerun()
                elif "gmail sync failed" in report_msg.lower():
                    st.error(report_msg)
                elif "offline mode" in report_msg.lower():
                    st.warning(report_msg)
                else:
                    linked_mirrors = db.auto_link_internal_transfer_mirrors(day_window=1)
                    if linked_mirrors > 0:
                        _clear_tx_editor_state(clear_all=True, invalidate_balances=True)
                        _set_flash(
                            "success",
                            f"{report_msg} Auto-linked {linked_mirrors} internal transfer pair(s).",
                        )
                        st.rerun()
                    st.info(report_msg)

            with st.expander("📖 Gmail OAuth2 & IMAP Setup Guide", expanded=False):
                st.markdown(
                    """
**Option A — Google OAuth2 (Recommended, Read-Only):**
1. Create a Desktop App OAuth Client in Google Cloud Console with the **Gmail API** enabled.
2. Save the client secret JSON as `credentials.json` in the project root.
3. Click **Sync Gmail Now**; complete browser consent once to cache `token.json`.

**Option B — 60-Second IMAP App Password:**
1. Generate a Google App Password for your Gmail account.
2. Set `GMAIL_USER` and `GMAIL_APP_PASSWORD` in `.env` (see `.env.example`).
3. Restart Streamlit and click **Sync Gmail Now**.
"""
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
            if st.button(
                "📥 Import Statement(s)",
                key="t5_import_btn",
                type="primary",
                width="stretch",
            ):
                if not uploaded_files:
                    st.warning("Please upload one or more `.pdf` or `.csv` files first.")
                else:
                    acct_hint = None if hint_choice == "Auto-Detect" else hint_choice
                    all_parsed: list[ParsedTransaction] = []
                    errors: list[str] = []
                    for up_file in uploaded_files:
                        try:
                            file_txs = import_statement_file(
                                up_file.getvalue(),
                                up_file.name,
                                account_hint=acct_hint,
                            )
                            all_parsed.extend(file_txs)
                        except Exception as exc:
                            errors.append(f"{up_file.name}: {exc}")

                    if errors and all_parsed:
                        _add_flash("error", "Import errors: " + "; ".join(errors))
                        _ingest_parsed_batch(db, all_parsed, "Statement Import")
                        st.session_state["t5_uploader_nonce"] = uploader_nonce + 1
                        st.rerun()
                    elif all_parsed:
                        _ingest_parsed_batch(db, all_parsed, "Statement Import")
                        st.session_state["t5_uploader_nonce"] = uploader_nonce + 1
                        st.rerun()
                    elif errors:
                        st.error("Import errors: " + "; ".join(errors))
                    else:
                        st.warning(
                            "No parseable transactions found in the uploaded statement(s)."
                        )

    st.divider()
    col_cfg, col_bal = st.columns(2, gap="medium")

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
                "Monthly Income (PKR)",
                min_value=0.0,
                step=5000.0,
                key="t5_cfg_income",
            )
            cfg_fixed = st.number_input(
                "Fixed Cost Baseline (PKR)",
                min_value=0.0,
                step=2500.0,
                key="t5_cfg_fixed",
            )
            cfg_var = st.number_input(
                "Variable Spending Cap (PKR)",
                min_value=0.0,
                step=2500.0,
                key="t5_cfg_var",
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
                help=(
                    "When enabled, HabibMetro → NayaPay top-ups count directly as Variable Burn, "
                    "while granular NayaPay wallet debits are excluded from cap burn to prevent double-counting."
                ),
            )
            preview_cap = float(cfg_fixed) + float(cfg_var)
            preview_em = float(cfg_fixed) * int(cfg_mult)
            st.caption(
                f"Live Preview — Monthly Cap: **{format_pkr(preview_cap)}** • "
                f"Emergency Target: **{format_pkr(preview_em)}**"
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
                _set_flash(
                    "success",
                    f"Updated Month 1 Defense configuration (Cap: {format_pkr(preview_cap)}, "
                    f"Emergency Target: {format_pkr(preview_em)}).",
                )
                st.rerun()

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
                    "Enter each wallet's **exact balance right now** and save. We stamp the current date & time "
                    "(`As of Now`) across all 4 wallets—so past PDF statements or older synced emails stay in your "
                    "spending analytics **without** subtracting again from today's balance. Only newer transactions after this timestamp move live balances."
                )
                new_currents: dict[str, tuple[float, float, Optional[str]]] = {}
                for w in wallets:
                    slug = re.sub(r"[^A-Za-z0-9]+", "_", w.account_name)
                    cur_key = f"t5_cur_bal_{slug}"
                    st.session_state.setdefault(cur_key, float(w.current_balance))
                    anchor_note = (
                        f"Anchored as of {w.balance_as_of} at {format_pkr(w.opening_balance)}"
                        if w.balance_as_of
                        else f"Unanchored (Opening: {format_pkr(w.opening_balance)})"
                    )
                    val = st.number_input(
                        f"{w.icon} {w.account_name} — Balance Right Now (PKR)",
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
                        for new_val, old_val, cur_as_of in new_currents.values()
                        if abs(new_val - old_val) >= 0.01 or not cur_as_of
                    )
                    batch_ts = db.calibrate_wallets_live_batch(
                        {acct: vals[0] for acct, vals in new_currents.items()}
                    )
                    _invalidate_balance_inputs()
                    _set_flash(
                        "success",
                        f"Anchored all {len(new_currents)} wallet(s) as of {batch_ts} "
                        f"({changed} updated). Past statements/emails before {batch_ts} will stay in charts without altering these balances.",
                    )
                    st.rerun()
            else:
                st.caption(
                    "Advanced: set pre-history opening balances from BEFORE the earliest synced email or statement row "
                    "(clears date-based `As of` anchors so all historical transactions apply)."
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
                    for acct_name, (new_val, old_val, cur_as_of) in new_openings.items():
                        if abs(new_val - old_val) >= 0.01 or cur_as_of is not None:
                            db.update_wallet_opening_balance(acct_name, new_val)
                            changed += 1
                    _invalidate_balance_inputs()
                    _set_flash(
                        "success",
                        f"Updated historical starting balances ({changed} wallet(s) modified) and recomputed live balances.",
                    )
                    st.rerun()

    st.divider()
    with st.container(border=True):
        st.subheader("💾 1-Click CSV & JSON Backups")
        st.caption(
            f"Export your complete 6-table SQLite database or transaction ledger (active month: {month_key})."
        )
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        with st.container(key="ptos_compact_t5_bak"):
            b1, b2, b3 = st.columns(3)
            with b1:
                if st.button(
                    "💾 Save Local Snapshot",
                    key="t5_save_disk_backup",
                    width="stretch",
                ):
                    try:
                        json_p, csv_p = db.save_backup_to_disk(_BACKUP_DIR)
                    except OSError as exc:
                        st.error(f"Could not write backup files: {exc}")
                    else:
                        _set_flash(
                            "success",
                            f"Saved backups to `{json_p.as_posix()}` and `{csv_p.as_posix()}`.",
                        )
                        st.rerun()
            with b2:
                st.download_button(
                    label="⬇️ JSON Backup",
                    data=db.export_backup_json(),
                    file_name=f"treasury_backup_{stamp}.json",
                    mime="application/json",
                    key="t5_dl_json",
                    width="stretch",
                )
            with b3:
                st.download_button(
                    label="⬇️ CSV Ledger",
                    data=db.export_transactions_csv(),
                    file_name=f"transactions_export_{stamp}.csv",
                    mime="text/csv",
                    key="t5_dl_csv",
                    width="stretch",
                )

    with st.container(border=True):
        st.subheader("🔐 Security, Encrypted Backups & Cloud Vault")
        auth_on = is_auth_enabled()
        timeout_sec = get_session_timeout_seconds()
        cloud_status = get_cloud_sync_status()
        timeout_label = (
            f"{max(1, timeout_sec // 60)} min idle lock"
            if timeout_sec > 0
            else "No idle timeout"
        )
        if auth_on:
            st.success(f"🔒 App Lock Screen: ACTIVE ({timeout_label})")
        else:
            st.info(
                "🔓 App Lock Screen: OFF — set `TREASURY_APP_PASSWORD_HASH` or "
                "`TREASURY_APP_PASSWORD` in `.env` or Streamlit Secrets before cloud deployment."
            )

        if cloud_status["configured"]:
            st.success(
                f"☁️ Encrypted Cloud Vault ({cloud_status['provider']}): {cloud_status['detail']}"
            )
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
                help="Used to encrypt/decrypt `.ptos.enc` downloads, uploads, and cloud vault snapshots.",
            )
            effective_pass = (enc_pass or "").strip()

        with st.container(key="ptos_compact_t5_cloud"):
            ev_col1, ev_col2, ev_col3 = st.columns(3)
            with ev_col1:
                if effective_pass:
                    st.download_button(
                        label="🔐 Download Encrypted",
                        data=db.export_encrypted_backup(passphrase=effective_pass),
                        file_name=f"treasury_vault_{stamp}.ptos.enc",
                        mime="application/json",
                        key="t5_dl_enc",
                        width="stretch",
                    )
                else:
                    st.button(
                        "🔐 Download Encrypted",
                        disabled=True,
                        key="t5_dl_enc_disabled",
                        width="stretch",
                        help="Enter a passphrase above or set TREASURY_BACKUP_KEY to export encrypted vaults.",
                    )
            with ev_col2:
                can_cloud_action = bool(cloud_status.get("backend_ready") and effective_pass)
                if st.button(
                    "☁️ Push to Cloud",
                    key="t5_cloud_push_btn",
                    width="stretch",
                    disabled=not can_cloud_action,
                ):
                    ok_push, msg_push = db.sync_to_cloud_vault(
                        passphrase=(enc_pass or "").strip() or None
                    )
                    st.session_state["ptos_synced_changes"] = getattr(db, "change_counter", 0)
                    st.session_state["ptos_last_cloud_sync"] = (ok_push, msg_push)
                    _set_flash("success" if ok_push else "error", msg_push)
                    st.rerun()
            with ev_col3:
                if st.button(
                    "📥 Pull from Cloud",
                    key="t5_cloud_pull_btn",
                    width="stretch",
                    disabled=not can_cloud_action,
                ):
                    ok_pull, msg_pull = db.restore_from_cloud_vault(
                        passphrase=(enc_pass or "").strip() or None
                    )
                    if ok_pull:
                        _invalidate_balance_inputs()
                        st.session_state["ptos_synced_changes"] = getattr(
                            db, "change_counter", 0
                        )
                    st.session_state["ptos_last_cloud_sync"] = (ok_pull, msg_pull)
                    _set_flash("success" if ok_pull else "error", msg_pull)
                    st.rerun()

        restore_file = st.file_uploader(
            "Restore from Encrypted (.ptos.enc / .enc) or Checksummed (.json) Backup",
            type=["enc", "json"],
            key="t5_restore_uploader",
        )
        if restore_file is not None:
            if st.button(
                "♻️ Restore Uploaded Backup",
                key="t5_restore_vault_btn",
                type="primary",
                width="stretch",
            ):
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
                        _invalidate_balance_inputs()
                        st.session_state["ptos_synced_changes"] = getattr(
                            db, "change_counter", 0
                        )
                        _set_flash(
                            "success",
                            f"Restored {summary['transactions']} transaction(s) and {summary['accounts']} wallet(s).",
                        )
                        st.rerun()

    with st.expander("🧪 Developer & Demo Tools", expanded=False):
        st.caption(
            "Loads sample HTML email fixtures from `tests/fixtures/` idempotently for testing without real bank emails."
        )
        if st.button(
            "🧪 Load Sample Fixtures",
            key="t5_load_fixtures_btn",
            width="stretch",
        ):
            fixture_txs, loaded_names = _load_html_fixtures(_FIXTURES_DIR)
            if not loaded_names:
                st.warning(
                    "No `.html` fixture files found in `tests/fixtures/` yet."
                )
            elif not fixture_txs:
                st.warning(
                    f"Scanned {', '.join(loaded_names)} but parsed 0 transactions."
                )
            else:
                _ingest_parsed_batch(
                    db,
                    fixture_txs,
                    f"Fixtures ({', '.join(loaded_names)})",
                )
                st.rerun()

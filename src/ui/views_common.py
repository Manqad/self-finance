"""Shared UI helpers, category catalog rules, batch ingestion, and Plotly layouts."""

from __future__ import annotations

import calendar
import inspect
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

import streamlit as st
from bs4 import BeautifulSoup

from src.database.db_manager import DatabaseManager, _learned_keyword_matches
from src.engine.categorizer import (
    apply_rules_batch,
    extract_canonical_payee,
    learn_new_rule,
)
from src.ingestion.gmail_service import parse_email_payload
from src.models import (
    WALLET_CASH,
    WALLET_MMF,
    WALLET_NAYAPAY,
    CostBucket,
    ParsedTransaction,
    TransactionRecord,
    TxType,
)
from src.ui.state import (
    _BUCKET_LABELS,
    _DEBIT_BUCKETS,
    _NON_DEBIT_SYSTEM_CATEGORIES,
    _SYSTEM_CATEGORY_BUCKETS,
    _TX_TYPE_LABELS,
    _add_flash,
    _set_flash,
    invalidate_all_caches,
    navigate_to_tab,
    notify_success,
)

_FIXTURES_DIR = Path("tests/fixtures")
_BACKUP_DIR = Path("data/backups")


def format_bucket(bucket: str) -> str:
    """Return user-friendly label for a CostBucket code."""
    return _BUCKET_LABELS.get(bucket, bucket)


def format_tx_type(tx_type: str) -> str:
    """Return user-friendly label for a TxType code."""
    return _TX_TYPE_LABELS.get(tx_type, tx_type)


def get_category_catalog(
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

    for cat, bkt in db.get_distinct_categories():
        if cat and cat not in bucket_map:
            if include_archived or cat not in inactive_cats:
                categories.append(cat)
            bucket_map[cat] = bkt  # type: ignore[assignment]
            emoji_map[cat] = "📌"

    return categories, bucket_map, emoji_map


def categories_for_tx_type(
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


def buckets_for_tx_type(tx_type: str) -> list[CostBucket]:
    """Return valid CostBucket options for a transaction type."""
    if tx_type == "CREDIT":
        return ["INFLOW"]
    if tx_type == "TRANSFER_INTERNAL":
        return ["IGNORE"]
    if tx_type == "TRANSFER_MMF":
        return ["INVESTMENT"]
    return list(_DEBIT_BUCKETS)


def internal_transfer_targets(
    wallet_names: list[str],
    source_wallet: str,
) -> list[str]:
    """Return valid own-account destinations (bidirectional including MMF redemption)."""
    return [
        w
        for w in wallet_names
        if w != source_wallet and (source_wallet == WALLET_MMF or w != WALLET_MMF)
    ]


def default_transfer_category(
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


def sync_bucket_on_cat_change(
    cat_key: str,
    bkt_key: str,
    bucket_map: dict[str, CostBucket],
    tx_type: str = "DEBIT",
) -> None:
    """Keep a transaction editor's Cost Bucket aligned with selected Category and Type."""
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
    if not chosen or chosen == "Auto-Categorize (Rules)":
        return
    mapped = bucket_map.get(chosen, "VARIABLE")
    st.session_state[bkt_key] = mapped if mapped in _DEBIT_BUCKETS else "VARIABLE"


def default_date_for_month(month_key: str) -> date:
    """Return today if inside active month, else the 1st of that month."""
    today = date.today()
    if today.strftime("%Y-%m") == month_key:
        return today
    year_s, mon_s = month_key.split("-")
    return date(int(year_s), int(mon_s), 1)


def month_date_bounds(month_key: str) -> tuple[date, date]:
    """Return (first_day, last_day) dates for YYYY-MM."""
    year_s, mon_s = month_key.split("-")
    year, month = int(year_s), int(mon_s)
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def bill_date_for_month(month_key: str, due_day: int) -> date:
    """Return bill payment date: today when in current month, else clamped due_day."""
    today = date.today()
    if today.strftime("%Y-%m") == month_key:
        return today
    year_s, mon_s = month_key.split("-")
    year, month = int(year_s), int(mon_s)
    last_day = calendar.monthrange(year, month)[1]
    safe_day = min(max(1, int(due_day)), last_day)
    return date(year, month, safe_day)


def bill_date_str_for_month(month_key: str, due_day: int) -> str:
    """Return formatted YYYY-MM-DD bill payment date."""
    return bill_date_for_month(month_key, due_day).strftime("%Y-%m-%d")


def default_rule_keyword_for_tx(
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


def render_inline_category_creator(
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

    allowed_bkts = buckets_for_tx_type(tx_type)
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
                    format_func=format_bucket,
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
                return
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
                return
            saved = db.upsert_category_budget(
                category=matched.category if matched else clean_name,
                monthly_limit=float(nc_limit) if nc_bkt in _DEBIT_BUCKETS else 0.0,
                emoji=nc_emoji.strip() or (matched.emoji if matched else default_emoji),
                cost_bucket=nc_bkt,  # type: ignore[arg-type]
                is_active=True,
            )
            pending = st.session_state.setdefault("ptos_pending_widget_sync", {})
            if isinstance(pending, dict):
                pending[target_cat_key] = saved.category
                if target_bkt_key:
                    pending[target_bkt_key] = saved.cost_bucket
            st.session_state[reset_flag] = True
            notify_success(f"Created {saved.cost_bucket} category '{saved.category}'.")
            st.rerun()


def apply_pending_widget_sync() -> None:
    """Apply deferred selectbox/input updates before widgets instantiate on rerun."""
    pending = st.session_state.pop("ptos_pending_widget_sync", None)
    if isinstance(pending, dict):
        for w_key, w_val in pending.items():
            st.session_state[w_key] = w_val


def save_tx_and_maybe_learn(
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
    invalidate_all_caches()
    return updated_tx, affected_count


def ingest_parsed_batch(
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
        invalidate_all_caches()
    notify_success(
        f"{label}: parsed {len(parsed)} transaction(s) — inserted {inserted} new, skipped {skipped} duplicate(s).",
        persistent=True,
    )
    return inserted, skipped


def run_gmail_sync(db: DatabaseManager, max_emails: int) -> tuple[bool, str]:
    """Run Gmail sync through views facade (supports monkeypatching) and auto-link mirrors."""
    import src.ui.views as views_facade

    fetch_fn = views_facade.fetch_emails_with_report
    known_ids: set[str] = set()
    if "known_message_ids" in inspect.signature(fetch_fn).parameters:
        for existing_tx in db.get_transactions():
            if existing_tx.message_id:
                known_ids.add(existing_tx.message_id)
            if existing_tx.notes:
                for alt_m in re.findall(r"\[alt_id:([^\]]+)\]", existing_tx.notes):
                    known_ids.add(alt_m)
                for paired_m in re.findall(r"\[paired:([^\s\]]+)", existing_tx.notes):
                    known_ids.add(paired_m)
        parsed_emails, report_msg = fetch_fn(
            max_results=int(max_emails),
            known_message_ids=known_ids,
        )
    else:
        parsed_emails, report_msg = fetch_fn(max_results=int(max_emails))

    if parsed_emails:
        categorized = apply_rules_batch(parsed_emails, db)
        inserted, skipped = db.upsert_many(categorized)
        linked_mirrors = db.auto_link_internal_transfer_mirrors(day_window=1)
        if inserted > 0 or skipped > 0 or linked_mirrors > 0:
            invalidate_all_caches()
        extra_link = (
            f" Auto-linked {linked_mirrors} internal transfer pair(s)."
            if linked_mirrors > 0
            else ""
        )
        msg = f"{report_msg} Inserted {inserted} new, skipped {skipped} duplicate(s).{extra_link}"
        notify_success(msg, persistent=True)
        return True, msg

    if "gmail sync failed" in report_msg.lower():
        _add_flash("error", report_msg)
        return False, report_msg
    if "offline mode" in report_msg.lower():
        _add_flash("warning", report_msg)
        return False, report_msg

    linked_mirrors = db.auto_link_internal_transfer_mirrors(day_window=1)
    if linked_mirrors > 0:
        invalidate_all_caches()
        msg = f"{report_msg} Auto-linked {linked_mirrors} internal transfer pair(s)."
        notify_success(msg, persistent=True)
        return True, msg
    _add_flash("info", report_msg)
    return True, report_msg


def extract_fixture_fragments(raw_html: str) -> list[tuple[str, str, str, str]]:
    """Split an HTML fixture file into (fragment_html, subject_hint, sender_hint, date_hint) tuples."""
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


def load_html_fixtures(
    fixtures_dir: Path = _FIXTURES_DIR,
) -> tuple[list[ParsedTransaction], list[str]]:
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

        fragments = extract_fixture_fragments(raw_html)
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


def plotly_base_layout(title: str = "", height: int = 330) -> dict[str, Any]:
    """Return consistent responsive Plotly layout configuration."""
    return {
        "title": {"text": title, "font": {"size": 15, "color": "#F8FAFC"}},
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor": "rgba(0,0,0,0)",
        "font": {"color": "#CBD5E1", "size": 12},
        "height": height,
        "autosize": True,
        "margin": {"l": 14, "r": 14, "t": 44, "b": 42},
        "legend": {
            "orientation": "h",
            "yanchor": "top",
            "y": -0.14,
            "xanchor": "center",
            "x": 0.5,
            "font": {"size": 11},
        },
    }

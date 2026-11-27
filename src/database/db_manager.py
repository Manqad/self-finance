"""SQLite database manager with double-entry wallet balance recalculation."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import math
import os
import random
import re
import sqlite3
import tempfile
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from src.database.seed_rules import (
    get_default_budget_config,
    get_default_category_budgets,
    get_default_category_rules,
    get_default_non_expense_category_budgets,
    get_default_recurring_commitments,
    get_default_wallets,
)
from src.engine.categorizer import (
    _apply_rules_with_list,
    _is_masked_payee,
    extract_canonical_payee,
)
from src.models import (
    DEFAULT_WALLETS,
    WALLET_CASH,
    WALLET_MMF,
    WALLET_NAYAPAY,
    AccountWallet,
    BudgetConfig,
    CategoryBudget,
    CategoryRule,
    CostBucket,
    ParsedTransaction,
    RecurringCommitment,
    ReviewStatus,
    TransactionRecord,
    TxType,
    normalize_balance_as_of,
)
from src.security import (
    decrypt_backup_payload,
    encrypt_backup_payload,
    get_backup_passphrase,
    get_cloud_sync_status,
    get_local_now,
    get_secret,
    pull_encrypted_cloud_backup,
    push_encrypted_cloud_backup,
    redact_sensitive_text,
)

logger = logging.getLogger(__name__)
_CSV_FORMULA_CHARS = ("=", "+", "-", "@", "\t", "\r")


def _sanitize_csv_cell(value: Any) -> Any:
    """Prefix leading spreadsheet formula triggers with an apostrophe for CSV safety."""
    if not isinstance(value, str) or not value:
        return value
    stripped = value.lstrip(" ")
    if value[0] in _CSV_FORMULA_CHARS or (stripped and stripped[0] in _CSV_FORMULA_CHARS):
        return f"'{value}"
    return value

_WALLET_ORDER = {name: idx for idx, (name, _, _) in enumerate(DEFAULT_WALLETS)}
_BACKUP_SCHEMA_VERSION = 1
_HHMMSS_SUFFIX_RE = re.compile(r"(\d{2}:\d{2}:\d{2})$")

_UPDATABLE_TX_FIELDS = {
    "tx_date",
    "tx_timestamp",
    "account_name",
    "target_account",
    "tx_type",
    "amount",
    "tax_fee_amount",
    "payee",
    "merchant_alias",
    "category",
    "cost_bucket",
    "review_status",
    "is_internal_transfer",
    "source",
    "payment_channel",
    "raw_subject",
    "notes",
}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_name TEXT NOT NULL UNIQUE,
    account_type TEXT NOT NULL CHECK (account_type IN ('BANK', 'WALLET', 'CASH', 'INVESTMENT')),
    opening_balance REAL NOT NULL DEFAULT 0.0,
    current_balance REAL NOT NULL DEFAULT 0.0,
    balance_as_of TEXT,
    currency TEXT NOT NULL DEFAULT 'PKR',
    icon TEXT NOT NULL DEFAULT '🏦',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS budget_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    monthly_income REAL NOT NULL DEFAULT 200000.0 CHECK (monthly_income >= 0.0),
    fixed_cost_baseline REAL NOT NULL DEFAULT 80000.0 CHECK (fixed_cost_baseline >= 0.0),
    variable_spending_cap REAL NOT NULL DEFAULT 40000.0 CHECK (variable_spending_cap >= 0.0),
    emergency_multiplier INTEGER NOT NULL DEFAULT 6 CHECK (emergency_multiplier >= 1 AND emergency_multiplier <= 24),
    envelope_mode INTEGER NOT NULL DEFAULT 0 CHECK (envelope_mode IN (0, 1)),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS category_budgets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL UNIQUE,
    emoji TEXT NOT NULL DEFAULT '📦',
    cost_bucket TEXT NOT NULL DEFAULT 'VARIABLE' CHECK (cost_bucket IN ('INFLOW', 'FIXED', 'VARIABLE', 'LEAKAGE', 'INVESTMENT', 'IGNORE')),
    monthly_limit REAL NOT NULL DEFAULT 0.0 CHECK (monthly_limit >= 0.0),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL UNIQUE,
    tx_date TEXT NOT NULL,
    tx_timestamp TEXT,
    account_name TEXT NOT NULL,
    target_account TEXT,
    tx_type TEXT NOT NULL CHECK (tx_type IN ('DEBIT', 'CREDIT', 'TRANSFER_INTERNAL', 'TRANSFER_MMF')),
    amount REAL NOT NULL CHECK (amount > 0.0),
    tax_fee_amount REAL NOT NULL DEFAULT 0.0 CHECK (tax_fee_amount >= 0.0),
    payee TEXT NOT NULL,
    merchant_alias TEXT,
    category TEXT NOT NULL DEFAULT 'Uncategorized',
    cost_bucket TEXT NOT NULL DEFAULT 'VARIABLE' CHECK (cost_bucket IN ('INFLOW', 'FIXED', 'VARIABLE', 'LEAKAGE', 'INVESTMENT', 'IGNORE')),
    review_status TEXT NOT NULL DEFAULT 'NEEDS_REVIEW' CHECK (review_status IN ('AUTO_VERIFIED', 'NEEDS_REVIEW', 'MANUALLY_VERIFIED')),
    is_internal_transfer INTEGER NOT NULL DEFAULT 0 CHECK (is_internal_transfer IN (0, 1)),
    source TEXT NOT NULL DEFAULT 'MANUAL' CHECK (source IN ('GMAIL_HMB', 'GMAIL_NAYAPAY', 'PDF_IMPORT', 'CSV_IMPORT', 'MANUAL')),
    payment_channel TEXT,
    raw_subject TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(tx_date);
CREATE INDEX IF NOT EXISTS idx_tx_account ON transactions(account_name);
CREATE INDEX IF NOT EXISTS idx_tx_target_account ON transactions(target_account);
CREATE INDEX IF NOT EXISTS idx_tx_category ON transactions(category);
CREATE INDEX IF NOT EXISTS idx_tx_review_status ON transactions(review_status);
CREATE INDEX IF NOT EXISTS idx_tx_cost_bucket ON transactions(cost_bucket);

CREATE TABLE IF NOT EXISTS category_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT NOT NULL UNIQUE COLLATE NOCASE,
    merchant_alias TEXT,
    category TEXT NOT NULL,
    cost_bucket TEXT NOT NULL CHECK (cost_bucket IN ('INFLOW', 'FIXED', 'VARIABLE', 'LEAKAGE', 'INVESTMENT', 'IGNORE')),
    target_account TEXT,
    priority INTEGER NOT NULL DEFAULT 10,
    is_user_defined INTEGER NOT NULL DEFAULT 0 CHECK (is_user_defined IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_rules_priority ON category_rules(priority DESC);

CREATE TABLE IF NOT EXISTS recurring_commitments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL UNIQUE,
    match_keyword TEXT NOT NULL,
    expected_amount REAL NOT NULL CHECK (expected_amount > 0.0),
    due_day INTEGER NOT NULL DEFAULT 1 CHECK (due_day >= 1 AND due_day <= 31),
    category TEXT NOT NULL DEFAULT 'Bills & Utilities',
    cost_bucket TEXT NOT NULL DEFAULT 'FIXED' CHECK (cost_bucket IN ('INFLOW', 'FIXED', 'VARIABLE', 'LEAKAGE', 'INVESTMENT', 'IGNORE')),
    is_paid_this_month INTEGER NOT NULL DEFAULT 0 CHECK (is_paid_this_month IN (0, 1)),
    last_paid_month TEXT,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
);
"""

_SCHEMA_STATEMENTS = tuple(
    stmt.strip() for stmt in _SCHEMA_SQL.split(";") if stmt.strip()
)


def _validate_finite_float(value: Any, field_name: str) -> float:
    """Validate that a numeric input is a finite float (not NaN/Inf/bool)."""
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite number, not bool")
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a valid finite number") from exc
    if not math.isfinite(num):
        raise ValueError(f"{field_name} must be a finite number")
    return num


def _is_busy_error(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _escape_like(value: str) -> str:
    """Escape SQL LIKE wildcard characters using backslash as ESCAPE character."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _learned_keyword_matches(
    keyword: str, payee: str, merchant_alias: Optional[str]
) -> bool:
    """Match learned keyword against payee/alias and canonical counterparty wrappers."""
    def _raw_match(needle_kw: str, target_text: str) -> bool:
        kw_clean = needle_kw.strip()
        if not kw_clean or not target_text:
            return False
        if len(kw_clean) < 5:
            lower_kw = kw_clean.lower()
            left = r"(?<![a-z0-9])" if lower_kw[0].isalnum() else ""
            right = r"(?![a-z0-9])" if lower_kw[-1].isalnum() else ""
            pattern = re.compile(rf"{left}{re.escape(lower_kw)}{right}", re.IGNORECASE)
            return bool(pattern.search(target_text))
        return kw_clean.lower() in target_text.lower()

    kw = keyword.strip()
    if not kw:
        return False
    if _raw_match(kw, payee) or (merchant_alias and _raw_match(kw, merchant_alias)):
        return True

    canon_kw = extract_canonical_payee(kw)
    canon_payee = extract_canonical_payee(payee)
    if len(canon_kw) >= 3 and canon_kw.lower() != kw.lower():
        if _raw_match(canon_kw, payee) or (
            canon_payee and _raw_match(canon_kw, canon_payee)
        ):
            return True
    if canon_payee and canon_payee.lower() != payee.strip().lower():
        if _raw_match(kw, canon_payee):
            return True
    return False


_OUTFLOW_TX_TYPES = ("DEBIT", "TRANSFER_INTERNAL", "TRANSFER_MMF")
_INGESTION_SOURCES = frozenset(
    {"GMAIL_HMB", "GMAIL_NAYAPAY", "PDF_IMPORT", "CSV_IMPORT"}
)
_MASKED_4DIGIT_RE = re.compile(r"(?i)(?:raast[-\s]*|[*●•⚫x]{2,})(\d{4})\b")
_BOGUS_PDF_PERIOD_PAYEE_RE = re.compile(
    r"^(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s+\d{4}"
    r"(?:\s*$|.*\bcurrent\s+mailing\s+address\b)",
    re.IGNORECASE,
)


def _parse_time_seconds(tx_timestamp: Optional[str]) -> Optional[int]:
    """Return seconds since midnight for non-midnight HH:MM:SS timestamps, else None."""
    raw = (tx_timestamp or "").strip()
    m = _HHMMSS_SUFFIX_RE.search(raw)
    if not m:
        return None
    hhmmss = m.group(1)
    if hhmmss == "00:00:00":
        return None
    hh, mm, ss = (int(part) for part in hhmmss.split(":"))
    return hh * 3600 + mm * 60 + ss


def _extract_masked_4_digits(text: Optional[str]) -> set[str]:
    """Extract 4-digit account suffixes from Raast-xxxx or masked ●●●●xxxx strings."""
    return set(_MASKED_4DIGIT_RE.findall(text or ""))


_STOP_TOKENS = frozenset(
    {
        "muhammad",
        "mohammad",
        "mohammed",
        "transfer",
        "payment",
        "bank",
        "raast",
        "ibft",
        "account",
        "pkr",
        "sent",
        "paid",
        "received",
        "from",
        "the",
    }
)
_STRUCTURED_TAG_RE = re.compile(r"\[(?:alt_id|alt_src|paired|bill):[^\]]+\]")


def _canonical_tokens(canon: str) -> set[str]:
    """Return lowercase alphanumeric tokens >= 3 chars for overlap checks (excluding noise words)."""
    return {
        tok
        for tok in re.findall(r"[a-z0-9]{3,}", (canon or "").lower())
        if tok not in _STOP_TOKENS
    }


def _append_alt_tags(
    existing_notes: Optional[str],
    alt_message_id: str,
    alt_source: str,
    extra_notes: Optional[str] = None,
) -> str:
    """Append [alt_id:...]/[alt_src:...] and preserve structured tags & non-duplicate user notes."""
    base = (existing_notes or "").strip()
    extra = (extra_notes or "").strip()
    parts: list[str] = [base] if base else []
    if extra:
        if not base:
            parts.append(extra)
        else:
            # Preserve all structured tags ([alt_id:...], [paired:...], [bill:...], etc.)
            for tag in _STRUCTURED_TAG_RE.findall(extra):
                if tag not in base and tag not in " ".join(parts):
                    parts.append(tag)
            extra_plain = _STRUCTURED_TAG_RE.sub("", extra).strip(" |")
            if extra_plain and extra_plain.lower() not in base.lower():
                parts.append(extra_plain)
    joined_so_far = " ".join(parts)
    alt_id_tag = f"[alt_id:{alt_message_id}]"
    alt_src_tag = f"[alt_src:{alt_source}]"
    if alt_id_tag not in joined_so_far:
        parts.append(alt_id_tag)
    if alt_src_tag not in joined_so_far:
        parts.append(alt_src_tag)
    return " ".join(parts).strip()


def _same_source_alert_duplicate_match(row_a: Any, row_b: Any) -> bool:
    """Detect duplicate same-source Gmail alerts (same minute with :00 fallback, or identical timestamp)."""
    if row_a["account_name"] != row_b["account_name"]:
        return False
    if row_a["tx_date"] != row_b["tx_date"]:
        return False
    src_a = str(row_a["source"])
    src_b = str(row_b["source"])
    if src_a != src_b or not src_a.startswith("GMAIL_"):
        return False
    if str(row_a["message_id"]) == str(row_b["message_id"]):
        return True

    type_a = str(row_a["tx_type"])
    type_b = str(row_b["tx_type"])
    if type_a != type_b and not (
        type_a in _OUTFLOW_TX_TYPES and type_b in _OUTFLOW_TX_TYPES
    ):
        return False

    amt_a = float(row_a["amount"])
    amt_b = float(row_b["amount"])
    tax_a = float(row_a["tax_fee_amount"] or 0.0)
    tax_b = float(row_b["tax_fee_amount"] or 0.0)
    if abs(amt_a - amt_b) > 0.01 or abs(tax_a - tax_b) > 0.01:
        return False

    ts_a = str(row_a["tx_timestamp"] or "").strip().replace("T", " ")[:19]
    ts_b = str(row_b["tx_timestamp"] or "").strip().replace("T", " ")[:19]
    if len(ts_a) != 19 or len(ts_b) != 19:
        return False
    if ts_a.endswith(" 00:00:00") and ts_b.endswith(" 00:00:00"):
        return False
    # Duplicate alerts from HabibMetro pair a body HH:MM (:00) email with a full HH:MM:SS email.
    # Identical seconds or same minute with at least one :00 fallback is duplicate;
    # genuine twins with distinct non-:00 seconds (e.g. :17 vs :59) stay separate.
    if ts_a != ts_b and not (
        ts_a[:16] == ts_b[:16] and (ts_a.endswith(":00") or ts_b.endswith(":00"))
    ):
        return False

    pa = extract_canonical_payee(str(row_a["payee"] or "")).strip().lower()
    pb = extract_canonical_payee(str(row_b["payee"] or "")).strip().lower()
    if not pa or not pb:
        return False
    if pa == pb or (len(pa) >= 4 and len(pb) >= 4 and (pa in pb or pb in pa)):
        return True
    toks_a = _canonical_tokens(pa)
    toks_b = _canonical_tokens(pb)
    return bool(toks_a and toks_b and (toks_a & toks_b))


def _cross_source_rows_match(
    row_a: Any, row_b: Any
) -> tuple[bool, int, bool]:
    """Return (matches, time_diff_sec, payee_matched) for two cross-source tx rows."""
    if row_a["account_name"] != row_b["account_name"]:
        return False, 999999, False
    da = date.fromisoformat(str(row_a["tx_date"]))
    db_dt = date.fromisoformat(str(row_b["tx_date"]))
    day_delta = abs((da - db_dt).days)
    if day_delta > 1:
        return False, 999999, False
    src_a = str(row_a["source"])
    src_b = str(row_b["source"])
    if (
        src_a == src_b
        or src_a not in _INGESTION_SOURCES
        or src_b not in _INGESTION_SOURCES
    ):
        return False, 999999, False

    notes_a = str(row_a["notes"] or "")
    notes_b = str(row_b["notes"] or "")
    if f"[alt_src:{src_b}]" in notes_a or f"[alt_src:{src_a}]" in notes_b:
        return False, 999999, False

    type_a = str(row_a["tx_type"])
    type_b = str(row_b["tx_type"])
    if type_a != type_b and not (
        type_a in _OUTFLOW_TX_TYPES and type_b in _OUTFLOW_TX_TYPES
    ):
        return False, 999999, False

    amt_a = float(row_a["amount"])
    tax_a = float(row_a["tax_fee_amount"] or 0.0)
    amt_b = float(row_b["amount"])
    tax_b = float(row_b["tax_fee_amount"] or 0.0)
    if (
        abs(amt_a - amt_b) > 0.01
        and abs((amt_a + tax_a) - (amt_b + tax_b)) > 0.01
    ):
        return False, 999999, False

    def _effective_canon(row: Any) -> str:
        raw_payee = str(row["payee"] or "")
        canon = extract_canonical_payee(raw_payee).strip().lower()
        if _is_masked_payee(canon) and row["raw_subject"]:
            subj_canon = extract_canonical_payee(str(row["raw_subject"])).strip().lower()
            if subj_canon and not _is_masked_payee(subj_canon):
                return subj_canon
        return canon

    ca = _effective_canon(row_a)
    cb = _effective_canon(row_b)
    masked_a = _is_masked_payee(ca)
    masked_b = _is_masked_payee(cb)

    exact_or_sub = False
    if len(ca) >= 2 and len(cb) >= 2 and not masked_a and not masked_b:
        exact_or_sub = (ca == cb) or (
            len(ca) >= 4 and len(cb) >= 4 and (ca in cb or cb in ca)
        )

    alias_a = str(row_a["merchant_alias"] or "").strip().lower()
    alias_b = str(row_b["merchant_alias"] or "").strip().lower()
    alias_match = (
        (len(alias_a) >= 4 and len(cb) >= 4 and not masked_b and (alias_a in cb or cb in alias_a))
        or (len(alias_b) >= 4 and len(ca) >= 4 and not masked_a and (alias_b in ca or ca in alias_b))
    )
    shared_digits = bool(
        _extract_masked_4_digits(str(row_a["payee"] or ""))
        & _extract_masked_4_digits(str(row_b["payee"] or ""))
    )
    payee_match = exact_or_sub or alias_match or shared_digits

    ta = _parse_time_seconds(row_a["tx_timestamp"])
    tb = _parse_time_seconds(row_b["tx_timestamp"])
    if day_delta == 1:
        # Cross-midnight / T+1 settlement only matches with explicit payee confirmation
        if not payee_match:
            return False, 999999, False
        return True, 86400, True

    if ta is not None and tb is not None:
        diff_sec = abs(ta - tb)
        if diff_sec <= 300:
            if (
                not masked_a
                and not masked_b
                and len(ca) >= 2
                and len(cb) >= 2
                and not payee_match
                and not (_canonical_tokens(ca) & _canonical_tokens(cb))
            ):
                return False, diff_sec, False
            return True, diff_sec, payee_match
        # Both rows have real non-midnight times > 1 hour apart -> separate same-day transactions
        if diff_sec > 3600:
            return False, diff_sec, payee_match

    if payee_match:
        fallback_diff = abs(ta - tb) if (ta is not None and tb is not None) else 86400
        return True, fallback_diff, True

    return False, 999999, False


def _compute_backup_checksum(payload_without_checksum: dict[str, Any]) -> str:
    """Compute deterministic SHA-256 hex digest for JSON backup payload."""
    canonical = json.dumps(
        payload_without_checksum,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _atomic_write_bytes(target_path: Path, data: bytes) -> None:
    """Write bytes to a temp file in the target directory and atomically replace."""
    tmp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target_path.parent,
            prefix=f".{target_path.stem}_",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target_path)
    except Exception:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise


def _effective_tx_ts(tx_date: str, tx_timestamp: Optional[str]) -> str:
    """Return comparable YYYY-MM-DD HH:MM:SS timestamp for a transaction row."""
    raw_ts = (tx_timestamp or "").strip().replace("T", " ")
    if len(raw_ts) >= 19:
        return raw_ts[:19]
    if len(raw_ts) == 10:
        return f"{raw_ts} 00:00:00"
    return f"{(tx_date or '').strip()[:10]} 00:00:00"


class DatabaseManager:
    """Thread-safe SQLite manager with atomic double-entry wallet balance tracking."""

    def __init__(self, db_path: str | Path = "data/treasury.db") -> None:
        self._lock = threading.RLock()
        self.cloud_boot_restore_failed: bool = False
        raw_path = str(db_path)
        self._is_memory = raw_path == ":memory:" or raw_path.startswith("file::memory:")
        self.db_path: str | Path = raw_path if self._is_memory else Path(db_path)

        if not self._is_memory:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            conn_target = str(self.db_path)
        else:
            conn_target = raw_path

        self.conn = sqlite3.connect(
            conn_target, timeout=15.0, check_same_thread=False
        )
        self.conn.row_factory = sqlite3.Row
        try:
            self._configure_and_init()
        except Exception:
            self.conn.close()
            raise

    def _configure_and_init(self) -> None:
        """Apply SQLite pragmas (busy_timeout before WAL) and initialize schema with BUSY retry."""
        max_attempts = 15
        for attempt in range(max_attempts):
            try:
                with self._lock:
                    self.conn.execute("PRAGMA foreign_keys = ON;")
                    self.conn.execute("PRAGMA busy_timeout = 15000;")
                    if not self._is_memory:
                        self.conn.execute("PRAGMA journal_mode = WAL;")
                self.init_db()
                return
            except sqlite3.OperationalError as exc:
                if _is_busy_error(exc) and attempt < max_attempts - 1:
                    if self.conn.in_transaction:
                        self.conn.rollback()
                    time.sleep(0.02 * (attempt + 1) + random.uniform(0.0, 0.02))
                    continue
                raise

    def __enter__(self) -> "DatabaseManager":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close the persistent SQLite connection."""
        with self._lock:
            self.conn.close()

    def init_db(self) -> None:
        """Create all 6 tables and seed initial defaults idempotently when empty."""
        max_attempts = 15
        for attempt in range(max_attempts):
            try:
                with self._lock:
                    if self.conn.in_transaction:
                        self.conn.commit()
                    self.conn.execute("BEGIN IMMEDIATE;")
                    try:
                        for stmt in _SCHEMA_STATEMENTS:
                            self.conn.execute(stmt)
                        acct_cols = {
                            r["name"]
                            for r in self.conn.execute(
                                "PRAGMA table_info(accounts)"
                            ).fetchall()
                        }
                        if "balance_as_of" not in acct_cols:
                            self.conn.execute(
                                "ALTER TABLE accounts ADD COLUMN balance_as_of TEXT"
                            )
                        rule_cols = {
                            r["name"]
                            for r in self.conn.execute(
                                "PRAGMA table_info(category_rules)"
                            ).fetchall()
                        }
                        if "target_account" not in rule_cols:
                            self.conn.execute(
                                "ALTER TABLE category_rules ADD COLUMN target_account TEXT"
                            )
                        self._seed_defaults_if_empty()
                        self._reconcile_existing_duplicates_and_payees_unlocked()
                        self.conn.commit()
                    except Exception:
                        self.conn.rollback()
                        raise
                break
            except sqlite3.OperationalError as exc:
                if _is_busy_error(exc) and attempt < max_attempts - 1:
                    time.sleep(0.02 * (attempt + 1) + random.uniform(0.0, 0.02))
                    continue
                raise

        restored = self._auto_restore_from_cloud_if_empty()
        if restored:
            with self._lock, self.conn:
                self._reconcile_existing_duplicates_and_payees_unlocked()

    @property
    def change_counter(self) -> int:
        """Return cumulative SQLite INSERT/UPDATE/DELETE count for dirty-state tracking."""
        return int(self.conn.total_changes)

    def _auto_restore_from_cloud_if_empty(self) -> bool:
        """Auto-restore encrypted cloud snapshot only on a fresh untouched empty DB."""
        if self._is_memory:
            return False
        auto_restore = get_secret("TREASURY_AUTO_RESTORE", "1").lower()
        if auto_restore in {"0", "false", "no", "off"}:
            return False
        if not get_cloud_sync_status().get("configured"):
            return False
        with self._lock:
            tx_empty = self._table_is_empty("transactions")
            if not tx_empty:
                return False
            # Do not wipe user-calibrated wallets/budgets if they configured them before adding txs
            cal_row = self.conn.execute(
                "SELECT 1 FROM accounts WHERE balance_as_of IS NOT NULL OR opening_balance != 0.0 LIMIT 1"
            ).fetchone()
            if cal_row is not None:
                return False
        try:
            ok, msg = self.restore_from_cloud_vault()
            if ok:
                logger.info("Auto-restored ledger from encrypted cloud vault: %s", msg)
                self.cloud_boot_restore_failed = False
                return True
            else:
                msg_low = msg.lower()
                if any(k in msg_low for k in ("failed", "network", "error", "timeout", "http")):
                    self.cloud_boot_restore_failed = True
                logger.info("Skipped cloud auto-restore: %s", redact_sensitive_text(msg))
                return False
        except Exception as exc:
            self.cloud_boot_restore_failed = True
            logger.warning(
                "Cloud auto-restore error ignored during init: %s",
                redact_sensitive_text(exc),
            )
            return False

    def _seed_defaults_if_empty(self) -> None:
        """Populate default wallets, config, category budgets, rules, and bills if empty."""
        if self._table_is_empty("accounts"):
            for wallet in get_default_wallets():
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO accounts (
                        account_name, account_type, opening_balance, current_balance, currency, icon
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        wallet.account_name,
                        wallet.account_type,
                        wallet.opening_balance,
                        wallet.current_balance,
                        wallet.currency,
                        wallet.icon,
                    ),
                )

        if self._table_is_empty("budget_config"):
            cfg = get_default_budget_config()
            self.conn.execute(
                """
                INSERT OR IGNORE INTO budget_config (
                    id, monthly_income, fixed_cost_baseline, variable_spending_cap,
                    emergency_multiplier, envelope_mode
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    cfg.id,
                    cfg.monthly_income,
                    cfg.fixed_cost_baseline,
                    cfg.variable_spending_cap,
                    cfg.emergency_multiplier,
                    int(cfg.envelope_mode),
                ),
            )

        cur_user_ver = int(self.conn.execute("PRAGMA user_version;").fetchone()[0])
        if self._table_is_empty("category_budgets"):
            for cb in get_default_category_budgets():
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO category_budgets (
                        category, emoji, cost_bucket, monthly_limit, is_active
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        cb.category,
                        cb.emoji,
                        cb.cost_bucket,
                        cb.monthly_limit,
                        int(cb.is_active),
                    ),
                )
            self.conn.execute("PRAGMA user_version = 2;")
        elif cur_user_ver < 2:
            for cb in get_default_non_expense_category_budgets():
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO category_budgets (
                        category, emoji, cost_bucket, monthly_limit, is_active
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        cb.category,
                        cb.emoji,
                        cb.cost_bucket,
                        cb.monthly_limit,
                        int(cb.is_active),
                    ),
                )
            self.conn.execute("PRAGMA user_version = 2;")

        if self._table_is_empty("category_rules"):
            for rule in get_default_category_rules():
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO category_rules (
                        keyword, merchant_alias, category, cost_bucket, priority, is_user_defined
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rule.keyword,
                        rule.merchant_alias,
                        rule.category,
                        rule.cost_bucket,
                        rule.priority,
                        int(rule.is_user_defined),
                    ),
                )

        if self._table_is_empty("recurring_commitments"):
            for item in get_default_recurring_commitments():
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO recurring_commitments (
                        title, match_keyword, expected_amount, due_day,
                        category, cost_bucket, is_paid_this_month, last_paid_month, is_active
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.title,
                        item.match_keyword,
                        item.expected_amount,
                        item.due_day,
                        item.category,
                        item.cost_bucket,
                        int(item.is_paid_this_month),
                        item.last_paid_month,
                        int(item.is_active),
                    ),
                )

    def _table_is_empty(self, table_name: str) -> bool:
        row = self.conn.execute(f"SELECT COUNT(*) AS cnt FROM {table_name}").fetchone()
        return int(row["cnt"]) == 0

    # ------------------------------------------------------------------
    # Row converters
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_wallet(row: sqlite3.Row) -> AccountWallet:
        return AccountWallet(
            id=row["id"],
            account_name=row["account_name"],
            account_type=row["account_type"],
            opening_balance=float(row["opening_balance"]),
            current_balance=float(row["current_balance"]),
            balance_as_of=(
                row["balance_as_of"] if "balance_as_of" in row.keys() else None
            ),
            currency=row["currency"],
            icon=row["icon"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_tx(row: sqlite3.Row) -> TransactionRecord:
        return TransactionRecord(
            id=row["id"],
            message_id=row["message_id"],
            tx_date=row["tx_date"],
            tx_timestamp=row["tx_timestamp"],
            account_name=row["account_name"],
            target_account=row["target_account"],
            tx_type=row["tx_type"],
            amount=float(row["amount"]),
            tax_fee_amount=float(row["tax_fee_amount"]),
            payee=row["payee"],
            merchant_alias=row["merchant_alias"],
            category=row["category"],
            cost_bucket=row["cost_bucket"],
            review_status=row["review_status"],
            is_internal_transfer=bool(row["is_internal_transfer"]),
            source=row["source"],
            payment_channel=row["payment_channel"],
            raw_subject=row["raw_subject"],
            notes=row["notes"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_budget_config(row: sqlite3.Row) -> BudgetConfig:
        return BudgetConfig(
            id=int(row["id"]),
            monthly_income=float(row["monthly_income"]),
            fixed_cost_baseline=float(row["fixed_cost_baseline"]),
            variable_spending_cap=float(row["variable_spending_cap"]),
            emergency_multiplier=int(row["emergency_multiplier"]),
            envelope_mode=bool(row["envelope_mode"]),
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_category_budget(row: sqlite3.Row) -> CategoryBudget:
        return CategoryBudget(
            id=row["id"],
            category=row["category"],
            emoji=row["emoji"],
            cost_bucket=row["cost_bucket"],
            monthly_limit=float(row["monthly_limit"]),
            is_active=bool(row["is_active"]),
        )

    @staticmethod
    def _row_to_category_rule(row: sqlite3.Row) -> CategoryRule:
        tgt = row["target_account"] if "target_account" in row.keys() else None
        return CategoryRule(
            id=row["id"],
            keyword=row["keyword"],
            merchant_alias=row["merchant_alias"],
            category=row["category"],
            cost_bucket=row["cost_bucket"],
            target_account=tgt,
            priority=int(row["priority"]),
            is_user_defined=bool(row["is_user_defined"]),
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_recurring(row: sqlite3.Row) -> RecurringCommitment:
        return RecurringCommitment(
            id=row["id"],
            title=row["title"],
            match_keyword=row["match_keyword"],
            expected_amount=float(row["expected_amount"]),
            due_day=int(row["due_day"]),
            category=row["category"],
            cost_bucket=row["cost_bucket"],
            is_paid_this_month=bool(row["is_paid_this_month"]),
            last_paid_month=row["last_paid_month"],
            is_active=bool(row["is_active"]),
        )

    # ------------------------------------------------------------------
    # Double-Entry Wallet Balance Engine & Wallet CRUD
    # ------------------------------------------------------------------

    def _validate_accounts_unlocked(
        self,
        account_name: str,
        tx_type: TxType,
        target_account: Optional[str],
        *,
        existing_accounts: Optional[set[str]] = None,
    ) -> None:
        """Validate account_name and transfer target_account exist in accounts."""
        if existing_accounts is None:
            existing_accounts = {
                row["account_name"]
                for row in self.conn.execute("SELECT account_name FROM accounts").fetchall()
            }
        if account_name not in existing_accounts:
            raise ValueError(f"Unknown wallet account: '{account_name}'")
        if tx_type == "TRANSFER_INTERNAL":
            if not target_account or target_account not in existing_accounts:
                raise ValueError(
                    f"TRANSFER_INTERNAL requires valid target_account in accounts; got '{target_account}'"
                )
            if target_account == account_name:
                raise ValueError(
                    "TRANSFER_INTERNAL target_account must differ from account_name"
                )
        elif tx_type == "TRANSFER_MMF":
            mmf_target = target_account or WALLET_MMF
            if mmf_target not in existing_accounts:
                raise ValueError(f"Unknown MMF target account: '{mmf_target}'")

    def _recalculate_balances_unlocked(self) -> list[AccountWallet]:
        """Recompute current_balance across all accounts inside an active lock/tx."""
        acct_rows = self.conn.execute(
            "SELECT account_name, opening_balance, balance_as_of FROM accounts"
        ).fetchall()
        balances: dict[str, float] = {
            row["account_name"]: float(row["opening_balance"]) for row in acct_rows
        }
        as_of_map: dict[str, Optional[str]] = {
            row["account_name"]: normalize_balance_as_of(row["balance_as_of"])
            for row in acct_rows
        }

        tx_rows = self.conn.execute(
            """
            SELECT account_name, target_account, tx_type, amount, tax_fee_amount,
                   tx_date, tx_timestamp
            FROM transactions
            """
        ).fetchall()

        def _applies(wallet_name: str, eff_ts: str) -> bool:
            if wallet_name not in balances:
                return False
            cutoff = as_of_map.get(wallet_name)
            return cutoff is None or eff_ts > cutoff

        for tx in tx_rows:
            acct = tx["account_name"]
            target = tx["target_account"]
            tx_type = tx["tx_type"]
            amount = float(tx["amount"])
            tax_fee = float(tx["tax_fee_amount"] or 0.0)
            eff_ts = _effective_tx_ts(tx["tx_date"], tx["tx_timestamp"])

            if tx_type == "CREDIT":
                if _applies(acct, eff_ts):
                    balances[acct] += amount
            elif tx_type == "DEBIT":
                if _applies(acct, eff_ts):
                    balances[acct] -= amount + tax_fee
            elif tx_type == "TRANSFER_INTERNAL":
                if _applies(acct, eff_ts):
                    balances[acct] -= amount + tax_fee
                if target and _applies(target, eff_ts):
                    balances[target] += amount
            elif tx_type == "TRANSFER_MMF":
                if _applies(acct, eff_ts):
                    balances[acct] -= amount + tax_fee
                mmf_target = target or WALLET_MMF
                if _applies(mmf_target, eff_ts):
                    balances[mmf_target] += amount

        now_ts = get_local_now().replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
        for account_name, bal in balances.items():
            self.conn.execute(
                "UPDATE accounts SET current_balance = ?, updated_at = ? WHERE account_name = ?",
                (round(bal, 2), now_ts, account_name),
            )

        return self._get_wallets_unlocked()

    def _get_wallets_unlocked(self) -> list[AccountWallet]:
        rows = self.conn.execute("SELECT * FROM accounts ORDER BY id ASC").fetchall()
        wallets = [self._row_to_wallet(r) for r in rows]
        wallets.sort(key=lambda w: (_WALLET_ORDER.get(w.account_name, 999), w.id or 0))
        return wallets

    def recalculate_balances(self) -> list[AccountWallet]:
        """Atomically recompute current_balance across all accounts from opening_balance."""
        with self._lock, self.conn:
            return self._recalculate_balances_unlocked()

    def get_wallets(self) -> list[AccountWallet]:
        """Return all configured wallets in canonical display order."""
        with self._lock:
            return self._get_wallets_unlocked()

    def get_wallet(self, account_name: str) -> Optional[AccountWallet]:
        """Return a single wallet by account_name, or None if not found."""
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM accounts WHERE account_name = ?",
                (account_name,),
            ).fetchone()
            return self._row_to_wallet(row) if row else None

    def update_wallet_opening_balance(
        self, account_name: str, opening_balance: float
    ) -> AccountWallet:
        """Update pre-history opening_balance (clearing balance_as_of) and recompute balances."""
        clean_bal = _validate_finite_float(opening_balance, "opening_balance")
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE accounts SET opening_balance = ?, balance_as_of = NULL WHERE account_name = ?",
                (round(clean_bal, 2), account_name),
            )
            if cur.rowcount == 0:
                raise ValueError(f"Unknown wallet account: '{account_name}'")
            self._recalculate_balances_unlocked()
            row = self.conn.execute(
                "SELECT * FROM accounts WHERE account_name = ?",
                (account_name,),
            ).fetchone()
            return self._row_to_wallet(row)

    def _resolve_live_anchor_ts_unlocked(self) -> str:
        """Return a current PKT anchor timestamp >= any recently-bumped same-day tx_timestamp."""
        now_dt = get_local_now().replace(tzinfo=None)
        anchor_ts = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        horizon_ts = (now_dt + timedelta(seconds=15)).strftime("%Y-%m-%d %H:%M:%S")
        row = self.conn.execute(
            """
            SELECT MAX(tx_timestamp) AS max_ts
            FROM transactions
            WHERE tx_timestamp IS NOT NULL
              AND tx_timestamp > ?
              AND tx_timestamp <= ?
            """,
            (anchor_ts, horizon_ts),
        ).fetchone()
        max_tx_raw = row["max_ts"] if row and row["max_ts"] else None
        if max_tx_raw:
            max_tx_ts = str(max_tx_raw).strip().replace("T", " ")[:19]
            if len(max_tx_ts) == 19 and max_tx_ts > anchor_ts:
                return max_tx_ts
        return anchor_ts

    def calibrate_wallet_current_balance(
        self,
        account_name: str,
        target_current_balance: float,
        as_of: Optional[str] = None,
    ) -> AccountWallet:
        """Anchor wallet to target_current_balance as of `as_of` (default: now).

        Any transaction at or before `as_of` stays in the ledger for budgets/analytics
        without altering current_balance; only transactions strictly after `as_of`
        move current_balance from this anchored live amount.
        """
        clean_target = _validate_finite_float(
            target_current_balance, "target_current_balance"
        )
        with self._lock, self.conn:
            if as_of is not None:
                anchor_ts = normalize_balance_as_of(as_of)
                if anchor_ts is None:
                    raise ValueError("as_of cannot be blank when provided")
            else:
                anchor_ts = self._resolve_live_anchor_ts_unlocked()

            cur = self.conn.execute(
                "UPDATE accounts SET opening_balance = ?, balance_as_of = ? WHERE account_name = ?",
                (round(clean_target, 2), anchor_ts, account_name),
            )
            if cur.rowcount == 0:
                raise ValueError(f"Unknown wallet account: '{account_name}'")
            self._recalculate_balances_unlocked()
            updated = self.conn.execute(
                "SELECT * FROM accounts WHERE account_name = ?",
                (account_name,),
            ).fetchone()
            return self._row_to_wallet(updated)

    def calibrate_wallets_live_batch(
        self, balances: dict[str, float]
    ) -> str:
        """Atomically anchor multiple wallets to their current live balances as of now."""
        if not balances:
            raise ValueError("At least one wallet balance is required")
        cleaned = {
            acct: round(_validate_finite_float(val, f"balance[{acct}]"), 2)
            for acct, val in balances.items()
        }
        with self._lock, self.conn:
            batch_ts = self._resolve_live_anchor_ts_unlocked()
            for acct_name, clean_target in cleaned.items():
                cur = self.conn.execute(
                    "UPDATE accounts SET opening_balance = ?, balance_as_of = ? WHERE account_name = ?",
                    (clean_target, batch_ts, acct_name),
                )
                if cur.rowcount == 0:
                    raise ValueError(f"Unknown wallet account: '{acct_name}'")
            self._recalculate_balances_unlocked()
            return batch_ts

    # ------------------------------------------------------------------
    # Transaction CRUD
    # ------------------------------------------------------------------

    def _find_cross_source_duplicate_unlocked(
        self, tx: TransactionRecord
    ) -> Optional[sqlite3.Row]:
        """Find an existing cross-source (or same-source duplicate alert) matching date/wallet/amount/payee."""
        if tx.source not in _INGESTION_SOURCES:
            return None
        candidates = self.conn.execute(
            """
            SELECT * FROM transactions
            WHERE account_name = ?
              AND tx_date BETWEEN date(?, '-1 day') AND date(?, '+1 day')
              AND source != ?
            ORDER BY id ASC
            """,
            (tx.account_name, tx.tx_date, tx.tx_date, tx.source),
        ).fetchall()

        tx_dict = tx.model_dump()
        best_row: Optional[sqlite3.Row] = None
        best_score: Optional[tuple[int, int, int]] = None
        for row in candidates:
            matched, diff_sec, payee_match = _cross_source_rows_match(row, tx_dict)
            if not matched:
                continue
            score = (diff_sec, 0 if payee_match else 1, int(row["id"]))
            if best_score is None or score < best_score:
                best_score = score
                best_row = row
        if best_row is not None:
            return best_row

        same_candidates = self.conn.execute(
            """
            SELECT * FROM transactions
            WHERE account_name = ? AND tx_date = ? AND source = ?
            ORDER BY id ASC
            """,
            (tx.account_name, tx.tx_date, tx.source),
        ).fetchall()
        for row in same_candidates:
            if _same_source_alert_duplicate_match(row, tx_dict):
                return row
        return None

    def _reconcile_cross_source_row_unlocked(
        self, keeper: sqlite3.Row, dupe: Any
    ) -> bool:
        """Merge cross-source metadata/verification into keeper without inserting a duplicate."""
        keeper_id = int(keeper["id"])
        next_notes = _append_alt_tags(
            keeper["notes"],
            str(dupe["message_id"]),
            str(dupe["source"]),
            extra_notes=dupe["notes"],
        )
        next_subject = keeper["raw_subject"] or dupe["raw_subject"]
        next_channel = keeper["payment_channel"] or dupe["payment_channel"]
        next_alias = keeper["merchant_alias"] or dupe["merchant_alias"]

        next_payee = str(keeper["payee"])
        if _is_masked_payee(next_payee):
            cand = extract_canonical_payee(
                str(dupe["raw_subject"] or dupe["payee"] or "")
            )
            if cand and not _is_masked_payee(cand):
                next_payee = cand[:120]

        next_amount = round(float(keeper["amount"]), 2)
        next_tax = round(float(keeper["tax_fee_amount"] or 0.0), 2)
        dupe_amt = round(float(dupe["amount"]), 2)
        dupe_tax = round(float(dupe["tax_fee_amount"] or 0.0), 2)
        balance_dirty = False
        if (
            str(keeper["tx_type"]) != "CREDIT"
            and next_tax == 0.0
            and dupe_tax > 0.0
            and abs(next_amount - (dupe_amt + dupe_tax)) <= 0.01
        ):
            next_amount = dupe_amt
            next_tax = dupe_tax
            balance_dirty = True

        # Upgrade midnight (date-only) keeper timestamp if duplicate provides real time-of-day
        next_ts = keeper["tx_timestamp"]
        if (
            str(keeper["tx_date"]) == str(dupe["tx_date"])
            and _parse_time_seconds(keeper["tx_timestamp"]) is None
            and _parse_time_seconds(dupe["tx_timestamp"]) is not None
        ):
            dupe_hhmmss = str(dupe["tx_timestamp"]).strip().replace("T", " ")[11:19]
            if len(dupe_hhmmss) == 8:
                next_ts = f"{keeper['tx_date']} {dupe_hhmmss}"
                balance_dirty = True

        next_tx_type = str(keeper["tx_type"])
        next_target = keeper["target_account"]
        next_internal = int(keeper["is_internal_transfer"])
        next_cat = str(keeper["category"])
        next_bucket = str(keeper["cost_bucket"])
        next_status = str(keeper["review_status"])

        if (
            next_status == "NEEDS_REVIEW"
            and str(dupe["review_status"]) in ("MANUALLY_VERIFIED", "AUTO_VERIFIED")
            and str(dupe["category"]) != "Uncategorized"
        ):
            next_cat = str(dupe["category"])
            next_status = str(dupe["review_status"])
            if next_tx_type == "CREDIT":
                next_bucket = "INFLOW"
            elif (
                next_tx_type == "DEBIT"
                and str(dupe["tx_type"]) == "TRANSFER_INTERNAL"
                and dupe["target_account"]
                and dupe["target_account"] != keeper["account_name"]
            ):
                next_tx_type = "TRANSFER_INTERNAL"
                next_target = dupe["target_account"]
                next_internal = 1
                next_bucket = "IGNORE"
                balance_dirty = True
            elif (
                next_tx_type == "DEBIT"
                and str(dupe["tx_type"]) == "TRANSFER_MMF"
                and keeper["account_name"] != WALLET_MMF
            ):
                next_tx_type = "TRANSFER_MMF"
                next_target = WALLET_MMF
                next_internal = 0
                next_bucket = "INVESTMENT"
                balance_dirty = True
            elif str(dupe["cost_bucket"]) in ("FIXED", "VARIABLE", "LEAKAGE"):
                next_bucket = str(dupe["cost_bucket"])

        self.conn.execute(
            """
            UPDATE transactions
            SET tx_timestamp = ?,
                tx_type = ?,
                target_account = ?,
                is_internal_transfer = ?,
                amount = ?,
                tax_fee_amount = ?,
                payee = ?,
                merchant_alias = ?,
                category = ?,
                cost_bucket = ?,
                review_status = ?,
                payment_channel = ?,
                raw_subject = ?,
                notes = ?
            WHERE id = ?
            """,
            (
                next_ts,
                next_tx_type,
                next_target,
                next_internal,
                next_amount,
                next_tax,
                next_payee,
                next_alias,
                next_cat,
                next_bucket,
                next_status,
                next_channel,
                next_subject,
                next_notes,
                keeper_id,
            ),
        )
        return balance_dirty

    def _reconcile_existing_duplicates_and_payees_unlocked(self) -> dict[str, int]:
        """Idempotently clean bogus PDF headers, payees, verbose rules, and cross-source dupes."""
        removed_bogus = 0
        cleaned_payees = 0
        normalized_rules = 0
        merged_dupes = 0
        recategorized = 0
        balances_dirty = False

        # 1. Remove bogus PDF period header lines (e.g. 'Nov 2026' Rs. 12 rows)
        pdf_header_candidates = self.conn.execute(
            "SELECT id, payee FROM transactions WHERE source = 'PDF_IMPORT' AND amount = 12.0"
        ).fetchall()
        for row in pdf_header_candidates:
            if _BOGUS_PDF_PERIOD_PAYEE_RE.match(str(row["payee"] or "").strip()):
                self.conn.execute("DELETE FROM transactions WHERE id = ?", (row["id"],))
                removed_bogus += 1
                balances_dirty = True

        # 2. Clean raw subject-line & masked payees on unverified Gmail rows
        gmail_rows = self.conn.execute(
            """
            SELECT id, payee, raw_subject
            FROM transactions
            WHERE source IN ('GMAIL_NAYAPAY', 'GMAIL_HMB')
              AND review_status != 'MANUALLY_VERIFIED'
            """
        ).fetchall()
        for row in gmail_rows:
            cur_payee = str(row["payee"] or "")
            raw_subj = str(row["raw_subject"] or "")
            next_payee = cur_payee
            if _is_masked_payee(cur_payee) and raw_subj:
                cand = extract_canonical_payee(raw_subj)
                if cand and not _is_masked_payee(cand):
                    next_payee = cand[:120]
            else:
                cand = extract_canonical_payee(cur_payee)
                if cand and cand != cur_payee and len(cand) >= 2:
                    next_payee = cand[:120]
            if next_payee != cur_payee:
                self.conn.execute(
                    "UPDATE transactions SET payee = ? WHERE id = ?",
                    (next_payee, row["id"]),
                )
                cleaned_payees += 1

        # 3. Normalize verbose user-defined category_rules keywords without UNIQUE collisions
        user_rules = self.conn.execute(
            "SELECT * FROM category_rules WHERE is_user_defined = 1 ORDER BY priority DESC, id DESC"
        ).fetchall()
        for r in user_rules:
            still_exists = self.conn.execute(
                "SELECT id FROM category_rules WHERE id = ?", (r["id"],)
            ).fetchone()
            if not still_exists:
                continue
            cur_kw = str(r["keyword"]).strip()
            canon_kw = extract_canonical_payee(cur_kw).strip()
            if len(canon_kw) < 3 or canon_kw == cur_kw:
                continue
            collision = self.conn.execute(
                "SELECT id, is_user_defined FROM category_rules WHERE LOWER(keyword) = LOWER(?) AND id != ?",
                (canon_kw, r["id"]),
            ).fetchone()
            if collision is None:
                self.conn.execute(
                    "UPDATE category_rules SET keyword = ? WHERE id = ?",
                    (canon_kw, r["id"]),
                )
                normalized_rules += 1
            elif int(collision["is_user_defined"]) == 0:
                self.conn.execute(
                    "DELETE FROM category_rules WHERE id = ?", (collision["id"],)
                )
                self.conn.execute(
                    "UPDATE category_rules SET keyword = ? WHERE id = ?",
                    (canon_kw, r["id"]),
                )
                normalized_rules += 1
            else:
                # Keep the higher-(priority, id) user rule `r`, retire the older colliding rule
                self.conn.execute(
                    "DELETE FROM category_rules WHERE id = ?", (collision["id"],)
                )
                self.conn.execute(
                    "UPDATE category_rules SET keyword = ? WHERE id = ?",
                    (canon_kw, r["id"]),
                )
                normalized_rules += 1

        # 4. Merge existing cross-source duplicate pairs (keeping MANUALLY_VERIFIED / PDF)
        all_rows = self.conn.execute(
            "SELECT * FROM transactions ORDER BY id ASC"
        ).fetchall()
        by_group: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for row in all_rows:
            if str(row["source"]) in _INGESTION_SOURCES:
                by_group.setdefault(
                    (str(row["account_name"]), str(row["tx_date"])), []
                ).append(row)

        def _keep_rank(r: sqlite3.Row) -> tuple[int, int, int, int, int]:
            st_val = str(r["review_status"])
            st_rank = 3 if st_val == "MANUALLY_VERIFIED" else (2 if st_val == "AUTO_VERIFIED" else 1)
            cat_rank = 1 if str(r["category"] or "Uncategorized") != "Uncategorized" else 0
            al_rank = 1 if (r["merchant_alias"] and str(r["merchant_alias"]).strip()) else 0
            src_rank = 1 if str(r["source"]) == "PDF_IMPORT" else 0
            return (st_rank, cat_rank, al_rank, src_rank, -int(r["id"]))

        pair_candidates: list[tuple[int, int, int, int, sqlite3.Row, sqlite3.Row]] = []
        for group_rows in by_group.values():
            if len(group_rows) < 2:
                continue
            for idx_a in range(len(group_rows)):
                row_a = group_rows[idx_a]
                for idx_b in range(idx_a + 1, len(group_rows)):
                    row_b = group_rows[idx_b]
                    matched, diff_sec, payee_match = _cross_source_rows_match(
                        row_a, row_b
                    )
                    if not matched and _same_source_alert_duplicate_match(row_a, row_b):
                        matched = True
                        diff_sec = 0
                        payee_match = True
                    if matched:
                        pair_candidates.append(
                            (
                                diff_sec,
                                0 if payee_match else 1,
                                int(row_a["id"]),
                                int(row_b["id"]),
                                row_a,
                                row_b,
                            )
                        )

        pair_candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
        retired_ids: set[int] = set()
        reconciled_sources: dict[int, set[str]] = {}
        for _, _, _, _, row_a, row_b in pair_candidates:
            id_a = int(row_a["id"])
            id_b = int(row_b["id"])
            if id_a in retired_ids or id_b in retired_ids:
                continue
            fresh_a = self.conn.execute(
                "SELECT * FROM transactions WHERE id = ?", (id_a,)
            ).fetchone()
            fresh_b = self.conn.execute(
                "SELECT * FROM transactions WHERE id = ?", (id_b,)
            ).fetchone()
            if not fresh_a or not fresh_b:
                continue
            # Re-verify match on fresh DB rows (respecting any newly added [alt_src:...] tags)
            cross_ok, _, _ = _cross_source_rows_match(fresh_a, fresh_b)
            if not cross_ok and not _same_source_alert_duplicate_match(fresh_a, fresh_b):
                continue
            if _keep_rank(fresh_a) >= _keep_rank(fresh_b):
                keeper_row, dupe_row = fresh_a, fresh_b
            else:
                keeper_row, dupe_row = fresh_b, fresh_a
            keeper_id = int(keeper_row["id"])
            dupe_id = int(dupe_row["id"])
            dupe_src = str(dupe_row["source"])
            if dupe_src != str(keeper_row["source"]) and dupe_src in reconciled_sources.get(keeper_id, set()):
                continue
            self._reconcile_cross_source_row_unlocked(keeper_row, dupe_row)
            self.conn.execute("DELETE FROM transactions WHERE id = ?", (dupe_id,))
            retired_ids.add(dupe_id)
            reconciled_sources.setdefault(keeper_id, set()).add(dupe_src)
            merged_dupes += 1
            balances_dirty = True

        # 5. Re-evaluate remaining NEEDS_REVIEW rows against normalized rules & cross-source verified history
        needs_rows = self.conn.execute(
            "SELECT * FROM transactions WHERE review_status = 'NEEDS_REVIEW' ORDER BY id ASC"
        ).fetchall()
        if needs_rows:
            rules = [
                self._row_to_category_rule(r)
                for r in self.conn.execute(
                    "SELECT * FROM category_rules ORDER BY priority DESC, length(keyword) DESC, id DESC"
                ).fetchall()
            ]
            verified_rows = [
                self._row_to_tx(r)
                for r in self.conn.execute(
                    """
                    SELECT * FROM transactions
                    WHERE review_status = 'MANUALLY_VERIFIED' AND category != 'Uncategorized'
                    ORDER BY tx_date DESC, id DESC
                    """
                ).fetchall()
            ]
            for nr in needs_rows:
                tx_obj = self._row_to_tx(nr)
                cross_verified = [v for v in verified_rows if v.source != tx_obj.source]
                updated_obj = _apply_rules_with_list(
                    tx_obj, rules, verified_txs=cross_verified
                )
                if (
                    updated_obj.review_status != tx_obj.review_status
                    or updated_obj.category != tx_obj.category
                    or updated_obj.tx_type != tx_obj.tx_type
                    or updated_obj.merchant_alias != tx_obj.merchant_alias
                ):
                    if (
                        updated_obj.tx_type != tx_obj.tx_type
                        or updated_obj.target_account != tx_obj.target_account
                        or updated_obj.cost_bucket != tx_obj.cost_bucket
                    ):
                        balances_dirty = True
                    self.conn.execute(
                        """
                        UPDATE transactions
                        SET tx_type = ?,
                            target_account = ?,
                            is_internal_transfer = ?,
                            merchant_alias = ?,
                            category = ?,
                            cost_bucket = ?,
                            review_status = ?
                        WHERE id = ?
                        """,
                        (
                            updated_obj.tx_type,
                            updated_obj.target_account,
                            int(updated_obj.is_internal_transfer),
                            updated_obj.merchant_alias,
                            updated_obj.category,
                            updated_obj.cost_bucket,
                            updated_obj.review_status,
                            tx_obj.id,
                        ),
                    )
                    recategorized += 1

        if balances_dirty:
            self._recalculate_balances_unlocked()

        return {
            "removed_bogus": removed_bogus,
            "cleaned_payees": cleaned_payees,
            "normalized_rules": normalized_rules,
            "merged_dupes": merged_dupes,
            "recategorized": recategorized,
        }

    def reconcile_existing_duplicates_and_payees(self) -> dict[str, int]:
        """Reconcile duplicates, normalize payees/rules, and re-apply rules atomically."""
        with self._lock, self.conn:
            return self._reconcile_existing_duplicates_and_payees_unlocked()

    def _insert_tx_ignore(self, tx: TransactionRecord) -> Optional[int]:
        candidate_ids = [tx.message_id]
        if tx.message_id.startswith("XGM_") and tx.message_id[4:].isdigit():
            candidate_ids.append(format(int(tx.message_id[4:]), "x"))
        elif re.fullmatch(r"[0-9a-fA-F]{12,16}", tx.message_id):
            candidate_ids.append(f"XGM_{int(tx.message_id, 16)}")

        for cid in candidate_ids:
            dup_id_row = self.conn.execute(
                """
                SELECT id FROM transactions
                WHERE message_id = ?
                   OR COALESCE(notes, '') LIKE ? ESCAPE '\\'
                   OR COALESCE(notes, '') LIKE ? ESCAPE '\\'
                LIMIT 1
                """,
                (
                    cid,
                    f"%[alt_id:{_escape_like(cid)}]%",
                    f"%[paired:{_escape_like(cid)} %",
                ),
            ).fetchone()
            if dup_id_row is not None:
                return None

        cross_match = self._find_cross_source_duplicate_unlocked(tx)
        if cross_match is not None:
            dirty = self._reconcile_cross_source_row_unlocked(
                cross_match, tx.model_dump()
            )
            if dirty:
                self._recalculate_balances_unlocked()
            return None

        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO transactions (
                message_id, tx_date, tx_timestamp, account_name, target_account,
                tx_type, amount, tax_fee_amount, payee, merchant_alias,
                category, cost_bucket, review_status, is_internal_transfer,
                source, payment_channel, raw_subject, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tx.message_id,
                tx.tx_date,
                tx.tx_timestamp,
                tx.account_name,
                tx.target_account,
                tx.tx_type,
                round(tx.amount, 2),
                round(tx.tax_fee_amount, 2),
                tx.payee,
                tx.merchant_alias,
                tx.category,
                tx.cost_bucket,
                tx.review_status,
                int(tx.is_internal_transfer),
                tx.source,
                tx.payment_channel,
                tx.raw_subject,
                tx.notes,
            ),
        )
        if cur.rowcount == 1:
            return int(cur.lastrowid)
        return None

    def upsert_transaction(self, tx: TransactionRecord) -> bool:
        """Insert a transaction idempotently by message_id and recalculate balances if inserted."""
        validated = TransactionRecord.model_validate(tx)
        _validate_finite_float(validated.amount, "amount")
        _validate_finite_float(validated.tax_fee_amount, "tax_fee_amount")
        with self._lock, self.conn:
            self._validate_accounts_unlocked(
                validated.account_name, validated.tx_type, validated.target_account
            )
            inserted_id = self._insert_tx_ignore(validated)
            if inserted_id is not None:
                self._recalculate_balances_unlocked()
                return True
            return False

    def upsert_many(self, txs: list[TransactionRecord]) -> tuple[int, int]:
        """Batch-insert transactions idempotently; returns (inserted_count, skipped_count)."""
        if not txs:
            return 0, 0
        validated_list = [TransactionRecord.model_validate(tx) for tx in txs]
        for validated in validated_list:
            _validate_finite_float(validated.amount, "amount")
            _validate_finite_float(validated.tax_fee_amount, "tax_fee_amount")

        inserted = 0
        skipped = 0
        with self._lock, self.conn:
            existing_accounts = {
                row["account_name"]
                for row in self.conn.execute("SELECT account_name FROM accounts").fetchall()
            }
            for validated in validated_list:
                self._validate_accounts_unlocked(
                    validated.account_name,
                    validated.tx_type,
                    validated.target_account,
                    existing_accounts=existing_accounts,
                )
            for validated in validated_list:
                if self._insert_tx_ignore(validated) is not None:
                    inserted += 1
                else:
                    skipped += 1
            if inserted > 0:
                self._recalculate_balances_unlocked()
        return inserted, skipped

    def add_manual_transaction(
        self,
        tx_date: str,
        account_name: str,
        tx_type: TxType,
        amount: float,
        payee: str,
        category: Optional[str] = None,
        cost_bucket: Optional[CostBucket] = None,
        target_account: Optional[str] = None,
        tax_fee_amount: float = 0.0,
        payment_channel: Optional[str] = None,
        notes: Optional[str] = None,
        review_status: Optional[ReviewStatus] = None,
        tx_timestamp: Optional[str] = None,
    ) -> TransactionRecord:
        """Create and persist a manual transaction with auto-categorization when omitted."""
        clean_amount = _validate_finite_float(amount, "amount")
        clean_tax = _validate_finite_float(tax_fee_amount, "tax_fee_amount")
        explicit_ts = tx_timestamp
        with self._lock:
            self._validate_accounts_unlocked(account_name, tx_type, target_account)

        msg_id = f"MANUAL_{uuid4()}"
        if category is None:
            from src.engine.categorizer import apply_rules

            parsed = ParsedTransaction(
                message_id=msg_id,
                tx_date=tx_date,
                tx_timestamp=explicit_ts,
                account_name=account_name,
                target_account=target_account,
                tx_type=tx_type,
                amount=clean_amount,
                tax_fee_amount=clean_tax,
                payee=payee,
                source="MANUAL",
                payment_channel=payment_channel,
                notes=notes,
            )
            record = apply_rules(parsed, self)
            if cost_bucket is not None:
                record.cost_bucket = cost_bucket
            if review_status is not None:
                record.review_status = review_status
            record = TransactionRecord.model_validate(record)
        else:
            resolved_bucket = cost_bucket or self._infer_bucket_for_category(
                category, tx_type
            )
            resolved_status: ReviewStatus = review_status or "MANUALLY_VERIFIED"
            record = TransactionRecord(
                message_id=msg_id,
                tx_date=tx_date,
                tx_timestamp=explicit_ts,
                account_name=account_name,
                target_account=target_account,
                tx_type=tx_type,
                amount=clean_amount,
                tax_fee_amount=clean_tax,
                payee=payee,
                category=category,
                cost_bucket=resolved_bucket,
                review_status=resolved_status,
                is_internal_transfer=(tx_type == "TRANSFER_INTERNAL"),
                source="MANUAL",
                payment_channel=payment_channel,
                notes=notes,
            )

        with self._lock, self.conn:
            self._validate_accounts_unlocked(
                record.account_name, record.tx_type, record.target_account
            )
            if explicit_ts is None:
                now_dt = get_local_now().replace(tzinfo=None)
                today_str = now_dt.strftime("%Y-%m-%d")
                if record.tx_date == today_str:
                    clean_ts = now_dt.strftime("%Y-%m-%d %H:%M:%S")
                    acct_rows = self.conn.execute(
                        "SELECT balance_as_of FROM accounts WHERE account_name IN (?, ?)",
                        (record.account_name, record.target_account or ""),
                    ).fetchall()
                    cutoffs = [
                        c
                        for c in (normalize_balance_as_of(r["balance_as_of"]) for r in acct_rows)
                        if c is not None
                    ]
                    max_cutoff = max(cutoffs, default=None)
                    if (
                        max_cutoff
                        and clean_ts <= max_cutoff
                        and max_cutoff[:10] == today_str
                    ):
                        cutoff_dt = datetime.strptime(max_cutoff, "%Y-%m-%d %H:%M:%S")
                        if abs((cutoff_dt - now_dt).total_seconds()) <= 5.0 and max_cutoff[11:] < "23:59:59":
                            clean_ts = (cutoff_dt + timedelta(seconds=1)).strftime(
                                "%Y-%m-%d %H:%M:%S"
                            )
                    record.tx_timestamp = clean_ts
            row_id = self._insert_tx_ignore(record)
            if row_id is None:
                raise RuntimeError("Failed to insert manual transaction")
            self._recalculate_balances_unlocked()
            row = self.conn.execute(
                "SELECT * FROM transactions WHERE id = ?",
                (row_id,),
            ).fetchone()
            return self._row_to_tx(row)

    def _infer_bucket_for_category_unlocked(
        self, category: str, tx_type: TxType
    ) -> CostBucket:
        if tx_type == "CREDIT":
            return "INFLOW"
        if tx_type == "TRANSFER_INTERNAL":
            return "IGNORE"
        if tx_type == "TRANSFER_MMF":
            return "INVESTMENT"
        row = self.conn.execute(
            "SELECT cost_bucket FROM category_budgets WHERE category = ?",
            (category,),
        ).fetchone()
        if row and row["cost_bucket"] in ("FIXED", "VARIABLE", "LEAKAGE"):
            return row["cost_bucket"]
        return "VARIABLE"

    def _infer_bucket_for_category(self, category: str, tx_type: TxType) -> CostBucket:
        with self._lock:
            return self._infer_bucket_for_category_unlocked(category, tx_type)

    def get_transactions(
        self,
        month_key: Optional[str] = None,
        account_name: Optional[str] = None,
        category: Optional[str] = None,
        cost_bucket: Optional[CostBucket] = None,
        review_status: Optional[ReviewStatus] = None,
        search_query: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        include_incoming_transfers: bool = False,
    ) -> list[TransactionRecord]:
        """Query persisted transactions with optional filters and escaped LIKE wildcards."""
        clauses: list[str] = []
        params: list[Any] = []

        if month_key:
            clauses.append(r"tx_date LIKE ? ESCAPE '\'")
            params.append(f"{_escape_like(month_key)}-%")
        if account_name:
            if include_incoming_transfers:
                clauses.append("(account_name = ? OR target_account = ?)")
                params.extend([account_name, account_name])
            else:
                clauses.append("account_name = ?")
                params.append(account_name)
        if category:
            clauses.append("category = ?")
            params.append(category)
        if cost_bucket:
            clauses.append("cost_bucket = ?")
            params.append(cost_bucket)
        if review_status:
            clauses.append("review_status = ?")
            params.append(review_status)
        if start_date:
            clauses.append("tx_date >= ?")
            params.append(start_date)
        if end_date:
            clauses.append("tx_date <= ?")
            params.append(end_date)
        if search_query and search_query.strip():
            escaped = _escape_like(search_query.strip().lower())
            like_val = f"%{escaped}%"
            clauses.append(
                "("
                r"LOWER(payee) LIKE ? ESCAPE '\' "
                r"OR LOWER(COALESCE(merchant_alias, '')) LIKE ? ESCAPE '\' "
                r"OR LOWER(category) LIKE ? ESCAPE '\' "
                r"OR LOWER(COALESCE(notes, '')) LIKE ? ESCAPE '\' "
                r"OR LOWER(COALESCE(raw_subject, '')) LIKE ? ESCAPE '\'"
                ")"
            )
            params.extend([like_val, like_val, like_val, like_val, like_val])

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT * FROM transactions {where_sql} "
            "ORDER BY tx_date DESC, COALESCE(tx_timestamp, tx_date || ' 00:00:00') DESC, id DESC"
        )
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
            return [self._row_to_tx(r) for r in rows]

    def get_transaction_by_id(self, tx_id: int) -> Optional[TransactionRecord]:
        """Fetch a single transaction by its primary key ID."""
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM transactions WHERE id = ?",
                (tx_id,),
            ).fetchone()
            return self._row_to_tx(row) if row else None

    def update_transaction(
        self, tx_id: int, **fields: Any
    ) -> Optional[TransactionRecord]:
        """Update fields on an existing transaction and recompute wallet balances."""
        unknown = set(fields.keys()) - _UPDATABLE_TX_FIELDS
        if unknown:
            raise ValueError(f"Unsupported transaction update fields: {sorted(unknown)}")
        if "amount" in fields:
            fields["amount"] = _validate_finite_float(fields["amount"], "amount")
        if "tax_fee_amount" in fields:
            fields["tax_fee_amount"] = _validate_finite_float(
                fields["tax_fee_amount"], "tax_fee_amount"
            )

        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM transactions WHERE id = ?",
                (tx_id,),
            ).fetchone()
            if not row:
                return None

            existing = self._row_to_tx(row)
            merged_data = existing.model_dump()
            merged_data.update(fields)

            if "tx_date" in fields and "tx_timestamp" not in fields:
                raw_date = str(fields["tx_date"]).strip()
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
                    if existing.tx_timestamp:
                        m_time = _HHMMSS_SUFFIX_RE.search(existing.tx_timestamp.strip())
                        if m_time:
                            merged_data["tx_timestamp"] = f"{raw_date} {m_time.group(1)}"
                        else:
                            merged_data["tx_timestamp"] = f"{raw_date} 00:00:00"
                    else:
                        merged_data["tx_timestamp"] = f"{raw_date} 00:00:00"
                else:
                    merged_data["tx_timestamp"] = None

            new_tx_type = merged_data["tx_type"]
            if new_tx_type == "TRANSFER_INTERNAL":
                merged_data["cost_bucket"] = "IGNORE"
                merged_data["is_internal_transfer"] = True
            elif new_tx_type == "TRANSFER_MMF":
                merged_data["target_account"] = WALLET_MMF
                merged_data["is_internal_transfer"] = False
                merged_data["cost_bucket"] = "INVESTMENT"
            elif new_tx_type == "CREDIT":
                merged_data["target_account"] = None
                merged_data["is_internal_transfer"] = False
                merged_data["cost_bucket"] = "INFLOW"
            elif new_tx_type == "DEBIT":
                merged_data["target_account"] = None
                merged_data["is_internal_transfer"] = False
                if str(merged_data.get("cost_bucket") or "") in (
                    "INFLOW",
                    "IGNORE",
                    "INVESTMENT",
                ):
                    if "cost_bucket" in fields:
                        raise ValueError(
                            f"DEBIT transactions cannot use cost_bucket '{merged_data.get('cost_bucket')}'."
                        )
                    merged_data["cost_bucket"] = (
                        self._infer_bucket_for_category_unlocked(
                            str(merged_data.get("category") or "Uncategorized"),
                            "DEBIT",
                        )
                    )

            updated = TransactionRecord.model_validate(merged_data)
            self._validate_accounts_unlocked(
                updated.account_name, updated.tx_type, updated.target_account
            )

            self.conn.execute(
                """
                UPDATE transactions SET
                    tx_date = ?, tx_timestamp = ?, account_name = ?, target_account = ?,
                    tx_type = ?, amount = ?, tax_fee_amount = ?, payee = ?,
                    merchant_alias = ?, category = ?, cost_bucket = ?, review_status = ?,
                    is_internal_transfer = ?, source = ?, payment_channel = ?,
                    raw_subject = ?, notes = ?
                WHERE id = ?
                """,
                (
                    updated.tx_date,
                    updated.tx_timestamp,
                    updated.account_name,
                    updated.target_account,
                    updated.tx_type,
                    round(updated.amount, 2),
                    round(updated.tax_fee_amount, 2),
                    updated.payee,
                    updated.merchant_alias,
                    updated.category,
                    updated.cost_bucket,
                    updated.review_status,
                    int(updated.is_internal_transfer),
                    updated.source,
                    updated.payment_channel,
                    updated.raw_subject,
                    updated.notes,
                    tx_id,
                ),
            )
            self._recalculate_balances_unlocked()
            new_row = self.conn.execute(
                "SELECT * FROM transactions WHERE id = ?",
                (tx_id,),
            ).fetchone()
            return self._row_to_tx(new_row)

    def delete_transaction(self, tx_id: int) -> bool:
        """Delete a transaction by ID and recompute wallet balances."""
        with self._lock, self.conn:
            cur = self.conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
            if cur.rowcount > 0:
                self._recalculate_balances_unlocked()
                return True
            return False

    def get_available_months(self) -> list[str]:
        """Return distinct YYYY-MM months present in transactions plus current month, sorted DESC."""
        current_month = get_local_now().strftime("%Y-%m")
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT DISTINCT substr(tx_date, 1, 7) AS month_key
                FROM transactions
                WHERE length(tx_date) >= 7
                """
            ).fetchall()
        months = {row["month_key"] for row in rows if row["month_key"]}
        months.add(current_month)
        return sorted(months, reverse=True)

    def has_any_transactions(self) -> bool:
        """Return True if at least one transaction exists without hydrating Pydantic models."""
        with self._lock:
            row = self.conn.execute("SELECT 1 FROM transactions LIMIT 1").fetchone()
        return row is not None

    def get_needs_review_count(self) -> int:
        """Return total NEEDS_REVIEW count via lightweight SQL COUNT(*)."""
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS cnt FROM transactions WHERE review_status = 'NEEDS_REVIEW'"
            ).fetchone()
        return int(row["cnt"]) if row else 0

    def get_month_transaction_counts(self) -> dict[str, int]:
        """Return {YYYY-MM: count} via lightweight SQL GROUP BY."""
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT substr(tx_date, 1, 7) AS month_key, COUNT(*) AS cnt
                FROM transactions
                WHERE length(tx_date) >= 7
                GROUP BY month_key
                """
            ).fetchall()
        return {str(r["month_key"]): int(r["cnt"]) for r in rows if r["month_key"]}

    def get_distinct_categories(self) -> list[tuple[str, str]]:
        """Return distinct (category, cost_bucket) pairs from transactions via SQL."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT category, cost_bucket FROM transactions"
            ).fetchall()
        return [(str(r["category"]), str(r["cost_bucket"])) for r in rows if r["category"]]

    # ------------------------------------------------------------------
    # Budget Config, Category Budgets, Category Rules & Recurring Bills
    # ------------------------------------------------------------------

    def _get_budget_config_unlocked(self) -> BudgetConfig:
        row = self.conn.execute("SELECT * FROM budget_config WHERE id = 1").fetchone()
        if not row:
            cfg = get_default_budget_config()
            self.conn.execute(
                """
                INSERT INTO budget_config (
                    id, monthly_income, fixed_cost_baseline, variable_spending_cap,
                    emergency_multiplier, envelope_mode
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    cfg.id,
                    cfg.monthly_income,
                    cfg.fixed_cost_baseline,
                    cfg.variable_spending_cap,
                    cfg.emergency_multiplier,
                    int(cfg.envelope_mode),
                ),
            )
            row = self.conn.execute("SELECT * FROM budget_config WHERE id = 1").fetchone()
        return self._row_to_budget_config(row)

    def get_budget_config(self) -> BudgetConfig:
        """Return the singleton BudgetConfig row (id=1)."""
        with self._lock, self.conn:
            return self._get_budget_config_unlocked()

    def update_budget_config(self, **kwargs: Any) -> BudgetConfig:
        """Update BudgetConfig parameters and return the persisted configuration."""
        unknown = set(kwargs.keys()) - set(BudgetConfig.model_fields.keys())
        if unknown:
            raise ValueError(f"Unsupported budget_config fields: {sorted(unknown)}")
        if "id" in kwargs and kwargs["id"] != 1:
            raise ValueError("BudgetConfig id must be 1")
        for num_field in (
            "monthly_income",
            "fixed_cost_baseline",
            "variable_spending_cap",
        ):
            if num_field in kwargs:
                kwargs[num_field] = _validate_finite_float(kwargs[num_field], num_field)

        with self._lock, self.conn:
            current = self._get_budget_config_unlocked()
            merged = current.model_dump()
            merged.update(kwargs)
            merged["id"] = 1
            validated = BudgetConfig.model_validate(merged)
            now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.conn.execute(
                """
                UPDATE budget_config SET
                    monthly_income = ?,
                    fixed_cost_baseline = ?,
                    variable_spending_cap = ?,
                    emergency_multiplier = ?,
                    envelope_mode = ?,
                    updated_at = ?
                WHERE id = 1
                """,
                (
                    round(validated.monthly_income, 2),
                    round(validated.fixed_cost_baseline, 2),
                    round(validated.variable_spending_cap, 2),
                    validated.emergency_multiplier,
                    int(validated.envelope_mode),
                    now_ts,
                ),
            )
            row = self.conn.execute("SELECT * FROM budget_config WHERE id = 1").fetchone()
            return self._row_to_budget_config(row)

    def get_category_budgets(self, active_only: bool = True) -> list[CategoryBudget]:
        """Return category sub-budgets ordered by id."""
        sql = "SELECT * FROM category_budgets"
        if active_only:
            sql += " WHERE is_active = 1"
        sql += " ORDER BY id ASC"
        with self._lock:
            rows = self.conn.execute(sql).fetchall()
            return [self._row_to_category_budget(r) for r in rows]

    def upsert_category_budget(
        self,
        category: str,
        monthly_limit: float,
        emoji: str = "📦",
        cost_bucket: CostBucket = "VARIABLE",
        is_active: bool = True,
    ) -> CategoryBudget:
        """Insert or update a per-category monthly spending limit."""
        clean_limit = _validate_finite_float(monthly_limit, "monthly_limit")
        validated = CategoryBudget(
            category=category.strip(),
            emoji=emoji,
            cost_bucket=cost_bucket,
            monthly_limit=clean_limit,
            is_active=is_active,
        )
        with self._lock, self.conn:
            existing_ci = self.conn.execute(
                "SELECT id, category FROM category_budgets WHERE LOWER(category) = LOWER(?)",
                (validated.category,),
            ).fetchone()
            target_cat = str(existing_ci["category"]) if existing_ci else validated.category
            self.conn.execute(
                """
                INSERT INTO category_budgets (category, emoji, cost_bucket, monthly_limit, is_active)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(category) DO UPDATE SET
                    emoji = excluded.emoji,
                    cost_bucket = excluded.cost_bucket,
                    monthly_limit = excluded.monthly_limit,
                    is_active = excluded.is_active
                """,
                (
                    target_cat,
                    validated.emoji,
                    validated.cost_bucket,
                    round(validated.monthly_limit, 2),
                    int(validated.is_active),
                ),
            )
            row = self.conn.execute(
                "SELECT * FROM category_budgets WHERE category = ?",
                (target_cat,),
            ).fetchone()
            return self._row_to_category_budget(row)

    def update_category_budget_by_id(
        self,
        cat_id: int,
        *,
        category: str,
        emoji: str,
        cost_bucket: CostBucket,
        monthly_limit: float,
        is_active: bool = True,
    ) -> CategoryBudget:
        """Edit or rename a category by ID and cascade renames to transactions, rules, and bills."""
        clean_limit = _validate_finite_float(monthly_limit, "monthly_limit")
        validated = CategoryBudget(
            id=cat_id,
            category=" ".join(category.split()).strip(),
            emoji=(emoji or "🏷️").strip() or "🏷️",
            cost_bucket=cost_bucket,
            monthly_limit=0.0 if cost_bucket in ("INFLOW", "IGNORE", "INVESTMENT") else clean_limit,
            is_active=is_active,
        )
        with self._lock, self.conn:
            existing_row = self.conn.execute(
                "SELECT * FROM category_budgets WHERE id = ?",
                (cat_id,),
            ).fetchone()
            if not existing_row:
                raise ValueError(f"Category budget id={cat_id} does not exist")
            old_name = str(existing_row["category"])
            if old_name == "Uncategorized":
                if validated.category != "Uncategorized":
                    raise ValueError("The system fallback category 'Uncategorized' cannot be renamed.")
                if validated.cost_bucket not in ("FIXED", "VARIABLE", "LEAKAGE"):
                    raise ValueError("'Uncategorized' must remain an Outflow bucket.")

            conflict = self.conn.execute(
                "SELECT id FROM category_budgets WHERE LOWER(category) = LOWER(?) AND id != ?",
                (validated.category, cat_id),
            ).fetchone()
            if conflict:
                raise ValueError(f"Another category named '{validated.category}' already exists.")

            self.conn.execute(
                """
                UPDATE category_budgets
                SET category = ?, emoji = ?, cost_bucket = ?, monthly_limit = ?, is_active = ?
                WHERE id = ?
                """,
                (
                    validated.category,
                    validated.emoji,
                    validated.cost_bucket,
                    round(validated.monthly_limit, 2),
                    int(validated.is_active),
                    cat_id,
                ),
            )
            if old_name != validated.category:
                self.conn.execute(
                    "UPDATE transactions SET category = ? WHERE category = ?",
                    (validated.category, old_name),
                )
                self.conn.execute(
                    "UPDATE category_rules SET category = ? WHERE category = ?",
                    (validated.category, old_name),
                )
                self.conn.execute(
                    "UPDATE recurring_commitments SET category = ? WHERE category = ?",
                    (validated.category, old_name),
                )
            if validated.cost_bucket in ("FIXED", "VARIABLE", "LEAKAGE", "INFLOW", "IGNORE", "INVESTMENT"):
                self.conn.execute(
                    "UPDATE category_rules SET cost_bucket = ? WHERE category = ?",
                    (validated.cost_bucket, validated.category),
                )
                if validated.cost_bucket in ("FIXED", "VARIABLE", "LEAKAGE"):
                    self.conn.execute(
                        "UPDATE transactions SET cost_bucket = ? WHERE category = ? AND tx_type = 'DEBIT'",
                        (validated.cost_bucket, validated.category),
                    )
                    self.conn.execute(
                        "UPDATE recurring_commitments SET cost_bucket = ? WHERE category = ?",
                        (validated.cost_bucket, validated.category),
                    )
            updated_row = self.conn.execute(
                "SELECT * FROM category_budgets WHERE id = ?",
                (cat_id,),
            ).fetchone()
            return self._row_to_category_budget(updated_row)

    def delete_category_budget(self, cat_id: int) -> tuple[bool, int]:
        """Remove a category, reassigning its transactions/bills and cleaning up orphaned rules."""
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM category_budgets WHERE id = ?",
                (cat_id,),
            ).fetchone()
            if not row:
                return False, 0
            old_name = str(row["category"])
            old_bucket = str(row["cost_bucket"])
            if old_name == "Uncategorized":
                raise ValueError("The system fallback category 'Uncategorized' cannot be removed.")

            fallback = "Uncategorized"
            if old_bucket == "INFLOW" and old_name != "Other Inflow":
                has_other = self.conn.execute(
                    "SELECT 1 FROM category_budgets WHERE category = 'Other Inflow'"
                ).fetchone()
                if has_other:
                    fallback = "Other Inflow"
            elif old_bucket == "IGNORE" and old_name != "Internal Transfer":
                has_int = self.conn.execute(
                    "SELECT 1 FROM category_budgets WHERE category = 'Internal Transfer'"
                ).fetchone()
                if has_int:
                    fallback = "Internal Transfer"

            tx_cur = self.conn.execute(
                "UPDATE transactions SET category = ? WHERE category = ?",
                (fallback, old_name),
            )
            self.conn.execute(
                "UPDATE recurring_commitments SET category = ? WHERE category = ?",
                (fallback, old_name),
            )
            self.conn.execute(
                "DELETE FROM category_rules WHERE category = ?",
                (old_name,),
            )
            del_cur = self.conn.execute(
                "DELETE FROM category_budgets WHERE id = ?",
                (cat_id,),
            )
            return del_cur.rowcount > 0, int(tx_cur.rowcount or 0)

    def find_internal_transfer_pairs(self, day_window: int = 2) -> list[dict[str, Any]]:
        """Detect matching cross-wallet transfer pairs (DEBIT+CREDIT or duplicate CREDIT mirror)."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM transactions ORDER BY tx_date DESC, id DESC"
            ).fetchall()
            txs = [self._row_to_tx(r) for r in rows]

        outgoing = [t for t in txs if t.tx_type in ("DEBIT", "TRANSFER_INTERNAL")]
        incoming = [t for t in txs if t.tx_type == "CREDIT"]

        def _is_salary_like(tx: TransactionRecord) -> bool:
            txt = f"{tx.payee} {tx.merchant_alias or ''}".lower()
            return bool(re.search(r"\b(?:salary|payroll|remuneration|dividend)\b", txt))

        def _transfer_cue_score(out_tx: TransactionRecord, in_tx: TransactionRecord) -> int:
            out_txt = f"{out_tx.payee} {out_tx.merchant_alias or ''} {out_tx.payment_channel or ''}".lower()
            in_txt = f"{in_tx.payee} {in_tx.merchant_alias or ''} {in_tx.payment_channel or ''}".lower()
            score = 0
            if out_tx.tx_type == "TRANSFER_INTERNAL" and out_tx.target_account == in_tx.account_name:
                score += 5
            if out_tx.account_name.lower() in in_txt or in_tx.account_name.lower() in out_txt:
                score += 3
            if any(k in in_txt or k in out_txt for k in ("raast", "ibft", "top-up", "topup", "own", "self", "transfer", "wallet")):
                score += 2
            return score

        candidates: list[tuple[int, int, dict[str, Any]]] = []
        for out_tx in outgoing:
            if out_tx.tx_type == "TRANSFER_INTERNAL" and "[paired:" in (out_tx.notes or ""):
                continue
            out_dt = date.fromisoformat(out_tx.tx_date)
            for in_tx in incoming:
                if out_tx.account_name == in_tx.account_name:
                    continue
                if out_tx.tx_type == "TRANSFER_INTERNAL" and out_tx.target_account != in_tx.account_name:
                    continue
                if out_tx.tx_type == "DEBIT" and (
                    out_tx.account_name == WALLET_MMF or in_tx.account_name == WALLET_MMF
                ):
                    if out_tx.account_name != WALLET_MMF:
                        continue
                if abs(out_tx.amount - in_tx.amount) > 0.01:
                    continue
                in_dt = date.fromisoformat(in_tx.tx_date)
                diff_days = abs((out_dt - in_dt).days)
                if diff_days > day_window:
                    continue
                if _is_salary_like(in_tx):
                    continue
                cue = _transfer_cue_score(out_tx, in_tx)
                mode = (
                    "MERGE_DUPLICATE_CREDIT"
                    if out_tx.tx_type == "TRANSFER_INTERNAL"
                    else "PAIR_DEBIT_CREDIT"
                )
                candidates.append(
                    (
                        -cue,
                        diff_days,
                        {
                            "mode": mode,
                            "out_tx": out_tx,
                            "in_tx": in_tx,
                            "confidence": "High" if cue >= 2 else "Possible",
                        },
                    )
                )

        candidates.sort(key=lambda item: (item[0], item[1], -(item[2]["out_tx"].id or 0)))
        used_ids: set[int] = set()
        results: list[dict[str, Any]] = []
        for _, _, item in candidates:
            oid = item["out_tx"].id
            iid = item["in_tx"].id
            if oid is None or iid is None or oid in used_ids or iid in used_ids:
                continue
            used_ids.add(oid)
            used_ids.add(iid)
            results.append(item)
        return results

    def link_internal_transfer_pair(
        self, out_tx_id: int, in_tx_id: int
    ) -> TransactionRecord:
        """Link/deduplicate an outgoing leg and mirror CREDIT into one TRANSFER_INTERNAL record."""
        with self._lock, self.conn:
            out_row = self.conn.execute(
                "SELECT * FROM transactions WHERE id = ?",
                (out_tx_id,),
            ).fetchone()
            in_row = self.conn.execute(
                "SELECT * FROM transactions WHERE id = ?",
                (in_tx_id,),
            ).fetchone()
            if not out_row or not in_row:
                raise ValueError("One or both selected transfer transactions no longer exist.")
            out_tx = self._row_to_tx(out_row)
            in_tx = self._row_to_tx(in_row)
            if out_tx.tx_type not in ("DEBIT", "TRANSFER_INTERNAL"):
                raise ValueError("Outgoing transfer leg must be a DEBIT or TRANSFER_INTERNAL transaction.")
            if in_tx.tx_type != "CREDIT":
                raise ValueError("Incoming mirror leg must be a CREDIT transaction.")
            if out_tx.account_name == in_tx.account_name:
                raise ValueError("Own-account transfer pair must involve two different wallets.")
            if (
                out_tx.tx_type == "TRANSFER_INTERNAL"
                and out_tx.target_account
                and out_tx.target_account != in_tx.account_name
            ):
                raise ValueError("Outgoing TRANSFER_INTERNAL already targets a different wallet.")
            if abs(out_tx.amount - in_tx.amount) > 0.01:
                raise ValueError("Transfer pair amounts do not match.")

            target = in_tx.account_name
            is_mmf = target == WALLET_MMF
            new_type = "TRANSFER_MMF" if is_mmf else "TRANSFER_INTERNAL"
            new_bucket = "INVESTMENT" if is_mmf else "IGNORE"
            new_internal = 0 if is_mmf else 1
            if is_mmf:
                cat = "Money Market Fund"
            elif target == WALLET_NAYAPAY:
                cat = "Wallet Top-Up"
            elif target == WALLET_CASH:
                cat = "Cash Withdrawal"
            elif out_tx.account_name == WALLET_MMF:
                cat = "MMF Redemption"
            else:
                cat = "Internal Transfer"

            pair_tag = f"[paired:{in_tx.message_id} {in_tx.tx_date}]"
            merged_notes = (out_tx.notes or "").strip()
            if pair_tag not in merged_notes:
                merged_notes = f"{merged_notes} {pair_tag}".strip()
            for tag in _STRUCTURED_TAG_RE.findall(in_tx.notes or ""):
                if tag not in merged_notes:
                    merged_notes = f"{merged_notes} {tag}".strip()

            next_ts = out_tx.tx_timestamp
            if (
                out_tx.tx_date == in_tx.tx_date
                and _parse_time_seconds(out_tx.tx_timestamp) is None
                and _parse_time_seconds(in_tx.tx_timestamp) is not None
                and in_tx.tx_timestamp
            ):
                hhmmss = in_tx.tx_timestamp.strip().replace("T", " ")[11:19]
                if len(hhmmss) == 8:
                    next_ts = f"{out_tx.tx_date} {hhmmss}"

            self.conn.execute(
                """
                UPDATE transactions
                SET tx_type = ?,
                    tx_timestamp = ?,
                    target_account = ?,
                    category = ?,
                    cost_bucket = ?,
                    is_internal_transfer = ?,
                    review_status = 'MANUALLY_VERIFIED',
                    notes = ?
                WHERE id = ?
                """,
                (new_type, next_ts, target, cat, new_bucket, new_internal, merged_notes, out_tx_id),
            )
            self.conn.execute(
                "DELETE FROM transactions WHERE id = ?",
                (in_tx_id,),
            )
            self._recalculate_balances_unlocked()
            updated_row = self.conn.execute(
                "SELECT * FROM transactions WHERE id = ?",
                (out_tx_id,),
            ).fetchone()
            return self._row_to_tx(updated_row)

    def auto_link_internal_transfer_mirrors(self, day_window: int = 1) -> int:
        """Auto-merge corroborated TRANSFER_INTERNAL + mirror CREDIT pairs without deleting unrelated inflows."""
        linked_count = 0
        for pair in self.find_internal_transfer_pairs(day_window=day_window):
            if pair.get("mode") != "MERGE_DUPLICATE_CREDIT":
                continue
            out_tx = pair.get("out_tx")
            in_tx = pair.get("in_tx")
            if not out_tx or not in_tx or out_tx.id is None or in_tx.id is None:
                continue
            if "[paired:" in (out_tx.notes or ""):
                continue
            if out_tx.tx_type != "TRANSFER_INTERNAL" or out_tx.target_account != in_tx.account_name:
                continue
            # Require corroborating evidence: either explicit bank/transfer cue OR tight timestamp proximity
            out_txt = f"{out_tx.payee} {out_tx.merchant_alias or ''} {out_tx.payment_channel or ''}".lower()
            in_txt = f"{in_tx.payee} {in_tx.merchant_alias or ''} {in_tx.payment_channel or ''}".lower()
            has_cue = (
                out_tx.account_name.lower() in in_txt
                or in_tx.account_name.lower() in out_txt
                or any(k in in_txt or k in out_txt for k in ("raast", "ibft", "top-up", "topup", "own", "self"))
            )
            ts_close = False
            if out_tx.tx_date == in_tx.tx_date:
                to_s = _parse_time_seconds(out_tx.tx_timestamp)
                ti_s = _parse_time_seconds(in_tx.tx_timestamp)
                if to_s is not None and ti_s is not None and abs(ti_s - to_s) <= 1800:
                    ts_close = True
            if not (has_cue or ts_close):
                continue
            self.link_internal_transfer_pair(int(out_tx.id), int(in_tx.id))
            linked_count += 1
        return linked_count

    def get_category_rules(self) -> list[CategoryRule]:
        """Return categorization rules ordered by priority DESC, keyword length DESC, id DESC."""
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT * FROM category_rules
                ORDER BY priority DESC, length(keyword) DESC, id DESC
                """
            ).fetchall()
            return [self._row_to_category_rule(r) for r in rows]

    def _upsert_category_rule_unlocked(self, rule: CategoryRule) -> CategoryRule:
        validated = CategoryRule.model_validate(rule)
        self.conn.execute(
            """
            INSERT INTO category_rules (
                keyword, merchant_alias, category, cost_bucket, target_account, priority, is_user_defined
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(keyword) DO UPDATE SET
                keyword = excluded.keyword,
                merchant_alias = excluded.merchant_alias,
                category = excluded.category,
                cost_bucket = excluded.cost_bucket,
                target_account = COALESCE(excluded.target_account, category_rules.target_account),
                priority = excluded.priority,
                is_user_defined = excluded.is_user_defined
            """,
            (
                validated.keyword,
                validated.merchant_alias,
                validated.category,
                validated.cost_bucket,
                validated.target_account,
                validated.priority,
                int(validated.is_user_defined),
            ),
        )
        row = self.conn.execute(
            "SELECT * FROM category_rules WHERE keyword = ? COLLATE NOCASE",
            (validated.keyword,),
        ).fetchone()
        return self._row_to_category_rule(row)

    def upsert_category_rule(self, rule: CategoryRule) -> CategoryRule:
        """Insert or update a categorization rule keyed case-insensitively by keyword."""
        with self._lock, self.conn:
            return self._upsert_category_rule_unlocked(rule)

    def delete_category_rule(self, rule_id: int) -> bool:
        """Delete a categorization rule by primary key ID."""
        with self._lock, self.conn:
            cur = self.conn.execute(
                "DELETE FROM category_rules WHERE id = ?",
                (rule_id,),
            )
            return cur.rowcount > 0

    def _apply_learned_rule_to_transactions_unlocked(
        self,
        keyword: str,
        alias: Optional[str],
        category: str,
        cost_bucket: CostBucket,
        overwrite_verified: bool = False,
        target_account: Optional[str] = None,
        source_tx_type: Optional[TxType] = None,
    ) -> int:
        clean_kw = keyword.strip()
        if len(clean_kw) < 3:
            raise ValueError("Learned rule keyword must be at least 3 characters")

        valid_accounts = {
            r["account_name"]
            for r in self.conn.execute("SELECT account_name FROM accounts").fetchall()
        }
        if target_account is not None and target_account not in valid_accounts:
            raise ValueError(f"Unknown wallet account: '{target_account}'")

        updated_count = 0
        balances_dirty = False
        rows = self.conn.execute(
            """
            SELECT id, account_name, target_account, tx_type, is_internal_transfer,
                   merchant_alias, payee, cost_bucket, review_status
            FROM transactions
            """
        ).fetchall()

        for row in rows:
            if not overwrite_verified and row["review_status"] == "MANUALLY_VERIFIED":
                continue
            if not _learned_keyword_matches(
                clean_kw, row["payee"], row["merchant_alias"]
            ):
                continue

            tx_type = row["tx_type"]
            if source_tx_type is not None:
                if source_tx_type == "CREDIT" and tx_type != "CREDIT":
                    continue
                if source_tx_type == "DEBIT" and tx_type != "DEBIT":
                    continue
                if source_tx_type == "TRANSFER_MMF" and tx_type not in (
                    "DEBIT",
                    "TRANSFER_MMF",
                ):
                    continue
                if source_tx_type == "TRANSFER_INTERNAL" and tx_type not in (
                    "DEBIT",
                    "TRANSFER_INTERNAL",
                ):
                    continue

            next_target = row["target_account"]
            is_internal = int(row["is_internal_transfer"])
            effective_bucket: CostBucket = cost_bucket

            if tx_type == "CREDIT":
                effective_bucket = "INFLOW"
            elif tx_type == "TRANSFER_MMF":
                effective_bucket = "INVESTMENT"
            elif tx_type == "TRANSFER_INTERNAL":
                effective_bucket = "IGNORE"
                is_internal = 1
                if target_account and row["account_name"] != target_account and (
                    target_account != WALLET_MMF or row["account_name"] == WALLET_MMF
                ):
                    if next_target != target_account:
                        next_target = target_account
                        balances_dirty = True
            elif tx_type == "DEBIT":
                if cost_bucket == "INVESTMENT" and row["account_name"] != WALLET_MMF:
                    tx_type = "TRANSFER_MMF"
                    next_target = WALLET_MMF
                    is_internal = 0
                    effective_bucket = "INVESTMENT"
                    balances_dirty = True
                elif (
                    cost_bucket == "IGNORE"
                    and target_account
                    and row["account_name"] != target_account
                    and target_account != WALLET_MMF
                ):
                    tx_type = "TRANSFER_INTERNAL"
                    next_target = target_account
                    is_internal = 1
                    effective_bucket = "IGNORE"
                    balances_dirty = True
                elif cost_bucket in ("INFLOW", "IGNORE", "INVESTMENT"):
                    effective_bucket = self._infer_bucket_for_category_unlocked(
                        category, "DEBIT"
                    )

            new_alias = alias if alias else row["merchant_alias"]
            self.conn.execute(
                """
                UPDATE transactions
                SET tx_type = ?,
                    target_account = ?,
                    is_internal_transfer = ?,
                    category = ?,
                    cost_bucket = ?,
                    merchant_alias = ?,
                    review_status = 'MANUALLY_VERIFIED'
                WHERE id = ?
                """,
                (
                    tx_type,
                    next_target,
                    is_internal,
                    category,
                    effective_bucket,
                    new_alias,
                    row["id"],
                ),
            )
            updated_count += 1

        if balances_dirty:
            self._recalculate_balances_unlocked()
        return updated_count

    def apply_learned_rule_to_transactions(
        self,
        keyword: str,
        alias: Optional[str],
        category: str,
        cost_bucket: CostBucket,
        overwrite_verified: bool = False,
        target_account: Optional[str] = None,
        source_tx_type: Optional[TxType] = None,
    ) -> int:
        """Retroactively re-tag matching transactions using boundary-safe matching."""
        clean_kw = keyword.strip()
        if len(clean_kw) < 3:
            raise ValueError("Learned rule keyword must be at least 3 characters")
        with self._lock, self.conn:
            return self._apply_learned_rule_to_transactions_unlocked(
                keyword=clean_kw,
                alias=alias,
                category=category,
                cost_bucket=cost_bucket,
                overwrite_verified=overwrite_verified,
                target_account=target_account,
                source_tx_type=source_tx_type,
            )

    def learn_and_apply_rule(
        self,
        rule: CategoryRule,
        overwrite_verified: bool = False,
        target_account: Optional[str] = None,
        source_tx_type: Optional[TxType] = None,
    ) -> int:
        """Atomically upsert a rule and retroactively apply it in one SQLite transaction."""
        clean_kw = rule.keyword.strip()
        if len(clean_kw) < 3:
            raise ValueError("Learned rule keyword must be at least 3 characters")
        with self._lock, self.conn:
            saved = self._upsert_category_rule_unlocked(rule)
            return self._apply_learned_rule_to_transactions_unlocked(
                keyword=saved.keyword,
                alias=saved.merchant_alias,
                category=saved.category,
                cost_bucket=saved.cost_bucket,
                overwrite_verified=overwrite_verified,
                target_account=target_account,
                source_tx_type=source_tx_type,
            )

    def get_recurring_commitments(
        self, active_only: bool = True
    ) -> list[RecurringCommitment]:
        """Return recurring bill and subscription commitments."""
        sql = "SELECT * FROM recurring_commitments"
        if active_only:
            sql += " WHERE is_active = 1"
        sql += " ORDER BY due_day ASC, id ASC"
        with self._lock:
            rows = self.conn.execute(sql).fetchall()
            return [self._row_to_recurring(r) for r in rows]

    def upsert_recurring_commitment(
        self, commitment: RecurringCommitment
    ) -> RecurringCommitment:
        """Insert or update a recurring commitment."""
        validated = RecurringCommitment.model_validate(commitment)
        with self._lock, self.conn:
            try:
                if validated.id is not None:
                    cur = self.conn.execute(
                        """
                        UPDATE recurring_commitments SET
                            title = ?, match_keyword = ?, expected_amount = ?, due_day = ?,
                            category = ?, cost_bucket = ?, is_paid_this_month = ?,
                            last_paid_month = ?, is_active = ?
                        WHERE id = ?
                        """,
                        (
                            validated.title,
                            validated.match_keyword,
                            round(validated.expected_amount, 2),
                            validated.due_day,
                            validated.category,
                            validated.cost_bucket,
                            int(validated.is_paid_this_month),
                            validated.last_paid_month,
                            int(validated.is_active),
                            validated.id,
                        ),
                    )
                    if cur.rowcount > 0:
                        row = self.conn.execute(
                            "SELECT * FROM recurring_commitments WHERE id = ?",
                            (validated.id,),
                        ).fetchone()
                        return self._row_to_recurring(row)

                self.conn.execute(
                    """
                    INSERT INTO recurring_commitments (
                        title, match_keyword, expected_amount, due_day,
                        category, cost_bucket, is_paid_this_month, last_paid_month, is_active
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(title) DO UPDATE SET
                        match_keyword = excluded.match_keyword,
                        expected_amount = excluded.expected_amount,
                        due_day = excluded.due_day,
                        category = excluded.category,
                        cost_bucket = excluded.cost_bucket,
                        is_paid_this_month = excluded.is_paid_this_month,
                        last_paid_month = excluded.last_paid_month,
                        is_active = excluded.is_active
                    """,
                    (
                        validated.title,
                        validated.match_keyword,
                        round(validated.expected_amount, 2),
                        validated.due_day,
                        validated.category,
                        validated.cost_bucket,
                        int(validated.is_paid_this_month),
                        validated.last_paid_month,
                        int(validated.is_active),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    f"Another recurring bill named '{validated.title}' already exists."
                ) from exc
            row = self.conn.execute(
                "SELECT * FROM recurring_commitments WHERE title = ?",
                (validated.title,),
            ).fetchone()
            return self._row_to_recurring(row)

    def delete_recurring_commitment(self, commitment_id: int) -> bool:
        """Delete a recurring commitment by ID."""
        with self._lock, self.conn:
            cur = self.conn.execute(
                "DELETE FROM recurring_commitments WHERE id = ?",
                (commitment_id,),
            )
            return cur.rowcount > 0

    def mark_recurring_paid(
        self, commitment_id: int, is_paid: bool, month_key: Optional[str] = None
    ) -> None:
        """Idempotently sync recurring payment flags (no-op writes do not advance total_changes)."""
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT is_paid_this_month, last_paid_month FROM recurring_commitments WHERE id = ?",
                (commitment_id,),
            ).fetchone()
            if not row:
                return
            cur_paid = int(row["is_paid_this_month"])
            cur_last = row["last_paid_month"]
            if is_paid:
                next_last = cur_last
                if month_key and (cur_last is None or month_key > cur_last):
                    next_last = month_key
                if cur_paid == 1 and cur_last == next_last:
                    return
                self.conn.execute(
                    "UPDATE recurring_commitments SET is_paid_this_month = 1, last_paid_month = ? WHERE id = ?",
                    (next_last, commitment_id),
                )
            else:
                next_last = None if (month_key and cur_last == month_key) else cur_last
                if cur_paid == 0 and cur_last == next_last:
                    return
                self.conn.execute(
                    "UPDATE recurring_commitments SET is_paid_this_month = 0, last_paid_month = ? WHERE id = ?",
                    (next_last, commitment_id),
                )

    # ------------------------------------------------------------------
    # Backups, Restore & Exports
    # ------------------------------------------------------------------

    def _export_backup_json_unlocked(self) -> str:
        tx_rows = self.conn.execute(
            """
            SELECT * FROM transactions
            ORDER BY tx_date DESC, COALESCE(tx_timestamp, tx_date || ' 00:00:00') DESC, id DESC
            """
        ).fetchall()
        cb_rows = self.conn.execute(
            "SELECT * FROM category_budgets ORDER BY id ASC"
        ).fetchall()
        rule_rows = self.conn.execute(
            "SELECT * FROM category_rules ORDER BY priority DESC, length(keyword) DESC, id DESC"
        ).fetchall()
        rec_rows = self.conn.execute(
            "SELECT * FROM recurring_commitments ORDER BY due_day ASC, id ASC"
        ).fetchall()

        payload: dict[str, Any] = {
            "schema_version": _BACKUP_SCHEMA_VERSION,
            "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "accounts": [w.model_dump() for w in self._get_wallets_unlocked()],
            "budget_config": self._get_budget_config_unlocked().model_dump(),
            "category_budgets": [
                self._row_to_category_budget(r).model_dump() for r in cb_rows
            ],
            "transactions": [self._row_to_tx(r).model_dump() for r in tx_rows],
            "category_rules": [
                self._row_to_category_rule(r).model_dump() for r in rule_rows
            ],
            "recurring_commitments": [
                self._row_to_recurring(r).model_dump() for r in rec_rows
            ],
        }
        payload["checksum"] = _compute_backup_checksum(payload)
        return json.dumps(payload, indent=2, ensure_ascii=False)

    def _export_transactions_csv_unlocked(self) -> str:
        tx_rows = self.conn.execute(
            """
            SELECT * FROM transactions
            ORDER BY tx_date DESC, COALESCE(tx_timestamp, tx_date || ' 00:00:00') DESC, id DESC
            """
        ).fetchall()
        txs = [self._row_to_tx(r) for r in tx_rows]
        fieldnames = list(TransactionRecord.model_fields.keys())
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames)
        writer.writeheader()
        for tx in txs:
            safe_row = {
                key: _sanitize_csv_cell(val)
                for key, val in tx.model_dump().items()
            }
            writer.writerow(safe_row)
        return buf.getvalue()

    def export_backup_json(self) -> str:
        """Serialize all 6 database tables with schema_version and SHA-256 checksum."""
        with self._lock, self.conn:
            return self._export_backup_json_unlocked()

    def export_transactions_csv(self) -> str:
        """Export all persisted transactions to a UTF-8 CSV string."""
        with self._lock:
            return self._export_transactions_csv_unlocked()

    def save_backup_to_disk(
        self, backup_dir: str | Path = "data/backups"
    ) -> tuple[Path, Path]:
        """Write collision-safe, atomic JSON and CSV backups from a single snapshot."""
        target_dir = Path(backup_dir)
        target_dir.mkdir(parents=True, exist_ok=True)

        with self._lock, self.conn:
            json_str = self._export_backup_json_unlocked()
            csv_str = self._export_transactions_csv_unlocked()

            base_stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            suffix_idx = 0
            while True:
                stamp = (
                    base_stamp
                    if suffix_idx == 0
                    else f"{base_stamp}_{suffix_idx:02d}"
                )
                json_path = target_dir / f"treasury_backup_{stamp}.json"
                csv_path = target_dir / f"transactions_export_{stamp}.csv"
                if not json_path.exists() and not csv_path.exists():
                    break
                suffix_idx += 1

            _atomic_write_bytes(json_path, json_str.encode("utf-8"))
            _atomic_write_bytes(csv_path, csv_str.encode("utf-8-sig"))
        return json_path, csv_path

    def restore_backup_json(self, json_str: str) -> dict[str, int]:
        """Validate schema_version + SHA-256 checksum and atomically restore all 6 tables."""
        try:
            payload = json.loads(json_str)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid backup JSON: {exc}") from exc

        if not isinstance(payload, dict):
            raise ValueError("Backup JSON root must be an object")

        schema_ver = payload.get("schema_version")
        if schema_ver != _BACKUP_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported backup schema_version: {schema_ver!r}"
            )

        checksum = payload.get("checksum")
        if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise ValueError("Backup JSON missing valid SHA-256 checksum")

        expected_checksum = _compute_backup_checksum(
            {k: v for k, v in payload.items() if k != "checksum"}
        )
        if checksum != expected_checksum:
            raise ValueError("Backup checksum verification failed")

        required_keys = (
            "accounts",
            "budget_config",
            "category_budgets",
            "transactions",
            "category_rules",
            "recurring_commitments",
        )
        for key in required_keys:
            if key not in payload:
                raise ValueError(f"Backup JSON missing required key: '{key}'")

        raw_accounts = payload["accounts"]
        if not isinstance(raw_accounts, list) or not raw_accounts:
            raise ValueError("Backup accounts must be a non-empty list")

        raw_cfg = payload["budget_config"]
        if not isinstance(raw_cfg, dict):
            raise ValueError("Backup budget_config must be a JSON object")

        list_table_keys = (
            "accounts",
            "category_budgets",
            "transactions",
            "category_rules",
            "recurring_commitments",
        )
        for tbl_key in list_table_keys:
            rows_val = payload[tbl_key]
            if not isinstance(rows_val, list):
                raise ValueError(f"Backup '{tbl_key}' must be a JSON array")
            for idx, item in enumerate(rows_val):
                if not isinstance(item, dict):
                    raise ValueError(
                        f"Backup '{tbl_key}[{idx}]' must be a JSON object"
                    )

        wallets = [AccountWallet.model_validate(item) for item in raw_accounts]
        acct_names = {w.account_name for w in wallets}
        if len(acct_names) != len(wallets):
            raise ValueError("Backup contains duplicate account_name entries")

        cfg_raw = dict(raw_cfg)
        cfg_raw["id"] = 1
        cfg = BudgetConfig.model_validate(cfg_raw)

        budgets = [
            CategoryBudget.model_validate(item)
            for item in payload["category_budgets"]
        ]
        txs = [
            TransactionRecord.model_validate(item) for item in payload["transactions"]
        ]
        for tx in txs:
            if tx.account_name not in acct_names:
                raise ValueError(
                    f"Backup transaction references unknown account '{tx.account_name}'"
                )
            if tx.tx_type == "TRANSFER_INTERNAL" and (
                not tx.target_account or tx.target_account not in acct_names
            ):
                raise ValueError(
                    f"Backup transfer references unknown target_account '{tx.target_account}'"
                )
            if tx.tx_type == "TRANSFER_MMF" and (
                (tx.target_account or WALLET_MMF) not in acct_names
            ):
                raise ValueError(
                    f"Backup MMF sweep references unknown target_account '{tx.target_account or WALLET_MMF}'"
                )

        rules = [
            CategoryRule.model_validate(item) for item in payload["category_rules"]
        ]
        commitments = [
            RecurringCommitment.model_validate(item)
            for item in payload["recurring_commitments"]
        ]

        for tbl_name, records in (
            ("accounts", wallets),
            ("category_budgets", budgets),
            ("transactions", txs),
            ("category_rules", rules),
            ("recurring_commitments", commitments),
        ):
            seen_ids: set[int] = set()
            for rec in records:
                if rec.id is not None:
                    if rec.id in seen_ids:
                        raise ValueError(
                            f"Backup '{tbl_name}' contains duplicate id={rec.id}"
                        )
                    seen_ids.add(rec.id)

        if len({b.category for b in budgets}) != len(budgets):
            raise ValueError("Backup 'category_budgets' contains duplicate category entries")
        if len({t.message_id for t in txs}) != len(txs):
            raise ValueError("Backup 'transactions' contains duplicate message_id entries")
        if len({r.keyword.lower() for r in rules}) != len(rules):
            raise ValueError("Backup 'category_rules' contains duplicate keyword entries")
        if len({c.title for c in commitments}) != len(commitments):
            raise ValueError("Backup 'recurring_commitments' contains duplicate title entries")

        with self._lock, self.conn:
            for tbl in (
                "transactions",
                "category_rules",
                "recurring_commitments",
                "category_budgets",
                "budget_config",
                "accounts",
            ):
                self.conn.execute(f"DELETE FROM {tbl}")

            for w in wallets:
                self.conn.execute(
                    """
                    INSERT INTO accounts (
                        id, account_name, account_type, opening_balance, current_balance,
                        balance_as_of, currency, icon, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))
                    """,
                    (
                        w.id,
                        w.account_name,
                        w.account_type,
                        round(w.opening_balance, 2),
                        round(w.current_balance, 2),
                        w.balance_as_of,
                        w.currency,
                        w.icon,
                        w.updated_at,
                    ),
                )

            self.conn.execute(
                """
                INSERT INTO budget_config (
                    id, monthly_income, fixed_cost_baseline, variable_spending_cap,
                    emergency_multiplier, envelope_mode, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))
                """,
                (
                    1,
                    round(cfg.monthly_income, 2),
                    round(cfg.fixed_cost_baseline, 2),
                    round(cfg.variable_spending_cap, 2),
                    cfg.emergency_multiplier,
                    int(cfg.envelope_mode),
                    cfg.updated_at,
                ),
            )

            for cb in budgets:
                self.conn.execute(
                    """
                    INSERT INTO category_budgets (
                        id, category, emoji, cost_bucket, monthly_limit, is_active
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cb.id,
                        cb.category,
                        cb.emoji,
                        cb.cost_bucket,
                        round(cb.monthly_limit, 2),
                        int(cb.is_active),
                    ),
                )

            for r in rules:
                self.conn.execute(
                    """
                    INSERT INTO category_rules (
                        id, keyword, merchant_alias, category, cost_bucket,
                        target_account, priority, is_user_defined, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))
                    """,
                    (
                        r.id,
                        r.keyword,
                        r.merchant_alias,
                        r.category,
                        r.cost_bucket,
                        r.target_account,
                        r.priority,
                        int(r.is_user_defined),
                        r.created_at,
                    ),
                )

            for c in commitments:
                self.conn.execute(
                    """
                    INSERT INTO recurring_commitments (
                        id, title, match_keyword, expected_amount, due_day,
                        category, cost_bucket, is_paid_this_month, last_paid_month, is_active
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        c.id,
                        c.title,
                        c.match_keyword,
                        round(c.expected_amount, 2),
                        c.due_day,
                        c.category,
                        c.cost_bucket,
                        int(c.is_paid_this_month),
                        c.last_paid_month,
                        int(c.is_active),
                    ),
                )

            for tx in txs:
                self.conn.execute(
                    """
                    INSERT INTO transactions (
                        id, message_id, tx_date, tx_timestamp, account_name, target_account,
                        tx_type, amount, tax_fee_amount, payee, merchant_alias,
                        category, cost_bucket, review_status, is_internal_transfer,
                        source, payment_channel, raw_subject, notes, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))
                    """,
                    (
                        tx.id,
                        tx.message_id,
                        tx.tx_date,
                        tx.tx_timestamp,
                        tx.account_name,
                        tx.target_account,
                        tx.tx_type,
                        round(tx.amount, 2),
                        round(tx.tax_fee_amount, 2),
                        tx.payee,
                        tx.merchant_alias,
                        tx.category,
                        tx.cost_bucket,
                        tx.review_status,
                        int(tx.is_internal_transfer),
                        tx.source,
                        tx.payment_channel,
                        tx.raw_subject,
                        tx.notes,
                        tx.created_at,
                    ),
                )

            self._recalculate_balances_unlocked()

        return {
            "schema_version": _BACKUP_SCHEMA_VERSION,
            "accounts": len(wallets),
            "category_budgets": len(budgets),
            "transactions": len(txs),
            "category_rules": len(rules),
            "recurring_commitments": len(commitments),
        }

    def export_encrypted_backup(self, passphrase: Optional[str] = None) -> str:
        """Export full JSON backup encrypted with PBKDF2-HMAC-SHA256 + Fernet."""
        key = (passphrase or "").strip() or get_backup_passphrase()
        if not key:
            raise ValueError(
                "Missing encryption passphrase (set TREASURY_BACKUP_KEY or provide a passphrase)."
            )
        plaintext = self.export_backup_json()
        return encrypt_backup_payload(plaintext, key)

    def restore_encrypted_backup(
        self, envelope_text: str, passphrase: Optional[str] = None
    ) -> dict[str, int]:
        """Decrypt and atomically restore a PBKDF2 + Fernet encrypted backup envelope."""
        key = (passphrase or "").strip() or get_backup_passphrase()
        if not key:
            raise ValueError(
                "Missing decryption passphrase (set TREASURY_BACKUP_KEY or provide a passphrase)."
            )
        plaintext = decrypt_backup_payload(envelope_text, key)
        return self.restore_backup_json(plaintext)

    def sync_to_cloud_vault(self, passphrase: Optional[str] = None) -> tuple[bool, str]:
        """Encrypt the current database snapshot and upload it to the configured cloud vault."""
        status = get_cloud_sync_status()
        if not status.get("backend_ready"):
            return False, str(status.get("detail") or "Cloud vault is not configured.")
        try:
            envelope = self.export_encrypted_backup(passphrase=passphrase)
            return push_encrypted_cloud_backup(envelope)
        except Exception as exc:
            return False, redact_sensitive_text(f"Cloud vault sync failed: {exc}")

    def restore_from_cloud_vault(
        self, passphrase: Optional[str] = None
    ) -> tuple[bool, str]:
        """Download and atomically restore the latest encrypted snapshot from cloud vault."""
        envelope, pull_msg = pull_encrypted_cloud_backup()
        if envelope is None:
            return False, pull_msg
        try:
            summary = self.restore_encrypted_backup(envelope, passphrase=passphrase)
            return True, (
                f"Restored {summary['transactions']} transaction(s) from encrypted cloud vault."
            )
        except Exception as exc:
            return False, redact_sensitive_text(f"Cloud vault restore failed: {exc}")

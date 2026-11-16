"""Database schema, double-entry wallet ledger, deduplication, and backup tests."""

from __future__ import annotations

import csv
import io
import json
import threading
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.database.db_manager import DatabaseManager, _compute_backup_checksum
from src.engine.categorizer import apply_rules
from src.models import (
    WALLET_CASH,
    WALLET_HABIBMETRO,
    WALLET_MMF,
    WALLET_NAYAPAY,
    AccountWallet,
    BudgetConfig,
    CategoryBudget,
    CategoryRule,
    ParsedTransaction,
    RecurringCommitment,
    TransactionRecord,
)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    """Provide an isolated temporary SQLite DatabaseManager instance."""
    manager = DatabaseManager(tmp_path / "test_treasury.db")
    try:
        yield manager
    finally:
        manager.close()


def test_schema_tables_and_initial_seeding(db: DatabaseManager) -> None:
    """Verify all 6 tables, 4 default wallets, category budgets, 35+ rules, and idempotency."""
    rows = db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    table_names = {row["name"] for row in rows}
    assert table_names == {
        "accounts",
        "budget_config",
        "category_budgets",
        "transactions",
        "category_rules",
        "recurring_commitments",
    }

    wallets = db.get_wallets()
    assert [(w.account_name, w.account_type) for w in wallets] == [
        (WALLET_HABIBMETRO, "BANK"),
        (WALLET_NAYAPAY, "WALLET"),
        (WALLET_CASH, "CASH"),
        (WALLET_MMF, "INVESTMENT"),
    ]
    assert all(w.opening_balance == 0.0 and w.current_balance == 0.0 for w in wallets)

    rules = db.get_category_rules()
    assert len(rules) >= 35
    assert any(r.keyword == "K-ELECTRIC" for r in rules)
    assert any(r.keyword == "FOODPANDA" for r in rules)
    assert any(r.keyword == "AL MEEZAN" for r in rules)

    cfg = db.get_budget_config()
    assert cfg.id == 1
    assert cfg.monthly_income == 200000.0
    assert cfg.fixed_cost_baseline == 80000.0
    assert cfg.variable_spending_cap == 40000.0
    assert cfg.emergency_multiplier == 6
    assert cfg.envelope_mode is False

    budgets = db.get_category_budgets()
    assert len(budgets) >= 10
    commitments = db.get_recurring_commitments()
    assert len(commitments) >= 5

    # Re-running init_db must be completely idempotent
    db.init_db()
    assert len(db.get_wallets()) == 4
    assert len(db.get_category_rules()) == len(rules)
    assert len(db.get_category_budgets()) == len(budgets)


def test_atomic_double_entry_wallet_balances(db: DatabaseManager) -> None:
    """Verify CREDIT, DEBIT, TRANSFER_INTERNAL, and TRANSFER_MMF balance updates."""
    db.update_wallet_opening_balance(WALLET_HABIBMETRO, 100000.0)
    db.update_wallet_opening_balance(WALLET_NAYAPAY, 5000.0)
    db.update_wallet_opening_balance(WALLET_CASH, 2000.0)
    db.update_wallet_opening_balance(WALLET_MMF, 50000.0)

    txs = [
        # 1. Salary CREDIT to HabibMetro (+200,000 -> 300,000)
        TransactionRecord(
            message_id="DB_CREDIT_1",
            tx_date="2026-11-01",
            account_name=WALLET_HABIBMETRO,
            tx_type="CREDIT",
            amount=200000.0,
            payee="ACME PAYROLL SALARY",
            category="Salary & Income",
            cost_bucket="INFLOW",
            review_status="AUTO_VERIFIED",
            source="GMAIL_HMB",
        ),
        # 2. Utility DEBIT on HabibMetro with bank fee (-(14,500 + 50) -> 285,450)
        TransactionRecord(
            message_id="DB_DEBIT_1",
            tx_date="2026-11-05",
            account_name=WALLET_HABIBMETRO,
            tx_type="DEBIT",
            amount=14500.0,
            tax_fee_amount=50.0,
            payee="K-ELECTRIC",
            category="Bills & Utilities",
            cost_bucket="FIXED",
            review_status="AUTO_VERIFIED",
            source="GMAIL_HMB",
        ),
        # 3. TRANSFER_INTERNAL HabibMetro -> NayaPay (-20,000 HMB -> 265,450; +20,000 NayaPay -> 25,000)
        TransactionRecord(
            message_id="DB_XFER_NP",
            tx_date="2026-11-06",
            account_name=WALLET_HABIBMETRO,
            target_account=WALLET_NAYAPAY,
            tx_type="TRANSFER_INTERNAL",
            amount=20000.0,
            payee="NAYAPAY - DEMO ACCOUNT",
            category="Wallet Top-Up",
            cost_bucket="IGNORE",
            review_status="AUTO_VERIFIED",
            source="GMAIL_HMB",
        ),
        # 4. TRANSFER_INTERNAL ATM HabibMetro -> Cash in Hand (-10,000 HMB -> 255,450; +10,000 Cash -> 12,000)
        TransactionRecord(
            message_id="DB_XFER_CASH",
            tx_date="2026-11-06",
            account_name=WALLET_HABIBMETRO,
            target_account=WALLET_CASH,
            tx_type="TRANSFER_INTERNAL",
            amount=10000.0,
            payee="ATM CASH WITHDRAWAL",
            category="Cash Withdrawal",
            cost_bucket="IGNORE",
            review_status="AUTO_VERIFIED",
            source="GMAIL_HMB",
        ),
        # 5. TRANSFER_MMF HabibMetro -> Money Market Fund (-55,000 HMB -> 200,450; +55,000 MMF -> 105,000)
        TransactionRecord(
            message_id="DB_XFER_MMF",
            tx_date="2026-11-07",
            account_name=WALLET_HABIBMETRO,
            target_account=WALLET_MMF,
            tx_type="TRANSFER_MMF",
            amount=55000.0,
            payee="AL MEEZAN INVESTMENT - MRAF",
            category="Money Market Fund",
            cost_bucket="INVESTMENT",
            review_status="AUTO_VERIFIED",
            source="GMAIL_HMB",
        ),
        # 6. NayaPay DEBIT with WHT + FX fee (-(1,100 + 165) -> 23,735)
        TransactionRecord(
            message_id="DB_DEBIT_NP",
            tx_date="2026-11-10",
            account_name=WALLET_NAYAPAY,
            tx_type="DEBIT",
            amount=1100.0,
            tax_fee_amount=165.0,
            payee="NETFLIX.COM",
            category="Subscriptions & Tech",
            cost_bucket="VARIABLE",
            review_status="AUTO_VERIFIED",
            source="GMAIL_NAYAPAY",
        ),
    ]
    inserted, skipped = db.upsert_many(txs)
    assert inserted == 6
    assert skipped == 0

    balances = {w.account_name: w.current_balance for w in db.get_wallets()}
    assert balances[WALLET_HABIBMETRO] == pytest.approx(200450.0)
    assert balances[WALLET_NAYAPAY] == pytest.approx(23735.0)
    assert balances[WALLET_CASH] == pytest.approx(12000.0)
    assert balances[WALLET_MMF] == pytest.approx(105000.0)


def test_opening_balance_update_and_recalculate_idempotency(db: DatabaseManager) -> None:
    """Updating opening_balance and repeating recalculate_balances() remains exact."""
    db.upsert_transaction(
        TransactionRecord(
            message_id="RECALC_1",
            tx_date="2026-11-03",
            account_name=WALLET_NAYAPAY,
            tx_type="DEBIT",
            amount=1850.0,
            payee="FOODPANDA PAKISTAN",
            category="Food & Dining",
            cost_bucket="VARIABLE",
            review_status="AUTO_VERIFIED",
            source="GMAIL_NAYAPAY",
        )
    )
    assert db.get_wallet(WALLET_NAYAPAY).current_balance == pytest.approx(-1850.0)

    updated_wallet = db.update_wallet_opening_balance(WALLET_NAYAPAY, 15000.0)
    assert updated_wallet.opening_balance == pytest.approx(15000.0)
    assert updated_wallet.current_balance == pytest.approx(13150.0)

    # Idempotent recalculation
    db.recalculate_balances()
    db.recalculate_balances()
    assert db.get_wallet(WALLET_NAYAPAY).current_balance == pytest.approx(13150.0)

    with pytest.raises(ValueError, match="Unknown wallet account"):
        db.update_wallet_opening_balance("NonExistentWallet", 500.0)


def test_message_id_deduplication(db: DatabaseManager) -> None:
    """Duplicate message_ids must be skipped without double-affecting wallet balances."""
    tx = TransactionRecord(
        message_id="DUP_MSG_001",
        tx_date="2026-11-08",
        account_name=WALLET_HABIBMETRO,
        tx_type="DEBIT",
        amount=4500.0,
        payee="STORMFIBER",
        category="Bills & Utilities",
        cost_bucket="FIXED",
        review_status="AUTO_VERIFIED",
        source="GMAIL_HMB",
    )
    assert db.upsert_transaction(tx) is True
    assert db.upsert_transaction(tx) is False

    dup_batch_tx = TransactionRecord(
        message_id="DUP_MSG_002",
        tx_date="2026-11-09",
        account_name=WALLET_HABIBMETRO,
        tx_type="DEBIT",
        amount=1500.0,
        payee="PSO",
        category="Fuel & Commute",
        cost_bucket="FIXED",
        review_status="AUTO_VERIFIED",
        source="GMAIL_HMB",
    )
    inserted, skipped = db.upsert_many([tx, dup_batch_tx, dup_batch_tx])
    assert inserted == 1
    assert skipped == 2
    assert len(db.get_transactions()) == 2
    assert db.get_wallet(WALLET_HABIBMETRO).current_balance == pytest.approx(-6000.0)


def test_update_delete_manual_and_query_filters(db: DatabaseManager) -> None:
    """Verify transaction update, delete balance restoration, manual creation, and query filters."""
    db.update_wallet_opening_balance(WALLET_CASH, 10000.0)
    db.update_wallet_opening_balance(WALLET_NAYAPAY, 15000.0)

    cash_tx = db.add_manual_transaction(
        tx_date="2026-11-04",
        account_name=WALLET_CASH,
        tx_type="DEBIT",
        amount=1200.0,
        payee="Chai & Paratha",
        category="Food & Dining",
        notes="Evening snacks",
    )
    assert cash_tx.id is not None
    assert cash_tx.cost_bucket == "VARIABLE"
    assert cash_tx.review_status == "MANUALLY_VERIFIED"
    assert db.get_wallet(WALLET_CASH).current_balance == pytest.approx(8800.0)

    np_tx = db.add_manual_transaction(
        tx_date="2026-10-25",
        account_name=WALLET_NAYAPAY,
        tx_type="DEBIT",
        amount=2500.0,
        payee="Unknown Corner Mart",
    )
    assert np_tx.review_status == "NEEDS_REVIEW"
    assert np_tx.category == "Uncategorized"
    assert db.get_wallet(WALLET_NAYAPAY).current_balance == pytest.approx(12500.0)

    # Update amount & category -> balance updates atomically
    updated = db.update_transaction(
        cash_tx.id,
        amount=1500.0,
        merchant_alias="Local Dhaba",
        review_status="MANUALLY_VERIFIED",
    )
    assert updated is not None
    assert updated.amount == pytest.approx(1500.0)
    assert db.get_wallet(WALLET_CASH).current_balance == pytest.approx(8500.0)

    # Filters
    assert len(db.get_transactions(month_key="2026-11")) == 1
    assert len(db.get_transactions(month_key="2026-10")) == 1
    assert len(db.get_transactions(account_name=WALLET_NAYAPAY)) == 1
    assert len(db.get_transactions(category="Food & Dining")) == 1
    assert len(db.get_transactions(review_status="NEEDS_REVIEW")) == 1
    assert len(db.get_transactions(search_query="dhaba")) == 1
    assert len(db.get_transactions(start_date="2026-11-01", end_date="2026-11-30")) == 1

    # Delete restores wallet balance
    assert db.delete_transaction(cash_tx.id) is True
    assert db.get_transaction_by_id(cash_tx.id) is None
    assert db.get_wallet(WALLET_CASH).current_balance == pytest.approx(10000.0)
    assert db.delete_transaction(cash_tx.id) is False


def test_json_and_csv_backup_exports(db: DatabaseManager, tmp_path: Path) -> None:
    """Verify JSON and CSV exports include all tables and write cleanly to disk."""
    db.add_manual_transaction(
        tx_date="2026-11-02",
        account_name=WALLET_HABIBMETRO,
        tx_type="DEBIT",
        amount=4500.0,
        payee="STORMFIBER",
    )

    json_str = db.export_backup_json()
    payload = json.loads(json_str)
    for key in (
        "exported_at",
        "accounts",
        "budget_config",
        "category_budgets",
        "transactions",
        "category_rules",
        "recurring_commitments",
    ):
        assert key in payload
    assert len(payload["accounts"]) == 4
    assert len(payload["transactions"]) == 1
    assert payload["transactions"][0]["payee"] == "STORMFIBER"

    csv_str = db.export_transactions_csv()
    rows = list(csv.DictReader(io.StringIO(csv_str)))
    assert len(rows) == 1
    assert rows[0]["payee"] == "STORMFIBER"
    assert float(rows[0]["amount"]) == pytest.approx(4500.0)

    json_path, csv_path = db.save_backup_to_disk(tmp_path / "backups")
    assert json_path.is_file()
    assert csv_path.is_file()
    assert json.loads(json_path.read_text(encoding="utf-8"))["transactions"][0]["payee"] == "STORMFIBER"
    assert "STORMFIBER" in csv_path.read_text(encoding="utf-8")


def test_models_validation_rejects_inf_and_invalid_transfers() -> None:
    """Reject non-finite floats, missing/self TRANSFER_INTERNAL targets, and MMF->MMF sweeps."""
    for bad_num in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(ValidationError):
            ParsedTransaction(
                message_id="INF_P1",
                tx_date="2026-11-10",
                account_name=WALLET_NAYAPAY,
                tx_type="DEBIT",
                amount=bad_num,
                payee="FOODPANDA",
                source="MANUAL",
            )
        with pytest.raises(ValidationError):
            TransactionRecord(
                message_id="INF_T1",
                tx_date="2026-11-10",
                account_name=WALLET_NAYAPAY,
                tx_type="DEBIT",
                amount=100.0,
                tax_fee_amount=bad_num,
                payee="FOODPANDA",
            )
        with pytest.raises(ValidationError):
            AccountWallet(
                account_name=WALLET_CASH,
                account_type="CASH",
                opening_balance=bad_num,
            )
        with pytest.raises(ValidationError):
            BudgetConfig(monthly_income=bad_num)
        with pytest.raises(ValidationError):
            CategoryBudget(category="Food & Dining", monthly_limit=bad_num)

    # Missing target_account on TRANSFER_INTERNAL
    with pytest.raises(ValidationError, match="target_account"):
        TransactionRecord(
            message_id="XFER_NO_TARGET",
            tx_date="2026-11-10",
            account_name=WALLET_HABIBMETRO,
            target_account=None,
            tx_type="TRANSFER_INTERNAL",
            amount=5000.0,
            payee="NAYAPAY",
        )

    # Self-transfer on TRANSFER_INTERNAL
    with pytest.raises(ValidationError, match="differ from account_name"):
        TransactionRecord(
            message_id="XFER_SELF",
            tx_date="2026-11-10",
            account_name=WALLET_HABIBMETRO,
            target_account=WALLET_HABIBMETRO,
            tx_type="TRANSFER_INTERNAL",
            amount=5000.0,
            payee="SELF",
        )

    # TRANSFER_MMF from Money Market Fund itself
    with pytest.raises(ValidationError, match="Money Market Fund"):
        TransactionRecord(
            message_id="MMF_SELF",
            tx_date="2026-11-10",
            account_name=WALLET_MMF,
            target_account=WALLET_MMF,
            tx_type="TRANSFER_MMF",
            amount=5000.0,
            payee="AL MEEZAN",
        )


def test_unknown_wallet_validation_and_budget_config_field_rejection(
    db: DatabaseManager,
) -> None:
    """Reject unknown wallet names across mutations and unknown fields in update_budget_config."""
    with pytest.raises(ValueError, match="Unknown wallet account"):
        db.upsert_transaction(
            TransactionRecord(
                message_id="UNK_W1",
                tx_date="2026-11-10",
                account_name="SadaPay",
                tx_type="DEBIT",
                amount=500.0,
                payee="FOODPANDA",
            )
        )

    with pytest.raises(ValueError, match="Unknown wallet account"):
        db.add_manual_transaction(
            tx_date="2026-11-10",
            account_name="SadaPay",
            tx_type="DEBIT",
            amount=500.0,
            payee="FOODPANDA",
        )

    with pytest.raises(ValueError, match="target_account"):
        db.upsert_many(
            [
                TransactionRecord(
                    message_id="UNK_T1",
                    tx_date="2026-11-10",
                    account_name=WALLET_HABIBMETRO,
                    target_account="SadaPay",
                    tx_type="TRANSFER_INTERNAL",
                    amount=500.0,
                    payee="SADAPAY TOPUP",
                )
            ]
        )

    valid_tx = db.add_manual_transaction(
        tx_date="2026-11-10",
        account_name=WALLET_CASH,
        tx_type="DEBIT",
        amount=300.0,
        payee="Chai",
    )
    assert valid_tx.id is not None
    with pytest.raises(ValueError, match="Unknown wallet account"):
        db.update_transaction(valid_tx.id, account_name="UnknownWallet")

    with pytest.raises(ValueError, match="Unsupported budget_config fields"):
        db.update_budget_config(unknown_field=12345.0)

    with pytest.raises(ValueError, match="BudgetConfig id must be 1"):
        db.update_budget_config(id=2)

    with pytest.raises(ValueError, match="finite number"):
        db.update_budget_config(monthly_income=float("inf"))


def test_update_transaction_type_transitions_and_timestamp_preservation(
    db: DatabaseManager,
) -> None:
    """Verify tx_type transitions update bucket/target/balances cleanly and preserve HH:MM:SS."""
    db.update_wallet_opening_balance(WALLET_HABIBMETRO, 100000.0)
    db.update_wallet_opening_balance(WALLET_NAYAPAY, 5000.0)
    db.update_wallet_opening_balance(WALLET_MMF, 0.0)

    # 1. Start with TRANSFER_INTERNAL (HMB -> NayaPay, 20,000) with explicit timestamp
    xfer = TransactionRecord(
        message_id="TRANS_1",
        tx_date="2026-11-06",
        tx_timestamp="2026-11-06 14:25:30",
        account_name=WALLET_HABIBMETRO,
        target_account=WALLET_NAYAPAY,
        tx_type="TRANSFER_INTERNAL",
        amount=20000.0,
        payee="TOPUP OR SWEEP",
        category="Wallet Top-Up",
        cost_bucket="IGNORE",
        review_status="AUTO_VERIFIED",
        source="GMAIL_HMB",
    )
    assert db.upsert_transaction(xfer) is True
    tx_id = db.get_transactions()[0].id
    assert tx_id is not None
    assert db.get_wallet(WALLET_HABIBMETRO).current_balance == pytest.approx(80000.0)
    assert db.get_wallet(WALLET_NAYAPAY).current_balance == pytest.approx(25000.0)

    # Transition A: TRANSFER_INTERNAL -> TRANSFER_MMF (plus date change preserving 14:25:30!)
    to_mmf = db.update_transaction(tx_id, tx_type="TRANSFER_MMF", tx_date="2026-11-08")
    assert to_mmf is not None
    assert to_mmf.tx_type == "TRANSFER_MMF"
    assert to_mmf.target_account == WALLET_MMF
    assert to_mmf.cost_bucket == "INVESTMENT"
    assert to_mmf.is_internal_transfer is False
    assert to_mmf.tx_date == "2026-11-08"
    assert to_mmf.tx_timestamp == "2026-11-08 14:25:30"
    assert db.get_wallet(WALLET_NAYAPAY).current_balance == pytest.approx(5000.0)
    assert db.get_wallet(WALLET_MMF).current_balance == pytest.approx(20000.0)

    # Transition B: TRANSFER_INTERNAL -> DEBIT (clears target_account & transitions bucket off IGNORE)
    back_internal = db.update_transaction(
        tx_id,
        tx_type="TRANSFER_INTERNAL",
        target_account=WALLET_NAYAPAY,
    )
    assert back_internal is not None
    assert back_internal.cost_bucket == "IGNORE"
    to_debit = db.update_transaction(
        tx_id,
        tx_type="DEBIT",
        category="Bills & Utilities",
    )
    assert to_debit is not None
    assert to_debit.tx_type == "DEBIT"
    assert to_debit.target_account is None
    assert to_debit.is_internal_transfer is False
    assert to_debit.cost_bucket == "FIXED"  # inferred from Bills & Utilities, NOT IGNORE!
    assert db.get_wallet(WALLET_NAYAPAY).current_balance == pytest.approx(5000.0)
    assert db.get_wallet(WALLET_MMF).current_balance == pytest.approx(0.0)
    assert db.get_wallet(WALLET_HABIBMETRO).current_balance == pytest.approx(80000.0)

    # Transition C: CREDIT -> DEBIT flips cost_bucket away from INFLOW and reverses balance by 2x
    cred_tx = db.add_manual_transaction(
        tx_date="2026-11-10",
        account_name=WALLET_HABIBMETRO,
        tx_type="CREDIT",
        amount=10000.0,
        payee="Refund Adjustment",
        category="Shopping",
        cost_bucket="INFLOW",
    )
    assert cred_tx.id is not None
    assert db.get_wallet(WALLET_HABIBMETRO).current_balance == pytest.approx(90000.0)

    flipped_debit = db.update_transaction(cred_tx.id, tx_type="DEBIT")
    assert flipped_debit is not None
    assert flipped_debit.tx_type == "DEBIT"
    assert flipped_debit.cost_bucket == "VARIABLE"
    assert flipped_debit.target_account is None
    assert db.get_wallet(WALLET_HABIBMETRO).current_balance == pytest.approx(70000.0)


def test_get_transactions_like_escaping_and_incoming_transfers(
    db: DatabaseManager,
) -> None:
    """SQL LIKE %/_ wildcards must match literally; include_incoming_transfers=True includes transfers in."""
    db.upsert_many(
        [
            TransactionRecord(
                message_id="LIKE_PCT",
                tx_date="2026-11-05",
                account_name=WALLET_NAYAPAY,
                tx_type="DEBIT",
                amount=500.0,
                payee="100% Pure Mart",
                category="Groceries",
                cost_bucket="FIXED",
            ),
            TransactionRecord(
                message_id="LIKE_NUM",
                tx_date="2026-11-05",
                account_name=WALLET_NAYAPAY,
                tx_type="DEBIT",
                amount=500.0,
                payee="1000 Stores",
                category="Shopping",
                cost_bucket="VARIABLE",
            ),
            TransactionRecord(
                message_id="LIKE_UNDER",
                tx_date="2026-11-06",
                account_name=WALLET_HABIBMETRO,
                tx_type="DEBIT",
                amount=1200.0,
                payee="K_ELECTRIC_BILL",
                category="Bills & Utilities",
                cost_bucket="FIXED",
            ),
            TransactionRecord(
                message_id="LIKE_DASH",
                tx_date="2026-11-06",
                account_name=WALLET_HABIBMETRO,
                tx_type="DEBIT",
                amount=1200.0,
                payee="K-ELECTRIC",
                category="Bills & Utilities",
                cost_bucket="FIXED",
            ),
            TransactionRecord(
                message_id="IN_XFER_NP",
                tx_date="2026-11-07",
                account_name=WALLET_HABIBMETRO,
                target_account=WALLET_NAYAPAY,
                tx_type="TRANSFER_INTERNAL",
                amount=15000.0,
                payee="NAYAPAY TOPUP",
                category="Wallet Top-Up",
                cost_bucket="IGNORE",
            ),
        ]
    )

    pct_hits = db.get_transactions(search_query="100%")
    assert [t.message_id for t in pct_hits] == ["LIKE_PCT"]

    under_hits = db.get_transactions(search_query="K_ELECTRIC")
    assert [t.message_id for t in under_hits] == ["LIKE_UNDER"]

    # Escaped month_key wildcard must NOT match "2026-11"
    assert db.get_transactions(month_key="2026_11") == []

    # include_incoming_transfers=False vs True
    np_out_only = db.get_transactions(
        account_name=WALLET_NAYAPAY, include_incoming_transfers=False
    )
    assert all(t.account_name == WALLET_NAYAPAY for t in np_out_only)
    assert "IN_XFER_NP" not in {t.message_id for t in np_out_only}

    np_with_in = db.get_transactions(
        account_name=WALLET_NAYAPAY, include_incoming_transfers=True
    )
    assert "IN_XFER_NP" in {t.message_id for t in np_with_in}


def test_concurrent_6_thread_fresh_db_wal_startup(tmp_path: Path) -> None:
    """Six threads starting fresh WAL DB simultaneously must succeed with zero lock errors."""
    db_file = tmp_path / "concurrent_wal.db"
    barrier = threading.Barrier(6)
    errors: list[BaseException] = []

    def _worker(idx: int) -> None:
        try:
            barrier.wait(timeout=10)
            with DatabaseManager(db_file) as thread_db:
                thread_db.add_manual_transaction(
                    tx_date="2026-11-11",
                    account_name=WALLET_CASH,
                    tx_type="DEBIT",
                    amount=100.0 * (idx + 1),
                    payee=f"Concurrent Merchant {idx}",
                )
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not errors
    with DatabaseManager(db_file) as verify_db:
        mode = verify_db.conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert str(mode).lower() == "wal"
        txs = verify_db.get_transactions()
        assert len(txs) == 6
        assert verify_db.get_wallet(WALLET_CASH).current_balance == pytest.approx(-2100.0)


def test_delete_category_rule_and_recency_tie_breaking(db: DatabaseManager) -> None:
    """Verify delete_category_rule and id DESC recency tie-breaking on equal priority & length."""
    older = db.upsert_category_rule(
        CategoryRule(
            keyword="ALPHA",
            merchant_alias="Older Alpha",
            category="Shopping",
            cost_bucket="VARIABLE",
            priority=50,
            is_user_defined=True,
        )
    )
    newer = db.upsert_category_rule(
        CategoryRule(
            keyword="OMEGA",
            merchant_alias="Newer Omega",
            category="Food & Dining",
            cost_bucket="VARIABLE",
            priority=50,
            is_user_defined=True,
        )
    )
    assert older.id is not None and newer.id is not None
    assert newer.id > older.id

    rules = db.get_category_rules()
    rule_indices = {r.keyword: idx for idx, r in enumerate(rules)}
    assert rule_indices["OMEGA"] < rule_indices["ALPHA"]

    matched = apply_rules(
        ParsedTransaction(
            message_id="TIE_1",
            tx_date="2026-11-10",
            account_name=WALLET_NAYAPAY,
            tx_type="DEBIT",
            amount=1000.0,
            payee="ALPHA OMEGA STORE",
            source="GMAIL_NAYAPAY",
        ),
        db,
    )
    assert matched.category == "Food & Dining"
    assert matched.merchant_alias == "Newer Omega"

    assert db.delete_category_rule(newer.id) is True
    assert db.delete_category_rule(newer.id) is False
    assert all(r.id != newer.id for r in db.get_category_rules())


def test_backup_csv_no_double_cr_sha256_tamper_rejection_and_restore_roundtrip(
    db: DatabaseManager, tmp_path: Path
) -> None:
    """Verify CSV bytes have no \\r\\r\\n, tampered JSON is rejected, and restore_backup_json round-trips."""
    db.update_wallet_opening_balance(WALLET_HABIBMETRO, 120000.0)
    db.update_wallet_opening_balance(WALLET_NAYAPAY, 5000.0)
    db.update_budget_config(
        monthly_income=250000.0,
        fixed_cost_baseline=90000.0,
        variable_spending_cap=45000.0,
        emergency_multiplier=8,
        envelope_mode=True,
    )
    db.upsert_category_budget("Fitness & Gym", 7500.0, emoji="🏋️", cost_bucket="VARIABLE")
    db.upsert_category_rule(
        CategoryRule(
            keyword="FITNESS",
            merchant_alias="Iron Gym",
            category="Fitness & Gym",
            cost_bucket="VARIABLE",
            priority=100,
            is_user_defined=True,
        )
    )
    db.upsert_recurring_commitment(
        RecurringCommitment(
            title="Gym Membership",
            match_keyword="FITNESS",
            expected_amount=7500.0,
            due_day=3,
            category="Fitness & Gym",
            cost_bucket="VARIABLE",
        )
    )
    db.upsert_many(
        [
            TransactionRecord(
                message_id="BK_XFER",
                tx_date="2026-11-05",
                account_name=WALLET_HABIBMETRO,
                target_account=WALLET_NAYAPAY,
                tx_type="TRANSFER_INTERNAL",
                amount=15000.0,
                payee="NAYAPAY TOPUP",
                category="Wallet Top-Up",
                cost_bucket="IGNORE",
            ),
            TransactionRecord(
                message_id="BK_MMF",
                tx_date="2026-11-06",
                account_name=WALLET_HABIBMETRO,
                target_account=WALLET_MMF,
                tx_type="TRANSFER_MMF",
                amount=30000.0,
                payee="AL MEEZAN MRAF",
                category="Money Market Fund",
                cost_bucket="INVESTMENT",
            ),
            TransactionRecord(
                message_id="BK_GYM",
                tx_date="2026-11-07",
                account_name=WALLET_NAYAPAY,
                tx_type="DEBIT",
                amount=7500.0,
                payee="IRON FITNESS",
                category="Fitness & Gym",
                cost_bucket="VARIABLE",
                notes="Line1\nLine2",
            ),
        ]
    )

    json_path, csv_path = db.save_backup_to_disk(tmp_path / "disk_backups")
    raw_csv_bytes = csv_path.read_bytes()
    assert b"\r\r\n" not in raw_csv_bytes

    backup_json = json_path.read_text(encoding="utf-8")
    parsed_payload = json.loads(backup_json)
    assert parsed_payload["schema_version"] == 1
    assert len(parsed_payload["checksum"]) == 64

    # Tamper detection: modifying amount without updating checksum must fail
    tampered = json.loads(backup_json)
    tampered["transactions"][0]["amount"] = 999999.0
    with pytest.raises(ValueError, match="checksum"):
        db.restore_backup_json(json.dumps(tampered))

    no_checksum = json.loads(backup_json)
    no_checksum.pop("checksum")
    with pytest.raises(ValueError, match="checksum"):
        db.restore_backup_json(json.dumps(no_checksum))

    bad_ver = json.loads(backup_json)
    bad_ver["schema_version"] = 99
    with pytest.raises(ValueError, match="schema_version"):
        db.restore_backup_json(json.dumps(bad_ver))

    # Full round-trip restore into a brand new DB
    with DatabaseManager(tmp_path / "restored.db") as restored_db:
        summary = restored_db.restore_backup_json(backup_json)
        assert summary["transactions"] == 3
        assert summary["accounts"] == 4
        assert restored_db.get_budget_config().envelope_mode is True
        assert restored_db.get_budget_config().emergency_multiplier == 8
        assert any(
            cb.category == "Fitness & Gym" and cb.monthly_limit == 7500.0
            for cb in restored_db.get_category_budgets()
        )
        assert any(r.keyword == "FITNESS" for r in restored_db.get_category_rules())
        assert any(
            c.title == "Gym Membership" for c in restored_db.get_recurring_commitments()
        )
        assert restored_db.get_wallet(WALLET_HABIBMETRO).current_balance == pytest.approx(75000.0)
        assert restored_db.get_wallet(WALLET_NAYAPAY).current_balance == pytest.approx(12500.0)
        assert restored_db.get_wallet(WALLET_MMF).current_balance == pytest.approx(30000.0)


def test_restore_backup_json_structural_duplicate_guards_and_record_normalization(
    db: DatabaseManager,
) -> None:
    """Verify TransactionRecord field normalization and SHA-256-signed malformed backup rejection before DB mutation."""
    # 1. TransactionRecord normalization checks
    credit_rec = TransactionRecord(
        message_id="REC_NORM_CREDIT",
        tx_date="2026-11-10",
        account_name=WALLET_HABIBMETRO,
        tx_type="CREDIT",
        amount=1500.0,
        payee="Cashback Bonus",
        category="Shopping",
        cost_bucket="FIXED",
    )
    assert credit_rec.cost_bucket == "INFLOW"

    aligned_ts_rec = TransactionRecord(
        message_id="REC_NORM_TS",
        tx_date="2026-11-15 09:00:00",
        tx_timestamp="2026-11-01 14:30:00",
        account_name=WALLET_NAYAPAY,
        tx_type="DEBIT",
        amount=500.0,
        payee="Bykea Ride",
    )
    assert aligned_ts_rec.tx_date == "2026-11-15"
    assert aligned_ts_rec.tx_timestamp == "2026-11-15 14:30:00"

    # Seed valid baseline backup payload from a temporary sentinel state
    db.upsert_transaction(
        TransactionRecord(
            message_id="SENTINEL_KEEP",
            tx_date="2026-11-09",
            account_name=WALLET_HABIBMETRO,
            tx_type="DEBIT",
            amount=1234.0,
            payee="Sentinel Store",
            category="Shopping",
            cost_bucket="VARIABLE",
        )
    )
    valid_snapshot = json.loads(db.export_backup_json())

    def _signed(mutator) -> str:
        payload = json.loads(json.dumps(valid_snapshot))
        payload.pop("checksum", None)
        mutator(payload)
        payload["checksum"] = _compute_backup_checksum(payload)
        return json.dumps(payload)

    # Now modify live DB slightly to detect if failed restore mutates or wipes sentinel
    db.update_budget_config(monthly_income=333000.0)

    bad_cases: list[tuple[str, Any]] = [
        ("budget_config not a dict", lambda p: p.__setitem__("budget_config", ["not-a-dict"])),
        ("table not a list", lambda p: p.__setitem__("category_rules", {"not": "a-list"})),
        ("table row not a dict", lambda p: p.__setitem__("category_budgets", ["row-str"])),
        (
            "duplicate id in category_rules",
            lambda p: p["category_rules"].append({**p["category_rules"][0], "keyword": "UNIQ_DUP_ID_KW"}),
        ),
        (
            "duplicate category in category_budgets",
            lambda p: p["category_budgets"].append({**p["category_budgets"][0], "id": 99999}),
        ),
        (
            "duplicate keyword case-insensitive in category_rules",
            lambda p: p["category_rules"].append(
                {**p["category_rules"][0], "id": 99998, "keyword": p["category_rules"][0]["keyword"].lower()}
            ),
        ),
        (
            "duplicate title in recurring_commitments",
            lambda p: p["recurring_commitments"].append({**p["recurring_commitments"][0], "id": 99997}),
        ),
        (
            "duplicate message_id in transactions",
            lambda p: p["transactions"].append({**p["transactions"][0], "id": 99996}),
        ),
    ]

    for label, mutator in bad_cases:
        with pytest.raises(ValueError):
            db.restore_backup_json(_signed(mutator))

        # Sentinel transaction and live budget_config must remain completely untouched
        live_txs = db.get_transactions()
        assert len(live_txs) == 1, label
        assert live_txs[0].message_id == "SENTINEL_KEEP", label
        assert live_txs[0].amount == pytest.approx(1234.0), label
        assert db.get_budget_config().monthly_income == pytest.approx(333000.0), label


def test_calibrate_wallet_current_balance_no_double_deduction(
    db: DatabaseManager, tmp_path: Path
) -> None:
    """Timestamp-anchored live balance treats pre-anchor PDF/email imports as past while applying newer txns."""
    # Calibrate live balances across wallets as of explicit cutoff timestamp
    cutoff = "2026-11-12 15:00:00"
    db.calibrate_wallet_current_balance(WALLET_HABIBMETRO, 100000.0, as_of=cutoff)
    calibrated = db.calibrate_wallet_current_balance(WALLET_NAYAPAY, 15000.0, as_of=cutoff)
    assert calibrated.current_balance == pytest.approx(15000.0)
    assert calibrated.opening_balance == pytest.approx(15000.0)
    assert calibrated.balance_as_of == cutoff

    # Later, user imports historical PDF / old synced emails dated AT OR BEFORE cutoff (<= 15:00:00)
    db.upsert_many(
        [
            TransactionRecord(
                message_id="CAL_NP_HIST_1",
                tx_date="2026-11-02",
                tx_timestamp="2026-11-02 12:30:00",
                account_name=WALLET_NAYAPAY,
                tx_type="DEBIT",
                amount=6000.0,
                tax_fee_amount=250.0,
                payee="FOODPANDA HIST",
                category="Dining & Takeout",
                cost_bucket="VARIABLE",
            ),
            TransactionRecord(
                message_id="CAL_XFER_HIST",
                tx_date="2026-11-12",
                tx_timestamp="2026-11-12 00:00:00",
                account_name=WALLET_HABIBMETRO,
                target_account=WALLET_NAYAPAY,
                tx_type="TRANSFER_INTERNAL",
                amount=5000.0,
                payee="NAYAPAY HIST TOPUP",
                category="Wallet Top-Up",
                cost_bucket="IGNORE",
            ),
        ]
    )
    # Past rows must be in ledger for analytics, yet MUST NOT double-deduct or inflate current balances
    assert len(db.get_transactions()) == 2
    assert db.get_wallet(WALLET_NAYAPAY).current_balance == pytest.approx(15000.0)
    assert db.get_wallet(WALLET_HABIBMETRO).current_balance == pytest.approx(100000.0)

    # New transaction arriving STRICTLY AFTER cutoff (> 15:00:00) deducts cleanly
    db.upsert_many(
        [
            TransactionRecord(
                message_id="CAL_NP_NEW_1",
                tx_date="2026-11-12",
                tx_timestamp="2026-11-12 16:30:00",
                account_name=WALLET_NAYAPAY,
                tx_type="DEBIT",
                amount=1200.0,
                payee="DARAZ NEW",
                category="Shopping & Retail",
                cost_bucket="VARIABLE",
            )
        ]
    )
    after_new = db.get_wallet(WALLET_NAYAPAY)
    assert after_new is not None and after_new.current_balance == pytest.approx(13800.0)

    # Manual transaction logged today after calibrating at now must apply immediately too
    cash_cal = db.calibrate_wallet_current_balance(WALLET_CASH, 5000.0)
    assert cash_cal.current_balance == pytest.approx(5000.0)
    db.add_manual_transaction(
        tx_date=cash_cal.balance_as_of[:10],
        account_name=WALLET_CASH,
        tx_type="DEBIT",
        amount=450.0,
        payee="CHAI CAFE",
        category="Dining & Takeout",
        cost_bucket="VARIABLE",
    )
    assert db.get_wallet(WALLET_CASH).current_balance == pytest.approx(4550.0)

    # Updating pre-history opening balance clears balance_as_of anchor cleanly
    cleared = db.update_wallet_opening_balance(WALLET_NAYAPAY, 20000.0)
    assert cleared.balance_as_of is None
    assert cleared.current_balance == pytest.approx(17550.0)

    # Pre-migration SQLite file without balance_as_of column must migrate automatically
    legacy_path = tmp_path / "legacy.db"
    import sqlite3

    raw_conn = sqlite3.connect(legacy_path)
    raw_conn.execute(
        """
        CREATE TABLE accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_name TEXT NOT NULL UNIQUE,
            account_type TEXT NOT NULL,
            opening_balance REAL NOT NULL DEFAULT 0.0,
            current_balance REAL NOT NULL DEFAULT 0.0,
            currency TEXT NOT NULL DEFAULT 'PKR',
            icon TEXT NOT NULL DEFAULT '🏦',
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    raw_conn.commit()
    raw_conn.close()
    migrated_db = DatabaseManager(legacy_path)
    try:
        cols = {
            r["name"]
            for r in migrated_db.conn.execute("PRAGMA table_info(accounts)").fetchall()
        }
        assert "balance_as_of" in cols
    finally:
        migrated_db.close()

    with pytest.raises(ValueError, match="Unknown wallet"):
        db.calibrate_wallet_current_balance("NonExistentWallet", 1000.0)
    with pytest.raises(ValueError, match="finite"):
        db.calibrate_wallet_current_balance(WALLET_NAYAPAY, float("nan"))


def test_dynamic_category_crud_rename_cascade_delete_and_user_version_migration(
    db: DatabaseManager, tmp_path: Path
) -> None:
    """Verify INFLOW/IGNORE seeding, rename cascading across tables, delete fallback, and v2 migration."""
    budgets = db.get_category_budgets(active_only=False)
    by_name = {cb.category: cb for cb in budgets}
    assert "Salary & Income" in by_name and by_name["Salary & Income"].cost_bucket == "INFLOW"
    assert "Freelance & Client" in by_name and by_name["Freelance & Client"].cost_bucket == "INFLOW"
    assert "Internal Transfer" in by_name and by_name["Internal Transfer"].cost_bucket == "IGNORE"

    # Create a custom outflow category and attach tx + rule + recurring bill to it
    custom = db.upsert_category_budget(
        category="Coffee & Tea",
        monthly_limit=6000.0,
        emoji="☕",
        cost_bucket="VARIABLE",
        is_active=True,
    )
    assert custom.id is not None
    tx = db.add_manual_transaction(
        tx_date="2026-11-05",
        account_name=WALLET_CASH,
        tx_type="DEBIT",
        amount=800.0,
        payee="CHAI SPOT",
        category="Coffee & Tea",
        cost_bucket="VARIABLE",
    )
    db.upsert_category_rule(
        CategoryRule(
            keyword="CHAI SPOT",
            category="Coffee & Tea",
            cost_bucket="VARIABLE",
            priority=90,
        )
    )
    db.upsert_recurring_commitment(
        RecurringCommitment(
            title="Monthly Beans",
            match_keyword="CHAI SPOT",
            expected_amount=2000.0,
            due_day=5,
            category="Coffee & Tea",
            cost_bucket="VARIABLE",
        )
    )

    # Rename + re-bucket cascades to transactions, category_rules, and recurring_commitments
    renamed = db.update_category_budget_by_id(
        custom.id,
        category="Artisan Drinks",
        emoji="🧋",
        cost_bucket="LEAKAGE",
        monthly_limit=4500.0,
        is_active=True,
    )
    assert renamed.category == "Artisan Drinks"
    tx_after = db.get_transaction_by_id(tx.id or 0)
    assert tx_after is not None and tx_after.category == "Artisan Drinks"
    assert tx_after.cost_bucket == "LEAKAGE"
    assert any(
        r.keyword == "CHAI SPOT" and r.category == "Artisan Drinks" and r.cost_bucket == "LEAKAGE"
        for r in db.get_category_rules()
    )
    assert any(
        b.title == "Monthly Beans" and b.category == "Artisan Drinks" and b.cost_bucket == "LEAKAGE"
        for b in db.get_recurring_commitments(active_only=False)
    )

    # Collision check prevents renaming onto existing category
    with pytest.raises(ValueError, match="already exists"):
        db.update_category_budget_by_id(
            custom.id,
            category="Groceries",
            emoji="🧋",
            cost_bucket="VARIABLE",
            monthly_limit=4500.0,
        )

    # Delete removes row, deletes rules, and reassigns ledger + recurring commitments to Uncategorized
    deleted, reassigned = db.delete_category_budget(custom.id)
    assert deleted is True
    assert reassigned == 1
    tx_post_del = db.get_transaction_by_id(tx.id or 0)
    assert tx_post_del is not None and tx_post_del.category == "Uncategorized"
    assert not any(r.category == "Artisan Drinks" for r in db.get_category_rules())
    assert any(
        b.title == "Monthly Beans" and b.category == "Uncategorized"
        for b in db.get_recurring_commitments(active_only=False)
    )

    # Guard prevents deleting system fallback Uncategorized
    uncat_id = next(c.id for c in db.get_category_budgets(active_only=False) if c.category == "Uncategorized")
    with pytest.raises(ValueError, match="cannot be removed"):
        db.delete_category_budget(uncat_id or 0)

    # Simulate legacy pre-v2 DB that already had outflow categories but lacked INFLOW defaults
    import sqlite3

    v1_path = tmp_path / "legacy_v1_cats.db"
    init_db = DatabaseManager(v1_path)
    init_db.conn.execute("DELETE FROM category_budgets WHERE cost_bucket IN ('INFLOW', 'IGNORE')")
    init_db.conn.execute("PRAGMA user_version = 1")
    init_db.conn.commit()
    init_db.close()

    upgraded = DatabaseManager(v1_path)
    try:
        names = {cb.category for cb in upgraded.get_category_budgets(active_only=False)}
        assert "Salary & Income" in names
        assert "Internal Transfer" in names
        assert upgraded.conn.execute("PRAGMA user_version").fetchone()[0] >= 2
        # Delete Salary & Income after migration -> must NOT come back on next open
        sal_id = next(
            cb.id for cb in upgraded.get_category_budgets(active_only=False) if cb.category == "Salary & Income"
        )
        upgraded.delete_category_budget(sal_id or 0)
    finally:
        upgraded.close()

    reopened = DatabaseManager(v1_path)
    try:
        names_reopen = {cb.category for cb in reopened.get_category_budgets(active_only=False)}
        assert "Salary & Income" not in names_reopen
    finally:
        reopened.close()


def test_find_and_link_internal_transfer_pairs_and_mirror_credits(db: DatabaseManager) -> None:
    """Verify own-account transfer pair detection & reconciliation preserves net balances."""
    db.update_wallet_opening_balance(WALLET_HABIBMETRO, 50000.0)
    db.update_wallet_opening_balance(WALLET_NAYAPAY, 5000.0)

    # Case 1: Matching DEBIT on HabibMetro and CREDIT on NayaPay within 1 day
    db.upsert_many(
        [
            TransactionRecord(
                message_id="PAIR_OUT_1",
                tx_date="2026-11-10",
                tx_timestamp="2026-11-10 10:00:00",
                account_name=WALLET_HABIBMETRO,
                tx_type="DEBIT",
                amount=10000.0,
                payee="RAAST IBFT TO 03001234567",
                category="Uncategorized",
                cost_bucket="VARIABLE",
                review_status="NEEDS_REVIEW",
                source="GMAIL_HMB",
            ),
            TransactionRecord(
                message_id="PAIR_IN_1",
                tx_date="2026-11-10",
                tx_timestamp="2026-11-10 10:02:00",
                account_name=WALLET_NAYAPAY,
                tx_type="CREDIT",
                amount=10000.0,
                payee="Incoming Raast from HabibMetro",
                category="Salary & Income",
                cost_bucket="INFLOW",
                review_status="AUTO_VERIFIED",
                source="GMAIL_NAYAPAY",
            ),
        ]
    )
    # Before linking, mirror credit caused NayaPay to receive 10k AND HMB lost 10k,
    # BUT both rows wrongly looked like expense + inflow!
    pairs = db.find_internal_transfer_pairs(day_window=2)
    assert len(pairs) == 1
    assert pairs[0]["mode"] == "PAIR_DEBIT_CREDIT"
    out_id = pairs[0]["out_tx"].id
    in_id = pairs[0]["in_tx"].id

    linked = db.link_internal_transfer_pair(out_id, in_id)
    assert linked.tx_type == "TRANSFER_INTERNAL"
    assert linked.target_account == WALLET_NAYAPAY
    assert linked.category == "Wallet Top-Up"
    assert linked.cost_bucket == "IGNORE"
    assert db.get_transaction_by_id(in_id) is None
    assert db.get_wallet(WALLET_HABIBMETRO).current_balance == pytest.approx(40000.0)
    assert db.get_wallet(WALLET_NAYAPAY).current_balance == pytest.approx(15000.0)

    # Case 2: Already TRANSFER_INTERNAL on HMB + duplicate mirror CREDIT imported from NayaPay PDF
    db.upsert_many(
        [
            TransactionRecord(
                message_id="MIRROR_OUT_2",
                tx_date="2026-11-11",
                tx_timestamp="2026-11-11 12:00:00",
                account_name=WALLET_HABIBMETRO,
                target_account=WALLET_NAYAPAY,
                tx_type="TRANSFER_INTERNAL",
                amount=3000.0,
                payee="IBFT NayaPay",
                category="Wallet Top-Up",
                cost_bucket="IGNORE",
                review_status="AUTO_VERIFIED",
                is_internal_transfer=True,
                source="GMAIL_HMB",
            ),
            TransactionRecord(
                message_id="MIRROR_IN_2",
                tx_date="2026-11-11",
                tx_timestamp="2026-11-11 12:05:00",
                account_name=WALLET_NAYAPAY,
                tx_type="CREDIT",
                amount=3000.0,
                payee="Raast Transfer Received",
                category="Other Inflow",
                cost_bucket="INFLOW",
                review_status="AUTO_VERIFIED",
                source="PDF_IMPORT",
            ),
        ]
    )
    mirror_pairs = db.find_internal_transfer_pairs(day_window=2)
    assert len(mirror_pairs) == 1
    assert mirror_pairs[0]["mode"] == "MERGE_DUPLICATE_CREDIT"
    db.link_internal_transfer_pair(mirror_pairs[0]["out_tx"].id, mirror_pairs[0]["in_tx"].id)
    assert db.find_internal_transfer_pairs(day_window=2) == []
    # Without double-credit, NayaPay moved from 15k -> 18k (not 21k!)
    assert db.get_wallet(WALLET_HABIBMETRO).current_balance == pytest.approx(37000.0)
    assert db.get_wallet(WALLET_NAYAPAY).current_balance == pytest.approx(18000.0)


def test_cross_source_deduplication_and_reconciliation(
    db: DatabaseManager, tmp_path: Path
) -> None:
    """Verify cross-source PDF/Gmail deduplication, alt_id tracking, same-source twins, and cleanup."""
    db.update_wallet_opening_balance(WALLET_NAYAPAY, 10000.0)

    # 1. Pre-existing verified PDF rows (Raast transfer + combined ATM fee)
    pdf_tariq = TransactionRecord(
        message_id="PDF_NAYAPAY_2026-11-11_tariq",
        tx_date="2026-11-11",
        tx_timestamp="2026-11-11 21:46:00",
        account_name=WALLET_NAYAPAY,
        tx_type="DEBIT",
        amount=1389.0,
        payee="Raast Out Outgoing fund transfer to Tariq easypaisa Bank-8210 Raast-1234",
        merchant_alias="Foodpanda",
        category="Food & Dining",
        cost_bucket="VARIABLE",
        review_status="MANUALLY_VERIFIED",
        source="PDF_IMPORT",
        notes="Transaction ID abc123 | Service Charges Rs. 0",
    )
    pdf_atm = TransactionRecord(
        message_id="PDF_NAYAPAY_2026-11-08_atm",
        tx_date="2026-11-08",
        tx_timestamp="2026-11-08 16:22:00",
        account_name=WALLET_NAYAPAY,
        tx_type="DEBIT",
        amount=1035.0,
        tax_fee_amount=0.0,
        payee="Cash Withdrawal Cash Withdrawn at ATM",
        merchant_alias="ATM Cash Withdrawal",
        category="Cash Envelope",
        cost_bucket="VARIABLE",
        review_status="AUTO_VERIFIED",
        source="PDF_IMPORT",
    )
    ins_pdf, _ = db.upsert_many([pdf_tariq, pdf_atm])
    assert ins_pdf == 2

    # 2. Gmail sync brings matching rows with different message_ids, timestamps within 1 min, and split fee
    gmail_tariq = TransactionRecord(
        message_id="XGM_TARIQ_1234",
        tx_date="2026-11-11",
        tx_timestamp="2026-11-11 21:46:05",
        account_name=WALLET_NAYAPAY,
        tx_type="DEBIT",
        amount=1389.0,
        payee="Tariq",
        category="Uncategorized",
        cost_bucket="VARIABLE",
        review_status="NEEDS_REVIEW",
        source="GMAIL_NAYAPAY",
        payment_channel="Raast",
        raw_subject="You sent Rs. 1,389 to Tariq 💸",
    )
    gmail_atm = TransactionRecord(
        message_id="XGM_ATM_1035",
        tx_date="2026-11-08",
        tx_timestamp="2026-11-08 16:22:43",
        account_name=WALLET_NAYAPAY,
        tx_type="DEBIT",
        amount=1000.0,
        tax_fee_amount=35.0,
        payee="ATM Cash Withdrawal",
        category="Cash Envelope",
        cost_bucket="VARIABLE",
        review_status="AUTO_VERIFIED",
        source="GMAIL_NAYAPAY",
        raw_subject="Cash withdrawal of Rs. 1,035 🏧",
    )
    ins_gm, skip_gm = db.upsert_many([gmail_tariq, gmail_atm])
    assert (ins_gm, skip_gm) == (0, 2)

    # Re-syncing again still skips both via [alt_id:...]
    ins_gm2, skip_gm2 = db.upsert_many([gmail_tariq, gmail_atm])
    assert (ins_gm2, skip_gm2) == (0, 2)

    txs = {t.message_id: t for t in db.get_transactions()}
    assert len(txs) == 2
    kept_tariq = txs["PDF_NAYAPAY_2026-11-11_tariq"]
    assert kept_tariq.review_status == "MANUALLY_VERIFIED"
    assert kept_tariq.category == "Food & Dining"
    assert kept_tariq.raw_subject == "You sent Rs. 1,389 to Tariq 💸"
    assert kept_tariq.payment_channel == "Raast"
    assert "[alt_id:XGM_TARIQ_1234]" in (kept_tariq.notes or "")

    kept_atm = txs["PDF_NAYAPAY_2026-11-08_atm"]
    assert kept_atm.amount == pytest.approx(1000.0)
    assert kept_atm.tax_fee_amount == pytest.approx(35.0)
    assert "[alt_id:XGM_ATM_1035]" in (kept_atm.notes or "")
    # Wallet balance unaffected by deduplication (10000 - 1389 - 1035 = 7576)
    assert db.get_wallet(WALLET_NAYAPAY).current_balance == pytest.approx(7576.0)

    # 3. Same-source same-day twin transactions (42s apart) MUST NOT be merged
    twin_1 = TransactionRecord(
        message_id="XGM_TWIN_1",
        tx_date="2026-09-19",
        tx_timestamp="2026-09-19 20:37:17",
        account_name=WALLET_NAYAPAY,
        tx_type="DEBIT",
        amount=1000.0,
        tax_fee_amount=35.0,
        payee="ATM Cash Withdrawal",
        category="Cash Envelope",
        cost_bucket="VARIABLE",
        review_status="AUTO_VERIFIED",
        source="GMAIL_NAYAPAY",
    )
    twin_2 = TransactionRecord(
        message_id="XGM_TWIN_2",
        tx_date="2026-09-19",
        tx_timestamp="2026-09-19 20:37:59",
        account_name=WALLET_NAYAPAY,
        tx_type="DEBIT",
        amount=1000.0,
        tax_fee_amount=35.0,
        payee="ATM Cash Withdrawal",
        category="Cash Envelope",
        cost_bucket="VARIABLE",
        review_status="AUTO_VERIFIED",
        source="GMAIL_NAYAPAY",
    )
    ins_twins, skip_twins = db.upsert_many([twin_1, twin_2])
    assert (ins_twins, skip_twins) == (2, 0)

    # 4. reconcile_existing_duplicates_and_payees cleans pre-existing dupes, bogus headers & colliding rules
    dirty_path = tmp_path / "dirty_legacy.db"
    with DatabaseManager(dirty_path) as dirty_db:
        # Insert directly via SQL to simulate pre-fix legacy state
        dirty_db.conn.execute(
            """
            INSERT INTO transactions (
                message_id, tx_date, tx_timestamp, account_name, tx_type, amount,
                tax_fee_amount, payee, category, cost_bucket, review_status, source
            ) VALUES
            ('PDF_BOGUS_1', '2026-11-01', '2026-11-01 00:00:00', 'NayaPay', 'DEBIT', 12.0, 0.0, 'Nov 2026', 'Uncategorized', 'VARIABLE', 'NEEDS_REVIEW', 'PDF_IMPORT'),
            ('PDF_KEEP_Z', '2026-11-10', '2026-11-10 22:58:00', 'NayaPay', 'DEBIT', 400.0, 0.0, 'Raast Out Outgoing fund transfer to Bilal Khan easypaisa Bank-4455', 'Peer Transfer', 'VARIABLE', 'MANUALLY_VERIFIED', 'PDF_IMPORT'),
            ('XGM_DUPE_Z', '2026-11-10', '2026-11-10 22:58:15', 'NayaPay', 'DEBIT', 400.0, 0.0, 'You sent Rs. 400 to Bilal Khan 💸', 'Uncategorized', 'VARIABLE', 'NEEDS_REVIEW', 'GMAIL_NAYAPAY'),
            ('XGM_OLD_Z', '2026-10-15', '2026-10-15 22:13:13', 'NayaPay', 'DEBIT', 200.0, 0.0, 'You sent Rs. 200 to Bilal Khan 💸', 'Uncategorized', 'VARIABLE', 'NEEDS_REVIEW', 'GMAIL_NAYAPAY'),
            ('XGM_MASK_U', '2026-11-12', '2026-11-12 10:00:00', 'NayaPay', 'DEBIT', 500.0, 0.0, '●●●●9999', 'Uncategorized', 'VARIABLE', 'NEEDS_REVIEW', 'GMAIL_NAYAPAY')
            """
        )
        dirty_db.conn.execute(
            "UPDATE transactions SET raw_subject = 'You sent Rs. 500 to Test Person 💸' WHERE message_id = 'XGM_MASK_U'"
        )
        dirty_db.conn.execute(
            """
            INSERT INTO category_rules (keyword, merchant_alias, category, cost_bucket, priority, is_user_defined)
            VALUES
            ('Raast Out Outgoing fund transfer to Bilal Khan easypaisa Bank-4455', 'Peer Transfer', 'Peer Transfer', 'VARIABLE', 100, 1),
            ('You sent Rs. 200 to Bilal Khan 💸', 'Peer Transfer', 'Peer Transfer', 'VARIABLE', 100, 1)
            """
        )
        dirty_db.conn.commit()
        stats = dirty_db.reconcile_existing_duplicates_and_payees()
        assert stats["removed_bogus"] == 1
        assert stats["merged_dupes"] == 1
        assert stats["normalized_rules"] == 2
        assert stats["recategorized"] >= 1

        clean_txs = {t.message_id: t for t in dirty_db.get_transactions()}
        assert "PDF_BOGUS_1" not in clean_txs
        assert "XGM_DUPE_Z" not in clean_txs
        assert "[alt_id:XGM_DUPE_Z]" in (clean_txs["PDF_KEEP_Z"].notes or "")
        assert clean_txs["XGM_OLD_Z"].payee == "Bilal Khan"
        assert clean_txs["XGM_OLD_Z"].category == "Peer Transfer"
        assert clean_txs["XGM_OLD_Z"].review_status == "AUTO_VERIFIED"
        assert clean_txs["XGM_MASK_U"].payee == "Test Person"

        user_rule_kws = [
            r.keyword for r in dirty_db.get_category_rules() if r.is_user_defined
        ]
        assert user_rule_kws == ["Bilal Khan"]


def test_security_password_hashing_and_constant_time_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify PBKDF2-HMAC-SHA256 hashing, unique salts, and env/secret password verification."""
    from src.security import hash_password, is_auth_enabled, verify_password

    monkeypatch.delenv("TREASURY_APP_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("TREASURY_APP_PASSWORD", raising=False)
    monkeypatch.delenv("TREASURY_REQUIRE_AUTH", raising=False)
    assert is_auth_enabled() is False

    h1 = hash_password("TopSecretPin#2026")
    h2 = hash_password("TopSecretPin#2026")
    assert h1.startswith("pbkdf2_sha256$310000$")
    assert h1 != h2
    assert verify_password("TopSecretPin#2026", stored_hash=h1) is True
    assert verify_password("WrongPin", stored_hash=h1) is False
    assert verify_password("", stored_hash=h1) is False
    assert verify_password("TopSecretPin#2026", stored_hash="malformed$hash") is False

    monkeypatch.setenv("TREASURY_APP_PASSWORD_HASH", h1)
    assert is_auth_enabled() is True
    assert verify_password("TopSecretPin#2026") is True
    assert verify_password("WrongPin") is False


def test_security_rate_limiter_exponential_lockout() -> None:
    """Verify 5-failure threshold and 30s -> 60s -> 120s -> 300s exponential cooldowns."""
    from src.security import AuthRateLimiter

    fake_now = [1000.0]
    limiter = AuthRateLimiter(time_fn=lambda: fake_now[0])

    for _ in range(4):
        assert limiter.record_failure("acct") == 0
        assert limiter.is_locked("acct") == (False, 0)

    # 5th failure locks for 30s
    assert limiter.record_failure("acct") == 30
    locked, rem = limiter.is_locked("acct")
    assert locked is True and rem == 30

    fake_now[0] += 31.0
    assert limiter.is_locked("acct")[0] is False
    assert limiter.record_failure("acct") == 60

    fake_now[0] += 61.0
    assert limiter.record_failure("acct") == 120

    fake_now[0] += 121.0
    assert limiter.record_failure("acct") == 240

    fake_now[0] += 241.0
    assert limiter.record_failure("acct") == 300

    fake_now[0] += 301.0
    assert limiter.record_failure("acct") == 300  # capped at 300s

    limiter.record_success("acct")
    assert limiter.failure_count("acct") == 0
    assert limiter.is_locked("acct") == (False, 0)


def test_encrypted_backup_roundtrip_wrong_key_and_tamper_rejection(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Verify Fernet + PBKDF2 backups conceal data, restore losslessly, and reject tampering."""
    db.upsert_many(
        [
            apply_rules(
                ParsedTransaction(
                    message_id="ENC_TX_1",
                    tx_date="2026-11-11",
                    account_name=WALLET_NAYAPAY,
                    tx_type="DEBIT",
                    amount=1450.0,
                    payee="Confidential Pharmacy",
                    source="GMAIL_NAYAPAY",
                    suggested_category="Health & Pharmacy",
                ),
                db,
            )
        ]
    )
    envelope_json = db.export_encrypted_backup("MyStrongVaultPass!99")
    envelope = json.loads(envelope_json)
    assert envelope["format"] == "ptos-encrypted-vault"
    assert envelope["iterations"] == 310_000
    assert "Confidential Pharmacy" not in envelope_json

    dest_path = tmp_path / "dest_restored.db"
    with DatabaseManager(dest_path) as dest_db:
        with pytest.raises(ValueError, match="wrong backup passphrase"):
            dest_db.restore_encrypted_backup(envelope_json, passphrase="wrong-password")
        assert dest_db.get_transactions() == []

        # Tamper a single character inside the ciphertext HMAC envelope
        tampered = dict(envelope)
        ct = list(tampered["ciphertext"])
        ct[-2] = "A" if ct[-2] != "A" else "B"
        tampered["ciphertext"] = "".join(ct)
        with pytest.raises(ValueError, match="tampered|wrong backup passphrase"):
            dest_db.restore_encrypted_backup(
                json.dumps(tampered), passphrase="MyStrongVaultPass!99"
            )

        summary = dest_db.restore_encrypted_backup(
            envelope_json, passphrase="MyStrongVaultPass!99"
        )
        assert summary["transactions"] == 1
        restored_txs = dest_db.get_transactions()
        assert len(restored_txs) == 1
        assert restored_txs[0].payee == "Confidential Pharmacy"
        assert restored_txs[0].amount == pytest.approx(1450.0)


def test_cloud_vault_github_gist_sync_and_empty_init_auto_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify GitHub Gist encrypted push and automatic zero-data-loss restore on empty DB init."""
    from src.security import GIST_BACKUP_FILENAME, push_encrypted_cloud_backup

    gist_store: dict[str, str] = {}

    class _FakeResp:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._bytes = json.dumps(payload).encode("utf-8")

        def read(self) -> bytes:
            return self._bytes

        def __enter__(self) -> "_FakeResp":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

    def _fake_urlopen(req: urllib.request.Request, timeout: int = 10, context: Any = None) -> _FakeResp:
        assert req.full_url == "https://api.github.com/gists/abcdef1234567890"
        assert req.get_header("Authorization") == "Bearer ghp_testtoken_1234567890"
        if req.get_method() == "PATCH":
            body = json.loads((req.data or b"{}").decode("utf-8"))
            gist_store["content"] = body["files"][GIST_BACKUP_FILENAME]["content"]
            return _FakeResp({"id": "abcdef1234567890"})
        return _FakeResp(
            {"files": {GIST_BACKUP_FILENAME: {"content": gist_store.get("content", "")}}}
        )

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setenv("GITHUB_BACKUP_TOKEN", "ghp_testtoken_1234567890")
    monkeypatch.setenv("GITHUB_BACKUP_GIST_ID", "abcdef1234567890")
    monkeypatch.setenv("TREASURY_BACKUP_KEY", "CloudVaultMaster#2026")
    monkeypatch.delenv("TREASURY_CLOUD_SYNC", raising=False)

    src_path = tmp_path / "source_cloud.db"
    with DatabaseManager(src_path) as src_db:
        src_db.upsert_many(
            [
                apply_rules(
                    ParsedTransaction(
                        message_id="CLOUD_TX_1",
                        tx_date="2026-11-12",
                        account_name=WALLET_HABIBMETRO,
                        tx_type="DEBIT",
                        amount=3200.0,
                        payee="Cloud Synced Roastery",
                        source="GMAIL_HMB",
                    ),
                    src_db,
                )
            ]
        )
        ok, msg = src_db.sync_to_cloud_vault()
        assert ok is True, msg
        assert "Cloud Synced Roastery" not in gist_store["content"]

    # Spinning up a fresh DB automatically restores the encrypted Gist vault on init_db()
    fresh_path = tmp_path / "fresh_container.db"
    with DatabaseManager(fresh_path) as fresh_db:
        txs = fresh_db.get_transactions()
        assert len(txs) == 1
        assert txs[0].message_id == "CLOUD_TX_1"
        assert txs[0].payee == "Cloud Synced Roastery"
        assert txs[0].amount == pytest.approx(3200.0)

    # Malformed Gist ID must fail validation cleanly
    monkeypatch.setenv("GITHUB_BACKUP_GIST_ID", "../evil/path")
    ok_bad, msg_bad = push_encrypted_cloud_backup("{}")
    assert ok_bad is False
    assert "invalid" in msg_bad.lower()


def test_cloud_vault_gmail_imap_sync_and_secret_redaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify Gmail IMAP encrypted self-vault sync/pull and sensitive credential redaction."""
    import imaplib
    from src.security import redact_sensitive_text

    mailbox: list[bytes] = []

    class _FakeImapSSL:
        def __init__(self, host: str, port: int, **kwargs: Any) -> None:
            self.host = host
            self.port = port

        def login(self, user: str, pw: str) -> tuple[str, list[bytes]]:
            return "OK", [b"Logged in"]

        def append(
            self, box: str, flags: str, dt: Any, raw_msg: bytes
        ) -> tuple[str, list[bytes]]:
            mailbox.append(raw_msg)
            return "OK", [b"APPENDED"]

        def select(self, box: str = "INBOX", readonly: bool = True) -> tuple[str, list[bytes]]:
            return "OK", [str(len(mailbox)).encode()]

        def search(self, charset: Any, *criteria: str) -> tuple[str, list[bytes]]:
            if not mailbox:
                return "OK", [b""]
            ids = " ".join(str(i + 1) for i in range(len(mailbox))).encode()
            return "OK", [ids]

        def fetch(self, msg_id: bytes, spec: str) -> tuple[str, list[tuple[bytes, bytes]]]:
            idx = int(msg_id.decode()) - 1
            return "OK", [(b"RFC822", mailbox[idx])]

        def logout(self) -> tuple[str, list[bytes]]:
            return "BYE", []

    monkeypatch.setattr(imaplib, "IMAP4_SSL", _FakeImapSSL)
    monkeypatch.setenv("TREASURY_CLOUD_SYNC", "gmail")
    monkeypatch.setenv("GMAIL_USER", "vaultowner@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.setenv("TREASURY_BACKUP_KEY", "GmailVaultKey!77")

    with DatabaseManager(tmp_path / "gmail_src.db") as src_db:
        src_db.upsert_many(
            [
                apply_rules(
                    ParsedTransaction(
                        message_id="GMAIL_VAULT_1",
                        tx_date="2026-11-13",
                        account_name=WALLET_NAYAPAY,
                        tx_type="CREDIT",
                        amount=15000.0,
                        payee="Freelance Retainer",
                        source="GMAIL_NAYAPAY",
                    ),
                    src_db,
                )
            ]
        )
        ok, msg = src_db.sync_to_cloud_vault()
        assert ok is True, msg
        assert len(mailbox) == 1

    # Fresh DB auto-restores cleanly from Gmail IMAP vault
    with DatabaseManager(tmp_path / "gmail_dest.db") as dest_db:
        txs = dest_db.get_transactions()
        assert len(txs) == 1
        assert txs[0].payee == "Freelance Retainer"
        assert txs[0].amount == pytest.approx(15000.0)

    leaked = (
        "Error connecting with app_password=secret_pw_999, "
        "token=ghp_1234567890abcdef1234, Bearer ya29.super_oauth_secret, "
        "abcd efgh ijkl mnop"
    )
    redacted = redact_sensitive_text(leaked)
    assert "secret_pw_999" not in redacted
    assert "ghp_1234567890abcdef1234" not in redacted
    assert "ya29.super_oauth_secret" not in redacted
    assert "abcd efgh ijkl mnop" not in redacted


def test_csv_export_formula_injection_sanitization(db: DatabaseManager) -> None:
    """Verify CSV ledger exports escape spreadsheet formula triggers while preserving numbers."""
    formula_payees = [
        "=SUM(A1:A5)",
        "+cmd|'/C calc'!A0",
        "-1+1",
        "@HYPERLINK(\"https://evil.test\",\"click\")",
        "\t=IMPORTXML(\"https://evil.test\",\"//a\")",
    ]
    db.upsert_many(
        [
            apply_rules(
                ParsedTransaction(
                    message_id=f"CSV_INJ_{idx}",
                    tx_date="2026-11-14",
                    account_name=WALLET_CASH,
                    tx_type="DEBIT",
                    amount=150.0 + idx,
                    payee=f_payee,
                    notes=f_payee,
                    source="MANUAL",
                ),
                db,
            )
            for idx, f_payee in enumerate(formula_payees)
        ]
    )

    csv_text = db.export_transactions_csv()
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert len(rows) == len(formula_payees)
    for row in rows:
        assert row["payee"].startswith("'")
        assert row["notes"].startswith("'")
        assert float(row["amount"]) >= 150.0


def test_same_source_minute_alert_dedup_and_auto_link_mirrors(
    db: DatabaseManager,
) -> None:
    """Same-minute :00 duplicate bank alerts merge; HabibMetro->NayaPay mirror credits auto-link without 2x inflation."""
    db.update_wallet_opening_balance(WALLET_HABIBMETRO, 50000.0)
    db.update_wallet_opening_balance(WALLET_NAYAPAY, 1000.0)

    # 1. Duplicate HabibMetro alert emails (:00 vs :26 in same minute)
    hmb_1 = TransactionRecord(
        message_id="XGM_HMB_DUP_1",
        tx_date="2026-11-21",
        tx_timestamp="2026-11-21 16:42:00",
        account_name=WALLET_HABIBMETRO,
        target_account=WALLET_NAYAPAY,
        tx_type="TRANSFER_INTERNAL",
        amount=4000.0,
        payee="MUHAMMAD MANQAD RAZA NayaPay",
        category="Wallet Top-Up",
        cost_bucket="IGNORE",
        review_status="AUTO_VERIFIED",
        is_internal_transfer=True,
        source="GMAIL_HMB",
    )
    hmb_2 = TransactionRecord(
        message_id="XGM_HMB_DUP_2",
        tx_date="2026-11-21",
        tx_timestamp="2026-11-21 16:42:26",
        account_name=WALLET_HABIBMETRO,
        target_account=WALLET_NAYAPAY,
        tx_type="TRANSFER_INTERNAL",
        amount=4000.0,
        payee="MUHAMMAD MANQAD RAZA NayaPay",
        category="Wallet Top-Up",
        cost_bucket="IGNORE",
        review_status="AUTO_VERIFIED",
        is_internal_transfer=True,
        source="GMAIL_HMB",
    )
    # 2. NayaPay's own incoming CREDIT email for that same 4,000 top-up
    np_credit = TransactionRecord(
        message_id="XGM_NP_MIRROR_1",
        tx_date="2026-11-21",
        tx_timestamp="2026-11-21 16:42:28",
        account_name=WALLET_NAYAPAY,
        tx_type="CREDIT",
        amount=4000.0,
        payee="Muhammad Manqad Raza",
        category="Other Inflow",
        cost_bucket="INFLOW",
        review_status="AUTO_VERIFIED",
        source="GMAIL_NAYAPAY",
    )
    ins, skip = db.upsert_many([hmb_1, hmb_2, np_credit])
    assert (ins, skip) == (2, 1)

    linked = db.auto_link_internal_transfer_mirrors(day_window=1)
    assert linked == 1
    assert db.get_wallet(WALLET_HABIBMETRO).current_balance == pytest.approx(46000.0)
    assert db.get_wallet(WALLET_NAYAPAY).current_balance == pytest.approx(5000.0)

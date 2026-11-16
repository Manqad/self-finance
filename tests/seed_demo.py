"""Idempotent Month 1 demo database seeder for Personal Treasury OS."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.database.db_manager import DatabaseManager
from src.engine.categorizer import apply_rules_batch
from src.engine.metrics import compute_dashboard_metrics
from src.engine.recurring import sync_recurring_status
from src.models import (
    WALLET_CASH,
    WALLET_HABIBMETRO,
    WALLET_MMF,
    WALLET_NAYAPAY,
    DashboardMetrics,
    ParsedTransaction,
    TransactionRecord,
)


def _build_demo_records(db: DatabaseManager) -> list[TransactionRecord]:
    """Construct realistic 2026-10 and 2026-11 Month 1 transactions across all 4 wallets."""
    rule_inputs: list[ParsedTransaction] = [
        # --- Prior Month (2026-10) for Month-over-Month Pace Baseline ---
        ParsedTransaction(
            message_id="DEMO_202610_SAL",
            tx_date="2026-10-01",
            tx_timestamp="2026-10-01 09:00:00",
            account_name=WALLET_HABIBMETRO,
            tx_type="CREDIT",
            amount=200000.0,
            payee="ACME TECH PAKISTAN - SALARY REMUNERATION",
            source="GMAIL_HMB",
            payment_channel="IBFT",
        ),
        ParsedTransaction(
            message_id="DEMO_202610_HOUSE",
            tx_date="2026-10-05",
            tx_timestamp="2026-10-05 11:15:00",
            account_name=WALLET_HABIBMETRO,
            tx_type="DEBIT",
            amount=35000.0,
            payee="HOUSE RENT CONTRIBUTION",
            source="GMAIL_HMB",
            payment_channel="IBFT",
        ),
        ParsedTransaction(
            message_id="DEMO_202610_KE",
            tx_date="2026-10-12",
            tx_timestamp="2026-10-12 14:20:00",
            account_name=WALLET_HABIBMETRO,
            tx_type="DEBIT",
            amount=13800.0,
            payee="K-ELECTRIC BILL PAYMENT",
            source="GMAIL_HMB",
            payment_channel="Bill Payment",
        ),
        ParsedTransaction(
            message_id="DEMO_202610_SF",
            tx_date="2026-10-14",
            tx_timestamp="2026-10-14 10:05:00",
            account_name=WALLET_HABIBMETRO,
            tx_type="DEBIT",
            amount=4500.0,
            payee="STORMFIBER INTERNET",
            source="GMAIL_HMB",
            payment_channel="Bill Payment",
        ),
        ParsedTransaction(
            message_id="DEMO_202610_IMTIAZ",
            tx_date="2026-10-15",
            tx_timestamp="2026-10-15 18:45:00",
            account_name=WALLET_HABIBMETRO,
            tx_type="DEBIT",
            amount=18400.0,
            payee="IMTIAZ SUPER MARKET",
            source="GMAIL_HMB",
            payment_channel="POS",
        ),
        ParsedTransaction(
            message_id="DEMO_202610_PSO",
            tx_date="2026-10-18",
            tx_timestamp="2026-10-18 17:10:00",
            account_name=WALLET_HABIBMETRO,
            tx_type="DEBIT",
            amount=7500.0,
            payee="PSO FUEL STATION",
            source="GMAIL_HMB",
            payment_channel="POS",
        ),
        ParsedTransaction(
            message_id="DEMO_202610_NP_TOPUP",
            tx_date="2026-10-20",
            tx_timestamp="2026-10-20 12:00:00",
            account_name=WALLET_HABIBMETRO,
            target_account=WALLET_NAYAPAY,
            tx_type="TRANSFER_INTERNAL",
            amount=25000.0,
            payee="NAYAPAY - DEMO ACCOUNT",
            source="GMAIL_HMB",
            payment_channel="IBFT",
        ),
        ParsedTransaction(
            message_id="DEMO_202610_FP",
            tx_date="2026-10-21",
            tx_timestamp="2026-10-21 20:30:00",
            account_name=WALLET_NAYAPAY,
            tx_type="DEBIT",
            amount=2400.0,
            payee="FOODPANDA PAKISTAN",
            source="GMAIL_NAYAPAY",
            payment_channel="Card",
        ),
        ParsedTransaction(
            message_id="DEMO_202610_NF",
            tx_date="2026-10-22",
            tx_timestamp="2026-10-22 10:30:00",
            account_name=WALLET_NAYAPAY,
            tx_type="DEBIT",
            amount=1100.0,
            tax_fee_amount=165.0,
            payee="NETFLIX.COM",
            source="GMAIL_NAYAPAY",
            payment_channel="Card",
            notes="Includes WHT Rs. 110 + FX Fee Rs. 55",
        ),
        ParsedTransaction(
            message_id="DEMO_202610_DARAZ",
            tx_date="2026-10-25",
            tx_timestamp="2026-10-25 15:20:00",
            account_name=WALLET_NAYAPAY,
            tx_type="DEBIT",
            amount=6800.0,
            payee="DARAZ.PK ORDER",
            source="GMAIL_NAYAPAY",
            payment_channel="Card",
        ),
        ParsedTransaction(
            message_id="DEMO_202610_MMF",
            tx_date="2026-10-28",
            tx_timestamp="2026-10-28 09:30:00",
            account_name=WALLET_HABIBMETRO,
            target_account=WALLET_MMF,
            tx_type="TRANSFER_MMF",
            amount=45000.0,
            payee="AL MEEZAN INVESTMENT - MRAF",
            source="GMAIL_HMB",
            payment_channel="IBFT",
        ),
        # --- Current Month (2026-11) ---
        ParsedTransaction(
            message_id="DEMO_202611_SAL",
            tx_date="2026-11-01",
            tx_timestamp="2026-11-01 09:05:00",
            account_name=WALLET_HABIBMETRO,
            tx_type="CREDIT",
            amount=200000.0,
            payee="ACME TECH PAKISTAN - SALARY REMUNERATION",
            source="GMAIL_HMB",
            payment_channel="IBFT",
        ),
        ParsedTransaction(
            message_id="DEMO_202611_HOUSE",
            tx_date="2026-11-02",
            tx_timestamp="2026-11-02 11:00:00",
            account_name=WALLET_HABIBMETRO,
            tx_type="DEBIT",
            amount=35000.0,
            payee="HOUSE RENT CONTRIBUTION",
            source="GMAIL_HMB",
            payment_channel="IBFT",
        ),
        ParsedTransaction(
            message_id="DEMO_202611_KE",
            tx_date="2026-11-03",
            tx_timestamp="2026-11-03 14:18:00",
            account_name=WALLET_HABIBMETRO,
            tx_type="DEBIT",
            amount=14500.0,
            payee="K-ELECTRIC",
            source="GMAIL_HMB",
            payment_channel="Bill Payment",
        ),
        ParsedTransaction(
            message_id="DEMO_202611_SF",
            tx_date="2026-11-04",
            tx_timestamp="2026-11-04 10:12:00",
            account_name=WALLET_HABIBMETRO,
            tx_type="DEBIT",
            amount=4500.0,
            payee="STORMFIBER",
            source="GMAIL_HMB",
            payment_channel="Bill Payment",
        ),
        ParsedTransaction(
            message_id="DEMO_202611_IMTIAZ",
            tx_date="2026-11-05",
            tx_timestamp="2026-11-05 19:20:00",
            account_name=WALLET_HABIBMETRO,
            tx_type="DEBIT",
            amount=16200.0,
            payee="IMTIAZ SUPER MARKET",
            source="GMAIL_HMB",
            payment_channel="POS",
        ),
        ParsedTransaction(
            message_id="DEMO_202611_PSO",
            tx_date="2026-11-06",
            tx_timestamp="2026-11-06 08:45:00",
            account_name=WALLET_HABIBMETRO,
            tx_type="DEBIT",
            amount=8200.0,
            payee="PSO PETROL PUMP",
            source="GMAIL_HMB",
            payment_channel="POS",
        ),
        ParsedTransaction(
            message_id="DEMO_202611_NP_TOPUP",
            tx_date="2026-11-06",
            tx_timestamp="2026-11-06 11:05:00",
            account_name=WALLET_HABIBMETRO,
            target_account=WALLET_NAYAPAY,
            tx_type="TRANSFER_INTERNAL",
            amount=22000.0,
            payee="NAYAPAY - DEMO ACCOUNT",
            source="GMAIL_HMB",
            payment_channel="IBFT",
        ),
        ParsedTransaction(
            message_id="DEMO_202611_ATM",
            tx_date="2026-11-07",
            tx_timestamp="2026-11-07 13:15:00",
            account_name=WALLET_HABIBMETRO,
            target_account=WALLET_CASH,
            tx_type="TRANSFER_INTERNAL",
            amount=10000.0,
            payee="ATM CASH WITHDRAWAL - CLIFTON",
            source="GMAIL_HMB",
            payment_channel="ATM",
        ),
        ParsedTransaction(
            message_id="DEMO_202611_MMF",
            tx_date="2026-11-07",
            tx_timestamp="2026-11-07 09:40:00",
            account_name=WALLET_HABIBMETRO,
            target_account=WALLET_MMF,
            tx_type="TRANSFER_MMF",
            amount=55000.0,
            payee="AL MEEZAN INVESTMENT - MRAF",
            source="GMAIL_HMB",
            payment_channel="IBFT",
        ),
        ParsedTransaction(
            message_id="DEMO_202611_FP",
            tx_date="2026-11-08",
            tx_timestamp="2026-11-08 20:15:00",
            account_name=WALLET_NAYAPAY,
            tx_type="DEBIT",
            amount=2850.0,
            payee="FOODPANDA PAKISTAN",
            source="GMAIL_NAYAPAY",
            payment_channel="Card",
        ),
        ParsedTransaction(
            message_id="DEMO_202611_NF",
            tx_date="2026-11-08",
            tx_timestamp="2026-11-08 10:30:00",
            account_name=WALLET_NAYAPAY,
            tx_type="DEBIT",
            amount=1100.0,
            tax_fee_amount=165.0,
            payee="NETFLIX.COM",
            source="GMAIL_NAYAPAY",
            payment_channel="Card",
            notes="Includes WHT Rs. 110 + FX Fee Rs. 55",
        ),
        ParsedTransaction(
            message_id="DEMO_202611_DARAZ",
            tx_date="2026-11-09",
            tx_timestamp="2026-11-09 16:40:00",
            account_name=WALLET_NAYAPAY,
            tx_type="DEBIT",
            amount=6450.0,
            payee="DARAZ.PK ONLINE",
            source="GMAIL_NAYAPAY",
            payment_channel="Card",
        ),
        ParsedTransaction(
            message_id="DEMO_202611_BYKEA",
            tx_date="2026-11-09",
            tx_timestamp="2026-11-09 09:10:00",
            account_name=WALLET_NAYAPAY,
            tx_type="DEBIT",
            amount=650.0,
            payee="BYKEA",
            source="GMAIL_NAYAPAY",
            payment_channel="Raast",
            notes="Raast IBAN: PK36MEZN0002110104567890",
        ),
        ParsedTransaction(
            message_id="DEMO_202611_FEES",
            tx_date="2026-11-10",
            tx_timestamp="2026-11-10 17:00:00",
            account_name=WALLET_HABIBMETRO,
            tx_type="DEBIT",
            amount=350.0,
            payee="BANK CHARGES - IBFT & SMS FEE",
            source="GMAIL_HMB",
            payment_channel="Bank Fee",
        ),
    ]

    records = apply_rules_batch(rule_inputs, db)

    # Explicit Cash Wallet entries + 2 realistic NEEDS_REVIEW queue items
    records.extend(
        [
            TransactionRecord(
                message_id="DEMO_202610_CASH_CHAI",
                tx_date="2026-10-27",
                tx_timestamp="2026-10-27 18:10:00",
                account_name=WALLET_CASH,
                tx_type="DEBIT",
                amount=4200.0,
                payee="Local Grocery & Chai",
                merchant_alias="Local Cash Purchases",
                category="Cash Envelope",
                cost_bucket="VARIABLE",
                review_status="MANUALLY_VERIFIED",
                source="MANUAL",
                payment_channel="Cash",
            ),
            TransactionRecord(
                message_id="DEMO_202611_CASH_SABZI",
                tx_date="2026-11-10",
                tx_timestamp="2026-11-10 11:30:00",
                account_name=WALLET_CASH,
                tx_type="DEBIT",
                amount=2150.0,
                payee="Fresh Sabzi & Fruit Market",
                merchant_alias="Weekly Sabzi Mandi",
                category="Cash Envelope",
                cost_bucket="VARIABLE",
                review_status="MANUALLY_VERIFIED",
                source="MANUAL",
                payment_channel="Cash",
            ),
            TransactionRecord(
                message_id="DEMO_202611_REV_COFFEE",
                tx_date="2026-11-10",
                tx_timestamp="2026-11-10 16:45:00",
                account_name=WALLET_NAYAPAY,
                tx_type="DEBIT",
                amount=1450.0,
                payee="Zameen Roastery",
                category="Uncategorized",
                cost_bucket="VARIABLE",
                review_status="NEEDS_REVIEW",
                source="GMAIL_NAYAPAY",
                payment_channel="Card",
                raw_subject="NayaPay Card Transaction - Zameen Roastery",
            ),
            TransactionRecord(
                message_id="DEMO_202611_REV_BILAL",
                tx_date="2026-11-11",
                tx_timestamp="2026-11-11 13:20:00",
                account_name=WALLET_NAYAPAY,
                tx_type="DEBIT",
                amount=3200.0,
                payee="Raast Transfer - Bilal Ahmed",
                category="Uncategorized",
                cost_bucket="VARIABLE",
                review_status="NEEDS_REVIEW",
                source="GMAIL_NAYAPAY",
                payment_channel="Raast",
                notes="Raast IBAN: PK49HABB0009876543210123",
                raw_subject="NayaPay Raast Money Sent - Bilal Ahmed",
            ),
        ]
    )
    return records


def seed_demo_database(
    db_path: str | Path = "data/treasury.db",
    reference_date: date | None = None,
    reset: bool = False,
) -> DashboardMetrics:
    """Idempotently seed Month 1 demo data and return computed metrics for 2026-11."""
    ref = reference_date or date(2026, 11, 11)
    month_key = ref.strftime("%Y-%m")

    with DatabaseManager(db_path) as db:
        if reset:
            with db._lock, db.conn:
                for tbl in (
                    "transactions",
                    "category_rules",
                    "recurring_commitments",
                    "category_budgets",
                    "budget_config",
                    "accounts",
                ):
                    db.conn.execute(f"DELETE FROM {tbl}")
                has_seq = db.conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
                ).fetchone()
                if has_seq:
                    db.conn.execute("DELETE FROM sqlite_sequence")
            db.init_db()

        db.update_budget_config(
            monthly_income=200000.0,
            fixed_cost_baseline=80000.0,
            variable_spending_cap=40000.0,
            emergency_multiplier=6,
            envelope_mode=False,
        )
        db.update_wallet_opening_balance(WALLET_HABIBMETRO, 140000.0)
        db.update_wallet_opening_balance(WALLET_NAYAPAY, 8500.0)
        db.update_wallet_opening_balance(WALLET_CASH, 6000.0)
        db.update_wallet_opening_balance(WALLET_MMF, 180000.0)

        records = _build_demo_records(db)
        inserted, skipped = db.upsert_many(records)
        db.recalculate_balances()
        sync_recurring_status(db, month_key, reference_date=ref)
        metrics = compute_dashboard_metrics(db, month_key, reference_date=ref)

        print(f"Seeded {db_path}: inserted={inserted}, skipped={skipped}")
        print("\nWallet Balances:")
        for w in metrics.wallet_balances:
            print(
                f"  - {w.account_name} ({w.account_type}): "
                f"opening=Rs. {w.opening_balance:,.2f}, current=Rs. {w.current_balance:,.2f}"
            )
        print(f"\nMonth 1 Metrics ({metrics.month_key}):")
        print(f"  - Total Liquid Balance: Rs. {metrics.total_liquid_balance:,.2f}")
        print(f"  - Total Net Worth:      Rs. {metrics.total_net_worth:,.2f}")
        print(f"  - Total Spending Cap:   Rs. {metrics.total_spending_cap:,.2f}")
        print(f"  - Fixed Burn:           Rs. {metrics.actual_fixed_burn:,.2f}")
        print(f"  - Variable Burn:        Rs. {metrics.actual_variable_burn:,.2f}")
        print(f"  - Leakage & Tax Burn:   Rs. {metrics.actual_leakage_burn:,.2f} (tax_fees={metrics.total_tax_fees:,.2f})")
        print(f"  - Total Monthly Burn:   Rs. {metrics.total_monthly_burn:,.2f} ({metrics.burn_utilization_pct:.2f}%)")
        print(f"  - Remaining Allowance:  Rs. {metrics.remaining_allowance:,.2f}")
        print(f"  - Safe Daily Spend:     Rs. {metrics.safe_daily_spend:,.2f}/day ({metrics.days_remaining} days left)")
        print(f"  - Daily Burn Pace:      Rs. {metrics.actual_daily_burn_pace:,.2f}/day (MoM: {metrics.mom_pace_delta_pct:+.2f}%)")
        print(f"  - Emergency Reserve:    Rs. {metrics.emergency_current:,.2f} / Rs. {metrics.emergency_target:,.2f} ({metrics.emergency_progress_pct:.2f}%)")
        print(f"  - Needs Review Count:   {metrics.needs_review_count}")
        print(f"  - Unpaid Bills Total:   Rs. {metrics.unpaid_bills_total:,.2f}")
        return metrics


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed Month 1 Personal Treasury OS demo DB.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear existing tables and re-seed default rules, commitments, and demo records.",
    )
    args = parser.parse_args()
    seed_demo_database(reset=args.reset)

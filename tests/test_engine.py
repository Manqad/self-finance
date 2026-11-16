"""Financial rule engine, recurring detector, and SPEC.md math engine tests."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from src.database.db_manager import DatabaseManager
from src.engine.categorizer import apply_rules, learn_new_rule
from src.engine.metrics import compute_dashboard_metrics
from src.engine.recurring import sync_recurring_status
from src.models import (
    WALLET_CASH,
    WALLET_HABIBMETRO,
    WALLET_MMF,
    WALLET_NAYAPAY,
    ParsedTransaction,
    TransactionRecord,
)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    """Provide a fresh isolated DatabaseManager."""
    manager = DatabaseManager(tmp_path / "test_engine.db")
    try:
        yield manager
    finally:
        manager.close()


def _parsed(
    msg_id: str,
    payee: str,
    amount: float,
    *,
    tx_date: str = "2026-11-10",
    account_name: str = WALLET_NAYAPAY,
    tx_type: str = "DEBIT",
    tax_fee_amount: float = 0.0,
    payment_channel: str | None = None,
    target_account: str | None = None,
    notes: str | None = None,
) -> ParsedTransaction:
    return ParsedTransaction(
        message_id=msg_id,
        tx_date=tx_date,
        account_name=account_name,
        target_account=target_account,
        tx_type=tx_type,  # type: ignore[arg-type]
        amount=amount,
        tax_fee_amount=tax_fee_amount,
        payee=payee,
        source="GMAIL_NAYAPAY" if account_name == WALLET_NAYAPAY else "GMAIL_HMB",
        payment_channel=payment_channel,
        notes=notes,
    )


def test_apply_rules_pakistani_merchants_and_needs_review(db: DatabaseManager) -> None:
    """Verify deterministic Pakistani merchant rules, transfer classification, and NEEDS_REVIEW fallback."""
    ke_rec = apply_rules(_parsed("R1", "K-ELECTRIC", 14500.0, account_name=WALLET_HABIBMETRO), db)
    assert ke_rec.category == "Bills & Utilities"
    assert ke_rec.cost_bucket == "FIXED"
    assert ke_rec.review_status == "AUTO_VERIFIED"

    pso_rec = apply_rules(_parsed("R2", "PSO PETROL STATION", 6000.0, account_name=WALLET_HABIBMETRO), db)
    assert pso_rec.category == "Fuel & Commute"
    assert pso_rec.cost_bucket == "FIXED"

    imtiaz_rec = apply_rules(_parsed("R3", "IMTIAZ SUPER MARKET", 16000.0, account_name=WALLET_HABIBMETRO), db)
    assert imtiaz_rec.category == "Groceries"
    assert imtiaz_rec.cost_bucket == "FIXED"

    fp_rec = apply_rules(_parsed("R4", "FOODPANDA PAKISTAN", 1850.0), db)
    assert fp_rec.category == "Food & Dining"
    assert fp_rec.cost_bucket == "VARIABLE"
    assert fp_rec.merchant_alias == "Foodpanda"

    nf_rec = apply_rules(_parsed("R5", "NETFLIX.COM", 1100.0, tax_fee_amount=165.0), db)
    assert nf_rec.category == "Subscriptions & Tech"
    assert nf_rec.cost_bucket == "VARIABLE"

    wht_rec = apply_rules(_parsed("R6", "WITHHOLDING TAX DEDUCTION", 350.0, account_name=WALLET_HABIBMETRO), db)
    assert wht_rec.category == "Bank & Tax Friction"
    assert wht_rec.cost_bucket == "LEAKAGE"

    # HMB -> NayaPay DEBIT auto-converts to TRANSFER_INTERNAL / IGNORE
    np_topup = apply_rules(
        _parsed("R7", "NAYAPAY - DEMO ACCOUNT", 20000.0, account_name=WALLET_HABIBMETRO),
        db,
    )
    assert np_topup.tx_type == "TRANSFER_INTERNAL"
    assert np_topup.target_account == WALLET_NAYAPAY
    assert np_topup.cost_bucket == "IGNORE"
    assert np_topup.is_internal_transfer is True
    assert np_topup.review_status == "AUTO_VERIFIED"

    # HMB -> Al Meezan DEBIT auto-converts to TRANSFER_MMF / INVESTMENT
    mmf_sweep = apply_rules(
        _parsed("R8", "AL MEEZAN INVESTMENT - MRAF", 55000.0, account_name=WALLET_HABIBMETRO),
        db,
    )
    assert mmf_sweep.tx_type == "TRANSFER_MMF"
    assert mmf_sweep.target_account == WALLET_MMF
    assert mmf_sweep.cost_bucket == "INVESTMENT"
    assert mmf_sweep.review_status == "AUTO_VERIFIED"

    # Salary CREDIT -> INFLOW
    sal_rec = apply_rules(
        _parsed("R9", "MONTHLY SALARY CREDIT", 200000.0, account_name=WALLET_HABIBMETRO, tx_type="CREDIT"),
        db,
    )
    assert sal_rec.category == "Salary & Income"
    assert sal_rec.cost_bucket == "INFLOW"

    # Unknown payee -> NEEDS_REVIEW (including NayaPay-account debits containing 'NayaPay')
    unk_1 = apply_rules(_parsed("R10", "Raast Transfer - Bilal Ahmed", 3200.0), db)
    assert unk_1.category == "Uncategorized"
    assert unk_1.review_status == "NEEDS_REVIEW"
    assert unk_1.cost_bucket == "VARIABLE"

    unk_2 = apply_rules(_parsed("R11", "NayaPay P2P - Tariq Mahmood", 1500.0), db)
    assert unk_2.category == "Uncategorized"
    assert unk_2.review_status == "NEEDS_REVIEW"


def test_learn_new_rule_and_retroactive_updates(db: DatabaseManager) -> None:
    """Verify learn_new_rule saves a priority-100 rule and updates historical matching rows."""
    t1 = apply_rules(_parsed("LRN_1", "Raast Transfer - Bilal Ahmed", 2500.0, tx_date="2026-10-12"), db)
    t2 = apply_rules(_parsed("LRN_2", "Raast Transfer - Bilal Ahmed", 3200.0, tx_date="2026-11-08"), db)
    t3 = apply_rules(_parsed("LRN_3", "Sana Boutique Karachi", 4800.0, tx_date="2026-11-09"), db)
    db.upsert_many([t1, t2, t3])
    assert len(db.get_transactions(review_status="NEEDS_REVIEW")) == 3

    updated_count = learn_new_rule(
        db=db,
        payee="Raast Transfer - Bilal Ahmed",
        alias="Bilal (House Utilities)",
        category="Housing & Rent",
        cost_bucket="FIXED",
        keyword="Bilal Ahmed",
    )
    assert updated_count == 2

    rules = db.get_category_rules()
    learned = next((r for r in rules if r.keyword == "Bilal Ahmed"), None)
    assert learned is not None
    assert learned.priority == 100
    assert learned.is_user_defined is True
    assert learned.category == "Housing & Rent"
    assert learned.cost_bucket == "FIXED"

    all_txs = {tx.message_id: tx for tx in db.get_transactions()}
    for mid in ("LRN_1", "LRN_2"):
        assert all_txs[mid].category == "Housing & Rent"
        assert all_txs[mid].cost_bucket == "FIXED"
        assert all_txs[mid].merchant_alias == "Bilal (House Utilities)"
        assert all_txs[mid].review_status == "MANUALLY_VERIFIED"

    # Unrelated transaction remains untouched
    assert all_txs["LRN_3"].category == "Uncategorized"
    assert all_txs["LRN_3"].review_status == "NEEDS_REVIEW"

    # Future transaction matching learned rule is categorized automatically
    future = apply_rules(_parsed("LRN_4", "Raast - BILAL AHMED", 1900.0, tx_date="2026-11-11"), db)
    assert future.category == "Housing & Rent"
    assert future.cost_bucket == "FIXED"
    assert future.review_status == "AUTO_VERIFIED"


def test_recurring_commitment_detection(db: DatabaseManager) -> None:
    """Verify sync_recurring_status marks paid bills for the selected month and leaves unpaid ones open."""
    db.upsert_many(
        [
            apply_rules(_parsed("REC_1", "K-ELECTRIC", 14500.0, tx_date="2026-11-05", account_name=WALLET_HABIBMETRO), db),
            apply_rules(_parsed("REC_2", "STORMFIBER", 4500.0, tx_date="2026-11-06", account_name=WALLET_HABIBMETRO), db),
            apply_rules(_parsed("REC_3", "NETFLIX.COM", 1100.0, tx_date="2026-11-10", tax_fee_amount=165.0), db),
        ]
    )

    statuses_nov = sync_recurring_status(db, "2026-11")
    by_title = {s.commitment.title: s for s in statuses_nov}

    assert by_title["K-Electric Bill"].is_paid is True
    assert by_title["K-Electric Bill"].paid_amount == pytest.approx(14500.0)
    assert by_title["K-Electric Bill"].paid_date == "2026-11-05"
    assert by_title["StormFiber Internet"].is_paid is True
    assert by_title["Netflix"].is_paid is True
    assert by_title["House Contribution"].is_paid is False
    assert by_title["Spotify"].is_paid is False

    # DB rows synced
    db_recs = {r.title: r for r in db.get_recurring_commitments()}
    assert db_recs["K-Electric Bill"].is_paid_this_month is True
    assert db_recs["K-Electric Bill"].last_paid_month == "2026-11"
    assert db_recs["Spotify"].is_paid_this_month is False

    # Switching to an unpaid month resets is_paid_this_month
    statuses_dec = sync_recurring_status(db, "2026-12")
    assert all(not s.is_paid for s in statuses_dec)


def test_compute_dashboard_metrics_all_8_equations_no_transfer_inflation(db: DatabaseManager) -> None:
    """Verify all 8 SPEC.md equations and prove internal/MMF transfers never inflate burn."""
    db.update_budget_config(
        monthly_income=200000.0,
        fixed_cost_baseline=80000.0,
        variable_spending_cap=40000.0,
        emergency_multiplier=6,
        envelope_mode=False,
    )
    db.update_wallet_opening_balance(WALLET_HABIBMETRO, 100000.0)
    db.update_wallet_opening_balance(WALLET_NAYAPAY, 5000.0)
    db.update_wallet_opening_balance(WALLET_CASH, 2000.0)
    db.update_wallet_opening_balance(WALLET_MMF, 180000.0)

    db.upsert_many(
        [
            apply_rules(_parsed("M_SAL", "ACME SALARY", 200000.0, tx_date="2026-11-01", account_name=WALLET_HABIBMETRO, tx_type="CREDIT"), db),
            apply_rules(_parsed("M_HOUSE", "HOUSE RENT CONTRIBUTION", 35000.0, tx_date="2026-11-02", account_name=WALLET_HABIBMETRO), db),
            apply_rules(_parsed("M_KE", "K-ELECTRIC", 14500.0, tx_date="2026-11-05", account_name=WALLET_HABIBMETRO), db),
            apply_rules(_parsed("M_SF", "STORMFIBER", 4500.0, tx_date="2026-11-06", account_name=WALLET_HABIBMETRO), db),
            # Internal transfers & MMF sweep must NOT inflate monthly burn
            apply_rules(_parsed("M_TOPUP", "NAYAPAY - DEMO ACCOUNT", 20000.0, tx_date="2026-11-06", account_name=WALLET_HABIBMETRO), db),
            TransactionRecord(
                message_id="M_ATM",
                tx_date="2026-11-07",
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
            apply_rules(_parsed("M_MMF", "AL MEEZAN INVESTMENT - MRAF", 55000.0, tx_date="2026-11-07", account_name=WALLET_HABIBMETRO), db),
            # Variable & leakage debits
            apply_rules(_parsed("M_FP", "FOODPANDA PAKISTAN", 1850.0, tx_date="2026-11-08"), db),
            apply_rules(_parsed("M_NF", "NETFLIX.COM", 1100.0, tx_date="2026-11-10", tax_fee_amount=165.0), db),
            apply_rules(_parsed("M_FEE", "BANK CHARGES IBFT", 350.0, tx_date="2026-11-10", account_name=WALLET_HABIBMETRO), db),
        ]
    )

    metrics = compute_dashboard_metrics(db, "2026-11", reference_date=date(2026, 11, 11))

    # Eq 1: C_total = fixed_cost_baseline + variable_spending_cap
    assert metrics.total_spending_cap == pytest.approx(120000.0)
    # Eq 2: B_fixed = 35,000 + 14,500 + 4,500 = 54,000
    assert metrics.actual_fixed_burn == pytest.approx(54000.0)
    # Eq 3: B_var = 1,850 + 1,100 = 2,950 (excludes 20k top-up, 10k ATM, 55k MMF)
    assert metrics.actual_variable_burn == pytest.approx(2950.0)
    # Eq 4: B_leak = 350 + 165 = 515 (no double-counting of Netflix WHT+FX)
    assert metrics.total_tax_fees == pytest.approx(165.0)
    assert metrics.actual_leakage_burn == pytest.approx(515.0)
    # Eq 5: B_total = B_fixed + B_var + B_leak
    assert metrics.total_monthly_burn == pytest.approx(57465.0)
    # Eq 6: A_rem = C_total - B_total
    assert metrics.remaining_allowance == pytest.approx(62535.0)
    # Eq 7: D_safe = max(0, A_rem / max(1, days_remaining))
    assert metrics.days_in_month == 30
    assert metrics.days_elapsed == 11
    assert metrics.days_remaining == 20
    assert metrics.safe_daily_spend == pytest.approx(round(62535.0 / 20.0, 2))
    # Eq 8: Emergency Reserve Progress
    assert metrics.emergency_target == pytest.approx(480000.0)
    assert metrics.emergency_current == pytest.approx(235000.0)  # 180k + 55k
    assert metrics.emergency_progress_pct == pytest.approx(round(235000.0 / 480000.0 * 100.0, 2))

    # Auxiliary checks
    assert metrics.monthly_mmf_swept == pytest.approx(55000.0)
    assert metrics.actual_inflow == pytest.approx(200000.0)
    assert metrics.unallocated_cash_surplus == pytest.approx(25000.0)
    assert metrics.daily_cumulative_spend[-1].cumulative_spend == pytest.approx(metrics.total_monthly_burn)


def test_envelope_mode_true_vs_false(db: DatabaseManager) -> None:
    """Verify envelope_mode=True counts HMB->NayaPay top-up and excludes NayaPay debits."""
    db.upsert_many(
        [
            apply_rules(_parsed("ENV_TOP", "NAYAPAY - DEMO ACCOUNT", 20000.0, tx_date="2026-11-06", account_name=WALLET_HABIBMETRO), db),
            apply_rules(_parsed("ENV_FP", "FOODPANDA", 1850.0, tx_date="2026-11-08", account_name=WALLET_NAYAPAY), db),
            apply_rules(_parsed("ENV_NF", "NETFLIX.COM", 1100.0, tx_date="2026-11-10", account_name=WALLET_NAYAPAY, tax_fee_amount=165.0), db),
            apply_rules(_parsed("ENV_FEE", "BANK CHARGES", 350.0, tx_date="2026-11-10", account_name=WALLET_HABIBMETRO), db),
        ]
    )

    db.update_budget_config(envelope_mode=False)
    m_granular = compute_dashboard_metrics(db, "2026-11", reference_date=date(2026, 11, 11))
    assert m_granular.actual_variable_burn == pytest.approx(2950.0)
    assert m_granular.actual_leakage_burn == pytest.approx(515.0)
    assert m_granular.total_monthly_burn == pytest.approx(3465.0)

    db.update_budget_config(envelope_mode=True)
    m_envelope = compute_dashboard_metrics(db, "2026-11", reference_date=date(2026, 11, 11))
    assert m_envelope.actual_variable_burn == pytest.approx(20000.0)
    assert m_envelope.total_tax_fees == pytest.approx(0.0)
    assert m_envelope.actual_leakage_burn == pytest.approx(350.0)
    assert m_envelope.total_monthly_burn == pytest.approx(20350.0)


def test_category_threshold_colors_top_merchants_and_mom_pace(db: DatabaseManager) -> None:
    """Verify green/amber/red budget thresholds, top 5 merchants ranking, and MoM pace delta."""
    # Limits: Groceries=25,000, Bills & Utilities=25,000, Shopping=10,000, Food & Dining=15,000
    # Prior month (2026-10, 31 days): 31,000 burn -> 1,000.00/day
    db.upsert_many(
        [
            apply_rules(_parsed("OCT_1", "IMTIAZ", 31000.0, tx_date="2026-10-15", account_name=WALLET_HABIBMETRO), db),
            # Current month (2026-11, ref day 11):
            # Groceries: 10,000 / 25,000 = 40% -> green (<75%)
            apply_rules(_parsed("NOV_1", "IMTIAZ", 10000.0, tx_date="2026-11-02", account_name=WALLET_HABIBMETRO), db),
            # Bills & Utilities: 20,000 / 25,000 = 80% -> amber (75-95%)
            apply_rules(_parsed("NOV_2", "K-ELECTRIC", 15500.0, tx_date="2026-11-03", account_name=WALLET_HABIBMETRO), db),
            apply_rules(_parsed("NOV_3", "STORMFIBER", 4500.0, tx_date="2026-11-04", account_name=WALLET_HABIBMETRO), db),
            # Shopping: 11,000 / 10,000 = 110% -> red (>95%)
            apply_rules(_parsed("NOV_4", "DARAZ.PK", 11000.0, tx_date="2026-11-05"), db),
            # Extra merchants to test top-5 truncation
            apply_rules(_parsed("NOV_5", "PSO FUEL", 7000.0, tx_date="2026-11-06", account_name=WALLET_HABIBMETRO), db),
            apply_rules(_parsed("NOV_6", "FOODPANDA", 2500.0, tx_date="2026-11-07"), db),
            apply_rules(_parsed("NOV_7", "BYKEA", 900.0, tx_date="2026-11-08"), db),
        ]
    )

    metrics = compute_dashboard_metrics(db, "2026-11", reference_date=date(2026, 11, 11))
    cat_map = {cp.category: cp for cp in metrics.category_progress}

    assert cat_map["Groceries"].utilization_pct == pytest.approx(40.0)
    assert cat_map["Groceries"].status_color == "green"
    assert cat_map["Bills & Utilities"].utilization_pct == pytest.approx(80.0)
    assert cat_map["Bills & Utilities"].status_color == "amber"
    assert cat_map["Shopping"].utilization_pct == pytest.approx(110.0)
    assert cat_map["Shopping"].status_color == "red"

    # Top 5 merchants sorted descending by total_spent
    assert len(metrics.top_merchants) == 5
    assert [m.merchant for m in metrics.top_merchants] == [
        "K-Electric",
        "Daraz.pk",
        "Imtiaz Super Market",
        "PSO Fuel",
        "StormFiber",
    ]
    assert metrics.top_merchants[0].total_spent == pytest.approx(15500.0)

    # MoM daily burn pace: Nov burn = 51,400 over 11 elapsed days = 4,672.73/day vs Oct 1,000.00/day
    assert metrics.prev_month_daily_burn_pace == pytest.approx(1000.0)
    assert metrics.actual_daily_burn_pace == pytest.approx(round(51400.0 / 11.0, 2))
    expected_delta = round(((metrics.actual_daily_burn_pace - 1000.0) / 1000.0) * 100.0, 2)
    assert metrics.mom_pace_delta_pct == pytest.approx(expected_delta)


def test_seed_demo_database_idempotent(tmp_path: Path) -> None:
    """Verify seed_demo_database seeds all 4 wallets cleanly and is idempotent."""
    from tests.seed_demo import seed_demo_database

    demo_db = tmp_path / "demo_treasury.db"
    m1 = seed_demo_database(demo_db, reference_date=date(2026, 11, 11))
    m2 = seed_demo_database(demo_db, reference_date=date(2026, 11, 11))

    assert m1.needs_review_count == 2
    assert m1.total_monthly_burn == pytest.approx(m2.total_monthly_burn)
    assert [w.current_balance for w in m1.wallet_balances] == pytest.approx(
        [w.current_balance for w in m2.wallet_balances]
    )
    assert all(w.current_balance > 0 for w in m1.wallet_balances)
    assert m1.prev_month_daily_burn_pace > 0

    mgr = DatabaseManager(demo_db)
    try:
        zameen = next(
            tx for tx in mgr.get_transactions() if tx.message_id == "DEMO_202611_REV_COFFEE"
        )
        assert zameen.payee == "Zameen Roastery"
        assert zameen.category == "Uncategorized"
        assert zameen.review_status == "NEEDS_REVIEW"
        re_evaluated = apply_rules(zameen, mgr)
        assert re_evaluated.category == "Uncategorized"
        assert re_evaluated.review_status == "NEEDS_REVIEW"
    finally:
        mgr.close()


def test_categorizer_adversarial_false_positive_and_boundary_guards(
    db: DatabaseManager,
) -> None:
    """Verify Round 1 word-boundary and semantic disambiguation guards in categorizer.py."""
    # Non-MMF banks & unrelated corporate acronyms must NEVER convert to TRANSFER_MMF
    for idx, payee in enumerate(
        ("Meezan Bank", "Bank Alfalah", "MRAF TRADERS", "MCF FOUNDATION"),
        start=1,
    ):
        rec = apply_rules(
            _parsed(f"MMF_NEG_{idx}", payee, 2500.0, account_name=WALLET_HABIBMETRO),
            db,
        )
        assert rec.tx_type == "DEBIT", payee
        assert rec.cost_bucket != "INVESTMENT", payee
        assert rec.category == "Uncategorized", payee
        assert rec.review_status == "NEEDS_REVIEW", payee

    # Substring & disambiguation traps must NOT misclassify
    ke_trap = apply_rules(_parsed("TRAP_KE", "Biryani Ke Deewane", 1400.0), db)
    assert ke_trap.category != "Bills & Utilities"

    shell_trap = apply_rules(_parsed("TRAP_SHELL", "SEASHELL RESTAURANT", 3800.0), db)
    assert shell_trap.category != "Fuel & Commute"

    steam_trap = apply_rules(_parsed("TRAP_STEAM", "GULFAM STEAM", 1200.0), db)
    assert steam_trap.category != "Subscriptions & Tech"

    apple_trap = apply_rules(_parsed("TRAP_APPLE", "GREEN APPLE CAFE", 1900.0), db)
    assert apple_trap.category != "Subscriptions & Tech"

    aj_trap = apply_rules(_parsed("TRAP_AJ", "A.J. MOTORS", 5500.0), db)
    assert aj_trap.category != "Shopping"

    fed_trap = apply_rules(_parsed("TRAP_FED", "FED UP BURGERS", 2200.0), db)
    assert fed_trap.category != "Bank & Tax Friction"
    assert fed_trap.cost_bucket != "LEAKAGE"

    profit_debit = apply_rules(
        _parsed("TRAP_PROF_D", "DONATION NON-PROFIT TRUST", 4000.0, tx_type="DEBIT"),
        db,
    )
    assert profit_debit.category == "Uncategorized"
    assert profit_debit.cost_bucket == "VARIABLE"
    assert profit_debit.review_status == "NEEDS_REVIEW"

    for ch_idx, ch in enumerate((None, "Raast"), start=1):
        raast_imtiaz = apply_rules(
            _parsed(
                f"TRAP_IMTIAZ_{ch_idx}",
                "Raast - Imtiaz Ali",
                7500.0,
                payment_channel=ch,
            ),
            db,
        )
        assert raast_imtiaz.category == "Uncategorized"
        assert raast_imtiaz.review_status == "NEEDS_REVIEW"

    np_agent = apply_rules(
        _parsed("TRAP_NP_AGENT", "NAYAPAY CASH AGENT", 3000.0, account_name=WALLET_NAYAPAY),
        db,
    )
    assert np_agent.tx_type == "DEBIT"
    assert np_agent.is_internal_transfer is False
    assert np_agent.category == "Uncategorized"
    assert np_agent.review_status == "NEEDS_REVIEW"

    # Expected valid medical & cafeteria matches
    akh = apply_rules(
        _parsed("POS_AKH", "AGA KHAN HOSPITAL", 8500.0, account_name=WALLET_HABIBMETRO),
        db,
    )
    assert akh.category == "Healthcare"
    assert akh.cost_bucket == "FIXED"
    assert akh.review_status == "AUTO_VERIFIED"

    cafe = apply_rules(_parsed("POS_CAFE", "OFFICE CAFETERIA", 950.0), db)
    assert cafe.category == "Food & Dining"
    assert cafe.cost_bucket == "VARIABLE"
    assert cafe.review_status == "AUTO_VERIFIED"

    # CREDIT to DONATION NON-PROFIT TRUST must not falsely match PROFIT / Salary & Income
    profit_credit = apply_rules(
        _parsed("TRAP_PROF_C", "DONATION NON-PROFIT TRUST", 4000.0, tx_type="CREDIT"),
        db,
    )
    assert profit_credit.category == "Uncategorized"
    assert profit_credit.cost_bucket == "INFLOW"
    assert profit_credit.review_status == "NEEDS_REVIEW"


def test_categorizer_leakage_and_priority100_override_hmb_nayapay(
    db: DatabaseManager,
) -> None:
    """Verify LEAKAGE rules and priority-100 user rules supersede HMB->NayaPay promotion."""
    wht_np = apply_rules(
        _parsed("HMB_WHT_NP", "WHT ON NAYAPAY TRANSFER", 150.0, account_name=WALLET_HABIBMETRO),
        db,
    )
    assert wht_np.tx_type == "DEBIT"
    assert wht_np.target_account is None
    assert wht_np.is_internal_transfer is False
    assert wht_np.category == "Bank & Tax Friction"
    assert wht_np.cost_bucket == "LEAKAGE"

    # Priority-100 user rule explicitly overrides HMB -> NayaPay internal transfer promotion
    learn_new_rule(
        db=db,
        payee="NAYAPAY BIZ FEE",
        alias="NayaPay Merchant Fee",
        category="Subscriptions & Tech",
        cost_bucket="VARIABLE",
        keyword="NAYAPAY BIZ FEE",
    )
    overridden = apply_rules(
        _parsed("HMB_P100_NP", "NAYAPAY BIZ FEE", 1200.0, account_name=WALLET_HABIBMETRO),
        db,
    )
    assert overridden.tx_type == "DEBIT"
    assert overridden.target_account is None
    assert overridden.is_internal_transfer is False
    assert overridden.category == "Subscriptions & Tech"
    assert overridden.cost_bucket == "VARIABLE"
    assert overridden.review_status == "AUTO_VERIFIED"


def test_learn_new_rule_preserves_credit_inflow_manual_verified_and_upgrades_investment(
    db: DatabaseManager,
) -> None:
    """Verify learn_new_rule preserves CREDIT INFLOW, skips MANUALLY_VERIFIED unless forced, and upgrades INVESTMENT."""
    credit_row = apply_rules(
        _parsed("LNP_CRED", "ACME PARTNER PAYOUT", 50000.0, account_name=WALLET_HABIBMETRO, tx_type="CREDIT"),
        db,
    )
    manual_row = TransactionRecord(
        message_id="LNP_MAN",
        tx_date="2026-11-05",
        account_name=WALLET_NAYAPAY,
        tx_type="DEBIT",
        amount=3000.0,
        payee="ACME PARTNER FEE",
        category="Shopping",
        cost_bucket="VARIABLE",
        review_status="MANUALLY_VERIFIED",
        source="MANUAL",
    )
    unverified_inv = apply_rules(
        _parsed("LNP_INV", "JS TREASURY FUND PLAN", 25000.0, account_name=WALLET_HABIBMETRO),
        db,
    )
    db.upsert_many([credit_row, manual_row, unverified_inv])

    # 1. Learning a VARIABLE rule matching CREDIT + MANUALLY_VERIFIED must keep CREDIT INFLOW & skip manual row
    updated = learn_new_rule(
        db=db,
        payee="ACME PARTNER",
        alias="Acme Corp",
        category="Food & Dining",
        cost_bucket="VARIABLE",
        keyword="ACME PARTNER",
        overwrite_verified=False,
    )
    assert updated == 1

    rows = {r.message_id: r for r in db.get_transactions()}
    assert rows["LNP_CRED"].cost_bucket == "INFLOW"
    assert rows["LNP_CRED"].category == "Food & Dining"
    assert rows["LNP_CRED"].merchant_alias == "Acme Corp"
    assert rows["LNP_MAN"].category == "Shopping"
    assert rows["LNP_MAN"].review_status == "MANUALLY_VERIFIED"

    # overwrite_verified=True updates the MANUALLY_VERIFIED row as well
    updated_forced = learn_new_rule(
        db=db,
        payee="ACME PARTNER",
        alias="Acme Corp",
        category="Food & Dining",
        cost_bucket="VARIABLE",
        keyword="ACME PARTNER",
        overwrite_verified=True,
    )
    assert updated_forced == 2
    rows_after = {r.message_id: r for r in db.get_transactions()}
    assert rows_after["LNP_MAN"].category == "Food & Dining"

    # 2. Learning an INVESTMENT rule upgrades historical DEBIT to TRANSFER_MMF and recalculates wallets
    inv_updated = learn_new_rule(
        db=db,
        payee="JS TREASURY FUND PLAN",
        alias="JS Money Market",
        category="Money Market Fund",
        cost_bucket="INVESTMENT",
        keyword="JS TREASURY",
    )
    assert inv_updated == 1
    rows_inv = {r.message_id: r for r in db.get_transactions()}
    upgraded = rows_inv["LNP_INV"]
    assert upgraded.tx_type == "TRANSFER_MMF"
    assert upgraded.target_account == WALLET_MMF
    assert upgraded.cost_bucket == "INVESTMENT"
    assert upgraded.category == "Money Market Fund"

    wallets = {w.account_name: w.current_balance for w in db.get_wallets()}
    assert wallets[WALLET_MMF] == pytest.approx(25000.0)


def test_recurring_coffee_house_and_historical_month_non_mutation(
    db: DatabaseManager,
) -> None:
    """Verify 'Coffee House' does not settle House Contribution and historical view does not mutate DB flags."""
    # Seed Coffee House and House Rent + K-Electric in Nov 2026
    coffee = apply_rules(_parsed("REC_CH", "Coffee House", 1200.0, tx_date="2026-11-03"), db)
    db.upsert_many([coffee])

    statuses_before = sync_recurring_status(db, "2026-11", reference_date=date(2026, 11, 11))
    house_before = next(s for s in statuses_before if s.commitment.title == "House Contribution")
    assert house_before.is_paid is False

    # Now pay House Rent in 2026-11
    rent = apply_rules(
        _parsed("REC_RENT", "HOUSE RENT TRANSFER", 35000.0, tx_date="2026-11-05", account_name=WALLET_HABIBMETRO),
        db,
    )
    db.upsert_many([rent])

    statuses_nov = sync_recurring_status(db, "2026-11", reference_date=date(2026, 11, 11))
    house_nov = next(s for s in statuses_nov if s.commitment.title == "House Contribution")
    assert house_nov.is_paid is True

    # Viewing historical month 2026-10 (where nothing was paid) must NOT overwrite persisted 2026-11 flags
    statuses_oct = sync_recurring_status(db, "2026-10", reference_date=date(2026, 11, 11))
    house_oct = next(s for s in statuses_oct if s.commitment.title == "House Contribution")
    assert house_oct.is_paid is False

    compute_dashboard_metrics(db, "2026-10", reference_date=date(2026, 11, 11))
    persisted_house = next(
        c for c in db.get_recurring_commitments() if c.title == "House Contribution"
    )
    assert persisted_house.is_paid_this_month is True
    assert persisted_house.last_paid_month == "2026-11"


def test_metrics_envelope_return_netting_zero_cap_inflow_exclusion_and_calendar_edges(
    db: DatabaseManager,
) -> None:
    """Verify envelope return netting, zero-cap 100%, INFLOW debit exclusion, leap Feb, and Jan/Dec rollover."""
    # Case 1: Envelope mode return netting & excess clamp >= 0 + zero-cap 100% + INFLOW debit exclusion
    db.update_budget_config(fixed_cost_baseline=0.0, variable_spending_cap=0.0, envelope_mode=True)
    db.upsert_many(
        [
            TransactionRecord(
                message_id="MET_TOPUP",
                tx_date="2024-02-05",
                account_name=WALLET_HABIBMETRO,
                target_account=WALLET_NAYAPAY,
                tx_type="TRANSFER_INTERNAL",
                amount=10000.0,
                payee="NAYAPAY TOPUP",
                category="Wallet Top-Up",
                cost_bucket="IGNORE",
                review_status="AUTO_VERIFIED",
                is_internal_transfer=True,
                source="MANUAL",
            ),
            TransactionRecord(
                message_id="MET_RETURN_1",
                tx_date="2024-02-06",
                account_name=WALLET_NAYAPAY,
                target_account=WALLET_HABIBMETRO,
                tx_type="TRANSFER_INTERNAL",
                amount=3000.0,
                payee="RETURN TO HABIBMETRO",
                category="Internal Transfer",
                cost_bucket="IGNORE",
                review_status="AUTO_VERIFIED",
                is_internal_transfer=True,
                source="MANUAL",
            ),
            # Unusual DEBIT tagged INFLOW (e.g. reversal/contra) must be excluded from category_progress & top_merchants
            TransactionRecord(
                message_id="MET_INFLOW_DEB",
                tx_date="2024-02-07",
                account_name=WALLET_HABIBMETRO,
                tx_type="DEBIT",
                amount=2500.0,
                payee="CONTRABUCKET MERCHANT",
                category="Groceries",
                cost_bucket="INFLOW",
                review_status="MANUALLY_VERIFIED",
                source="MANUAL",
            ),
            # Prior month Jan 2024 spend to test Jan 2024 -> Dec 2023 and Feb leap days (29)
            apply_rules(
                _parsed("MET_DEC23", "K-ELECTRIC", 31000.0, tx_date="2023-12-15", account_name=WALLET_HABIBMETRO),
                db,
            ),
            apply_rules(
                _parsed("MET_JAN24", "K-ELECTRIC", 15500.0, tx_date="2024-01-10", account_name=WALLET_HABIBMETRO),
                db,
            ),
        ]
    )

    m_leap = compute_dashboard_metrics(db, "2024-02", reference_date=date(2024, 2, 10))
    assert m_leap.actual_variable_burn == pytest.approx(7000.0)
    assert m_leap.total_monthly_burn == pytest.approx(7000.0)
    assert m_leap.total_spending_cap == pytest.approx(0.0)
    assert m_leap.burn_utilization_pct == pytest.approx(100.0)
    assert len(m_leap.daily_cumulative_spend) == 29
    assert sum(p.daily_spend for p in m_leap.daily_cumulative_spend) == pytest.approx(7000.0)
    groceries_prog = next(cp for cp in m_leap.category_progress if cp.category == "Groceries")
    assert groceries_prog.spent == pytest.approx(0.0)
    assert all(m.merchant != "CONTRABUCKET MERCHANT" for m in m_leap.top_merchants)

    # Return exceeding topup clamps variable burn cleanly to >= 0.0 and burn_utilization_pct == 0.0 when cap == 0 and burn == 0
    db.upsert_many(
        [
            TransactionRecord(
                message_id="MET_RETURN_EXCESS",
                tx_date="2024-02-08",
                account_name=WALLET_NAYAPAY,
                target_account=WALLET_HABIBMETRO,
                tx_type="TRANSFER_INTERNAL",
                amount=12000.0,
                payee="EXCESS RETURN TO HMB",
                category="Internal Transfer",
                cost_bucket="IGNORE",
                review_status="AUTO_VERIFIED",
                is_internal_transfer=True,
                source="MANUAL",
            )
        ]
    )
    m_clamped = compute_dashboard_metrics(db, "2024-02", reference_date=date(2024, 2, 10))
    assert m_clamped.actual_variable_burn == pytest.approx(0.0)
    assert m_clamped.total_monthly_burn == pytest.approx(0.0)
    assert m_clamped.burn_utilization_pct == pytest.approx(0.0)

    # Non-leap Feb 2025 has 28 days
    m_nonleap = compute_dashboard_metrics(db, "2025-02", reference_date=date(2025, 2, 10))
    assert len(m_nonleap.daily_cumulative_spend) == 28

    # Jan 2024 MoM rollover uses Dec 2023 (31,000 / 31 days = 1,000.0/day; Jan = 15,500 / 31 elapsed = 500.0/day)
    m_jan = compute_dashboard_metrics(db, "2024-01", reference_date=date(2024, 2, 10))
    assert m_jan.prev_month_daily_burn_pace == pytest.approx(1000.0)
    assert m_jan.actual_daily_burn_pace == pytest.approx(500.0)
    assert m_jan.mom_pace_delta_pct == pytest.approx(-50.0)


def test_envelope_mode_returns_exceed_topups_never_erase_non_envelope_spend(
    tmp_path: Path,
) -> None:
    """Verify NayaPay->HMB envelope returns never erase non-NayaPay VARIABLE spend or yield negative daily spend."""
    # Case A: 0 topups + 5,000 NayaPay->HMB return on day 5 + 5,000 HMB VARIABLE debit on day 6
    with DatabaseManager(tmp_path / "env_zero_topup.db") as db_a:
        db_a.update_budget_config(envelope_mode=True)
        db_a.upsert_many(
            [
                TransactionRecord(
                    message_id="ENVA_RET",
                    tx_date="2026-11-05",
                    account_name=WALLET_NAYAPAY,
                    target_account=WALLET_HABIBMETRO,
                    tx_type="TRANSFER_INTERNAL",
                    amount=5000.0,
                    payee="RETURN TO HMB",
                    category="Internal Transfer",
                    cost_bucket="IGNORE",
                    review_status="AUTO_VERIFIED",
                    is_internal_transfer=True,
                    source="MANUAL",
                ),
                TransactionRecord(
                    message_id="ENVA_HMB_VAR",
                    tx_date="2026-11-06",
                    account_name=WALLET_HABIBMETRO,
                    tx_type="DEBIT",
                    amount=5000.0,
                    payee="HABIBMETRO POS SHOPPING",
                    category="Shopping",
                    cost_bucket="VARIABLE",
                    review_status="MANUALLY_VERIFIED",
                    source="MANUAL",
                ),
            ]
        )
        m_a = compute_dashboard_metrics(db_a, "2026-11", reference_date=date(2026, 11, 11))
        assert m_a.actual_variable_burn == pytest.approx(5000.0)
        assert m_a.total_monthly_burn == pytest.approx(5000.0)
        assert all(p.daily_spend >= 0.0 for p in m_a.daily_cumulative_spend)
        assert sum(p.daily_spend for p in m_a.daily_cumulative_spend) == pytest.approx(
            m_a.total_monthly_burn
        )
        by_day_a = {p.day: p.daily_spend for p in m_a.daily_cumulative_spend}
        assert by_day_a[5] == pytest.approx(0.0)
        assert by_day_a[6] == pytest.approx(5000.0)

    # Case B: 20,000 topup on day 2 + 5,000 return on day 5 + 5,000 HMB VARIABLE debit on day 6
    with DatabaseManager(tmp_path / "env_partial_ret.db") as db_b:
        db_b.update_budget_config(envelope_mode=True)
        db_b.upsert_many(
            [
                TransactionRecord(
                    message_id="ENVB_TOPUP",
                    tx_date="2026-11-02",
                    account_name=WALLET_HABIBMETRO,
                    target_account=WALLET_NAYAPAY,
                    tx_type="TRANSFER_INTERNAL",
                    amount=20000.0,
                    payee="NAYAPAY TOPUP",
                    category="Wallet Top-Up",
                    cost_bucket="IGNORE",
                    review_status="AUTO_VERIFIED",
                    is_internal_transfer=True,
                    source="MANUAL",
                ),
                TransactionRecord(
                    message_id="ENVB_RET",
                    tx_date="2026-11-05",
                    account_name=WALLET_NAYAPAY,
                    target_account=WALLET_HABIBMETRO,
                    tx_type="TRANSFER_INTERNAL",
                    amount=5000.0,
                    payee="RETURN TO HMB",
                    category="Internal Transfer",
                    cost_bucket="IGNORE",
                    review_status="AUTO_VERIFIED",
                    is_internal_transfer=True,
                    source="MANUAL",
                ),
                TransactionRecord(
                    message_id="ENVB_HMB_VAR",
                    tx_date="2026-11-06",
                    account_name=WALLET_HABIBMETRO,
                    tx_type="DEBIT",
                    amount=5000.0,
                    payee="HABIBMETRO DINING",
                    category="Food & Dining",
                    cost_bucket="VARIABLE",
                    review_status="MANUALLY_VERIFIED",
                    source="MANUAL",
                ),
            ]
        )
        m_b = compute_dashboard_metrics(db_b, "2026-11", reference_date=date(2026, 11, 11))
        assert m_b.actual_variable_burn == pytest.approx(20000.0)
        assert m_b.total_monthly_burn == pytest.approx(20000.0)
        assert all(p.daily_spend >= 0.0 for p in m_b.daily_cumulative_spend)
        assert sum(p.daily_spend for p in m_b.daily_cumulative_spend) == pytest.approx(
            m_b.total_monthly_burn
        )
        by_day_b = {p.day: p.daily_spend for p in m_b.daily_cumulative_spend}
        assert by_day_b[2] == pytest.approx(15000.0)
        assert by_day_b[5] == pytest.approx(0.0)
        assert by_day_b[6] == pytest.approx(5000.0)

    # Case C: Return (12,000) exceeds topup (5,000) alongside HMB VARIABLE debit (3,500)
    with DatabaseManager(tmp_path / "env_excess_ret.db") as db_c:
        db_c.update_budget_config(envelope_mode=True)
        db_c.upsert_many(
            [
                TransactionRecord(
                    message_id="ENVC_TOPUP",
                    tx_date="2026-11-02",
                    account_name=WALLET_HABIBMETRO,
                    target_account=WALLET_NAYAPAY,
                    tx_type="TRANSFER_INTERNAL",
                    amount=5000.0,
                    payee="NAYAPAY TOPUP",
                    category="Wallet Top-Up",
                    cost_bucket="IGNORE",
                    review_status="AUTO_VERIFIED",
                    is_internal_transfer=True,
                    source="MANUAL",
                ),
                TransactionRecord(
                    message_id="ENVC_RET",
                    tx_date="2026-11-05",
                    account_name=WALLET_NAYAPAY,
                    target_account=WALLET_HABIBMETRO,
                    tx_type="TRANSFER_INTERNAL",
                    amount=12000.0,
                    payee="RETURN TO HMB",
                    category="Internal Transfer",
                    cost_bucket="IGNORE",
                    review_status="AUTO_VERIFIED",
                    is_internal_transfer=True,
                    source="MANUAL",
                ),
                TransactionRecord(
                    message_id="ENVC_HMB_VAR",
                    tx_date="2026-11-06",
                    account_name=WALLET_HABIBMETRO,
                    tx_type="DEBIT",
                    amount=3500.0,
                    payee="HABIBMETRO SHOPPING",
                    category="Shopping",
                    cost_bucket="VARIABLE",
                    review_status="MANUALLY_VERIFIED",
                    source="MANUAL",
                ),
            ]
        )
        m_c = compute_dashboard_metrics(db_c, "2026-11", reference_date=date(2026, 11, 11))
        assert m_c.actual_variable_burn == pytest.approx(3500.0)
        assert m_c.total_monthly_burn == pytest.approx(3500.0)
        assert all(p.daily_spend >= 0.0 for p in m_c.daily_cumulative_spend)
        assert sum(p.daily_spend for p in m_c.daily_cumulative_spend) == pytest.approx(
            m_c.total_monthly_burn
        )


def test_categorizer_self_transfers_and_metrics_excludes_non_expense_category_caps(
    db: DatabaseManager,
) -> None:
    """Verify own-account transfer promotion and exclusion of INFLOW/IGNORE from spending cap bars."""
    # Explicit NayaPay self-transfer cue promotes DEBIT -> TRANSFER_INTERNAL (NayaPay -> HabibMetro)
    np_self = apply_rules(
        _parsed(
            "SELF_NP_HMB",
            "My HabibMetro Own Account",
            15000.0,
            account_name=WALLET_NAYAPAY,
        ),
        db,
    )
    assert np_self.tx_type == "TRANSFER_INTERNAL"
    assert np_self.target_account == WALLET_HABIBMETRO
    assert np_self.cost_bucket == "IGNORE"
    assert np_self.is_internal_transfer is True

    # User-taught IGNORE rule naming an own wallet promotes matched DEBIT -> TRANSFER_INTERNAL
    learn_new_rule(
        db=db,
        payee="RAAST TO PERSONAL NAYAPAY WALLET",
        alias="Own NayaPay",
        category="Wallet Top-Up",
        cost_bucket="IGNORE",
        keyword="PERSONAL NAYAPAY",
    )
    hmb_to_np = apply_rules(
        _parsed(
            "RULE_HMB_NP",
            "IBFT TO PERSONAL NAYAPAY",
            8000.0,
            account_name=WALLET_HABIBMETRO,
        ),
        db,
    )
    assert hmb_to_np.tx_type == "TRANSFER_INTERNAL"
    assert hmb_to_np.target_account == WALLET_NAYAPAY
    assert hmb_to_np.cost_bucket == "IGNORE"
    assert hmb_to_np.is_internal_transfer is True

    # Dashboard category_progress tracks only expense categories (FIXED/VARIABLE/LEAKAGE)
    metrics = compute_dashboard_metrics(db, "2026-11", reference_date=date(2026, 11, 11))
    progress_cats = {cp.category for cp in metrics.category_progress}
    assert "Salary & Income" not in progress_cats
    assert "Freelance & Client" not in progress_cats
    assert "Internal Transfer" not in progress_cats
    assert "Wallet Top-Up" not in progress_cats


def test_canonical_payee_rule_matching_and_verified_history_inheritance(
    db: DatabaseManager,
) -> None:
    """Verify canonical counterparty extraction, verbose rule matching, and MANUALLY_VERIFIED inheritance."""
    from src.engine.categorizer import extract_canonical_payee

    assert extract_canonical_payee("You sent Rs. 150 to Kamran Ali 💸") == "Kamran Ali"
    assert extract_canonical_payee("You spent Rs. 100 at Easyvend 📱") == "Easyvend"
    assert extract_canonical_payee("You got Rs. 10,000 from Ayesha Siddiqui 🎉") == "Ayesha Siddiqui"
    assert (
        extract_canonical_payee(
            "Raast Out Outgoing fund transfer to Bilal Khan easypaisa Bank-4455"
        )
        == "Bilal Khan"
    )
    assert (
        extract_canonical_payee(
            "Raast In Incoming fund transfer from Omar Farooq Meezan-1122 AMEZNPKKA99390110021122261105134825"
        )
        == "Omar Farooq"
    )
    assert extract_canonical_payee("Retail Payment Paid to Easyvend") == "Easyvend"
    assert (
        extract_canonical_payee("POS Paid to REHMAT E SHEREEN PVT KARACHI PK Visa xxxx6525")
        == "REHMAT E SHEREEN PVT KARACHI PK"
    )
    assert (
        extract_canonical_payee("SALMAN AHMED Meezan Bank from your HMB A/C *4321")
        == "SALMAN AHMED Meezan Bank"
    )

    # Verbose PDF rule keyword matches clean NayaPay email payee
    learn_new_rule(
        db=db,
        payee="Raast Out Outgoing fund transfer to Bilal Khan easypaisa Bank-4455",
        alias="Bilal",
        category="Food & Dining",
        cost_bucket="VARIABLE",
        keyword="Raast Out Outgoing fund transfer to Bilal Khan easypaisa Bank-4455",
    )
    matched_email = apply_rules(
        _parsed("CANON_1", "You sent Rs. 400 to Bilal Khan 💸", 400.0),
        db,
    )
    assert matched_email.category == "Food & Dining"
    assert matched_email.cost_bucket == "VARIABLE"
    assert matched_email.merchant_alias == "Bilal"
    assert matched_email.review_status == "AUTO_VERIFIED"

    # MANUALLY_VERIFIED row without a rule auto-verifies matching future incoming transaction of same direction
    db.upsert_transaction(
        TransactionRecord(
            message_id="VER_HIST_PDF",
            tx_date="2026-11-11",
            tx_timestamp="2026-11-11 17:06:00",
            account_name=WALLET_NAYAPAY,
            tx_type="DEBIT",
            amount=850.0,
            payee="Raast Out Outgoing fund transfer to Huzaifa Ahmed Khan SadaPay-6192",
            merchant_alias="Huzaifa",
            category="Shopping",
            cost_bucket="VARIABLE",
            review_status="MANUALLY_VERIFIED",
            source="PDF_IMPORT",
        )
    )
    inherited_debit = apply_rules(
        _parsed(
            "VER_HIST_EMAIL",
            "You sent Rs. 500 to Huzaifa Ahmed Khan 💸",
            500.0,
            tx_date="2026-11-16",
        ),
        db,
    )
    assert inherited_debit.category == "Shopping"
    assert inherited_debit.cost_bucket == "VARIABLE"
    assert inherited_debit.merchant_alias == "Huzaifa"
    assert inherited_debit.review_status == "AUTO_VERIFIED"

    # Opposite direction (CREDIT) must NOT inherit DEBIT Shopping category
    credit_same_name = apply_rules(
        _parsed(
            "VER_HIST_CRED",
            "You got Rs. 500 from Huzaifa Ahmed Khan 🎉",
            500.0,
            tx_date="2026-11-16",
            tx_type="CREDIT",
        ),
        db,
    )
    assert credit_same_name.review_status == "NEEDS_REVIEW"
    assert credit_same_name.cost_bucket == "INFLOW"


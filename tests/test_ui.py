"""Streamlit AppTest end-to-end UI regression tests across all 5 tabs."""

from __future__ import annotations

from datetime import date
import os
from pathlib import Path
from typing import Iterator
import warnings

import pytest
from streamlit.testing.v1 import AppTest

from src.database.db_manager import DatabaseManager
from src.engine.categorizer import apply_rules
from src.models import (
    WALLET_CASH,
    WALLET_HABIBMETRO,
    WALLET_MMF,
    WALLET_NAYAPAY,
    ParsedTransaction,
    RecurringCommitment,
    TransactionRecord,
)
from src.ui.views import (
    ALL_TABS,
    TAB_BUDGETS,
    TAB_HOME,
    TAB_INSIGHTS,
    TAB_SETUP,
    TAB_TRANSACTIONS,
)
from tests.seed_demo import seed_demo_database

APP_PATH = str(Path(__file__).resolve().parent.parent / "src" / "ui" / "app.py")


@pytest.fixture(autouse=True)
def _isolate_streamlit_db_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Ensure every UI test gets an isolated SQLite DB cache and clean teardown."""
    import streamlit as st
    from src.security import get_auth_rate_limiter
    from src.ui.app import _get_db_manager

    get_auth_rate_limiter().reset()
    for env_key in (
        "TREASURY_APP_PASSWORD",
        "TREASURY_APP_PASSWORD_HASH",
        "TREASURY_REQUIRE_AUTH",
        "TREASURY_BACKUP_KEY",
        "TREASURY_CLOUD_SYNC",
        "GITHUB_BACKUP_TOKEN",
        "GITHUB_BACKUP_GIST_ID",
    ):
        monkeypatch.delenv(env_key, raising=False)
    db_paths: list[str] = []
    orig_setenv = monkeypatch.setenv

    def _tracking_setenv(name: str, value: str) -> None:
        if name == "TREASURY_DB_PATH":
            db_paths.append(value)
        orig_setenv(name, value)

    monkeypatch.setattr(monkeypatch, "setenv", _tracking_setenv)
    st.cache_resource.clear()
    try:
        yield
    finally:
        get_auth_rate_limiter().reset()
        for p in db_paths:
            try:
                _get_db_manager(p).close()
            except Exception:
                pass
        env_p = os.environ.get("TREASURY_DB_PATH")
        if env_p and env_p in db_paths:
            pass
        st.cache_resource.clear()


def _configure_env(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    *,
    configure_gmail: bool = True,
) -> None:
    monkeypatch.setenv("TREASURY_DB_PATH", str(db_path))
    monkeypatch.setenv("GMAIL_CREDENTIALS_PATH", str(db_path.parent / "no_creds.json"))
    monkeypatch.setenv("GMAIL_TOKEN_PATH", str(db_path.parent / "no_token.json"))
    if configure_gmail:
        monkeypatch.setenv("GMAIL_USER", "tester@example.com")
        monkeypatch.setenv("GMAIL_APP_PASSWORD", "test-app-password")
    else:
        monkeypatch.delenv("GMAIL_USER", raising=False)
        monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)


def _create_app(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    *,
    configure_gmail: bool = True,
) -> AppTest:
    _configure_env(monkeypatch, db_path, configure_gmail=configure_gmail)
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.run()
    return at


def _parsed(
    msg_id: str,
    payee: str,
    amount: float,
    *,
    tx_date: str = "2026-11-10",
    account_name: str = WALLET_NAYAPAY,
    tx_type: str = "DEBIT",
) -> ParsedTransaction:
    return ParsedTransaction(
        message_id=msg_id,
        tx_date=tx_date,
        account_name=account_name,
        tx_type=tx_type,  # type: ignore[arg-type]
        amount=amount,
        payee=payee,
        source="GMAIL_NAYAPAY" if account_name == WALLET_NAYAPAY else "GMAIL_HMB",
    )


def _switch_tab(at: AppTest, tab_label: str) -> AppTest:
    """Switch active tab via stateful navigation control and rerun."""
    at.radio(key="ptos_active_tab").set_value(tab_label).run()
    assert at.radio(key="ptos_active_tab").value == tab_label
    return at


def _find_form_button(at: AppTest, label: str):
    """Locate a form submit button or regular button by exact label."""
    for group in ("form_submit_button", "button"):
        for btn in at.get(group):
            if getattr(btn, "label", None) == label:
                return btn
    raise AssertionError(f"Button with label {label!r} not found")


def test_empty_and_demo_db_render_all_tabs_zero_errors_or_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify both an empty DB and full demo DB render all 5 tabs with zero errors/warnings."""
    empty_db = tmp_path / "empty_ui.db"
    DatabaseManager(empty_db).close()

    expected_tabs = list(ALL_TABS)

    with warnings.catch_warnings(record=True) as caught_empty:
        warnings.simplefilter("always")
        at_empty = _create_app(monkeypatch, empty_db, configure_gmail=True)
        assert list(at_empty.exception) == []
        assert list(at_empty.error) == []
        assert list(at_empty.warning) == []
        assert at_empty.radio(key="ptos_active_tab").options == expected_tabs
        for tab_label in expected_tabs:
            _switch_tab(at_empty, tab_label)
            assert list(at_empty.exception) == []
            assert list(at_empty.error) == []
            assert list(at_empty.warning) == []

    src_warnings_empty = [
        w for w in caught_empty if "budget" in str(getattr(w, "filename", ""))
    ]
    assert src_warnings_empty == []

    # Empty DB with unconfigured Gmail only renders the offline auth warning on Tab 5
    at_unconf = _create_app(monkeypatch, empty_db, configure_gmail=False)
    _switch_tab(at_unconf, TAB_SETUP)
    assert list(at_unconf.exception) == []
    assert list(at_unconf.error) == []
    assert all(
        w.value.startswith("Offline / Unconfigured:") for w in at_unconf.warning
    )

    demo_db = tmp_path / "demo_ui.db"
    seed_demo_database(demo_db, reference_date=date(2026, 11, 11))

    with warnings.catch_warnings(record=True) as caught_demo:
        warnings.simplefilter("always")
        at_demo = _create_app(monkeypatch, demo_db, configure_gmail=True)
        assert at_demo.radio(key="ptos_active_tab").options == expected_tabs
        for tab_label in expected_tabs:
            _switch_tab(at_demo, tab_label)
            assert list(at_demo.exception) == []
            assert list(at_demo.error) == []
            assert list(at_demo.warning) == []

    src_warnings_demo = [
        w for w in caught_demo if "budget" in str(getattr(w, "filename", ""))
    ]
    assert src_warnings_demo == []


def test_quick_add_bucket_sync_on_category_and_type_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify Tab 1 Quick-Add syncs t1_qa_bucket and prevents DEBIT transfer/investment buckets."""
    db_path = tmp_path / "qa_sync.db"
    seed_demo_database(db_path, reference_date=date(2026, 11, 11))
    at = _create_app(monkeypatch, db_path)

    at.selectbox(key="t1_qa_type").set_value("DEBIT").run()
    cat_opts = at.selectbox(key="t1_qa_category").options
    assert "Money Market Fund" not in cat_opts
    assert "Internal Transfer" not in cat_opts
    assert "Salary & Income" not in cat_opts

    at.selectbox(key="t1_qa_category").set_value("Bills & Utilities").run()
    assert [opt.split(" — ")[0] for opt in at.selectbox(key="t1_qa_bucket").options] == [
        "FIXED",
        "VARIABLE",
        "LEAKAGE",
    ]
    assert at.selectbox(key="t1_qa_bucket").value == "FIXED"

    at.selectbox(key="t1_qa_category").set_value("Bank & Tax Friction").run()
    assert at.selectbox(key="t1_qa_bucket").value == "LEAKAGE"

    at.selectbox(key="t1_qa_category").set_value("Food & Dining").run()
    assert at.selectbox(key="t1_qa_bucket").value == "VARIABLE"

    at.selectbox(key="t1_qa_type").set_value("CREDIT").run()
    assert [opt.split(" — ")[0] for opt in at.selectbox(key="t1_qa_bucket").options] == ["INFLOW"]
    assert at.selectbox(key="t1_qa_bucket").value == "INFLOW"

    at.selectbox(key="t1_qa_type").set_value("DEBIT").run()
    assert at.selectbox(key="t1_qa_bucket").value == "VARIABLE"
    assert list(at.exception) == []


def test_quick_tag_refreshes_tab2_editor_state_without_stale_revert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify Tab 1 Quick-Tag stays on Tab 1 and refreshes Tab 2 Editor widgets cleanly."""
    db_path = tmp_path / "qt_refresh.db"
    mgr = DatabaseManager(db_path)
    try:
        tx = apply_rules(_parsed("QT_1", "Zameen Roastery", 1450.0), mgr)
        mgr.upsert_many([tx])
        tx_id = mgr.get_transactions()[0].id
    finally:
        mgr.close()
    assert tx_id is not None

    at = _create_app(monkeypatch, db_path)

    # Confirm default memory checkbox is unchecked so single tags do not bulk-mutate unexpectedly
    assert at.checkbox(key=f"t1_qt_mem_{tx_id}").value is False

    # Quick-Tag on Tab 1 (with category + alias + FIXED bucket)
    at.text_input(key=f"t1_qt_alias_{tx_id}").set_value("Zameen Utilities")
    at.selectbox(key=f"t1_qt_cat_{tx_id}").set_value("Bills & Utilities").run()
    assert at.selectbox(key=f"t1_qt_bkt_{tx_id}").value == "FIXED"

    at.button(key=f"t1_qt_save_{tx_id}").click().run()
    assert list(at.exception) == []
    assert list(at.error) == []
    assert at.radio(key="ptos_active_tab").value == TAB_HOME

    # Switch to Tab 2 -> editor must show fresh DB state without stale defaults
    _switch_tab(at, TAB_TRANSACTIONS)
    assert at.selectbox(key=f"t2_edit_cat_{tx_id}").value == "Bills & Utilities"
    assert at.selectbox(key=f"t2_edit_bkt_{tx_id}").value == "FIXED"
    assert at.text_input(key=f"t2_edit_alias_{tx_id}").value == "Zameen Utilities"

    verify_mgr = DatabaseManager(db_path)
    try:
        saved = verify_mgr.get_transactions()[0]
        assert saved.category == "Bills & Utilities"
        assert saved.cost_bucket == "FIXED"
        assert saved.merchant_alias == "Zameen Utilities"
        assert saved.review_status == "MANUALLY_VERIFIED"
    finally:
        verify_mgr.close()


def test_tab3_category_limit_sync_casing_normalization_and_popover_deletes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify Tab 3 stays on Tab 3 across limit updates, category add, bill delete, and Tab 2 delete."""
    db_path = tmp_path / "tab3_ops.db"
    seed_demo_database(db_path, reference_date=date(2026, 11, 11))

    mgr = DatabaseManager(db_path)
    try:
        groceries = next(cb for cb in mgr.get_category_budgets() if cb.category == "Groceries")
        bills = mgr.get_recurring_commitments()
        target_bill_id = bills[0].id
        target_tx_id = mgr.get_transactions(month_key="2026-11")[0].id
    finally:
        mgr.close()
    assert groceries.id is not None
    assert target_bill_id is not None
    assert target_tx_id is not None

    at = _create_app(monkeypatch, db_path)
    _switch_tab(at, TAB_BUDGETS)

    # 1. Update Groceries cap and submit -> stays on Tab 3 and keeps 30000.0
    lim_key = f"t3_cat_limit_{groceries.id}_Groceries"
    at.number_input(key=lim_key).set_value(30000.0)
    _find_form_button(at, "💾 Save Category Limits").click().run()
    assert list(at.exception) == []
    assert at.radio(key="ptos_active_tab").value == TAB_BUDGETS
    assert at.number_input(key=lim_key).value == pytest.approx(30000.0)

    # 2. Add custom category with extra spaces, then update using mismatched case -> single canonical category
    at.text_input(key="t3_nc_name").set_value("  Fitness  &  Gym ")
    at.number_input(key="t3_nc_limit").set_value(6000.0)
    at.button(key="t3_nc_submit").click().run()
    assert list(at.exception) == []
    assert at.radio(key="ptos_active_tab").value == TAB_BUDGETS

    at.text_input(key="t3_nc_name").set_value("  fitness &   gym ")
    at.number_input(key="t3_nc_limit").set_value(7500.0)
    at.button(key="t3_nc_submit").click().run()
    assert list(at.exception) == []
    assert at.radio(key="ptos_active_tab").value == TAB_BUDGETS

    # 3. Recurring commitment popover delete confirmation -> stays on Tab 3
    at.button(key=f"t3_bill_del_{target_bill_id}").click().run()
    assert list(at.exception) == []
    assert at.radio(key="ptos_active_tab").value == TAB_BUDGETS

    # 4. Transaction Editor popover delete confirmation on Tab 2 -> stays on Tab 2
    _switch_tab(at, TAB_TRANSACTIONS)
    at.selectbox(key="t2_selected_tx_id").set_value(target_tx_id).run()
    at.button(key=f"t2_btn_del_{target_tx_id}").click().run()
    assert list(at.exception) == []
    assert list(at.error) == []
    assert at.radio(key="ptos_active_tab").value == TAB_TRANSACTIONS

    verify_mgr = DatabaseManager(db_path)
    try:
        all_cats = verify_mgr.get_category_budgets(active_only=False)
        fitness_matches = [
            cb for cb in all_cats if cb.category.lower() == "fitness & gym"
        ]
        assert len(fitness_matches) == 1
        assert fitness_matches[0].category == "Fitness & Gym"
        assert fitness_matches[0].monthly_limit == pytest.approx(7500.0)

        groceries_db = next(cb for cb in all_cats if cb.category == "Groceries")
        assert groceries_db.monthly_limit == pytest.approx(30000.0)

        remaining_bill_ids = {
            c.id for c in verify_mgr.get_recurring_commitments(active_only=False)
        }
        assert target_bill_id not in remaining_bill_ids

        remaining_tx_ids = {tx.id for tx in verify_mgr.get_transactions()}
        assert target_tx_id not in remaining_tx_ids
    finally:
        verify_mgr.close()


def test_month_switching_refreshes_quick_add_and_tab2_custom_date_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify switching ptos_active_month refreshes month-keyed Quick-Add and Tab 2 custom date bounds."""
    db_path = tmp_path / "month_switch.db"
    mgr = DatabaseManager(db_path)
    try:
        mgr.upsert_many(
            [
                apply_rules(
                    _parsed("MS_OCT", "IMTIAZ", 8000.0, tx_date="2026-10-12"),
                    mgr,
                ),
                apply_rules(
                    _parsed("MS_NOV", "FOODPANDA", 1500.0, tx_date="2026-11-09"),
                    mgr,
                ),
            ]
        )
    finally:
        mgr.close()

    at = _create_app(monkeypatch, db_path)
    at.selectbox(key="ptos_active_month").set_value("2026-11").run()
    qa_nov = at.date_input(key="t1_qa_date_2026-11").value
    assert qa_nov.year == 2026 and qa_nov.month == 11

    at.selectbox(key="ptos_active_month").set_value("2026-10").run()
    assert list(at.exception) == []
    qa_oct = at.date_input(key="t1_qa_date_2026-10").value
    assert qa_oct.year == 2026 and qa_oct.month == 10

    _switch_tab(at, TAB_TRANSACTIONS)
    at.selectbox(key="t2_period_mode").set_value("custom").run()
    assert at.date_input(key="t2_d_start_2026-10").value == date(2026, 10, 1)
    assert at.date_input(key="t2_d_end_2026-10").value == date(2026, 10, 31)

    at.selectbox(key="ptos_active_month").set_value("2026-11").run()
    assert list(at.exception) == []
    assert at.radio(key="ptos_active_tab").value == TAB_TRANSACTIONS
    assert at.date_input(key="t2_d_start_2026-11").value == date(2026, 11, 1)
    assert at.date_input(key="t2_d_end_2026-11").value == date(2026, 11, 30)


def test_tab4_leakage_table_sum_matches_metric_under_envelope_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify Tab 4 Leakage ledger excludes double-counted NayaPay debits and matches Total Leakage & Tax."""
    db_path = tmp_path / "tab4_leakage.db"
    seed_demo_database(db_path, reference_date=date(2026, 11, 11))

    mgr = DatabaseManager(db_path)
    try:
        mgr.update_budget_config(envelope_mode=True)
    finally:
        mgr.close()

    at = _create_app(monkeypatch, db_path)
    _switch_tab(at, TAB_INSIGHTS)
    assert list(at.exception) == []

    leakage_metric = next(
        m for m in at.metric if m.label == "Total Leakage & Tax"
    )
    assert leakage_metric.value == "Rs. 350"

    leak_df_elem = next(
        df for df in at.dataframe if "total_leakage_impact" in df.value.columns
    )
    assert round(float(leak_df_elem.value["total_leakage_impact"].sum()), 2) == pytest.approx(
        350.0
    )
    assert len(leak_df_elem.value) == 1

    captions = [c.value for c in at.caption]
    assert any(
        "Itemized leakage total for 2026-11: **Rs. 350**" in c for c in captions
    )


def test_zero_cap_hero_gauge_renders_no_cap_set_not_on_track_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify zero spending cap renders 'No Cap Set' instead of misleading 'On Track • 0.0%'."""
    db_path = tmp_path / "zero_cap.db"
    mgr = DatabaseManager(db_path)
    try:
        mgr.update_budget_config(fixed_cost_baseline=0.0, variable_spending_cap=0.0)
    finally:
        mgr.close()

    at_zero = _create_app(monkeypatch, db_path)
    assert list(at_zero.exception) == []
    md_zero = "\n".join(m.value for m in at_zero.markdown)
    assert "No Cap Set" in md_zero
    assert "On Track • 0.0%" not in md_zero

    mgr = DatabaseManager(db_path)
    try:
        mgr.upsert_many(
            [
                apply_rules(
                    _parsed(
                        "ZCAP_KE",
                        "K-ELECTRIC",
                        5000.0,
                        tx_date="2026-11-05",
                        account_name=WALLET_HABIBMETRO,
                    ),
                    mgr,
                )
            ]
        )
    finally:
        mgr.close()

    at_burn = _create_app(monkeypatch, db_path)
    assert list(at_burn.exception) == []
    md_burn = "\n".join(m.value for m in at_burn.markdown)
    assert "No Cap Set" in md_burn
    assert "On Track • 0.0%" not in md_burn


def test_tab5_partial_import_errors_survive_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify Tab 5 stays on Tab 5, preserves import errors across rerun, and resets uploader."""
    db_path = tmp_path / "tab5_import.db"
    DatabaseManager(db_path).close()

    at = _create_app(monkeypatch, db_path)
    _switch_tab(at, TAB_SETUP)

    valid_csv = (
        "Date,Description,Debit,Credit,Balance\n"
        "2026-11-09,K-ELECTRIC BILL PAYMENT,12500.00,,187500.00\n"
    ).encode("utf-8")
    broken_pdf = b"%PDF-1.4\ncorrupted non-pdf stream"

    uploader = next(
        u for u in at.get("file_uploader") if str(u.key).startswith("t5_statement_files_")
    )
    uploader.upload("valid_hmb.csv", valid_csv, "text/csv").upload(
        "broken_stmt.pdf", broken_pdf, "application/pdf"
    )
    at.button(key="t5_import_btn").click().run()

    assert list(at.exception) == []
    assert at.radio(key="ptos_active_tab").value == TAB_SETUP
    errors = [e.value for e in at.error]
    successes = [s.value for s in at.success]
    assert len(errors) == 1
    assert "broken_stmt.pdf" in errors[0]
    assert any("Statement Import" in s and "1 new" in s for s in successes)

    # Uploader key incremented so stale file list cleared cleanly
    assert any(
        str(u.key) == "t5_statement_files_1" for u in at.get("file_uploader")
    )

    verify_mgr = DatabaseManager(db_path)
    try:
        txs = verify_mgr.get_transactions()
        assert len(txs) == 1
        assert txs[0].amount == pytest.approx(12500.0)
        assert txs[0].category == "Bills & Utilities"
    finally:
        verify_mgr.close()


def test_tab5_live_balance_calibration_prevents_double_deduction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Submitting Tab 5 Live Balance mode calibrates anchor timestamp and keeps user on Tab 5."""
    db_path = tmp_path / "tab5_live_cal.db"
    mgr = DatabaseManager(db_path)
    try:
        mgr.update_wallet_opening_balance(WALLET_NAYAPAY, 10000.0)
        mgr.upsert_many(
            [
                TransactionRecord(
                    message_id="UI_CAL_NP_1",
                    tx_date="2026-11-04",
                    account_name=WALLET_NAYAPAY,
                    tx_type="DEBIT",
                    amount=4000.0,
                    tax_fee_amount=100.0,
                    payee="HISTORICAL MERCH",
                    category="Shopping & Retail",
                    cost_bucket="VARIABLE",
                )
            ]
        )
        w_init = mgr.get_wallet(WALLET_NAYAPAY)
        assert w_init is not None and w_init.current_balance == pytest.approx(5900.0)
    finally:
        mgr.close()

    at = _create_app(monkeypatch, db_path)
    _switch_tab(at, TAB_SETUP)
    assert list(at.exception) == []
    at.number_input(key="t5_cur_bal_NayaPay").set_value(10000.0)
    at.button(key="t5_save_live_bal_btn").click().run()
    assert list(at.exception) == []
    assert at.radio(key="ptos_active_tab").value == TAB_SETUP

    verify_mgr = DatabaseManager(db_path)
    try:
        w_after = verify_mgr.get_wallet(WALLET_NAYAPAY)
        assert w_after is not None
        assert w_after.current_balance == pytest.approx(10000.0)
        assert w_after.opening_balance == pytest.approx(10000.0)
        assert w_after.balance_as_of is not None
        all_wallets = verify_mgr.get_wallets()
        assert all(w.balance_as_of == w_after.balance_as_of for w in all_wallets)

        old_csv = (
            "Date,Description,Debit,Credit,Balance\n"
            "2026-11-01,OLD COFFEE HOUSE,850.00,,9150.00\n"
        ).encode("utf-8")
        at2 = _create_app(monkeypatch, db_path)
        _switch_tab(at2, TAB_SETUP)
        uploader = next(
            u for u in at2.get("file_uploader") if str(u.key).startswith("t5_statement_files_")
        )
        uploader.upload("nayapay_past.csv", old_csv, "text/csv")
        at2.button(key="t5_import_btn").click().run()
        assert list(at2.exception) == []
        assert at2.radio(key="ptos_active_tab").value == TAB_SETUP
    finally:
        verify_mgr.close()

    verify_mgr2 = DatabaseManager(db_path)
    try:
        w_post_csv = verify_mgr2.get_wallet(WALLET_NAYAPAY)
        assert w_post_csv is not None
        assert w_post_csv.current_balance == pytest.approx(10000.0)
        assert len(verify_mgr2.get_transactions()) == 2
    finally:
        verify_mgr2.close()


def test_tab2_save_only_isolation_and_rule_keyword_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify Tab 2 Save Only mutates strictly 1 row and Rule validation fails before DB commit."""
    db_path = tmp_path / "tab2_isolation.db"
    mgr = DatabaseManager(db_path)
    try:
        mgr.upsert_many(
            [
                apply_rules(_parsed("ISO_1", "ZAMEEN ROASTERY", 500.0, tx_date="2026-11-08"), mgr),
                apply_rules(_parsed("ISO_2", "ZAMEEN ROASTERY", 750.0, tx_date="2026-11-09"), mgr),
                TransactionRecord(
                    message_id="ISO_MMF",
                    tx_date="2026-11-10",
                    account_name=WALLET_HABIBMETRO,
                    target_account=WALLET_MMF,
                    tx_type="TRANSFER_MMF",
                    amount=15000.0,
                    payee="AL MEEZAN SWEEP",
                    category="Money Market Fund",
                    cost_bucket="INVESTMENT",
                    review_status="AUTO_VERIFIED",
                ),
            ]
        )
        all_tx = mgr.get_transactions()
        tx1_id = next(t.id for t in all_tx if t.message_id == "ISO_2")
        tx2_id = next(t.id for t in all_tx if t.message_id == "ISO_1")
    finally:
        mgr.close()
    assert tx1_id is not None and tx2_id is not None

    at = _create_app(monkeypatch, db_path)
    # Test Tab 1 quick jump into Tab 2 Review Queue
    at.button(key="t1_go_review").click().run()
    assert at.radio(key="ptos_active_tab").value == TAB_TRANSACTIONS
    assert at.checkbox(key="t2_needs_review_only").value is True

    # 1. Attempting Save & Apply Rule with <3 char keyword must fail cleanly without touching DB
    at.selectbox(key="t2_selected_tx_id").set_value(tx1_id).run()
    at.selectbox(key=f"t2_edit_cat_{tx1_id}").set_value("Food & Dining").run()
    at.text_input(key=f"t2_edit_kw_{tx1_id}").set_value("KE")
    at.button(key=f"t2_btn_learn_{tx1_id}").click().run()
    assert list(at.exception) == []
    assert any("at least 3 characters" in e.value for e in at.error)

    mgr_chk1 = DatabaseManager(db_path)
    try:
        tx_still_pending = next(t for t in mgr_chk1.get_transactions() if t.id == tx1_id)
        assert tx_still_pending.review_status == "NEEDS_REVIEW"
        assert tx_still_pending.category == "Uncategorized"
    finally:
        mgr_chk1.close()

    # 2. Save Only This Transaction must mutate ONLY tx1_id and leave tx2_id untouched
    at.button(key=f"t2_btn_save_{tx1_id}").click().run()
    assert list(at.exception) == []
    assert list(at.error) == []
    assert at.radio(key="ptos_active_tab").value == TAB_TRANSACTIONS

    mgr_chk2 = DatabaseManager(db_path)
    try:
        by_id = {t.id: t for t in mgr_chk2.get_transactions()}
        assert by_id[tx1_id].category == "Food & Dining"
        assert by_id[tx1_id].review_status == "MANUALLY_VERIFIED"
        assert by_id[tx2_id].category == "Uncategorized"
        assert by_id[tx2_id].review_status == "NEEDS_REVIEW"
    finally:
        mgr_chk2.close()

    # 3. Wallet filter for Money Market Fund includes incoming TRANSFER_MMF rows
    at.checkbox(key="t2_needs_review_only").uncheck().run()
    at.selectbox(key="t2_wallet").set_value(WALLET_MMF).run()
    mmf_rows = [m for m in at.metric if m.label == "Matching Rows"]
    assert mmf_rows and mmf_rows[0].value == "1"


def test_tab3_bill_pay_anchors_live_balance_and_tab5_cross_card_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify Tab 3 Log Payment adjusts anchored live balance and Tab 5 cards stay mutually isolated."""
    db_path = tmp_path / "tab3_tab5_iso.db"
    today_month = date.today().strftime("%Y-%m")
    mgr = DatabaseManager(db_path)
    try:
        mgr.calibrate_wallet_current_balance(WALLET_NAYAPAY, 10000.0)
        bill = mgr.upsert_recurring_commitment(
            RecurringCommitment(
                title="K-Electric",
                match_keyword="KE",
                expected_amount=2200.0,
                due_day=15,
                category="Bills & Utilities",
                cost_bucket="FIXED",
                is_active=True,
            )
        )
    finally:
        mgr.close()
    assert bill.id is not None

    at = _create_app(monkeypatch, db_path)
    at.selectbox(key="ptos_active_month").set_value(today_month).run()
    _switch_tab(at, TAB_BUDGETS)
    at.selectbox(key="t3_bill_pay_wallet").set_value(WALLET_NAYAPAY).run()
    at.button(key=f"t3_bill_pay_{bill.id}").click().run()
    assert list(at.exception) == []
    assert at.radio(key="ptos_active_tab").value == TAB_BUDGETS

    # Switch to Tab 5 -> NayaPay current balance input must reflect updated 7800.0 (not stale 10000.0)
    _switch_tab(at, TAB_SETUP)
    assert at.number_input(key="t5_cur_bal_NayaPay").value == pytest.approx(7800.0)

    # Edit Defense Config WITHOUT saving, then save Live Balances -> Defense Config edit survives and does NOT commit prematurely
    at.number_input(key="t5_cfg_income").set_value(320000.0)
    at.number_input(key="t5_cur_bal_Cash_in_Hand").set_value(12500.0)
    at.button(key="t5_save_live_bal_btn").click().run()
    assert list(at.exception) == []
    assert at.radio(key="ptos_active_tab").value == TAB_SETUP
    assert at.number_input(key="t5_cfg_income").value == pytest.approx(320000.0)

    verify_mgr = DatabaseManager(db_path)
    try:
        np = verify_mgr.get_wallet(WALLET_NAYAPAY)
        cash = verify_mgr.get_wallet(WALLET_CASH)
        cfg = verify_mgr.get_budget_config()
        assert np is not None and np.current_balance == pytest.approx(7800.0)
        assert cash is not None and cash.current_balance == pytest.approx(12500.0)
        assert cfg.monthly_income != pytest.approx(320000.0)
    finally:
        verify_mgr.close()


def test_tab1_quick_tag_short_keyword_guard_and_quick_add_tab_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify Tab 1 Quick-Tag rejects <3 char keyword safely and Quick-Add stays on Tab 1."""
    db_path = tmp_path / "tab1_guards.db"
    mgr = DatabaseManager(db_path)
    try:
        tx = apply_rules(_parsed("GUARD_1", "AI", 600.0, tx_date="2026-11-05"), mgr)
        mgr.upsert_many([tx])
        tx_id = mgr.get_transactions()[0].id
    finally:
        mgr.close()
    assert tx_id is not None

    at = _create_app(monkeypatch, db_path)
    at.checkbox(key=f"t1_qt_mem_{tx_id}").check()
    at.text_input(key=f"t1_qt_kw_{tx_id}").set_value("AI")
    at.button(key=f"t1_qt_save_{tx_id}").click().run()

    assert list(at.exception) == []
    assert any("at least 3 characters" in e.value for e in at.error)

    verify_mgr = DatabaseManager(db_path)
    try:
        row = verify_mgr.get_transactions()[0]
        assert row.review_status == "NEEDS_REVIEW"
    finally:
        verify_mgr.close()

    # Quick-Add clean submit stays on Tab 1 and resets category back to Auto-Categorize
    at.selectbox(key="t1_qa_type").set_value("DEBIT").run()
    at.selectbox(key="t1_qa_category").set_value("Groceries").run()
    at.number_input(key="t1_qa_amount").set_value(1200.0)
    at.text_input(key="t1_qa_payee").set_value("Fresh Fruit Cart")
    at.button(key="t1_qa_submit").click().run()
    assert list(at.exception) == []
    assert list(at.error) == []
    assert at.radio(key="ptos_active_tab").value == TAB_HOME
    assert at.selectbox(key="t1_qa_category").value == "Auto-Categorize (Rules)"


def test_tab5_defense_config_reverse_isolation_and_opening_balance_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify Defense Config save preserves unsaved balance edits and Historical Opening mode clears anchors."""
    db_path = tmp_path / "tab5_defense_open.db"
    mgr = DatabaseManager(db_path)
    try:
        mgr.calibrate_wallet_current_balance(WALLET_HABIBMETRO, 80000.0)
        mgr.upsert_many(
            [
                TransactionRecord(
                    message_id="HIST_1",
                    tx_date="2026-11-01",
                    tx_timestamp="2026-11-01 09:00:00",
                    account_name=WALLET_HABIBMETRO,
                    tx_type="DEBIT",
                    amount=5000.0,
                    payee="Historical Bill",
                    category="Bills & Utilities",
                    cost_bucket="FIXED",
                    review_status="AUTO_VERIFIED",
                )
            ]
        )
    finally:
        mgr.close()

    at = _create_app(monkeypatch, db_path)
    _switch_tab(at, TAB_SETUP)

    # Leave an unsaved live balance edit in sibling card before saving Defense Config
    at.number_input(key="t5_cur_bal_Cash_in_Hand").set_value(9999.0)
    at.number_input(key="t5_cfg_income").set_value(350000.0)
    at.number_input(key="t5_cfg_fixed").set_value(90000.0)
    at.number_input(key="t5_cfg_var").set_value(45000.0)
    at.number_input(key="t5_cfg_mult").set_value(8)
    at.checkbox(key="t5_cfg_env").check()
    at.button(key="t5_save_budget_btn").click().run()

    assert list(at.exception) == []
    assert list(at.error) == []
    assert at.radio(key="ptos_active_tab").value == TAB_SETUP
    assert at.number_input(key="t5_cur_bal_Cash_in_Hand").value == pytest.approx(9999.0)

    mgr_chk1 = DatabaseManager(db_path)
    try:
        cfg = mgr_chk1.get_budget_config()
        assert cfg.monthly_income == pytest.approx(350000.0)
        assert cfg.fixed_cost_baseline == pytest.approx(90000.0)
        assert cfg.variable_spending_cap == pytest.approx(45000.0)
        assert cfg.emergency_multiplier == 8
        assert cfg.envelope_mode is True
        cash_w = mgr_chk1.get_wallet(WALLET_CASH)
        hmb_w = mgr_chk1.get_wallet(WALLET_HABIBMETRO)
        assert cash_w is not None and cash_w.current_balance != pytest.approx(9999.0)
        assert hmb_w is not None and hmb_w.balance_as_of is not None
    finally:
        mgr_chk1.close()

    # Switch to Historical Opening Balance mode and save
    at.radio(key="t5_wallet_balance_mode").set_value(
        "Set Historical Opening Balance (Before Tracked Txns)"
    ).run()
    at.number_input(key="t5_open_bal_HabibMetro").set_value(50000.0)
    at.button(key="t5_save_open_bal_btn").click().run()

    assert list(at.exception) == []
    assert list(at.error) == []
    assert at.radio(key="ptos_active_tab").value == TAB_SETUP

    mgr_chk2 = DatabaseManager(db_path)
    try:
        hmb_after = mgr_chk2.get_wallet(WALLET_HABIBMETRO)
        assert hmb_after is not None
        assert hmb_after.balance_as_of is None
        assert hmb_after.opening_balance == pytest.approx(50000.0)
        assert hmb_after.current_balance == pytest.approx(45000.0)
    finally:
        mgr_chk2.close()


def test_rule_learning_happy_paths_and_quick_add_transfers_and_nav(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify >=3-char rule learning on Tab 1/Tab 2, Quick-Add transfer types & errors, and Tab 1 nav shortcuts."""
    db_path = tmp_path / "rules_qa_nav.db"
    mgr = DatabaseManager(db_path)
    try:
        mgr.update_wallet_opening_balance(WALLET_HABIBMETRO, 100000.0)
        mgr.upsert_many(
            [
                apply_rules(_parsed("QT_1", "XYZZY BAZAAR", 450.0, tx_date="2026-11-08"), mgr),
                apply_rules(_parsed("QT_2", "XYZZY BAZAAR", 650.0, tx_date="2026-11-09"), mgr),
                apply_rules(_parsed("T2_1", "QWEVOK TRADERS", 1500.0, tx_date="2026-11-06"), mgr),
                apply_rules(_parsed("T2_2", "QWEVOK TRADERS", 2500.0, tx_date="2026-11-07"), mgr),
            ]
        )
        all_tx = mgr.get_transactions()
        qt1_id = next(t.id for t in all_tx if t.message_id == "QT_2")
        t2_id = next(t.id for t in all_tx if t.message_id == "T2_2")
    finally:
        mgr.close()
    assert qt1_id is not None and t2_id is not None

    at = _create_app(monkeypatch, db_path)

    # 1. Tab 1 Quick-Tag learn rule happy path updates rule + sibling row
    at.selectbox(key=f"t1_qt_cat_{qt1_id}").set_value("Food & Dining").run()
    at.text_input(key=f"t1_qt_alias_{qt1_id}").set_value("Xyzzy Food")
    at.text_input(key=f"t1_qt_kw_{qt1_id}").set_value("XYZZY BAZAAR")
    at.checkbox(key=f"t1_qt_mem_{qt1_id}").check()
    at.button(key=f"t1_qt_save_{qt1_id}").click().run()
    assert list(at.exception) == []
    assert list(at.error) == []
    assert at.radio(key="ptos_active_tab").value == TAB_HOME

    # 2. Tab 2 Learn Rule happy path updates rule + sibling QWEVOK TRADERS row
    at.button(key="t1_go_review").click().run()
    assert at.radio(key="ptos_active_tab").value == TAB_TRANSACTIONS
    at.selectbox(key="t2_selected_tx_id").set_value(t2_id).run()
    at.selectbox(key=f"t2_edit_cat_{t2_id}").set_value("Shopping").run()
    at.text_input(key=f"t2_edit_kw_{t2_id}").set_value("QWEVOK TRADERS")
    at.button(key=f"t2_btn_learn_{t2_id}").click().run()
    assert list(at.exception) == []
    assert list(at.error) == []
    assert at.radio(key="ptos_active_tab").value == TAB_TRANSACTIONS

    mgr_rules = DatabaseManager(db_path)
    try:
        by_msg = {t.message_id: t for t in mgr_rules.get_transactions()}
        assert by_msg["QT_1"].category == "Food & Dining"
        assert by_msg["QT_2"].category == "Food & Dining"
        assert by_msg["QT_1"].review_status != "NEEDS_REVIEW"
        assert by_msg["T2_1"].category == "Shopping"
        assert by_msg["T2_2"].category == "Shopping"
        rule_kws = {r.keyword.upper() for r in mgr_rules.get_category_rules()}
        assert "XYZZY BAZAAR" in rule_kws
        assert "QWEVOK TRADERS" in rule_kws
    finally:
        mgr_rules.close()

    # 3. Quick-Add validation errors (amount <= 0, blank payee) + DEBIT auto-categorize + CREDIT + transfers
    _switch_tab(at, TAB_HOME)
    at.number_input(key="t1_qa_amount").set_value(0.0)
    at.button(key="t1_qa_submit").click().run()
    assert any("greater than Rs. 0" in e.value for e in at.error)

    at.number_input(key="t1_qa_amount").set_value(500.0)
    at.text_input(key="t1_qa_payee").set_value("")
    at.button(key="t1_qa_submit").click().run()
    assert any("Payee" in e.value for e in at.error)

    # Auto-Categorize matches learned XYZZY BAZAAR rule
    at.selectbox(key="t1_qa_account").set_value(WALLET_HABIBMETRO).run()
    at.selectbox(key="t1_qa_type").set_value("DEBIT").run()
    at.selectbox(key="t1_qa_category").set_value("Auto-Categorize (Rules)").run()
    at.number_input(key="t1_qa_amount").set_value(500.0)
    at.text_input(key="t1_qa_payee").set_value("XYZZY BAZAAR DHA")
    at.button(key="t1_qa_submit").click().run()
    assert list(at.error) == []

    # CREDIT
    at.selectbox(key="t1_qa_account").set_value(WALLET_HABIBMETRO).run()
    at.selectbox(key="t1_qa_type").set_value("CREDIT").run()
    at.number_input(key="t1_qa_amount").set_value(50000.0)
    at.text_input(key="t1_qa_payee").set_value("Freelance Client")
    at.button(key="t1_qa_submit").click().run()
    assert list(at.error) == []

    # TRANSFER_INTERNAL -> NayaPay
    at.selectbox(key="t1_qa_account").set_value(WALLET_HABIBMETRO).run()
    at.selectbox(key="t1_qa_type").set_value("TRANSFER_INTERNAL").run()
    at.selectbox(key="t1_qa_target").set_value(WALLET_NAYAPAY).run()
    at.number_input(key="t1_qa_amount").set_value(4000.0)
    at.text_input(key="t1_qa_payee").set_value("Top Up NayaPay")
    at.button(key="t1_qa_submit").click().run()
    assert list(at.error) == []

    # TRANSFER_MMF -> Money Market Fund
    at.selectbox(key="t1_qa_account").set_value(WALLET_HABIBMETRO).run()
    at.selectbox(key="t1_qa_type").set_value("TRANSFER_MMF").run()
    at.number_input(key="t1_qa_amount").set_value(6000.0)
    at.text_input(key="t1_qa_payee").set_value("Al Meezan Sweep")
    at.button(key="t1_qa_submit").click().run()
    assert list(at.error) == []
    assert at.radio(key="ptos_active_tab").value == TAB_HOME

    mgr_bal = DatabaseManager(db_path)
    try:
        qa_rows = [t for t in mgr_bal.get_transactions() if t.payee == "XYZZY BAZAAR DHA"]
        assert qa_rows and qa_rows[0].category == "Food & Dining"
        assert qa_rows[0].review_status == "AUTO_VERIFIED"
        hmb_w = mgr_bal.get_wallet(WALLET_HABIBMETRO)
        np_w = mgr_bal.get_wallet(WALLET_NAYAPAY)
        mmf_w = mgr_bal.get_wallet(WALLET_MMF)
        assert hmb_w is not None and hmb_w.current_balance == pytest.approx(139500.0)
        assert np_w is not None and np_w.current_balance == pytest.approx(-1100.0)
        assert mmf_w is not None and mmf_w.current_balance == pytest.approx(6000.0)
    finally:
        mgr_bal.close()

    # 4. Tab 1 quick-nav shortcuts to Tab 3 and Tab 2
    at.button(key="t1_go_budgets").click().run()
    assert at.radio(key="ptos_active_tab").value == TAB_BUDGETS
    _switch_tab(at, TAB_HOME)
    at.button(key="t1_go_ledger").click().run()
    assert at.radio(key="ptos_active_tab").value == TAB_TRANSACTIONS


def test_tab3_recurring_lifecycle_bill_note_matching_and_tab2_filters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify Tab 3 add/pay([bill:id])/pause/resume/delete/archive/restore and Tab 2 full filter suite."""
    db_path = tmp_path / "tab3_lifecycle_tab2_filters.db"
    today_month = date.today().strftime("%Y-%m")
    mgr = DatabaseManager(db_path)
    try:
        cb = mgr.upsert_category_budget("Dining & Coffee", 8000.0, "☕", "VARIABLE", is_active=True)
        mgr.upsert_many(
            [
                TransactionRecord(
                    message_id="FILT_1",
                    tx_date=f"{today_month}-05",
                    account_name=WALLET_HABIBMETRO,
                    tx_type="DEBIT",
                    amount=1800.0,
                    payee="OPTICAL FIBER CO",
                    category="Dining & Coffee",
                    cost_bucket="VARIABLE",
                    review_status="AUTO_VERIFIED",
                ),
                TransactionRecord(
                    message_id="FILT_2",
                    tx_date="2025-12-15",
                    account_name=WALLET_NAYAPAY,
                    tx_type="DEBIT",
                    amount=950.0,
                    payee="LEGACY GROCER",
                    category="Groceries",
                    cost_bucket="FIXED",
                    review_status="AUTO_VERIFIED",
                ),
            ]
        )
    finally:
        mgr.close()
    assert cb.id is not None

    at = _create_app(monkeypatch, db_path)
    at.selectbox(key="ptos_active_month").set_value(today_month).run()
    _switch_tab(at, TAB_BUDGETS)

    # 1. Recurring form validation + valid creation with non-matching keyword (tests [bill:{id}] matching)
    at.text_input(key="t3_rc_title").set_value("")
    at.button(key="t3_rc_submit").click().run()
    assert any("required" in e.value.lower() for e in at.error)

    at.text_input(key="t3_rc_title").set_value("Internet Plan")
    at.text_input(key="t3_rc_keyword").set_value("UNRELATED_TOKEN_XYZ")
    at.number_input(key="t3_rc_amount").set_value(3500.0)
    at.number_input(key="t3_rc_due_day").set_value(10)
    at.button(key="t3_rc_submit").click().run()
    assert list(at.error) == []
    assert at.radio(key="ptos_active_tab").value == TAB_BUDGETS
    assert at.text_input(key="t3_rc_title").value == ""

    mgr_b = DatabaseManager(db_path)
    try:
        bills = mgr_b.get_recurring_commitments(active_only=False)
        bill_id = next(b.id for b in bills if b.title == "Internet Plan")
    finally:
        mgr_b.close()
    assert bill_id is not None

    # 2. Log Payment stamps [bill:{bill_id}] and drops Unpaid Remaining by 3500 + flips to Matched in ledger
    unpaid_before_str = [m for m in at.metric if m.label == "Unpaid Remaining"][0].value
    unpaid_before = float(unpaid_before_str.replace("Rs.", "").replace(",", "").strip())
    at.button(key=f"t3_bill_pay_{bill_id}").click().run()
    assert list(at.error) == []
    assert at.radio(key="ptos_active_tab").value == TAB_BUDGETS
    unpaid_after_str = [m for m in at.metric if m.label == "Unpaid Remaining"][0].value
    unpaid_after = float(unpaid_after_str.replace("Rs.", "").replace(",", "").strip())
    assert unpaid_before - unpaid_after == pytest.approx(3500.0)
    assert any("Matched in ledger" in c.value for c in at.caption)

    # 3. Pause -> Resume -> Pause -> Delete paused bill
    at.button(key=f"t3_bill_pause_{bill_id}").click().run()
    assert at.radio(key="ptos_active_tab").value == TAB_BUDGETS
    at.button(key=f"t3_bill_resume_{bill_id}").click().run()
    assert at.radio(key="ptos_active_tab").value == TAB_BUDGETS
    at.button(key=f"t3_bill_pause_{bill_id}").click().run()
    at.button(key=f"t3_bill_pdel_{bill_id}").click().run()
    assert at.radio(key="ptos_active_tab").value == TAB_BUDGETS

    # 4. Archive -> Restore category budget
    at.button(key=f"t3_cat_archive_{cb.id}").click().run()
    assert at.radio(key="ptos_active_tab").value == TAB_BUDGETS
    at.button(key=f"t3_cat_restore_{cb.id}").click().run()
    assert at.radio(key="ptos_active_tab").value == TAB_BUDGETS

    mgr_fin = DatabaseManager(db_path)
    try:
        assert not any(
            b.id == bill_id
            for b in mgr_fin.get_recurring_commitments(active_only=False)
        )
        cat_restored = next(c for c in mgr_fin.get_category_budgets(active_only=False) if c.id == cb.id)
        assert cat_restored.is_active is True
    finally:
        mgr_fin.close()

    # 5. Tab 2 filters: search, category, bucket, all_months, custom swapped dates
    _switch_tab(at, TAB_TRANSACTIONS)
    at.selectbox(key="t2_period_mode").set_value("all_months").run()
    at.text_input(key="t2_search").set_value("LEGACY").run()
    rows_m = [m for m in at.metric if m.label == "Matching Rows"]
    assert rows_m and rows_m[0].value == "1"

    at.text_input(key="t2_search").set_value("").run()
    at.selectbox(key="t2_category").set_value("Dining & Coffee").run()
    at.selectbox(key="t2_bucket").set_value("VARIABLE").run()
    assert at.radio(key="ptos_active_tab").value == TAB_TRANSACTIONS

    at.selectbox(key="t2_category").set_value("All Categories").run()
    at.selectbox(key="t2_bucket").set_value("All Buckets").run()
    at.selectbox(key="t2_period_mode").set_value("custom").run()
    at.date_input(key=f"t2_d_start_{today_month}").set_value(date(2026, 12, 20))
    at.date_input(key=f"t2_d_end_{today_month}").set_value(date(2026, 12, 1)).run()
    assert any("swapping bounds" in w.value for w in at.warning)
    assert at.radio(key="ptos_active_tab").value == TAB_TRANSACTIONS


def test_tab5_gmail_sync_fixtures_and_backups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify Tab 5 Gmail Sync (mocked), Sample Fixtures load, Disk Backup, and Download buttons."""
    import src.ui.views as views_mod

    db_path = tmp_path / "tab5_sync_backup.db"
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(views_mod, "_BACKUP_DIR", backup_dir)

    mock_tx = ParsedTransaction(
        message_id="GMAIL_MOCK_001",
        tx_date="2026-11-11",
        account_name=WALLET_NAYAPAY,
        tx_type="DEBIT",
        amount=820.0,
        payee="CHAAYE KHANA",
        payment_channel="Raast",
        source="GMAIL_NAYAPAY",
        source_email_subject="Transaction Alert",
    )
    monkeypatch.setattr(
        views_mod,
        "fetch_emails_with_report",
        lambda max_results=100: ([mock_tx], "Mocked Gmail scan complete."),
    )

    at = _create_app(monkeypatch, db_path)
    _switch_tab(at, TAB_SETUP)

    # 1. Sync Gmail Now
    at.button(key="t5_gmail_sync_btn").click().run()
    assert list(at.exception) == []
    assert list(at.error) == []
    assert at.radio(key="ptos_active_tab").value == TAB_SETUP

    # 2. Load Sample Fixtures
    at.button(key="t5_load_fixtures_btn").click().run()
    assert list(at.exception) == []
    assert list(at.error) == []
    assert at.radio(key="ptos_active_tab").value == TAB_SETUP

    # 3. Save Snapshot to isolated backup dir
    at.button(key="t5_save_disk_backup").click().run()
    assert list(at.exception) == []
    assert list(at.error) == []
    assert at.radio(key="ptos_active_tab").value == TAB_SETUP

    assert len(list(backup_dir.glob("*.json"))) == 1
    assert len(list(backup_dir.glob("*.csv"))) == 1
    assert any(
        b.key in {"t5_dl_json", "t5_dl_csv"}
        for b in at.get("download_button")
    )

    mgr = DatabaseManager(db_path)
    try:
        msg_ids = {t.message_id for t in mgr.get_transactions()}
        assert "GMAIL_MOCK_001" in msg_ids
        assert len(msg_ids) >= 7
    finally:
        mgr.close()


def test_inflow_vs_outflow_separation_inline_create_and_tab3_category_studio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify Inflow vs Outflow separation, MMF redemption targets, inline create, and Tab 3 edit/delete."""
    db_path = tmp_path / "dynamic_categories.db"
    mgr = DatabaseManager(db_path)
    try:
        mgr.upsert_many(
            [
                TransactionRecord(
                    message_id="CAT_TX_1",
                    tx_date="2026-11-08",
                    account_name=WALLET_CASH,
                    tx_type="DEBIT",
                    amount=750.0,
                    payee="SPECIAL COFFEE",
                    category="Food & Dining",
                    cost_bucket="VARIABLE",
                    review_status="MANUALLY_VERIFIED",
                )
            ]
        )
    finally:
        mgr.close()

    at = _create_app(monkeypatch, db_path)

    # 1. Strict separation on Tab 1 Quick-Add: CREDIT shows INFLOW categories only; DEBIT shows expense only
    at.selectbox(key="t1_qa_type").set_value("CREDIT").run()
    credit_opts = at.selectbox(key="t1_qa_category").options
    assert "Salary & Income" in credit_opts
    assert "Freelance & Client" in credit_opts
    assert "Groceries" not in credit_opts
    assert "Food & Dining" not in credit_opts

    # Create a custom INFLOW category inline right on Tab 1 Quick-Add
    at.text_input(key="t1_qa_newcat_name").set_value("Rental Yield")
    at.button(key="t1_qa_newcat_save").click().run()
    assert list(at.exception) == []
    assert list(at.error) == []
    assert at.radio(key="ptos_active_tab").value == TAB_HOME
    assert "Rental Yield" in at.selectbox(key="t1_qa_category").options
    assert at.selectbox(key="t1_qa_category").value == "Rental Yield"

    # Switching back to DEBIT must NOT show Rental Yield or Salary & Income
    at.selectbox(key="t1_qa_type").set_value("DEBIT").run()
    debit_opts = at.selectbox(key="t1_qa_category").options
    assert "Groceries" in debit_opts
    assert "Rental Yield" not in debit_opts
    assert "Salary & Income" not in debit_opts

    # Bidirectional MMF -> HabibMetro redemption supported cleanly under TRANSFER_INTERNAL
    at.selectbox(key="t1_qa_account").set_value(WALLET_MMF).run()
    assert "TRANSFER_MMF" not in [opt.split(" — ")[0] for opt in at.selectbox(key="t1_qa_type").options]
    at.selectbox(key="t1_qa_type").set_value("TRANSFER_INTERNAL").run()
    assert WALLET_HABIBMETRO in at.selectbox(key="t1_qa_target").options
    assert at.selectbox(key="t1_qa_transfer_cat").value == "MMF Redemption"

    # 2. Tab 3 Dynamic Category Studio: add custom INFLOW, verify excluded from Outflow caps form, edit & delete
    _switch_tab(at, TAB_BUDGETS)
    at.text_input(key="t3_nc_name").set_value("YouTube Payout")
    at.selectbox(key="t3_nc_bucket").set_value("INFLOW").run()
    at.button(key="t3_nc_submit").click().run()
    assert list(at.exception) == []
    assert list(at.error) == []
    assert at.radio(key="ptos_active_tab").value == TAB_BUDGETS

    # Must NOT clutter Outflow category budget limits number inputs
    assert not any(
        "YouTube Payout" in (n.key or "")
        for n in at.number_input
    )

    # Pick 'Food & Dining' via row '✏️ Edit' button, switch VARIABLE -> FIXED & rename -> 'Dining & Cafes'
    mgr2 = DatabaseManager(db_path)
    try:
        all_cats_now = mgr2.get_category_budgets(active_only=False)
        food_id = next(c.id for c in all_cats_now if c.category == "Food & Dining")
        yt_id = next(c.id for c in all_cats_now if c.category == "YouTube Payout")
        rental_id = next(c.id for c in all_cats_now if c.category == "Rental Yield")
    finally:
        mgr2.close()
    assert food_id is not None and yt_id is not None and rental_id is not None

    at.button(key=f"t3_cat_pick_{food_id}").click().run()
    assert list(at.exception) == []
    assert at.selectbox(key="t3_edit_cat_id").value == food_id
    assert at.text_input(key="t3_ec_name").value == "Food & Dining"
    at.text_input(key="t3_ec_name").set_value("Dining & Cafes")
    at.selectbox(key="t3_ec_bucket").set_value("FIXED").run()
    at.button(key="t3_ec_save_btn").click().run()
    assert list(at.exception) == []
    assert list(at.error) == []
    assert at.radio(key="ptos_active_tab").value == TAB_BUDGETS

    # Permanently delete 'YouTube Payout' from Manage list AND 'Rental Yield' from Edit panel
    at.button(key=f"t3_cat_del_{yt_id}").click().run()
    assert list(at.exception) == []
    assert list(at.error) == []
    assert at.radio(key="ptos_active_tab").value == TAB_BUDGETS

    at.button(key=f"t3_cat_pick_{rental_id}").click().run()
    at.button(key="t3_ec_del_btn").click().run()
    assert list(at.exception) == []
    assert list(at.error) == []
    assert at.radio(key="ptos_active_tab").value == TAB_BUDGETS

    mgr3 = DatabaseManager(db_path)
    try:
        cats_after = {c.category: c for c in mgr3.get_category_budgets(active_only=False)}
        assert "Dining & Cafes" in cats_after
        assert cats_after["Dining & Cafes"].cost_bucket == "FIXED"
        assert "Food & Dining" not in cats_after
        assert "YouTube Payout" not in cats_after
        assert "Rental Yield" not in cats_after
        tx_renamed = mgr3.get_transactions()[0]
        assert tx_renamed.category == "Dining & Cafes"
        assert tx_renamed.cost_bucket == "FIXED"
    finally:
        mgr3.close()


def test_tab1_quick_tag_internal_transfer_and_tab2_transfer_pair_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify Quick-Tag supports converting NEEDS_REVIEW DEBIT -> TRANSFER_INTERNAL and Tab 2 links pairs."""
    db_path = tmp_path / "own_transfer_flows.db"
    mgr = DatabaseManager(db_path)
    try:
        mgr.update_wallet_opening_balance(WALLET_HABIBMETRO, 50000.0)
        mgr.update_wallet_opening_balance(WALLET_NAYAPAY, 5000.0)
        mgr.upsert_many(
            [
                TransactionRecord(
                    message_id="QT_TR_1",
                    tx_date="2026-11-10",
                    tx_timestamp="2026-11-10 09:00:00",
                    account_name=WALLET_HABIBMETRO,
                    tx_type="DEBIT",
                    amount=4000.0,
                    payee="IBFT UNKNOWN SELF TRANSFER",
                    category="Uncategorized",
                    cost_bucket="VARIABLE",
                    review_status="NEEDS_REVIEW",
                    source="GMAIL_HMB",
                ),
                TransactionRecord(
                    message_id="PAIR_TR_OUT",
                    tx_date="2026-11-09",
                    tx_timestamp="2026-11-09 11:00:00",
                    account_name=WALLET_HABIBMETRO,
                    tx_type="DEBIT",
                    amount=7500.0,
                    payee="RAAST IBFT SELF",
                    category="Shopping",
                    cost_bucket="VARIABLE",
                    review_status="AUTO_VERIFIED",
                    source="GMAIL_HMB",
                ),
                TransactionRecord(
                    message_id="PAIR_TR_IN",
                    tx_date="2026-11-09",
                    tx_timestamp="2026-11-09 11:03:00",
                    account_name=WALLET_NAYAPAY,
                    tx_type="CREDIT",
                    amount=7500.0,
                    payee="Received from HabibMetro",
                    category="Other Inflow",
                    cost_bucket="INFLOW",
                    review_status="AUTO_VERIFIED",
                    source="PDF_IMPORT",
                ),
            ]
        )
        all_tx = mgr.get_transactions()
        qt_id = next(t.id for t in all_tx if t.message_id == "QT_TR_1")
        pair_out_id = next(t.id for t in all_tx if t.message_id == "PAIR_TR_OUT")
        pair_in_id = next(t.id for t in all_tx if t.message_id == "PAIR_TR_IN")
    finally:
        mgr.close()
    assert qt_id is not None and pair_out_id is not None and pair_in_id is not None

    at = _create_app(monkeypatch, db_path)

    # 1. Convert top pending NEEDS_REVIEW DEBIT into HabibMetro -> NayaPay TRANSFER_INTERNAL via Quick-Tag
    at.selectbox(key=f"t1_qt_type_{qt_id}").set_value("TRANSFER_INTERNAL").run()
    at.selectbox(key=f"t1_qt_target_{qt_id}").set_value(WALLET_NAYAPAY).run()
    at.button(key=f"t1_qt_save_{qt_id}").click().run()
    assert list(at.exception) == []
    assert list(at.error) == []
    assert at.radio(key="ptos_active_tab").value == TAB_HOME

    # 2. Switch to Tab 2 and 1-click link the remaining DEBIT + CREDIT pair across HabibMetro -> NayaPay
    _switch_tab(at, TAB_TRANSACTIONS)
    at.button(key=f"t2_pair_link_{pair_out_id}_{pair_in_id}").click().run()
    assert list(at.exception) == []
    assert list(at.error) == []
    assert at.radio(key="ptos_active_tab").value == TAB_TRANSACTIONS

    mgr_fin = DatabaseManager(db_path)
    try:
        qt_tx = mgr_fin.get_transaction_by_id(qt_id)
        linked_tx = mgr_fin.get_transaction_by_id(pair_out_id)
        assert qt_tx is not None and qt_tx.tx_type == "TRANSFER_INTERNAL"
        assert qt_tx.target_account == WALLET_NAYAPAY
        assert qt_tx.cost_bucket == "IGNORE"
        assert linked_tx is not None and linked_tx.tx_type == "TRANSFER_INTERNAL"
        assert linked_tx.target_account == WALLET_NAYAPAY
        assert mgr_fin.get_transaction_by_id(pair_in_id) is None
        assert mgr_fin.get_wallet(WALLET_HABIBMETRO).current_balance == pytest.approx(38500.0)
        assert mgr_fin.get_wallet(WALLET_NAYAPAY).current_balance == pytest.approx(16500.0)
    finally:
        mgr_fin.close()


def test_tab2_re_editing_and_reapplying_rule_updates_all_similar_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-editing a previously applied row and clicking 'Save & Apply Rule' updates all similar rows."""
    db_path = tmp_path / "reapply_rule.db"
    mgr = DatabaseManager(db_path)
    try:
        mgr.upsert_many(
            [
                apply_rules(_parsed("RP_1", "ZAMEEN ROASTERY #101", 900.0, tx_date="2026-11-08"), mgr),
                apply_rules(_parsed("RP_2", "ZAMEEN ROASTERY #202", 1100.0, tx_date="2026-11-09"), mgr),
                apply_rules(_parsed("TR_1", "OWN IBFT TRANSFER 1", 3000.0, tx_date="2026-11-10", account_name=WALLET_HABIBMETRO), mgr),
                apply_rules(_parsed("TR_2", "OWN IBFT TRANSFER 2", 4000.0, tx_date="2026-11-11", account_name=WALLET_HABIBMETRO), mgr),
            ]
        )
        by_msg = {t.message_id: t.id for t in mgr.get_transactions()}
        rp1_id = by_msg["RP_1"]
        tr1_id = by_msg["TR_1"]
    finally:
        mgr.close()
    assert rp1_id is not None and tr1_id is not None

    at = _create_app(monkeypatch, db_path)
    _switch_tab(at, TAB_TRANSACTIONS)

    # Pass 1: Tag RP_1 with keyword ZAMEEN ROASTERY -> Food & Dining
    at.selectbox(key="t2_selected_tx_id").set_value(rp1_id).run()
    at.selectbox(key=f"t2_edit_cat_{rp1_id}").set_value("Food & Dining").run()
    at.text_input(key=f"t2_edit_alias_{rp1_id}").set_value("Zameen Coffee")
    at.text_input(key=f"t2_edit_kw_{rp1_id}").set_value("ZAMEEN ROASTERY")
    at.button(key=f"t2_btn_learn_{rp1_id}").click().run()
    assert list(at.exception) == []
    assert list(at.error) == []

    # Pass 2: Re-edit RP_1 to Shopping (FIXED) + new alias; keyword pre-fills ZAMEEN ROASTERY automatically!
    at.selectbox(key="t2_selected_tx_id").set_value(rp1_id).run()
    assert at.text_input(key=f"t2_edit_kw_{rp1_id}").value == "ZAMEEN ROASTERY"
    at.selectbox(key=f"t2_edit_cat_{rp1_id}").set_value("Shopping").run()
    at.selectbox(key=f"t2_edit_bkt_{rp1_id}").set_value("FIXED").run()
    at.text_input(key=f"t2_edit_alias_{rp1_id}").set_value("Zameen Mart")
    at.button(key=f"t2_btn_learn_{rp1_id}").click().run()
    assert list(at.exception) == []
    assert list(at.error) == []

    # Also convert OWN IBFT TRANSFER group from DEBIT -> TRANSFER_INTERNAL -> NayaPay across both rows
    at.selectbox(key="t2_selected_tx_id").set_value(tr1_id).run()
    at.selectbox(key=f"t2_edit_type_{tr1_id}").set_value("TRANSFER_INTERNAL").run()
    at.selectbox(key=f"t2_edit_target_{tr1_id}").set_value(WALLET_NAYAPAY).run()
    at.text_input(key=f"t2_edit_kw_{tr1_id}").set_value("OWN IBFT TRANSFER")
    at.button(key=f"t2_btn_learn_{tr1_id}").click().run()
    assert list(at.exception) == []
    assert list(at.error) == []

    mgr_chk = DatabaseManager(db_path)
    try:
        rows = {t.message_id: t for t in mgr_chk.get_transactions()}
        for msg in ("RP_1", "RP_2"):
            assert rows[msg].category == "Shopping"
            assert rows[msg].cost_bucket == "FIXED"
            assert rows[msg].merchant_alias == "Zameen Mart"
        for msg in ("TR_1", "TR_2"):
            assert rows[msg].tx_type == "TRANSFER_INTERNAL"
            assert rows[msg].target_account == WALLET_NAYAPAY
            assert rows[msg].cost_bucket == "IGNORE"
    finally:
        mgr_chk.close()


def test_lock_screen_gate_rate_limit_timeout_and_manual_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify App Lock Screen blocks data access, enforces cooldowns/timeouts, and unlocks cleanly."""
    import time as _time
    from src.security import hash_password

    db_path = tmp_path / "locked_ui.db"
    with DatabaseManager(db_path) as mgr:
        mgr.upsert_many(
            [
                apply_rules(
                    _parsed("LOCK_TX_1", "Private Wealth Transfer", 9500.0, tx_date="2026-11-10"),
                    mgr,
                )
            ]
        )

    monkeypatch.setenv("TREASURY_APP_PASSWORD_HASH", hash_password("VaultPass#2026"))
    monkeypatch.setenv("TREASURY_SESSION_TIMEOUT_MINUTES", "5")
    at = _create_app(monkeypatch, db_path)

    has_key = lambda widget_list, target_key: any(
        getattr(w, "key", None) == target_key for w in widget_list
    )

    # 1. Locked by default: lock input shown, main tabs and private ledger payees hidden
    assert list(at.exception) == []
    assert has_key(at.text_input, "ptos_lock_password")
    assert not has_key(at.radio, "ptos_active_tab")
    rendered_text = " ".join(m.value for m in at.markdown)
    assert "Private Wealth Transfer" not in rendered_text

    # 2. Five failed unlock attempts trigger brute-force cooldown lockout
    for attempt in range(5):
        at.text_input(key="ptos_lock_password").set_value(f"wrong-{attempt}")
        at.button[0].click().run()
    assert any(
        "wait 30 second" in e.value.lower() or "locked for 30 second" in e.value.lower()
        for e in at.error
    )
    assert at.text_input(key="ptos_lock_password").disabled is True

    # Reset limiter & unlock with valid credentials
    from src.security import get_auth_rate_limiter

    get_auth_rate_limiter().reset()
    at.run()
    at.text_input(key="ptos_lock_password").set_value("VaultPass#2026")
    at.button[0].click().run()

    assert list(at.exception) == []
    assert list(at.error) == []
    assert has_key(at.radio, "ptos_active_tab")
    assert has_key(at.button, "ptos_lock_btn")

    # 3. Simulate idle timeout expiry (> 5 min) -> automatically re-locks
    at.session_state["ptos_auth_last_active"] = _time.monotonic() - 600.0
    at.run()
    assert has_key(at.text_input, "ptos_lock_password")
    assert any("inactivity" in info.value.lower() for info in at.info)

    # Re-unlock then test manual Lock button in top bar
    at.text_input(key="ptos_lock_password").set_value("VaultPass#2026")
    at.button[0].click().run()
    at.button(key="ptos_lock_btn").click().run()
    assert has_key(at.text_input, "ptos_lock_password")
    assert not has_key(at.radio, "ptos_active_tab")


def test_tab5_cloud_vault_auto_push_and_manual_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify Tab 5 encrypted cloud push/pull controls and mutation auto-sync to Gist."""
    import json
    import urllib.request
    from src.security import GIST_BACKUP_FILENAME

    gist_store: dict[str, str] = {}

    class _Resp:
        def __init__(self, obj: dict) -> None:
            self._data = json.dumps(obj).encode("utf-8")

        def read(self) -> bytes:
            return self._data

        def __enter__(self) -> "_Resp":
            return self

        def __exit__(self, *args) -> None:
            return None

    def _fake_urlopen(req: urllib.request.Request, timeout: int = 10, context=None) -> _Resp:
        if req.get_method() == "PATCH":
            payload = json.loads((req.data or b"{}").decode("utf-8"))
            gist_store["content"] = payload["files"][GIST_BACKUP_FILENAME]["content"]
            return _Resp({"id": "12345678abcdef"})
        return _Resp(
            {"files": {GIST_BACKUP_FILENAME: {"content": gist_store.get("content", "")}}}
        )

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setenv("GITHUB_BACKUP_TOKEN", "ghp_uiTestToken_12345678")
    monkeypatch.setenv("GITHUB_BACKUP_GIST_ID", "12345678abcdef")
    monkeypatch.setenv("TREASURY_BACKUP_KEY", "UiVaultSecret!2026")

    db_path = tmp_path / "tab5_cloud.db"
    with DatabaseManager(db_path) as mgr:
        mgr.upsert_many(
            [
                apply_rules(
                    _parsed("T5_CLOUD_1", "Vault Espresso Bar", 780.0, tx_date="2026-11-14"),
                    mgr,
                )
            ]
        )

    at = _create_app(monkeypatch, db_path)
    _switch_tab(at, TAB_SETUP)
    assert list(at.exception) == []
    assert list(at.error) == []

    # Manual Push Vault to Cloud button works
    at.button(key="t5_cloud_push_btn").click().run()
    assert list(at.exception) == []
    assert list(at.error) == []
    assert "content" in gist_store
    assert "Vault Espresso Bar" not in gist_store["content"]

    # Manual Pull Vault from Cloud restores cleanly
    at.button(key="t5_cloud_pull_btn").click().run()
    assert list(at.exception) == []
    assert list(at.error) == []

    # Quick-Tag Verify/Save mutation on Tab 1 triggers automatic cloud vault sync
    _switch_tab(at, TAB_HOME)
    gist_store["content"] = "stale"
    with DatabaseManager(db_path) as mgr:
        tx_id = mgr.get_transactions()[0].id
    assert tx_id is not None
    at.button(key=f"t1_qt_save_{tx_id}").click().run()
    assert list(at.exception) == []
    assert list(at.error) == []
    assert gist_store["content"] != "stale"
    assert "Vault Espresso Bar" not in gist_store["content"]




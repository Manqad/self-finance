"""Clean facade re-exporting all 5 redesigned treasury views, helpers, and patchable ingestion hooks."""

from __future__ import annotations

from src.ingestion.gmail_service import (
    fetch_emails_with_report,
    get_gmail_auth_status,
)
from src.ingestion.pdf_importer import import_statement_file
from src.ui.state import (
    ALL_TABS,
    SLUG_TO_TAB,
    TAB_BUDGETS,
    TAB_HOME,
    TAB_INSIGHTS,
    TAB_SETUP,
    TAB_TO_SLUG,
    TAB_TRANSACTIONS,
    _set_flash,
    drain_toasts,
    invalidate_all_caches,
    navigate_to_tab,
    notify_success,
    open_dialog,
    render_flash_banner,
)
from src.ui.views_budgets import _queue_cat_limit_sync, render_budgets_tab
from src.ui.views_common import (
    _BACKUP_DIR,
    _FIXTURES_DIR,
    extract_fixture_fragments as _extract_fixture_fragments,
    ingest_parsed_batch as _ingest_parsed_batch,
    load_html_fixtures,
    load_html_fixtures as _load_html_fixtures,
)
from src.ui.views_home import render_home_tab
from src.ui.views_insights import render_insights_tab
from src.ui.views_ledger import render_transactions_tab
from src.ui.views_setup import render_sync_setup_tab

__all__ = [
    "ALL_TABS",
    "SLUG_TO_TAB",
    "TAB_BUDGETS",
    "TAB_HOME",
    "TAB_INSIGHTS",
    "TAB_SETUP",
    "TAB_TO_SLUG",
    "TAB_TRANSACTIONS",
    "_BACKUP_DIR",
    "_FIXTURES_DIR",
    "_extract_fixture_fragments",
    "_ingest_parsed_batch",
    "_load_html_fixtures",
    "_queue_cat_limit_sync",
    "_set_flash",
    "drain_toasts",
    "fetch_emails_with_report",
    "get_gmail_auth_status",
    "import_statement_file",
    "invalidate_all_caches",
    "load_html_fixtures",
    "navigate_to_tab",
    "notify_success",
    "open_dialog",
    "render_budgets_tab",
    "render_flash_banner",
    "render_home_tab",
    "render_insights_tab",
    "render_sync_setup_tab",
    "render_transactions_tab",
]

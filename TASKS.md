# Multi-Agent Execution Plan (`TASKS.md`)

## Agent Roster & Strict Ownership
- **Agent 1 (Lead Architect & DevOps):** Owns scaffolding, `.venv`, `src/models.py`, and cross-module integration.
- **Agent 2 (Database, Wallets & Math Engine):** Owns `src/database/*` and `src/engine/*`. Implements the 6 SQLite tables, double-entry wallet balance updater, rule learning engine, and financial metrics.
- **Agent 3 (Email & Statement Ingestion Pipeline):** Owns `src/ingestion/*`. Implements Gmail OAuth poller, Outlook-forwarded HabibMetro parser, NayaPay receipt/tax parser, and NayaPay/HMB PDF statement importer.
- **Agent 4 (Batwa-Style Streamlit UI Engineer):** Owns `src/ui/*`. Builds the 5-tab mobile-first UI, Wallet Cards, Category Budget bars, "Needs Review" inbox queue, and Plotly analytics.
- **Agent 5 (QA, Fixtures & Live Browser Verification):** Owns `tests/*`. Writes mock HTML/PDF fixtures, runs `pytest`, seeds realistic Month 1 data, and verifies the live UI via Antigravity's headless browser.

---

## Phase 1: Foundation & Strict Data Contracts (Agent 1)
- [x] **Task 1.1:** Create directory structure matching `ARCHITECTURE.md` and configure `.gitignore`.
- [x] **Task 1.2:** Create `requirements.txt` (`streamlit`, `google-api-python-client`, `google-auth-httplib2`, `google-auth-oauthlib`, `beautifulsoup4`, `lxml`, `pandas`, `plotly`, `pypdf`, `pydantic`, `pytest`) and install in `.venv`.
- [x] **Task 1.3:** Implement `src/models.py` with Pydantic schemas for `AccountWallet`, `TransactionRecord`, `BudgetConfig`, `CategoryBudget`, `CategoryRule`, `RecurringCommitment`, and `DashboardMetrics`.

## Phase 2: Database, Wallets & Financial Engine (Agent 2)
- [x] **Task 2.1:** Implement `src/database/db_manager.py` managing all 6 tables (`accounts`, `budget_config`, `category_budgets`, `transactions`, `category_rules`, `recurring_commitments`), including atomic double-entry balance recalculation on `accounts` whenever transactions change.
- [x] **Task 2.2:** Implement `src/database/seed_rules.py` to seed the 4 default Wallets (`HabibMetro`, `NayaPay`, `Cash in Hand`, `Money Market Fund`), default Pakistani category budgets with emoji icons, and 35+ Pakistani merchant rules.
- [x] **Task 2.3:** Implement `src/engine/categorizer.py` with `apply_rules(tx)` and `learn_new_rule(payee, alias, category, cost_bucket)` which saves the rule and retroactively updates all matching unverified transactions in SQLite.
- [x] **Task 2.4:** Implement `src/engine/recurring.py` and `src/engine/metrics.py` calculating all 8 core equations in `SPECS.md`, per-category budget utilization percentages, top 5 merchants by spend, and Month-over-Month daily burn pace.

## Phase 3: Email & PDF Statement Ingestion (Agent 3)
- [x] **Task 3.1:** Implement `src/ingestion/gmail_service.py` with read-only OAuth2 (`credentials.json` -> `token.json`), query filtering, OTP/decline skipping, and graceful offline demo fallback.
- [x] **Task 3.2:** Implement `src/ingestion/hmb_parser.py` to parse Outlook-forwarded `Habib Metro E-Service` emails, automatically detecting NayaPay top-ups (`TRANSFER_INTERNAL` -> target `NayaPay`) and AMC deposits (`TRANSFER_MMF` -> target `Money Market Fund`).
- [x] **Task 3.3:** Implement `src/ingestion/nayapay_parser.py` to parse NayaPay HTML receipts, extracting merchant, Raast IBAN/recipient, base PKR amount, and separating international WHT/FX fees into `tax_fee_amount`.
- [x] **Task 3.4:** Implement `src/ingestion/pdf_importer.py` using `pypdf` and `pandas` to parse uploaded NayaPay PDF statements and HabibMetro PDF/CSV statements with hash-based deduplication.

## Phase 4: Batwa-Parity Mobile-First UI (Agent 4)
- [x] **Task 4.1:** Build `src/ui/components.py` containing:
  - **Wallet Balance Carousel:** Cards showing live balances for `HabibMetro`, `NayaPay`, `Cash in Hand`, and `Money Market Fund`.
  - **Safe Daily Spend & Burn Gauge:** Hero card showing `Safe Daily Spend (PKR/day)`, `Days Left in Month`, and overall spending cap gauge.
  - **Category Budget Bars:** Batwa-style horizontal bars with emoji icons, `Spent / Limit`, and Green/Amber/Red thresholds.
- [x] **Task 4.2:** Build `src/ui/views.py` with 5 navigation tabs:
  - **Tab 1: Home & Wallets:** Wallet balance cards, Hero Burn Gauge, "Needs Review (N)" quick-action banner, Upcoming Bills checklist, and Quick-Add Manual/Cash Transaction modal.
  - **Tab 2: Transactions & Review Queue:** Searchable filter bar (by Wallet, Category, Bucket, Date Range, or "Needs Review Only") + interactive editor where tagging a payee offers a one-click "Save & Apply Rule to All Similar" button.
  - **Tab 3: Budgets & Commitments:** Per-category monthly PKR limit sliders/inputs, progress bars, and Recurring Bills / Subscriptions manager.
  - **Tab 4: Insights & Leakage Analytics:** Plotly charts for Fixed vs. Variable vs. Leakage split, Daily Cumulative Spend curve, Top 5 Payees bar chart, and Total Tax/Bank Fee Leakage counter.
  - **Tab 5: Sync, PDF Import & Setup:** "Sync Gmail Now" button, NayaPay/HMB PDF & CSV drag-and-drop uploader, Month 1 Income/Emergency Target config, Wallet starting balance adjustments, and 1-click CSV/JSON Backup export.
- [x] **Task 4.3:** Create `src/ui/app.py` with clean mobile CSS styling so it works seamlessly when added to a smartphone home screen.

## Phase 5: QA, Mock Fixtures & Browser Verification (Agent 5)
- [x] **Task 5.1:** Create realistic HTML fixtures in `tests/fixtures/sample_hmb.html` (forwarded Outlook HMB debit + NayaPay transfer + Meezan MMF sweep) and `tests/fixtures/sample_nayapay.html` (Foodpanda order + Netflix receipt with WHT fee).
- [x] **Task 5.2:** Write and execute `tests/test_parsers.py`, `tests/test_engine.py`, and `tests/test_db.py` via `pytest`. Verify that double-entry transfers update both wallet balances accurately without inflating `Total Monthly Burn`.
- [x] **Task 5.3:** Seed `data/treasury.db` with realistic sample Month 1 transactions across all 4 wallets, launch `streamlit run src/ui/app.py --server.port 8501 --server.headless true`, navigate through all 5 tabs using Antigravity's browser tool, verify zero runtime errors, and capture verification screenshots.
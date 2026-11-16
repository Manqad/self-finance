### File 2: `SPECS.md` (Upgraded Functional & Mathematical Specifications)

```markdown
# Functional & Mathematical Specifications (`SPECS.md`)

## 1. Batwa Core Feature Parity Requirements

### A. Multi-Account Wallet Sync & Double-Entry Transfers
1. **Four Default Wallets:** Seed `accounts` with `HabibMetro` (BANK), `NayaPay` (WALLET), `Cash in Hand` (CASH), and `Money Market Fund` (INVESTMENT).
2. **Real-Time Wallet Balance Recomputation:**
   Whenever a transaction is inserted, edited, or deleted, recalculate `current_balance` for each account:
   - If `tx_type == 'CREDIT'`: adds `amount` to `account_name`.
   - If `tx_type == 'DEBIT'`: subtracts `(amount + tax_fee_amount)` from `account_name`.
   - If `tx_type == 'TRANSFER_INTERNAL'` (e.g., HabibMetro -> NayaPay, or ATM Withdrawal -> Cash in Hand):
     - Subtracts `amount + tax_fee_amount` from `account_name` (`HabibMetro`) AND adds `amount` to `target_account` (`NayaPay` or `Cash in Hand`).
     - Sets `cost_bucket = 'IGNORE'` so wallet top-ups NEVER double-count as spending. (When `envelope_mode = True`, net `HabibMetro -> NayaPay` envelope transfers minus `NayaPay -> HabibMetro/Cash` returns count toward macro `B_var`, while `NayaPay` debits/fees are excluded from macro burn, `B_leak`, and `B_total`; Category Sub-Budgets and Top Merchants continue tracking underlying merchant `DEBIT`s.)
   - If `tx_type == 'TRANSFER_MMF'` (HabibMetro -> Money Market Fund):
     - Subtracts `amount + tax_fee_amount` from `account_name` AND adds `amount` to `Money Market Fund`.
     - Sets `cost_bucket = 'INVESTMENT'`.

### B. Batwa "Needs Review" Inbox Queue & One-Click Rule Learning
1. Any transaction where `payee` does not match a rule in `category_rules` defaults to `category = 'Uncategorized'` and `review_status = 'NEEDS_REVIEW'`.
2. In the UI, show a prominent **"Transactions to Review (N)"** badge at the top of the screen.
3. When a user assigns a category to an uncategorized transaction and checks **"Always remember this payee"**, automatically insert/update a row in `category_rules` AND retro-actively update all historical transactions from that same `payee`.

### C. International Card Tax & Fee Breakdown (NayaPay Specific)
NayaPay receipts for international merchants (Netflix, Spotify, Steam, Google, Apple, OpenAI) include Withholding Tax (WHT) and FX/Service fees.
- If a receipt contains tax/fee lines, extract the base amount into `amount` and the extra tax/fees into `tax_fee_amount`.
- Automatically log or aggregate `tax_fee_amount` under the `LEAKAGE` bucket (`Bank & Tax Friction`) so the user sees exactly how much money was lost to FBR taxes and bank charges.

### D. Per-Category Sub-Budgets & Alert Bars
Alongside the global Monthly Spending Cap, users can set individual monthly PKR limits per category (e.g., `Food & Dining`: Rs. 15,000, `Fuel & Transit`: Rs. 12,000, `Shopping`: Rs. 10,000).
- Render horizontal Batwa-style progress bars for each category showing `Spent / Limit`, remaining PKR, and color coding:
  - `< 75%`: Emerald Green
  - `75% – 95%`: Amber Warning
  - `> 95%`: Crimson Alert ("Over Budget!")

### E. Recurring Bills & Subscription Radar
1. Match incoming transactions against `recurring_commitments`. If a transaction matches the keyword/title in the current `YYYY-MM`, mark `is_paid_this_month = 1`.
2. Display an **"Upcoming Bills This Month"** widget showing unpaid fixed bills and the exact total PKR still required before month-end.

---

## 2. Core Financial Defense Equations (`src/engine/metrics.py`)
For any selected calendar month (`YYYY-MM`):

1. **Total Spending Cap ($C_{\text{total}}$):**
   $$C_{\text{total}} = \text{fixed\_cost\_baseline} + \text{variable\_spending\_cap}$$

2. **Actual Fixed Burn ($B_{\text{fixed}}$):**
   $$B_{\text{fixed}} = \sum \text{amount} \quad \text{WHERE } \text{tx\_type} = \text{'DEBIT'} \text{ AND } \text{cost\_bucket} = \text{'FIXED'}$$

3. **Actual Variable Burn ($B_{\text{var}}$):**
   $$B_{\text{var}} = \sum \text{amount} \quad \text{WHERE } \text{tx\_type} = \text{'DEBIT'} \text{ AND } \text{cost\_bucket} = \text{'VARIABLE'}$$

4. **Actual Friction & Leakage ($B_{\text{leak}}$):**
   $$B_{\text{leak}} = \sum \text{amount}_{\text{tx\_type}=\text{'DEBIT'},\, \text{cost\_bucket}=\text{'LEAKAGE'}} + \sum \text{tax\_fee\_amount}_{\text{all outflows (DEBIT + transfers)}}$$

5. **Total Monthly Burn ($B_{\text{total}}$):**
   $$B_{\text{total}} = B_{\text{fixed}} + B_{\text{var}} + B_{\text{leak}}$$

6. **Remaining Monthly Allowance ($A_{\text{rem}}$):**
   $$A_{\text{rem}} = C_{\text{total}} - B_{\text{total}}$$

7. **Safe Daily Spend Rate ($D_{\text{safe}}$):**
   $$D_{\text{safe}} = \max\left(0, \frac{A_{\text{rem}}}{\max(1, \text{Days Remaining in Month})}\right)$$

8. **Emergency Reserve Progress ($E_{\text{current}} / E_{\text{target}}$):**
   $$E_{\text{target}} = \text{fixed\_cost\_baseline} \times \text{emergency\_multiplier}$$
   $$E_{\text{current}} = \text{current\_balance of 'Money Market Fund' wallet}$$

---

## 3. Email & PDF Ingestion Rules

### A. Gmail Query String
`q = '(from:nayapay OR from:habibmetro OR subject:"HabibMetro" OR subject:"NayaPay" OR "HABIBMETRO" OR "Habib Metro E-Service")'`
- Must inspect inner body of Microsoft 365 Outlook auto-forwarded emails (`From: Habib Metro E-Service`).
- Ignore OTP emails (match `(?i)(otp|one time password|verification code|login attempt)` -> skip).
- Ignore declined/failed alerts (match `(?i)(declined|failed|unsuccessful|insufficient)` -> skip).

### B. Multi-Bank PDF & CSV Backfill (`pdf_importer.py`)
- Support drag-and-drop upload of **NayaPay PDF E-Statements** and **HabibMetro PDF/CSV Statements**.
- Generate deterministic `message_id = f"PDF_{account}_{tx_date}_{amount}_{payee[:10]}"` (hashed) so uploading overlapping PDF statements or syncing Gmail never creates duplicate rows.
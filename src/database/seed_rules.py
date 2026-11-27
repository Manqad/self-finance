"""Seed data for Personal Treasury OS wallets, budgets, Pakistani merchant rules, and recurring bills."""

from __future__ import annotations

from src.models import (
    DEFAULT_WALLETS,
    AccountWallet,
    BudgetConfig,
    CategoryBudget,
    CategoryRule,
    CostBucket,
    RecurringCommitment,
)

DEFAULT_CATEGORY_BUDGETS: tuple[tuple[str, str, CostBucket, float], ...] = (
    ("Bills & Utilities", "⚡", "FIXED", 25000.0),
    ("Fuel & Commute", "⛽", "FIXED", 15000.0),
    ("Groceries", "🛒", "FIXED", 25000.0),
    ("Housing & Rent", "🏠", "FIXED", 35000.0),
    ("Healthcare", "💊", "FIXED", 8000.0),
    ("Food & Dining", "🍔", "VARIABLE", 15000.0),
    ("Subscriptions & Tech", "💻", "VARIABLE", 8000.0),
    ("Shopping", "🛍️", "VARIABLE", 10000.0),
    ("Cash Envelope", "💵", "VARIABLE", 10000.0),
    ("Bank & Tax Friction", "💸", "LEAKAGE", 3000.0),
    ("Uncategorized", "❓", "VARIABLE", 0.0),
)

DEFAULT_INFLOW_CATEGORIES: tuple[tuple[str, str, CostBucket, float], ...] = (
    ("Salary & Income", "💰", "INFLOW", 0.0),
    ("Freelance & Client", "💼", "INFLOW", 0.0),
    ("Business & Sales", "🏢", "INFLOW", 0.0),
    ("Profit & Dividends", "📈", "INFLOW", 0.0),
    ("Refunds & Cashback", "↩️", "INFLOW", 0.0),
    ("Reimbursements", "🤝", "INFLOW", 0.0),
    ("Gifts & Family Support", "🎁", "INFLOW", 0.0),
    ("Other Inflow", "💵", "INFLOW", 0.0),
)

DEFAULT_TRANSFER_CATEGORIES: tuple[tuple[str, str, CostBucket, float], ...] = (
    ("Internal Transfer", "🔄", "IGNORE", 0.0),
    ("Wallet Top-Up", "🔁", "IGNORE", 0.0),
    ("Cash Withdrawal", "🏧", "IGNORE", 0.0),
    ("MMF Redemption", "🏦", "IGNORE", 0.0),
    ("Money Market Fund", "📈", "INVESTMENT", 0.0),
)

# (keyword, merchant_alias, category, cost_bucket, priority)
DEFAULT_CATEGORY_RULES: tuple[tuple[str, str, str, CostBucket, int], ...] = (
    # --- Bank & Tax Friction (LEAKAGE) - higher priority to catch fees/taxes before merchant/wallet names ---
    ("WITHHOLDING TAX", "Withholding Tax (WHT)", "Bank & Tax Friction", "LEAKAGE", 35),
    ("BANK CHARGES", "Bank Service Charges", "Bank & Tax Friction", "LEAKAGE", 35),
    ("ANNUAL FEE", "Annual Card/Account Fee", "Bank & Tax Friction", "LEAKAGE", 35),
    ("ATM CARD FEE", "ATM Card Fee", "Bank & Tax Friction", "LEAKAGE", 35),
    ("SMS FEE", "SMS Alert Fee", "Bank & Tax Friction", "LEAKAGE", 35),
    ("FX FEE", "International FX Fee", "Bank & Tax Friction", "LEAKAGE", 35),
    ("FX SETTLEMENT", "International FX Fee", "Bank & Tax Friction", "LEAKAGE", 35),
    ("FOREIGN FEE", "Foreign Transaction Fee", "Bank & Tax Friction", "LEAKAGE", 35),
    ("FAILED INT. TRANSACTION", "Failed International Tx Fee", "Bank & Tax Friction", "LEAKAGE", 35),
    ("INT. TRANSACTION FEE", "International Tx Fee", "Bank & Tax Friction", "LEAKAGE", 35),
    ("SST ON INT", "SST on Int. Fees", "Bank & Tax Friction", "LEAKAGE", 35),
    ("SERVICE FEE", "Service Fee", "Bank & Tax Friction", "LEAKAGE", 35),
    ("CONVENIENCE FEE", "Convenience Fee", "Bank & Tax Friction", "LEAKAGE", 35),
    ("FBR TAX", "FBR Tax / WHT", "Bank & Tax Friction", "LEAKAGE", 35),
    ("SURCHARGE", "Bank / Card Surcharge", "Bank & Tax Friction", "LEAKAGE", 35),
    ("WHT", "Withholding Tax (WHT)", "Bank & Tax Friction", "LEAKAGE", 30),
    ("FED", "Federal Excise Duty (FED)", "Bank & Tax Friction", "LEAKAGE", 30),
    # --- Money Market Fund / AMC Sweeps (INVESTMENT) ---
    ("AL MEEZAN", "Al Meezan Investment", "Money Market Fund", "INVESTMENT", 30),
    ("MEEZAN INVESTMENT", "Al Meezan Investment", "Money Market Fund", "INVESTMENT", 30),
    ("MEEZAN AMC", "Al Meezan Investment", "Money Market Fund", "INVESTMENT", 30),
    ("MEEZAN ROZANA", "Meezan Rozana Amdani Fund", "Money Market Fund", "INVESTMENT", 30),
    ("MEEZAN CASH FUND", "Meezan Cash Fund", "Money Market Fund", "INVESTMENT", 30),
    ("MRAF", "Meezan Rozana Amdani Fund", "Money Market Fund", "INVESTMENT", 30),
    ("MCF", "Meezan Cash Fund", "Money Market Fund", "INVESTMENT", 30),
    ("NBP FUND", "NBP Funds MMF", "Money Market Fund", "INVESTMENT", 30),
    ("UBL FUND", "UBL Fund Managers", "Money Market Fund", "INVESTMENT", 30),
    ("AL-AMEEN FUNDS", "Al-Ameen Funds", "Money Market Fund", "INVESTMENT", 30),
    ("AL-AMEEN FUND", "Al-Ameen Funds", "Money Market Fund", "INVESTMENT", 30),
    ("ALFALAH GHP", "Alfalah GHP Investments", "Money Market Fund", "INVESTMENT", 30),
    ("ALFALAH ASSET", "Alfalah Asset Management", "Money Market Fund", "INVESTMENT", 30),
    ("ALFALAH INVESTMENT", "Alfalah Investments", "Money Market Fund", "INVESTMENT", 30),
    # --- Salary & Inflows (INFLOW) ---
    ("REMUNERATION", "Monthly Salary", "Salary & Income", "INFLOW", 25),
    ("PAYROLL", "Payroll Deposit", "Salary & Income", "INFLOW", 25),
    ("SALARY", "Monthly Salary", "Salary & Income", "INFLOW", 25),
    ("FREELANCE", "Freelance Income", "Freelance & Client", "INFLOW", 25),
    ("DIVIDEND", "Dividend Payout", "Profit & Dividends", "INFLOW", 25),
    ("PROFIT", "Savings / MMF Profit", "Profit & Dividends", "INFLOW", 20),
    ("CASHBACK", "Cashback Reward", "Refunds & Cashback", "INFLOW", 24),
    ("REFUND", "Merchant Refund", "Refunds & Cashback", "INFLOW", 24),
    # --- Utilities (FIXED) ---
    ("K-ELECTRIC", "K-Electric", "Bills & Utilities", "FIXED", 20),
    ("KARACHI ELECTRIC", "K-Electric", "Bills & Utilities", "FIXED", 20),
    ("K ELECTRIC", "K-Electric", "Bills & Utilities", "FIXED", 20),
    ("KE BILL", "K-Electric", "Bills & Utilities", "FIXED", 19),
    ("SNGPL", "Sui Northern Gas (SNGPL)", "Bills & Utilities", "FIXED", 20),
    ("SSGC", "Sui Southern Gas (SSGC)", "Bills & Utilities", "FIXED", 20),
    ("PTCL", "PTCL", "Bills & Utilities", "FIXED", 20),
    ("STORMFIBER", "StormFiber", "Bills & Utilities", "FIXED", 20),
    ("TRANSWORLD", "Transworld Home", "Bills & Utilities", "FIXED", 20),
    ("NAYATEL", "Nayatel", "Bills & Utilities", "FIXED", 20),
    ("LESCO", "LESCO Electricity", "Bills & Utilities", "FIXED", 20),
    ("IESCO", "IESCO Electricity", "Bills & Utilities", "FIXED", 20),
    # --- Housing & Healthcare (FIXED) ---
    ("HOUSE RENT", "House Rent", "Housing & Rent", "FIXED", 20),
    ("AGHA KHAN", "Aga Khan Hospital", "Healthcare", "FIXED", 20),
    ("AGA KHAN", "Aga Khan Hospital", "Healthcare", "FIXED", 20),
    ("CHUGHTAI", "Chughtai Lab", "Healthcare", "FIXED", 20),
    ("PHARMACY", "Pharmacy", "Healthcare", "FIXED", 15),
    ("SHIFA", "Shifa International", "Healthcare", "FIXED", 18),
    # --- Fuel & Commute (FIXED) ---
    ("TOTAL PARCO", "Total Parco", "Fuel & Commute", "FIXED", 20),
    ("GO PETROLEUM", "GO Petroleum", "Fuel & Commute", "FIXED", 20),
    ("ATTOCK", "Attock Petroleum", "Fuel & Commute", "FIXED", 20),
    ("SHELL", "Shell Pakistan", "Fuel & Commute", "FIXED", 18),
    ("PSO", "PSO Fuel", "Fuel & Commute", "FIXED", 18),
    ("BYKEA", "Bykea", "Fuel & Commute", "FIXED", 18),
    ("INDRIVE", "inDrive", "Fuel & Commute", "FIXED", 18),
    ("YANGO", "Yango", "Fuel & Commute", "FIXED", 18),
    ("CAREEM", "Careem", "Fuel & Commute", "FIXED", 18),
    # --- Groceries (FIXED) ---
    ("METRO CASH", "Metro Cash & Carry", "Groceries", "FIXED", 20),
    ("CARREFOUR", "Carrefour Pakistan", "Groceries", "FIXED", 20),
    ("CHASE UP", "Chase Up", "Groceries", "FIXED", 20),
    ("KRACRMART", "KrachiMart", "Groceries", "FIXED", 20),
    ("KRACHIMART", "KrachiMart", "Groceries", "FIXED", 20),
    ("AL-FATAH", "Al-Fatah", "Groceries", "FIXED", 20),
    ("AL FATAH", "Al-Fatah", "Groceries", "FIXED", 20),
    ("IMTIAZ SUPER", "Imtiaz Super Market", "Groceries", "FIXED", 21),
    ("IMTIAZ STORE", "Imtiaz Super Market", "Groceries", "FIXED", 21),
    ("IMTIAZ MEGA", "Imtiaz Super Market", "Groceries", "FIXED", 21),
    ("IMTIAZ", "Imtiaz Super Market", "Groceries", "FIXED", 20),
    ("NAHEED SUPER", "Naheed Super Market", "Groceries", "FIXED", 21),
    ("NAHEED.PK", "Naheed Super Market", "Groceries", "FIXED", 21),
    ("NAHEED", "Naheed Super Market", "Groceries", "FIXED", 20),
    ("SPRINGS", "Springs Store", "Groceries", "FIXED", 18),
    # --- Food & Dining (VARIABLE) ---
    ("FOODPANDA", "Foodpanda", "Food & Dining", "VARIABLE", 18),
    ("MCDONALDS", "McDonald's", "Food & Dining", "VARIABLE", 18),
    ("MCDONALD", "McDonald's", "Food & Dining", "VARIABLE", 18),
    ("KABABJEES", "Kababjees", "Food & Dining", "VARIABLE", 18),
    ("CHEEZIOUS", "Cheezious", "Food & Dining", "VARIABLE", 18),
    ("BROADWAY", "Broadway Pizza", "Food & Dining", "VARIABLE", 18),
    ("DOMINOS", "Domino's Pizza", "Food & Dining", "VARIABLE", 18),
    ("DOMINO'S", "Domino's Pizza", "Food & Dining", "VARIABLE", 18),
    ("KFC", "KFC Pakistan", "Food & Dining", "VARIABLE", 18),
    ("RESTAURANT", "Restaurant", "Food & Dining", "VARIABLE", 12),
    ("CAFETERIA", "Cafe / Cafeteria", "Food & Dining", "VARIABLE", 12),
    ("COFFEE", "Coffee Shop", "Food & Dining", "VARIABLE", 12),
    ("CAFE", "Cafe", "Food & Dining", "VARIABLE", 12),
    # --- Subscriptions & Tech (VARIABLE) ---
    ("NETFLIX", "Netflix", "Subscriptions & Tech", "VARIABLE", 20),
    ("SPOTIFY", "Spotify", "Subscriptions & Tech", "VARIABLE", 20),
    ("GOOGLE", "Google Services", "Subscriptions & Tech", "VARIABLE", 18),
    ("APPLE", "Apple Services", "Subscriptions & Tech", "VARIABLE", 18),
    ("STEAM", "Steam Store", "Subscriptions & Tech", "VARIABLE", 18),
    ("CLAUDE", "Anthropic Claude", "Subscriptions & Tech", "VARIABLE", 20),
    ("OPENAI", "OpenAI", "Subscriptions & Tech", "VARIABLE", 20),
    ("CURSOR", "Cursor AI", "Subscriptions & Tech", "VARIABLE", 20),
    # --- Shopping (VARIABLE) ---
    ("OUTFITTERS", "Outfitters", "Shopping", "VARIABLE", 18),
    ("KHAADI", "Khaadi", "Shopping", "VARIABLE", 18),
    ("SHOPPING", "Retail Shopping", "Shopping", "VARIABLE", 12),
    ("DARAZ", "Daraz.pk", "Shopping", "VARIABLE", 18),
    ("LAMA", "LAMA Retail", "Shopping", "VARIABLE", 18),
    ("ELO", "Export Leftovers (ELO)", "Shopping", "VARIABLE", 18),
    ("J.", "J. Junaid Jamshed", "Shopping", "VARIABLE", 18),
    # --- Cash & ATM (VARIABLE) ---
    ("CASH WITHDRAWAL", "ATM Cash Withdrawal", "Cash Envelope", "VARIABLE", 18),
    ("ATM", "ATM Cash Withdrawal", "Cash Envelope", "VARIABLE", 15),
)

DEFAULT_RECURRING_COMMITMENTS: tuple[tuple[str, str, float, int, str, CostBucket], ...] = (
    ("K-Electric Bill", "K-ELECTRIC", 15000.0, 18, "Bills & Utilities", "FIXED"),
    ("StormFiber Internet", "STORMFIBER", 4500.0, 10, "Bills & Utilities", "FIXED"),
    ("House Contribution", "HOUSE RENT", 35000.0, 5, "Housing & Rent", "FIXED"),
    ("Netflix", "NETFLIX", 1500.0, 12, "Subscriptions & Tech", "VARIABLE"),
    ("Spotify", "SPOTIFY", 650.0, 15, "Subscriptions & Tech", "VARIABLE"),
)


def get_default_wallets() -> list[AccountWallet]:
    """Return the 4 default Pakistani treasury wallets."""
    return [
        AccountWallet(
            account_name=name,
            account_type=acct_type,
            opening_balance=0.0,
            current_balance=0.0,
            currency="PKR",
            icon=icon,
        )
        for name, acct_type, icon in DEFAULT_WALLETS
    ]


def get_default_budget_config() -> BudgetConfig:
    """Return the default Month 1 Financial Defense configuration."""
    return BudgetConfig(
        id=1,
        monthly_income=200000.0,
        fixed_cost_baseline=80000.0,
        variable_spending_cap=40000.0,
        emergency_multiplier=6,
        envelope_mode=False,
    )


def get_default_non_expense_category_budgets() -> list[CategoryBudget]:
    """Return default Inflow and Own-Account Transfer categories."""
    rows = DEFAULT_INFLOW_CATEGORIES + DEFAULT_TRANSFER_CATEGORIES
    return [
        CategoryBudget(
            category=cat,
            emoji=emoji,
            cost_bucket=bucket,
            monthly_limit=limit,
            is_active=True,
        )
        for cat, emoji, bucket, limit in rows
    ]


def get_default_category_budgets() -> list[CategoryBudget]:
    """Return default Pakistani category budgets (outflow caps, inflow, and transfer categories)."""
    outflow = [
        CategoryBudget(
            category=cat,
            emoji=emoji,
            cost_bucket=bucket,
            monthly_limit=limit,
            is_active=True,
        )
        for cat, emoji, bucket, limit in DEFAULT_CATEGORY_BUDGETS
    ]
    return outflow + get_default_non_expense_category_budgets()


def get_default_category_rules() -> list[CategoryRule]:
    """Return pre-seeded Pakistani merchant & transaction rules."""
    return [
        CategoryRule(
            keyword=kw,
            merchant_alias=alias,
            category=cat,
            cost_bucket=bucket,
            priority=priority,
            is_user_defined=False,
        )
        for kw, alias, cat, bucket, priority in DEFAULT_CATEGORY_RULES
    ]


def get_default_recurring_commitments() -> list[RecurringCommitment]:
    """Return sensible initial recurring bills and subscriptions."""
    return [
        RecurringCommitment(
            title=title,
            match_keyword=kw,
            expected_amount=amount,
            due_day=due_day,
            category=cat,
            cost_bucket=bucket,
            is_paid_this_month=False,
            last_paid_month=None,
            is_active=True,
        )
        for title, kw, amount, due_day, cat, bucket in DEFAULT_RECURRING_COMMITMENTS
    ]

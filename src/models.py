"""Strict Pydantic data contracts for Personal Treasury OS."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

AccountType = Literal["BANK", "WALLET", "CASH", "INVESTMENT"]
TxType = Literal["DEBIT", "CREDIT", "TRANSFER_INTERNAL", "TRANSFER_MMF"]
CostBucket = Literal["INFLOW", "FIXED", "VARIABLE", "LEAKAGE", "INVESTMENT", "IGNORE"]
ReviewStatus = Literal["AUTO_VERIFIED", "NEEDS_REVIEW", "MANUALLY_VERIFIED"]
SourceChannel = Literal["GMAIL_HMB", "GMAIL_NAYAPAY", "PDF_IMPORT", "CSV_IMPORT", "MANUAL"]

WALLET_HABIBMETRO = "HabibMetro"
WALLET_NAYAPAY = "NayaPay"
WALLET_CASH = "Cash in Hand"
WALLET_MMF = "Money Market Fund"

DEFAULT_WALLETS: tuple[tuple[str, AccountType, str], ...] = (
    (WALLET_HABIBMETRO, "BANK", "🏦"),
    (WALLET_NAYAPAY, "WALLET", "📱"),
    (WALLET_CASH, "CASH", "💵"),
    (WALLET_MMF, "INVESTMENT", "📈"),
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}$")


def _normalize_date_and_timestamp(
    tx_date: str, tx_timestamp: Optional[str]
) -> tuple[str, Optional[str]]:
    """Normalize date strings to YYYY-MM-DD and keep timestamp date-aligned."""
    raw = tx_date.strip()
    if _DATE_RE.match(raw):
        datetime.strptime(raw, "%Y-%m-%d")
        norm_date = raw
        if tx_timestamp and _DATETIME_RE.match(tx_timestamp.strip()):
            clean_ts = tx_timestamp.strip().replace("T", " ")
            if clean_ts[:10] != norm_date:
                clean_ts = f"{norm_date} {clean_ts[11:]}"
            return norm_date, clean_ts
        return norm_date, tx_timestamp
    if _DATETIME_RE.match(raw):
        clean_ts = raw.replace("T", " ")
        dt = datetime.strptime(clean_ts, "%Y-%m-%d %H:%M:%S")
        norm_date = dt.strftime("%Y-%m-%d")
        if tx_timestamp and _DATETIME_RE.match(tx_timestamp.strip()):
            ts_clean = tx_timestamp.strip().replace("T", " ")
            if ts_clean[:10] != norm_date:
                ts_clean = f"{norm_date} {ts_clean[11:]}"
            return norm_date, ts_clean
        return norm_date, tx_timestamp or clean_ts
    raise ValueError(f"Invalid tx_date format '{tx_date}'; expected YYYY-MM-DD")


class AccountWallet(BaseModel):
    """Represents one of the user's tracked financial wallets/accounts."""

    model_config = ConfigDict(from_attributes=True, allow_inf_nan=False)

    id: Optional[int] = None
    account_name: str = Field(..., min_length=1)
    account_type: AccountType
    opening_balance: float = 0.0
    current_balance: float = 0.0
    balance_as_of: Optional[str] = None
    currency: str = "PKR"
    icon: str = "🏦"
    updated_at: Optional[str] = None


class ParsedTransaction(BaseModel):
    """Normalized transaction extracted from Gmail or PDF/CSV before categorization."""

    model_config = ConfigDict(from_attributes=True, allow_inf_nan=False)

    message_id: str = Field(..., min_length=1)
    tx_date: str
    tx_timestamp: Optional[str] = None
    account_name: str = Field(..., min_length=1)
    target_account: Optional[str] = None
    tx_type: TxType
    amount: float = Field(..., gt=0.0)
    tax_fee_amount: float = Field(default=0.0, ge=0.0)
    payee: str = Field(..., min_length=1)
    merchant_alias: Optional[str] = None
    source: SourceChannel
    payment_channel: Optional[str] = None
    raw_subject: Optional[str] = None
    notes: Optional[str] = None
    suggested_category: Optional[str] = None
    suggested_bucket: Optional[CostBucket] = None

    @field_validator("payee")
    @classmethod
    def _clean_payee(cls, value: str) -> str:
        cleaned = " ".join(value.split()).strip()
        if not cleaned:
            raise ValueError("payee cannot be blank")
        return cleaned

    @model_validator(mode="after")
    def _validate_dates(self) -> "ParsedTransaction":
        norm_date, norm_ts = _normalize_date_and_timestamp(
            self.tx_date, self.tx_timestamp
        )
        self.tx_date = norm_date
        self.tx_timestamp = norm_ts or f"{norm_date} 00:00:00"
        return self


ParsedEmailTransaction = ParsedTransaction


class TransactionRecord(BaseModel):
    """Persisted financial transaction stored in SQLite."""

    model_config = ConfigDict(from_attributes=True, allow_inf_nan=False)

    id: Optional[int] = None
    message_id: str = Field(..., min_length=1)
    tx_date: str
    tx_timestamp: Optional[str] = None
    account_name: str = Field(..., min_length=1)
    target_account: Optional[str] = None
    tx_type: TxType
    amount: float = Field(..., gt=0.0)
    tax_fee_amount: float = Field(default=0.0, ge=0.0)
    payee: str = Field(..., min_length=1)
    merchant_alias: Optional[str] = None
    category: str = Field(default="Uncategorized", min_length=1)
    cost_bucket: CostBucket = "VARIABLE"
    review_status: ReviewStatus = "NEEDS_REVIEW"
    is_internal_transfer: bool = False
    source: SourceChannel = "MANUAL"
    payment_channel: Optional[str] = None
    raw_subject: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[str] = None

    @field_validator("payee")
    @classmethod
    def _clean_payee(cls, value: str) -> str:
        cleaned = " ".join(value.split()).strip()
        if not cleaned:
            raise ValueError("payee cannot be blank")
        return cleaned

    @model_validator(mode="after")
    def _sync_fields(self) -> "TransactionRecord":
        norm_date, norm_ts = _normalize_date_and_timestamp(
            self.tx_date, self.tx_timestamp
        )
        self.tx_date = norm_date
        self.tx_timestamp = norm_ts or f"{norm_date} 00:00:00"
        if self.tx_type == "TRANSFER_INTERNAL":
            target = (self.target_account or "").strip()
            if not target:
                raise ValueError("TRANSFER_INTERNAL requires a non-empty target_account")
            if target == self.account_name:
                raise ValueError("TRANSFER_INTERNAL target_account must differ from account_name")
            self.target_account = target
            self.is_internal_transfer = True
            self.cost_bucket = "IGNORE"
        elif self.tx_type == "TRANSFER_MMF":
            if self.account_name == WALLET_MMF:
                raise ValueError("TRANSFER_MMF source account cannot be Money Market Fund")
            self.target_account = WALLET_MMF
            self.is_internal_transfer = False
            self.cost_bucket = "INVESTMENT"
        else:
            self.is_internal_transfer = False
            self.target_account = None
            if self.tx_type == "CREDIT":
                self.cost_bucket = "INFLOW"
        return self

    def total_outflow(self) -> float:
        """Total wallet deduction including tax/FX friction."""
        return round(self.amount + self.tax_fee_amount, 2)


class BudgetConfig(BaseModel):
    """Month 1 Financial Defense configuration parameters."""

    model_config = ConfigDict(from_attributes=True, allow_inf_nan=False)

    id: int = 1
    monthly_income: float = Field(default=200000.0, ge=0.0)
    fixed_cost_baseline: float = Field(default=80000.0, ge=0.0)
    variable_spending_cap: float = Field(default=40000.0, ge=0.0)
    emergency_multiplier: int = Field(default=6, ge=1, le=24)
    envelope_mode: bool = False
    updated_at: Optional[str] = None


class CategoryBudget(BaseModel):
    """Per-category monthly spending limit and display metadata."""

    model_config = ConfigDict(from_attributes=True, allow_inf_nan=False)

    id: Optional[int] = None
    category: str = Field(..., min_length=1)
    emoji: str = "📦"
    cost_bucket: CostBucket = "VARIABLE"
    monthly_limit: float = Field(default=0.0, ge=0.0)
    is_active: bool = True


class CategoryRule(BaseModel):
    """Deterministic keyword or Raast IBAN/payee rule for auto-categorization."""

    model_config = ConfigDict(from_attributes=True, allow_inf_nan=False)

    id: Optional[int] = None
    keyword: str = Field(..., min_length=1)
    merchant_alias: Optional[str] = None
    category: str = Field(..., min_length=1)
    cost_bucket: CostBucket
    priority: int = 10
    is_user_defined: bool = False
    created_at: Optional[str] = None


class RecurringCommitment(BaseModel):
    """Tracked monthly recurring bill or subscription."""

    model_config = ConfigDict(from_attributes=True, allow_inf_nan=False)

    id: Optional[int] = None
    title: str = Field(..., min_length=1)
    match_keyword: str = Field(..., min_length=1)
    expected_amount: float = Field(..., gt=0.0)
    due_day: int = Field(default=1, ge=1, le=31)
    category: str = "Bills & Utilities"
    cost_bucket: CostBucket = "FIXED"
    is_paid_this_month: bool = False
    last_paid_month: Optional[str] = None
    is_active: bool = True


class CategoryProgress(BaseModel):
    """Per-category spending utilization vs monthly sub-budget."""

    model_config = ConfigDict(allow_inf_nan=False)

    category: str
    emoji: str
    cost_bucket: CostBucket
    spent: float
    monthly_limit: float
    remaining: float
    utilization_pct: float
    status_color: Literal["green", "amber", "red"]


class MerchantSpend(BaseModel):
    """Aggregated spend for top-merchants analytics."""

    merchant: str
    total_spent: float
    tx_count: int
    category: str


class DailyBurnPoint(BaseModel):
    """Daily and cumulative spend series point for charts."""

    day: int
    tx_date: str
    daily_spend: float
    cumulative_spend: float


class RecurringStatus(BaseModel):
    """Month-specific payment status for a recurring commitment."""

    commitment: RecurringCommitment
    is_paid: bool
    paid_amount: float = 0.0
    paid_date: Optional[str] = None
    matched_tx_id: Optional[int] = None


class DashboardMetrics(BaseModel):
    """Complete Month 1 Defense + Batwa analytics snapshot for a calendar month."""

    month_key: str
    monthly_income: float
    actual_inflow: float
    fixed_cost_baseline: float
    variable_spending_cap: float
    total_spending_cap: float
    actual_fixed_burn: float
    actual_variable_burn: float
    actual_leakage_burn: float
    total_tax_fees: float
    total_monthly_burn: float
    remaining_allowance: float
    burn_utilization_pct: float
    days_in_month: int
    days_elapsed: int
    days_remaining: int
    safe_daily_spend: float
    actual_daily_burn_pace: float
    prev_month_daily_burn_pace: float = 0.0
    mom_pace_delta_pct: float = 0.0
    monthly_mmf_swept: float
    emergency_multiplier: int
    emergency_target: float
    emergency_current: float
    emergency_progress_pct: float
    unallocated_cash_surplus: float
    needs_review_count: int = 0
    total_liquid_balance: float = 0.0
    total_net_worth: float = 0.0
    wallet_balances: list[AccountWallet] = Field(default_factory=list)
    category_progress: list[CategoryProgress] = Field(default_factory=list)
    top_merchants: list[MerchantSpend] = Field(default_factory=list)
    daily_cumulative_spend: list[DailyBurnPoint] = Field(default_factory=list)
    upcoming_bills: list[RecurringStatus] = Field(default_factory=list)
    unpaid_bills_total: float = 0.0


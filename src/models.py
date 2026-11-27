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
_MONTH_KEY_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _reject_bool_num(value: object) -> object:
    """Reject Python booleans silently coercing into floats/ints in Pydantic."""
    if isinstance(value, bool):
        raise ValueError("Boolean value is not valid for a numeric field")
    return value


def _round_money(value: object) -> float:
    """Reject booleans and round numeric monetary inputs to 2 decimal places."""
    if isinstance(value, bool):
        raise ValueError("Boolean value is not valid for a monetary amount")
    return round(float(value), 2)


def _strip_non_empty(value: object, field_name: str = "field") -> str:
    """Strip whitespace and reject blank strings."""
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError(f"{field_name} cannot be blank")
    return cleaned


def normalize_balance_as_of(value: Optional[str]) -> Optional[str]:
    """Normalize balance_as_of to 'YYYY-MM-DD HH:MM:SS' (10-char dates expand to 23:59:59)."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if _DATE_RE.match(raw):
        datetime.strptime(raw, "%Y-%m-%d")
        return f"{raw} 23:59:59"
    clean = raw.replace("T", " ")[:19]
    if not _DATETIME_RE.match(clean):
        raise ValueError(f"Invalid balance_as_of format '{value}'; expected YYYY-MM-DD[ HH:MM:SS]")
    datetime.strptime(clean, "%Y-%m-%d %H:%M:%S")
    return clean


def _normalize_date_and_timestamp(
    tx_date: str, tx_timestamp: Optional[str]
) -> tuple[str, Optional[str]]:
    """Normalize date strings to YYYY-MM-DD and validate/align timestamp."""
    raw = str(tx_date or "").strip()
    norm_ts: Optional[str] = None
    if tx_timestamp is not None:
        raw_ts = str(tx_timestamp).strip()
        if raw_ts:
            clean_ts = raw_ts.replace("T", " ")
            if not _DATETIME_RE.match(clean_ts):
                raise ValueError(
                    f"Invalid tx_timestamp format '{tx_timestamp}'; expected YYYY-MM-DD HH:MM:SS"
                )
            datetime.strptime(clean_ts, "%Y-%m-%d %H:%M:%S")
            norm_ts = clean_ts

    if _DATE_RE.match(raw):
        datetime.strptime(raw, "%Y-%m-%d")
        norm_date = raw
        if norm_ts is not None and norm_ts[:10] != norm_date:
            norm_ts = f"{norm_date} {norm_ts[11:]}"
        return norm_date, norm_ts
    if _DATETIME_RE.match(raw):
        clean_raw_ts = raw.replace("T", " ")
        dt = datetime.strptime(clean_raw_ts, "%Y-%m-%d %H:%M:%S")
        norm_date = dt.strftime("%Y-%m-%d")
        if norm_ts is not None:
            if norm_ts[:10] != norm_date:
                norm_ts = f"{norm_date} {norm_ts[11:]}"
            return norm_date, norm_ts
        return norm_date, clean_raw_ts
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

    @field_validator("account_name")
    @classmethod
    def _clean_acct(cls, value: str) -> str:
        return _strip_non_empty(value, "account_name")

    @field_validator("opening_balance", "current_balance", mode="before")
    @classmethod
    def _clean_bal(cls, value: object) -> float:
        return _round_money(value)

    @field_validator("balance_as_of")
    @classmethod
    def _clean_as_of(cls, value: Optional[str]) -> Optional[str]:
        return normalize_balance_as_of(value)


class ParsedTransaction(BaseModel):
    """Normalized transaction extracted from Gmail or PDF/CSV before categorization."""

    model_config = ConfigDict(from_attributes=True, allow_inf_nan=False)

    message_id: str = Field(..., min_length=1)
    tx_date: str
    tx_timestamp: Optional[str] = None
    account_name: str = Field(..., min_length=1)
    target_account: Optional[str] = None
    tx_type: TxType
    amount: float = Field(..., ge=0.01)
    tax_fee_amount: float = Field(default=0.0, ge=0.0)
    payee: str = Field(..., min_length=1)
    merchant_alias: Optional[str] = None
    source: SourceChannel
    payment_channel: Optional[str] = None
    raw_subject: Optional[str] = None
    notes: Optional[str] = None
    suggested_category: Optional[str] = None
    suggested_bucket: Optional[CostBucket] = None

    @field_validator("message_id", "account_name")
    @classmethod
    def _clean_req_str(cls, value: str, info) -> str:
        return _strip_non_empty(value, info.field_name)

    @field_validator("amount", "tax_fee_amount", mode="before")
    @classmethod
    def _clean_money(cls, value: object) -> float:
        return _round_money(value)

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
        if self.target_account is not None:
            stripped_tgt = self.target_account.strip()
            self.target_account = stripped_tgt or None
        if self.tx_type == "TRANSFER_INTERNAL":
            if not self.target_account:
                raise ValueError("TRANSFER_INTERNAL requires a non-empty target_account")
            if self.target_account == self.account_name:
                raise ValueError("TRANSFER_INTERNAL target_account must differ from account_name")
        elif self.tx_type == "TRANSFER_MMF":
            if self.account_name == WALLET_MMF:
                raise ValueError("TRANSFER_MMF source account cannot be Money Market Fund")
            self.target_account = WALLET_MMF
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
    amount: float = Field(..., ge=0.01)
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

    @field_validator("message_id", "account_name", "category")
    @classmethod
    def _clean_req_str(cls, value: str, info) -> str:
        return _strip_non_empty(value, info.field_name)

    @field_validator("amount", "tax_fee_amount", mode="before")
    @classmethod
    def _clean_money(cls, value: object) -> float:
        return _round_money(value)

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
            if target == WALLET_MMF:
                raise ValueError("Use TRANSFER_MMF for transfers to Money Market Fund")
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
        """Total wallet deduction including tax/FX friction (0.0 for inflow credits)."""
        if self.tx_type == "CREDIT":
            return 0.0
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

    @field_validator("monthly_income", "fixed_cost_baseline", "variable_spending_cap", mode="before")
    @classmethod
    def _clean_cfg_money(cls, value: object) -> float:
        return _round_money(value)

    @field_validator("emergency_multiplier", mode="before")
    @classmethod
    def _clean_mult(cls, value: object) -> object:
        return _reject_bool_num(value)


class CategoryBudget(BaseModel):
    """Per-category monthly spending limit and display metadata."""

    model_config = ConfigDict(from_attributes=True, allow_inf_nan=False)

    id: Optional[int] = None
    category: str = Field(..., min_length=1)
    emoji: str = "📦"
    cost_bucket: CostBucket = "VARIABLE"
    monthly_limit: float = Field(default=0.0, ge=0.0)
    is_active: bool = True

    @field_validator("category")
    @classmethod
    def _clean_cat(cls, value: str) -> str:
        return _strip_non_empty(value, "category")

    @field_validator("monthly_limit", mode="before")
    @classmethod
    def _clean_limit(cls, value: object) -> float:
        return _round_money(value)


class CategoryRule(BaseModel):
    """Deterministic keyword or Raast IBAN/payee rule for auto-categorization."""

    model_config = ConfigDict(from_attributes=True, allow_inf_nan=False)

    id: Optional[int] = None
    keyword: str = Field(..., min_length=1)
    merchant_alias: Optional[str] = None
    category: str = Field(..., min_length=1)
    cost_bucket: CostBucket
    target_account: Optional[str] = None
    priority: int = 10
    is_user_defined: bool = False
    created_at: Optional[str] = None

    @field_validator("keyword", "category")
    @classmethod
    def _clean_req(cls, value: str, info) -> str:
        return _strip_non_empty(value, info.field_name)

    @field_validator("target_account")
    @classmethod
    def _clean_tgt(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class RecurringCommitment(BaseModel):
    """Tracked monthly recurring bill or subscription."""

    model_config = ConfigDict(from_attributes=True, allow_inf_nan=False)

    id: Optional[int] = None
    title: str = Field(..., min_length=1)
    match_keyword: str = Field(..., min_length=1)
    expected_amount: float = Field(..., ge=0.01)
    due_day: int = Field(default=1, ge=1, le=31)
    category: str = "Bills & Utilities"
    cost_bucket: CostBucket = "FIXED"
    is_paid_this_month: bool = False
    last_paid_month: Optional[str] = None
    is_active: bool = True

    @field_validator("title", "match_keyword", "category")
    @classmethod
    def _clean_req(cls, value: str, info) -> str:
        return _strip_non_empty(value, info.field_name)

    @field_validator("expected_amount", mode="before")
    @classmethod
    def _clean_exp(cls, value: object) -> float:
        return _round_money(value)

    @field_validator("due_day", mode="before")
    @classmethod
    def _clean_due(cls, value: object) -> object:
        return _reject_bool_num(value)

    @field_validator("last_paid_month")
    @classmethod
    def _clean_last_paid(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if not _MONTH_KEY_RE.match(cleaned):
            raise ValueError(f"Invalid last_paid_month '{value}'; expected YYYY-MM")
        return cleaned


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

    model_config = ConfigDict(allow_inf_nan=False)

    merchant: str
    total_spent: float
    tx_count: int
    category: str


class DailyBurnPoint(BaseModel):
    """Daily and cumulative spend series point for charts."""

    model_config = ConfigDict(allow_inf_nan=False)

    day: int
    tx_date: str
    daily_spend: float
    cumulative_spend: float


class RecurringStatus(BaseModel):
    """Month-specific payment status for a recurring commitment."""

    model_config = ConfigDict(allow_inf_nan=False)

    commitment: RecurringCommitment
    is_paid: bool
    paid_amount: float = 0.0
    paid_date: Optional[str] = None
    matched_tx_id: Optional[int] = None
    due_date: Optional[str] = None
    status: Literal["PAID", "OVERDUE", "DUE_SOON", "UPCOMING"] = "UPCOMING"


class DashboardMetrics(BaseModel):
    """Complete Month 1 Defense + Batwa analytics snapshot for a calendar month."""

    model_config = ConfigDict(allow_inf_nan=False)

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


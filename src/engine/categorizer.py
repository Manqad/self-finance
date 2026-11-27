"""Deterministic keyword & merchant rule engine for Personal Treasury OS."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional

from src.models import (
    WALLET_CASH,
    WALLET_HABIBMETRO,
    WALLET_MMF,
    WALLET_NAYAPAY,
    CategoryRule,
    CostBucket,
    ParsedTransaction,
    TransactionRecord,
    TxType,
)

if TYPE_CHECKING:
    from src.database.db_manager import DatabaseManager

_AMC_KEYWORDS: tuple[str, ...] = (
    "AL MEEZAN",
    "MEEZAN INVESTMENT",
    "MEEZAN AMC",
    "MEEZAN ROZANA",
    "MEEZAN CASH FUND",
    "MRAF",
    "MCF",
    "NBP FUND",
    "UBL FUND",
    "AL-AMEEN FUNDS",
    "AL-AMEEN FUND",
    "ALFALAH GHP",
    "ALFALAH ASSET",
    "ALFALAH INVESTMENT",
)

_KE_EXPLICIT_RE = re.compile(
    r"(?i)(?:\bk-electric\b|\bkarachi\s+electric\b|\bke\s+(?:bill|electric|power)\b)"
)
_J_DOT_RE = re.compile(
    r"(?i)(?<![a-z0-9.])(?<!\bdr\.\s)(?<!\bmr\.\s)(?<!\bms\.\s)(?<!\b[a-z]\.\s)j\.(?:\s|$)"
)

_FUND_CONTEXT_RE = re.compile(
    r"(?i)\b(?:meezan|al[\s\-]*meezan|amc|fund|funds|investment|investments|"
    r"rozana|amdani|cash\s+fund|mutual|money\s+market|sweep|units?)\b"
)
_NON_FUND_ENTITY_RE = re.compile(
    r"(?i)\b(?:traders?|trading|foundation|charity|welfare|trust|society|"
    r"enterprises?|store|mart|motors?|academy|school|clinic|hospital|"
    r"restaurant|cafe|bakery|textiles?|pharma(?:cy)?|consulting|club|builders?)\b"
)

_STEAM_TECH_RE = re.compile(
    r"(?i)(?:steampowered|steam\s*(?:games|store|wallet|purchase|card|com)|"
    r"valve\s*steam|\bsteam\.com\b|\bsteam\s*\*|\*\s*steam\b)"
)
_APPLE_TECH_RE = re.compile(
    r"(?i)(?:\bapple\.com\b|\bapple\s*(?:services?|bill|store|pay|one|music|"
    r"tv|support|inc|care|cloud|arcade|id|distribution)\b|\bapple\s*\*|"
    r"\bitunes\b|\bapp\s+store\b)"
)
_FED_UP_RE = re.compile(r"(?i)\bfed\s+up\b")
_NON_PROFIT_RE = re.compile(
    r"(?i)\b(?:non[\s\-]*profit|not[\s\-]+for[\s\-]+profit)\b"
)
_FED_TAX_RE = re.compile(
    r"(?i)(?:\bfederal\s+excise\b|\bf\.e\.d\.?\b|"
    r"(?<![a-z0-9])fed(?:\s*(?:tax|charges?|fee|deduction|amount|on|duty|rs|pkr|:|-|\())|"
    r"(?:&|\+|/|,|\()\s*fed(?![a-z0-9])|"
    r"(?:\bbank|\bibft|\bsms|\bcard|\bwht|\btax|\bcharges?)\s+fed(?![a-z0-9]))"
)

_GROCERY_STORE_CUE_RE = re.compile(
    r"(?i)\b(?:super|store|mega|mart|market|grocery|supermarket|hyper)\b"
)
_P2P_CUE_RE = re.compile(
    r"(?i)(?:\braast\b|\bp2p\b|\btransfer\s+to\b|\bsent\s+to\b|\bibft\s+to\b|"
    r"\breceived\s+from\b|\bmoney\s+sent\b|\bfunds?\s+transfer\b|"
    r"^(?:ibft|raast|p2p|ft|transfer)\b)"
)
_P2P_CHANNELS = frozenset({"Raast", "Wallet", "IBFT", "Fund Transfer"})
_SHELL_NON_FUEL_RE = re.compile(r"(?i)\b(?:sea\s*shell|restaurant|cafe|coffee|bakery|grill)\b")
_SHELL_FUEL_RE = re.compile(r"(?i)\b(?:petrol|fuel|diesel|pump|station|pakistan|energy|lubricant)\b")

_NAYAPAY_TEXT_RE = re.compile(r"(?i)\bnaya\s*pay\b")
_OWN_HMB_TRANSFER_RE = re.compile(
    r"(?i)(?:\b(?:own\s+account|self\s+transfer|my\s+habib\s*metro|habib\s*metro\s+self|"
    r"wallet\s+to\s+(?:my\s+)?habib\s*metro|my\s+hmb|hmb\s+self)\b)"
)
_ATM_WITHDRAWAL_RE = re.compile(
    r"(?i)\b(?:atm(?!\s*(?:/\s*debit)?\s*card\b)|cash\s+withdrawal)\b"
)
_ATM_NON_WITHDRAWAL_RE = re.compile(
    r"(?i)\b(?:pos|purchase|fee|charges?|tax|wht|fed|surcharge|annual|"
    r"issuance|replacement|pin|inquiry)\b"
)
_VALID_TRANSFER_CATEGORIES = frozenset(
    {"Internal Transfer", "Wallet Top-Up", "Cash Withdrawal", "MMF Redemption"}
)
_GENERIC_FALLBACK_PAYEES = frozenset(
    {
        "statement transaction",
        "nayapay transaction",
        "mobile bundle",
        "mobile balance top-up",
        "atm cash withdrawal",
    }
)

_TRAILING_EMOJI_RE = re.compile(
    r"[\s\U00010000-\U0010ffff\u2600-\u27bf\ufe0e\ufe0f\u200d]+$"
)
_MASKED_PAYEE_RE = re.compile(
    r"^(?:[*xX●•⚫◦▪\-.\s]*[*xX●•⚫◦▪]{2,}[*xX●•⚫◦▪\-.\s\d]*|[\d\-\s]{4,})$"
)
_NP_SPECIAL_ATM_RE = re.compile(
    r"(?i)^(?:cash\s+withdrawal(?:\s+of)?|cash\s+withdrawn\s+at\s+atm)\b"
)
_NP_SPECIAL_BUNDLE_RE = re.compile(
    r"(?i)^you\s+subscribed\s+to\s+(?:a\s+)?mobile\s+bundle\b"
)
_NP_SPECIAL_MOBILE_TOPUP_RE = re.compile(
    r"(?i)^you\s+topped\s+up\s+(?:your\s+)?mobile\s+balance\b"
)
_NP_EMAIL_WRAPPER_RE = re.compile(
    r"(?i)^(?:you\s+(?:sent|spent|paid|got|received)|money\s+(?:sent|received))\s+"
    r"(?:PKR|Rs\.?|PK\s*Rs\.?)\s*[0-9][0-9,]*(?:\.[0-9]{1,2})?\s+"
    r"(?:to|at|from)\s+(.+?)$"
)
_NP_PDF_RAAST_WRAPPER_RE = re.compile(
    r"(?i)^(?:raast\s+(?:out|in)\s+)?(?:outgoing|incoming)\s+fund\s+transfer\s+(?:to|from)\s+(.+?)$"
)
_NP_PDF_RAAST_LONG_REF_RE = re.compile(r"\s+(?=[A-Z0-9]*\d)[A-Z0-9]{14,}$")
_NP_PDF_RAAST_TAG_RE = re.compile(r"(?i)\s+Raast[-\s]*\d{3,6}$")
_NP_PDF_KNOWN_BANK_SUFFIX_RE = re.compile(
    r"(?i)\s+(?:"
    r"easypaisa(?:\s+Bank)?|JazzCash(?:\s*/\s*Mobilink\s+MFB)?|Mobilink\s+MFB|"
    r"SadaPay|NayaPay|Zindigi|"
    r"Meezan(?:\s+Bank)?|Habib\s*Metro(?:politan)?(?:\s+Bank)?|"
    r"Bank\s+Al\s+Habib|Bank\s+Alfalah|Standard\s+Chartered|Allied\s+Bank|"
    r"Askari\s+Bank|Faysal\s+Bank|Dubai\s+Islamic(?:\s+Bank)?|Bank\s+of\s+Punjab|"
    r"JS\s+Bank|Soneri\s+Bank|Silk\s+Bank|Al\s+Baraka|BankIslami|"
    r"U\s+Microfinance(?:\s+Bank)?|Telenor\s+Microfinance(?:\s+Bank)?|"
    r"First\s+Women(?:\s+Bank)?|"
    r"BAHL|HBL|UBL|MCB|NBP|BOP|ABL|SCB|FBL|AKBL|JSBL|MEZN|HABB|MPBL"
    r")[-\s]*\d{3,6}$"
)
_NP_PDF_GENERIC_BANK_SUFFIX_RE = re.compile(r"\s+[A-Za-z0-9/().]+-\d{3,6}$")
_NP_PDF_RETAIL_POS_RE = re.compile(
    r"(?i)^(?:retail\s+payment|pos)\s+paid\s+to\s+(.+?)$"
)
_NP_PDF_CARD_MASK_SUFFIX_RE = re.compile(
    r"(?i)\s+(?:visa|mastercard|paypak)\s+[xX*●•\d]{4,}$"
)
_NP_PDF_CARD_TX_PREFIX_RE = re.compile(
    r"(?i)^card\s+(?:transaction|purchase)\s+(.+?)$"
)
_HMB_OWN_AC_SUFFIX_RE = re.compile(
    r"(?i)\s+from\s+(?:your\s+)?(?:hmb\b|habib\s*metro\b|a/c\b|account\b).*$"
)


def _is_masked_payee(value: str) -> bool:
    """Return True if value is purely a masked card/account tag or digit string."""
    cleaned = " ".join((value or "").split()).strip()
    return bool(cleaned and _MASKED_PAYEE_RE.fullmatch(cleaned))


def extract_canonical_payee(text: str) -> str:
    """Normalize verbose NayaPay/HMB email & PDF payee wrappers into the core counterparty name."""
    cleaned = " ".join((text or "").split()).strip()
    cleaned = _TRAILING_EMOJI_RE.sub("", cleaned).strip()
    if not cleaned:
        return ""

    if _NP_SPECIAL_ATM_RE.match(cleaned):
        return "ATM Cash Withdrawal"
    if _NP_SPECIAL_BUNDLE_RE.match(cleaned):
        return "Mobile Bundle"
    if _NP_SPECIAL_MOBILE_TOPUP_RE.match(cleaned):
        return "Mobile Balance Top-Up"

    m_email = _NP_EMAIL_WRAPPER_RE.match(cleaned)
    if m_email:
        core = _TRAILING_EMOJI_RE.sub("", m_email.group(1)).strip(" .|-")
        if core:
            return core

    m_raast = _NP_PDF_RAAST_WRAPPER_RE.match(cleaned)
    if m_raast:
        core = m_raast.group(1).strip()
        core = _NP_PDF_RAAST_LONG_REF_RE.sub("", core).strip()
        core = _NP_PDF_RAAST_TAG_RE.sub("", core).strip()
        core = _NP_PDF_KNOWN_BANK_SUFFIX_RE.sub("", core).strip()
        core = _NP_PDF_GENERIC_BANK_SUFFIX_RE.sub("", core).strip()
        if core:
            return core

    m_retail = _NP_PDF_RETAIL_POS_RE.match(cleaned)
    if m_retail:
        core = _NP_PDF_CARD_MASK_SUFFIX_RE.sub("", m_retail.group(1)).strip(" .|-")
        if core:
            return core

    m_card = _NP_PDF_CARD_TX_PREFIX_RE.match(cleaned)
    if m_card:
        core = _NP_PDF_CARD_MASK_SUFFIX_RE.sub("", m_card.group(1)).strip(" .|-")
        if core:
            return core

    no_hmb = _HMB_OWN_AC_SUFFIX_RE.sub("", cleaned).strip()
    return no_hmb or cleaned


def _boundary_match(keyword: str, text: str) -> bool:
    """Match keyword with strict alphanumeric word boundaries on both sides."""
    lower_kw = keyword.lower()
    left = r"(?<![a-z0-9])" if lower_kw[0].isalnum() else ""
    right = r"(?![a-z0-9])" if lower_kw[-1].isalnum() else ""
    return bool(re.search(rf"{left}{re.escape(lower_kw)}{right}", text.lower()))


def _keyword_matches(
    keyword: str,
    text: str,
    *,
    is_user_defined: bool = False,
    payment_channel: Optional[str] = None,
) -> bool:
    """Match rule keyword case-insensitively with strict disambiguation & boundary safety."""
    if not text:
        return False
    kw = keyword.strip()
    if not kw:
        return False

    if is_user_defined:
        return _boundary_match(kw, text)

    upper_kw = kw.upper()
    canon_upper = extract_canonical_payee(text).strip().upper()
    stripped_upper = text.strip().upper()

    if upper_kw == "KE":
        return bool(_KE_EXPLICIT_RE.search(text))

    if upper_kw == "J.":
        return bool(_J_DOT_RE.search(text))

    if upper_kw in ("MRAF", "MCF", "AL-AMEEN"):
        if not _boundary_match(kw, text):
            return False
        if stripped_upper == upper_kw or canon_upper == upper_kw:
            return True
        if _NON_FUND_ENTITY_RE.search(text):
            return bool(
                re.search(
                    r"(?i)\b(?:meezan|al[\s\-]*meezan|amc|mutual|rozana|cash\s+fund|fund|funds)\b",
                    text,
                )
            )
        return bool(
            _FUND_CONTEXT_RE.search(text)
            or re.search(rf"(?i)(?:\({upper_kw}\)|\bibft\b)", text)
            or upper_kw == "AL-AMEEN"
        )

    if upper_kw == "STEAM":
        if stripped_upper == "STEAM" or canon_upper == "STEAM":
            return True
        return bool(_STEAM_TECH_RE.search(text))

    if upper_kw == "APPLE":
        if stripped_upper == "APPLE" or canon_upper == "APPLE":
            return True
        return bool(_APPLE_TECH_RE.search(text))

    if upper_kw == "FED":
        if _FED_UP_RE.search(text):
            return False
        if stripped_upper == "FED" or canon_upper == "FED":
            return True
        return bool(_FED_TAX_RE.search(text))

    if upper_kw == "PROFIT":
        if _NON_PROFIT_RE.search(text):
            return False
        return _boundary_match(kw, text)

    if upper_kw in ("IMTIAZ", "NAHEED", "SHIFA"):
        if not _boundary_match(kw, text):
            return False
        if stripped_upper == upper_kw or canon_upper == upper_kw:
            return True
        if upper_kw == "SHIFA":
            if re.search(r"(?i)\b(?:hospital|int(?:ernational|\.)|clinic|medical|lab)\b", text):
                return True
            if (payment_channel in _P2P_CHANNELS) or _P2P_CUE_RE.search(text):
                return False
            return True
        if _GROCERY_STORE_CUE_RE.search(text):
            return True
        if (payment_channel in _P2P_CHANNELS) or _P2P_CUE_RE.search(text):
            return False
        return True

    if upper_kw == "SHELL":
        if not _boundary_match(kw, text):
            return False
        if _SHELL_NON_FUEL_RE.search(text) and not _SHELL_FUEL_RE.search(text):
            return False
        return True

    if upper_kw == "ATM":
        if not _boundary_match(kw, text):
            return False
        if re.search(r"(?i)\bcard\b", text) and not re.search(r"(?i)\bcash\s+withdrawal\b", text):
            return False
        return not bool(_ATM_NON_WITHDRAWAL_RE.search(text))

    return _boundary_match(kw, text)


def _matches_any_amc(payee: str, alias: Optional[str]) -> bool:
    for candidate in (payee, alias or ""):
        if not candidate:
            continue
        for amc_kw in _AMC_KEYWORDS:
            if _keyword_matches(amc_kw, candidate, is_user_defined=False):
                return True
    return False


def _rule_matches_tx(
    rule: CategoryRule,
    payee: str,
    merchant_alias: Optional[str],
    payment_channel: Optional[str],
) -> bool:
    if _keyword_matches(
        rule.keyword,
        payee,
        is_user_defined=rule.is_user_defined,
        payment_channel=payment_channel,
    ):
        return True
    if merchant_alias and _keyword_matches(
        rule.keyword,
        merchant_alias,
        is_user_defined=rule.is_user_defined,
        payment_channel=payment_channel,
    ):
        return True
    if rule.is_user_defined:
        canon_kw = extract_canonical_payee(rule.keyword)
        canon_payee = extract_canonical_payee(payee)
        if len(canon_kw) >= 3 and canon_kw.lower() != rule.keyword.strip().lower():
            if _keyword_matches(
                canon_kw,
                payee,
                is_user_defined=True,
                payment_channel=payment_channel,
            ):
                return True
            if canon_payee and _keyword_matches(
                canon_kw,
                canon_payee,
                is_user_defined=True,
                payment_channel=payment_channel,
            ):
                return True
        if canon_payee and canon_payee.lower() != payee.strip().lower():
            if _keyword_matches(
                rule.keyword,
                canon_payee,
                is_user_defined=True,
                payment_channel=payment_channel,
            ):
                return True
    return False


def _match_verified_history(
    base: dict[str, object],
    verified_txs: list[TransactionRecord],
) -> Optional[TransactionRecord]:
    """Match an unmatched transaction against previously MANUALLY_VERIFIED counterparties."""
    tx_type = str(base["tx_type"])
    account_name = str(base["account_name"])
    payee = str(base["payee"])
    merchant_alias = base.get("merchant_alias")

    canon_payee = extract_canonical_payee(payee).strip().lower()
    if (
        len(canon_payee) < 3
        or _is_masked_payee(canon_payee)
        or canon_payee in _GENERIC_FALLBACK_PAYEES
    ):
        return None

    for vtx in verified_txs:
        if vtx.review_status != "MANUALLY_VERIFIED" or vtx.category == "Uncategorized":
            continue
        if tx_type == "CREDIT" and vtx.tx_type != "CREDIT":
            continue
        if tx_type != "CREDIT" and vtx.tx_type == "CREDIT":
            continue
        if vtx.tx_type == "TRANSFER_INTERNAL" and (
            vtx.account_name != account_name
            or not vtx.target_account
            or vtx.target_account == account_name
        ):
            continue

        v_canon = extract_canonical_payee(vtx.payee).strip().lower()
        v_alias = (vtx.merchant_alias or "").strip().lower()
        matched = (
            (len(v_canon) >= 3 and not _is_masked_payee(v_canon) and v_canon == canon_payee)
            or (len(v_alias) >= 3 and not _is_masked_payee(v_alias) and v_alias == canon_payee)
        )
        if not matched:
            continue

        if not merchant_alias and vtx.merchant_alias:
            base["merchant_alias"] = vtx.merchant_alias

        if tx_type == "CREDIT":
            base["category"] = vtx.category
            base["cost_bucket"] = "INFLOW"
            base["review_status"] = "AUTO_VERIFIED"
        elif vtx.tx_type == "TRANSFER_INTERNAL" and vtx.target_account:
            base["tx_type"] = "TRANSFER_INTERNAL"
            base["target_account"] = vtx.target_account
            base["category"] = vtx.category
            base["cost_bucket"] = "IGNORE"
            base["is_internal_transfer"] = True
            base["review_status"] = "AUTO_VERIFIED"
        elif vtx.tx_type == "TRANSFER_MMF" and account_name != WALLET_MMF:
            base["tx_type"] = "TRANSFER_MMF"
            base["target_account"] = WALLET_MMF
            base["category"] = "Money Market Fund"
            base["cost_bucket"] = "INVESTMENT"
            base["is_internal_transfer"] = False
            base["review_status"] = "AUTO_VERIFIED"
        else:
            base["category"] = vtx.category
            base["cost_bucket"] = (
                vtx.cost_bucket
                if vtx.cost_bucket in ("FIXED", "VARIABLE", "LEAKAGE")
                else "VARIABLE"
            )
            base["review_status"] = "AUTO_VERIFIED"
        return TransactionRecord.model_validate(base)

    return None


def _infer_transfer_target_from_rule(
    matched_rule: CategoryRule, account_name: str
) -> Optional[str]:
    """Infer destination wallet for IGNORE internal-transfer rules when explicitly named."""
    explicit = (matched_rule.target_account or "").strip()
    if explicit and explicit != account_name and explicit != WALLET_MMF:
        return explicit
    probe = " ".join(
        part
        for part in (
            matched_rule.category,
            matched_rule.merchant_alias or "",
            matched_rule.keyword,
        )
        if part
    ).lower()
    if (
        "nayapay" in probe or "naya pay" in probe or "wallet top-up" in probe
    ) and account_name != WALLET_NAYAPAY:
        return WALLET_NAYAPAY
    if (
        "habibmetro" in probe or "habib metro" in probe or " hmb" in f" {probe}"
    ) and account_name != WALLET_HABIBMETRO:
        return WALLET_HABIBMETRO
    if (
        "cash withdrawal" in probe or "cash in hand" in probe
    ) and account_name != WALLET_CASH:
        return WALLET_CASH
    return None


def _apply_matched_rule(
    base: dict[str, object],
    matched_rule: CategoryRule,
    tx_type: str,
    merchant_alias: Optional[str],
) -> TransactionRecord:
    if not merchant_alias and matched_rule.merchant_alias:
        base["merchant_alias"] = matched_rule.merchant_alias

    account_name = str(base["account_name"])
    if tx_type == "DEBIT" and matched_rule.cost_bucket == "INVESTMENT":
        if account_name != WALLET_MMF:
            base["tx_type"] = "TRANSFER_MMF"
            base["target_account"] = WALLET_MMF
            base["category"] = "Money Market Fund"
            base["cost_bucket"] = "INVESTMENT"
            base["review_status"] = "AUTO_VERIFIED"
            base["is_internal_transfer"] = False
        else:
            # Debits on MMF itself cannot be TRANSFER_MMF; treat fees as LEAKAGE, others for review
            base["tx_type"] = "DEBIT"
            base["target_account"] = None
            base["is_internal_transfer"] = False
            payee_low = str(base.get("payee") or "").lower()
            if any(k in payee_low for k in ("fee", "tax", "wht", "zakat", "charges", "fed")):
                base["category"] = "Bank & Tax Friction"
                base["cost_bucket"] = "LEAKAGE"
                base["review_status"] = "AUTO_VERIFIED"
            else:
                base["category"] = "Uncategorized"
                base["cost_bucket"] = "VARIABLE"
                base["review_status"] = "NEEDS_REVIEW"
    elif tx_type == "CREDIT":
        base["category"] = matched_rule.category
        base["cost_bucket"] = "INFLOW"
        base["review_status"] = "AUTO_VERIFIED"
    else:
        base["category"] = matched_rule.category
        base["cost_bucket"] = matched_rule.cost_bucket
        base["review_status"] = "AUTO_VERIFIED"
        if matched_rule.cost_bucket == "IGNORE":
            if tx_type == "DEBIT":
                inferred_target = base.get("target_account") or _infer_transfer_target_from_rule(
                    matched_rule, account_name
                )
                if (
                    inferred_target
                    and inferred_target != account_name
                    and inferred_target != WALLET_MMF
                ):
                    base["tx_type"] = "TRANSFER_INTERNAL"
                    base["target_account"] = inferred_target
                    base["is_internal_transfer"] = True
                elif inferred_target == WALLET_MMF and account_name != WALLET_MMF:
                    base["tx_type"] = "TRANSFER_MMF"
                    base["target_account"] = WALLET_MMF
                    base["category"] = "Money Market Fund"
                    base["cost_bucket"] = "INVESTMENT"
                    base["is_internal_transfer"] = False
                else:
                    # Never silently create an AUTO_VERIFIED DEBIT/IGNORE black hole
                    base["is_internal_transfer"] = False
                    base["cost_bucket"] = "VARIABLE"
                    base["review_status"] = "NEEDS_REVIEW"
            else:
                base["is_internal_transfer"] = True
    return TransactionRecord.model_validate(base)


def _apply_rules_with_list(
    tx: ParsedTransaction | TransactionRecord,
    rules: list[CategoryRule],
    verified_txs: Optional[list[TransactionRecord]] = None,
) -> TransactionRecord:
    """Categorize a single transaction against an ordered rule list and verified history."""
    base = tx.model_dump()
    suggested_category = base.pop("suggested_category", None)
    suggested_bucket = base.pop("suggested_bucket", None)

    tx_type = base["tx_type"]
    account_name = base["account_name"]
    target_account = base.get("target_account")
    payee = base["payee"]
    merchant_alias = base.get("merchant_alias")
    payment_channel = base.get("payment_channel")

    # Sort defensively: priority DESC, keyword length DESC, id DESC
    ordered_rules = sorted(
        rules,
        key=lambda r: (r.priority, len(r.keyword.strip()), r.id or 0),
        reverse=True,
    )

    # User-defined rules (priority-100) override parser-preclassified transfers on ParsedTransaction
    if isinstance(tx, ParsedTransaction) and tx_type in ("TRANSFER_INTERNAL", "TRANSFER_MMF"):
        for rule in ordered_rules:
            if not rule.is_user_defined or rule.cost_bucket == "INFLOW":
                continue
            if _rule_matches_tx(rule, payee, merchant_alias, payment_channel):
                if rule.cost_bucket in ("FIXED", "VARIABLE", "LEAKAGE"):
                    base["tx_type"] = "DEBIT"
                    base["target_account"] = None
                    base["is_internal_transfer"] = False
                    return _apply_matched_rule(base, rule, "DEBIT", merchant_alias)
                break

    if tx_type == "TRANSFER_INTERNAL":
        base["is_internal_transfer"] = True
        base["cost_bucket"] = "IGNORE"
        base["review_status"] = "AUTO_VERIFIED"
        existing_cat = base.get("category")
        if existing_cat and existing_cat != "Uncategorized":
            base["category"] = existing_cat
        elif suggested_category and suggested_category in _VALID_TRANSFER_CATEGORIES:
            base["category"] = suggested_category
        elif target_account == WALLET_CASH:
            base["category"] = "Cash Withdrawal"
        elif target_account == WALLET_NAYAPAY:
            base["category"] = "Wallet Top-Up"
        else:
            base["category"] = "Internal Transfer"
        return TransactionRecord.model_validate(base)

    if tx_type == "TRANSFER_MMF":
        base["target_account"] = target_account or WALLET_MMF
        base["cost_bucket"] = "INVESTMENT"
        base["category"] = "Money Market Fund"
        base["review_status"] = "AUTO_VERIFIED"
        base["is_internal_transfer"] = False
        return TransactionRecord.model_validate(base)

    matched_rule: Optional[CategoryRule] = None
    for rule in ordered_rules:
        if tx_type == "CREDIT" and rule.cost_bucket != "INFLOW":
            continue
        if rule.cost_bucket == "INFLOW" and tx_type != "CREDIT":
            continue
        if (
            not rule.is_user_defined
            and rule.cost_bucket == "IGNORE"
            and rule.keyword.strip().upper() == "NAYAPAY"
        ):
            continue
        if _rule_matches_tx(rule, payee, merchant_alias, payment_channel):
            matched_rule = rule
            break

    # Priority-100 user-defined rules and LEAKAGE rules supersede HMB transfer promotions
    if matched_rule is not None and (
        matched_rule.is_user_defined or matched_rule.cost_bucket == "LEAKAGE"
    ):
        return _apply_matched_rule(base, matched_rule, tx_type, merchant_alias)

    # Suggested LEAKAGE fee/tax rows (e.g., PDF/CSV fees) must not be promoted to transfers or merchants
    if tx_type == "DEBIT" and suggested_bucket == "LEAKAGE":
        base["category"] = suggested_category or "Bank & Tax Friction"
        base["cost_bucket"] = "LEAKAGE"
        base["review_status"] = "AUTO_VERIFIED"
        return TransactionRecord.model_validate(base)

    # Explicit merchant/AMC default rules outrank generic HMB->"NayaPay" text matching
    if matched_rule is not None and matched_rule.cost_bucket in ("FIXED", "VARIABLE", "INVESTMENT"):
        if not (
            account_name == WALLET_HABIBMETRO
            and tx_type == "DEBIT"
            and _NAYAPAY_TEXT_RE.search(payee)
            and _NAYAPAY_TEXT_RE.search(matched_rule.keyword)
        ):
            return _apply_matched_rule(base, matched_rule, tx_type, merchant_alias)

    # HabibMetro DEBIT to NayaPay -> internal wallet top-up
    if (
        account_name == WALLET_HABIBMETRO
        and tx_type == "DEBIT"
        and _NAYAPAY_TEXT_RE.search(payee)
    ):
        base["tx_type"] = "TRANSFER_INTERNAL"
        base["target_account"] = WALLET_NAYAPAY
        base["category"] = "Wallet Top-Up"
        base["cost_bucket"] = "IGNORE"
        base["is_internal_transfer"] = True
        base["review_status"] = "AUTO_VERIFIED"
        if not merchant_alias:
            base["merchant_alias"] = "NayaPay Wallet Top-Up"
        return TransactionRecord.model_validate(base)

    # NayaPay DEBIT explicitly marked as own-account HabibMetro transfer
    if (
        account_name == WALLET_NAYAPAY
        and tx_type == "DEBIT"
        and _OWN_HMB_TRANSFER_RE.search(f"{payee} {merchant_alias or ''}")
    ):
        base["tx_type"] = "TRANSFER_INTERNAL"
        base["target_account"] = WALLET_HABIBMETRO
        base["category"] = "Internal Transfer"
        base["cost_bucket"] = "IGNORE"
        base["is_internal_transfer"] = True
        base["review_status"] = "AUTO_VERIFIED"
        if not merchant_alias:
            base["merchant_alias"] = "Own HabibMetro Transfer"
        return TransactionRecord.model_validate(base)

    # HabibMetro / NayaPay ATM / Cash Withdrawal DEBIT -> internal transfer to Cash in Hand
    if (
        account_name in (WALLET_HABIBMETRO, WALLET_NAYAPAY)
        and tx_type == "DEBIT"
        and payment_channel not in ("POS", "Card")
        and _ATM_WITHDRAWAL_RE.search(payee)
        and not _ATM_NON_WITHDRAWAL_RE.search(payee)
    ):
        base["tx_type"] = "TRANSFER_INTERNAL"
        base["target_account"] = WALLET_CASH
        base["category"] = "Cash Withdrawal"
        base["cost_bucket"] = "IGNORE"
        base["is_internal_transfer"] = True
        base["review_status"] = "AUTO_VERIFIED"
        if not merchant_alias:
            base["merchant_alias"] = "ATM Cash Withdrawal"
        return TransactionRecord.model_validate(base)

    if matched_rule is not None:
        return _apply_matched_rule(base, matched_rule, tx_type, merchant_alias)

    # Fallback AMC detection for non-MMF DEBITs even if seed rules were customized
    if tx_type == "DEBIT" and account_name != WALLET_MMF and _matches_any_amc(payee, merchant_alias):
        base["tx_type"] = "TRANSFER_MMF"
        base["target_account"] = WALLET_MMF
        base["category"] = "Money Market Fund"
        base["cost_bucket"] = "INVESTMENT"
        base["review_status"] = "AUTO_VERIFIED"
        base["is_internal_transfer"] = False
        return TransactionRecord.model_validate(base)

    # Fallback: match against previously MANUALLY_VERIFIED counterparties by canonical payee
    if verified_txs:
        verified_match = _match_verified_history(base, verified_txs)
        if verified_match is not None:
            return verified_match

    # Unmatched transactions go to Needs Review inbox queue
    base["category"] = "Uncategorized"
    base["cost_bucket"] = "INFLOW" if tx_type == "CREDIT" else "VARIABLE"
    base["review_status"] = "NEEDS_REVIEW"
    return TransactionRecord.model_validate(base)


def apply_rules(
    tx: ParsedTransaction | TransactionRecord,
    db: DatabaseManager,
    *,
    include_verified_history: Optional[bool] = None,
) -> TransactionRecord:
    """Apply categorization rules from SQLite to a parsed or persisted transaction."""
    rules = db.get_category_rules()
    use_verified = (
        isinstance(tx, ParsedTransaction)
        if include_verified_history is None
        else bool(include_verified_history)
    )
    verified_txs: Optional[list[TransactionRecord]] = None
    if use_verified:
        verified_txs = [
            v
            for v in db.get_transactions(review_status="MANUALLY_VERIFIED")
            if v.category != "Uncategorized"
        ]
    return _apply_rules_with_list(tx, rules, verified_txs=verified_txs)


def apply_rules_batch(
    txs: list[ParsedTransaction | TransactionRecord],
    db: DatabaseManager,
    *,
    include_verified_history: Optional[bool] = None,
) -> list[TransactionRecord]:
    """Categorize a batch of transactions using a single rule and verified-history load."""
    if not txs:
        return []
    rules = db.get_category_rules()
    needs_verified = (
        any(isinstance(tx, ParsedTransaction) for tx in txs)
        if include_verified_history is None
        else bool(include_verified_history)
    )
    verified_txs: list[TransactionRecord] = []
    if needs_verified:
        verified_txs = [
            v
            for v in db.get_transactions(review_status="MANUALLY_VERIFIED")
            if v.category != "Uncategorized"
        ]
    results: list[TransactionRecord] = []
    for tx in txs:
        use_v = (
            isinstance(tx, ParsedTransaction)
            if include_verified_history is None
            else bool(include_verified_history)
        )
        results.append(
            _apply_rules_with_list(
                tx, rules, verified_txs=verified_txs if use_v else None
            )
        )
    return results


def learn_new_rule(
    db: DatabaseManager,
    payee: str,
    alias: Optional[str],
    category: str,
    cost_bucket: CostBucket,
    keyword: Optional[str] = None,
    overwrite_verified: bool = False,
    target_account: Optional[str] = None,
    source_tx_type: Optional[TxType] = None,
) -> int:
    """Atomically save a priority-100 user rule and retroactively re-tag matching transactions."""
    rule_keyword = (keyword if keyword is not None else payee).strip()
    if len(rule_keyword) < 3:
        raise ValueError("Rule keyword must be at least 3 characters")

    clean_target = target_account.strip() if target_account and target_account.strip() else None
    rule = CategoryRule(
        keyword=rule_keyword,
        merchant_alias=alias.strip() if alias else None,
        category=category.strip(),
        cost_bucket=cost_bucket,
        target_account=clean_target,
        priority=100,
        is_user_defined=True,
    )
    return db.learn_and_apply_rule(
        rule,
        overwrite_verified=overwrite_verified,
        target_account=clean_target,
        source_tx_type=source_tx_type,
    )

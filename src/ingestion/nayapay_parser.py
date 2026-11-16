"""NayaPay HTML receipt and email parser with WHT/FX fee separation."""

from __future__ import annotations

import re
from typing import Optional

from src.ingestion.hmb_parser import (
    html_to_structured_text,
    is_ignored_bank_alert,
    normalize_datetime_str,
    parse_amount_value,
)
from src.models import (
    WALLET_NAYAPAY,
    CostBucket,
    ParsedTransaction,
    TxType,
)

_NAYAPAY_SENDER_RE = re.compile(r"(?i)nayapay\.com|\bnaya\s*pay\b")
_NAYAPAY_INNER_FROM_RE = re.compile(r"(?im)^from\s*:\s*[^\n]*\bnaya\s*pay\b")
_NAYAPAY_BRANDING_RE = re.compile(
    r"(?i)(?:nayapay\.com|"
    r"\bnayapay(?:\s+[a-z]+){0,3}\s+(?:receipt|alert|wallet|card|e-?statement|transaction)\b|"
    r"\b(?:via|using|from)\s+(?:your\s+)?nayapay\b|"
    r"\bthank\s+you\s+for\s+using\s+nayapay\b|"
    r"\bnayapay\s+visa\b)"
)
_HMB_ORIGIN_RE = re.compile(
    r"(?i)(habib\s*metro|habibmetro|eservice@habibmetro)"
)
_HMB_INNER_FROM_RE = re.compile(
    r"(?im)^from\s*:\s*[^\n]*(?:habib\s*metro|habibmetro|eservice@habibmetro)"
)

_BAL_QUALIFIER = (
    r"(?:account|acct\.?|a/c|avail\.?|available|avl\.?|closing|opening|"
    r"current|ledger|remaining|wallet|new|net|total|updated)"
)
_BALANCE_LINE_RE = re.compile(
    rf"(?i)^\s*(?:{_BAL_QUALIFIER}\s+)?bal(?:ance)?\.?\s*[:\-]"
)
_BALANCE_PHRASE_RE = re.compile(
    rf"(?i)(?:(?:\byour\s+)?(?:{_BAL_QUALIFIER}\s+)?|\byour\s+)"
    rf"bal(?:ance)?\.?\s*(?:is|of|:|-)?\s*(?:PKR|Rs\.?|PK\s*Rs\.?)?\s*[0-9][0-9,]*(?:\.[0-9]{{1,2}})?"
)
_INCLUSIVE_RE = re.compile(
    r"(?i)\b(?:includ(?:es?|ing)|inclusiv(?:e|ely)|inclusive\s+of|incl\b\.?)\b"
)
_EXCLUSIVE_RE = re.compile(
    r"(?i)\b(?:excl\b\.?\s*(?:of\s+)?(?:tax|fee|charges)|excluding\s+(?:tax|fee|charges)|excl\b\.?)\b"
)

_HERO_CREDIT_RE = re.compile(
    r"(?i)\b(?:money\s+received|you\s+(?:received|got)|received\s+(?:a\s+)?refund|"
    r"refund(?:\s+(?:received|credited|processed|issued|alert))?|cashback\s+received|"
    r"inward\s+raast|has\s+been\s+credited)\b"
)
_HERO_DEBIT_RE = re.compile(
    r"(?i)\b(?:you\s+(?:paid|spent)|money\s+sent|sent\s+(?:rs\.?|pkr)\s*[0-9]|"
    r"spent\s+(?:rs\.?|pkr)\s*[0-9]|cash\s+withdrawal(?:\s+of)?|"
    r"subscribed\s+to|topped\s+up|bill\s+payment|has\s+been\s+debited)\b"
)
_CREDIT_RE = re.compile(
    r"(?i)\b(?:money\s+received|you\s+(?:received|got)|got\s+(?:rs\.?|pkr|from)|"
    r"received\s+(?:rs\.?|pkr|from|a\s+refund)|"
    r"amount\s+received|credited|cashback\s+received|refund(?:ed)?|"
    r"funds?\s+received|inward\s+raast)\b"
)
_DEBIT_RE = re.compile(
    r"(?i)\b(?:you\s+(?:paid|spent|sent)|money\s+sent|sent\s+(?:rs\.?|pkr|to)|"
    r"spent\s+(?:rs\.?|pkr|at)|cash\s+withdrawal(?:\s+of)?|"
    r"subscribed\s+to|topped\s+up|"
    r"card\s+transaction|bill\s+payment|payment\s+to|paid\s+to|debited|"
    r"purchase|charged)\b"
)

_NON_AMOUNT_LABEL_RE = re.compile(
    r"(?i)(?:\b(?:id|no|number|ref|reference|code|invoice|ntn|strn|stan|rrn|"
    r"auth|arn|iban|pin|otp|zip|postal)\b|"
    r"\b(?:card|account|acct)\s*(?:no|number|ending|last|digits|title|type|#|id)\b|"
    r"^(?:card|account|acct)$)"
)
_MONETARY_LABEL_SUFFIX_RE = re.compile(
    r"(?i)\b(?:amount|fee|fees|tax|taxes|charges|total|wht|fed|duty)\s*$"
)
_TOTAL_LABEL_RE = re.compile(
    r"(?i)^(?:total\s+(?:amount|charged|debited|paid|payable)|"
    r"amount\s+charged|grand\s+total|total)$"
)
_BASE_LABEL_RE = re.compile(
    r"(?i)^(?:base\s+amount|merchant\s+amount|(?:card\s+)?transaction\s+amount|"
    r"tx\s+amount|original\s+amount|bill\s+amount|order\s+amount|"
    r"purchase\s+amount|transfer\s+amount|sent\s+amount|received\s+amount|"
    r"principal\s+amount|payment\s+amount|card\s+amount|amount)$"
)
_SUMMARY_TAX_LABEL_RE = re.compile(
    r"(?i)^(?:total\s+(?:tax(?:es)?|fee(?:s)?|charges)|"
    r"(?:total\s+)?tax(?:es)?\s*(?:&|and|/)\s*fee(?:s)?|"
    r"(?:total\s+)?fee(?:s)?\s*(?:&|and|/)\s*tax(?:es)?|"
    r"(?:total\s+)?fee(?:s)?\s*(?:&|and|/)\s*charges)$"
)
_INDIVIDUAL_TAX_LABEL_RE = re.compile(
    r"(?i)(?:wht|withholding\s+tax|advance\s+(?:income\s+)?tax|"
    r"income\s+tax|fbr\s+tax|international(?:\s+transaction)?\s+tax|"
    r"fx\s+fee|foreign(?:\s+currency|\s+transaction)?\s+fee|"
    r"international(?:\s+transaction)?\s+fee|cross[\s\-]*border\s+fee|"
    r"currency\s+conversion\s+fee|service\s+fee|transaction\s+fee|"
    r"processing\s+fee|convenience\s+fee|card\s+fee|fed|federal\s+excise|"
    r"surcharge|bank\s+charges|charges|\btax\b|\bfee\b)"
)

_INLINE_TAX_RE = re.compile(
    r"(?i)\b(?:withholding\s+tax|advance\s+(?:income\s+)?tax|income\s+tax|"
    r"fbr\s+tax|wht|fx\s+fee|foreign(?:\s+currency|\s+transaction)?\s+fee|"
    r"international(?:\s+transaction)?\s+fee|cross[\s\-]*border\s+fee|"
    r"currency\s+conversion\s+fee|service\s+fee|transaction\s+fee|"
    r"processing\s+fee|convenience\s+fee|card\s+fee|fed|federal\s+excise(?:\s+duty)?|"
    r"surcharge|bank\s+charges)\b(?:\s*\([^)\n]{1,20}\))?\s*(?:[:\-]|\bof)?\s*"
    r"(?:PKR|Rs\.?|PK\s*Rs\.?)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)"
)

_HERO_AMOUNT_RE = re.compile(
    r"(?i)\b(?:you\s+(?:paid|spent|sent|received|got)|money\s+(?:sent|received)|"
    r"refund(?:\s+of)?|paid|spent|sent|received|got|"
    r"cash\s+withdrawal(?:\s+of)?|subscribed\s+to\s+[^0-9\n]{0,40}?\bof|"
    r"topped\s+up\s+[^0-9\n]{0,40}?\bwith|"
    r"card\s+transaction\s+(?:of|for)|bill\s+payment\s+(?:of|for))\s+"
    r"(?:PKR|Rs\.?|PK\s*Rs\.?)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)"
)
_CURRENCY_ANY_RE = re.compile(
    r"(?i)(?:PKR|Rs\.?|PK\s*Rs\.?)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)"
)
_IBAN_RE = re.compile(r"\bPK\d{2}[A-Z]{4}[A-Z0-9]{16}\b", re.IGNORECASE)
_TRAILING_EMOJI_RE = re.compile(
    r"[\s\U00010000-\U0010ffff\u2600-\u27bf\ufe0e\ufe0f\u200d]+$"
)
_MASKED_ID_RE = re.compile(
    r"^(?:[*xX●•⚫◦▪\-.\s]*[*xX●•⚫◦▪]{2,}[*xX●•⚫◦▪\-.\s\d]*|[\d\-\s]{4,})$"
)


def _strip_trailing_emojis(value: str) -> str:
    """Remove trailing emoji/pictograph glyphs and surrounding whitespace."""
    return _TRAILING_EMOJI_RE.sub("", value or "").strip()


def _is_masked_identifier(value: str) -> bool:
    """Return True when value is a masked card/account identifier (e.g. ●●●●1234, ****7890)."""
    cleaned = " ".join((value or "").split()).strip()
    if not cleaned:
        return False
    return bool(_MASKED_ID_RE.fullmatch(cleaned))

_DEBIT_PAYEE_LABELS = (
    "Merchant Name",
    "Merchant",
    "Paid To",
    "You Paid",
    "Receiver Name",
    "Receiver",
    "Recipient Name",
    "Recipient",
    "To Account Title",
    "Account Title",
    "Transfer To",
    "Sent To",
    "Beneficiary Name",
    "Beneficiary",
    "Biller Name",
    "Biller",
    "Store",
    "To",
    "Description",
)
_CREDIT_PAYEE_LABELS = (
    "From Account Title",
    "Sender Name",
    "Sender",
    "Received From",
    "You Received From",
    "Remitter",
    "Merchant Name",
    "Merchant",
    "Account Title",
    "From",
    "Description",
)


def _nayapay_html_to_lines(raw_html_or_text: str) -> list[str]:
    """Parse NayaPay HTML tables/flex-rows/text into normalized non-empty lines."""
    structured = html_to_structured_text(raw_html_or_text)
    return [ln for ln in structured.splitlines() if ln]


def is_ignored_nayapay_email(subject: str, body: str) -> bool:
    """Return True for OTP/verification/login alerts or non-refund declined/failed alerts."""
    lines = _nayapay_html_to_lines(body)
    combined = f"{subject or ''}\n" + "\n".join(lines)
    return is_ignored_bank_alert(combined)


def is_nayapay_email(subject: str, sender: str, body: str) -> bool:
    """Detect NayaPay receipt emails while rejecting unrelated Uber/SadaPay/HMB-only alerts."""
    sender_l = (sender or "").lower()
    subject_l = (subject or "").lower()
    lines_text = "\n".join(_nayapay_html_to_lines(body))
    combined = f"{subject or ''}\n{lines_text}"

    np_sender_or_from = bool(
        _NAYAPAY_SENDER_RE.search(sender_l) or _NAYAPAY_INNER_FROM_RE.search(combined)
    )
    if np_sender_or_from:
        return True

    hmb_origin = bool(
        _HMB_ORIGIN_RE.search(sender_l) or _HMB_INNER_FROM_RE.search(combined)
    )
    if hmb_origin:
        return False

    if "nayapay" in subject_l and not _HMB_ORIGIN_RE.search(subject_l):
        return True

    return bool(_NAYAPAY_BRANDING_RE.search(lines_text))


def _strip_label_qualifiers(raw_label: str) -> str:
    """Remove trailing parenthetical or inclusive/exclusive tax qualifiers from an amount label."""
    cleaned = re.sub(r"\s*\([^)]*\)", "", raw_label)
    cleaned = re.sub(
        r"\s+(?:incl\b\.?|inclusive|excl\b\.?|excluding)"
        r"(?:\s+of)?(?:\s+(?:tax(?:es)?|fee(?:s)?|charges|wht|fx))?\s*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return " ".join(cleaned.split()).strip()


def _extract_amounts_and_taxes(
    lines: list[str], subject: str
) -> tuple[Optional[float], float]:
    """Extract base PKR amount and total tax/fee amount without double counting."""
    explicit_base: Optional[float] = None
    base_excl_tax = False
    base_incl_tax = False
    explicit_total: Optional[float] = None
    hero_amount: Optional[float] = None
    individual_tax_sum = 0.0
    summary_tax_sum = 0.0

    # Split pipe-delimited segments BEFORE filtering balance lines and extracting KV pairs
    split_lines: list[str] = []
    for ln in lines:
        if "|" in ln:
            for seg in ln.split("|"):
                seg_clean = seg.strip()
                if seg_clean:
                    split_lines.append(seg_clean)
        else:
            split_lines.append(ln)

    safe_lines: list[str] = []
    for ln in split_lines:
        stripped_ln = _BALANCE_PHRASE_RE.sub(" ", ln).strip()
        if not stripped_ln:
            continue
        if _BALANCE_LINE_RE.search(stripped_ln) and not _CURRENCY_ANY_RE.search(stripped_ln):
            continue
        safe_lines.append(stripped_ln)
    joined_safe = "\n".join(safe_lines)

    # 1. Inspect structured "Label: Value" or "Label - Value" lines
    unmatched_lines: list[str] = []
    for ln in safe_lines:
        m_kv = re.match(r"^([^:\n]{2,65}?)(?:\s*:\s*|\s+-\s+)(.+)$", ln)
        if not m_kv:
            unmatched_lines.append(ln)
            continue
        raw_label = " ".join(m_kv.group(1).split())
        val_str = m_kv.group(2).strip()
        label_clean = _strip_label_qualifiers(raw_label)

        # Never parse reference/invoice/ID lines as tax or transaction amounts
        if _NON_AMOUNT_LABEL_RE.search(raw_label) and not _MONETARY_LABEL_SUFFIX_RE.search(
            label_clean
        ):
            continue

        m_explicit_curr = _CURRENCY_ANY_RE.search(val_str)
        m_plain_num = re.match(
            r"^\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?:PKR|Rs\.?)?\s*$",
            val_str,
            re.IGNORECASE,
        )
        m_curr = m_explicit_curr or m_plain_num
        if not m_curr:
            unmatched_lines.append(ln)
            continue
        amt = parse_amount_value(m_curr.group(1))
        if amt is None:
            continue

        if _TOTAL_LABEL_RE.match(label_clean):
            explicit_total = amt
        elif _BASE_LABEL_RE.match(raw_label) or _BASE_LABEL_RE.match(label_clean):
            if explicit_base is None:
                explicit_base = amt
                if _EXCLUSIVE_RE.search(raw_label) or _EXCLUSIVE_RE.search(val_str):
                    base_excl_tax = True
                elif _INCLUSIVE_RE.search(raw_label) or _INCLUSIVE_RE.search(val_str):
                    base_incl_tax = True
        elif _SUMMARY_TAX_LABEL_RE.match(label_clean):
            # Require explicit PKR/Rs currency on tax/fee lines
            if m_explicit_curr is not None:
                summary_tax_sum += amt
        elif _INDIVIDUAL_TAX_LABEL_RE.search(raw_label) and not _EXCLUSIVE_RE.search(raw_label):
            if m_explicit_curr is not None:
                individual_tax_sum += amt
        else:
            unmatched_lines.append(ln)

    # 2. Check unmatched/prose lines for inline tax mentions without double-counting labeled lines
    for m_tax in _INLINE_TAX_RE.finditer("\n".join(unmatched_lines)):
        val = parse_amount_value(m_tax.group(1))
        if val is not None:
            individual_tax_sum += val

    tax_fee_amount = round(
        individual_tax_sum if individual_tax_sum > 0.0 else summary_tax_sum, 2
    )

    # 3. Extract hero amount ("You paid Rs. X to ...")
    for probe in (subject or "", joined_safe):
        m_hero = _HERO_AMOUNT_RE.search(probe)
        if m_hero:
            hero_amount = parse_amount_value(m_hero.group(1))
            if hero_amount is not None:
                break

    # 4. Reconcile base amount vs total charged to strictly avoid double counting
    base_amount: Optional[float] = None
    if explicit_base is not None:
        if (
            tax_fee_amount > 0.0
            and explicit_base > tax_fee_amount
            and not base_excl_tax
            and (
                base_incl_tax
                or (explicit_total is not None and abs(explicit_base - explicit_total) <= 0.05)
            )
        ):
            base_amount = round(explicit_base - tax_fee_amount, 2)
        else:
            base_amount = round(explicit_base, 2)
    elif explicit_total is not None:
        if tax_fee_amount > 0.0 and explicit_total > tax_fee_amount:
            base_amount = round(explicit_total - tax_fee_amount, 2)
        else:
            base_amount = round(explicit_total, 2)
    elif hero_amount is not None:
        if (
            tax_fee_amount > 0.0
            and hero_amount > tax_fee_amount
            and _INCLUSIVE_RE.search(joined_safe)
        ):
            base_amount = round(hero_amount - tax_fee_amount, 2)
        else:
            base_amount = round(hero_amount, 2)
    else:
        for ln in safe_lines:
            if _NON_AMOUNT_LABEL_RE.search(ln):
                continue
            if _INDIVIDUAL_TAX_LABEL_RE.search(ln) or _SUMMARY_TAX_LABEL_RE.search(ln):
                continue
            m_any = _CURRENCY_ANY_RE.search(ln)
            if m_any:
                cand = parse_amount_value(m_any.group(1))
                if cand is not None:
                    base_amount = cand
                    break
        if base_amount is None and subject:
            m_subj = _CURRENCY_ANY_RE.search(subject)
            if m_subj:
                base_amount = parse_amount_value(m_subj.group(1))

    if (base_amount is None or base_amount <= 0.0) and tax_fee_amount > 0.0:
        return tax_fee_amount, 0.0

    return base_amount, tax_fee_amount


def _detect_nayapay_direction(subject: str, joined_text: str) -> TxType:
    """Detect CREDIT vs DEBIT on a NayaPay receipt, prioritizing refund/credit hero cues."""
    combined = f"{subject or ''}\n{joined_text}"
    if _HERO_CREDIT_RE.search(combined) and not _HERO_DEBIT_RE.search(combined):
        return "CREDIT"
    if re.search(r"(?i)\brefund\b", combined) and not re.search(
        r"(?i)\b(?:you\s+paid|money\s+sent|debited)\b", combined
    ):
        return "CREDIT"

    has_credit = bool(_CREDIT_RE.search(combined))
    has_debit = bool(_DEBIT_RE.search(combined))
    if has_credit and not has_debit:
        return "CREDIT"
    if has_debit and not has_credit:
        return "DEBIT"
    if _CREDIT_RE.search(subject or "") and not _HERO_DEBIT_RE.search(subject or ""):
        return "CREDIT"
    return "DEBIT"


def _extract_nayapay_datetime(lines: list[str], fallback_date: str) -> tuple[str, str]:
    """Extract normalized transaction date and timestamp from NayaPay lines."""
    date_part = ""
    time_part = ""
    for ln in lines:
        m_dt = re.match(
            r"(?i)^(?:transaction\s+)?date\s*(?:&|and|/)\s*time\s*[:\-]\s*(.+)$",
            ln,
        )
        if m_dt:
            return normalize_datetime_str(m_dt.group(1), fallback_date)
        if not date_part:
            m_d = re.match(r"(?i)^(?:transaction\s+)?date\s*[:\-]\s*(.+)$", ln)
            if m_d:
                date_part = m_d.group(1).strip()
        if not time_part:
            m_t = re.match(r"(?i)^(?:transaction\s+)?time\s*[:\-]\s*(.+)$", ln)
            if m_t:
                time_part = m_t.group(1).strip()

    if date_part:
        combined = f"{date_part} {time_part}".strip() if time_part else date_part
        return normalize_datetime_str(combined, fallback_date)

    joined = "\n".join(lines)
    m_on = re.search(
        r"(?i)\bon\s+(\d{1,2}[-/ ](?:[A-Za-z]{3,9}|\d{1,2})[-/ ]\d{2,4}"
        r"(?:\s+(?:at\s+)?\d{1,2}:\d{2}(?::\d{2})?(?:\s*[APap][Mm])?)?|"
        r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2})?)",
        joined,
    )
    if m_on:
        return normalize_datetime_str(m_on.group(1), fallback_date)

    return normalize_datetime_str(fallback_date)


def _extract_nayapay_channel(subject: str, joined_text: str) -> Optional[str]:
    """Identify NayaPay payment channel (Card, Raast, Bill Payment, IBFT, Wallet)."""
    combined = f"{subject or ''}\n{joined_text}"
    m_lbl = re.search(
        r"(?im)^(?:payment\s+(?:method|channel)|channel|transaction\s+type|mode)\s*[:\-]\s*(.+)$",
        joined_text,
    )
    probe = m_lbl.group(1) if m_lbl else combined

    if re.search(r"(?i)\braast\b", probe) or re.search(r"(?i)\braast\b", combined):
        return "Raast"
    if re.search(
        r"(?i)\b(?:card|visa|mastercard|paypak|pos|online\s+payment)\b", probe
    ) or re.search(r"(?i)\bcard\s+transaction\b", combined):
        return "Card"
    if re.search(r"(?i)\bbill\s+payment\b|\butility\b|\bbiller\b", combined):
        return "Bill Payment"
    if re.search(r"(?i)\bibft\b", combined):
        return "IBFT"
    if re.search(r"(?i)\b(?:wallet|p2p|money\s+sent|money\s+received)\b", combined):
        return "Wallet"
    return None


def _extract_nayapay_payee(
    lines: list[str], subject: str, direction: TxType
) -> tuple[str, Optional[str]]:
    """Extract merchant/recipient/Raast IBAN and optional IBAN note."""
    expanded: list[str] = []
    for ln in lines:
        if "|" in ln:
            expanded.extend(seg.strip() for seg in ln.split("|") if seg.strip())
        else:
            expanded.append(ln)
    joined = "\n".join(expanded)
    iban_match = _IBAN_RE.search(joined)
    iban_str = iban_match.group(0).upper() if iban_match else None

    labels = _CREDIT_PAYEE_LABELS if direction == "CREDIT" else _DEBIT_PAYEE_LABELS
    for label in labels:
        pattern = re.compile(rf"(?im)^{re.escape(label)}\s*[:\-]\s*(.+)$")
        m = pattern.search(joined)
        if m:
            val = _strip_trailing_emojis(m.group(1).strip())
            if (
                val
                and not _is_masked_identifier(val)
                and not re.fullmatch(
                    r"(?i)(?:PKR|Rs\.?|PK\s*Rs\.?)\s*[0-9,]+(?:\.[0-9]{1,2})?",
                    val,
                )
            ):
                notes = (
                    f"Raast IBAN: {iban_str}"
                    if (iban_str and iban_str not in val.upper())
                    else None
                )
                return val[:120], notes

    for probe in (subject or "", joined):
        clean_probe = "\n".join(
            _strip_trailing_emojis(ln) for ln in probe.splitlines()
        ).strip()
        if not clean_probe:
            continue
        m_paid = re.search(
            r"(?i)\b(?:you\s+(?:paid|spent|sent)|money\s+sent|sent|paid|spent)\s+"
            r"(?:PKR|Rs\.?|PK\s*Rs\.?)\s*[0-9][0-9,]*(?:\.[0-9]{1,2})?\s+"
            r"(?:to|at)\s+([A-Za-z0-9 *&'.\-/()]{2,70}?)"
            r"(?:\s+on\s+\d|\s+via\s+|\s+using\s+|\s+with\s+|\n|\.(?=\s|$)|$)",
            clean_probe,
        )
        if m_paid:
            val = _strip_trailing_emojis(m_paid.group(1).strip())
            if val and not _is_masked_identifier(val):
                notes = (
                    f"Raast IBAN: {iban_str}"
                    if (iban_str and iban_str not in val.upper())
                    else None
                )
                return val[:120], notes

        m_rec = re.search(
            r"(?i)\b(?:money\s+received|you\s+(?:received|got)|received|got)\s+"
            r"(?:PKR|Rs\.?|PK\s*Rs\.?)\s*[0-9][0-9,]*(?:\.[0-9]{1,2})?\s+"
            r"from\s+([A-Za-z0-9 *&'.\-/()]{2,70}?)"
            r"(?:\s+on\s+\d|\s+via\s+|\n|\.(?=\s|$)|$)",
            clean_probe,
        )
        if m_rec:
            val = _strip_trailing_emojis(m_rec.group(1).strip())
            if val and not _is_masked_identifier(val):
                notes = (
                    f"Raast IBAN: {iban_str}"
                    if (iban_str and iban_str not in val.upper())
                    else None
                )
                return val[:120], notes

    combined_text = f"{subject or ''}\n{joined}"
    if re.search(r"(?i)\bcash\s+withdrawal\b", combined_text):
        return "ATM Cash Withdrawal", None
    if re.search(r"(?i)\bsubscribed\s+to\s+(?:a\s+)?mobile\s+bundle\b", combined_text):
        return "Mobile Bundle", None
    if re.search(r"(?i)\btopped\s+up\s+(?:your\s+)?mobile\s+balance\b", combined_text):
        return "Mobile Balance Top-Up", None

    m_raast_id = re.search(
        r"(?im)^(?:raast\s+id|receiver\s+iban|iban|receiver\s+account|account\s+number)\s*[:\-]\s*(.+)$",
        joined,
    )
    if m_raast_id:
        val = _strip_trailing_emojis(m_raast_id.group(1).strip())
        if val and not _is_masked_identifier(val):
            return val[:120], None

    if iban_str:
        return iban_str, None

    clean_subject = _strip_trailing_emojis(subject or "")
    return (clean_subject or "NayaPay Transaction")[:120], None


def parse_nayapay_email(
    message_id: str,
    subject: str,
    sender: str,
    body: str,
    fallback_date: str = "",
) -> Optional[ParsedTransaction]:
    """Parse a NayaPay HTML/text receipt email into a ParsedTransaction."""
    if not message_id or not is_nayapay_email(subject, sender, body):
        return None
    if is_ignored_nayapay_email(subject, body):
        return None

    lines = _nayapay_html_to_lines(body)
    joined_text = "\n".join(lines)

    amount, tax_fee_amount = _extract_amounts_and_taxes(lines, subject)
    if amount is None or amount <= 0.0:
        return None

    direction = _detect_nayapay_direction(subject, joined_text)
    tx_date, tx_timestamp = _extract_nayapay_datetime(lines, fallback_date)
    payment_channel = _extract_nayapay_channel(subject, joined_text)
    payee, notes = _extract_nayapay_payee(lines, subject, direction)

    suggested_bucket: Optional[CostBucket] = "INFLOW" if direction == "CREDIT" else None

    return ParsedTransaction(
        message_id=message_id,
        tx_date=tx_date,
        tx_timestamp=tx_timestamp,
        account_name=WALLET_NAYAPAY,
        target_account=None,
        tx_type=direction,
        amount=amount,
        tax_fee_amount=tax_fee_amount,
        payee=payee,
        source="GMAIL_NAYAPAY",
        payment_channel=payment_channel,
        raw_subject=subject.strip() if subject and subject.strip() else None,
        notes=notes,
        suggested_bucket=suggested_bucket,
    )

"""Multi-bank PDF and CSV statement importer for NayaPay and HabibMetro."""

from __future__ import annotations

import csv
import hashlib
import io
import re
from pathlib import Path
from typing import Any, BinaryIO, Optional

from pypdf import PdfReader

from src.ingestion.hmb_parser import (
    classify_hmb_transfer,
    normalize_datetime_str,
    parse_amount_value,
)
from src.models import (
    WALLET_CASH,
    WALLET_HABIBMETRO,
    WALLET_MMF,
    WALLET_NAYAPAY,
    CostBucket,
    ParsedTransaction,
    SourceChannel,
    TxType,
)

_CANONICAL_WALLETS = {
    WALLET_HABIBMETRO,
    WALLET_NAYAPAY,
    WALLET_CASH,
    WALLET_MMF,
}
_MAX_STATEMENT_BYTES = 15 * 1024 * 1024
_MAX_PDF_PAGES = 150
_MAX_CSV_ROWS = 25_000
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _strip_csv_formula_guard(value: str) -> str:
    """Undo Excel formula-injection apostrophe prefix when re-importing exported CSVs."""
    if len(value) >= 2 and value[0] == "'" and value[1] in _CSV_FORMULA_PREFIXES:
        return value[1:]
    return value
_VALID_COST_BUCKETS: set[str] = {
    "INFLOW",
    "FIXED",
    "VARIABLE",
    "LEAKAGE",
    "INVESTMENT",
    "IGNORE",
}

_MONTH_NAME_PAT = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*"
_LEADING_DATE_RE = re.compile(
    rf"^\s*(?P<date>"
    rf"\d{{4}}[-/]\d{{1,2}}[-/]\d{{1,2}}"
    rf"|\d{{1,2}}[-/ ]\d{{1,2}}[-/ ]\d{{2,4}}"
    rf"|\d{{1,2}}[-/ ]{_MONTH_NAME_PAT}[-/, ]+\d{{2,4}}"
    rf"|{_MONTH_NAME_PAT}[-/ ]+\d{{1,2}}[-/, ]+\d{{2,4}}"
    rf")"
    r"(?:(?:[\s,T]+|[\s|,\-]+)(?P<time>\d{1,2}:\d{2}(?::\d{2})?(?:\s*[APap][Mm])?))?"
    r"(?P<rest>.*)$",
    re.IGNORECASE,
)
_DATE_RANGE_CONTINUATION_RE = re.compile(
    rf"^\s*(?:[-–—]|to\b)\s*(?:"
    rf"\d{{4}}[-/]\d{{1,2}}[-/]\d{{1,2}}"
    rf"|\d{{1,2}}[-/ ]\d{{1,2}}[-/ ]\d{{2,4}}"
    rf"|\d{{1,2}}[-/ ]{_MONTH_NAME_PAT}[-/, ]+\d{{2,4}}"
    rf"|{_MONTH_NAME_PAT}[-/ ]+\d{{1,2}}[-/, ]+\d{{2,4}}"
    rf")\b",
    re.IGNORECASE,
)
_BARE_MONTH_YEAR_HEADER_RE = re.compile(
    rf"^(?:{_MONTH_NAME_PAT}\s+\d{{4}}(?:\s*$|.*\bcurrent\s+mailing\s+address\b))",
    re.IGNORECASE,
)

_SUMMARY_SKIP_RE = re.compile(
    r"(?i)\b(?:opening\s+bal(?:ance)?|closing\s+bal(?:ance)?|brought\s+forward|"
    r"carried\s+forward|total\s+debits?|total\s+credits?|total\s+income|"
    r"total\s+deposits?|total\s+spent|total\s+withdrawals?|statement\s+period|"
    r"end\s+of\s+statement|page\s+\d+\s+of\s+\d+)\b"
)
_PDF_HEADER_FOOTER_RE = re.compile(
    r"(?i)(?:^\s*page\s+\d+\b(?:\s*(?:/|of)\s*\d+)?(?:\s*[\.\-\|].*)?$"
    r"|^\s*(?:note|disclaimer|important)\s*:"
    r"|\bthis\s+is\s+a\s+computer[\s\-]+generated\b"
    r"|\bstatement\s+period\b"
    r"|\b(?:account|wallet|bank|e-?)\s*statement\b"
    r"|\bfor\s+(?:[^\n]{0,40}\s+)?(?:queries|assistance|support)\b"
    r"|\b111[-\s]?\d{3}[-\s]?\d{3}\b"
    r"|\b(?:customer\s+(?:support|service|care)|helpline|uan\s*:|toll[\s\-]?free)\b"
    r"|\b(?:generated|printed)\s+on\b"
    r"|\b(?:all\s+rights\s+reserved|authorized\s+signature)\b"
    r"|^\s*time\s+type\s+description\b"
    r"|^\s*(?:transaction\s+)?date\s+(?:description|particulars|details|narration)\b)"
)
_OPENING_BAL_RE = re.compile(
    r"(?i)\b(?:opening\s+bal(?:ance)?|brought\s+forward|b/f\s+bal(?:ance)?)\b"
    r"[^\d\-+]{0,30}(?:PKR|Rs\.?)?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)"
)

_FOREIGN_CURR_CODES = r"(?:USD|EUR|GBP|AED|SAR|CAD|AUD|TRY|CNY|JPY)"
_FOREIGN_CURR_TOKEN_RE = re.compile(
    rf"(?i)(?:(?<![A-Za-z0-9])[+\-]?\s*)?"
    rf"(?:\b{_FOREIGN_CURR_CODES}\s*[0-9][0-9,]*(?:\.[0-9]{{1,2}})?\b"
    rf"|\b[0-9][0-9,]*(?:\.[0-9]{{1,2}})?\s*{_FOREIGN_CURR_CODES}\b(?!\s*[0-9]))"
)

_MONEY_TOKEN_RE = re.compile(
    r"(?i)(?:(?<![A-Za-z0-9.\-])(?P<sign>[+\-])(?=(?:PKR|Rs\.?|PK\s*Rs\.?|[0-9]))"
    r"|(?<=[\s|])(?P<sign_s1>[+\-])\s+(?=(?:PKR|Rs\.?|PK\s*Rs\.?|[0-9]{1,3}(?:,[0-9]{2,3})+(?:\.[0-9]{2})?|[0-9]+\.[0-9]{2}))"
    r"|(?<=^)(?P<sign_s2>[+\-])\s+(?=(?:PKR|Rs\.?|PK\s*Rs\.?|[0-9]{1,3}(?:,[0-9]{2,3})+(?:\.[0-9]{2})?|[0-9]+\.[0-9]{2})))?"
    r"(?:(?P<curr>PKR|Rs\.?|PK\s*Rs\.?)\s*)?"
    r"(?:(?<![A-Za-z0-9.\-])(?P<sign_s3>[+\-])(?=[0-9]))?"
    r"(?P<num>(?(curr)[0-9][0-9,]*(?:\.[0-9]{1,2})?|(?:[0-9]{1,3}(?:,[0-9]{2,3})+(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{2}))))"
    r"(?:/-)?"
    r"(?:\s*(?P<drcr>DR|CR|Debit(?!\s+Card\b)|Credit(?!\s+Card\b))\b)?"
)
_EXPLICIT_CURR_OR_SIGNED_RE = re.compile(
    r"(?i)(?:(?<![A-Za-z0-9.\-])(?P<sign>[+\-])|(?<=[\s|])(?P<sign_s1>[+\-])\s+|(?<=^)(?P<sign_s2>[+\-])\s+)"
    r"(?:PKR|Rs\.?|PK\s*Rs\.?)?\s*(?P<num>[0-9][0-9,]*(?:\.[0-9]{1,2})?)"
    r"|(?:PKR|Rs\.?|PK\s*Rs\.?)\s*(?P<cnum>[0-9][0-9,]*(?:\.[0-9]{1,2})?)(?:/-)?"
    r"|(?P<dnum>[0-9][0-9,]*(?:\.[0-9]{1,2})?)(?:/-)?\s*(?P<drcr>DR|CR)\b"
)
_SINGLE_BALANCE_PREFIX_RE = re.compile(
    r"(?i)(?:avail(?:able)?|closing|opening|running|current|ledger|wallet|a/c|acct\.?)?\s*"
    r"\bbal(?:ance)?\.?\s*[:\-=]?\s*(?:PKR|Rs\.?|PK\s*Rs\.?)?\s*$"
)

_CREDIT_DESC_RE = re.compile(
    r"(?i)\b(?:credit(?:ed)?|salary|received|inward|incoming|deposit(?:ed)?|refund|"
    r"cashback|top-?up\s+from|remittance|profit|dividend|cr\b)\b"
)
_DEBIT_DESC_RE = re.compile(
    r"(?i)\b(?:debit(?:ed)?|paid|payment|purchase|pos|atm|withdrawal|sent|outgoing|"
    r"ibft\s+to|transfer\s+to|raast\s+to|bill|fee|wht|tax|charges|fed|dr\b)\b"
)

_TAX_FEE_DESC_RE = re.compile(
    r"(?i)\b(?:wht|withholding\s+tax|advance\s+tax|fx\s+fee|foreign\s+fee|"
    r"service\s+fee|annual\s+fee|card\s+fee|sms\s+(?:alert\s+)?(?:fee|charges)|"
    r"bank\s+charges|fed|fbr\s+tax|surcharge|excise)\b"
)


def _normalize_account_name(account_hint: Optional[str]) -> Optional[str]:
    """Normalize friendly bank/wallet names to one of the 4 canonical wallet names or None."""
    if not account_hint:
        return None
    raw = str(account_hint).strip()
    if not raw or raw.lower() in ("nan", "none", "auto-detect"):
        return None
    if raw in _CANONICAL_WALLETS:
        return raw

    low = " ".join(re.sub(r"[_\-]+", " ", raw.lower()).split())
    if re.search(r"\b(?:nayapay|naya\s+pay|np)\b", low):
        return WALLET_NAYAPAY
    if re.search(r"\b(?:habibmetro|habib\s+metro|habibmetropolitan|habib|hmb)\b", low):
        return WALLET_HABIBMETRO
    if re.search(r"\bcash(?:\s+in\s+hand)?\b", low):
        return WALLET_CASH
    if re.search(r"\b(?:mmf|money\s+market(?:\s+fund)?|al\s+meezan)\b", low):
        return WALLET_MMF
    return None


def _detect_statement_wallet(text: str, filename: str = "") -> str:
    """Detect institution header-first from filename, IBAN/branding header, and document structure."""
    fname_low = (filename or "").strip().lower()
    if any(k in fname_low for k in ("nayapay", "naya_pay", "naya-pay")):
        return WALLET_NAYAPAY
    if any(k in fname_low for k in ("habib", "hmb")):
        return WALLET_HABIBMETRO

    # Collect top header/summary lines before the first transaction row starts
    norm_text = (text or "").replace("\xa0", " ").replace("\u2212", "-").replace("\u2013", "-")
    non_empty = [re.sub(r"[ \t]+", " ", ln).strip() for ln in norm_text.splitlines() if ln.strip()]
    header_lines: list[str] = []
    for ln in non_empty[:35]:
        # Keep scanning past metadata/summary lines even if they mention dates (e.g. Statement Period)
        has_money = bool(
            re.search(
                r"(?i)(?:PKR|Rs\.?|PK\s*Rs\.?|[0-9]{1,3}(?:,[0-9]{2,3})+\.[0-9]{2}|[0-9]+\.[0-9]{2})",
                ln,
            )
        )
        m_hdr_lead = _LEADING_DATE_RE.match(ln)
        is_tx_start = (
            bool(m_hdr_lead)
            and not _DATE_RANGE_CONTINUATION_RE.match(m_hdr_lead.group("rest") or "")
            and has_money
            and not _SUMMARY_SKIP_RE.search(ln)
            and not re.search(
                r"(?i)\b(?:statement\s+period|period|closing|opening)\b", ln
            )
        )
        if is_tx_start:
            break
        header_lines.append(ln)
    top_header = "\n".join(header_lines)[:2500].lower()

    hmb_in_header = bool(
        re.search(r"\b(?:habibmetro|habib\s*metro|habibmetropolitan|hmb)\b|\bpk\d{2}mpbl[a-z0-9]{6,}\b", top_header)
    )
    np_in_header = bool(
        re.search(
            r"\b(?:nayapay|naya\s*pay)\b|@nayapay|\bpk\d{2}naya[a-z0-9]{6,}\b|nayapay\.com",
            top_header,
        )
    )
    if np_in_header and not hmb_in_header:
        return WALLET_NAYAPAY
    if hmb_in_header and not np_in_header:
        return WALLET_HABIBMETRO

    full_low = norm_text.lower()
    np_score = 0
    hmb_score = 0

    if re.search(r"@nayapay|\bnayapay\s+id\b|\bpk\d{2}naya[a-z0-9]{6,}\b|nayapay\.com|help@nayapay", full_low):
        np_score += 5
    if re.search(r"\bnayapay\s+(?:wallet\s+)?e?-?statement\b", full_low):
        np_score += 4
    if re.search(r"(?m)^\s*time\s+type\s+description\s+amount\s+balance\b", full_low):
        np_score += 3
    if re.search(r"(?m)(?:^|\s)[+\-]\s*rs\.?\s*[0-9]", full_low):
        np_score += 2
    if re.search(r"(?<!to\s)(?<!from\s)(?<!ibft\s)(?<!via\s)\bnayapay\b", full_low):
        np_score += 2

    if re.search(r"\bhabibmetropolitan\b|\bpk\d{2}mpbl[a-z0-9]{6,}\b|myhabibmetro", full_low):
        hmb_score += 5
    if re.search(r"\bhabib\s*metro(?:politan)?\s+(?:bank\s+)?(?:account\s+|e-?)?statement\b", full_low):
        hmb_score += 4
    if re.search(r"(?<!to\s)(?<!from\s)(?<!ibft\s)(?<!via\s)\bhabib\s*metro(?:politan)?\b", full_low):
        hmb_score += 2

    if np_score > hmb_score:
        return WALLET_NAYAPAY
    if hmb_score > np_score:
        return WALLET_HABIBMETRO

    if any(k in full_low for k in ("habibmetro", "habib metro", "habibmetropolitan")):
        return WALLET_HABIBMETRO
    if "nayapay" in full_low or "raast" in full_low or "card transaction" in full_low:
        return WALLET_NAYAPAY
    return WALLET_HABIBMETRO


def _build_pdf_message_id(
    account_name: str,
    tx_date: str,
    amount: float,
    payee: str,
    occurrence_idx: int,
) -> str:
    """Generate deterministic PDF_{account}_{tx_date}_{sha256_12} identifier."""
    payee_clean = " ".join(payee.split()).strip()
    key_tuple = (
        account_name.strip().upper(),
        tx_date,
        f"{amount:.2f}",
        payee_clean[:10].upper(),
        str(occurrence_idx),
    )
    digest = hashlib.sha256("|".join(key_tuple).encode("utf-8")).hexdigest()[:12]
    account_slug = re.sub(r"[^A-Za-z0-9]+", "", account_name).upper() or "BANK"
    return f"PDF_{account_slug}_{tx_date}_{digest}"


def _normalize_pdf_text(text: str) -> str:
    """Normalize Unicode spaces and minus/dash glyphs in extracted PDF text."""
    if not text:
        return ""
    return (
        text.replace("\xa0", " ")
        .replace("\u2007", " ")
        .replace("\u202f", " ")
        .replace("\u2212", "-")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
    )


def _detect_channel_from_text(text: str) -> Optional[str]:
    """Infer payment channel from statement row description."""
    if re.search(r"(?i)\braast\b", text):
        return "Raast"
    if re.search(r"(?i)\b(?:pos|debit\s+card|mastercard|visa|paypak)\b", text) and not re.search(
        r"(?i)\b(?:cash\s+withdrawal|atm\s+withdrawal)\b", text
    ):
        return "Card"
    if re.search(r"(?i)\batm\b|\bcash\s+withdrawal\b", text):
        return "ATM"
    if re.search(r"(?i)\bibft\b", text):
        return "IBFT"
    if re.search(r"(?i)\bcard\b", text):
        return "Card"
    if re.search(r"(?i)\bbill\b|\bk-?electric\b|\bssgc\b|\bsngpl\b|\bptcl\b", text):
        return "Bill Payment"
    if re.search(r"(?i)\bwallet\b|\bp2p\b|\bpeer[\s\-]+to[\s\-]+peer\b", text):
        return "Wallet"
    return None


_TRAILING_MONEY_END_RE = re.compile(
    r"(?i)(?:(?:PKR|Rs\.?|PK\s*Rs\.?)\s*)?"
    r"(?:[0-9]{1,3}(?:,[0-9]{2,3})+(?:\.[0-9]{2})|[0-9]+\.[0-9]{2}"
    r"|(?:PKR|Rs\.?|PK\s*Rs\.?)\s*[0-9][0-9,]*)"
    r"(?:/-)?(?:\s*(?:DR|CR|Debit|Credit))?\s*$"
)
_NON_CONTINUATION_LINE_RE = re.compile(
    r"(?i)(?:^\s*(?:page|pg)\.?\s*\d+\b"
    r"|^\W*\d+\W*$"
    r"|www\.|https?://|[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}"
    r"|\b(?:call|dial|visit|contact|email|website|queries|inquiries|assistance|"
    r"support|discrepancy|complaints?|helpline|uan|toll[\s\-]?free|"
    r"thank\s+you|dear\s+customer|please\s+(?:note|contact|call|visit)|"
    r"if\s+you|in\s+case\s+of|subject\s+to|terms\s+(?:&|and)\s+conditions|"
    r"head\s+office|registered\s+office|regards)\b"
    r"|\b(?:111|0800)[-\s]?\d{3}[-\s]?\d{3,4}\b"
    r"|\b0\d{2,3}[-\s]\d{7,8}\b)"
)
_STANDALONE_TIME_PREFIX_RE = re.compile(
    r"^\s*(?P<time>(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?(?:\s*[AP]M)?)(?:\s+(?P<rest>.*))?$",
    re.IGNORECASE,
)
_PURE_MONEY_LINE_RE = re.compile(
    r"(?i)^\s*(?:[+\-]?\s*(?:(?:PKR|Rs\.?|PK\s*Rs\.?)\s*)?"
    r"(?:[0-9]{1,3}(?:,[0-9]{2,3})+(?:\.[0-9]{2})?|[0-9]+(?:\.[0-9]{2})?)"
    r"(?:/-)?(?:\s*(?:DR|CR|Debit|Credit))?\s*)+$"
)
_FEE_DISCLOSURE_LINE_RE = re.compile(
    r"(?i)\b(?:service\s+charges?|fed|excise|wht|withholding|advance\s+tax|"
    r"govt\.?\s+tax|gst|fx\s+fee|intl\.?\s+fee|conversion\s+fee|charges?\s*/?\s*fed)\b"
)
_REF_ID_LINE_RE = re.compile(
    r"(?i)^(?:transaction\s*id|txn\s*id|ref(?:erence)?\s*(?:no\.?|id|#|code)?|stan|rrn|auth(?:orization)?\s*(?:code|id))\b"
)


def _extract_pkr_money_tokens(scan_text: str) -> list[dict[str, Any]]:
    """Extract normalized PKR money tokens from already FX-masked text."""
    tokens: list[dict[str, Any]] = []
    for m in _MONEY_TOKEN_RE.finditer(scan_text):
        num_str = m.group("num")
        if "," not in num_str and "." not in num_str and len(num_str) >= 10:
            continue
        gd = m.groupdict()
        sign = (
            gd.get("sign")
            or gd.get("sign_s1")
            or gd.get("sign_s2")
            or gd.get("sign_s3")
        )
        tokens.append(
            {
                "start": m.start(),
                "end": m.end(),
                "val": round(float(num_str.replace(",", "")), 2),
                "sign": sign,
                "drcr": (gd.get("drcr") or "").upper(),
                "raw": m.group(0),
            }
        )

    if not tokens:
        for m in _EXPLICIT_CURR_OR_SIGNED_RE.finditer(scan_text):
            gd = m.groupdict()
            raw_num = gd.get("num") or gd.get("cnum") or gd.get("dnum")
            if raw_num:
                sign = (
                    gd.get("sign")
                    or gd.get("sign_s1")
                    or gd.get("sign_s2")
                    or gd.get("sign_s3")
                )
                tokens.append(
                    {
                        "start": m.start(),
                        "end": m.end(),
                        "val": round(float(raw_num.replace(",", "")), 2),
                        "sign": sign,
                        "drcr": (gd.get("drcr") or "").upper(),
                        "raw": m.group(0),
                    }
                )
    return tokens


def _record_ends_with_money(parts: list[str]) -> bool:
    """Return True when the accumulated record already terminates with PKR money/balance tokens."""
    acc = " ".join(parts)
    acc_masked = _FOREIGN_CURR_TOKEN_RE.sub("", acc).strip()
    return bool(_TRAILING_MONEY_END_RE.search(acc_masked))


def _looks_like_continuation_desc(line: str) -> bool:
    """Return False for footers, disclaimers, contact info, or boilerplate sentences."""
    if not line or _NON_CONTINUATION_LINE_RE.search(line):
        return False
    words = line.split()
    if len(words) >= 6 and re.match(r"^(?:For|If|Please|Note|In|This|Thank)\b", line):
        return False
    return True


def _group_pdf_records(text: str) -> tuple[list[dict[str, Any]], Optional[float], bool]:
    """Group PDF lines into structured transaction records, opening balance, and column order."""
    norm_text = _normalize_pdf_text(text)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in norm_text.splitlines()]
    lines = [ln for ln in lines if ln]

    opening_balance: Optional[float] = None
    credit_before_debit = False
    records: list[dict[str, Any]] = []
    current_rec: Optional[dict[str, Any]] = None

    def _flush_current() -> None:
        nonlocal current_rec
        if current_rec is not None:
            records.append(
                {
                    "text": " ".join(current_rec["parts"]),
                    "extra_time": current_rec["extra_time"],
                    "extra_desc": list(current_rec["extra_desc"]),
                    "extra_notes": list(current_rec["extra_notes"]),
                }
            )
            current_rec = None

    for ln in lines:
        if opening_balance is None:
            m_open = _OPENING_BAL_RE.search(ln)
            if m_open:
                opening_balance = parse_amount_value(m_open.group(1))
                _flush_current()
                continue

        # Detect column header layout (Credit before Debit vs Debit before Credit)
        if re.search(r"(?i)\bcredit\b", ln) and re.search(r"(?i)\bdebit\b", ln):
            _flush_current()
            if ln.lower().find("credit") < ln.lower().find("debit"):
                credit_before_debit = True
            continue

        m_lead_line = _LEADING_DATE_RE.match(ln)
        if m_lead_line and _DATE_RANGE_CONTINUATION_RE.match(
            m_lead_line.group("rest") or ""
        ):
            _flush_current()
            continue

        if (_SUMMARY_SKIP_RE.search(ln) or _PDF_HEADER_FOOTER_RE.search(ln)) and not m_lead_line:
            _flush_current()
            continue

        if m_lead_line:
            _flush_current()
            current_rec = {
                "parts": [ln],
                "has_lead_time": bool((m_lead_line.groupdict().get("time") or "").strip()),
                "extra_time": "",
                "extra_desc": [],
                "extra_notes": [],
            }
            continue

        if current_rec is None:
            continue

        # Peel stacked timestamp on continuation line (e.g. "08:29 PM" under "Oct 15, 2026")
        if not current_rec["has_lead_time"] and not current_rec["extra_time"]:
            m_time = _STANDALONE_TIME_PREFIX_RE.match(ln)
            if m_time:
                current_rec["extra_time"] = m_time.group("time").strip()
                ln = (m_time.group("rest") or "").strip()
                if not ln:
                    continue

        if _record_ends_with_money(current_rec["parts"]):
            if not _looks_like_continuation_desc(ln):
                _flush_current()
                continue

            primary_masked = _FOREIGN_CURR_TOKEN_RE.sub(" ", " ".join(current_rec["parts"]))
            cont_masked = _FOREIGN_CURR_TOKEN_RE.sub(" ", ln)
            primary_tokens = _extract_pkr_money_tokens(primary_masked)
            cont_tokens = _extract_pkr_money_tokens(cont_masked)

            if (
                len(primary_tokens) < 2
                and _PURE_MONEY_LINE_RE.match(ln)
                and not _FEE_DISCLOSURE_LINE_RE.search(ln)
            ):
                current_rec["parts"].append(ln)
            elif cont_tokens or _FEE_DISCLOSURE_LINE_RE.search(ln):
                # Keep sub-line fee disclosures / secondary amounts out of main amount/balance columns
                current_rec["extra_notes"].append(ln)
            elif _REF_ID_LINE_RE.search(ln):
                current_rec["extra_notes"].append(ln)
            else:
                current_rec["extra_desc"].append(ln)
        else:
            if _NON_CONTINUATION_LINE_RE.search(ln):
                continue
            current_rec["parts"].append(ln)

    _flush_current()
    return records, opening_balance, credit_before_debit


def _parse_pdf_record(
    rec_obj: dict[str, Any],
    credit_before_debit: bool,
) -> Optional[
    tuple[
        str,
        str,
        str,
        Optional[TxType],
        float,
        Optional[float],
        Optional[str],
        str,
        Optional[str],
    ]
]:
    """Parse one grouped PDF record into tx_date, tx_ts, payee, dir, amount, new_bal, channel, rest, notes."""
    record = rec_obj["text"]
    extra_time = (rec_obj.get("extra_time") or "").strip()
    extra_desc = " ".join(rec_obj.get("extra_desc") or []).strip()
    notes_list = [n.strip() for n in (rec_obj.get("extra_notes") or []) if n and n.strip()]
    notes_str = " | ".join(notes_list) if notes_list else None

    if _SUMMARY_SKIP_RE.search(record):
        return None

    m_lead = _LEADING_DATE_RE.match(record)
    if not m_lead:
        return None

    raw_date = m_lead.group("date")
    raw_time = (m_lead.groupdict().get("time") or extra_time or "").strip()
    dt_input = f"{raw_date} {raw_time}".strip() if raw_time else raw_date
    tx_date, tx_ts = normalize_datetime_str(dt_input)
    rest = m_lead.group("rest").strip()
    if not rest or _DATE_RANGE_CONTINUATION_RE.match(rest):
        return None

    # Mask non-PKR foreign currency tokens with equal-length spaces to preserve offsets
    scan_rest = _FOREIGN_CURR_TOKEN_RE.sub(lambda m: " " * len(m.group(0)), rest)
    tokens = _extract_pkr_money_tokens(scan_rest)

    if not tokens:
        return None

    col_tokens = [tokens[-1]]
    for idx in range(len(tokens) - 2, -1, -1):
        # Measure gap in scan_rest so masked FX tokens (e.g. "1,265.00 USD 4.99 16,885.00") bridge columns
        gap = scan_rest[tokens[idx]["end"] : col_tokens[0]["start"]]
        if re.fullmatch(
            r"[\s|/\-:,]*(?:(?:PKR|Rs\.?|Bal(?:ance)?|DR|CR)\s*[:\-]?\s*)*",
            gap,
            re.IGNORECASE,
        ):
            col_tokens.insert(0, tokens[idx])
            if len(col_tokens) == 3:
                break
        else:
            break

    # Guard against capturing a single running-balance token as transaction amount
    if len(col_tokens) == 1 and _SINGLE_BALANCE_PREFIX_RE.search(
        rest[: col_tokens[0]["start"]]
    ):
        return None

    desc_end = col_tokens[0]["start"]
    desc_raw = rest[:desc_end].strip(" |-:\t")
    if not desc_raw:
        desc_raw = rest[col_tokens[-1]["end"] :].strip(" |-:\t")

    extra_desc_clean = ""
    if extra_desc:
        extra_desc_clean = _FOREIGN_CURR_TOKEN_RE.sub(" ", extra_desc)
        extra_desc_clean = re.sub(r"\(\s*\)", "", extra_desc_clean)
        extra_desc_clean = " ".join(extra_desc_clean.split()).strip(" |-:\t")

    # Strip masked foreign-currency snippets from narration for a clean merchant/payee label
    desc_raw = _FOREIGN_CURR_TOKEN_RE.sub(" ", desc_raw)
    desc_raw = re.sub(r"\(\s*\)", "", desc_raw)
    desc_raw = " ".join(desc_raw.split()).strip(" |-:\t")
    desc_raw = re.sub(
        r"(?i)\b(?:PKR|Rs\.?|DR|CR|Debit|Credit|Balance|Bal)\s*[:\-]?\s*$",
        "",
        desc_raw,
    ).strip(" |-:\t")
    if extra_desc_clean:
        desc_raw = f"{desc_raw} {extra_desc_clean}".strip() if desc_raw else extra_desc_clean

    amount: Optional[float] = None
    new_balance: Optional[float] = None
    direction: Optional[TxType] = None

    if len(col_tokens) >= 3:
        col_a, col_b, bal_tok = col_tokens[-3], col_tokens[-2], col_tokens[-1]
        new_balance = bal_tok["val"]
        if col_a["val"] > 0 and col_b["val"] == 0:
            amount = col_a["val"]
            direction = "CREDIT" if credit_before_debit else "DEBIT"
        elif col_b["val"] > 0 and col_a["val"] == 0:
            amount = col_b["val"]
            direction = "DEBIT" if credit_before_debit else "CREDIT"
        elif col_a["val"] > 0:
            amount = col_a["val"]
        elif col_b["val"] > 0:
            amount = col_b["val"]
    elif len(col_tokens) == 2:
        amt_tok, bal_tok = col_tokens[0], col_tokens[1]
        if amt_tok["val"] == 0 and bal_tok["val"] > 0:
            amount = bal_tok["val"]
            direction = "DEBIT" if credit_before_debit else "CREDIT"
        elif bal_tok["val"] == 0 and amt_tok["val"] > 0:
            amount = amt_tok["val"]
            direction = None
            new_balance = 0.0
        else:
            amount = amt_tok["val"]
            new_balance = bal_tok["val"]
            if amt_tok["sign"] == "-" or amt_tok["drcr"] in ("DR", "DEBIT"):
                direction = "DEBIT"
            elif amt_tok["sign"] == "+" or amt_tok["drcr"] in ("CR", "CREDIT"):
                direction = "CREDIT"
    else:
        amt_tok = col_tokens[0]
        amount = amt_tok["val"]
        if amt_tok["sign"] == "-" or amt_tok["drcr"] in ("DR", "DEBIT"):
            direction = "DEBIT"
        elif amt_tok["sign"] == "+" or amt_tok["drcr"] in ("CR", "CREDIT"):
            direction = "CREDIT"

    if amount is None or amount <= 0.0:
        return None

    combined_rest = f"{rest} {extra_desc_clean}".strip() if extra_desc_clean else rest
    channel = _detect_channel_from_text(combined_rest)
    payee = re.sub(
        r"\s*\|\s*(?:Card|Raast|IBFT|ATM|Wallet|Bill Payment|Debit|Credit|DR|CR)\s*$",
        "",
        desc_raw,
        flags=re.IGNORECASE,
    ).strip(" |-")
    payee = re.sub(
        r"\s+(?:Card|Raast|IBFT|Wallet|Bill Payment)$",
        "",
        payee,
        flags=re.IGNORECASE,
    ).strip(" |-")
    if not payee:
        payee = "Statement Transaction"
    if _BARE_MONTH_YEAR_HEADER_RE.match(payee):
        return None

    return (
        tx_date,
        tx_ts,
        payee[:120],
        direction,
        amount,
        new_balance,
        channel,
        combined_rest,
        notes_str,
    )


def _detect_descending_order(
    raw_rows: list[dict[str, Any]],
    opening_balance: Optional[float],
) -> bool:
    """Detect reverse-chronological (newest-first) row order from dates or balance equations."""
    inc_dates = 0
    dec_dates = 0
    for idx in range(len(raw_rows) - 1):
        d1 = raw_rows[idx]["tx_date"]
        d2 = raw_rows[idx + 1]["tx_date"]
        if d1 < d2:
            inc_dates += 1
        elif d1 > d2:
            dec_dates += 1
    if dec_dates > inc_dates:
        return True
    if inc_dates > dec_dates:
        return False

    # Same-date fallback: compare ascending vs descending balance transition consistency
    asc_score = 0
    desc_score = 0
    for idx in range(len(raw_rows)):
        cur = raw_rows[idx]
        cur_dir = cur["direction"]
        cur_bal = cur["new_bal"]
        cur_amt = cur["amount"]
        if cur_dir is None or cur_bal is None:
            continue
        delta = cur_amt if cur_dir == "CREDIT" else -cur_amt

        if idx > 0 and raw_rows[idx - 1]["new_bal"] is not None:
            if abs((raw_rows[idx - 1]["new_bal"] + delta) - cur_bal) <= 1.0:
                asc_score += 1
        elif idx == 0 and opening_balance is not None:
            if abs((opening_balance + delta) - cur_bal) <= 1.0:
                asc_score += 1

        if idx + 1 < len(raw_rows) and raw_rows[idx + 1]["new_bal"] is not None:
            if abs((raw_rows[idx + 1]["new_bal"] + delta) - cur_bal) <= 1.0:
                desc_score += 1
        elif idx + 1 == len(raw_rows) and opening_balance is not None:
            if abs((opening_balance + delta) - cur_bal) <= 1.0:
                desc_score += 1

    return desc_score > asc_score


def _infer_fallback_pdf_direction(rest: str, payee: str) -> TxType:
    """Fallback direction detection using row markers and description keywords."""
    if re.search(r"(?i)\b(?:CR|Credit)\b", rest) and not re.search(
        r"(?i)\bCredit\s+Card\b", rest
    ):
        return "CREDIT"
    if re.search(r"(?i)\b(?:DR|Debit)\b", rest) and not re.search(
        r"(?i)\bDebit\s+Card\b", rest
    ):
        return "DEBIT"
    if _CREDIT_DESC_RE.search(payee) and not _DEBIT_DESC_RE.search(payee):
        return "CREDIT"
    return "DEBIT"


def _parse_bank_pdf_text(
    text: str,
    account_name: str,
    is_hmb: bool,
) -> list[ParsedTransaction]:
    """Shared PDF text parser for NayaPay and HabibMetro statements."""
    records, opening_balance, credit_before_debit = _group_pdf_records(text)
    raw_rows: list[dict[str, Any]] = []

    for rec in records:
        parsed = _parse_pdf_record(rec, credit_before_debit)
        if parsed is None:
            continue
        tx_date, tx_ts, payee, direction, amount, new_bal, channel, rest, notes = parsed
        raw_rows.append(
            {
                "rec": rec["text"],
                "tx_date": tx_date,
                "tx_ts": tx_ts,
                "payee": payee,
                "direction": direction,
                "amount": amount,
                "new_bal": new_bal,
                "channel": channel,
                "rest": rest,
                "notes": notes,
            }
        )

    if not raw_rows:
        return []

    is_descending = _detect_descending_order(raw_rows, opening_balance)

    occurrences: dict[tuple[str, str, str, str], int] = {}
    parsed_list: list[ParsedTransaction] = []

    for idx, item in enumerate(raw_rows):
        direction: Optional[TxType] = item["direction"]
        amount: float = item["amount"]
        new_bal: Optional[float] = item["new_bal"]

        if direction is None and new_bal is not None:
            if is_descending:
                chrono_prev_bal = (
                    raw_rows[idx + 1]["new_bal"]
                    if idx + 1 < len(raw_rows)
                    else opening_balance
                )
            else:
                chrono_prev_bal = (
                    raw_rows[idx - 1]["new_bal"] if idx > 0 else opening_balance
                )

            if chrono_prev_bal is not None:
                if abs((chrono_prev_bal - amount) - new_bal) <= 1.0:
                    direction = "DEBIT"
                elif abs((chrono_prev_bal + amount) - new_bal) <= 1.0:
                    direction = "CREDIT"

        if direction is None:
            # Handle 2-column Debit/Credit table where 2nd column was 0.00 (no Balance)
            if new_bal == 0.0:
                direction = "CREDIT" if credit_before_debit else "DEBIT"
            else:
                direction = _infer_fallback_pdf_direction(item["rest"], item["payee"])

        payee = item["payee"]
        channel = item["channel"]
        tx_date = item["tx_date"]
        tx_ts = item["tx_ts"]

        tx_type: TxType = direction
        target_account: Optional[str] = None
        suggested_category: Optional[str] = None
        suggested_bucket: Optional[CostBucket] = (
            "INFLOW" if direction == "CREDIT" else None
        )

        if is_hmb and direction == "DEBIT":
            (
                tx_type,
                target_account,
                suggested_category,
                transfer_bucket,
                channel,
            ) = classify_hmb_transfer(payee, payee, channel)
            if transfer_bucket is not None:
                suggested_bucket = transfer_bucket
        elif direction == "DEBIT" and _TAX_FEE_DESC_RE.search(payee):
            suggested_category = "Bank & Tax Friction"
            suggested_bucket = "LEAKAGE"

        occ_key = (
            account_name.upper(),
            tx_date,
            f"{amount:.2f}",
            payee[:10].upper(),
        )
        occ_idx = occurrences.get(occ_key, 0)
        occurrences[occ_key] = occ_idx + 1

        msg_id = _build_pdf_message_id(account_name, tx_date, amount, payee, occ_idx)
        parsed_list.append(
            ParsedTransaction(
                message_id=msg_id,
                tx_date=tx_date,
                tx_timestamp=tx_ts,
                account_name=account_name,
                target_account=target_account,
                tx_type=tx_type,
                amount=amount,
                tax_fee_amount=0.0,
                payee=payee,
                source="PDF_IMPORT",
                payment_channel=channel,
                notes=item["notes"],
                suggested_category=suggested_category,
                suggested_bucket=suggested_bucket,
            )
        )

    return parsed_list


def parse_nayapay_pdf_text(text: str) -> list[ParsedTransaction]:
    """Parse extracted text from a NayaPay PDF E-Statement into ParsedTransactions."""
    return _parse_bank_pdf_text(text, account_name=WALLET_NAYAPAY, is_hmb=False)


def parse_hmb_pdf_text(text: str) -> list[ParsedTransaction]:
    """Parse extracted text from a HabibMetro PDF Statement into ParsedTransactions."""
    return _parse_bank_pdf_text(text, account_name=WALLET_HABIBMETRO, is_hmb=True)


def _decode_csv_text(content: bytes | str) -> str:
    """Decode raw CSV bytes or return string cleanly."""
    if isinstance(content, str):
        return content.lstrip("\ufeff")
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _normalize_col_name(col: str) -> str:
    """Normalize CSV column header by turning separators into spaces and lowercasing."""
    return " ".join(re.sub(r"[_\-/]+", " ", str(col).strip().lower()).split())


def _find_csv_header_row(text: str) -> int:
    """Locate the actual CSV table header row skipping bank metadata preamble lines."""
    lines = text.splitlines()
    date_pat = re.compile(r"\b(?:date|tx\s+date|timestamp)\b")
    money_pat = re.compile(
        r"\b(?:debit|credit|amount|withdrawal|withdrawals|deposit|deposits|dr|cr)\b"
    )
    for idx, line in enumerate(lines[:30]):
        try:
            cells = next(csv.reader([line]))
        except Exception:
            continue
        non_empty = [c.strip() for c in cells if c.strip()]
        if len(non_empty) < 3:
            continue
        # Reject key-value metadata preambles like "Date: 2026-11-30, Total Debit: 5000"
        if any(":" in c for c in non_empty):
            continue
        norm_cells = [_normalize_col_name(c) for c in non_empty]
        has_date = any(date_pat.search(c) for c in norm_cells)
        has_money = any(money_pat.search(c) for c in norm_cells)
        if has_date and has_money:
            return idx
    return 0


def _parse_csv_number_and_drcr(val: Any) -> tuple[float, Optional[TxType]]:
    """Parse CSV cell numeric value and optional trailing DR/CR indicator safely."""
    if val is None:
        return 0.0, None
    s = str(val).strip()
    if not s or s in ("-", "--", "N/A", "nan", "None"):
        return 0.0, None

    drcr_dir: Optional[TxType] = None
    m_drcr = re.search(r"(?i)(?<![A-Za-z])(DR|CR|DEBIT|CREDIT)\.?\s*$", s)
    if m_drcr:
        tag = m_drcr.group(1).upper()
        drcr_dir = "DEBIT" if tag in ("DR", "DEBIT") else "CREDIT"
        s = s[: m_drcr.start()].strip()

    negative = s.startswith("(") and s.endswith(")")
    s_clean = re.sub(r"(?i)/-|(?:PKR|Rs\.?|PK\s*Rs\.?)|[(),\s]", "", s)
    if not s_clean:
        return 0.0, drcr_dir
    try:
        num = float(s_clean)
    except ValueError:
        return 0.0, drcr_dir
    return (-abs(num) if negative else num), drcr_dir


def _clean_csv_number(val: Any) -> float:
    """Parse CSV cell numeric value safely, handling currency, commas, DR/CR, and '/-'."""
    num, _ = _parse_csv_number_and_drcr(val)
    return num


def _match_column(
    columns: list[str],
    candidates: tuple[str, ...],
    *,
    exclude_tokens: tuple[str, ...] = (),
    exclude_cols: set[str] | None = None,
) -> Optional[str]:
    """Match the first CSV column matching candidate aliases by token/word boundary."""
    excluded = exclude_cols or set()
    norm_entries: list[tuple[str, str, set[str]]] = []
    for col in columns:
        if col in excluded:
            continue
        col_norm = _normalize_col_name(col)
        col_tokens = set(col_norm.split())
        if exclude_tokens and any(tok in col_tokens for tok in exclude_tokens):
            continue
        norm_entries.append((col, col_norm, col_tokens))

    # Pass 1: exact normalized match
    for cand in candidates:
        cand_norm = _normalize_col_name(cand)
        for orig, col_norm, _ in norm_entries:
            if col_norm == cand_norm:
                return orig

    # Pass 2: word/token-boundary match
    for cand in candidates:
        cand_norm = _normalize_col_name(cand)
        pat = re.compile(rf"(?<![a-z0-9]){re.escape(cand_norm)}(?![a-z0-9])")
        for orig, col_norm, _ in norm_entries:
            if pat.search(col_norm):
                return orig

    return None


def _read_csv_rows_resilient(text: str, header_idx: int) -> tuple[list[str], list[dict[str, str]]]:
    """Parse CSV table starting at header_idx while tolerating ragged summary footers."""
    lines = text.splitlines()
    if header_idx >= len(lines):
        return [], []
    reader = csv.reader(lines[header_idx:])

    cols: list[str] = []
    for row in reader:
        if any(cell.strip() for cell in row):
            cols = [cell.strip() for cell in row]
            break
    if not cols:
        return [], []

    num_cols = len(cols)
    rows: list[dict[str, str]] = []
    for raw_row in reader:
        if not raw_row or not any(cell.strip() for cell in raw_row):
            continue
        if len(raw_row) > num_cols:
            # Allow harmless trailing empty commas, skip genuinely ragged footer rows
            if any(cell.strip() for cell in raw_row[num_cols:]):
                continue
            aligned = raw_row[:num_cols]
        elif len(raw_row) < num_cols:
            aligned = raw_row + [""] * (num_cols - len(raw_row))
        else:
            aligned = raw_row
        if len(rows) >= _MAX_CSV_ROWS:
            raise ValueError(
                f"Uploaded CSV exceeds the maximum supported limit of {_MAX_CSV_ROWS:,} rows."
            )
        rows.append({cols[i]: aligned[i] for i in range(num_cols)})

    return cols, rows


def parse_statement_csv(
    content: bytes | str,
    account_hint: Optional[str] = None,
) -> list[ParsedTransaction]:
    """Parse a HabibMetro, NayaPay, or exported Treasury CSV statement into ParsedTransactions."""
    if isinstance(content, (bytes, bytearray)) and len(content) > _MAX_STATEMENT_BYTES:
        raise ValueError("Uploaded CSV exceeds the maximum allowed size of 15 MB.")
    if isinstance(content, str) and len(content.encode("utf-8", errors="ignore")) > _MAX_STATEMENT_BYTES:
        raise ValueError("Uploaded CSV exceeds the maximum allowed size of 15 MB.")
    text = _decode_csv_text(content)
    if not text.strip():
        return []

    header_idx = _find_csv_header_row(text)
    cols, row_dicts = _read_csv_rows_resilient(text, header_idx)
    if not cols or not row_dicts:
        return []

    date_col = _match_column(
        cols,
        (
            "tx_date",
            "transaction date",
            "trans date",
            "posting date",
            "value date",
            "date & time",
            "datetime",
            "date",
        ),
    )
    if not date_col:
        return []

    time_col = _match_column(cols, ("tx_time", "transaction time", "time"))
    ts_col = _match_column(cols, ("tx_timestamp", "timestamp"))
    desc_col = _match_column(
        cols,
        (
            "description",
            "particulars",
            "narration",
            "details",
            "payee",
            "merchant",
            "beneficiary",
            "title",
            "memo",
            "remarks",
        ),
    )
    type_col = _match_column(
        cols, ("tx_type", "transaction type", "dr/cr", "drcr", "type")
    )
    drcr_excluded = {type_col} if type_col else set()
    debit_col = _match_column(
        cols,
        ("debit amount", "debit", "withdrawal", "withdrawals", "outflow", "dr"),
        exclude_cols=drcr_excluded,
    )
    credit_col = _match_column(
        cols,
        ("credit amount", "credit", "deposit", "deposits", "inflow", "cr"),
        exclude_cols=drcr_excluded,
    )
    tax_col = _match_column(
        cols,
        ("tax_fee_amount", "tax_fee", "wht", "fx fee", "tax", "fee", "charges"),
        exclude_tokens=("id", "no", "number", "ref", "reference", "title", "code", "invoice"),
    )
    claimed_numeric = {c for c in (debit_col, credit_col, tax_col) if c}
    amount_col = _match_column(
        cols,
        ("transaction amount", "tx_amount", "pkr amount", "amount", "value"),
        exclude_cols=claimed_numeric,
    )
    channel_col = _match_column(cols, ("payment_channel", "channel", "mode"))
    acct_col = _match_column(
        cols,
        ("account_name", "account", "wallet"),
        exclude_tokens=(
            "id",
            "no",
            "number",
            "ref",
            "reference",
            "title",
            "target",
            "destination",
        ),
    )
    target_col = _match_column(
        cols,
        ("target_account", "target account", "destination_account", "destination wallet"),
    )
    mid_col = _match_column(cols, ("message_id",))
    alias_col = _match_column(cols, ("merchant_alias", "alias"))
    category_col = _match_column(cols, ("category",))
    bucket_col = _match_column(cols, ("cost_bucket", "bucket"))
    notes_col = _match_column(cols, ("notes", "note"))
    subject_col = _match_column(cols, ("raw_subject", "subject"))

    default_account = _normalize_account_name(account_hint) or _detect_statement_wallet(text)
    is_exported_backup = bool(type_col and target_col)

    occurrences: dict[tuple[str, str, str, str], int] = {}
    results: list[ParsedTransaction] = []

    for row in row_dicts:
        raw_date = str(row.get(date_col, "") or "").strip()
        if not raw_date or raw_date.lower() in ("nan", "none"):
            continue
        if _SUMMARY_SKIP_RE.search(raw_date):
            continue

        raw_ts = str(row.get(ts_col, "") or "").strip() if ts_col else ""
        if raw_ts.lower() in ("nan", "none"):
            raw_ts = ""
        raw_time = str(row.get(time_col, "") or "").strip() if time_col else ""
        if raw_time.lower() in ("nan", "none"):
            raw_time = ""

        if raw_ts and re.search(r"\d{1,2}:\d{2}", raw_ts):
            dt_input = raw_ts
        elif raw_time:
            dt_input = f"{raw_date} {raw_time}".strip()
        else:
            dt_input = raw_date

        if not re.search(r"\d", dt_input):
            continue
        try:
            tx_date, tx_ts = normalize_datetime_str(dt_input)
        except Exception:
            continue

        payee = (
            _strip_csv_formula_guard(str(row.get(desc_col, "") or "").strip())
            if desc_col
            else "Statement Transaction"
        )
        if not payee or payee.lower() in ("nan", "none"):
            payee = "Statement Transaction"
        if _SUMMARY_SKIP_RE.search(payee):
            continue

        row_acct = (
            _normalize_account_name(row.get(acct_col))
            if acct_col and not account_hint
            else None
        )
        account_name = row_acct if row_acct in _CANONICAL_WALLETS else default_account

        debit_val = abs(_clean_csv_number(row.get(debit_col))) if debit_col else 0.0
        credit_val = abs(_clean_csv_number(row.get(credit_col))) if credit_col else 0.0
        tax_fee = abs(_clean_csv_number(row.get(tax_col))) if tax_col else 0.0

        type_hint = (
            str(row.get(type_col, "") or "").strip().lower() if type_col else ""
        )
        explicit_tx_type: Optional[TxType] = None
        if type_hint == "transfer_internal":
            explicit_tx_type = "TRANSFER_INTERNAL"
        elif type_hint == "transfer_mmf":
            explicit_tx_type = "TRANSFER_MMF"
        elif type_hint == "credit":
            explicit_tx_type = "CREDIT"
        elif type_hint == "debit":
            explicit_tx_type = "DEBIT"

        direction: TxType
        amount: float
        if debit_val > 0.0:
            direction = "DEBIT"
            amount = round(debit_val, 2)
        elif credit_val > 0.0:
            direction = "CREDIT"
            amount = round(credit_val, 2)
        elif amount_col:
            raw_amt, amt_drcr = _parse_csv_number_and_drcr(row.get(amount_col))
            if raw_amt == 0.0:
                continue
            if explicit_tx_type == "CREDIT":
                direction = "CREDIT"
            elif explicit_tx_type in ("DEBIT", "TRANSFER_INTERNAL", "TRANSFER_MMF"):
                direction = "DEBIT"
            elif raw_amt < 0:
                direction = "DEBIT"
            elif amt_drcr is not None:
                direction = amt_drcr
            elif any(k in type_hint for k in ("credit", "cr", "deposit", "inflow")):
                direction = "CREDIT"
            elif any(k in type_hint for k in ("debit", "dr", "withdrawal", "outflow")):
                direction = "DEBIT"
            elif _CREDIT_DESC_RE.search(payee) and not _DEBIT_DESC_RE.search(payee):
                direction = "CREDIT"
            else:
                direction = "DEBIT"
            amount = round(abs(raw_amt), 2)
        else:
            continue

        if amount <= 0.0:
            continue

        channel = (
            str(row.get(channel_col, "") or "").strip() if channel_col else ""
        )
        if channel.lower() in ("", "nan", "none"):
            channel = _detect_channel_from_text(payee) or ""

        raw_target = str(row.get(target_col, "") or "").strip() if target_col else ""
        norm_target = _normalize_account_name(raw_target)

        tx_type: TxType = direction
        target_account: Optional[str] = None
        suggested_category: Optional[str] = (
            _strip_csv_formula_guard(str(row.get(category_col, "") or "").strip()) or None
            if category_col
            else None
        )
        raw_bucket = (
            str(row.get(bucket_col, "") or "").strip().upper()
            if bucket_col
            else ""
        )
        suggested_bucket: Optional[CostBucket] = (
            raw_bucket  # type: ignore[assignment]
            if raw_bucket in _VALID_COST_BUCKETS
            else ("INFLOW" if direction == "CREDIT" else None)
        )

        if explicit_tx_type == "TRANSFER_INTERNAL":
            resolved_target = (
                norm_target
                if (norm_target in _CANONICAL_WALLETS and norm_target != account_name)
                else None
            )
            if resolved_target is None and account_name == WALLET_HABIBMETRO:
                _, inferred_target, inferred_cat, _, resolved_channel = classify_hmb_transfer(
                    payee, payee, channel or None
                )
                if inferred_target and inferred_target != account_name:
                    resolved_target = inferred_target
                    suggested_category = suggested_category or inferred_cat
                    channel = resolved_channel or channel
            if resolved_target is not None:
                tx_type = "TRANSFER_INTERNAL"
                target_account = resolved_target
                suggested_bucket = "IGNORE"
        elif explicit_tx_type == "TRANSFER_MMF" and account_name != WALLET_MMF:
            tx_type = "TRANSFER_MMF"
            target_account = WALLET_MMF
            suggested_category = suggested_category or "Money Market Fund"
            suggested_bucket = "INVESTMENT"
        elif not is_exported_backup and account_name == WALLET_HABIBMETRO and direction == "DEBIT":
            (
                tx_type,
                target_account,
                inferred_cat,
                transfer_bucket,
                resolved_channel,
            ) = classify_hmb_transfer(payee, payee, channel or None)
            channel = resolved_channel or channel
            if inferred_cat and not suggested_category:
                suggested_category = inferred_cat
            if transfer_bucket is not None:
                suggested_bucket = transfer_bucket
        elif not is_exported_backup and direction == "DEBIT" and _TAX_FEE_DESC_RE.search(payee):
            suggested_category = suggested_category or "Bank & Tax Friction"
            suggested_bucket = "LEAKAGE"

        explicit_mid = str(row.get(mid_col, "") or "").strip() if mid_col else ""
        if explicit_mid and explicit_mid.lower() not in ("nan", "none"):
            msg_id = explicit_mid
        else:
            occ_key = (
                account_name.upper(),
                tx_date,
                f"{amount:.2f}",
                payee[:10].upper(),
            )
            occ_idx = occurrences.get(occ_key, 0)
            occurrences[occ_key] = occ_idx + 1
            msg_id = _build_pdf_message_id(account_name, tx_date, amount, payee, occ_idx)

        merchant_alias = (
            _strip_csv_formula_guard(str(row.get(alias_col, "") or "").strip()) or None
            if alias_col
            else None
        )
        notes = (
            _strip_csv_formula_guard(str(row.get(notes_col, "") or "").strip()) or None
            if notes_col
            else None
        )
        raw_subject = (
            _strip_csv_formula_guard(str(row.get(subject_col, "") or "").strip()) or None
            if subject_col
            else None
        )
        source: SourceChannel = "CSV_IMPORT"

        results.append(
            ParsedTransaction(
                message_id=msg_id,
                tx_date=tx_date,
                tx_timestamp=tx_ts,
                account_name=account_name,
                target_account=target_account,
                tx_type=tx_type,
                amount=amount,
                tax_fee_amount=round(tax_fee, 2),
                payee=payee[:120],
                merchant_alias=merchant_alias,
                source=source,
                payment_channel=channel or None,
                raw_subject=raw_subject,
                notes=notes,
                suggested_category=suggested_category,
                suggested_bucket=suggested_bucket,
            )
        )

    return results


def _read_input_bytes(
    file_obj_or_bytes: bytes | bytearray | BinaryIO,
    max_bytes: int = _MAX_STATEMENT_BYTES,
) -> bytes:
    """Read raw bytes safely from bytes or a file-like BinaryIO stream with size cap."""
    if isinstance(file_obj_or_bytes, (bytes, bytearray)):
        if len(file_obj_or_bytes) > max_bytes:
            raise ValueError("Uploaded file exceeds the maximum allowed size of 15 MB.")
        return bytes(file_obj_or_bytes)
    if hasattr(file_obj_or_bytes, "seek"):
        try:
            file_obj_or_bytes.seek(0)
        except Exception:
            pass
    data = file_obj_or_bytes.read(max_bytes + 1)
    raw = bytes(data) if isinstance(data, bytearray) else data
    if len(raw) > max_bytes:
        raise ValueError("Uploaded file exceeds the maximum allowed size of 15 MB.")
    return raw


def import_statement_file(
    file_obj_or_bytes: bytes | BinaryIO,
    filename: str,
    account_hint: Optional[str] = None,
) -> list[ParsedTransaction]:
    """Import an uploaded NayaPay or HabibMetro PDF or CSV statement file."""
    raw_bytes = _read_input_bytes(file_obj_or_bytes, max_bytes=_MAX_STATEMENT_BYTES)
    safe_name = Path((filename or "").strip()).name
    fname_lower = safe_name.lower()
    norm_hint = _normalize_account_name(account_hint)

    if fname_lower.endswith(".csv"):
        if norm_hint is None:
            if any(k in fname_lower for k in ("nayapay", "naya_pay", "naya-pay")):
                norm_hint = WALLET_NAYAPAY
            elif any(k in fname_lower for k in ("habib", "hmb")):
                norm_hint = WALLET_HABIBMETRO
        return parse_statement_csv(raw_bytes, account_hint=norm_hint)

    if fname_lower.endswith(".pdf"):
        if not raw_bytes:
            return []
        reader = PdfReader(io.BytesIO(raw_bytes))
        if len(reader.pages) > _MAX_PDF_PAGES:
            raise ValueError(
                f"Uploaded PDF has {len(reader.pages)} pages, exceeding the {_MAX_PDF_PAGES}-page safety limit."
            )
        if getattr(reader, "is_encrypted", False):
            try:
                dec_res = reader.decrypt("")
            except Exception as exc:
                raise ValueError(
                    "Uploaded PDF is password-protected and could not be decrypted automatically."
                ) from exc
            if not dec_res:
                raise ValueError(
                    "Uploaded PDF is password-protected; please export an unlocked PDF or CSV and try again."
                )

        layout_pages: list[str] = []
        plain_pages: list[str] = []
        for page in reader.pages:
            plain_txt = ""
            try:
                plain_txt = page.extract_text() or ""
            except Exception:
                plain_txt = ""
            plain_pages.append(plain_txt)

            layout_txt = ""
            try:
                layout_txt = page.extract_text(extraction_mode="layout") or ""
            except Exception:
                layout_txt = plain_txt
            layout_pages.append(layout_txt or plain_txt)

        layout_full = _normalize_pdf_text("\n".join(layout_pages)).strip()
        plain_full = _normalize_pdf_text("\n".join(plain_pages)).strip()
        primary_text = layout_full or plain_full
        if not primary_text:
            raise ValueError(
                "Uploaded PDF contains no selectable text (it may be a scanned/image-only PDF); please export a text PDF or CSV from your banking app."
            )

        detected_wallet = norm_hint or _detect_statement_wallet(primary_text, fname_lower)
        parser_fn = (
            parse_nayapay_pdf_text
            if detected_wallet == WALLET_NAYAPAY
            else parse_hmb_pdf_text
        )
        parsed = parser_fn(primary_text)
        if not parsed and plain_full and plain_full != layout_full:
            plain_wallet = norm_hint or _detect_statement_wallet(plain_full, fname_lower)
            plain_parser = (
                parse_nayapay_pdf_text
                if plain_wallet == WALLET_NAYAPAY
                else parse_hmb_pdf_text
            )
            parsed = plain_parser(plain_full)
        return parsed

    raise ValueError(f"Unsupported statement format for '{filename}'; expected .pdf or .csv")

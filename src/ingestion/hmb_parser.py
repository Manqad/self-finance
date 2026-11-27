"""HabibMetro (Habib Metro E-Service) email parser and Outlook-forward cleaner."""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from bs4 import BeautifulSoup
from dateutil import parser as date_parser

from src.models import (
    WALLET_CASH,
    WALLET_HABIBMETRO,
    WALLET_MMF,
    WALLET_NAYAPAY,
    CostBucket,
    ParsedTransaction,
    TxType,
)

_HTML_TAG_RE = re.compile(
    r"<(?:html|head|body|div|p|br|table|tbody|thead|tr|td|th|span|strong|b|em|i|"
    r"font|style|meta|title|ul|ol|li|h[1-6]|blockquote|hr)\b[^>]*>",
    re.IGNORECASE,
)

_PKT_TZ = timezone(timedelta(hours=5))

_DISCLAIMER_SENT_RE = re.compile(
    r"(?i)\b(?:(?:will\s+)?never\s+(?:asks?|disclose|reveal|give|provide|shares?|calls?|requests?)|"
    r"do(?:es)?\s+not\s+(?:asks?|disclose|share)|don'?t\s+(?:share|ask|disclose)|doesn'?t\s+ask|"
    r"keep\s+[^.\n]{0,40}\bconfidential\b|protect\s+your\s+(?:otp|pin|passcode)|beware\s+of)\b"
)
_SENTENCE_SPLIT_RE = re.compile(
    r"(?<!\be\.g)(?<!\bi\.e)(?<!\bRs)(?<=[.!?])\s+",
    re.IGNORECASE,
)

_IGNORED_OTP_RE = re.compile(
    r"(?i)\b(?:otp|one[\s\-]+time[\s\-]+password|verification\s+code|passcode|"
    r"login\s+(?:attempt|alert)|security\s+alert|mpin\s+(?:reset|change)|password\s+reset)\b"
)
_IGNORED_PROMO_RE = re.compile(
    r"(?i)(?:\bupgrade\s+your\s+[^.\n]{0,40}\blimit\b|"
    r"\blimit\s+(?:up\s+to|is\s+now|has\s+been\s+increased|used)\b|"
    r"\b(?:spend|pay)\s+(?:rs\.?|pkr)\s*[0-9,.]+\s+and\s+get\b|"
    r"\b(?:cashback|discount)\s+offer\b|\bschedule\s+of\s+charges\b|"
    r"\be-?statement\s+(?:is\s+ready|for\s+the\s+month)\b)"
)
_IGNORED_STATUS_RE = re.compile(
    r"(?i)\b(declined|failed|unsuccessful|insufficient|rejected|cancell?ed|expired|timed?\s*out)\b"
)
_CONFIRMED_TX_OR_REFUND_RE = re.compile(
    r"(?i)\b(?:has\s+been\s+debited|has\s+been\s+credited|was\s+successful|"
    r"refund(?:ed)?|revers(?:al|ed))\b"
)
_EXPLICIT_TX_FAILURE_RE = re.compile(
    r"(?i)\b(?:transaction\s+(?:(?:was|has\s+been|is)\s+)?(?:declined|failed|unsuccessful|rejected|cancell?ed)|"
    r"payment\s+(?:(?:was|has\s+been|is)\s+)?(?:declined|failed|unsuccessful|rejected|cancell?ed)|"
    r"insufficient\s+funds|exceeds?\s+[^.\n]{0,30}balance|"
    r"(?:was|is)\s+(?:declined|failed|unsuccessful|rejected))\b"
)
_REFUND_CREDIT_CONFIRMED_RE = re.compile(
    r"(?i)(?:\b(?:has\s+been\s+)?credited\b[^.\n]{0,60}\b(?:refund(?:ed)?|reversal|reversed)\b"
    r"|\b(?:refund(?:ed)?|reversal|reversed)\b[^.\n]{0,60}\b(?:has\s+been\s+)?credited\b"
    r"|\brefund(?:ed)?\s+(?:of|processed|issued|received|credited)\b"
    r"|\breversal\s+of\b"
    r"|\b(?:credited\s+as\s+(?:a\s+)?refund|reversed\s+(?:to|into)\s+your\s+account)\b)"
)

_HMB_TRIGGER_RE = re.compile(
    r"(?i)(habib\s*metro|habibmetro|eservice@habibmetro)"
)
_HMB_INNER_FROM_RE = re.compile(
    r"(?im)^from\s*:\s*[^\n]*(?:(?:@|<[^>\n]+>)[^\n]*(?:habib\s*metro|habibmetro)|"
    r"(?:habib\s*metro|habibmetro)[^\n]*(?:@|<[^>\n]+>)|eservice@habibmetro)"
)
_NAYAPAY_SENDER_RE = re.compile(r"(?i)nayapay\.com|\bnayapay\b")
_NAYAPAY_ORIGIN_RE = re.compile(
    r"(?im)(?:^from\s*:\s*[^\n]*(?:service@nayapay|nayapay\.com|naya\s*pay[^\n]*(?:@|<[^>\n]+>))|"
    r"\b(?:via\s+nayapay|using\s+(?:your\s+)?nayapay|"
    r"nayapay(?:\s+[a-z]+){0,3}\s+receipt)\b)"
)

_OUTLOOK_DIVIDER_RE = re.compile(
    r"^\s*(?:-{2,}\s*(?:Original Message|Forwarded message)\s*-{2,}|_{5,})\s*$",
    re.IGNORECASE,
)
_EMAIL_HEADER_LINE_RE = re.compile(
    r"^\s*(From|Sent|Date|To|Cc|Bcc|Subject|Reply-To|Importance|Sensitivity|Attachments?)\s*:\s*(.*)$",
    re.IGNORECASE,
)
_EMAIL_SIG_RE = re.compile(r"@|<[^>\n]+>")
_ISO_DATE_PREFIX_RE = re.compile(r"^\s*\d{4}[-/]\d{1,2}[-/]\d{1,2}(?![0-9])")

_BAL_QUALIFIER = (
    r"(?:account|acct\.?|a/c|avail\.?|available|avl\.?|closing|opening|"
    r"current|ledger|remaining|new|net|total|updated|wallet)"
)
_BALANCE_LINE_RE = re.compile(
    rf"(?i)^\s*(?:{_BAL_QUALIFIER}\s+)?bal(?:ance)?\.?\s*[:\-]"
)
_BALANCE_PHRASE_RE = re.compile(
    rf"(?:\b(?:{_BAL_QUALIFIER})\s+|\byour\s+)"
    rf"bal(?:ance)?\.?(?:\s+after\s+transaction)?\s*(?:is|of|:|-)?\s*(?:PKR|Rs\.?|PK\s*Rs\.?)?\s*[0-9][0-9,]*(?:\.[0-9]{{1,2}})?|"
    r"\bbalance\s+after\s+transaction\s*(?:is|of|:|-)?\s*(?:PKR|Rs\.?|PK\s*Rs\.?)?\s*[0-9][0-9,]*(?:\.[0-9]{1,2})?",
    re.IGNORECASE,
)

_LABELED_AMOUNT_RE = re.compile(
    r"(?im)^\s*(?:transaction\s+amount|tx\s+amount|debited\s+amount|credited\s+amount|"
    r"transfer\s+amount|withdrawal\s+amount|bill\s+amount|amount)\b\s*[:\-]\s*"
    r"(?:PKR|Rs\.?|PK\s*Rs\.?)?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)"
)
_CURRENCY_BEFORE_RE = re.compile(
    r"(?i)(?:PKR|Rs\.?|PK\s*Rs\.?)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)"
)
_CURRENCY_AFTER_RE = re.compile(
    r"(?i)\b([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?:PKR|Rs\.?)\b"
)
_FEE_LINE_RE = re.compile(
    r"(?im)^(?:[^\n:]{0,40}\b(?:fee|charges|fed|wht|tax|commission|surcharge)\b"
    r"(?:\s*\((?!\s*(?:ntn|strn|id|no|ref|code)\s*\))[^)\n]{1,20}\))?\s*[:\-]\s*"
    r"(?:PKR|Rs\.?|PK\s*Rs\.?)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?))"
)
_FEE_LINE_STRIP_RE = re.compile(
    r"(?im)^(?:[^\n:]{0,40}\b(?:fee|charges|fed|wht|tax|commission|surcharge)\b"
    r"(?:\s*\([^)\n]{1,20}\))?\s*[:\-].*|"
    r"[^\n]*(?:schedule\s+of\s+charges|\bmay\s+apply\b|\bas\s+applicable\b)[^\n]*)$"
)
_HMB_FEE_TAX_RE = re.compile(
    r"(?i)(?:\bwht\b|\bwithholding(?:\s+tax)?\b|\bfed\b|\bfederal\s+excise\b|\bfbr\s+tax\b|"
    r"\b(?:bank|service|sms(?:\s+alert)?|annual|card|atm|ibft|fx|foreign|surcharge|processing)\s+(?:fee|charges)\b)"
)
_HMB_FEE_NARRATIVE_RE = re.compile(
    r"(?i)\b(?:debited(?:\s+(?:with|for|from|by))?\s+(?:[^\n.]|\.(?=\d)){0,90}"
    r"\b(?:bank\s+charges|service\s+charges|sms\s+fee|annual\s+fee|card\s+fee|wht|withholding\s+tax|fed|excise)\b|"
    r"(?:annual|card)\s+fee|sms\s+(?:alert\s+)?(?:fee|charges)|"
    r"(?:wht|withholding\s+tax|fed|excise)\s+(?:on|for|deducted))\b"
)

_CREDIT_STRONG_RE = re.compile(
    r"(?i)\b(?:has\s+been\s+credited|credited\s+(?:to|with)|credit\s+alert|"
    r"amount\s+received|money\s+received|received\s+(?:from|in|via)|"
    r"inward\s+(?:ibft|raast|remittance|transfer)|deposited|deposit\s+alert|"
    r"salary\s+credited|refund(?:ed)?|revers(?:al|ed))\b"
)
_DEBIT_STRONG_RE = re.compile(
    r"(?i)\b(?:has\s+been\s+debited|debited\s+(?:from|with|by)|debit\s+alert|"
    r"cash\s+withdrawal|withdrawn|withdrawal|was\s+used\s+for|pos\s+transaction|"
    r"bill\s+payment|funds?\s+transfer\s+to|transferred\s+to|paid\s+to|sent\s+to|"
    r"purchase\s+at)\b"
)
_CREDIT_WEAK_RE = re.compile(r"(?i)\bcredit(?!\s+card)\b|\bcredited\b|\bdeposit\b")
_DEBIT_WEAK_RE = re.compile(r"(?i)\bdebit(?!\s+card)\b|\bdebited\b|\bpayment\b|\bpurchased?\b")

_MMF_RE = re.compile(
    r"(?i)(?:"
    r"\bal[\s\-]*meezan\b"
    r"|\bmeezan\s+(?:investment|amc|rozana(?:\s+amdani)?|cash\s+fund|mutual|islamic\s+fund)\b"
    r"|\bmraf\b(?!\s+(?:traders?|enterprises?|corp|ltd|limited|co\b|store|mart))"
    r"|\bmcf\b(?!\s+(?:foundation|charity|trust|welfare|society|traders?))"
    r"|\bnbp\s*funds?\b"
    r"|\bubl\s*funds?\b"
    r"|\bal[\s\-]*ameen(?:\s+funds?)?\b"
    r"|\balfalah\s+(?:ghp|asset|investment|fund|mmf)\b"
    r"|\bmoney\s*market\s*fund\b"
    r")"
)
_CASH_WITHDRAWAL_RE = re.compile(
    r"(?i)\b(?:cash\s+withdrawal|atm\s+withdrawal|atm\s+cash)\b"
)
_CARD_POS_CONTEXT_RE = re.compile(
    r"(?i)(?:\bpos\b|\bpurchase\s+at\b|\bwas\s+used\s+for\b.{0,40}\bat\b|"
    r"(?:debit\s+card|mastercard|visa|paypak)\b(?![^\n]{0,80}\bat\s+[A-Za-z0-9 *&'.\-/]{0,40}\batm\b))"
)
_ATM_RE = re.compile(
    r"(?i)\b(?:cash\s+withdrawal|atm\s+withdrawal|atm(?!\s*(?:/\s*debit)?\s*card\b))\b"
)
_NAYAPAY_RE = re.compile(r"(?i)\bnaya\s*pay\b|PK\d{2}NAYA[A-Z0-9]+")

_DATE_TIME_LABEL_RE = re.compile(
    r"(?i)^(?:transaction\s+)?date\s*(?:&|and|/)\s*time\s*[:\-]\s*(.+)$"
)
_DATE_LABEL_RE = re.compile(
    r"(?i)^(?:transaction\s+|tx\s+|value\s+|posting\s+)?date\s*[:\-]\s*(.+)$"
)
_TIME_LABEL_RE = re.compile(
    r"(?i)^(?:transaction\s+|tx\s+)?time\s*[:\-]\s*(.+)$"
)
_INLINE_ON_DATE_RE = re.compile(
    r"(?i)\bon\s+(\d{1,2}[-/ ](?:[A-Za-z]{3,9}|\d{1,2})[-/ ]\d{2,4}"
    r"(?:\s+(?:at\s+)?\d{1,2}:\d{2}(?::\d{2})?(?:\s*[APap][Mm])?)?|"
    r"[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}"
    r"(?:\s+(?:at\s+)?\d{1,2}:\d{2}(?::\d{2})?(?:\s*[APap][Mm])?)?|"
    r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2})?)"
)

_DEBIT_PAYEE_LABELS = (
    "Merchant Name",
    "Merchant",
    "To Account Title",
    "Beneficiary Name",
    "Beneficiary",
    "Paid To",
    "Receiver Name",
    "Receiver",
    "Transfer To",
    "Biller Name",
    "Biller",
    "Company",
    "To",
    "Description",
    "Narration",
    "Particulars",
    "Details",
)
_CREDIT_PAYEE_LABELS = (
    "From Account Title",
    "Sender Name",
    "Sender",
    "Remitter",
    "Received From",
    "From",
    "Description",
    "Narration",
    "Particulars",
    "Details",
)


def parse_amount_value(raw_num: str) -> Optional[float]:
    """Convert a comma-formatted numeric string to a positive float if valid."""
    try:
        val = float(raw_num.replace(",", "").strip())
    except (ValueError, AttributeError):
        return None
    return round(val, 2) if val > 0.0 else None


def normalize_datetime_str(
    raw_dt: str,
    fallback_date: str = "",
    *,
    allow_now_fallback: bool = True,
    dayfirst_hint: Optional[bool] = None,
) -> tuple[str, str]:
    """Parse date/time string into PKT (YYYY-MM-DD, YYYY-MM-DD HH:MM:SS), converting aware TZ offsets."""
    for candidate in (raw_dt, fallback_date):
        text = (candidate or "").strip()
        if not text:
            continue
        cleaned = re.sub(r"\bPKT\b", "+0500", text, flags=re.IGNORECASE).strip()
        cleaned = re.sub(
            r"\(?\s*(?:GMT|UTC)\s*([+-]\d{1,2})(?::?(\d{2}))?\s*\)?",
            lambda m: f"{int(m.group(1)):+03d}{m.group(2) or '00'}",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"\s+at\s+", " ", cleaned, flags=re.IGNORECASE)
        dayfirst = (
            dayfirst_hint
            if dayfirst_hint is not None
            else (not bool(_ISO_DATE_PREFIX_RE.match(cleaned)))
        )
        try:
            dt = date_parser.parse(cleaned, dayfirst=dayfirst, fuzzy=allow_now_fallback)
            if dt.tzinfo is not None:
                dt = dt.astimezone(_PKT_TZ).replace(tzinfo=None)
            has_time = bool(re.search(r"\d{1,2}:\d{2}", cleaned))
            date_str = dt.strftime("%Y-%m-%d")
            ts_str = (
                dt.strftime("%Y-%m-%d %H:%M:%S")
                if has_time
                else f"{date_str} 00:00:00"
            )
            return date_str, ts_str
        except (ValueError, OverflowError, TypeError):
            continue

    if not allow_now_fallback:
        raise ValueError(f"Unparseable date/timestamp string: {raw_dt!r}")
    now = datetime.now(_PKT_TZ).replace(tzinfo=None)
    date_str = now.strftime("%Y-%m-%d")
    return date_str, now.strftime("%Y-%m-%d %H:%M:%S")


def strip_security_disclaimers(text: str) -> str:
    """Remove boilerplate bank security disclaimer sentences/lines before OTP inspection."""
    if not text:
        return ""
    kept_lines: list[str] = []
    for raw_line in text.splitlines():
        sentences = _SENTENCE_SPLIT_RE.split(raw_line)
        kept_sents = [
            sent for sent in sentences if sent and not _DISCLAIMER_SENT_RE.search(sent)
        ]
        joined = " ".join(s.strip() for s in kept_sents if s.strip())
        if joined:
            kept_lines.append(joined)
    return "\n".join(kept_lines)


def is_ignored_bank_alert(combined_text: str) -> bool:
    """Return True for OTP/login/promo/limit alerts or non-refund declined/failed alerts."""
    stripped = strip_security_disclaimers(combined_text)
    if _IGNORED_OTP_RE.search(stripped):
        return True
    if _IGNORED_PROMO_RE.search(stripped) and not _CONFIRMED_TX_OR_REFUND_RE.search(stripped):
        return True
    if _IGNORED_STATUS_RE.search(stripped):
        if _REFUND_CREDIT_CONFIRMED_RE.search(stripped):
            return False
        if _CONFIRMED_TX_OR_REFUND_RE.search(stripped) and not _EXPLICIT_TX_FAILURE_RE.search(stripped):
            return False
        return True
    return False


def html_to_structured_text(raw_html_or_text: str) -> str:
    """Convert HTML or plain text into clean line-structured text without stripping email envelopes."""
    if not raw_html_or_text:
        return ""

    text = html.unescape(raw_html_or_text)
    text = text.replace("\xa0", " ").replace("\u200b", "").replace("\ufeff", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    if _HTML_TAG_RE.search(text):
        try:
            soup = BeautifulSoup(text, "lxml")
        except Exception:
            soup = BeautifulSoup(text, "html.parser")

        for tag in soup(["script", "style", "head", "title", "noscript"]):
            tag.decompose()

        for br in soup.find_all("br"):
            br.replace_with("\n")

        # Format leaf key-value table rows cleanly onto a single line when simple
        for tr in soup.find_all("tr"):
            if tr.find("table") is not None:
                continue
            cells = tr.find_all(["td", "th"], recursive=False)
            if not cells:
                cells = tr.find_all(["td", "th"])
            if len(cells) >= 2:
                parts = [
                    " ".join(c.get_text(" ", strip=True).split())
                    for c in cells
                ]
                parts = [p for p in parts if p]
                if len(parts) == 2 and not parts[0].endswith("-"):
                    if parts[0].endswith(":"):
                        tr.replace_with(f"\n{parts[0]} {parts[1]}\n")
                    else:
                        tr.replace_with(f"\n{parts[0]}: {parts[1]}\n")
                elif parts:
                    tr.replace_with("\n" + " ".join(parts) + "\n")
                continue
            tr.append("\n")

        # Format innermost 2-child flex rows (e.g., <div><span>Label</span><span>Value</span></div>)
        for container in reversed(soup.find_all(["div", "li"])):
            if container.parent is None:
                continue
            if container.find(["table", "tr"]) is not None:
                continue
            children = [
                ch
                for ch in container.find_all(recursive=False)
                if ch.name in ("span", "div", "p", "strong", "b", "label")
            ]
            if len(children) != 2:
                continue
            # Ensure we only collapse leaf key/value pairs, not multi-paragraph envelopes
            if any(ch.find(["table", "div", "p"]) for ch in children):
                continue
            if any("\n" in ch.get_text() for ch in children):
                continue
            raw_children_text = "".join(ch.get_text() for ch in children)
            whole_text = container.get_text()
            if len(whole_text.strip()) > len(raw_children_text.strip()) + 4:
                continue
            left = " ".join(children[0].get_text(" ", strip=True).split())
            right = " ".join(children[1].get_text(" ", strip=True).split())
            if (
                left
                and right
                and len(left) <= 50
                and len(right) <= 180
                and not left.startswith(("-", "_"))
                and ":" not in left.rstrip(":")
                and not left.endswith("-")
            ):
                if left.endswith(":"):
                    container.replace_with(f"\n{left} {right}\n")
                else:
                    container.replace_with(f"\n{left}: {right}\n")

        for block_tag in soup.find_all(
            ["p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "hr"]
        ):
            block_tag.append("\n")

        text = soup.get_text()
        text = html.unescape(text)
        text = text.replace("\xa0", " ").replace("\u200b", "")

    # Strip '>' email quote prefixes
    lines = [re.sub(r"^\s*>+\s?", "", ln) for ln in text.splitlines()]
    cleaned_lines: list[str] = []
    for ln in lines:
        norm = re.sub(r"[ \t]+", " ", ln).strip()
        if norm:
            cleaned_lines.append(norm)
        elif cleaned_lines and cleaned_lines[-1] != "":
            cleaned_lines.append("")

    return "\n".join(cleaned_lines).strip()


def _extract_outlook_forward_meta(structured_text: str) -> tuple[str, str, str]:
    """Extract (unwrapped_body, forwarded_sent_date, forwarded_subject) from Outlook forwards."""
    lines = structured_text.splitlines()
    if not lines:
        return "", "", ""

    forwarded_sent = ""
    forwarded_subject = ""
    skip_indices: set[int] = set()

    i = 0
    while i < len(lines):
        line = lines[i]
        if _OUTLOOK_DIVIDER_RE.match(line):
            skip_indices.add(i)
            i += 1
            # Consume contiguous Outlook header block immediately after divider
            block_end = i
            seen_in_block: set[str] = set()
            while block_end < len(lines):
                candidate = lines[block_end]
                if candidate == "":
                    block_end += 1
                    continue
                m = _EMAIL_HEADER_LINE_RE.match(candidate)
                if m:
                    hdr_name = m.group(1).lower()
                    if hdr_name in seen_in_block and hdr_name in ("from", "subject"):
                        break
                    seen_in_block.add(hdr_name)
                    hdr_val = m.group(2).strip()
                    if hdr_name in ("sent", "date") and hdr_val:
                        forwarded_sent = hdr_val
                    elif hdr_name == "subject" and hdr_val:
                        forwarded_subject = hdr_val
                    skip_indices.add(block_end)
                    block_end += 1
                else:
                    break
            i = block_end
            continue

        # Detect divider-less Outlook/email header blocks ONLY when they match real email signatures
        m = _EMAIL_HEADER_LINE_RE.match(line)
        if m and m.group(1).lower() == "from":
            j = i + 1
            seen_headers = {"from"}
            temp_indices = [i]
            temp_sent = ""
            temp_subj = ""
            has_email_sig = bool(_EMAIL_SIG_RE.search(m.group(2)))
            while j < len(lines):
                cand = lines[j]
                if cand == "":
                    j += 1
                    continue
                hm = _EMAIL_HEADER_LINE_RE.match(cand)
                if hm:
                    hname = hm.group(1).lower()
                    if hname in seen_headers and hname in ("from", "subject"):
                        break
                    hval = hm.group(2).strip()
                    seen_headers.add(hname)
                    temp_indices.append(j)
                    if _EMAIL_SIG_RE.search(hval):
                        has_email_sig = True
                    if hname in ("sent", "date") and hval:
                        temp_sent = hval
                    elif hname == "subject" and hval:
                        temp_subj = hval
                    j += 1
                else:
                    break
            if len(seen_headers) >= 2 and (has_email_sig or "sent" in seen_headers):
                skip_indices.update(temp_indices)
                if temp_sent:
                    forwarded_sent = temp_sent
                if temp_subj:
                    forwarded_subject = temp_subj
                i = j
                continue

        i += 1

    remaining = [ln for idx, ln in enumerate(lines) if idx not in skip_indices]
    while remaining and remaining[0] == "":
        remaining.pop(0)
    while remaining and remaining[-1] == "":
        remaining.pop()

    deduped: list[str] = []
    for ln in remaining:
        if ln == "" and (not deduped or deduped[-1] == ""):
            continue
        deduped.append(ln)

    return "\n".join(deduped), forwarded_sent, forwarded_subject


def clean_email_html(raw_html_or_text: str) -> str:
    """Strip HTML tags via BeautifulSoup, normalize whitespace, and unwrap Outlook headers."""
    structured = html_to_structured_text(raw_html_or_text)
    unwrapped, _, _ = _extract_outlook_forward_meta(structured)
    return unwrapped


def is_ignored_hmb_email(subject: str, body: str) -> bool:
    """Return True for OTP/verification/login alerts or non-refund declined/failed alerts."""
    cleaned_body = html_to_structured_text(body)
    combined = f"{subject or ''}\n{cleaned_body}"
    return is_ignored_bank_alert(combined)


def is_hmb_email(subject: str, sender: str, body: str) -> bool:
    """Detect HabibMetro / Habib Metro E-Service / HABIBMETRO transaction alerts."""
    sender_l = (sender or "").lower()
    subject_l = (subject or "").lower()
    if _NAYAPAY_SENDER_RE.search(sender_l) and not _HMB_TRIGGER_RE.search(sender_l):
        return False

    raw_text = html_to_structured_text(body)

    hmb_is_sender_or_inner_from = bool(
        _HMB_TRIGGER_RE.search(sender_l) or _HMB_INNER_FROM_RE.search(raw_text)
    )
    if not hmb_is_sender_or_inner_from:
        combined_probe = f"{subject or ''}\n{raw_text}"
        if _NAYAPAY_ORIGIN_RE.search(combined_probe):
            return False

    return bool(
        _HMB_TRIGGER_RE.search(sender_l)
        or _HMB_TRIGGER_RE.search(subject_l)
        or _HMB_TRIGGER_RE.search(raw_text)
    )


def _is_card_pos_purchase(text: str) -> bool:
    """Return True if text reflects a card POS/online purchase rather than cash withdrawal."""
    if _CASH_WITHDRAWAL_RE.search(text) and not re.search(
        r"(?i)\b(?:pos|purchase\s+at)\b", text
    ):
        return False
    if re.search(r"(?i)\batm\b(?!\s*(?:/\s*debit)?\s*card\b)", text) and not re.search(
        r"(?i)\b(?:pos|purchase)\b", text
    ):
        return False
    return bool(_CARD_POS_CONTEXT_RE.search(text))


def classify_hmb_transfer(
    payee: str,
    context_text: str,
    payment_channel: Optional[str] = None,
) -> tuple[TxType, Optional[str], Optional[str], Optional[CostBucket], Optional[str]]:
    """Classify HabibMetro DEBIT entries into fee leakage, NayaPay top-up, ATM Cash, or MMF."""
    payee_u = (payee or "").upper()
    combined = f"{payee or ''}\n{context_text or ''}"
    narrative_ctx = _FEE_LINE_STRIP_RE.sub(" ", context_text or "")

    # 1. Check fee/tax/charge transactions BEFORE ATM or NayaPay internal-transfer promotion
    if _HMB_FEE_TAX_RE.search(payee_u) or _HMB_FEE_NARRATIVE_RE.search(narrative_ctx):
        return (
            "DEBIT",
            None,
            "Bank & Tax Friction",
            "LEAKAGE",
            payment_channel,
        )

    # 2. HabibMetro -> NayaPay internal wallet top-up (require NayaPay in payee/IBFT-to line, not third-party bank name)
    if _NAYAPAY_RE.search(payee_u) or re.search(
        r"(?i)(?:to\s+account(?:\s+title)?|ibft\s+to|transfer\s+to)"
        r"[^\n]{0,60}\bnaya\s*pay\b|PK\d{2}NAYA[A-Z0-9]+",
        combined,
    ):
        return (
            "TRANSFER_INTERNAL",
            WALLET_NAYAPAY,
            "Wallet Top-Up",
            "IGNORE",
            payment_channel or "IBFT",
        )

    # 3. ATM Cash Withdrawal (excluding ATM/Debit Card POS purchases)
    card_pos = (payment_channel == "POS") or _is_card_pos_purchase(combined)
    if not card_pos and (
        payment_channel == "ATM"
        or _ATM_RE.search(payee_u)
        or _ATM_RE.search(context_text or "")
        or _CASH_WITHDRAWAL_RE.search(combined)
    ):
        return (
            "TRANSFER_INTERNAL",
            WALLET_CASH,
            "Cash Envelope",
            "IGNORE",
            "ATM",
        )

    # 4. Money Market Fund / AMC sweep
    if _MMF_RE.search(payee_u) or _MMF_RE.search(combined):
        return (
            "TRANSFER_MMF",
            WALLET_MMF,
            "Money Market Fund",
            "INVESTMENT",
            payment_channel or "IBFT",
        )

    return "DEBIT", None, None, None, payment_channel


def _extract_hmb_amount_and_fee(cleaned_body: str, subject: str) -> tuple[Optional[float], float]:
    """Extract primary transaction amount and optional bank fee while ignoring balance figures."""
    safe_lines: list[str] = []
    for ln in cleaned_body.splitlines():
        stripped_ln = _BALANCE_PHRASE_RE.sub(" ", ln).strip()
        if not stripped_ln:
            continue
        if _BALANCE_LINE_RE.search(stripped_ln) and not (
            _CURRENCY_BEFORE_RE.search(stripped_ln)
            or _CURRENCY_AFTER_RE.search(stripped_ln)
        ):
            continue
        safe_lines.append(stripped_ln)
    safe_body = "\n".join(safe_lines)
    safe_subject = _BALANCE_PHRASE_RE.sub(" ", subject or "")

    fee_amount = 0.0
    for m in _FEE_LINE_RE.finditer(safe_body):
        val = parse_amount_value(m.group(1))
        if val is not None:
            fee_amount += val

    # 1. Explicit labeled amount in body
    m_labeled = _LABELED_AMOUNT_RE.search(safe_body)
    if m_labeled:
        amt = parse_amount_value(m_labeled.group(1))
        if amt is not None:
            return amt, round(fee_amount, 2)

    # 2. Contextual debit/credit verb + currency
    m_verb = re.search(
        r"(?i)\b(?:debited|credited|withdrawn|withdrawal|paid|transfer(?:red)?|amount)\b"
        r"[^0-9\n]{0,30}(?:PKR|Rs\.?|PK\s*Rs\.?)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
        safe_body,
    )
    if m_verb:
        amt = parse_amount_value(m_verb.group(1))
        if amt is not None:
            return amt, round(fee_amount, 2)

    # 3. Currency before/after in body (excluding fee lines)
    body_no_fee = _FEE_LINE_RE.sub(" ", safe_body)
    for pattern in (_CURRENCY_BEFORE_RE, _CURRENCY_AFTER_RE):
        m = pattern.search(body_no_fee)
        if m:
            amt = parse_amount_value(m.group(1))
            if amt is not None:
                return amt, round(fee_amount, 2)

    # 4. Fallback to subject
    for pattern in (_CURRENCY_BEFORE_RE, _CURRENCY_AFTER_RE):
        m = pattern.search(safe_subject)
        if m:
            amt = parse_amount_value(m.group(1))
            if amt is not None:
                return amt, round(fee_amount, 2)

    # 5. Standalone fee/tax alert fallback
    if fee_amount > 0.0:
        return round(fee_amount, 2), 0.0

    return None, 0.0


def _detect_hmb_direction(subject: str, cleaned_body: str) -> TxType:
    """Determine DEBIT vs CREDIT from HMB subject and body cues."""
    combined = f"{subject or ''}\n{cleaned_body}"
    has_credit_strong = bool(_CREDIT_STRONG_RE.search(combined))
    has_debit_strong = bool(_DEBIT_STRONG_RE.search(combined))

    if has_credit_strong and not has_debit_strong:
        return "CREDIT"
    if has_debit_strong and not has_credit_strong:
        return "DEBIT"

    # Resolve ties by checking body verb first
    body_credit = bool(_CREDIT_STRONG_RE.search(cleaned_body))
    body_debit = bool(_DEBIT_STRONG_RE.search(cleaned_body))
    if body_credit and not body_debit:
        return "CREDIT"
    if body_debit and not body_credit:
        return "DEBIT"

    if _CREDIT_WEAK_RE.search(combined) and not _DEBIT_WEAK_RE.search(combined):
        return "CREDIT"
    return "DEBIT"


def _extract_hmb_datetime(
    cleaned_body: str, forwarded_sent: str, fallback_date: str
) -> tuple[str, str]:
    """Extract normalized PKT date/timestamp, preferring body fields then forwarded_sent then fallback_date."""
    date_part = ""
    time_part = ""
    fb_chain = forwarded_sent or fallback_date

    for line in cleaned_body.splitlines():
        m_dt = _DATE_TIME_LABEL_RE.match(line)
        if m_dt:
            return normalize_datetime_str(m_dt.group(1), fb_chain)
        if not date_part:
            m_d = _DATE_LABEL_RE.match(line)
            if m_d:
                date_part = m_d.group(1).strip()
        if not time_part:
            m_t = _TIME_LABEL_RE.match(line)
            if m_t:
                time_part = m_t.group(1).strip()

    if date_part:
        combined_dt = f"{date_part} {time_part}".strip() if time_part else date_part
        return normalize_datetime_str(combined_dt, fb_chain)

    m_inline = _INLINE_ON_DATE_RE.search(cleaned_body)
    if m_inline:
        return normalize_datetime_str(m_inline.group(1), fb_chain)

    return normalize_datetime_str(forwarded_sent, fallback_date)


def _extract_hmb_channel(subject: str, cleaned_body: str) -> Optional[str]:
    """Identify HabibMetro transaction payment channel."""
    combined = f"{subject or ''}\n{cleaned_body}"
    m_lbl = re.search(
        r"(?i)^(?:payment\s+channel|channel|transaction\s+type|tx\s+type|mode)\s*[:\-]\s*(.+)$",
        cleaned_body,
        re.MULTILINE,
    )
    probe = m_lbl.group(1) if m_lbl else combined

    if re.search(r"(?i)\braast\b", probe) or re.search(r"(?i)\braast\b", combined):
        return "Raast"
    if m_lbl:
        if re.search(r"(?i)\b(?:pos|debit\s+card|mastercard|visa|paypak)\b", probe):
            return "POS"
        if _ATM_RE.search(probe) or _CASH_WITHDRAWAL_RE.search(probe):
            return "ATM"
    if _is_card_pos_purchase(combined):
        return "POS"
    if _ATM_RE.search(combined) or _CASH_WITHDRAWAL_RE.search(combined):
        return "ATM"
    if re.search(r"(?i)\bibft\b|inter\s*bank\s*fund", combined):
        return "IBFT"
    if re.search(r"(?i)\bpos\b|debit\s+card|mastercard|visa|paypak", combined):
        return "POS"
    if re.search(r"(?i)bill\s*payment|utility\s+bill|1link\s+bill", combined):
        return "Bill Payment"
    if re.search(r"(?i)funds?\s*transfer", combined):
        return "Fund Transfer"
    return None


_HMB_OWN_ACCOUNT_SUFFIX_RE = re.compile(
    r"(?i)\s+from\s+(?:your\s+)?(?:hmb\b|habib\s*metro\b|a/c\b|account\b).*$"
)


def _strip_hmb_own_account_suffix(value: str) -> str:
    """Remove trailing 'from your HMB A/C *xxxx' phrases from extracted payees."""
    return _HMB_OWN_ACCOUNT_SUFFIX_RE.sub("", value or "").strip()


def _extract_hmb_payee(
    cleaned_body: str, subject: str, direction: TxType, channel: Optional[str]
) -> str:
    """Extract counterparty/payee name from structured fields or narrative text."""
    labels = _CREDIT_PAYEE_LABELS if direction == "CREDIT" else _DEBIT_PAYEE_LABELS
    for label in labels:
        pattern = re.compile(
            rf"(?im)^{re.escape(label)}\s*[:\-]\s*(.+)$"
        )
        m = pattern.search(cleaned_body)
        if m:
            val = _strip_hmb_own_account_suffix(m.group(1).strip()) or m.group(1).strip()
            if val and not re.fullmatch(r"[*X\d\-]{4,}", val):
                return val[:120]

    m_bank = re.search(r"(?im)^Bank(?:\s+Name)?\s*[:\-]\s*(.+)$", cleaned_body)
    bank_hint = m_bank.group(1).strip() if m_bank else ""

    m_at = re.search(
        r"(?i)\b(?:(?:used\s+for|debit(?:ed)?\s+(?:of|with|by|for)?|spent|charged)\s*"
        r"(?:PKR|Rs\.?|PK\s*Rs\.?)\s*[0-9,.]+\s+at|"
        r"purchase\s+at|pos\s+at)\s+"
        r"([A-Za-z0-9 *&'.\-/]{2,60}?)(?:\s+on\s+\d|\s+for\s+(?:PKR|Rs)|\.(?=\s|$)|\n|$)",
        cleaned_body,
    )
    if m_at:
        return m_at.group(1).strip()

    for m_debited_at in re.finditer(
        r"(?i)\bdebited\b(?:[^\n.]|\.(?=\d)){0,90}?\bat\s+"
        r"([A-Za-z0-9 *&'.\-/]{2,60}?)(?:\s+on\s+\d|\s+for\s+(?:PKR|Rs)|\.(?=\s|$)|\n|$)",
        cleaned_body,
    ):
        cand_at = m_debited_at.group(1).strip()
        if not re.match(
            r"(?i)^(?:your\s+|the\s+|a\s+|an\s+|habib\s*metro\b|\d{1,2}:\d{2}\b)",
            cand_at,
        ):
            return cand_at

    m_rel = re.search(
        r"(?i)\b(?:ibft|raast|fund\s+transfer|transfer(?:red)?|payment|paid|sent|received)\s+"
        r"(?:from\s+(?:your\s+)?(?:hmb\b|habib\s*metro\b|a/c\b|account\b)\s*[*●•\d\-]+\s+)?"
        r"(?:to|from)\s+([A-Za-z0-9 *&'.\-/()]{2,70}?)"
        r"(?:\s+from\s+(?:your\s+)?(?:hmb\b|habib\s*metro\b|a/c\b|account\b)|\s+on\s+\d|\s+via\s+|\s+ref|\n|\.(?=\s|$)|$)",
        cleaned_body,
    )
    if m_rel:
        candidate = _strip_hmb_own_account_suffix(m_rel.group(1).strip())
        if candidate and not re.match(r"(?i)^your\s+(?:habibmetro|account)", candidate):
            return candidate

    m_debited_for = re.search(
        r"(?i)\bdebited\b(?:[^\n.]|\.(?=\d)){0,70}?\bfor\s+"
        r"([A-Za-z0-9 *&'.\-/()]{2,70}?)(?:\s+on\s+\d|\s+via\s+|\s+from\s+|\s+ref|\n|\.(?=\s|$)|$)",
        cleaned_body,
    )
    if m_debited_for:
        cand_for = _strip_hmb_own_account_suffix(m_debited_for.group(1).strip())
        if cand_for and not re.match(
            r"(?i)^(?:pkr\b|rs\.?\b|[0-9]+(?:\.[0-9]+)?\b|your\s+(?:account|card|habib\s*metro)|"
            r"the\s+(?:account|card|transaction)|an?\s+|transaction\b|payment\b|purchase\b|"
            r"cash\s+withdrawal\b|habib\s*metro\s+account\b)",
            cand_for,
        ):
            return cand_for

    if (channel == "ATM" or _CASH_WITHDRAWAL_RE.search(cleaned_body)) and not _HMB_FEE_TAX_RE.search(
        cleaned_body
    ):
        return "ATM Cash Withdrawal"
    if bank_hint:
        return bank_hint[:120]
    if _NAYAPAY_RE.search(cleaned_body):
        return "NayaPay Top-Up"
    return (subject or "HabibMetro Transaction").strip()[:120]


def parse_hmb_email(
    message_id: str,
    subject: str,
    sender: str,
    body: str,
    fallback_date: str = "",
) -> Optional[ParsedTransaction]:
    """Parse a HabibMetro (or Outlook-forwarded Habib Metro E-Service) email into ParsedTransaction."""
    if not message_id or not is_hmb_email(subject, sender, body):
        return None
    if is_ignored_hmb_email(subject, body):
        return None

    structured = html_to_structured_text(body)
    cleaned_body, forwarded_sent, forwarded_subject = _extract_outlook_forward_meta(structured)
    raw_subj = (subject or "").strip()
    if forwarded_subject and (
        not raw_subj or re.match(r"(?i)^fwd?\s*:", raw_subj)
    ):
        effective_subject = forwarded_subject
    else:
        effective_subject = raw_subj or forwarded_subject

    if not re.search(
        r"(?i)\b(?:has\s+been\s+(?:debited|credited|used)|debited|credited|"
        r"debit(?!\s+card\b)|credit(?!\s+card\b)|was\s+used|used\s+for|sent|paid|"
        r"spent|received|withdrawn|withdrawal|transfer(?:red)?|purchase|charged|"
        r"refund(?:ed)?|transaction\s+amount)\b",
        f"{effective_subject}\n{cleaned_body}",
    ):
        return None

    amount, tax_fee_amount = _extract_hmb_amount_and_fee(cleaned_body, effective_subject)
    if amount is None or amount <= 0.0:
        return None

    direction = _detect_hmb_direction(effective_subject, cleaned_body)
    tx_date, tx_timestamp = _extract_hmb_datetime(
        cleaned_body, forwarded_sent, fallback_date
    )
    payment_channel = _extract_hmb_channel(effective_subject, cleaned_body)
    payee = _extract_hmb_payee(cleaned_body, effective_subject, direction, payment_channel)

    tx_type: TxType = direction
    target_account: Optional[str] = None
    suggested_category: Optional[str] = None
    suggested_bucket: Optional[CostBucket] = "INFLOW" if direction == "CREDIT" else None

    if direction == "DEBIT":
        (
            tx_type,
            target_account,
            suggested_category,
            mmf_or_internal_bucket,
            payment_channel,
        ) = classify_hmb_transfer(payee, cleaned_body, payment_channel)
        if tx_type == "TRANSFER_INTERNAL" and suggested_category == "Cash Envelope":
            suggested_category = "Cash Withdrawal"
        if mmf_or_internal_bucket is not None:
            suggested_bucket = mmf_or_internal_bucket

    return ParsedTransaction(
        message_id=message_id,
        tx_date=tx_date,
        tx_timestamp=tx_timestamp,
        account_name=WALLET_HABIBMETRO,
        target_account=target_account,
        tx_type=tx_type,
        amount=amount,
        tax_fee_amount=tax_fee_amount,
        payee=payee,
        source="GMAIL_HMB",
        payment_channel=payment_channel,
        raw_subject=effective_subject or None,
        suggested_category=suggested_category,
        suggested_bucket=suggested_bucket,
    )

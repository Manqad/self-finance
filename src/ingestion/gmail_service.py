"""Gmail API OAuth2 & IMAP fallback fetcher and unified email payload dispatcher."""

from __future__ import annotations

import base64
import email
import email.policy
import hashlib
import imaplib
import logging
import os
import re
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

from src.ingestion.hmb_parser import is_hmb_email, parse_hmb_email
from src.ingestion.nayapay_parser import is_nayapay_email, parse_nayapay_email
from src.models import ParsedTransaction
from src.security import get_secret, redact_sensitive_text

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

DEFAULT_GMAIL_QUERY = (
    '(from:nayapay OR from:habibmetro OR subject:"HabibMetro" '
    'OR subject:"NayaPay" OR "HABIBMETRO" OR "Habib Metro E-Service")'
)


def parse_email_payload(
    message_id: str,
    subject: str,
    sender: str,
    body: str,
    date_hdr: str = "",
) -> Optional[ParsedTransaction]:
    """Dispatch a raw email payload to the HabibMetro or NayaPay parser."""
    if not message_id:
        return None

    if is_hmb_email(subject, sender, body):
        return parse_hmb_email(
            message_id=message_id,
            subject=subject,
            sender=sender,
            body=body,
            fallback_date=date_hdr,
        )

    if is_nayapay_email(subject, sender, body):
        return parse_nayapay_email(
            message_id=message_id,
            subject=subject,
            sender=sender,
            body=body,
            fallback_date=date_hdr,
        )

    return None


def _get_auth_paths_and_env() -> dict[str, Any]:
    """Load .env if present and resolve Gmail OAuth2 and IMAP configuration."""
    if "PYTEST_CURRENT_TEST" not in os.environ:
        load_dotenv(override=False)
    credentials_path = Path(get_secret("GMAIL_CREDENTIALS_PATH", "credentials.json"))
    token_path = Path(get_secret("GMAIL_TOKEN_PATH", "token.json"))
    gmail_user = get_secret("GMAIL_USER")
    gmail_app_password = get_secret("GMAIL_APP_PASSWORD")

    return {
        "credentials_path": credentials_path,
        "token_path": token_path,
        "has_credentials": credentials_path.is_file(),
        "has_token": token_path.is_file(),
        "gmail_user": gmail_user,
        "gmail_app_password": gmail_app_password,
        "has_imap": bool(gmail_user and gmail_app_password),
    }


def get_gmail_auth_status() -> dict[str, Any]:
    """Inspect OAuth2 files and .env IMAP credentials and report active sync mode."""
    cfg = _get_auth_paths_and_env()
    has_credentials: bool = cfg["has_credentials"]
    has_token: bool = cfg["has_token"]
    has_imap: bool = cfg["has_imap"]

    if has_credentials or has_token:
        mode = "oauth"
        if has_token:
            detail = (
                f"Gmail API OAuth2 ready ({cfg['token_path'].name} cached)."
            )
        else:
            detail = (
                f"Gmail API OAuth2 configured ({cfg['credentials_path'].name} found; "
                "browser consent needed on first sync)."
            )
    elif has_imap:
        mode = "imap"
        detail = f"IMAP App Password configured for {cfg['gmail_user']}."
    else:
        mode = "unconfigured"
        detail = (
            "Offline mode: neither credentials.json/token.json nor "
            "GMAIL_USER/GMAIL_APP_PASSWORD in .env is configured."
        )

    return {
        "mode": mode,
        "has_credentials": has_credentials,
        "has_token": has_token,
        "has_imap": has_imap,
        "detail": detail,
    }


def _decode_base64url(data: str) -> str:
    """Decode Gmail API base64url-encoded MIME payload safely."""
    if not data:
        return ""
    cleaned = "".join(data.split())
    if not cleaned:
        return ""
    padded = cleaned + "=" * (-len(cleaned) % 4)
    raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _extract_gmail_api_body(payload: dict[str, Any]) -> str:
    """Recursively traverse Gmail MIME payload preferring non-empty text/html over text/plain."""
    html_parts: list[str] = []
    plain_parts: list[str] = []

    def _walk(node: dict[str, Any]) -> None:
        filename = str(node.get("filename") or "").strip()
        headers = {
            str(h.get("name", "")).lower(): str(h.get("value", "")).lower()
            for h in (node.get("headers") or [])
            if isinstance(h, dict)
        }
        disp = headers.get("content-disposition", "")
        if filename or "attachment" in disp:
            return

        mime_type = (node.get("mimeType") or "").lower()
        body_data = (node.get("body") or {}).get("data")
        parts = node.get("parts") or []

        if body_data and mime_type == "text/html":
            html_parts.append(_decode_base64url(body_data))
        elif body_data and mime_type == "text/plain":
            plain_parts.append(_decode_base64url(body_data))

        for child in parts:
            if isinstance(child, dict):
                _walk(child)

    _walk(payload or {})
    non_empty_html = [p for p in html_parts if p.strip()]
    if non_empty_html:
        return "\n".join(non_empty_html)
    non_empty_plain = [p for p in plain_parts if p.strip()]
    return "\n".join(non_empty_plain)


def _extract_imap_body(msg: EmailMessage) -> str:
    """Walk an EmailMessage MIME tree preferring non-empty text/html over text/plain."""
    html_parts: list[str] = []
    plain_parts: list[str] = []

    for part in msg.walk():
        if part.is_multipart():
            continue
        disp = (part.get_content_disposition() or "").lower()
        if disp == "attachment" or bool(part.get_filename()):
            continue
        ctype = (part.get_content_type() or "").lower()
        if ctype not in ("text/html", "text/plain"):
            continue
        try:
            content = part.get_content()
            text = content if isinstance(content, str) else str(content)
        except Exception:
            raw_payload = part.get_payload(decode=True)
            if not raw_payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                text = raw_payload.decode(charset, errors="replace")
            except LookupError:
                text = raw_payload.decode("utf-8", errors="replace")

        if ctype == "text/html":
            html_parts.append(text)
        else:
            plain_parts.append(text)

    non_empty_html = [p for p in html_parts if p.strip()]
    if non_empty_html:
        return "\n".join(non_empty_html)
    non_empty_plain = [p for p in plain_parts if p.strip()]
    return "\n".join(non_empty_plain)


def _fetch_via_oauth(
    max_results: int,
    query: str,
    cfg: dict[str, Any],
    known_message_ids: Optional[set[str]] = None,
) -> tuple[list[ParsedTransaction], int]:
    """Fetch and parse matching bank emails via Google Gmail API OAuth2."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds: Optional[Credentials] = None
    token_path: Path = cfg["token_path"]
    credentials_path: Path = cfg["credentials_path"]

    if token_path.is_file():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except Exception as exc:
            logger.warning("Cached token at %s is unreadable (%s); re-authenticating.", token_path, exc)
            creds = None

    if not creds or not creds.valid:
        refreshed = False
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                refreshed = bool(creds.valid)
            except RefreshError as exc:
                logger.warning(
                    "Gmail OAuth2 refresh failed (%s); falling back to browser consent.",
                    exc,
                )
                creds = None

        if not refreshed:
            if credentials_path.is_file():
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(credentials_path), SCOPES
                )
                creds = flow.run_local_server(port=0)
            else:
                raise FileNotFoundError(
                    f"Missing OAuth credentials file at {credentials_path}"
                )

        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    message_refs: list[dict[str, Any]] = []
    page_token: Optional[str] = None
    while len(message_refs) < max_results:
        batch_size = min(max_results - len(message_refs), 500)
        req_kwargs: dict[str, Any] = {
            "userId": "me",
            "q": query,
            "maxResults": batch_size,
        }
        if page_token:
            req_kwargs["pageToken"] = page_token
        resp = service.users().messages().list(**req_kwargs).execute()
        message_refs.extend(resp.get("messages") or [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    parsed: list[ParsedTransaction] = []
    seen_ids: set[str] = set()
    known_set = _expand_known_message_ids(known_message_ids)
    for ref in message_refs[:max_results]:
        msg_id = ref.get("id")
        if not msg_id or msg_id in seen_ids:
            continue
        seen_ids.add(msg_id)
        if msg_id in known_set:
            continue
        try:
            full_msg = (
                service.users()
                .messages()
                .get(userId="me", id=msg_id, format="full")
                .execute()
            )
            payload = full_msg.get("payload") or {}
            headers = {
                str(h.get("name", "")).lower(): str(h.get("value", ""))
                for h in (payload.get("headers") or [])
            }
            subject = headers.get("subject", "")
            sender = headers.get("from", "")
            date_hdr = headers.get("date", "")
            body = _extract_gmail_api_body(payload)
            tx = parse_email_payload(
                message_id=msg_id,
                subject=subject,
                sender=sender,
                body=body,
                date_hdr=date_hdr,
            )
            if tx is not None:
                parsed.append(tx)
        except Exception as exc:
            logger.warning("Failed parsing Gmail message %s: %s", msg_id, exc)
            continue

    return parsed, len(seen_ids)


def _expand_known_message_ids(known_message_ids: Optional[set[str]]) -> set[str]:
    """Expand known IDs with OAuth hex <-> IMAP XGM decimal equivalents."""
    expanded: set[str] = set()
    for mid in known_message_ids or ():
        s = str(mid or "").strip()
        if not s:
            continue
        expanded.add(s)
        if s.startswith("XGM_") and s[4:].isdigit():
            expanded.add(format(int(s[4:]), "x"))
        elif re.fullmatch(r"[0-9a-fA-F]{12,16}", s):
            expanded.add(f"XGM_{int(s, 16)}")
    return expanded


def _parse_imap_fetch_items(
    msg_data: Any,
) -> list[tuple[Optional[str], bytes]]:
    """Extract (gm_msgid, raw_rfc822_bytes) pairs from an IMAP FETCH response."""
    items: list[tuple[Optional[str], bytes]] = []
    for item in msg_data or []:
        if isinstance(item, (bytes, bytearray)):
            if items and items[-1][0] is None:
                m_gm = re.search(rb"X-GM-MSGID\s+(\d+)", bytes(item), re.IGNORECASE)
                if m_gm:
                    items[-1] = (
                        m_gm.group(1).decode("ascii", errors="ignore"),
                        items[-1][1],
                    )
            continue
        if not (isinstance(item, tuple) and len(item) >= 2):
            continue
        meta = item[0]
        raw_part = item[1]
        if not isinstance(raw_part, (bytes, bytearray)):
            continue
        gm_msgid: Optional[str] = None
        if isinstance(meta, (bytes, bytearray)):
            m_gm = re.search(rb"X-GM-MSGID\s+(\d+)", bytes(meta), re.IGNORECASE)
            if m_gm:
                gm_msgid = m_gm.group(1).decode("ascii", errors="ignore")
        items.append((gm_msgid, bytes(raw_part)))
    return items


def _fetch_via_imap(
    max_results: int,
    query: str,
    cfg: dict[str, Any],
    known_message_ids: Optional[set[str]] = None,
) -> tuple[list[ParsedTransaction], int]:
    """Fetch and parse matching bank emails via Gmail IMAP SSL using batched UID fetches."""
    try:
        conn = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=15)
    except TypeError:
        conn = imaplib.IMAP4_SSL("imap.gmail.com", 993)

    try:
        conn.login(cfg["gmail_user"], cfg["gmail_app_password"])
        conn.select("INBOX", readonly=True)

        msg_uids: list[bytes] = []
        gm_raw_ok = False
        escaped_query = query.replace('"', '\\"')
        try:
            typ, data = conn.uid("SEARCH", None, "X-GM-RAW", f'"{escaped_query}"')
            if typ == "OK":
                gm_raw_ok = True
                if data and data[0]:
                    msg_uids = data[0].split()
        except Exception:
            gm_raw_ok = False
            msg_uids = []

        # Only fall back to ALL if X-GM-RAW itself failed (not when OK with 0 matches)
        if not gm_raw_ok:
            typ, data = conn.uid("SEARCH", None, "ALL")
            if typ == "OK" and data and data[0]:
                msg_uids = data[0].split()

        target_uids = list(reversed(msg_uids[-max_results:])) if msg_uids else []
        parsed: list[ParsedTransaction] = []
        seen_ids: set[str] = set()
        if not target_uids:
            return parsed, 0

        known_set = _expand_known_message_ids(known_message_ids)
        uids_to_fetch = list(target_uids)

        # Fast pre-filter: fetch only tiny UID + X-GM-MSGID metadata in one batch
        # so already-imported emails don't have their full bodies downloaded again.
        if known_set and len(uids_to_fetch) > 1:
            try:
                uid_to_gm: dict[bytes, str] = {}
                for start in range(0, len(uids_to_fetch), 100):
                    uid_set = b",".join(uids_to_fetch[start : start + 100])
                    m_typ, m_data = conn.uid("FETCH", uid_set, "(UID X-GM-MSGID)")
                    if m_typ != "OK" or not m_data:
                        continue
                    for entry in m_data:
                        raw_line = (
                            bytes(entry[0])
                            if isinstance(entry, tuple) and isinstance(entry[0], (bytes, bytearray))
                            else (bytes(entry) if isinstance(entry, (bytes, bytearray)) else b"")
                        )
                        if not raw_line:
                            continue
                        m_uid = re.search(rb"\bUID\s+(\d+)", raw_line, re.IGNORECASE)
                        m_gm = re.search(rb"\bX-GM-MSGID\s+(\d+)", raw_line, re.IGNORECASE)
                        if m_uid and m_gm:
                            uid_to_gm[m_uid.group(1)] = m_gm.group(1).decode("ascii", errors="ignore")
                if uid_to_gm:
                    uids_to_fetch = [
                        uid
                        for uid in uids_to_fetch
                        if f"XGM_{uid_to_gm[uid]}" not in known_set
                        if uid in uid_to_gm
                    ] + [uid for uid in uids_to_fetch if uid not in uid_to_gm]
            except Exception:
                uids_to_fetch = list(target_uids)

        # Batch full body fetches (25 messages per IMAP round-trip instead of 1-by-1)
        fetched_entries: list[tuple[Optional[str], bytes]] = []
        for start in range(0, len(uids_to_fetch), 25):
            chunk = uids_to_fetch[start : start + 25]
            uid_arg: bytes = chunk[0] if len(chunk) == 1 else b",".join(chunk)
            msg_data = None
            typ = "NO"
            try:
                typ, msg_data = conn.uid("FETCH", uid_arg, "(X-GM-MSGID BODY.PEEK[])")
            except Exception:
                typ = "NO"
            if typ != "OK" or not msg_data:
                try:
                    typ, msg_data = conn.uid("FETCH", uid_arg, "(BODY.PEEK[])")
                except Exception:
                    typ = "NO"
            if typ == "OK" and msg_data:
                fetched_entries.extend(_parse_imap_fetch_items(msg_data))
                continue

            # Fallback to single-UID fetch only if a batch failed
            if len(chunk) > 1:
                for single_uid in chunk:
                    try:
                        s_typ, s_data = conn.uid("FETCH", single_uid, "(X-GM-MSGID BODY.PEEK[])")
                        if s_typ != "OK" or not s_data:
                            s_typ, s_data = conn.uid("FETCH", single_uid, "(BODY.PEEK[])")
                        if s_typ == "OK" and s_data:
                            fetched_entries.extend(_parse_imap_fetch_items(s_data))
                    except Exception as exc:
                        logger.warning("Failed fetching IMAP UID %s: %s", single_uid, exc)

        for gm_msgid, raw_bytes in fetched_entries:
            try:
                msg = email.message_from_bytes(
                    raw_bytes, policy=email.policy.default
                )
                raw_mid = str(msg.get("Message-ID") or "").strip().strip("<>")
                if gm_msgid:
                    msg_id = f"XGM_{gm_msgid}"
                elif raw_mid:
                    msg_id = raw_mid
                else:
                    digest = hashlib.sha256(raw_bytes).hexdigest()[:16]
                    msg_id = f"IMAP_SHA_{digest}"

                if msg_id in seen_ids or msg_id in known_set:
                    continue
                seen_ids.add(msg_id)

                subject = str(msg.get("Subject") or "")
                sender = str(msg.get("From") or "")
                date_hdr = str(msg.get("Date") or "")
                body = _extract_imap_body(msg)  # type: ignore[arg-type]

                tx = parse_email_payload(
                    message_id=msg_id,
                    subject=subject,
                    sender=sender,
                    body=body,
                    date_hdr=date_hdr,
                )
                if tx is not None:
                    parsed.append(tx)
            except Exception as exc:
                logger.warning("Failed parsing IMAP message: %s", exc)
                continue

        return parsed, len(target_uids)
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _sync_emails_internal(
    max_results: int,
    query: str,
    known_message_ids: Optional[set[str]] = None,
) -> tuple[list[ParsedTransaction], int, str]:
    """Internal coordinator selecting OAuth2, IMAP, or offline fallback."""
    status = get_gmail_auth_status()
    cfg = _get_auth_paths_and_env()
    mode = status["mode"]

    if mode == "oauth":
        txs, scanned = _fetch_via_oauth(
            max_results=max_results,
            query=query,
            cfg=cfg,
            known_message_ids=known_message_ids,
        )
        return txs, scanned, "Gmail OAuth2"

    if mode == "imap":
        txs, scanned = _fetch_via_imap(
            max_results=max_results,
            query=query,
            cfg=cfg,
            known_message_ids=known_message_ids,
        )
        return txs, scanned, "IMAP App Password"

    raise RuntimeError(status["detail"])


def fetch_recent_bank_emails(
    max_results: int = 100,
    query: str = DEFAULT_GMAIL_QUERY,
    raise_on_error: bool = False,
    known_message_ids: Optional[set[str]] = None,
) -> list[ParsedTransaction]:
    """Fetch and parse recent HabibMetro and NayaPay alerts from Gmail."""
    status = get_gmail_auth_status()
    if status["mode"] == "unconfigured":
        if raise_on_error:
            raise RuntimeError(status["detail"])
        return []

    try:
        txs, _, _ = _sync_emails_internal(
            max_results=max_results,
            query=query,
            known_message_ids=known_message_ids,
        )
        return txs
    except Exception as exc:
        if raise_on_error:
            raise
        logger.warning("Gmail fetch failed gracefully: %s", exc)
        return []


def fetch_emails_with_report(
    max_results: int = 100,
    query: str = DEFAULT_GMAIL_QUERY,
    known_message_ids: Optional[set[str]] = None,
) -> tuple[list[ParsedTransaction], str]:
    """Fetch bank emails and return (parsed_transactions, human_readable_status)."""
    status = get_gmail_auth_status()
    if status["mode"] == "unconfigured":
        return [], status["detail"]

    try:
        txs, scanned, mode_label = _sync_emails_internal(
            max_results=max_results,
            query=query,
            known_message_ids=known_message_ids,
        )
        msg = (
            f"Synced {len(txs)} transaction(s) from {scanned} matching email(s) "
            f"via {mode_label}."
        )
        return txs, msg
    except Exception as exc:
        safe_exc = redact_sensitive_text(exc)
        logger.warning("Gmail sync failed: %s", safe_exc)
        return [], f"Gmail sync failed ({status['mode']}): {safe_exc}"

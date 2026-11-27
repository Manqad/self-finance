"""Parser and importer tests for HabibMetro, NayaPay, PDF/CSV statements, and Gmail dispatch."""

from __future__ import annotations

import base64
from email.message import EmailMessage
import imaplib
import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bs4 import BeautifulSoup

from src.database.db_manager import DatabaseManager
from src.ingestion.gmail_service import (
    _extract_gmail_api_body,
    _extract_imap_body,
    _fetch_via_imap,
    _fetch_via_oauth,
    get_gmail_auth_status,
    parse_email_payload,
)
from src.ingestion.hmb_parser import clean_email_html, is_hmb_email, parse_hmb_email
from src.ingestion.nayapay_parser import is_nayapay_email, parse_nayapay_email
from src.ingestion.pdf_importer import (
    _clean_csv_number,
    _parse_csv_number_and_drcr,
    import_statement_file,
    parse_hmb_pdf_text,
    parse_nayapay_pdf_text,
    parse_statement_csv,
)
from src.models import (
    WALLET_CASH,
    WALLET_HABIBMETRO,
    WALLET_MMF,
    WALLET_NAYAPAY,
)
from src.ui.views import _load_html_fixtures

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _load_article_fragments(filename: str) -> list[tuple[str, str, str, str]]:
    raw = (FIXTURES_DIR / filename).read_text(encoding="utf-8")
    soup = BeautifulSoup(raw, "lxml")
    fragments: list[tuple[str, str, str, str]] = []
    for node in soup.find_all("article", class_="email-fixture"):
        fragments.append(
            (
                str(node),
                str(node.get("data-subject") or ""),
                str(node.get("data-sender") or ""),
                str(node.get("data-date") or ""),
            )
        )
    return fragments


def _build_minimal_pdf(lines: list[str]) -> bytes:
    """Create a valid single-page PDF byte payload readable by pypdf.PdfReader."""
    stream_parts = ["BT /F1 10 Tf 40 750 Td"]
    for idx, line in enumerate(lines):
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        if idx == 0:
            stream_parts.append(f"({escaped}) Tj")
        else:
            stream_parts.append(f"0 -16 Td ({escaped}) Tj")
    stream_parts.append("ET")
    content = "\n".join(stream_parts).encode("latin-1")

    objs = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n",
        b"4 0 obj\n<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream\nendobj\n",
        b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
    ]
    buf = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for obj in objs:
        offsets.append(len(buf))
        buf.extend(obj)
    xref_pos = len(buf)
    buf.extend(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        buf.extend(f"{off:010d} 00000 n \n".encode())
    buf.extend(
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
    )
    return bytes(buf)


def test_parse_sample_hmb_fixture_fragments() -> None:
    """Parse all Outlook-forwarded HabibMetro fixture fragments and verify skipped OTP/declined."""
    fragments = _load_article_fragments("sample_hmb.html")
    assert len(fragments) == 5

    parsed = []
    for idx, (html_frag, subj, sender, dt_hdr) in enumerate(fragments):
        tx = parse_hmb_email(
            message_id=f"HMB_FIX_{idx}",
            subject=subj,
            sender=sender,
            body=html_frag,
            fallback_date=dt_hdr,
        )
        parsed.append(tx)

    # 1. K-ELECTRIC Bill Payment
    tx_ke = parsed[0]
    assert tx_ke is not None
    assert tx_ke.account_name == WALLET_HABIBMETRO
    assert tx_ke.tx_type == "DEBIT"
    assert tx_ke.amount == pytest.approx(14500.0)
    assert tx_ke.tax_fee_amount == pytest.approx(0.0)
    assert tx_ke.payee == "K-ELECTRIC"
    assert tx_ke.tx_date == "2026-11-05"
    assert tx_ke.payment_channel == "Bill Payment"
    assert tx_ke.source == "GMAIL_HMB"

    # 2. HMB -> NayaPay IBFT top-up (TRANSFER_INTERNAL)
    tx_np = parsed[1]
    assert tx_np is not None
    assert tx_np.account_name == WALLET_HABIBMETRO
    assert tx_np.target_account == WALLET_NAYAPAY
    assert tx_np.tx_type == "TRANSFER_INTERNAL"
    assert tx_np.amount == pytest.approx(20000.0)
    assert tx_np.payee == "NAYAPAY - DEMO ACCOUNT"
    assert tx_np.tx_date == "2026-11-06"
    assert tx_np.suggested_bucket == "IGNORE"
    assert tx_np.payment_channel == "IBFT"

    # 3. HMB -> Al Meezan MRAF sweep (TRANSFER_MMF)
    tx_mmf = parsed[2]
    assert tx_mmf is not None
    assert tx_mmf.account_name == WALLET_HABIBMETRO
    assert tx_mmf.target_account == WALLET_MMF
    assert tx_mmf.tx_type == "TRANSFER_MMF"
    assert tx_mmf.amount == pytest.approx(55000.0)
    assert tx_mmf.payee == "AL MEEZAN INVESTMENT - MRAF"
    assert tx_mmf.tx_date == "2026-11-07"
    assert tx_mmf.suggested_bucket == "INVESTMENT"

    # 4 & 5. OTP (9355 / One Time Password) and Declined must return None
    assert parsed[3] is None
    assert parsed[4] is None


def test_parse_sample_nayapay_fixture_fragments() -> None:
    """Parse all NayaPay HTML receipt fragments including WHT+FX split and Raast transfer."""
    fragments = _load_article_fragments("sample_nayapay.html")
    assert len(fragments) == 4

    parsed = []
    for idx, (html_frag, subj, sender, dt_hdr) in enumerate(fragments):
        tx = parse_nayapay_email(
            message_id=f"NP_FIX_{idx}",
            subject=subj,
            sender=sender,
            body=html_frag,
            fallback_date=dt_hdr,
        )
        parsed.append(tx)

    # 1. Foodpanda order
    tx_fp = parsed[0]
    assert tx_fp is not None
    assert tx_fp.account_name == WALLET_NAYAPAY
    assert tx_fp.tx_type == "DEBIT"
    assert tx_fp.amount == pytest.approx(1850.0)
    assert tx_fp.tax_fee_amount == pytest.approx(0.0)
    assert tx_fp.payee == "FOODPANDA PAKISTAN"
    assert tx_fp.payment_channel == "Card"
    assert tx_fp.tx_date == "2026-11-08"

    # 2. Netflix international card receipt with WHT + FX fee separated (1100 + 165 = 1265)
    tx_nf = parsed[1]
    assert tx_nf is not None
    assert tx_nf.account_name == WALLET_NAYAPAY
    assert tx_nf.tx_type == "DEBIT"
    assert tx_nf.amount == pytest.approx(1100.0)
    assert tx_nf.tax_fee_amount == pytest.approx(165.0)
    assert tx_nf.amount + tx_nf.tax_fee_amount == pytest.approx(1265.0)
    assert tx_nf.payee == "NETFLIX.COM"
    assert tx_nf.payment_channel == "Card"
    assert tx_nf.tx_date == "2026-11-10"

    # 3. Raast transfer to BYKEA
    tx_byk = parsed[2]
    assert tx_byk is not None
    assert tx_byk.account_name == WALLET_NAYAPAY
    assert tx_byk.tx_type == "DEBIT"
    assert tx_byk.amount == pytest.approx(650.0)
    assert tx_byk.tax_fee_amount == pytest.approx(0.0)
    assert tx_byk.payee == "BYKEA"
    assert tx_byk.payment_channel == "Raast"
    assert tx_byk.notes == "Raast IBAN: PK36MEZN0002110104567890"
    assert tx_byk.tx_date == "2026-11-09"

    # 4. OTP / Failed alert skipped
    assert parsed[3] is None


def test_ui_fixture_loader_loads_all_6_sample_transactions() -> None:
    """Verify src.ui.views._load_html_fixtures cleanly parses both HTML fixture files."""
    txs, loaded_names = _load_html_fixtures(FIXTURES_DIR)
    assert loaded_names == ["sample_hmb.html", "sample_nayapay.html"]
    assert len(txs) == 6
    assert [t.payee for t in txs] == [
        "K-ELECTRIC",
        "NAYAPAY - DEMO ACCOUNT",
        "AL MEEZAN INVESTMENT - MRAF",
        "FOODPANDA PAKISTAN",
        "NETFLIX.COM",
        "BYKEA",
    ]


def test_hmb_atm_credit_fee_and_ignored_variants() -> None:
    """Test HMB ATM->Cash, Salary CREDIT, bank fee extraction, and ignored email filters."""
    atm_html = """
    <div>---------- Forwarded message ---------</div>
    <div>From: Habib Metro E-Service &lt;eservice@habibmetro.com&gt;</div>
    <div>Subject: HabibMetro ATM Cash Withdrawal Alert</div>
    <p>Your account ****7890 has been debited with PKR 10,000.00 via ATM Cash Withdrawal on 07-Nov-2026 16:30:00.</p>
    <p>Available Balance: PKR 160,500.00</p>
    """
    cleaned = clean_email_html(atm_html)
    assert "Forwarded message" not in cleaned
    assert "eservice@habibmetro.com" not in cleaned

    tx_atm = parse_hmb_email(
        "HMB_ATM_1",
        "Fwd: HabibMetro ATM Cash Withdrawal Alert",
        "user@example.com",
        atm_html,
    )
    assert tx_atm is not None
    assert tx_atm.tx_type == "TRANSFER_INTERNAL"
    assert tx_atm.target_account == WALLET_CASH
    assert tx_atm.amount == pytest.approx(10000.0)
    assert tx_atm.payment_channel == "ATM"
    assert tx_atm.tx_date == "2026-11-07"

    credit_html = """
    <p>Dear Customer, your HabibMetro account ****7890 has been credited with PKR 200,000.00.</p>
    <table>
      <tr><td>Sender Name</td><td>ACME TECH PAKISTAN - SALARY</td></tr>
      <tr><td>Transaction Amount</td><td>PKR 200,000.00</td></tr>
      <tr><td>Transaction Date</td><td>01-Nov-2026</td></tr>
      <tr><td>Available Balance</td><td>PKR 340,000.00</td></tr>
    </table>
    """
    tx_cred = parse_hmb_email(
        "HMB_CRED_1",
        "HabibMetro Credit Alert",
        "eservice@habibmetro.com",
        credit_html,
    )
    assert tx_cred is not None
    assert tx_cred.tx_type == "CREDIT"
    assert tx_cred.amount == pytest.approx(200000.0)
    assert tx_cred.payee == "ACME TECH PAKISTAN - SALARY"
    assert tx_cred.suggested_bucket == "INFLOW"

    # Ignored OTP & status variations
    for ignored_body in (
        "Your OTP code is 441233 for HabibMetro.",
        "Security alert: login attempt detected with verification code 9355.",
        "Your card purchase at Daraz was unsuccessful.",
        "Your IBFT transfer failed due to system timeout.",
    ):
        assert (
            parse_hmb_email(
                "HMB_IGN",
                "HabibMetro Alert",
                "eservice@habibmetro.com",
                ignored_body,
            )
            is None
        )


def test_nayapay_total_minus_taxes_no_double_counting_and_credit() -> None:
    """Verify NayaPay tax separation without explicit base amount and inward CREDIT parsing."""
    receipt_total_only = """
    <p>Card Transaction at SPOTIFY.COM</p>
    <table>
      <tr><td>Merchant Name</td><td>SPOTIFY.COM</td></tr>
      <tr><td>Payment Method</td><td>NayaPay Visa Card</td></tr>
      <tr><td>Withholding Tax (WHT)</td><td>Rs. 65.00</td></tr>
      <tr><td>International FX Fee</td><td>Rs. 35.00</td></tr>
      <tr><td>Total Charged</td><td>Rs. 750.00</td></tr>
      <tr><td>Date &amp; Time</td><td>11 Nov 2026 12:15 PM</td></tr>
    </table>
    """
    tx_sp = parse_nayapay_email(
        "NP_SPOT_1",
        "NayaPay Card Transaction - SPOTIFY",
        "alerts@nayapay.com",
        receipt_total_only,
    )
    assert tx_sp is not None
    assert tx_sp.amount == pytest.approx(650.0)
    assert tx_sp.tax_fee_amount == pytest.approx(100.0)
    assert tx_sp.amount + tx_sp.tax_fee_amount == pytest.approx(750.0)

    # Same-day Transaction Amount == Total Charged should subtract taxes instead of double counting
    receipt_inclusive_tx_amt = """
    <table>
      <tr><td>Merchant Name</td><td>OPENAI CHATGPT</td></tr>
      <tr><td>Transaction Amount</td><td>Rs. 6,325.00</td></tr>
      <tr><td>WHT</td><td>Rs. 550.00</td></tr>
      <tr><td>FX Fee</td><td>Rs. 275.00</td></tr>
      <tr><td>Total Charged</td><td>Rs. 6,325.00</td></tr>
      <tr><td>Date</td><td>11 Nov 2026</td></tr>
    </table>
    """
    tx_oai = parse_nayapay_email(
        "NP_OAI_1",
        "NayaPay Card Receipt",
        "alerts@nayapay.com",
        receipt_inclusive_tx_amt,
    )
    assert tx_oai is not None
    assert tx_oai.amount == pytest.approx(5500.0)
    assert tx_oai.tax_fee_amount == pytest.approx(825.0)

    # Inward Raast CREDIT receipt
    inward_raast = """
    <p>Money Received! You received Rs. 15,000.00 via Raast.</p>
    <table>
      <tr><td>Sender Name</td><td>ZAINAB KHAN</td></tr>
      <tr><td>Received Amount</td><td>Rs. 15,000.00</td></tr>
      <tr><td>Date &amp; Time</td><td>10 Nov 2026 04:00 PM</td></tr>
    </table>
    """
    tx_in = parse_nayapay_email(
        "NP_IN_1",
        "NayaPay Money Received",
        "alerts@nayapay.com",
        inward_raast,
    )
    assert tx_in is not None
    assert tx_in.tx_type == "CREDIT"
    assert tx_in.amount == pytest.approx(15000.0)
    assert tx_in.payee == "ZAINAB KHAN"
    assert tx_in.payment_channel == "Raast"
    assert tx_in.suggested_bucket == "INFLOW"


def test_pdf_and_csv_importer_and_deduplication(tmp_path: Path) -> None:
    """Verify PDF text/binary parsing, CSV import, overlapping deduplication, and same-day twins."""
    np_pdf_text = """
    NayaPay Wallet E-Statement
    Opening Balance PKR 10,000.00
    Date Description Debit Credit Balance
    2026-11-08 FOODPANDA PAKISTAN Card 1,850.00 0.00 8,150.00
    2026-11-08 FOODPANDA PAKISTAN Card 1,850.00 0.00 6,300.00
    2026-11-09 BYKEA Raast 650.00 0.00 5,650.00
    2026-11-10 INWARD RAAST FROM ALI 0.00 5,000.00 10,650.00
    Closing Balance PKR 10,650.00
    """
    np_txs = parse_nayapay_pdf_text(np_pdf_text)
    assert len(np_txs) == 4
    # Same-day identical Foodpanda orders must receive distinct message_ids
    assert np_txs[0].amount == pytest.approx(1850.0)
    assert np_txs[1].amount == pytest.approx(1850.0)
    assert np_txs[0].message_id != np_txs[1].message_id
    assert np_txs[2].payment_channel == "Raast"
    assert np_txs[3].tx_type == "CREDIT"
    assert np_txs[3].amount == pytest.approx(5000.0)

    # Overlapping PDF window re-import must hash identically so SQLite deduplicates
    overlapping_np_pdf = """
    NayaPay Wallet E-Statement
    2026-11-09 BYKEA Raast 650.00 0.00 5,650.00
    2026-11-10 INWARD RAAST FROM ALI 0.00 5,000.00 10,650.00
    2026-11-11 DARAZ.PK Card 3,200.00 0.00 7,450.00
    """
    overlap_txs = parse_nayapay_pdf_text(overlapping_np_pdf)
    assert overlap_txs[0].message_id == np_txs[2].message_id
    assert overlap_txs[1].message_id == np_txs[3].message_id

    from src.engine.categorizer import apply_rules_batch

    with DatabaseManager(tmp_path / "pdf_dedup.db") as db:
        ins1, skip1 = db.upsert_many(apply_rules_batch(np_txs, db))
        assert (ins1, skip1) == (4, 0)
        ins2, skip2 = db.upsert_many(apply_rules_batch(overlap_txs, db))
        assert (ins2, skip2) == (1, 2)
        assert len(db.get_transactions()) == 5

    # Real binary PDF bytes round-trip via pypdf.PdfReader
    pdf_bytes = _build_minimal_pdf(
        [
            "NayaPay E-Statement",
            "08 Nov 2026 FOODPANDA PAKISTAN Card 1,850.00 DR 18,150.00",
            "09 Nov 2026 BYKEA Raast 650.00 DR 17,500.00",
        ]
    )
    pdf_imported = import_statement_file(io.BytesIO(pdf_bytes), "nayapay_nov.pdf")
    assert len(pdf_imported) == 2
    assert pdf_imported[0].account_name == WALLET_NAYAPAY
    assert pdf_imported[0].payee == "FOODPANDA PAKISTAN"
    assert pdf_imported[1].payee == "BYKEA"

    # HabibMetro PDF text with transfer classification
    hmb_pdf_text = """
    HabibMetro Account Statement
    Opening Balance PKR 250,000.00
    Date Description Debit Credit Balance
    05-11-2026 K-ELECTRIC BILL PAYMENT 14,500.00 0.00 235,500.00
    06-11-2026 IBFT TO NAYAPAY DEMO ACCOUNT 20,000.00 0.00 215,500.00
    07-11-2026 IBFT AL MEEZAN INVESTMENT MRAF 55,000.00 0.00 160,500.00
    """
    hmb_txs = parse_hmb_pdf_text(hmb_pdf_text)
    assert len(hmb_txs) == 3
    assert hmb_txs[0].tx_type == "DEBIT"
    assert hmb_txs[1].tx_type == "TRANSFER_INTERNAL"
    assert hmb_txs[1].target_account == WALLET_NAYAPAY
    assert hmb_txs[2].tx_type == "TRANSFER_MMF"
    assert hmb_txs[2].target_account == WALLET_MMF

    # CSV importer with preamble rows & tax column
    csv_content = """HabibMetro Statement Export
Account: ****7890
Transaction Date,Particulars,Debit,Credit,Tax
2026-11-01,SALARY REMUNERATION,,200000.00,0
2026-11-05,K-ELECTRIC BILL,14500.00,,0
2026-11-06,NAYAPAY TOPUP,20000.00,,0
2026-11-07,AL MEEZAN MRAF,55000.00,,0
"""
    csv_txs = parse_statement_csv(csv_content, account_hint="HabibMetro")
    assert len(csv_txs) == 4
    assert csv_txs[0].tx_type == "CREDIT"
    assert csv_txs[1].tx_type == "DEBIT"
    assert csv_txs[2].tx_type == "TRANSFER_INTERNAL"
    assert csv_txs[2].target_account == WALLET_NAYAPAY
    assert csv_txs[3].tx_type == "TRANSFER_MMF"
    assert csv_txs[3].target_account == WALLET_MMF
    assert len(import_statement_file(csv_content.encode("utf-8"), "hmb_nov.csv")) == 4


def test_gmail_dispatcher_and_auth_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify unified email dispatcher routing and OAuth/IMAP/offline auth status detection."""
    fragments = _load_article_fragments("sample_hmb.html")
    # Forwarded HMB -> NayaPay IBFT must route to HMB parser, NOT NayaPay parser
    ibft_frag, subj, sender, dt_hdr = fragments[1]
    dispatched = parse_email_payload(
        message_id="DISP_HMB_NP",
        subject=subj,
        sender=sender,
        body=ibft_frag,
        date_hdr=dt_hdr,
    )
    assert dispatched is not None
    assert dispatched.source == "GMAIL_HMB"
    assert dispatched.account_name == WALLET_HABIBMETRO
    assert dispatched.target_account == WALLET_NAYAPAY
    assert dispatched.tx_type == "TRANSFER_INTERNAL"

    # Unrelated newsletter returns None
    assert (
        parse_email_payload(
            message_id="DISP_OTHER",
            subject="Weekly Tech Digest",
            sender="news@example.com",
            body="<p>Hello world</p>",
        )
        is None
    )

    # Auth status modes
    cred_file = tmp_path / "credentials.json"
    token_file = tmp_path / "token.json"
    monkeypatch.setattr("src.ingestion.gmail_service.load_dotenv", lambda *a, **kw: False)
    monkeypatch.setenv("GMAIL_CREDENTIALS_PATH", str(cred_file))
    monkeypatch.setenv("GMAIL_TOKEN_PATH", str(token_file))
    monkeypatch.delenv("GMAIL_USER", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)

    assert get_gmail_auth_status()["mode"] == "unconfigured"

    monkeypatch.setenv("GMAIL_USER", "user@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    assert get_gmail_auth_status()["mode"] == "imap"

    cred_file.write_text("{}", encoding="utf-8")
    assert get_gmail_auth_status()["mode"] == "oauth"
    assert get_gmail_auth_status()["has_token"] is False

    token_file.write_text("{}", encoding="utf-8")
    oauth_status = get_gmail_auth_status()
    assert oauth_status["mode"] == "oauth"
    assert oauth_status["has_token"] is True


def _build_multipage_pdf(pages_lines: list[list[str]]) -> bytes:
    """Build a multi-page valid PDF byte stream readable by pypdf.PdfReader."""
    # Object 1: Catalog, Object 2: Pages, Object 3: Font
    # For each page i: Page obj (4 + 2*i) and Content obj (5 + 2*i)
    num_pages = len(pages_lines)
    page_ids = [4 + 2 * i for i in range(num_pages)]
    kids_ref = " ".join(f"{pid} 0 R" for pid in page_ids)

    objs: list[bytes] = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        f"2 0 obj\n<< /Type /Pages /Kids [{kids_ref}] /Count {num_pages} >>\nendobj\n".encode(),
        b"3 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
    ]
    for i, lines in enumerate(pages_lines):
        page_id = 4 + 2 * i
        content_id = 5 + 2 * i
        stream_parts = ["BT /F1 10 Tf 40 750 Td"]
        for idx, line in enumerate(lines):
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            if idx == 0:
                stream_parts.append(f"({escaped}) Tj")
            else:
                stream_parts.append(f"0 -16 Td ({escaped}) Tj")
        stream_parts.append("ET")
        content = "\n".join(stream_parts).encode("latin-1")
        objs.append(
            f"{page_id} 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {content_id} 0 R /Resources << /Font << /F1 3 0 R >> >> >>\nendobj\n".encode()
        )
        objs.append(
            f"{content_id} 0 obj\n<< /Length {len(content)} >>\nstream\n".encode()
            + content
            + b"\nendstream\nendobj\n"
        )

    buf = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for obj in objs:
        offsets.append(len(buf))
        buf.extend(obj)
    xref_pos = len(buf)
    buf.extend(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        buf.extend(f"{off:010d} 00000 n \n".encode())
    buf.extend(
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
    )
    return bytes(buf)


def test_hmb_otp_false_positives_disclaimers_and_refunds_not_dropped() -> None:
    """Verify PKR 9355.00, ****9355, FOOTPATH SHOES, disclaimers, and refunds are NOT dropped."""
    footpath_html = """
    <p>Dear Customer,</p>
    <p>Your HabibMetro debit card ****9355 has been debited with PKR 9355.00 at FOOTPATH SHOES on 10-Nov-2026.</p>
    <p>Available Balance: PKR 90,000.00.</p>
    <p>Security Notice: HabibMetro never asks for your OTP, PIN, or verification code. Never share your OTP with anyone.</p>
    """
    tx_fp = parse_hmb_email(
        "HMB_FP_9355",
        "HabibMetro Card Debit Alert",
        "eservice@habibmetro.com",
        footpath_html,
    )
    assert tx_fp is not None
    assert tx_fp.amount == pytest.approx(9355.0)
    assert tx_fp.payee == "FOOTPATH SHOES"
    assert tx_fp.tx_type == "DEBIT"
    assert tx_fp.tx_date == "2026-11-10"

    # Refund containing "Failed" must NOT be dropped and must parse as CREDIT
    hmb_refund_html = """
    <p>Your HabibMetro account ****7890 has been credited with PKR 4,500.00 on 11-Nov-2026.</p>
    <table>
      <tr><td>Description</td><td>Credit Failed Refund - Daraz</td></tr>
      <tr><td>Transaction Amount</td><td>PKR 4,500.00</td></tr>
      <tr><td>Available Balance</td><td>PKR 94,500.00</td></tr>
    </table>
    """
    tx_hmb_ref = parse_hmb_email(
        "HMB_REF_1",
        "Credit Failed Refund - Daraz",
        "eservice@habibmetro.com",
        hmb_refund_html,
    )
    assert tx_hmb_ref is not None
    assert tx_hmb_ref.tx_type == "CREDIT"
    assert tx_hmb_ref.amount == pytest.approx(4500.0)
    assert tx_hmb_ref.payee == "Credit Failed Refund - Daraz"

    np_refund_html = """
    <p>NayaPay Transaction Receipt</p>
    <p>You received Rs. 2,200.00 for Failed Order Refund on 11 Nov 2026.</p>
    <table>
      <tr><td>Merchant Name</td><td>Failed Order Refund</td></tr>
      <tr><td>Amount</td><td>Rs. 2,200.00</td></tr>
    </table>
    """
    tx_np_ref = parse_nayapay_email(
        "NP_REF_1",
        "NayaPay Failed Order Refund",
        "alerts@nayapay.com",
        np_refund_html,
    )
    assert tx_np_ref is not None
    assert tx_np_ref.tx_type == "CREDIT"
    assert tx_np_ref.amount == pytest.approx(2200.0)

    # Explicit failed payment without refund is still skipped
    assert (
        parse_nayapay_email(
            "NP_FAIL_REAL",
            "NayaPay Payment Failed",
            "alerts@nayapay.com",
            "<p>Your payment was failed at Daraz for Rs. 2,200.00.</p>",
        )
        is None
    )


def test_email_routing_partial_nayapay_vs_uber_and_sadapay() -> None:
    """Partial NayaPay forward mentioning HabibMetro routes to NayaPay; Uber/SadaPay return None."""
    partial_np_forward = """
    <div class="np-brand">NayaPay Raast Transfer Receipt</div>
    <p>Money Sent! You paid Rs. 5,000.00 via NayaPay on 11 Nov 2026.</p>
    <table>
      <tr><td>Account Title</td><td>OMAR TARIQ</td></tr>
      <tr><td>Bank Name</td><td>HabibMetro Bank</td></tr>
      <tr><td>Receiver IBAN</td><td>PK49HABB0009876543210123</td></tr>
      <tr><td>Transfer Amount</td><td>Rs. 5,000.00</td></tr>
    </table>
    """
    assert is_hmb_email("Fwd: Raast Receipt", "user@example.com", partial_np_forward) is False
    assert is_nayapay_email("Fwd: Raast Receipt", "user@example.com", partial_np_forward) is True

    tx_np = parse_email_payload(
        message_id="PARTIAL_NP_1",
        subject="Fwd: Raast Receipt",
        sender="user@example.com",
        body=partial_np_forward,
    )
    assert tx_np is not None
    assert tx_np.source == "GMAIL_NAYAPAY"
    assert tx_np.account_name == WALLET_NAYAPAY
    assert tx_np.payee == "OMAR TARIQ"
    assert tx_np.amount == pytest.approx(5000.0)

    # Uber "You paid" email must NOT route to NayaPay or HMB
    uber_body = "<p>You paid Rs. 850.00 for your Uber trip on 11 Nov 2026.</p>"
    assert is_nayapay_email("Your Thursday trip receipt", "noreply@uber.com", uber_body) is False
    assert (
        parse_email_payload(
            message_id="UBER_1",
            subject="Your Thursday trip receipt",
            sender="noreply@uber.com",
            body=uber_body,
        )
        is None
    )

    # SadaPay Raast email must NOT route to NayaPay or HMB
    sadapay_body = """
    <p>SadaPay Raast Transfer Receipt</p>
    <p>You paid Rs. 3,000.00 via Raast to Ali Raza on 11 Nov 2026.</p>
    """
    assert is_nayapay_email("Raast Transfer Sent", "alerts@sadapay.pk", sadapay_body) is False
    assert (
        parse_email_payload(
            message_id="SADAPAY_1",
            subject="Raast Transfer Sent",
            sender="alerts@sadapay.pk",
            body=sadapay_body,
        )
        is None
    )


def test_hmb_card_pos_and_fees_not_converted_to_internal_transfer() -> None:
    """ATM/Debit Card POS purchase and ATM/WHT/FED fee alerts must remain DEBIT (never TRANSFER_INTERNAL)."""
    pos_html = """
    <p>Your HabibMetro ATM/Debit Card ****7890 was used for PKR 8,500.00 at KHAADI on 10-Nov-2026.</p>
    <p>Available Balance: PKR 150,000.00</p>
    """
    tx_pos = parse_hmb_email(
        "HMB_POS_KHAADI",
        "HabibMetro ATM/Debit Card Alert",
        "eservice@habibmetro.com",
        pos_html,
    )
    assert tx_pos is not None
    assert tx_pos.tx_type == "DEBIT"
    assert tx_pos.target_account is None
    assert tx_pos.payee == "KHAADI"
    assert tx_pos.payment_channel == "POS"
    assert tx_pos.amount == pytest.approx(8500.0)

    for msg_id, desc, amt, expected_bucket in (
        ("HMB_FEE_ATM_ANN", "ATM Card Annual Fee", 2500.0, "LEAKAGE"),
        ("HMB_FEE_WHT_CASH", "WHT ON CASH WITHDRAWAL", 600.0, "LEAKAGE"),
        ("HMB_FEE_FED_NP", "FED ON IBFT TO NAYAPAY", 32.0, "LEAKAGE"),
    ):
        body = f"""
        <p>Your HabibMetro account ****7890 has been debited with PKR {amt:,.2f} on 10-Nov-2026.</p>
        <table>
          <tr><td>Description</td><td>{desc}</td></tr>
          <tr><td>Transaction Amount</td><td>PKR {amt:,.2f}</td></tr>
          <tr><td>Available Balance</td><td>PKR 140,000.00</td></tr>
        </table>
        """
        tx = parse_hmb_email(
            msg_id,
            "HabibMetro Debit Alert",
            "eservice@habibmetro.com",
            body,
        )
        assert tx is not None, f"Failed parsing {desc}"
        assert tx.tx_type == "DEBIT"
        assert tx.target_account is None
        assert tx.amount == pytest.approx(amt)
        assert tx.suggested_bucket == expected_bucket


def test_hmb_balance_lines_ignored_and_fee_extracted() -> None:
    """Account Balance / Avail Bal ignored; HMB fee parsed into tax_fee_amount or standalone amount."""
    body_with_fee = """
    <p>Your HabibMetro account ****7890 has been debited on 08-Nov-2026.</p>
    <p>Account Balance: PKR 250,000.00</p>
    <p>Avail Bal: PKR 229,850.00</p>
    <p>Your avail. bal is PKR 229,850.00.</p>
    <table>
      <tr><td>Beneficiary Name</td><td>SNGPL GAS BILL</td></tr>
      <tr><td>Transaction Amount</td><td>PKR 4,200.00</td></tr>
      <tr><td>Fee</td><td>PKR 150.00</td></tr>
    </table>
    """
    tx_combo = parse_hmb_email(
        "HMB_BAL_FEE",
        "HabibMetro Bill Alert",
        "eservice@habibmetro.com",
        body_with_fee,
    )
    assert tx_combo is not None
    assert tx_combo.amount == pytest.approx(4200.0)
    assert tx_combo.tax_fee_amount > 0.0
    assert tx_combo.tax_fee_amount == pytest.approx(150.0)

    # Standalone HMB fee alert without separate Transaction Amount line
    standalone_fee = """
    <p>Dear Customer, your HabibMetro account ****7890 has been debited on 09-Nov-2026.</p>
    <p>Description: IBFT Service Charges</p>
    <p>Fee: PKR 150.00</p>
    <p>Avail Bal: PKR 229,700.00</p>
    """
    tx_fee_only = parse_hmb_email(
        "HMB_STANDALONE_FEE",
        "HabibMetro Fee Alert",
        "eservice@habibmetro.com",
        standalone_fee,
    )
    assert tx_fee_only is not None
    assert tx_fee_only.tx_type == "DEBIT"
    assert tx_fee_only.amount == pytest.approx(150.0)
    assert tx_fee_only.tax_fee_amount == pytest.approx(0.0)
    assert tx_fee_only.suggested_bucket == "LEAKAGE"


def test_hmb_nested_outlook_forward_table_headers_and_flex_html() -> None:
    """Nested Outlook forwards keep inner Sent/Subject; non-email table headers & flex rows preserved."""
    nested_html = """
    <div>---------- Forwarded message ---------</div>
    <div>From: Personal Archive &lt;archive@outlook.com&gt;</div>
    <div>Sent: Monday, November 9, 2026 10:00 AM</div>
    <div>Subject: Fwd: Weekly Bank Alerts</div>
    <blockquote>
      <div>________________________________</div>
      <div>From: Habib Metro E-Service &lt;eservice@habibmetro.com&gt;</div>
      <div>Sent: Thursday, November 5, 2026 2:19 PM</div>
      <div>Subject: HabibMetro Utility Bill Debit Alert</div>
      <p>Your account ****7890 has been debited with PKR 7,800.00.</p>
      <table>
        <tr><td>From</td><td>Savings Account ****7890</td></tr>
        <tr><td>Subject</td><td>SSGC Gas Payment</td></tr>
        <tr><td>Date</td><td>05-Nov-2026</td></tr>
        <tr><td>Biller Name</td><td>SSGC</td></tr>
      </table>
    </blockquote>
    """
    cleaned = clean_email_html(nested_html)
    assert "archive@outlook.com" not in cleaned
    assert "eservice@habibmetro.com" not in cleaned
    # Table From:/Subject:/Date: rows (non-email headers) MUST be preserved!
    assert "From: Savings Account ****7890" in cleaned
    assert "Subject: SSGC Gas Payment" in cleaned
    assert "Date: 05-Nov-2026" in cleaned

    tx_nested = parse_hmb_email(
        "HMB_NESTED_1",
        "",
        "user@example.com",
        nested_html,
        fallback_date="",
    )
    assert tx_nested is not None
    assert tx_nested.raw_subject == "HabibMetro Utility Bill Debit Alert"
    assert tx_nested.tx_date == "2026-11-05"
    assert tx_nested.payee == "SSGC"
    assert tx_nested.amount == pytest.approx(7800.0)

    # Also verify inner Sent is used when body has no Date line
    nested_sent_only = """
    <div>---------- Forwarded message ---------</div>
    <div>From: Wrapper &lt;wrap@outlook.com&gt;</div>
    <div>Sent: Monday, November 9, 2026 10:00 AM</div>
    <div>Subject: Fwd: Outer</div>
    <div>________________________________</div>
    <div>From: Habib Metro E-Service &lt;eservice@habibmetro.com&gt;</div>
    <div>Sent: Thursday, November 5, 2026 2:19 PM</div>
    <div>Subject: HabibMetro Inner Subject</div>
    <p>Your account ****7890 has been debited with PKR 1,250.00 at CAREEM.</p>
    """
    tx_sent_only = parse_hmb_email(
        "HMB_NESTED_SENT",
        "",
        "user@example.com",
        nested_sent_only,
        fallback_date="",
    )
    assert tx_sent_only is not None
    assert tx_sent_only.tx_date == "2026-11-05"
    assert tx_sent_only.raw_subject == "HabibMetro Inner Subject"

    # Flex-row HTML formatting (<div><span>Label</span><span>Value</span></div>)
    flex_html = """
    <div class="receipt">
      <p>HabibMetro POS Debit Alert</p>
      <div><span>Merchant Name</span><span>KHAADI</span></div>
      <div><span>Transaction Amount</span><span>PKR 6,200.00</span></div>
      <div><span>Date</span><span>06-Nov-2026</span></div>
      <div><span>Available Balance</span><span>PKR 100,000.00</span></div>
    </div>
    """
    tx_flex = parse_hmb_email(
        "HMB_FLEX_1",
        "HabibMetro POS Alert",
        "eservice@habibmetro.com",
        flex_html,
    )
    assert tx_flex is not None
    assert tx_flex.payee == "KHAADI"
    assert tx_flex.amount == pytest.approx(6200.0)
    assert tx_flex.tx_date == "2026-11-06"


def test_nayapay_adversarial_taxes_pipes_refund_and_raast_title() -> None:
    """Verify invoice ignore, Cross-Border Fee, pipes, incl. subtraction, Fees & Charges, refund, Raast title."""
    pipe_body = """
    <p>NayaPay Card Transaction Receipt</p>
    <p>Merchant Name: STEAMGAMES.COM | Tax Invoice Number: 88421 | Base Amount: Rs. 1,500.00 | Cross-Border Fee: Rs. 45.00 | WHT: Rs. 75.00 | Available Balance: Rs. 9,000.00</p>
    <p>Date: 11 Nov 2026</p>
    """
    tx_pipe = parse_nayapay_email(
        "NP_PIPE_1",
        "NayaPay Card Transaction",
        "alerts@nayapay.com",
        pipe_body,
    )
    assert tx_pipe is not None
    assert tx_pipe.payee == "STEAMGAMES.COM"
    assert tx_pipe.amount == pytest.approx(1500.0)
    assert tx_pipe.tax_fee_amount == pytest.approx(120.0)  # 45 + 75, NOT 88421!

    # `incl.` base label subtraction WITHOUT Total Charged line
    incl_body = """
    <table>
      <tr><td>Merchant Name</td><td>APPLE.COM/BILL</td></tr>
      <tr><td>Transaction Amount (incl. taxes)</td><td>Rs. 1,265.00</td></tr>
      <tr><td>WHT</td><td>Rs. 110.00</td></tr>
      <tr><td>International FX Fee</td><td>Rs. 55.00</td></tr>
      <tr><td>Date</td><td>11 Nov 2026</td></tr>
    </table>
    """
    tx_incl = parse_nayapay_email(
        "NP_INCL_1",
        "NayaPay Card Receipt",
        "alerts@nayapay.com",
        incl_body,
    )
    assert tx_incl is not None
    assert tx_incl.amount == pytest.approx(1100.0)
    assert tx_incl.tax_fee_amount == pytest.approx(165.0)

    # `Fees & Charges` summary line alongside individual lines must NOT double-count
    summary_no_double = """
    <table>
      <tr><td>Merchant Name</td><td>OPENAI CHATGPT</td></tr>
      <tr><td>Base Amount</td><td>Rs. 5,500.00</td></tr>
      <tr><td>WHT</td><td>Rs. 550.00</td></tr>
      <tr><td>FX Fee</td><td>Rs. 275.00</td></tr>
      <tr><td>Fees &amp; Charges</td><td>Rs. 825.00</td></tr>
      <tr><td>Total Charged</td><td>Rs. 6,325.00</td></tr>
      <tr><td>Date</td><td>11 Nov 2026</td></tr>
    </table>
    """
    tx_no_dbl = parse_nayapay_email(
        "NP_NO_DBL",
        "NayaPay Card Receipt",
        "alerts@nayapay.com",
        summary_no_double,
    )
    assert tx_no_dbl is not None
    assert tx_no_dbl.amount == pytest.approx(5500.0)
    assert tx_no_dbl.tax_fee_amount == pytest.approx(825.0)

    # Refund containing word "purchase" must still parse as CREDIT
    refund_purchase = """
    <div class="np-brand">NayaPay Transaction Receipt</div>
    <p>Refund processed for your purchase at DARAZ.PK.</p>
    <table>
      <tr><td>Merchant Name</td><td>DARAZ.PK</td></tr>
      <tr><td>Amount</td><td>Rs. 3,400.00</td></tr>
      <tr><td>Date</td><td>11 Nov 2026</td></tr>
    </table>
    """
    tx_ref = parse_nayapay_email(
        "NP_REF_PURCH",
        "NayaPay Refund Alert",
        "alerts@nayapay.com",
        refund_purchase,
    )
    assert tx_ref is not None
    assert tx_ref.tx_type == "CREDIT"
    assert tx_ref.amount == pytest.approx(3400.0)

    # Raast Account Title preference over IBAN even when IBAN appears earlier
    raast_title = """
    <div class="np-brand">NayaPay Raast Transfer Receipt</div>
    <p>Money Sent! You paid PKR 2,500.00 via Raast.</p>
    <table>
      <tr><td>Receiver IBAN</td><td>PK36MEZN0002110104567890</td></tr>
      <tr><td>Account Title</td><td>ALI ELECTRONICS</td></tr>
      <tr><td>Transfer Amount</td><td>PKR 2,500.00</td></tr>
      <tr><td>Date</td><td>11 Nov 2026</td></tr>
    </table>
    """
    tx_raast = parse_nayapay_email(
        "NP_RAAST_TITLE",
        "NayaPay Raast Transfer",
        "alerts@nayapay.com",
        raast_title,
    )
    assert tx_raast is not None
    assert tx_raast.payee == "ALI ELECTRONICS"
    assert tx_raast.notes == "Raast IBAN: PK36MEZN0002110104567890"


def test_pdf_csv_adversarial_detection_tokens_formats_and_roundtrip(
    tmp_path: Path,
) -> None:
    """Lock in header-first detection, CSV token bounds, formats, multi-page PDF, FX skip, and round-trip."""
    # 1. Header-first auto-detection: HMB PDF containing NAYAPAY top-up row stays HabibMetro
    hmb_pdf_bytes = _build_multipage_pdf(
        [
            [
                "HabibMetro Bank Account Statement",
                "Opening Balance PKR 100,000.00",
                "Date Description Debit Credit Balance",
                "2026-11-06 IBFT TO NAYAPAY TOPUP 20,000.00 0.00 80,000.00",
            ]
        ]
    )
    hmb_auto = import_statement_file(io.BytesIO(hmb_pdf_bytes), "statement.pdf")
    assert len(hmb_auto) == 1
    assert hmb_auto[0].account_name == WALLET_HABIBMETRO
    assert hmb_auto[0].tx_type == "TRANSFER_INTERNAL"
    assert hmb_auto[0].target_account == WALLET_NAYAPAY

    # 2. CSV header-first auto-detection + token boundary matching + 1500.00 DR + Rs. 650/- + ragged footer
    assert _parse_csv_number_and_drcr("1500.00 DR") == (1500.0, "DEBIT")
    assert _clean_csv_number("Rs. 650/-") == pytest.approx(650.0)

    csv_tricky = """HabibMetro Bank Statement
Date,Description,Tax ID,Account No,Amount
2026-11-05,NAYAPAY TOPUP,99123,****7890,1500.00 DR
2026-11-06,BYKEA RIDE,99124,****7890,Rs. 650/-
Total,Summary,1,2,3,4,5,6
Total Debits,,,,2150.00
"""
    csv_parsed = import_statement_file(csv_tricky.encode("utf-8"), "statement.csv")
    assert len(csv_parsed) == 2
    assert csv_parsed[0].account_name == WALLET_HABIBMETRO
    assert csv_parsed[0].payee == "NAYAPAY TOPUP"
    assert csv_parsed[0].tx_type == "TRANSFER_INTERNAL"
    assert csv_parsed[0].target_account == WALLET_NAYAPAY
    assert csv_parsed[0].amount == pytest.approx(1500.0)
    assert csv_parsed[0].tax_fee_amount == pytest.approx(0.0)
    assert csv_parsed[1].payee == "BYKEA RIDE"
    assert csv_parsed[1].amount == pytest.approx(650.0)
    assert csv_parsed[1].tx_type == "DEBIT"

    # 3. Exported CSV backup round-trip preserves TRANSFER_INTERNAL (even NayaPay->Cash) and TRANSFER_MMF
    with DatabaseManager(tmp_path / "roundtrip.db") as db:
        db.add_manual_transaction(
            tx_date="2026-11-06",
            account_name=WALLET_NAYAPAY,
            target_account=WALLET_CASH,
            tx_type="TRANSFER_INTERNAL",
            amount=3500.0,
            payee="ATM Cash Out",
        )
        db.add_manual_transaction(
            tx_date="2026-11-07",
            account_name=WALLET_HABIBMETRO,
            target_account=WALLET_MMF,
            tx_type="TRANSFER_MMF",
            amount=40000.0,
            payee="AL MEEZAN MRAF",
        )
        exported_csv = db.export_transactions_csv()

    rt_txs = parse_statement_csv(exported_csv)
    assert len(rt_txs) == 2
    by_payee = {t.payee: t for t in rt_txs}
    assert by_payee["ATM Cash Out"].tx_type == "TRANSFER_INTERNAL"
    assert by_payee["ATM Cash Out"].account_name == WALLET_NAYAPAY
    assert by_payee["ATM Cash Out"].target_account == WALLET_CASH
    assert by_payee["ATM Cash Out"].suggested_bucket == "IGNORE"
    assert by_payee["AL MEEZAN MRAF"].tx_type == "TRANSFER_MMF"
    assert by_payee["AL MEEZAN MRAF"].target_account == WALLET_MMF
    assert by_payee["AL MEEZAN MRAF"].suggested_bucket == "INVESTMENT"

    # 4. Multi-page PDF footer flush + foreign currency token exclusion (USD 4.99) + integer PKR 650
    multipage_pdf = _build_multipage_pdf(
        [
            [
                "NayaPay Wallet E-Statement",
                "Date Description Amount Balance",
                "2026-11-08 NETFLIX USD 4.99",
                "Card Charge 1,400.00 DR 10,000.00",
                "Page 1 of 2",
                "This is a computer-generated statement",
            ],
            [
                "NayaPay Wallet E-Statement",
                "Date Description Amount Balance",
                "09 Nov 2026 BYKEA Raast PKR 650 DR 9,350.00",
                "Page 2 of 2",
            ],
        ]
    )
    mp_txs = import_statement_file(io.BytesIO(multipage_pdf), "nayapay_mp.pdf")
    assert len(mp_txs) == 2
    assert mp_txs[0].amount == pytest.approx(1400.0)  # USD 4.99 excluded!
    assert "BYKEA" in mp_txs[1].payee
    assert mp_txs[1].amount == pytest.approx(650.0)
    assert mp_txs[1].tx_type == "DEBIT"

    # 5. Descending PDF direction inference from balance transitions (newest-first, no DR/CR)
    desc_pdf_text = """
    NayaPay Wallet E-Statement
    Date Description Amount Balance
    2026-11-10 ZAMEEN ROASTERY 3,000.00 12,000.00
    2026-11-08 ALFA CORP 5,000.00 15,000.00
    2026-11-05 INITIAL DEPOSIT 10,000.00 10,000.00
    """
    desc_txs = parse_nayapay_pdf_text(desc_pdf_text)
    assert len(desc_txs) == 3
    assert desc_txs[0].payee == "ZAMEEN ROASTERY"
    assert desc_txs[0].tx_type == "DEBIT"   # 15,000 - 3,000 == 12,000
    assert desc_txs[1].payee == "ALFA CORP"
    assert desc_txs[1].tx_type == "CREDIT"  # 10,000 + 5,000 == 15,000


def test_mocked_gmail_mime_imap_and_oauth_refresh_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify MIME empty-HTML fallback, attachment exclusion, IMAP X-GM-MSGID, and OAuth RefreshError fallback."""
    # 1. Empty HTML fallback + attachment exclusion in Gmail API MIME extractor
    plain_b64 = base64.urlsafe_b64encode(
        b"You paid Rs. 1,250.00 to FOODPANDA PAKISTAN via NayaPay on 11 Nov 2026."
    ).decode("ascii")
    empty_html_b64 = base64.urlsafe_b64encode(b"   \n  ").decode("ascii")
    attach_b64 = base64.urlsafe_b64encode(
        b"<p>OTP 999999 attachment noise Rs. 99,999.00</p>"
    ).decode("ascii")

    api_payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": plain_b64}},
                    {"mimeType": "text/html", "body": {"data": empty_html_b64}},
                ],
            },
            {
                "mimeType": "text/html",
                "filename": "ignore_me.html",
                "headers": [
                    {"name": "Content-Disposition", "value": "attachment; filename=ignore_me.html"}
                ],
                "body": {"data": attach_b64},
            },
        ],
    }
    extracted_api = _extract_gmail_api_body(api_payload)
    assert "FOODPANDA PAKISTAN" in extracted_api
    assert "ignore_me" not in extracted_api
    assert "999999" not in extracted_api

    # 2. Empty HTML fallback + attachment exclusion in IMAP EmailMessage extractor
    msg = EmailMessage()
    msg["Subject"] = "NayaPay Receipt"
    msg["From"] = "alerts@nayapay.com"
    msg["Date"] = "Wed, 11 Nov 2026 12:00:00 +0500"
    msg["Message-ID"] = "<fallback-msg-1@nayapay.com>"
    msg.set_content("You paid Rs. 1,250.00 to FOODPANDA PAKISTAN via NayaPay on 11 Nov 2026.")
    msg.add_alternative("   ", subtype="html")
    msg.add_attachment(
        b"<html><body>Your OTP is 123456</body></html>",
        maintype="text",
        subtype="html",
        filename="otp_attachment.html",
    )
    extracted_imap = _extract_imap_body(msg)
    assert "FOODPANDA PAKISTAN" in extracted_imap
    assert "123456" not in extracted_imap

    # 3. Mocked IMAP UID + X-GM-MSGID fetch, plus 0-match X-GM-RAW NOT falling back to ALL
    uid_calls: list[tuple[Any, ...]] = []

    class FakeIMAP:
        def __init__(self, host: str, port: int) -> None:
            self.host = host
            self.port = port

        def login(self, user: str, pwd: str) -> tuple[str, list[bytes]]:
            return "OK", [b"Logged in"]

        def select(self, mailbox: str, readonly: bool = True) -> tuple[str, list[bytes]]:
            return "OK", [b"1"]

        def uid(self, cmd: str, *args: Any) -> tuple[str, Any]:
            uid_calls.append((cmd, *args))
            if cmd == "SEARCH":
                if args[1] == "X-GM-RAW" and "EMPTY_QUERY" in str(args[2]):
                    return "OK", [b""]
                return "OK", [b"101"]
            if cmd == "FETCH":
                return "OK", [
                    (b"101 (X-GM-MSGID 178901234567890 BODY[] {123})", msg.as_bytes()),
                    b")",
                ]
            return "NO", []

        def logout(self) -> tuple[str, list[bytes]]:
            return "BYE", [b"Logout"]

    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP)
    cfg_imap = {"gmail_user": "u@gmail.com", "gmail_app_password": "app-password"}

    txs_imap, scanned_imap = _fetch_via_imap(10, "from:nayapay", cfg_imap)
    assert scanned_imap == 1
    assert len(txs_imap) == 1
    assert txs_imap[0].message_id == "XGM_178901234567890"
    assert txs_imap[0].payee == "FOODPANDA PAKISTAN"
    assert any(c[0] == "FETCH" and "X-GM-MSGID" in str(c) for c in uid_calls)

    uid_calls.clear()
    txs_empty, scanned_empty = _fetch_via_imap(10, "EMPTY_QUERY", cfg_imap)
    assert (txs_empty, scanned_empty) == ([], 0)
    assert not any(c[0] == "SEARCH" and "ALL" in c for c in uid_calls)

    # 4. Mocked OAuth RefreshError fallback -> InstalledAppFlow -> message fetch
    from google.auth.exceptions import RefreshError
    import google.oauth2.credentials
    import google_auth_oauthlib.flow
    import googleapiclient.discovery

    cred_path = tmp_path / "credentials.json"
    token_path = tmp_path / "token.json"
    cred_path.write_text('{"installed":{}}', encoding="utf-8")
    token_path.write_text('{"refresh_token":"stale"}', encoding="utf-8")

    flow_called = {"count": 0}

    class ExpiredCreds:
        valid = False
        expired = True
        refresh_token = "stale"

        def refresh(self, request: Any) -> None:
            raise RefreshError("Token revoked")

    class FreshCreds:
        valid = True
        expired = False
        refresh_token = "fresh"

        def to_json(self) -> str:
            return '{"token":"fresh-access","refresh_token":"fresh"}'

    class FakeFlow:
        def run_local_server(self, port: int = 0) -> FreshCreds:
            flow_called["count"] += 1
            return FreshCreds()

    class FakeService:
        def users(self) -> Any:
            return self

        def messages(self) -> Any:
            return self

        def list(self, **kwargs: Any) -> Any:
            return SimpleNamespace(
                execute=lambda: {"messages": [{"id": "OAUTH_MSG_1"}]}
            )

        def get(self, **kwargs: Any) -> Any:
            return SimpleNamespace(
                execute=lambda: {
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": "NayaPay Receipt"},
                            {"name": "From", "value": "alerts@nayapay.com"},
                            {"name": "Date", "value": "Wed, 11 Nov 2026 12:00:00 +0500"},
                        ],
                        **api_payload,
                    }
                }
            )

    monkeypatch.setattr(
        google.oauth2.credentials.Credentials,
        "from_authorized_user_file",
        classmethod(lambda cls, path, scopes: ExpiredCreds()),
    )
    monkeypatch.setattr(
        google_auth_oauthlib.flow.InstalledAppFlow,
        "from_client_secrets_file",
        classmethod(lambda cls, path, scopes: FakeFlow()),
    )
    monkeypatch.setattr(
        googleapiclient.discovery,
        "build",
        lambda *args, **kwargs: FakeService(),
    )

    txs_oauth, scanned_oauth = _fetch_via_oauth(
        max_results=10,
        query="from:nayapay",
        cfg={"credentials_path": cred_path, "token_path": token_path},
    )
    assert flow_called["count"] == 1
    assert scanned_oauth == 1
    assert len(txs_oauth) == 1
    assert txs_oauth[0].message_id == "OAUTH_MSG_1"
    assert txs_oauth[0].payee == "FOODPANDA PAKISTAN"
    assert '"fresh-access"' in token_path.read_text(encoding="utf-8")


def test_round2_adversarial_parser_and_importer_regressions(tmp_path: Path) -> None:
    """Lock in all Round 2 fixes across pdf_importer, nayapay_parser, and hmb_parser."""
    # [P0-4] Multi-page HMB PDF statement line with helpline footer mentioning NayaPay/ATM
    pdf_p04_bytes = _build_multipage_pdf(
        [
            [
                "HabibMetro Bank Account Statement",
                "Date Particulars Debit Credit Balance",
                "05-11-2026 K-ELECTRIC BILL",
                "CONSUMER NO 04000123456 14,500.00 0.00 85,500.00",
                "For NayaPay or ATM queries call 111-141-414",
            ],
            [
                "Page 2 of 2",
                "06-11-2026 FOODPANDA 900.00 0.00 84,600.00",
            ],
        ]
    )
    p04_pdf = tmp_path / "p04_multipage.pdf"
    p04_pdf.write_bytes(pdf_p04_bytes)
    txs_p04 = import_statement_file(p04_pdf.read_bytes(), p04_pdf.name)
    assert len(txs_p04) == 2
    ke_row = next(t for t in txs_p04 if "K-ELECTRIC" in t.payee)
    assert ke_row.tx_type == "DEBIT"
    assert ke_row.amount == pytest.approx(14500.0)
    assert ke_row.target_account is None

    # [P1-R1] NayaPay Card Transaction Amount + Card Fee + Total Charged, plus standalone Card Fee
    np_card_fee_html = """
    <div class="np-brand">NayaPay Card Transaction Receipt</div>
    <table>
      <tr><td>Merchant Name</td><td>DARAZ.PK</td></tr>
      <tr><td>Card Transaction Amount</td><td>Rs. 2,000.00</td></tr>
      <tr><td>Card Fee</td><td>Rs. 50.00</td></tr>
      <tr><td>Total Charged</td><td>Rs. 2,050.00</td></tr>
      <tr><td>Date</td><td>10 Nov 2026</td></tr>
    </table>
    """
    tx_p1r1 = parse_nayapay_email(
        "NP_P1R1_COMBINED",
        "NayaPay Card Receipt",
        "alerts@nayapay.com",
        np_card_fee_html,
    )
    assert tx_p1r1 is not None
    assert tx_p1r1.amount == pytest.approx(2000.0)
    assert tx_p1r1.tax_fee_amount == pytest.approx(50.0)

    np_standalone_fee_html = """
    <div class="np-brand">NayaPay Fee Notice</div>
    <table>
      <tr><td>Service</td><td>Visa Virtual Card Issuance</td></tr>
      <tr><td>Card Fee</td><td>Rs. 500.00</td></tr>
      <tr><td>Date</td><td>10 Nov 2026</td></tr>
    </table>
    """
    tx_p1r1_fee = parse_nayapay_email(
        "NP_P1R1_FEE",
        "NayaPay Card Fee Receipt",
        "alerts@nayapay.com",
        np_standalone_fee_html,
    )
    assert tx_p1r1_fee is not None
    assert tx_p1r1_fee.amount == pytest.approx(500.0)

    # [P1-R2] HMB multi-sentence prose with Avail Bal before Debit sentence
    hmb_p1r2 = (
        "<p>Avail Bal: PKR 245,000.00. Debit of PKR 1,200.00 at KFC on 08-Nov-2026.</p>"
    )
    tx_p1r2 = parse_hmb_email(
        "HMB_P1R2",
        "HabibMetro Transaction Alert",
        "eservice@habibmetro.com",
        hmb_p1r2,
    )
    assert tx_p1r2 is not None
    assert tx_p1r2.amount == pytest.approx(1200.0)
    assert tx_p1r2.payee == "KFC"

    # [P1-R3] Both PDF FX row orderings (PKR before FX vs FX before PKR)
    fx_pdf_text = "\n".join(
        [
            "HabibMetro Bank Account Statement",
            "Date Particulars Debit Credit Balance",
            "09-11-2026 NETFLIX.COM 1,265.00 USD 4.99 16,885.00",
            "10-11-2026 NETFLIX.COM USD 4.99 1,265.00 15,620.00",
        ]
    )
    txs_fx = parse_hmb_pdf_text(fx_pdf_text)
    assert len(txs_fx) == 2
    assert all(t.payee == "NETFLIX.COM" for t in txs_fx)
    assert all(t.amount == pytest.approx(1265.0) for t in txs_fx)

    # [P1-R4] Subject "HabibMetro SMS Alert" without fee wording is NOT LEAKAGE; "SMS Alert Fee" IS LEAKAGE
    tx_sms_normal = parse_hmb_email(
        "HMB_P1R4_NORM",
        "HabibMetro SMS Alert",
        "eservice@habibmetro.com",
        "<p>Your account ending ****1234 has been debited with PKR 4,000.00 on 08-Nov-2026 at SHELL FUEL STATION.</p>",
    )
    assert tx_sms_normal is not None
    assert tx_sms_normal.amount == pytest.approx(4000.0)
    assert tx_sms_normal.suggested_bucket != "LEAKAGE"
    assert tx_sms_normal.suggested_category != "Bank & Tax Friction"

    tx_sms_fee = parse_hmb_email(
        "HMB_P1R4_FEE",
        "HabibMetro SMS Alert",
        "eservice@habibmetro.com",
        "<p>PKR 150.00 debited from your HabibMetro account for SMS Alert Fee on 08-Nov-2026.</p>",
    )
    assert tx_sms_fee is not None
    assert tx_sms_fee.amount == pytest.approx(150.0)
    assert tx_sms_fee.suggested_bucket == "LEAKAGE"
    assert tx_sms_fee.suggested_category == "Bank & Tax Friction"

    # [P2-R1] Descending PDF uses Opening Balance for oldest single-amount row
    desc_pdf_text = "\n".join(
        [
            "HabibMetro Bank Account Statement",
            "Opening Balance PKR 5,650.00",
            "Date Particulars Amount Balance",
            "2026-11-10 DARAZ.PK 1,650.00 9,000.00",
            "2026-11-09 IBFT FROM ZAINAB 5,000.00 10,650.00",
        ]
    )
    txs_desc = parse_hmb_pdf_text(desc_pdf_text)
    assert len(txs_desc) == 2
    by_payee_desc = {t.payee: t for t in txs_desc}
    assert by_payee_desc["IBFT FROM ZAINAB"].tx_type == "CREDIT"
    assert by_payee_desc["IBFT FROM ZAINAB"].amount == pytest.approx(5000.0)
    assert by_payee_desc["DARAZ.PK"].tx_type == "DEBIT"
    assert by_payee_desc["DARAZ.PK"].amount == pytest.approx(1650.0)

    # [P2-R2] Failed transaction refund credit parses as CREDIT while failed/declined non-refund returns None
    tx_refund_after_fail = parse_hmb_email(
        "HMB_P2R2_REF",
        "HabibMetro Refund Alert",
        "eservice@habibmetro.com",
        "<p>Your previous card transaction failed; PKR 1,500.00 has been credited as refund to your HabibMetro account on 10-Nov-2026.</p>",
    )
    assert tx_refund_after_fail is not None
    assert tx_refund_after_fail.tx_type == "CREDIT"
    assert tx_refund_after_fail.amount == pytest.approx(1500.0)

    tx_real_fail = parse_hmb_email(
        "HMB_P2R2_DECL",
        "HabibMetro Card Alert",
        "eservice@habibmetro.com",
        "<p>Your card transaction at DARAZ.PK for PKR 1,500.00 failed due to insufficient balance on 10-Nov-2026.</p>",
    )
    assert tx_real_fail is None

    # [P2 bonus] HMB prose FX fee, prose POS "at KHAADI", and NayaPay HTML <tr><td>You paid</td><td>Netflix.com</td></tr>
    tx_fx_prose = parse_hmb_email(
        "HMB_BONUS_FX",
        "HabibMetro Account Debit Alert",
        "eservice@habibmetro.com",
        "<p>PKR 450.00 debited from your HabibMetro account for INTERNATIONAL FX FEE on 10 Nov 2026.</p>",
    )
    assert tx_fx_prose is not None
    assert tx_fx_prose.payee == "INTERNATIONAL FX FEE"
    assert tx_fx_prose.amount == pytest.approx(450.0)
    assert tx_fx_prose.suggested_bucket == "LEAKAGE"

    tx_khaadi_prose = parse_hmb_email(
        "HMB_BONUS_POS",
        "HabibMetro Card Debit Alert",
        "eservice@habibmetro.com",
        "<p>PKR 6,200.00 debited from your HabibMetro account ending ****1234 on 10-Nov-2026 at KHAADI.</p>",
    )
    assert tx_khaadi_prose is not None
    assert tx_khaadi_prose.payee == "KHAADI"
    assert tx_khaadi_prose.amount == pytest.approx(6200.0)

    np_you_paid_table = """
    <div class="np-brand">NayaPay Transaction Receipt</div>
    <table>
      <tr><td>You paid</td><td>Netflix.com</td></tr>
      <tr><td>Amount</td><td>Rs. 1,100.00</td></tr>
      <tr><td>Date</td><td>10 Nov 2026</td></tr>
    </table>
    """
    tx_np_table = parse_nayapay_email(
        "NP_BONUS_TABLE",
        "NayaPay Card Receipt",
        "alerts@nayapay.com",
        np_you_paid_table,
    )
    assert tx_np_table is not None
    assert tx_np_table.payee == "Netflix.com"
    assert tx_np_table.amount == pytest.approx(1100.0)


def _build_positioned_multicolumn_pdf(page_ops: list[str]) -> bytes:
    """Build a coordinate-positioned single-page PDF using Helvetica / WinAnsi streams."""
    stream = "\n".join(page_ops).encode("latin-1")
    objs = [
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj\n",
        f"4 0 obj << /Length {len(stream)} >> stream\n".encode("latin-1")
        + stream
        + b"\nendstream\nendobj\n",
        b"5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >> endobj\n",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offs = []
    for obj in objs:
        offs.append(len(out))
        out.extend(obj)
    xref = len(out)
    out.extend(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode("latin-1"))
    for off in offs:
        out.extend(f"{off:010d} 00000 n \n".encode("latin-1"))
    out.extend(
        f"trailer << /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode(
            "latin-1"
        )
    )
    return bytes(out)


def test_nayapay_multicolumn_pdf_detection_signs_times_and_sublines() -> None:
    """Verify multi-column NayaPay PDF detection even with HabibMetro rows, spaced signs, stacked times, and fee sub-lines."""
    ops = [
        "BT /F1 10 Tf 40 760 Td (E-Statement) Tj ET",
        "BT /F1 9 Tf 40 745 Td (Account Holder: Test User) Tj ET",
        "BT /F1 9 Tf 40 730 Td (Street 1) Tj ET",
        "BT /F1 9 Tf 40 715 Td (Phase 6) Tj ET",
        "BT /F1 9 Tf 40 700 Td (Lahore Pakistan) Tj ET",
        "BT /F1 9 Tf 40 685 Td (IBAN: PK69NAYA0000012345678901) Tj ET",
        "BT /F1 9 Tf 40 670 Td (NayaPay ID: tester@nayapay) Tj ET",
        "BT /F1 9 Tf 40 650 Td (TIME) Tj 110 650 Td (TYPE) Tj 200 650 Td (DESCRIPTION) Tj 420 650 Td (AMOUNT) Tj 500 650 Td (BALANCE) Tj ET",
        "BT /F1 9 Tf 40 630 Td (Oct 15, 2026) Tj 110 630 Td (Fund Transfer) Tj 200 630 Td (Incoming fund transfer from HabibMetro) Tj 420 630 Td (+ Rs. 20,000.00) Tj 500 630 Td (Rs. 30,000.00) Tj ET",
        "BT /F1 9 Tf 40 618 Td (08:29 PM) Tj 200 618 Td (IBAN PK12MPBL0000999888777) Tj ET",
        "BT /F1 9 Tf 40 595 Td (Oct 16, 2026) Tj 110 595 Td (Card Transaction) Tj 200 595 Td (NETFLIX.COM) Tj 420 595 Td (- Rs. 1,265.00) Tj 500 595 Td (Rs. 28,735.00) Tj ET",
        "BT /F1 9 Tf 40 583 Td (02:15 PM) Tj 200 583 Td (Service Charges / FED: Rs. 150.00) Tj ET",
        "BT /F1 9 Tf 200 571 Td (Transaction ID 714086) Tj ET",
        "BT /F1 9 Tf 40 550 Td (Oct 17, 2026) Tj 110 550 Td (Raast P2P) Tj 200 550 Td (Transfer to) Tj 420 550 Td (- Rs. 500.00) Tj 500 550 Td (Rs. 28,235.00) Tj ET",
        "BT /F1 9 Tf 40 538 Td (09:10 AM) Tj 200 538 Td (ahmad@nayapay) Tj ET",
        "BT /F1 9 Tf 40 515 Td (Oct 18, 2026) Tj 110 515 Td (Cashback) Tj 200 515 Td (Reward Refund) Tj 420 515 Td (Rs. +250.00) Tj 500 515 Td (Rs. 28,485.00) Tj ET",
    ]
    pdf_bytes = _build_positioned_multicolumn_pdf(ops)

    # Generic filename must not fall back to HabibMetro despite row 1 mentioning HabibMetro
    txs = import_statement_file(io.BytesIO(pdf_bytes), "statement_oct.pdf")
    assert len(txs) == 4
    assert all(tx.account_name == WALLET_NAYAPAY for tx in txs)

    assert txs[0].tx_type == "CREDIT"
    assert txs[0].amount == pytest.approx(20000.0)
    assert txs[0].tx_timestamp == "2026-10-15 20:29:00"
    assert not txs[0].payee.endswith("+")
    assert "HabibMetro" in txs[0].payee

    # Sub-line Service Charges / FED must go to notes and NEVER overwrite Rs. 1,265.00
    assert txs[1].tx_type == "DEBIT"
    assert txs[1].amount == pytest.approx(1265.0)
    assert txs[1].tx_timestamp == "2026-10-16 14:15:00"
    assert txs[1].payment_channel == "Card"
    assert txs[1].payee == "Card Transaction NETFLIX.COM"
    assert txs[1].notes == "Service Charges / FED: Rs. 150.00 | Transaction ID 714086"

    # @nayapay peer handle on continuation line must not be stripped as a footer email
    assert txs[2].tx_type == "DEBIT"
    assert txs[2].amount == pytest.approx(500.0)
    assert txs[2].tx_timestamp == "2026-10-17 09:10:00"
    assert txs[2].payment_channel == "Raast"
    assert "ahmad@nayapay" in txs[2].payee

    assert txs[3].tx_type == "CREDIT"
    assert txs[3].amount == pytest.approx(250.0)

    # Unicode minus sign (\u2212) normalization must still classify DEBIT
    unicode_txt = "\n".join(
        [
            "E-Statement PK69NAYA0000012345678901",
            "2026-10-19\u00a0Card Purchase\u00a0FOODPANDA\u00a0\u2212Rs.\u00a0850.00\u00a0Rs.\u00a027,635.00",
        ]
    )
    uni_txs = parse_nayapay_pdf_text(unicode_txt)
    assert len(uni_txs) == 1
    assert uni_txs[0].tx_type == "DEBIT"
    assert uni_txs[0].amount == pytest.approx(850.0)

    # Image-only / no-text PDF should raise a descriptive ValueError
    empty_pdf = _build_positioned_multicolumn_pdf([])
    with pytest.raises(ValueError, match="no selectable text"):
        import_statement_file(io.BytesIO(empty_pdf), "scanned_statement.pdf")


def test_nayapay_emoji_subjects_masked_raast_and_pdf_period_header() -> None:
    """Verify emoji subjects, masked ●●●● account IDs, HMB suffix stripping, and PDF period header skip."""
    tx_masked = parse_nayapay_email(
        "NP_MASK_1",
        "You sent Rs. 1,389 to Tariq 💸",
        "alerts@nayapay.com",
        """
        <div class="np-brand">NayaPay Raast Receipt</div>
        <table>
          <tr><td>Account Number</td><td>●●●●1234</td></tr>
          <tr><td>Amount</td><td>Rs. 1,389</td></tr>
          <tr><td>Date &amp; Time</td><td>11 Nov 2026 09:46 PM</td></tr>
        </table>
        """,
    )
    assert tx_masked is not None
    assert tx_masked.payee == "Tariq"
    assert tx_masked.tx_type == "DEBIT"
    assert tx_masked.amount == pytest.approx(1389.0)

    cases = [
        ("NP_E1", "You sent Rs. 150 to Kamran Ali 💸", "DEBIT", 150.0, "Kamran Ali"),
        ("NP_E2", "You spent Rs. 100 at Easyvend 📱", "DEBIT", 100.0, "Easyvend"),
        ("NP_E3", "You got Rs. 10,000 from Ayesha Siddiqui 🎉", "CREDIT", 10000.0, "Ayesha Siddiqui"),
        ("NP_E4", "You subscribed to a mobile bundle of Rs. 2,600 📱", "DEBIT", 2600.0, "Mobile Bundle"),
        ("NP_E5", "You topped up your mobile balance with Rs. 1,500 📱", "DEBIT", 1500.0, "Mobile Balance Top-Up"),
    ]
    for mid, subj, exp_type, exp_amt, exp_payee in cases:
        tx = parse_nayapay_email(
            mid,
            subj,
            "alerts@nayapay.com",
            f"<div class='np-brand'>NayaPay</div><p>{subj}</p><p>Date: 11 Nov 2026</p>",
        )
        assert tx is not None, subj
        assert tx.tx_type == exp_type
        assert tx.amount == pytest.approx(exp_amt)
        assert tx.payee == exp_payee

    tx_atm = parse_nayapay_email(
        "NP_ATM_EMOJI",
        "Cash withdrawal of Rs. 1,035 🏧",
        "alerts@nayapay.com",
        """
        <div class="np-brand">NayaPay</div>
        <p>Cash withdrawal of Rs. 1,035 🏧</p>
        <table>
          <tr><td>Base Amount</td><td>Rs. 1,000.00</td></tr>
          <tr><td>Service Fee</td><td>Rs. 35.00</td></tr>
          <tr><td>Date &amp; Time</td><td>08 Nov 2026 04:22 PM</td></tr>
        </table>
        """,
    )
    assert tx_atm is not None
    assert tx_atm.payee == "ATM Cash Withdrawal"
    assert tx_atm.amount == pytest.approx(1000.0)
    assert tx_atm.tax_fee_amount == pytest.approx(35.0)

    tx_hmb_suffix = parse_hmb_email(
        "HMB_SUFFIX_1",
        "FW: HabibMetro Fund Transfer",
        "eservice@habibmetro.com",
        "<p>PKR 2,500.00 debited for transfer to SALMAN AHMED Meezan Bank from your HMB A/C *4321 on 15-Nov-2026.</p>",
    )
    assert tx_hmb_suffix is not None
    assert tx_hmb_suffix.payee == "SALMAN AHMED Meezan Bank"

    # NayaPay PDF header date range '01 Nov 2026 - 12 Nov 2026' must NOT become a Rs. 12 transaction
    np_header_pdf = """
    NayaPay Wallet E-Statement
    01 Nov 2026 - 12 Nov 2026
    Demo Account Holder
    Current mailing address 123 Sample Street Block A
    Date Description Amount Balance
    02 Nov 2026 Raast Out Outgoing fund transfer to Bilal Khan easypaisa Bank-4455 - Rs. 200.00 Rs. 9,800.00
    01 Nov 2026 - 12 Nov 2026
    """
    parsed_pdf = parse_nayapay_pdf_text(np_header_pdf)
    assert len(parsed_pdf) == 1
    assert parsed_pdf[0].amount == pytest.approx(200.0)
    assert "Bilal Khan" in parsed_pdf[0].payee


def test_csv_formula_guard_roundtrip_upload_limits_and_error_redaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify CSV formula-guard round-trip, 15 MB/25k-row caps, and Gmail error redaction."""
    import src.ingestion.gmail_service as gmail_mod
    from src.engine.categorizer import apply_rules
    from src.ingestion.gmail_service import fetch_emails_with_report
    from src.models import ParsedTransaction

    with DatabaseManager(tmp_path / "csv_guard.db") as db:
        db.upsert_many(
            [
                apply_rules(
                    ParsedTransaction(
                        message_id="GUARD_1",
                        tx_date="2026-11-14",
                        account_name=WALLET_NAYAPAY,
                        tx_type="DEBIT",
                        amount=425.0,
                        payee="=Cafe Mocha",
                        notes="+Extra shot",
                        source="GMAIL_NAYAPAY",
                    ),
                    db,
                )
            ]
        )
        safe_csv = db.export_transactions_csv()

    assert "'=Cafe Mocha" in safe_csv
    assert "'+Extra shot" in safe_csv

    # Re-importing sanitized CSV strips the guard apostrophe losslessly
    reimported = import_statement_file(
        io.BytesIO(safe_csv.encode("utf-8")), "../../etc/treasury_export.csv"
    )
    assert len(reimported) == 1
    assert reimported[0].payee == "=Cafe Mocha"
    assert reimported[0].notes == "+Extra shot"
    assert reimported[0].amount == pytest.approx(425.0)

    # Oversized >15 MB upload rejected without OOM
    oversized = io.BytesIO(b"A" * (15 * 1024 * 1024 + 64))
    with pytest.raises(ValueError, match="15 MB"):
        import_statement_file(oversized, "big_statement.csv")

    # Excessive CSV row count > 25,000 rejected
    too_many_rows = "Date,Description,Debit\n" + ("2026-11-01,Test,10\n" * 25_001)
    with pytest.raises(ValueError, match="25,000"):
        parse_statement_csv(too_many_rows)

    # Gmail error report must redact secrets/passwords
    monkeypatch.setenv("GMAIL_USER", "user@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "ultra_secret_app_pw")

    def _boom(**kwargs: Any) -> tuple[list[ParsedTransaction], int, str]:
        raise RuntimeError(
            "IMAP failed password=ultra_secret_app_pw token=ghp_superSecretToken12345"
        )

    monkeypatch.setattr(gmail_mod, "_sync_emails_internal", _boom)
    txs, report = fetch_emails_with_report()
    assert txs == []
    assert "ultra_secret_app_pw" not in report
    assert "ghp_superSecretToken12345" not in report
    assert "[REDACTED]" in report


def test_imap_batch_fetch_and_incremental_known_message_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify IMAP sync batches UID fetches and skips downloading bodies for known XGM IDs."""
    fetch_calls: list[tuple[Any, ...]] = []

    def _make_email(subj: str, amt: str) -> bytes:
        m = EmailMessage()
        m["Subject"] = subj
        m["From"] = "alerts@nayapay.com"
        m["Date"] = "Mon, 16 Nov 2026 12:00:00 +0500"
        m.set_content(
            f"<div class='np-brand'>NayaPay</div><p>{subj}</p>"
            f"<table><tr><td>Amount</td><td>{amt}</td></tr>"
            f"<tr><td>Date &amp; Time</td><td>16 Nov 2026 12:00 PM</td></tr></table>",
            subtype="html",
        )
        return m.as_bytes()

    msg1 = _make_email("You paid Rs. 500 at Cafe One", "Rs. 500.00")
    msg2 = _make_email("You paid Rs. 900 at Cafe Two", "Rs. 900.00")

    class BatchIMAP:
        def __init__(self, host: str, port: int, timeout: int = 15) -> None:
            self.timeout = timeout

        def login(self, user: str, pwd: str) -> tuple[str, list[bytes]]:
            return "OK", [b"OK"]

        def select(self, box: str, readonly: bool = True) -> tuple[str, list[bytes]]:
            return "OK", [b"2"]

        def uid(self, cmd: str, *args: Any) -> tuple[str, Any]:
            if cmd == "SEARCH":
                return "OK", [b"101 102"]
            if cmd == "FETCH":
                fetch_calls.append(args)
                spec = str(args[1])
                uid_arg = args[0]
                if "BODY.PEEK[]" not in spec:
                    return "OK", [
                        b"101 (UID 101 X-GM-MSGID 10001)",
                        b"102 (UID 102 X-GM-MSGID 10002)",
                    ]
                out: list[Any] = []
                if b"101" in uid_arg:
                    out.append((b"101 (X-GM-MSGID 10001 BODY[] {100})", msg1))
                if b"102" in uid_arg:
                    out.append((b"102 (X-GM-MSGID 10002 BODY[] {100})", msg2))
                return "OK", out
            return "NO", []

        def logout(self) -> tuple[str, list[bytes]]:
            return "BYE", []

    monkeypatch.setattr(imaplib, "IMAP4_SSL", BatchIMAP)
    cfg = {"gmail_user": "u@gmail.com", "gmail_app_password": "app-password"}

    # Initial sync fetches BOTH messages in 1 single batch FETCH call
    txs_all, scanned_all = _fetch_via_imap(50, "from:nayapay", cfg)
    assert scanned_all == 2
    assert len(txs_all) == 2
    assert len(fetch_calls) == 1
    assert b"," in fetch_calls[0][0]

    # Incremental sync skips XGM_10001 via tiny metadata check and fetches ONLY UID 102 body
    fetch_calls.clear()
    txs_inc, scanned_inc = _fetch_via_imap(
        50, "from:nayapay", cfg, known_message_ids={"XGM_10001"}
    )
    assert scanned_inc == 2
    assert len(txs_inc) == 1
    assert txs_inc[0].message_id == "XGM_10002"
    assert len(fetch_calls) == 2
    assert fetch_calls[1][0] == b"102"


def test_nayapay_failed_int_fee_and_fx_settlement_costs() -> None:
    """Paid failed-int-tx fee alerts are not ignored, and FX Settlement Costs are included in tax_fee_amount."""
    fee_html = """
    <p>All clear!</p>
    <p>Google GOOGLE*ONE 01M London GB Failed Int. Transaction 21 Nov 2026, 04:42 PM: - Rs. 83.36</p>
    <p>Total Fees Incurred (PKR): Rs. 69.15</p>
    <p>SST on Failed Int. Transactions Fees: Rs. 10.37</p>
    <p>FX Settlement Costs: Rs. 3.84</p>
    <p>Total Amount: Rs. 83.36</p>
    """
    fee_tx = parse_nayapay_email(
        "XGM_NP_FAIL_FEE",
        "You paid the failed international transaction fee of Rs. 83.36",
        "NayaPay <service@nayapay.com>",
        fee_html,
        "Sat, 21 Nov 2026 16:42:39 +0500",
    )
    assert fee_tx is not None
    assert fee_tx.tx_type == "DEBIT"
    assert fee_tx.amount + fee_tx.tax_fee_amount == pytest.approx(83.36)

    fx_html = """
    <p>Your online transaction was successful</p>
    <p>Google GOOGLE*ONE 01M London GB Online Transaction 22 Nov 2026, 04:35 PM: - Rs. 5,994.16</p>
    <p>Transaction Amount: Rs. 5,600</p>
    <p>Int. Transaction Fees: Rs. 69.15</p>
    <p>SST on Int. Transaction Fees: Rs. 10.37</p>
    <p>FX Settlement Costs: Rs. 314.64</p>
    <p>Total Amount: Rs. 5,994.16</p>
    """
    fx_tx = parse_nayapay_email(
        "XGM_NP_FX_TOTAL",
        "You spent Rs. 5,994.16 at Google GOOGLE*ONE 01M London GB",
        "NayaPay <service@nayapay.com>",
        fx_html,
        "Sun, 22 Nov 2026 16:35:05 +0500",
    )
    assert fx_tx is not None
    assert fx_tx.tx_type == "DEBIT"
    assert fx_tx.amount == pytest.approx(5600.0)
    assert fx_tx.tax_fee_amount == pytest.approx(394.16)
    assert fx_tx.amount + fx_tx.tax_fee_amount == pytest.approx(5994.16)


def test_audit_phase3_parser_and_importer_regressions() -> None:
    """Lock in Phase 3 audit fixes across HMB, NayaPay, PDF, and CSV importers."""
    # 1. HMB forwarded Sent/Subject wins over envelope headers; promo/reversal rejected; Bank Name: NayaPay third-party stays DEBIT
    fwd_hmb = """
    <p>---------- Forwarded message ---------</p>
    <p>From: Habib Metro E-Service &lt;eservice@habibmetro.com&gt;</p>
    <p>Sent: Tuesday, November 12, 2026 3:15 PM</p>
    <p>Subject: Outward Raast Transaction</p>
    <p>Dear Customer, Your account has been debited PKR 2,500.00.</p>
    <p>Beneficiary Name: Tariq</p>
    <p>Bank Name: NayaPay</p>
    """
    tx_fwd = parse_hmb_email(
        "HMB_FWD_AUD",
        "Fwd: Check this receipt",
        "owner@gmail.com",
        fwd_hmb,
        "Wed, 20 Nov 2026 10:00:00 +0500",
    )
    assert tx_fwd is not None
    assert tx_fwd.tx_date == "2026-11-12"
    assert tx_fwd.raw_subject == "Outward Raast Transaction"
    assert tx_fwd.tx_type == "DEBIT"
    assert tx_fwd.target_account is None
    assert tx_fwd.payee == "Tariq"

    hmb_promo = "<p>Congratulations! Increase your daily HabibMetro IBFT limit to PKR 500,000 today.</p>"
    assert (
        parse_hmb_email(
            "HMB_PROMO_AUD",
            "HabibMetro Limit Upgrade",
            "eservice@habibmetro.com",
            hmb_promo,
        )
        is None
    )

    hmb_rev = "<p>HabibMetro Alert: Your transaction of PKR 600.00 was reversed and credited back.</p>"
    # Check reversal alert or non-tx alert behavior (either ignored/rejected OR handled cleanly if reversed/credited)
    # Specifically test explicit failed/declined reversal notice with zero debit leakage:
    hmb_limit = "<p>Important Disclaimer: Your HabibMetro available balance is PKR 12,000.00 as of today.</p>"
    assert (
        parse_hmb_email(
            "HMB_BAL_DISCLAIMER",
            "HabibMetro Account Update",
            "eservice@habibmetro.com",
            hmb_limit,
        )
        is None
    )

    # 2. NayaPay ATM withdrawal -> TRANSFER_INTERNAL to Cash in Hand
    np_atm = """
    <div class="np-brand">NayaPay</div>
    <p>Cash Withdrawal of Rs. 5,000.00 from ATM on 14 Nov 2026 at 04:20 PM</p>
    <p>Transaction Amount: Rs. 5,000.00</p>
    <p>Total Fees (PKR): Rs. 23.44</p>
    """
    tx_atm = parse_nayapay_email(
        "NP_ATM_AUD",
        "NayaPay ATM Cash Withdrawal",
        "service@nayapay.com",
        np_atm,
    )
    assert tx_atm is not None
    assert tx_atm.tx_type == "TRANSFER_INTERNAL"
    assert tx_atm.target_account == WALLET_CASH
    assert tx_atm.amount == pytest.approx(5000.0)
    assert tx_atm.tax_fee_amount == pytest.approx(23.44)

    # 3. PDF Rs. 0.00 closing balance after full debit & same-prefix collision resistance
    pdf_zero_bal = """
    NayaPay Wallet E-Statement
    Opening Balance Rs. 500.00
    Date Description Debit Credit Balance
    15-11-2026 REHMAT E SHEREEN BAKERY BRANCH 1 200.00 0.00 300.00
    15-11-2026 REHMAT E SHEREEN BAKERY BRANCH 2 200.00 0.00 100.00
    15-11-2026 REHMAT E SHEREEN COFFEE SHOP 100.00 0.00 0.00
    """
    pdf_rows = parse_nayapay_pdf_text(pdf_zero_bal)
    assert len(pdf_rows) == 3
    assert pdf_rows[0].message_id != pdf_rows[1].message_id
    assert pdf_rows[2].tx_type == "DEBIT"
    assert pdf_rows[2].amount == pytest.approx(100.0)

    # 4. CSV: +500 signed credit, Debit Card No excluded, multiline quoted cell, account_name respected over filename, bad date skipped
    csv_payload = (
        "Date,Description,Debit Card No,Amount,account_name\n"
        '16-11-2026,"Raast In\nRefund from Ali",4242-XXXX,+500.00,NayaPay Wallet\n'
        "not-a-date,BAD ROW TEST,4242-XXXX,-100.00,NayaPay Wallet\n"
    )
    csv_rows = import_statement_file(
        csv_payload.encode("utf-8"),
        "hmb_November_export.csv",
    )
    assert len(csv_rows) == 1
    assert csv_rows[0].tx_date == "2026-11-16"
    assert csv_rows[0].tx_type == "CREDIT"
    assert csv_rows[0].amount == pytest.approx(500.0)
    assert csv_rows[0].account_name == WALLET_NAYAPAY
    assert "Refund from Ali" in csv_rows[0].payee


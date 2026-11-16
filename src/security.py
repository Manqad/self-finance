"""Security, authentication, rate-limiting, and zero-trust encrypted cloud vault helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import imaplib
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from email import message_from_bytes
from pathlib import Path
from typing import Any, Callable, Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

PBKDF2_ITERATIONS = 310_000
AUTH_HASH_PREFIX = "pbkdf2_sha256"
VAULT_ENVELOPE_FORMAT = "ptos-encrypted-vault"
VAULT_ENVELOPE_VERSION = 1
GIST_BACKUP_FILENAME = "treasury_vault.ptos.enc"
GMAIL_VAULT_SUBJECT = "[PTOS-ENCRYPTED-VAULT]"
_GIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

_SENSITIVE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{10,}\b"), "[REDACTED_TOKEN]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{10,}\b"), "[REDACTED_TOKEN]"),
    (re.compile(r"\bya29\.[A-Za-z0-9._-]{10,}\b"), "[REDACTED_OAUTH_TOKEN]"),
    (re.compile(r"\bAIza[A-Za-z0-9_-]{16,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._-]{6,}"), r"\1[REDACTED]"),
    (re.compile(r"(https?://)([^/\s:@]+):([^/\s@]+)@"), r"\1[REDACTED]:[REDACTED]@"),
    (
        re.compile(
            r"(?i)\b(password|app_password|passphrase|secret|token|api_key|access_token|refresh_token)"
            r"(\s*[=:]\s*)([^\s,;'\"]+)"
        ),
        r"\1\2[REDACTED]",
    ),
)


def get_secret(key: str, default: str = "") -> str:
    """Resolve a configuration secret from environment variables first, then Streamlit secrets."""
    if key in os.environ:
        val = (os.environ.get(key) or "").strip()
        return val if val else default

    try:
        import streamlit as st

        secrets = getattr(st, "secrets", None)
        if secrets is not None:
            raw = secrets.get(key)
            if raw is not None:
                val = str(raw).strip()
                if val:
                    return val
    except Exception:
        pass

    return default


def redact_sensitive_text(text: Any) -> str:
    """Mask known credentials, tokens, and password fields before logging or displaying errors."""
    if text is None:
        return ""
    cleaned = str(text)

    for env_key in (
        "GMAIL_APP_PASSWORD",
        "GITHUB_BACKUP_TOKEN",
        "TREASURY_APP_PASSWORD",
        "TREASURY_APP_PASSWORD_HASH",
        "TREASURY_BACKUP_KEY",
    ):
        secret_val = get_secret(env_key)
        if len(secret_val) >= 4 and secret_val in cleaned:
            cleaned = cleaned.replace(secret_val, "[REDACTED]")

    for pattern, replacement in _SENSITIVE_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)

    return cleaned


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Hash a password or PIN with PBKDF2-HMAC-SHA256 and a 16-byte cryptographic salt."""
    if not isinstance(password, str) or not password:
        raise ValueError("Password must be a non-empty string.")
    iters = max(100_000, int(iterations))
    salt = os.urandom(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters)
    salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii")
    dk_b64 = base64.urlsafe_b64encode(derived).decode("ascii")
    return f"{AUTH_HASH_PREFIX}${iters}${salt_b64}${dk_b64}"


def _verify_pbkdf2_hash(candidate: str, encoded_hash: str) -> bool:
    parts = encoded_hash.strip().split("$")
    if len(parts) != 4 or parts[0] != AUTH_HASH_PREFIX:
        return False
    try:
        iters = int(parts[1])
        if iters < 10_000:
            return False
        salt = base64.urlsafe_b64decode(parts[2].encode("ascii"))
        expected = base64.urlsafe_b64decode(parts[3].encode("ascii"))
    except Exception:
        return False

    derived = hashlib.pbkdf2_hmac("sha256", candidate.encode("utf-8"), salt, iters)
    return hmac.compare_digest(derived, expected)


def verify_password(candidate: str, stored_hash: Optional[str] = None) -> bool:
    """Verify a candidate password/PIN using constant-time comparison."""
    if not isinstance(candidate, str) or not candidate:
        return False

    if stored_hash is not None:
        return _verify_pbkdf2_hash(candidate, stored_hash)

    configured_hash = get_secret("TREASURY_APP_PASSWORD_HASH")
    if configured_hash:
        return _verify_pbkdf2_hash(candidate, configured_hash)

    configured_pw = get_secret("TREASURY_APP_PASSWORD")
    if configured_pw:
        cand_dk = hashlib.pbkdf2_hmac(
            "sha256",
            candidate.encode("utf-8"),
            b"ptos-auth-fallback-salt",
            PBKDF2_ITERATIONS,
        )
        cfg_dk = hashlib.pbkdf2_hmac(
            "sha256",
            configured_pw.encode("utf-8"),
            b"ptos-auth-fallback-salt",
            PBKDF2_ITERATIONS,
        )
        return hmac.compare_digest(cand_dk, cfg_dk)

    return False


def is_auth_enabled() -> bool:
    """Return True when lock-screen authentication is configured or running in cloud hosting."""
    req = get_secret("TREASURY_REQUIRE_AUTH").lower()
    if req in {"0", "false", "no", "off"}:
        return False
    if get_secret("TREASURY_APP_PASSWORD_HASH") or get_secret("TREASURY_APP_PASSWORD"):
        return True
    if req in {"1", "true", "yes", "on"}:
        return True
    return Path("/mount/src").exists() or bool(os.environ.get("STREAMLIT_SHARING_MODE"))


def get_session_timeout_seconds() -> int:
    """Return configured idle lock timeout in seconds (default 15 min, 0 disables timeout)."""
    raw = get_secret("TREASURY_SESSION_TIMEOUT_MINUTES", "15")
    try:
        minutes = float(raw)
    except ValueError:
        return 15 * 60
    if minutes <= 0:
        return 0
    return max(1, int(min(minutes, 1440.0) * 60))


class AuthRateLimiter:
    """Thread-safe exponential backoff limiter after repeated authentication failures."""

    def __init__(
        self,
        max_failures: int = 5,
        base_cooldown_seconds: int = 30,
        max_cooldown_seconds: int = 300,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_failures = max(1, int(max_failures))
        self._base_cooldown = max(1, int(base_cooldown_seconds))
        self._max_cooldown = max(self._base_cooldown, int(max_cooldown_seconds))
        self._time_fn = time_fn
        self._lock = threading.Lock()
        self._state: dict[str, tuple[int, float]] = {}

    def is_locked(self, key: str = "global") -> tuple[bool, int]:
        """Return (locked, remaining_seconds) for the given rate-limit bucket."""
        now = self._time_fn()
        with self._lock:
            failures, locked_until = self._state.get(key, (0, 0.0))
            if locked_until > now:
                remaining = int(max(1, round(locked_until - now)))
                return True, remaining
            if locked_until > 0.0 and locked_until <= now:
                # Preserve failure count so the next bad attempt escalates cooldown.
                self._state[key] = (failures, 0.0)
            return False, 0

    def record_failure(self, key: str = "global") -> int:
        """Record a failed auth attempt and return cooldown seconds applied (0 if below threshold)."""
        now = self._time_fn()
        with self._lock:
            failures, _ = self._state.get(key, (0, 0.0))
            failures += 1
            if failures < self._max_failures:
                self._state[key] = (failures, 0.0)
                return 0
            exponent = failures - self._max_failures
            cooldown = min(self._max_cooldown, self._base_cooldown * (2**exponent))
            self._state[key] = (failures, now + float(cooldown))
            return int(cooldown)

    def record_success(self, key: str = "global") -> None:
        """Clear failure tracking after a successful authentication."""
        with self._lock:
            self._state.pop(key, None)

    def failure_count(self, key: str = "global") -> int:
        """Return current consecutive failure count for a bucket."""
        with self._lock:
            return self._state.get(key, (0, 0.0))[0]

    def reset(self) -> None:
        """Clear all tracked rate-limit buckets."""
        with self._lock:
            self._state.clear()


_GLOBAL_RATE_LIMITER = AuthRateLimiter()


def get_auth_rate_limiter() -> AuthRateLimiter:
    """Return the process-wide authentication rate limiter singleton."""
    return _GLOBAL_RATE_LIMITER


def _derive_fernet_key(passphrase: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    if not isinstance(passphrase, str) or not passphrase.strip():
        raise ValueError("Encryption passphrase must be non-empty.")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    raw_key = kdf.derive(passphrase.encode("utf-8"))
    return base64.urlsafe_b64encode(raw_key)


def get_backup_passphrase() -> str:
    """Return the encryption passphrase from TREASURY_BACKUP_KEY or TREASURY_APP_PASSWORD."""
    return get_secret("TREASURY_BACKUP_KEY") or get_secret("TREASURY_APP_PASSWORD")


def encrypt_backup_payload(plaintext_json: str, passphrase: str) -> str:
    """Encrypt a UTF-8 JSON backup string using PBKDF2-HMAC-SHA256 + Fernet authenticated encryption."""
    if not isinstance(plaintext_json, str) or not plaintext_json.strip():
        raise ValueError("Backup payload must be non-empty.")
    salt = os.urandom(16)
    key = _derive_fernet_key(passphrase, salt, iterations=PBKDF2_ITERATIONS)
    token = Fernet(key).encrypt(plaintext_json.encode("utf-8"))
    envelope = {
        "format": VAULT_ENVELOPE_FORMAT,
        "version": VAULT_ENVELOPE_VERSION,
        "kdf": "pbkdf2_hmac_sha256",
        "iterations": PBKDF2_ITERATIONS,
        "cipher": "fernet_aes128_cbc_hmac_sha256",
        "salt": base64.b64encode(salt).decode("ascii"),
        "ciphertext": token.decode("ascii"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return json.dumps(envelope, indent=2)


def decrypt_backup_payload(envelope_text: str, passphrase: str) -> str:
    """Verify HMAC integrity and decrypt a PTOS encrypted vault JSON envelope."""
    if not isinstance(envelope_text, str) or not envelope_text.strip():
        raise ValueError("Encrypted backup payload is empty.")
    try:
        envelope = json.loads(envelope_text)
    except json.JSONDecodeError as exc:
        raise ValueError("Encrypted backup is not valid JSON.") from exc

    if not isinstance(envelope, dict):
        raise ValueError("Encrypted backup envelope must be a JSON object.")
    if envelope.get("format") != VAULT_ENVELOPE_FORMAT:
        raise ValueError("Unsupported encrypted backup format.")
    if envelope.get("version") != VAULT_ENVELOPE_VERSION:
        raise ValueError("Unsupported encrypted backup version.")
    if envelope.get("kdf") != "pbkdf2_hmac_sha256":
        raise ValueError("Unsupported backup KDF.")

    try:
        iterations = int(envelope.get("iterations", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid KDF iteration count in backup envelope.") from exc
    if iterations < 100_000:
        raise ValueError("Backup KDF iteration count is below minimum policy.")

    try:
        salt = base64.b64decode(str(envelope["salt"]).encode("ascii"), validate=True)
        ciphertext = str(envelope["ciphertext"]).encode("ascii")
    except Exception as exc:
        raise ValueError("Corrupted salt or ciphertext in encrypted backup.") from exc

    if len(salt) != 16:
        raise ValueError("Invalid salt length in encrypted backup.")

    key = _derive_fernet_key(passphrase, salt, iterations=iterations)
    try:
        plaintext = Fernet(key).decrypt(ciphertext)
    except InvalidToken as exc:
        raise ValueError(
            "Decryption failed: wrong backup passphrase or backup file was tampered with."
        ) from exc

    return plaintext.decode("utf-8")


def get_cloud_sync_status() -> dict[str, Any]:
    """Inspect configured zero-trust encrypted cloud vault backend without leaking secrets."""
    mode = get_secret("TREASURY_CLOUD_SYNC").lower()
    passphrase_ok = bool(get_backup_passphrase())

    if mode in {"off", "none", "disabled", "false", "0"}:
        return {
            "configured": False,
            "backend_ready": False,
            "provider": "disabled",
            "has_passphrase": passphrase_ok,
            "detail": "Cloud auto-sync is explicitly disabled (TREASURY_CLOUD_SYNC=off).",
        }

    gh_token = get_secret("GITHUB_BACKUP_TOKEN")
    gh_gist_id = get_secret("GITHUB_BACKUP_GIST_ID")
    gmail_user = get_secret("GMAIL_USER")
    gmail_app_pw = get_secret("GMAIL_APP_PASSWORD")

    if mode in {"gmail", "gmail_imap"}:
        backend_ok = bool(gmail_user and gmail_app_pw)
        ready = bool(backend_ok and passphrase_ok)
        missing = [
            name
            for name, present in (
                ("GMAIL_USER", bool(gmail_user)),
                ("GMAIL_APP_PASSWORD", bool(gmail_app_pw)),
                ("TREASURY_BACKUP_KEY/TREASURY_APP_PASSWORD", passphrase_ok),
            )
            if not present
        ]
        detail = (
            "Encrypted Gmail IMAP vault configured."
            if ready
            else f"Gmail vault missing: {', '.join(missing)}."
        )
        return {
            "configured": ready,
            "backend_ready": backend_ok,
            "provider": "gmail_imap",
            "has_passphrase": passphrase_ok,
            "detail": detail,
        }

    if gh_token and gh_gist_id:
        if not _GIST_ID_RE.match(gh_gist_id):
            return {
                "configured": False,
                "backend_ready": False,
                "provider": "github_gist",
                "has_passphrase": passphrase_ok,
                "detail": "GITHUB_BACKUP_GIST_ID format is invalid.",
            }
        detail = (
            "Encrypted Private GitHub Gist vault configured."
            if passphrase_ok
            else "GitHub Gist set, but missing TREASURY_BACKUP_KEY or TREASURY_APP_PASSWORD."
        )
        return {
            "configured": passphrase_ok,
            "backend_ready": True,
            "provider": "github_gist",
            "has_passphrase": passphrase_ok,
            "detail": detail,
        }

    if mode in {"gist", "github_gist"}:
        return {
            "configured": False,
            "backend_ready": False,
            "provider": "github_gist",
            "has_passphrase": passphrase_ok,
            "detail": "GitHub Gist vault selected, but GITHUB_BACKUP_TOKEN or GITHUB_BACKUP_GIST_ID is missing.",
        }

    return {
        "configured": False,
        "backend_ready": False,
        "provider": "unconfigured",
        "has_passphrase": passphrase_ok,
        "detail": (
            "Configure GITHUB_BACKUP_TOKEN + GITHUB_BACKUP_GIST_ID (or TREASURY_CLOUD_SYNC=gmail) "
            "plus TREASURY_BACKUP_KEY to enable zero-trust encrypted cloud persistence."
        ),
    }


def _github_gist_request(
    gist_id: str,
    token: str,
    method: str = "GET",
    payload: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if not _GIST_ID_RE.match(gist_id):
        raise ValueError("Invalid GITHUB_BACKUP_GIST_ID.")
    url = f"https://api.github.com/gists/{gist_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Personal-Treasury-OS/1.0",
    }
    data_bytes: Optional[bytes] = None
    if payload is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
        data_bytes = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"GitHub Gist API returned HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(redact_sensitive_text(f"GitHub Gist network error: {exc.reason}")) from exc


def _push_gmail_imap_vault(envelope_json: str) -> tuple[bool, str]:
    user = get_secret("GMAIL_USER")
    app_pw = get_secret("GMAIL_APP_PASSWORD")
    if not user or not app_pw:
        return False, "Gmail IMAP vault credentials missing."

    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = user
    msg["Subject"] = GMAIL_VAULT_SUBJECT
    msg["X-PTOS-Vault"] = "1"
    msg.set_content(envelope_json)

    imap: Optional[imaplib.IMAP4_SSL] = None
    try:
        ctx = ssl.create_default_context()
        imap = imaplib.IMAP4_SSL("imap.gmail.com", 993, ssl_context=ctx, timeout=10)
        imap.login(user, app_pw)
        status, _ = imap.append(
            "INBOX",
            "\\Seen",
            imaplib.Time2Internaldate(time.time()),
            msg.as_bytes(),
        )
        if status != "OK":
            return False, f"Gmail IMAP append rejected with status {status}."
        return True, "Encrypted vault pushed to Gmail IMAP self-vault."
    except Exception as exc:
        return False, redact_sensitive_text(f"Gmail IMAP vault push failed: {exc}")
    finally:
        if imap is not None:
            try:
                imap.logout()
            except Exception:
                pass


def _pull_gmail_imap_vault() -> tuple[Optional[str], str]:
    user = get_secret("GMAIL_USER")
    app_pw = get_secret("GMAIL_APP_PASSWORD")
    if not user or not app_pw:
        return None, "Gmail IMAP vault credentials missing."

    imap: Optional[imaplib.IMAP4_SSL] = None
    try:
        ctx = ssl.create_default_context()
        imap = imaplib.IMAP4_SSL("imap.gmail.com", 993, ssl_context=ctx, timeout=10)
        imap.login(user, app_pw)
        imap.select("INBOX", readonly=True)
        status, data = imap.search(None, "SUBJECT", f'"{GMAIL_VAULT_SUBJECT}"')
        if status != "OK" or not data or not data[0]:
            return None, "No encrypted Gmail vault snapshot found yet."

        msg_ids = sorted(
            (token for token in data[0].split() if token.isdigit()),
            key=lambda token: int(token),
        )
        if not msg_ids:
            return None, "No encrypted Gmail vault snapshot found yet."

        latest_id = msg_ids[-1]
        fetch_status, msg_data = imap.fetch(latest_id, "(RFC822)")
        if fetch_status != "OK" or not msg_data:
            return None, "Failed to read Gmail encrypted vault message."

        raw_bytes: Optional[bytes] = None
        for item in msg_data:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
                raw_bytes = bytes(item[1])
                break
        if not raw_bytes:
            return None, "Empty Gmail encrypted vault message."

        parsed = message_from_bytes(raw_bytes)
        payload = parsed.get_payload(decode=True)
        if isinstance(payload, (bytes, bytearray)):
            envelope_text = payload.decode("utf-8", errors="replace").strip()
        else:
            envelope_text = str(parsed.get_payload() or "").strip()

        if not envelope_text:
            return None, "Gmail encrypted vault message had no payload."
        return envelope_text, "Encrypted vault pulled from Gmail IMAP."
    except Exception as exc:
        return None, redact_sensitive_text(f"Gmail IMAP vault pull failed: {exc}")
    finally:
        if imap is not None:
            try:
                imap.logout()
            except Exception:
                pass


def push_encrypted_cloud_backup(envelope_json: str) -> tuple[bool, str]:
    """Push an encrypted vault JSON envelope to the configured cloud vault backend."""
    status = get_cloud_sync_status()
    if not status.get("backend_ready"):
        return False, status["detail"]

    provider = status["provider"]
    if provider == "github_gist":
        token = get_secret("GITHUB_BACKUP_TOKEN")
        gist_id = get_secret("GITHUB_BACKUP_GIST_ID")
        try:
            _github_gist_request(
                gist_id=gist_id,
                token=token,
                method="PATCH",
                payload={
                    "description": "Personal Treasury OS — Encrypted Vault Backup",
                    "files": {GIST_BACKUP_FILENAME: {"content": envelope_json}},
                },
            )
            return True, "Encrypted vault synced to Private GitHub Gist."
        except Exception as exc:
            return False, redact_sensitive_text(f"GitHub Gist vault push failed: {exc}")

    if provider == "gmail_imap":
        return _push_gmail_imap_vault(envelope_json)

    return False, f"Unsupported cloud vault provider: {provider}"


def pull_encrypted_cloud_backup() -> tuple[Optional[str], str]:
    """Pull the latest encrypted vault JSON envelope from the configured cloud vault backend."""
    status = get_cloud_sync_status()
    if not status.get("backend_ready"):
        return None, status["detail"]

    provider = status["provider"]
    if provider == "github_gist":
        token = get_secret("GITHUB_BACKUP_TOKEN")
        gist_id = get_secret("GITHUB_BACKUP_GIST_ID")
        try:
            gist = _github_gist_request(gist_id=gist_id, token=token, method="GET")
            files = gist.get("files") or {}
            file_entry = files.get(GIST_BACKUP_FILENAME)
            if not file_entry:
                return None, "GitHub Gist does not yet contain a vault snapshot."
            if file_entry.get("truncated") and file_entry.get("raw_url"):
                raw_url = str(file_entry["raw_url"])
                if not raw_url.startswith("https://"):
                    return None, "Refusing non-HTTPS Gist raw_url."
                req = urllib.request.Request(
                    raw_url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "User-Agent": "Personal-Treasury-OS/1.0",
                    },
                )
                with urllib.request.urlopen(req, timeout=10, context=ssl.create_default_context()) as resp:
                    content = resp.read().decode("utf-8", errors="replace").strip()
            else:
                content = str(file_entry.get("content") or "").strip()

            if not content:
                return None, "GitHub Gist vault file is empty."
            return content, "Encrypted vault pulled from Private GitHub Gist."
        except Exception as exc:
            return None, redact_sensitive_text(f"GitHub Gist vault pull failed: {exc}")

    if provider == "gmail_imap":
        return _pull_gmail_imap_vault()

    return None, f"Unsupported cloud vault provider: {provider}"

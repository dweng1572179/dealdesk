"""Email loop — the open answer to Lev's dedicated email microservice + Gmail/
Outlook OAuth. Here it's just Python's stdlib imaplib + smtplib against any inbox
you bring (Gmail/Outlook/Fastmail all speak IMAP+SMTP). Gmail/Outlook need an
*App Password*, not your login password. No OAuth, no third-party service.

ponytail: stdlib IMAP/SMTP, pulled on demand from a 'Sync inbox' button — not a
push webhook. Fine for one desk checking a few times a day; add IDLE/polling only
if you need sub-minute inbound latency."""
import email
import imaplib
import logging
import smtplib
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parseaddr

from .config import settings

log = logging.getLogger("dealdesk")


def configured() -> bool:
    return bool(settings.email_user and settings.email_password)


def _decode(raw) -> str:
    try:
        return str(make_header(decode_header(raw or "")))
    except Exception:  # noqa: BLE001 — never let a weird header kill a sync
        return raw or ""


def send(to: str, subject: str, body: str) -> None:
    if not configured():
        raise RuntimeError("Email not configured — set your address + App Password in Settings.")
    msg = EmailMessage()
    msg["From"] = settings.email_from or settings.email_user
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    # Port 465 = implicit TLS (Gmail/Fastmail). Anything else (587) = STARTTLS
    # (Outlook/Office365) — SMTP_SSL against a plaintext 587 banner just SSL-errors.
    if settings.smtp_port == 465:
        smtp = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30)
    else:
        smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
        smtp.starttls()
    with smtp as s:
        s.login(settings.email_user, settings.email_password)
        s.send_message(msg)


def test_connection() -> None:
    """Log in to both IMAP and SMTP without sending or reading anything — proves the
    credentials + host/port before the user relies on them. Raises on any failure."""
    if not configured():
        raise RuntimeError("Email not configured — set your address + App Password in Settings.")
    imap = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    try:
        imap.login(settings.email_user, settings.email_password)
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass
    # SMTP: same 465-implicit-TLS / else-STARTTLS split as send(), login only (NOOP, no mail).
    if settings.smtp_port == 465:
        smtp = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30)
    else:
        smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
        smtp.starttls()
    with smtp as s:
        s.login(settings.email_user, settings.email_password)


def _safe_decode(payload: bytes, charset: str | None) -> str:
    """Decode bytes with the declared charset, tolerating a bogus/unregistered codec
    name — errors='replace' does NOT catch that (Python raises LookupError before
    decoding), and one weird message must not abort the whole sync."""
    try:
        return payload.decode(charset or "utf-8", errors="replace")
    except (LookupError, TypeError):
        return payload.decode("utf-8", errors="replace")


def _body_text(msg: email.message.Message) -> str:
    """First text/plain part, ignoring attachments; falls back to any text part."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(
                    part.get("Content-Disposition", "")):
                payload = part.get_payload(decode=True)
                if payload:
                    return _safe_decode(payload, part.get_content_charset())
        return ""
    payload = msg.get_payload(decode=True)
    return _safe_decode(payload, msg.get_content_charset()) if payload else ""


def fetch(limit: int = 15) -> list[dict]:
    """Pull the most recent `limit` messages from the inbox. Read-only (PEEK — does
    not mark them seen). Returns newest first."""
    if not configured():
        raise RuntimeError("Email not configured — set your address + App Password in Settings.")
    out: list[dict] = []
    imap = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    try:
        imap.login(settings.email_user, settings.email_password)
        imap.select("INBOX", readonly=True)
        typ, data = imap.search(None, "ALL")
        if typ != "OK" or not data or not data[0]:
            return []
        ids = data[0].split()[-limit:]
        for num in reversed(ids):  # newest first
            typ, msg_data = imap.fetch(num, "(BODY.PEEK[])")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            from_name, from_email = parseaddr(_decode(msg.get("From")))
            out.append({
                "from": from_name or from_email,
                "from_email": from_email.lower(),
                "subject": _decode(msg.get("Subject")) or "(no subject)",
                "date": _decode(msg.get("Date")),
                "body": _body_text(msg).strip()[:8000],
            })
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass
    return out


def demo() -> None:
    # offline check: unconfigured send/fetch raise a clear message, not a socket error.
    settings.email_user = settings.email_password = ""
    for fn in (lambda: send("a@b.com", "s", "b"), lambda: fetch()):
        try:
            fn(); raise AssertionError("should have raised")
        except RuntimeError as e:
            assert "not configured" in str(e), e
    # header decoding survives RFC2047 + junk
    assert _decode("=?utf-8?q?Hello?=") == "Hello"
    assert _decode(None) == ""
    # an unregistered charset must not raise (errors='replace' doesn't cover a bad
    # codec NAME) — it falls back to utf-8 rather than aborting the whole sync.
    assert _safe_decode(b"hi there", "x-unknown-8bit") == "hi there"
    assert _safe_decode(b"\xff\xfe", "totally-made-up") != ""
    print("inbox.demo OK")


if __name__ == "__main__":
    demo()

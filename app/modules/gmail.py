"""Read Redfin alert emails from Gmail over IMAP (app password, read-only, never marks as read)."""
from __future__ import annotations

import email
import imaplib
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from email import policy

log = logging.getLogger(__name__)
GMAIL_IMAP = "imap.gmail.com"


@dataclass(frozen=True)
class Mail:
    message_id: str
    subject: str
    sender: str
    html: str


def _html_of(msg: email.message.EmailMessage) -> str:
    body = msg.get_body(("html",))
    return body.get_content() if body else ""


def fetch_mail(address: str, app_password: str, sender_filter: str, since_days: int,
               skip_ids: set[str], mailbox: str = "INBOX") -> list[Mail]:
    since = (date.today() - timedelta(days=since_days)).strftime("%d-%b-%Y")
    out: list[Mail] = []
    with imaplib.IMAP4_SSL(GMAIL_IMAP) as imap:
        imap.login(address, app_password)
        imap.select(f'"{mailbox}"', readonly=True)
        typ, data = imap.search(None, "FROM", f'"{sender_filter}"', "SINCE", since)
        if typ != "OK":
            raise RuntimeError(f"IMAP search failed: {typ}")
        for num in data[0].split():
            typ, parts = imap.fetch(num, "(BODY.PEEK[])")
            if typ != "OK" or not parts or not isinstance(parts[0], tuple):
                continue
            msg = email.message_from_bytes(parts[0][1], policy=policy.default)
            mid = (msg["Message-ID"] or f"imap:{num.decode()}").strip()
            if mid in skip_ids:
                continue
            out.append(Mail(mid, str(msg["Subject"] or ""), str(msg["From"] or ""), _html_of(msg)))
    log.info("Fetched %d new %s emails", len(out), sender_filter)
    return out

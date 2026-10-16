"""Read emails from Gmail over IMAP (imap.gmail.com, SSL, app password).

Read-only by design: mailboxes are opened with SELECT ... readonly and bodies are fetched with
BODY.PEEK, so nothing is ever marked as read, moved or deleted. One Session = one login; callers do all
their searches inside it. For each search we first fetch only Message-ID headers (one batched FETCH),
then download full bodies only for messages we haven't processed before.
"""
from __future__ import annotations

import email
import imaplib
import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from email import policy

log = logging.getLogger(__name__)
GMAIL_IMAP = "imap.gmail.com"
SPAM = "[Gmail]/Spam"
MSGID_RE = re.compile(rb"Message-ID:\s*(<[^>\r\n]+>)", re.I)


@dataclass(frozen=True)
class Mail:
    message_id: str
    subject: str
    sender: str
    html: str


def _body_of(msg: email.message.EmailMessage) -> str:
    body = msg.get_body(("html",)) or msg.get_body(("plain",))
    return body.get_content() if body else ""


class Session:
    """`with gmail.Session(addr, app_password) as s: s.search([...], ...)` — one IMAP login."""

    def __init__(self, address: str, app_password: str):
        self.address, self.app_password = address, app_password
        self.imap: imaplib.IMAP4_SSL | None = None
        self.stats = {"searches": 0, "headers": 0, "bodies": 0}

    def __enter__(self) -> Session:
        self.imap = imaplib.IMAP4_SSL(GMAIL_IMAP)
        self.imap.login(self.address, self.app_password)
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self.imap:
                self.imap.logout()
        finally:
            log.info("Gmail session: %(searches)d searches, %(headers)d headers, %(bodies)d bodies", self.stats)

    def search(self, criteria: list[str], since_days: int, skip_ids: set[str],
               mailboxes: tuple[str, ...] = ("INBOX",)) -> list[Mail]:
        """New (not in skip_ids) messages matching IMAP `criteria`, e.g. ['FROM', '"redfin.com"']."""
        assert self.imap, "use inside `with Session(...)`"
        since = (date.today() - timedelta(days=since_days)).strftime("%d-%b-%Y")
        out: list[Mail] = []
        seen = set(skip_ids)
        for box in mailboxes:
            typ, _ = self.imap.select(f'"{box}"', readonly=True)
            if typ != "OK":
                log.warning("Gmail mailbox %s not available", box)
                continue
            typ, data = self.imap.search(None, *criteria, "SINCE", since)
            self.stats["searches"] += 1
            if typ != "OK":
                raise RuntimeError(f"IMAP search failed in {box}: {typ}")
            nums = data[0].split()
            if not nums:
                continue
            # Pass 1: Message-IDs only, one batched request.
            typ, parts = self.imap.fetch(b",".join(nums), "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
            new_nums = []
            for part in parts:
                if not isinstance(part, tuple):
                    continue
                self.stats["headers"] += 1
                num = part[0].split()[0]
                m = MSGID_RE.search(part[1])
                mid = m.group(1).decode(errors="replace") if m else f"imap:{box}:{num.decode()}"
                if mid not in seen:
                    seen.add(mid)
                    new_nums.append((num, mid))
            # Pass 2: full bodies for new messages only.
            for num, mid in new_nums:
                typ, body = self.imap.fetch(num, "(BODY.PEEK[])")
                if typ != "OK" or not body or not isinstance(body[0], tuple):
                    continue
                self.stats["bodies"] += 1
                msg = email.message_from_bytes(body[0][1], policy=policy.default)
                out.append(Mail(mid, str(msg["Subject"] or ""), str(msg["From"] or ""), _body_of(msg)))
        return out

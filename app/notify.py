"""Send Telegram messages to profiles, splitting long HTML messages safely."""
from __future__ import annotations

import io
import logging

from telegram import Bot
from telegram.constants import ParseMode

from app.db import DB

log = logging.getLogger(__name__)
TELEGRAM_LIMIT = 4096


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split on line boundaries so HTML tags (always single-line here) are never cut."""
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:  # pathological single line
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


class Notifier:
    def __init__(self, bot: Bot, db: DB):
        self.bot = bot
        self.db = db

    async def send_text(self, profiles: list[str], html_text: str) -> int:
        sent = 0
        for chat_id in self.db.chat_ids_for(profiles):
            for chunk in split_message(html_text):
                await self.bot.send_message(chat_id, chunk, parse_mode=ParseMode.HTML,
                                            disable_web_page_preview=True)
            sent += 1
        if not sent:
            log.warning("No registered chats for profiles %s; message dropped", profiles)
        return sent

    async def send_photo(self, profiles: list[str], data: bytes, caption_html: str) -> int:
        sent = 0
        for chat_id in self.db.chat_ids_for(profiles):
            await self.bot.send_photo(chat_id, io.BytesIO(data), caption=caption_html, parse_mode=ParseMode.HTML)
            sent += 1
        return sent

    async def send_document(self, profiles: list[str], data: bytes, filename: str, caption: str) -> int:
        sent = 0
        for chat_id in self.db.chat_ids_for(profiles):
            await self.bot.send_document(chat_id, io.BytesIO(data), filename=filename, caption=caption)
            sent += 1
        if not sent:
            log.warning("No registered chats for profiles %s; document dropped", profiles)
        return sent

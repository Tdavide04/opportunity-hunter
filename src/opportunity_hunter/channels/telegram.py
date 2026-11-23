"""Telegram Channel: send-only (sendPhoto with caption, falling back to sendMessage).

It never reads updates from the bot: SubitoScanner's bot polls the same token during the
migration, and two consumers on one token conflict (ADR-0003).
"""
from html import escape
from typing import Any

import requests

from opportunity_hunter.channels.base import ChannelError, OutgoingMessage
from opportunity_hunter.logging_utils import mask_secrets

API = "https://api.telegram.org"
CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096
TIMEOUT_S = 20


class TelegramChannel:
    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str, session: Any | None = None):
        self._token = bot_token
        self._chat_id = chat_id
        self._session = session if session is not None else requests.Session()

    def send(self, message: OutgoingMessage) -> None:
        if message.image_url:
            try:
                self._call("sendPhoto", photo=message.image_url, caption=render(message, CAPTION_LIMIT))
                return
            except _PhotoRejected:
                pass  # Telegram could not fetch or use the image: the text still has the link
        self._call("sendMessage", text=render(message, TEXT_LIMIT), disable_web_page_preview=False)

    def _call(self, method: str, **fields: Any) -> None:
        url = f"{API}/bot{self._token}/{method}"
        try:
            response = self._session.post(url, json={"chat_id": self._chat_id, "parse_mode": "HTML", **fields}, timeout=TIMEOUT_S)
        except requests.RequestException as exc:
            raise ChannelError(self._clean(f"Telegram unreachable: {type(exc).__name__}: {exc}")) from None
        if response.status_code == 200:
            return
        try:
            description = response.json().get("description", "")
        except ValueError:
            description = ""
        if method == "sendPhoto" and response.status_code == 400:
            raise _PhotoRejected(description)
        raise ChannelError(self._clean(f"Telegram {method} failed: HTTP {response.status_code} {description}".strip()))

    def _clean(self, text: str) -> str:
        return mask_secrets(text, (self._token,))


class _PhotoRejected(Exception):
    pass


def render(message: OutgoingMessage, limit: int) -> str:
    """HTML message; the title is shortened first so the link and the price always fit within `limit`."""
    details = " · ".join(escape(part) for part in (message.price, message.location) if part)
    tail = (
        f"\n{details}\nScore {message.score} · {escape(message.search_profile)}"
        f'\n<a href="{escape(message.url, quote=True)}">Apri l\'annuncio</a>'
    )
    if message.note:
        tail = f"\n<i>{escape(message.note)}</i>" + tail
    room = limit - len(tail) - len("<b></b>")
    raw = message.title
    if len(escape(raw)) > room:
        # cut the raw text, not the escaped one, so no HTML entity is ever split
        while raw and len(escape(raw)) + 1 > room:
            raw = raw[: -max(1, (len(escape(raw)) + 1 - room) // 6)]
        return f"<b>{escape(raw)}…</b>{tail}"
    return f"<b>{escape(raw)}</b>{tail}"

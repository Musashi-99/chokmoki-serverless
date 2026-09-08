from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from src.plugins.logger import logger
from src.services.msg91_service import Msg91Service
from src.services.telegram_service import TelegramService


class NotificationChannel(ABC):
    """Where a formatted alert message actually gets delivered. Handlers
    depend on this, not on Telegram directly — adding Slack/email later is a
    new channel class, with no changes to src/alerts/handlers.py.
    """

    @abstractmethod
    async def send(self, text: str, extra_chat_ids: Optional[List[str]] = None) -> bool: ...


class TelegramChannel(NotificationChannel):
    def __init__(self) -> None:
        self._telegram = TelegramService()

    def is_enabled(self) -> bool:
        return self._telegram.is_enabled()

    async def send(self, text: str, extra_chat_ids: Optional[List[str]] = None) -> bool:
        """Sends to the global chat first (unchanged behavior/return value),
        then best-effort mirrors the same text to any extra per-admin/region
        chat ids — a secondary send failing never fails the primary send,
        matching TelegramService's own "never raise" contract."""
        if not self._telegram.is_enabled():
            return False
        sent = await self._telegram.send_message(text)

        for chat_id in extra_chat_ids or []:
            if not chat_id:
                continue
            try:
                await self._telegram.send_message(text, chat_id=chat_id)
            except Exception as exc:
                if logger:
                    logger.warning(f"Secondary Telegram send to {chat_id} failed: {exc}")

        return sent


class SmsChannel:
    """Customer-facing lifecycle SMS via MSG91 — deliberately NOT a
    NotificationChannel (its send() needs a phone number + template key +
    variables, not free-text), so it's a sibling dependency the two
    customer-lifecycle handlers (OrderCreatedHandler, ShipmentUpdateHandler)
    take in addition to their NotificationChannel, not a drop-in replacement.
    """

    def __init__(self) -> None:
        self._msg91 = Msg91Service()

    def is_enabled(self) -> bool:
        return self._msg91.is_enabled()

    async def send(self, phone: str, template_key: str, variables: Dict[str, str]) -> bool:
        if not phone or not self._msg91.is_enabled():
            return False
        return await self._msg91.send_template(phone, template_key, variables)

from typing import Optional

from telegram import Bot
from telegram.error import NetworkError, RetryAfter, TelegramError, TimedOut

from src.config import settings
from src.plugins.logger import logger
from src.resilience.circuit_breaker import CircuitBreaker
from src.resilience.guarded import call_guarded

TELEGRAM_BREAKER = CircuitBreaker(
    "telegram", failure_threshold=5, failure_window_seconds=60, cooldown_seconds=30
)


class TelegramService:
    """Thin wrapper around the Telegram Bot API. Real-time alert formatting
    and dispatch logic lives in src/alerts/ (Chain of Responsibility over a
    Redis Streams consumer) — this class only knows how to check whether
    Telegram is configured and send a single message.
    """

    def is_enabled(self) -> bool:
        if not settings.telegram_enabled:
            return False
        if not settings.telegram_bot_token or not settings.telegram_chat_id:
            return False
        return True

    async def send_message(self, text: str, chat_id: Optional[str] = None) -> bool:
        """Send message to Telegram using python-telegram-bot SDK. Never
        raises — a lost alert is genuinely low-stakes (unlike a lost
        payment/shipment event), so the public contract stays "return False
        on any failure" exactly as before; timeout/retry/breaker/bulkhead
        are added underneath without changing that contract. A retried send
        that actually succeeded the first time just means a duplicate
        Telegram message — an acceptable, low-severity tradeoff for not
        losing the notification.

        `chat_id`, when provided, targets that chat instead of the global
        `settings.telegram_chat_id` — used for per-admin/region routing
        (src/alerts/handlers.py). The bot/token itself is still the single
        global one; `is_enabled()` gates on that, not on `chat_id`.
        """
        if not self.is_enabled():
            return False

        target_chat_id = chat_id or settings.telegram_chat_id

        async def _send() -> bool:
            bot = Bot(token=settings.telegram_bot_token)
            # Deliberately NO parse_mode (plain text): Telegram's legacy
            # Markdown parser rejects the ENTIRE message on any unescaped/
            # unbalanced special character (_ * ` [) anywhere in dynamic
            # content — product names, stock status strings like
            # "out_of_stock", dict keys in a debug context, admin emails —
            # which is exactly what caused repeated "Can't parse entities"
            # failures (and DLQ drops) even after multiple rounds of manual
            # per-field escaping in src/alerts/handlers.py. Plain text has
            # no entities to parse, so this eliminates the entire bug class
            # rather than requiring every new formatter to remember to
            # escape every new field forever.
            await bot.send_message(
                chat_id=target_chat_id,
                text=text,
                disable_web_page_preview=False,
            )
            return True

        try:
            return await call_guarded(
                dependency="telegram",
                fn=_send,
                breaker=TELEGRAM_BREAKER,
                timeout_seconds=10.0,
                bulkhead_limit=5,
                retries=2,
                retryable_exceptions=(NetworkError, TimedOut, RetryAfter),
            )
        except TelegramError as e:
            if logger:
                logger.error(f"Telegram API error: {e}")
            return False
        except Exception as e:
            if logger:
                logger.error(f"Failed to send Telegram message: {e}")
            return False

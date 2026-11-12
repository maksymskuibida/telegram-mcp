"""The Telegram gateway: the only way any component reaches Telegram (C3)."""

from telegram_mcp.gateway.models import (
    BadCode,
    BadPassword,
    ChatNotFound,
    ChatRef,
    CodeExpired,
    FloodWait,
    GatewayError,
    GatewayTimeout,
    Me,
    NotAuthorized,
    OutcomeUnknown,
    PasswordNeeded,
    QrExpired,
    QrToken,
    SentMessage,
    TelegramError,
)
from telegram_mcp.gateway.protocol import GatewayFactory, TelegramGateway

__all__ = [
    "BadCode",
    "BadPassword",
    "ChatNotFound",
    "ChatRef",
    "CodeExpired",
    "FloodWait",
    "GatewayError",
    "GatewayFactory",
    "GatewayTimeout",
    "Me",
    "NotAuthorized",
    "OutcomeUnknown",
    "PasswordNeeded",
    "QrExpired",
    "QrToken",
    "SentMessage",
    "TelegramError",
    "TelegramGateway",
]

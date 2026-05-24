"""События GGSEL (по аналогии с FunPayAPI EventTypes).

Плагины подписываются на события через списки на уровне модуля:

    BIND_TO_PRE_INIT = [...]
    BIND_TO_NEW_ORDER = [...]
    BIND_TO_NEW_MESSAGE = [...]
    BIND_TO_LAST_CHAT_MESSAGE_CHANGED = [...]
    BIND_TO_ORDER_STATUS_CHANGED = [...]
    BIND_TO_DELETE = [...]
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .types import Order, Message, ChatShortcut


class EventType(str, Enum):
    PRE_INIT = "PRE_INIT"
    POST_INIT = "POST_INIT"
    NEW_ORDER = "NEW_ORDER"
    ORDER_STATUS_CHANGED = "ORDER_STATUS_CHANGED"
    NEW_MESSAGE = "NEW_MESSAGE"
    LAST_CHAT_MESSAGE_CHANGED = "LAST_CHAT_MESSAGE_CHANGED"
    PLUGIN_LOAD = "PLUGIN_LOAD"
    PLUGIN_UNLOAD = "PLUGIN_UNLOAD"
    SHUTDOWN = "SHUTDOWN"


@dataclass
class BaseEvent:
    type: EventType
    raw: dict[str, Any] | None = None


@dataclass
class NewOrderEvent(BaseEvent):
    order: Order | None = None


@dataclass
class OrderStatusChangedEvent(BaseEvent):
    order: Order | None = None
    old_status: str = ""
    new_status: str = ""


@dataclass
class NewMessageEvent(BaseEvent):
    message: Message | None = None


@dataclass
class LastChatMessageChangedEvent(BaseEvent):
    chat: ChatShortcut | None = None
    message: Message | None = None

"""События GGSEL API runner.

Совместимы по именам с FunPayCardinal, чтобы плагины, написанные под FPC,
работали без правок.
"""
from enum import Enum


class EventTypes(str, Enum):
    INITIAL_CHAT = "initial_chat"
    NEW_MESSAGE = "new_message"
    LAST_CHAT_MESSAGE_CHANGED = "last_chat_message_changed"
    NEW_ORDER = "new_order"
    ORDER_STATUS_CHANGED = "order_status_changed"

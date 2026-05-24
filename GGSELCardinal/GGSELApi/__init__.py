"""GGSEL marketplace API wrapper.

Структура повторяет FunPayAPI: Account, Runner, типы, события.
"""
from .account import Account
from .runner import Runner
from .events import EventTypes
from .types import OrderShortcut, MessageShortcut, LotShortcut, LotFields

__all__ = [
    "Account",
    "Runner",
    "EventTypes",
    "OrderShortcut",
    "MessageShortcut",
    "LotShortcut",
    "LotFields",
]

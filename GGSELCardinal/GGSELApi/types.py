"""Типы данных GGSEL API.

Совместимы по полям с FunPayAPI там, где это возможно — чтобы плагины,
писавшиеся под FPC, требовали минимальной адаптации.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class OrderShortcut:
    """Краткое представление заказа GGSEL.

    Совместимый набор полей с `FunPayAPI.types.OrderShortcut`:
      id, title, description, price, buyer_username, buyer_id,
      chat_id, chat_name, amount, lot_id, status.
    """
    id: str
    title: str = ""
    description: str = ""
    price: float = 0.0
    buyer_username: str = ""
    buyer_id: str = ""
    chat_id: str = ""
    chat_name: str = ""
    amount: int = 1
    lot_id: str = ""
    status: str = "new"
    raw: dict = field(default_factory=dict)


@dataclass
class MessageShortcut:
    id: str
    chat_id: str
    chat_name: str
    author_id: str
    author_username: str
    text: str
    is_my: bool = False
    raw: dict = field(default_factory=dict)


@dataclass
class LotShortcut:
    """Лот (товар) продавца GGSEL."""
    id: str
    title: str = ""
    price: float = 0.0
    subcategory_id: str = ""
    active: bool = True
    raw: dict = field(default_factory=dict)


@dataclass
class LotFields:
    """Редактируемые поля лота, аналог FunPayAPI.LotFields."""
    lot_id: str
    fields: dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        return self.fields[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.fields[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.fields.get(key, default)

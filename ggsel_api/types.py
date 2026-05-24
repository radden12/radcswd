"""Доменные типы GGSEL.

Спроектированы так, чтобы быть совместимыми по полям с FunPayAPI: плагин,
написанный для FunPay, должен суметь читать ``order.id``, ``order.title``,
``order.amount``, ``order.price``, ``order.buyer_id``, ``order.buyer_username``
и т. д. без изменений.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Order:
    """Заказ GGSEL.

    Поля специально названы как в FunPayAPI: id (строка), title, amount,
    price, buyer_id, buyer_username, lot_id, status.
    """
    id: str
    title: str = ""
    amount: int = 1
    price: float = 0.0
    buyer_id: int | str = 0
    buyer_username: str = ""
    lot_id: int | str = 0
    status: str = "new"
    chat_id: int | str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Message:
    id: int | str
    chat_id: int | str
    chat_name: str = ""
    author_id: int | str = 0
    author: str = ""
    text: str = ""
    is_my_message: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChatShortcut:
    """Краткая инфа о чате — нужна для совместимости с runner-ом."""
    id: int | str
    name: str = ""
    last_message_text: str = ""
    last_message_id: int | str = 0
    unread: bool = False


@dataclass
class LotFields:
    """Поля редактируемого лота на GGSEL.

    Заполняются ``GGSELAccount.get_lot_fields`` и сохраняются через
    ``GGSELAccount.save_lot``. Доступ к произвольным полям выполняется
    через ``fields`` (dict).
    """
    lot_id: int | str
    fields: dict[str, Any] = field(default_factory=dict)
    csrf_token: str | None = None

    @property
    def price(self) -> float | None:
        try:
            return float(self.fields.get("price"))
        except (TypeError, ValueError):
            return None

    @price.setter
    def price(self, value: float) -> None:
        self.fields["price"] = float(value)

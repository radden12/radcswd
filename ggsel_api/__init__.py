"""GGSEL API клиент и типы событий (по аналогии с FunPayAPI).

GGSEL — это маркетплейс цифровых товаров (https://ggsel.net). У него нет
официально документированного публичного API в духе FunPay, поэтому здесь
сделан тонкий HTTP-клиент, который:

* умеет логиниться по логину/паролю или работать по API-токену продавца
  (как только пользователь его укажет в .env);
* умеет тянуть список заказов и сообщений из личного кабинета;
* умеет отправлять сообщения покупателю;
* умеет менять цену лота.

Эти методы намеренно изолированы за одним классом ``GGSELAccount``, чтобы
плагины НЕ зависели от конкретных HTTP-запросов. Если у вас другая
структура API (приватный токен, неофициальный gateway) — переопределите
методы в наследнике и зарегистрируйте его как ``account_factory``.
"""

from .account import GGSELAccount, GGSELError, GGSELAuthError
from .events import (
    EventType,
    BaseEvent,
    NewOrderEvent,
    NewMessageEvent,
    OrderStatusChangedEvent,
    LastChatMessageChangedEvent,
)
from .types import Order, Message, ChatShortcut, LotFields

__all__ = [
    "GGSELAccount",
    "GGSELError",
    "GGSELAuthError",
    "EventType",
    "BaseEvent",
    "NewOrderEvent",
    "NewMessageEvent",
    "OrderStatusChangedEvent",
    "LastChatMessageChangedEvent",
    "Order",
    "Message",
    "ChatShortcut",
    "LotFields",
]

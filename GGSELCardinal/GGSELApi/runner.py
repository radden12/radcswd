"""GGSEL Runner — long-poll над seller-sales и debates.

Эмулирует FunPayAPI.Runner: бесконечный итератор, на каждой итерации
отдаёт `(event_type, payload)` кортежи.

Реализован максимально просто: периодически опрашиваем
  • /api/seller-sales/v2  — для NEW_ORDER;
  • /api/debates/v2       — для NEW_MESSAGE (по списку активных чатов).

Семантика идемпотентности:
  • видели order_id или message_id → не отдаём повторно;
  • состояние seen-id хранится в памяти, переживание перезапуска
    обеспечивается плагином (он сам ведёт pending-state).
"""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Iterator

from .account import Account
from .events import EventTypes
from .types import MessageShortcut, OrderShortcut

logger = logging.getLogger("GGSEL.runner")


class Runner:
    POLL_INTERVAL_SEC = 8

    def __init__(self, account: Account) -> None:
        self.account = account
        self._seen_orders: deque[str] = deque(maxlen=2048)
        self._seen_orders_set: set[str] = set()
        self._seen_messages: dict[str, set[str]] = {}  # chat_id -> msg_ids
        self._active_chats: set[str] = set()

    # ─────────────────────────────────────────────────────────── helpers
    def _remember_order(self, order_id: str) -> bool:
        if order_id in self._seen_orders_set:
            return False
        self._seen_orders.append(order_id)
        self._seen_orders_set.add(order_id)
        # bound the set to match the deque
        while len(self._seen_orders_set) > self._seen_orders.maxlen:
            self._seen_orders_set.discard(self._seen_orders.popleft())
        return True

    def _remember_message(self, chat_id: str, msg_id: str) -> bool:
        bag = self._seen_messages.setdefault(chat_id, set())
        if msg_id in bag:
            return False
        bag.add(msg_id)
        # ограничим размер bag-а
        if len(bag) > 500:
            for x in list(bag)[:100]:
                bag.discard(x)
        return True

    @staticmethod
    def _to_order(raw: dict) -> OrderShortcut:
        return OrderShortcut(
            id=str(raw.get("invoice_id") or raw.get("id") or raw.get("inv") or ""),
            title=str(raw.get("product_name") or raw.get("name_goods") or ""),
            description=str(raw.get("product_name") or ""),
            price=float(raw.get("amount_usd") or raw.get("amount") or 0.0),
            buyer_username=str(raw.get("email") or raw.get("buyer_login") or ""),
            buyer_id=str(raw.get("buyer_id") or ""),
            chat_id=str(raw.get("id_i") or raw.get("debate_id") or ""),
            chat_name=str(raw.get("email") or "ggsel-chat"),
            amount=int(raw.get("cnt_goods") or raw.get("amount_goods") or 1),
            lot_id=str(raw.get("id_goods") or raw.get("product_id") or ""),
            status=str(raw.get("status") or "new"),
            raw=raw,
        )

    @staticmethod
    def _to_message(chat_id: str, raw: dict) -> MessageShortcut:
        return MessageShortcut(
            id=str(raw.get("id") or raw.get("message_id") or ""),
            chat_id=chat_id,
            chat_name=str(raw.get("chat_name") or chat_id),
            author_id=str(raw.get("owner_id") or raw.get("from") or ""),
            author_username=str(raw.get("owner_name") or ""),
            text=str(raw.get("message") or raw.get("text") or ""),
            is_my=bool(raw.get("owner_type") == "seller"
                       or raw.get("is_seller")),
            raw=raw,
        )

    # ─────────────────────────────────────────────────────────── iteration
    def listen(self) -> Iterator[tuple[EventTypes, object]]:
        """Бесконечно ходит за заказами и сообщениями.

        Без exception-наружу: любая ошибка логируется и цикл продолжается.
        """
        while True:
            try:
                yield from self._tick()
            except Exception:  # noqa: BLE001
                logger.exception("GGSEL runner tick failed")
            time.sleep(self.POLL_INTERVAL_SEC)

    def _tick(self) -> Iterator[tuple[EventTypes, object]]:
        # 1) орлим за новыми заказами
        try:
            orders = self.account.get_orders()
        except Exception as exc:  # noqa: BLE001
            logger.warning("GGSEL get_orders failed: %s", exc)
            orders = []
        for raw in orders:
            order = self._to_order(raw)
            if not order.id:
                continue
            if self._remember_order(order.id):
                if order.chat_id:
                    self._active_chats.add(order.chat_id)
                yield EventTypes.NEW_ORDER, order

        # 2) проверяем все активные чаты на свежие сообщения
        for chat_id in list(self._active_chats):
            try:
                msgs = self.account.get_chat_messages(chat_id, limit=20)
            except Exception as exc:  # noqa: BLE001
                logger.warning("GGSEL get_chat_messages %s failed: %s",
                               chat_id, exc)
                continue
            for raw in msgs:
                msg = self._to_message(chat_id, raw)
                if not msg.id:
                    continue
                if self._remember_message(chat_id, msg.id):
                    yield EventTypes.NEW_MESSAGE, msg
                    yield EventTypes.LAST_CHAT_MESSAGE_CHANGED, msg

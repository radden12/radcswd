"""Раннер событий GGSEL: опрашивает аккаунт и эмитит NEW_ORDER / NEW_MESSAGE.

Архитектурно — близкий аналог FunPayAPI.Runner: бесконечный цикл, который
сравнивает текущий снимок состояния с предыдущим и отдаёт дельту в виде
событий. Реализован поверх ``threading.Thread`` (можно перейти на asyncio
позже без изменений API плагинов).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from .account import GGSELAccount, GGSELError
from .events import (
    BaseEvent,
    EventType,
    LastChatMessageChangedEvent,
    NewMessageEvent,
    NewOrderEvent,
    OrderStatusChangedEvent,
)
from .types import ChatShortcut

logger = logging.getLogger("ggsel.runner")


EventCallback = Callable[[BaseEvent], None]


class GGSELRunner:
    def __init__(
        self,
        account: GGSELAccount,
        callback: EventCallback,
        *,
        poll_interval: float = 10.0,
    ) -> None:
        self.account = account
        self.callback = callback
        self.poll_interval = poll_interval

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._seen_order_ids: set[str] = set()
        self._order_statuses: dict[str, str] = {}
        self._chat_last_msg_id: dict[str, int] = {}
        self._first_pass = True

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="GGSEL-Runner", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except GGSELError as e:
                logger.warning("[runner] GGSELError: %s", e)
            except Exception as e:  # noqa: BLE001
                logger.exception("[runner] неожиданная ошибка: %s", e)
            self._stop.wait(self.poll_interval)

    def _tick(self) -> None:
        # 1) Заказы
        try:
            orders = self.account.get_orders()
        except GGSELError as e:
            logger.debug("[runner] не удалось получить заказы: %s", e)
            orders = []
        for o in orders:
            oid = str(o.id)
            if oid not in self._seen_order_ids:
                self._seen_order_ids.add(oid)
                if not self._first_pass:
                    self._emit(NewOrderEvent(
                        type=EventType.NEW_ORDER, order=o,
                    ))
            else:
                prev = self._order_statuses.get(oid)
                if prev != o.status:
                    self._emit(OrderStatusChangedEvent(
                        type=EventType.ORDER_STATUS_CHANGED,
                        order=o,
                        old_status=prev or "",
                        new_status=o.status,
                    ))
            self._order_statuses[oid] = o.status

        # 2) Сообщения / чаты
        try:
            chats = self.account.get_chats()
        except GGSELError as e:
            logger.debug("[runner] не удалось получить чаты: %s", e)
            chats = []
        for chat in chats:
            self._process_chat(chat)

        self._first_pass = False

    def _process_chat(self, chat: ChatShortcut) -> None:
        cid = str(chat.id)
        prev = self._chat_last_msg_id.get(cid, 0)
        if chat.last_message_id and chat.last_message_id != prev:
            self._chat_last_msg_id[cid] = int(chat.last_message_id)
            if self._first_pass:
                return  # на первом проходе не шумим
            # NEW_MESSAGE — детально тянем новое сообщение, если можем.
            try:
                msgs = self.account.get_chat_messages(chat.id, limit=5)
            except GGSELError:
                msgs = []
            new_msg = None
            for m in msgs:
                if int(m.id) > prev:
                    self.callback(NewMessageEvent(
                        type=EventType.NEW_MESSAGE, message=m,
                    ))
                    new_msg = m
            self._emit(LastChatMessageChangedEvent(
                type=EventType.LAST_CHAT_MESSAGE_CHANGED,
                chat=chat,
                message=new_msg,
            ))

    def _emit(self, event: BaseEvent) -> None:
        try:
            self.callback(event)
        except Exception as e:  # noqa: BLE001
            logger.exception("[runner] callback упал: %s", e)

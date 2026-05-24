"""Telegram-бот для управления GGSEL Cardinal.

Сделан на pyTelegramBotAPI (telebot). API совпадает с FunPayCardinal:

  * у плагинов есть доступ к ``cardinal.telegram.bot`` (TeleBot инстанс);
  * есть ``cardinal.telegram.add_admins_handler`` / ``add_admins_callback``;
  * у InlineKeyboardButton callback_data используют формат
    ``{prefix}:{uuid}:{offset}`` — точно как ожидают портированные плагины.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable

import telebot
from telebot.types import (
    CallbackQuery,
    InlineKeyboardButton as IKB,
    InlineKeyboardMarkup as IKM,
    Message,
)

from .cbt import CBT

logger = logging.getLogger("tg_bot")


class TelegramBot:
    def __init__(self, token: str, admin_ids: list[int], cardinal) -> None:
        self.token = (token or "").strip()
        self.admin_ids = set(admin_ids or [])
        self.cardinal = cardinal
        self._stop = threading.Event()

        if not self.token:
            self.bot = None  # type: ignore[assignment]
        else:
            self.bot = telebot.TeleBot(
                self.token, parse_mode="HTML",
                threaded=True,
            )
            self._register_core_handlers()

    # ─────────────────────── helper-API для плагинов ────────────────
    def add_admins_handler(self, handler: Callable[[Message], None], **kw) -> None:
        """Регистрирует message-handler, который сработает только для админов."""
        if not self.bot:
            return

        def wrapper(msg: Message, *args, **kwargs):
            if msg.from_user and msg.from_user.id in self.admin_ids:
                return handler(msg, *args, **kwargs)

        kw.setdefault("func", lambda m: True)
        self.bot.message_handler(**kw)(wrapper)

    def add_admins_callback(self, handler: Callable[[CallbackQuery], None],
                            func: Callable[[CallbackQuery], bool] | None = None) -> None:
        if not self.bot:
            return

        def wrapper(call: CallbackQuery, *args, **kwargs):
            if call.from_user and call.from_user.id in self.admin_ids:
                return handler(call, *args, **kwargs)

        self.bot.callback_query_handler(
            func=(func or (lambda c: True))
        )(wrapper)

    def notify_admins(self, text: str, **kw) -> None:
        if not self.bot:
            return
        for uid in self.admin_ids:
            try:
                self.bot.send_message(uid, text, **kw)
            except Exception as e:  # noqa: BLE001
                logger.debug("notify_admins(%s): %s", uid, e)

    # ─────────────────────── core handlers ──────────────────────────
    def _register_core_handlers(self) -> None:
        bot = self.bot

        @bot.message_handler(commands=["start", "menu", "help"])
        def _on_start(msg: Message) -> None:
            if msg.from_user.id not in self.admin_ids:
                bot.reply_to(
                    msg,
                    "🚫 Этот бот предназначен только для администратора GGSEL Cardinal.",
                )
                return
            bot.send_message(
                msg.chat.id, self._main_text(), reply_markup=self._main_kb()
            )

        @bot.callback_query_handler(
            func=lambda c: (c.data or "").startswith(f"{CBT.MAIN}")
        )
        def _on_main(call: CallbackQuery) -> None:
            if call.from_user.id not in self.admin_ids:
                bot.answer_callback_query(call.id, "🚫")
                return
            self._show_main(call)

        @bot.callback_query_handler(
            func=lambda c: (c.data or "").startswith(CBT.PLUGINS_LIST)
        )
        def _on_plugins_list(call: CallbackQuery) -> None:
            if call.from_user.id not in self.admin_ids:
                bot.answer_callback_query(call.id, "🚫")
                return
            self._show_plugins_list(call)

        @bot.callback_query_handler(
            func=lambda c: (c.data or "").startswith(f"{CBT.EDIT_PLUGIN}:")
        )
        def _on_edit_plugin(call: CallbackQuery) -> None:
            if call.from_user.id not in self.admin_ids:
                bot.answer_callback_query(call.id, "🚫")
                return
            parts = (call.data or "").split(":")
            if len(parts) < 2:
                bot.answer_callback_query(call.id)
                return
            uuid = parts[1]
            offset = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            self._show_plugin_card(call, uuid, offset)

        @bot.callback_query_handler(
            func=lambda c: (c.data or "").startswith(f"{CBT.TOGGLE_PLUGIN}:")
        )
        def _on_toggle_plugin(call: CallbackQuery) -> None:
            if call.from_user.id not in self.admin_ids:
                bot.answer_callback_query(call.id, "🚫")
                return
            uuid = (call.data or "").split(":")[1]
            p = self.cardinal.plugins.get(uuid)
            if not p:
                bot.answer_callback_query(call.id, "Не найден")
                return
            self.cardinal.plugins.set_enabled(uuid, not p.enabled)
            self._show_plugin_card(call, uuid, 0)

        @bot.callback_query_handler(
            func=lambda c: (c.data or "").startswith(f"{CBT.RELOAD_PLUGIN}:")
        )
        def _on_reload_plugin(call: CallbackQuery) -> None:
            if call.from_user.id not in self.admin_ids:
                bot.answer_callback_query(call.id, "🚫")
                return
            uuid = (call.data or "").split(":")[1]
            new_p = self.cardinal.plugins.reload(uuid)
            if new_p:
                bot.answer_callback_query(call.id, "🔄 Перезагружен")
                self._show_plugin_card(call, new_p.uuid, 0)
            else:
                bot.answer_callback_query(call.id, "Не удалось перезагрузить")

        @bot.callback_query_handler(
            func=lambda c: (c.data or "") == CBT.STATUS
        )
        def _on_status(call: CallbackQuery) -> None:
            if call.from_user.id not in self.admin_ids:
                bot.answer_callback_query(call.id, "🚫")
                return
            self._show_status(call)

    # ─────────────────────────── views ─────────────────────────────
    def _main_text(self) -> str:
        return (
            "👋 <b>GGSEL Cardinal</b>\n\n"
            "Я бот-управляющий для вашего GGSEL-аккаунта. Выберите раздел:"
        )

    def _main_kb(self) -> IKM:
        kb = IKM(row_width=2)
        kb.row(
            IKB("🧩 Плагины", callback_data=CBT.PLUGINS_LIST),
            IKB("📦 Статус", callback_data=CBT.STATUS),
        )
        kb.row(
            IKB("📃 Заказы", callback_data=CBT.ORDERS),
            IKB("💬 Сообщения", callback_data=CBT.MESSAGES),
        )
        return kb

    def _show_main(self, call: CallbackQuery) -> None:
        try:
            self.bot.edit_message_text(
                self._main_text(),
                call.message.chat.id, call.message.message_id,
                reply_markup=self._main_kb(),
            )
        except Exception:
            self.bot.send_message(
                call.message.chat.id, self._main_text(),
                reply_markup=self._main_kb(),
            )
        self.bot.answer_callback_query(call.id)

    def _show_plugins_list(self, call: CallbackQuery) -> None:
        plugins = self.cardinal.plugins.list_plugins()
        text = "🧩 <b>Плагины</b>\n\n"
        kb = IKM(row_width=1)
        if not plugins:
            text += "Пока нет ни одного плагина. Положите .py-файл в <code>plugins/</code>."
        else:
            for p in plugins:
                badge = "🟢" if p.enabled else "🔴"
                kb.add(IKB(
                    f"{badge} {p.name} v{p.version}",
                    callback_data=f"{CBT.EDIT_PLUGIN}:{p.uuid}:0",
                ))
        kb.add(IKB("« Назад", callback_data=CBT.MAIN))
        try:
            self.bot.edit_message_text(
                text, call.message.chat.id, call.message.message_id,
                reply_markup=kb,
            )
        except Exception:
            self.bot.send_message(call.message.chat.id, text, reply_markup=kb)
        self.bot.answer_callback_query(call.id)

    def _show_plugin_card(self, call: CallbackQuery, uuid: str, offset: int) -> None:
        p = self.cardinal.plugins.get(uuid)
        if not p:
            self.bot.answer_callback_query(call.id, "Плагин не найден")
            return
        text = (
            f"<b>{p.name}</b> v{p.version}\n"
            f"UUID: <code>{p.uuid}</code>\n"
            f"Статус: {'🟢 включён' if p.enabled else '🔴 выключен'}\n\n"
            f"{p.description or '—'}\n\n"
            f"Автор: {p.credits or '—'}"
        )
        kb = IKM(row_width=2)
        kb.row(
            IKB(
                "🔴 Выключить" if p.enabled else "🟢 Включить",
                callback_data=f"{CBT.TOGGLE_PLUGIN}:{p.uuid}",
            ),
            IKB("🔄 Перезагрузить", callback_data=f"{CBT.RELOAD_PLUGIN}:{p.uuid}"),
        )
        if p.settings_page:
            kb.add(IKB(
                "⚙ Настройки",
                callback_data=f"{CBT.PLUGIN_SETTINGS}:{p.uuid}:{offset}",
            ))
        kb.add(IKB("« К списку", callback_data=CBT.PLUGINS_LIST))
        try:
            self.bot.edit_message_text(
                text, call.message.chat.id, call.message.message_id,
                reply_markup=kb,
            )
        except Exception:
            self.bot.send_message(call.message.chat.id, text, reply_markup=kb)
        self.bot.answer_callback_query(call.id)

    def _show_status(self, call: CallbackQuery) -> None:
        plugins = self.cardinal.plugins.list_plugins()
        on = sum(1 for p in plugins if p.enabled)
        text = (
            "📦 <b>Статус GGSEL Cardinal</b>\n\n"
            f"Плагинов: <b>{len(plugins)}</b> (включено {on})\n"
            f"Раннер: {'🟢' if self.cardinal.runner else '⚪️ выключен'}\n"
            f"Аккаунт: "
            f"{self.cardinal.account.login or 'token' if self.cardinal.account else '—'}\n"
        )
        kb = IKM()
        kb.add(IKB("« Назад", callback_data=CBT.MAIN))
        try:
            self.bot.edit_message_text(
                text, call.message.chat.id, call.message.message_id,
                reply_markup=kb,
            )
        except Exception:
            self.bot.send_message(call.message.chat.id, text, reply_markup=kb)
        self.bot.answer_callback_query(call.id)

    # ─────────────────────────── lifecycle ─────────────────────────
    def run_forever(self) -> None:
        if not self.bot:
            return
        while not self._stop.is_set():
            try:
                self.bot.infinity_polling(
                    timeout=20, long_polling_timeout=20,
                    skip_pending=True,
                )
            except Exception as e:  # noqa: BLE001
                logger.exception("Telegram polling упал: %s — рестарт через 5с", e)
                self._stop.wait(5)

    def stop(self) -> None:
        self._stop.set()
        if self.bot:
            try:
                self.bot.stop_polling()
            except Exception:
                pass

"""Telegram-обёртка над pyTelegramBotAPI.

Идея — дать плагинам тот же `cardinal.telegram.bot` (это `telebot.TeleBot`),
тот же `cardinal.telegram.send_notification(text)`, и тот же набор
CBT-констант, что и у FPC.

Главное меню умеет:
  • показать /menu — корневое меню с разделами;
  • показать список плагинов (CBT.PLUGINS_LIST);
  • открыть экран плагина (CBT.EDIT_PLUGIN);
  • в этом экране кнопка «⚙ Настройки» уводит на
    `CBT.PLUGIN_SETTINGS:<UUID>:<offset>`, который перехватывает сам
    плагин — точно так же, как в FunPayCardinal.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING

import telebot
from telebot.types import (
    CallbackQuery,
    InlineKeyboardButton as IKB,
    InlineKeyboardMarkup as IKM,
    Message,
)

from .cbt import CBT

if TYPE_CHECKING:
    from cardinal import Cardinal

logger = logging.getLogger("GGSEL.tg")


class TGBot:
    """Тонкая обёртка вокруг pyTelegramBotAPI."""

    def __init__(
        self,
        token: str,
        admin_ids: list[int] | None = None,
    ) -> None:
        if not token:
            raise ValueError("Telegram token is empty — set TG_BOT_TOKEN")
        self.token = token
        self.admin_ids = list(admin_ids or [])
        self.bot = telebot.TeleBot(token, parse_mode="HTML")
        self.cardinal: "Cardinal | None" = None
        self._polling_thread: threading.Thread | None = None
        self._setup_default_handlers()

    # ─────────────────────────────────────────────── access control
    def is_admin(self, user_id: int) -> bool:
        if not self.admin_ids:
            return True  # если список пуст — открыто (dev-mode)
        return int(user_id) in self.admin_ids

    # ─────────────────────────────────────────────── notifications
    def send_notification(self, text: str) -> None:
        for adm in self.admin_ids:
            try:
                self.bot.send_message(adm, text, disable_web_page_preview=True)
            except Exception:  # noqa: BLE001
                logger.exception("send_notification to %s failed", adm)

    # ─────────────────────────────────────────────── menus
    def _main_menu_kb(self) -> IKM:
        kb = IKM()
        kb.row(IKB("🧩 Плагины", callback_data=CBT.PLUGINS_LIST))
        kb.row(
            IKB("💵 Баланс", callback_data="MAIN:bal"),
            IKB("🛒 Заказы", callback_data="MAIN:ord"),
        )
        kb.row(IKB("🔁 Перезагрузить плагины", callback_data="MAIN:reload"))
        return kb

    def _plugins_list_kb(self) -> IKM:
        kb = IKM()
        if not self.cardinal:
            kb.row(IKB("Cardinal не подключён", callback_data=CBT.NOOP))
            return kb
        for offset, plug in enumerate(self.cardinal.plugins.values()):
            status = "🟢" if plug.enabled else "⚪"
            label = f"{status} {plug.name} v{plug.version}"
            kb.row(IKB(
                label[:60],
                callback_data=f"{CBT.EDIT_PLUGIN}:{plug.uuid}:{offset}",
            ))
        kb.row(IKB("⬅ Назад", callback_data=CBT.MAIN))
        return kb

    def _plugin_page_kb(self, uuid: str, offset: int) -> IKM:
        kb = IKM()
        plug = (self.cardinal.plugins.get(uuid)
                if self.cardinal else None)
        if plug is None:
            kb.row(IKB("Плагин не найден", callback_data=CBT.NOOP))
            kb.row(IKB("⬅ К списку", callback_data=CBT.PLUGINS_LIST))
            return kb
        has_settings = bool(getattr(plug.module, "SETTINGS_PAGE", False))
        if has_settings:
            kb.row(IKB(
                "⚙ Настройки",
                callback_data=f"{CBT.PLUGIN_SETTINGS}:{uuid}:{offset}",
            ))
        toggle_label = "🔴 Выключить" if plug.enabled else "🟢 Включить"
        kb.row(IKB(
            toggle_label,
            callback_data=f"{CBT.PLUGIN_TOGGLE}:{uuid}:{offset}",
        ))
        kb.row(IKB("♻️ Перезагрузить", callback_data=f"PLG:rl:{uuid}"))
        kb.row(IKB("⬅ К списку", callback_data=CBT.PLUGINS_LIST))
        return kb

    def refresh_plugin_buttons(self) -> None:
        """Совместимость с FPC — у нас меню рендерится по-запросу."""
        return None

    # ─────────────────────────────────────────────── handlers
    def _setup_default_handlers(self) -> None:
        bot = self.bot

        @bot.message_handler(commands=["start", "menu", "help"])
        def on_menu(msg: Message) -> None:
            if not self.is_admin(msg.from_user.id):
                bot.reply_to(msg, "⛔ Доступ запрещён.")
                return
            bot.send_message(
                msg.chat.id,
                "🤖 <b>GGSELCardinal</b> — главное меню",
                reply_markup=self._main_menu_kb(),
            )

        @bot.callback_query_handler(func=lambda c: c.data == CBT.MAIN)
        def on_main(c: CallbackQuery) -> None:
            self._safe_edit(c, "🤖 <b>GGSELCardinal</b> — главное меню",
                            self._main_menu_kb())

        @bot.callback_query_handler(func=lambda c: c.data == CBT.PLUGINS_LIST)
        def on_plugins(c: CallbackQuery) -> None:
            self._safe_edit(c, "🧩 <b>Установленные плагины:</b>",
                            self._plugins_list_kb())

        @bot.callback_query_handler(
            func=lambda c: c.data and c.data.startswith(f"{CBT.EDIT_PLUGIN}:"))
        def on_edit_plugin(c: CallbackQuery) -> None:
            try:
                _, uuid, offset = c.data.split(":", 2)
                offset_i = int(offset)
            except ValueError:
                bot.answer_callback_query(c.id, "bad cb")
                return
            plug = (self.cardinal.plugins.get(uuid) if self.cardinal else None)
            if plug is None:
                bot.answer_callback_query(c.id, "plugin gone")
                return
            descr = getattr(plug.module, "DESCRIPTION", "—")
            text = (
                f"<b>{plug.name}</b> v{plug.version}\n"
                f"UUID: <code>{plug.uuid}</code>\n"
                f"Файл: <code>{os.path.basename(plug.path)}</code>\n\n"
                f"{descr}"
            )
            self._safe_edit(c, text, self._plugin_page_kb(uuid, offset_i))

        @bot.callback_query_handler(
            func=lambda c: c.data and c.data.startswith(f"{CBT.PLUGIN_TOGGLE}:"))
        def on_toggle(c: CallbackQuery) -> None:
            try:
                _, uuid, offset = c.data.split(":", 2)
                offset_i = int(offset)
            except ValueError:
                bot.answer_callback_query(c.id, "bad cb")
                return
            if not self.cardinal:
                return
            plug = self.cardinal.plugins.get(uuid)
            if plug:
                plug.enabled = not plug.enabled
                bot.answer_callback_query(
                    c.id, "Включено" if plug.enabled else "Выключено",
                )
            self._safe_edit(
                c,
                f"<b>{plug.name}</b> v{plug.version}",
                self._plugin_page_kb(uuid, offset_i),
            )

        @bot.callback_query_handler(
            func=lambda c: c.data and c.data.startswith("PLG:rl:"))
        def on_reload(c: CallbackQuery) -> None:
            uuid = c.data.split(":", 2)[2]
            if self.cardinal and self.cardinal.reload_plugin(uuid):
                bot.answer_callback_query(c.id, "♻️ Перезагружено")
            else:
                bot.answer_callback_query(c.id, "Не удалось")

        @bot.callback_query_handler(func=lambda c: c.data == "MAIN:reload")
        def on_reload_all(c: CallbackQuery) -> None:
            if not self.cardinal:
                return
            count = 0
            for uuid in list(self.cardinal.plugins.keys()):
                if self.cardinal.reload_plugin(uuid):
                    count += 1
            bot.answer_callback_query(c.id, f"♻️ Перезагружено: {count}")

        @bot.callback_query_handler(func=lambda c: c.data == "MAIN:bal")
        def on_balance(c: CallbackQuery) -> None:
            bot.answer_callback_query(
                c.id, "Баланс смотрите внутри плагина (SMMPrime → 💰)",
                show_alert=True,
            )

        @bot.callback_query_handler(func=lambda c: c.data == "MAIN:ord")
        def on_orders(c: CallbackQuery) -> None:
            if not self.cardinal:
                bot.answer_callback_query(c.id, "Cardinal не запущен")
                return
            try:
                orders = self.cardinal.account.get_orders()
            except Exception as exc:  # noqa: BLE001
                bot.answer_callback_query(c.id, f"ошибка: {exc}", show_alert=True)
                return
            if not orders:
                bot.answer_callback_query(c.id, "Заказов нет")
                return
            text = ["<b>Последние заказы GGSEL</b>"]
            for row in orders[:10]:
                text.append(
                    f"• #{row.get('invoice_id', row.get('id', '?'))} — "
                    f"{row.get('product_name', '')} "
                    f"({row.get('amount_usd', row.get('amount', 0))} {row.get('currency', '')})"
                )
            self._safe_edit(c, "\n".join(text), self._main_menu_kb())

        @bot.callback_query_handler(func=lambda c: c.data == CBT.NOOP)
        def on_noop(c: CallbackQuery) -> None:
            bot.answer_callback_query(c.id)

    # ─────────────────────────────────────────────── helpers
    def _safe_edit(
        self,
        c: CallbackQuery,
        text: str,
        reply_markup: IKM | None = None,
    ) -> None:
        try:
            self.bot.edit_message_text(
                text,
                chat_id=c.message.chat.id,
                message_id=c.message.message_id,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
        except Exception:  # noqa: BLE001
            try:
                self.bot.send_message(
                    c.message.chat.id, text, reply_markup=reply_markup,
                    disable_web_page_preview=True,
                )
            except Exception:  # noqa: BLE001
                logger.exception("safe_edit fallback failed")
        try:
            self.bot.answer_callback_query(c.id)
        except Exception:  # noqa: BLE001
            pass

    # ─────────────────────────────────────────────── lifecycle
    def start_polling_in_thread(self) -> None:
        if self._polling_thread and self._polling_thread.is_alive():
            return

        def _run() -> None:
            while True:
                try:
                    logger.info("Telegram polling started")
                    self.bot.infinity_polling(timeout=20, long_polling_timeout=20)
                except Exception:  # noqa: BLE001
                    logger.exception("polling crashed — restart in 5s")
                    import time as _t
                    _t.sleep(5)

        self._polling_thread = threading.Thread(
            target=_run, daemon=True, name="tg-polling",
        )
        self._polling_thread.start()

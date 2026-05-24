"""Cardinal — ядро GGSEL Cardinal.

Делает то же, что FunPayCardinal делает для FunPay:

  * держит GGSELAccount + GGSELRunner;
  * держит Telegram-бот;
  * грузит плагины из ``plugins/`` (hot reload через watchdog);
  * раздаёт события NEW_ORDER / NEW_MESSAGE / LAST_CHAT_MESSAGE_CHANGED
    подписанным плагинам.

Плагин — это обычный python-файл с верхнеуровневыми атрибутами:

    NAME = "..."
    VERSION = "..."
    DESCRIPTION = "..."
    UUID = "..."
    SETTINGS_PAGE = True
    BIND_TO_PRE_INIT = [callable]
    BIND_TO_NEW_ORDER = [callable]
    BIND_TO_NEW_MESSAGE = [callable]
    BIND_TO_LAST_CHAT_MESSAGE_CHANGED = [callable]

Каждый callable получает ``(cardinal, event)`` — точно как в FunPay.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

from ggsel_api import (
    GGSELAccount,
    GGSELAuthError,
    EventType,
    BaseEvent,
)
from ggsel_api.runner import GGSELRunner
from plugin_system import PluginManager
from storage import JSONStorage
from tg_bot.bot import TelegramBot

logger = logging.getLogger("cardinal")


class Cardinal:
    def __init__(self, settings: dict[str, Any]) -> None:
        self.settings = settings
        self.root = Path(settings.get("root_dir") or ".").resolve()

        self.storage = JSONStorage(self.root / settings.get("storage_dir", "storage"))
        self.configs = JSONStorage(self.root / settings.get("configs_dir", "configs"))

        self.account: GGSELAccount | None = None
        self.runner: GGSELRunner | None = None
        self.telegram: TelegramBot | None = None

        self.plugins = PluginManager(
            self.root / settings.get("plugins_dir", "plugins"),
            cardinal=self,
            hot_reload=bool(settings.get("plugin_hot_reload", True)),
        )

        self._shutdown = threading.Event()

    # ───────────────────────── Lifecycle ──────────────────────────────
    def init(self) -> None:
        logger.info("Cardinal: инициализация ...")

        self.account = self._build_account()
        if self.account:
            try:
                self.account.login_if_needed()
            except GGSELAuthError as e:
                logger.warning("GGSEL: %s — продолжаю без авторизации", e)

        self.telegram = TelegramBot(
            token=self.settings.get("telegram_bot_token", ""),
            admin_ids=self.settings.get("telegram_admin_ids", []),
            cardinal=self,
        )

        self.plugins.load_all()
        self.plugins.fire(EventType.PRE_INIT, None)

    def run(self) -> None:
        if not self.account or not self.account.api_token and not self.account.login:
            logger.warning(
                "GGSEL аккаунт не настроен (нет токена/логина) — раннер не "
                "стартует. Telegram-бот всё равно поднимется."
            )
        else:
            self.runner = GGSELRunner(
                self.account,
                self._on_ggsel_event,
                poll_interval=float(self.settings.get("ggsel_poll_interval", 10)),
            )
            self.runner.start()
            logger.info("GGSEL runner запущен (poll=%ss)",
                        self.settings.get("ggsel_poll_interval", 10))

        if self.telegram and self.telegram.token:
            t = threading.Thread(
                target=self.telegram.run_forever,
                name="TG-Bot", daemon=True,
            )
            t.start()
            logger.info("Telegram-бот запущен")
        else:
            logger.warning(
                "TELEGRAM_BOT_TOKEN не задан — Telegram-бот отключён."
            )

        # Главный поток — ждёт сигнала остановки.
        try:
            while not self._shutdown.is_set():
                self._shutdown.wait(1)
        except KeyboardInterrupt:
            logger.info("Получен Ctrl+C — останавливаюсь")
            self.shutdown()

    def shutdown(self) -> None:
        self._shutdown.set()
        if self.runner:
            self.runner.stop()
        if self.telegram:
            self.telegram.stop()
        self.plugins.fire(EventType.SHUTDOWN, None)
        if self.account:
            self.account.close()
        logger.info("Cardinal остановлен")

    # ─────────────────────── события ──────────────────────────────────
    def _on_ggsel_event(self, event: BaseEvent) -> None:
        self.plugins.fire(event.type, event)

    # ─────────────────────── factories ────────────────────────────────
    def _build_account(self) -> GGSELAccount | None:
        s = self.settings
        if not (s.get("ggsel_api_token") or s.get("ggsel_login")):
            logger.warning(
                "GGSEL: не задан ни API-токен, ни логин/пароль. "
                "Аккаунт создаётся в офлайн-режиме."
            )
        return GGSELAccount(
            seller_id=s.get("ggsel_seller_id"),
            api_token=s.get("ggsel_api_token"),
            login=s.get("ggsel_login"),
            password=s.get("ggsel_password"),
        )

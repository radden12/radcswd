"""GGSELCardinal — ядро движка.

Идея и API в точности повторяют FunPayCardinal:
  • `Cardinal` — singleton-оркестратор, держит Account, Runner, Telegram,
    список плагинов и шину событий;
  • плагины — `.py` файлы в директории `plugins/`, экспортирующие
    `NAME`, `VERSION`, `UUID`, `BIND_TO_PRE_INIT`, `BIND_TO_POST_INIT`,
    `BIND_TO_NEW_ORDER`, `BIND_TO_NEW_MESSAGE`,
    `BIND_TO_LAST_CHAT_MESSAGE_CHANGED`, `BIND_TO_DELETE`;
  • hot reload: на лету подменяем плагин при изменении файла на диске.
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import sys
import threading
import time
import traceback
import types
from pathlib import Path
from typing import Any, Callable

from GGSELApi import Account, EventTypes, Runner
from GGSELApi.types import MessageShortcut, OrderShortcut

logger = logging.getLogger("GGSEL.cardinal")

# Список «жизненных» точек, на которые плагины подписываются.
_BIND_NAMES = (
    "BIND_TO_PRE_INIT",
    "BIND_TO_POST_INIT",
    "BIND_TO_NEW_ORDER",
    "BIND_TO_NEW_MESSAGE",
    "BIND_TO_LAST_CHAT_MESSAGE_CHANGED",
    "BIND_TO_DELETE",
)


class LoadedPlugin:
    __slots__ = ("uuid", "name", "version", "path", "mtime", "module",
                 "enabled", "handlers")

    def __init__(self, module: types.ModuleType, path: str) -> None:
        self.module = module
        self.path = path
        self.mtime = os.path.getmtime(path)
        self.uuid = getattr(module, "UUID", os.path.basename(path))
        self.name = getattr(module, "NAME", os.path.basename(path))
        self.version = getattr(module, "VERSION", "0.0.0")
        self.enabled = True
        self.handlers: dict[str, list[Callable[..., Any]]] = {
            b: list(getattr(module, b, []) or []) for b in _BIND_NAMES
        }


class Cardinal:
    """Главный класс движка.

    Использование:
        c = Cardinal(account=Account(...), telegram=TGBot(...))
        c.init()
        c.run()
    """

    def __init__(
        self,
        account: Account,
        telegram: "TGBot | None" = None,  # noqa: F821 — циклический импорт
        plugins_dir: str | Path = "plugins",
    ) -> None:
        self.account = account
        self.telegram = telegram
        self.plugins_dir = Path(plugins_dir)
        self.plugins_dir.mkdir(exist_ok=True)
        self.plugins: dict[str, LoadedPlugin] = {}   # by uuid
        self.runner: Runner | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._reload_thread: threading.Thread | None = None

    # ─────────────────────────────────────────────────────────── plugin api
    def load_plugins(self) -> None:
        """Сканируем директорию и подгружаем все *.py-файлы как плагины."""
        for fp in sorted(self.plugins_dir.glob("*.py")):
            if fp.name.startswith("_"):
                continue
            self._load_one(fp)

    def _load_one(self, fp: Path) -> LoadedPlugin | None:
        try:
            mod_name = f"ggsel_plugin_{fp.stem}"
            spec = importlib.util.spec_from_file_location(mod_name, fp)
            if spec is None or spec.loader is None:
                logger.error("cannot import plugin %s — bad spec", fp)
                return None
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
            plug = LoadedPlugin(mod, str(fp))
            with self._lock:
                # если был старый под тем же UUID — выгрузим
                old = self.plugins.get(plug.uuid)
                if old is not None:
                    self._unload(old)
                self.plugins[plug.uuid] = plug
            logger.info("Loaded plugin %s v%s (%s)",
                        plug.name, plug.version, fp.name)
            return plug
        except Exception:  # noqa: BLE001
            logger.exception("Failed to load plugin %s", fp)
            return None

    def _unload(self, plug: LoadedPlugin) -> None:
        try:
            for h in plug.handlers.get("BIND_TO_DELETE", []):
                try:
                    h(self)
                except Exception:  # noqa: BLE001
                    logger.exception("BIND_TO_DELETE failed for %s", plug.name)
        finally:
            mod_key = next(
                (k for k, v in sys.modules.items() if v is plug.module),
                None,
            )
            if mod_key:
                sys.modules.pop(mod_key, None)
            logger.info("Unloaded plugin %s", plug.name)

    def reload_plugin(self, uuid: str) -> bool:
        plug = self.plugins.get(uuid)
        if plug is None:
            return False
        self._unload(plug)
        result = self._load_one(Path(plug.path))
        if result and self.telegram:
            try:
                self.telegram.send_notification(
                    f"♻️ Плагин <b>{result.name}</b> v{result.version} перезагружен.",
                )
            except Exception:  # noqa: BLE001
                pass
        # Полный жизненный цикл повторного INIT
        if result:
            self._dispatch_init(result)
        return result is not None

    # ─────────────────────────────────────────────────────────── lifecycle
    def init(self) -> None:
        """Полный инициализирующий цикл."""
        logger.info("== Cardinal: init start ==")
        self.account.get()
        self.load_plugins()
        for plug in list(self.plugins.values()):
            self._dispatch_init(plug)
        if self.telegram:
            self.telegram.cardinal = self
            self.telegram.refresh_plugin_buttons()
        logger.info("== Cardinal: init end ==")

    def _dispatch_init(self, plug: LoadedPlugin) -> None:
        for h in plug.handlers["BIND_TO_PRE_INIT"]:
            try:
                h(self)
            except Exception:  # noqa: BLE001
                logger.exception("PRE_INIT failed for %s", plug.name)
        for h in plug.handlers["BIND_TO_POST_INIT"]:
            try:
                h(self)
            except Exception:  # noqa: BLE001
                logger.exception("POST_INIT failed for %s", plug.name)

    # ─────────────────────────────────────────────────────────── runtime
    def run(self) -> None:
        """Главный цикл — запускаем Runner и pollим события."""
        self.runner = Runner(self.account)
        self._reload_thread = threading.Thread(
            target=self._hot_reload_loop,
            daemon=True,
            name="cardinal-hot-reload",
        )
        self._reload_thread.start()

        if self.telegram:
            self.telegram.start_polling_in_thread()

        for ev_type, payload in self.runner.listen():
            if self._stop.is_set():
                break
            self._dispatch(ev_type, payload)

    def _dispatch(self, ev_type: EventTypes, payload: Any) -> None:
        for plug in list(self.plugins.values()):
            if not plug.enabled:
                continue
            handlers = self._handlers_for(plug, ev_type)
            for h in handlers:
                try:
                    h(self, payload)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "plugin %s handler %s failed for %s",
                        plug.name, getattr(h, "__name__", h), ev_type,
                    )
                    if self.telegram:
                        try:
                            self.telegram.send_notification(
                                f"❌ Ошибка в плагине <b>{plug.name}</b> "
                                f"({ev_type.value}):\n<pre>{traceback.format_exc()[-1500:]}</pre>",
                            )
                        except Exception:  # noqa: BLE001
                            pass

    @staticmethod
    def _handlers_for(plug: LoadedPlugin, ev_type: EventTypes) -> list[Callable]:
        if ev_type is EventTypes.NEW_ORDER:
            return plug.handlers["BIND_TO_NEW_ORDER"]
        if ev_type is EventTypes.NEW_MESSAGE:
            return plug.handlers["BIND_TO_NEW_MESSAGE"]
        if ev_type is EventTypes.LAST_CHAT_MESSAGE_CHANGED:
            return plug.handlers["BIND_TO_LAST_CHAT_MESSAGE_CHANGED"]
        return []

    # ─────────────────────────────────────────────────────────── hot-reload
    def _hot_reload_loop(self) -> None:
        while not self._stop.wait(2.5):
            try:
                for plug in list(self.plugins.values()):
                    if not os.path.exists(plug.path):
                        continue
                    cur = os.path.getmtime(plug.path)
                    if cur != plug.mtime:
                        logger.info("Hot-reload: %s changed, reloading",
                                    plug.path)
                        self.reload_plugin(plug.uuid)
                # Заодно ловим новые файлы:
                known = {p.path for p in self.plugins.values()}
                for fp in self.plugins_dir.glob("*.py"):
                    if fp.name.startswith("_"):
                        continue
                    if str(fp) not in known:
                        new = self._load_one(fp)
                        if new:
                            self._dispatch_init(new)
                            if self.telegram:
                                self.telegram.refresh_plugin_buttons()
            except Exception:  # noqa: BLE001
                logger.exception("hot-reload loop error")

    # ─────────────────────────────────────────────────────────── helpers
    def send_message(
        self,
        chat_id: str,
        text: str,
        chat_name: str | None = None,  # noqa: ARG002 — для совместимости
    ) -> bool:
        return self.account.send_message(chat_id, text)

    def stop(self) -> None:
        self._stop.set()

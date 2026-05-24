"""Загрузчик плагинов с поддержкой hot-reload.

Плагин — обычный python-модуль в ``plugins/``. Контракт:

  NAME, VERSION, DESCRIPTION, UUID, CREDITS, SETTINGS_PAGE
  BIND_TO_PRE_INIT, BIND_TO_NEW_ORDER, BIND_TO_NEW_MESSAGE,
  BIND_TO_LAST_CHAT_MESSAGE_CHANGED, BIND_TO_ORDER_STATUS_CHANGED,
  BIND_TO_DELETE

Каждый из BIND_TO_* — список callable вида ``fn(cardinal, event)``.

Hot-reload отслеживает изменения файлов через watchdog (если установлен).
Если watchdog недоступен — плагины можно перезагружать вручную через TG.
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

logger = logging.getLogger("plugins")


_BIND_ATTRS = (
    "BIND_TO_PRE_INIT",
    "BIND_TO_POST_INIT",
    "BIND_TO_NEW_ORDER",
    "BIND_TO_ORDER_STATUS_CHANGED",
    "BIND_TO_NEW_MESSAGE",
    "BIND_TO_LAST_CHAT_MESSAGE_CHANGED",
    "BIND_TO_DELETE",
    "BIND_TO_SHUTDOWN",
)


@dataclass
class LoadedPlugin:
    path: Path
    name: str
    version: str
    description: str
    uuid: str
    credits: str = ""
    settings_page: bool = False
    module: Any = None
    enabled: bool = True
    error: str | None = None
    handlers: dict[str, list[Callable]] = field(default_factory=dict)


class PluginManager:
    def __init__(self, plugins_dir: Path | str, cardinal, *,
                 hot_reload: bool = True) -> None:
        self.dir = Path(plugins_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cardinal = cardinal
        self.hot_reload = hot_reload
        self._lock = threading.RLock()
        self.plugins: dict[str, LoadedPlugin] = {}  # uuid -> plugin
        self._observer = None

    # ────────────────────────── load ──────────────────────────────────
    def load_all(self) -> None:
        for f in sorted(self.dir.glob("*.py")):
            if f.name.startswith("_"):
                continue
            self.load_file(f)
        if self.hot_reload:
            self._start_watcher()

    def load_file(self, path: Path) -> LoadedPlugin | None:
        with self._lock:
            mod_name = f"ggsel_plugin_{path.stem}"
            try:
                spec = importlib.util.spec_from_file_location(mod_name, path)
                if not spec or not spec.loader:
                    raise ImportError(f"spec is None for {path}")
                module = importlib.util.module_from_spec(spec)
                sys.modules[mod_name] = module
                spec.loader.exec_module(module)  # type: ignore[union-attr]
            except Exception as e:  # noqa: BLE001
                logger.error("Плагин %s НЕ загружен: %s\n%s",
                             path.name, e, traceback.format_exc())
                return None

            p = LoadedPlugin(
                path=path,
                name=getattr(module, "NAME", path.stem),
                version=getattr(module, "VERSION", "0.0.0"),
                description=getattr(module, "DESCRIPTION", ""),
                uuid=str(getattr(module, "UUID", path.stem)),
                credits=getattr(module, "CREDITS", ""),
                settings_page=bool(getattr(module, "SETTINGS_PAGE", False)),
                module=module,
            )
            for attr in _BIND_ATTRS:
                handlers = getattr(module, attr, None) or []
                if not isinstance(handlers, (list, tuple)):
                    handlers = [handlers]
                p.handlers[attr] = [h for h in handlers if callable(h)]
            self.plugins[p.uuid] = p
            logger.info("Плагин загружен: %s v%s (%s)", p.name, p.version, path.name)
            return p

    def unload(self, uuid: str) -> None:
        with self._lock:
            p = self.plugins.pop(uuid, None)
            if not p:
                return
            mod = p.module
            if mod is not None:
                for cb in p.handlers.get("BIND_TO_DELETE", []):
                    try:
                        cb(self.cardinal, None)
                    except Exception as e:  # noqa: BLE001
                        logger.exception("BIND_TO_DELETE упал: %s", e)
                try:
                    del sys.modules[mod.__name__]
                except KeyError:
                    pass
            logger.info("Плагин выгружен: %s", p.name)

    def reload(self, uuid: str) -> LoadedPlugin | None:
        p = self.plugins.get(uuid)
        if not p:
            return None
        path = p.path
        self.unload(uuid)
        return self.load_file(path)

    def reload_by_path(self, path: Path) -> LoadedPlugin | None:
        with self._lock:
            for uuid, p in list(self.plugins.items()):
                if p.path == path:
                    self.unload(uuid)
                    break
        return self.load_file(path)

    # ───────────────────────── enable/disable ────────────────────────
    def set_enabled(self, uuid: str, enabled: bool) -> None:
        p = self.plugins.get(uuid)
        if p:
            p.enabled = enabled
            logger.info("Плагин %s -> %s", p.name,
                        "включён" if enabled else "выключен")

    def list_plugins(self) -> list[LoadedPlugin]:
        return list(self.plugins.values())

    def get(self, uuid: str) -> LoadedPlugin | None:
        return self.plugins.get(uuid)

    # ────────────────────────── fire ──────────────────────────────────
    def fire(self, event_type, event) -> None:
        attr = f"BIND_TO_{getattr(event_type, 'name', str(event_type))}"
        for p in list(self.plugins.values()):
            if not p.enabled:
                continue
            for cb in p.handlers.get(attr, []):
                try:
                    cb(self.cardinal, event)
                except Exception as e:  # noqa: BLE001
                    logger.exception(
                        "Плагин %s: обработчик %s упал: %s",
                        p.name, attr, e,
                    )

    # ─────────────────────── hot reload ──────────────────────────────
    def _start_watcher(self) -> None:
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            logger.info("watchdog не установлен — hot-reload отключён")
            return

        mgr = self

        class _Handler(FileSystemEventHandler):
            def on_modified(self, event):
                self._reload_if_plugin(event)

            def on_created(self, event):
                self._reload_if_plugin(event)

            def _reload_if_plugin(self, event):
                if event.is_directory:
                    return
                p = Path(event.src_path)
                if p.suffix != ".py" or p.name.startswith("_"):
                    return
                time.sleep(0.3)  # дать редактору дозаписать
                try:
                    mgr.reload_by_path(p)
                except Exception as e:  # noqa: BLE001
                    logger.exception("hot-reload упал: %s", e)

        obs = Observer()
        obs.schedule(_Handler(), str(self.dir), recursive=False)
        obs.daemon = True
        obs.start()
        self._observer = obs
        logger.info("Hot-reload плагинов включён (%s)", self.dir)

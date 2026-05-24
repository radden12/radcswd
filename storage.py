"""Простой JSON-storage с атомарной записью.

Используется и Cardinal-ом для своих файлов, и плагинами — для конфигов
и pending-заказов. Реализован поверх ``json.dump``: дёшево, переживает
перезапуск, легко править руками.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("storage")


class JSONStorage:
    """Корневая папка для JSON-файлов с lock-on-write.

    Файлы пишутся атомарно (через tempfile + ``os.replace``), чтобы при
    падении посередине не получить обрезанный JSON.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def path(self, name: str) -> Path:
        return self.root / name

    def load(self, name: str, default: Any = None) -> Any:
        p = self.path(name)
        if not p.exists():
            return default if default is not None else {}
        try:
            with self._lock, p.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:  # noqa: BLE001
            logger.error("storage: не смог прочитать %s: %s", p, e)
            return default if default is not None else {}

    def save(self, name: str, data: Any) -> None:
        p = self.path(name)
        with self._lock:
            tmp = tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8",
                dir=str(p.parent), delete=False, suffix=".tmp",
            )
            try:
                json.dump(data, tmp, ensure_ascii=False, indent=2)
                tmp.flush()
                os.fsync(tmp.fileno())
                tmp.close()
                os.replace(tmp.name, p)
            except Exception:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
                raise

    def exists(self, name: str) -> bool:
        return self.path(name).exists()

    def delete(self, name: str) -> None:
        p = self.path(name)
        with self._lock:
            try:
                p.unlink()
            except FileNotFoundError:
                pass

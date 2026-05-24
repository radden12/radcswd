"""Точка входа GGSEL Cardinal.

Запуск: ``python main.py``.

Делает:
  1) загружает .env;
  2) настраивает логирование;
  3) создаёт ``Cardinal``, инициализирует и стартует.
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*a, **k):  # type: ignore[no-redef]
        return False


ROOT = Path(__file__).resolve().parent


def _setup_logging(level: str, logs_dir: Path) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    if root.handlers:
        return  # уже настроено (на случай reload)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    fh = RotatingFileHandler(
        logs_dir / "cardinal.log",
        maxBytes=5 * 1024 * 1024, backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)


def _parse_admin_ids(raw: str) -> list[int]:
    out: list[int] = []
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            continue
    return out


def _read_settings() -> dict:
    load_dotenv(ROOT / ".env")
    return {
        "root_dir": str(ROOT),
        "storage_dir": os.getenv("STORAGE_DIR", "storage"),
        "configs_dir": os.getenv("CONFIGS_DIR", "configs"),
        "plugins_dir": os.getenv("PLUGINS_DIR", "plugins"),
        "logs_dir": os.getenv("LOGS_DIR", "logs"),
        "log_level": os.getenv("LOG_LEVEL", "INFO"),
        "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        "telegram_admin_ids": _parse_admin_ids(os.getenv("TELEGRAM_ADMIN_IDS", "")),
        "ggsel_seller_id": os.getenv("GGSEL_SELLER_ID", "").strip(),
        "ggsel_api_token": os.getenv("GGSEL_API_TOKEN", "").strip(),
        "ggsel_login": os.getenv("GGSEL_LOGIN", "").strip(),
        "ggsel_password": os.getenv("GGSEL_PASSWORD", ""),
        "ggsel_poll_interval": float(os.getenv("GGSEL_POLL_INTERVAL", "10")),
        "plugin_hot_reload": os.getenv("PLUGIN_HOT_RELOAD", "1") not in ("0", "false", "no", ""),
        "auto_restart": os.getenv("AUTO_RESTART", "1") not in ("0", "false", "no", ""),
    }


def main() -> int:
    settings = _read_settings()
    _setup_logging(settings["log_level"], ROOT / settings["logs_dir"])

    logger = logging.getLogger("main")
    logger.info("=" * 60)
    logger.info("GGSEL Cardinal стартует")
    logger.info("Корень проекта: %s", ROOT)
    logger.info("=" * 60)

    # Создание базовых директорий — «при запуске создаётся папка проекта».
    for sub in (settings["storage_dir"], settings["configs_dir"],
                settings["plugins_dir"], settings["logs_dir"]):
        (ROOT / sub).mkdir(parents=True, exist_ok=True)

    # Импортируем Cardinal после настройки логов, чтобы дочерние логгеры
    # подхватили формат.
    from cardinal import Cardinal

    card = Cardinal(settings)
    try:
        card.init()
        card.run()
        return 0
    except KeyboardInterrupt:
        logger.info("Остановка по Ctrl+C")
        card.shutdown()
        return 0
    except Exception as e:  # noqa: BLE001
        logger.exception("Фатальная ошибка: %s", e)
        try:
            card.shutdown()
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())

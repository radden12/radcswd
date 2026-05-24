"""GGSELCardinal — точка входа.

Запуск: `python main.py`.
Чтение конфига:
  • .env (если установлен python-dotenv);
  • переменные окружения GGSEL_SELLER_ID, GGSEL_API_KEY, TG_BOT_TOKEN,
    TG_ADMIN_IDS (через запятую).
"""
from __future__ import annotations

import logging
import os
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:  # опционально
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from GGSELApi import Account
from cardinal import Cardinal
from tg_bot import TGBot


def _configure_logging() -> None:
    logdir = ROOT / "logs"
    logdir.mkdir(exist_ok=True)
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(logdir / "cardinal.log", encoding="utf-8"),
        ],
    )


def _parse_admins(raw: str) -> list[int]:
    out: list[int] = []
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if chunk.lstrip("-").isdigit():
            out.append(int(chunk))
    return out


def main() -> None:
    _configure_logging()
    log = logging.getLogger("GGSEL.main")

    seller_id = os.environ.get("GGSEL_SELLER_ID", "")
    api_key = os.environ.get("GGSEL_API_KEY", "")
    tg_token = os.environ.get("TG_BOT_TOKEN", "")
    admins = _parse_admins(os.environ.get("TG_ADMIN_IDS", ""))

    if not seller_id or not api_key:
        log.warning("GGSEL_SELLER_ID/GGSEL_API_KEY не заданы — "
                    "Cardinal стартует, но реальный API недоступен.")
    if not tg_token:
        log.error("TG_BOT_TOKEN не задан — Telegram-бот не запустится.")

    account = Account(seller_id=seller_id, api_key=api_key)
    tg = TGBot(tg_token, admin_ids=admins) if tg_token else None

    cardinal = Cardinal(
        account=account,
        telegram=tg,
        plugins_dir=ROOT / "plugins",
    )

    def _graceful_stop(signum, frame):  # noqa: ARG001
        log.info("Получен сигнал %s, завершаюсь...", signum)
        cardinal.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _graceful_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _graceful_stop)

    try:
        cardinal.init()
        cardinal.run()
    except Exception:
        log.exception("FATAL: cardinal crashed")
        raise


if __name__ == "__main__":
    main()

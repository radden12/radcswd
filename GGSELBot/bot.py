"""GGSELBot — автоматизация заказов GGSEL → SMMPrime через Telegram.

Полностью самодостаточный бот. НЕ требует FunPayCardinal.
При первом запуске спрашивает в консоли ТОЛЬКО Telegram Bot Token,
всё остальное (GGSEL API-ключ, SMMPrime API-ключ, связки, шаблоны,
admin-id) настраивается потом через сам бот.

Тексты, кнопки и поведение максимально повторяют исходный
`smmprime_plugin_v1_0_2.py`:
  • state-machine waiting_for_link → waiting_for_confirm → done;
  • DRY-RUN по умолчанию у каждой новой связки;
  • защита от двойных вызовов через `_PROCESSING` под Lock;
  • pending-state в JSON, переживает рестарт;
  • все 9 buyer-шаблонов (ask_link, confirm, cancelled, success, error,
    dry_run, qty_too_small, qty_too_large, already_done);
  • inline-меню повторяет _kb_main/_kb_binding из оригинала;
  • маскирование API-ключа `abcd****wxyz`;
  • guard-блоклист «технических» текстов, не уходящих покупателю.

Запуск:
    python bot.py

Технологии: Python 3.11+, aiogram 3.x, requests.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

import requests
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton as IKB,
    InlineKeyboardMarkup as IKM,
    Message,
)

# ─────────────────────────────────────────────────────────────────────────────
#  ОБЩИЕ КОНСТАНТЫ
# ─────────────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
BINDINGS_PATH = ROOT / "bindings.json"
STORAGE_DIR = ROOT / "storage"
PENDING_PATH = STORAGE_DIR / "pending_orders.json"
ORDERS_LOG_PATH = STORAGE_DIR / "orders.log"
LOG_PATH = STORAGE_DIR / "bot.log"

STORAGE_DIR.mkdir(exist_ok=True)

VERSION = "1.0.0"

SMMPRIME_API_URL_DEFAULT = "https://smmprime.com/api/v2"
GGSEL_API_URL_DEFAULT = "https://api.digiseller.com"
REQUEST_TIMEOUT = 15
POLL_INTERVAL_SEC = 10

# ─────────────────────────────────────────────────────────────────────────────
#  ЛОГГИРОВАНИЕ
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
    ],
)
logger = logging.getLogger("GGSELBot")

# ─────────────────────────────────────────────────────────────────────────────
#  CALLBACK PREFIXES  (короткие, чтобы влезали в 64 байта)
# ─────────────────────────────────────────────────────────────────────────────
_CB = "GG"
_MAIN = f"{_CB}:m"
_TOGGLE_ENABLED = f"{_CB}:tge"
_SET_API = f"{_CB}:sak"
_SET_GGSEL = f"{_CB}:sgg"
_SET_ADMIN = f"{_CB}:sadm"
_LIST_BIND = f"{_CB}:l"
_ADD_BIND = f"{_CB}:a"
_DEL_BIND = f"{_CB}:d"
_BIND_DETAIL = f"{_CB}:i"
_BIND_EDIT = f"{_CB}:ie"
_BIND_TOGGLE_DRY = f"{_CB}:btd"
_BIND_TOGGLE_ON = f"{_CB}:bte"
_BIND_TEST = f"{_CB}:tst"
_BIND_REFRESH_SVC = f"{_CB}:brs"
_CHECK_BAL = f"{_CB}:b"
_LIST_SERVICES = f"{_CB}:svc"
_PENDING_LIST = f"{_CB}:pl"
_PENDING_DEL_ASK = f"{_CB}:pda"
_PENDING_DEL_OK = f"{_CB}:pdo"
_PENDING_PURGE_ASK = f"{_CB}:ppa"
_PENDING_PURGE_OK = f"{_CB}:ppo"
_SET_ASK_LINK_TEXT = f"{_CB}:sal"
_SET_CONFIRM_TEXT = f"{_CB}:scf"
_SET_CANCELLED_TEXT = f"{_CB}:scn"
_SET_OK_TEXT = f"{_CB}:sok"
_SET_ERR_TEXT = f"{_CB}:ser"
_SET_DRY_TEXT = f"{_CB}:sdr"
_HELP = f"{_CB}:h"
_HELP_FLOW = f"{_CB}:hflow"
_NOOP = f"{_CB}:noop"
_BACK = f"{_CB}:bk"

# ─────────────────────────────────────────────────────────────────────────────
#  ДЕФОЛТНЫЕ ШАБЛОНЫ (взяты ровно из smmprime_plugin_v1_0_2.py)
# ─────────────────────────────────────────────────────────────────────────────
_DEFAULT_ASK_LINK_TEMPLATE = (
    "Здравствуйте! Спасибо за заказ — я бот-помощник продавца, "
    "помогу его оформить автоматически.\n\n"
    "Пришлите, пожалуйста, ссылку для продвижения одним сообщением. "
    "Например, ссылку на профиль, канал, видео или конкретный пост."
)

_DEFAULT_CONFIRM_TEMPLATE = (
    "Проверьте, всё ли верно:\n\n"
    "Ссылка: {link}\n"
    "Количество: {quantity}\n"
    "Услуга SMMPrime: {service_id}\n\n"
    "Если всё правильно — напишите: Да, и я оформлю заказ.\n"
    "Если хотите изменить ссылку — напишите: Отмена, и мы начнём заново."
)

_DEFAULT_CANCELLED_TEMPLATE = (
    "Хорошо, пришлите, пожалуйста, новую ссылку одним сообщением."
)

_DEFAULT_NOT_LINK_TEMPLATE = (
    "Кажется, это не похоже на ссылку. Пришлите, пожалуйста, "
    "корректную ссылку одним сообщением."
)

_DEFAULT_NOT_CONFIRM_TEMPLATE = (
    "Не совсем понял ответ. Напишите Да — чтобы оформить заказ, "
    "или Отмена — чтобы изменить ссылку."
)

_DEFAULT_ALREADY_DONE_TEMPLATE = (
    "Этот заказ уже оформлен ранее — повторно создавать его не нужно. "
    "Если что-то пошло не так, напишите продавцу — он подскажет."
)

_DEFAULT_QTY_TOO_SMALL_TEMPLATE = (
    "{buyer_username}, к сожалению, для этого товара минимальное "
    "количество — {min} шт., а в заказе указано {quantity} шт. "
    "Поэтому я не могу его автоматически оформить.\n\n"
    "Что делать: дождитесь, пока продавец отменит заказ — деньги вернутся "
    "автоматически. После этого вы сможете оформить новый заказ "
    "с количеством от {min} шт.\n\n"
    "GGSEL заказ: {ggsel_order_id}\n"
    "Услуга SMMPrime: {service_id}"
)

_DEFAULT_QTY_TOO_LARGE_TEMPLATE = (
    "{buyer_username}, к сожалению, для этого товара максимальное "
    "количество — {max} шт., а в заказе указано {quantity} шт. "
    "Поэтому я не могу его автоматически оформить.\n\n"
    "Что делать: дождитесь, пока продавец отменит заказ — деньги вернутся "
    "автоматически. После этого вы сможете оформить новый заказ "
    "с количеством до {max} шт.\n\n"
    "GGSEL заказ: {ggsel_order_id}\n"
    "Услуга SMMPrime: {service_id}"
)

_DEFAULT_SUCCESS_TEMPLATE = (
    "{buyer_username}, ваш заказ успешно оформлен. Спасибо за покупку!\n\n"
    "Услуга SMMPrime: {service_id}\n"
    "Количество: {quantity}\n"
    "Ссылка: {link}\n"
    "Номер заказа SMMPrime: {smm_order_id}\n"
    "GGSEL заказ: {ggsel_order_id}"
)

_DEFAULT_ERROR_TEMPLATE = (
    "{buyer_username}, к сожалению, не получилось оформить заказ "
    "автоматически. Не переживайте — продавец увидит ваш заказ "
    "и оформит его вручную в ближайшее время.\n\n"
    "Если есть уточнения — напишите их в этот же чат, продавец прочитает.\n\n"
    "GGSEL заказ: {ggsel_order_id}\n"
    "Услуга SMMPrime: {service_id}\n"
    "Количество: {quantity}"
)

_DEFAULT_DRY_RUN_TEMPLATE = (
    "Тестовый режим — заказ принят, но реальный заказ в SMMPrime "
    "сейчас не создаётся (продавец проверяет настройку).\n\n"
    "{buyer_username}, ваши данные сохранены:\n"
    "Услуга SMMPrime: {service_id}\n"
    "Количество: {quantity}\n"
    "Ссылка: {link}\n"
    "GGSEL заказ: {ggsel_order_id}\n"
    "Товар GGSEL: {lot_id}"
)

DEFAULT_TEMPLATES: dict[str, str] = {
    "ask_link": _DEFAULT_ASK_LINK_TEMPLATE,
    "confirm": _DEFAULT_CONFIRM_TEMPLATE,
    "cancelled": _DEFAULT_CANCELLED_TEMPLATE,
    "not_link": _DEFAULT_NOT_LINK_TEMPLATE,
    "not_confirm": _DEFAULT_NOT_CONFIRM_TEMPLATE,
    "already_done": _DEFAULT_ALREADY_DONE_TEMPLATE,
    "qty_too_small": _DEFAULT_QTY_TOO_SMALL_TEMPLATE,
    "qty_too_large": _DEFAULT_QTY_TOO_LARGE_TEMPLATE,
    "success": _DEFAULT_SUCCESS_TEMPLATE,
    "error": _DEFAULT_ERROR_TEMPLATE,
    "dry_run": _DEFAULT_DRY_RUN_TEMPLATE,
}

# ─────────────────────────────────────────────────────────────────────────────
#  ИНСТРУКЦИЯ (укороченный аналог _HELP_TEXT / _HELP_FLOW_TEXT из оригинала)
# ─────────────────────────────────────────────────────────────────────────────
_HELP_TEXT = (
    "📖 <b>Инструкция GGSELBot v" + VERSION + "</b>\n\n"
    "<b>Как настроить с нуля</b>\n"
    "1. ⚙ Настройки → 🔑 API-ключ SMMPrime — введите ключ SMMPrime.\n"
    "2. 🌐 GGSEL — введите seller_id и api_key вашего ЛК GGSEL.\n"
    "3. 💰 Баланс — должна показаться сумма (если 401, см. внизу).\n"
    "4. 📃 Список услуг — выпишите ID нужной услуги (например 5017).\n"
    "5. 🛒 Связки → ➕ Добавить связку. Wizard:\n"
    "   GGSEL lot_id → название → service_id → dry-run.\n"
    "6. 🧪 Сделайте тестовую покупку с DRY-RUN — проверьте.\n"
    "7. Если всё ОК — выключите dry-run в карточке связки. Готово.\n\n"
    "<b>Что такое dry-run</b>\n"
    "Dry-run — тестовый режим. В нём бот <b>НЕ создаёт</b> реальный заказ "
    "в SMMPrime и <b>НЕ тратит баланс</b>, но повторяет всё остальное "
    "поведение. По умолчанию у новой связки ВКЛ.\n\n"
    "<b>Шаблоны (переменные)</b>\n"
    "В текстах УСПЕХА / ОШИБКИ / DRY-RUN можно использовать:\n"
    "• <code>{buyer_username}</code> — ник покупателя\n"
    "• <code>{ggsel_order_id}</code> — ID заказа GGSEL\n"
    "• <code>{lot_id}</code> — ID товара GGSEL\n"
    "• <code>{service_id}</code> — ID услуги SMMPrime\n"
    "• <code>{quantity}</code> — количество\n"
    "• <code>{link}</code> — ссылка покупателя\n"
    "• <code>{smm_order_id}</code> — ID заказа SMMPrime\n\n"
    "<b>Безопасность</b>\n"
    "API-ключи всегда маскируются (<code>abcd****wxyz</code>) и хранятся "
    "только локально в config.json."
)

_HELP_FLOW_TEXT = (
    "📖 <b>Как работает flow</b>\n\n"
    "<b>1. Покупка на GGSEL.</b> Бот ловит новый заказ (long-poll API "
    "GGSEL), матчит lot_id со связкой, сохраняет pending-запись и "
    "отправляет покупателю «пришлите ссылку».\n\n"
    "<b>2. Сообщение покупателя.</b> Бот проверяет URL, переводит "
    "запись в waiting_for_confirm и показывает сводку: «Ссылка / "
    "Количество / Услуга SMMPrime». Просит «Да» или «Отмена».\n\n"
    "<b>3. Подтверждение.</b>\n"
    "• Если связка в dry-run (🟡) — реального POST нет, статус "
    "<code>dry_run_done</code>, покупателю шаблон dry-run.\n"
    "• Если связка в боевом (⚪) — POST к SMMPrime, при успехе статус "
    "<code>smm_created</code>, при ошибке — <code>failed</code>.\n\n"
    "<b>Защита от дубликатов:</b> при повторной «Да» по тому же заказу "
    "бот ответит «заказ уже был создан, SMMPrime ID: N» и не делает "
    "второй POST. Под Lock + processing-set."
)

# ─────────────────────────────────────────────────────────────────────────────
#  УТИЛЫ ХРАНИЛИЩА (атомарная запись)
# ─────────────────────────────────────────────────────────────────────────────
_FILE_LOCK = threading.Lock()


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with _FILE_LOCK:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


def _load_json(path: Path, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        logger.warning("Corrupt JSON at %s — using defaults", path)
        return default


def log_event(line: str) -> None:
    STORAGE_DIR.mkdir(exist_ok=True)
    with open(ORDERS_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}\n")


# ─────────────────────────────────────────────────────────────────────────────
#  КОНФИГ / СВЯЗКИ / PENDING
# ─────────────────────────────────────────────────────────────────────────────
_DEFAULT_CONFIG: dict[str, Any] = {
    "tg_bot_token": "",
    "admin_ids": [],
    "enabled": True,
    "smmprime_api_key": "",
    "smmprime_api_url": "",
    "ggsel_seller_id": "",
    "ggsel_api_key": "",
    "ggsel_api_url": "",
    "templates": {},  # пустой = используем DEFAULT_TEMPLATES
}


def load_config() -> dict:
    cfg = _load_json(CONFIG_PATH, _DEFAULT_CONFIG.copy())
    if not isinstance(cfg, dict):
        cfg = _DEFAULT_CONFIG.copy()
    for k, v in _DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    return cfg


def save_config(cfg: dict) -> None:
    _atomic_write_json(CONFIG_PATH, cfg)


def load_bindings() -> list[dict]:
    data = _load_json(BINDINGS_PATH, [])
    return data if isinstance(data, list) else []


def save_bindings(bs: list[dict]) -> None:
    _atomic_write_json(BINDINGS_PATH, bs)


def load_pending() -> dict:
    data = _load_json(PENDING_PATH, {})
    return data if isinstance(data, dict) else {}


def save_pending(p: dict) -> None:
    _atomic_write_json(PENDING_PATH, p)


def get_template(cfg: dict, b: dict, key: str) -> str:
    """Per-binding override → глобальный override → дефолт."""
    field = f"buyer_{key}_text"
    if isinstance(b, dict) and b.get(field):
        return str(b[field])
    if cfg.get("templates", {}).get(key):
        return str(cfg["templates"][key])
    return DEFAULT_TEMPLATES.get(key, "")


# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS (повторяют поведение оригинала)
# ─────────────────────────────────────────────────────────────────────────────
_URL_RE = re.compile(
    r"(?:https?://|(?:www\.|t\.me/|vk\.com/|instagram\.com/|youtube\.com/))\S+",
    re.IGNORECASE,
)
_LEADING_GARBAGE_RE = re.compile(
    r"^[\s]*[^A-Za-zА-Яа-яЁё0-9«\"'\(\[\{]+[\s]*"
)

_CONFIRM_YES = {
    "да", "+", "ок", "ok", "yes", "y", "оформить", "оформи",
    "/confirm", "/yes", "/ok", "подтверждаю", "верно", "всё верно",
    "все верно", "правильно",
}
_CONFIRM_CANCEL = {
    "отмена", "отменить", "нет", "no", "cancel", "stop",
    "/cancel", "/no", "не надо", "отказ", "не верно", "неверно",
}

_TECH_BLOCKLIST = frozenset({
    "menu", "/menu", "/start", "start", "/help", "help",
    "debug", "callback", "cb", "test", "ping",
    "ggselbot", "ggsel",
})


def _strip_leading_emoji(text: str) -> str:
    if not text:
        return text
    return _LEADING_GARBAGE_RE.sub("", text, count=1)


def _mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}****{key[-4:]}"


def _extract_link(text: str) -> str | None:
    if not text:
        return None
    m = _URL_RE.search(text)
    return m.group(0) if m else None


def _parse_confirm_reply(text: str) -> str | None:
    if not text:
        return None
    t = text.strip().lower()
    if not t:
        return None
    head_word = re.split(r"[^\w/+]+", t, maxsplit=1)[0]
    if head_word in _CONFIRM_CANCEL:
        return "cancel"
    if head_word in _CONFIRM_YES:
        return "yes"
    for pat in _CONFIRM_CANCEL:
        if t == pat or t.startswith(pat + " "):
            return "cancel"
    for pat in _CONFIRM_YES:
        if t == pat or t.startswith(pat + " "):
            return "yes"
    return None


class _SafeFormat(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render_template(template: str, vars_: dict) -> str:
    try:
        out = template.format_map(_SafeFormat(vars_))
    except (IndexError, KeyError):
        out = template
    return _strip_leading_emoji(out)


def build_vars(pending: dict, link: str | None = None,
               smm_order_id: Any = None) -> dict:
    v = {
        "buyer_username": pending.get("buyer_username", ""),
        "ggsel_order_id": pending.get("ggsel_order_id", ""),
        "lot_id": pending.get("lot_id", ""),
        "service_id": pending.get("service_id", ""),
        "quantity": pending.get("quantity", ""),
        "link": link or pending.get("link") or "",
        "smm_order_id": smm_order_id or pending.get("smm_order_id") or "",
        "min": pending.get("min_quantity", 0),
        "max": pending.get("max_quantity", 0),
        "dry_run": "включен" if pending.get("dry_run") else "выключен",
    }
    return v


def _h(s: Any) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _truncate(text: str, limit: int = 80) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ─────────────────────────────────────────────────────────────────────────────
#  SMMPrime API CLIENT
# ─────────────────────────────────────────────────────────────────────────────
class SMMPrimeError(Exception):
    pass


class SMMPrimeAuthError(SMMPrimeError):
    pass


class SMMPrimeClient:
    def __init__(self, api_key: str, base_url: str = SMMPRIME_API_URL_DEFAULT,
                 timeout: int = REQUEST_TIMEOUT) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or SMMPRIME_API_URL_DEFAULT).strip() \
            or SMMPRIME_API_URL_DEFAULT
        self.timeout = timeout

    def _request(self, payload: dict) -> Any:
        if not self.api_key:
            raise SMMPrimeAuthError("API-ключ не задан")
        data = {"key": self.api_key, **payload}
        try:
            r = requests.post(self.base_url, data=data, timeout=self.timeout)
        except requests.RequestException as e:
            raise SMMPrimeError(f"network: {e}") from e
        if r.status_code == 401:
            raise SMMPrimeAuthError(self._extract_err(r) or "Unauthorized")
        if r.status_code >= 400:
            raise SMMPrimeError(
                f"HTTP {r.status_code}: {self._extract_err(r) or r.reason}"
            )
        try:
            body = r.json()
        except ValueError as e:
            raise SMMPrimeError(f"non-JSON: {e}") from e
        if isinstance(body, dict) and "error" in body:
            err = str(body["error"])
            if "key" in err.lower() or "auth" in err.lower():
                raise SMMPrimeAuthError(err)
            raise SMMPrimeError(err)
        return body

    @staticmethod
    def _extract_err(r: requests.Response) -> str:
        try:
            j = r.json()
            if isinstance(j, dict):
                return str(j.get("detail") or j.get("error")
                           or j.get("message") or "")
        except Exception:  # noqa: BLE001
            pass
        return (r.text or "")[:200]

    def get_services(self) -> list[dict]:
        body = self._request({"action": "services"})
        if not isinstance(body, list):
            raise SMMPrimeError(
                f"ожидался список услуг, пришло: {type(body).__name__}"
            )
        return body

    def get_service(self, service_id: int) -> dict | None:
        try:
            target = int(service_id)
        except (TypeError, ValueError):
            return None
        for svc in self.get_services():
            if not isinstance(svc, dict):
                continue
            try:
                if int(svc.get("service") or 0) == target:
                    return svc
            except (TypeError, ValueError):
                continue
        return None

    def add_order(self, service: int, link: str, quantity: int) -> dict:
        return self._request({
            "action": "add", "service": int(service),
            "link": link, "quantity": int(quantity),
        })

    def get_status(self, order: int | str) -> dict:
        return self._request({"action": "status", "order": order})

    def get_balance(self) -> dict:
        return self._request({"action": "balance"})


def _smm_client(cfg: dict) -> SMMPrimeClient:
    return SMMPrimeClient(
        cfg.get("smmprime_api_key", ""),
        cfg.get("smmprime_api_url") or SMMPRIME_API_URL_DEFAULT,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  GGSEL API CLIENT (Digiseller-совместимый)
# ─────────────────────────────────────────────────────────────────────────────
class GgselError(Exception):
    pass


class GgselClient:
    """Минимальный клиент к публичному API GGSEL/Digiseller.

    Делает только то, что нужно боту: логин (apilogin),
    список последних продаж (seller-sales/v2), детальная инфа по
    покупке (purchase/info), отправка сообщения в чат покупателя
    (debates/v2), и чтение сообщений.
    """

    def __init__(self, seller_id: str, api_key: str,
                 base_url: str = GGSEL_API_URL_DEFAULT,
                 timeout: int = REQUEST_TIMEOUT) -> None:
        self.seller_id = str(seller_id or "")
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or GGSEL_API_URL_DEFAULT).rstrip("/")
        self.timeout = timeout
        self._token = ""
        self._token_expires = 0.0

    def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        if not self.seller_id or not self.api_key:
            raise GgselError("seller_id или api_key не заданы")
        ts = int(time.time())
        sign = hashlib.sha256(f"{self.api_key}{ts}".encode("utf-8")).hexdigest()
        body = {
            "seller_id": int(self.seller_id) if self.seller_id.isdigit() else 0,
            "timestamp": ts,
            "sign": sign,
        }
        try:
            r = requests.post(f"{self.base_url}/api/apilogin",
                              json=body, timeout=self.timeout)
        except requests.RequestException as e:
            raise GgselError(f"login network: {e}") from e
        try:
            data = r.json()
        except ValueError:
            raise GgselError(f"login non-JSON: {r.text[:200]}")
        if not isinstance(data, dict) or data.get("retval") != 0:
            raise GgselError(f"login failed: {data!r}")
        self._token = data.get("token", "")
        self._token_expires = time.time() + 2 * 3600
        return self._token

    def _get(self, path: str, params: dict | None = None) -> Any:
        token = self._ensure_token()
        p = {"token": token, **(params or {})}
        try:
            r = requests.get(f"{self.base_url}{path}", params=p,
                             timeout=self.timeout)
        except requests.RequestException as e:
            raise GgselError(f"{path} network: {e}") from e
        try:
            return r.json()
        except ValueError:
            raise GgselError(f"{path} non-JSON: {r.text[:200]}")

    def _post(self, path: str, params: dict | None = None,
              json_body: dict | None = None) -> Any:
        token = self._ensure_token()
        p = {"token": token, **(params or {})}
        try:
            r = requests.post(f"{self.base_url}{path}", params=p,
                              json=json_body or {}, timeout=self.timeout)
        except requests.RequestException as e:
            raise GgselError(f"{path} network: {e}") from e
        try:
            return r.json()
        except ValueError:
            raise GgselError(f"{path} non-JSON: {r.text[:200]}")

    def get_orders(self) -> list[dict]:
        data = self._get("/api/seller-sales/v2",
                         params={"rows": 50, "page": 1, "returned": 0})
        if isinstance(data, dict):
            return data.get("rows") or data.get("sales") or []
        return []

    def get_chat_messages(self, chat_id: str, limit: int = 30) -> list[dict]:
        data = self._get("/api/debates/v2",
                         params={"id_i": chat_id, "newer": 1, "count": limit})
        if isinstance(data, dict):
            return data.get("messages", []) or []
        return []

    def send_message(self, chat_id: str, text: str) -> bool:
        data = self._post("/api/debates/v2",
                          json_body={"id_i": chat_id, "message": text})
        return isinstance(data, dict) and data.get("retval") == 0


# ─────────────────────────────────────────────────────────────────────────────
#  СВЯЗКИ — структура и поиск
# ─────────────────────────────────────────────────────────────────────────────
def _new_binding(lot_id: str, title: str = "", service: int = 0) -> dict:
    return {
        "ggsel_lot_id": str(lot_id),
        "title": title,
        "service": int(service or 0),
        "service_name": "",
        "service_category": "",
        "min_quantity": 0,
        "max_quantity": 0,
        "dry_run": True,
        "enabled": True,
        # Per-binding overrides шаблонов (как в оригинале).
        "buyer_ask_link_text": "",
        "buyer_confirm_text": "",
        "buyer_cancelled_text": "",
        "buyer_success_text": "",
        "buyer_error_text": "",
        "buyer_dry_run_text": "",
        "created_at": int(time.time()),
    }


def find_binding(bs: list[dict], lot_id: str) -> tuple[int, dict] | tuple[None, None]:
    if not lot_id:
        return None, None
    for i, b in enumerate(bs):
        if str(b.get("ggsel_lot_id")) == str(lot_id):
            return i, b
    return None, None


# ─────────────────────────────────────────────────────────────────────────────
#  PENDING-STATE: claim/release под Lock (защита от двойных API-вызовов)
# ─────────────────────────────────────────────────────────────────────────────
_PENDING_LOCK = threading.RLock()
_PROCESSING: set[str] = set()


def pending_upsert(p_row: dict) -> None:
    with _PENDING_LOCK:
        data = load_pending()
        data[str(p_row["ggsel_order_id"])] = p_row
        save_pending(data)


def pending_get(oid: str) -> dict | None:
    with _PENDING_LOCK:
        return load_pending().get(str(oid))


def pending_claim(oid: str) -> bool:
    with _PENDING_LOCK:
        if str(oid) in _PROCESSING:
            return False
        p = pending_get(oid)
        if p and p.get("status") in ("smm_created", "dry_run_done"):
            return False
        _PROCESSING.add(str(oid))
        return True


def pending_release(oid: str) -> None:
    with _PENDING_LOCK:
        _PROCESSING.discard(str(oid))


def pending_find_by_chat(chat_id: str) -> tuple[str, dict] | tuple[None, None]:
    with _PENDING_LOCK:
        for oid, p in load_pending().items():
            if not isinstance(p, dict):
                continue
            if str(p.get("chat_id")) == str(chat_id) and p.get(
                "status"
            ) in ("waiting_for_link", "waiting_for_confirm"):
                return oid, p
        return None, None


def pending_purge(group: str) -> int:
    with _PENDING_LOCK:
        data = load_pending()
        before = len(data)
        keep_map = {
            "wait": lambda v: v.get("status") != "waiting_for_link",
            "dry": lambda v: v.get("status") != "dry_run_done",
            "done": lambda v: v.get("status") != "smm_created",
            "failed": lambda v: v.get("status") != "failed",
        }
        keeper = keep_map.get(group)
        if keeper is None:
            return 0
        new = {k: v for k, v in data.items()
               if not isinstance(v, dict) or keeper(v)}
        save_pending(new)
        return before - len(new)


# ─────────────────────────────────────────────────────────────────────────────
#  ОТПРАВКА В ЧАТ ПОКУПАТЕЛЯ + ГВАРД (как _send_buyer в оригинале)
# ─────────────────────────────────────────────────────────────────────────────
def send_buyer(client: GgselClient, chat_id: str, text: str) -> None:
    if not chat_id:
        logger.warning("send_buyer: пустой chat_id — skip")
        return
    if not isinstance(text, str):
        logger.error("send_buyer: text не str: %r", type(text))
        return
    text = _strip_leading_emoji(text)
    stripped = text.strip()
    if not stripped:
        logger.error("send_buyer: пустой текст — БЛОК")
        return
    if stripped.lower() in _TECH_BLOCKLIST:
        logger.error("send_buyer: попытка отправить техническую команду %r "
                     "покупателю — БЛОК", stripped)
        return
    try:
        client.send_message(chat_id, text)
        logger.info("→ buyer chat=%s len=%d preview=%r",
                    chat_id, len(text), text[:80])
    except Exception as e:  # noqa: BLE001
        logger.error("send_buyer failed: %s", e, exc_info=True)


# ─────────────────────────────────────────────────────────────────────────────
#  TELEGRAM BOT — состояния админ-диалога
# ─────────────────────────────────────────────────────────────────────────────
# Простой диалог-машина: per-user_id хранится «что ждём»
_DIALOG: dict[int, dict] = {}


def is_admin(cfg: dict, user_id: int) -> bool:
    admins = cfg.get("admin_ids") or []
    if not admins:
        return True  # первый запуск — открыто, чтобы можно было задать админа
    return int(user_id) in [int(x) for x in admins]


# ─────────────────────────────────────────────────────────────────────────────
#  inline-клавиатуры (повторяют _kb_main / _kb_binding из оригинала)
# ─────────────────────────────────────────────────────────────────────────────
def kb_main(cfg: dict) -> IKM:
    api_icon = "✅" if cfg.get("smmprime_api_key") else "❌"
    ggsel_icon = "✅" if cfg.get("ggsel_api_key") else "❌"
    on_icon = "🟢 ВКЛ" if cfg.get("enabled") else "🔴 ВЫКЛ"
    bs = load_bindings()
    cnt = len(bs)
    pending = load_pending()
    waiting = sum(1 for v in pending.values()
                  if isinstance(v, dict)
                  and v.get("status") == "waiting_for_link")
    total = sum(1 for v in pending.values() if isinstance(v, dict))
    rows: list[list[IKB]] = []
    rows.append([IKB(text=f"Статус бота: {on_icon}",
                     callback_data=_TOGGLE_ENABLED)])
    rows.append([IKB(text=f"{api_icon} API-ключ SMMPrime",
                     callback_data=_SET_API)])
    rows.append([IKB(text=f"{ggsel_icon} GGSEL seller+api",
                     callback_data=_SET_GGSEL)])
    rows.append([IKB(text=f"🛒 Связки GGSEL→SMMPrime ({cnt})",
                     callback_data=_LIST_BIND)])
    rows.append([IKB(text=f"📋 Заказы (⏳ {waiting} / всего {total})",
                     callback_data=_PENDING_LIST)])
    rows.append([IKB(text="💬 ASK LINK", callback_data=_SET_ASK_LINK_TEXT)])
    rows.append([
        IKB(text="💬 CONFIRM", callback_data=_SET_CONFIRM_TEXT),
        IKB(text="💬 ОТМЕНА", callback_data=_SET_CANCELLED_TEXT),
    ])
    rows.append([
        IKB(text="💬 УСПЕХ", callback_data=_SET_OK_TEXT),
        IKB(text="💬 ОШИБКА", callback_data=_SET_ERR_TEXT),
        IKB(text="💬 DRY-RUN", callback_data=_SET_DRY_TEXT),
    ])
    rows.append([
        IKB(text="💰 Баланс", callback_data=_CHECK_BAL),
        IKB(text="📃 Список услуг", callback_data=_LIST_SERVICES),
    ])
    rows.append([IKB(text="👤 Админы", callback_data=_SET_ADMIN)])
    rows.append([
        IKB(text="📖 Инструкция", callback_data=_HELP),
        IKB(text="🔁 Как работает flow", callback_data=_HELP_FLOW),
    ])
    return IKM(inline_keyboard=rows)


def kb_bindings(bs: list[dict]) -> IKM:
    rows: list[list[IKB]] = []
    for i, b in enumerate(bs):
        st = "🟢" if b.get("enabled", True) else "🔴"
        dry = "🟡" if b.get("dry_run", True) else "⚪"
        title = (b.get("title") or "").strip() or f"связка #{i+1}"
        label = f"{st}{dry} {_truncate(title, 24)} | #{i+1}"
        rows.append([IKB(text=label,
                         callback_data=f"{_BIND_DETAIL}:{i}")])
    rows.append([IKB(text="➕ Добавить связку", callback_data=_ADD_BIND)])
    rows.append([IKB(text="◀ Назад", callback_data=_MAIN)])
    return IKM(inline_keyboard=rows)


def kb_binding(idx: int, b: dict) -> IKM:
    en = "🟢 ВКЛ" if b.get("enabled", True) else "🔴 ВЫКЛ"
    dry = "🟡 ВКЛ" if b.get("dry_run", True) else "⚪ ВЫКЛ"
    return IKM(inline_keyboard=[
        [IKB(text=f"Статус: {en}",
             callback_data=f"{_BIND_TOGGLE_ON}:{idx}"),
         IKB(text=f"Dry-run: {dry}",
             callback_data=f"{_BIND_TOGGLE_DRY}:{idx}")],
        [IKB(text="🧪 Тест связки", callback_data=f"{_BIND_TEST}:{idx}")],
        [IKB(text="✏ Название",
             callback_data=f"{_BIND_EDIT}:{idx}:title"),
         IKB(text="✏ Lot ID",
             callback_data=f"{_BIND_EDIT}:{idx}:ggsel_lot_id")],
        [IKB(text="✏ service_id",
             callback_data=f"{_BIND_EDIT}:{idx}:service")],
        [IKB(text="🔄 Обновить инфо услуги (min/max из SMMPrime)",
             callback_data=f"{_BIND_REFRESH_SVC}:{idx}")],
        [IKB(text="✏ ASK LINK",
             callback_data=f"{_BIND_EDIT}:{idx}:buyer_ask_link_text")],
        [IKB(text="✏ CONFIRM",
             callback_data=f"{_BIND_EDIT}:{idx}:buyer_confirm_text"),
         IKB(text="✏ ОТМЕНА",
             callback_data=f"{_BIND_EDIT}:{idx}:buyer_cancelled_text")],
        [IKB(text="✏ УСПЕХ",
             callback_data=f"{_BIND_EDIT}:{idx}:buyer_success_text"),
         IKB(text="✏ ОШИБКА",
             callback_data=f"{_BIND_EDIT}:{idx}:buyer_error_text"),
         IKB(text="✏ DRY-RUN",
             callback_data=f"{_BIND_EDIT}:{idx}:buyer_dry_run_text")],
        [IKB(text="🗑 Удалить связку", callback_data=f"{_DEL_BIND}:{idx}")],
        [IKB(text="◀ К списку связок", callback_data=_LIST_BIND)],
    ])


def kb_pending(p: dict) -> IKM:
    rows: list[list[IKB]] = []
    rows.append([IKB(text="🧹 Очистить ожидающие",
                     callback_data=f"{_PENDING_PURGE_ASK}:wait")])
    rows.append([IKB(text="🧹 Очистить dry-run",
                     callback_data=f"{_PENDING_PURGE_ASK}:dry")])
    rows.append([IKB(text="🧹 Очистить обработанные",
                     callback_data=f"{_PENDING_PURGE_ASK}:done")])
    rows.append([IKB(text="🧹 Очистить failed",
                     callback_data=f"{_PENDING_PURGE_ASK}:failed")])
    rows.append([IKB(text="◀ Назад", callback_data=_MAIN)])
    return IKM(inline_keyboard=rows)


def main_text(cfg: dict) -> str:
    bs = load_bindings()
    pending = load_pending()
    waiting = sum(1 for v in pending.values()
                  if isinstance(v, dict)
                  and v.get("status") == "waiting_for_link")
    return (
        f"⚙️ <b>GGSELBot v{VERSION} — Настройки</b>\n\n"
        f"Бот: <b>{'включён' if cfg.get('enabled') else 'выключен'}</b>\n"
        f"🔑 SMMPrime API-ключ: <code>{_mask(cfg.get('smmprime_api_key', ''))}</code>\n"
        f"🔑 GGSEL seller_id: <code>{cfg.get('ggsel_seller_id') or '—'}</code>\n"
        f"🔑 GGSEL api_key: <code>{_mask(cfg.get('ggsel_api_key', ''))}</code>\n"
        f"🛒 Связок: <b>{len(bs)}</b>\n"
        f"⏳ Ожидающих ссылку: <b>{waiting}</b>\n\n"
        f"<i>📋 Flow: покупка GGSEL → бот просит ссылку → подтверждение "
        f"«Да/Отмена» → заказ в SMMPrime (или dry-run).</i>"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  ОБРАБОТКА НОВОГО ЗАКАЗА И СООБЩЕНИЙ — ядро state-machine
# ─────────────────────────────────────────────────────────────────────────────
def handle_new_order(cfg: dict, client: GgselClient, order: dict) -> None:
    """Аналог `_on_new_order` из оригинала."""
    bs = load_bindings()
    oid = str(order.get("invoice_id") or order.get("id") or order.get("inv") or "")
    lot_id = str(order.get("id_goods") or order.get("product_id") or "")
    chat_id = str(order.get("id_i") or order.get("debate_id") or "")
    buyer = str(order.get("email") or order.get("buyer_login") or "buyer")
    qty = int(order.get("cnt_goods") or order.get("amount_goods") or 1)

    if not oid or not chat_id or not lot_id:
        logger.warning("ORDER skip: incomplete fields oid=%s lot=%s chat=%s",
                       oid, lot_id, chat_id)
        return

    idx, b = find_binding(bs, lot_id)
    if b is None:
        logger.info("ORDER #%s: связка для lot=%s НЕ найдена — пропуск",
                    oid, lot_id)
        return
    if not b.get("enabled", True):
        logger.warning("ORDER #%s: связка %s ВЫКЛ — пропуск", oid, idx)
        return

    existing = pending_get(oid)
    if existing and existing.get("status") in (
        "waiting_for_link", "waiting_for_confirm", "smm_created",
        "dry_run_done", "failed",
    ):
        logger.info("ORDER #%s уже в pending (status=%s) — пропуск",
                    oid, existing.get("status"))
        return

    svc_id = int(b.get("service") or 0)
    if svc_id <= 0:
        logger.error("ORDER #%s: service_id некорректен в связке #%s", oid, idx)
        return

    dry_run = bool(b.get("dry_run", True))
    min_q = int(b.get("min_quantity") or 0)
    max_q = int(b.get("max_quantity") or 0)

    p_row: dict[str, Any] = {
        "ggsel_order_id": oid,
        "buyer_username": buyer,
        "chat_id": chat_id,
        "lot_id": lot_id,
        "binding_idx": idx,
        "service_id": svc_id,
        "service_name": b.get("service_name") or "",
        "min_quantity": min_q,
        "max_quantity": max_q,
        "dry_run": dry_run,
        "smm_order_id": None,
        "link": None,
        "quantity": qty,
        "status": "waiting_for_link",
        "created_at": int(time.time()),
    }

    # Валидация min/max — если quantity вне диапазона, авто-оформить нельзя.
    if min_q > 0 and qty < min_q:
        p_row["status"] = "failed"
        pending_upsert(p_row)
        text = render_template(get_template(cfg, b, "qty_too_small"),
                               build_vars(p_row))
        send_buyer(client, chat_id, text)
        log_event(f"ORDER #{oid} qty<min ({qty}<{min_q}) — failed")
        return
    if max_q > 0 and qty > max_q:
        p_row["status"] = "failed"
        pending_upsert(p_row)
        text = render_template(get_template(cfg, b, "qty_too_large"),
                               build_vars(p_row))
        send_buyer(client, chat_id, text)
        log_event(f"ORDER #{oid} qty>max ({qty}>{max_q}) — failed")
        return

    pending_upsert(p_row)
    send_buyer(client, chat_id,
               render_template(get_template(cfg, b, "ask_link"),
                               build_vars(p_row)))
    log_event(f"ORDER #{oid} lot={lot_id} -> service={svc_id} "
              f"qty={qty} dry={dry_run}")


def handle_buyer_message(cfg: dict, client: GgselClient,
                         chat_id: str, text: str,
                         smm_notify_admin) -> None:
    """Аналог `_on_new_message` — состояния waiting_for_link → confirm → done."""
    oid, p = pending_find_by_chat(chat_id)
    if oid is None:
        return
    bs = load_bindings()
    idx = p.get("binding_idx")
    b = bs[idx] if isinstance(idx, int) and 0 <= idx < len(bs) else {}

    status = p.get("status")
    if status == "waiting_for_link":
        link = _extract_link(text)
        if not link:
            send_buyer(client, chat_id,
                       render_template(get_template(cfg, b, "not_link"),
                                       build_vars(p)))
            return
        p["link"] = link
        p["status"] = "waiting_for_confirm"
        pending_upsert(p)
        send_buyer(client, chat_id,
                   render_template(get_template(cfg, b, "confirm"),
                                   build_vars(p, link=link)))
        return

    if status == "waiting_for_confirm":
        reply = _parse_confirm_reply(text)
        if reply == "cancel":
            p["status"] = "waiting_for_link"
            p["link"] = None
            pending_upsert(p)
            send_buyer(client, chat_id,
                       render_template(get_template(cfg, b, "cancelled"),
                                       build_vars(p)))
            return
        if reply == "yes":
            create_smm_order(cfg, client, oid, b, smm_notify_admin)
            return
        send_buyer(client, chat_id,
                   render_template(get_template(cfg, b, "not_confirm"),
                                   build_vars(p)))
        return

    if status in ("smm_created", "dry_run_done"):
        send_buyer(client, chat_id,
                   render_template(get_template(cfg, b, "already_done"),
                                   build_vars(p)))
        return


def create_smm_order(cfg: dict, client: GgselClient, oid: str,
                     b: dict, smm_notify_admin) -> None:
    """Создание заказа в SMMPrime под Lock с защитой от дублей."""
    if not pending_claim(oid):
        logger.info("create_smm_order #%s: claim не получен — skip", oid)
        return
    try:
        p = pending_get(oid) or {}
        link = p.get("link") or ""
        qty = int(p.get("quantity") or 0)
        svc = int(p.get("service_id") or 0)
        chat_id = str(p.get("chat_id") or "")

        if p.get("dry_run"):
            p["status"] = "dry_run_done"
            pending_upsert(p)
            send_buyer(client, chat_id,
                       render_template(get_template(cfg, b, "dry_run"),
                                       build_vars(p)))
            log_event(f"DRY-RUN #{oid} svc={svc} qty={qty} link={link}")
            smm_notify_admin(
                f"🟡 <b>[GGSELBot]</b> DRY-RUN заказ <code>#{oid}</code>\n"
                f"Услуга: <code>{svc}</code> × {qty}\n"
                f"Ссылка: {_h(_truncate(link, 100))}"
            )
            return

        try:
            resp = _smm_client(cfg).add_order(svc, link, qty)
            smm_id = resp.get("order") if isinstance(resp, dict) else None
            if not smm_id:
                raise SMMPrimeError(f"unexpected add response: {resp!r}")
            p["status"] = "smm_created"
            p["smm_order_id"] = smm_id
            pending_upsert(p)
            send_buyer(client, chat_id,
                       render_template(get_template(cfg, b, "success"),
                                       build_vars(p, smm_order_id=smm_id)))
            log_event(f"OK #{oid} smm={smm_id} svc={svc} qty={qty}")
            smm_notify_admin(
                f"✅ <b>[GGSELBot]</b> SMMPrime заказ создан "
                f"<code>{smm_id}</code>\n"
                f"GGSEL: <code>{oid}</code> | Услуга: <code>{svc}</code> × {qty}"
            )
        except (SMMPrimeError, requests.RequestException) as e:
            p["status"] = "failed"
            p["error"] = str(e)
            pending_upsert(p)
            send_buyer(client, chat_id,
                       render_template(get_template(cfg, b, "error"),
                                       build_vars(p)))
            log_event(f"ERR #{oid} {type(e).__name__}: {e}")
            smm_notify_admin(
                f"❌ <b>[GGSELBot]</b> ошибка SMMPrime по <code>{oid}</code>:\n"
                f"<pre>{_h(_truncate(str(e), 500))}</pre>"
            )
    finally:
        pending_release(oid)


# ─────────────────────────────────────────────────────────────────────────────
#  FIRST-RUN WIZARD (только TG-токен)
# ─────────────────────────────────────────────────────────────────────────────
def first_run_wizard() -> str:
    """При первом запуске спрашиваем ТОЛЬКО Telegram bot token, создаём config.json.

    Возвращает токен. Всё остальное настраивается через сам бот.
    """
    cfg = load_config()
    if cfg.get("tg_bot_token"):
        return cfg["tg_bot_token"]

    print("╔══════════════════════════════════════════════════════════════════╗")
    print("║                    GGSELBot — первая настройка                   ║")
    print("╠══════════════════════════════════════════════════════════════════╣")
    print("║  Нужен только Telegram Bot Token.                                ║")
    print("║  Получить его: @BotFather → /newbot → скопировать токен.         ║")
    print("║                                                                  ║")
    print("║  Всё остальное (SMMPrime API-ключ, GGSEL seller_id/api_key,      ║")
    print("║  связки и шаблоны) настраивается потом ВНУТРИ бота через         ║")
    print("║  inline-меню /menu.                                              ║")
    print("╚══════════════════════════════════════════════════════════════════╝")

    while True:
        token = input("Telegram Bot Token: ").strip()
        if re.match(r"^\d+:[\w-]{20,}$", token):
            break
        print("❌ Похоже, не похоже на токен Telegram (формат '123:ABC...'). "
              "Попробуйте ещё раз.")

    cfg["tg_bot_token"] = token
    save_config(cfg)
    print(f"\n✅ Токен сохранён в {CONFIG_PATH}")
    print("→ Запускаю бота. Откройте чат с ним в Telegram и отправьте /start.\n")
    return token


# ─────────────────────────────────────────────────────────────────────────────
#  TELEGRAM-РОУТИНГ (aiogram v3)
# ─────────────────────────────────────────────────────────────────────────────
async def _safe_edit(cq: CallbackQuery, text: str,
                     kb: IKM | None = None) -> None:
    try:
        await cq.message.edit_text(text, reply_markup=kb,
                                   disable_web_page_preview=True)
    except Exception:  # noqa: BLE001
        try:
            await cq.message.answer(text, reply_markup=kb,
                                    disable_web_page_preview=True)
        except Exception:  # noqa: BLE001
            logger.exception("safe_edit fallback failed")
    try:
        await cq.answer()
    except Exception:  # noqa: BLE001
        pass


def register_handlers(dp: Dispatcher, bot: Bot) -> None:

    @dp.message(Command("start", "menu", "help"))
    async def on_menu(m: Message) -> None:
        cfg = load_config()
        if not is_admin(cfg, m.from_user.id):
            await m.reply("⛔ Доступ запрещён.")
            return
        # На первое /start добавляем юзера в админы, если список пуст.
        if not cfg.get("admin_ids"):
            cfg["admin_ids"] = [m.from_user.id]
            save_config(cfg)
            await m.answer(f"👤 Вы добавлены как первый администратор "
                           f"(<code>{m.from_user.id}</code>).")
        await m.answer(main_text(cfg), reply_markup=kb_main(cfg))

    @dp.callback_query(F.data == _MAIN)
    async def on_main(cq: CallbackQuery) -> None:
        cfg = load_config()
        if not is_admin(cfg, cq.from_user.id):
            await cq.answer("⛔", show_alert=True); return
        await _safe_edit(cq, main_text(cfg), kb_main(cfg))

    @dp.callback_query(F.data == _TOGGLE_ENABLED)
    async def on_toggle(cq: CallbackQuery) -> None:
        cfg = load_config()
        if not is_admin(cfg, cq.from_user.id):
            await cq.answer("⛔", show_alert=True); return
        cfg["enabled"] = not cfg["enabled"]
        save_config(cfg)
        await _safe_edit(cq, main_text(cfg), kb_main(cfg))

    @dp.callback_query(F.data == _SET_API)
    async def on_set_api(cq: CallbackQuery) -> None:
        cfg = load_config()
        if not is_admin(cfg, cq.from_user.id):
            await cq.answer("⛔", show_alert=True); return
        _DIALOG[cq.from_user.id] = {"want": "smmprime_api_key"}
        await cq.message.answer(
            "🔑 Пришлите ваш API-ключ <b>SMMPrime</b> одним сообщением.\n"
            "(ЛК smmprime.com → Settings → API)"
        )
        await cq.answer()

    @dp.callback_query(F.data == _SET_GGSEL)
    async def on_set_ggsel(cq: CallbackQuery) -> None:
        cfg = load_config()
        if not is_admin(cfg, cq.from_user.id):
            await cq.answer("⛔", show_alert=True); return
        _DIALOG[cq.from_user.id] = {"want": "ggsel_seller_id"}
        await cq.message.answer(
            "🔑 Пришлите <b>GGSEL seller_id</b> (числовой ID продавца "
            "из ЛК GGSEL → API)."
        )
        await cq.answer()

    @dp.callback_query(F.data == _SET_ADMIN)
    async def on_set_admin(cq: CallbackQuery) -> None:
        cfg = load_config()
        if not is_admin(cfg, cq.from_user.id):
            await cq.answer("⛔", show_alert=True); return
        admins = ", ".join(str(x) for x in cfg.get("admin_ids", [])) or "—"
        _DIALOG[cq.from_user.id] = {"want": "admins"}
        await cq.message.answer(
            f"👤 Текущие админы: <code>{admins}</code>\n"
            "Пришлите ID админов через запятую (либо одну цифру)."
        )
        await cq.answer()

    @dp.callback_query(F.data == _CHECK_BAL)
    async def on_check_bal(cq: CallbackQuery) -> None:
        cfg = load_config()
        if not is_admin(cfg, cq.from_user.id):
            await cq.answer("⛔", show_alert=True); return
        try:
            d = _smm_client(cfg).get_balance()
            if isinstance(d, dict):
                txt = (f"💰 Баланс SMMPrime: <b>{d.get('balance', '?')}</b> "
                       f"{d.get('currency', '')}")
            else:
                txt = f"💰 {d!r}"
        except SMMPrimeAuthError as e:
            txt = (f"🔑 <b>SMMPrime отклонил API ключ.</b>\n{_h(str(e))}\n\n"
                   f"Текущий ключ: <code>{_mask(cfg.get('smmprime_api_key',''))}</code>")
        except SMMPrimeError as e:
            txt = f"❌ <b>Ошибка SMMPrime:</b>\n<code>{_h(_truncate(str(e), 300))}</code>"
        except Exception as e:  # noqa: BLE001
            txt = f"❌ <b>Неожиданная ошибка:</b> <code>{type(e).__name__}: {_h(str(e))}</code>"
        await cq.message.answer(txt)
        await cq.answer()

    @dp.callback_query(F.data == _LIST_SERVICES)
    async def on_list_svc(cq: CallbackQuery) -> None:
        cfg = load_config()
        if not is_admin(cfg, cq.from_user.id):
            await cq.answer("⛔", show_alert=True); return
        try:
            svcs = _smm_client(cfg).get_services()[:25]
        except SMMPrimeError as e:
            await cq.answer(f"❌ {e}", show_alert=True); return
        if not svcs:
            await cq.answer("Услуг нет.", show_alert=True); return
        lines = []
        for s in svcs:
            name = s.get("name", "")
            if isinstance(name, dict):
                name = name.get("ru") or name.get("en") or "?"
            lines.append(f"<code>{s.get('service')}</code> — {_h(_truncate(str(name), 60))}")
        await cq.message.answer("📃 Первые 25 услуг SMMPrime:\n" + "\n".join(lines))
        await cq.answer()

    @dp.callback_query(F.data == _LIST_BIND)
    async def on_list_bind(cq: CallbackQuery) -> None:
        cfg = load_config()
        if not is_admin(cfg, cq.from_user.id):
            await cq.answer("⛔", show_alert=True); return
        bs = load_bindings()
        await _safe_edit(cq,
                         f"🛒 <b>Связки GGSEL → SMMPrime ({len(bs)}):</b>",
                         kb_bindings(bs))

    @dp.callback_query(F.data == _ADD_BIND)
    async def on_add_bind(cq: CallbackQuery) -> None:
        cfg = load_config()
        if not is_admin(cfg, cq.from_user.id):
            await cq.answer("⛔", show_alert=True); return
        _DIALOG[cq.from_user.id] = {"want": "bind_lot"}
        await cq.message.answer(
            "➕ Пришлите <b>GGSEL lot_id</b> (числовой ID товара в GGSEL)."
        )
        await cq.answer()

    @dp.callback_query(F.data.startswith(f"{_BIND_DETAIL}:"))
    async def on_bind_detail(cq: CallbackQuery) -> None:
        cfg = load_config()
        if not is_admin(cfg, cq.from_user.id):
            await cq.answer("⛔", show_alert=True); return
        idx = int(cq.data.split(":")[2])
        bs = load_bindings()
        if not (0 <= idx < len(bs)):
            await cq.answer("связка пропала"); return
        b = bs[idx]
        txt = (
            f"<b>Связка #{idx+1}: {_h(b.get('title') or '—')}</b>\n"
            f"GGSEL lot_id: <code>{b.get('ggsel_lot_id')}</code>\n"
            f"SMMPrime service_id: <code>{b.get('service')}</code>\n"
            f"Имя услуги: {_h(b.get('service_name') or '—')}\n"
            f"min/max: <code>{b.get('min_quantity', 0)}</code> / "
            f"<code>{b.get('max_quantity', 0)}</code>\n"
            f"Dry-run: {'🟡 ВКЛ' if b.get('dry_run', True) else '⚪ ВЫКЛ'}\n"
            f"Статус: {'🟢 ВКЛ' if b.get('enabled', True) else '🔴 ВЫКЛ'}"
        )
        await _safe_edit(cq, txt, kb_binding(idx, b))

    @dp.callback_query(F.data.startswith(f"{_BIND_TOGGLE_ON}:"))
    async def on_bind_toggle_on(cq: CallbackQuery) -> None:
        cfg = load_config()
        if not is_admin(cfg, cq.from_user.id):
            await cq.answer("⛔", show_alert=True); return
        idx = int(cq.data.split(":")[2])
        bs = load_bindings()
        if 0 <= idx < len(bs):
            bs[idx]["enabled"] = not bs[idx].get("enabled", True)
            save_bindings(bs)
        await on_bind_detail(cq)

    @dp.callback_query(F.data.startswith(f"{_BIND_TOGGLE_DRY}:"))
    async def on_bind_toggle_dry(cq: CallbackQuery) -> None:
        cfg = load_config()
        if not is_admin(cfg, cq.from_user.id):
            await cq.answer("⛔", show_alert=True); return
        idx = int(cq.data.split(":")[2])
        bs = load_bindings()
        if 0 <= idx < len(bs):
            bs[idx]["dry_run"] = not bs[idx].get("dry_run", True)
            save_bindings(bs)
        await on_bind_detail(cq)

    @dp.callback_query(F.data.startswith(f"{_DEL_BIND}:"))
    async def on_del_bind(cq: CallbackQuery) -> None:
        cfg = load_config()
        if not is_admin(cfg, cq.from_user.id):
            await cq.answer("⛔", show_alert=True); return
        idx = int(cq.data.split(":")[2])
        bs = load_bindings()
        if 0 <= idx < len(bs):
            bs.pop(idx)
            save_bindings(bs)
        await _safe_edit(cq,
                         f"🛒 <b>Связки GGSEL → SMMPrime ({len(bs)}):</b>",
                         kb_bindings(bs))

    @dp.callback_query(F.data.startswith(f"{_BIND_REFRESH_SVC}:"))
    async def on_refresh_svc(cq: CallbackQuery) -> None:
        cfg = load_config()
        if not is_admin(cfg, cq.from_user.id):
            await cq.answer("⛔", show_alert=True); return
        idx = int(cq.data.split(":")[2])
        bs = load_bindings()
        if not (0 <= idx < len(bs)):
            await cq.answer("связка пропала"); return
        b = bs[idx]
        try:
            svc = _smm_client(cfg).get_service(int(b.get("service") or 0))
        except SMMPrimeError as e:
            await cq.answer(f"❌ {e}", show_alert=True); return
        if not svc:
            await cq.answer("Услуга в SMMPrime не найдена", show_alert=True)
            return
        name = svc.get("name")
        if isinstance(name, dict):
            name = name.get("ru") or name.get("en") or "?"
        b["service_name"] = str(name)
        b["service_category"] = str(svc.get("category", "") or "")
        try:
            b["min_quantity"] = int(svc.get("min") or 0)
            b["max_quantity"] = int(svc.get("max") or 0)
        except (TypeError, ValueError):
            pass
        bs[idx] = b
        save_bindings(bs)
        await cq.answer("✅ Инфо услуги обновлено")
        await on_bind_detail(cq)

    @dp.callback_query(F.data.startswith(f"{_BIND_TEST}:"))
    async def on_bind_test(cq: CallbackQuery) -> None:
        cfg = load_config()
        if not is_admin(cfg, cq.from_user.id):
            await cq.answer("⛔", show_alert=True); return
        idx = int(cq.data.split(":")[2])
        bs = load_bindings()
        if not (0 <= idx < len(bs)):
            await cq.answer("связка пропала"); return
        b = bs[idx]
        txt = (
            "🧪 <b>Параметры связки:</b>\n"
            f"GGSEL lot_id: <code>{b.get('ggsel_lot_id')}</code>\n"
            f"SMMPrime service_id: <code>{b.get('service')}</code>\n"
            f"Имя услуги: {_h(b.get('service_name') or '—')}\n"
            f"min/max: <code>{b.get('min_quantity', 0)}</code> / "
            f"<code>{b.get('max_quantity', 0)}</code>\n"
            f"Dry-run: {'🟡 ВКЛ' if b.get('dry_run', True) else '⚪ ВЫКЛ'}\n\n"
            "Чтобы реально проверить — сделайте тестовую покупку на GGSEL "
            "при включённом dry-run."
        )
        await cq.message.answer(txt)
        await cq.answer()

    @dp.callback_query(F.data.startswith(f"{_BIND_EDIT}:"))
    async def on_bind_edit(cq: CallbackQuery) -> None:
        cfg = load_config()
        if not is_admin(cfg, cq.from_user.id):
            await cq.answer("⛔", show_alert=True); return
        _, _, idx_s, field = cq.data.split(":", 3)
        idx = int(idx_s)
        bs = load_bindings()
        if not (0 <= idx < len(bs)):
            await cq.answer("связка пропала"); return
        cur = bs[idx].get(field, "")
        _DIALOG[cq.from_user.id] = {"want": "bind_edit", "idx": idx, "field": field}
        await cq.message.answer(
            f"✏ Поле <code>{field}</code>\nТекущее значение:\n"
            f"<pre>{_h(_truncate(str(cur), 1000)) or '—'}</pre>\n\n"
            "Пришлите новое значение одним сообщением. /default — сброс."
        )
        await cq.answer()

    # Глобальные шаблоны (cfg.templates[key])
    _GLOBAL_TPL_MAP = {
        _SET_ASK_LINK_TEXT: "ask_link",
        _SET_CONFIRM_TEXT: "confirm",
        _SET_CANCELLED_TEXT: "cancelled",
        _SET_OK_TEXT: "success",
        _SET_ERR_TEXT: "error",
        _SET_DRY_TEXT: "dry_run",
    }

    @dp.callback_query(F.data.in_(set(_GLOBAL_TPL_MAP.keys())))
    async def on_global_tpl(cq: CallbackQuery) -> None:
        cfg = load_config()
        if not is_admin(cfg, cq.from_user.id):
            await cq.answer("⛔", show_alert=True); return
        key = _GLOBAL_TPL_MAP[cq.data]
        cur = cfg.get("templates", {}).get(key) or DEFAULT_TEMPLATES.get(key, "")
        _DIALOG[cq.from_user.id] = {"want": "global_tpl", "key": key}
        await cq.message.answer(
            f"💬 Шаблон <b>{key}</b>\nТекущий текст:\n"
            f"<pre>{_h(_truncate(cur, 1200))}</pre>\n\n"
            "Пришлите новый текст одним сообщением. /default — сброс."
        )
        await cq.answer()

    @dp.callback_query(F.data == _PENDING_LIST)
    async def on_pending(cq: CallbackQuery) -> None:
        cfg = load_config()
        if not is_admin(cfg, cq.from_user.id):
            await cq.answer("⛔", show_alert=True); return
        p = load_pending()
        if not p:
            await _safe_edit(cq, "📋 <b>Pending пуст.</b>", kb_pending(p))
            return
        lines = []
        for oid, ps in list(p.items())[:30]:
            if not isinstance(ps, dict):
                continue
            lines.append(
                f"<code>{oid}</code> [{ps.get('status')}] "
                f"lot=<code>{ps.get('lot_id')}</code> "
                f"qty={ps.get('quantity')} dry={ps.get('dry_run')}"
            )
        await _safe_edit(cq,
                         "📋 <b>Заказы (pending):</b>\n" + "\n".join(lines),
                         kb_pending(p))

    @dp.callback_query(F.data.startswith(f"{_PENDING_PURGE_ASK}:"))
    async def on_purge_ask(cq: CallbackQuery) -> None:
        cfg = load_config()
        if not is_admin(cfg, cq.from_user.id):
            await cq.answer("⛔", show_alert=True); return
        group = cq.data.split(":")[2]
        kb = IKM(inline_keyboard=[
            [IKB(text=f"✅ Подтвердить очистку '{group}'",
                 callback_data=f"{_PENDING_PURGE_OK}:{group}")],
            [IKB(text="◀ Отмена", callback_data=_PENDING_LIST)],
        ])
        await _safe_edit(cq,
                         f"⚠ <b>Подтвердите очистку группы:</b> {group}",
                         kb)

    @dp.callback_query(F.data.startswith(f"{_PENDING_PURGE_OK}:"))
    async def on_purge_ok(cq: CallbackQuery) -> None:
        cfg = load_config()
        if not is_admin(cfg, cq.from_user.id):
            await cq.answer("⛔", show_alert=True); return
        group = cq.data.split(":")[2]
        n = pending_purge(group)
        await cq.answer(f"🧹 Удалено: {n}", show_alert=True)
        await on_pending(cq)

    @dp.callback_query(F.data == _HELP)
    async def on_help(cq: CallbackQuery) -> None:
        cfg = load_config()
        if not is_admin(cfg, cq.from_user.id):
            await cq.answer("⛔", show_alert=True); return
        kb = IKM(inline_keyboard=[[IKB(text="◀ Назад", callback_data=_MAIN)]])
        await _safe_edit(cq, _HELP_TEXT, kb)

    @dp.callback_query(F.data == _HELP_FLOW)
    async def on_help_flow(cq: CallbackQuery) -> None:
        cfg = load_config()
        if not is_admin(cfg, cq.from_user.id):
            await cq.answer("⛔", show_alert=True); return
        kb = IKM(inline_keyboard=[[IKB(text="◀ Назад", callback_data=_MAIN)]])
        await _safe_edit(cq, _HELP_FLOW_TEXT, kb)

    @dp.callback_query(F.data == _NOOP)
    async def on_noop(cq: CallbackQuery) -> None:
        await cq.answer()

    # ───────────────────────── текстовые ответы внутри диалога ──────────
    @dp.message(Command("default"))
    async def on_default(m: Message) -> None:
        d = _DIALOG.get(m.from_user.id)
        if not d:
            return
        cfg = load_config()
        if d.get("want") == "global_tpl":
            key = d["key"]
            cfg.setdefault("templates", {}).pop(key, None)
            save_config(cfg)
            _DIALOG.pop(m.from_user.id, None)
            await m.reply(f"♻ Шаблон <b>{key}</b> сброшен на стандартный.")
        elif d.get("want") == "bind_edit":
            idx, field = d["idx"], d["field"]
            bs = load_bindings()
            if 0 <= idx < len(bs):
                bs[idx][field] = "" if isinstance(bs[idx].get(field), str) else 0
                save_bindings(bs)
            _DIALOG.pop(m.from_user.id, None)
            await m.reply(f"♻ Поле <code>{field}</code> сброшено.")

    @dp.message(F.text & ~F.text.startswith("/"))
    async def on_text_dialog(m: Message) -> None:
        d = _DIALOG.get(m.from_user.id)
        if d is None:
            return
        cfg = load_config()
        if not is_admin(cfg, m.from_user.id):
            return
        want = d.get("want")
        text = (m.text or "").strip()

        if want == "smmprime_api_key":
            cfg["smmprime_api_key"] = text
            save_config(cfg)
            _DIALOG.pop(m.from_user.id, None)
            await m.reply(f"🔑 API-ключ SMMPrime сохранён: "
                          f"<code>{_mask(text)}</code>")
            return

        if want == "ggsel_seller_id":
            cfg["ggsel_seller_id"] = text
            save_config(cfg)
            _DIALOG[m.from_user.id] = {"want": "ggsel_api_key"}
            await m.reply("🔑 Теперь пришлите <b>GGSEL api_key</b>.")
            return

        if want == "ggsel_api_key":
            cfg["ggsel_api_key"] = text
            save_config(cfg)
            _DIALOG.pop(m.from_user.id, None)
            await m.reply("✅ GGSEL ключ сохранён.")
            return

        if want == "admins":
            ids = []
            for chunk in text.replace(";", ",").split(","):
                chunk = chunk.strip()
                if chunk.lstrip("-").isdigit():
                    ids.append(int(chunk))
            if not ids:
                await m.reply("❌ Не нашёл числовых ID.")
                return
            cfg["admin_ids"] = ids
            save_config(cfg)
            _DIALOG.pop(m.from_user.id, None)
            await m.reply(f"👤 Админы: <code>{', '.join(map(str, ids))}</code>")
            return

        if want == "bind_lot":
            _DIALOG[m.from_user.id] = {"want": "bind_title", "lot": text}
            await m.reply("Введите <b>название</b> связки (для удобства).")
            return

        if want == "bind_title":
            _DIALOG[m.from_user.id] = {
                "want": "bind_service", "lot": d["lot"], "title": text,
            }
            await m.reply(
                "Введите <b>SMMPrime service_id</b> (число из «📃 Список услуг»)."
            )
            return

        if want == "bind_service":
            try:
                svc = int(text)
                if svc <= 0:
                    raise ValueError
            except ValueError:
                await m.reply("❌ service_id должен быть положительным числом.")
                return
            b = _new_binding(d["lot"], d["title"], svc)
            bs = load_bindings()
            bs.append(b)
            save_bindings(bs)
            _DIALOG.pop(m.from_user.id, None)
            await m.reply(
                f"✅ Связка добавлена (DRY-RUN включён):\n"
                f"lot <code>{d['lot']}</code> → service <code>{svc}</code>\n"
                f"Откройте «🛒 Связки» → выберите её → 🔄 Обновить инфо "
                f"услуги, чтобы подтянуть имя/min/max."
            )
            return

        if want == "bind_edit":
            idx, field = d["idx"], d["field"]
            bs = load_bindings()
            if not (0 <= idx < len(bs)):
                await m.reply("❌ Связка пропала."); _DIALOG.pop(m.from_user.id, None); return
            if field == "service":
                try:
                    bs[idx][field] = int(text)
                except ValueError:
                    await m.reply("❌ service_id должен быть числом."); return
            else:
                bs[idx][field] = text
            save_bindings(bs)
            _DIALOG.pop(m.from_user.id, None)
            await m.reply(f"✏ Поле <code>{field}</code> обновлено.")
            return

        if want == "global_tpl":
            key = d["key"]
            cfg.setdefault("templates", {})[key] = text
            save_config(cfg)
            _DIALOG.pop(m.from_user.id, None)
            await m.reply(f"💬 Шаблон <b>{key}</b> обновлён.")
            return


# ─────────────────────────────────────────────────────────────────────────────
#  GGSEL POLLER — фоновый long-poll
# ─────────────────────────────────────────────────────────────────────────────
class GgselPoller(threading.Thread):
    """Отдельный поток (не event-loop), долбит GGSEL API и пушит события.

    Идемпотентность: видели invoice_id или message_id — не повторяем.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop,
                 notify_admin) -> None:
        super().__init__(daemon=True, name="ggsel-poller")
        self.loop = loop
        self.notify_admin = notify_admin
        self.seen_orders: set[str] = set()
        self.seen_messages: dict[str, set[str]] = {}
        self.active_chats: set[str] = set()
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def _client(self, cfg: dict) -> GgselClient:
        return GgselClient(
            cfg.get("ggsel_seller_id", ""),
            cfg.get("ggsel_api_key", ""),
            cfg.get("ggsel_api_url") or GGSEL_API_URL_DEFAULT,
        )

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                logger.exception("poller tick crashed")
            self._stop.wait(POLL_INTERVAL_SEC)

    def _tick(self) -> None:
        cfg = load_config()
        if not cfg.get("enabled"):
            return
        if not cfg.get("ggsel_seller_id") or not cfg.get("ggsel_api_key"):
            return
        client = self._client(cfg)

        # --- новые заказы ---
        try:
            orders = client.get_orders()
        except GgselError as e:
            logger.warning("get_orders: %s", e)
            orders = []
        for raw in orders:
            oid = str(raw.get("invoice_id") or raw.get("id") or "")
            if not oid or oid in self.seen_orders:
                continue
            self.seen_orders.add(oid)
            chat_id = str(raw.get("id_i") or raw.get("debate_id") or "")
            if chat_id:
                self.active_chats.add(chat_id)
            try:
                handle_new_order(cfg, client, raw)
            except Exception:  # noqa: BLE001
                logger.exception("handle_new_order failed for %s", oid)
                self.notify_admin(
                    f"❌ <b>[GGSELBot]</b> handle_new_order упал по "
                    f"<code>{oid}</code>"
                )

        # --- сообщения в активных чатах ---
        for chat_id in list(self.active_chats):
            try:
                msgs = client.get_chat_messages(chat_id, limit=20)
            except GgselError as e:
                logger.warning("get_chat_messages(%s): %s", chat_id, e)
                continue
            seen = self.seen_messages.setdefault(chat_id, set())
            for raw in msgs:
                mid = str(raw.get("id") or raw.get("message_id") or "")
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                if raw.get("is_seller") or raw.get("owner_type") == "seller":
                    continue
                text = str(raw.get("message") or raw.get("text") or "")
                try:
                    handle_buyer_message(cfg, client, chat_id, text,
                                         self.notify_admin)
                except Exception:  # noqa: BLE001
                    logger.exception("handle_buyer_message failed")


# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────
async def amain() -> None:
    cfg = load_config()
    token = cfg.get("tg_bot_token")
    if not token:
        token = first_run_wizard()

    bot = Bot(token=token,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())

    def _notify_admin(text: str) -> None:
        """Шлёт сообщение всем админам. Безопасно вызывать из любого потока."""
        c = load_config()
        admins = c.get("admin_ids") or []
        loop = asyncio.get_event_loop()
        for adm in admins:
            try:
                asyncio.run_coroutine_threadsafe(
                    bot.send_message(int(adm), text), loop,
                )
            except Exception:  # noqa: BLE001
                logger.exception("notify_admin send failed")

    register_handlers(dp, bot)

    poller = GgselPoller(asyncio.get_event_loop(), _notify_admin)
    poller.start()

    me = await bot.get_me()
    logger.info("GGSELBot v%s started as @%s (id=%s)",
                VERSION, me.username, me.id)
    print(f"\n🤖 Бот запущен: @{me.username}. Откройте чат и /start.\n")

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        poller.stop()
        await bot.session.close()


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        print("\n👋 Останов по Ctrl+C.")


if __name__ == "__main__":
    main()

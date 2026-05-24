# =============================================================================
#  Плагин: SMMPrime Auto-Order для GGSEL Cardinal v1.0.0
#  Порт smmprime_plugin_v1_0_2.py (FunPay → GGSEL).
#
#  Сохранены (как в оригинале):
#    • state-machine: ASK_LINK → ASK_CONFIRM → DONE/DRY/ERR;
#    • шаблоны сообщений (ASK_LINK, CONFIRM, SUCCESS, ERROR, DRY, CANCELLED,
#      ALREADY_DONE, PRE_PURCHASE, QTY_TOO_SMALL, QTY_TOO_LARGE);
#    • DRY-RUN режим — поштучно у каждой связки;
#    • подтверждение перед созданием заказа (Да / Отмена);
#    • извлечение ссылки регэкспом из произвольного текста;
#    • валидация min/max количества по услуге SMMPrime;
#    • pending-заказы переживают перезапуск (JSON);
#    • Telegram-интерфейс: связки (поиск, сортировка, постраничный список),
#      редактирование шаблонов, баланс, список услуг, тесты, pending-список.
#
#  Адаптировано под GGSEL Cardinal:
#    • вместо ``Cardinal.account.get_lot_fields`` / ``save_lot`` зовём
#      ``cardinal.account.get_lot_fields`` / ``save_lot`` (GGSELAccount);
#    • вместо ``cardinal.account.send_message`` зовём
#      ``cardinal.account.send_message(chat_id, text)``;
#    • событие FunPay ``NEW_ORDER`` → GGSEL ``NEW_ORDER``;
#    • событие FunPay ``NEW_MESSAGE`` → GGSEL ``NEW_MESSAGE``.
# =============================================================================

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import requests
from telebot.types import (
    CallbackQuery,
    InlineKeyboardButton as IKB,
    InlineKeyboardMarkup as IKM,
    Message,
)

from tg_bot import CBT

# ─────────────────────────────────────────────────────────────────────────────
#  ОБЯЗАТЕЛЬНЫЕ ПОЛЯ ПЛАГИНА
# ─────────────────────────────────────────────────────────────────────────────
NAME = "SMMPrime Auto-Order"
VERSION = "1.0.0-ggsel"
DESCRIPTION = (
    "Связки GGSEL → SMMPrime: после оплаты плагин берёт количество из "
    "заказа GGSEL, проверяет min/max услуги SMMPrime, просит у "
    "покупателя ссылку, показывает сводку и по «Да» создаёт реальный "
    "заказ в SMMPrime (или dry-run). По «Отмена» — просит другую ссылку, "
    "заказ остаётся активным.\n\n"
    "Поддержка изменения цены лота GGSEL прямо из админ-чата, сортировка, "
    "поиск, постраничный список, DRY-RUN, pending переживает рестарт."
)
CREDITS = "@radcswd"
UUID = "8e5f4d3c-2a1b-4c9e-9f7a-1b2c3d4e5f6a"
SETTINGS_PAGE = True
BIND_TO_DELETE = None

# ─────────────────────────────────────────────────────────────────────────────
#  Константы
# ─────────────────────────────────────────────────────────────────────────────
logger = logging.getLogger("ggsel.smmprime")

SMMPRIME_API_URL = "https://smmprime.com/api/v2"
REQUEST_TIMEOUT = 15

CONFIG_NAME = "smmprime_config.json"
ORDERS_LOG_NAME = "smmprime_orders.log"
PENDING_NAME = "smmprime_pending_orders.json"

# Callback-префиксы (короткие, чтобы влезать в 64 байта callback_data).
_CB = "SMMP"
_MAIN = f"{_CB}:m"
_TOGGLE_ENABLED = f"{_CB}:tge"
_SET_API = f"{_CB}:sak"
_SET_API_URL = f"{_CB}:sau"
_SET_OK_TEXT = f"{_CB}:sok"
_SET_ERR_TEXT = f"{_CB}:ser"
_SET_DRY_TEXT = f"{_CB}:sdr"
_SET_ASK_LINK_TEXT = f"{_CB}:sal"
_SET_CONFIRM_TEXT = f"{_CB}:scf"
_SET_CANCELLED_TEXT = f"{_CB}:scn"
_LIST_BIND = f"{_CB}:l"
_LIST_BIND_PAGE = f"{_CB}:lp"
_BIND_SEARCH = f"{_CB}:bs"
_BIND_SEARCH_RESET = f"{_CB}:bsr"
_BIND_SORT_CYCLE = f"{_CB}:bsrt"
_ADD_BIND = f"{_CB}:a"
_DEL_BIND = f"{_CB}:d"
_BIND_DETAIL = f"{_CB}:i"
_BIND_EDIT = f"{_CB}:ie"            # SMMP:ie:<idx>:<field>
_BIND_TOGGLE_DRY = f"{_CB}:btd"     # SMMP:btd:<idx>
_BIND_TOGGLE_ON = f"{_CB}:bte"      # SMMP:bte:<idx>
_BIND_REFRESH_SVC = f"{_CB}:brs"    # SMMP:brs:<idx>
_BIND_PRICE_VIEW = f"{_CB}:bpv"     # SMMP:bpv:<idx>
_BIND_PRICE_EDIT = f"{_CB}:bpe"     # SMMP:bpe:<idx>
_CHECK_BAL = f"{_CB}:b"
_LIST_SERVICES = f"{_CB}:svc"
_PENDING_LIST = f"{_CB}:pl"
_PENDING_DEL = f"{_CB}:pd"
_PENDING_PURGE = f"{_CB}:pp"
_HELP = f"{_CB}:h"
_NOOP = f"{_CB}:noop"

_PAGE_SIZE = 10
_SHORT_LABEL_LIMIT = 30

_SORT_MODES = ("newest", "oldest", "cheap", "expensive", "title")
_SORT_LABELS = {
    "newest": "🆕 Сначала новые",
    "oldest": "🕰 Сначала старые",
    "cheap": "💵▼ Дешёвые сверху",
    "expensive": "💵▲ Дорогие сверху",
    "title": "🔤 По названию (А→Я)",
}

_URL_RE = re.compile(
    r"(?:https?://|(?:www\.|t\.me/|vk\.com/|instagram\.com/|youtube\.com/))\S+",
    re.IGNORECASE,
)
_QUANTITY_RE = re.compile(r"(?<![A-zА-яЁё0-9])(\d{1,9})(?![A-zА-яЁё0-9])")
_LEADING_GARBAGE_RE = re.compile(
    r"^[\s]*[^A-Za-zА-Яа-яЁё0-9«\"'\(\[\{]+[\s]*"
)

_FUNPAY_PRICE_COOLDOWN_SEC = 30
_PRICE_LAST_EDIT: dict[str, float] = {}


# ─────────────────────────────────────────────────────────────────────────────
#  ШАБЛОНЫ ПО УМОЛЧАНИЮ (взяты из smmprime_plugin_v1_0_2.py)
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
_DEFAULT_SUCCESS_TEMPLATE = (
    "{buyer_username}, ваш заказ успешно оформлен. Спасибо за покупку!\n\n"
    "Услуга SMMPrime: {service_id}\n"
    "Количество: {quantity}\n"
    "Ссылка: {link}\n"
    "Номер заказа SMMPrime: {smm_order_id}\n"
    "Заказ GGSEL: {ggsel_order_id}"
)
_DEFAULT_ERROR_TEMPLATE = (
    "{buyer_username}, к сожалению, не получилось оформить заказ "
    "автоматически. Не переживайте — продавец увидит ваш заказ "
    "и оформит его вручную в ближайшее время.\n\n"
    "Если есть уточнения — напишите их в этот же чат, продавец прочитает.\n\n"
    "Заказ GGSEL: {ggsel_order_id}\n"
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
    "Заказ GGSEL: {ggsel_order_id}"
)
_DEFAULT_ALREADY_DONE_TEMPLATE = (
    "Этот заказ уже оформлен ранее — повторно создавать его не нужно. "
    "Если что-то пошло не так, напишите продавцу — он подскажет."
)
_DEFAULT_NOT_LINK_TEMPLATE = (
    "Кажется, это не похоже на ссылку. Пришлите, пожалуйста, "
    "корректную ссылку одним сообщением."
)
_DEFAULT_NOT_CONFIRM_TEMPLATE = (
    "Не совсем понял ответ. Напишите Да — чтобы оформить заказ, "
    "или Отмена — чтобы изменить ссылку."
)
_DEFAULT_QTY_TOO_SMALL_TEMPLATE = (
    "{buyer_username}, к сожалению, для этого товара минимальное "
    "количество — {min} шт., а в заказе указано {quantity} шт. "
    "Поэтому я не могу его автоматически оформить.\n\n"
    "Заказ GGSEL: {ggsel_order_id}\n"
    "Услуга SMMPrime: {service_id}"
)
_DEFAULT_QTY_TOO_LARGE_TEMPLATE = (
    "{buyer_username}, к сожалению, для этого товара максимальное "
    "количество — {max} шт., а в заказе указано {quantity} шт. "
    "Поэтому я не могу его автоматически оформить.\n\n"
    "Заказ GGSEL: {ggsel_order_id}\n"
    "Услуга SMMPrime: {service_id}"
)


# ─────────────────────────────────────────────────────────────────────────────
#  Состояние плагина (in-memory)
# ─────────────────────────────────────────────────────────────────────────────
_DIALOG: dict[int, dict] = {}            # tg_chat_id -> {"step": ..., "data": ...}
_BIND_SEARCH_STATE: dict[int, str] = {}  # tg_chat_id -> query
_PENDING_LOCK = threading.Lock()
_ORDER_PROCESSING_SET: set[str] = set()
_HANDLERS_REGISTERED = False


# ─────────────────────────────────────────────────────────────────────────────
#  SMMPrime API client (тот же, что в исходнике v1.0.2)
# ─────────────────────────────────────────────────────────────────────────────
class SMMPrimeError(Exception):
    pass


class SMMPrimeAuthError(SMMPrimeError):
    pass


class SMMPrimeClient:
    def __init__(self, api_key: str, base_url: str = SMMPRIME_API_URL,
                 timeout: int = REQUEST_TIMEOUT):
        self._api_key = (api_key or "").strip()
        self._base_url = base_url or SMMPRIME_API_URL
        self._timeout = timeout

    def _request(self, payload: dict):
        if not self._api_key:
            raise SMMPrimeAuthError("API-ключ не задан")
        data = {"key": self._api_key, **payload}
        r = requests.post(self._base_url, data=data, timeout=self._timeout)
        if r.status_code == 401:
            raise SMMPrimeAuthError(self._extract_err(r) or "Unauthorized")
        if r.status_code >= 400:
            raise SMMPrimeError(
                f"HTTP {r.status_code}: {self._extract_err(r) or r.reason}"
            )
        try:
            body = r.json()
        except ValueError as e:
            raise ValueError(f"Невалидный JSON от SMMPrime: {e}") from e
        if isinstance(body, dict) and "error" in body:
            err = str(body["error"])
            if "key" in err.lower() or "auth" in err.lower():
                raise SMMPrimeAuthError(err)
            raise SMMPrimeError(err)
        return body

    @staticmethod
    def _extract_err(r) -> str:
        try:
            j = r.json()
            if isinstance(j, dict):
                return str(j.get("detail") or j.get("error") or j.get("message") or "")
        except Exception:
            pass
        return (r.text or "")[:200]

    def get_services(self) -> list[dict]:
        body = self._request({"action": "services"})
        if not isinstance(body, list):
            raise SMMPrimeError(
                f"Ожидался список услуг, пришло: {type(body).__name__}"
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


# ─────────────────────────────────────────────────────────────────────────────
#  Storage helpers
# ─────────────────────────────────────────────────────────────────────────────
def _cfg_default() -> dict:
    return {
        "enabled": True,
        "api_key": "",
        "api_url": SMMPRIME_API_URL,
        "global_ask_link_text": "",
        "global_confirm_text": "",
        "global_cancelled_text": "",
        "global_success_text": "",
        "global_error_text": "",
        "global_dry_run_text": "",
        "global_qty_too_small_text": "",
        "global_qty_too_large_text": "",
        "bindings": [],
        "sort_mode": "newest",
    }


def _binding_default(seq: int) -> dict:
    return {
        "id": seq,
        "title": "",
        "ggsel_lot_id": "",
        "service_id": 0,
        "service_name": "",
        "service_category": "",
        "min_quantity": 0,
        "max_quantity": 0,
        "enabled": True,
        "dry_run": True,           # по умолчанию DRY — чтобы не списать впустую
        "price": 0.0,
        "created_at": int(time.time()),
        "ask_link_text": "",
        "confirm_text": "",
        "cancelled_text": "",
        "success_text": "",
        "error_text": "",
        "dry_run_text": "",
    }


def _load(cardinal) -> dict:
    raw = cardinal.storage.load(CONFIG_NAME, {})
    if not raw:
        return _cfg_default()
    cfg = _cfg_default()
    cfg.update(raw)
    cfg["bindings"] = [_normalize_binding(b) for b in cfg.get("bindings", [])]
    return cfg


def _save(cardinal, cfg: dict) -> None:
    cardinal.storage.save(CONFIG_NAME, cfg)


def _normalize_binding(b: dict) -> dict:
    base = _binding_default(int(b.get("id", 0) or 0))
    base.update({k: v for k, v in b.items() if v is not None})
    return base


def _bindings(cfg: dict) -> list[dict]:
    return cfg.get("bindings", [])


# ───────────────────────── pending orders ──────────────────────────────
def _load_pending(cardinal) -> dict:
    return cardinal.storage.load(PENDING_NAME, {}) or {}


def _save_pending(cardinal, pending: dict) -> None:
    cardinal.storage.save(PENDING_NAME, pending)


def _pending_upsert(cardinal, order: dict) -> None:
    with _PENDING_LOCK:
        pending = _load_pending(cardinal)
        pending[str(order["ggsel_order_id"])] = order
        _save_pending(cardinal, pending)


def _pending_get(cardinal, order_id) -> dict | None:
    pending = _load_pending(cardinal)
    return pending.get(str(order_id))


def _pending_delete(cardinal, order_id) -> dict | None:
    with _PENDING_LOCK:
        pending = _load_pending(cardinal)
        v = pending.pop(str(order_id), None)
        if v is not None:
            _save_pending(cardinal, pending)
        return v


def _pending_list(cardinal, statuses=None) -> list[dict]:
    pending = _load_pending(cardinal)
    out = list(pending.values())
    if statuses:
        out = [v for v in out if v.get("status") in statuses]
    out.sort(key=lambda v: v.get("created_at", 0), reverse=True)
    return out


def _pending_purge(cardinal, group: str) -> int:
    with _PENDING_LOCK:
        pending = _load_pending(cardinal)
        groups = {
            "wait": ("waiting_for_link", "waiting_for_confirm"),
            "dry": ("dry_done",),
            "done": ("done", "error"),
        }
        target = groups.get(group, ())
        before = len(pending)
        pending = {k: v for k, v in pending.items() if v.get("status") not in target}
        _save_pending(cardinal, pending)
        return before - len(pending)


def _pending_find_active_for_chat(cardinal, chat_id) -> dict | None:
    for v in _pending_list(cardinal,
                           statuses=("waiting_for_link",
                                     "waiting_for_confirm")):
        if str(v.get("chat_id")) == str(chat_id):
            return v
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  Утилиты
# ─────────────────────────────────────────────────────────────────────────────
def _mask(key: str) -> str:
    if not key:
        return "<пусто>"
    if len(key) <= 8:
        return key[:2] + "***"
    return key[:4] + "***" + key[-4:]


def _truncate(text: str, limit: int = 80) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _h(text) -> str:
    return (str(text or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


def _extract_link(text: str) -> str | None:
    if not text:
        return None
    m = _URL_RE.search(text)
    return m.group(0).rstrip(").,;!?»") if m else None


def _parse_confirm_reply(text: str) -> str | None:
    if not text:
        return None
    s = text.strip().lower()
    if s in ("да", "yes", "y", "+", "ок", "ok"):
        return "yes"
    if s in ("отмена", "no", "n", "-", "cancel", "стоп", "отменить"):
        return "no"
    return None


def _strip_leading_emoji(text: str) -> str:
    if not text:
        return text
    return _LEADING_GARBAGE_RE.sub("", text)


def _make_client(cfg: dict) -> SMMPrimeClient:
    return SMMPrimeClient(cfg.get("api_key", ""),
                          cfg.get("api_url", SMMPRIME_API_URL))


def _api_error_text(e: Exception, api_key: str) -> str:
    if isinstance(e, SMMPrimeAuthError):
        return (
            "🔑 <b>SMMPrime отклонил API ключ.</b>\n"
            f"Текущий ключ: <code>{_mask(api_key)}</code>"
        )
    if isinstance(e, SMMPrimeError):
        return f"❌ <b>Ошибка SMMPrime:</b>\n<code>{_h(_truncate(str(e), 300))}</code>"
    if isinstance(e, requests.RequestException):
        return f"❌ <b>Сетевая ошибка:</b>\n<code>{_h(_truncate(str(e), 300))}</code>"
    return f"❌ <b>Неожиданная ошибка:</b> <code>{type(e).__name__}</code>"


def _apply_service_info(b: dict, svc: dict) -> dict:
    name = svc.get("name")
    if isinstance(name, dict):
        name = name.get("ru") or name.get("en") or str(name)
    b["service_name"] = name or ""
    b["service_category"] = svc.get("category") or ""
    try:
        b["min_quantity"] = int(svc.get("min") or 0)
    except (TypeError, ValueError):
        pass
    try:
        b["max_quantity"] = int(svc.get("max") or 0)
    except (TypeError, ValueError):
        pass
    try:
        b["price"] = float(svc.get("rate") or 0)
    except (TypeError, ValueError):
        pass
    return b


def _log_order(cardinal, line: str) -> None:
    p = Path(cardinal.storage.path(ORDERS_LOG_NAME))
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}\n")
    except OSError as e:
        logger.error("[smmprime] не смог записать лог заказов: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
#  Темплейты: рендеринг
# ─────────────────────────────────────────────────────────────────────────────
class _SafeFormat(dict):
    def __missing__(self, key):  # noqa: D401
        return "{" + key + "}"


def _render(template: str, **vars_) -> str:
    if not template:
        return ""
    try:
        return template.format_map(_SafeFormat(**vars_))
    except Exception as e:  # noqa: BLE001
        logger.warning("[smmprime] ошибка рендера шаблона: %s", e)
        return template


def _resolve_text(cfg: dict, b: dict, kind: str) -> str:
    default_map = {
        "ask_link": _DEFAULT_ASK_LINK_TEMPLATE,
        "confirm": _DEFAULT_CONFIRM_TEMPLATE,
        "cancelled": _DEFAULT_CANCELLED_TEMPLATE,
        "success": _DEFAULT_SUCCESS_TEMPLATE,
        "error": _DEFAULT_ERROR_TEMPLATE,
        "dry_run": _DEFAULT_DRY_RUN_TEMPLATE,
        "already_done": _DEFAULT_ALREADY_DONE_TEMPLATE,
        "not_link": _DEFAULT_NOT_LINK_TEMPLATE,
        "not_confirm": _DEFAULT_NOT_CONFIRM_TEMPLATE,
        "qty_too_small": _DEFAULT_QTY_TOO_SMALL_TEMPLATE,
        "qty_too_large": _DEFAULT_QTY_TOO_LARGE_TEMPLATE,
    }
    field_map = {
        "ask_link": ("ask_link_text", "global_ask_link_text"),
        "confirm": ("confirm_text", "global_confirm_text"),
        "cancelled": ("cancelled_text", "global_cancelled_text"),
        "success": ("success_text", "global_success_text"),
        "error": ("error_text", "global_error_text"),
        "dry_run": ("dry_run_text", "global_dry_run_text"),
        "qty_too_small": (None, "global_qty_too_small_text"),
        "qty_too_large": (None, "global_qty_too_large_text"),
    }
    per_bind, global_key = field_map.get(kind, (None, None))
    if per_bind and b.get(per_bind):
        return b[per_bind]
    if global_key and cfg.get(global_key):
        return cfg[global_key]
    return default_map.get(kind, "")


def _build_vars(pending: dict, link: str | None = None) -> dict:
    return {
        "buyer_username": pending.get("buyer_username", ""),
        "buyer_id": pending.get("buyer_id", ""),
        "ggsel_order_id": pending.get("ggsel_order_id", ""),
        "lot_id": pending.get("lot_id", ""),
        "service_id": pending.get("service_id", ""),
        "quantity": pending.get("quantity", ""),
        "link": link or pending.get("link", ""),
        "smm_order_id": pending.get("smm_order_id", ""),
        "min": pending.get("min_quantity", ""),
        "max": pending.get("max_quantity", ""),
    }


def _send_buyer(cardinal, chat_id, text: str) -> None:
    if not text or not chat_id:
        return
    text = _strip_leading_emoji(text)
    try:
        cardinal.account.send_message(chat_id, text)
    except Exception as e:  # noqa: BLE001
        logger.exception("[smmprime] не смог отправить покупателю: %s", e)


def _notify_admin(cardinal, text: str) -> None:
    try:
        if cardinal.telegram:
            cardinal.telegram.notify_admins(text)
    except Exception as e:  # noqa: BLE001
        logger.debug("[smmprime] notify_admin: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
#  Event-handlers (BIND_TO_NEW_ORDER / NEW_MESSAGE / LAST_CHAT_MESSAGE_CHANGED)
# ─────────────────────────────────────────────────────────────────────────────

def _find_binding_for_order(cfg: dict, order) -> dict | None:
    """Привязываем заказ к binding по lot_id или по title (case-insensitive)."""
    lot_id = str(getattr(order, "lot_id", "") or "")
    title = (getattr(order, "title", "") or "").lower().strip()
    for b in _bindings(cfg):
        if lot_id and str(b.get("ggsel_lot_id") or "") == lot_id:
            return b
        if title and (b.get("title") or "").lower().strip() == title:
            return b
    return None


def handle_new_order(cardinal, event, *args) -> None:
    """Обработчик NEW_ORDER GGSEL."""
    try:
        order = event.order if event else None
        if not order:
            return
        cfg = _load(cardinal)
        if not cfg.get("enabled", True):
            return
        b = _find_binding_for_order(cfg, order)
        if not b or not b.get("enabled", True):
            logger.debug("[smmprime] нет binding для заказа %s", order.id)
            return

        # Создаём pending запись.
        pending = {
            "ggsel_order_id": str(order.id),
            "buyer_id": order.buyer_id,
            "buyer_username": order.buyer_username,
            "chat_id": order.chat_id,
            "lot_id": str(order.lot_id or ""),
            "title": order.title,
            "quantity": int(order.amount or 1),
            "service_id": int(b.get("service_id") or 0),
            "min_quantity": int(b.get("min_quantity") or 0),
            "max_quantity": int(b.get("max_quantity") or 0),
            "dry_run": bool(b.get("dry_run", True)),
            "status": "waiting_for_link",
            "link": "",
            "smm_order_id": "",
            "created_at": int(time.time()),
            "binding_id": b.get("id"),
        }

        # Проверка min/max — обрываем сразу, не доводим до spam-link-asking.
        if pending["min_quantity"] and pending["quantity"] < pending["min_quantity"]:
            text = _render(_resolve_text(cfg, b, "qty_too_small"),
                           **_build_vars(pending))
            _send_buyer(cardinal, order.chat_id, text)
            pending["status"] = "error"
            _pending_upsert(cardinal, pending)
            _notify_admin(
                cardinal,
                f"⚠ SMMPrime: заказ {order.id} — qty {pending['quantity']} "
                f"< min {pending['min_quantity']}",
            )
            return
        if pending["max_quantity"] and pending["quantity"] > pending["max_quantity"]:
            text = _render(_resolve_text(cfg, b, "qty_too_large"),
                           **_build_vars(pending))
            _send_buyer(cardinal, order.chat_id, text)
            pending["status"] = "error"
            _pending_upsert(cardinal, pending)
            _notify_admin(
                cardinal,
                f"⚠ SMMPrime: заказ {order.id} — qty {pending['quantity']} "
                f"> max {pending['max_quantity']}",
            )
            return

        _pending_upsert(cardinal, pending)
        _send_buyer(cardinal, order.chat_id,
                    _render(_resolve_text(cfg, b, "ask_link"),
                            **_build_vars(pending)))
        _log_order(cardinal,
                   f"ORDER ggsel={order.id} lot={order.lot_id} "
                   f"qty={pending['quantity']} → ASK_LINK")
    except Exception as e:  # noqa: BLE001
        logger.exception("[smmprime] handle_new_order упал: %s", e)


def handle_new_message(cardinal, event, *args) -> None:
    """Обработчик NEW_MESSAGE / LAST_CHAT_MESSAGE_CHANGED GGSEL."""
    try:
        msg = getattr(event, "message", None)
        if msg is None or getattr(msg, "is_my_message", False):
            return
        chat_id = msg.chat_id
        text = (msg.text or "").strip()
        if not text:
            return

        cfg = _load(cardinal)
        if not cfg.get("enabled", True):
            return

        pending = _pending_find_active_for_chat(cardinal, chat_id)
        if not pending:
            return  # нет активного pending для этого чата

        b = _binding_for_pending(cfg, pending)
        if not b:
            logger.warning(
                "[smmprime] binding пропал для pending %s",
                pending.get("ggsel_order_id"),
            )
            return

        if pending["status"] == "waiting_for_link":
            _handle_link_message(cardinal, cfg, b, pending, text)
        elif pending["status"] == "waiting_for_confirm":
            _handle_confirm_message(cardinal, cfg, b, pending, text)
    except Exception as e:  # noqa: BLE001
        logger.exception("[smmprime] handle_new_message упал: %s", e)


def _binding_for_pending(cfg: dict, pending: dict) -> dict | None:
    bid = pending.get("binding_id")
    for b in _bindings(cfg):
        if b.get("id") == bid:
            return b
    # fallback по lot_id
    lot_id = str(pending.get("lot_id") or "")
    for b in _bindings(cfg):
        if str(b.get("ggsel_lot_id") or "") == lot_id:
            return b
    return None


def _handle_link_message(cardinal, cfg, b, pending, text) -> None:
    link = _extract_link(text)
    if not link:
        _send_buyer(cardinal, pending["chat_id"],
                    _render(_resolve_text(cfg, b, "not_link"),
                            **_build_vars(pending)))
        return

    pending["link"] = link
    pending["status"] = "waiting_for_confirm"
    _pending_upsert(cardinal, pending)

    _send_buyer(cardinal, pending["chat_id"],
                _render(_resolve_text(cfg, b, "confirm"),
                        **_build_vars(pending, link=link)))


def _handle_confirm_message(cardinal, cfg, b, pending, text) -> None:
    decision = _parse_confirm_reply(text)
    if decision is None:
        _send_buyer(cardinal, pending["chat_id"],
                    _render(_resolve_text(cfg, b, "not_confirm"),
                            **_build_vars(pending)))
        return

    if decision == "no":
        pending["link"] = ""
        pending["status"] = "waiting_for_link"
        _pending_upsert(cardinal, pending)
        _send_buyer(cardinal, pending["chat_id"],
                    _render(_resolve_text(cfg, b, "cancelled"),
                            **_build_vars(pending)))
        return

    # decision == "yes"
    _create_smm_from_pending(cardinal, cfg, b, pending)


def _create_smm_from_pending(cardinal, cfg, b, pending) -> None:
    """Делает реальный (или dry-run) заказ в SMMPrime и завершает pending."""
    oid = str(pending["ggsel_order_id"])
    # Защита от двойного API-вызова.
    with _PENDING_LOCK:
        if oid in _ORDER_PROCESSING_SET:
            logger.info("[smmprime] %s уже обрабатывается — пропуск", oid)
            return
        _ORDER_PROCESSING_SET.add(oid)
    try:
        pending["status"] = "processing"
        _pending_upsert(cardinal, pending)

        if pending["dry_run"]:
            pending["status"] = "dry_done"
            _pending_upsert(cardinal, pending)
            _send_buyer(cardinal, pending["chat_id"],
                        _render(_resolve_text(cfg, b, "dry_run"),
                                **_build_vars(pending)))
            _log_order(cardinal,
                       f"DRY_RUN ggsel={oid} svc={pending['service_id']} "
                       f"qty={pending['quantity']} link={pending['link']}")
            return

        client = _make_client(cfg)
        try:
            resp = client.add_order(
                service=pending["service_id"],
                link=pending["link"],
                quantity=pending["quantity"],
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[smmprime] add_order упал: %s", e)
            pending["status"] = "error"
            _pending_upsert(cardinal, pending)
            _send_buyer(cardinal, pending["chat_id"],
                        _render(_resolve_text(cfg, b, "error"),
                                **_build_vars(pending)))
            _notify_admin(
                cardinal,
                f"❌ SMMPrime add_order упал для GGSEL {oid}:\n"
                f"{_h(_truncate(str(e), 500))}",
            )
            return

        smm_id = ""
        if isinstance(resp, dict):
            smm_id = str(resp.get("order") or resp.get("id") or "")
        pending["smm_order_id"] = smm_id
        pending["status"] = "done"
        _pending_upsert(cardinal, pending)

        _send_buyer(cardinal, pending["chat_id"],
                    _render(_resolve_text(cfg, b, "success"),
                            **_build_vars(pending)))
        _log_order(cardinal,
                   f"DONE ggsel={oid} smm={smm_id} "
                   f"svc={pending['service_id']} qty={pending['quantity']}")
    finally:
        with _PENDING_LOCK:
            _ORDER_PROCESSING_SET.discard(oid)


# ─────────────────────────────────────────────────────────────────────────────
#  TELEGRAM UI
# ─────────────────────────────────────────────────────────────────────────────

def _back_to_plugin_cb(offset: int = 0) -> str:
    return f"{CBT.EDIT_PLUGIN}:{UUID}:{offset}"


def _settings_prefix() -> str:
    return f"{CBT.PLUGIN_SETTINGS}:{UUID}:"


def _kb_main(cfg: dict) -> IKM:
    kb = IKM(row_width=2)
    on = cfg.get("enabled", True)
    kb.add(IKB(
        f"{'🟢 Включено' if on else '🔴 Выключено'}",
        callback_data=_TOGGLE_ENABLED,
    ))
    kb.row(
        IKB("🛒 Связки", callback_data=f"{_LIST_BIND_PAGE}:0"),
        IKB("💰 Баланс", callback_data=_CHECK_BAL),
    )
    kb.row(
        IKB("📃 Список услуг", callback_data=f"{_LIST_SERVICES}:0"),
        IKB("📑 Pending", callback_data=_PENDING_LIST),
    )
    kb.row(
        IKB("🔑 API ключ", callback_data=_SET_API),
        IKB("🌐 API URL", callback_data=_SET_API_URL),
    )
    kb.row(
        IKB("📝 Шаблон ASK_LINK", callback_data=_SET_ASK_LINK_TEXT),
        IKB("📝 Шаблон CONFIRM", callback_data=_SET_CONFIRM_TEXT),
    )
    kb.row(
        IKB("📝 OK", callback_data=_SET_OK_TEXT),
        IKB("📝 ERR", callback_data=_SET_ERR_TEXT),
        IKB("📝 DRY", callback_data=_SET_DRY_TEXT),
    )
    kb.row(IKB("ℹ️ Помощь", callback_data=_HELP))
    kb.row(IKB("« Назад к плагинам", callback_data=_back_to_plugin_cb(0)))
    return kb


def _main_text(cfg: dict) -> str:
    return (
        "<b>SMMPrime Auto-Order (GGSEL)</b>\n\n"
        f"Состояние: {'🟢 ВКЛ' if cfg.get('enabled', True) else '🔴 ВЫКЛ'}\n"
        f"API URL: <code>{_h(cfg.get('api_url') or SMMPRIME_API_URL)}</code>\n"
        f"API key: <code>{_h(_mask(cfg.get('api_key', '')))}</code>\n"
        f"Связок: <b>{len(_bindings(cfg))}</b>\n"
        f"Сортировка: {_SORT_LABELS.get(cfg.get('sort_mode', 'newest'), '—')}"
    )


def _short_label(b: dict) -> str:
    title = (b.get("title") or b.get("service_name") or "?").strip()
    if len(title) > _SHORT_LABEL_LIMIT:
        title = title[:_SHORT_LABEL_LIMIT - 1] + "…"
    badge = "🟢" if b.get("enabled", True) else "🔴"
    dry = "·DRY" if b.get("dry_run") else ""
    return f"{badge}{dry} {title}"


def _sort_bindings(items: list[dict], mode: str) -> list[dict]:
    if mode == "newest":
        return sorted(items, key=lambda b: b.get("created_at", 0), reverse=True)
    if mode == "oldest":
        return sorted(items, key=lambda b: b.get("created_at", 0))
    if mode == "cheap":
        return sorted(items, key=lambda b: float(b.get("price") or 0))
    if mode == "expensive":
        return sorted(items, key=lambda b: float(b.get("price") or 0), reverse=True)
    if mode == "title":
        return sorted(items, key=lambda b: (b.get("title") or "").lower())
    return items


def _filter_bindings(items: list[dict], query: str) -> list[dict]:
    q = (query or "").lower().strip()
    if not q:
        return items
    out = []
    for b in items:
        hay = " ".join([
            b.get("title", ""), b.get("service_name", ""),
            b.get("service_category", ""), str(b.get("ggsel_lot_id", "")),
            str(b.get("service_id", "")),
        ]).lower()
        if q in hay:
            out.append(b)
    return out


def _kb_bindings(cfg: dict, page: int, query: str) -> IKM:
    kb = IKM(row_width=2)
    items = _filter_bindings(_bindings(cfg), query)
    items = _sort_bindings(items, cfg.get("sort_mode", "newest"))
    total = len(items)
    pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * _PAGE_SIZE
    chunk = items[start:start + _PAGE_SIZE]

    for b in chunk:
        kb.add(IKB(
            _short_label(b),
            callback_data=f"{_BIND_DETAIL}:{b['id']}",
        ))

    nav = []
    if pages > 1:
        nav.append(IKB("«", callback_data=f"{_LIST_BIND_PAGE}:{page - 1}"
                       if page > 0 else _NOOP))
        nav.append(IKB(f"{page + 1}/{pages}", callback_data=_NOOP))
        nav.append(IKB("»", callback_data=f"{_LIST_BIND_PAGE}:{page + 1}"
                       if page < pages - 1 else _NOOP))
        kb.row(*nav)

    kb.row(
        IKB("🔍 Поиск", callback_data=_BIND_SEARCH),
        IKB("✖ Сброс", callback_data=_BIND_SEARCH_RESET),
        IKB(_SORT_LABELS.get(cfg.get("sort_mode", "newest"), "?"),
            callback_data=_BIND_SORT_CYCLE),
    )
    kb.row(
        IKB("➕ Добавить связку", callback_data=_ADD_BIND),
        IKB("« Меню", callback_data=_MAIN),
    )
    return kb


def _kb_binding(b: dict) -> IKM:
    kb = IKM(row_width=2)
    idx = b["id"]
    kb.row(
        IKB(
            "🟢 Связка ВКЛ" if b.get("enabled", True) else "🔴 Связка ВЫКЛ",
            callback_data=f"{_BIND_TOGGLE_ON}:{idx}",
        ),
        IKB(
            "🧪 DRY ВКЛ" if b.get("dry_run") else "💸 DRY ВЫКЛ",
            callback_data=f"{_BIND_TOGGLE_DRY}:{idx}",
        ),
    )
    kb.row(
        IKB("✏ Название", callback_data=f"{_BIND_EDIT}:{idx}:title"),
        IKB("🔢 Service ID", callback_data=f"{_BIND_EDIT}:{idx}:service_id"),
    )
    kb.row(
        IKB("🆔 GGSEL Lot ID", callback_data=f"{_BIND_EDIT}:{idx}:ggsel_lot_id"),
        IKB("🔄 Обновить info", callback_data=f"{_BIND_REFRESH_SVC}:{idx}"),
    )
    kb.row(
        IKB("💵 Цена GGSEL", callback_data=f"{_BIND_PRICE_VIEW}:{idx}"),
        IKB("🗑 Удалить", callback_data=f"{_DEL_BIND}:{idx}"),
    )
    kb.row(IKB("« Связки", callback_data=f"{_LIST_BIND_PAGE}:0"))
    return kb


def _binding_text(b: dict) -> str:
    return (
        f"<b>Связка #{b['id']}</b>\n"
        f"Название: <code>{_h(b.get('title') or '—')}</code>\n"
        f"GGSEL Lot ID: <code>{_h(b.get('ggsel_lot_id') or '—')}</code>\n"
        f"Service ID (SMMPrime): <code>{b.get('service_id') or '—'}</code>\n"
        f"Service: <code>{_h(b.get('service_name') or '—')}</code>\n"
        f"Категория: <code>{_h(b.get('service_category') or '—')}</code>\n"
        f"min / max: <code>{b.get('min_quantity')}/{b.get('max_quantity')}</code>\n"
        f"Цена за 1000: <code>{b.get('price')}</code>\n"
        f"Состояние: {'🟢' if b.get('enabled') else '🔴'} "
        f" / DRY: {'🧪 ВКЛ' if b.get('dry_run') else '💸 ВЫКЛ'}"
    )


# ─────────────────────────── TG-регистрация ────────────────────────────────

def get_settings_keyboard() -> IKM:
    kb = IKM()
    kb.add(IKB("⚙ Открыть настройки SMMPrime", callback_data=_MAIN))
    return kb


def get_settings_text() -> str:
    return (
        "⚙ <b>SMMPrime Auto-Order</b>\n\n"
        "Откройте панель настроек — там связки, баланс, шаблоны и pending."
    )


def _safe_edit(bot, chat_id, message_id, text, kb) -> None:
    try:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
    except Exception:
        try:
            bot.send_message(chat_id, text, reply_markup=kb)
        except Exception as e:  # noqa: BLE001
            logger.exception("[smmprime] _safe_edit упал: %s", e)


def _register_tg(cardinal) -> None:
    """Регистрирует все callback/message обработчики в Telegram-боте."""
    global _HANDLERS_REGISTERED
    if _HANDLERS_REGISTERED:
        return
    if not cardinal.telegram or not cardinal.telegram.bot:
        logger.info("[smmprime] Telegram не настроен — UI не регистрирую")
        return
    bot = cardinal.telegram.bot

    def _is_admin(uid: int) -> bool:
        return uid in cardinal.telegram.admin_ids

    # ── главный экран
    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith(_MAIN)
                                or (c.data or "").startswith(_settings_prefix()))
    def _on_main(call: CallbackQuery):
        if not _is_admin(call.from_user.id):
            bot.answer_callback_query(call.id, "🚫"); return
        cfg = _load(cardinal)
        _safe_edit(bot, call.message.chat.id, call.message.message_id,
                   _main_text(cfg), _kb_main(cfg))
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda c: c.data == _TOGGLE_ENABLED)
    def _on_toggle(call: CallbackQuery):
        if not _is_admin(call.from_user.id):
            bot.answer_callback_query(call.id, "🚫"); return
        cfg = _load(cardinal)
        cfg["enabled"] = not cfg.get("enabled", True)
        _save(cardinal, cfg)
        _safe_edit(bot, call.message.chat.id, call.message.message_id,
                   _main_text(cfg), _kb_main(cfg))
        bot.answer_callback_query(call.id,
                                  "ВКЛ" if cfg["enabled"] else "ВЫКЛ")

    # ── баланс
    @bot.callback_query_handler(func=lambda c: c.data == _CHECK_BAL)
    def _on_balance(call: CallbackQuery):
        if not _is_admin(call.from_user.id):
            bot.answer_callback_query(call.id, "🚫"); return
        cfg = _load(cardinal)
        try:
            bal = _make_client(cfg).get_balance()
            txt = f"💰 <b>Баланс SMMPrime</b>\n<code>{_h(json.dumps(bal, ensure_ascii=False))}</code>"
        except Exception as e:  # noqa: BLE001
            txt = _api_error_text(e, cfg.get("api_key", ""))
        kb = IKM(); kb.add(IKB("« Меню", callback_data=_MAIN))
        _safe_edit(bot, call.message.chat.id, call.message.message_id, txt, kb)
        bot.answer_callback_query(call.id)

    # ── список услуг
    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith(f"{_LIST_SERVICES}:"))
    def _on_services(call: CallbackQuery):
        if not _is_admin(call.from_user.id):
            bot.answer_callback_query(call.id, "🚫"); return
        offset = int((call.data or "0:0").split(":")[2] or 0)
        cfg = _load(cardinal)
        try:
            svcs = _make_client(cfg).get_services()
        except Exception as e:  # noqa: BLE001
            _safe_edit(bot, call.message.chat.id, call.message.message_id,
                       _api_error_text(e, cfg.get("api_key", "")),
                       IKM().add(IKB("« Меню", callback_data=_MAIN)))
            bot.answer_callback_query(call.id); return
        page_sz = 15
        chunk = svcs[offset:offset + page_sz]
        lines = [f"📃 <b>Услуги SMMPrime</b> ({offset + 1}–{offset + len(chunk)} из {len(svcs)})\n"]
        for s in chunk:
            sid = s.get("service") or s.get("id")
            name = s.get("name")
            if isinstance(name, dict):
                name = name.get("ru") or name.get("en") or "?"
            lines.append(f"<code>{sid}</code> — {_h(_truncate(str(name), 70))}")
        kb = IKM(row_width=3)
        if offset > 0:
            kb.add(IKB("« пред", callback_data=f"{_LIST_SERVICES}:{max(0, offset - page_sz)}"))
        if offset + page_sz < len(svcs):
            kb.add(IKB("след »", callback_data=f"{_LIST_SERVICES}:{offset + page_sz}"))
        kb.add(IKB("« Меню", callback_data=_MAIN))
        _safe_edit(bot, call.message.chat.id, call.message.message_id,
                   "\n".join(lines), kb)
        bot.answer_callback_query(call.id)

    # ── список связок
    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith(f"{_LIST_BIND_PAGE}:"))
    def _on_list(call: CallbackQuery):
        if not _is_admin(call.from_user.id):
            bot.answer_callback_query(call.id, "🚫"); return
        page = int((call.data or "0:0").split(":")[2] or 0)
        cfg = _load(cardinal)
        q = _BIND_SEARCH_STATE.get(call.from_user.id, "")
        items = _filter_bindings(_bindings(cfg), q)
        title = f"🛒 <b>Связки</b>: {len(items)} / {len(_bindings(cfg))}"
        if q:
            title += f"\nФильтр: <code>{_h(q)}</code>"
        _safe_edit(bot, call.message.chat.id, call.message.message_id,
                   title, _kb_bindings(cfg, page, q))
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda c: c.data == _BIND_SORT_CYCLE)
    def _on_sort(call: CallbackQuery):
        if not _is_admin(call.from_user.id):
            bot.answer_callback_query(call.id, "🚫"); return
        cfg = _load(cardinal)
        cur = cfg.get("sort_mode", "newest")
        nxt = _SORT_MODES[(_SORT_MODES.index(cur) + 1) % len(_SORT_MODES)] if cur in _SORT_MODES else "newest"
        cfg["sort_mode"] = nxt; _save(cardinal, cfg)
        _safe_edit(bot, call.message.chat.id, call.message.message_id,
                   "Сортировка обновлена", _kb_bindings(cfg, 0, ""))
        bot.answer_callback_query(call.id, _SORT_LABELS[nxt])

    @bot.callback_query_handler(func=lambda c: c.data == _BIND_SEARCH_RESET)
    def _on_search_reset(call: CallbackQuery):
        _BIND_SEARCH_STATE.pop(call.from_user.id, None)
        cfg = _load(cardinal)
        _safe_edit(bot, call.message.chat.id, call.message.message_id,
                   "Фильтр сброшен", _kb_bindings(cfg, 0, ""))
        bot.answer_callback_query(call.id, "✖ сброшен")

    @bot.callback_query_handler(func=lambda c: c.data == _BIND_SEARCH)
    def _on_search(call: CallbackQuery):
        _DIALOG[call.from_user.id] = {"step": "search"}
        bot.send_message(call.from_user.id,
                         "🔍 Введите подстроку для фильтра (или /cancel).")
        bot.answer_callback_query(call.id)

    # ── детальная карточка связки
    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith(f"{_BIND_DETAIL}:"))
    def _on_bind_detail(call: CallbackQuery):
        if not _is_admin(call.from_user.id):
            bot.answer_callback_query(call.id, "🚫"); return
        idx = int((call.data or ":0").split(":")[2])
        cfg = _load(cardinal)
        b = next((x for x in _bindings(cfg) if x["id"] == idx), None)
        if not b:
            bot.answer_callback_query(call.id, "Не найдено"); return
        _safe_edit(bot, call.message.chat.id, call.message.message_id,
                   _binding_text(b), _kb_binding(b))
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith(f"{_BIND_TOGGLE_ON}:"))
    def _on_toggle_bind(call: CallbackQuery):
        idx = int((call.data or ":0").split(":")[2])
        cfg = _load(cardinal)
        for b in _bindings(cfg):
            if b["id"] == idx:
                b["enabled"] = not b.get("enabled", True)
                _save(cardinal, cfg)
                _safe_edit(bot, call.message.chat.id, call.message.message_id,
                           _binding_text(b), _kb_binding(b))
                bot.answer_callback_query(call.id, "ok"); return
        bot.answer_callback_query(call.id, "Не найдено")

    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith(f"{_BIND_TOGGLE_DRY}:"))
    def _on_toggle_dry(call: CallbackQuery):
        idx = int((call.data or ":0").split(":")[2])
        cfg = _load(cardinal)
        for b in _bindings(cfg):
            if b["id"] == idx:
                b["dry_run"] = not b.get("dry_run", True)
                _save(cardinal, cfg)
                _safe_edit(bot, call.message.chat.id, call.message.message_id,
                           _binding_text(b), _kb_binding(b))
                bot.answer_callback_query(call.id, "ok"); return
        bot.answer_callback_query(call.id, "Не найдено")

    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith(f"{_BIND_REFRESH_SVC}:"))
    def _on_refresh(call: CallbackQuery):
        idx = int((call.data or ":0").split(":")[2])
        cfg = _load(cardinal)
        for b in _bindings(cfg):
            if b["id"] == idx:
                try:
                    svc = _make_client(cfg).get_service(b.get("service_id"))
                    if svc:
                        _apply_service_info(b, svc)
                        _save(cardinal, cfg)
                        bot.answer_callback_query(call.id, "✓ обновлено")
                    else:
                        bot.answer_callback_query(call.id, "услуга не найдена")
                except Exception as e:  # noqa: BLE001
                    bot.answer_callback_query(call.id, str(e)[:60])
                _safe_edit(bot, call.message.chat.id, call.message.message_id,
                           _binding_text(b), _kb_binding(b))
                return
        bot.answer_callback_query(call.id, "Не найдено")

    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith(f"{_BIND_EDIT}:"))
    def _on_bind_edit(call: CallbackQuery):
        _, _, idx_str, field = (call.data or ":::").split(":", 3)
        _DIALOG[call.from_user.id] = {
            "step": "edit_bind", "idx": int(idx_str), "field": field,
        }
        bot.send_message(call.from_user.id,
                         f"Введите новое значение для <b>{field}</b> "
                         f"(или /cancel):", parse_mode="HTML")
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith(f"{_DEL_BIND}:"))
    def _on_del(call: CallbackQuery):
        idx = int((call.data or ":0").split(":")[2])
        cfg = _load(cardinal)
        cfg["bindings"] = [b for b in _bindings(cfg) if b["id"] != idx]
        _save(cardinal, cfg)
        _safe_edit(bot, call.message.chat.id, call.message.message_id,
                   "Связка удалена", _kb_bindings(cfg, 0, ""))
        bot.answer_callback_query(call.id, "удалено")

    @bot.callback_query_handler(func=lambda c: c.data == _ADD_BIND)
    def _on_add(call: CallbackQuery):
        cfg = _load(cardinal)
        max_id = max((b["id"] for b in _bindings(cfg)), default=0)
        b = _binding_default(max_id + 1)
        b["title"] = f"Новая связка #{b['id']}"
        cfg["bindings"].append(b); _save(cardinal, cfg)
        _safe_edit(bot, call.message.chat.id, call.message.message_id,
                   _binding_text(b), _kb_binding(b))
        bot.answer_callback_query(call.id, "➕ добавлено")

    # ── редактирование цены GGSEL
    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith(f"{_BIND_PRICE_VIEW}:"))
    def _on_price_view(call: CallbackQuery):
        idx = int((call.data or ":0").split(":")[2])
        cfg = _load(cardinal)
        b = next((x for x in _bindings(cfg) if x["id"] == idx), None)
        if not b:
            bot.answer_callback_query(call.id, "Не найдено"); return
        lot_id = b.get("ggsel_lot_id") or ""
        text = f"<b>💵 Цена лота GGSEL</b>\n\nLot: <code>{_h(lot_id)}</code>\n"
        if lot_id and cardinal.account:
            try:
                lf = cardinal.account.get_lot_fields(lot_id)
                text += f"Текущая цена: <code>{_h(lf.fields.get('price', '—'))}</code>"
            except Exception as e:  # noqa: BLE001
                text += f"⚠ не удалось получить: <code>{_h(str(e))}</code>"
        else:
            text += "⚠ Lot ID не задан в связке"
        kb = IKM()
        kb.add(IKB("✏ Новая цена", callback_data=f"{_BIND_PRICE_EDIT}:{idx}"))
        kb.add(IKB("« Назад", callback_data=f"{_BIND_DETAIL}:{idx}"))
        _safe_edit(bot, call.message.chat.id, call.message.message_id, text, kb)
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith(f"{_BIND_PRICE_EDIT}:"))
    def _on_price_edit(call: CallbackQuery):
        idx = int((call.data or ":0").split(":")[2])
        _DIALOG[call.from_user.id] = {"step": "price_edit", "idx": idx}
        bot.send_message(call.from_user.id,
                         "Введите новую цену числом (например 199.99) "
                         "или /cancel.")
        bot.answer_callback_query(call.id)

    # ── шаблоны
    for cb_const, key, default in [
        (_SET_API, "api_key", ""),
        (_SET_API_URL, "api_url", SMMPRIME_API_URL),
        (_SET_ASK_LINK_TEXT, "global_ask_link_text", _DEFAULT_ASK_LINK_TEMPLATE),
        (_SET_CONFIRM_TEXT, "global_confirm_text", _DEFAULT_CONFIRM_TEMPLATE),
        (_SET_OK_TEXT, "global_success_text", _DEFAULT_SUCCESS_TEMPLATE),
        (_SET_ERR_TEXT, "global_error_text", _DEFAULT_ERROR_TEMPLATE),
        (_SET_DRY_TEXT, "global_dry_run_text", _DEFAULT_DRY_RUN_TEMPLATE),
    ]:
        def _make(cb_const=cb_const, key=key, default=default):
            @bot.callback_query_handler(func=lambda c, _cb=cb_const: c.data == _cb)
            def _h(call: CallbackQuery):
                if not _is_admin(call.from_user.id):
                    bot.answer_callback_query(call.id, "🚫"); return
                _DIALOG[call.from_user.id] = {
                    "step": "edit_global", "key": key,
                }
                cur = _load(cardinal).get(key) or default
                preview = _truncate(cur, 400)
                bot.send_message(
                    call.from_user.id,
                    f"Введите новое значение для <b>{key}</b>:\n"
                    f"Текущее: <code>{_h_safe(preview)}</code>\n\n"
                    f"(или /cancel)",
                    parse_mode="HTML",
                )
                bot.answer_callback_query(call.id)
        _make()

    # ── pending
    @bot.callback_query_handler(func=lambda c: c.data == _PENDING_LIST)
    def _on_pending(call: CallbackQuery):
        if not _is_admin(call.from_user.id):
            bot.answer_callback_query(call.id, "🚫"); return
        items = _pending_list(cardinal)
        if not items:
            txt = "📑 <b>Pending</b>\n\nПусто."
        else:
            lines = ["📑 <b>Pending</b>\n"]
            for v in items[:30]:
                lines.append(
                    f"#{_h(v.get('ggsel_order_id'))} "
                    f"[{v.get('status')}] qty={v.get('quantity')} "
                    f"link={_h(_truncate(v.get('link') or '—', 40))}"
                )
            txt = "\n".join(lines)
        kb = IKM()
        kb.add(IKB("🗑 Очистить done", callback_data=f"{_PENDING_PURGE}:done"))
        kb.add(IKB("🗑 Очистить dry", callback_data=f"{_PENDING_PURGE}:dry"))
        kb.add(IKB("« Меню", callback_data=_MAIN))
        _safe_edit(bot, call.message.chat.id, call.message.message_id, txt, kb)
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith(f"{_PENDING_PURGE}:"))
    def _on_purge(call: CallbackQuery):
        group = (call.data or ":").split(":")[2]
        n = _pending_purge(cardinal, group)
        bot.answer_callback_query(call.id, f"очищено {n}")

    # ── помощь
    @bot.callback_query_handler(func=lambda c: c.data == _HELP)
    def _on_help(call: CallbackQuery):
        text = (
            "ℹ <b>SMMPrime Auto-Order для GGSEL</b>\n\n"
            "1. Введите API ключ SMMPrime (smmprime.com).\n"
            "2. Жмите «💰 Баланс» — должно показать ваш баланс.\n"
            "3. «📃 Список услуг» — увидите числовые service_id.\n"
            "4. «🛒 Связки → ➕ Добавить связку», задайте:\n"
            "   • Название (для поиска),\n"
            "   • GGSEL Lot ID — то самое id товара на ggsel.net,\n"
            "   • Service ID SMMPrime,\n"
            "   • «🔄 Обновить info» — подтянет min/max и цену.\n"
            "5. Сначала DRY=ВКЛ — сделайте тестовый заказ.\n"
            "6. DRY=ВЫКЛ — боевой режим.\n\n"
            "При новом заказе GGSEL плагин пишет покупателю в чат, "
            "спрашивает ссылку, показывает сводку, ждёт «Да/Отмена» — "
            "и только после «Да» делает реальный API-вызов."
        )
        kb = IKM(); kb.add(IKB("« Меню", callback_data=_MAIN))
        _safe_edit(bot, call.message.chat.id, call.message.message_id, text, kb)
        bot.answer_callback_query(call.id)

    # ── обработка ввода (dialog state-machine)
    @bot.message_handler(func=lambda m: m.from_user and m.from_user.id in _DIALOG)
    def _on_dialog(msg: Message):
        if not _is_admin(msg.from_user.id):
            return
        d = _DIALOG.pop(msg.from_user.id, {})
        text = (msg.text or "").strip()
        if text == "/cancel":
            bot.send_message(msg.chat.id, "Отменено."); return

        step = d.get("step")
        if step == "search":
            _BIND_SEARCH_STATE[msg.from_user.id] = text
            cfg = _load(cardinal)
            kb = _kb_bindings(cfg, 0, text)
            bot.send_message(msg.chat.id,
                             f"🔍 Фильтр: <code>{_h_safe(text)}</code>", reply_markup=kb)
            return

        if step == "edit_global":
            cfg = _load(cardinal)
            cfg[d["key"]] = text
            _save(cardinal, cfg)
            bot.send_message(msg.chat.id, "✓ сохранено", reply_markup=_kb_main(cfg))
            return

        if step == "edit_bind":
            cfg = _load(cardinal)
            for b in _bindings(cfg):
                if b["id"] == d["idx"]:
                    field = d["field"]
                    if field == "service_id":
                        try:
                            b[field] = int(text)
                        except ValueError:
                            bot.send_message(msg.chat.id, "Нужно число"); return
                    else:
                        b[field] = text
                    _save(cardinal, cfg)
                    bot.send_message(msg.chat.id, "✓ сохранено",
                                     reply_markup=_kb_binding(b))
                    return
            bot.send_message(msg.chat.id, "Связка не найдена")
            return

        if step == "price_edit":
            try:
                new_price = float(text.replace(",", "."))
            except ValueError:
                bot.send_message(msg.chat.id, "Нужно число"); return
            cfg = _load(cardinal)
            b = next((x for x in _bindings(cfg) if x["id"] == d["idx"]), None)
            if not b:
                bot.send_message(msg.chat.id, "Связка не найдена"); return
            lot_id = b.get("ggsel_lot_id")
            if not lot_id:
                bot.send_message(msg.chat.id, "У связки нет Lot ID"); return
            now = time.time()
            last = _PRICE_LAST_EDIT.get(str(lot_id), 0)
            if now - last < _FUNPAY_PRICE_COOLDOWN_SEC:
                remain = int(_FUNPAY_PRICE_COOLDOWN_SEC - (now - last))
                bot.send_message(
                    msg.chat.id,
                    f"⏱ Кулдаун цены — подождите ещё {remain} сек.",
                )
                return
            try:
                lf = cardinal.account.get_lot_fields(lot_id)
                old = lf.fields.get("price", "?")
                lf.fields["price"] = str(new_price)
                ok = cardinal.account.save_lot(lf)
                if ok:
                    b["price"] = new_price
                    _save(cardinal, cfg)
                    _PRICE_LAST_EDIT[str(lot_id)] = now
                    bot.send_message(
                        msg.chat.id,
                        f"💵 Цена обновлена: было {old} → стало {new_price}",
                    )
                else:
                    bot.send_message(msg.chat.id, "❌ GGSEL вернул ошибку при save_lot")
            except Exception as e:  # noqa: BLE001
                bot.send_message(msg.chat.id, f"❌ {e}")
            return

    _HANDLERS_REGISTERED = True
    logger.info("[smmprime] Telegram-handlers зарегистрированы")


def _h_safe(s: str) -> str:
    """HTML-escape для send_message с parse_mode=HTML."""
    return _h(s)


# ─────────────────────────────────────────────────────────────────────────────
#  PRE_INIT — инициализация
# ─────────────────────────────────────────────────────────────────────────────
def _pre_init_handler(cardinal, *args) -> None:
    logger.info("[smmprime] PRE_INIT")
    # Гарантируем дефолтный конфиг.
    cfg = _load(cardinal)
    _save(cardinal, cfg)
    _register_tg(cardinal)


# ─────────────────────────────────────────────────────────────────────────────
#  ПРИВЯЗКА
# ─────────────────────────────────────────────────────────────────────────────
BIND_TO_PRE_INIT = [_pre_init_handler]
BIND_TO_NEW_ORDER = [handle_new_order]
BIND_TO_NEW_MESSAGE = [handle_new_message]
BIND_TO_LAST_CHAT_MESSAGE_CHANGED = [handle_new_message]

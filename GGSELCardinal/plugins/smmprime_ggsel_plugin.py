# =============================================================================
#  Плагин: SMMPrime Auto-Order для GGSELCardinal v1.0.0
#
#  Адаптация плагина SMMPrime Auto-Order (для FunPayCardinal) на GGSEL.
#  Сохранены:
#    • state-machine «жду ссылку → жду подтверждение → создаю заказ»;
#    • DRY-RUN режим;
#    • привязки (bindings) ggsel_lot_id → smmprime_service_id;
#    • шаблоны сообщений (ASK_LINK, CONFIRM, CANCELLED, SUCCESS, ERROR, DRY);
#    • pending-state в JSON, переживает перезапуск;
#    • UI в Telegram (главное меню SMMPrime + список связок + редакторы текста);
#    • безопасность: API-ключ только локально, никаких внешних утечек.
#
#  Что заменено (по сравнению с FPC-версией):
#    • cardinal.account.get_lot_fields/save_lot → GGSEL Account.get/save_lot;
#    • буфер сообщений FunPay → cardinal.send_message(chat_id, text);
#    • OrderShortcut пришёл из GGSELApi, но интерфейс по полям совпадает.
# =============================================================================
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import TYPE_CHECKING, Any

import requests
from telebot.types import (
    CallbackQuery,
    InlineKeyboardButton as IKB,
    InlineKeyboardMarkup as IKM,
    Message,
)

from tg_bot import CBT

if TYPE_CHECKING:
    from cardinal import Cardinal

# ────────────────────────────────────────── обязательные поля плагина
NAME = "SMMPrime Auto-Order"
VERSION = "1.0.0-ggsel"
DESCRIPTION = (
    "Связки GGSEL → SMMPrime. После оплаты заказа на GGSEL плагин "
    "просит у покупателя ссылку, показывает сводку и по «Да» создаёт "
    "реальный заказ в SMMPrime (или dry-run). По «Отмена» — просит "
    "другую ссылку."
)
CREDITS = "@your_username"
UUID = "ggsel-smmprime-9c2e58f4"
SETTINGS_PAGE = True
BIND_TO_DELETE = None

logger = logging.getLogger("GGSEL.smmprime")

SMMPRIME_API_URL = "https://smmprime.com/api/v2"
REQUEST_TIMEOUT = 15
CONFIG_PATH = "storage/smmprime_config.json"
PENDING_PATH = "storage/smmprime_pending_orders.json"
ORDERS_LOG_PATH = "storage/smmprime_orders.log"

# ────────────────────────────────────────── callback-data
_CB = "SMMG"
_MAIN = f"{_CB}:m"
_TOGGLE_ENABLED = f"{_CB}:tge"
_SET_API = f"{_CB}:sak"
_LIST_BIND = f"{_CB}:l"
_ADD_BIND = f"{_CB}:a"
_DEL_BIND = f"{_CB}:d"
_BIND_DETAIL = f"{_CB}:i"
_BIND_TOGGLE_DRY = f"{_CB}:btd"
_BIND_TOGGLE_ON = f"{_CB}:bte"
_CHECK_BAL = f"{_CB}:b"
_LIST_SERVICES = f"{_CB}:svc"
_PENDING_LIST = f"{_CB}:pl"
_PENDING_PURGE = f"{_CB}:pp"
_SET_TEMPLATE = f"{_CB}:tpl"        # SMMG:tpl:<key>
_BACK = f"{_CB}:bk"

_NOOP = "SMMG:noop"

_DIALOG: dict[int, dict] = {}
_PENDING_LOCK = threading.Lock()
_ORDER_PROCESSING_SET: set[str] = set()

_URL_RE = re.compile(
    r"(?:https?://|(?:www\.|t\.me/|vk\.com/|instagram\.com/|youtube\.com/))\S+",
    re.IGNORECASE,
)
_QUANTITY_RE = re.compile(r"(?<!\d)(\d{1,9})(?!\d)")

_SETTINGS_PREFIX = f"{CBT.PLUGIN_SETTINGS}:{UUID}:"

# ────────────────────────────────────────── дефолтные шаблоны
DEFAULT_TEMPLATES: dict[str, str] = {
    "ask_link": (
        "Здравствуйте! Спасибо за заказ — я бот-помощник продавца.\n\n"
        "Пришлите, пожалуйста, ссылку для продвижения одним сообщением. "
        "Например, ссылку на профиль, канал, видео или пост."
    ),
    "confirm": (
        "Проверьте, всё ли верно:\n\n"
        "Ссылка: {link}\n"
        "Количество: {quantity}\n"
        "Услуга SMMPrime: {service_id}\n\n"
        "Если всё правильно — напишите: Да.\n"
        "Если хотите изменить ссылку — напишите: Отмена."
    ),
    "cancelled": "Хорошо, пришлите, пожалуйста, новую ссылку одним сообщением.",
    "success": (
        "{buyer_username}, ваш заказ успешно оформлен. Спасибо за покупку!\n\n"
        "Услуга SMMPrime: {service_id}\n"
        "Количество: {quantity}\n"
        "Ссылка: {link}\n"
        "Номер заказа SMMPrime: {smm_order_id}\n"
        "GGSEL заказ: {ggsel_order_id}"
    ),
    "error": (
        "{buyer_username}, к сожалению, не получилось оформить заказ "
        "автоматически. Продавец увидит ваш заказ и оформит его вручную.\n\n"
        "GGSEL заказ: {ggsel_order_id}\n"
        "Услуга SMMPrime: {service_id}\n"
        "Количество: {quantity}"
    ),
    "dry_run": (
        "Тестовый режим — заказ принят, но реальный заказ в SMMPrime "
        "сейчас не создаётся (продавец проверяет настройку).\n\n"
        "{buyer_username}, ваши данные сохранены:\n"
        "Услуга SMMPrime: {service_id}\n"
        "Количество: {quantity}\n"
        "Ссылка: {link}\n"
        "GGSEL заказ: {ggsel_order_id}"
    ),
    "not_link": (
        "Кажется, это не похоже на ссылку. Пришлите, пожалуйста, "
        "корректную ссылку одним сообщением."
    ),
    "not_confirm": (
        "Не совсем понял ответ. Напишите Да — чтобы оформить заказ, "
        "или Отмена — чтобы изменить ссылку."
    ),
    "already_done": (
        "Этот заказ уже оформлен ранее — повторно создавать его не нужно. "
        "Если что-то пошло не так, напишите продавцу — он подскажет."
    ),
}


# ───────────────────────────────────────── конфиг и сторедж
def _atomic_write_json(path: str, data: dict | list) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _load_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        logger.warning("Corrupt JSON at %s, using defaults", path)
        return default


def load_cfg() -> dict:
    cfg = _load_json(CONFIG_PATH, {})
    cfg.setdefault("enabled", True)
    cfg.setdefault("smmprime_api_key", "")
    cfg.setdefault("smmprime_api_url", SMMPRIME_API_URL)
    cfg.setdefault("bindings", [])
    cfg.setdefault("templates", {})
    for k, v in DEFAULT_TEMPLATES.items():
        cfg["templates"].setdefault(k, v)
    return cfg


def save_cfg(cfg: dict) -> None:
    _atomic_write_json(CONFIG_PATH, cfg)


def load_pending() -> dict:
    with _PENDING_LOCK:
        return _load_json(PENDING_PATH, {})


def save_pending(p: dict) -> None:
    with _PENDING_LOCK:
        _atomic_write_json(PENDING_PATH, p)


def log_event(line: str) -> None:
    os.makedirs(os.path.dirname(ORDERS_LOG_PATH) or ".", exist_ok=True)
    with open(ORDERS_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}\n")


# ───────────────────────────────────────── SMMPrime API
class SmmPrimeError(RuntimeError):
    pass


def _smm_request(cfg: dict, action: str, **kwargs: Any) -> dict | list:
    url = cfg.get("smmprime_api_url") or SMMPRIME_API_URL
    key = cfg.get("smmprime_api_key", "")
    if not key:
        raise SmmPrimeError("Не задан API-ключ SMMPrime")
    payload = {"key": key, "action": action, **kwargs}
    try:
        r = requests.post(url, data=payload, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise SmmPrimeError(f"network: {exc}") from exc
    if r.status_code != 200:
        raise SmmPrimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    try:
        data = r.json()
    except ValueError as exc:
        raise SmmPrimeError(f"non-JSON: {r.text[:200]}") from exc
    if isinstance(data, dict) and data.get("error"):
        raise SmmPrimeError(str(data["error"]))
    return data


def smm_balance(cfg: dict) -> str:
    d = _smm_request(cfg, "balance")
    if isinstance(d, dict):
        return f"{d.get('balance', '?')} {d.get('currency', '')}"
    return str(d)


def smm_services(cfg: dict) -> list[dict]:
    d = _smm_request(cfg, "services")
    return d if isinstance(d, list) else []


def smm_add_order(cfg: dict, service_id: int, link: str, quantity: int) -> int:
    d = _smm_request(cfg, "add",
                     service=service_id, link=link, quantity=quantity)
    if isinstance(d, dict) and "order" in d:
        return int(d["order"])
    raise SmmPrimeError(f"add failed: {d!r}")


def smm_status(cfg: dict, order_id: int) -> dict:
    d = _smm_request(cfg, "status", order=order_id)
    return d if isinstance(d, dict) else {"raw": d}


# ───────────────────────────────────────── helpers для шаблонов
def _strip_leading_garbage(s: str) -> str:
    return re.sub(r"^[\s]*[^A-Za-zА-Яа-яЁё0-9«\"'\(\[\{]+[\s]*", "", s)


def _render(template: str, **kwargs: Any) -> str:
    try:
        return _strip_leading_garbage(template.format(**kwargs))
    except (KeyError, IndexError):
        return _strip_leading_garbage(template)


# ───────────────────────────────────────── обработчики событий движка
def handle_new_order(cardinal: "Cardinal", order) -> None:
    cfg = load_cfg()
    if not cfg["enabled"]:
        return
    binding = _find_binding(cfg, order.lot_id)
    if binding is None:
        logger.info("[SMMG] no binding for lot %s — skip", order.lot_id)
        return

    pending = load_pending()
    if order.id in pending and pending[order.id].get("status") == "done":
        cardinal.send_message(
            order.chat_id,
            _render(cfg["templates"]["already_done"],
                    buyer_username=order.buyer_username),
        )
        return

    pending[order.id] = {
        "status": "wait_link",
        "lot_id": order.lot_id,
        "service_id": binding["service_id"],
        "quantity": int(order.amount or 1),
        "chat_id": order.chat_id,
        "buyer_username": order.buyer_username,
        "dry_run": bool(binding.get("dry_run", False)),
        "created_at": time.time(),
    }
    save_pending(pending)

    cardinal.send_message(
        order.chat_id,
        _render(cfg["templates"]["ask_link"],
                buyer_username=order.buyer_username,
                quantity=order.amount),
    )
    log_event(f"NEW_ORDER ggsel={order.id} lot={order.lot_id} "
              f"-> service={binding['service_id']}")


def handle_new_message(cardinal: "Cardinal", msg) -> None:
    if getattr(msg, "is_my", False):
        return
    cfg = load_cfg()
    if not cfg["enabled"]:
        return
    chat_id = str(msg.chat_id)

    pending = load_pending()
    # ищем активный pending по chat_id
    order_id, p = None, None
    for oid, ps in pending.items():
        if str(ps.get("chat_id")) == chat_id and ps.get("status") not in (
            "done", "error",
        ):
            order_id, p = oid, ps
            break
    if order_id is None:
        return

    text = (msg.text or "").strip()
    status = p.get("status")

    if status == "wait_link":
        if not _URL_RE.search(text):
            cardinal.send_message(chat_id, cfg["templates"]["not_link"])
            return
        link = _URL_RE.search(text).group(0)
        p["link"] = link
        p["status"] = "wait_confirm"
        save_pending(pending)
        cardinal.send_message(
            chat_id,
            _render(cfg["templates"]["confirm"],
                    link=link, quantity=p["quantity"],
                    service_id=p["service_id"]),
        )
        return

    if status == "wait_confirm":
        low = text.lower()
        if low in ("да", "yes", "y", "ок", "ok", "+"):
            _create_smm_from_pending(cardinal, order_id, p, cfg)
            return
        if low in ("отмена", "cancel", "нет", "no", "-"):
            p["status"] = "wait_link"
            p.pop("link", None)
            save_pending(pending)
            cardinal.send_message(chat_id, cfg["templates"]["cancelled"])
            return
        cardinal.send_message(chat_id, cfg["templates"]["not_confirm"])
        return


def _create_smm_from_pending(
    cardinal: "Cardinal",
    order_id: str,
    p: dict,
    cfg: dict,
) -> None:
    # Защита от двойного API-вызова
    with _PENDING_LOCK:
        if order_id in _ORDER_PROCESSING_SET:
            logger.info("[SMMG] order %s already processing — skip", order_id)
            return
        _ORDER_PROCESSING_SET.add(order_id)

    try:
        pending = load_pending()
        if pending.get(order_id, {}).get("status") in ("done", "error"):
            return
        kw = dict(
            buyer_username=p.get("buyer_username", ""),
            quantity=p["quantity"],
            link=p.get("link", ""),
            service_id=p["service_id"],
            ggsel_order_id=order_id,
            smm_order_id="",
        )
        if p.get("dry_run"):
            cardinal.send_message(
                p["chat_id"],
                _render(cfg["templates"]["dry_run"], **kw),
            )
            p["status"] = "done"
            p["dry"] = True
            pending[order_id] = p
            save_pending(pending)
            log_event(f"DRY ok ggsel={order_id} link={p['link']}")
            return
        try:
            smm_id = smm_add_order(
                cfg, int(p["service_id"]), p["link"], int(p["quantity"]),
            )
            kw["smm_order_id"] = smm_id
            cardinal.send_message(
                p["chat_id"],
                _render(cfg["templates"]["success"], **kw),
            )
            p["status"] = "done"
            p["smm_order_id"] = smm_id
            pending[order_id] = p
            save_pending(pending)
            log_event(f"OK ggsel={order_id} smm={smm_id}")
            if cardinal.telegram:
                cardinal.telegram.send_notification(
                    f"✅ SMMPrime заказ создан: <b>{smm_id}</b>\n"
                    f"GGSEL: <code>{order_id}</code>\n"
                    f"Услуга: {p['service_id']} × {p['quantity']}",
                )
        except SmmPrimeError as exc:
            cardinal.send_message(
                p["chat_id"],
                _render(cfg["templates"]["error"], **kw),
            )
            p["status"] = "error"
            p["error"] = str(exc)
            pending[order_id] = p
            save_pending(pending)
            log_event(f"ERR ggsel={order_id} err={exc}")
            if cardinal.telegram:
                cardinal.telegram.send_notification(
                    f"❌ SMMPrime ошибка по GGSEL заказу <code>{order_id}</code>:\n"
                    f"<pre>{exc}</pre>",
                )
    finally:
        with _PENDING_LOCK:
            _ORDER_PROCESSING_SET.discard(order_id)


def _find_binding(cfg: dict, lot_id: str) -> dict | None:
    for b in cfg.get("bindings", []):
        if str(b.get("ggsel_lot_id")) == str(lot_id) and b.get("enabled", True):
            return b
    return None


# ───────────────────────────────────────── Telegram UI
def _main_kb(cfg: dict) -> IKM:
    kb = IKM()
    on = "🟢 Включено" if cfg["enabled"] else "🔴 Выключено"
    kb.row(IKB(on, callback_data=_TOGGLE_ENABLED))
    api_state = "✔" if cfg.get("smmprime_api_key") else "✘"
    kb.row(IKB(f"🔑 API-ключ {api_state}", callback_data=_SET_API))
    kb.row(IKB("💰 Баланс", callback_data=_CHECK_BAL),
           IKB("📃 Список услуг", callback_data=_LIST_SERVICES))
    kb.row(IKB("🛒 Связки", callback_data=_LIST_BIND),
           IKB("⏳ Pending", callback_data=_PENDING_LIST))
    kb.row(IKB("📝 Шаблоны", callback_data=f"{_SET_TEMPLATE}:_menu"))
    kb.row(IKB("⬅ Назад", callback_data=f"{CBT.EDIT_PLUGIN}:{UUID}:0"))
    return kb


def _bindings_kb(cfg: dict) -> IKM:
    kb = IKM()
    for i, b in enumerate(cfg.get("bindings", [])):
        flag = "🟢" if b.get("enabled", True) else "⚪"
        dry = " 🧪" if b.get("dry_run") else ""
        kb.row(IKB(
            f"{flag}{dry} lot {b.get('ggsel_lot_id')} → svc {b.get('service_id')}",
            callback_data=f"{_BIND_DETAIL}:{i}",
        ))
    kb.row(IKB("➕ Добавить связку", callback_data=_ADD_BIND))
    kb.row(IKB("⬅ Назад", callback_data=_MAIN))
    return kb


def _bind_detail_kb(idx: int, b: dict) -> IKM:
    kb = IKM()
    dry = "🧪 DRY: ВКЛ" if b.get("dry_run") else "🧪 DRY: выкл"
    on = "🔴 Выключить" if b.get("enabled", True) else "🟢 Включить"
    kb.row(IKB(dry, callback_data=f"{_BIND_TOGGLE_DRY}:{idx}"),
           IKB(on, callback_data=f"{_BIND_TOGGLE_ON}:{idx}"))
    kb.row(IKB("🗑 Удалить", callback_data=f"{_DEL_BIND}:{idx}"))
    kb.row(IKB("⬅ К списку", callback_data=_LIST_BIND))
    return kb


def _templates_kb() -> IKM:
    kb = IKM()
    for key in DEFAULT_TEMPLATES.keys():
        kb.row(IKB(f"📝 {key}", callback_data=f"{_SET_TEMPLATE}:{key}"))
    kb.row(IKB("⬅ Назад", callback_data=_MAIN))
    return kb


def _register_tg(cardinal: "Cardinal") -> None:
    bot = cardinal.telegram.bot

    def _safe_edit(c: CallbackQuery, text: str, kb: IKM | None) -> None:
        try:
            bot.edit_message_text(
                text, chat_id=c.message.chat.id,
                message_id=c.message.message_id,
                reply_markup=kb, disable_web_page_preview=True,
            )
        except Exception:  # noqa: BLE001
            bot.send_message(c.message.chat.id, text,
                             reply_markup=kb, disable_web_page_preview=True)
        try:
            bot.answer_callback_query(c.id)
        except Exception:  # noqa: BLE001
            pass

    # точка входа из FPC-меню «⚙ Настройки»
    @bot.callback_query_handler(
        func=lambda c: c.data and c.data.startswith(_SETTINGS_PREFIX))
    def on_settings(c: CallbackQuery) -> None:
        cfg = load_cfg()
        _safe_edit(c, _main_text(cfg), _main_kb(cfg))

    @bot.callback_query_handler(func=lambda c: c.data == _MAIN)
    def on_main(c: CallbackQuery) -> None:
        cfg = load_cfg()
        _safe_edit(c, _main_text(cfg), _main_kb(cfg))

    @bot.callback_query_handler(func=lambda c: c.data == _TOGGLE_ENABLED)
    def on_toggle(c: CallbackQuery) -> None:
        cfg = load_cfg()
        cfg["enabled"] = not cfg["enabled"]
        save_cfg(cfg)
        _safe_edit(c, _main_text(cfg), _main_kb(cfg))

    @bot.callback_query_handler(func=lambda c: c.data == _SET_API)
    def on_set_api(c: CallbackQuery) -> None:
        _DIALOG[c.from_user.id] = {"want": "api_key"}
        bot.send_message(c.message.chat.id,
                         "Пришлите ваш API-ключ SMMPrime одним сообщением.")
        bot.answer_callback_query(c.id)

    @bot.callback_query_handler(func=lambda c: c.data == _CHECK_BAL)
    def on_bal(c: CallbackQuery) -> None:
        cfg = load_cfg()
        try:
            txt = f"💰 Баланс SMMPrime: <b>{smm_balance(cfg)}</b>"
        except SmmPrimeError as exc:
            txt = f"❌ {exc}"
        bot.send_message(c.message.chat.id, txt)
        bot.answer_callback_query(c.id)

    @bot.callback_query_handler(func=lambda c: c.data == _LIST_SERVICES)
    def on_services(c: CallbackQuery) -> None:
        cfg = load_cfg()
        try:
            svcs = smm_services(cfg)[:20]
        except SmmPrimeError as exc:
            bot.answer_callback_query(c.id, f"❌ {exc}", show_alert=True)
            return
        if not svcs:
            bot.answer_callback_query(c.id, "Услуг нет", show_alert=True)
            return
        lines = [f"<code>{s.get('service')}</code> — {s.get('name', '')[:60]}"
                 for s in svcs]
        bot.send_message(c.message.chat.id,
                         "Первые 20 услуг:\n" + "\n".join(lines))
        bot.answer_callback_query(c.id)

    @bot.callback_query_handler(func=lambda c: c.data == _LIST_BIND)
    def on_list_bind(c: CallbackQuery) -> None:
        cfg = load_cfg()
        _safe_edit(c, "🛒 <b>Связки GGSEL → SMMPrime:</b>", _bindings_kb(cfg))

    @bot.callback_query_handler(func=lambda c: c.data == _ADD_BIND)
    def on_add_bind(c: CallbackQuery) -> None:
        _DIALOG[c.from_user.id] = {"want": "bind_lot"}
        bot.send_message(c.message.chat.id,
                         "Пришлите GGSEL lot_id (числовой ID товара).")
        bot.answer_callback_query(c.id)

    @bot.callback_query_handler(
        func=lambda c: c.data and c.data.startswith(f"{_BIND_DETAIL}:"))
    def on_bind_detail(c: CallbackQuery) -> None:
        idx = int(c.data.split(":")[2])
        cfg = load_cfg()
        try:
            b = cfg["bindings"][idx]
        except IndexError:
            bot.answer_callback_query(c.id, "связка пропала")
            return
        txt = (
            f"<b>Связка #{idx}</b>\n"
            f"GGSEL lot: <code>{b.get('ggsel_lot_id')}</code>\n"
            f"SMMPrime service: <code>{b.get('service_id')}</code>\n"
            f"DRY-RUN: {b.get('dry_run', False)}\n"
            f"Включена: {b.get('enabled', True)}"
        )
        _safe_edit(c, txt, _bind_detail_kb(idx, b))

    @bot.callback_query_handler(
        func=lambda c: c.data and c.data.startswith(f"{_BIND_TOGGLE_DRY}:"))
    def on_toggle_dry(c: CallbackQuery) -> None:
        idx = int(c.data.split(":")[2])
        cfg = load_cfg()
        try:
            cfg["bindings"][idx]["dry_run"] = not cfg["bindings"][idx].get("dry_run", False)
            save_cfg(cfg)
            on_bind_detail(c)
        except IndexError:
            bot.answer_callback_query(c.id, "связка пропала")

    @bot.callback_query_handler(
        func=lambda c: c.data and c.data.startswith(f"{_BIND_TOGGLE_ON}:"))
    def on_toggle_bind(c: CallbackQuery) -> None:
        idx = int(c.data.split(":")[2])
        cfg = load_cfg()
        try:
            cfg["bindings"][idx]["enabled"] = not cfg["bindings"][idx].get("enabled", True)
            save_cfg(cfg)
            on_bind_detail(c)
        except IndexError:
            bot.answer_callback_query(c.id, "связка пропала")

    @bot.callback_query_handler(
        func=lambda c: c.data and c.data.startswith(f"{_DEL_BIND}:"))
    def on_del_bind(c: CallbackQuery) -> None:
        idx = int(c.data.split(":")[2])
        cfg = load_cfg()
        if 0 <= idx < len(cfg["bindings"]):
            cfg["bindings"].pop(idx)
            save_cfg(cfg)
        _safe_edit(c, "🛒 <b>Связки GGSEL → SMMPrime:</b>", _bindings_kb(cfg))

    @bot.callback_query_handler(func=lambda c: c.data == _PENDING_LIST)
    def on_pending(c: CallbackQuery) -> None:
        p = load_pending()
        if not p:
            bot.answer_callback_query(c.id, "pending пуст", show_alert=True)
            return
        lines = []
        for oid, ps in list(p.items())[:30]:
            lines.append(
                f"<code>{oid}</code> [{ps.get('status')}] "
                f"lot={ps.get('lot_id')} qty={ps.get('quantity')}"
            )
        kb = IKM()
        kb.row(IKB("🗑 Очистить done/error", callback_data=_PENDING_PURGE))
        kb.row(IKB("⬅ Назад", callback_data=_MAIN))
        _safe_edit(c, "⏳ <b>Pending:</b>\n" + "\n".join(lines), kb)

    @bot.callback_query_handler(func=lambda c: c.data == _PENDING_PURGE)
    def on_pending_purge(c: CallbackQuery) -> None:
        p = load_pending()
        before = len(p)
        p = {k: v for k, v in p.items() if v.get("status") not in ("done", "error")}
        save_pending(p)
        bot.answer_callback_query(c.id, f"Удалено: {before - len(p)}")
        on_pending(c)

    @bot.callback_query_handler(
        func=lambda c: c.data and c.data.startswith(f"{_SET_TEMPLATE}:"))
    def on_set_template(c: CallbackQuery) -> None:
        key = c.data.split(":", 2)[2]
        if key == "_menu":
            _safe_edit(c, "📝 <b>Шаблоны сообщений:</b>", _templates_kb())
            return
        if key not in DEFAULT_TEMPLATES:
            bot.answer_callback_query(c.id, "unknown key")
            return
        cfg = load_cfg()
        cur = cfg["templates"].get(key, DEFAULT_TEMPLATES[key])
        bot.send_message(
            c.message.chat.id,
            f"Текущий шаблон <b>{key}</b>:\n<pre>{cur}</pre>\n\n"
            "Пришлите новый текст одним сообщением, либо команду /default "
            "чтобы вернуть стандартный.",
        )
        _DIALOG[c.from_user.id] = {"want": "tpl", "key": key}
        bot.answer_callback_query(c.id)

    # Универсальный msg-handler для всех «жду текст» диалогов
    @bot.message_handler(
        func=lambda m: _DIALOG.get(m.from_user.id) is not None
        and not (m.text or "").startswith("/"))
    def on_dialog_text(m: Message) -> None:
        d = _DIALOG.pop(m.from_user.id, None)
        if d is None:
            return
        cfg = load_cfg()
        want = d.get("want")
        text = (m.text or "").strip()
        if want == "api_key":
            cfg["smmprime_api_key"] = text
            save_cfg(cfg)
            bot.reply_to(m, "🔑 API-ключ сохранён.")
        elif want == "bind_lot":
            _DIALOG[m.from_user.id] = {"want": "bind_svc", "lot": text}
            bot.reply_to(m, "Теперь пришлите SMMPrime service_id (число).")
        elif want == "bind_svc":
            try:
                svc = int(text)
            except ValueError:
                bot.reply_to(m, "service_id должен быть числом.")
                return
            cfg["bindings"].append({
                "ggsel_lot_id": d["lot"],
                "service_id": svc,
                "dry_run": True,
                "enabled": True,
                "created_at": time.time(),
            })
            save_cfg(cfg)
            bot.reply_to(m,
                         f"✅ Связка добавлена (DRY-RUN включён): "
                         f"lot <code>{d['lot']}</code> → svc <code>{svc}</code>")
        elif want == "tpl":
            key = d["key"]
            cfg["templates"][key] = text
            save_cfg(cfg)
            bot.reply_to(m, f"📝 Шаблон <b>{key}</b> обновлён.")

    @bot.message_handler(commands=["default"])
    def on_default(m: Message) -> None:
        d = _DIALOG.get(m.from_user.id)
        if not d or d.get("want") != "tpl":
            return
        key = d["key"]
        _DIALOG.pop(m.from_user.id, None)
        cfg = load_cfg()
        cfg["templates"][key] = DEFAULT_TEMPLATES[key]
        save_cfg(cfg)
        bot.reply_to(m, f"♻ Шаблон <b>{key}</b> сброшен на стандартный.")


def _main_text(cfg: dict) -> str:
    binds = cfg.get("bindings", [])
    has_key = "✔" if cfg.get("smmprime_api_key") else "✘"
    return (
        f"🧩 <b>SMMPrime Auto-Order v{VERSION}</b>\n\n"
        f"Состояние: {'🟢 Включён' if cfg['enabled'] else '🔴 Выключен'}\n"
        f"API-ключ: {has_key}\n"
        f"Связок: {len(binds)}\n"
        f"URL: <code>{cfg.get('smmprime_api_url')}</code>"
    )


# ───────────────────────────────────────── lifecycle
def _pre_init_handler(cardinal: "Cardinal") -> None:
    logger.info("[SMMG] pre_init start, v=%s", VERSION)
    os.makedirs("storage", exist_ok=True)
    load_cfg()  # форс-создание дефолтного конфига
    if cardinal.telegram is None:
        logger.warning("[SMMG] telegram disabled — skip TG registration")
        return
    try:
        _register_tg(cardinal)
    except Exception:  # noqa: BLE001
        logger.exception("[SMMG] TG registration failed")
    logger.info("[SMMG] pre_init done")


BIND_TO_PRE_INIT = [_pre_init_handler]
BIND_TO_NEW_ORDER = [handle_new_order]
BIND_TO_NEW_MESSAGE = [handle_new_message]
BIND_TO_LAST_CHAT_MESSAGE_CHANGED = [handle_new_message]

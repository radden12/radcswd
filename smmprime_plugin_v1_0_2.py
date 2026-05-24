# =============================================================================
#  Плагин: SMMPrime Auto-Order для FunPayCardinal  v1.0.0
#
#  ИСТОРИЯ / FEATURE-PARITY:
#  -----------------------------------------------------------------------------
#  Этот плагин — точный аналог SMMPrime Auto-Order v3.16.0, но работает
#  через панель SMMPrime (https://smmprime.com). Весь UX, диалоги,
#  state-machine, кнопки и шаблоны — взяты ровно из v3.16, заменён только
#  поставщик услуг.
#
#  Что осталось как было (фичи v3.16):
#    • 💵 «Цена FunPay» — изменение цены лота на FunPay прямо из бота
#      через `cardinal.account.get_lot_fields` / `save_lot`, превью «было
#      X → станет Y», подтверждение, cooldown 30 сек, обработка всех
#      ошибок (CSRF протух, 401, лот заблокирован).
#    • 🔍 Поиск по title/funpay_lot_id (case-insensitive).
#    • 🔀 5 режимов сортировки списка связок:
#      🆕 Новые / 🕰 Старые / 💵▼ Дешёвые / 💵▲ Дорогие / 🔤 По названию.
#    • 👁 Превью текущего и дефолтного текстов в редакторах шаблонов
#      (ASK_LINK, OK, ERR, DRY, CONFIRM, CANCELLED, PREPUR).
#    • Тёплые покупательские шаблоны (без обвинительного тона).
#    • Pending-state с устойчивостью к перезапуску, DRY-RUN, авто-обновление
#      min/max услуг, поиск/пагинация связок.
#
#  Что добавилось в SMMPrime v1.0.0 (поверх v3.16):
#    • Конфигурируемый API URL: по дефолту https://smmprime.com/api/v2.
#      Если у вашего ключа другой endpoint — задайте поле
#      `smmprime_api_url` в `storage/smmprime_config.json`, перезапуск
#      не нужен (читается на каждый запрос через `_make_client(cfg)`).
#      Для большинства пользователей smmprime.com менять ничего не надо.
#    • Расширенный warning о legacy-конфигах: если рядом лежат
#      `storage/smmfast_config.json` или `storage/taplike_config.json` —
#      плагин их НЕ читает, но в логи пишется warning «связки нужно создать
#      заново под SMMPrime» (см. `_legacy_other_panels_warning()`).
#    • Шаблонная переменная для ID заказа SMMPrime по-прежнему называется
#      {smm_order_id} — тот же placeholder, что был в SMMFast v3.16. Если
#      вы вставляли её в кастомные buyer-шаблоны, она работает без правок.
#
#  API SMMPrime:
#    POST https://smmprime.com/api/v2
#    body (form-urlencoded): key=<API_KEY>&action=<services|balance|add|status>[, ...]
#    Стандартный SMM-panel v2 API (тот же, что был у SMMPrime).
#
#  Установка:
#    1) Положите файл в plugins/ FunPayCardinal.
#    2) Перезапустите FPC.
#    3) /menu → 🧩 Плагины → SMMPrime Auto-Order → ⚙ Настройки.
#    4) Введите API-ключ SMMPrime из ЛК https://smmprime.com.
#    5) Жмёте «💰 Баланс» — должно вернуть баланс. Если HTML/404 —
#       поменяйте URL через «🌐 SMMPrime API URL».
#    6) «📃 Список услуг» — увидите каталог с числовыми service_id.
#    7) «🛒 Связки → ➕ Добавить связку» с включённым DRY-RUN.
#    8) Тестовая покупка → DRY-RUN → выключаем DRY → боевой тест.
#
#  Безопасность:
#    • API-ключ хранится локально в storage/smmprime_config.json.
#    • Никуда, кроме SMMPrime API, не отправляется.
#    • Состояние pending-заказов сохраняется в JSON и переживает
#      перезапуск FPC.
#    • На любую ошибку плагин показывает понятное сообщение покупателю
#      и техническое — продавцу; заказ не создаётся «вслепую».
#
#  ИСТОРИЯ ПЕРЕХОДОВ:
#    • SMMPrime v3.14 → v3.15 → v3.16 — основная линейка, наработанная UX.
#    • TapLike v1.0.0 — попытка перейти на taplike.ru. Не получилась:
#      TapLike оказался peer-to-peer приложением, не SMM-панелью.
#    • SMMPrime v1.0.0 — переход на smmprime.com. **Настоящая** SMM-панель
#      со стандартным v2 API.
# =============================================================================

import json
import logging
import os
import re
import time
import requests

from threading import Lock, Thread
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cardinal import Cardinal

from telebot.types import (
    InlineKeyboardMarkup as IKM,
    InlineKeyboardButton as IKB,
    Message,
    CallbackQuery,
)

# ─────────────────────────────────────────────────────────────────────────────
#  CBT — модуль кардинала с константами callback'ов.
# ─────────────────────────────────────────────────────────────────────────────
_CBT_SOURCE = "?"
try:
    from tg_bot import CBT  # type: ignore
    _CBT_SOURCE = "real tg_bot.CBT"
except Exception as _imp_e:  # pragma: no cover
    class CBT:  # минимальный фолбэк
        PLUGIN_SETTINGS = "47"
        EDIT_PLUGIN = "45"
        PLUGINS_LIST = "44"
    _CBT_SOURCE = f"FALLBACK ({type(_imp_e).__name__}: {_imp_e})"

# ─────────────────────────────────────────────────────────────────────────────
#  ОБЯЗАТЕЛЬНЫЕ ПОЛЯ ПЛАГИНА
# ─────────────────────────────────────────────────────────────────────────────
NAME = "SMMPrime Auto-Order"
VERSION = "1.0.0"
DESCRIPTION = ("Связки FunPay→SMMPrime: после оплаты плагин берёт количество "
               "из заказа FunPay, проверяет min/max услуги SMMPrime, просит "
               "у покупателя ссылку, показывает сводку и по «Да» создаёт "
               "реальный заказ в SMMPrime (или dry-run). По «Отмена» — "
               "просит другую ссылку, заказ остаётся активным. "
               "v3.16: изменение цены лота FunPay прямо из админ-чата "
               "(FunPayAPI save_lot), сортировка списка связок, "
               "превью текущего шаблона перед редактированием, "
               "обновлённые дефолтные тексты для покупателя.")
CREDITS = "@your_username"
UUID = "9c2e58f4-3b1a-4d67-92f3-8e5b4a1c0d29"
SETTINGS_PAGE = True
BIND_TO_DELETE = None

# ─────────────────────────────────────────────────────────────────────────────
#  КОНСТАНТЫ
# ─────────────────────────────────────────────────────────────────────────────
logger = logging.getLogger("FPC.smmprime")

SMMPRIME_API_URL = "https://smmprime.com/api/v2"
REQUEST_TIMEOUT = 15
CONFIG_PATH = "storage/smmprime_config.json"
ORDERS_LOG_PATH = "storage/smmprime_orders.log"
PENDING_PATH = "storage/smmprime_pending_orders.json"

# Внутренние префиксы callback_data (короткие, чтобы влезать в 64 байта).
_CB = "SMMP"
_MAIN = f"{_CB}:m"
_TOGGLE_ENABLED = f"{_CB}:tge"
_SET_API = f"{_CB}:sak"
_SET_OK_TEXT = f"{_CB}:sok"
_SET_ERR_TEXT = f"{_CB}:ser"
_SET_DRY_TEXT = f"{_CB}:sdr"
_LIST_BIND = f"{_CB}:l"
_ADD_BIND = f"{_CB}:a"
_DEL_BIND = f"{_CB}:d"
_BIND_DETAIL = f"{_CB}:i"
_BIND_EDIT = f"{_CB}:ie"            # SMMF:ie:<idx>:<field>
_BIND_TEMPLATE = f"{_CB}:it"        # SMMF:it:<idx> — шаблон для публикации
_BIND_TEST = f"{_CB}:tst"           # SMMF:tst:<idx> — тест связки
_BIND_TEST_CONFIRM = f"{_CB}:tcf"   # SMMF:tcf:<idx>
_BIND_TOGGLE_DRY = f"{_CB}:btd"     # SMMF:btd:<idx>
_BIND_TOGGLE_ON = f"{_CB}:bte"      # SMMF:bte:<idx>
_CHECK_BAL = f"{_CB}:b"
_LIST_SERVICES = f"{_CB}:svc"
_HELP = f"{_CB}:h"
_HELP_FUNPAY = f"{_CB}:hf"
_HELP_FLOW = f"{_CB}:hflow"
_PENDING_LIST = f"{_CB}:pl"
# v3.8 — управление pending-заказами:
_PENDING_DEL_LIST = f"{_CB}:pdl"     # экран «выбрать заказ для удаления»
_PENDING_DEL_ASK = f"{_CB}:pda"      # SMMF:pda:<oid> — подтверждение
_PENDING_DEL_OK = f"{_CB}:pdo"       # SMMF:pdo:<oid> — собственно удаление
_PENDING_PURGE_ASK = f"{_CB}:ppa"    # SMMF:ppa:<group> — подтверждение purge
_PENDING_PURGE_OK = f"{_CB}:ppo"     # SMMF:ppo:<group> — собственно purge
# group ∈ {wait, dry, done}
# v3.9 — компактный список связок (2 колонки + пагинация + поиск):
_LIST_BIND_PAGE = f"{_CB}:lp"        # SMMF:lp:<page> — листание
_BIND_SEARCH = f"{_CB}:bs"           # вход в режим поиска (диалог)
_BIND_SEARCH_RESET = f"{_CB}:bsr"    # сброс фильтра поиска
_NOOP = f"{_CB}:noop"                # «занятая» кнопка-индикатор
# v3.10 — refresh service info + edit ask-texts:
_BIND_REFRESH_SVC = f"{_CB}:brs"     # SMMF:brs:<idx> — тянем свежую инфо услуги
# v3.11 — глобальные тексты для покупателя (полный набор из 7 шаблонов).
_SET_ASK_LINK_TEXT = f"{_CB}:sal"    # глобальный текст ask_link
_SET_ASK_QTY_TEXT = f"{_CB}:saq"     # глобальный текст ask_quantity
_SET_QTY_SMALL_TEXT = f"{_CB}:sqs"   # глобальный текст «количество < min»
_SET_QTY_LARGE_TEXT = f"{_CB}:sql"   # глобальный текст «количество > max»
# v3.12 — глобальные тексты для CONFIRM и CANCELLED.
_SET_CONFIRM_TEXT = f"{_CB}:scf"     # глобальный текст подтверждения
_SET_CANCELLED_TEXT = f"{_CB}:scn"   # глобальный текст отмены
# v3.15 — pre-purchase greeting (приветствие до покупки).
_TOGGLE_PREPUR = f"{_CB}:ppt"        # вкл/выкл приветствие до покупки
_SET_PREPUR_TEXT = f"{_CB}:pps"      # глобальный текст приветствия до покупки
# v3.16 — циклическая смена режима сортировки списка связок:
_BIND_SORT_CYCLE = f"{_CB}:bsrt"     # SMMF:bsrt — переключить сортировку
# v3.16 — изменение цены лота FunPay через бота (FunPayAPI save_lot):
_BIND_PRICE_VIEW = f"{_CB}:bpv"      # SMMF:bpv:<idx> — экран цены
_BIND_PRICE_EDIT = f"{_CB}:bpe"      # SMMF:bpe:<idx> — ввод новой цены
_BIND_PRICE_CONFIRM = f"{_CB}:bpc"   # SMMF:bpc:<idx> — подтверждение и save
# Кол-во связок на странице компактного списка:
_PAGE_SIZE = 10
# Лимит длины короткого лейбла (UTF-8 символов) на одной кнопке списка:
_SHORT_LABEL_LIMIT = 30
# v3.16 — режимы сортировки списка связок (циклически переключаются).
_BIND_SORT_MODES: tuple[str, ...] = (
    "newest", "oldest", "cheap", "expensive", "title",
)
_BIND_SORT_LABELS: dict[str, str] = {
    "newest": "🆕 Сначала новые",
    "oldest": "🕰 Сначала старые",
    "cheap": "💵▼ Дешёвые сверху",
    "expensive": "💵▲ Дорогие сверху",
    "title": "🔤 По названию (А→Я)",
}
# v3.16 — кулдаун между правками цены ОДНОГО лота на стороне плагина,
# чтобы FunPay не тротлил и не банил аккаунт за частые save_lot.
_FUNPAY_PRICE_COOLDOWN_SEC = 30
_FUNPAY_PRICE_LAST_EDIT: dict[str, float] = {}  # lot_id (str) -> ts
# v3.16 — пределы валидации новой цены, чтобы исключить случайные опечатки
# («1500000» вместо «150» при руках на скорую). Можно переопределить через
# cfg["funpay_price_min"] / cfg["funpay_price_max"].
_FUNPAY_PRICE_MIN_DEFAULT = 1.0
_FUNPAY_PRICE_MAX_DEFAULT = 999999.0

# Реальный формат callback кардинала на ⚙ Настройки.
_SETTINGS_PREFIX = f"{CBT.PLUGIN_SETTINGS}:{UUID}:"


def _back_to_plugin_cb(offset: int = 0) -> str:
    return f"{CBT.EDIT_PLUGIN}:{UUID}:{offset}"


_DIALOG: dict[int, dict] = {}
# Хранилище временных тест-заказов (TG): tg_chat_id -> {idx, link}
_PENDING_TEST: dict[int, dict] = {}
# v3.9 — текущий поисковый запрос по связкам на этого админа (in-memory).
# Ключ: tg-chat_id админа. Значение: подстрока для фильтра (lower-case).
_BIND_SEARCH_STATE: dict[int, str] = {}
# Lock на доступ к pending-orders файлу (in-process)
_PENDING_LOCK = Lock()
# FIX v1.0.2 — защита от двойного API-вызова.
# _ORDER_PROCESSING_SET: множество funpay_order_id, для которых прямо
# сейчас выполняется _create_smm_from_pending. Проверяется и обновляется
# внутри _PENDING_LOCK, чтобы гарантировать атомарность «прочитал статус
# → занял слот → вызвал API». Два потока (NEW_MESSAGE +
# LAST_CHAT_MESSAGE_CHANGED), прилетающих одновременно по сообщению «Да»,
# будут конкурировать за этот Lock, и только первый проставит запись —
# второй увидит, что слот уже занят, и выйдет.
_ORDER_PROCESSING_SET: set = set()
_HANDLERS_REGISTERED = False
_OWN_CB_HANDLERS: list = []
_OWN_MSG_HANDLERS: list = []

_URL_RE = re.compile(
    r"(?:https?://|(?:www\.|t\.me/|vk\.com/|instagram\.com/|youtube\.com/))\S+",
    re.IGNORECASE,
)
_FUNPAY_LOT_RE = re.compile(r"id=(\d+)|/(\d{6,})")
# v3.10: число «голым» (для парсинга quantity от покупателя). Допускаем
# пробелы/знаки `>`, `=`, `:` перед числом, и единицы измерения после.
_QUANTITY_RE = re.compile(r"(?<![A-zА-яЁё0-9])(\d{1,9})(?![A-zА-яЁё0-9])")
# v3.10: ведущие emoji/мусорные символы в начале buyer-сообщения.
# В чате FunPay некоторые символы (♀, 💃, эмодзи) ломаются и рендерятся как
# «птичка». Strip-им любое не-буквенное/не-цифровое в самом начале.
_LEADING_GARBAGE_RE = re.compile(
    r"^[\s]*[^A-Za-zА-Яа-яЁё0-9«\"'\(\[\{]+[\s]*"
)


# ─────────────────────────────────────────────────────────────────────────────
#  ШАБЛОНЫ ТЕКСТОВ ПО УМОЛЧАНИЮ — v3.11
# ─────────────────────────────────────────────────────────────────────────────
# ⚠ Для buyer-сообщений НЕ используем ведущие emoji — в чате FunPay они
#   иногда рендерятся как «птичка»/«💃»/мусорный символ.
#   Дополнительно: при рендеринге buyer-шаблона мы автоматически
#   strip-аем ведущие не-буквенные символы (см. _strip_leading_emoji),
#   так что даже если кто-то поставит эмодзи в кастомный текст —
#   покупатель его не увидит на первой позиции.
#
# v3.11 — порядок сценария обновлён по ТЗ покупателя:
#   1) бот просит ссылку  (ASK_LINK)
#   2) покупатель присылает ссылку
#   3) бот просит количество  (ASK_QUANTITY)
#   4) покупатель присылает число
#   5) проверка min/max  (QTY_TOO_SMALL / QTY_TOO_LARGE)
#   6) создание заказа SMMPrime или dry-run
#   7) финальное сообщение  (SUCCESS / ERROR / DRY_RUN)

# 1) Просьба ссылки (первое сообщение от плагина после покупки).
# v3.16 — мягче и приветливее, объясняем покупателю что от него нужно.
_DEFAULT_ASK_LINK_TEMPLATE = (
    "Здравствуйте! Спасибо за заказ — я бот-помощник продавца, "
    "помогу его оформить автоматически.\n\n"
    "Пришлите, пожалуйста, ссылку для продвижения одним сообщением. "
    "Например, ссылку на профиль, канал, видео или конкретный пост."
)
# Старое имя оставлено для обратной совместимости со старыми тестами.
_ASK_LINK_TEMPLATE = _DEFAULT_ASK_LINK_TEMPLATE

# 3) Просьба количества (после получения ссылки) — v3.12 укоротили.
# В v3.11 текст показывал min/max услуги — но в v3.12 min/max валидируются
# уже после получения количества (отдельные сообщения qty_too_small /
# qty_too_large), а здесь просто просим число.
_DEFAULT_ASK_QUANTITY_TEMPLATE = (
    "Укажите количество одним числом."
)

# v3.14 — подтверждение перед созданием заказа. По новому ТЗ:
# - количество ВСЕГДА берётся из заказа FunPay (плагин его не спрашивает);
# - если что-то не так со ссылкой, покупатель пишет «Отмена» — статус
#   возвращается к «жду ссылку», бот просит другую ссылку.
# v3.16 — формулировки чуть мягче и яснее для покупателя.
_DEFAULT_CONFIRM_TEMPLATE = (
    "Проверьте, всё ли верно:\n\n"
    "Ссылка: {link}\n"
    "Количество: {quantity}\n"
    "Услуга SMMPrime: {service_id}\n\n"
    "Если всё правильно — напишите: Да, и я оформлю заказ.\n"
    "Если хотите изменить ссылку — напишите: Отмена, и мы начнём заново."
)

# v3.14 — сообщение после «Отмена»: НЕ финальное, заказ продолжает
# жить, бот ждёт новую ссылку.
_DEFAULT_CANCELLED_TEMPLATE = (
    "Хорошо, пришлите, пожалуйста, новую ссылку одним сообщением."
)

# v3.13 — мягкие fallback-ответы на нераспознанный ввод покупателя
# (раньше плагин молчал). Шлются с rate-limit (см. _NOT_UNDERSTOOD_*).
# v3.16 — мягкие, без «не понял» в лоб.
_DEFAULT_NOT_LINK_TEMPLATE = (
    "Кажется, это не похоже на ссылку. Пришлите, пожалуйста, "
    "корректную ссылку одним сообщением."
)
_DEFAULT_NOT_NUMBER_TEMPLATE = (
    "Кажется, это не число. Укажите, пожалуйста, количество одной цифрой."
)
_DEFAULT_NOT_CONFIRM_TEMPLATE = (
    "Не совсем понял ответ. Напишите Да — чтобы оформить заказ, "
    "или Отмена — чтобы изменить ссылку."
)
# v3.13 — идемпотентный ответ при попытке снова обработать
# завершённый заказ.
# v3.16 — добавляем мягкое объяснение что делать.
_DEFAULT_ALREADY_DONE_TEMPLATE = (
    "Этот заказ уже оформлен ранее — повторно создавать его не нужно. "
    "Если что-то пошло не так, напишите продавцу — он подскажет."
)

# 5a) Количество меньше минимума. v3.15 — переписано по новому ТЗ:
# покупатель уже зафиксировал quantity на FunPay перед оплатой, поэтому
# просить его «исправить» бессмысленно. Пишем дружелюбное сообщение и
# отправляем разбираться к продавцу. Шаблон срабатывает ТОЛЬКО когда у
# услуги SMMPrime реально задана `min_quantity > 0` (после миграции
# `_migrate_legacy_bindings_v314` или после `services`-rebloom). Если у
# услуги min = 0 — это сообщение не вызывается.
_DEFAULT_QTY_TOO_SMALL_TEMPLATE = (
    "{buyer_username}, к сожалению, для этого товара минимальное "
    "количество — {min} шт., а в заказе указано {quantity} шт. "
    "Поэтому я не могу его автоматически оформить.\n\n"
    "Что делать: дождитесь, пока продавец отменит заказ — деньги вернутся "
    "автоматически. После этого вы сможете оформить новый заказ "
    "с количеством от {min} шт.\n\n"
    "FunPay заказ: {funpay_order_id}\n"
    "Услуга SMMPrime: {service_id}"
)

# 5b) Количество больше максимума. v3.15 — симметрично qty_too_small:
# срабатывает только когда `max_quantity > 0` реально задано.
_DEFAULT_QTY_TOO_LARGE_TEMPLATE = (
    "{buyer_username}, к сожалению, для этого товара максимальное "
    "количество — {max} шт., а в заказе указано {quantity} шт. "
    "Поэтому я не могу его автоматически оформить.\n\n"
    "Что делать: дождитесь, пока продавец отменит заказ — деньги вернутся "
    "автоматически. После этого вы сможете оформить новый заказ "
    "с количеством до {max} шт.\n\n"
    "FunPay заказ: {funpay_order_id}\n"
    "Услуга SMMPrime: {service_id}"
)

# v3.15 — приветствие до покупки (когда у покупателя нет активного pending,
# но он пишет в чат). По умолчанию ВЫКЛЮЧЕНО (см. cfg["pre_purchase_greeting_enabled"]),
# чтобы не наступать на Cardinal-автоответчик. Включается одной кнопкой
# в админ-меню Telegram. Rate-limit: 1 раз в _PRE_PURCHASE_COOLDOWN_SEC
# на чат, чтобы не спамить покупателя при многократных сообщениях.
_DEFAULT_PRE_PURCHASE_GREETING_TEMPLATE = (
    "Здравствуйте! Я бот-помощник продавца — помогу оформить заказ "
    "автоматически.\n\n"
    "Чтобы начать:\n"
    "1) Выберите нужный товар на странице продавца на FunPay.\n"
    "2) Укажите количество.\n"
    "3) Оплатите заказ.\n\n"
    "Сразу после оплаты я напишу вам в этот чат и помогу пройти "
    "оставшиеся шаги. Если будут вопросы — продавец на связи и "
    "ответит лично."
)

# 7a) Финал: успех (заказ создан в SMMPrime).
# v3.14: убрана строка «Товар FunPay: {lot_id}» по новому ТЗ.
# v3.16: добавили дружелюбное «спасибо за покупку».
_DEFAULT_SUCCESS_TEMPLATE = (
    "{buyer_username}, ваш заказ успешно оформлен. Спасибо за покупку!\n\n"
    "Услуга SMMPrime: {service_id}\n"
    "Количество: {quantity}\n"
    "Ссылка: {link}\n"
    "Номер заказа SMMPrime: {smm_order_id}\n"
    "FunPay заказ: {funpay_order_id}"
)

# 7b) Финал: ошибка (бот не смог автоматически оформить).
# v3.14: упрощено по новому ТЗ — без «Товар FunPay» и без хвоста
# «Продавец скоро проверит вручную».
# v3.16: возвращаем мягкое объяснение что произойдёт дальше — покупатель
# не должен думать «всё пропало».
_DEFAULT_ERROR_TEMPLATE = (
    "{buyer_username}, к сожалению, не получилось оформить заказ "
    "автоматически. Не переживайте — продавец увидит ваш заказ "
    "и оформит его вручную в ближайшее время.\n\n"
    "Если есть уточнения — напишите их в этот же чат, продавец прочитает.\n\n"
    "FunPay заказ: {funpay_order_id}\n"
    "Услуга SMMPrime: {service_id}\n"
    "Количество: {quantity}"
)

# 7c) Финал: dry-run (тестовый режим, реального заказа не создавали).
# v3.16: явно объясняем что это нестрашно — продавец просто проверяет настройку.
_DEFAULT_DRY_RUN_TEMPLATE = (
    "Тестовый режим — заказ принят, но реальный заказ в SMMPrime "
    "сейчас не создаётся (продавец проверяет настройку).\n\n"
    "{buyer_username}, ваши данные сохранены:\n"
    "Услуга SMMPrime: {service_id}\n"
    "Количество: {quantity}\n"
    "Ссылка: {link}\n"
    "FunPay заказ: {funpay_order_id}\n"
    "Товар FunPay: {lot_id}"
)

# v3.11 — single-source-of-truth для проверки диапазона: оба под-шаблона
# (TOO_SMALL / TOO_LARGE) ниже. Старая константа _QTY_OUT_OF_RANGE_TEMPLATE
# оставлена ради обратной совместимости со старыми тестами и тек.
# pending-данными (если она где-то используется как fallback).
_QTY_OUT_OF_RANGE_TEMPLATE = _DEFAULT_QTY_TOO_SMALL_TEMPLATE

# v3.11 — больше НЕ дёргаем покупателя автоматическими «не тот ввод»
# подсказками. Это создавало шум в чате (FunPay Terminal-приветствие
# может содержать число → бот думал «это quantity» → отвечал
# «теперь пришлите ссылку», что путало покупателя). Сейчас на ввод не
# того типа просто молча ждём дальше.
_WRONG_INPUT_NEEDS_QTY = ""
_WRONG_INPUT_NEEDS_LINK = ""

_ALREADY_DONE_TEMPLATE = (
    "{buyer_username}, заказ уже был создан ранее. SMMPrime ID: {smm_order_id}"
)


# ─────────────────────────────────────────────────────────────────────────────
#  v3.10 — ДЕТЕКЦИЯ ТИПА ССЫЛКИ ПО CATEGORY/NAME УСЛУГИ SMMPrime
# ─────────────────────────────────────────────────────────────────────────────
# Каждое правило: ((keywords...), link_type, link_example).
# Применяется first-match по конкатенации category+name (lower-case).
# Поэтому порядок важен: более специфичные правила — выше.
_LINK_TYPE_RULES: list[tuple[tuple[str, ...], str, str]] = [
    # ── Telegram ─────────────────────────────────────────────────────────
    (("telegram premium", "tg premium", "телеграм премиум", "тг премиум"),
     "telegram_premium", "@username  или  https://t.me/username"),
    (("telegram", "tg ", " tg", "tgr ", "телеграм", "тг", "tg-"),
     "telegram_channel",
     "https://t.me/your_channel  или  https://t.me/your_post/123"),
    # ── Instagram ────────────────────────────────────────────────────────
    (("instagram", "инстаграм", "инст", "ig "),
     "instagram_profile",
     "https://www.instagram.com/your_profile  "
     "или  https://www.instagram.com/p/POST_ID"),
    # ── YouTube ──────────────────────────────────────────────────────────
    (("youtube", "ютуб", "yt "),
     "youtube_video",
     "https://www.youtube.com/watch?v=VIDEO_ID  "
     "или  https://www.youtube.com/@your_channel"),
    # ── TikTok ───────────────────────────────────────────────────────────
    (("tiktok", "тикток", "tt "),
     "tiktok_profile",
     "https://www.tiktok.com/@your_profile  "
     "или  https://www.tiktok.com/@user/video/VIDEO_ID"),
    # ── Twitter / X ──────────────────────────────────────────────────────
    (("twitter", " x ", "x.com", "твиттер"),
     "twitter_profile",
     "https://x.com/your_profile  или  https://x.com/your_profile/status/ID"),
    # ── VK ───────────────────────────────────────────────────────────────
    (("вконтакте", "vkontakte", "vk ", " vk", "вк "),
     "vk_profile",
     "https://vk.com/your_profile  или  https://vk.com/wall-12345_67890"),
    # ── Discord ──────────────────────────────────────────────────────────
    (("discord", "дискорд"),
     "discord",
     "https://discord.gg/INVITE_CODE"),
    # ── Spotify ──────────────────────────────────────────────────────────
    (("spotify",),
     "spotify",
     "https://open.spotify.com/track/TRACK_ID"),
    # ── Twitch ───────────────────────────────────────────────────────────
    (("twitch", "твич"),
     "twitch_channel",
     "https://www.twitch.tv/your_channel"),
    # ── SoundCloud ───────────────────────────────────────────────────────
    (("soundcloud",),
     "soundcloud",
     "https://soundcloud.com/your_user/track-name"),
    # ── Facebook ─────────────────────────────────────────────────────────
    (("facebook", "фейсбук", "fb "),
     "facebook_profile",
     "https://www.facebook.com/your_page  "
     "или  https://www.facebook.com/your/posts/123"),
]
_LINK_TYPE_GENERIC_EXAMPLE = (
    "https://example.com/your_profile  или  ссылка на пост/видео"
)


def _detect_link_info(category: str, name: str) -> tuple[str, str]:
    """Определяет (link_type, link_example) по category+name услуги SMMPrime.

    Используется при сохранении/обновлении service_id в связке: один раз
    подтянули данные услуги из SMMPrime (`action=services`) — сразу
    предложили админу подходящий пример ссылки. Может быть переопределено
    вручную в карточке связки.
    """
    blob = (str(category or "") + " " + str(name or "")).lower()
    for kws, ltype, lex in _LINK_TYPE_RULES:
        for kw in kws:
            if kw in blob:
                return ltype, lex
    return "generic", _LINK_TYPE_GENERIC_EXAMPLE


# v3.12 — собственный regex для парсинга «10 шт.» / «10 pcs.» из текста
# заказа FunPay (полностью соответствует FunPayAPI.RegularExpressions
# .PRODUCTS_AMOUNT_ORDER). Используем его, чтобы отличить «100 шт. в
# описании» от «amount=1 потому что регулярка не сработала и FunPayAPI
# вернул дефолт 1».
_FP_PRODUCTS_AMOUNT_RE = re.compile(r"(\d{1,3}(?:\s?\d{3})*)\s(шт|pcs)\.")


def _resolve_quantity_from_order(order, binding: dict) -> int | None:
    """v3.14 — забираем quantity из заказа FunPay.

    Источники в порядке приоритета:
      1) `OrderShortcut.amount`, если > 1 (FunPayAPI считает 1 fallback'ом
         когда регулярка по описанию не сработала). Если amount > 1 —
         мы уверены, что это реально пришло из «N шт.».
      2) Сами парсим описание (`OrderShortcut.description`) по той же
         регулярке `(\\d+)\\s(шт|pcs)\\.` — на случай если amount=1 был
         из-за каких-то edge-кейсов.
      3) Если ничего не нашли, но `order.amount` равен 1 — считаем,
         что покупатель действительно купил 1 штуку. Раньше (v3.12)
         в этом случае плагин возвращал None и просил количество у
         покупателя; в v3.14 мы доверяем FunPay и используем 1.

    Возвращаем `None`, только если у объекта order вообще нет amount
    и нет описания — это аномалия.
    """
    if order is None:
        return None
    a = getattr(order, "amount", None)
    try:
        a_int = int(a) if a is not None else None
    except (TypeError, ValueError):
        a_int = None
    if a_int and a_int > 1:
        return a_int

    # 2) сами ищем «N шт.» в описании
    desc = (
        getattr(order, "description", None)
        or getattr(order, "title", None)
        or ""
    )
    if isinstance(desc, str):
        m = _FP_PRODUCTS_AMOUNT_RE.search(desc)
        if m:
            try:
                return int(m.group(1).replace(" ", "").replace("\xa0", ""))
            except ValueError:
                pass

    # 3) v3.14 — fallback: amount == 1 → доверяем FunPay (1 шт.).
    if a_int and a_int >= 1:
        return a_int
    return None


def _validate_quantity_range(qty: int, b: dict) -> tuple[bool, str]:
    """v3.11 — проверка quantity ∈ [min..max] из binding.

    Возвращает:
      (True, "") если ok;
      (False, "too_small") если qty < min;
      (False, "too_large") если qty > max;
      (False, "not_a_number" / "not_positive") при кривых входных данных.
    Если соответствующая граница 0 — она не проверяется.

    Старый текстовый reason ("меньше минимума (100)") заменён на тег,
    чтобы вызывающий код мог выбрать правильный шаблон сообщения
    покупателю (qty_too_small vs qty_too_large) и одновременно записать
    адекватный лог для админа.
    """
    try:
        q = int(qty)
    except (TypeError, ValueError):
        return False, "not_a_number"
    if q <= 0:
        return False, "not_positive"
    mn = int((b or {}).get("min_quantity", 0) or 0)
    mx = int((b or {}).get("max_quantity", 0) or 0)
    if mn > 0 and q < mn:
        return False, "too_small"
    if mx > 0 and q > mx:
        return False, "too_large"
    return True, ""


def _parse_quantity_from_text(text: str) -> int | None:
    """v3.10 — извлекает первое число (1..9 цифр) из произвольного текста.

    Используется на фазе waiting_for_quantity — покупатель может прислать
    «100», «100 шт», «количество: 350», и т.п.
    """
    if not text:
        return None
    m = _QUANTITY_RE.search(text)
    if not m:
        return None
    try:
        v = int(m.group(1))
        return v if v > 0 else None
    except ValueError:
        return None


# v3.13 — упрощённый парсер ответа покупателя на подтверждение заказа.
# Возвращает одно из: "yes", "cancel" или None (если текст не распознан
# как команда подтверждения). По требованиям v3.13 покупателю больше
# НЕ предлагается опция «изменить ссылку»/«изменить количество» —
# количество всегда берётся из заказа FunPay автоматически. Если
# ссылка/количество ему не подходят — он отвечает «отмена».
#
# Принимаем как полные слова, так и короткие команды/буквы — чтобы
# покупателю было удобно ответить с телефона.
_CONFIRM_YES = (
    "да", "+", "ок", "ok", "yes", "y", "оформить", "оформи",
    "/confirm", "/yes", "/ok", "подтверждаю", "верно", "всё верно",
    "все верно", "правильно",
)
_CONFIRM_CANCEL = (
    "отмена", "отменить", "нет", "no", "cancel", "stop",
    "/cancel", "/no", "не надо", "отказ", "не верно", "неверно",
)


def _parse_confirm_reply(text: str) -> str | None:
    """v3.13 — распознать ответ покупателя на подтверждение заказа.

    Возвращает 'yes' / 'cancel' / None.

    Изменено в v3.13: больше нет вариантов 'edit_link' / 'edit_qty'.
    Если покупатель пишет что-то про «изменить количество» / «другая
    ссылка», парсер возвращает None — обработчик пришлёт fallback
    «Не понял. Напишите Да или Отмена».
    """
    if not text:
        return None
    t = text.strip().lower()
    if not t:
        return None
    # Нормализуем — убираем знаки препинания в начале/конце, чтобы
    # «Да, оформить.», «Да!», «отмена.» и т.п. парсились как простое слово.
    # Разбиваем по любому небуквенному символу и берём первое слово —
    # этого достаточно для коротких команд типа «да», «отмена».
    head_word = re.split(r"[^\w/+]+", t, maxsplit=1)[0]

    # cancel / yes — обычно первое слово ответа.
    if head_word in _CONFIRM_CANCEL:
        return "cancel"
    if head_word in _CONFIRM_YES:
        return "yes"
    # Полный текст тоже проверим, на случай мульти-словных подтверждений
    # вроде «всё верно», «оформить пожалуйста».
    for pat in _CONFIRM_CANCEL:
        if t == pat or t.startswith(pat + " "):
            return "cancel"
    for pat in _CONFIRM_YES:
        if t == pat or t.startswith(pat + " "):
            return "yes"
    return None


def _strip_leading_emoji(text: str) -> str:
    """v3.10 — убирает ведущие emoji/мусорные символы перед текстом.

    Применяется к buyer-сообщениям в `_render_template` ПОСЛЕ подстановки
    переменных, чтобы любой кастомный шаблон с эмодзи в начале (например,
    «💃 Тестовый режим…») всё равно начинался с обычной буквы/цифры.
    """
    if not text:
        return text
    return _LEADING_GARBAGE_RE.sub("", text, count=1)


# ─────────────────────────────────────────────────────────────────────────────
#  КОНФИГ — лениво (НЕ на module level!)
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT_CFG: dict = {
    "enabled": True,
    "api_key": "",
    # v3.12 — 9 buyer-шаблонов (v3.11 их было 7), все редактируются в
    # Telegram-боте. Пустая строка = используем дефолтный шаблон
    # (см. _DEFAULT_*_TEMPLATE).
    "buyer_ask_link_text": "",        # 1) просьба ссылки
    "buyer_ask_quantity_text": "",    # 2) просьба количества
    "buyer_qty_too_small_text": "",   # 3) qty < min
    "buyer_qty_too_large_text": "",   # 4) qty > max
    "buyer_success_text": "",         # 5) финал: успех
    "buyer_error_text": "",           # 6) финал: ошибка
    "buyer_dry_run_text": "",         # 7) финал: dry-run
    "buyer_confirm_text": "",         # 8) v3.12 — сводка перед заказом
    "buyer_cancelled_text": "",       # 9) v3.12 — отмена заказа
    # v3.15 — приветствие до покупки. По умолчанию ВЫКЛЮЧЕНО, чтобы
    # не наступать на Cardinal-автоответчик. Включается одной кнопкой
    # в админ-меню Telegram.
    "pre_purchase_greeting_enabled": False,
    "pre_purchase_greeting_text": "",
    # v3.15 — список фраз, на которые плагин НЕ отвечает (передаёт
    # обработку Cardinal-автоответчику). Пустой = используется
    # хардкод-дефолт `_CARDINAL_PASSTHROUGH_DEFAULTS`. Каждая фраза
    # сравнивается case-insensitive со всем сообщением (после strip).
    "cardinal_passthrough": [],
    # v1.0.0 — SMMPrime API endpoint. Пустая строка = SMMPRIME_API_URL.
    # Меняется через UI бота («🌐 SMMPrime API URL»). Перезапуск не нужен.
    # По умолчанию для smmprime.com ничего менять не нужно — пустая
    # строка работает с дефолтным эндпоинтом.
    "smmprime_api_url": "",
    "bindings": [],
}

# v3.11 — quantity_mode полностью убран. Сценарий теперь единственный:
# спрашиваем СНАЧАЛА ссылку, потом количество. Старые константы оставлены
# как пустые tuple/строки только для обратной совместимости с миграциями.
_QTY_MODE_FROM_ORDER = ""
_QTY_MODE_ASK_BUYER = ""
_QTY_MODE_FIXED = ""
_QTY_MODES: tuple = ()

_DEFAULT_BINDING: dict = {
    "funpay_lot_id": "",
    "title": "",
    "description": "",
    "price": 0.0,
    "service": 0,
    # `quantity` оставлено только ради миграции старых v3.x конфигов.
    # В v3.11 покупатель ВСЕГДА сам присылает количество, и binding.quantity
    # больше не используется в боевом flow.
    "quantity": 0,
    # Поля link_mode/fixed_link — наследие v3.5; не используются.
    "link_mode": "ask_buyer",
    "fixed_link": "",
    # Per-binding override для всех 7 buyer-текстов. Пустое = глобальный → дефолт.
    "buyer_ask_link_text": "",
    "buyer_ask_quantity_text": "",
    "buyer_qty_too_small_text": "",
    "buyer_qty_too_large_text": "",
    "buyer_success_text": "",
    "buyer_error_text": "",
    "buyer_dry_run_text": "",
    "buyer_confirm_text": "",     # v3.12
    "buyer_cancelled_text": "",   # v3.12
    "dry_run": True,
    "enabled": True,
    # v3.11: метаданные услуги SMMPrime (кэш, обновляется только через
    # action=services). НЕ редактируются вручную в UI.
    "service_name": "",
    "service_category": "",
    "service_type": "",
    "service_rate": "",
    "min_quantity": 0,   # 0 = «не задано / SMMPrime не вернул»
    "max_quantity": 0,   # 0 = «не задано / SMMPrime не вернул»
    # link_example / link_type — авто-детект по category+name услуги
    # SMMPrime. Можно переопределить вручную через UI.
    "link_example": "",
    "link_type": "generic",
}

def _migrate_to_v36(d: dict) -> dict:
    """Автомиграция конфига:
       v3.3: items = dict[str, {...}]
       v3.4: items = list[{...}]
       v3.5: bindings = list[{..., link_mode}]
       v3.6: bindings = list[{...}], link_mode игнорируется.
    """
    if "bindings" not in d and "items" in d:
        items = d.pop("items")
        if isinstance(items, dict):
            converted = []
            for kw, v in items.items():
                if not isinstance(v, dict):
                    continue
                b = _DEFAULT_BINDING.copy()
                b["title"] = str(kw)
                b["service"] = v.get("service", 0)
                b["quantity"] = v.get("quantity", 0)
                b["dry_run"] = bool(d.get("dry_run", False))
                converted.append(b)
            d["bindings"] = converted
        elif isinstance(items, list):
            converted = []
            old_dry = bool(d.get("dry_run", False))
            for v in items:
                if not isinstance(v, dict):
                    continue
                b = _DEFAULT_BINDING.copy()
                for k in b:
                    if k in v:
                        b[k] = v[k]
                if "dry_run" not in v:
                    b["dry_run"] = old_dry
                converted.append(b)
            d["bindings"] = converted
    d.pop("dry_run", None)  # старое глобальное поле
    return d


def _normalize_binding(b) -> dict:
    if not isinstance(b, dict):
        return _DEFAULT_BINDING.copy()
    out = _DEFAULT_BINDING.copy()
    for k, v in b.items():
        if k in out:
            out[k] = v
    # link_mode в v3.6 всегда фактически ask_buyer; поле сохраняем,
    # но не показываем в UI.
    out["link_mode"] = "ask_buyer"
    out["fixed_link"] = ""
    # v3.11 — quantity_mode из старых конфигов выкидываем. Покупатель
    # теперь всегда сам присылает quantity → старые поля больше не нужны.
    # Поле просто игнорируется (если было в b — мы его не копировали в out,
    # т.к. в _DEFAULT_BINDING его уже нет).
    # v3.10 — приведём min/max к int на случай, если в конфиге строка.
    for k in ("min_quantity", "max_quantity"):
        try:
            out[k] = int(out.get(k) or 0)
        except (TypeError, ValueError):
            out[k] = 0
    # v3.10 — если link_example/link_type пустые, попытаемся вывести
    # из service_category/service_name (это быстрая локальная детекция).
    if not out.get("link_example") or not out.get("link_type") \
            or out.get("link_type") == "generic":
        ltype, lex = _detect_link_info(
            out.get("service_category", ""),
            out.get("service_name", "") or out.get("title", ""),
        )
        if not out.get("link_type") or out.get("link_type") == "generic":
            out["link_type"] = ltype
        if not out.get("link_example"):
            out["link_example"] = lex
    return out


def _load() -> dict:
    """Лениво грузит конфиг, никогда не падает."""
    try:
        os.makedirs("storage", exist_ok=True)
    except Exception as e:
        logger.warning(f"[SMMPrime] Не могу создать storage/: {e}.")
        return _DEFAULT_CFG.copy()

    try:
        if not os.path.exists(CONFIG_PATH):
            _save(_DEFAULT_CFG.copy())
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            d = {}
    except Exception as e:
        logger.warning(f"[SMMPrime] Не могу прочитать {CONFIG_PATH}: {e}.")
        return _DEFAULT_CFG.copy()

    d = _migrate_to_v36(d)
    for k, v in _DEFAULT_CFG.items():
        d.setdefault(k, v)
    d["bindings"] = [_normalize_binding(b) for b in d.get("bindings", [])]
    return d


def _save(cfg: dict) -> None:
    try:
        os.makedirs("storage", exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"[SMMPrime] Не могу сохранить {CONFIG_PATH}: {e}")


# v3.14 — миграция legacy-связок: однократно на старте после апгрейда.
_MIGRATION_FLAG_V314 = "_legacy_bindings_migrated_to_smmprime_v1"


def _migrate_legacy_bindings_v314(cardinal: "Cardinal") -> None:
    """v3.14 — однократная миграция связок из v3.x → v3.14.

    Что делает:
      • Загружает текущий конфиг.
      • Если флаг `_legacy_bindings_migrated_to_smmprime_v1` уже стоит — выходит.
      • Для каждой связки:
          – сохраняет service_id (как есть);
          – обнуляет старый `quantity` (поле остаётся в _DEFAULT_BINDING
            ради совместимости JSON, но больше не используется в flow);
          – пытается через SMMPrime services обновить
            min/max/service_name/category. Если API-ключ не задан или
            услугу не нашли — записываем причину и идём дальше.
      • Сохраняет конфиг с поднятым флагом миграции.
      • Шлёт админу одно итоговое сообщение со статистикой:
        сколько связок обновлено, у скольких не удалось подтянуть
        min/max, сколько legacy-quantity были обнулены.

    Запускается в отдельном потоке из `_pre_init_handler`, чтобы не
    тормозить старт Cardinal'а сетевыми запросами в SMMPrime.
    """
    try:
        cfg = _load()
    except Exception as e:  # noqa: BLE001
        logger.error(f"[SMMPrime.MIG] не могу загрузить конфиг: {e}")
        return

    if cfg.get(_MIGRATION_FLAG_V314):
        logger.info("[SMMPrime.MIG] миграция v3.14 уже выполнена, пропуск.")
        return

    bs = list(cfg.get("bindings", []))
    if not bs:
        # Нечего мигрировать — просто ставим флаг и выходим (тихо).
        cfg[_MIGRATION_FLAG_V314] = True
        _save(cfg)
        logger.info("[SMMPrime.MIG] связок нет, миграция v3.14 завершена "
                    "(только флаг).")
        return

    api_key = (cfg.get("api_key") or "").strip()
    total = len(bs)
    qty_zeroed = 0
    refreshed_ok = 0
    refresh_failed: list[tuple[int, str, str]] = []
    skipped_no_key = 0

    for i, b in enumerate(bs):
        # Сохраняем legacy-quantity для отчёта, но обнуляем поле:
        # по новому ТЗ quantity всегда из заказа FunPay.
        old_q = b.get("quantity")
        try:
            old_q_int = int(old_q or 0)
        except (TypeError, ValueError):
            old_q_int = 0
        if old_q_int > 0:
            b["quantity"] = 0
            qty_zeroed += 1

        # Обновляем min/max/service_name через SMMPrime (если можно).
        try:
            sid = int(b.get("service") or 0)
        except (TypeError, ValueError):
            sid = 0
        if sid <= 0:
            refresh_failed.append((i + 1, b.get("title") or "?",
                                   "service_id не задан"))
            continue
        if not api_key:
            skipped_no_key += 1
            continue
        try:
            ok, reason = _refresh_service_info(api_key, b)
        except Exception as e:  # noqa: BLE001
            ok, reason = False, f"исключение: {e}"
        if ok:
            refreshed_ok += 1
        else:
            refresh_failed.append((i + 1, b.get("title") or "?", reason))

    cfg["bindings"] = bs
    cfg[_MIGRATION_FLAG_V314] = True
    _save(cfg)

    # Отчёт админу.
    lines = [
        "🔄 <b>[SMMPrime] Миграция связок до v3.14 завершена</b>\n",
        f"Всего связок: <b>{total}</b>",
        f"• Обнулено legacy-<code>quantity</code>: <b>{qty_zeroed}</b> "
        f"(теперь quantity берётся из заказа FunPay)",
        f"• Обновлено <code>min/max</code> через SMMPrime: "
        f"<b>{refreshed_ok}</b>",
    ]
    if skipped_no_key:
        lines.append(
            f"• Не обновлено (API-ключ SMMPrime не задан): "
            f"<b>{skipped_no_key}</b>")
    if refresh_failed:
        lines.append(
            f"• Не удалось обновить: <b>{len(refresh_failed)}</b>")
        # До 5 примеров.
        for n, title, reason in refresh_failed[:5]:
            lines.append(
                f"   – #{n} «{_h(_truncate(title, 40))}»: "
                f"<code>{_h(_truncate(str(reason), 100))}</code>"
            )
        if len(refresh_failed) > 5:
            lines.append(
                f"   … и ещё <b>{len(refresh_failed) - 5}</b> связок.")

    if not api_key and (qty_zeroed or refresh_failed or skipped_no_key):
        lines.append("")
        lines.append(
            "⚠ Чтобы автоматически подтянуть актуальные min/max — "
            "введите API-ключ SMMPrime в настройках плагина и нажмите "
            "🔄 «Обновить инфо услуги» в карточке каждой связки."
        )

    try:
        _notify_admin(cardinal, "\n".join(lines))
    except Exception as e:  # noqa: BLE001
        logger.error(f"[SMMPrime.MIG] не удалось отправить отчёт админу: {e}")

    logger.info(
        f"[SMMPrime.MIG] миграция v3.14 завершена: total={total} "
        f"qty_zeroed={qty_zeroed} refreshed={refreshed_ok} "
        f"failed={len(refresh_failed)} skipped_no_key={skipped_no_key}"
    )


def _log_order(line: str) -> None:
    try:
        os.makedirs("storage", exist_ok=True)
        with open(ORDERS_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[SMMPrime] Не удалось записать в orders.log: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  PENDING ORDERS — персистентное состояние «ждём ссылку»
# ─────────────────────────────────────────────────────────────────────────────
#
#  Структура файла storage/smmprime_pending_orders.json:
#  {
#    "<funpay_order_id>": {
#       "funpay_order_id": "ABC123",
#       "buyer_username": "alice",
#       "buyer_id": 12345,
#       "chat_id": 678,
#       "lot_id": "66017420",          # из связки
#       "lot_title": "переходы telegram",
#       "binding_idx": 0,
#       "service_id": 5017,
#       "quantity": 350,
#       "dry_run": true,
#       "status": "waiting_for_link",
#       "smm_order_id": null,
#       "link": null,
#       "created_at": 1714560000,
#       "updated_at": 1714560000
#    },
#    ...
#  }
# ─────────────────────────────────────────────────────────────────────────────

_PENDING_STATUSES = (
    "waiting_for_quantity",  # v3.10 — ждём числовое количество от покупателя
    "waiting_for_link",      # ждём URL от покупателя
    "waiting_for_confirm",   # v3.12 — ждём подтверждения покупателя
    "link_received",         # legacy / временный статус пока идёт вызов SMMPrime
    "processing",            # v3.10 — явный статус обработки
    "smm_created",
    "dry_run_done",
    "failed",
    "cancelled",             # v3.12 — покупатель отменил заказ
)

# v3.12 — статусы АКТИВНОГО ожидания. Только эти статусы матчатся
# с новыми сообщениями покупателя. Остальные (smm_created, dry_run_done,
# failed, cancelled) — терминальные и НЕ должны цепляться к новым
# сообщениям, иначе старый завершённый заказ перетянет на себя
# сообщения по новой покупке (баг из логов: «match by buyer_username
# 'wefdggfhb6' → #HEA2KAPT status=dry_run_done» при активном #SME2XV1F).
_ACTIVE_PENDING_STATUSES = (
    "waiting_for_link",
    "waiting_for_quantity",
    "waiting_for_confirm",
)


def _load_pending() -> dict:
    """Грузит pending-orders из persistent JSON."""
    try:
        os.makedirs("storage", exist_ok=True)
    except Exception:
        pass
    if not os.path.exists(PENDING_PATH):
        return {}
    try:
        with open(PENDING_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[SMMPrime] Не могу прочитать {PENDING_PATH}: {e}")
        return {}


def _save_pending(pending: dict) -> None:
    try:
        os.makedirs("storage", exist_ok=True)
        with open(PENDING_PATH, "w", encoding="utf-8") as f:
            json.dump(pending, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        logger.error(f"[SMMPrime] Не могу сохранить {PENDING_PATH}: {e}")


def _pending_upsert(order: dict) -> None:
    """Создаёт/обновляет pending-запись (потокобезопасно)."""
    with _PENDING_LOCK:
        pending = _load_pending()
        order = dict(order)
        order["updated_at"] = int(time.time())
        order.setdefault("created_at", order["updated_at"])
        pending[str(order["funpay_order_id"])] = order
        _save_pending(pending)


def _pending_get(funpay_order_id) -> dict | None:
    with _PENDING_LOCK:
        pending = _load_pending()
        return pending.get(str(funpay_order_id))


def _pending_claim_processing(funpay_order_id) -> bool:
    """FIX v1.0.2 — атомарный compare-and-set для перехода в «processing».

    Выполняется внутри _PENDING_LOCK, поэтому два потока, одновременно
    получивших «Да» от покупателя (NEW_MESSAGE + LAST_CHAT_MESSAGE_CHANGED
    в old_mode, или двойной NEW_MESSAGE в new_mode), не смогут оба
    выполнить API-запрос в SMMPrime.

    Алгоритм:
      1) Под _PENDING_LOCK читаем текущий статус заказа из файла.
      2) Проверяем in-memory множество _ORDER_PROCESSING_SET.
      3) Если статус != waiting_for_confirm ИЛИ oid уже в множестве
         → возвращаем False (другой поток уже взял задание).
      4) Иначе: добавляем oid в _ORDER_PROCESSING_SET, записываем
         status=processing в файл → возвращаем True.

    Вызывающий код должен убрать oid из _ORDER_PROCESSING_SET после
    завершения (успех или ошибка) через _pending_release_processing().
    """
    key = str(funpay_order_id)
    with _PENDING_LOCK:
        # Проверяем in-memory флаг (защита от параллельных потоков в рамках
        # одного процесса; срабатывает быстрее чем чтение файла).
        if key in _ORDER_PROCESSING_SET:
            return False
        # Читаем актуальный статус из файла (защита после рестарта / другой
        # копии процесса, хотя FPC обычно один).
        pending_store = _load_pending()
        record = pending_store.get(key)
        if not record:
            return False
        if record.get("status") != "waiting_for_confirm":
            return False
        # Атомарно занимаем слот: in-memory + файл.
        _ORDER_PROCESSING_SET.add(key)
        record["status"] = "processing"
        record["updated_at"] = int(time.time())
        pending_store[key] = record
        _save_pending(pending_store)
        return True


def _pending_release_processing(funpay_order_id) -> None:
    """FIX v1.0.2 — убираем oid из in-memory множества после завершения
    _create_smm_from_pending (успех или ошибка). Вызывается в finally-блоке.
    """
    _ORDER_PROCESSING_SET.discard(str(funpay_order_id))


def _pending_find_by_buyer(buyer_id, chat_id=None,
                           statuses=("waiting_for_link",)) -> dict | None:
    """Ищет последний pending-заказ от данного покупателя.

    ⚠ Сопоставление идёт по `buyer_id`, а НЕ по `chat_id`. У FunPay
    `OrderShortcut.chat_id` = `"users-<id1>-<id2>"` (строка), а
    `Message.chat_id` — это integer chat-node ID. Они разные. Поэтому
    chat_id используется только как мягкая подсказка (если оба int —
    проверяем; иначе игнорируем).

    Если несколько pending — возвращает самый свежий по created_at.
    """
    if buyer_id is None:
        return None
    with _PENDING_LOCK:
        pending = _load_pending()
    cands = []
    for v in pending.values():
        if not isinstance(v, dict):
            continue
        if v.get("status") not in statuses:
            continue
        if str(v.get("buyer_id")) != str(buyer_id):
            continue
        cands.append(v)
    if not cands:
        return None
    cands.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return cands[0]


def _pending_find_active_for_buyer(buyer_id, chat_id=None) -> dict | None:
    """v3.12 — найти АКТИВНЫЙ pending покупателя
    (waiting_for_link / waiting_for_quantity / waiting_for_confirm).

    Этот матчер используется в обработке сообщений от покупателя
    в первую очередь. Терминальные статусы (smm_created, dry_run_done,
    failed, cancelled) сюда НЕ попадают — это исправляет баг, когда
    новое сообщение от покупателя цеплялось к старому завершённому
    заказу.
    """
    return _pending_find_by_buyer(
        buyer_id, chat_id, statuses=_ACTIVE_PENDING_STATUSES
    )


def _pending_find_any_for_buyer(buyer_id, chat_id=None) -> dict | None:
    """Любой pending-заказ от покупателя (включая терминальные).

    v3.12 — оставлено для идемпотентного ответа «заказ уже создан»,
    если активного pending нет, но в истории есть smm_created /
    dry_run_done. Используется как fallback после
    `_pending_find_active_for_buyer`.
    """
    return _pending_find_by_buyer(
        buyer_id, chat_id, statuses=_PENDING_STATUSES
    )


def _pending_find_by_username(buyer_username,
                              statuses=("waiting_for_link",)) -> dict | None:
    """Ищет pending по нику покупателя (для old_mode, где автор-id
    не приходит — только chat_name == buyer_username).
    """
    if not buyer_username:
        return None
    target = str(buyer_username).strip().lower()
    with _PENDING_LOCK:
        pending = _load_pending()
    cands = []
    for v in pending.values():
        if not isinstance(v, dict):
            continue
        if v.get("status") not in statuses:
            continue
        if str(v.get("buyer_username") or "").strip().lower() != target:
            continue
        cands.append(v)
    if not cands:
        return None
    cands.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return cands[0]


def _pending_find_active_for_username(buyer_username) -> dict | None:
    """v3.12 — old-mode матчинг по username, фильтрует по АКТИВНЫМ
    статусам (исправление бага из логов: «match by buyer_username
    'wefdggfhb6' → #HEA2KAPT status=dry_run_done»)."""
    return _pending_find_by_username(
        buyer_username, statuses=_ACTIVE_PENDING_STATUSES
    )


def _pending_find_any_for_username(buyer_username) -> dict | None:
    """Old-mode fallback: ищем любой pending по нику для
    идемпотентного ответа «уже создан»."""
    return _pending_find_by_username(
        buyer_username, statuses=_PENDING_STATUSES
    )


def _pending_list(statuses=None) -> list[dict]:
    """Возвращает все pending-заказы (отфильтрованные по статусу).
    Сортировано: новые сверху."""
    with _PENDING_LOCK:
        pending = _load_pending()
    items = [v for v in pending.values() if isinstance(v, dict)]
    if statuses is not None:
        items = [v for v in items if v.get("status") in statuses]
    items.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return items


# ─── v3.8: удаление pending-записей из Telegram-меню ──────────────────────

def _pending_delete(funpay_order_id) -> dict | None:
    """Удаляет pending-запись из storage. Возвращает удалённую запись
    (для лога/уведомления) или None, если не нашлась.

    ⚠ Удаляется ТОЛЬКО запись плагина. FunPay-заказ и SMM-заказ не
    трогаем. После удаления бот больше не ждёт ссылку по этому заказу.
    """
    key = str(funpay_order_id)
    with _PENDING_LOCK:
        pending = _load_pending()
        removed = pending.pop(key, None)
        if removed is not None:
            _save_pending(pending)
    if removed is not None:
        logger.info(
            f"[SMMPrime.PENDING] УДАЛЕНО #{removed.get('funpay_order_id')} "
            f"buyer={removed.get('buyer_username')!r} "
            f"status_before={removed.get('status')!r} "
            f"(только запись плагина; FunPay/SMMPrime не тронуты)"
        )
    return removed


# Группы статусов для массовой очистки.
_PURGE_GROUPS: dict[str, tuple[str, ...]] = {
    "wait": ("waiting_for_link",),
    "dry": ("dry_run_done",),
    "done": ("smm_created",),
}
_PURGE_LABELS: dict[str, str] = {
    "wait": "ожидающие",
    "dry": "dry-run",
    "done": "обработанные",
}


def _pending_purge(group: str) -> int:
    """Удаляет все pending-записи с указанным набором статусов.
    Возвращает количество удалённых. Группы: wait | dry | done.
    """
    statuses = _PURGE_GROUPS.get(group)
    if not statuses:
        return 0
    with _PENDING_LOCK:
        pending = _load_pending()
        to_delete = [
            k for k, v in pending.items()
            if isinstance(v, dict) and v.get("status") in statuses
        ]
        for k in to_delete:
            pending.pop(k, None)
        if to_delete:
            _save_pending(pending)
    if to_delete:
        logger.info(
            f"[SMMPrime.PENDING] PURGE group={group} "
            f"statuses={statuses} удалено={len(to_delete)}"
        )
    return len(to_delete)


# ─────────────────────────────────────────────────────────────────────────────
#  УТИЛИТЫ
# ─────────────────────────────────────────────────────────────────────────────

def _mask(key: str) -> str:
    if not key:
        return "не задан"
    if len(key) <= 4:
        return "*" * len(key)
    if len(key) <= 12:
        return key[:2] + "****" + key[-2:]
    return key[:4] + "****" + key[-4:]


def _truncate(text: str, limit: int = 80) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _h(text) -> str:
    return (str(text) if text is not None else "") \
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _bindings(cfg: dict) -> list[dict]:
    return cfg.get("bindings", [])


def _parse_lot_id(text: str) -> str:
    """Извлекает lot_id из URL вида https://funpay.com/lots/offer?id=12345
       или из URL/строки с числом >= 6 цифр. Если не найдено — возвращает
       исходный текст (вдруг это и есть голый ID)."""
    text = (text or "").strip()
    m = _FUNPAY_LOT_RE.search(text)
    if m:
        return m.group(1) or m.group(2) or text
    if text.isdigit():
        return text
    return text


def _find_binding(cfg: dict, lot_name: str) -> tuple[int, dict] | tuple[None, None]:
    low = (lot_name or "").lower()
    for idx, b in enumerate(_bindings(cfg)):
        title = (b.get("title") or "").lower().strip()
        if title and title in low:
            return idx, b
    return None, None


def _extract_link(text: str) -> str | None:
    m = _URL_RE.search(text or "")
    if not m:
        return None
    url = m.group(0).rstrip(".,;!?)")
    return url if url.startswith("http") else "https://" + url


class _SafeFormat(dict):
    """Заглушка для отсутствующих ключей в str.format_map — чтобы шаблон
       никогда не валился KeyError'ом."""
    def __missing__(self, key):
        return "?"


def _render_template(template: str, vars_: dict,
                     strip_leading: bool = True) -> str:
    """v3.10 — рендерит шаблон и strip-ает ведущие emoji в buyer-текстах.

    Параметр strip_leading=True (по умолчанию) убирает любые
    не-буквенные/не-цифровые символы в самом начале результата.
    Это защищает от «рябиновых птичек» (FunPay плохо рендерит
    эмодзи в начале сообщения), даже если админ задаёт свой
    кастомный текст с эмодзи.
    """
    if not template:
        return ""
    safe = _SafeFormat()
    for k, v in (vars_ or {}).items():
        safe[k] = "" if v is None else str(v)
    try:
        out = template.format_map(safe)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[SMMPrime] Ошибка рендеринга шаблона: {e}")
        out = template
    if strip_leading:
        out = _strip_leading_emoji(out)
    return out


def _build_template_vars(pending: dict, link: str = "",
                         smm_order_id=None) -> dict:
    """v3.10 — все доступные плейсхолдеры для buyer-шаблонов.

    Новые ключи:
      service_name — название услуги SMMPrime (из кэша binding'a)
      link_example — пример формата ссылки (по link_type)
      min, max     — min/max quantity услуги
    """
    return {
        "buyer_username": pending.get("buyer_username", "клиент"),
        "funpay_order_id": pending.get("funpay_order_id", "—"),
        "lot_id": pending.get("lot_id") or pending.get("lot_title", "—"),
        "service_id": pending.get("service_id", "—"),
        "service_name": pending.get("service_name") or "—",
        "quantity": pending.get("quantity") if pending.get("quantity") is not None else "—",
        "link": link or pending.get("link") or "—",
        "link_example": pending.get("link_example") or _LINK_TYPE_GENERIC_EXAMPLE,
        "smm_order_id": (smm_order_id
                         or pending.get("smm_order_id")
                         or "—"),
        "min": pending.get("min_quantity") or 0,
        "max": pending.get("max_quantity") or 0,
        "dry_run": "включен" if pending.get("dry_run") else "выключен",
    }


def _resolve_text(cfg: dict, b: dict, kind: str) -> str:
    """v3.13 — kind ∈ {ask_link, ask_quantity, qty_too_small, qty_too_large,
    success, error, dry_run, confirm, cancelled, not_link, not_number,
    not_confirm, already_done}.

    Приоритет: per-binding текст → глобальный текст → дефолтный шаблон.
    Пустая строка означает «использовать следующий уровень».

    v3.13 добавил «Не понял …» и «Этот заказ уже обработан» fallback'и:
      • not_link    — ждём ссылку, пришло не-ссылка;
      • not_number  — ждём число, пришло не-число;
      • not_confirm — ждём да/отмена, пришло что-то другое;
      • already_done — заказ уже обработан, повторно не создаём.
    """
    field_map = {
        "ask_link":      ("buyer_ask_link_text",      _DEFAULT_ASK_LINK_TEMPLATE),
        "ask_quantity":  ("buyer_ask_quantity_text",  _DEFAULT_ASK_QUANTITY_TEMPLATE),
        "qty_too_small": ("buyer_qty_too_small_text", _DEFAULT_QTY_TOO_SMALL_TEMPLATE),
        "qty_too_large": ("buyer_qty_too_large_text", _DEFAULT_QTY_TOO_LARGE_TEMPLATE),
        "success":       ("buyer_success_text",       _DEFAULT_SUCCESS_TEMPLATE),
        "error":         ("buyer_error_text",         _DEFAULT_ERROR_TEMPLATE),
        "dry_run":       ("buyer_dry_run_text",       _DEFAULT_DRY_RUN_TEMPLATE),
        "confirm":       ("buyer_confirm_text",       _DEFAULT_CONFIRM_TEMPLATE),
        "cancelled":     ("buyer_cancelled_text",     _DEFAULT_CANCELLED_TEMPLATE),
        # v3.13 — «Не понял» fallback'и (per-binding не настраиваются —
        # только глобально через cfg).
        "not_link":      ("buyer_not_link_text",      _DEFAULT_NOT_LINK_TEMPLATE),
        "not_number":    ("buyer_not_number_text",    _DEFAULT_NOT_NUMBER_TEMPLATE),
        "not_confirm":   ("buyer_not_confirm_text",   _DEFAULT_NOT_CONFIRM_TEMPLATE),
        "already_done":  ("buyer_already_done_text",  _DEFAULT_ALREADY_DONE_TEMPLATE),
    }
    field, default = field_map[kind]
    return ((b or {}).get(field) or "").strip() \
        or ((cfg or {}).get(field) or "").strip() \
        or default


# ─────────────────────────────────────────────────────────────────────────────
#  SMMPRIME API КЛИЕНТ
# ─────────────────────────────────────────────────────────────────────────────

class SMMPrimeError(Exception):
    """Базовая ошибка SMMPrime."""


class SMMPrimeAuthError(SMMPrimeError):
    """API ключ отсутствует или отвергнут (401)."""


class SMMPrimeClient:
    def __init__(self, api_key: str, base_url: str = SMMPRIME_API_URL,
                 timeout: int = REQUEST_TIMEOUT):
        self._api_key = (api_key or "").strip()
        self._base_url = base_url
        self._timeout = timeout

    def _request(self, payload: dict):
        if not self._api_key:
            raise SMMPrimeAuthError("API-ключ не задан")
        data = {"key": self._api_key, **payload}

        r = requests.post(self._base_url, data=data, timeout=self._timeout)

        if r.status_code == 401:
            try:
                rg = requests.get(self._base_url, params=data, timeout=self._timeout)
            except requests.RequestException:
                rg = None
            if rg is not None and rg.status_code != 401:
                r = rg
            else:
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
        """v3.10 — выбирает одну услугу из выдачи action=services.

        SMMPrime НЕ отдаёт одну услугу по ID отдельным эндпоинтом —
        тянем всё и фильтруем по service.
        """
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


def _api_error_text(e: Exception, api_key: str) -> str:
    if isinstance(e, SMMPrimeAuthError):
        return (
            "🔑 <b>SMMPrime отклонил API ключ.</b>\n"
            "Проверьте ключ в настройках (smmprime.com → Settings → API).\n\n"
            f"Текущий ключ: <code>{_mask(api_key)}</code>"
        )
    if isinstance(e, SMMPrimeError):
        return f"❌ <b>Ошибка SMMPrime:</b>\n<code>{_h(_truncate(str(e), 300))}</code>"
    if isinstance(e, requests.RequestException):
        return f"❌ <b>Сетевая ошибка:</b>\n<code>{_h(_truncate(str(e), 300))}</code>"
    return f"❌ <b>Неожиданная ошибка:</b> <code>{type(e).__name__}</code>"


def _service_name(svc: dict) -> str:
    name = svc.get("name")
    if isinstance(name, dict):
        return name.get("ru") or name.get("en") or str(name)
    return str(name) if name else "?"


def _apply_service_info(b: dict, svc: dict) -> dict:
    """v3.10 — записывает данные услуги SMMPrime (action=services) в binding.

    Заполняет: service_name, service_category, service_type, service_rate,
    min_quantity, max_quantity, link_type, link_example.

    link_example пишется только если поле было пустым — чтобы не затирать
    ручное переопределение админа.
    """
    if not isinstance(b, dict) or not isinstance(svc, dict):
        return b
    b["service_name"] = _service_name(svc)
    b["service_category"] = str(svc.get("category") or "")
    b["service_type"] = str(svc.get("type") or "")
    rate = svc.get("rate")
    b["service_rate"] = "" if rate is None else str(rate)
    try:
        b["min_quantity"] = int(svc.get("min") or 0)
    except (TypeError, ValueError):
        b["min_quantity"] = 0
    try:
        b["max_quantity"] = int(svc.get("max") or 0)
    except (TypeError, ValueError):
        b["max_quantity"] = 0
    ltype, lex = _detect_link_info(b["service_category"], b["service_name"])
    # link_type обновляем всегда (чтобы переключение услуги меняло
    # подсказку); link_example обновляем только если был дефолт/пусто.
    b["link_type"] = ltype
    if not (b.get("link_example") or "").strip() \
            or b.get("link_example") == _LINK_TYPE_GENERIC_EXAMPLE:
        b["link_example"] = lex
    return b



# v1.0.0 — Helpers для конфигурируемого URL SMMPrime API.
def _effective_api_url(cfg: dict | None = None) -> str:
    """Возвращает фактически используемый URL SMMPrime API.

    Если в cfg задан непустой smmprime_api_url — возвращаем его,
    иначе дефолтный SMMPRIME_API_URL. Удобно для логов и UI:
    показываем реальный endpoint, а не дефолт, если админ его
    переопределил.
    """
    if cfg is None:
        cfg = _load()
    return (cfg.get("smmprime_api_url") or "").strip() or SMMPRIME_API_URL


def _make_client(cfg: dict | None = None) -> "SMMPrimeClient":
    """v1.0.0 — единая фабрика SMMPrimeClient с учётом cfg-настройки URL.

    Раньше во многих местах было ``SMMPrimeClient(api_key)`` — теперь
    везде ``_make_client()``, чтобы кастомный URL подхватывался без
    правок в каждом колбэке.
    """
    if cfg is None:
        cfg = _load()
    api_key = (cfg.get("api_key") or "").strip()
    url = (cfg.get("smmprime_api_url") or "").strip() or SMMPRIME_API_URL
    return SMMPrimeClient(api_key, base_url=url)


def _legacy_other_panels_warning() -> str:
    """v1.0.0 — если рядом лежит конфиг от другой SMM-панели (SMMFast или
    TapLike), показываем плашку в главном экране настроек.

    НЕ читаем чужие файлы, НЕ копируем оттуда настройки. Просто говорим,
    что обнаружен legacy-файл и связки нужно создать заново под SMMPrime
    (потому что service_id у каждой панели свой).
    """
    legacy_paths = [
        ("storage/smmfast_config.json", "SMMFast"),
        ("storage/taplike_config.json", "TapLike"),
    ]
    found = [(p, b) for p, b in legacy_paths if os.path.exists(p)]
    if not found:
        return ""
    lines = ["\n⚠️ <b>Обнаружены legacy-конфиги от других панелей:</b>"]
    for p, b in found:
        lines.append(f"• <code>{p}</code> ({b})")
    lines.append(
        "ID услуг у разных SMM-панелей <b>не совместимы</b>. Связки нужно "
        "создать заново, указав корректные service_id из каталога SMMPrime."
    )
    lines.append("Старые файлы плагин <b>не читает</b> и не трогает.\n")
    return "\n".join(lines) + "\n"



def _refresh_service_info(api_key: str, b: dict) -> tuple[bool, str]:
    """v3.10 — тянет инфо услуги из SMMPrime и обновляет binding in-place.

    Возвращает (True, "") при успехе или (False, reason) при ошибке.
    """
    if not api_key:
        return False, "API-ключ не задан"
    try:
        sid = int(b.get("service") or 0)
    except (TypeError, ValueError):
        return False, "service_id некорректен"
    if sid <= 0:
        return False, "service_id не задан"
    try:
        svc = SMMPrimeClient(api_key).get_service(sid)
    except SMMPrimeError as e:
        return False, str(e)
    except requests.RequestException as e:
        return False, f"сеть: {e}"
    if not svc:
        return False, f"услуга #{sid} не найдена в SMMPrime"
    _apply_service_info(b, svc)
    return True, ""


def _qty_mode_label(mode: str) -> str:  # pragma: no cover — legacy stub
    """v3.11 — quantity_mode выпилен. Функция оставлена ради старых тестов."""
    return ""


def _qty_mode_next(mode: str) -> str:  # pragma: no cover — legacy stub
    """v3.11 — quantity_mode выпилен. Функция оставлена ради старых тестов."""
    return ""


# ─────────────────────────────────────────────────────────────────────────────
#  УВЕДОМЛЕНИЯ
# ─────────────────────────────────────────────────────────────────────────────

def _notify_admin(cardinal: "Cardinal", text: str) -> None:
    """Шлёт текст администратору через telegram-бота FPC.

    v3.12 — убрали `parse_mode="HTML"`: в твоей версии FunPayCardinal
    `TGBot.send_notification` его НЕ принимает (`unexpected keyword
    argument 'parse_mode'`). HTML-разметка всё равно отрендерится —
    у telebot-инстанса в FPC по умолчанию выставлен `parse_mode='HTML'`
    (см. tg_bot/bot.py:44).

    Также добавлена защита `try/except` на любую другую сигнатуру:
    если send_notification вообще не примет наш вызов, ошибка не
    свалит главный поток.
    """
    if cardinal.telegram is None:
        return
    try:
        cardinal.telegram.send_notification(text)
    except TypeError as e:
        # Обратная совместимость: вдруг сигнатура иная.
        logger.warning(f"[SMMPrime] notify_admin TypeError, retry: {e}")
        try:
            cardinal.telegram.send_notification(text=text)
        except Exception as e2:  # noqa: BLE001
            logger.error(f"[SMMPrime] notify_admin failed (retry): {e2}")
    except Exception as e:  # noqa: BLE001
        logger.error(f"[SMMPrime] notify_admin failed: {e}")


# v3.11.1 — guard-список технических/мусорных текстов, которые НИКОГДА
# не должны прилететь покупателю. Если по какой-то причине шаблон вернул
# одно слово вроде «menu», «/start», «debug», «error», «callback» — мы
# не отправляем его, а пишем громкий лог. Это защита от регрессий и от
# случайной отправки имени callback'а вместо ответа.
_TECH_BLOCKLIST = frozenset({
    "menu", "/menu", "/start", "start", "/help", "help",
    "debug", "callback", "cb", "test", "ping",
    "smmf", "smmprime", "smmf:main", "smmf:settings",
})


def _send_buyer(cardinal: "Cardinal", chat_id, chat_name, text: str) -> None:
    """Отправка сообщения покупателю в чат FunPay.

    v3.11.1 — добавлен guard:
      • убираем ведущие эмодзи/мусор ещё раз (на случай если шаблон
        обошёл `_render_template`);
      • не отправляем пустые / whitespace-only / односложные
        технические команды («menu», «/menu», «/start», «debug» и т.д.);
      • любой такой случай — громкий ERROR в лог Cardinal с трассировкой,
        чтобы легко отлаживать.
    """
    if not chat_id:
        logger.debug("[SMMPrime.SEND] guard: chat_id пустой — не отправляем.")
        return
    if not isinstance(text, str):
        logger.error(f"[SMMPrime.SEND] guard: text не str ({type(text)}), skip.")
        return
    # 1) ещё раз убираем ведущие эмодзи (двойная защита от «💃 …»).
    text = _strip_leading_emoji(text)
    # 2) пусто / только пробелы — никогда не отправляем.
    stripped = text.strip()
    if not stripped:
        logger.error("[SMMPrime.SEND] guard: пустой/whitespace-only текст — "
                     "блокируем отправку покупателю.", stack_info=False)
        return
    # 3) односложные технические команды/идентификаторы — блок.
    if stripped.lower() in _TECH_BLOCKLIST:
        logger.error(
            f"[SMMPrime.SEND] guard: попытка отправить техническую "
            f"команду {stripped!r} покупателю (chat_id={chat_id}). "
            f"БЛОКИРУЕМ. Если вы видите это в логе — где-то ошибка в "
            f"шаблоне или это отвечает FPC autoresponse "
            f"(configs/auto_response.cfg)."
        )
        return
    try:
        cardinal.send_message(chat_id, text, chat_name)
        logger.info(
            f"[SMMPrime.SEND] → buyer chat_id={chat_id} "
            f"({chat_name!r}) len={len(text)} preview={text[:80]!r}"
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"[SMMPrime.SEND] send_buyer failed: {e}", exc_info=True)


# ─────────────────────────────────────────────────────────────────────────────
#  ИНСТРУКЦИЯ
# ─────────────────────────────────────────────────────────────────────────────

_HELP_TEXT = (
    "📖 <b>Инструкция по плагину SMMPrime Auto-Order v3.8</b>\n\n"

    "<b>🆕 Новое в v3.8</b>\n"
    "• На экране <b>📋 Заказы</b> теперь можно <b>удалять</b> "
    "зависшие записи. Кнопки:\n"
    "   — 🗑 <b>Удалить заказ</b>: выбор одного + подтверждение;\n"
    "   — 🧹 <b>Очистить ожидающие</b>: удалить все <code>waiting_for_link</code>;\n"
    "   — 🧹 <b>Очистить dry-run</b>: удалить все <code>dry_run_done</code>;\n"
    "   — 🧹 <b>Очистить обработанные</b>: удалить все <code>smm_created</code>.\n"
    "⚠ Удаление чистит ТОЛЬКО запись плагина. Заказ FunPay НЕ отменяется. "
    "Заказ SMMPrime НЕ отменяется (если был создан — отмена решается "
    "на стороне SMMPrime).\n"
    "• Из дефолтных буфер-сообщений покупателю убраны ведущие emoji "
    "(«✅», «❌», «🧪») — в чате FunPay они иногда рендерятся как «птичка» "
    "или «💃». Сообщения теперь начинаются с имени покупателя. Если "
    "хотите свои emoji — добавьте их в кастомные шаблоны "
    "«💬 УСПЕХ/ОШИБКА/DRY-RUN» в главном меню.\n\n"
    "<b>Как теперь устроен flow (ВАЖНО)</b>\n"
    "1. Покупатель оплачивает лот на FunPay.\n"
    "2. Бот пишет ему: «пришлите ссылку на профиль/пост одной строкой».\n"
    "3. Покупатель отправляет ссылку в чат FunPay.\n"
    "4. Плагин ловит это сообщение и:\n"
    "   • если связка в <b>dry-run</b> 🟡 — показывает параметры будущего "
    "запроса, реальный заказ в SMMPrime НЕ создаёт, баланс не тратит;\n"
    "   • если связка в <b>боевом режиме</b> ⚪ — отправляет реальный POST "
    "в SMMPrime и присылает покупателю SMMPrime ID.\n"
    "5. Если покупатель отправит ссылку повторно — плагин ответит "
    "«заказ уже был создан, SMMPrime ID: …» и не создаст дубликат.\n\n"

    "<b>⚠ Что бот НЕ делает</b>\n"
    "Бот <b>не редактирует</b> ваши лоты на funpay.com (название, "
    "описание, цена, картинки — это всё вы меняете на FunPay вручную). "
    "Поля связки нужны только для:\n"
    "• матчинга оплаченного заказа FunPay со связкой (по названию);\n"
    "• генерации шаблона для ручной публикации;\n"
    "• передачи в SMMPrime service_id и quantity.\n\n"

    "<b>Как настроить с нуля</b>\n"
    "1. ⚙ Настройки → 🔑 API-ключ → введите ключ SMMPrime.\n"
    "2. 💰 Баланс — должна показаться сумма (если 401, см. внизу).\n"
    "3. 📃 Список услуг — выпишите ID нужной услуги (например 5017).\n"
    "4. <b>Сначала</b> опубликуйте лот на funpay.com вручную "
    "(можно с помощью «📋 Шаблон для ручной публикации» в карточке связки).\n"
    "5. Скопируйте URL созданного лота.\n"
    "6. ⚙ Настройки → 🛒 Связки FunPay→SMMPrime → ➕ Добавить связку.\n"
    "7. Wizard из 7 шагов: URL/ID лота → название → service_id → "
    "quantity → текст УСПЕХА → текст ОШИБКИ → dry-run.\n"
    "8. 🧪 Тест связки — проверить, что параметры верны.\n"
    "9. Если всё ОК — выключите dry-run в карточке связки. Готово.\n\n"

    "<b>Что такое dry-run и как им пользоваться</b>\n"
    "Dry-run — тестовый режим. В нём плагин <b>НЕ создаёт</b> реальный "
    "заказ в SMMPrime и <b>НЕ тратит баланс</b>. При срабатывании "
    "показывает все параметры запроса, чтобы можно было проверить "
    "настройки. Dry-run настраивается у каждой связки отдельно. "
    "По умолчанию ВКЛ.\n\n"
    "<b>Пошагово:</b>\n"
    "1. Создайте связку (dry-run по умолчанию ВКЛ — 🟡).\n"
    "2. На funpay.com сделайте тестовую покупку (или попросите друга).\n"
    "3. Покупатель получит «пришлите ссылку».\n"
    "4. Покупатель отправит ссылку в чат.\n"
    "5. В Telegram придёт уведомление DRY-RUN со всеми параметрами.\n"
    "6. Покупатель получит дефолтный (или ваш) текст dry-run.\n"
    "7. Если параметры верные — в карточке связки переключите "
    "«Dry-run: 🟡» → «Dry-run: ⚪». С этого момента реальные покупки "
    "будут идти в SMMPrime.\n\n"

    "<b>Шаблоны (переменные)</b>\n"
    "В текстах УСПЕХА / ОШИБКИ / DRY-RUN можно использовать:\n"
    "• <code>{buyer_username}</code> — ник покупателя\n"
    "• <code>{funpay_order_id}</code> — ID заказа FunPay\n"
    "• <code>{lot_id}</code> — ID лота FunPay\n"
    "• <code>{service_id}</code> — ID услуги SMMPrime\n"
    "• <code>{quantity}</code> — количество\n"
    "• <code>{link}</code> — ссылка покупателя\n"
    "• <code>{smm_order_id}</code> — ID заказа SMMPrime (после создания)\n"
    "• <code>{dry_run}</code> — «включен»/«выключен»\n"
    "Если оставить поле пустым — используется дефолтный шаблон.\n\n"

    "<b>Что делать при 401 Unauthorized</b>\n"
    "1. Проверьте, что ключ скопирован полностью (без пробелов).\n"
    "2. Сравните последние 4 символа ключа с тем, что показывает плагин "
    "(<code>abcd****wxyz</code>).\n"
    "3. На smmprime.com → Settings → API нажмите Reset/Generate, введите "
    "новый ключ.\n"
    "4. Если 401 остался — проверьте, что аккаунт SMMPrime не "
    "заблокирован.\n\n"

    "<b>Безопасность</b>\n"
    "• API-ключ всегда маскируется (<code>abcd****wxyz</code>).\n"
    "• Покупателю никогда не уходит API-ключ или техническая ошибка.\n"
    "• Только настроенные тексты (успех/ошибка/dry-run)."
)

_HELP_FLOW_TEXT = (
    "📖 <b>Поток обработки заказа в v3.8</b>\n\n"
    "<b>1. Покупка на FunPay</b>\n"
    "Срабатывает <code>BIND_TO_NEW_ORDER</code>. Плагин:\n"
    "  • матчит лот по названию со связкой;\n"
    "  • сохраняет pending-запись в "
    "<code>storage/smmprime_pending_orders.json</code>;\n"
    "  • отправляет покупателю «пришлите ссылку».\n\n"

    "<b>2. Сообщение покупателя</b>\n"
    "Срабатывает <code>BIND_TO_NEW_MESSAGE</code>. Плагин:\n"
    "  • проверяет, что author_id совпадает с buyer_id pending-заказа;\n"
    "  • ищет URL в тексте сообщения;\n"
    "  • если URL нет — игнорирует, ждёт следующего сообщения;\n"
    "  • если URL есть и статус = waiting_for_link — переходим к шагу 3;\n"
    "  • если статус smm_created — отвечает идемпотентно «уже создан».\n\n"

    "<b>3. Создание заказа в SMMPrime</b>\n"
    "Если связка в dry-run (🟡):\n"
    "  • реальный POST не идёт;\n"
    "  • статус → <code>dry_run_done</code>;\n"
    "  • админу — уведомление с параметрами;\n"
    "  • покупателю — текст dry-run.\n"
    "Если связка в боевом режиме (⚪):\n"
    "  • POST на SMMPrime c <code>action=add</code>;\n"
    "  • при успехе → статус <code>smm_created</code>, "
    "    SMMPrime ID сохраняется;\n"
    "  • при ошибке → статус <code>failed</code>, "
    "    покупателю текст ошибки.\n\n"

    "<b>Состояние</b>\n"
    "Pending-заказы переживают рестарт бота. Все статусы:\n"
    "<code>waiting_for_link</code>, <code>link_received</code>, "
    "<code>smm_created</code>, <code>dry_run_done</code>, "
    "<code>failed</code>.\n\n"

    "<b>Защита от дубликатов</b>\n"
    "Если покупатель отправит ссылку повторно после успешного создания "
    "заказа — плагин не сделает второй POST. Бот ответит:\n"
    "<code>«заказ уже был создан ранее. SMMPrime ID: N»</code>"
)

_HELP_FUNPAY_TEXT = (
    "📖 <b>Как опубликовать товар на FunPay</b>\n\n"
    "Бот <b>не создаёт</b> лоты на funpay.com автоматически. Это нужно "
    "сделать вручную в личном кабинете. Бот помогает: даёт готовый "
    "шаблон для копирования.\n\n"

    "<b>Шаги:</b>\n"
    "1. ⚙ Настройки → 🛒 Связки → откройте карточку связки → "
    "<b>📋 Шаблон для ручной публикации</b>.\n"
    "2. Бот пришлёт готовый блок (название, описание, цена). "
    "Скопируйте.\n"
    "3. На funpay.com → ваш профиль → ➕ Создать лот → выберите "
    "категорию (Telegram / Instagram / TikTok / VK …).\n"
    "4. Вставьте название и описание.\n"
    "5. Автоматическая выдача — НЕТ (плагин сам отвечает в чате).\n"
    "6. Опубликуйте лот.\n"
    "7. Скопируйте URL лота "
    "(<code>https://funpay.com/lots/offer?id=...</code>).\n"
    "8. Вернитесь в карточку связки → ✏ Lot ID → вставьте URL/ID.\n\n"

    "<b>Важно про матчинг:</b>\n"
    "Плагин сматчивает оплаченный заказ FunPay со связкой по <b>названию</b> "
    "(не по lot_id). Название связки в боте должно встречаться (как "
    "подстрока, без учёта регистра) в названии лота FunPay. Например "
    "связка <code>переходы telegram</code> сматчится с лотом "
    "<code>📲350 ПЕРЕХОДОВ ПО РЕФЕРАЛЬНЫМ ССЫЛКАМ TELEGRAM</code>."
)


# ─────────────────────────────────────────────────────────────────────────────
#  КЛАВИАТУРЫ
# ─────────────────────────────────────────────────────────────────────────────

def _kb_main() -> IKM:
    cfg = _load()
    api_icon = "✅" if cfg.get("api_key") else "❌"
    on_icon = "🟢 ВКЛ" if cfg.get("enabled") else "🔴 ВЫКЛ"
    cnt = len(_bindings(cfg))
    pending = _load_pending()
    waiting = sum(1 for v in pending.values()
                  if isinstance(v, dict) and v.get("status") == "waiting_for_link")
    total_pending = sum(1 for v in pending.values() if isinstance(v, dict))
    kb = IKM()
    kb.add(IKB(f"Статус плагина: {on_icon}", callback_data=_TOGGLE_ENABLED))
    kb.add(IKB(f"{api_icon} API-ключ SMMPrime", callback_data=_SET_API))
    kb.add(IKB(f"🛒 Связки FunPay→SMMPrime ({cnt})", callback_data=_LIST_BIND))
    kb.add(IKB(
        f"📋 Заказы (⏳ {waiting} / всего {total_pending})",
        callback_data=_PENDING_LIST))
    # v3.15 — оставлены только тексты, которые реально используются
    # в новом сценарии заказа. Убраны:
    #  • ASK QTY  — quantity всегда из заказа FunPay, бот не просит число;
    #  • ERR<min / ERR>max — для below_min/above_max используются хардкод-
    #    шаблоны `_DEFAULT_QTY_TOO_SMALL/LARGE_TEMPLATE` (см. v3.15 ТЗ
    #    п.1: «Это должны быть обычные автоматические сообщения»).
    # Поля в cfg остаются на случай отката, просто кнопок редактирования
    # для них больше нет.
    kb.row(
        IKB("💬 ASK LINK", callback_data=_SET_ASK_LINK_TEXT),
    )
    # v3.12 — два новых глобальных текста: CONFIRM (сводка перед заказом)
    # и CANCELLED (отмена покупателем).
    kb.row(
        IKB("💬 CONFIRM", callback_data=_SET_CONFIRM_TEXT),
        IKB("💬 ОТМЕНА", callback_data=_SET_CANCELLED_TEXT),
    )
    kb.row(
        IKB("💬 УСПЕХ", callback_data=_SET_OK_TEXT),
        IKB("💬 ОШИБКА", callback_data=_SET_ERR_TEXT),
        IKB("💬 DRY-RUN", callback_data=_SET_DRY_TEXT),
    )
    kb.row(
        IKB("💰 Баланс", callback_data=_CHECK_BAL),
        IKB("📃 Список услуг", callback_data=f"{_LIST_SERVICES}:0"),
    )
    # v3.15 — приветствие ДО покупки (pre-purchase greeting).
    pp_on = bool(cfg.get("pre_purchase_greeting_enabled", False))
    pp_label = "🟢 ВКЛ" if pp_on else "🔴 ВЫКЛ"
    kb.row(
        IKB(f"👋 До покупки: {pp_label}", callback_data=_TOGGLE_PREPUR),
        IKB("✏ Текст до покупки", callback_data=_SET_PREPUR_TEXT),
    )
    kb.row(
        IKB("📖 Инструкция", callback_data=_HELP),
        IKB("🔁 Как работает flow", callback_data=_HELP_FLOW),
    )
    kb.add(IKB("◀ Назад к плагину", callback_data=_back_to_plugin_cb(0)))
    return kb


def _short_label(b: dict, idx: int) -> str:
    """v3.9 — короткий лейбл связки для компактного списка-кнопки.

    Формат: «<status> <title> | #<n>», обрезается до _SHORT_LABEL_LIMIT
    символов. Длинные названия урезаются с многоточием. Используется
    в 2-колоночном списке связок.
    """
    st = "🟢" if b.get("enabled", True) else "🔴"
    title = (b.get("title") or "").strip() or f"связка #{idx + 1}"
    suffix = f" | #{idx + 1}"
    head = f"{st} "
    budget = _SHORT_LABEL_LIMIT - len(head) - len(suffix)
    if budget < 4:
        budget = 4
    return f"{head}{_truncate(title, budget)}{suffix}"


def _filter_bindings(bs: list[dict],
                     query: str) -> list[tuple[int, dict]]:
    """v3.9 — фильтр списка связок по подстроке `query` (case-insensitive).

    Сопоставляет с полями `title` и `funpay_lot_id`. Возвращает
    список пар `(оригинальный_индекс, binding)` — оригинальный индекс
    нужен, чтобы клик по кнопке открывал ту же карточку, что и раньше.
    """
    if not query:
        return list(enumerate(bs))
    q = query.lower().strip()
    if not q:
        return list(enumerate(bs))
    out: list[tuple[int, dict]] = []
    for i, b in enumerate(bs):
        title = (b.get("title") or "").lower()
        lot = str(b.get("funpay_lot_id") or "").lower()
        if q in title or q in lot:
            out.append((i, b))
    return out


def _get_sort_mode() -> str:
    """v3.16 — текущий режим сортировки списка связок (хранится в cfg)."""
    cfg = _load()
    mode = str(cfg.get("bindings_sort", "newest"))
    if mode not in _BIND_SORT_MODES:
        mode = "newest"
    return mode


def _set_sort_mode(mode: str) -> None:
    """v3.16 — записать режим сортировки в cfg."""
    if mode not in _BIND_SORT_MODES:
        mode = "newest"
    cfg = _load()
    cfg["bindings_sort"] = mode
    _save(cfg)


def _next_sort_mode(mode: str) -> str:
    """v3.16 — следующий режим в цикле для одной кнопки переключения."""
    try:
        i = _BIND_SORT_MODES.index(mode)
    except ValueError:
        i = -1
    return _BIND_SORT_MODES[(i + 1) % len(_BIND_SORT_MODES)]


def _sort_bindings(filtered: list[tuple[int, dict]],
                   mode: str) -> list[tuple[int, dict]]:
    """v3.16 — сортирует пары `(idx, binding)` по выбранному режиму.

    Используется поверх результата `_filter_bindings`. Оригинальный idx
    сохраняется (он нужен, чтобы клик по карточке открывал ту же связку),
    меняется только порядок отображения.

    Режимы:
      newest    — по индексу убывания (последняя добавленная — первая);
      oldest    — по индексу возрастания (порядок добавления);
      cheap     — по price ASC; ties → idx ASC (стабильно);
      expensive — по price DESC; ties → idx ASC;
      title     — case-insensitive алфавитная сортировка по `title`.

    Если режим неизвестен — отдаём filtered без изменений.
    """
    if not filtered:
        return filtered
    if mode == "newest":
        return sorted(filtered, key=lambda p: -p[0])
    if mode == "oldest":
        return sorted(filtered, key=lambda p: p[0])
    if mode == "cheap":
        def k_cheap(p: tuple[int, dict]) -> tuple[float, int]:
            try:
                pr = float(p[1].get("price") or 0)
            except (TypeError, ValueError):
                pr = 0.0
            return (pr, p[0])
        return sorted(filtered, key=k_cheap)
    if mode == "expensive":
        def k_exp(p: tuple[int, dict]) -> tuple[float, int]:
            try:
                pr = float(p[1].get("price") or 0)
            except (TypeError, ValueError):
                pr = 0.0
            return (-pr, p[0])
        return sorted(filtered, key=k_exp)
    if mode == "title":
        return sorted(
            filtered,
            key=lambda p: ((p[1].get("title") or "").lower(), p[0]),
        )
    return filtered


def _bindings_page_info(bs: list[dict],
                        query: str,
                        page: int,
                        sort_mode: str | None = None
                        ) -> tuple[list[tuple[int, dict]],
                                            int, int, int]:
    """v3.9 — служебка пагинации.

    Возвращает (срез текущей страницы, total_filtered, page_now,
    total_pages). page_now нормализуется в [0, total_pages-1].

    v3.16: добавлен параметр `sort_mode`. Если None — берём из cfg
    (см. `_get_sort_mode`). Сортировка применяется ПОСЛЕ фильтра.
    """
    filtered = _filter_bindings(bs, query)
    mode = sort_mode if sort_mode is not None else _get_sort_mode()
    filtered = _sort_bindings(filtered, mode)
    total = len(filtered)
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page_now = max(0, min(int(page or 0), total_pages - 1))
    start = page_now * _PAGE_SIZE
    chunk = filtered[start:start + _PAGE_SIZE]
    return chunk, total, page_now, total_pages


def _format_current_template_block(custom_text: str,
                                   default_text: str) -> str:
    """v3.16 — формирует блок «Сейчас используется...» для P3.

    Используется во всех `cb_set_*` (и в редакторе связки в `_BIND_EDIT`)
    для текстовых шаблонов, чтобы продавец, прежде чем менять текст,
    видел, что именно сейчас отправляется покупателю.

    Если у поля задан кастомный текст (custom != "" и custom != default),
    показываем его как «ваш текст» И отдельным блоком — дефолт, чтобы
    легко было сравнить или вернуть стандарт. Если кастомного нет —
    показываем только дефолт с пометкой «стандартный».
    """
    custom = (custom_text or "").strip()
    default = (default_text or "").strip()
    if custom and custom != default:
        return (
            "📋 <b>Сейчас используется ВАШ текст:</b>\n"
            f"<code>{_h(custom)}</code>\n\n"
            "📚 <b>Стандартный (на случай отката):</b>\n"
            f"<code>{_h(default)}</code>"
        )
    return (
        "📋 <b>Сейчас используется стандартный текст:</b>\n"
        f"<code>{_h(default)}</code>"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  v3.16 — РАБОТА С ЦЕНОЙ ЛОТА FUNPAY ЧЕРЕЗ FunPayAPI (Variant B)
# ─────────────────────────────────────────────────────────────────────────────
# Архитектура (см. v3.16 ТЗ п.1):
#   1. Админ → TG-кнопка «💵 Цена FunPay» в карточке связки.
#   2. Плагин дёргает `cardinal.account.get_lot_fields(lot_id)` —
#      фактическая цена приходит ИЗ FunPay (single source of truth).
#   3. Админ вводит новую цену → плагин показывает превью «было X → станет Y».
#   4. Админ подтверждает → плагин ещё раз дёргает get_lot_fields (свежий
#      CSRF), пишет новую цену в `lot_fields.fields["price"]`, шлёт
#      `cardinal.account.save_lot(lot_fields)`.
#   5. На успехе обновляем `binding["price"]` (для шаблона) и пишем в чат
#      админу. На ошибке — детально объясняем что пошло не так.
#
# ВСЕ вызовы FunPayAPI обёрнуты в `getattr` + try/except, чтобы плагин не
# падал на старых версиях FunPayCardinal без save_lot. Если методов нет —
# админу выводится понятное сообщение «обновите FPC до актуальной версии».
#
# Cooldown: одну и ту же цену лота нельзя править чаще 1 раза в 30 сек,
# чтобы FunPay не банил аккаунт за частые правки.

def _funpay_price_get_cfg_bounds() -> tuple[float, float]:
    """v3.16 — нижняя/верхняя граница допустимой цены (берём из cfg, иначе
    дефолты 1₽ … 999999₽). Используется для валидации ввода админа."""
    cfg = _load()
    try:
        lo = float(cfg.get("funpay_price_min") or _FUNPAY_PRICE_MIN_DEFAULT)
    except (TypeError, ValueError):
        lo = _FUNPAY_PRICE_MIN_DEFAULT
    try:
        hi = float(cfg.get("funpay_price_max") or _FUNPAY_PRICE_MAX_DEFAULT)
    except (TypeError, ValueError):
        hi = _FUNPAY_PRICE_MAX_DEFAULT
    if lo <= 0:
        lo = _FUNPAY_PRICE_MIN_DEFAULT
    if hi <= lo:
        hi = _FUNPAY_PRICE_MAX_DEFAULT
    return lo, hi


def _funpay_price_parse(raw: str) -> tuple[float | None, str | None]:
    """v3.16 — парсит ввод админа. Возвращает (цена, ошибка).

    Принимает форматы: «150», «150.00», «150,00», «150 руб», «150₽»,
    «150 р.», «1 500». Отвергает: пустоту, отрицательные, текст,
    превышение пределов из cfg.
    """
    if raw is None:
        return None, "пустой ввод"
    s = str(raw).strip().lower()
    if not s:
        return None, "пустой ввод"
    # Удаляем всё кроме цифр, точки, запятой, минуса.
    s = re.sub(r"[^\d,.\-]", "", s)
    # Запятая → точка.
    s = s.replace(",", ".")
    # «1.500» (тысячи через точку) — оставим как 1.500 (1.5₽), это редкий
    # случай; админ должен писать через пробел или без разделителя.
    if not s:
        return None, "не удалось распознать число"
    try:
        val = float(s)
    except ValueError:
        return None, f"не удалось распознать число: {raw!r}"
    if val < 0:
        return None, "цена должна быть положительной"
    lo, hi = _funpay_price_get_cfg_bounds()
    if val < lo:
        return None, (
            f"цена слишком мала — минимум {lo:g} ₽ (для защиты от опечаток). "
            f"Если вам реально нужна цена меньше, напишите продавцу."
        )
    if val > hi:
        return None, (
            f"цена слишком велика — максимум {hi:g} ₽ (для защиты от опечаток). "
            f"Проверьте, не лишний ли ноль."
        )
    # Округление до 2 знаков (FunPay принимает копейки).
    return round(val, 2), None


def _funpay_price_cooldown_remaining(lot_id: str) -> int:
    """v3.16 — сколько секунд осталось ждать до следующей правки этого лота.

    Возвращает 0, если можно править прямо сейчас.
    """
    if not lot_id:
        return 0
    last = _FUNPAY_PRICE_LAST_EDIT.get(str(lot_id), 0.0)
    elapsed = time.time() - last
    if elapsed >= _FUNPAY_PRICE_COOLDOWN_SEC:
        return 0
    return int(_FUNPAY_PRICE_COOLDOWN_SEC - elapsed) + 1


def _funpay_price_mark_edited(lot_id: str) -> None:
    """v3.16 — отметить факт правки лота (запускает cooldown)."""
    if lot_id:
        _FUNPAY_PRICE_LAST_EDIT[str(lot_id)] = time.time()


def _funpay_get_lot_fields(cardinal, lot_id: str
                           ) -> tuple[object | None, str | None]:
    """v3.16 — безопасно тянет lot_fields с FunPay через FunPayAPI.

    Возвращает (lot_fields, error). lot_fields — объект FunPayAPI.LotFields
    либо аналог; точный тип зависит от версии FPC. Все None-проверки и
    отсутствие методов трактуем как мягкую ошибку с понятным текстом
    в `error`, без exception в коллера.
    """
    if not str(lot_id).strip().isdigit():
        return None, ("ID лота должен быть числовым (например, 12345678). "
                      "Сейчас в связке: " + repr(lot_id))
    acc = getattr(cardinal, "account", None)
    if acc is None:
        return None, ("Объект cardinal.account недоступен — плагин не "
                      "может обратиться к FunPay. Перезапустите FPC.")
    get_fn = getattr(acc, "get_lot_fields", None)
    if not callable(get_fn):
        return None, (
            "Версия FunPayCardinal не поддерживает get_lot_fields. "
            "Обновите FPC до актуальной (метод появился в FunPayAPI давно, "
            "но в очень старых сборках может отсутствовать)."
        )
    try:
        # Современная сигнатура FunPayAPI: get_lot_fields(lot_id) —
        # subcategory подсасывает сам по странице лота. Если у вас другая
        # сигнатура — будет TypeError, ловим ниже и сообщаем админу.
        fields = get_fn(int(str(lot_id).strip()))
    except TypeError as e:
        return None, (
            "FunPayAPI требует subcategory_id, но плагин его не хранит. "
            f"Деталь: {e}. Сообщите автору плагина версию FunPayCardinal."
        )
    except Exception as e:  # noqa: BLE001
        return None, (
            f"FunPay вернул ошибку при чтении лота: "
            f"{type(e).__name__}: {e}. Возможные причины: лот удалён, "
            "сессия FunPay протухла (требуется перелогин в FPC), "
            "временная недоступность FunPay."
        )
    if fields is None:
        return None, ("FunPay вернул пустой ответ — возможно лот удалён "
                      "или скрыт.")
    return fields, None


def _funpay_lot_fields_get_price(lot_fields) -> float | None:
    """v3.16 — достаёт текущую цену из объекта lot_fields.

    FunPayAPI.LotFields обычно даёт `.fields["price"]` (строка с точкой)
    и/или прямой `.price`. Пробуем оба варианта.
    """
    if lot_fields is None:
        return None
    f = getattr(lot_fields, "fields", None)
    if isinstance(f, dict):
        for key in ("price", "fields[price]"):
            if key in f and f[key] not in (None, ""):
                try:
                    return float(str(f[key]).replace(",", "."))
                except (TypeError, ValueError):
                    pass
    p = getattr(lot_fields, "price", None)
    if p not in (None, ""):
        try:
            return float(str(p).replace(",", "."))
        except (TypeError, ValueError):
            pass
    return None


def _funpay_lot_fields_set_price(lot_fields, new_price: float) -> bool:
    """v3.16 — записывает новую цену в lot_fields. True — записали.

    Изменяем ТОЛЬКО ключ `price`, остальные поля (название, описание,
    server, side и т.п.) сохраняем нетронутыми, иначе save_lot затрёт их.
    """
    if lot_fields is None:
        return False
    written = False
    f = getattr(lot_fields, "fields", None)
    if isinstance(f, dict):
        # FunPay принимает строку с точкой, 2 знака после запятой.
        f["price"] = f"{float(new_price):.2f}"
        written = True
    if hasattr(lot_fields, "price"):
        try:
            lot_fields.price = float(new_price)
            written = True
        except Exception:  # noqa: BLE001
            pass
    return written


def _funpay_save_lot(cardinal, lot_fields) -> tuple[bool, str | None]:
    """v3.16 — безопасно вызывает FunPayAPI.Account.save_lot.

    Возвращает (success, error). На успехе error=None.
    """
    acc = getattr(cardinal, "account", None)
    if acc is None:
        return False, "cardinal.account недоступен (FPC не запущен?)"
    save_fn = getattr(acc, "save_lot", None)
    if not callable(save_fn):
        return False, (
            "Версия FunPayCardinal не поддерживает save_lot. "
            "Обновите FPC до актуальной."
        )
    try:
        save_fn(lot_fields)
    except Exception as e:  # noqa: BLE001
        return False, (
            f"FunPay отклонил сохранение: {type(e).__name__}: {e}. "
            "Частые причины: на лоте есть активный заказ (FunPay блокирует "
            "редактирование), сессия FunPay протухла, лот удалён, "
            "временный rate-limit."
        )
    return True, None


def _kb_bindings(page: int = 0, query: str = "") -> IKM:
    """v3.9 — компактная клавиатура списка связок.

    Параметры:
      page  — номер страницы (0-based; нормализуется автоматически).
      query — фильтр-подстрока. Пусто → весь список.

    Структура клавиатуры:
      1) ➕ Добавить связку | 🔍 Поиск (или 🔍 Сброс, если фильтр активен)
      2) 2-колоночный список связок (до _PAGE_SIZE на странице)
      3) ⬅️ | <page+1>/<total> | ➡️           (только если страниц > 1)
      4) ◀ Назад
    """
    cfg = _load()
    bs = _bindings(cfg)
    chunk, total, page_now, total_pages = _bindings_page_info(
        bs, query, page)
    kb = IKM()

    # 1) контролы списка
    if query:
        kb.row(
            IKB("➕ Добавить связку", callback_data=_ADD_BIND),
            IKB(f"🔍 Сброс ({_truncate(query, 16)})",
                callback_data=_BIND_SEARCH_RESET),
        )
    else:
        kb.row(
            IKB("➕ Добавить связку", callback_data=_ADD_BIND),
            IKB("🔍 Поиск", callback_data=_BIND_SEARCH),
        )
    # v3.16 — циклическая кнопка сортировки. Один клик = следующий режим.
    sort_mode = _get_sort_mode()
    sort_label = _BIND_SORT_LABELS.get(sort_mode, sort_mode)
    kb.add(IKB(f"Сортировка: {sort_label}", callback_data=_BIND_SORT_CYCLE))

    # 2) сама сетка 2 × N
    cb_page = (
        (lambda p: f"{_LIST_BIND_PAGE}:{p}")
    )
    for i in range(0, len(chunk), 2):
        pair = chunk[i:i + 2]
        row_buttons = [
            IKB(_short_label(b, idx),
                callback_data=f"{_BIND_DETAIL}:{idx}")
            for idx, b in pair
        ]
        kb.row(*row_buttons)

    # 3) пагинация — только если страниц больше одной
    if total_pages > 1:
        prev_p = max(0, page_now - 1)
        next_p = min(total_pages - 1, page_now + 1)
        kb.row(
            IKB("⬅️", callback_data=cb_page(prev_p)),
            IKB(f"{page_now + 1}/{total_pages}", callback_data=_NOOP),
            IKB("➡️", callback_data=cb_page(next_p)),
        )

    # 4) дополнительная навигация
    if total == 0 and bs:
        # ничего не нашлось по запросу
        kb.add(IKB("🔍 Сброс фильтра",
                   callback_data=_BIND_SEARCH_RESET))
    kb.add(IKB("📖 Как опубликовать на FunPay",
               callback_data=_HELP_FUNPAY))
    kb.add(IKB("◀ Назад", callback_data=_MAIN))
    return kb


def _kb_binding(idx: int, b: dict) -> IKM:
    """v3.11 — карточка связки.

    Изменения по сравнению с v3.10:
      - Убраны кнопки `✏ min`, `✏ max` и `🔁 Режим quantity` —
        min/max теперь подтягиваются ТОЛЬКО автоматически из SMMPrime
        через action=services (см. кнопку «🔄 Обновить инфо услуги»).
      - Убрана кнопка `✏ quantity (фикс)` — количество всегда
        присылает покупатель.
      - Добавлены 2 новые кнопки текстов: «✏ ERR<min» и «✏ ERR>max».
    """
    kb = IKM()
    enabled = "🟢 ВКЛ" if b.get("enabled", True) else "🔴 ВЫКЛ"
    dry = "🟡 ВКЛ" if b.get("dry_run", True) else "⚪ ВЫКЛ"
    kb.row(
        IKB(f"Статус: {enabled}", callback_data=f"{_BIND_TOGGLE_ON}:{idx}"),
        IKB(f"Dry-run: {dry}", callback_data=f"{_BIND_TOGGLE_DRY}:{idx}"),
    )
    kb.add(IKB("🧪 Тест связки", callback_data=f"{_BIND_TEST}:{idx}"))
    kb.row(
        IKB("✏ Название", callback_data=f"{_BIND_EDIT}:{idx}:title"),
        IKB("✏ Lot ID/URL", callback_data=f"{_BIND_EDIT}:{idx}:funpay_lot_id"),
    )
    kb.add(IKB("✏ service_id", callback_data=f"{_BIND_EDIT}:{idx}:service"))
    # v3.11 — пример ссылки + обновление инфо услуги (источник min/max).
    kb.add(IKB("✏ Пример ссылки",
               callback_data=f"{_BIND_EDIT}:{idx}:link_example"))
    kb.add(IKB("🔄 Обновить инфо услуги (min/max из SMMPrime)",
               callback_data=f"{_BIND_REFRESH_SVC}:{idx}"))
    kb.add(IKB("✏ Описание (для шаблона)",
               callback_data=f"{_BIND_EDIT}:{idx}:description"))
    # v3.16 — отдельная кнопка для управления реальной ценой лота на FunPay.
    # Старая «✏ Цена (для шаблона)» меняла только локальное поле в cfg —
    # её оставили на случай если шаблон используется отдельно от FunPay-цены.
    kb.add(IKB("💵 Цена FunPay (показать / изменить)",
               callback_data=f"{_BIND_PRICE_VIEW}:{idx}"))
    kb.add(IKB("✏ Цена (только для шаблона)",
               callback_data=f"{_BIND_EDIT}:{idx}:price"))
    # v3.15 — per-binding overrides для ask_quantity / qty_too_small /
    # qty_too_large убраны (см. правки в `_kb_main`). Остались тексты,
    # которые реально используются в новом сценарии. Поля в bindings.json
    # остаются на случай отката.
    kb.row(
        IKB("✏ ASK LINK",
            callback_data=f"{_BIND_EDIT}:{idx}:buyer_ask_link_text"),
    )
    # v3.12 — две новые кнопки текста: CONFIRM (сводка перед заказом)
    # и CANCELLED (сообщение об отмене покупателем).
    kb.row(
        IKB("✏ CONFIRM",
            callback_data=f"{_BIND_EDIT}:{idx}:buyer_confirm_text"),
        IKB("✏ ОТМЕНА",
            callback_data=f"{_BIND_EDIT}:{idx}:buyer_cancelled_text"),
    )
    kb.row(
        IKB("✏ УСПЕХ", callback_data=f"{_BIND_EDIT}:{idx}:buyer_success_text"),
        IKB("✏ ОШИБКА", callback_data=f"{_BIND_EDIT}:{idx}:buyer_error_text"),
        IKB("✏ DRY-RUN", callback_data=f"{_BIND_EDIT}:{idx}:buyer_dry_run_text"),
    )
    kb.add(IKB("📋 Шаблон для ручной публикации",
               callback_data=f"{_BIND_TEMPLATE}:{idx}"))
    kb.add(IKB("🗑 Удалить связку", callback_data=f"{_DEL_BIND}:{idx}"))
    kb.add(IKB("◀ К списку связок", callback_data=_LIST_BIND))
    return kb


# ─────────────────────────────────────────────────────────────────────────────
#  ЭКРАНЫ
# ─────────────────────────────────────────────────────────────────────────────

def _safe_edit(bot, chat_id: int, message_id: int, text: str, kb: IKM | None) -> None:
    try:
        bot.edit_message_text(
            text, chat_id, message_id,
            parse_mode="HTML", reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning(f"[SMMPrime] edit_message_text упал: {e}")


def _edit(bot, call: CallbackQuery, text: str, kb: IKM | None) -> None:
    _safe_edit(bot, call.message.chat.id, call.message.id, text, kb)


def _main_text() -> str:
    cfg = _load()
    pending = _load_pending()
    waiting = sum(1 for v in pending.values()
                  if isinstance(v, dict) and v.get("status") == "waiting_for_link")
    return (
        f"⚙️ <b>SMMPrime Auto-Order v{VERSION} — Настройки</b>\n\n"
        f"Плагин: <b>{'включён' if cfg.get('enabled') else 'выключен'}</b>\n"
        f"🔑 API-ключ: <code>{_mask(cfg.get('api_key', ''))}</code>\n"
        f"🛒 Связок: <b>{len(_bindings(cfg))}</b>\n"
        f"⏳ Ожидающих ссылку: <b>{waiting}</b>\n\n"
        f"<i>📋 Flow: покупка → бот просит ссылку → покупатель отправляет → "
        f"плагин делает заказ в SMMPrime (или dry-run).</i>\n\n"
        f"<i>ℹ Бот не редактирует уже опубликованные лоты FunPay. "
        f"Меняйте лот на funpay.com вручную, в боте — только привязка к "
        f"услуге SMMPrime.</i>"
    )


def _screen_main(bot, call: CallbackQuery) -> None:
    _edit(bot, call, _main_text(), _kb_main())


def _screen_bindings(bot,
                     call: CallbackQuery,
                     page: int = 0) -> None:
    """v3.9 — компактный список связок с пагинацией и поиском.

    В списке только статус + короткое название + #N (см. _short_label).
    Полная информация по связке открывается по тапу на её кнопку
    через _BIND_DETAIL → _screen_binding.
    """
    cid = call.message.chat.id
    query = _BIND_SEARCH_STATE.get(cid, "")
    cfg = _load()
    bs = _bindings(cfg)
    _, total, page_now, total_pages = _bindings_page_info(bs, query, page)

    if not bs:
        text = (
            "🛒 <b>Связки FunPay → SMMPrime</b>\n\n"
            "Связок пока нет. Сначала опубликуйте лот на funpay.com, "
            "потом нажмите <b>«➕ Добавить связку»</b> и привяжите его к "
            "услуге SMMPrime.\n\n"
            "Если нужен шаблон для публикации — заведите связку, и в её "
            "карточке появится кнопка <b>📋 Шаблон для ручной публикации</b>."
        )
    elif query and total == 0:
        text = (
            "🛒 <b>Связки FunPay → SMMPrime</b>\n\n"
            f"По запросу <code>{_h(_truncate(query, 60))}</code> "
            "ничего не найдено.\n\n"
            "<i>Поиск идёт по названию связки и по lot_id. Сбросьте "
            "фильтр или попробуйте другую подстроку.</i>"
        )
    else:
        sort_mode = _get_sort_mode()
        sort_label = _BIND_SORT_LABELS.get(sort_mode, sort_mode)
        head = (
            "🛒 <b>Связки FunPay → SMMPrime</b>\n\n"
            f"Всего связок: <b>{len(bs)}</b>"
        )
        if query:
            head += (f" · фильтр: <code>{_h(_truncate(query, 40))}</code>"
                     f" → <b>{total}</b>")
        head += (
            f"\nСортировка: <b>{sort_label}</b>"
            f"\nСтраница: <b>{page_now + 1} / {total_pages}</b> "
            f"(по {_PAGE_SIZE} на странице)\n\n"
            "<i>🟢 — связка включена, 🔴 — выключена. "
            "Тапните на связку, чтобы открыть её карточку с полной "
            "информацией (lot_id / service_id / цена / dry-run / "
            "шаблоны).</i>"
        )
        text = head
    _edit(bot, call, text, _kb_bindings(page=page_now, query=query))


def _screen_binding(bot, call: CallbackQuery, idx: int) -> None:
    cfg = _load()
    bs = _bindings(cfg)
    if not (0 <= idx < len(bs)):
        bot.answer_callback_query(call.id, "Связка не найдена", show_alert=True)
        _screen_bindings(bot, call)
        return
    b = bs[idx]
    lot = b.get("funpay_lot_id") or "—"
    lot_url = (f"https://funpay.com/lots/offer?id={lot}"
               if lot.isdigit() else lot)
    if b.get("dry_run", True):
        mode_text = ("🟡 <b>Тестовый режим:</b> реальные заказы в SMMPrime "
                     "не создаются, баланс не тратится.")
    else:
        mode_text = ("⚪ <b>Боевой режим:</b> после получения ссылки и "
                     "количества будет создан реальный заказ в SMMPrime.")
    svc_name = b.get("service_name") or "—"
    svc_cat = b.get("service_category") or "—"
    svc_rate = b.get("service_rate") or "—"
    mn = int(b.get("min_quantity") or 0)
    mx = int(b.get("max_quantity") or 0)
    if mn or mx:
        minmax_str = f"{mn} — {mx}"
    else:
        minmax_str = ("<i>не подтянуто из SMMPrime — нажмите «🔄 Обновить "
                      "инфо услуги»</i>")
    link_example = b.get("link_example") or "<i>не задан</i>"
    link_type = b.get("link_type") or "generic"
    text = (
        f"🔹 <b>Связка #{idx+1}</b>\n\n"
        f"<b>FunPay лот:</b> <code>{_h(lot)}</code>\n"
        f"  └ <a href=\"{_h(lot_url)}\">открыть на FunPay</a>\n"
        f"<b>Название (ключ матча):</b> "
        f"<code>{_h(b.get('title') or '—')}</code>\n\n"
        f"<b>SMMPrime услуга:</b> <code>{b.get('service')}</code>"
        f" — {_h(_truncate(svc_name, 60))}\n"
        f"  └ category: <code>{_h(svc_cat)}</code> | "
        f"rate: <code>{_h(svc_rate)}</code>/1000\n"
        f"  └ <b>min/max</b> <i>(read-only из SMMPrime)</i>: "
        f"<b>{minmax_str}</b>\n\n"
        f"<b>link_type:</b> <code>{_h(link_type)}</code>\n"
        f"<b>Пример ссылки:</b> "
        f"<code>{_h(_truncate(link_example, 120))}</code>\n\n"
        f"<b>Цена FunPay:</b> {b.get('price') or 0}₽ "
        f"<i>(только для шаблона)</i>\n"
        f"<b>Описание:</b> "
        f"<code>{_h(_truncate(b.get('description') or '—', 100))}</code>\n\n"
        f"<b>Статус связки:</b> "
        f"{'🟢 ВКЛ' if b.get('enabled', True) else '🔴 ВЫКЛ'}\n"
        f"{mode_text}\n\n"
        f"<i>📋 Flow v3.11 (14-step):</i>\n"
        f"<i>покупка → бот <b>1)</b> просит ссылку → покупатель "
        f"присылает ссылку → бот <b>2)</b> просит количество → "
        f"покупатель присылает число → бот проверяет min/max → "
        f"создаёт заказ в SMMPrime (или dry-run) → <b>3)</b> отправляет "
        f"финальное сообщение успеха или ошибки.</i>\n\n"
        f"<i>ℹ min/max нельзя править вручную — это значения из SMMPrime. "
        f"Цену на FunPay можно изменить через кнопку «💵 Цена FunPay».</i>"
    )
    _edit(bot, call, text, _kb_binding(idx, b))


# ─────────────────────────────────────────────────────────────────────────────
#  v3.16 — ЭКРАНЫ ЦЕНЫ FUNPAY
# ─────────────────────────────────────────────────────────────────────────────

def _kb_price_view(idx: int, has_lot_id: bool, can_edit: bool) -> IKM:
    """v3.16 — клавиатура экрана «текущая цена FunPay»."""
    kb = IKM()
    if has_lot_id and can_edit:
        kb.add(IKB("💵 Изменить цену на FunPay",
                   callback_data=f"{_BIND_PRICE_EDIT}:{idx}"))
    kb.add(IKB("🔄 Обновить (повторно прочитать с FunPay)",
               callback_data=f"{_BIND_PRICE_VIEW}:{idx}"))
    kb.add(IKB("◀ К связке", callback_data=f"{_BIND_DETAIL}:{idx}"))
    return kb


def _screen_price_view(bot, call: CallbackQuery, cardinal,
                       idx: int) -> None:
    """v3.16 — показывает текущую цену лота FunPay (читает её прямо
    с FunPay через FunPayAPI.get_lot_fields).

    Если лот не привязан / не числовой / FunPay не отвечает —
    объясняем причину и предлагаем кнопки коррекции.
    """
    cfg = _load()
    bs = _bindings(cfg)
    if not (0 <= idx < len(bs)):
        bot.answer_callback_query(call.id, "Связка не найдена",
                                  show_alert=True)
        _screen_bindings(bot, call)
        return
    b = bs[idx]
    lot_id_raw = str(b.get("funpay_lot_id") or "").strip()
    title = b.get("title") or f"связка #{idx+1}"

    # 1) валидация lot_id перед HTTP-запросом
    if not lot_id_raw:
        text = (
            f"💵 <b>Цена FunPay — {_h(title)}</b>\n\n"
            "❌ В этой связке не указан <b>Lot ID/URL</b> на FunPay.\n\n"
            "Нажмите «◀ К связке» и заполните поле «✏ Lot ID/URL»."
        )
        _edit(bot, call, text, _kb_price_view(idx, False, False))
        return
    if not lot_id_raw.isdigit():
        text = (
            f"💵 <b>Цена FunPay — {_h(title)}</b>\n\n"
            f"❌ Lot ID должен быть числом, сейчас: <code>{_h(lot_id_raw)}</code>.\n\n"
            "Нажмите «◀ К связке» → «✏ Lot ID/URL» и поправьте."
        )
        _edit(bot, call, text, _kb_price_view(idx, False, False))
        return

    # 2) тянем lot_fields с FunPay
    lot_fields, err = _funpay_get_lot_fields(cardinal, lot_id_raw)
    if err is not None:
        # Кэшируем последнюю известную цену из cfg (для шаблона) — её
        # как fallback покажем; реальную FunPay-цену сейчас не получили.
        cached = b.get("price") or 0
        text = (
            f"💵 <b>Цена FunPay — {_h(title)}</b>\n\n"
            f"<b>Лот:</b> <code>{_h(lot_id_raw)}</code>\n"
            f"<b>Локально сохранённая цена:</b> {cached} ₽ "
            f"<i>(только для шаблона)</i>\n\n"
            f"⚠ <b>Не удалось прочитать цену с FunPay:</b>\n"
            f"<code>{_h(_truncate(err, 300))}</code>\n\n"
            "<i>Можно попробовать кнопку «🔄 Обновить» (повторный запрос). "
            "Если ошибка стабильна — изменить цену через бота сейчас "
            "не получится, отредактируйте на FunPay напрямую.</i>"
        )
        _edit(bot, call, text, _kb_price_view(idx, True, False))
        return

    current_price = _funpay_lot_fields_get_price(lot_fields)
    if current_price is None:
        text = (
            f"💵 <b>Цена FunPay — {_h(title)}</b>\n\n"
            f"<b>Лот:</b> <code>{_h(lot_id_raw)}</code>\n\n"
            "⚠ FunPay вернул лот, но цена не найдена в его полях. "
            "Это может означать, что FunPay изменил формат API. "
            "Изменить цену через бота сейчас не получится — отредактируйте "
            "на FunPay напрямую.\n\n"
            f"<a href=\"https://funpay.com/lots/offerEdit?offer={_h(lot_id_raw)}\">"
            "Открыть редактирование на FunPay</a>"
        )
        _edit(bot, call, text, _kb_price_view(idx, True, False))
        return

    # 3) синхронизируем cached price (для шаблона) — раз уж тянули.
    try:
        b["price"] = float(current_price)
        bs[idx] = b
        cfg["bindings"] = bs
        _save(cfg)
    except Exception:  # noqa: BLE001
        pass

    cooldown = _funpay_price_cooldown_remaining(lot_id_raw)
    cd_line = ""
    if cooldown > 0:
        cd_line = (f"\n\n⏱ <i>Cooldown: следующая правка этого лота "
                   f"через {cooldown} сек (защита от FunPay rate-limit).</i>")

    lo, hi = _funpay_price_get_cfg_bounds()
    text = (
        f"💵 <b>Цена FunPay — {_h(title)}</b>\n\n"
        f"<b>Лот:</b> <code>{_h(lot_id_raw)}</code>\n"
        f"<b>Текущая цена на FunPay:</b> <b>{current_price:g} ₽</b>\n\n"
        f"<i>Цена прочитана прямо с FunPay (через FunPayAPI). "
        f"Это и есть та цена, которую видят покупатели.</i>\n\n"
        f"Допустимый диапазон новой цены: <b>{lo:g} … {hi:g} ₽</b>"
        f"{cd_line}"
    )
    _edit(bot, call, text, _kb_price_view(idx, True, cooldown == 0))


def _screen_price_edit(bot, call: CallbackQuery, idx: int) -> None:
    """v3.16 — экран ввода новой цены. Сохраняет state для msg_step."""
    cfg = _load()
    bs = _bindings(cfg)
    if not (0 <= idx < len(bs)):
        bot.answer_callback_query(call.id, "Связка не найдена",
                                  show_alert=True)
        _screen_bindings(bot, call)
        return
    b = bs[idx]
    lot_id_raw = str(b.get("funpay_lot_id") or "").strip()
    if not lot_id_raw.isdigit():
        bot.answer_callback_query(call.id,
                                  "У связки нет числового Lot ID",
                                  show_alert=True)
        return

    cooldown = _funpay_price_cooldown_remaining(lot_id_raw)
    if cooldown > 0:
        bot.answer_callback_query(
            call.id,
            f"⏱ Cooldown: подождите ещё {cooldown} сек",
            show_alert=True,
        )
        return

    # сохраняем state — msg_step примет следующее сообщение как новую цену
    _DIALOG[call.message.chat.id] = {
        "step": "await_funpay_price",
        "msg_id": call.message.id,
        "bind_idx": idx,
        "lot_id": lot_id_raw,
    }
    lo, hi = _funpay_price_get_cfg_bounds()
    cur = b.get("price") or 0
    title = b.get("title") or f"связка #{idx+1}"
    text = (
        f"💵 <b>Изменение цены — {_h(title)}</b>\n\n"
        f"<b>Лот:</b> <code>{_h(lot_id_raw)}</code>\n"
        f"<b>Известная цена:</b> {cur} ₽ "
        f"<i>(локально сохранённая, может отличаться от FunPay)</i>\n\n"
        f"Введите <b>новую цену в рублях</b> одним сообщением.\n\n"
        f"Допустимый диапазон: <b>{lo:g} … {hi:g} ₽</b>\n"
        f"Принимаются форматы: <code>150</code>, <code>150.50</code>, "
        f"<code>150,50</code>, <code>150 руб</code>.\n\n"
        f"<i>После ввода я покажу превью «было → станет» и попрошу "
        f"подтверждение. Только после «Да» цена обновится на FunPay.</i>\n\n"
        f"<i>/cancel — отмена.</i>"
    )
    _edit(bot, call, text,
          IKM().add(IKB("❌ Отмена", callback_data=f"{_BIND_PRICE_VIEW}:{idx}")))


def _screen_price_confirm(bot, cid: int, msg_id: int, idx: int,
                          new_price: float, lot_id: str,
                          old_price_known: float | None) -> None:
    """v3.16 — экран подтверждения «было X → станет Y» перед save_lot."""
    cfg = _load()
    bs = _bindings(cfg)
    if not (0 <= idx < len(bs)):
        return
    b = bs[idx]
    title = b.get("title") or f"связка #{idx+1}"
    old_str = (f"{old_price_known:g} ₽"
               if old_price_known is not None else "—")
    text = (
        f"💵 <b>Подтверждение изменения цены</b>\n\n"
        f"<b>Связка:</b> {_h(title)}\n"
        f"<b>Лот FunPay:</b> <code>{_h(lot_id)}</code>\n\n"
        f"<b>Было:</b> {old_str}\n"
        f"<b>Станет:</b> <b>{new_price:g} ₽</b>\n\n"
        "<i>Я пере-прочитаю поля лота с FunPay (для свежего CSRF-токена), "
        "обновлю в них только цену и сохраню обратно. Все остальные поля "
        "(название, описание, server, side и т.п.) останутся "
        "нетронутыми.</i>\n\n"
        "Сохранить?"
    )
    kb = IKM()
    kb.row(
        IKB("✅ Да, сохранить",
            callback_data=f"{_BIND_PRICE_CONFIRM}:{idx}"),
        IKB("❌ Отмена",
            callback_data=f"{_BIND_PRICE_VIEW}:{idx}"),
    )
    _safe_edit(bot, cid, msg_id, text, kb)


def _do_save_funpay_price(cardinal, cid: int, msg_id: int, bot,
                          idx: int) -> None:
    """v3.16 — реально сохраняет новую цену на FunPay.

    Берёт `pending_price` из _DIALOG, делает свежий get_lot_fields,
    подменяет поле price, save_lot, обновляет binding["price"].
    """
    state = _DIALOG.get(cid, {})
    new_price = state.get("pending_price")
    expected_idx = state.get("bind_idx")
    expected_lot = str(state.get("lot_id") or "")
    if new_price is None or expected_idx != idx:
        _safe_edit(bot, cid, msg_id,
                   "❌ Сессия изменения цены потеряна. Попробуйте заново.",
                   IKM().add(IKB("◀ К связке",
                                 callback_data=f"{_BIND_DETAIL}:{idx}")))
        _DIALOG.pop(cid, None)
        return

    cfg = _load()
    bs = _bindings(cfg)
    if not (0 <= idx < len(bs)):
        _DIALOG.pop(cid, None)
        return
    b = bs[idx]
    lot_id_raw = str(b.get("funpay_lot_id") or "").strip()
    if lot_id_raw != expected_lot:
        _safe_edit(bot, cid, msg_id,
                   "❌ Lot ID связки изменился, отмена. Откройте экран "
                   "цены заново.",
                   IKM().add(IKB("◀ К связке",
                                 callback_data=f"{_BIND_DETAIL}:{idx}")))
        _DIALOG.pop(cid, None)
        return

    cooldown = _funpay_price_cooldown_remaining(lot_id_raw)
    if cooldown > 0:
        _safe_edit(
            bot, cid, msg_id,
            f"⏱ Cooldown: подождите ещё {cooldown} сек перед "
            f"следующей правкой этого лота.",
            IKM().add(IKB("◀ К связке",
                          callback_data=f"{_BIND_DETAIL}:{idx}")),
        )
        return

    # 1) свежий get_lot_fields
    lot_fields, err = _funpay_get_lot_fields(cardinal, lot_id_raw)
    if err is not None or lot_fields is None:
        _safe_edit(
            bot, cid, msg_id,
            f"❌ <b>Не удалось обновить цену на FunPay.</b>\n\n"
            f"FunPay вернул ошибку при чтении лота:\n"
            f"<code>{_h(_truncate(err or 'unknown', 300))}</code>\n\n"
            "Локальная цена связки <b>не изменена</b>.",
            IKM().add(IKB("◀ К связке",
                          callback_data=f"{_BIND_DETAIL}:{idx}")),
        )
        _DIALOG.pop(cid, None)
        return

    # 2) подмена поля price
    if not _funpay_lot_fields_set_price(lot_fields, float(new_price)):
        _safe_edit(
            bot, cid, msg_id,
            "❌ <b>Не удалось записать цену в lot_fields.</b>\n\n"
            "Возможно, FunPayAPI вашей версии не позволяет это сделать. "
            "Локальная цена связки <b>не изменена</b>.",
            IKM().add(IKB("◀ К связке",
                          callback_data=f"{_BIND_DETAIL}:{idx}")),
        )
        _DIALOG.pop(cid, None)
        return

    # 3) save_lot
    ok, err2 = _funpay_save_lot(cardinal, lot_fields)
    if not ok:
        _safe_edit(
            bot, cid, msg_id,
            f"❌ <b>FunPay не сохранил цену.</b>\n\n"
            f"<code>{_h(_truncate(err2 or 'unknown', 300))}</code>\n\n"
            "Локальная цена связки <b>не изменена</b>.",
            IKM().add(IKB("◀ К связке",
                          callback_data=f"{_BIND_DETAIL}:{idx}")),
        )
        _DIALOG.pop(cid, None)
        return

    # 4) success — отмечаем cooldown, синкаем cached price
    _funpay_price_mark_edited(lot_id_raw)
    try:
        b["price"] = float(new_price)
        bs[idx] = b
        cfg["bindings"] = bs
        _save(cfg)
    except Exception:  # noqa: BLE001
        pass

    title = b.get("title") or f"связка #{idx+1}"
    _safe_edit(
        bot, cid, msg_id,
        f"✅ <b>Цена обновлена на FunPay</b>\n\n"
        f"Связка: {_h(title)}\n"
        f"Лот: <code>{_h(lot_id_raw)}</code>\n"
        f"Новая цена: <b>{float(new_price):g} ₽</b>\n\n"
        "<i>FunPay подтвердил сохранение. Cooldown 30 сек до следующей "
        "правки этого лота.</i>",
        IKM().add(IKB("◀ К связке",
                      callback_data=f"{_BIND_DETAIL}:{idx}")),
    )
    _DIALOG.pop(cid, None)


def _screen_template(bot, call: CallbackQuery, idx: int) -> None:
    cfg = _load()
    bs = _bindings(cfg)
    if not (0 <= idx < len(bs)):
        bot.answer_callback_query(call.id, "Связка не найдена", show_alert=True)
        return
    b = bs[idx]
    title = b.get("title") or "Товар"
    desc = b.get("description") or "—"
    price = b.get("price") or 0
    qty = b.get("quantity") or "?"
    template = (
        f"📋 <b>Шаблон для ручной публикации на FunPay</b>\n"
        f"<i>Скопируйте блоки в карточку лота на funpay.com.</i>\n\n"
        f"<b>Название лота:</b>\n<code>{_h(title)}</code>\n\n"
        f"<b>Описание:</b>\n<code>{_h(desc)}</code>\n\n"
        f"<b>Цена (₽):</b> <code>{price}</code>\n"
        f"<b>Количество в одном выкупе:</b> <code>{qty}</code>\n\n"
        f"<b>Что делать:</b>\n"
        f"1. Откройте funpay.com → ваш профиль → ➕ Создать лот\n"
        f"2. Выберите подходящую категорию.\n"
        f"3. Вставьте название и описание.\n"
        f"4. Автовыдачу выключите (плагин отвечает сам).\n"
        f"5. Опубликуйте лот.\n"
        f"6. Скопируйте URL лота и вернитесь в этот бот → "
        f"<b>✏ Lot ID/URL</b> в карточке связки."
    )
    kb = IKM()
    kb.add(IKB("✏ Lot ID/URL (вставить ID опубликованного лота)",
               callback_data=f"{_BIND_EDIT}:{idx}:funpay_lot_id"))
    kb.add(IKB("◀ К связке", callback_data=f"{_BIND_DETAIL}:{idx}"))
    _edit(bot, call, template, kb)


def _screen_help(bot, call: CallbackQuery) -> None:
    _edit(bot, call, _HELP_TEXT,
          IKM().add(IKB("◀ Назад", callback_data=_MAIN)))


def _screen_help_flow(bot, call: CallbackQuery) -> None:
    _edit(bot, call, _HELP_FLOW_TEXT,
          IKM().add(IKB("◀ Назад", callback_data=_MAIN)))


def _screen_help_funpay(bot, call: CallbackQuery) -> None:
    _edit(bot, call, _HELP_FUNPAY_TEXT,
          IKM().add(IKB("◀ К связкам", callback_data=_LIST_BIND)))


def _screen_bind_search_prompt(bot, call: CallbackQuery) -> None:
    """v3.9 — экран запроса подстроки для поиска по связкам.

    Открывает диалог: следующее текстовое сообщение от админа в этом
    чате будет принято за поисковый запрос (см. msg_step ветка
    `await_bind_search`). `/cancel` — выход без изменений.
    """
    cid = call.message.chat.id
    _DIALOG[cid] = {
        "step": "await_bind_search",
        "msg_id": call.message.id,
    }
    text = (
        "🔍 <b>Поиск по связкам</b>\n\n"
        "Введите подстроку. Поиск идёт без учёта регистра по полям:\n"
        "• <b>название связки</b> (то, что показывается в кнопках);\n"
        "• <b>FunPay lot_id</b> (число или часть URL).\n\n"
        "Примеры:\n"
        "• <code>переходы</code> — найдёт все связки про переходы;\n"
        "• <code>123456</code> — найдёт связку по lot_id;\n"
        "• <code>tg</code> — найдёт всё, где это есть в названии.\n\n"
        "<i>/cancel — отмена. Чтобы сбросить уже стоящий фильтр, "
        "нажмите кнопку «🔍 Сброс» в списке.</i>"
    )
    kb = IKM()
    kb.add(IKB("❌ Отмена", callback_data=_LIST_BIND))
    _edit(bot, call, text, kb)


_STATUS_ICONS = {
    "waiting_for_link": "⏳",
    "link_received": "🔄",
    "smm_created": "✅",
    "dry_run_done": "🟡",
    "failed": "❌",
}


def _pending_row(v: dict) -> str:
    status = v.get("status", "?")
    icon = _STATUS_ICONS.get(status, "•")
    smm = v.get("smm_order_id") or "—"
    extra = f" | SMM: <code>{smm}</code>" if status == "smm_created" else ""
    return (
        f"{icon} <code>#{v.get('funpay_order_id')}</code> "
        f"<b>{_h(v.get('buyer_username') or '?')}</b> | "
        f"{_h(_truncate(v.get('lot_title') or '?', 30))} | "
        f"<i>{status}</i>{extra}"
    )


def _screen_pending(bot, call: CallbackQuery) -> None:
    pending = _load_pending()
    counts = {st: 0 for st in _STATUS_ICONS}
    if pending:
        for v in pending.values():
            if isinstance(v, dict):
                st = v.get("status")
                if st in counts:
                    counts[st] += 1

    if not pending:
        text = (
            "📋 <b>Ожидающие/обработанные заказы</b>\n\n"
            "Список пуст. Когда покупатель оплатит лот — здесь появится "
            "запись «ждём ссылку»."
        )
    else:
        rows = [
            _pending_row(v)
            for v in sorted(pending.values(),
                            key=lambda x: x.get("created_at", 0),
                            reverse=True)[:25]
            if isinstance(v, dict)
        ]
        text = (
            "📋 <b>Ожидающие/обработанные заказы</b>\n\n"
            + "\n".join(rows)
            + "\n\n<i>⏳ ждём ссылку | 🔄 обрабатывается | ✅ создан | "
              "🟡 dry-run | ❌ ошибка</i>"
        )

    kb = IKM()
    if pending:
        kb.add(IKB("🗑 Удалить заказ", callback_data=_PENDING_DEL_LIST))
    if counts.get("waiting_for_link"):
        kb.add(IKB(
            f"🧹 Очистить ожидающие ({counts['waiting_for_link']})",
            callback_data=f"{_PENDING_PURGE_ASK}:wait"))
    if counts.get("dry_run_done"):
        kb.add(IKB(
            f"🧹 Очистить dry-run ({counts['dry_run_done']})",
            callback_data=f"{_PENDING_PURGE_ASK}:dry"))
    if counts.get("smm_created"):
        kb.add(IKB(
            f"🧹 Очистить обработанные ({counts['smm_created']})",
            callback_data=f"{_PENDING_PURGE_ASK}:done"))
    kb.add(IKB("◀ Назад", callback_data=_MAIN))
    _edit(bot, call, text, kb)


def _screen_pending_del_list(bot, call: CallbackQuery) -> None:
    """v3.8 — список pending-заказов с кнопкой «удалить» на каждом."""
    items = _pending_list()
    if not items:
        _edit(bot, call,
              "🗑 <b>Удаление заказов</b>\n\n"
              "Список заказов пуст — удалять нечего.",
              IKM().add(IKB("◀ К списку", callback_data=_PENDING_LIST)))
        return

    rows = [_pending_row(v) for v in items[:25]]
    text = (
        "🗑 <b>Удаление заказов</b>\n\n"
        + "\n".join(rows)
        + "\n\n<i>Выберите заказ, который хотите удалить из плагина. "
          "FunPay-заказ и SMMPrime-заказ при этом не отменяются — "
          "удаляется ТОЛЬКО запись плагина.</i>"
    )
    kb = IKM()
    for v in items[:25]:
        oid = v.get("funpay_order_id")
        st = v.get("status", "?")
        icon = _STATUS_ICONS.get(st, "•")
        buyer = _truncate(v.get("buyer_username") or "?", 16)
        kb.add(IKB(
            f"{icon} #{oid} · {buyer}",
            callback_data=f"{_PENDING_DEL_ASK}:{oid}",
        ))
    kb.add(IKB("◀ К списку", callback_data=_PENDING_LIST))
    _edit(bot, call, text, kb)


def _screen_pending_del_ask(bot, call: CallbackQuery, oid: str) -> None:
    """v3.8 — диалог подтверждения «вы точно хотите удалить?»."""
    rec = _pending_get(oid)
    if rec is None:
        bot.answer_callback_query(
            call.id, "Заказ не найден (возможно, уже удалён)",
            show_alert=True)
        _screen_pending_del_list(bot, call)
        return
    text = (
        "🗑 <b>Удалить этот заказ из плагина?</b>\n\n"
        f"{_pending_row(rec)}\n\n"
        "<b>Вы точно хотите удалить этот заказ?</b>\n\n"
        "<i>Удаляется ТОЛЬКО запись плагина в "
        "<code>storage/smmprime_pending_orders.json</code>. "
        "Заказ FunPay не отменяется. Заказ SMMPrime (если уже создан) "
        "тоже не отменяется. После удаления бот больше не будет ждать "
        "ссылку по этому заказу.</i>"
    )
    kb = IKM()
    kb.add(IKB("✅ Да, удалить",
               callback_data=f"{_PENDING_DEL_OK}:{oid}"))
    kb.add(IKB("❌ Отмена", callback_data=_PENDING_DEL_LIST))
    _edit(bot, call, text, kb)


def _screen_pending_purge_ask(bot, call: CallbackQuery, group: str) -> None:
    """v3.8 — подтверждение массовой очистки группы."""
    statuses = _PURGE_GROUPS.get(group)
    label = _PURGE_LABELS.get(group, group)
    if not statuses:
        bot.answer_callback_query(call.id, "Bad group", show_alert=True)
        _screen_pending(bot, call)
        return
    items = _pending_list(statuses=statuses)
    if not items:
        bot.answer_callback_query(
            call.id, f"Нет заказов в группе «{label}»", show_alert=True)
        _screen_pending(bot, call)
        return
    preview = "\n".join(_pending_row(v) for v in items[:10])
    more = ""
    if len(items) > 10:
        more = f"\n<i>… и ещё {len(items) - 10}</i>"
    text = (
        f"🧹 <b>Очистить «{label}»?</b>\n\n"
        f"Будет удалено записей: <b>{len(items)}</b>\n"
        f"Статусы: <code>{', '.join(statuses)}</code>\n\n"
        f"{preview}{more}\n\n"
        "<b>Вы точно хотите удалить эти заказы?</b>\n\n"
        "<i>Удаляются ТОЛЬКО записи плагина. Заказы FunPay и SMMPrime "
        "не отменяются.</i>"
    )
    kb = IKM()
    kb.add(IKB(f"✅ Да, удалить {len(items)}",
               callback_data=f"{_PENDING_PURGE_OK}:{group}"))
    kb.add(IKB("❌ Отмена", callback_data=_PENDING_LIST))
    _edit(bot, call, text, kb)


def _screen_balance(bot, call: CallbackQuery) -> None:
    cfg = _load()
    api_key = cfg.get("api_key", "")
    if not api_key:
        _edit(bot, call,
              "🔑 <b>API ключ SMMPrime не задан.</b>\n\nВведите ключ в "
              "главном меню → 🔑 API-ключ.",
              IKM().add(IKB("◀ Назад", callback_data=_MAIN)))
        return
    try:
        d = SMMPrimeClient(api_key).get_balance()
        text = (
            f"💰 <b>Баланс SMMPrime</b>\n\n"
            f"<b>{_h(str(d.get('balance', '?')))} {_h(str(d.get('currency', '')))}</b>\n\n"
            f"🔑 Ключ: <code>{_mask(api_key)}</code>"
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("[SMMPrime] balance error")
        text = _api_error_text(e, api_key)
    _edit(bot, call, text, IKM().add(IKB("◀ Назад", callback_data=_MAIN)))


def _screen_services(bot, call: CallbackQuery, offset: int) -> None:
    cfg = _load()
    api_key = cfg.get("api_key", "")
    if not api_key:
        _edit(bot, call,
              "🔑 <b>API ключ SMMPrime не задан.</b>",
              IKM().add(IKB("◀ Назад", callback_data=_MAIN)))
        return
    try:
        services = SMMPrimeClient(api_key).get_services()
    except Exception as e:  # noqa: BLE001
        logger.exception("[SMMPrime] services error")
        _edit(bot, call, _api_error_text(e, api_key),
              IKM().add(IKB("◀ Назад", callback_data=_MAIN)))
        return

    page_size = 10
    total = len(services)
    if total == 0:
        _edit(bot, call, "📃 <b>Услуг не найдено.</b>",
              IKM().add(IKB("◀ Назад", callback_data=_MAIN)))
        return
    offset = max(0, min(offset, max(0, total - 1)))
    page = services[offset:offset + page_size]
    rows = []
    for svc in page:
        sid = svc.get("id") or svc.get("service") or "?"
        nm = _h(_truncate(_service_name(svc), 60))
        mn = svc.get("min", "?")
        mx = svc.get("max", "?")
        rate = svc.get("rate", "?")
        rows.append(f"<code>{sid}</code> — {nm}\n  min={mn} max={mx} rate={rate}")
    text = (
        f"📃 <b>Список услуг SMMPrime</b> "
        f"({offset + 1}-{min(offset + page_size, total)} из {total})\n\n"
        + "\n\n".join(rows)
    )
    nav = IKM()
    row = []
    if offset > 0:
        row.append(IKB("◀", callback_data=f"{_LIST_SERVICES}:{max(0, offset - page_size)}"))
    if offset + page_size < total:
        row.append(IKB("▶", callback_data=f"{_LIST_SERVICES}:{offset + page_size}"))
    if row:
        nav.row(*row)
    nav.add(IKB("◀ В главное меню", callback_data=_MAIN))
    _edit(bot, call, text, nav)


# ─────────────────────────────────────────────────────────────────────────────
#  Совместимость с возможным API кардинала get_settings_*
# ─────────────────────────────────────────────────────────────────────────────

def get_settings_keyboard() -> IKM:
    return _kb_main()


def get_settings_text() -> str:
    return _main_text()


# ─────────────────────────────────────────────────────────────────────────────
#  ЛОГИКА: (фаза 1) NEW_ORDER → запоминаем pending + просим ссылку.
# ─────────────────────────────────────────────────────────────────────────────

def _process_order(cardinal: "Cardinal", event) -> None:
    """v3.11 — единый сценарий (по ТЗ покупателя 14-step):

      1) Новый заказ FunPay приходит сюда.
      2) Не мешаем приветствию автоответчика FunPay Terminal — мы
         присылаем своё сообщение независимо, оно идёт в тот же чат.
      3) Плагин отправляет текст просьбы ссылки.
      4) Записываем pending → status=waiting_for_link.
      5..14) дальше всё в _process_buyer_message:
           ссылка → status=waiting_for_quantity → число → проверка
           min/max → SMMPrime или dry-run → финальное сообщение.

    Никакого Variant A/B и quantity_mode больше нет: количество ВСЕГДА
    спрашивается у покупателя ПОСЛЕ ссылки.
    """
    order = event.order
    oid = str(getattr(order, "id", "???"))
    title = getattr(order, "description", "") or ""
    buyer = getattr(order, "buyer_username", "???") or "???"
    buyer_id = getattr(order, "buyer_id", None)
    chat_id = getattr(order, "chat_id", None)

    # v1.0.0 — расширенное логирование NEW_ORDER. Раньше падало в одну
    # строку, и продавцу было непонятно почему «бот молчит». Теперь
    # видно весь набор полей события и состояние связок.
    logger.info(
        f"[SMMPrime.ORDER] NEW_ORDER #{oid}: title='{_truncate(title, 100)}' "
        f"buyer='{buyer}' buyer_id={buyer_id} chat_id={chat_id}"
    )

    cfg = _load()
    if not cfg.get("enabled", True):
        logger.info(
            f"[SMMPrime.ORDER] #{oid}: плагин выключен (enabled=False), "
            "ничего не делаем. Включите его в /menu → SMMPrime Auto-Order → "
            "«Статус плагина: …»."
        )
        return

    all_bindings = _bindings(cfg)
    if not all_bindings:
        # v1.0.0 — частый кейс после миграции: пользователь поставил плагин,
        # но не создал ни одной связки. NEW_ORDER приходит, но связки нет —
        # раньше это уходило в тихий лог, и в боте было видно «Заказы 0/0».
        # Теперь шлём админу уведомление, чтобы заказ не остался незамечен.
        logger.warning(
            f"[SMMPrime.ORDER] #{oid}: ни одной связки не задано — заказ "
            "не будет обработан. Создайте связку через "
            "/menu → SMMPrime Auto-Order → 🛒 Связки → ➕ Добавить связку."
        )
        _notify_admin(
            cardinal,
            f"⚠️ <b>[SMMPrime]</b> Получен заказ <code>#{oid}</code> "
            f"«<b>{_h(_truncate(title, 80))}</b>» от <b>{_h(buyer)}</b>, "
            f"но в плагине <b>нет ни одной связки</b>.\n"
            f"Создайте связку: /menu → 🧩 Плагины → SMMPrime Auto-Order → "
            f"🛒 Связки → ➕ Добавить связку.\n"
            f"<i>До этого момента плагин не будет создавать заказы "
            f"SMMPrime и не будет писать покупателю.</i>"
        )
        return

    idx, b = _find_binding(cfg, title)
    if b is None:
        # v1.0.0 — раньше тут было только «не в связках» одной строкой,
        # без указания КАКИХ именно связок и что искалось. Теперь
        # выдаём диагностический дамп: lot.title (что пришло от FunPay)
        # и список title всех существующих связок. Так продавец сразу
        # видит проблему — например, в связке title="Stars", а лот
        # называется «Telegram | Голоса». Или связка стоит на другой
        # лот по ошибке.
        existing_titles = [
            f"#{i+1} '{(bb.get('title') or '?')[:60]}'"
            for i, bb in enumerate(all_bindings)
        ]
        logger.warning(
            f"[SMMPrime.ORDER] #{oid}: лот '{_truncate(title, 80)}' НЕ "
            f"совпал ни с одной из {len(all_bindings)} связок. "
            f"Связки: {', '.join(existing_titles) if existing_titles else '—'}. "
            f"Сравнение — подстрока binding.title.lower() ⊆ lot.title.lower()."
        )
        _notify_admin(
            cardinal,
            f"⚠️ <b>[SMMPrime]</b> Получен заказ <code>#{oid}</code> "
            f"«<b>{_h(_truncate(title, 80))}</b>» от <b>{_h(buyer)}</b>, "
            f"но <b>ни одна связка не сработала</b>.\n"
            f"Проверьте, что в одной из ваших {len(all_bindings)} "
            f"связок поле <code>title</code> является подстрокой названия "
            f"лота FunPay (регистронезависимо).\n"
            f"<i>Сейчас связки: "
            f"{', '.join(existing_titles[:5]) if existing_titles else '—'}"
            f"{'…' if len(existing_titles) > 5 else ''}</i>"
        )
        return
    if not b.get("enabled", True):
        logger.warning(
            f"[SMMPrime.ORDER] #{oid}: связка #{idx+1} "
            f"('{b.get('title')}') СОПОСТАВЛЕНА, но выключена "
            f"(enabled=False) — заказ не обрабатывается."
        )
        _notify_admin(
            cardinal,
            f"⚠️ <b>[SMMPrime]</b> Заказ <code>#{oid}</code> совпал со "
            f"связкой #{idx+1} «<b>{_h(b.get('title') or '?')}</b>», но "
            f"связка <b>выключена</b>. Включите её в /menu → SMMPrime "
            f"Auto-Order → 🛒 Связки → выберите → Статус."
        )
        return

    logger.info(
        f"[SMMPrime.ORDER] #{oid}: связка #{idx+1} '{b.get('title')}' "
        f"матч найдена (service_id={b.get('service')}, "
        f"dry_run={b.get('dry_run', True)})."
    )

    api_key = cfg.get("api_key", "")
    dry_run = bool(b.get("dry_run", True))

    if not api_key and not dry_run:
        _notify_admin(
            cardinal,
            f"⚠️ <b>[SMMPrime]</b> Заказ <code>#{oid}</code> от "
            f"<b>{_h(buyer)}</b>\n"
            f"❌ API-ключ не задан. Заказ не будет создан.\n"
            f"<i>Включите dry-run или задайте API-ключ.</i>")
        return

    # Валидация service_id (без него — ничего не сделаем).
    try:
        svc_id = int(b["service"])
        if svc_id <= 0:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        _notify_admin(cardinal,
                      f"❌ <b>[SMMPrime]</b> #{oid}: некорректный service_id "
                      f"в связке «{_h(b.get('title') or '?')}».")
        _send_buyer_with_template(
            cardinal, cfg, b,
            kind="error",
            pending_for_vars={
                "funpay_order_id": oid, "buyer_username": buyer,
                "lot_id": b.get("funpay_lot_id"),
                "service_id": b.get("service"),
                "service_name": b.get("service_name") or "—",
                "quantity": "—",
                "dry_run": dry_run,
            },
            chat_id=chat_id, chat_name=buyer,
        )
        return

    # Идемпотентность по NEW_ORDER (на случай повторных событий).
    # v3.12 — расширили список «известных» статусов до всех 9 (добавили
    # waiting_for_confirm и cancelled): любой повторный NEW_ORDER по
    # уже существующему оформлению игнорируем, иначе перезапишем pending
    # и покупатель получит дублированную просьбу ссылки.
    existing = _pending_get(oid)
    if existing and existing.get("status") in (
        "waiting_for_link", "waiting_for_quantity", "waiting_for_confirm",
        "processing", "smm_created", "dry_run_done", "failed", "cancelled",
    ):
        logger.info(f"[SMMPrime] Заказ #{oid} уже в pending "
                    f"(status={existing.get('status')}), повторный "
                    f"NEW_ORDER игнорируется.")
        return

    # v3.11 — авто-подтягивание min/max/имени услуги SMMPrime, если их ещё нет
    # в связке. Это закрывает баг «ASK_LINK с пустым service_name» из
    # скриншота: мы не показываем покупателю «—» как название.
    need_refresh = (
        not (b.get("service_name") or "").strip()
        or int(b.get("min_quantity") or 0) <= 0
        or int(b.get("max_quantity") or 0) <= 0
    )
    if need_refresh:
        if not api_key:
            # v3.12 — гард: НЕ дёргаем SMMPrime, если api_key не задан.
            # Раньше тут была попытка _refresh_service_info с пустым
            # ключом → SMMPrimeAuthError → лог «auto-refresh service
            # info FAILED: Unauthorized», что вводило в заблуждение
            # (ключ не «отклонён», его просто нет).
            logger.warning(
                f"[SMMPrime.ORDER] #{oid} auto-refresh service info "
                f"SKIPPED: api_key не задан в настройках плагина."
            )
            _notify_admin(
                cardinal,
                f"⚠ <b>[SMMPrime]</b> #{oid}: "
                f"<b>API ключ SMMPrime не задан</b>. Информацию услуги "
                f"#{b.get('service')} (min/max/название) подтянуть не "
                f"удалось. Откройте настройки плагина и введите API ключ "
                f"SMMPrime, чтобы заказы создавались автоматически."
            )
        else:
            try:
                ok_r, reason_r = _refresh_service_info(api_key, b)
            except Exception as e:  # noqa: BLE001
                ok_r, reason_r = False, f"исключение: {e}"
            if ok_r:
                # Сохраняем обновлённый binding в конфиг.
                bs = _bindings(cfg)
                if isinstance(idx, int) and 0 <= idx < len(bs):
                    bs[idx] = b
                    cfg["bindings"] = bs
                    _save(cfg)
                logger.info(
                    f"[SMMPrime.ORDER] #{oid} auto-refresh service info OK: "
                    f"name='{b.get('service_name')}' "
                    f"min={b.get('min_quantity')} max={b.get('max_quantity')}"
                )
            else:
                logger.warning(
                    f"[SMMPrime.ORDER] #{oid} auto-refresh service info "
                    f"FAILED: {reason_r}"
                )
                # v3.12 — отдельное уведомление админу по 401:
                # «SMMPrime отклонил API ключ». Иначе — обычное предупреждение.
                rl = reason_r.lower() if isinstance(reason_r, str) else ""
                if ("unauthorized" in rl or "401" in rl
                        or "api ключ" in rl):
                    _notify_admin(
                        cardinal,
                        f"❌ <b>[SMMPrime]</b> #{oid}: "
                        f"<b>SMMPrime отклонил API ключ</b> при попытке "
                        f"подтянуть инфо услуги #{b.get('service')}. "
                        f"Проверьте api_key в настройках плагина."
                    )
                else:
                    _notify_admin(
                        cardinal,
                        f"⚠ <b>[SMMPrime]</b> #{oid}: не удалось "
                        f"подтянуть инфо услуги #{b.get('service')}: "
                        f"<code>{_h(str(reason_r))}</code>"
                    )

    # v3.14 — quantity ВСЕГДА берём из заказа FunPay по новому ТЗ.
    # Если по какой-то причине не смогли вытащить (нет amount, нет
    # описания) — заказ оформляется НЕ автоматически (админ разберётся
    # вручную). Покупателя количеством мы больше не дёргаем.
    auto_qty = _resolve_quantity_from_order(order, b)
    if auto_qty is None:
        logger.warning(
            f"[SMMPrime.ORDER] #{oid}: не удалось определить количество "
            f"из заказа FunPay (amount={getattr(order, 'amount', None)!r}, "
            f"desc={_truncate(getattr(order, 'description', '') or '', 80)!r})."
        )
        _notify_admin(
            cardinal,
            f"❌ <b>[SMMPrime]</b> #{oid}: <b>не удалось определить "
            f"количество</b> из заказа FunPay. Авто-оформление пропущено, "
            f"оформите заказ вручную.\n\n"
            f"Покупатель: <b>{_h(buyer)}</b>\n"
            f"Связка: <b>{_h(b.get('title') or '?')}</b>"
        )
        # Покупателю — короткое error-сообщение (без quantity).
        _send_buyer_with_template(
            cardinal, cfg, b, kind="error",
            pending_for_vars={
                "funpay_order_id": oid, "buyer_username": buyer,
                "lot_id": b.get("funpay_lot_id"),
                "service_id": svc_id,
                "service_name": b.get("service_name") or "—",
                "quantity": "—",
                "dry_run": dry_run,
            },
            chat_id=chat_id, chat_name=buyer,
        )
        return
    logger.info(
        f"[SMMPrime.ORDER] #{oid} количество из заказа FunPay: "
        f"quantity={auto_qty}."
    )

    # v3.14 — сразу проверяем min/max услуги SMMPrime. Если quantity вне
    # диапазона, авто-оформить нельзя: количество в FunPay уже зафиксировано
    # покупателем, изменить нельзя. Уведомляем админа и пишем покупателю
    # error-сообщение. Заказ дальше по сценарию НЕ идёт.
    min_q = int(b.get("min_quantity") or 0)
    max_q = int(b.get("max_quantity") or 0)
    qty_ok, qty_reason = _validate_quantity_range(
        auto_qty, {"min_quantity": min_q, "max_quantity": max_q}
    )
    if not qty_ok:
        logger.warning(
            f"[SMMPrime.ORDER] #{oid}: quantity={auto_qty} вне диапазона "
            f"услуги SMMPrime #{svc_id} (min={min_q} max={max_q}, "
            f"reason={qty_reason})."
        )
        _notify_admin(
            cardinal,
            f"❌ <b>[SMMPrime]</b> #{oid}: <b>quantity={auto_qty} вне "
            f"диапазона услуги</b> SMMPrime #{svc_id} "
            f"(min={min_q}, max={max_q}, reason={qty_reason}). "
            f"Авто-оформление пропущено.\n\n"
            f"Покупатель: <b>{_h(buyer)}</b>\n"
            f"Связка: <b>{_h(b.get('title') or '?')}</b>\n"
            f"Решите вручную: либо вернуть деньги, либо оформить вручную "
            f"в SMMPrime."
        )
        # v3.15 — отдельные шаблоны для below_min / above_max. По ТЗ:
        # покупатель уже зафиксировал quantity на FunPay, поэтому в чате
        # ему говорим коротко и направляем ждать отмены. Шаблоны
        # qty_too_small / qty_too_large срабатывают ТОЛЬКО когда min/max
        # реально заданы (>0) — это уже гарантировано
        # `_validate_quantity_range`. Фолбэк kind="error" остаётся для
        # любых других reasons (not_a_number / not_positive).
        if qty_reason == "too_small" and min_q > 0:
            buyer_kind = "qty_too_small"
        elif qty_reason == "too_large" and max_q > 0:
            buyer_kind = "qty_too_large"
        else:
            buyer_kind = "error"
        _send_buyer_with_template(
            cardinal, cfg, b, kind=buyer_kind,
            pending_for_vars={
                "funpay_order_id": oid, "buyer_username": buyer,
                "lot_id": b.get("funpay_lot_id"),
                "service_id": svc_id,
                "service_name": b.get("service_name") or "—",
                "quantity": auto_qty,
                "min": min_q, "max": max_q,
                "min_quantity": min_q, "max_quantity": max_q,
                "dry_run": dry_run,
            },
            chat_id=chat_id, chat_name=buyer,
        )
        # Сохраняем pending как failed, чтобы был трейл в админ-меню.
        failed_pending = {
            "funpay_order_id": oid,
            "buyer_username": buyer,
            "buyer_id": buyer_id,
            "chat_id": chat_id,
            "lot_id": b.get("funpay_lot_id"),
            "lot_title": b.get("title"),
            "binding_idx": idx,
            "service_id": svc_id,
            "service_name": b.get("service_name") or "",
            "min_quantity": min_q,
            "max_quantity": max_q,
            "dry_run": dry_run,
            "smm_order_id": None,
            "link": None,
            "quantity": auto_qty,
            "quantity_source": "from_order",
            "status": "failed",
            "created_at": int(time.time()),
        }
        _pending_upsert(failed_pending)
        return

    pending = {
        "funpay_order_id": oid,
        "buyer_username": buyer,
        "buyer_id": buyer_id,
        "chat_id": chat_id,
        "lot_id": b.get("funpay_lot_id"),
        "lot_title": b.get("title"),
        "binding_idx": idx,
        "service_id": svc_id,
        "service_name": b.get("service_name") or "",
        "service_category": b.get("service_category") or "",
        "link_example": b.get("link_example") or "",
        "link_type": b.get("link_type") or "generic",
        "min_quantity": min_q,
        "max_quantity": max_q,
        "dry_run": dry_run,
        "smm_order_id": None,
        "link": None,
        # v3.14 — quantity всегда из заказа FunPay, валидно.
        "quantity": auto_qty,
        "quantity_source": "from_order",
        "status": "waiting_for_link",
        "created_at": int(time.time()),
    }
    _pending_upsert(pending)
    logger.info(
        f"[SMMPrime.ORDER] Pending #{oid} СОХРАНЁН: "
        f"status=waiting_for_link service_id={svc_id} "
        f"quantity={auto_qty} min={pending['min_quantity']} "
        f"max={pending['max_quantity']} dry_run={dry_run}"
    )

    ask_text = _render_template(
        _resolve_text(cfg, b, "ask_link"),
        _build_template_vars(pending),
    )
    _send_buyer(cardinal, chat_id, buyer, ask_text)

    _notify_admin(
        cardinal,
        f"⏳ <b>[SMMPrime]</b> Новый заказ <code>#{oid}</code>\n"
        f"Покупатель: <b>{_h(buyer)}</b>\n"
        f"Связка: <b>{_h(b.get('title') or '?')}</b>\n"
        f"FunPay лот: <code>{_h(b.get('funpay_lot_id') or '—')}</code>\n"
        f"service_id: <code>{svc_id}</code> "
        f"({_h(b.get('service_name') or '—')})\n"
        f"quantity (из заказа FunPay): <code>{auto_qty}</code>\n"
        f"min/max: <code>{pending['min_quantity']}</code> — "
        f"<code>{pending['max_quantity']}</code>\n"
        f"Режим: <b>{'🟡 dry-run' if dry_run else '⚪ боевой'}</b>\n\n"
        f"Бот попросил у покупателя ссылку. Дальше: "
        f"ссылка → подтверждение → SMMPrime/dry-run → финал."
    )


def _send_buyer_with_template(cardinal, cfg, b, kind, pending_for_vars,
                              chat_id, chat_name) -> None:
    """Утилита: отправить покупателю текст по шаблону соответствующего типа.

    v3.13 — если кастомный per-binding или глобальный шаблон оказался
    невалидным (пустой после рендера, ИЛИ в blocklist техкоманд вроде
    «menu»/«/menu»), мы пишем громкий ERROR в лог + уведомляем админа +
    автоматически откатываемся на дефолтный шаблон для этого `kind`.
    Так покупатель никогда не остаётся без финального ответа из-за
    случайно сохранённого мусорного текста (см. issue v3.13: после
    «Да» бот пытался отправить literal 'menu', который попал в шаблон
    `buyer_dry_run_text` через диалог настройки в TG-боте).
    """
    template = _resolve_text(cfg, b, kind)
    vars_ = _build_template_vars(pending_for_vars)
    rendered = _render_template(template, vars_)
    if _is_buyer_text_invalid(rendered):
        # Шаблон сломан/мусорный → fallback на дефолт.
        default_tpl = _default_template_for_kind(kind)
        rendered_default = _render_template(default_tpl, vars_)
        logger.error(
            f"[SMMPrime.SEND] guard: шаблон '{kind}' рендерится как "
            f"пустой/блокированный текст ({rendered.strip()[:80]!r}). "
            f"Откатываемся на ДЕФОЛТНЫЙ шаблон. Проверьте настройки "
            f"buyer_{_field_for_kind(kind)} в плагине — там могло "
            f"оказаться 'menu' или другой мусор."
        )
        try:
            _notify_admin(
                cardinal,
                f"⚠ <b>[SMMPrime]</b> шаблон <code>{_h(kind)}</code> "
                f"рендерится как мусор: <code>{_h(rendered.strip()[:80])}"
                f"</code>. Откатились на дефолт. Проверьте текст "
                f"<code>buyer_{_h(_field_for_kind(kind))}</code> "
                f"в настройках плагина / связки."
            )
        except Exception:  # noqa: BLE001
            pass
        rendered = rendered_default
    _send_buyer(cardinal, chat_id, chat_name, rendered)


def _is_buyer_text_invalid(rendered: str) -> bool:
    """v3.13 — true если рендеренный текст НЕ годен для отправки покупателю.

    Используется как safety-net в `_send_buyer_with_template`.
    Дублирует ту же логику что в `_send_buyer.guard`, чтобы заранее
    подменить мусорный текст на дефолтный.
    """
    if not isinstance(rendered, str):
        return True
    stripped = _strip_leading_emoji(rendered).strip()
    if not stripped:
        return True
    if stripped.lower() in _TECH_BLOCKLIST:
        return True
    return False


_KIND_TO_DEFAULT = {
    "ask_link":      lambda: _DEFAULT_ASK_LINK_TEMPLATE,
    "ask_quantity":  lambda: _DEFAULT_ASK_QUANTITY_TEMPLATE,
    "qty_too_small": lambda: _DEFAULT_QTY_TOO_SMALL_TEMPLATE,
    "qty_too_large": lambda: _DEFAULT_QTY_TOO_LARGE_TEMPLATE,
    "success":       lambda: _DEFAULT_SUCCESS_TEMPLATE,
    "error":         lambda: _DEFAULT_ERROR_TEMPLATE,
    "dry_run":       lambda: _DEFAULT_DRY_RUN_TEMPLATE,
    "confirm":       lambda: _DEFAULT_CONFIRM_TEMPLATE,
    "cancelled":     lambda: _DEFAULT_CANCELLED_TEMPLATE,
    "not_link":      lambda: _DEFAULT_NOT_LINK_TEMPLATE,
    "not_number":    lambda: _DEFAULT_NOT_NUMBER_TEMPLATE,
    "not_confirm":   lambda: _DEFAULT_NOT_CONFIRM_TEMPLATE,
    "already_done":  lambda: _DEFAULT_ALREADY_DONE_TEMPLATE,
}

_KIND_TO_FIELD = {
    "ask_link":      "ask_link_text",
    "ask_quantity":  "ask_quantity_text",
    "qty_too_small": "qty_too_small_text",
    "qty_too_large": "qty_too_large_text",
    "success":       "success_text",
    "error":         "error_text",
    "dry_run":       "dry_run_text",
    "confirm":       "confirm_text",
    "cancelled":     "cancelled_text",
}


def _default_template_for_kind(kind: str) -> str:
    fn = _KIND_TO_DEFAULT.get(kind)
    return fn() if fn else ""


def _field_for_kind(kind: str) -> str:
    return _KIND_TO_FIELD.get(kind, kind)


# v3.13 — rate-limit для «Не понял» fallback'ов. Чтобы не спамить
# покупателя в пингпонге с автоответчиком FunPay Cardinal: один
# «Не понял …» в 60 секунд per (chat_id, kind).
_NOT_UNDERSTOOD_COOLDOWN_SEC = 60.0
_NOT_UNDERSTOOD_LAST: dict[tuple, float] = {}

# v3.14 — «grace-период» сразу после создания pending. В это окно
# любые «Не понял …» подавляются. Это исправляет баг (см. скриншот
# v3.13): иногда NEW_MESSAGE по «старому» сообщению чата (FunPay
# Terminal-приветствие, history-replay, перезапуск чата при оплате)
# обрабатывался ДО того, как наш ASK_LINK успевал улететь — и в чате
# покупатель сначала видел «Не понял», а потом «Привет!». 5 секунд
# хватает, чтобы ASK_LINK гарантированно ушло первым; на нормальный
# UX это не влияет (живой человек не успевает напечатать ссылку быстрее).
_NOT_UNDERSTOOD_GRACE_SEC = 5.0


def _send_not_understood(cardinal, cfg, b, kind, pending_for_vars,
                        chat_id, chat_name) -> bool:
    """v3.13 — отправить «Не понял …» с rate-limit + (v3.14) grace-period.

    `kind` ∈ {'not_link', 'not_number', 'not_confirm'}.
    Возвращает True, если сообщение было отправлено, False если
    подавлено rate-limit'ом, grace-периодом или у нас нет валидного
    chat_id.
    """
    if not chat_id:
        return False
    # v3.14 grace: если pending свежий, не пишем «Не понял».
    try:
        created = int((pending_for_vars or {}).get("created_at") or 0)
    except (TypeError, ValueError):
        created = 0
    now = time.time()
    if created and now - created < _NOT_UNDERSTOOD_GRACE_SEC:
        logger.info(
            f"[SMMPrime.NOT_UNDERSTOOD] grace-suppressed kind={kind} "
            f"chat_id={chat_id} (pending создан {now - created:.1f}s "
            f"назад, grace={_NOT_UNDERSTOOD_GRACE_SEC}s)"
        )
        return False
    key = (chat_id, kind)
    last = _NOT_UNDERSTOOD_LAST.get(key, 0.0)
    if now - last < _NOT_UNDERSTOOD_COOLDOWN_SEC:
        logger.debug(
            f"[SMMPrime.NOT_UNDERSTOOD] suppressed kind={kind} "
            f"chat_id={chat_id} (cooldown {_NOT_UNDERSTOOD_COOLDOWN_SEC}s)"
        )
        return False
    _NOT_UNDERSTOOD_LAST[key] = now
    _send_buyer_with_template(
        cardinal, cfg, b, kind=kind,
        pending_for_vars=pending_for_vars,
        chat_id=chat_id, chat_name=chat_name,
    )
    return True


# v3.15 — совместимость с Cardinal-автоответчиком и встроенным меню.
# Если покупатель пишет команду из этого списка, плагин её НЕ
# обрабатывает (не шлёт «Не понял», не ставит pending в processing,
# не шлёт pre-purchase greeting). Cardinal обрабатывает её сам.
#
# Сравнение — case-insensitive, по полному тексту сообщения после
# strip(). Также любое сообщение, начинающееся с «/» (slash-command),
# считается passthrough автоматически.
#
# Если этого недостаточно — в cfg["cardinal_passthrough"] можно
# добавить свой список фраз. Они МЕРДЖАТСЯ с дефолтом.
_CARDINAL_PASSTHROUGH_DEFAULTS: frozenset[str] = frozenset({
    "позвать продавца",
    "инструкция",
    "помощь",
    "menu",
    "меню",
    "/menu",
    "/start",
    "/help",
    "помоги",
    "поддержка",
})


def _is_cardinal_passthrough_message(text: str, cfg: dict) -> bool:
    """v3.15 — true, если сообщение должно пройти мимо плагина к Cardinal.

    Срабатывает на:
      • любые slash-команды (`/`, `/menu`, `/start`, `/help`…);
      • точное совпадение (case-insensitive) с фразой из
        `_CARDINAL_PASSTHROUGH_DEFAULTS` или `cfg["cardinal_passthrough"]`.

    Не блокирует ссылки, «Да», «Отмена», числа — они нужны плагину.
    """
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.startswith("/"):
        return True
    lowered = stripped.lower()
    extra = cfg.get("cardinal_passthrough") if isinstance(cfg, dict) else None
    extra_set: set[str] = set()
    if isinstance(extra, list):
        for item in extra:
            if isinstance(item, str) and item.strip():
                extra_set.add(item.strip().lower())
    if lowered in _CARDINAL_PASSTHROUGH_DEFAULTS:
        return True
    if lowered in extra_set:
        return True
    return False


# v3.15 — pre-purchase greeting (см. _DEFAULT_PRE_PURCHASE_GREETING_TEMPLATE).
# Rate-limit на чат: чтобы не спамить покупателя при многократных
# сообщениях, шлём приветствие максимум 1 раз в 24 часа per chat_id
# (или per buyer_username, если chat_id неизвестен).
_PRE_PURCHASE_COOLDOWN_SEC = 24 * 60 * 60.0
_PRE_PURCHASE_GREETING_LAST: dict[str, float] = {}


def _maybe_send_pre_purchase_greeting(cardinal, cfg, chat_id, chat_name,
                                      author_id) -> bool:
    """v3.15 — отправить дружелюбное приветствие покупателю, который
    пишет в чат до покупки (нет активного pending).

    По умолчанию ВЫКЛЮЧЕНО (`cfg["pre_purchase_greeting_enabled"]` = False),
    включается одной кнопкой в админ-меню Telegram.

    Не вызывается, если сообщение прошло passthrough-проверку
    `_is_cardinal_passthrough_message` — это уже сделано в
    `_process_buyer_message` ДО прихода сюда.

    Возвращает True если сообщение было отправлено, False иначе.
    """
    if not cfg.get("pre_purchase_greeting_enabled", False):
        return False
    if not chat_id:
        return False
    key = str(chat_id) if chat_id else f"u:{author_id}:{chat_name}"
    now = time.time()
    last = _PRE_PURCHASE_GREETING_LAST.get(key, 0.0)
    if now - last < _PRE_PURCHASE_COOLDOWN_SEC:
        logger.debug(
            f"[SMMPrime.PRE_PURCHASE] greeting suppressed for chat_id="
            f"{chat_id} (cooldown {_PRE_PURCHASE_COOLDOWN_SEC/3600:.0f}h)"
        )
        return False
    custom = (cfg.get("pre_purchase_greeting_text") or "").strip()
    text = custom or _DEFAULT_PRE_PURCHASE_GREETING_TEMPLATE
    # Минимальный набор переменных — без funpay_order_id и т.п.,
    # т.к. покупки ещё нет. Шаблон поддерживает {buyer_username}.
    rendered = _render_template(text, {
        "buyer_username": chat_name or "клиент",
    })
    if _is_buyer_text_invalid(rendered):
        logger.error(
            f"[SMMPrime.PRE_PURCHASE] custom text рендерится как мусор "
            f"({rendered.strip()[:80]!r}). Откатываемся на дефолт."
        )
        rendered = _render_template(
            _DEFAULT_PRE_PURCHASE_GREETING_TEMPLATE,
            {"buyer_username": chat_name or "клиент"},
        )
    _PRE_PURCHASE_GREETING_LAST[key] = now
    _send_buyer(cardinal, chat_id, chat_name, rendered)
    logger.info(
        f"[SMMPrime.PRE_PURCHASE] greeting sent to chat_id={chat_id} "
        f"username={chat_name!r}"
    )
    return True


# ─────────────────────────────────────────────────────────────────────────────
#  ЛОГИКА: (фаза 2) NEW_MESSAGE от покупателя → ловим ссылку.
# ─────────────────────────────────────────────────────────────────────────────

def _process_buyer_message(cardinal: "Cardinal", event) -> None:
    """PHASE 2: ловим сообщение покупателя со ссылкой.

    Поддерживает оба режима FunPayCardinal:
      - new mode (по умолчанию): event.message с author_id, chat_id, text;
      - old mode: event.chat (ChatShortcut) с id, name, last_message_text.
    """
    # ── 0) Извлекаем author_id / chat_id / text / username ──
    msg = getattr(event, "message", None)
    chat = getattr(event, "chat", None)

    if msg is not None:
        # NEW mode: NewMessageEvent
        mode = "new"
        author_id = getattr(msg, "author_id", None)
        text = getattr(msg, "text", "") or ""
        chat_id = getattr(msg, "chat_id", None)
        chat_name = (getattr(msg, "chat_name", None)
                     or getattr(msg, "author", None))
        by_bot = bool(getattr(msg, "by_bot", False))
    elif chat is not None:
        # OLD mode: LastChatMessageChangedEvent
        mode = "old"
        author_id = None  # старый режим не отдаёт author_id
        text = getattr(chat, "last_message_text", "") or ""
        chat_id = getattr(chat, "id", None)
        chat_name = getattr(chat, "name", None)
        by_bot = bool(getattr(chat, "last_by_bot", False))
    else:
        logger.debug("[SMMPrime.MSG] событие без message/chat — игнор")
        return

    # ── 1) Логируем ВХОД (всегда — это главный диагностический лог) ──
    logger.info(
        f"[SMMPrime.MSG] {mode}-event: chat_id={chat_id} "
        f"author_id={author_id} chat_name={chat_name} "
        f"by_bot={by_bot} text={text[:160]!r}"
    )

    # ── 2) Пропускаем своё (от продавца/бота) ──
    if by_bot:
        logger.debug("[SMMPrime.MSG] by_bot=True — игнор (наше сообщение).")
        return
    try:
        my_id = cardinal.account.id
    except Exception:
        my_id = None
    if author_id is not None and my_id is not None and \
            str(author_id) == str(my_id):
        logger.debug("[SMMPrime.MSG] author_id == my_id — игнор (свой "
                     "аккаунт).")
        return
    if author_id == 0:
        logger.debug("[SMMPrime.MSG] author_id == 0 — игнор "
                     "(системное сообщение FunPay).")
        return

    # ── 3) Плагин включён? ──
    cfg = _load()
    if not cfg.get("enabled", True):
        logger.debug("[SMMPrime.MSG] плагин выключен — игнор.")
        return

    # ── 3a) v3.15 — Cardinal passthrough. Если сообщение — это команда
    #         Cardinal-автоответчика/меню («Позвать продавца», «Инструкция»,
    #         «/menu» и т.п.), плагин её НЕ обрабатывает. Cardinal сам
    #         ответит. Это НЕ блокирует ссылки, «Да», «Отмена» — они не
    #         совпадают с passthrough-листом.
    if _is_cardinal_passthrough_message(text, cfg):
        logger.info(
            f"[SMMPrime.MSG] passthrough match for Cardinal "
            f"(text={text.strip()[:80]!r}) — отдаём обработку Cardinal'у."
        )
        return

    # ── 4) Ищем pending. v3.12 двух-этапный матчинг: ──
    #   1) ACTIVE: только waiting_for_link / waiting_for_quantity /
    #      waiting_for_confirm. Это нормальный путь — обрабатываем
    #      шаги покупки.
    #   2) Если ACTIVE не нашли, fallback в ANY (включая терминальные)
    #      — для идемпотентности: если покупатель пишет в чат после
    #      завершения заказа, мы можем ответить «уже создан». Но это
    #      работает ТОЛЬКО если активного нет.
    # Раньше (v3.11.1) был только ANY-матчинг, что приводило к багу:
    # новое сообщение по новому заказу #SME2XV1F цеплялось к старому
    # завершённому #HEA2KAPT.
    pending_any = None
    if author_id is not None:
        pending_any = _pending_find_active_for_buyer(author_id, chat_id)
        if pending_any:
            logger.info(
                f"[SMMPrime.MSG] match (active) by buyer_id={author_id} → "
                f"#{pending_any.get('funpay_order_id')} "
                f"status={pending_any.get('status')}"
            )
    if pending_any is None and chat_name:
        pending_any = _pending_find_active_for_username(chat_name)
        if pending_any:
            logger.info(
                f"[SMMPrime.MSG] match (active) by buyer_username="
                f"{chat_name!r} → #{pending_any.get('funpay_order_id')} "
                f"status={pending_any.get('status')}"
            )
    # Fallback на терминальные ТОЛЬКО для идемпотентности.
    if pending_any is None and author_id is not None:
        pending_any = _pending_find_any_for_buyer(author_id, chat_id)
        if pending_any:
            logger.info(
                f"[SMMPrime.MSG] match (terminal-fallback) by "
                f"buyer_id={author_id} → "
                f"#{pending_any.get('funpay_order_id')} "
                f"status={pending_any.get('status')}"
            )
    if pending_any is None and chat_name:
        pending_any = _pending_find_any_for_username(chat_name)
        if pending_any:
            logger.info(
                f"[SMMPrime.MSG] match (terminal-fallback) by "
                f"buyer_username={chat_name!r} → "
                f"#{pending_any.get('funpay_order_id')} "
                f"status={pending_any.get('status')}"
            )
    if pending_any is None:
        # v3.15 — pre-purchase greeting. Если в cfg включено и
        # покупатель ещё не получал приветствие в окне 24ч — шлём.
        # Cardinal-passthrough уже отфильтрован выше (см. шаг 3a),
        # так что мы НЕ перебьём ответ на «Позвать продавца».
        # Покупателю-боту/администратору тоже не отвечаем (см. шаг 2).
        if cfg.get("pre_purchase_greeting_enabled", False):
            sent = _maybe_send_pre_purchase_greeting(
                cardinal, cfg, chat_id, chat_name, author_id
            )
            if sent:
                logger.info(
                    f"[SMMPrime.MSG] нет pending — отправили pre-purchase "
                    f"приветствие chat_id={chat_id} username={chat_name!r}"
                )
                return
        logger.debug(
            f"[SMMPrime.MSG] нет pending для author_id={author_id} "
            f"username={chat_name!r} — игнор."
        )
        return

    # ── 5) Извлекаем ссылку И число ─────────────────────────────────────
    link = _extract_link(text)
    qty_in_msg = _parse_quantity_from_text(text)
    status = pending_any.get("status")
    funpay_oid = pending_any.get("funpay_order_id")

    logger.info(
        f"[SMMPrime.MSG] #{funpay_oid} status={status} "
        f"link={link!r} qty_in_msg={qty_in_msg}"
    )

    # ── 6) Если заказ уже создан/dry-run завершён — идемпотентный ответ. ─
    if status in ("smm_created", "dry_run_done"):
        if link is not None:
            vars_ = _build_template_vars(
                pending_any, link=link,
                smm_order_id=pending_any.get("smm_order_id") or "—",
            )
            reply = _render_template(_ALREADY_DONE_TEMPLATE, vars_)
            _send_buyer(cardinal,
                        pending_any.get("chat_id"),
                        pending_any.get("buyer_username"),
                        reply)
            logger.info(f"[SMMPrime.MSG] #{funpay_oid}: дубль ссылки — "
                        f"ответили «уже создан» (SMM={pending_any.get('smm_order_id')}).")
        return

    if status in ("link_received", "processing"):
        logger.info(f"[SMMPrime.MSG] #{funpay_oid} уже {status} — "
                    f"обработка идёт, игнор.")
        return

    # ── 7) v3.14 STATE MACHINE: ссылка → подтверждение → заказ. ───────
    #
    # Состояния:
    #   waiting_for_link    — ждём URL от покупателя.
    #   waiting_for_confirm — показали сводку, ждём «Да» или «Отмена».
    #   processing          — нажали «Да», создаём заказ в SMMPrime.
    #   smm_created/dry_run_done/failed — терминальные.
    #   cancelled           — терминальный (раньше использовался для
    #                          «Отмены», теперь только для совместимости
    #                          с уже сохранёнными pending v3.13).
    #
    # v3.14: «Отмена» больше НЕ терминальная. Она возвращает заказ
    # в waiting_for_link и просит покупателя другую ссылку.
    cardinal_chat_id = pending_any.get("chat_id")
    cardinal_chat_name = pending_any.get("buyer_username")

    cfg = _load()
    bs = _bindings(cfg)
    bidx = pending_any.get("binding_idx")
    b_for_text = (bs[bidx] if isinstance(bidx, int)
                  and 0 <= bidx < len(bs) else {})

    # v3.14 совместимость со старыми pending (v3.10–v3.13): если в
    # JSON остался pending со статусом waiting_for_quantity, мы его
    # больше не поддерживаем как отдельную фазу — переводим обратно
    # в waiting_for_link, чтобы покупатель прислал ссылку, а quantity
    # возьмётся из заказа FunPay (если он был сохранён в pending).
    # Если quantity вообще не было — заказ всё равно поедет дальше
    # по новому сценарию (1 шт. как минимум, см. _resolve_quantity).
    if status == "waiting_for_quantity":
        logger.info(
            f"[SMMPrime.MSG] #{funpay_oid}: legacy status "
            f"waiting_for_quantity — переводим в waiting_for_link "
            f"(в v3.14 покупателя количеством не дёргаем)."
        )
        pending_any = dict(pending_any)
        pending_any["status"] = "waiting_for_link"
        # Нормализуем quantity: если он не был установлен, оставим
        # 1 как минимальный fallback. _process_order по-новому сценарию
        # всегда сохраняет quantity из FunPay-заказа; этот fallback
        # нужен только для уже существующих миграционных pending.
        if not isinstance(pending_any.get("quantity"), int) \
                or int(pending_any.get("quantity") or 0) <= 0:
            pending_any["quantity"] = 1
            pending_any["quantity_source"] = "from_order"
        _pending_upsert(pending_any)
        status = "waiting_for_link"

    # ── 7a) waiting_for_link ────────────────────────────────────────────
    if status == "waiting_for_link":
        if link is None:
            logger.info(
                f"[SMMPrime.MSG] #{funpay_oid}: текст без URL в "
                f"waiting_for_link — отправляем «Не понял» (rate-limited)."
            )
            _send_not_understood(
                cardinal, cfg, b_for_text, kind="not_link",
                pending_for_vars=pending_any,
                chat_id=cardinal_chat_id, chat_name=cardinal_chat_name,
            )
            return

        # Ссылка получена. Сохраняем + переводим в waiting_for_confirm.
        # quantity уже в pending (сохранили на этапе NEW_ORDER из
        # заказа FunPay). По новому ТЗ покупателя количеством не
        # дёргаем — даже если оно вне диапазона услуги, мы это
        # отлавливаем ещё до создания pending.
        pending_any = dict(pending_any)
        pending_any["link"] = link
        pending_any["status"] = "waiting_for_confirm"
        _pending_upsert(pending_any)
        logger.info(
            f"[SMMPrime.MSG] #{funpay_oid}: ссылка={link!r} получена, "
            f"quantity={pending_any.get('quantity')}, "
            f"статус → waiting_for_confirm."
        )
        _send_buyer_with_template(
            cardinal, cfg, b_for_text, kind="confirm",
            pending_for_vars=pending_any,
            chat_id=cardinal_chat_id, chat_name=cardinal_chat_name,
        )
        return

    # ── 7c) waiting_for_confirm ─────────────────────────────────────────
    # v3.14 — покупатель должен подтвердить заказ перед созданием.
    # Опции:
    #   • «Да»     → создаём заказ в SMMPrime (или dry-run).
    #   • «Отмена» → возвращаемся в waiting_for_link, бот просит
    #                другую ссылку. Заказ остаётся активным.
    #   • Новая ссылка (URL) без «Да/Отмена» → заменяем link и снова
    #                показываем подтверждение. Это удобно: покупатель
    #                может прислать другую ссылку, не нажимая «Отмена».
    #   • Что-то ещё → «Не понял …» с rate-limit.
    if status == "waiting_for_confirm":
        action = _parse_confirm_reply(text)
        logger.info(
            f"[SMMPrime.MSG] #{funpay_oid} waiting_for_confirm: "
            f"action={action!r} link_in_msg={link!r} "
            f"(raw={text[:80]!r})"
        )

        # v3.14 — покупатель прислал новую ссылку без явного да/отмена:
        # заменяем link, показываем подтверждение заново.
        if action is None and link is not None:
            pending_any = dict(pending_any)
            pending_any["link"] = link
            _pending_upsert(pending_any)
            logger.info(
                f"[SMMPrime.MSG] #{funpay_oid}: получили новую ссылку "
                f"в waiting_for_confirm — обновили link={link!r}, "
                f"показываем confirm заново."
            )
            _send_buyer_with_template(
                cardinal, cfg, b_for_text, kind="confirm",
                pending_for_vars=pending_any,
                chat_id=cardinal_chat_id, chat_name=cardinal_chat_name,
            )
            return

        if action is None:
            # v3.13 — мягкий fallback вместо молчания.
            _send_not_understood(
                cardinal, cfg, b_for_text, kind="not_confirm",
                pending_for_vars=pending_any,
                chat_id=cardinal_chat_id, chat_name=cardinal_chat_name,
            )
            return

        if action == "cancel":
            # v3.14 — «Отмена» НЕ терминальная: возвращаемся в
            # waiting_for_link и просим у покупателя другую ссылку.
            # Заказ FunPay остаётся в обработке плагина, покупатель
            # может прислать новую ссылку и продолжить оформление.
            pending_any = dict(pending_any)
            pending_any["status"] = "waiting_for_link"
            pending_any["link"] = None
            _pending_upsert(pending_any)
            logger.info(
                f"[SMMPrime.MSG] #{funpay_oid}: покупатель написал "
                f"«Отмена» (text={text[:80]!r}). Возвращаемся в "
                f"waiting_for_link, ждём новую ссылку."
            )
            _send_buyer_with_template(
                cardinal, cfg, b_for_text, kind="cancelled",
                pending_for_vars=pending_any,
                chat_id=cardinal_chat_id, chat_name=cardinal_chat_name,
            )
            _notify_admin(
                cardinal,
                f"↩️ <b>[SMMPrime]</b> #{funpay_oid}: покупатель "
                f"<b>{_h(pending_any.get('buyer_username') or '?')}</b> "
                f"написал «Отмена» на этапе подтверждения. Бот ждёт "
                f"новую ссылку."
            )
            return

        # action == "yes" — FIX v1.0.2: атомарный переход в processing.
        #
        # ПРОБЛЕМА (v1.0.1): два потока (NEW_MESSAGE + LAST_CHAT_MESSAGE_CHANGED,
        # или двойной NEW_MESSAGE при old_mode) могут оба прочитать
        # status=waiting_for_confirm, оба вызвать _pending_upsert(processing)
        # и оба запустить _create_smm_from_pending — что даёт два одинаковых
        # заказа в SMMPrime.
        #
        # РЕШЕНИЕ: _pending_claim_processing выполняет compare-and-set
        # под _PENDING_LOCK — читает статус, проверяет in-memory множество
        # _ORDER_PROCESSING_SET, и только если оба условия (статус ==
        # waiting_for_confirm И oid не в множестве) выполнены — атомарно
        # занимает слот и записывает processing в файл.
        # Второй поток при попытке claim получит False и выйдет.
        claimed = _pending_claim_processing(funpay_oid)
        if not claimed:
            logger.info(
                f"[SMMPrime.MSG] #{funpay_oid}: «Да» получено, но "
                f"_pending_claim_processing вернул False — другой поток "
                f"уже обрабатывает этот заказ (дубль NEW_MESSAGE / "
                f"LAST_CHAT_MESSAGE_CHANGED). Этот поток выходит."
            )
            return

        # Обновляем локальную копию pending_any (claim уже изменил файл).
        pending_any = dict(pending_any)
        pending_any["status"] = "processing"
        logger.info(
            f"[SMMPrime.MSG] #{funpay_oid}: подтверждение получено, claim OK. "
            f"binding_idx={pending_any.get('binding_idx')}, "
            f"service_id={pending_any.get('service_id')}, "
            f"link={pending_any.get('link')!r}, "
            f"quantity={pending_any.get('quantity')}, "
            f"dry_run={pending_any.get('dry_run')}, "
            f"статус: waiting_for_confirm → processing → "
            f"_create_smm_from_pending()."
        )
        try:
            _create_smm_from_pending(cardinal, pending_any)
        except Exception as e:  # noqa: BLE001
            logger.exception(
                f"[SMMPrime.MSG] #{funpay_oid}: НЕОБРАБОТАННОЕ ИСКЛЮЧЕНИЕ "
                f"в _create_smm_from_pending: {e}"
            )
            try:
                pending_any["status"] = "failed"
                _pending_upsert(pending_any)
            except Exception:  # noqa: BLE001
                pass
            try:
                _notify_admin(
                    cardinal,
                    f"❌ <b>[SMMPrime]</b> #{funpay_oid}: внутренняя "
                    f"ошибка плагина при создании заказа: <code>"
                    f"{_h(str(e))}</code>"
                )
            except Exception:  # noqa: BLE001
                pass
            try:
                _send_buyer_with_template(
                    cardinal, cfg, b_for_text, kind="error",
                    pending_for_vars=pending_any,
                    chat_id=cardinal_chat_id, chat_name=cardinal_chat_name,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "[SMMPrime.MSG] не удалось отправить даже "
                    "финальное error-сообщение."
                )
        finally:
            # Всегда освобождаем слот — независимо от результата.
            _pending_release_processing(funpay_oid)
        return

    # Любые другие статусы (failed / cancelled и т.д.) — игнор.
    logger.info(
        f"[SMMPrime.MSG] #{funpay_oid}: status={status} — без действий."
    )


def _create_smm_from_pending(cardinal: "Cardinal", pending: dict) -> None:
    # FIX v1.0.2 — второй эшелон защиты от дублирования.
    # Убеждаемся, что статус в файле на момент вызова действительно
    # «processing» (а не, например, «smm_created» — что означает,
    # что параллельный поток уже успел всё сделать).
    oid_check = str(pending.get("funpay_order_id", ""))
    if oid_check:
        current = _pending_get(oid_check)
        if current and current.get("status") not in ("processing",):
            logger.warning(
                f"[SMMPrime] _create_smm_from_pending #{oid_check}: "
                f"статус в файле = {current.get('status')!r}, ожидался "
                f"'processing' — вероятно дубль вызова. Выходим."
            )
            return

    cfg = _load()
    api_key = cfg.get("api_key", "")
    bs = _bindings(cfg)
    idx = pending.get("binding_idx")
    if not isinstance(idx, int) or not (0 <= idx < len(bs)):
        # связка удалена — берём поля из pending
        b = {
            "service": pending.get("service_id"),
            "quantity": pending.get("quantity"),
            "title": pending.get("lot_title"),
            "dry_run": pending.get("dry_run", True),
        }
    else:
        b = bs[idx]

    oid = pending.get("funpay_order_id")
    buyer = pending.get("buyer_username")
    chat_id = pending.get("chat_id")
    link = pending.get("link") or ""
    svc_id = int(pending.get("service_id") or 0)
    qty = int(pending.get("quantity") or 0)
    dry_run = bool(pending.get("dry_run", True))

    if dry_run:
        log_line = (f"[DRY-RUN] funpay_order={oid} buyer={buyer} "
                    f"lot_id={pending.get('lot_id')} "
                    f"binding='{pending.get('lot_title')}' "
                    f"service={svc_id} qty={qty} link={link}")
        logger.info(f"[SMMPrime] {log_line}")
        _log_order(log_line)

        pending2 = dict(pending)
        pending2["status"] = "dry_run_done"
        pending2["smm_order_id"] = None
        _pending_upsert(pending2)

        _notify_admin(cardinal, _format_dry_run_admin(pending2))
        _send_buyer_with_template(
            cardinal, cfg, b, kind="dry_run",
            pending_for_vars=pending2, chat_id=chat_id, chat_name=buyer,
        )
        return

    # Боевой режим
    if not api_key:
        pending2 = dict(pending)
        pending2["status"] = "failed"
        _pending_upsert(pending2)
        _notify_admin(cardinal,
                      f"❌ <b>[SMMPrime]</b> #{oid}: API-ключ не задан, "
                      f"заказ не создан. Включите dry-run или задайте ключ.")
        _send_buyer_with_template(
            cardinal, cfg, b, kind="error",
            pending_for_vars=pending2, chat_id=chat_id, chat_name=buyer,
        )
        return

    # v3.14 — sanity-check параметров перед отправкой в SMMPrime.
    # Если что-то нулевое/пустое — сразу падаем в failed с чётким сообщением
    # админу, без бесполезного запроса в SMMPrime (он всё равно вернёт
    # ошибку, но мы потеряем понятный диагностический контекст).
    pre_errors = []
    if svc_id <= 0:
        pre_errors.append(
            f"service_id={svc_id} (<b>0/пусто</b>) — связка сломана, "
            f"проверьте поле <code>service</code>")
    if qty <= 0:
        pre_errors.append(
            f"quantity={qty} (<b>0/пусто</b>) — quantity не пришло из "
            f"FunPay-заказа или сломалась миграция")
    if not link:
        pre_errors.append("link пустой — покупатель не прислал ссылку?")
    if pre_errors:
        pending2 = dict(pending)
        pending2["status"] = "failed"
        _pending_upsert(pending2)
        _notify_admin(
            cardinal,
            f"❌ <b>[SMMPrime]</b> #{oid}: <b>пред-проверка параметров "
            f"провалилась</b>, заказ в SMMPrime НЕ отправлен.\n\n"
            + "\n".join(f"• {e}" for e in pre_errors)
            + f"\n\nКонтекст:\n"
            f"• Связка: <b>{_h(pending.get('lot_title') or '?')}</b>\n"
            f"• Покупатель: <b>{_h(buyer)}</b>"
        )
        _send_buyer_with_template(
            cardinal, cfg, b, kind="error",
            pending_for_vars=pending2, chat_id=chat_id, chat_name=buyer,
        )
        return

    # ── FIX v1.0.1: API-вызов изолирован от постобработки. ───────────────
    # ПРОБЛЕМА (v1.0.0): весь блок — API + лог + _pending_upsert +
    # _notify_admin + _send_buyer(success) — был внутри одного try.
    # Если любой из этих шагов кидал исключение ПОСЛЕ успешного
    # add_order (например, IOError при записи в smmprime_orders.log,
    # или ошибка FunPay при отправке сообщения покупателю), управление
    # падало в except → покупатель получал сначала «заказ оформлен»,
    # а сразу следом — «не удалось оформить автоматически».
    #
    # РЕШЕНИЕ: два независимых блока.
    #   1) try/except ТОЛЬКО для SMMPrime API — определяем api_success.
    #   2) Постобработка (лог, pending, notify, send) — отдельно,
    #      каждый необязательный шаг обёрнут в свой try, чтобы его
    #      ошибка не затронула отправку сообщения покупателю.
    # ─────────────────────────────────────────────────────────────────────
    smm_id = None
    api_success = False
    api_exception = None

    try:
        logger.info(
            f"[SMMPrime.API] → POST {SMMPRIME_API_URL} action=add "
            f"service={svc_id} quantity={qty} "
            f"link={_truncate(link, 200)} "
            f"key=***{api_key[-4:] if len(api_key) >= 4 else '?'}"
        )
        res = SMMPrimeClient(api_key).add_order(svc_id, link, qty)
        logger.info(f"[SMMPrime.API] ← {res!r}")
        smm_id = res.get("order", "???") if isinstance(res, dict) else "???"
        api_success = True
    except Exception as e:  # noqa: BLE001
        api_exception = e

    if api_success:
        # ── Успех: постобработка. Каждый шаг в отдельном try —
        # некритичная ошибка (лог, pending, notify) не должна
        # влиять на отправку сообщения покупателю. ────────────────
        log_line = (f"funpay_order={oid} buyer={buyer} "
                    f"lot_id={pending.get('lot_id')} "
                    f"binding='{pending.get('lot_title')}' "
                    f"service={svc_id} qty={qty} link={link} smm_id={smm_id}")
        try:
            _log_order(log_line)
        except Exception as _le:  # noqa: BLE001
            logger.warning(f"[SMMPrime] _log_order упал (некритично): {_le}")

        pending2 = dict(pending)
        pending2["status"] = "smm_created"
        pending2["smm_order_id"] = smm_id
        try:
            _pending_upsert(pending2)
        except Exception as _pe:  # noqa: BLE001
            logger.error(
                f"[SMMPrime] _pending_upsert упал (заказ создан в SMMPrime, "
                f"но статус не сохранён локально): {_pe}"
            )

        try:
            _notify_admin(
                cardinal,
                f"✅ <b>[SMMPrime]</b> Заказ размещён!\n\n"
                f"FunPay: <code>#{oid}</code> | <b>{_h(buyer)}</b>\n"
                f"Связка: <b>{_h(pending.get('lot_title') or '?')}</b>\n"
                f"FunPay лот: <code>{_h(pending.get('lot_id') or '—')}</code>\n"
                f"Ссылка: <code>{_h(link)}</code>\n"
                f"Услуга: <code>{svc_id}</code> × <code>{qty}</code>\n"
                f"SMM ID: <code>{smm_id}</code>"
            )
        except Exception as _ne:  # noqa: BLE001
            logger.warning(f"[SMMPrime] _notify_admin (success) упал (некритично): {_ne}")

        logger.info(f"[SMMPrime] FunPay #{oid} → SMM #{smm_id}")
        _send_buyer_with_template(
            cardinal, cfg, b, kind="success",
            pending_for_vars=pending2, chat_id=chat_id, chat_name=buyer,
        )

    else:
        # ── Ошибка API: расширенный диагностический отчёт админу. ────
        # Покупателю идёт короткий buyer_error_text без технических
        # подробностей (как было в v1.0.0).
        e = api_exception
        err_text = _api_error_text(e, api_key)
        pending2 = dict(pending)
        pending2["status"] = "failed"
        try:
            _pending_upsert(pending2)
        except Exception as _pe:  # noqa: BLE001
            logger.error(f"[SMMPrime] _pending_upsert (failed) упал: {_pe}")

        admin_msg = (
            f"❌ <b>[SMMPrime]</b> #{oid}: создание заказа в SMMPrime "
            f"<b>упало</b>.\n\n{err_text}\n\n"
            f"<b>Параметры запроса:</b>\n"
            f"<code>POST {SMMPRIME_API_URL}</code>\n"
            f"<code>key=***{_h(api_key[-4:] if len(api_key) >= 4 else '?')}"
            f"</code>\n"
            f"<code>action=add</code>\n"
            f"<code>service={svc_id}</code>\n"
            f"<code>quantity={qty}</code>\n"
            f"<code>link={_h(_truncate(link, 200))}</code>\n\n"
            f"<b>Контекст:</b>\n"
            f"• Связка: <b>{_h(pending.get('lot_title') or '?')}</b>\n"
            f"• Покупатель: <b>{_h(buyer)}</b>\n"
            f"• Класс ошибки: <code>{type(e).__name__}</code>"
        )
        try:
            _notify_admin(cardinal, admin_msg)
        except Exception as _ne:  # noqa: BLE001
            logger.warning(f"[SMMPrime] _notify_admin (error) упал (некритично): {_ne}")

        logger.error(
            f"[SMMPrime.API] ✗ FunPay #{oid}: {type(e).__name__}: {e} "
            f"(service={svc_id}, qty={qty}, link={_truncate(link, 80)!r})"
        )
        if not isinstance(e, (SMMPrimeError, requests.RequestException)):
            logger.exception(f"[SMMPrime] order #{oid}")
        _send_buyer_with_template(
            cardinal, cfg, b, kind="error",
            pending_for_vars=pending2, chat_id=chat_id, chat_name=buyer,
        )


def _format_dry_run_admin(pending: dict) -> str:
    return (
        f"🟡 <b>[SMMPrime] DRY-RUN — реальный заказ НЕ создан</b>\n\n"
        f"<b>FunPay заказ:</b> <code>#{pending.get('funpay_order_id')}</code> | "
        f"<b>{_h(pending.get('buyer_username'))}</b>\n"
        f"<b>FunPay лот ID:</b> <code>{_h(pending.get('lot_id') or '—')}</code>\n"
        f"<b>Связка:</b> <b>{_h(pending.get('lot_title') or '?')}</b>\n\n"
        f"<b>Параметры запроса в SMMPrime:</b>\n"
        f"<code>POST {SMMPRIME_API_URL}</code>\n"
        f"<code>action=add</code>\n"
        f"<code>service={pending.get('service_id')}</code>\n"
        f"<code>quantity={pending.get('quantity')}</code>\n"
        f"<code>link={_h(pending.get('link'))}</code>\n\n"
        f"<i>Это тестовый режим. Реальный заказ в SMMPrime не создан, "
        f"баланс не потрачен.</i>\n\n"
        f"DRY-RUN: заказ не создан. "
        f"buyer={pending.get('buyer_username')}, "
        f"lot_id={pending.get('lot_id')}, "
        f"service_id={pending.get('service_id')}, "
        f"quantity={pending.get('quantity')}, "
        f"link={pending.get('link')}"
    )


# v3.11.1 — обёртки-supervisor'ы. Если в главной функции поднимется
# непредвиденное исключение (опечатка, сторонний баг), поток не «умрёт
# тихо»: мы залогируем traceback в Cardinal-лог, чтобы пользователь
# мог прислать его и мы быстро нашли проблему.
def _safe_call(fn, cardinal, event):
    try:
        fn(cardinal, event)
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[SMMPrime] supervisor: {fn.__name__} crashed: {e}")


def handle_new_order(cardinal: "Cardinal", event, *args) -> None:
    Thread(target=_safe_call, args=(_process_order, cardinal, event),
           daemon=True,
           name=f"SMMPrime-order-{getattr(event.order, 'id', 'x')}").start()


def handle_new_message(cardinal: "Cardinal", event, *args) -> None:
    Thread(target=_safe_call, args=(_process_buyer_message, cardinal, event),
           daemon=True,
           name="SMMPrime-msg").start()


# ─────────────────────────────────────────────────────────────────────────────
#  TG-ХЕНДЛЕРЫ
# ─────────────────────────────────────────────────────────────────────────────

# Поля, которые редактируются через SMMF:ie:<idx>:<field>
_EDIT_FIELDS = {
    "title": ("📝 Введите название связки (это же — ключ для матча в "
              "названии лота FunPay).",
              "title", str),
    "description": ("📝 Введите описание (для шаблона ручной публикации).",
                    "description", str),
    "price": ("📝 Введите цену в рублях для шаблона (число, можно с точкой).",
              "price", "price"),
    "service": ("📝 Введите service_id из списка услуг SMMPrime (целое число).",
                "service", int),
    "quantity": ("📝 Введите quantity (количество, целое > 0).",
                 "quantity", "qty"),
    "buyer_success_text": ("💬 Введите текст УСПЕХА покупателю для этой "
                           "связки.\n<i>Доступны переменные "
                           "<code>{buyer_username} {funpay_order_id} "
                           "{lot_id} {service_id} {quantity} {link} "
                           "{smm_order_id} {dry_run}</code>.</i>\n"
                           "<i>Пусто = глобальный → дефолт.</i>",
                           "buyer_success_text", str),
    "buyer_error_text": ("💬 Введите текст ОШИБКИ покупателю для этой "
                         "связки.\n<i>Доступны переменные "
                         "<code>{buyer_username} {funpay_order_id} "
                         "{lot_id} {service_id} {quantity}</code>.</i>\n"
                         "<i>Пусто = глобальный → дефолт.</i>",
                         "buyer_error_text", str),
    "buyer_dry_run_text": ("💬 Введите текст DRY-RUN покупателю для этой "
                           "связки.\n<i>Доступны переменные "
                           "<code>{buyer_username} {funpay_order_id} "
                           "{lot_id} {service_id} {quantity} {link}</code>.</i>\n"
                           "<i>Пусто = глобальный → дефолт.</i>",
                           "buyer_dry_run_text", str),
    "funpay_lot_id": ("🔗 Введите ID или URL лота на FunPay.\n"
                      "Например: <code>https://funpay.com/lots/offer?id=66017420</code>\n"
                      "или просто <code>66017420</code>.",
                      "funpay_lot_id", "lot"),
    # v3.11 — поля связки. min_quantity / max_quantity больше нет в
    # _EDIT_FIELDS: они read-only и подтягиваются только из SMMPrime
    # через action=services (кнопка «🔄 Обновить инфо услуги»).
    "link_example": ("📝 Введите пример ссылки, который бот покажет "
                     "покупателю.\nНапример: "
                     "<code>https://t.me/your_channel</code>",
                     "link_example", str),
    "buyer_ask_link_text": ("💬 Введите текст для <b>просьбы ссылки</b> "
                            "(этап 1).\n"
                            "<i>Доступны переменные: <code>{service_id} "
                            "{service_name} {link_example}</code>.</i>\n"
                            "<i>Пусто = глобальный → дефолт.</i>",
                            "buyer_ask_link_text", str),
    "buyer_ask_quantity_text": ("💬 Введите текст для <b>просьбы "
                                "количества</b> (этап 2).\n"
                                "<i>Доступны переменные: <code>{min} {max} "
                                "{service_id} {service_name}</code>.</i>\n"
                                "<i>Пусто = глобальный → дефолт.</i>",
                                "buyer_ask_quantity_text", str),
    "buyer_qty_too_small_text": ("💬 Введите текст ошибки <b>«количество "
                                 "меньше минимума»</b>.\n"
                                 "<i>Доступны: <code>{min} {max} "
                                 "{service_id} {service_name} "
                                 "{quantity}</code>.</i>\n"
                                 "<i>Пусто = глобальный → дефолт.</i>",
                                 "buyer_qty_too_small_text", str),
    "buyer_qty_too_large_text": ("💬 Введите текст ошибки <b>«количество "
                                 "больше максимума»</b>.\n"
                                 "<i>Доступны: <code>{min} {max} "
                                 "{service_id} {service_name} "
                                 "{quantity}</code>.</i>\n"
                                 "<i>Пусто = глобальный → дефолт.</i>",
                                 "buyer_qty_too_large_text", str),
    # v3.12 — два новых поля связки: CONFIRM и CANCELLED.
    "buyer_confirm_text": ("💬 Введите текст <b>ПОДТВЕРЖДЕНИЯ заказа</b>.\n"
                           "Покупатель видит сводку и отвечает «да», "
                           "«отмена», «изменить ссылку» или «изменить "
                           "количество».\n"
                           "<i>Доступны переменные: <code>{link} {quantity} "
                           "{service_id} {service_name} {min} {max}</code>."
                           "</i>\n"
                           "<i>Пусто = глобальный → дефолт.</i>",
                           "buyer_confirm_text", str),
    "buyer_cancelled_text": ("💬 Введите текст <b>ОТМЕНЫ заказа</b> "
                             "покупателем.\n"
                             "<i>Доступны: <code>{buyer_username} "
                             "{funpay_order_id}</code>.</i>\n"
                             "<i>Пусто = глобальный → дефолт.</i>",
                             "buyer_cancelled_text", str),
}


def _make_handlers(cardinal: "Cardinal"):
    bot = cardinal.telegram.bot

    def cb_open_settings(call: CallbackQuery):
        logger.info(f"[SMMPrime] cb_open_settings: data={call.data!r}")
        _screen_main(bot, call)
        try:
            bot.answer_callback_query(call.id)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[SMMPrime] answer_callback_query: {e}")

    def cb_main(call: CallbackQuery):
        _screen_main(bot, call)
        bot.answer_callback_query(call.id)

    def cb_toggle_enabled(call: CallbackQuery):
        cfg = _load()
        cfg["enabled"] = not cfg.get("enabled", True)
        _save(cfg)
        _screen_main(bot, call)
        bot.answer_callback_query(call.id,
                                  "✅ Включён" if cfg["enabled"] else "⏸ Выключен")

    def cb_list(call: CallbackQuery):
        # v3.9 — клик по «🛒 Связки» открывает 1-ю страницу.
        # Текущий поисковый фильтр админа сохраняется (если был задан
        # ранее), сбросить можно через «🔍 Сброс» в шапке списка.
        _screen_bindings(bot, call, page=0)
        bot.answer_callback_query(call.id)

    def cb_list_page(call: CallbackQuery):
        # v3.9 — листание страниц компактного списка.
        try:
            page = int(call.data.split(":", 2)[2])
        except (ValueError, IndexError):
            page = 0
        _screen_bindings(bot, call, page=page)
        bot.answer_callback_query(call.id)

    def cb_bind_search_start(call: CallbackQuery):
        # v3.9 — открыть диалог ввода поисковой подстроки.
        _screen_bind_search_prompt(bot, call)
        bot.answer_callback_query(call.id)

    def cb_bind_search_reset(call: CallbackQuery):
        # v3.9 — сбросить поисковый фильтр и вернуться к 1-й странице.
        cid = call.message.chat.id
        _BIND_SEARCH_STATE.pop(cid, None)
        _screen_bindings(bot, call, page=0)
        bot.answer_callback_query(call.id, "🔍 Фильтр сброшен")

    def cb_bind_sort_cycle(call: CallbackQuery):
        # v3.16 — циклически переключает режим сортировки списка связок.
        cur = _get_sort_mode()
        nxt = _next_sort_mode(cur)
        _set_sort_mode(nxt)
        _screen_bindings(bot, call, page=0)
        label = _BIND_SORT_LABELS.get(nxt, nxt)
        bot.answer_callback_query(call.id, f"Сортировка: {label}")

    def cb_price_view(call: CallbackQuery):
        # v3.16 — экран «Цена FunPay» для конкретной связки.
        try:
            idx = int(call.data.split(":", 2)[2])
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "Bad idx", show_alert=True)
            return
        _screen_price_view(bot, call, cardinal, idx)
        bot.answer_callback_query(call.id)

    def cb_price_edit(call: CallbackQuery):
        # v3.16 — попросить ввести новую цену.
        try:
            idx = int(call.data.split(":", 2)[2])
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "Bad idx", show_alert=True)
            return
        _screen_price_edit(bot, call, idx)
        bot.answer_callback_query(call.id)

    def cb_price_confirm(call: CallbackQuery):
        # v3.16 — подтвердили «Да» → реально save_lot на FunPay.
        try:
            idx = int(call.data.split(":", 2)[2])
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "Bad idx", show_alert=True)
            return
        cid = call.message.chat.id
        msg_id = call.message.id
        bot.answer_callback_query(call.id, "💵 Сохраняю на FunPay…")
        _do_save_funpay_price(cardinal, cid, msg_id, bot, idx)

    def cb_noop(call: CallbackQuery):
        # v3.9 — кнопка-индикатор «<page>/<total>» без действия.
        bot.answer_callback_query(call.id)

    def cb_pending(call: CallbackQuery):
        _screen_pending(bot, call)
        bot.answer_callback_query(call.id)

    # v3.8 — управление pending-заказами
    def cb_pending_del_list(call: CallbackQuery):
        _screen_pending_del_list(bot, call)
        bot.answer_callback_query(call.id)

    def cb_pending_del_ask(call: CallbackQuery):
        try:
            oid = call.data.split(":", 2)[2]
        except IndexError:
            bot.answer_callback_query(call.id, "Bad oid", show_alert=True)
            return
        _screen_pending_del_ask(bot, call, oid)
        bot.answer_callback_query(call.id)

    def cb_pending_del_ok(call: CallbackQuery):
        try:
            oid = call.data.split(":", 2)[2]
        except IndexError:
            bot.answer_callback_query(call.id, "Bad oid", show_alert=True)
            return
        removed = _pending_delete(oid)
        if removed is None:
            bot.answer_callback_query(
                call.id, "Заказ не найден (возможно, уже удалён)",
                show_alert=True)
        else:
            bot.answer_callback_query(
                call.id,
                f"🗑 Заказ #{oid} удалён из плагина "
                f"(FunPay/SMMPrime не тронуты)")
        _screen_pending(bot, call)

    def cb_pending_purge_ask(call: CallbackQuery):
        try:
            group = call.data.split(":", 2)[2]
        except IndexError:
            bot.answer_callback_query(call.id, "Bad group", show_alert=True)
            return
        _screen_pending_purge_ask(bot, call, group)
        bot.answer_callback_query(call.id)

    def cb_pending_purge_ok(call: CallbackQuery):
        try:
            group = call.data.split(":", 2)[2]
        except IndexError:
            bot.answer_callback_query(call.id, "Bad group", show_alert=True)
            return
        n = _pending_purge(group)
        label = _PURGE_LABELS.get(group, group)
        if n == 0:
            bot.answer_callback_query(
                call.id, f"В группе «{label}» нечего удалять",
                show_alert=True)
        else:
            bot.answer_callback_query(
                call.id, f"🧹 Удалено {n} «{label}» (только записи плагина)")
        _screen_pending(bot, call)

    def cb_bind_detail(call: CallbackQuery):
        try:
            idx = int(call.data.split(":")[2])
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "Bad index", show_alert=True)
            return
        _screen_binding(bot, call, idx)
        bot.answer_callback_query(call.id)

    def cb_bind_edit(call: CallbackQuery):
        try:
            parts = call.data.split(":")
            idx = int(parts[2])
            field = parts[3]
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "Bad call", show_alert=True)
            return
        if field not in _EDIT_FIELDS:
            bot.answer_callback_query(call.id, "Unknown field", show_alert=True)
            return
        prompt = _EDIT_FIELDS[field][0]
        _DIALOG[call.message.chat.id] = {
            "step": "await_bind_field",
            "msg_id": call.message.id,
            "bind_idx": idx,
            "field": field,
        }
        _edit(bot, call,
              f"✏ <b>Редактирование связки #{idx+1}</b>\n\n{prompt}\n\n"
              f"<i>/cancel — отмена</i>",
              IKM().add(IKB("❌ Отмена", callback_data=f"{_BIND_DETAIL}:{idx}")))
        bot.answer_callback_query(call.id)

    def cb_bind_template(call: CallbackQuery):
        try:
            idx = int(call.data.split(":")[2])
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "Bad index", show_alert=True)
            return
        _screen_template(bot, call, idx)
        bot.answer_callback_query(call.id)

    def cb_bind_test(call: CallbackQuery):
        try:
            idx = int(call.data.split(":")[2])
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "Bad index", show_alert=True)
            return
        cfg = _load()
        bs = _bindings(cfg)
        if not (0 <= idx < len(bs)):
            bot.answer_callback_query(call.id, "Связка не найдена", show_alert=True)
            return
        b = bs[idx]
        _DIALOG[call.message.chat.id] = {
            "step": "await_test_link",
            "msg_id": call.message.id,
            "bind_idx": idx,
        }
        _edit(bot, call,
              f"🧪 <b>Тест связки «{_h(b.get('title') or f'#{idx+1}')}»</b>\n\n"
              f"Введите тестовую ссылку (URL), которую плагин использовал бы "
              f"для заказа в SMMPrime.\n\n"
              f"Пример: <code>https://t.me/example_channel</code>\n\n"
              f"<i>/cancel — отмена</i>",
              IKM().add(IKB("❌ Отмена", callback_data=f"{_BIND_DETAIL}:{idx}")))
        bot.answer_callback_query(call.id)

    def cb_bind_test_confirm(call: CallbackQuery):
        try:
            idx = int(call.data.split(":")[2])
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "Bad index", show_alert=True)
            return
        chat_id = call.message.chat.id
        pending = _PENDING_TEST.get(chat_id)
        if not pending or pending.get("idx") != idx:
            bot.answer_callback_query(
                call.id, "Тест истёк, начните заново", show_alert=True)
            _screen_binding(bot, call, idx)
            return
        cfg = _load()
        api_key = cfg.get("api_key", "")
        bs = _bindings(cfg)
        if not (0 <= idx < len(bs)):
            bot.answer_callback_query(call.id, "Связка не найдена", show_alert=True)
            return
        b = bs[idx]
        try:
            res = SMMPrimeClient(api_key).add_order(
                int(b["service"]), pending["link"], int(b["quantity"]))
            smm_id = res.get("order", "???")
            _log_order(f"TEST funpay_order=manual binding='{b.get('title')}' "
                       f"lot={b.get('funpay_lot_id')} service={b['service']} "
                       f"qty={b['quantity']} link={pending['link']} "
                       f"smm_id={smm_id}")
            text = (
                f"✅ <b>Тестовый РЕАЛЬНЫЙ заказ создан!</b>\n\n"
                f"<b>SMM ID:</b> <code>{smm_id}</code>\n"
                f"<b>Связка:</b> {_h(b.get('title') or '?')}\n"
                f"<b>Ссылка:</b> <code>{_h(pending['link'])}</code>\n"
                f"<b>Услуга:</b> <code>{b['service']}</code> × <code>{b['quantity']}</code>\n\n"
                f"<i>Баланс SMMPrime уменьшился. Это был реальный заказ.</i>"
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[SMMPrime] test confirm error")
            text = _api_error_text(e, api_key)
        _PENDING_TEST.pop(chat_id, None)
        _edit(bot, call, text,
              IKM().add(IKB("◀ К связке", callback_data=f"{_BIND_DETAIL}:{idx}")))
        bot.answer_callback_query(call.id)

    def cb_bind_toggle_dry(call: CallbackQuery):
        try:
            idx = int(call.data.split(":")[2])
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "Bad index", show_alert=True)
            return
        cfg = _load()
        bs = _bindings(cfg)
        if not (0 <= idx < len(bs)):
            bot.answer_callback_query(call.id, "Связка не найдена", show_alert=True)
            return
        bs[idx]["dry_run"] = not bs[idx].get("dry_run", True)
        cfg["bindings"] = bs
        _save(cfg)
        _screen_binding(bot, call, idx)
        bot.answer_callback_query(
            call.id, "🟡 Dry-run ВКЛ" if bs[idx]["dry_run"] else "⚪ Dry-run ВЫКЛ")

    def cb_bind_toggle_on(call: CallbackQuery):
        try:
            idx = int(call.data.split(":")[2])
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "Bad index", show_alert=True)
            return
        cfg = _load()
        bs = _bindings(cfg)
        if not (0 <= idx < len(bs)):
            bot.answer_callback_query(call.id, "Связка не найдена", show_alert=True)
            return
        bs[idx]["enabled"] = not bs[idx].get("enabled", True)
        cfg["bindings"] = bs
        _save(cfg)
        _screen_binding(bot, call, idx)
        bot.answer_callback_query(
            call.id, "🟢 Включена" if bs[idx]["enabled"] else "🔴 Выключена")

    def cb_bind_refresh_svc(call: CallbackQuery):
        """v3.10 — обновляет инфо услуги SMMPrime по service_id связки."""
        try:
            idx = int(call.data.split(":")[2])
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "Bad index", show_alert=True)
            return
        cfg = _load()
        bs = _bindings(cfg)
        if not (0 <= idx < len(bs)):
            bot.answer_callback_query(call.id, "Связка не найдена", show_alert=True)
            return
        bot.answer_callback_query(call.id, "⏳ Запрашиваю SMMPrime…")
        ok, reason = _refresh_service_info(cfg.get("api_key", ""), bs[idx])
        cfg["bindings"] = bs
        _save(cfg)
        _screen_binding(bot, call, idx)
        if ok:
            bot.answer_callback_query(
                call.id,
                f"🔄 {bs[idx].get('service_name', '?')} | "
                f"min={bs[idx].get('min_quantity')} "
                f"max={bs[idx].get('max_quantity')}",
                show_alert=True)
        else:
            bot.answer_callback_query(
                call.id, f"❌ {reason}", show_alert=True)

    def cb_del(call: CallbackQuery):
        try:
            idx = int(call.data.split(":")[2])
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "Bad index", show_alert=True)
            return
        cfg = _load()
        bs = _bindings(cfg)
        if 0 <= idx < len(bs):
            removed = bs.pop(idx)
            cfg["bindings"] = bs
            _save(cfg)
            bot.answer_callback_query(
                call.id,
                f"✅ «{_truncate(removed.get('title') or f'#{idx+1}', 30)}» удалена"
            )
        else:
            bot.answer_callback_query(call.id, "Связка не найдена", show_alert=True)
        _screen_bindings(bot, call)

    def cb_balance(call: CallbackQuery):
        cfg = _load()
        if not cfg.get("api_key"):
            bot.answer_callback_query(call.id, "❌ API-ключ не задан!", show_alert=True)
            return
        bot.answer_callback_query(call.id, "⏳ Запрашиваю баланс...")
        _screen_balance(bot, call)

    def cb_list_services(call: CallbackQuery):
        try:
            offset = int(call.data.split(":")[2])
        except (ValueError, IndexError):
            offset = 0
        bot.answer_callback_query(call.id, "⏳ Загружаю услуги...")
        _screen_services(bot, call, offset)

    def cb_api_start(call: CallbackQuery):
        _DIALOG[call.message.chat.id] = {
            "step": "await_api_key", "msg_id": call.message.id,
        }
        _edit(bot, call,
              "🔑 <b>Введите ваш API-ключ SMMPrime:</b>\n\n"
              "Найти: smmprime.com → <b>Settings → API</b>\n\n"
              "<i>/cancel — отмена.</i>",
              IKM().add(IKB("❌ Отмена", callback_data=_MAIN)))
        bot.answer_callback_query(call.id)

    # v3.16 — единый помощник для cb_set_*: показывает текущий/дефолтный
    # текст (P3 из ТЗ) + просит ввести новый одним сообщением.
    def _open_text_editor(call: CallbackQuery, *, step: str, title: str,
                          cfg_key: str, default_text: str,
                          variables: str) -> None:
        cfg = _load()
        custom = cfg.get(cfg_key, "") or ""
        _DIALOG[call.message.chat.id] = {
            "step": step, "msg_id": call.message.id,
        }
        block = _format_current_template_block(custom, default_text)
        body = (
            f"💬 <b>{title}</b>\n\n"
            f"{block}\n\n"
            f"✏ <b>Введите новый текст одним сообщением.</b>\n"
            f"Доступные переменные: <code>{variables}</code>\n\n"
            "<i>/cancel — отмена. \"default\" / \"стандарт\" — вернуть "
            "стандартный текст.</i>"
        )
        _edit(bot, call, body,
              IKM().add(IKB("❌ Отмена", callback_data=_MAIN)))
        bot.answer_callback_query(call.id)

    def cb_set_ok(call: CallbackQuery):
        _open_text_editor(
            call,
            step="await_ok_text",
            title="Глобальный текст УСПЕХА покупателю",
            cfg_key="buyer_success_text",
            default_text=_DEFAULT_SUCCESS_TEMPLATE,
            variables=("{buyer_username} {funpay_order_id} {lot_id} "
                       "{service_id} {quantity} {link} {smm_order_id} "
                       "{dry_run}"),
        )

    def cb_set_err(call: CallbackQuery):
        _open_text_editor(
            call,
            step="await_err_text",
            title="Глобальный текст ОШИБКИ покупателю",
            cfg_key="buyer_error_text",
            default_text=_DEFAULT_ERROR_TEMPLATE,
            variables=("{buyer_username} {funpay_order_id} {lot_id} "
                       "{service_id} {quantity}"),
        )

    def cb_set_dry(call: CallbackQuery):
        _open_text_editor(
            call,
            step="await_dry_text",
            title="Глобальный текст DRY-RUN покупателю",
            cfg_key="buyer_dry_run_text",
            default_text=_DEFAULT_DRY_RUN_TEMPLATE,
            variables=("{buyer_username} {funpay_order_id} {lot_id} "
                       "{service_id} {quantity} {link}"),
        )

    # v3.11 — глобальные тексты для 4 «сценарных» сообщений.
    def cb_set_ask_link(call: CallbackQuery):
        _open_text_editor(
            call,
            step="await_ask_link_text",
            title="Глобальный текст ПРОСЬБЫ ССЫЛКИ покупателю",
            cfg_key="buyer_ask_link_text",
            default_text=_DEFAULT_ASK_LINK_TEMPLATE,
            variables="{service_id} {service_name} {link_example}",
        )

    def cb_set_ask_qty(call: CallbackQuery):
        _open_text_editor(
            call,
            step="await_ask_qty_text",
            title="Глобальный текст ПРОСЬБЫ КОЛИЧЕСТВА",
            cfg_key="buyer_ask_quantity_text",
            default_text=_DEFAULT_ASK_QUANTITY_TEMPLATE,
            variables="{min} {max} {service_id} {service_name}",
        )

    def cb_set_qty_small(call: CallbackQuery):
        _open_text_editor(
            call,
            step="await_qty_small_text",
            title="Глобальный текст «количество меньше минимума»",
            cfg_key="buyer_qty_too_small_text",
            default_text=_DEFAULT_QTY_TOO_SMALL_TEMPLATE,
            variables=("{buyer_username} {min} {max} {service_id} "
                       "{service_name} {quantity} {funpay_order_id}"),
        )

    def cb_set_qty_large(call: CallbackQuery):
        _open_text_editor(
            call,
            step="await_qty_large_text",
            title="Глобальный текст «количество больше максимума»",
            cfg_key="buyer_qty_too_large_text",
            default_text=_DEFAULT_QTY_TOO_LARGE_TEMPLATE,
            variables=("{buyer_username} {min} {max} {service_id} "
                       "{service_name} {quantity} {funpay_order_id}"),
        )

    # v3.12 — два новых глобальных текста: CONFIRM и CANCELLED.
    def cb_set_confirm(call: CallbackQuery):
        _open_text_editor(
            call,
            step="await_confirm_text",
            title="Глобальный текст ПОДТВЕРЖДЕНИЯ заказа",
            cfg_key="buyer_confirm_text",
            default_text=_DEFAULT_CONFIRM_TEMPLATE,
            variables=("{link} {quantity} {service_id} {service_name} "
                       "{min} {max}"),
        )

    def cb_set_cancelled(call: CallbackQuery):
        _open_text_editor(
            call,
            step="await_cancelled_text",
            title="Глобальный текст ОТМЕНЫ заказа покупателем",
            cfg_key="buyer_cancelled_text",
            default_text=_DEFAULT_CANCELLED_TEMPLATE,
            variables="{buyer_username} {funpay_order_id}",
        )

    # v3.15 — pre-purchase greeting (приветствие до покупки).
    # По умолчанию выключено, чтобы не наступать на Cardinal-автоответчик.
    def cb_toggle_prepur(call: CallbackQuery):
        cfg = _load()
        cfg["pre_purchase_greeting_enabled"] = not bool(
            cfg.get("pre_purchase_greeting_enabled", False)
        )
        _save(cfg)
        _screen_main(bot, call)
        new_state = ("✅ Приветствие до покупки ВКЛЮЧЕНО"
                     if cfg["pre_purchase_greeting_enabled"]
                     else "⏸ Приветствие до покупки ВЫКЛЮЧЕНО")
        bot.answer_callback_query(call.id, new_state)

    def cb_set_prepur_text(call: CallbackQuery):
        cfg = _load()
        custom = cfg.get("pre_purchase_greeting_text", "") or ""
        _DIALOG[call.message.chat.id] = {
            "step": "await_prepur_text", "msg_id": call.message.id,
        }
        block = _format_current_template_block(
            custom, _DEFAULT_PRE_PURCHASE_GREETING_TEMPLATE)
        _edit(bot, call,
              "👋 <b>Текст приветствия ДО покупки</b>\n\n"
              f"{block}\n\n"
              "Отправляется покупателю, если он пишет в чат до того, "
              "как оформил заказ. Активируется только при включенном "
              "переключателе «👋 До покупки». "
              "Rate-limit: 1 раз в 24 часа на покупателя.\n\n"
              "Доступная переменная: <code>{buyer_username}</code>\n\n"
              "✏ Отправьте новый текст одним сообщением.\n"
              "<i>/cancel — отмена. \"default\" / \"стандарт\" — вернуть "
              "стандартный текст.</i>",
              IKM().add(IKB("❌ Отмена", callback_data=_MAIN)))
        bot.answer_callback_query(call.id)

    def cb_add_start(call: CallbackQuery):
        _DIALOG[call.message.chat.id] = {
            "step": "await_new_lot",
            "msg_id": call.message.id,
            "draft": _DEFAULT_BINDING.copy(),
        }
        _edit(bot, call,
              "➕ <b>Новая связка — шаг 1/6</b>\n\n"
              "Сначала <b>опубликуйте лот на funpay.com вручную</b>, "
              "затем вернитесь сюда.\n\n"
              "Введите <b>URL или ID лота</b> на FunPay.\n"
              "Примеры:\n"
              "• <code>https://funpay.com/lots/offer?id=66017420</code>\n"
              "• <code>66017420</code>\n\n"
              "<i>/cancel — отмена</i>",
              IKM().add(IKB("❌ Отмена", callback_data=_LIST_BIND)))
        bot.answer_callback_query(call.id)

    def cb_help(call: CallbackQuery):
        _screen_help(bot, call)
        bot.answer_callback_query(call.id)

    def cb_help_flow(call: CallbackQuery):
        _screen_help_flow(bot, call)
        bot.answer_callback_query(call.id)

    def cb_help_funpay(call: CallbackQuery):
        _screen_help_funpay(bot, call)
        bot.answer_callback_query(call.id)

    # ── единый message-handler для всех текстовых диалогов ─────────────────
    def msg_step(message: Message):
        cid = message.chat.id
        state = _DIALOG.get(cid, {})
        step = state.get("step")
        text = (message.text or "").strip()

        try:
            bot.delete_message(cid, message.id)
        except Exception:
            pass

        def back_to_main():
            _safe_edit(bot, cid, state.get("msg_id"), _main_text(), _kb_main())

        if text.lower() in ("/cancel", "отмена", "cancel"):
            _DIALOG.pop(cid, None)
            back_to_main()
            return

        # ── v3.9: ПОИСК по связкам (короткий шаг) ──────────────────────────
        if step == "await_bind_search":
            _DIALOG.pop(cid, None)
            q = text.strip()
            if q:
                _BIND_SEARCH_STATE[cid] = q
            else:
                _BIND_SEARCH_STATE.pop(cid, None)
            kb = _kb_bindings(page=0, query=_BIND_SEARCH_STATE.get(cid, ""))
            cfg_now = _load()
            bs_now = _bindings(cfg_now)
            _, total_now, _, total_pages_now = _bindings_page_info(
                bs_now, _BIND_SEARCH_STATE.get(cid, ""), 0)
            if not q:
                head = "🛒 <b>Связки FunPay → SMMPrime</b>\n\n🔍 Фильтр сброшен."
            elif total_now == 0:
                head = ("🛒 <b>Связки FunPay → SMMPrime</b>\n\n"
                        f"🔍 По запросу <code>{_h(_truncate(q, 60))}</code> "
                        "ничего не найдено.")
            else:
                head = (
                    "🛒 <b>Связки FunPay → SMMPrime</b>\n\n"
                    f"🔍 Фильтр: <code>{_h(_truncate(q, 60))}</code> → "
                    f"<b>{total_now}</b> найдено\n"
                    f"Страница: <b>1 / {total_pages_now}</b>"
                )
            _safe_edit(bot, cid, state.get("msg_id"), head, kb)
            return

        # ── Глобальные настройки ────────────────────────────────────────────
        if step == "await_api_key":
            cfg = _load()
            cfg["api_key"] = text
            _save(cfg)
            _DIALOG.pop(cid, None)
            _safe_edit(bot, cid, state.get("msg_id"),
                       f"✅ <b>API-ключ сохранён.</b>\n"
                       f"🔑 <code>{_mask(text)}</code>\n\n" + _main_text(),
                       _kb_main())
            return

        if step in ("await_ok_text", "await_err_text", "await_dry_text",
                    "await_ask_link_text", "await_ask_qty_text",
                    "await_qty_small_text", "await_qty_large_text",
                    # v3.12 — CONFIRM / CANCELLED глобальные тексты.
                    "await_confirm_text", "await_cancelled_text",
                    # v3.15 — pre-purchase greeting текст.
                    "await_prepur_text"):
            keymap = {
                "await_ok_text": "buyer_success_text",
                "await_err_text": "buyer_error_text",
                "await_dry_text": "buyer_dry_run_text",
                "await_ask_link_text": "buyer_ask_link_text",
                "await_ask_qty_text": "buyer_ask_quantity_text",
                "await_qty_small_text": "buyer_qty_too_small_text",
                "await_qty_large_text": "buyer_qty_too_large_text",
                "await_confirm_text": "buyer_confirm_text",
                "await_cancelled_text": "buyer_cancelled_text",
                # v3.15 — pre-purchase greeting текст.
                "await_prepur_text": "pre_purchase_greeting_text",
            }
            stripped = (text or "").strip()
            # v3.16 — спецслова «default»/«стандарт»/«сброс» возвращают
            # стандартный текст (очищают кастомное значение в cfg).
            if stripped.lower() in ("default", "стандарт", "стандартный",
                                     "сброс", "reset"):
                cfg = _load()
                cfg[keymap[step]] = ""
                _save(cfg)
                _DIALOG.pop(cid, None)
                _safe_edit(
                    bot, cid, state.get("msg_id"),
                    "✅ Стандартный текст восстановлен.\n\n" + _main_text(),
                    _kb_main())
                return
            # v3.13 — валидация: запрещаем сохранять тех-команды как
            # buyer-шаблон. Это и есть источник «menu»-бага: админ
            # случайно тыкал /menu в TG-боте находясь в диалоге
            # await_dry_text, и literal "menu" сохранялся как
            # buyer_dry_run_text. Теперь такие сохранения отклоняются.
            if (not stripped) or stripped.lower() in _TECH_BLOCKLIST:
                _safe_edit(
                    bot, cid, state.get("msg_id"),
                    f"❌ Текст <code>{_h(stripped[:80])}</code> похож на "
                    f"тех. команду и НЕ был сохранён как шаблон. "
                    f"Введите нормальный текст для покупателя или "
                    f"/cancel.",
                    IKM().add(IKB("❌ Отмена",
                                  callback_data=f"{_CB}:menu")),
                )
                return
            cfg = _load()
            cfg[keymap[step]] = text
            _save(cfg)
            _DIALOG.pop(cid, None)
            back_to_main()
            return

        # ── v3.16: ввод НОВОЙ ЦЕНЫ для FunPay ─────────────────────────────
        if step == "await_funpay_price":
            idx = state.get("bind_idx")
            lot_id = str(state.get("lot_id") or "")
            new_price, err = _funpay_price_parse(text)
            if err is not None:
                # пишем ошибку в исходное сообщение, оставляя state
                _safe_edit(
                    bot, cid, state.get("msg_id"),
                    f"❌ <b>Не удалось распознать цену:</b> {_h(err)}\n\n"
                    "Пришлите цену ещё раз одним сообщением "
                    "(например: <code>150</code>, <code>150.50</code>).\n\n"
                    "<i>/cancel — отмена.</i>",
                    IKM().add(IKB("❌ Отмена",
                                  callback_data=f"{_BIND_PRICE_VIEW}:{idx}")),
                )
                return
            cfg2 = _load()
            bs2 = _bindings(cfg2)
            if not (idx is not None and 0 <= idx < len(bs2)):
                _DIALOG.pop(cid, None)
                back_to_main()
                return
            old_known = bs2[idx].get("price")
            try:
                old_known_f: float | None = (
                    float(old_known) if old_known not in (None, "") else None)
            except (TypeError, ValueError):
                old_known_f = None
            # сохраняем pending_price в state — confirm-callback заберёт
            state["pending_price"] = float(new_price)
            _DIALOG[cid] = state
            _screen_price_confirm(
                bot, cid, state.get("msg_id"), idx,
                float(new_price), lot_id, old_known_f,
            )
            return

        # ── Редактирование любого поля связки ────────────────────────────
        if step == "await_bind_field":
            idx = state.get("bind_idx")
            field = state.get("field")
            if field not in _EDIT_FIELDS:
                _DIALOG.pop(cid, None)
                back_to_main()
                return
            cfg = _load()
            bs = _bindings(cfg)
            if not (idx is not None and 0 <= idx < len(bs)):
                _DIALOG.pop(cid, None)
                back_to_main()
                return

            _prompt, key, typ = _EDIT_FIELDS[field]
            value: object
            if typ is int:
                if not text.isdigit():
                    _safe_edit(bot, cid, state.get("msg_id"),
                               "❌ Нужно целое число.\n<i>/cancel — отмена</i>",
                               IKM().add(IKB("❌ Отмена",
                                             callback_data=f"{_BIND_DETAIL}:{idx}")))
                    return
                value = int(text)
            elif typ == "qty":
                if not text.isdigit() or int(text) <= 0:
                    _safe_edit(bot, cid, state.get("msg_id"),
                               "❌ Нужно положительное целое.\n<i>/cancel — отмена</i>",
                               IKM().add(IKB("❌ Отмена",
                                             callback_data=f"{_BIND_DETAIL}:{idx}")))
                    return
                value = int(text)
            elif typ == "price":
                try:
                    value = float(text.replace(",", "."))
                except ValueError:
                    _safe_edit(bot, cid, state.get("msg_id"),
                               "❌ Нужно число (49 или 49.99).\n<i>/cancel — отмена</i>",
                               IKM().add(IKB("❌ Отмена",
                                             callback_data=f"{_BIND_DETAIL}:{idx}")))
                    return
            elif typ == "lot":
                value = _parse_lot_id(text)
            elif typ == "minmax":
                # v3.10 — min/max quantity (целое ≥ 0). 0 = не проверять.
                if not text.isdigit() or int(text) < 0:
                    _safe_edit(bot, cid, state.get("msg_id"),
                               "❌ Нужно целое ≥ 0.\n<i>/cancel — отмена</i>",
                               IKM().add(IKB("❌ Отмена",
                                             callback_data=f"{_BIND_DETAIL}:{idx}")))
                    return
                value = int(text)
            else:
                # v3.13 — для buyer_*_text полей запрещаем сохранять
                # тех-команды как шаблон (источник «menu»-бага).
                if str(key).startswith("buyer_") and str(key).endswith(
                        "_text"):
                    stripped = (text or "").strip()
                    if stripped.lower() in _TECH_BLOCKLIST:
                        _safe_edit(
                            bot, cid, state.get("msg_id"),
                            f"❌ Текст <code>{_h(stripped[:80])}</code> "
                            f"похож на тех. команду и НЕ был сохранён "
                            f"как шаблон. Введите нормальный текст или "
                            f"/cancel.",
                            IKM().add(IKB(
                                "❌ Отмена",
                                callback_data=f"{_BIND_DETAIL}:{idx}")),
                        )
                        return
                value = text

            bs[idx][key] = value
            # v3.10: при смене service_id — сразу подтягиваем инфо услуги.
            extra_info = ""
            if key == "service":
                ok, reason = _refresh_service_info(
                    cfg.get("api_key", ""), bs[idx])
                extra_info = ("\n🔄 Инфо услуги SMMPrime обновлена."
                              if ok else f"\n⚠ Не удалось обновить "
                              f"инфо услуги: {_h(reason)}")
            cfg["bindings"] = bs
            _save(cfg)
            _DIALOG.pop(cid, None)
            _safe_edit(bot, cid, state.get("msg_id"),
                       f"✅ Сохранено.{extra_info}", _kb_binding(idx, bs[idx]))
            return

        # ── 7-шаговый wizard добавления связки ───────────────────────────
        draft = state.get("draft", _DEFAULT_BINDING.copy())

        if step == "await_new_lot":
            lot = _parse_lot_id(text)
            if not lot:
                _safe_edit(bot, cid, state.get("msg_id"),
                           "❌ Не понял ID. Введите URL или число.\n<i>/cancel — отмена</i>",
                           IKM().add(IKB("❌ Отмена", callback_data=_LIST_BIND)))
                return
            draft["funpay_lot_id"] = lot
            _DIALOG[cid].update({"draft": draft, "step": "await_new_title"})
            _safe_edit(bot, cid, state.get("msg_id"),
                       f"➕ <b>Новая связка — шаг 2/6</b>\n\n"
                       f"FunPay лот: <code>{_h(lot)}</code>\n\n"
                       f"Введите <b>название</b> для отображения в боте.\n"
                       f"Это же будет ключом для матча с названием лота "
                       f"FunPay (по подстроке без учёта регистра).\n\n"
                       f"Например: <code>переходы telegram</code>.\n\n"
                       f"<i>/cancel — отмена</i>",
                       IKM().add(IKB("❌ Отмена", callback_data=_LIST_BIND)))
            return

        if step == "await_new_title":
            if not text:
                return
            draft["title"] = text
            _DIALOG[cid].update({"draft": draft, "step": "await_new_service"})
            _safe_edit(bot, cid, state.get("msg_id"),
                       f"➕ <b>Новая связка — шаг 3/6</b>\n\n"
                       f"Название: <b>{_h(text)}</b>\n\n"
                       f"Введите <b>service_id</b> SMMPrime (целое число, "
                       f"посмотрите в 📃 Список услуг).\n\n"
                       f"<i>/cancel — отмена</i>",
                       IKM().add(IKB("❌ Отмена", callback_data=_LIST_BIND)))
            return

        if step == "await_new_service":
            if not text.isdigit() or int(text) <= 0:
                _safe_edit(bot, cid, state.get("msg_id"),
                           "❌ Нужно целое > 0.\n<i>/cancel — отмена</i>",
                           IKM().add(IKB("❌ Отмена", callback_data=_LIST_BIND)))
                return
            draft["service"] = int(text)
            # v3.14: quantity больше не спрашиваем — оно ВСЕГДА берётся
            # из заказа FunPay. Сразу переходим к тексту УСПЕХА.
            draft["quantity"] = 0
            _DIALOG[cid].update({"draft": draft, "step": "await_new_ok"})
            _safe_edit(bot, cid, state.get("msg_id"),
                       f"➕ <b>Новая связка — шаг 4/6</b>\n\n"
                       f"service_id: <code>{text}</code>\n\n"
                       f"<i>quantity больше не задаётся в связке — он "
                       f"автоматически берётся из заказа FunPay.</i>\n\n"
                       f"Введите <b>текст УСПЕХА</b> покупателю.\n"
                       f"Доступны переменные: <code>{{buyer_username}} "
                       f"{{funpay_order_id}} {{lot_id}} {{service_id}} "
                       f"{{quantity}} {{link}} {{smm_order_id}} "
                       f"{{dry_run}}</code>\n\n"
                       f"Отправьте <code>-</code> чтобы использовать "
                       f"дефолтный шаблон.\n\n"
                       f"<i>/cancel — отмена</i>",
                       IKM().add(IKB("❌ Отмена", callback_data=_LIST_BIND)))
            return

        if step == "await_new_ok":
            draft["buyer_success_text"] = "" if text == "-" else text
            _DIALOG[cid].update({"draft": draft, "step": "await_new_err"})
            _safe_edit(bot, cid, state.get("msg_id"),
                       f"➕ <b>Новая связка — шаг 5/6</b>\n\n"
                       f"Введите <b>текст ОШИБКИ</b> покупателю.\n"
                       f"Доступны переменные: <code>{{buyer_username}} "
                       f"{{funpay_order_id}} {{lot_id}} {{service_id}} "
                       f"{{quantity}}</code>\n\n"
                       f"Отправьте <code>-</code> чтобы использовать "
                       f"дефолтный шаблон.\n\n"
                       f"<i>/cancel — отмена</i>",
                       IKM().add(IKB("❌ Отмена", callback_data=_LIST_BIND)))
            return

        if step == "await_new_err":
            draft["buyer_error_text"] = "" if text == "-" else text
            _DIALOG[cid].update({"draft": draft, "step": "await_new_dry"})
            kb = IKM()
            kb.add(IKB("🟡 Dry-run ВКЛ (рекомендуется на старте)",
                       callback_data=_CB + ":dry:1"))
            kb.add(IKB("⚪ Dry-run ВЫКЛ (боевой режим — реальные заказы)",
                       callback_data=_CB + ":dry:0"))
            kb.add(IKB("❌ Отмена", callback_data=_LIST_BIND))
            _safe_edit(bot, cid, state.get("msg_id"),
                       f"➕ <b>Новая связка — шаг 6/6</b>\n\n"
                       f"<b>Включить dry-run?</b>\n\n"
                       f"Dry-run — тестовый режим: при покупке плагин "
                       f"показывает параметры, но <b>не</b> создаёт реальный "
                       f"заказ в SMMPrime и <b>не</b> тратит баланс.",
                       kb)
            return

        # ── Тест связки: ввод тестовой ссылки ────────────────────────────
        if step == "await_test_link":
            idx = state.get("bind_idx")
            cfg = _load()
            bs = _bindings(cfg)
            if not (idx is not None and 0 <= idx < len(bs)):
                _DIALOG.pop(cid, None)
                back_to_main()
                return
            link = _extract_link(text) or text
            if not (link.startswith("http://") or link.startswith("https://")):
                _safe_edit(bot, cid, state.get("msg_id"),
                           "❌ Нужен URL (http:// или https://).\n<i>/cancel — отмена</i>",
                           IKM().add(IKB("❌ Отмена",
                                         callback_data=f"{_BIND_DETAIL}:{idx}")))
                return
            b = bs[idx]
            svc = b.get("service")
            qty = b.get("quantity")
            try:
                svc_int = int(svc); qty_int = int(qty)
                if svc_int <= 0 or qty_int <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                _DIALOG.pop(cid, None)
                _safe_edit(bot, cid, state.get("msg_id"),
                           "❌ Сначала задайте корректные service_id и quantity.",
                           IKM().add(IKB("◀ К связке",
                                         callback_data=f"{_BIND_DETAIL}:{idx}")))
                return

            params_text = (
                f"🧪 <b>Тест связки «{_h(b.get('title') or f'#{idx+1}')}»</b>\n\n"
                f"<b>Параметры запроса:</b>\n"
                f"<code>POST {SMMPRIME_API_URL}</code>\n"
                f"<code>action=add</code>\n"
                f"<code>service={svc_int}</code>\n"
                f"<code>quantity={qty_int}</code>\n"
                f"<code>link={_h(link)}</code>\n"
            )

            if b.get("dry_run", True):
                params_text += (
                    "\n<b>Связка в режиме dry-run (🟡).</b>\n"
                    "<i>Реальный заказ в SMMPrime НЕ создан, баланс не "
                    "потрачен.</i>"
                )
                _DIALOG.pop(cid, None)
                _safe_edit(bot, cid, state.get("msg_id"), params_text,
                           IKM().add(IKB("◀ К связке",
                                         callback_data=f"{_BIND_DETAIL}:{idx}")))
            else:
                params_text += (
                    "\n<b>⚠ Связка в боевом режиме (⚪).</b>\n"
                    "Подтвердите создание <b>реального</b> заказа в "
                    "SMMPrime — это <b>потратит баланс</b>."
                )
                _PENDING_TEST[cid] = {"idx": idx, "link": link}
                _DIALOG.pop(cid, None)
                kb = IKM()
                kb.add(IKB("✅ ДА, создать реальный заказ",
                           callback_data=f"{_BIND_TEST_CONFIRM}:{idx}"))
                kb.add(IKB("◀ Отмена", callback_data=f"{_BIND_DETAIL}:{idx}"))
                _safe_edit(bot, cid, state.get("msg_id"), params_text, kb)
            return

        # неизвестный шаг → main
        _DIALOG.pop(cid, None)
        back_to_main()

    # ── Доп. callback'и для wizard'а: dry_run ───────────────────────────────
    def cb_wizard_dry(call: CallbackQuery):
        # SMMF:dry:0|1
        cid = call.message.chat.id
        state = _DIALOG.get(cid)
        if not state or state.get("step") != "await_new_dry":
            bot.answer_callback_query(call.id, "Сессия истекла", show_alert=True)
            return
        try:
            v = call.data.split(":", 2)[2]
        except IndexError:
            bot.answer_callback_query(call.id, "Bad call", show_alert=True)
            return
        draft = state.get("draft", _DEFAULT_BINDING.copy())
        draft["dry_run"] = (v == "1")
        # сохраняем
        cfg = _load()
        cfg["bindings"].append(draft)
        _save(cfg)
        new_idx = len(cfg["bindings"]) - 1
        _DIALOG.pop(cid, None)
        _edit(bot, call,
              f"✅ <b>Связка добавлена!</b>\n\n"
              f"FunPay лот: <code>{_h(draft.get('funpay_lot_id') or '—')}</code>\n"
              f"Название: <b>{_h(draft.get('title') or '—')}</b>\n"
              f"service_id: <code>{draft.get('service')}</code>\n"
              f"quantity: <code>{draft.get('quantity')}</code>\n"
              f"Dry-run: {'🟡 ВКЛ' if draft.get('dry_run') else '⚪ ВЫКЛ'}\n\n"
              f"<i>📋 Flow: покупка → бот просит ссылку → покупатель "
              f"отправляет → плагин делает заказ в SMMPrime.</i>\n\n"
              f"<i>Не забудьте: бот не редактирует лот FunPay. Если нужно "
              f"изменить название/описание/цену — делайте это на "
              f"funpay.com напрямую.</i>",
              IKM().add(
                  IKB("🔹 Открыть карточку",
                      callback_data=f"{_BIND_DETAIL}:{new_idx}"),
              ).add(IKB("📋 К списку связок", callback_data=_LIST_BIND)))
        bot.answer_callback_query(call.id, "Связка создана")

    return [
        # ★ ВАЖНО: cb_open_settings первый.
        ("cb_open_settings", cb_open_settings,
         lambda c: bool(c.data) and c.data.startswith(_SETTINGS_PREFIX)),
        ("cb_main", cb_main, lambda c: c.data == _MAIN),
        ("cb_toggle_enabled", cb_toggle_enabled, lambda c: c.data == _TOGGLE_ENABLED),
        ("cb_list", cb_list, lambda c: c.data == _LIST_BIND),
        # v3.9 — пагинация / поиск компактного списка связок
        ("cb_list_page", cb_list_page,
         lambda c: bool(c.data) and c.data.startswith(f"{_LIST_BIND_PAGE}:")),
        ("cb_bind_search_start", cb_bind_search_start,
         lambda c: c.data == _BIND_SEARCH),
        ("cb_bind_search_reset", cb_bind_search_reset,
         lambda c: c.data == _BIND_SEARCH_RESET),
        # v3.16 — циклическая сортировка списка связок.
        ("cb_bind_sort_cycle", cb_bind_sort_cycle,
         lambda c: c.data == _BIND_SORT_CYCLE),
        # v3.16 — управление ценой лота FunPay (view / edit / confirm).
        ("cb_price_view", cb_price_view,
         lambda c: bool(c.data) and c.data.startswith(f"{_BIND_PRICE_VIEW}:")),
        ("cb_price_edit", cb_price_edit,
         lambda c: bool(c.data) and c.data.startswith(f"{_BIND_PRICE_EDIT}:")),
        ("cb_price_confirm", cb_price_confirm,
         lambda c: bool(c.data) and c.data.startswith(f"{_BIND_PRICE_CONFIRM}:")),
        ("cb_noop", cb_noop, lambda c: c.data == _NOOP),
        ("cb_pending", cb_pending, lambda c: c.data == _PENDING_LIST),
        ("cb_pending_del_list", cb_pending_del_list,
         lambda c: c.data == _PENDING_DEL_LIST),
        ("cb_pending_del_ask", cb_pending_del_ask,
         lambda c: bool(c.data) and c.data.startswith(f"{_PENDING_DEL_ASK}:")),
        ("cb_pending_del_ok", cb_pending_del_ok,
         lambda c: bool(c.data) and c.data.startswith(f"{_PENDING_DEL_OK}:")),
        ("cb_pending_purge_ask", cb_pending_purge_ask,
         lambda c: bool(c.data) and c.data.startswith(f"{_PENDING_PURGE_ASK}:")),
        ("cb_pending_purge_ok", cb_pending_purge_ok,
         lambda c: bool(c.data) and c.data.startswith(f"{_PENDING_PURGE_OK}:")),
        ("cb_bind_detail", cb_bind_detail,
         lambda c: bool(c.data) and c.data.startswith(f"{_BIND_DETAIL}:")),
        ("cb_bind_edit", cb_bind_edit,
         lambda c: bool(c.data) and c.data.startswith(f"{_BIND_EDIT}:")),
        ("cb_bind_template", cb_bind_template,
         lambda c: bool(c.data) and c.data.startswith(f"{_BIND_TEMPLATE}:")),
        ("cb_bind_test", cb_bind_test,
         lambda c: bool(c.data) and c.data.startswith(f"{_BIND_TEST}:")),
        ("cb_bind_test_confirm", cb_bind_test_confirm,
         lambda c: bool(c.data) and c.data.startswith(f"{_BIND_TEST_CONFIRM}:")),
        ("cb_bind_toggle_dry", cb_bind_toggle_dry,
         lambda c: bool(c.data) and c.data.startswith(f"{_BIND_TOGGLE_DRY}:")),
        ("cb_bind_toggle_on", cb_bind_toggle_on,
         lambda c: bool(c.data) and c.data.startswith(f"{_BIND_TOGGLE_ON}:")),
        ("cb_bind_refresh_svc", cb_bind_refresh_svc,
         lambda c: bool(c.data) and c.data.startswith(f"{_BIND_REFRESH_SVC}:")),
        ("cb_del", cb_del,
         lambda c: bool(c.data) and c.data.startswith(f"{_DEL_BIND}:")),
        ("cb_balance", cb_balance, lambda c: c.data == _CHECK_BAL),
        ("cb_list_services", cb_list_services,
         lambda c: bool(c.data) and c.data.startswith(f"{_LIST_SERVICES}:")),
        ("cb_api_start", cb_api_start, lambda c: c.data == _SET_API),
        ("cb_set_ok", cb_set_ok, lambda c: c.data == _SET_OK_TEXT),
        ("cb_set_err", cb_set_err, lambda c: c.data == _SET_ERR_TEXT),
        ("cb_set_dry", cb_set_dry, lambda c: c.data == _SET_DRY_TEXT),
        # v3.11 — 4 новых глобальных текста.
        ("cb_set_ask_link", cb_set_ask_link,
         lambda c: c.data == _SET_ASK_LINK_TEXT),
        ("cb_set_ask_qty", cb_set_ask_qty,
         lambda c: c.data == _SET_ASK_QTY_TEXT),
        ("cb_set_qty_small", cb_set_qty_small,
         lambda c: c.data == _SET_QTY_SMALL_TEXT),
        ("cb_set_qty_large", cb_set_qty_large,
         lambda c: c.data == _SET_QTY_LARGE_TEXT),
        # v3.12 — CONFIRM / CANCELLED глобальные тексты.
        ("cb_set_confirm", cb_set_confirm,
         lambda c: c.data == _SET_CONFIRM_TEXT),
        ("cb_set_cancelled", cb_set_cancelled,
         lambda c: c.data == _SET_CANCELLED_TEXT),
        # v3.15 — pre-purchase greeting toggle / text.
        ("cb_toggle_prepur", cb_toggle_prepur,
         lambda c: c.data == _TOGGLE_PREPUR),
        ("cb_set_prepur_text", cb_set_prepur_text,
         lambda c: c.data == _SET_PREPUR_TEXT),
        ("cb_add_start", cb_add_start, lambda c: c.data == _ADD_BIND),
        ("cb_help", cb_help, lambda c: c.data == _HELP),
        ("cb_help_flow", cb_help_flow, lambda c: c.data == _HELP_FLOW),
        ("cb_help_funpay", cb_help_funpay, lambda c: c.data == _HELP_FUNPAY),
        ("cb_wizard_dry", cb_wizard_dry,
         lambda c: bool(c.data) and c.data.startswith(_CB + ":dry:")),
    ], [
        ("msg_step", msg_step,
         {"func": lambda m: m.chat.id in _DIALOG, "content_types": ["text"]}),
    ]


# ─────────────────────────────────────────────────────────────────────────────
#  РЕГИСТРАЦИЯ TG-ОБРАБОТЧИКОВ
# ─────────────────────────────────────────────────────────────────────────────

def _register_tg(cardinal: "Cardinal") -> None:
    global _HANDLERS_REGISTERED, _OWN_CB_HANDLERS, _OWN_MSG_HANDLERS

    if _HANDLERS_REGISTERED:
        logger.info("[SMMPrime] Обработчики уже зарегистрированы, пропуск.")
        return

    tg = cardinal.telegram
    bot = tg.bot

    cb_before = len(bot.callback_query_handlers)
    msg_before = len(bot.message_handlers)
    logger.info(f"[SMMPrime] До регистрации: cb={cb_before}, msg={msg_before}")

    cb_handlers, msg_handlers = _make_handlers(cardinal)

    cb_ok, cb_fail = 0, 0
    for name, fn, filt in cb_handlers:
        try:
            tg.cbq_handler(fn, filt)
            cb_ok += 1
        except Exception as e:
            cb_fail += 1
            logger.error(f"[SMMPrime] Ошибка регистрации {name}: {e}")
            logger.debug("TRACEBACK", exc_info=True)

    msg_ok, msg_fail = 0, 0
    for name, fn, kwargs in msg_handlers:
        try:
            tg.msg_handler(fn, **kwargs)
            msg_ok += 1
        except Exception as e:
            msg_fail += 1
            logger.error(f"[SMMPrime] Ошибка регистрации {name}: {e}")
            logger.debug("TRACEBACK", exc_info=True)

    logger.info(f"[SMMPrime] Зарегистрировано: cb={cb_ok}/{len(cb_handlers)}, "
                f"msg={msg_ok}/{len(msg_handlers)} (failures: cb={cb_fail}, msg={msg_fail})")

    new_cb = bot.callback_query_handlers[cb_before:]
    new_msg = bot.message_handlers[msg_before:]
    if new_cb:
        bot.callback_query_handlers = new_cb + bot.callback_query_handlers[:cb_before]
        _OWN_CB_HANDLERS = new_cb
    if new_msg:
        bot.message_handlers = new_msg + bot.message_handlers[:msg_before]
        _OWN_MSG_HANDLERS = new_msg

    logger.info(f"[SMMPrime] После front-insert: первый cb-handler — {_first_handler_repr(bot)}")
    _HANDLERS_REGISTERED = True


def _first_handler_repr(bot) -> str:
    try:
        h = bot.callback_query_handlers[0]
        fn = h.get("function")
        return f"{getattr(fn, '__module__', '?')}.{getattr(fn, '__name__', '?')}"
    except Exception:
        return "?"


# ─────────────────────────────────────────────────────────────────────────────
#  PRE_INIT
# ─────────────────────────────────────────────────────────────────────────────

def _pre_init_handler(cardinal: "Cardinal", *args) -> None:
    logger.info("[SMMPrime] ====== PRE_INIT START ======")
    logger.info(f"[SMMPrime] v{VERSION} UUID={UUID}")
    logger.info(f"[SMMPrime] CBT источник: {_CBT_SOURCE}, PLUGIN_SETTINGS={CBT.PLUGIN_SETTINGS!r}")
    logger.info(f"[SMMPrime] _SETTINGS_PREFIX={_SETTINGS_PREFIX!r}")
    logger.info(f"[SMMPrime] cwd={os.getcwd()}")
    logger.info(f"[SMMPrime] file={__file__}")

    if cardinal.telegram is None:
        logger.warning("[SMMPrime] cardinal.telegram is None — TG отключён.")
        return

    try:
        _register_tg(cardinal)
    except Exception as e:
        logger.error(f"[SMMPrime] КРИТИЧЕСКАЯ ОШИБКА регистрации TG: {e}")
        logger.exception("[SMMPrime] TRACEBACK")

    # v3.14 — однократная миграция legacy-связок (quantity → 0,
    # обновление min/max через SMMPrime services). Запускаем в потоке,
    # чтобы не блокировать старт Cardinal'a сетевыми запросами.
    def _run_migration():
        try:
            _migrate_legacy_bindings_v314(cardinal)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[SMMPrime.MIG] миграция упала: {e}")

    try:
        Thread(
            target=_run_migration,
            daemon=True,
            name="SMMPrime-migrate-v1",
        ).start()
    except Exception as e:  # noqa: BLE001
        logger.error(f"[SMMPrime] не удалось запустить миграцию v3.14: {e}")

    logger.info("[SMMPrime] ====== PRE_INIT END ======")


# ─────────────────────────────────────────────────────────────────────────────
#  ПРИВЯЗКА
# ─────────────────────────────────────────────────────────────────────────────
BIND_TO_PRE_INIT = [_pre_init_handler]
BIND_TO_NEW_ORDER = [handle_new_order]
BIND_TO_NEW_MESSAGE = [handle_new_message]
# old_mode FunPayCardinal'a (oldMsgGetMode=1) шлёт сообщения в
# LAST_CHAT_MESSAGE_CHANGED, а не в NEW_MESSAGE. Регистрируемся на оба
# события — внутри handle_new_message определим режим автоматически.
BIND_TO_LAST_CHAT_MESSAGE_CHANGED = [handle_new_message]

logger.info(f"[SMMPrime] Module loaded. v{VERSION} UUID={UUID} file={__file__}")

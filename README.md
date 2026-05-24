# GGSEL Cardinal

Полноценный аналог [FunPayCardinal](https://github.com/sidor0912/FunPayCardinal),
но для маркетплейса **[GGSEL](https://ggsel.net)**: автоматическая обработка
заказов, Telegram-бот для управления, система плагинов с hot-reload, JSON-сторадж,
запуск одной кнопкой через `start.bat` / `run.sh` / Docker.

В комплекте идёт плагин **SMMPrime Auto-Order** — порт `smmprime_plugin_v1_0_2.py`
под GGSEL: связки GGSEL → SMMPrime, ask-link диалог, подтверждение, DRY-RUN,
pending-заказы переживают рестарт.

---

## 🚀 Быстрый запуск (1 клик)

### Windows

1. Установите Python 3.11+ ([python.org](https://python.org)).
2. Дважды кликните по **`start.bat`**.
3. При первом запуске откроется `notepad .env` — заполните токены и сохраните.
4. Запустите `start.bat` ещё раз — бот стартует и сам поставит зависимости.

### Linux / macOS

```bash
chmod +x run.sh
./run.sh
```

При первом запуске создастся `.env` — заполните его и запустите снова.

### Docker

```bash
cp .env.example .env
# отредактируйте .env
docker compose up -d --build
docker compose logs -f
```

---

## ⚙ Настройка `.env`

| Переменная | Описание |
|------------|----------|
| `TELEGRAM_BOT_TOKEN` | Токен Telegram-бота от [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_ADMIN_IDS` | Ваш Telegram ID (узнать у [@userinfobot](https://t.me/userinfobot)). Несколько — через запятую |
| `GGSEL_SELLER_ID` | ID продавца GGSEL |
| `GGSEL_API_TOKEN` | API-токен GGSEL (если есть) |
| `GGSEL_LOGIN` | Логин в личном кабинете GGSEL (если нет токена) |
| `GGSEL_PASSWORD` | Пароль в личном кабинете GGSEL (если нет токена) |
| `GGSEL_POLL_INTERVAL` | Интервал опроса GGSEL в секундах (по умолчанию 10) |
| `LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `AUTO_RESTART` | `1` — автоматический рестарт при падении |
| `PLUGIN_HOT_RELOAD` | `1` — авто-перезагрузка плагинов при изменении файла |

---

## 📂 Структура проекта

```
.
├── main.py                  # точка входа
├── cardinal.py              # ядро (Cardinal)
├── storage.py               # JSON-сторадж с атомарной записью
├── plugin_system.py         # загрузчик плагинов + hot reload
├── ggsel_api/               # HTTP-клиент GGSEL и раннер событий
│   ├── account.py           # GGSELAccount (login, get_orders, send_message, ...)
│   ├── events.py            # типы событий (NEW_ORDER, NEW_MESSAGE, ...)
│   ├── runner.py            # цикл опроса GGSEL → события
│   └── types.py             # Order / Message / ChatShortcut / LotFields
├── tg_bot/                  # Telegram-бот
│   ├── bot.py
│   └── cbt.py               # callback-константы (совместимы с FunPay)
├── plugins/                 # сюда складываются .py-плагины
│   └── smmprime_ggsel.py    # порт SMMPrime под GGSEL
├── storage/                 # JSON-конфиги + pending-заказы плагинов
├── configs/                 # пользовательские конфиги
├── logs/                    # ротируемые логи
├── start.bat                # запуск на Windows
├── run.sh                   # запуск на Linux/macOS
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

---

## 🤖 Telegram-бот: что умеет

Откройте `/start` боту — увидите главное меню:

* **🧩 Плагины** — список плагинов, у каждого: включить/выключить, перезагрузить,
  открыть страницу настроек.
* **📦 Статус** — сколько плагинов загружено / включено, состояние GGSEL-раннера.
* **📃 Заказы** — список текущих заказов GGSEL (через клиент аккаунта).
* **💬 Сообщения** — сводка чатов.

Каждый плагин может зарегистрировать свои inline-меню — например, у
**SMMPrime Auto-Order** есть:

* список связок (поиск, сортировка, постраничный список),
* добавление / редактирование / удаление связок,
* кнопка «💵 Цена GGSEL» — меняет цену лота прямо из Telegram,
* «💰 Баланс» SMMPrime, «📃 Список услуг»,
* редакторы шаблонов сообщений покупателю,
* список pending-заказов с кнопками очистки.

Hot-reload включён по умолчанию: правьте `plugins/*.py` — изменения подтянутся
сами без перезапуска.

---

## 🧩 Как написать свой плагин

Положите файл `plugins/myplugin.py`:

```python
NAME = "Мой плагин"
VERSION = "1.0.0"
DESCRIPTION = "Что-то делает."
UUID = "12345678-1234-1234-1234-123456789012"
SETTINGS_PAGE = True

def handle_order(cardinal, event):
    order = event.order
    print(f"Новый заказ: {order.id} - {order.title}")
    cardinal.account.send_message(order.chat_id, "Спасибо за заказ!")

BIND_TO_NEW_ORDER = [handle_order]
```

Доступные точки привязки:

| Атрибут | Когда срабатывает |
|---------|-------------------|
| `BIND_TO_PRE_INIT` | После загрузки плагина, до старта раннера |
| `BIND_TO_POST_INIT` | После старта раннера |
| `BIND_TO_NEW_ORDER` | Появился новый заказ |
| `BIND_TO_ORDER_STATUS_CHANGED` | У заказа сменился статус |
| `BIND_TO_NEW_MESSAGE` | Новое сообщение в чате |
| `BIND_TO_LAST_CHAT_MESSAGE_CHANGED` | Изменилось последнее сообщение чата |
| `BIND_TO_DELETE` | Плагин выгружается |
| `BIND_TO_SHUTDOWN` | Cardinal останавливается |

Каждый обработчик получает `(cardinal, event)`. Доступ к API GGSEL:
`cardinal.account` (см. `ggsel_api/account.py`). Доступ к Telegram-боту:
`cardinal.telegram.bot` (это объект `telebot.TeleBot`), и удобные хелперы
`cardinal.telegram.add_admins_callback(...)`, `add_admins_handler(...)`.

JSON-сторадж: `cardinal.storage.load("my.json")`, `cardinal.storage.save(...)`.

---

## 🧪 SMMPrime Auto-Order — порт из FunPay

Логика 1-в-1 как в `smmprime_plugin_v1_0_2.py`:

1. Покупатель оплачивает заказ на GGSEL.
2. Плагин ищет связку (по `lot_id` или по названию товара).
3. Если есть — спрашивает у покупателя ссылку (`ASK_LINK`).
4. Покупатель присылает ссылку — плагин валидирует `_URL_RE`-ом, показывает
   сводку (`CONFIRM`).
5. Покупатель: «Да» → создаётся реальный заказ в SMMPrime (или dry-run).
   «Отмена» → возвращаемся к шагу 3, ждём другую ссылку.
6. Финальное сообщение покупателю: SUCCESS / ERROR / DRY_RUN.

Состояние `pending` — в `storage/smmprime_pending_orders.json`, переживает
перезапуск, защищено от двойного API-вызова.

Конфиг — `storage/smmprime_config.json`. Можно редактировать руками —
плагин подхватит сам.

---

## 🛡 Что осталось доделать под ваше окружение

GGSEL не публикует SDK, поэтому HTTP-парсер в `ggsel_api/account.py` основан
на эвристике (regex по HTML личного кабинета). При первом запуске:

* проверьте `python main.py` без ошибок логина;
* если GGSEL изменит вёрстку — обновите регулярки в
  `ggsel_api/account.py::_parse_orders` / `get_chats` / `get_chat_messages`;
* если у вас есть приватный API-токен от GGSEL — заполните `GGSEL_API_TOKEN`,
  он отправляется как `Authorization: Bearer ...`.

Эти места специально вынесены в один класс `GGSELAccount`, чтобы их можно
было подменить наследником без правки ядра.

---

## 📜 Лицензия

MIT.

## 🙋 Помощь

Issues / PR — приветствуются. Telegram: `@radcswd`.

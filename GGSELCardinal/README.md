# GGSELCardinal

Автоматизация продаж на [GGSEL](https://ggsel.com/) (бывший Digiseller) —
аналог [FunPayCardinal](https://github.com/sidor0912/FunPayCardinal),
но для GGSEL. Полноценный движок с Telegram-ботом, системой плагинов
и запуском «в один клик».

В комплекте идёт плагин **SMMPrime Auto-Order** — порт одноимённого
плагина из FunPayCardinal, который автоматически создаёт заказы в
SMM-панели [SMMPrime](https://smmprime.com/) при покупках на GGSEL.

---

## Возможности

| Что | Где |
|-----|-----|
| Long-poll за новыми заказами GGSEL | `GGSELApi/runner.py` |
| Long-poll за сообщениями в чатах покупателей | `GGSELApi/runner.py` |
| Авторизация и API-обёртка GGSEL/Digiseller | `GGSELApi/account.py` |
| Cardinal-оркестратор + шина событий | `cardinal.py` |
| Динамическая загрузка плагинов из `plugins/` | `cardinal.py` |
| Hot-reload плагина при изменении файла | `cardinal._hot_reload_loop` |
| Telegram-бот с inline-меню и управлением плагинами | `tg_bot/bot.py` |
| Уведомления об ошибках в Telegram | `tg_bot/bot.send_notification` |
| Плагин SMMPrime: state-machine, DRY-RUN, шаблоны | `plugins/smmprime_ggsel_plugin.py` |
| Защита от двойных API-вызовов | `_ORDER_PROCESSING_SET` |
| Pending-state в JSON, переживает перезапуск | `storage/smmprime_pending_orders.json` |
| Авто-перезапуск при падении | `start.bat` / `start.sh` |
| Docker / docker-compose | `Dockerfile`, `docker-compose.yml` |
| Подробные логи | `logs/cardinal.log` |
| `.env`-конфиг | `.env.example` |

---

## Запуск «в один клик»

### Windows

1. Скачайте [Python 3.11+](https://www.python.org/downloads/) (при установке
   включите галочку **Add Python to PATH**).
2. Дважды кликните `start.bat`.
   Скрипт сам создаст `venv/`, поставит зависимости, откроет `.env` на
   правку и запустит бот. При падении — авто-перезапуск через 5 секунд.

### Linux / macOS

```bash
chmod +x start.sh
./start.sh
```

### Docker

```bash
cp .env.example .env       # отредактируйте токены
docker compose up -d --build
docker compose logs -f
```

---

## Настройка `.env`

```ini
GGSEL_SELLER_ID=12345              # ваш ID продавца в GGSEL
GGSEL_API_KEY=xxxxxxxxxxxxxxxx     # API-ключ из ЛК GGSEL → "Для разработчиков"
TG_BOT_TOKEN=12345:AA...           # токен от @BotFather
TG_ADMIN_IDS=11111111,22222222     # ваш telegram user_id (можно несколько)
LOG_LEVEL=INFO
```

> `TG_ADMIN_IDS` ограничивает доступ к боту. Если оставить пустым —
> доступ открыт всем (dev-mode, **в проде не используйте**).

---

## Telegram-бот

| Команда | Что делает |
|---------|-----------|
| `/menu`, `/start`, `/help` | Главное меню |
| Кнопка «🧩 Плагины» | Список плагинов, вход в плагин |
| Кнопка «⚙ Настройки» внутри плагина | Передаёт управление плагину |
| Кнопка «🛒 Заказы» | Последние 10 заказов GGSEL |
| Кнопка «🔁 Перезагрузить плагины» | Hot-reload всех `.py` из `plugins/` |

Внутри плагина SMMPrime:

| Кнопка | Действие |
|--------|----------|
| 🟢 / 🔴 | Глобальный вкл/выкл плагина |
| 🔑 API-ключ | Ввод/смена SMMPrime API-ключа |
| 💰 Баланс | Запрос баланса SMMPrime |
| 📃 Список услуг | Первые 20 услуг из SMMPrime |
| 🛒 Связки | CRUD связок «GGSEL lot → SMMPrime service» |
| ⏳ Pending | Активные/завершённые заказы плагина |
| 📝 Шаблоны | Редактирование текстов для покупателя |

---

## Архитектура

```
GGSELCardinal/
├── main.py                  # точка входа
├── cardinal.py              # ядро + plugin loader + hot reload
├── GGSELApi/                # обёртка над API GGSEL/Digiseller
│   ├── account.py           # авторизация, заказы, чаты, лоты
│   ├── runner.py            # long-poll, эмиссия событий
│   ├── types.py             # OrderShortcut, MessageShortcut, LotFields
│   └── events.py            # EventTypes
├── tg_bot/                  # Telegram-бот
│   ├── bot.py               # TGBot — обёртка над pyTelegramBotAPI
│   └── cbt.py               # CBT-константы (совместимы с FPC)
├── plugins/                 # плагины (любой *.py подхватывается)
│   └── smmprime_ggsel_plugin.py
├── storage/                 # JSON-конфиги, pending-state, логи событий
├── logs/                    # cardinal.log
├── requirements.txt
├── start.bat / start.sh     # one-click запуск с авто-рестартом
├── run.bat                  # запуск без авто-рестарта
├── Dockerfile
└── docker-compose.yml
```

### Жизненный цикл плагина

При старте `cardinal.init()`:

1. Авторизация в GGSEL (`Account.get`).
2. Сканирование `plugins/*.py`, импорт модулей.
3. Для каждого плагина:
   * вызываются `BIND_TO_PRE_INIT`, затем `BIND_TO_POST_INIT`;
   * регистрируются Telegram-handlers через `cardinal.telegram.bot`.
4. Запуск `Runner`, рассылка событий:
   * `BIND_TO_NEW_ORDER` — при новом заказе;
   * `BIND_TO_NEW_MESSAGE` / `BIND_TO_LAST_CHAT_MESSAGE_CHANGED` — при сообщении.
5. Фоновый поток следит за `mtime` файлов плагинов — при изменении
   автоматически перезагружает плагин.

### Структура плагина (совместима с FunPayCardinal)

```python
NAME = "MyPlugin"
VERSION = "1.0.0"
UUID = "..."
DESCRIPTION = "..."
SETTINGS_PAGE = True   # появится кнопка "⚙ Настройки" в меню плагинов

def _pre_init(cardinal):
    ...

def handle_new_order(cardinal, order):
    ...

BIND_TO_PRE_INIT = [_pre_init]
BIND_TO_NEW_ORDER = [handle_new_order]
```

Это в точности тот же контракт, что у FPC — плагины, написанные
под FunPayCardinal, переносятся минимальными правками (заменить
`from FunPayAPI...` на `from GGSELApi...` и подправить поля
`OrderShortcut`).

---

## Плагин SMMPrime Auto-Order: как пользоваться

1. `/menu` → 🧩 Плагины → **SMMPrime Auto-Order** → ⚙ Настройки.
2. Введите API-ключ SMMPrime (из ЛК https://smmprime.com).
3. Жмёте «💰 Баланс» — должно вернуть число.
4. Жмёте «📃 Список услуг» — увидите каталог с `service_id`.
5. «🛒 Связки → ➕ Добавить связку»:
   * GGSEL `lot_id` — числовой ID товара GGSEL;
   * SMMPrime `service_id` — из шага 4.
   * По умолчанию связка создаётся с **DRY-RUN включён**.
6. Сделайте тестовую покупку — плагин должен:
   * написать покупателю «Пришлите ссылку»;
   * после ссылки — показать сводку и спросить «Да/Отмена»;
   * на «Да» в dry-run режиме реального заказа не создаётся.
7. Когда убедились что всё ок — выключите DRY-RUN в карточке связки.

### Защита от двойных заказов

`_ORDER_PROCESSING_SET` под `_PENDING_LOCK` гарантирует, что один и
тот же GGSEL `order_id` не может попасть в `smm_add_order` дважды,
даже если событие пришло одновременно по двум каналам
(`NEW_MESSAGE` + `LAST_CHAT_MESSAGE_CHANGED`).

---

## Логи

| Где | Что |
|-----|-----|
| `logs/cardinal.log` | Главный лог движка |
| `storage/smmprime_orders.log` | Журнал автоматических заказов |
| `storage/smmprime_pending_orders.json` | Состояние pending-заказов |
| `storage/smmprime_config.json` | Конфиг плагина (API-ключ, связки, шаблоны) |

API-ключ хранится **только** в `storage/smmprime_config.json` и никуда,
кроме SMMPrime API, не отправляется.

---

## FAQ

**Q: Можно ли перенести существующие плагины FunPayCardinal?**
Да, в большинстве случаев достаточно:
1. заменить `from FunPayAPI...` на `from GGSELApi...`;
2. убедиться, что плагин не использует FunPay-специфичные методы вроде
   `cardinal.account.get_subcategory_lots` (у GGSEL модель данных другая).

**Q: Хочу плагин для другой SMM-панели — что делать?**
Скопируйте `smmprime_ggsel_plugin.py`, замените `UUID`, `NAME`, и
функции `smm_*` под нужный API. Остальная логика (state-machine,
шаблоны, UI) останется как есть.

**Q: GGSEL заблокировал API-ключ.**
Это бывает при флуде. Поднимите `POLL_INTERVAL_SEC` в
`GGSELApi/runner.py` или попросите ключ заново в ЛК GGSEL.

---

## Лицензия

MIT. Используйте на свой страх и риск.

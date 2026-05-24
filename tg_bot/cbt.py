"""Константы callback'ов (Callback Token).

Совпадают по смыслу с FunPayCardinal tg_bot.CBT — это важно, потому что
портированные плагины ожидают именно такие префиксы.
"""


class CBT:
    # Главное меню
    MAIN = "0"
    BACK = "1"

    # Плагины
    PLUGINS_LIST = "44"
    EDIT_PLUGIN = "45"
    TOGGLE_PLUGIN = "46"
    PLUGIN_SETTINGS = "47"
    RELOAD_PLUGIN = "48"

    # Заказы / сообщения
    ORDERS = "50"
    MESSAGES = "51"
    SETTINGS = "60"
    STATUS = "70"

    # Используется в callback_data плагинов как `EDIT_PLUGIN:<uuid>:<offset>`
    # и `PLUGIN_SETTINGS:<uuid>:<offset>` — формат, точно как у FunPay.

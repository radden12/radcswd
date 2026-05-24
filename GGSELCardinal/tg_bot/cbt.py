"""Callback-константы для inline-кнопок.

Значения подобраны так же, как у FunPayCardinal — это нужно, чтобы
плагины могли пользоваться `tg_bot.CBT.PLUGIN_SETTINGS` без правок.
"""


class CBT:
    # Главные точки меню.
    MAIN = "00"
    PLUGINS_LIST = "44"
    EDIT_PLUGIN = "45"
    PLUGIN_SETTINGS = "47"
    PLUGIN_TOGGLE = "48"
    NOOP = "99"

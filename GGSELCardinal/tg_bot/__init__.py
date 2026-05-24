"""Telegram-бот GGSELCardinal.

API повторяет FPC.tg_bot — те же CBT-константы, та же `TGBot`-обёртка.
Это позволяет переносить плагины из FunPayCardinal без правок.
"""
from .cbt import CBT
from .bot import TGBot

__all__ = ["CBT", "TGBot"]

"""HTTP-клиент к GGSEL.

⚠ Важно: у GGSEL нет открытого SDK. Этот клиент использует приватные
эндпоинты личного кабинета. Если GGSEL поменяет вёрстку — нужно будет
обновить парсеры. Все методы изолированы, чтобы их можно было
переопределить в плагине или подменить mock-ом.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Iterable

import httpx

from .types import Order, Message, ChatShortcut, LotFields

logger = logging.getLogger("ggsel.account")


class GGSELError(Exception):
    """Базовая ошибка GGSEL."""


class GGSELAuthError(GGSELError):
    """Не авторизован / истекли куки / неверный токен."""


_BASE_URL = "https://ggsel.net"
_DEFAULT_TIMEOUT = 20


class GGSELAccount:
    """Тонкий клиент GGSEL.

    Можно авторизоваться двумя способами:

    1. Через ``api_token`` (если у вас есть приватный токен продавца).
    2. Через ``login`` / ``password`` — клиент логинится через форму.

    Все сетевые операции делаем через один ``httpx.Client``, чтобы куки
    автоматически сохранялись между запросами.
    """

    def __init__(
        self,
        *,
        seller_id: str | int | None = None,
        api_token: str | None = None,
        login: str | None = None,
        password: str | None = None,
        base_url: str = _BASE_URL,
        timeout: int = _DEFAULT_TIMEOUT,
        proxy: str | None = None,
    ) -> None:
        self.seller_id = str(seller_id) if seller_id else ""
        self.api_token = (api_token or "").strip()
        self.login = (login or "").strip()
        self.password = password or ""
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
        }
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"

        client_kwargs: dict = {
            "headers": headers,
            "timeout": timeout,
            "follow_redirects": True,
        }
        if proxy:
            client_kwargs["proxies"] = proxy

        self._client = httpx.Client(**client_kwargs)
        self._lock = threading.RLock()
        self._authenticated = bool(self.api_token)
        self._csrf_token: str | None = None
        self._username: str = ""

    # ────────────────────────── базовое HTTP ──────────────────────────
    def _get(self, path: str, **kw) -> httpx.Response:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        with self._lock:
            r = self._client.get(url, **kw)
        self._maybe_extract_csrf(r.text)
        return r

    def _post(self, path: str, **kw) -> httpx.Response:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        with self._lock:
            r = self._client.post(url, **kw)
        self._maybe_extract_csrf(r.text)
        return r

    _CSRF_RE = re.compile(r'name="(?:csrf_token|_token)"\s+value="([^"]+)"')

    def _maybe_extract_csrf(self, html: str) -> None:
        if not html:
            return
        m = self._CSRF_RE.search(html)
        if m:
            self._csrf_token = m.group(1)

    # ────────────────────────── авторизация ───────────────────────────
    def login_if_needed(self) -> None:
        if self._authenticated:
            return
        if not self.login or not self.password:
            raise GGSELAuthError(
                "Не указан api_token и не заполнены логин/пароль "
                "в .env (GGSEL_LOGIN / GGSEL_PASSWORD)"
            )
        # Тянем форму логина, чтобы получить cookies + csrf.
        self._get("/login")
        payload = {
            "login": self.login,
            "password": self.password,
        }
        if self._csrf_token:
            payload["csrf_token"] = self._csrf_token
        r = self._post("/login", data=payload)
        if r.status_code in (401, 403):
            raise GGSELAuthError(f"Логин отклонён: HTTP {r.status_code}")
        # Эвристика: после успешного логина в куках появляется session-id.
        # Не все эндпоинты GGSEL предсказуемы — поэтому считаем "ок", если
        # форма больше не показывается.
        if "login" in r.url.path:
            raise GGSELAuthError("Логин не удался: GGSEL вернул форму обратно")
        self._authenticated = True
        logger.info("[GGSEL] авторизация успешна (%s)", self.login)

    # ────────────────────── публичный API ─────────────────────────────
    def get_username(self) -> str:
        if self._username:
            return self._username
        self.login_if_needed()
        r = self._get("/my")
        m = re.search(r"<title>([^<]+)</title>", r.text or "")
        self._username = m.group(1).strip() if m else "ggsel-seller"
        return self._username

    def get_balance(self) -> dict:
        """Возвращает балансы продавца. Структура зависит от страницы LK."""
        self.login_if_needed()
        r = self._get("/my/balance")
        if r.status_code != 200:
            raise GGSELError(f"GGSEL /my/balance HTTP {r.status_code}")
        # Парсинг балансов — упрощённый: ищем "balance: <число>".
        out: dict[str, float] = {}
        for m in re.finditer(
            r"([A-Z]{3})[^0-9\-]{0,40}?([\-]?\d+[\.,]?\d*)",
            r.text or "",
        ):
            cur, val = m.group(1), m.group(2).replace(",", ".")
            try:
                out[cur] = float(val)
            except ValueError:
                pass
        return out

    def get_orders(self) -> list[Order]:
        """Список заказов продавца за последнее время.

        ⚠ Если GGSEL поменяет HTML — нужно обновить регэксп. Парсер
        специально лояльный: он возвращает пустой список, если ничего не
        нашёл, не падая с исключением.
        """
        self.login_if_needed()
        r = self._get("/my/sales")
        if r.status_code != 200:
            raise GGSELError(f"GGSEL /my/sales HTTP {r.status_code}")
        return list(self._parse_orders(r.text or ""))

    @staticmethod
    def _parse_orders(html: str) -> Iterable[Order]:
        # Минимальный универсальный парсер карточек заказов.
        # Поскольку реальная вёрстка GGSEL может меняться, выносим в
        # отдельный метод — легко переопределить в наследнике.
        for m in re.finditer(
            r"data-order-id=\"(\d+)\"[^>]*>.*?"
            r"data-title=\"([^\"]+)\".*?"
            r"data-amount=\"(\d+)\".*?"
            r"data-price=\"([\d\.]+)\".*?"
            r"data-buyer=\"([^\"]*)\".*?"
            r"data-status=\"([^\"]+)\"",
            html, re.DOTALL,
        ):
            yield Order(
                id=m.group(1),
                title=m.group(2),
                amount=int(m.group(3) or 1),
                price=float(m.group(4) or 0),
                buyer_username=m.group(5),
                status=m.group(6),
            )

    def get_chats(self) -> list[ChatShortcut]:
        self.login_if_needed()
        r = self._get("/my/messages")
        if r.status_code != 200:
            raise GGSELError(f"GGSEL /my/messages HTTP {r.status_code}")
        out: list[ChatShortcut] = []
        for m in re.finditer(
            r'data-chat-id=\"(\d+)\"[^>]*>.*?'
            r'data-chat-name=\"([^\"]+)\".*?'
            r'data-last-msg=\"([^\"]*)\".*?'
            r'data-last-msg-id=\"(\d+)\"',
            r.text or "", re.DOTALL,
        ):
            out.append(ChatShortcut(
                id=m.group(1),
                name=m.group(2),
                last_message_text=m.group(3),
                last_message_id=int(m.group(4) or 0),
            ))
        return out

    def get_chat_messages(self, chat_id: int | str,
                          limit: int = 50) -> list[Message]:
        self.login_if_needed()
        r = self._get(f"/my/messages/{chat_id}", params={"limit": limit})
        if r.status_code != 200:
            raise GGSELError(
                f"GGSEL /my/messages/{chat_id} HTTP {r.status_code}"
            )
        out: list[Message] = []
        for m in re.finditer(
            r'data-msg-id=\"(\d+)\"[^>]*data-author-id=\"(\d+)\"'
            r'[^>]*data-author=\"([^\"]*)\"[^>]*data-text=\"([^\"]*)\"'
            r'[^>]*data-is-mine=\"(true|false|0|1)\"',
            r.text or "",
        ):
            out.append(Message(
                id=int(m.group(1)),
                chat_id=chat_id,
                author_id=int(m.group(2)),
                author=m.group(3),
                text=m.group(4),
                is_my_message=m.group(5) in ("true", "1"),
            ))
        return out

    def send_message(self, chat_id: int | str, text: str) -> bool:
        self.login_if_needed()
        payload = {"chat_id": str(chat_id), "text": text}
        if self._csrf_token:
            payload["csrf_token"] = self._csrf_token
        r = self._post("/my/messages/send", data=payload)
        ok = r.status_code in (200, 204)
        if not ok:
            logger.warning("[GGSEL] send_message HTTP %s: %s",
                           r.status_code, (r.text or "")[:200])
        return ok

    def get_lot_fields(self, lot_id: int | str) -> LotFields:
        self.login_if_needed()
        r = self._get(f"/my/lots/{lot_id}/edit")
        if r.status_code != 200:
            raise GGSELError(
                f"GGSEL /my/lots/{lot_id}/edit HTTP {r.status_code}"
            )
        fields: dict = {}
        for m in re.finditer(
            r'<input[^>]+name=\"([^\"]+)\"[^>]+value=\"([^\"]*)\"',
            r.text or "",
        ):
            fields[m.group(1)] = m.group(2)
        # textarea description и т.д.
        for m in re.finditer(
            r'<textarea[^>]+name=\"([^\"]+)\"[^>]*>([^<]*)</textarea>',
            r.text or "",
        ):
            fields.setdefault(m.group(1), m.group(2))
        return LotFields(
            lot_id=lot_id,
            fields=fields,
            csrf_token=self._csrf_token,
        )

    def save_lot(self, lot_fields: LotFields) -> bool:
        self.login_if_needed()
        payload = dict(lot_fields.fields)
        if lot_fields.csrf_token:
            payload.setdefault("csrf_token", lot_fields.csrf_token)
        r = self._post(f"/my/lots/{lot_fields.lot_id}/edit", data=payload)
        ok = r.status_code in (200, 302)
        if not ok:
            logger.warning("[GGSEL] save_lot HTTP %s: %s",
                           r.status_code, (r.text or "")[:200])
        return ok

    # ─────────────────────────── сервис ───────────────────────────────
    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass

    def __repr__(self) -> str:
        return f"<GGSELAccount login={self.login or '<token>'}>"

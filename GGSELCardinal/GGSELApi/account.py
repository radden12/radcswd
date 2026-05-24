"""GGSEL Account — обёртка над публичным API площадки.

GGSEL (https://ggsel.com) — маркетплейс цифровых товаров, унаследовавший
API Digiseller. Эндпоинты документированы на https://my.digiseller.com/
inside/api.asp. Здесь мы инкапсулируем ключевые операции, нужные движку:
авторизация, получение заказов, отправка сообщений в чат покупателя,
чтение / сохранение лотов.

Все сетевые методы:
  • синхронны (requests), чтобы не плодить event-loops в плагинах;
  • кидают `GgselApiError` при ошибке HTTP/бизнес-логики;
  • снабжены retry на сетевых сбоях (см. `_request_with_retry`).
"""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

import requests

from .types import LotFields, LotShortcut

logger = logging.getLogger("GGSEL.account")

DEFAULT_BASE_URL = "https://api.digiseller.com"
DEFAULT_TIMEOUT = 20


class GgselApiError(RuntimeError):
    pass


class Account:
    """Авторизованный аккаунт продавца GGSEL.

    Параметры:
      seller_id   — числовой ID продавца GGSEL (он же Digiseller).
      api_key     — постоянный API-ключ из ЛК → API.
      base_url    — обычно оставлять дефолт.

    Использование:
        acc = Account(seller_id=12345, api_key="...")
        acc.get()              # ping, тянет токен
        acc.get_orders(...)    # список заказов
    """

    def __init__(
        self,
        seller_id: int | str | None = None,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.seller_id = str(seller_id) if seller_id is not None else ""
        self.api_key = api_key or ""
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

        self._token: str = ""
        self._token_expires_at: float = 0.0
        self._username: str = ""
        self.id: str = self.seller_id

    # ------------------------------------------------------------------ utils
    def _request_with_retry(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        retries: int = 2,
        backoff: float = 1.5,
    ) -> Any:
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = requests.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    timeout=self.timeout,
                    headers={"Accept": "application/json"},
                )
                if resp.status_code >= 500:
                    raise GgselApiError(f"HTTP {resp.status_code} from {path}")
                if resp.headers.get("content-type", "").startswith(
                    "application/json"
                ):
                    return resp.json()
                return resp.text
            except (requests.RequestException, GgselApiError) as exc:
                last_exc = exc
                logger.warning(
                    "GGSEL %s %s attempt %d/%d failed: %s",
                    method, path, attempt + 1, retries + 1, exc,
                )
                if attempt < retries:
                    time.sleep(backoff ** attempt)
        raise GgselApiError(f"All retries failed for {path}: {last_exc}")

    # ------------------------------------------------------------------ auth
    def _ensure_token(self) -> str:
        """Получить (или переиспользовать) сессионный токен Digiseller.

        Цикл: POST /api/apilogin → возвращает token со сроком ~2 часа.
        """
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        ts = int(time.time())
        sign = hashlib.sha256(
            f"{self.api_key}{ts}".encode("utf-8")
        ).hexdigest()
        body = {
            "seller_id": int(self.seller_id) if self.seller_id.isdigit() else 0,
            "timestamp": ts,
            "sign": sign,
        }
        data = self._request_with_retry(
            "POST", "/api/apilogin", json=body, retries=2,
        )
        if isinstance(data, dict) and data.get("retval") == 0:
            self._token = data.get("token", "")
            valid_thru = data.get("valid_thru", "")
            # 2h TTL по умолчанию.
            self._token_expires_at = time.time() + 2 * 3600
            logger.info("GGSEL login OK, token=%s..., valid_thru=%s",
                        self._token[:8], valid_thru)
            return self._token
        raise GgselApiError(
            f"Login failed: {data!r}" if data else "Login: empty response"
        )

    # ------------------------------------------------------------------ ping
    def get(self) -> "Account":
        """Аналог FunPayAPI.Account.get() — проверяет соединение и логинит."""
        self._ensure_token()
        return self

    # ---------------------------------------------------------------- orders
    def get_orders(
        self,
        *,
        date_start: str | None = None,
        date_finish: str | None = None,
        returned: int = 0,
    ) -> list[dict]:
        """Список заказов продавца.

        GET /api/seller-sales/v2?token=...&...
        Возвращает сырой список dict — преобразование в `OrderShortcut`
        делает Runner.
        """
        token = self._ensure_token()
        params: dict[str, Any] = {
            "token": token,
            "returned": returned,
            "page": 1,
            "rows": 50,
        }
        if date_start:
            params["date_start"] = date_start
        if date_finish:
            params["date_finish"] = date_finish
        data = self._request_with_retry(
            "GET", "/api/seller-sales/v2", params=params,
        )
        if isinstance(data, dict):
            return data.get("rows") or data.get("sales") or []
        return []

    def get_order(self, order_id: str) -> dict:
        """Подробности конкретного заказа."""
        token = self._ensure_token()
        data = self._request_with_retry(
            "GET",
            f"/api/purchase/info/{order_id}",
            params={"token": token},
        )
        return data if isinstance(data, dict) else {}

    # ----------------------------------------------------------------- chat
    def send_message(
        self,
        chat_id: str,
        text: str,
        chat_name: str | None = None,
    ) -> bool:
        """Отправить сообщение покупателю по `chat_id` (debate_id)."""
        token = self._ensure_token()
        body = {
            "id_i": int(chat_id) if str(chat_id).isdigit() else chat_id,
            "message": text,
        }
        data = self._request_with_retry(
            "POST",
            "/api/debates/v2",
            params={"token": token},
            json=body,
        )
        if isinstance(data, dict) and data.get("retval") == 0:
            return True
        logger.warning("GGSEL send_message failed chat=%s: %r", chat_id, data)
        return False

    def get_chat_messages(self, chat_id: str, limit: int = 30) -> list[dict]:
        token = self._ensure_token()
        data = self._request_with_retry(
            "GET",
            "/api/debates/v2",
            params={
                "token": token,
                "id_i": chat_id,
                "newer": 1,
                "count": limit,
            },
        )
        if isinstance(data, dict):
            return data.get("messages", []) or []
        return []

    # ----------------------------------------------------------------- lots
    def get_lot_fields(
        self,
        lot_id: str | int,
        subcategory_id: str | int | None = None,  # noqa: ARG002 — совместимость
    ) -> LotFields:
        """Чтение редактируемых полей лота.

        Эмулирует сигнатуру FunPayAPI.Account.get_lot_fields(lot_id) ради
        совместимости с плагинами FPC. subcategory_id у GGSEL не нужен,
        принимается как no-op.
        """
        token = self._ensure_token()
        data = self._request_with_retry(
            "GET",
            f"/api/products/{lot_id}",
            params={"token": token},
        )
        if not isinstance(data, dict) or data.get("retval") not in (0, None):
            raise GgselApiError(f"get_lot_fields failed: {data!r}")
        product = data.get("product") or data
        return LotFields(
            lot_id=str(lot_id),
            fields={
                "price": str(product.get("price", "")),
                "title": product.get("name", ""),
                "active": bool(product.get("enabled", True)),
                "_raw": product,
            },
        )

    def save_lot(self, lot_fields: LotFields) -> bool:
        """Сохранить отредактированные поля лота."""
        token = self._ensure_token()
        body = {
            "product_id": int(lot_fields.lot_id) if str(lot_fields.lot_id).isdigit()
            else lot_fields.lot_id,
            "price": float(lot_fields["price"]),
            "name": lot_fields.get("title"),
            "enabled": lot_fields.get("active", True),
        }
        data = self._request_with_retry(
            "POST",
            "/api/product/edit/uniprice",
            params={"token": token},
            json=body,
        )
        if isinstance(data, dict) and data.get("retval") == 0:
            return True
        raise GgselApiError(f"save_lot failed: {data!r}")

    def get_my_lots(self) -> list[LotShortcut]:
        token = self._ensure_token()
        data = self._request_with_retry(
            "GET",
            "/api/seller-goods",
            params={
                "token": token,
                "id_seller": self.seller_id,
                "rows": 100,
                "page": 1,
            },
        )
        rows: list[dict] = []
        if isinstance(data, dict):
            rows = data.get("rows") or data.get("product") or []
        out: list[LotShortcut] = []
        for r in rows:
            out.append(LotShortcut(
                id=str(r.get("id_goods") or r.get("id") or ""),
                title=r.get("name_goods") or r.get("name") or "",
                price=float(r.get("price") or 0.0),
                subcategory_id=str(r.get("category_id") or ""),
                active=bool(r.get("enabled", True)),
                raw=r,
            ))
        return out

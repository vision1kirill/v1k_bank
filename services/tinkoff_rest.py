"""
Клиент T-Bank Invest API через REST (без grpc-пакета).

T-Bank предоставляет REST-шлюз поверх gRPC:
https://invest-public-api.tinkoff.ru/rest/

Всё то же самое что gRPC SDK, но через обычные HTTP POST запросы с JSON.
Авторизация: Bearer токен в заголовке Authorization.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

REST_BASE = "https://invest-public-api.tinkoff.ru/rest"
SANDBOX_BASE = "https://sandbox-invest-public-api.tinkoff.ru/rest"
TIMEOUT = 15.0


def _q(value: float) -> dict:
    """float → Quotation dict для T-Bank API."""
    units = int(value)
    nano = round((value - units) * 1_000_000_000)
    return {"units": str(units), "nano": nano}


def _from_q(q: dict) -> float:
    """Quotation dict → float."""
    if not q:
        return 0.0
    return float(q.get("units", 0)) + float(q.get("nano", 0)) / 1_000_000_000


class TinkoffRestClient:
    """
    REST-клиент T-Bank Invest API.
    Полный аналог tinkoff-investments SDK, но через httpx.
    Работает как в sandbox, так и в реальном режиме.
    """

    def __init__(self, token: str, use_sandbox: bool, account_id: str = ""):
        self.token = token
        self.use_sandbox = use_sandbox
        self.account_id = account_id
        self._base = SANDBOX_BASE if use_sandbox else REST_BASE
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "accept": "application/json",
        }

    def _url(self, service: str, method: str) -> str:
        return f"{self._base}/tinkoff.public.invest.api.contract.v1.{service}/{method}"

    async def _post(self, service: str, method: str, body: dict) -> dict:
        """Выполнить REST запрос. Возвращает dict или {} при ошибке."""
        url = self._url(service, method)
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.post(url, json=body, headers=self._headers)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"T-Bank REST {method}: HTTP {e.response.status_code} — {e.response.text[:200]}")
            return {}
        except Exception as e:
            logger.error(f"T-Bank REST {method}: {e}")
            return {}

    # ─── Аккаунты ─────────────────────────────────────────────────────────────

    async def get_accounts(self) -> list[dict]:
        """Получить список счетов."""
        if self.use_sandbox:
            data = await self._post("SandboxService", "GetSandboxAccounts", {})
        else:
            data = await self._post("UsersService", "GetAccounts", {})
        return data.get("accounts", [])

    async def open_sandbox_account(self) -> str:
        """Открыть sandbox счёт. Возвращает account_id."""
        data = await self._post("SandboxService", "OpenSandboxAccount", {})
        return data.get("accountId", "")

    async def ensure_account(self) -> bool:
        """Инициализировать account_id. Возвращает True если успешно."""
        accounts = await self.get_accounts()
        if not accounts and self.use_sandbox:
            acc_id = await self.open_sandbox_account()
            if acc_id:
                self.account_id = acc_id
                logger.info(f"Открыт sandbox счёт: {acc_id}")
                return True
            return False
        if accounts:
            if not self.account_id:
                self.account_id = accounts[0].get("id", "")
            mode = "sandbox" if self.use_sandbox else "реальный"
            logger.info(f"T-Bank {mode} счёт: {self.account_id}")
            return True
        return False

    # ─── Инструменты ──────────────────────────────────────────────────────────

    async def find_instrument(self, ticker: str) -> Optional[dict]:
        """Найти инструмент по тикеру."""
        data = await self._post("InstrumentsService", "FindInstrument", {
            "query": ticker,
            "instrumentKind": "INSTRUMENT_TYPE_UNSPECIFIED",
        })
        instruments = data.get("instruments", [])
        for inst in instruments:
            if inst.get("ticker", "").upper() == ticker.upper():
                class_code = inst.get("classCode", "")
                if class_code in ("TQBR", "TQTF", "TQOB"):
                    return {
                        "figi": inst.get("figi", ""),
                        "ticker": inst.get("ticker", ""),
                        "name": inst.get("name", ""),
                        "lot": inst.get("lot", 1),
                        "currency": inst.get("currency", "rub"),
                        "class_code": class_code,
                        "uid": inst.get("uid", ""),
                    }
        return None

    # ─── Рыночные данные ──────────────────────────────────────────────────────

    async def get_last_prices(self, figis: list[str]) -> dict[str, float]:
        """Получить последние цены. Возвращает {figi: price}."""
        data = await self._post("MarketDataService", "GetLastPrices", {
            "instrumentId": figis
        })
        result = {}
        for lp in data.get("lastPrices", []):
            figi = lp.get("figi", "")
            price = _from_q(lp.get("price", {}))
            if figi and price:
                result[figi] = price
        return result

    async def get_last_price(self, figi: str) -> Optional[float]:
        """Получить последнюю цену одного инструмента."""
        prices = await self.get_last_prices([figi])
        return prices.get(figi)

    async def get_candles(
        self,
        figi: str,
        days: int = 30,
        interval: str = "CANDLE_INTERVAL_DAY",
    ) -> list[dict]:
        """Получить исторические свечи."""
        now = datetime.now(timezone.utc)
        from_ = now - timedelta(days=days)
        data = await self._post("MarketDataService", "GetCandles", {
            "instrumentId": figi,
            "from": from_.isoformat(),
            "to": now.isoformat(),
            "interval": interval,
        })
        candles = []
        for c in data.get("candles", []):
            candles.append({
                "time": c.get("time"),
                "open": _from_q(c.get("open", {})),
                "high": _from_q(c.get("high", {})),
                "low": _from_q(c.get("low", {})),
                "close": _from_q(c.get("close", {})),
                "volume": c.get("volume", 0),
            })
        return candles

    # ─── Ордера ───────────────────────────────────────────────────────────────

    async def place_order(
        self,
        figi: str,
        lots: int,
        direction: str,       # "buy" | "sell"
        order_type: str,      # "market" | "limit"
        price: float = 0.0,
        order_id: str = "",
    ) -> Optional[dict]:
        """Разместить ордер (рыночный или лимитный)."""
        dir_map = {
            "buy": "ORDER_DIRECTION_BUY",
            "sell": "ORDER_DIRECTION_SELL",
        }
        type_map = {
            "market": "ORDER_TYPE_MARKET",
            "limit": "ORDER_TYPE_LIMIT",
        }
        body = {
            "instrumentId": figi,
            "quantity": lots,
            "accountId": self.account_id,
            "direction": dir_map.get(direction, "ORDER_DIRECTION_BUY"),
            "orderType": type_map.get(order_type, "ORDER_TYPE_MARKET"),
            "orderId": order_id or f"bot_{int(datetime.utcnow().timestamp())}",
        }
        if order_type == "limit" and price:
            body["price"] = _q(price)

        if self.use_sandbox:
            data = await self._post("SandboxService", "PostSandboxOrder", body)
        else:
            data = await self._post("OrdersService", "PostOrder", body)

        if not data:
            return None

        executed_price = _from_q(data.get("executedOrderPrice", {}))
        if not executed_price:
            executed_price = _from_q(data.get("initialOrderPrice", {}))

        return {
            "order_id": data.get("orderId", ""),
            "status": data.get("executionReportStatus", ""),
            "price": executed_price or price,
            "lots": lots,
            "amount": (executed_price or price) * lots,
            "commission": _from_q(data.get("initialCommission", {})),
        }

    async def cancel_order(self, order_id: str) -> bool:
        """Отменить ордер."""
        body = {"accountId": self.account_id, "orderId": order_id}
        if self.use_sandbox:
            data = await self._post("SandboxService", "CancelSandboxOrder", body)
        else:
            data = await self._post("OrdersService", "CancelOrder", body)
        return bool(data)

    async def get_order_state(self, order_id: str) -> Optional[dict]:
        """Статус ордера."""
        body = {"accountId": self.account_id, "orderId": order_id}
        if self.use_sandbox:
            data = await self._post("SandboxService", "GetSandboxOrderState", body)
        else:
            data = await self._post("OrdersService", "GetOrderState", body)

        if not data:
            return None
        return {
            "order_id": order_id,
            "status": data.get("executionReportStatus", ""),
            "price": _from_q(data.get("averagePositionPrice", {})),
            "filled_lots": data.get("lotsExecuted", 0),
            "total_lots": data.get("lotsRequested", 0),
        }

    # ─── Операции ─────────────────────────────────────────────────────────────

    async def get_operations(
        self,
        from_date: datetime,
        to_date: datetime,
        operation_types: list[str] | None = None,
    ) -> list[dict]:
        """Получить операции по счёту."""
        body = {
            "accountId": self.account_id,
            "from": from_date.isoformat(),
            "to": to_date.isoformat(),
        }
        if self.use_sandbox:
            data = await self._post("SandboxService", "GetSandboxOperations", body)
        else:
            data = await self._post("OperationsService", "GetOperations", body)

        result = []
        for op in data.get("operations", []):
            op_type = op.get("operationType", "")
            if operation_types and not any(t in op_type for t in operation_types):
                continue
            result.append({
                "id": op.get("id", ""),
                "type": op_type,
                "figi": op.get("figi", ""),
                "date": op.get("date"),
                "amount": _from_q(op.get("payment", {})),
                "currency": op.get("currency", "rub"),
                "quantity": op.get("quantity", 0),
                "price": _from_q(op.get("price", {})),
            })
        return result

    # ─── Портфель ─────────────────────────────────────────────────────────────

    async def get_portfolio(self) -> dict:
        """Получить портфель."""
        body = {"accountId": self.account_id}
        if self.use_sandbox:
            data = await self._post("SandboxService", "GetSandboxPortfolio", body)
        else:
            data = await self._post("OperationsService", "GetPortfolio", body)

        if not data:
            return {"total_amount": 0.0, "expected_yield": 0.0, "positions": []}

        positions = []
        for p in data.get("positions", []):
            positions.append({
                "figi": p.get("figi", ""),
                "quantity": int(p.get("quantity", {}).get("units", 0)),
                "current_price": _from_q(p.get("currentPrice", {})),
                "avg_buy_price": _from_q(p.get("averageBuyPrice", {})),
                "expected_yield": _from_q(p.get("expectedYield", {})),
            })
        return {
            "total_amount": _from_q(data.get("totalAmountPortfolio", {})),
            "expected_yield": _from_q(data.get("expectedYield", {})),
            "positions": positions,
        }

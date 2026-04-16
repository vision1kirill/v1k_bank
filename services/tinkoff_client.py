"""
Обёртка над T-Bank (Tinkoff) Invest API.

Поддерживает:
- Sandbox режим (USE_SANDBOX=true) — бумажная торговля
- Real режим (USE_SANDBOX=false) — реальные сделки

Все методы возвращают простые Python объекты (dict/float/int),
а не gRPC объекты — чтобы остальной код не зависел от библиотеки.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from config import Config

logger = logging.getLogger(__name__)

# ─── Утилиты для работы с деньгами T-Bank ─────────────────────────────────────

def _quotation_to_float(q) -> float:
    """Tinkoff Quotation (units + nano) → float."""
    if q is None:
        return 0.0
    return float(q.units) + float(q.nano) / 1_000_000_000


def _money_to_float(m) -> float:
    """Tinkoff MoneyValue → float."""
    if m is None:
        return 0.0
    return float(m.units) + float(m.nano) / 1_000_000_000


def _float_to_quotation(value: float):
    """float → Tinkoff Quotation."""
    from tinkoff.invest.schemas import Quotation
    units = int(value)
    nano = round((value - units) * 1_000_000_000)
    return Quotation(units=units, nano=nano)


# ─── Основной клиент ──────────────────────────────────────────────────────────

class TinkoffClient:
    """
    Асинхронный клиент T-Bank Invest API.
    Создаётся один раз и переиспользуется (singleton через фабрику get_client).
    """

    def __init__(self, token: str, use_sandbox: bool, account_id: str = ""):
        self.token = token
        self.use_sandbox = use_sandbox
        self._account_id = account_id
        self._initialized = False
        self._package_available = False  # True только если tinkoff пакет установлен

        # Кэши
        self._instruments_cache: dict[str, dict] = {}  # ticker → instrument info
        self._figi_cache: dict[str, dict] = {}          # figi → instrument info
        self._price_cache: dict[str, tuple[float, datetime]] = {}  # figi → (price, time)
        self._PRICE_CACHE_TTL = 60  # секунды

    async def initialize(self) -> None:
        """Проверяем соединение и получаем account_id если не задан."""
        if not self.token:
            logger.warning("Tinkoff токен не задан. Клиент работает в режиме симуляции.")
            self._initialized = True
            return

        try:
            # Проверяем что пакет установлен
            from tinkoff.invest import AsyncClient
            self._package_available = True
        except ImportError:
            logger.warning(
                "Пакет tinkoff-investments не установлен. "
                "Бот работает в режиме симуляции (без реальных сделок)."
            )
            self._initialized = True
            return

        try:
            from tinkoff.invest import AsyncClient
            async with AsyncClient(self.token) as client:
                if self.use_sandbox:
                    accounts = await client.sandbox.get_sandbox_accounts()
                    if not accounts.accounts:
                        opened = await client.sandbox.open_sandbox_account()
                        self._account_id = opened.account_id
                        logger.info(f"Открыт sandbox счёт: {self._account_id}")
                    else:
                        if not self._account_id:
                            self._account_id = accounts.accounts[0].id
                        logger.info(f"Используем sandbox счёт: {self._account_id}")
                else:
                    accounts = await client.users.get_accounts()
                    if not accounts.accounts:
                        raise ValueError("Нет доступных торговых счетов в T-Bank")
                    if not self._account_id:
                        self._account_id = accounts.accounts[0].id
                    logger.info(f"Подключён к реальному счёту: {self._account_id}")

            self._initialized = True
            logger.info(f"T-Bank клиент инициализирован. Sandbox: {self.use_sandbox}")

        except Exception as e:
            logger.error(f"Ошибка инициализации T-Bank клиента: {e}")
            self._initialized = True

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def is_available(self) -> bool:
        """True если есть токен, пакет установлен, и можно делать сделки."""
        return bool(self.token and self._initialized and self._package_available)

    # ─── Поиск инструментов ───────────────────────────────────────────────────

    async def find_instrument(self, ticker: str) -> dict | None:
        """
        Находит инструмент по тикеру.
        Возвращает dict: {figi, ticker, name, lot, currency, ...}
        """
        ticker = ticker.upper().strip()

        if ticker in self._instruments_cache:
            return self._instruments_cache[ticker]

        if not self.is_available:
            instrument = self._mock_instrument(ticker)
            # Заполняем кэш чтобы _figi_to_ticker работал
            self._figi_cache[instrument["figi"]] = instrument
            self._instruments_cache[ticker] = instrument
            return instrument

        try:
            from tinkoff.invest import AsyncClient, InstrumentStatus
            async with AsyncClient(self.token) as client:
                resp = await client.instruments.find_instrument(query=ticker)
                for inst in resp.instruments:
                    if inst.ticker.upper() == ticker and inst.class_code in ("TQBR", "TQTF", "TQOB"):
                        data = {
                            "figi": inst.figi,
                            "ticker": inst.ticker,
                            "name": inst.name,
                            "lot": inst.lot,
                            "currency": inst.currency,
                            "class_code": inst.class_code,
                            "isin": inst.isin,
                            "exchange": "MOEX",
                        }
                        self._instruments_cache[ticker] = data
                        self._figi_cache[inst.figi] = data
                        return data

            logger.warning(f"Инструмент не найден: {ticker}")
            return None

        except Exception as e:
            logger.error(f"Ошибка поиска инструмента {ticker}: {e}")
            return None

    async def get_instrument_by_figi(self, figi: str) -> dict | None:
        """Получить инструмент по FIGI."""
        if figi in self._figi_cache:
            return self._figi_cache[figi]

        if not self.is_available:
            return {"figi": figi, "ticker": "UNKNOWN", "name": "Unknown", "lot": 1}

        try:
            from tinkoff.invest import AsyncClient
            async with AsyncClient(self.token) as client:
                resp = await client.instruments.share_by(
                    id_type=1,  # INSTRUMENT_ID_TYPE_FIGI
                    id=figi
                )
                inst = resp.instrument
                data = {
                    "figi": inst.figi,
                    "ticker": inst.ticker,
                    "name": inst.name,
                    "lot": inst.lot,
                    "currency": inst.currency,
                    "class_code": inst.class_code,
                }
                self._figi_cache[figi] = data
                self._instruments_cache[inst.ticker] = data
                return data
        except Exception as e:
            logger.error(f"Ошибка получения инструмента {figi}: {e}")
            return None

    # ─── Рыночные данные ──────────────────────────────────────────────────────

    async def get_last_price(self, figi: str) -> float | None:
        """Получить последнюю цену инструмента."""
        # Проверяем кэш (60 сек)
        cached = self._price_cache.get(figi)
        if cached and (datetime.utcnow() - cached[1]).seconds < self._PRICE_CACHE_TTL:
            return cached[0]

        if not self.is_available:
            # Пробуем получить реальную цену с MOEX по тикеру
            ticker = self._figi_to_ticker(figi)
            if ticker:
                from services.moex_client import get_last_price as moex_price
                price = await moex_price(ticker)
                if price:
                    self._price_cache[figi] = (price, datetime.utcnow())
                    return price
            return self._mock_price(figi)

        try:
            from tinkoff.invest import AsyncClient
            async with AsyncClient(self.token) as client:
                resp = await client.market_data.get_last_prices(figi=[figi])
                if resp.last_prices:
                    price = _quotation_to_float(resp.last_prices[0].price)
                    self._price_cache[figi] = (price, datetime.utcnow())
                    return price
            return None
        except Exception as e:
            logger.error(f"Ошибка получения цены {figi}: {e}")
            return None

    async def get_candles(
        self,
        figi: str,
        days: int = 30,
        interval_str: str = "day"
    ) -> list[dict]:
        """
        Получить исторические свечи.
        interval_str: "1min", "5min", "hour", "day"
        Возвращает list[{time, open, high, low, close, volume}]
        """
        if not self.is_available:
            ticker = self._figi_to_ticker(figi)
            if ticker:
                from services.moex_client import get_candles as moex_candles
                candles = await moex_candles(ticker, days)
                if candles:
                    return candles
            return self._mock_candles(figi, days)

        try:
            from tinkoff.invest import AsyncClient, CandleInterval
            interval_map = {
                "1min": CandleInterval.CANDLE_INTERVAL_1_MIN,
                "5min": CandleInterval.CANDLE_INTERVAL_5_MIN,
                "hour": CandleInterval.CANDLE_INTERVAL_HOUR,
                "day": CandleInterval.CANDLE_INTERVAL_DAY,
            }
            interval = interval_map.get(interval_str, CandleInterval.CANDLE_INTERVAL_DAY)
            now = datetime.now(timezone.utc)
            from_ = now - timedelta(days=days)

            async with AsyncClient(self.token) as client:
                resp = await client.market_data.get_candles(
                    figi=figi,
                    from_=from_,
                    to=now,
                    interval=interval,
                )
                return [
                    {
                        "time": c.time,
                        "open": _quotation_to_float(c.open),
                        "high": _quotation_to_float(c.high),
                        "low": _quotation_to_float(c.low),
                        "close": _quotation_to_float(c.close),
                        "volume": c.volume,
                    }
                    for c in resp.candles
                ]
        except Exception as e:
            logger.error(f"Ошибка получения свечей {figi}: {e}")
            return []

    # ─── Ордера ───────────────────────────────────────────────────────────────

    async def place_market_order(
        self,
        figi: str,
        lots: int,
        direction: str,  # "buy" | "sell"
        strategy_id: int,
    ) -> dict | None:
        """
        Размещает рыночный ордер.
        Возвращает: {order_id, status, price, lots, amount} или None при ошибке.
        """
        if lots <= 0:
            logger.warning("Попытка разместить ордер с quantity=0. Пропускаем.")
            return None

        if not self.is_available:
            return self._mock_order(figi, lots, direction)

        try:
            from tinkoff.invest import AsyncClient, OrderDirection, OrderType
            direction_map = {
                "buy": OrderDirection.ORDER_DIRECTION_BUY,
                "sell": OrderDirection.ORDER_DIRECTION_SELL,
            }

            async with AsyncClient(self.token) as client:
                if self.use_sandbox:
                    resp = await client.sandbox.post_sandbox_order(
                        figi=figi,
                        quantity=lots,
                        account_id=self._account_id,
                        direction=direction_map[direction],
                        order_type=OrderType.ORDER_TYPE_MARKET,
                        order_id=f"bot_{strategy_id}_{int(datetime.utcnow().timestamp())}",
                    )
                else:
                    resp = await client.orders.post_order(
                        figi=figi,
                        quantity=lots,
                        account_id=self._account_id,
                        direction=direction_map[direction],
                        order_type=OrderType.ORDER_TYPE_MARKET,
                        order_id=f"bot_{strategy_id}_{int(datetime.utcnow().timestamp())}",
                    )

                executed_price = _money_to_float(resp.executed_order_price)
                if executed_price == 0:
                    executed_price = _money_to_float(resp.initial_order_price)

                return {
                    "order_id": resp.order_id,
                    "status": str(resp.execution_report_status),
                    "price": executed_price,
                    "lots": lots,
                    "amount": executed_price * lots,
                    "commission": _money_to_float(resp.initial_commission),
                }

        except Exception as e:
            logger.error(f"Ошибка размещения ордера {direction} {figi} x{lots}: {e}")
            return None

    async def place_limit_order(
        self,
        figi: str,
        lots: int,
        direction: str,  # "buy" | "sell"
        price: float,
        strategy_id: int,
    ) -> dict | None:
        """Лимитный ордер (для Grid стратегии)."""
        if not self.is_available:
            return self._mock_order(figi, lots, direction, price=price, is_limit=True)

        try:
            from tinkoff.invest import AsyncClient, OrderDirection, OrderType
            direction_map = {
                "buy": OrderDirection.ORDER_DIRECTION_BUY,
                "sell": OrderDirection.ORDER_DIRECTION_SELL,
            }

            async with AsyncClient(self.token) as client:
                if self.use_sandbox:
                    resp = await client.sandbox.post_sandbox_order(
                        figi=figi,
                        quantity=lots,
                        price=_float_to_quotation(price),
                        account_id=self._account_id,
                        direction=direction_map[direction],
                        order_type=OrderType.ORDER_TYPE_LIMIT,
                        order_id=f"grid_{strategy_id}_{int(price * 100)}_{direction}",
                    )
                else:
                    resp = await client.orders.post_order(
                        figi=figi,
                        quantity=lots,
                        price=_float_to_quotation(price),
                        account_id=self._account_id,
                        direction=direction_map[direction],
                        order_type=OrderType.ORDER_TYPE_LIMIT,
                        order_id=f"grid_{strategy_id}_{int(price * 100)}_{direction}",
                    )

                return {
                    "order_id": resp.order_id,
                    "status": "pending",
                    "price": price,
                    "lots": lots,
                    "amount": price * lots,
                    "commission": _money_to_float(resp.initial_commission),
                }

        except Exception as e:
            logger.error(f"Ошибка лимитного ордера {direction} {figi} x{lots} @ {price}: {e}")
            return None

    async def cancel_order(self, order_id: str) -> bool:
        """Отменяет ордер."""
        if not self.is_available:
            return True  # в симуляции всегда успех

        try:
            from tinkoff.invest import AsyncClient
            async with AsyncClient(self.token) as client:
                if self.use_sandbox:
                    await client.sandbox.cancel_sandbox_order(
                        account_id=self._account_id, order_id=order_id
                    )
                else:
                    await client.orders.cancel_order(
                        account_id=self._account_id, order_id=order_id
                    )
            return True
        except Exception as e:
            logger.error(f"Ошибка отмены ордера {order_id}: {e}")
            return False

    async def get_order_status(self, order_id: str) -> dict | None:
        """Проверяет статус ордера."""
        if not self.is_available:
            return {"order_id": order_id, "status": "filled", "price": 0.0}

        try:
            from tinkoff.invest import AsyncClient
            async with AsyncClient(self.token) as client:
                if self.use_sandbox:
                    resp = await client.sandbox.get_sandbox_order_state(
                        account_id=self._account_id, order_id=order_id
                    )
                else:
                    resp = await client.orders.get_order_state(
                        account_id=self._account_id, order_id=order_id
                    )

                return {
                    "order_id": order_id,
                    "status": str(resp.execution_report_status),
                    "price": _money_to_float(resp.average_position_price),
                    "filled_lots": resp.lots_executed,
                    "total_lots": resp.lots_requested,
                }
        except Exception as e:
            logger.error(f"Ошибка статуса ордера {order_id}: {e}")
            return None

    # ─── Операции (для дивидендов) ────────────────────────────────────────────

    async def get_operations(
        self,
        from_date: datetime,
        to_date: datetime,
        operation_types: list[str] | None = None,
    ) -> list[dict]:
        """Получить список операций по счёту."""
        if not self.is_available:
            return []

        try:
            from tinkoff.invest import AsyncClient
            async with AsyncClient(self.token) as client:
                if self.use_sandbox:
                    resp = await client.sandbox.get_sandbox_operations(
                        account_id=self._account_id,
                        from_=from_date,
                        to=to_date,
                    )
                else:
                    resp = await client.operations.get_operations(
                        account_id=self._account_id,
                        from_=from_date,
                        to=to_date,
                    )

                result = []
                for op in resp.operations:
                    op_type = str(op.operation_type)
                    if operation_types and not any(t in op_type for t in operation_types):
                        continue
                    result.append({
                        "id": op.id,
                        "type": op_type,
                        "figi": op.figi,
                        "date": op.date,
                        "amount": _money_to_float(op.payment),
                        "currency": op.currency,
                        "quantity": op.quantity,
                        "price": _money_to_float(op.price),
                    })
                return result
        except Exception as e:
            logger.error(f"Ошибка получения операций: {e}")
            return []

    # ─── Портфель ─────────────────────────────────────────────────────────────

    async def get_portfolio_value(self) -> dict:
        """Получить суммарную стоимость портфеля."""
        if not self.is_available:
            return {"total_amount": 0.0, "expected_yield": 0.0}

        try:
            from tinkoff.invest import AsyncClient
            async with AsyncClient(self.token) as client:
                if self.use_sandbox:
                    resp = await client.sandbox.get_sandbox_portfolio(
                        account_id=self._account_id
                    )
                else:
                    resp = await client.operations.get_portfolio(
                        account_id=self._account_id
                    )

                return {
                    "total_amount": _money_to_float(resp.total_amount_portfolio),
                    "expected_yield": _money_to_float(resp.expected_yield),
                    "positions": [
                        {
                            "figi": p.figi,
                            "quantity": p.quantity.units,
                            "current_price": _money_to_float(p.current_price),
                            "avg_buy_price": _money_to_float(p.average_buy_price),
                            "current_nkd": _money_to_float(p.current_nkd) if hasattr(p, 'current_nkd') else 0,
                            "expected_yield": _money_to_float(p.expected_yield),
                        }
                        for p in resp.positions
                    ]
                }
        except Exception as e:
            logger.error(f"Ошибка получения портфеля: {e}")
            return {"total_amount": 0.0, "expected_yield": 0.0, "positions": []}

    # ─── Заглушки для симуляции ───────────────────────────────────────────────

    def _figi_to_ticker(self, figi: str) -> str | None:
        """Конвертирует FIGI → тикер используя кэш или встроенную таблицу."""
        # Сначала смотрим в кэше
        if figi in self._figi_cache:
            return self._figi_cache[figi].get("ticker")
        # Встроенная таблица для основных инструментов
        figi_map = {
            "BBG004730N88": "SBER",
            "BBG004731032": "LKOH",
            "BBG004730ZJ9": "GAZP",
            "BBG00475KKY8": "NVTK",
            "BBG004731354": "ROSN",
            "TCS109029557": "YDEX",
            "BBG004RVFCY3": "MGNT",
            "BBG000R608Y3": "MTSS",
            "BBG004731489": "TATN",
            "BBG004731996": "GMKN",
            "BBG333333333": "TMOS",
            "BBG222222222": "EQMX",
        }
        return figi_map.get(figi)

    def _mock_instrument(self, ticker: str) -> dict:
        """Фиктивный инструмент для симуляции."""
        mock_data = {
            "SBER": {"figi": "BBG004730N88", "name": "Сбербанк", "lot": 10},
            "LKOH": {"figi": "BBG004731032", "name": "ЛУКОЙЛ", "lot": 1},
            "GAZP": {"figi": "BBG004730ZJ9", "name": "Газпром", "lot": 10},
            "NVTK": {"figi": "BBG00475KKY8", "name": "Новатэк", "lot": 1},
            "ROSN": {"figi": "BBG004731354", "name": "Роснефть", "lot": 1},
            "YDEX": {"figi": "TCS109029557", "name": "Яндекс", "lot": 1},
            "MGNT": {"figi": "BBG004RVFCY3", "name": "Магнит", "lot": 1},
            "TMOS": {"figi": "BBG333333333", "name": "TMOS БПИФ", "lot": 1},
        }
        base = mock_data.get(ticker, {})
        return {
            "figi": base.get("figi", f"MOCK_{ticker}"),
            "ticker": ticker,
            "name": base.get("name", ticker),
            "lot": base.get("lot", 1),
            "currency": "rub",
            "class_code": "TQBR",
        }

    def _mock_price(self, figi: str) -> float:
        """Запасная цена если MOEX недоступен."""
        import random
        mock_prices = {
            "BBG004730N88": 280.0,   # SBER
            "BBG004731032": 7200.0,  # LKOH
            "BBG004730ZJ9": 155.0,   # GAZP
            "BBG00475KKY8": 1200.0,  # NVTK
            "BBG004731354": 520.0,   # ROSN
        }
        base = mock_prices.get(figi, 100.0)
        return round(base * (1 + random.uniform(-0.01, 0.01)), 2)

    def _mock_order(
        self, figi: str, lots: int, direction: str, price: float | None = None, is_limit: bool = False
    ) -> dict:
        """Фиктивный ордер для симуляции."""
        mock_price = price or self._mock_price(figi)
        return {
            "order_id": f"MOCK_{figi}_{int(datetime.utcnow().timestamp())}",
            "status": "filled" if not is_limit else "pending",
            "price": mock_price,
            "lots": lots,
            "amount": mock_price * lots,
            "commission": round(mock_price * lots * 0.003, 2),  # 0.3% условно
        }

    def _mock_candles(self, figi: str, days: int) -> list[dict]:
        """Фиктивные свечи для симуляции."""
        import random
        base_price = self._mock_price(figi)
        candles = []
        now = datetime.utcnow()
        for i in range(days, 0, -1):
            t = now - timedelta(days=i)
            change = random.uniform(-0.03, 0.03)
            close = round(base_price * (1 + change), 2)
            candles.append({
                "time": t,
                "open": round(base_price, 2),
                "high": round(base_price * 1.02, 2),
                "low": round(base_price * 0.98, 2),
                "close": close,
                "volume": random.randint(100000, 5000000),
            })
            base_price = close
        return candles


# ─── Singleton ────────────────────────────────────────────────────────────────

_client_instance: TinkoffClient | None = None


async def get_client() -> TinkoffClient:
    """Получить (создать если нет) единственный экземпляр клиента."""
    global _client_instance
    if _client_instance is None:
        _client_instance = TinkoffClient(
            token=Config.TINKOFF_TOKEN,
            use_sandbox=Config.USE_SANDBOX,
            account_id=Config.TINKOFF_ACCOUNT_ID,
        )
        await _client_instance.initialize()
    return _client_instance

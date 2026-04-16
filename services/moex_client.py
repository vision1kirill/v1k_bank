"""
Клиент Московской биржи (MOEX) — бесплатный публичный REST API без авторизации.
Используется для получения реальных цен и исторических данных по российским акциям.

Документация: https://iss.moex.com/iss/reference/
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://iss.moex.com/iss"
TIMEOUT = 10.0

# Некоторые тикеры торгуются на доске TQTF (ETF/БПИФ), остальные на TQBR
ETF_TICKERS = {"TMOS", "EQMX", "SBMX", "VTBX", "FXRL"}


def _board_for(ticker: str) -> str:
    return "TQTF" if ticker.upper() in ETF_TICKERS else "TQBR"


async def get_last_price(ticker: str) -> Optional[float]:
    """
    Получить последнюю цену инструмента с MOEX.
    Возвращает float или None если запрос не удался.
    """
    ticker = ticker.upper()
    board = _board_for(ticker)
    url = (
        f"{BASE_URL}/engines/stock/markets/shares/boards/{board}"
        f"/securities/{ticker}.json"
        "?iss.meta=off&iss.only=marketdata"
        "&marketdata.columns=SECID,LAST,OPEN,LCURRENTPRICE"
    )
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

        rows = data.get("marketdata", {}).get("data", [])
        cols = data.get("marketdata", {}).get("columns", [])
        if not rows or not cols:
            return None

        row = rows[0]
        row_dict = dict(zip(cols, row))

        # LAST может быть None в нерабочее время — берём LCURRENTPRICE как fallback
        price = row_dict.get("LAST") or row_dict.get("LCURRENTPRICE")
        if price is not None:
            return float(price)
        return None

    except Exception as e:
        logger.warning(f"MOEX: не удалось получить цену {ticker}: {e}")
        return None


async def get_candles(ticker: str, days: int = 30) -> list[dict]:
    """
    Получить дневные свечи с MOEX за последние N дней.
    Возвращает list[{time, open, high, low, close, volume}]
    """
    ticker = ticker.upper()
    board = _board_for(ticker)
    till = datetime.now()
    from_ = till - timedelta(days=days + 5)  # чуть больше на случай выходных

    url = (
        f"{BASE_URL}/history/engines/stock/markets/shares/boards/{board}"
        f"/securities/{ticker}/candles.json"
        f"?from={from_.strftime('%Y-%m-%d')}"
        f"&till={till.strftime('%Y-%m-%d')}"
        f"&interval=24"           # дневные свечи
        f"&iss.meta=off"
    )
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

        cols = data.get("candles", {}).get("columns", [])
        rows = data.get("candles", {}).get("data", [])
        if not rows or not cols:
            return []

        candles = []
        for row in rows[-days:]:  # берём последние N дней
            d = dict(zip(cols, row))
            candles.append({
                "time": datetime.fromisoformat(d["begin"]) if d.get("begin") else None,
                "open": float(d.get("open") or 0),
                "high": float(d.get("high") or 0),
                "low": float(d.get("low") or 0),
                "close": float(d.get("close") or 0),
                "volume": int(d.get("volume") or 0),
            })
        return candles

    except Exception as e:
        logger.warning(f"MOEX: не удалось получить свечи {ticker}: {e}")
        return []


async def get_multiple_prices(tickers: list[str]) -> dict[str, Optional[float]]:
    """
    Получить цены сразу для нескольких тикеров одним запросом (только TQBR).
    Возвращает {ticker: price}
    """
    # Разделяем ETF и акции
    shares = [t.upper() for t in tickers if t.upper() not in ETF_TICKERS]
    etfs = [t.upper() for t in tickers if t.upper() in ETF_TICKERS]
    result = {}

    for board, ticker_list in [("TQBR", shares), ("TQTF", etfs)]:
        if not ticker_list:
            continue
        securities_param = ",".join(ticker_list)
        url = (
            f"{BASE_URL}/engines/stock/markets/shares/boards/{board}/securities.json"
            f"?securities={securities_param}"
            f"&iss.meta=off&iss.only=marketdata"
            f"&marketdata.columns=SECID,LAST,LCURRENTPRICE"
        )
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()

            cols = data.get("marketdata", {}).get("columns", [])
            rows = data.get("marketdata", {}).get("data", [])
            for row in rows:
                d = dict(zip(cols, row))
                secid = d.get("SECID", "")
                price = d.get("LAST") or d.get("LCURRENTPRICE")
                if secid and price is not None:
                    result[secid] = float(price)
        except Exception as e:
            logger.warning(f"MOEX bulk: ошибка для {board}: {e}")

    return result

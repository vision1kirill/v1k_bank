"""
Стратегия реинвестирования дивидендов.

Логика:
- Следим за поступлением дивидендов на счёт
- При поступлении — автоматически докупаем те же акции
- Реализует эффект сложного процента
"""
import logging
from datetime import datetime, timezone, timedelta
from math import floor

from sqlalchemy.ext.asyncio import AsyncSession

from database import (
    Strategy, StrategyStatus, TradeDirection,
    record_trade, update_or_create_position
)
from services.tinkoff_client import TinkoffClient

logger = logging.getLogger(__name__)

# Строка в типе операции которая означает дивиденды
DIVIDEND_OP_KEYWORDS = ["DIVIDEND", "COUPON"]


def build_dividend_config(tickers_with_figis: list[dict]) -> dict:
    """
    Создаёт начальный конфиг стратегии дивидендов.

    tickers_with_figis: [{"ticker": "SBER", "figi": "BBG004730N88"}, ...]
    """
    return {
        "tickers": tickers_with_figis,
        "last_check_date": (datetime.utcnow() - timedelta(days=1)).isoformat(),
        "total_dividends_received": 0.0,
        "total_reinvested": 0.0,
    }


async def check_and_reinvest_dividends(
    session: AsyncSession,
    strategy: Strategy,
    client: TinkoffClient,
    notify_func=None,
) -> int:
    """
    Проверяет поступление дивидендов и реинвестирует их.
    Запускается ежедневно.

    Возвращает количество реинвестированных дивидендов.
    """
    config = strategy.config
    tickers = config.get("tickers", [])

    if not tickers:
        logger.warning(f"Dividend {strategy.id}: нет тикеров для отслеживания")
        return 0

    # Определяем период проверки
    last_check_str = config.get("last_check_date")
    if last_check_str:
        from_date = datetime.fromisoformat(last_check_str).replace(tzinfo=timezone.utc)
    else:
        from_date = datetime.now(timezone.utc) - timedelta(days=30)

    to_date = datetime.now(timezone.utc)

    # Получаем операции
    operations = await client.get_operations(
        from_date=from_date,
        to_date=to_date,
        operation_types=DIVIDEND_OP_KEYWORDS,
    )

    if not operations:
        logger.debug(f"Dividend {strategy.id}: новых дивидендов нет")
        # Обновляем дату проверки
        config["last_check_date"] = to_date.isoformat()
        strategy.set_config(config)
        await session.commit()
        return 0

    reinvested_count = 0
    figi_set = {t["figi"] for t in tickers}

    for op in operations:
        if op["figi"] not in figi_set:
            continue

        dividend_amount = op["amount"]
        figi = op["figi"]
        op_type = op["type"]

        # Находим тикер
        ticker_info = next((t for t in tickers if t["figi"] == figi), None)
        if not ticker_info:
            continue

        ticker = ticker_info["ticker"]
        logger.info(
            f"Dividend {strategy.id}: получен {op_type} "
            f"{dividend_amount:.2f}₽ по {ticker}"
        )

        # Проверяем хватает ли денег (дивиденд + остаток бюджета)
        available = dividend_amount + strategy.remaining_budget
        if available <= 0:
            continue

        # Получаем инструмент и цену
        instrument = await client.find_instrument(ticker)
        if not instrument:
            continue

        current_price = await client.get_last_price(figi)
        if not current_price or current_price <= 0:
            continue

        lot_size = instrument.get("lot", 1)
        price_per_lot = current_price * lot_size
        lots = floor(dividend_amount / price_per_lot)

        if lots <= 0:
            # Дивиденд слишком мал для 1 лота — накапливаем
            logger.debug(
                f"Dividend {strategy.id}: дивиденд {dividend_amount:.2f}₽ < 1 лот {price_per_lot:.2f}₽. "
                f"Накапливаем."
            )
            if notify_func:
                await notify_func(
                    strategy.user_id,
                    f"💸 Получен дивиденд по {ticker}: {dividend_amount:.2f}₽\n"
                    f"Недостаточно для покупки лота ({price_per_lot:.2f}₽). "
                    f"Средства накоплены в бюджете стратегии."
                )
            # Добавляем в бюджет для будущих покупок
            strategy.allocated_budget += dividend_amount
            await session.commit()
            reinvested_count += 1
            continue

        # Размещаем рыночный ордер
        order_result = await client.place_market_order(
            figi=figi,
            lots=lots,
            direction="buy",
            strategy_id=strategy.id,
        )

        if not order_result:
            logger.error(f"Dividend {strategy.id}: ордер не исполнен для {ticker}")
            continue

        exec_price = order_result["price"] or current_price
        exec_amount = exec_price * lots * lot_size
        commission = order_result.get("commission", 0.0)

        await record_trade(
            session=session,
            strategy_id=strategy.id,
            direction=TradeDirection.BUY,
            ticker=ticker,
            figi=figi,
            quantity=lots * lot_size,
            lot_size=lot_size,
            price=exec_price,
            amount=exec_amount,
            commission=commission,
            order_id=order_result.get("order_id"),
            note=f"Реинвест дивиденда {dividend_amount:.2f}₽",
        )

        await update_or_create_position(
            session=session,
            strategy_id=strategy.id,
            ticker=ticker,
            figi=figi,
            quantity_delta=lots * lot_size,
            price=exec_price,
        )

        # Обновляем статистику
        config["total_dividends_received"] = config.get("total_dividends_received", 0) + dividend_amount
        config["total_reinvested"] = config.get("total_reinvested", 0) + exec_amount

        if notify_func:
            await notify_func(
                strategy.user_id,
                f"♻️ Дивиденд реинвестирован!\n\n"
                f"🏷️ Акция: {ticker}\n"
                f"💸 Получено дивидендов: {dividend_amount:.2f}₽\n"
                f"📦 Докуплено: {lots} лот(а) = {lots * lot_size} шт.\n"
                f"💰 Цена: {exec_price:.2f}₽\n"
                f"💵 Сумма: {exec_amount:.2f}₽\n"
                f"🔄 Всего реинвестировано по стратегии: "
                f"{config['total_reinvested']:.2f}₽"
            )

        reinvested_count += 1

    # Обновляем дату последней проверки
    config["last_check_date"] = to_date.isoformat()
    strategy.set_config(config)
    strategy.updated_at = datetime.utcnow()
    await session.commit()

    return reinvested_count

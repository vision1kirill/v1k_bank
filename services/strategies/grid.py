"""
Стратегия Grid Trading (Сеточная торговля).

Логика:
- Задаём диапазон цены (low, high) и шаг (step)
- На каждом уровне ниже текущей цены — лимитный ордер на покупку
- На каждом уровне выше текущей цены — лимитный ордер на продажу
- Когда покупка исполнена → ставим продажу на уровень выше
- Когда продажа исполнена → ставим покупку на уровень ниже
- Зарабатываем на колебаниях внутри диапазона
"""
import logging
from datetime import datetime
from math import floor
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import (
    Strategy, Position, StrategyStatus, TradeDirection,
    record_trade, update_or_create_position
)
from services.tinkoff_client import TinkoffClient

logger = logging.getLogger(__name__)

# Статусы уровней сетки
LEVEL_EMPTY = "empty"             # нет активного ордера
LEVEL_BUY_PENDING = "buy_pending"   # стоит лимитная покупка
LEVEL_BOUGHT = "bought"           # куплено, ждём продажи
LEVEL_SELL_PENDING = "sell_pending"  # стоит лимитная продажа


def build_grid_config(
    ticker: str,
    figi: str,
    price_low: float,
    price_high: float,
    step: float,
    amount_per_level: float,
    lot_size: int,
) -> dict:
    """
    Создаёт начальный конфиг Grid стратегии.
    Строит массив уровней от low до high с шагом step.
    """
    levels = []
    price = price_low
    while price <= price_high + 0.001:
        price_rounded = round(price, 2)
        lots = max(1, floor(amount_per_level / (price_rounded * lot_size)))
        levels.append({
            "price": price_rounded,
            "status": LEVEL_EMPTY,
            "order_id": None,
            "lots": lots,
        })
        price += step

    return {
        "ticker": ticker.upper(),
        "figi": figi,
        "price_low": price_low,
        "price_high": price_high,
        "step": step,
        "amount_per_level": amount_per_level,
        "lot_size": lot_size,
        "levels": levels,
        "initialized": False,
    }


async def initialize_grid(
    session: AsyncSession,
    strategy: Strategy,
    client: TinkoffClient,
    notify_func=None,
) -> bool:
    """
    Первичная расстановка ордеров Grid стратегии.
    Вызывается один раз при создании или сбросе стратегии.
    """
    config = strategy.config
    figi = config["figi"]
    ticker = config["ticker"]
    levels = config["levels"]
    lot_size = config.get("lot_size", 1)

    # Текущая цена
    current_price = await client.get_last_price(figi)
    if not current_price:
        logger.error(f"Grid {strategy.id}: не удалось получить цену {ticker}")
        return False

    logger.info(f"Grid {strategy.id}: инициализация. Текущая цена {ticker}: {current_price:.2f}")

    # Расставляем ордера
    for i, level in enumerate(levels):
        level_price = level["price"]

        if level_price < current_price:
            # Ниже рынка → лимитная покупка
            order_result = await client.place_limit_order(
                figi=figi,
                lots=level["lots"],
                direction="buy",
                price=level_price,
                strategy_id=strategy.id,
            )
            if order_result:
                levels[i]["status"] = LEVEL_BUY_PENDING
                levels[i]["order_id"] = order_result["order_id"]
                logger.debug(f"Grid {strategy.id}: BUY @ {level_price} (order {order_result['order_id']})")

        elif level_price > current_price:
            # Выше рынка → проверяем есть ли позиция для продажи
            # При первом запуске продажи не ставим (нет позиции)
            levels[i]["status"] = LEVEL_EMPTY

    config["levels"] = levels
    config["initialized"] = True
    strategy.set_config(config)
    strategy.updated_at = datetime.utcnow()
    await session.commit()

    if notify_func:
        buy_count = sum(1 for l in levels if l["status"] == LEVEL_BUY_PENDING)
        msg = (
            f"🕸️ Grid «{strategy.name}» инициализирована!\n\n"
            f"🏷️ Тикер: {ticker}\n"
            f"📊 Диапазон: {config['price_low']:.2f}₽ — {config['price_high']:.2f}₽\n"
            f"📏 Шаг: {config['step']:.2f}₽\n"
            f"🛒 Лимитных покупок выставлено: {buy_count}\n"
            f"📈 Текущая цена: {current_price:.2f}₽"
        )
        await notify_func(strategy.user_id, msg)

    return True


async def check_grid_orders(
    session: AsyncSession,
    strategy: Strategy,
    client: TinkoffClient,
    notify_func=None,
) -> int:
    """
    Проверяет исполнение ордеров Grid стратегии.
    Запускается по таймеру (каждые N минут).

    Возвращает количество исполненных ордеров за этот цикл.
    """
    config = strategy.config
    if not config.get("initialized"):
        await initialize_grid(session, strategy, client, notify_func)
        return 0

    figi = config["figi"]
    ticker = config["ticker"]
    levels = config["levels"]
    lot_size = config.get("lot_size", 1)
    step = config["step"]
    price_low = config["price_low"]
    price_high = config["price_high"]

    executed_count = 0

    for i, level in enumerate(levels):
        if level["status"] not in (LEVEL_BUY_PENDING, LEVEL_SELL_PENDING):
            continue

        order_id = level.get("order_id")
        if not order_id:
            continue

        # Проверяем статус ордера
        order_status = await client.get_order_status(order_id)
        if not order_status:
            continue

        status_str = order_status.get("status", "")
        is_filled = "FILL" in status_str.upper() or status_str == "filled"

        if not is_filled:
            continue

        # ─── Ордер исполнен ─────────────────────────────────────────────
        exec_price = order_status.get("price") or level["price"]
        filled_lots = order_status.get("filled_lots", level["lots"])
        exec_amount = exec_price * filled_lots * lot_size

        if level["status"] == LEVEL_BUY_PENDING:
            # Покупка исполнена → записываем, ставим продажу выше
            await record_trade(
                session=session,
                strategy_id=strategy.id,
                direction=TradeDirection.BUY,
                ticker=ticker,
                figi=figi,
                quantity=filled_lots * lot_size,
                lot_size=lot_size,
                price=exec_price,
                amount=exec_amount,
                order_id=order_id,
                note=f"Grid BUY @ {level['price']}",
            )
            await update_or_create_position(
                session=session,
                strategy_id=strategy.id,
                ticker=ticker,
                figi=figi,
                quantity_delta=filled_lots * lot_size,
                price=exec_price,
            )

            levels[i]["status"] = LEVEL_BOUGHT
            levels[i]["order_id"] = None

            # Ставим лимитную продажу на уровень выше
            sell_price = round(level["price"] + step, 2)
            if sell_price <= price_high:
                sell_result = await client.place_limit_order(
                    figi=figi,
                    lots=filled_lots,
                    direction="sell",
                    price=sell_price,
                    strategy_id=strategy.id,
                )
                if sell_result:
                    # Находим уровень продажи и обновляем его
                    for j, lv in enumerate(levels):
                        if abs(lv["price"] - sell_price) < 0.01:
                            levels[j]["status"] = LEVEL_SELL_PENDING
                            levels[j]["order_id"] = sell_result["order_id"]
                            break

            executed_count += 1
            if notify_func:
                await notify_func(
                    strategy.user_id,
                    f"✅ Grid «{strategy.name}»: куплено {filled_lots} лот(а) {ticker} "
                    f"@ {exec_price:.2f}₽\n"
                    f"Выставлена продажа @ {sell_price:.2f}₽"
                )

        elif level["status"] == LEVEL_SELL_PENDING:
            # Продажа исполнена → записываем, ставим покупку ниже
            await record_trade(
                session=session,
                strategy_id=strategy.id,
                direction=TradeDirection.SELL,
                ticker=ticker,
                figi=figi,
                quantity=filled_lots * lot_size,
                lot_size=lot_size,
                price=exec_price,
                amount=exec_amount,
                order_id=order_id,
                note=f"Grid SELL @ {level['price']}",
            )
            await update_or_create_position(
                session=session,
                strategy_id=strategy.id,
                ticker=ticker,
                figi=figi,
                quantity_delta=-(filled_lots * lot_size),
                price=exec_price,
            )

            levels[i]["status"] = LEVEL_EMPTY
            levels[i]["order_id"] = None

            # Ставим покупку на уровень ниже
            buy_price = round(level["price"] - step, 2)
            if buy_price >= price_low:
                buy_result = await client.place_limit_order(
                    figi=figi,
                    lots=filled_lots,
                    direction="buy",
                    price=buy_price,
                    strategy_id=strategy.id,
                )
                if buy_result:
                    for j, lv in enumerate(levels):
                        if abs(lv["price"] - buy_price) < 0.01:
                            levels[j]["status"] = LEVEL_BUY_PENDING
                            levels[j]["order_id"] = buy_result["order_id"]
                            break

            executed_count += 1
            if notify_func:
                # Считаем прибыль от этого цикла (грубо — шаг * лоты)
                profit = step * filled_lots * lot_size
                await notify_func(
                    strategy.user_id,
                    f"💰 Grid «{strategy.name}»: продано {filled_lots} лот(а) {ticker} "
                    f"@ {exec_price:.2f}₽\n"
                    f"Прибыль от цикла: ~{profit:.2f}₽\n"
                    f"Выставлена покупка @ {buy_price:.2f}₽"
                )

    if executed_count > 0:
        config["levels"] = levels
        strategy.set_config(config)
        strategy.updated_at = datetime.utcnow()
        await session.commit()

    return executed_count


async def cancel_all_grid_orders(
    session: AsyncSession,
    strategy: Strategy,
    client: TinkoffClient,
) -> int:
    """Отменяет все активные ордера Grid стратегии. Возвращает количество отменённых."""
    config = strategy.config
    levels = config.get("levels", [])
    cancelled = 0

    for i, level in enumerate(levels):
        if level.get("order_id"):
            success = await client.cancel_order(level["order_id"])
            if success:
                levels[i]["status"] = LEVEL_EMPTY
                levels[i]["order_id"] = None
                cancelled += 1

    config["levels"] = levels
    config["initialized"] = False
    strategy.set_config(config)
    await session.commit()

    return cancelled

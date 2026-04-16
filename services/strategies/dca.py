"""
Стратегия DCA (Dollar Cost Averaging / Усреднение стоимости).

Логика:
- Покупаем на фиксированную сумму по расписанию (еженедельно/ежемесячно)
- На просадках автоматически берём больше лотов, на росте — меньше
- Строго соблюдаем выделенный бюджет
"""
import logging
from datetime import datetime, date, timedelta
from math import floor

from sqlalchemy.ext.asyncio import AsyncSession

from database import (
    Strategy, StrategyStatus, TradeDirection,
    record_trade, update_or_create_position
)
from services.tinkoff_client import TinkoffClient

logger = logging.getLogger(__name__)


async def execute_dca(
    session: AsyncSession,
    strategy: Strategy,
    client: TinkoffClient,
    notify_func=None,  # async callable(user_id, message)
) -> bool:
    """
    Исполняет DCA покупку если пришло время.

    Возвращает True если сделка выполнена.
    """
    config = strategy.config
    ticker = config.get("ticker", "")
    figi = config.get("figi", "")
    amount_per_buy = float(config.get("amount_per_buy", 0))
    frequency = config.get("frequency", "weekly")  # weekly | monthly

    # ─── Проверяем пришло ли время ─────────────────────────────────────────
    today = date.today()
    next_buy_str = config.get("next_buy_date")
    if next_buy_str:
        next_buy = date.fromisoformat(next_buy_str)
        if today < next_buy:
            logger.debug(f"DCA {strategy.id} ({ticker}): следующая покупка {next_buy}, сегодня {today}")
            return False

    # ─── Проверяем бюджет ──────────────────────────────────────────────────
    if strategy.remaining_budget < amount_per_buy:
        msg = (
            f"⚠️ DCA стратегия «{strategy.name}»\n"
            f"Недостаточно бюджета для покупки.\n"
            f"Нужно: {amount_per_buy:.2f}₽, осталось: {strategy.remaining_budget:.2f}₽\n"
            f"Стратегия приостановлена. Пополните бюджет через /strategies."
        )
        logger.warning(f"DCA {strategy.id}: недостаточно бюджета")
        if notify_func:
            await notify_func(strategy.user_id, msg)
        strategy.status = StrategyStatus.PAUSED
        await session.commit()
        return False

    # ─── Получаем инструмент и цену ────────────────────────────────────────
    instrument = await client.find_instrument(ticker)
    if not instrument:
        logger.error(f"DCA {strategy.id}: инструмент {ticker} не найден")
        return False

    figi = instrument["figi"]
    lot_size = instrument.get("lot", 1)

    current_price = await client.get_last_price(figi)
    if not current_price or current_price <= 0:
        logger.error(f"DCA {strategy.id}: не удалось получить цену {ticker}")
        return False

    # ─── Считаем количество лотов ──────────────────────────────────────────
    # Покупаем на amount_per_buy, но не больше remaining_budget
    effective_amount = min(amount_per_buy, strategy.remaining_budget)
    price_per_lot = current_price * lot_size
    lots = floor(effective_amount / price_per_lot)

    if lots <= 0:
        logger.warning(
            f"DCA {strategy.id}: сумма {effective_amount:.2f}₽ "
            f"меньше цены 1 лота {price_per_lot:.2f}₽ ({ticker})"
        )
        if notify_func:
            await notify_func(
                strategy.user_id,
                f"⚠️ DCA «{strategy.name}»: сумма покупки {effective_amount:.2f}₽ меньше "
                f"стоимости 1 лота {price_per_lot:.2f}₽ ({ticker}). Пополните бюджет."
            )
        return False

    actual_amount = price_per_lot * lots

    # ─── Размещаем ордер ───────────────────────────────────────────────────
    logger.info(
        f"DCA {strategy.id}: покупка {lots} лотов {ticker} "
        f"по ~{current_price:.2f}₽, сумма ~{actual_amount:.2f}₽"
    )
    order_result = await client.place_market_order(
        figi=figi,
        lots=lots,
        direction="buy",
        strategy_id=strategy.id,
    )

    if not order_result:
        logger.error(f"DCA {strategy.id}: ордер не исполнен")
        return False

    # ─── Записываем сделку ─────────────────────────────────────────────────
    exec_price = order_result["price"] if order_result["price"] > 0 else current_price
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
        note=f"DCA {frequency}",
    )

    await update_or_create_position(
        session=session,
        strategy_id=strategy.id,
        ticker=ticker,
        figi=figi,
        quantity_delta=lots * lot_size,
        price=exec_price,
    )

    # ─── Обновляем конфиг стратегии ────────────────────────────────────────
    next_buy_date = _calc_next_buy_date(today, frequency)
    config["next_buy_date"] = next_buy_date.isoformat()
    config["last_buy_date"] = today.isoformat()
    config["figi"] = figi
    strategy.set_config(config)
    strategy.updated_at = datetime.utcnow()
    await session.commit()

    # ─── Уведомление ───────────────────────────────────────────────────────
    if notify_func:
        mode = "🏖️ Симуляция" if not client.is_available else ("🏷️ Sandbox" if client.use_sandbox else "✅ Реальная сделка")
        msg = (
            f"📈 DCA «{strategy.name}» выполнена!\n\n"
            f"🏷️ Тикер: {ticker}\n"
            f"📦 Куплено: {lots} лот(а) = {lots * lot_size} шт.\n"
            f"💰 Цена: {exec_price:.2f}₽\n"
            f"💵 Сумма: {exec_amount:.2f}₽\n"
            f"🏦 Комиссия: {commission:.2f}₽\n"
            f"📅 Следующая покупка: {next_buy_date.strftime('%d.%m.%Y')}\n"
            f"💼 Остаток бюджета: {strategy.remaining_budget - exec_amount - commission:.2f}₽\n"
            f"{mode}"
        )
        await notify_func(strategy.user_id, msg)

    return True


def _calc_next_buy_date(from_date: date, frequency: str) -> date:
    if frequency == "weekly":
        return from_date + timedelta(weeks=1)
    elif frequency == "monthly":
        # Следующий месяц, тот же день
        if from_date.month == 12:
            return from_date.replace(year=from_date.year + 1, month=1)
        else:
            try:
                return from_date.replace(month=from_date.month + 1)
            except ValueError:
                # Например 31 марта → 30 апреля
                import calendar
                last_day = calendar.monthrange(from_date.year, from_date.month + 1)[1]
                return from_date.replace(month=from_date.month + 1, day=last_day)
    return from_date + timedelta(weeks=1)


def build_dca_config(
    ticker: str,
    figi: str,
    amount_per_buy: float,
    frequency: str,
    start_date: date | None = None,
) -> dict:
    """Создаёт начальный конфиг DCA стратегии."""
    start = start_date or date.today()
    return {
        "ticker": ticker.upper(),
        "figi": figi,
        "amount_per_buy": amount_per_buy,
        "frequency": frequency,
        "next_buy_date": start.isoformat(),
        "last_buy_date": None,
    }

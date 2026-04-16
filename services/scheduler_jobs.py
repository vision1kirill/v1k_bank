"""
Периодические задачи бота (запускаются через PTB JobQueue).

Расписание:
- Каждые 5 минут: проверка исполнения Grid ордеров
- Каждый день в 10:00 МСК: запуск DCA стратегий + дивиденды + анализ рынка
- Каждый понедельник в 11:00 МСК: еженедельный отчёт
"""
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import select
from telegram.ext import ContextTypes

from database import (
    AsyncSessionLocal, DailyAnalysis, WeeklyReport,
    get_all_active_strategies, StrategyType, Strategy
)
from services.market_analysis import run_daily_analysis, generate_position_summary
from services.strategies import (
    execute_dca, check_grid_orders, check_and_reinvest_dividends
)
from services.tinkoff_client import get_client

if TYPE_CHECKING:
    from telegram.ext import Application

logger = logging.getLogger(__name__)


async def _notify_user(context: ContextTypes.DEFAULT_TYPE, user_id: int, message: str) -> None:
    """Отправляет уведомление конкретному пользователю."""
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=message,
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления пользователю {user_id}: {e}")


# ─── Grid: проверка ордеров каждые N минут ────────────────────────────────────

async def job_check_grid_orders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Проверяет исполнение Grid ордеров для всех активных Grid стратегий."""
    logger.debug("Запуск job_check_grid_orders")
    try:
        client = await get_client()
        async with AsyncSessionLocal() as session:
            strategies = await get_all_active_strategies(session)

        grid_strategies = [s for s in strategies if s.type == StrategyType.GRID]
        if not grid_strategies:
            return

        for strategy in grid_strategies:
            try:
                async with AsyncSessionLocal() as session:
                    # Переподключаем к текущей сессии
                    result = await session.execute(
                        select(Strategy).where(Strategy.id == strategy.id)
                    )
                    strategy_fresh = result.scalar_one_or_none()
                    if not strategy_fresh:
                        continue

                    def make_notify(strat):
                        async def notify(user_id: int, msg: str) -> None:
                            await _notify_user(context, user_id, msg)
                        return notify

                    executed = await check_grid_orders(
                        session=session,
                        strategy=strategy_fresh,
                        client=client,
                        notify_func=make_notify(strategy_fresh),
                    )
                    if executed > 0:
                        logger.info(f"Grid {strategy.id}: исполнено {executed} ордеров")

            except Exception as e:
                logger.error(f"Ошибка проверки Grid стратегии {strategy.id}: {e}", exc_info=True)

    except Exception as e:
        logger.error(f"Критическая ошибка job_check_grid_orders: {e}", exc_info=True)


# ─── Ежедневные задачи ────────────────────────────────────────────────────────

async def job_daily_tasks(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Ежедневные задачи:
    1. Исполнение DCA стратегий
    2. Проверка дивидендов
    3. Анализ рынка и рассылка пользователям
    """
    logger.info(f"Запуск ежедневных задач: {datetime.now()}")
    try:
        client = await get_client()
        async with AsyncSessionLocal() as session:
            all_strategies = await get_all_active_strategies(session)

        # ── Группируем по типу ─────────────────────────────────────────────
        dca_strategies = [s for s in all_strategies if s.type == StrategyType.DCA]
        div_strategies = [s for s in all_strategies if s.type == StrategyType.DIVIDEND]

        # ── 1. DCA ─────────────────────────────────────────────────────────
        for strategy in dca_strategies:
            try:
                async with AsyncSessionLocal() as session:
                    result = await session.execute(
                        select(Strategy).where(Strategy.id == strategy.id)
                    )
                    strategy_fresh = result.scalar_one_or_none()
                    if not strategy_fresh:
                        continue

                    async def notify_dca(user_id: int, msg: str) -> None:
                        await _notify_user(context, user_id, msg)

                    executed = await execute_dca(
                        session=session,
                        strategy=strategy_fresh,
                        client=client,
                        notify_func=notify_dca,
                    )
                    if executed:
                        logger.info(f"DCA {strategy.id}: покупка выполнена")

            except Exception as e:
                logger.error(f"Ошибка DCA {strategy.id}: {e}", exc_info=True)

        # ── 2. Дивиденды ───────────────────────────────────────────────────
        for strategy in div_strategies:
            try:
                async with AsyncSessionLocal() as session:
                    result = await session.execute(
                        select(Strategy).where(Strategy.id == strategy.id)
                    )
                    strategy_fresh = result.scalar_one_or_none()
                    if not strategy_fresh:
                        continue

                    async def notify_div(user_id: int, msg: str) -> None:
                        await _notify_user(context, user_id, msg)

                    reinvested = await check_and_reinvest_dividends(
                        session=session,
                        strategy=strategy_fresh,
                        client=client,
                        notify_func=notify_div,
                    )
                    if reinvested > 0:
                        logger.info(f"Dividend {strategy.id}: реинвестировано {reinvested}")

            except Exception as e:
                logger.error(f"Ошибка Dividend {strategy.id}: {e}", exc_info=True)

        # ── 3. Анализ рынка ────────────────────────────────────────────────
        await job_daily_analysis(context)

    except Exception as e:
        logger.error(f"Критическая ошибка job_daily_tasks: {e}", exc_info=True)


async def job_daily_analysis(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Запускает анализ рынка и рассылает всем пользователям.
    Если анализ уже делали сегодня — берём из кэша.
    """
    logger.info("Запуск ежедневного анализа рынка")
    today = date.today()

    try:
        client = await get_client()

        # Проверяем кэш
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(DailyAnalysis).where(DailyAnalysis.analysis_date == today)
            )
            cached = result.scalar_one_or_none()

        if cached:
            analysis_text = cached.content
            analysis_results = cached.recommendations
        else:
            analysis_text, analysis_results = await run_daily_analysis(client)
            async with AsyncSessionLocal() as session:
                new_analysis = DailyAnalysis(
                    analysis_date=today,
                    content=analysis_text,
                    recommendations_json=json.dumps(
                        analysis_results, ensure_ascii=False, default=str
                    ),
                )
                session.add(new_analysis)
                await session.commit()

        # Получаем всех пользователей
        from database import User
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(User).where(User.is_active == True)
            )
            users = result.scalars().all()

        # Рассылаем каждому
        for user in users:
            try:
                from database import TrackedPosition
                async with AsyncSessionLocal() as session:
                    result = await session.execute(
                        select(TrackedPosition)
                        .where(TrackedPosition.user_id == user.id)
                        .where(TrackedPosition.is_active == True)
                    )
                    tracked = result.scalars().all()

                # Разбиваем длинный текст
                chunks = _split_text(analysis_text, 4000)
                for chunk in chunks:
                    await _notify_user(context, user.telegram_id, chunk)

                # Персональная сводка по позициям
                if tracked:
                    pos_summary = await generate_position_summary(
                        client=client,
                        tracked_positions=tracked,
                        analysis_results=analysis_results if isinstance(analysis_results, list) else [],
                    )
                    if pos_summary:
                        await _notify_user(context, user.telegram_id, pos_summary)

            except Exception as e:
                logger.error(f"Ошибка рассылки анализа пользователю {user.telegram_id}: {e}")

    except Exception as e:
        logger.error(f"Критическая ошибка job_daily_analysis: {e}", exc_info=True)


# ─── Еженедельный отчёт ───────────────────────────────────────────────────────

async def job_weekly_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Формирует и рассылает еженедельный отчёт всем пользователям."""
    logger.info(f"Запуск еженедельного отчёта")

    try:
        from database import User
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(User).where(User.is_active == True)
            )
            users = result.scalars().all()

        if not users:
            return

        client = await get_client()

        for user in users:
            try:
                from database import get_user_strategies, get_strategy_positions, Trade
                async with AsyncSessionLocal() as session:
                    strategies = await get_user_strategies(session, user.id)

                if not strategies:
                    continue

                lines = [
                    f"📋 *ЕЖЕНЕДЕЛЬНЫЙ ОТЧЁТ*",
                    f"📅 {datetime.now().strftime('%d.%m.%Y')}",
                    "",
                ]

                total_invested = 0.0
                total_current = 0.0

                for strategy in strategies:
                    async with AsyncSessionLocal() as session:
                        positions = await get_strategy_positions(session, strategy.id)
                        week_ago = datetime.utcnow() - timedelta(days=7)
                        result = await session.execute(
                            select(Trade)
                            .where(Trade.strategy_id == strategy.id)
                            .where(Trade.created_at >= week_ago)
                        )
                        recent_trades = result.scalars().all()

                    type_emoji = {"DCA": "📈", "GRID": "🕸️", "DIVIDEND": "♻️"}.get(
                        strategy.type.value, "📊"
                    )
                    lines.append(f"\n{type_emoji} *{strategy.name}*")
                    lines.append(
                        f"Бюджет: {strategy.allocated_budget:.0f}₽ | "
                        f"Потрачено: {strategy.spent_budget:.0f}₽ | "
                        f"Остаток: {strategy.remaining_budget:.0f}₽"
                    )

                    strategy_current = 0.0
                    for pos in positions:
                        if pos.quantity <= 0:
                            continue
                        current_price = await client.get_last_price(pos.figi)
                        if current_price:
                            val = current_price * pos.quantity
                            strategy_current += val
                            pnl = val - pos.total_invested
                            pnl_pct = (pnl / pos.total_invested * 100) if pos.total_invested > 0 else 0
                            sign = "+" if pnl >= 0 else ""
                            emoji = "📈" if pnl >= 0 else "📉"
                            lines.append(
                                f"  {pos.ticker}: {pos.quantity}шт. = {val:.2f}₽ "
                                f"({sign}{pnl:.2f}₽ {sign}{pnl_pct:.1f}% {emoji})"
                            )

                    total_invested += strategy.spent_budget
                    total_current += strategy_current

                    trades_summary = f"Сделок за неделю: {len(recent_trades)}"
                    if recent_trades:
                        buy_total = sum(t.amount or 0 for t in recent_trades if t.direction.value == "buy")
                        if buy_total > 0:
                            trades_summary += f" | Куплено: {buy_total:.0f}₽"
                    lines.append(f"  📋 {trades_summary}")

                if total_invested > 0:
                    pnl = total_current - total_invested
                    pnl_pct = pnl / total_invested * 100
                    sign = "+" if pnl >= 0 else ""
                    lines += [
                        "",
                        "─" * 20,
                        f"*ИТОГО: {sign}{pnl:.2f}₽ ({sign}{pnl_pct:.1f}%)*",
                        f"Вложено: {total_invested:.2f}₽ | Сейчас: {total_current:.2f}₽",
                    ]

                for chunk in _split_text("\n".join(lines), 4000):
                    await _notify_user(context, user.telegram_id, chunk)

            except Exception as e:
                logger.error(f"Ошибка еженедельного отчёта для {user.telegram_id}: {e}")

    except Exception as e:
        logger.error(f"Критическая ошибка job_weekly_report: {e}", exc_info=True)


# ─── Утилиты ──────────────────────────────────────────────────────────────────

def _split_text(text: str, max_len: int) -> list[str]:
    """Разбивает текст на части по max_len символов."""
    if len(text) <= max_len:
        return [text]
    parts = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > max_len:
            if current:
                parts.append(current)
            current = line
        else:
            current += ("\n" if current else "") + line
    if current:
        parts.append(current)
    return parts


def setup_scheduler(app) -> None:
    """
    Регистрирует все периодические задачи в PTB JobQueue.
    Вызывается из main.py.
    """
    from config import Config
    from datetime import time as dt_time

    jq = app.job_queue

    # Grid — каждые N минут
    jq.run_repeating(
        job_check_grid_orders,
        interval=Config.GRID_CHECK_INTERVAL_SEC,
        first=60,  # первый запуск через 1 минуту после старта
        name="grid_check",
    )

    # Ежедневный анализ + DCA + дивиденды
    jq.run_daily(
        job_daily_tasks,
        time=dt_time(
            hour=Config.DAILY_ANALYSIS_HOUR_UTC,
            minute=Config.DAILY_ANALYSIS_MINUTE_UTC,
            tzinfo=timezone.utc,
        ),
        name="daily_tasks",
    )

    # Еженедельный отчёт
    jq.run_daily(
        job_weekly_report,
        time=dt_time(
            hour=Config.WEEKLY_REPORT_HOUR_UTC,
            minute=0,
            tzinfo=timezone.utc,
        ),
        days=(Config.WEEKLY_REPORT_DAY,),
        name="weekly_report",
    )

    logger.info(
        f"Планировщик запущен. "
        f"Grid: каждые {Config.GRID_CHECK_INTERVAL_SEC}с, "
        f"Ежедневно: {Config.DAILY_ANALYSIS_HOUR_UTC}:{Config.DAILY_ANALYSIS_MINUTE_UTC:02d} UTC, "
        f"Еженедельно: пн {Config.WEEKLY_REPORT_HOUR_UTC}:00 UTC"
    )

"""
Хэндлеры: /analysis — ежедневный анализ рынка, /report — еженедельный отчёт.
"""
import logging
from datetime import date, datetime, timedelta
from telegram import Update
from telegram.ext import ContextTypes

from bot.keyboards import main_menu_keyboard, close_keyboard
from bot.middlewares import authorized_only
from database import (
    AsyncSessionLocal, get_or_create_user, get_user_strategies,
    get_strategy_positions, DailyAnalysis, WeeklyReport,
    TrackedPosition
)
from services.market_analysis import run_daily_analysis, generate_position_summary
from services.tinkoff_client import get_client
from sqlalchemy import select

logger = logging.getLogger(__name__)


@authorized_only
async def analysis_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /analysis — ежедневный анализ рынка."""
    msg = update.message or update.callback_query.message
    user = update.effective_user

    await msg.reply_text("🔄 Анализирую рынок, подождите...")

    try:
        # Проверяем кэш — уже делали анализ сегодня?
        today = date.today()
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(DailyAnalysis).where(DailyAnalysis.analysis_date == today)
            )
            cached = result.scalar_one_or_none()

        client = await get_client()

        if cached:
            analysis_text = cached.content
            analysis_results_json = cached.recommendations
        else:
            # Запускаем свежий анализ
            analysis_text, results = await run_daily_analysis(client)

            # Сохраняем в кэш
            import json
            async with AsyncSessionLocal() as session:
                new_analysis = DailyAnalysis(
                    analysis_date=today,
                    content=analysis_text,
                    recommendations_json=json.dumps(results, ensure_ascii=False, default=str),
                )
                session.add(new_analysis)
                await session.commit()

            analysis_results_json = results

        # Отправляем анализ (может быть длинным — режем на части)
        await _send_long_message(msg, analysis_text, parse_mode="Markdown")

        # Добавляем персональную сводку по позициям пользователя
        async with AsyncSessionLocal() as session:
            db_user = await get_or_create_user(session, user.id, user.username, user.first_name)
            result = await session.execute(
                select(TrackedPosition)
                .where(TrackedPosition.user_id == db_user.id)
                .where(TrackedPosition.is_active == True)
            )
            tracked = result.scalars().all()

        if tracked:
            position_summary = await generate_position_summary(
                client=client,
                tracked_positions=tracked,
                analysis_results=analysis_results_json if isinstance(analysis_results_json, list) else [],
            )
            if position_summary:
                await _send_long_message(msg, position_summary, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Ошибка analysis_handler: {e}", exc_info=True)
        await msg.reply_text(
            "❌ Ошибка при анализе рынка. Попробуй позже.",
            reply_markup=main_menu_keyboard(),
        )


@authorized_only
async def report_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /report — еженедельный отчёт."""
    msg = update.message or update.callback_query.message
    user = update.effective_user

    await msg.reply_text("🔄 Формирую еженедельный отчёт...")

    try:
        client = await get_client()

        async with AsyncSessionLocal() as session:
            db_user = await get_or_create_user(session, user.id, user.username, user.first_name)
            strategies = await get_user_strategies(session, db_user.id)

        if not strategies:
            await msg.reply_text(
                "📭 Нет активных стратегий для отчёта.\n\nСоздай через /strategies",
                reply_markup=main_menu_keyboard(),
            )
            return

        report_lines = _build_weekly_report_header()

        total_invested = 0.0
        total_current = 0.0

        for strategy in strategies:
            async with AsyncSessionLocal() as session:
                positions = await get_strategy_positions(session, strategy.id)
                # Сделки за последнюю неделю
                from sqlalchemy import select, and_
                from database import Trade
                week_ago = datetime.utcnow() - timedelta(days=7)
                result = await session.execute(
                    select(Trade)
                    .where(Trade.strategy_id == strategy.id)
                    .where(Trade.created_at >= week_ago)
                )
                recent_trades = result.scalars().all()

            type_emoji = {"DCA": "📈", "GRID": "🕸️", "DIVIDEND": "♻️"}.get(strategy.type.value, "📊")
            report_lines.append(f"\n{type_emoji} *{strategy.name}*")
            report_lines.append(f"Бюджет: {strategy.allocated_budget:.2f}₽ | Потрачено: {strategy.spent_budget:.2f}₽")

            strategy_current = 0.0
            for pos in positions:
                if pos.quantity <= 0:
                    continue
                current_price = await client.get_last_price(pos.figi)
                if current_price:
                    current_value = current_price * pos.quantity
                    strategy_current += current_value
                    pnl = current_value - pos.total_invested
                    pnl_pct = (pnl / pos.total_invested * 100) if pos.total_invested > 0 else 0
                    sign = "+" if pnl >= 0 else ""
                    pnl_emoji = "📈" if pnl >= 0 else "📉"
                    report_lines.append(
                        f"  {pos.ticker}: {pos.quantity} шт. = {current_value:.2f}₽ "
                        f"({sign}{pnl:.2f}₽ / {sign}{pnl_pct:.1f}% {pnl_emoji})"
                    )

            total_invested += strategy.spent_budget
            total_current += strategy_current

            if recent_trades:
                buys = [t for t in recent_trades if t.direction.value == "buy"]
                sells = [t for t in recent_trades if t.direction.value == "sell"]
                report_lines.append(f"  📋 За неделю: {len(buys)} покупок, {len(sells)} продаж")
                total_buy = sum(t.amount or 0 for t in buys)
                total_sell = sum(t.amount or 0 for t in sells)
                if total_buy > 0:
                    report_lines.append(f"  Куплено на: {total_buy:.2f}₽")
                if total_sell > 0:
                    report_lines.append(f"  Продано на: {total_sell:.2f}₽")
            else:
                report_lines.append("  📋 Сделок за неделю не было")

        # Итог
        if total_invested > 0:
            total_pnl = total_current - total_invested
            total_pnl_pct = total_pnl / total_invested * 100
            sign = "+" if total_pnl >= 0 else ""
            report_lines += [
                "",
                "─" * 25,
                "*ИТОГО ПО ВСЕМ СТРАТЕГИЯМ:*",
                f"Вложено в бумаги: {total_invested:.2f}₽",
                f"Текущая стоимость: {total_current:.2f}₽",
                f"P&L: {sign}{total_pnl:.2f}₽ ({sign}{total_pnl_pct:.1f}%)",
            ]

        report_lines.append(f"\n🕐 Сформирован: {datetime.now().strftime('%d.%m.%Y %H:%M')}")

        await _send_long_message(msg, "\n".join(report_lines), parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Ошибка report_handler: {e}", exc_info=True)
        await msg.reply_text(
            "❌ Ошибка при формировании отчёта.",
            reply_markup=main_menu_keyboard(),
        )


def _build_weekly_report_header() -> list[str]:
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    return [
        f"📋 *ЕЖЕНЕДЕЛЬНЫЙ ОТЧЁТ*",
        f"📅 Неделя: {week_start.strftime('%d.%m.%Y')} — {today.strftime('%d.%m.%Y')}",
        "",
    ]


async def _send_long_message(msg, text: str, max_len: int = 4000, **kwargs) -> None:
    """Разбивает длинный текст на части и отправляет."""
    if len(text) <= max_len:
        await msg.reply_text(text, **kwargs)
        return

    parts = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > max_len:
            parts.append(current)
            current = line
        else:
            current += ("\n" if current else "") + line

    if current:
        parts.append(current)

    for i, part in enumerate(parts):
        if i == len(parts) - 1:
            await msg.reply_text(part, **kwargs)
        else:
            await msg.reply_text(part, **{k: v for k, v in kwargs.items() if k != "reply_markup"})

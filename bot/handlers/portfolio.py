"""
Хэндлеры: /portfolio — текущий портфель, P&L по стратегиям.
"""
import logging
from datetime import datetime
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, filters

from bot.keyboards import main_menu_keyboard, close_keyboard
from bot.middlewares import authorized_only
from database import (
    AsyncSessionLocal, get_or_create_user, get_user_strategies,
    get_strategy_positions, StrategyType
)
from services.tinkoff_client import get_client

logger = logging.getLogger(__name__)


@authorized_only
async def portfolio_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает суммарный портфель и P&L по всем стратегиям."""
    user = update.effective_user
    msg = update.message or update.callback_query.message

    await msg.reply_text("🔄 Загружаю данные портфеля...", reply_markup=main_menu_keyboard())

    try:
        client = await get_client()

        async with AsyncSessionLocal() as session:
            db_user = await get_or_create_user(session, user.id, user.username, user.first_name)
            strategies = await get_user_strategies(session, db_user.id)

        if not strategies:
            await msg.reply_text(
                "📭 У тебя пока нет активных стратегий.\n\n"
                "Создай первую через /strategies → ➕ Новая стратегия",
                reply_markup=main_menu_keyboard(),
            )
            return

        lines = ["💼 *МОЙ ПОРТФЕЛЬ*\n"]
        total_invested = 0.0
        total_current = 0.0
        total_allocated = 0.0

        for strategy in strategies:
            async with AsyncSessionLocal() as session:
                positions = await get_strategy_positions(session, strategy.id)

            type_emoji = {"DCA": "📈", "GRID": "🕸️", "DIVIDEND": "♻️"}.get(strategy.type.value, "📊")
            status_emoji = "✅" if strategy.status.value == "active" else "⏸️"

            lines.append(f"{type_emoji} {status_emoji} *{strategy.name}*")
            lines.append(f"  Бюджет: выделено {strategy.allocated_budget:.0f}₽ | потрачено {strategy.spent_budget:.0f}₽ | остаток {strategy.remaining_budget:.0f}₽")

            total_allocated += strategy.allocated_budget
            strategy_current = 0.0
            strategy_invested = strategy.spent_budget

            if positions:
                pos_lines = []
                for pos in positions:
                    current_price = await client.get_last_price(pos.figi)
                    if current_price:
                        current_value = current_price * pos.quantity
                        pnl = current_value - pos.total_invested
                        pnl_pct = (pnl / pos.total_invested * 100) if pos.total_invested > 0 else 0
                        pnl_emoji = "📈" if pnl >= 0 else "📉"
                        sign = "+" if pnl >= 0 else ""
                        strategy_current += current_value
                        pos_lines.append(
                            f"  • {pos.ticker}: {pos.quantity} шт. × {current_price:.2f}₽ = {current_value:.2f}₽ "
                            f"({sign}{pnl:.2f}₽ {pnl_emoji})"
                        )
                    else:
                        pos_lines.append(f"  • {pos.ticker}: {pos.quantity} шт. (цена недоступна)")

                lines.extend(pos_lines)
                total_invested += strategy_invested
                total_current += strategy_current

                if strategy_invested > 0:
                    strategy_pnl = strategy_current - strategy_invested
                    strategy_pnl_pct = strategy_pnl / strategy_invested * 100
                    sign = "+" if strategy_pnl >= 0 else ""
                    lines.append(
                        f"  📊 P&L стратегии: {sign}{strategy_pnl:.2f}₽ ({sign}{strategy_pnl_pct:.1f}%)"
                    )
            else:
                lines.append("  📭 Позиций пока нет")

            lines.append("")

        # Итог
        if total_invested > 0:
            total_pnl = total_current - total_invested
            total_pnl_pct = total_pnl / total_invested * 100
            sign = "+" if total_pnl >= 0 else ""
            lines.append("─" * 25)
            lines.append(f"*ИТОГО:*")
            lines.append(f"Выделено под стратегии: {total_allocated:.0f}₽")
            lines.append(f"Вложено в бумаги: {total_invested:.2f}₽")
            lines.append(f"Текущая стоимость: {total_current:.2f}₽")
            lines.append(f"Общий P&L: {sign}{total_pnl:.2f}₽ ({sign}{total_pnl_pct:.1f}%)")

        lines.append("")
        lines.append(f"🕐 Обновлено: {datetime.now().strftime('%d.%m.%Y %H:%M')}")

        await msg.reply_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=close_keyboard(),
        )

    except Exception as e:
        logger.error(f"Ошибка portfolio_handler: {e}", exc_info=True)
        await msg.reply_text(
            "❌ Ошибка при загрузке портфеля. Попробуй позже.",
            reply_markup=main_menu_keyboard(),
        )

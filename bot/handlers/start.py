"""
Хэндлеры: /start, /help, главное меню.
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

from bot.keyboards import main_menu_keyboard
from bot.middlewares import authorized_only
from config import Config
from database import AsyncSessionLocal, get_or_create_user

logger = logging.getLogger(__name__)

HELP_TEXT = """
🤖 *Инвестиционный бот* — управление стратегиями на MOEX

*Стратегии:*
• 📈 *DCA* — покупай на фиксированную сумму каждую неделю/месяц
• 🕸️ *Grid* — зарабатывай на колебаниях в заданном диапазоне цен
• ♻️ *Дивиденды* — автоматически реинвестируй дивиденды

*Команды:*
/start — главное меню
/strategies — мои стратегии (создать, управлять)
/portfolio — текущий портфель и P&L
/analysis — ежедневный анализ рынка
/report — еженедельный отчёт по стратегиям
/help — эта справка

*Важно:*
Бот работает с выделенным бюджетом — деньги вне выделенного бюджета стратегии НЕ трогаются.
Перед реальными сделками убедитесь, что отключён режим симуляции (USE_SANDBOX=false).
"""


@authorized_only
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    async with AsyncSessionLocal() as session:
        await get_or_create_user(
            session=session,
            telegram_id=user.id,
            username=user.username,
            first_name=user.first_name,
        )

    mode_str = ""
    if not Config.TINKOFF_TOKEN:
        mode_str = "\n\n⚠️ Режим: симуляция (токен T-Bank не задан)"
    elif Config.USE_SANDBOX:
        mode_str = "\n\n🏷️ Режим: sandbox (бумажная торговля)"
    else:
        mode_str = "\n\n✅ Режим: реальная торговля"

    await update.message.reply_text(
        f"Привет, {user.first_name}! 👋\n\n"
        f"Я помогу управлять твоими инвестициями на Московской бирже "
        f"через автоматические стратегии.{mode_str}\n\n"
        f"Используй кнопки меню или /help для списка команд.",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown",
    )


@authorized_only
async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        HELP_TEXT,
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )

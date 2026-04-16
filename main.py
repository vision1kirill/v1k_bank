"""
Точка входа Telegram инвестиционного бота.
"""
import asyncio
import logging
import sys
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters
)

from config import Config
from database import init_db
from bot.handlers.start import start_handler, help_handler
from bot.handlers.portfolio import portfolio_handler
from bot.handlers.reports import analysis_handler, report_handler
from bot.handlers.strategies import (
    strategies_handler, strategy_detail_callback,
    strategy_pause_callback, strategy_resume_callback,
    strategy_stop_confirm_callback, strategy_stop_confirmed_callback,
    strategy_trades_callback,
    build_strategy_conversation, build_topup_conversation,
)
from services.scheduler_jobs import setup_scheduler

# ─── Логирование ──────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=getattr(logging, Config.LOG_LEVEL, logging.INFO),
    stream=sys.stdout,
)
# Снижаем шум от httpx/telegram
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


async def text_router(update: Update, context) -> None:
    """Роутер текстовых кнопок главного меню."""
    text = update.message.text
    if text == "📊 Стратегии":
        await strategies_handler(update, context)
    elif text == "💼 Портфель":
        await portfolio_handler(update, context)
    elif text == "📈 Анализ рынка":
        await analysis_handler(update, context)
    elif text == "📋 Отчёт":
        await report_handler(update, context)
    elif text == "ℹ️ Помощь":
        await help_handler(update, context)


async def callback_router(update: Update, context) -> None:
    """Роутер inline callback кнопок."""
    query = update.callback_query
    data = query.data

    if data == "close":
        await query.answer()
        try:
            await query.delete_message()
        except Exception:
            await query.edit_message_reply_markup(reply_markup=None)

    elif data == "strategies_list":
        await query.answer()
        await strategies_handler(update, context)

    elif data.startswith("strategy:"):
        await strategy_detail_callback(update, context)

    elif data.startswith("strategy_pause:"):
        await strategy_pause_callback(update, context)

    elif data.startswith("strategy_resume:"):
        await strategy_resume_callback(update, context)

    elif data.startswith("strategy_stop:"):
        await strategy_stop_confirm_callback(update, context)

    elif data.startswith("strategy_stop_confirmed:"):
        await strategy_stop_confirmed_callback(update, context)

    elif data.startswith("strategy_trades:"):
        await strategy_trades_callback(update, context)

    else:
        await query.answer()


def main() -> None:
    # ─── Валидация конфига ─────────────────────────────────────────────────
    Config.validate()

    # ─── Создаём приложение ────────────────────────────────────────────────
    app = (
        Application.builder()
        .token(Config.TELEGRAM_TOKEN)
        .build()
    )

    # ─── ConversationHandlers (регистрируем первыми — приоритет выше) ─────
    app.add_handler(build_strategy_conversation())
    app.add_handler(build_topup_conversation())

    # ─── Команды ──────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(CommandHandler("portfolio", portfolio_handler))
    app.add_handler(CommandHandler("strategies", strategies_handler))
    app.add_handler(CommandHandler("analysis", analysis_handler))
    app.add_handler(CommandHandler("report", report_handler))

    # ─── Роутеры ──────────────────────────────────────────────────────────
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    app.add_handler(CallbackQueryHandler(callback_router))

    # ─── Инициализация БД (синхронно перед запуском) ──────────────────────
    async def post_init(application: Application) -> None:
        await init_db()
        # Инициализируем T-Bank клиент
        from services.tinkoff_client import get_client
        await get_client()
        logger.info("Бот полностью инициализирован и готов к работе.")

    app.post_init = post_init

    # ─── Планировщик ──────────────────────────────────────────────────────
    setup_scheduler(app)

    # ─── Запуск ───────────────────────────────────────────────────────────
    logger.info(f"Запуск бота. Sandbox: {Config.USE_SANDBOX}")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,  # пропускаем старые сообщения при рестарте
    )


if __name__ == "__main__":
    main()

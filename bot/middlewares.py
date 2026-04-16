"""
Middleware для проверки прав доступа.
"""
import logging
from functools import wraps
from telegram import Update
from telegram.ext import ContextTypes

from config import Config

logger = logging.getLogger(__name__)


def authorized_only(func):
    """
    Декоратор: пропускает хэндлер только для разрешённых пользователей.
    Если ALLOWED_USER_IDS пуст — разрешаем всем (но выводим предупреждение).
    """
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if not user:
            return

        if Config.ALLOWED_USER_IDS and user.id not in Config.ALLOWED_USER_IDS:
            logger.warning(f"Попытка доступа от неизвестного пользователя: {user.id} (@{user.username})")
            if update.message:
                await update.message.reply_text(
                    "🚫 Доступ запрещён. Этот бот приватный."
                )
            elif update.callback_query:
                await update.callback_query.answer("Доступ запрещён.", show_alert=True)
            return

        return await func(update, context, *args, **kwargs)
    return wrapper

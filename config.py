"""
Конфигурация бота. Все параметры берутся из переменных окружения (.env файл или Railway Variables).
"""
import os
import logging
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


class Config:
    # ─── Telegram ────────────────────────────────────────────────────────────
    TELEGRAM_TOKEN: str = os.getenv("TELEGRAM_TOKEN", "")

    # ─── T-Bank (Tinkoff) Invest API ─────────────────────────────────────────
    TINKOFF_TOKEN: str = os.getenv("TINKOFF_TOKEN", "")
    TINKOFF_ACCOUNT_ID: str = os.getenv("TINKOFF_ACCOUNT_ID", "")

    # Если USE_SANDBOX=true — используем песочницу T-Bank (безопасно, без реальных денег)
    USE_SANDBOX: bool = os.getenv("USE_SANDBOX", "true").lower() == "true"

    # ─── Доступ ───────────────────────────────────────────────────────────────
    # Список Telegram User ID, которым разрешено пользоваться ботом.
    # Обязательно заполни! Иначе любой найдёт бота и получит доступ.
    ALLOWED_USER_IDS: list[int] = [
        int(x.strip())
        for x in os.getenv("ALLOWED_USER_IDS", "").split(",")
        if x.strip()
    ]

    # ─── База данных ──────────────────────────────────────────────────────────
    # Railway автоматически задаёт DATABASE_URL для PostgreSQL.
    # Если его нет — используем SQLite.
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./bot_data.db")

    # ─── Расписание ───────────────────────────────────────────────────────────
    # Время ежедневного анализа рынка (МСК = UTC+3)
    DAILY_ANALYSIS_HOUR_UTC: int = int(os.getenv("DAILY_ANALYSIS_HOUR_UTC", "7"))   # 10:00 МСК
    DAILY_ANALYSIS_MINUTE_UTC: int = int(os.getenv("DAILY_ANALYSIS_MINUTE_UTC", "0"))

    # День недели для еженедельного отчёта (0=пн, 6=вс)
    WEEKLY_REPORT_DAY: int = int(os.getenv("WEEKLY_REPORT_DAY", "0"))  # Понедельник
    WEEKLY_REPORT_HOUR_UTC: int = int(os.getenv("WEEKLY_REPORT_HOUR_UTC", "8"))   # 11:00 МСК

    # Как часто проверять исполнение ордеров Grid (секунды)
    GRID_CHECK_INTERVAL_SEC: int = int(os.getenv("GRID_CHECK_INTERVAL_SEC", "300"))  # 5 минут

    # ─── Прочее ───────────────────────────────────────────────────────────────
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    @classmethod
    def validate(cls) -> None:
        """Проверяем критичные настройки при старте."""
        errors = []

        if not cls.TELEGRAM_TOKEN:
            errors.append("TELEGRAM_TOKEN не задан")

        if not cls.ALLOWED_USER_IDS:
            logger.warning(
                "ALLOWED_USER_IDS не задан! Бот будет доступен ВСЕМ пользователям Telegram. "
                "Это НЕБЕЗОПАСНО если бот имеет доступ к реальным деньгам."
            )

        if not cls.TINKOFF_TOKEN:
            logger.warning(
                "TINKOFF_TOKEN не задан. Бот работает в режиме симуляции (без реальных сделок)."
            )

        if cls.USE_SANDBOX:
            logger.info("Режим: SANDBOX (бумажная торговля — реальные деньги не используются)")
        else:
            logger.warning("Режим: РЕАЛЬНАЯ торговля. Будьте осторожны!")

        if errors:
            raise ValueError(f"Критические ошибки конфигурации: {'; '.join(errors)}")

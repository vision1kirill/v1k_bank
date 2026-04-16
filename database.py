"""
Модели базы данных и операции с ней.
Используем SQLAlchemy 2.0 async + aiosqlite (SQLite) или asyncpg (PostgreSQL).
"""
import json
import logging
from datetime import datetime, date
from decimal import Decimal
from enum import Enum as PyEnum
from typing import Optional, Any

from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, Date,
    Text, Enum, ForeignKey, UniqueConstraint, Index, select, update
)
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, relationship

from config import Config

logger = logging.getLogger(__name__)

# ─── Движок базы данных ────────────────────────────────────────────────────────
# Railway даёт URL вида postgresql://... — меняем на asyncpg-совместимый
_db_url = Config.DATABASE_URL
if _db_url.startswith("postgresql://"):
    _db_url = _db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
elif _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql+asyncpg://", 1)

engine = create_async_engine(
    _db_url,
    echo=False,  # True — для дебага SQL запросов
    pool_pre_ping=True,
    pool_recycle=300,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    pass


# ─── Перечисления ─────────────────────────────────────────────────────────────

class StrategyType(str, PyEnum):
    DCA = "DCA"
    GRID = "GRID"
    DIVIDEND = "DIVIDEND"


class StrategyStatus(str, PyEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    STOPPED = "stopped"


class TradeDirection(str, PyEnum):
    BUY = "buy"
    SELL = "sell"


class TradeStatus(str, PyEnum):
    PENDING = "pending"
    EXECUTED = "executed"
    CANCELLED = "cancelled"
    FAILED = "failed"


# ─── Модели ────────────────────────────────────────────────────────────────────

class User(Base):
    """Пользователь бота."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(Integer, unique=True, nullable=False, index=True)
    username = Column(String(64), nullable=True)
    first_name = Column(String(64), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    strategies = relationship("Strategy", back_populates="user", cascade="all, delete-orphan")
    tracked_positions = relationship("TrackedPosition", back_populates="user", cascade="all, delete-orphan")


class Strategy(Base):
    """
    Инвестиционная стратегия пользователя.
    config_json хранит специфичные для стратегии параметры.

    DCA config:
    {
        "ticker": "SBER", "figi": "BBG004730N88",
        "amount_per_buy": 3000.0,        # сумма одной покупки в рублях
        "frequency": "weekly",           # weekly | monthly
        "next_buy_date": "2024-01-15",
        "last_buy_date": null
    }

    GRID config:
    {
        "ticker": "SBER", "figi": "BBG004730N88",
        "price_low": 250.0,
        "price_high": 320.0,
        "step": 10.0,                    # шаг сетки в рублях
        "amount_per_level": 500.0,       # сумма на каждый уровень
        "levels": [                      # массив уровней с их статусами
            {"price": 250.0, "status": "buy_pending", "order_id": null},
            ...
        ]
    }

    DIVIDEND config:
    {
        "tickers": [
            {"ticker": "SBER", "figi": "BBG004730N88"},
            ...
        ],
        "last_check_date": "2024-01-01"
    }
    """
    __tablename__ = "strategies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    type = Column(Enum(StrategyType), nullable=False)
    status = Column(Enum(StrategyStatus), default=StrategyStatus.ACTIVE, nullable=False)

    # Бюджет
    allocated_budget = Column(Float, nullable=False, default=0.0)   # выделено пользователем (₽)
    spent_budget = Column(Float, nullable=False, default=0.0)        # потрачено на покупки (₽)
    realized_pnl = Column(Float, nullable=False, default=0.0)        # реализованный P&L (₽)

    config_json = Column(Text, nullable=False, default="{}")

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="strategies")
    trades = relationship("Trade", back_populates="strategy", cascade="all, delete-orphan")
    positions = relationship("Position", back_populates="strategy", cascade="all, delete-orphan")

    @property
    def config(self) -> dict:
        return json.loads(self.config_json) if self.config_json else {}

    def set_config(self, data: dict) -> None:
        self.config_json = json.dumps(data, ensure_ascii=False, default=str)

    @property
    def remaining_budget(self) -> float:
        """Оставшийся бюджет для новых сделок."""
        return max(0.0, self.allocated_budget - self.spent_budget)

    def __repr__(self) -> str:
        return f"<Strategy id={self.id} type={self.type} name={self.name}>"


class Trade(Base):
    """Исполненная или запланированная сделка."""
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_id = Column(Integer, ForeignKey("strategies.id"), nullable=False, index=True)
    direction = Column(Enum(TradeDirection), nullable=False)
    ticker = Column(String(20), nullable=False)
    figi = Column(String(30), nullable=False)
    quantity = Column(Integer, nullable=False, default=0)          # количество лотов
    lot_size = Column(Integer, nullable=False, default=1)           # размер лота
    price = Column(Float, nullable=True)                            # цена исполнения
    amount = Column(Float, nullable=True)                           # сумма сделки (₽)
    commission = Column(Float, nullable=False, default=0.0)         # комиссия брокера

    order_id = Column(String(64), nullable=True, index=True)        # ID ордера в T-Bank
    status = Column(Enum(TradeStatus), default=TradeStatus.PENDING, nullable=False)
    note = Column(String(500), nullable=True)                       # пометка (дивиденд, DCA и т.п.)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    executed_at = Column(DateTime, nullable=True)

    strategy = relationship("Strategy", back_populates="trades")


class Position(Base):
    """
    Текущая позиция (открытые бумаги) в рамках конкретной стратегии.
    Обновляется после каждой сделки.
    """
    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("strategy_id", "figi", name="uq_strategy_figi"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_id = Column(Integer, ForeignKey("strategies.id"), nullable=False, index=True)
    ticker = Column(String(20), nullable=False)
    figi = Column(String(30), nullable=False)
    quantity = Column(Integer, nullable=False, default=0)           # количество штук (не лотов)
    avg_price = Column(Float, nullable=False, default=0.0)          # средняя цена покупки
    total_invested = Column(Float, nullable=False, default=0.0)     # всего вложено (₽)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    strategy = relationship("Strategy", back_populates="positions")

    @property
    def current_value(self) -> float:
        """Текущая стоимость (нужна текущая цена, считается снаружи)."""
        return self.avg_price * self.quantity


class TrackedPosition(Base):
    """
    Позиции, которые пользователь открыл по совету бота.
    Используются для персональных рекомендаций в ежедневном анализе.
    """
    __tablename__ = "tracked_positions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    ticker = Column(String(20), nullable=False)
    figi = Column(String(30), nullable=False)
    quantity = Column(Integer, nullable=False, default=0)
    avg_price = Column(Float, nullable=False, default=0.0)
    total_invested = Column(Float, nullable=False, default=0.0)
    opened_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    note = Column(String(300), nullable=True)                       # "Совет от 2024-01-10"

    user = relationship("User", back_populates="tracked_positions")


class DailyAnalysis(Base):
    """Кэш ежедневного анализа рынка."""
    __tablename__ = "daily_analysis"

    id = Column(Integer, primary_key=True, autoincrement=True)
    analysis_date = Column(Date, unique=True, nullable=False, index=True)
    content = Column(Text, nullable=False)                          # текст для пользователя
    recommendations_json = Column(Text, nullable=False, default="[]")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    @property
    def recommendations(self) -> list:
        return json.loads(self.recommendations_json) if self.recommendations_json else []


class WeeklyReport(Base):
    """Кэш еженедельного отчёта."""
    __tablename__ = "weekly_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    week_start = Column(Date, unique=True, nullable=False, index=True)
    week_end = Column(Date, nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


# ─── Инициализация БД ──────────────────────────────────────────────────────────

async def init_db() -> None:
    """Создаём все таблицы при первом запуске."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("База данных инициализирована.")


# ─── Вспомогательные CRUD функции ─────────────────────────────────────────────

async def get_or_create_user(session: AsyncSession, telegram_id: int, username: str | None, first_name: str | None) -> User:
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(telegram_id=telegram_id, username=username, first_name=first_name)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        logger.info(f"Новый пользователь зарегистрирован: {telegram_id}")
    return user


async def get_user_strategies(session: AsyncSession, user_id: int) -> list[Strategy]:
    result = await session.execute(
        select(Strategy)
        .where(Strategy.user_id == user_id)
        .where(Strategy.status != StrategyStatus.STOPPED)
        .order_by(Strategy.created_at)
    )
    return list(result.scalars().all())


async def get_strategy(session: AsyncSession, strategy_id: int, user_id: int) -> Strategy | None:
    result = await session.execute(
        select(Strategy)
        .where(Strategy.id == strategy_id)
        .where(Strategy.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def get_strategy_positions(session: AsyncSession, strategy_id: int) -> list[Position]:
    result = await session.execute(
        select(Position).where(Position.strategy_id == strategy_id, Position.quantity > 0)
    )
    return list(result.scalars().all())


async def get_all_active_strategies(session: AsyncSession) -> list[Strategy]:
    """Все активные стратегии всех пользователей (для шедулера)."""
    result = await session.execute(
        select(Strategy).where(Strategy.status == StrategyStatus.ACTIVE)
    )
    return list(result.scalars().all())


async def update_or_create_position(
    session: AsyncSession,
    strategy_id: int,
    ticker: str,
    figi: str,
    quantity_delta: int,
    price: float,
) -> Position:
    """
    Обновляет позицию после сделки.
    quantity_delta > 0 — покупка, < 0 — продажа.
    Пересчитывает среднюю цену покупки.
    """
    result = await session.execute(
        select(Position)
        .where(Position.strategy_id == strategy_id, Position.figi == figi)
    )
    pos = result.scalar_one_or_none()

    if pos is None:
        pos = Position(
            strategy_id=strategy_id,
            ticker=ticker,
            figi=figi,
            quantity=0,
            avg_price=0.0,
            total_invested=0.0,
        )
        session.add(pos)

    if quantity_delta > 0:
        # Покупка: пересчитываем среднюю
        total_qty = pos.quantity + quantity_delta
        pos.total_invested += price * quantity_delta
        pos.avg_price = pos.total_invested / total_qty if total_qty > 0 else 0.0
        pos.quantity = total_qty
    else:
        # Продажа
        pos.quantity = max(0, pos.quantity + quantity_delta)
        if pos.quantity == 0:
            pos.avg_price = 0.0
            pos.total_invested = 0.0
        else:
            pos.total_invested = pos.avg_price * pos.quantity

    pos.updated_at = datetime.utcnow()
    await session.commit()
    await session.refresh(pos)
    return pos


async def record_trade(
    session: AsyncSession,
    strategy_id: int,
    direction: TradeDirection,
    ticker: str,
    figi: str,
    quantity: int,
    lot_size: int,
    price: float,
    amount: float,
    commission: float = 0.0,
    order_id: str | None = None,
    note: str | None = None,
) -> Trade:
    trade = Trade(
        strategy_id=strategy_id,
        direction=direction,
        ticker=ticker,
        figi=figi,
        quantity=quantity,
        lot_size=lot_size,
        price=price,
        amount=amount,
        commission=commission,
        order_id=order_id,
        status=TradeStatus.EXECUTED,
        note=note,
        executed_at=datetime.utcnow(),
    )
    session.add(trade)

    # Обновляем потраченный бюджет стратегии
    result = await session.execute(select(Strategy).where(Strategy.id == strategy_id))
    strategy = result.scalar_one_or_none()
    if strategy:
        if direction == TradeDirection.BUY:
            strategy.spent_budget += amount + commission
        else:
            strategy.realized_pnl += (price - 0) * quantity  # упрощённо
        strategy.updated_at = datetime.utcnow()

    await session.commit()
    await session.refresh(trade)
    return trade

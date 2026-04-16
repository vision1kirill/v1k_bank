"""
Telegram InlineKeyboard кнопки и ReplyKeyboard для бота.
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton

from database import Strategy, StrategyType, StrategyStatus


# ─── Главное меню ─────────────────────────────────────────────────────────────

def main_menu_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton("📊 Стратегии"), KeyboardButton("💼 Портфель")],
        [KeyboardButton("📈 Анализ рынка"), KeyboardButton("📋 Отчёт")],
        [KeyboardButton("ℹ️ Помощь")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)


# ─── Выбор типа стратегии ─────────────────────────────────────────────────────

def strategy_type_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("📈 DCA (Усреднение)", callback_data="new_strategy:DCA")],
        [InlineKeyboardButton("🕸️ Grid (Сетка)", callback_data="new_strategy:GRID")],
        [InlineKeyboardButton("♻️ Реинвест дивидендов", callback_data="new_strategy:DIVIDEND")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ]
    return InlineKeyboardMarkup(keyboard)


# ─── DCA: частота ─────────────────────────────────────────────────────────────

def dca_frequency_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("📅 Еженедельно", callback_data="dca_freq:weekly")],
        [InlineKeyboardButton("🗓️ Ежемесячно", callback_data="dca_freq:monthly")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ]
    return InlineKeyboardMarkup(keyboard)


# ─── Список стратегий пользователя ────────────────────────────────────────────

def strategies_list_keyboard(strategies: list[Strategy]) -> InlineKeyboardMarkup:
    keyboard = []
    for s in strategies:
        status_emoji = "✅" if s.status == StrategyStatus.ACTIVE else "⏸️"
        type_emoji = {"DCA": "📈", "GRID": "🕸️", "DIVIDEND": "♻️"}.get(s.type.value, "📊")
        keyboard.append([
            InlineKeyboardButton(
                f"{type_emoji} {status_emoji} {s.name}",
                callback_data=f"strategy:{s.id}"
            )
        ])

    keyboard.append([InlineKeyboardButton("➕ Новая стратегия", callback_data="new_strategy")])
    keyboard.append([InlineKeyboardButton("❌ Закрыть", callback_data="close")])
    return InlineKeyboardMarkup(keyboard)


# ─── Управление конкретной стратегией ────────────────────────────────────────

def strategy_manage_keyboard(strategy: Strategy) -> InlineKeyboardMarkup:
    keyboard = []

    if strategy.status == StrategyStatus.ACTIVE:
        keyboard.append([InlineKeyboardButton("⏸️ Приостановить", callback_data=f"strategy_pause:{strategy.id}")])
    else:
        keyboard.append([InlineKeyboardButton("▶️ Возобновить", callback_data=f"strategy_resume:{strategy.id}")])

    keyboard.append([
        InlineKeyboardButton("💰 Пополнить бюджет", callback_data=f"strategy_topup:{strategy.id}"),
    ])
    keyboard.append([
        InlineKeyboardButton("📊 История сделок", callback_data=f"strategy_trades:{strategy.id}"),
    ])
    keyboard.append([
        InlineKeyboardButton("🗑️ Остановить и удалить", callback_data=f"strategy_stop:{strategy.id}"),
    ])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="strategies_list")])
    return InlineKeyboardMarkup(keyboard)


# ─── Подтверждения ────────────────────────────────────────────────────────────

def confirm_keyboard(action: str, yes_data: str, no_data: str = "cancel") -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("✅ Да, подтверждаю", callback_data=yes_data),
            InlineKeyboardButton("❌ Нет, отмена", callback_data=no_data),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def close_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Закрыть", callback_data="close")]])


def back_keyboard(back_data: str = "strategies_list") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=back_data)]])

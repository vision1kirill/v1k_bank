"""
Хэндлеры для управления стратегиями.

Создание стратегии — максимально просто:
1. Выбрать тип стратегии
2. Ввести бюджет
3. Бот сам анализирует рынок и предлагает готовый план
4. Подтвердить (или выбрать из альтернатив для DCA)
"""
import logging
from datetime import date, datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ConversationHandler
)
from sqlalchemy import select, desc

from bot.keyboards import (
    strategy_type_keyboard, strategies_list_keyboard,
    strategy_manage_keyboard, confirm_keyboard,
    main_menu_keyboard, close_keyboard, back_keyboard
)
from bot.middlewares import authorized_only
from database import (
    AsyncSessionLocal, get_or_create_user, get_user_strategies, get_strategy,
    Strategy, StrategyType, StrategyStatus, Trade
)
from services.tinkoff_client import get_client
from services.strategies import build_dca_config, build_grid_config, build_dividend_config
from services.auto_select import auto_select_for_dca, auto_select_for_grid, auto_select_for_dividends

logger = logging.getLogger(__name__)

# ─── Состояния ConversationHandler ───────────────────────────────────────────
(
    STATE_CHOOSE_TYPE,
    # Общий шаг — ввод бюджета (один для всех)
    STATE_ENTER_BUDGET,
    # Шаги подтверждения
    STATE_DCA_CONFIRM,
    STATE_GRID_CONFIRM,
    STATE_DIV_CONFIRM,
    # Пополнение бюджета
    STATE_TOPUP_AMOUNT,
) = range(7)

CANCEL_MSG = "❌ Создание стратегии отменено."
_ANALYZING_MSG = "🔍 Анализирую рынок и подбираю лучший вариант для тебя..."


# ─── /strategies ──────────────────────────────────────────────────────────────

@authorized_only
async def strategies_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if not msg:
        return

    async with AsyncSessionLocal() as session:
        db_user = await get_or_create_user(session, user.id, user.username, user.first_name)
        strategies = await get_user_strategies(session, db_user.id)

    if not strategies:
        text = (
            "📭 *Стратегий пока нет.*\n\n"
            "Создай первую — просто выдели бюджет, всё остальное я решу сам.\n\n"
            "Нажми *➕ Новая стратегия*."
        )
    else:
        text = f"📊 *Твои стратегии* ({len(strategies)} шт.):\n\nВыбери для управления или создай новую."

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode="Markdown",
            reply_markup=strategies_list_keyboard(strategies)
        )
    else:
        await msg.reply_text(
            text, parse_mode="Markdown",
            reply_markup=strategies_list_keyboard(strategies)
        )


# ─── Просмотр конкретной стратегии ────────────────────────────────────────────

async def strategy_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    strategy_id = int(query.data.split(":")[1])

    async with AsyncSessionLocal() as session:
        db_user = await get_or_create_user(session, user.id, user.username, user.first_name)
        strategy = await get_strategy(session, strategy_id, db_user.id)

    if not strategy:
        await query.edit_message_text("❌ Стратегия не найдена.")
        return

    config = strategy.config
    type_emoji = {"DCA": "📈", "GRID": "🕸️", "DIVIDEND": "♻️"}.get(strategy.type.value, "📊")
    status_ru = {"active": "✅ Активна", "paused": "⏸️ Приостановлена", "stopped": "🛑 Остановлена"}

    lines = [
        f"{type_emoji} *{strategy.name}*",
        f"Статус: {status_ru.get(strategy.status.value, strategy.status.value)}",
        "",
        f"💰 *Бюджет:*",
        f"  Выделено: {strategy.allocated_budget:.2f}₽",
        f"  Потрачено: {strategy.spent_budget:.2f}₽",
        f"  Остаток: {strategy.remaining_budget:.2f}₽",
        "",
    ]

    if strategy.type == StrategyType.DCA:
        lines += [
            f"📋 *Параметры DCA:*",
            f"  Тикер: {config.get('ticker', '—')}",
            f"  Сумма покупки: {config.get('amount_per_buy', 0):.2f}₽ / раз",
            f"  Частота: {'Еженедельно' if config.get('frequency') == 'weekly' else 'Ежемесячно'}",
            f"  Следующая покупка: {config.get('next_buy_date', '—')}",
            f"  Последняя покупка: {config.get('last_buy_date', 'ещё не было')}",
        ]
    elif strategy.type == StrategyType.GRID:
        levels = config.get("levels", [])
        active_buys = sum(1 for l in levels if l["status"] == "buy_pending")
        active_sells = sum(1 for l in levels if l["status"] == "sell_pending")
        lines += [
            f"📋 *Параметры Grid:*",
            f"  Тикер: {config.get('ticker', '—')}",
            f"  Диапазон: {config.get('price_low', 0):.2f}₽ — {config.get('price_high', 0):.2f}₽",
            f"  Шаг: {config.get('step', 0):.2f}₽ | Уровней: {len(levels)}",
            f"  Активных ордеров: {active_buys} покупок, {active_sells} продаж",
            f"  Инициализирована: {'✅ Да' if config.get('initialized') else '⏳ Ожидание...'}",
        ]
    elif strategy.type == StrategyType.DIVIDEND:
        tickers = [t["ticker"] for t in config.get("tickers", [])]
        lines += [
            f"📋 *Параметры дивидендов:*",
            f"  Акции: {', '.join(tickers) if tickers else '—'}",
            f"  Всего дивидендов получено: {config.get('total_dividends_received', 0):.2f}₽",
            f"  Всего реинвестировано: {config.get('total_reinvested', 0):.2f}₽",
            f"  Последняя проверка: {config.get('last_check_date', '—')[:10]}",
        ]

    lines.append(f"\n🕐 Создана: {strategy.created_at.strftime('%d.%m.%Y')}")

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=strategy_manage_keyboard(strategy),
    )


# ─── Управление стратегией ────────────────────────────────────────────────────

async def strategy_pause_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    strategy_id = int(query.data.split(":")[1])
    user = update.effective_user
    async with AsyncSessionLocal() as session:
        db_user = await get_or_create_user(session, user.id, user.username, user.first_name)
        strategy = await get_strategy(session, strategy_id, db_user.id)
        if strategy:
            strategy.status = StrategyStatus.PAUSED
            await session.commit()
            await query.edit_message_text(
                f"⏸️ Стратегия «{strategy.name}» приостановлена.",
                reply_markup=back_keyboard("strategies_list"),
            )


async def strategy_resume_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    strategy_id = int(query.data.split(":")[1])
    user = update.effective_user
    async with AsyncSessionLocal() as session:
        db_user = await get_or_create_user(session, user.id, user.username, user.first_name)
        strategy = await get_strategy(session, strategy_id, db_user.id)
        if strategy:
            strategy.status = StrategyStatus.ACTIVE
            await session.commit()
            await query.edit_message_text(
                f"▶️ Стратегия «{strategy.name}» возобновлена!",
                reply_markup=back_keyboard("strategies_list"),
            )


async def strategy_stop_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    strategy_id = int(query.data.split(":")[1])
    await query.edit_message_text(
        "⚠️ Ты уверен? Стратегия будет остановлена.\nЭто действие нельзя отменить.",
        reply_markup=confirm_keyboard(
            "stop",
            yes_data=f"strategy_stop_confirmed:{strategy_id}",
            no_data=f"strategy:{strategy_id}",
        )
    )


async def strategy_stop_confirmed_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    strategy_id = int(query.data.split(":")[1])
    user = update.effective_user
    async with AsyncSessionLocal() as session:
        db_user = await get_or_create_user(session, user.id, user.username, user.first_name)
        strategy = await get_strategy(session, strategy_id, db_user.id)
        if strategy:
            name = strategy.name
            strategy.status = StrategyStatus.STOPPED
            await session.commit()
            await query.edit_message_text(
                f"🛑 Стратегия «{name}» остановлена.",
                reply_markup=back_keyboard("strategies_list"),
            )


async def strategy_trades_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    strategy_id = int(query.data.split(":")[1])
    user = update.effective_user

    async with AsyncSessionLocal() as session:
        db_user = await get_or_create_user(session, user.id, user.username, user.first_name)
        strategy = await get_strategy(session, strategy_id, db_user.id)
        if not strategy:
            await query.edit_message_text("❌ Стратегия не найдена.")
            return
        result = await session.execute(
            select(Trade)
            .where(Trade.strategy_id == strategy_id)
            .order_by(desc(Trade.created_at))
            .limit(10)
        )
        trades = result.scalars().all()

    if not trades:
        await query.edit_message_text(
            f"📭 Сделок по «{strategy.name}» ещё нет.",
            reply_markup=back_keyboard(f"strategy:{strategy_id}"),
        )
        return

    lines = [f"📊 *Последние сделки — {strategy.name}:*\n"]
    for t in trades:
        d_emoji = "🟢" if t.direction.value == "buy" else "🔴"
        lines.append(
            f"{d_emoji} {t.ticker}: {t.quantity}шт × {(t.price or 0):.2f}₽ = {(t.amount or 0):.2f}₽\n"
            f"  {t.created_at.strftime('%d.%m.%Y %H:%M')} | {t.note or ''}"
        )

    await query.edit_message_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=back_keyboard(f"strategy:{strategy_id}"),
    )


# ─── ConversationHandler: создание стратегии ─────────────────────────────────

async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(CANCEL_MSG)
    else:
        await update.message.reply_text(CANCEL_MSG)
    context.user_data.clear()
    return ConversationHandler.END


async def new_strategy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает выбор типа стратегии."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🆕 *Выбери тип стратегии:*\n\n"
        "📈 *DCA* — регулярные покупки, усреднение цены\n"
        "🕸️ *Grid* — зарабатываем на колебаниях\n"
        "♻️ *Дивиденды* — пассивный доход, реинвестируем выплаты\n\n"
        "Ты только выделяешь бюджет — я сам подберу инструмент и параметры.",
        parse_mode="Markdown",
        reply_markup=strategy_type_keyboard(),
    )
    return STATE_CHOOSE_TYPE


async def choose_strategy_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Сохраняем тип и спрашиваем только бюджет."""
    query = update.callback_query
    await query.answer()
    strategy_type = query.data.split(":")[1]  # DCA | GRID | DIVIDEND
    context.user_data["strategy_type"] = strategy_type

    type_descriptions = {
        "DCA": (
            "📈 *DCA — Усреднение*\n\n"
            "Я сам выберу подходящий тикер на основе сегодняшнего анализа рынка.\n"
            "Буду покупать на часть бюджета каждую неделю.\n\n"
        ),
        "GRID": (
            "🕸️ *Grid — Сетка*\n\n"
            "Я выберу инструмент с хорошей волатильностью и сам расставлю сетку ордеров.\n"
            "Зарабатываем на колебаниях цены внутри диапазона.\n\n"
        ),
        "DIVIDEND": (
            "♻️ *Реинвестирование дивидендов*\n\n"
            "Я подберу 3-4 акции с лучшей дивидендной доходностью.\n"
            "Покажу сколько ты заработаешь в год и распределю бюджет.\n\n"
        ),
    }

    desc = type_descriptions.get(strategy_type, "")
    await query.edit_message_text(
        f"{desc}💰 *Сколько рублей выделяешь на эту стратегию?*\n\n"
        f"Введи сумму (например: `10000`):",
        parse_mode="Markdown",
    )
    return STATE_ENTER_BUDGET


async def budget_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Принимаем бюджет, запускаем анализ, показываем готовый план."""
    try:
        budget = float(update.message.text.strip().replace(",", ".").replace(" ", ""))
        if budget < 1000:
            await update.message.reply_text(
                "❌ Минимальный бюджет — 1000₽. Введи сумму:"
            )
            return STATE_ENTER_BUDGET
    except ValueError:
        await update.message.reply_text(
            "❌ Введи сумму числом, например: `15000`",
            parse_mode="Markdown",
        )
        return STATE_ENTER_BUDGET

    context.user_data["budget"] = budget
    strategy_type = context.user_data.get("strategy_type", "DCA")

    # Показываем что анализируем
    wait_msg = await update.message.reply_text(_ANALYZING_MSG)

    try:
        client = await get_client()

        if strategy_type == "DCA":
            plan = await auto_select_for_dca(client, budget)
            context.user_data["plan"] = plan
            await wait_msg.delete()
            await _show_dca_plan(update, context, plan, budget)
            return STATE_DCA_CONFIRM

        elif strategy_type == "GRID":
            plan = await auto_select_for_grid(client, budget)
            context.user_data["plan"] = plan
            await wait_msg.delete()
            await _show_grid_plan(update, context, plan, budget)
            return STATE_GRID_CONFIRM

        elif strategy_type == "DIVIDEND":
            plan = await auto_select_for_dividends(client, budget)
            context.user_data["plan"] = plan
            await wait_msg.delete()
            await _show_dividend_plan(update, context, plan, budget)
            return STATE_DIV_CONFIRM

    except Exception as e:
        logger.error(f"Ошибка анализа для стратегии: {e}", exc_info=True)
        await wait_msg.delete()
        await update.message.reply_text(
            "❌ Ошибка при анализе рынка. Попробуй ещё раз через /strategies",
            reply_markup=main_menu_keyboard(),
        )
        return ConversationHandler.END

    return ConversationHandler.END


# ─── Показ планов ─────────────────────────────────────────────────────────────

async def _show_dca_plan(update: Update, context: ContextTypes.DEFAULT_TYPE, plan: dict, budget: float) -> None:
    ticker = plan["ticker"]
    name = plan["name"]
    price = plan["current_price"]
    amount = plan["amount_per_buy"]
    lots = plan["lots_per_buy"]
    lot_size = plan["lot_size"]
    buys = plan["buys_count"]
    rsi = plan.get("rsi")
    rsi_str = f"RSI: {rsi:.0f}" if rsi else ""

    lines = [
        f"📈 *Мой план для DCA стратегии:*\n",
        f"🏷️ Выбранная акция: *{name} ({ticker})*",
        f"💰 Текущая цена: {price:.2f}₽",
        f"  {rsi_str}",
        f"",
        f"📋 *Как будет работать:*",
        f"• Покупки: каждую неделю",
        f"• Сумма за раз: {amount:.0f}₽ (~{lots} лот{'а' if 2 <= lots <= 4 else 'ов'} = {lots * lot_size} шт.)",
        f"• Бюджета хватит на: ~{buys} покупок",
        f"",
        f"📊 *Почему именно {ticker}:*",
        plan["why_chosen"],
    ]

    # Альтернативы
    alts = plan.get("alternatives", [])
    if alts:
        lines.append("")
        lines.append("🔄 *Альтернативы (если хочешь другой тикер):*")

    keyboard = [
        [InlineKeyboardButton("✅ Запустить стратегию", callback_data="dca_confirm_main")],
    ]
    for alt in alts[:2]:
        alt_rsi = f" RSI={alt.get('rsi', 0):.0f}" if alt.get("rsi") else ""
        keyboard.append([
            InlineKeyboardButton(
                f"🔄 Выбрать {alt['ticker']} ({alt['name']}){alt_rsi}",
                callback_data=f"dca_confirm_alt:{alt['ticker']}"
            )
        ])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def _show_grid_plan(update: Update, context: ContextTypes.DEFAULT_TYPE, plan: dict, budget: float) -> None:
    lines = [
        f"🕸️ *Мой план для Grid стратегии:*\n",
        f"🏷️ Выбранный инструмент: *{plan['name']} ({plan['ticker']})*",
        f"💰 Текущая цена: {plan['current_price']:.2f}₽",
        f"",
        f"📋 *Параметры сетки (рассчитал автоматически):*",
        f"• Нижняя граница: {plan['price_low']:.2f}₽",
        f"• Верхняя граница: {plan['price_high']:.2f}₽",
        f"• Шаг: {plan['step']:.2f}₽",
        f"• Уровней в сетке: {plan['levels_count']}",
        f"• На каждый уровень: {plan['amount_per_level']:.0f}₽",
        f"• Бюджет: {budget:.0f}₽",
        f"",
        f"📊 *Потенциал:*",
        plan["why_chosen"],
    ]

    keyboard = [
        [InlineKeyboardButton("✅ Запустить стратегию", callback_data="grid_confirm")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ]

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def _show_dividend_plan(update: Update, context: ContextTypes.DEFAULT_TYPE, plan: dict, budget: float) -> None:
    allocs = plan["allocations"]

    lines = [
        f"♻️ *Мой план для дивидендной стратегии:*\n",
        f"💰 Бюджет: {budget:.0f}₽",
        f"",
        f"📋 *Портфель (подобрал автоматически):*",
    ]

    for a in allocs:
        lines.append(
            f"• *{a['name']} ({a['ticker']})*\n"
            f"  {a['weight_pct']:.0f}% бюджета = {a['alloc_rub']:.0f}₽ ({a['lots']} лот{'а' if 2 <= a['lots'] <= 4 else 'ов'})\n"
            f"  Дивидендная доходность: {a['div_yield']}% годовых\n"
            f"  Стабильность: {a['stability']}\n"
            f"  Доход: ~{a['annual_income']:.0f}₽/год (~{a['monthly_income']:.0f}₽/мес)"
        )

    lines += [
        "",
        f"💎 *Итого:*",
        f"  Средняя доходность: {plan['avg_yield_pct']:.1f}% годовых",
        f"  Ожидаемый доход: ~{plan['total_annual_income']:.0f}₽/год",
        f"  ~{plan['total_monthly_income']:.0f}₽/месяц",
        "",
        f"При поступлении дивидендов — автоматически докупаю те же акции (сложный процент).",
    ]

    keyboard = [
        [InlineKeyboardButton("✅ Запустить стратегию", callback_data="div_confirm")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ]

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ─── Подтверждения ────────────────────────────────────────────────────────────

async def dca_confirm_main(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Подтверждение DCA с основным тикером."""
    await _create_dca_strategy(update, context, use_alt_ticker=None)
    return ConversationHandler.END


async def dca_confirm_alt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Подтверждение DCA с альтернативным тикером."""
    query = update.callback_query
    alt_ticker = query.data.split(":")[1]
    await _create_dca_strategy(update, context, use_alt_ticker=alt_ticker)
    return ConversationHandler.END


async def _create_dca_strategy(
    update: Update, context: ContextTypes.DEFAULT_TYPE, use_alt_ticker: str | None
) -> None:
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    budget = context.user_data["budget"]
    plan = context.user_data["plan"]

    # Если выбрана альтернатива — перестраиваем план
    if use_alt_ticker:
        alts = plan.get("alternatives", [])
        alt_data = next((a for a in alts if a["ticker"] == use_alt_ticker), None)
        if alt_data:
            client = await get_client()
            plan = await auto_select_for_dca.__wrapped__(client, budget) if hasattr(auto_select_for_dca, '__wrapped__') else plan
            # Просто берём данные альтернативы
            ticker = alt_data["ticker"]
            figi = alt_data.get("figi", plan["figi"])
            amount = plan["amount_per_buy"]
        else:
            ticker = use_alt_ticker
            figi = plan["figi"]
            amount = plan["amount_per_buy"]
    else:
        ticker = plan["ticker"]
        figi = plan["figi"]
        amount = plan["amount_per_buy"]

    async with AsyncSessionLocal() as session:
        db_user = await get_or_create_user(session, user.id, user.username, user.first_name)
        config = build_dca_config(
            ticker=ticker,
            figi=figi,
            amount_per_buy=amount,
            frequency="weekly",
            start_date=date.today(),
        )
        strategy = Strategy(
            user_id=db_user.id,
            name=f"DCA {ticker}",
            type=StrategyType.DCA,
            allocated_budget=budget,
        )
        strategy.set_config(config)
        session.add(strategy)
        await session.commit()
        strategy_id = strategy.id

    context.user_data.clear()
    await query.edit_message_text(
        f"🎉 *DCA стратегия запущена!*\n\n"
        f"Тикер: {ticker}\n"
        f"Первая покупка: сегодня (если рынок открыт)\n"
        f"Бюджет: {budget:.0f}₽\n\n"
        f"Управляй через /strategies → «DCA {ticker}»",
        parse_mode="Markdown",
        reply_markup=back_keyboard("strategies_list"),
    )


async def grid_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    budget = context.user_data["budget"]
    plan = context.user_data["plan"]

    async with AsyncSessionLocal() as session:
        db_user = await get_or_create_user(session, user.id, user.username, user.first_name)
        config = build_grid_config(
            ticker=plan["ticker"],
            figi=plan["figi"],
            price_low=plan["price_low"],
            price_high=plan["price_high"],
            step=plan["step"],
            amount_per_level=plan["amount_per_level"],
            lot_size=plan["lot_size"],
        )
        strategy = Strategy(
            user_id=db_user.id,
            name=f"Grid {plan['ticker']}",
            type=StrategyType.GRID,
            allocated_budget=budget,
        )
        strategy.set_config(config)
        session.add(strategy)
        await session.commit()
        strategy_id = strategy.id

    context.user_data.clear()
    await query.edit_message_text(
        f"🎉 *Grid стратегия запущена!*\n\n"
        f"Тикер: {plan['ticker']}\n"
        f"Диапазон: {plan['price_low']:.2f}₽ — {plan['price_high']:.2f}₽\n"
        f"Ордера будут выставлены в течение нескольких минут.\n"
        f"Бюджет: {budget:.0f}₽\n\n"
        f"Управляй через /strategies → «Grid {plan['ticker']}»",
        parse_mode="Markdown",
        reply_markup=back_keyboard("strategies_list"),
    )
    return ConversationHandler.END


async def div_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    budget = context.user_data["budget"]
    plan = context.user_data["plan"]

    tickers_list = [
        {"ticker": a["ticker"], "figi": a["figi"]}
        for a in plan["allocations"]
    ]

    async with AsyncSessionLocal() as session:
        db_user = await get_or_create_user(session, user.id, user.username, user.first_name)
        config = build_dividend_config(tickers_list)
        ticker_names = ", ".join(a["ticker"] for a in plan["allocations"])
        strategy = Strategy(
            user_id=db_user.id,
            name=f"Дивиденды ({ticker_names})",
            type=StrategyType.DIVIDEND,
            allocated_budget=budget,
        )
        strategy.set_config(config)
        session.add(strategy)
        await session.commit()
        strategy_id = strategy.id

    context.user_data.clear()
    await query.edit_message_text(
        f"🎉 *Дивидендная стратегия запущена!*\n\n"
        f"Акции: {ticker_names}\n"
        f"Буду ежедневно проверять дивиденды и реинвестировать их.\n"
        f"Бюджет: {budget:.0f}₽\n\n"
        f"Управляй через /strategies",
        parse_mode="Markdown",
        reply_markup=back_keyboard("strategies_list"),
    )
    return ConversationHandler.END


# ─── Пополнение бюджета ───────────────────────────────────────────────────────

async def strategy_topup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    strategy_id = int(query.data.split(":")[1])
    context.user_data["topup_strategy_id"] = strategy_id

    async with AsyncSessionLocal() as session:
        user = update.effective_user
        db_user = await get_or_create_user(session, user.id, user.username, user.first_name)
        strategy = await get_strategy(session, strategy_id, db_user.id)
        if not strategy:
            await query.edit_message_text("❌ Стратегия не найдена.")
            return ConversationHandler.END

    await query.edit_message_text(
        f"💰 Пополнение бюджета *«{strategy.name}»*\n\n"
        f"Текущий бюджет: {strategy.allocated_budget:.0f}₽\n"
        f"Остаток: {strategy.remaining_budget:.0f}₽\n\n"
        f"Введи сумму пополнения (₽):",
        parse_mode="Markdown",
    )
    return STATE_TOPUP_AMOUNT


async def topup_amount_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        amount = float(update.message.text.strip().replace(",", ".").replace(" ", ""))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введи сумму числом, например: `5000`", parse_mode="Markdown")
        return STATE_TOPUP_AMOUNT

    strategy_id = context.user_data.get("topup_strategy_id")
    user = update.effective_user

    async with AsyncSessionLocal() as session:
        db_user = await get_or_create_user(session, user.id, user.username, user.first_name)
        strategy = await get_strategy(session, strategy_id, db_user.id)
        if strategy:
            strategy.allocated_budget += amount
            await session.commit()
            await update.message.reply_text(
                f"✅ Бюджет пополнен на {amount:.0f}₽!\n\n"
                f"Новый бюджет: {strategy.allocated_budget:.0f}₽\n"
                f"Остаток: {strategy.remaining_budget:.0f}₽",
                reply_markup=main_menu_keyboard(),
            )

    context.user_data.clear()
    return ConversationHandler.END


# ─── Сборка ConversationHandler ───────────────────────────────────────────────

def build_strategy_conversation() -> ConversationHandler:
    """
    Единый ConversationHandler для создания всех типов стратегий.
    Всего 3 шага: выбор типа → бюджет → подтверждение.
    """
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(new_strategy_callback, pattern="^new_strategy$"),
        ],
        states={
            STATE_CHOOSE_TYPE: [
                CallbackQueryHandler(choose_strategy_type, pattern="^new_strategy:"),
            ],
            STATE_ENTER_BUDGET: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, budget_entered),
            ],
            STATE_DCA_CONFIRM: [
                CallbackQueryHandler(dca_confirm_main, pattern="^dca_confirm_main$"),
                CallbackQueryHandler(dca_confirm_alt, pattern="^dca_confirm_alt:"),
            ],
            STATE_GRID_CONFIRM: [
                CallbackQueryHandler(grid_confirm, pattern="^grid_confirm$"),
            ],
            STATE_DIV_CONFIRM: [
                CallbackQueryHandler(div_confirm, pattern="^div_confirm$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_handler, pattern="^cancel$"),
            CommandHandler("cancel", cancel_handler),
        ],
        per_message=False,
        allow_reentry=True,
    )


def build_topup_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(strategy_topup_callback, pattern="^strategy_topup:"),
        ],
        states={
            STATE_TOPUP_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, topup_amount_step),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_handler, pattern="^cancel$"),
        ],
        per_message=False,
        allow_reentry=True,
    )

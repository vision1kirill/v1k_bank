"""
Автоматический выбор инструментов и параметров стратегий.

Пользователь только выделяет бюджет — всё остальное бот решает сам.
"""
import logging
from math import floor

from services.market_analysis import analyze_instrument, WATCHLIST
from services.tinkoff_client import TinkoffClient

logger = logging.getLogger(__name__)


# ─── Данные по дивидендам (MOEX, актуальные оценки) ──────────────────────────
# Дивидендная доходность % (приблизительно, на основе исторических данных)
DIVIDEND_STOCKS = [
    {"ticker": "SBER",  "name": "Сбербанк",     "div_yield": 10.8, "sector": "Финансы",    "stability": "высокая"},
    {"ticker": "LKOH",  "name": "ЛУКОЙЛ",        "div_yield": 12.5, "sector": "Нефть/Газ",  "stability": "высокая"},
    {"ticker": "TATN",  "name": "Татнефть",      "div_yield": 14.2, "sector": "Нефть/Газ",  "stability": "высокая"},
    {"ticker": "GMKN",  "name": "Норникель",     "div_yield": 7.5,  "sector": "Металлы",    "stability": "средняя"},
    {"ticker": "NVTK",  "name": "Новатэк",       "div_yield": 5.2,  "sector": "Нефть/Газ",  "stability": "высокая"},
    {"ticker": "MGNT",  "name": "Магнит",        "div_yield": 9.8,  "sector": "Ритейл",     "stability": "средняя"},
    {"ticker": "MTSS",  "name": "МТС",           "div_yield": 11.5, "sector": "Телеком",    "stability": "высокая"},
    {"ticker": "ROSN",  "name": "Роснефть",      "div_yield": 6.8,  "sector": "Нефть/Газ",  "stability": "высокая"},
]


# ─── Авто-выбор для DCA ───────────────────────────────────────────────────────

async def auto_select_for_dca(client: TinkoffClient, budget: float) -> dict:
    """
    Выбирает лучший инструмент для DCA стратегии.

    Критерии:
    - RSI < 45 (есть потенциал роста, нет перекупленности)
    - Положительный или нейтральный тренд
    - Высокий score от анализа

    Возвращает полную конфигурацию готовую к созданию стратегии.
    """
    best_result = None
    best_score = -999
    all_results = []

    for instrument in WATCHLIST:
        # Пропускаем БПИФ — у них нет дивидендов и сложнее анализ
        if instrument["sector"] == "БПИФ":
            continue
        try:
            inst_info = await client.find_instrument(instrument["ticker"])
            if not inst_info:
                continue
            figi = inst_info["figi"]
            lot_size = inst_info.get("lot", 1)

            candles = await client.get_candles(figi=figi, days=65, interval_str="day")
            if not candles:
                continue

            analysis = analyze_instrument(
                ticker=instrument["ticker"],
                name=instrument["name"],
                sector=instrument["sector"],
                candles=candles,
            )
            analysis["figi"] = figi
            analysis["lot_size"] = lot_size
            analysis["current_price"] = candles[-1]["close"] if candles else 0

            all_results.append(analysis)

            # Штраф за перекупленность, бонус за перепроданность
            score = analysis["score"]
            rsi = analysis.get("rsi") or 50
            if rsi < 30:
                score += 2
            elif rsi < 40:
                score += 1
            elif rsi > 65:
                score -= 2

            if score > best_score:
                best_score = score
                best_result = analysis

        except Exception as e:
            logger.warning(f"auto_select DCA: ошибка анализа {instrument['ticker']}: {e}")

    if not best_result:
        # Fallback: SBER
        inst = await client.find_instrument("SBER")
        best_result = {
            "ticker": "SBER", "figi": inst["figi"] if inst else "BBG004730N88",
            "name": "Сбербанк", "current_price": 280.0, "lot_size": 10,
            "rsi": 45, "signal_ru": "📒 ДЕРЖАТЬ", "reasons": ["Защитный выбор"],
        }

    # ─── Параметры DCA ─────────────────────────────────────────────────────
    # Логика: 1 покупка = ~10% бюджета → ~10 еженедельных покупок
    # Если 10% < 1 лота → берём 1 лот за покупку
    # Если 10% > 5 лотов → ограничиваем 5 лотами (не вкладываем слишком много за раз)
    ticker = best_result["ticker"]
    figi = best_result["figi"]
    lot_size = best_result.get("lot_size", 1)
    current_price = best_result.get("current_price") or 100.0
    one_lot_price = current_price * lot_size

    # Идеальное количество лотов за покупку
    ideal_lots = max(1, floor(budget * 0.10 / one_lot_price))
    # Ограничиваем: не больше 5 лотов и не больше 40% бюджета за раз
    lots_per_buy = min(ideal_lots, 5, floor(budget * 0.40 / one_lot_price))
    lots_per_buy = max(1, lots_per_buy)

    amount_per_buy = lots_per_buy * one_lot_price
    # Сколько покупок влезает в бюджет
    buys_count = floor(budget / amount_per_buy)

    # Выбираем альтернативы (топ-3)
    sorted_results = sorted(all_results, key=lambda x: x.get("score", 0), reverse=True)
    alternatives = [r for r in sorted_results if r["ticker"] != ticker][:2]

    return {
        "ticker": ticker,
        "figi": figi,
        "name": best_result["name"],
        "current_price": current_price,
        "lot_size": lot_size,
        "rsi": best_result.get("rsi"),
        "signal_ru": best_result.get("signal_ru", ""),
        "reasons": best_result.get("reasons", []),
        "amount_per_buy": amount_per_buy,
        "frequency": "weekly",
        "lots_per_buy": lots_per_buy,
        "buys_count": buys_count,
        "alternatives": alternatives,
        "why_chosen": _build_dca_explanation(best_result, budget, amount_per_buy, buys_count),
    }


def _build_dca_explanation(analysis: dict, budget: float, amount: float, buys: int) -> str:
    rsi = analysis.get("rsi")
    rsi_str = ""
    if rsi:
        if rsi < 30:
            rsi_str = f"RSI={rsi:.0f} — сильно перепродан, отличное время для входа"
        elif rsi < 45:
            rsi_str = f"RSI={rsi:.0f} — умеренно перепродан, хороший момент для DCA"
        else:
            rsi_str = f"RSI={rsi:.0f} — нейтральная зона, подходит для планомерных покупок"

    reasons = analysis.get("reasons", [])
    first_reason = reasons[0] if reasons else ""

    lines = []
    if rsi_str:
        lines.append(f"📊 {rsi_str}")
    if first_reason and first_reason not in rsi_str:
        lines.append(f"📈 {first_reason}")
    lines.append(f"💼 Стратегия: покупать на {amount:.0f}₽ еженедельно (~{buys} покупок)")
    return "\n".join(lines)


# ─── Авто-выбор для Grid ─────────────────────────────────────────────────────

async def auto_select_for_grid(client: TinkoffClient, budget: float) -> dict:
    """
    Выбирает лучший инструмент и параметры для Grid стратегии.

    Для Grid нужен инструмент с:
    - Хорошей волатильностью (движется туда-сюда в диапазоне)
    - НЕ сильным трендом (боковое движение)
    - RSI около 45-55 (нейтральная зона)
    - Высокий объём
    """
    best_result = None
    best_grid_score = -999
    all_results = []

    for instrument in WATCHLIST:
        if instrument["sector"] == "БПИФ":
            continue
        try:
            inst_info = await client.find_instrument(instrument["ticker"])
            if not inst_info:
                continue
            figi = inst_info["figi"]
            lot_size = inst_info.get("lot", 1)

            candles = await client.get_candles(figi=figi, days=65, interval_str="day")
            if not candles or len(candles) < 20:
                continue

            closes = [c["close"] for c in candles]
            volumes = [c["volume"] for c in candles]

            analysis = analyze_instrument(
                ticker=instrument["ticker"],
                name=instrument["name"],
                sector=instrument["sector"],
                candles=candles,
            )
            analysis["figi"] = figi
            analysis["lot_size"] = lot_size
            analysis["current_price"] = closes[-1]

            # Для Grid: идеальна боковая волатильность
            # Считаем: насколько цена "ходит" относительно своего среднего
            recent_closes = closes[-20:]
            avg_price = sum(recent_closes) / len(recent_closes)
            price_std = (sum((p - avg_price) ** 2 for p in recent_closes) / len(recent_closes)) ** 0.5
            volatility_pct = (price_std / avg_price) * 100  # волатильность в %

            # Grid score: хотим умеренную волатильность (3-8%) и нейтральный тренд
            rsi = analysis.get("rsi") or 50
            grid_score = 0

            # Волатильность 3-8% — отлично для Grid
            if 3 <= volatility_pct <= 8:
                grid_score += 3
            elif 2 <= volatility_pct <= 10:
                grid_score += 1

            # RSI около нейтральной зоны
            if 40 <= rsi <= 60:
                grid_score += 2
            elif 35 <= rsi <= 65:
                grid_score += 1

            # Объём (высокий = ликвидность = хорошо для Grid)
            avg_vol = sum(volumes[-20:]) / 20
            if avg_vol > 1_000_000:
                grid_score += 1

            analysis["volatility_pct"] = round(volatility_pct, 2)
            analysis["avg_price_20d"] = round(avg_price, 2)
            all_results.append(analysis)

            if grid_score > best_grid_score:
                best_grid_score = grid_score
                best_result = analysis

        except Exception as e:
            logger.warning(f"auto_select Grid: ошибка анализа {instrument['ticker']}: {e}")

    if not best_result:
        inst = await client.find_instrument("SBER")
        best_result = {
            "ticker": "SBER", "figi": inst["figi"] if inst else "BBG004730N88",
            "name": "Сбербанк", "current_price": 280.0, "lot_size": 10,
            "volatility_pct": 4.0, "avg_price_20d": 280.0,
        }

    # ─── Параметры Grid ────────────────────────────────────────────────────
    ticker = best_result["ticker"]
    figi = best_result["figi"]
    lot_size = best_result.get("lot_size", 1)
    current_price = best_result.get("current_price") or 100.0
    avg_price = best_result.get("avg_price_20d", current_price)

    # Диапазон: -10% / +10% от средней цены за 20 дней
    price_low = round(avg_price * 0.90, 2)
    price_high = round(avg_price * 1.10, 2)

    # Шаг: 2% от средней цены (10 уровней в диапазоне)
    step_raw = avg_price * 0.02
    # Округляем до красивого числа
    if step_raw > 50:
        step = round(step_raw / 10) * 10
    elif step_raw > 10:
        step = round(step_raw / 5) * 5
    elif step_raw > 1:
        step = round(step_raw)
    else:
        step = round(step_raw, 1)
    step = max(step, 0.5)  # минимальный шаг

    # Количество уровней
    levels_count = max(1, floor((price_high - price_low) / step) + 1)
    # Сумма на уровень: делим бюджет на уровни с запасом
    amount_per_level = round(budget / levels_count, 0)
    # Лотов на уровень
    lots_per_level = max(1, floor(amount_per_level / (current_price * lot_size)))

    # Оценка потенциального заработка за один цикл (шаг * лоты)
    profit_per_cycle = step * lots_per_level * lot_size
    cycles_per_month_est = 4  # грубая оценка
    monthly_est = profit_per_cycle * cycles_per_month_est

    return {
        "ticker": ticker,
        "figi": figi,
        "name": best_result["name"],
        "current_price": current_price,
        "lot_size": lot_size,
        "price_low": price_low,
        "price_high": price_high,
        "step": step,
        "levels_count": levels_count,
        "amount_per_level": amount_per_level,
        "lots_per_level": lots_per_level,
        "volatility_pct": best_result.get("volatility_pct", 0),
        "profit_per_cycle": round(profit_per_cycle, 2),
        "monthly_est": round(monthly_est, 2),
        "why_chosen": _build_grid_explanation(best_result, step, levels_count, profit_per_cycle, monthly_est),
    }


def _build_grid_explanation(analysis: dict, step: float, levels: int, profit_cycle: float, monthly: float) -> str:
    vol = analysis.get("volatility_pct", 0)
    name = analysis.get("name", analysis.get("ticker", ""))
    lines = [
        f"📊 Волатильность {name} за 20 дней: {vol:.1f}% — {'отлично' if 3 <= vol <= 8 else 'хорошо'} для Grid",
        f"🕸️ Шаг сетки: {step:.2f}₽ ({levels} уровней)",
        f"💰 Ожидаемая прибыль за цикл: ~{profit_cycle:.2f}₽",
        f"📅 Оценка прибыли за месяц: ~{monthly:.2f}₽ (при 4 циклах)",
    ]
    return "\n".join(lines)


# ─── Авто-выбор для Дивидендов ────────────────────────────────────────────────

async def auto_select_for_dividends(client: TinkoffClient, budget: float) -> dict:
    """
    Подбирает оптимальный портфель дивидендных акций под бюджет.

    Стратегия:
    - Берём топ-4 по дивидендной доходности с учётом стабильности
    - Делим бюджет пропорционально доходности
    - Показываем ожидаемый годовой доход
    """
    # Получаем цены для всех дивидендных акций
    enriched = []
    for stock in DIVIDEND_STOCKS:
        try:
            inst = await client.find_instrument(stock["ticker"])
            if not inst:
                continue
            price = await client.get_last_price(inst["figi"])
            if not price:
                continue

            lot_size = inst.get("lot", 1)
            one_lot_rub = price * lot_size
            # Доход в рублях на 1 лот в год
            div_income_per_lot = one_lot_rub * (stock["div_yield"] / 100)

            enriched.append({
                **stock,
                "figi": inst["figi"],
                "current_price": price,
                "lot_size": lot_size,
                "one_lot_rub": one_lot_rub,
                "div_income_per_lot": round(div_income_per_lot, 2),
            })
        except Exception as e:
            logger.warning(f"auto_select Dividends: {stock['ticker']}: {e}")

    if not enriched:
        # Заглушка
        enriched = [
            {"ticker": "SBER", "name": "Сбербанк", "div_yield": 10.8, "figi": "BBG004730N88",
             "current_price": 280.0, "lot_size": 10, "one_lot_rub": 2800.0, "stability": "высокая",
             "div_income_per_lot": 302.4, "sector": "Финансы"},
        ]

    # Сортируем по дивидендной доходности (высокая + стабильная = лучше)
    stability_bonus = {"высокая": 2, "средняя": 1, "низкая": 0}
    sorted_stocks = sorted(
        enriched,
        key=lambda x: x["div_yield"] + stability_bonus.get(x["stability"], 0),
        reverse=True,
    )

    # Берём топ-4
    top_stocks = sorted_stocks[:4]
    total_yield_score = sum(s["div_yield"] for s in top_stocks)

    # Распределяем бюджет пропорционально доходности
    allocations = []
    total_annual_income = 0.0

    for stock in top_stocks:
        weight = stock["div_yield"] / total_yield_score
        alloc_rub = round(budget * weight, 0)
        lots = max(1, floor(alloc_rub / stock["one_lot_rub"]))
        actual_alloc = lots * stock["one_lot_rub"]
        annual_income = actual_alloc * (stock["div_yield"] / 100)
        monthly_income = annual_income / 12

        total_annual_income += annual_income
        allocations.append({
            **stock,
            "weight_pct": round(weight * 100, 1),
            "alloc_rub": actual_alloc,
            "lots": lots,
            "annual_income": round(annual_income, 2),
            "monthly_income": round(monthly_income, 2),
        })

    avg_yield = (total_annual_income / budget * 100) if budget > 0 else 0
    total_monthly_income = total_annual_income / 12

    return {
        "allocations": allocations,
        "total_annual_income": round(total_annual_income, 2),
        "total_monthly_income": round(total_monthly_income, 2),
        "avg_yield_pct": round(avg_yield, 1),
        "why_chosen": _build_dividend_explanation(allocations, total_annual_income, avg_yield),
    }


def _build_dividend_explanation(allocations: list, total_income: float, avg_yield: float) -> str:
    lines = [f"📊 Средняя дивидендная доходность портфеля: {avg_yield:.1f}% годовых"]
    for a in allocations:
        lines.append(
            f"• {a['name']} ({a['ticker']}): {a['weight_pct']:.0f}% бюджета | "
            f"доходность {a['div_yield']}% | +{a['annual_income']:.0f}₽/год"
        )
    lines.append(f"💰 Итого: ~{total_income:.0f}₽/год (~{total_income/12:.0f}₽/мес)")
    return "\n".join(lines)

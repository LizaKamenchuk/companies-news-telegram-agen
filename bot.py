import aiohttp
import asyncio
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Set, List

import pytz
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message
from dateutil import parser as dtparser


# ====== Конфиг из env ======
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SERPAPI_KEY = os.getenv("SERPAPI_KEY")
ALPHAVANTAGE_KEY = os.getenv("ALPHAVANTAGE_KEY")
FINNHUB_KEY = os.getenv("FINNHUB_KEY")
TWELVEDATA_KEY = os.getenv("TWELVEDATA_KEY")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")
LANG = os.getenv("NEWS_LANG", "ru")  # ru | pl | en

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
if not SERPAPI_KEY:
    raise RuntimeError("Missing SERPAPI_KEY")
if not ALPHAVANTAGE_KEY:
    raise RuntimeError("Missing ALPHAVANTAGE_KEY")

TZ = pytz.timezone("Europe/Warsaw")


# ====== Состояние подписок (в памяти) ======
@dataclass
class ChatState:
    companies: Set[str] = field(default_factory=set)  # названия компаний для новостей
    tickers: Set[str] = field(default_factory=set)  # тикеры для цен
    news_seen_ids: Set[str] = field(default_factory=set)  # чтобы не дублировать новости
    interval_min: int = 10
    price_threshold_pct: float = 2.0
    running: bool = False
    task: asyncio.Task | None = None


STATES: Dict[int, ChatState] = {}  # chat_id -> ChatState
dp = Dispatcher()


# ====== Утилиты ======
def now_tz() -> datetime:
    return datetime.now(TZ)


def parse_relative_date(text: str) -> datetime | None:
    # SerpAPI иногда: "1 hour ago", "2 days ago", "Just now"
    text = (text or "").lower()
    if not text:
        return None
    if "just now" in text:
        return now_tz()
    m = re.search(r"(\d+)\s+(minute|hour|day)", text)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        base = now_tz()
        if unit == "minute": return base - timedelta(minutes=n)
        if unit == "hour":   return base - timedelta(hours=n)
        if unit == "day":    return base - timedelta(days=n)
    # иногда приходит ISO
    try:
        return dtparser.isoparse(text).astimezone(TZ)
    except Exception:
        return None


def short(src: str, maxlen=64):
    s = (src or "").strip()
    return s if len(s) <= maxlen else s[:maxlen - 1] + "…"


# ====== Провайдеры ======
async def fetch_serpapi_news(session: aiohttp.ClientSession, query: str, num: int = 6):
    url = "https://serpapi.com/search.json"
    params = {
        "engine": "google_news",
        "q": query,
        "hl": LANG,
        "num": num,
        "api_key": SERPAPI_KEY,
        "tbs": "qdr:h"  # за последний час; можно qdr:d — за сутки
    }
    async with session.get(url, params=params, timeout=20) as r:
        r.raise_for_status()
        data = await r.json()
    results = []
    for v in (data.get("news_results") or [])[:num]:
        title = (v.get("title") or "").strip()
        link = v.get("link")
        source = (v.get("source") or {}).get("name", "")
        date_raw = v.get("date")
        dt = parse_relative_date(date_raw)
        # ID новости — по ссылке/тайтлу/дате, чтобы отсечь дубли
        nid = f"{title}|{link}|{date_raw}"
        results.append({
            "id": nid,
            "title": title,
            "url": link,
            "source": source,
            "dt": dt
        })
    return results


# ====== Источники котировок (цен) с одинаковым интерфейсом ======

async def fetch_alpha_global_quote(session: aiohttp.ClientSession, symbol: str):
    """Alpha Vantage GLOBAL_QUOTE: (price, change_pct) | (None, None)"""
    if not ALPHAVANTAGE_KEY:
        return None, None
    url = "https://www.alphavantage.co/query"
    params = {"function": "GLOBAL_QUOTE", "symbol": symbol, "apikey": ALPHAVANTAGE_KEY}
    try:
        async with session.get(url, params=params, timeout=20) as r:
            if r.status == 429:
                return None, None
            r.raise_for_status()
            data = await r.json()
        q = data.get("Global Quote") or {}
        price = q.get("05. price")
        chg_pct = q.get("10. change percent")
        if price is None or chg_pct is None:
            return None, None
        return float(price), float(chg_pct.rstrip("%"))
    except Exception:
        return None, None


async def fetch_finnhub_quote(session: aiohttp.ClientSession, symbol: str):
    """Finnhub /quote: c=current, dp=percent change"""
    if not FINNHUB_KEY:
        return None, None
    url = "https://finnhub.io/api/v1/quote"
    params = {"symbol": symbol, "token": FINNHUB_KEY}
    try:
        async with session.get(url, params=params, timeout=20) as r:
            if r.status == 429:
                return None, None
            r.raise_for_status()
            data = await r.json()
        price = data.get("c")
        chg_pct = data.get("dp")
        if price is None or chg_pct is None:
            return None, None
        return float(price), float(chg_pct)
    except Exception:
        return None, None


async def fetch_twelvedata_price(session: aiohttp.ClientSession, symbol: str):
    """Twelve Data /price + /quote (для процента). Возвращаем (price, change_pct)."""
    if not TWELVEDATA_KEY:
        return None, None
    try:
        # цена
        url_p = "https://api.twelvedata.com/price"
        params_p = {"symbol": symbol, "apikey": TWELVEDATA_KEY}
        async with session.get(url_p, params=params_p, timeout=20) as r1:
            if r1.status == 429:
                return None, None
            r1.raise_for_status()
            data_p = await r1.json()
        price = data_p.get("price")
        if price is None:
            return None, None
        price = float(price)
        # процент изменения
        url_q = "https://api.twelvedata.com/quote"
        params_q = {"symbol": symbol, "apikey": TWELVEDATA_KEY}
        async with session.get(url_q, params=params_q, timeout=20) as r2:
            if r2.status == 429:
                return None, None
            r2.raise_for_status()
            data_q = await r2.json()
        chg_pct = data_q.get("percent_change")
        if chg_pct is None:
            return price, None
        return price, float(chg_pct)
    except Exception:
        return None, None


async def fetch_yahoo_via_rapidapi(session: aiohttp.ClientSession, symbol: str):
    """Yahoo Finance через RapidAPI (опционально). Возвращаем (price, change_pct)."""
    if not RAPIDAPI_KEY:
        return None, None
    url = "https://yahoo-finance127.p.rapidapi.com/price"
    headers = {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": "yahoo-finance127.p.rapidapi.com",
    }
    params = {"symbol": symbol}
    try:
        async with session.get(url, headers=headers, params=params, timeout=20) as r:
            if r.status == 429:
                return None, None
            r.raise_for_status()
            data = await r.json()
        # формат может отличаться по провайдеру; часто есть fields regularMarketPrice / regularMarketChangePercent
        quote = data.get("price") or data
        price = (
            quote.get("regularMarketPrice", {}).get("raw")
            if isinstance(quote.get("regularMarketPrice"), dict)
            else quote.get("regularMarketPrice")
        )
        chg_pct = (
            quote.get("regularMarketChangePercent", {}).get("raw")
            if isinstance(quote.get("regularMarketChangePercent"), dict)
            else quote.get("regularMarketChangePercent")
        )
        if price is None or chg_pct is None:
            return None, None
        return float(price), float(chg_pct)
    except Exception:
        return None, None


async def get_stock_price(session: aiohttp.ClientSession, symbol: str):
    """
    Fallback-цепочка:
        Alpha Vantage -> Finnhub -> Twelve Data -> Yahoo(RapidAPI).
    Возвращаем (price, change_pct, provider) или (None, None, 'none')
    """
    # 1) Alpha Vantage
    p, c = await fetch_alpha_global_quote(session, symbol)
    if p is not None and c is not None:
        return p, c, "AlphaVantage"

    # 2) Finnhub
    p, c = await fetch_finnhub_quote(session, symbol)
    if p is not None and c is not None:
        return p, c, "Finnhub"

    # 3) Twelve Data
    p, c = await fetch_twelvedata_price(session, symbol)
    if p is not None and c is not None:
        return p, c, "TwelveData"

    # 4) Yahoo (RapidAPI)
    p, c = await fetch_yahoo_via_rapidapi(session, symbol)
    if p is not None and c is not None:
        return p, c, "Yahoo(RapidAPI)"

    return None, None, "none"


# ====== Фоновая задача ======
async def monitor_chat(bot: Bot, chat_id: int):
    state = STATES[chat_id]
    async with aiohttp.ClientSession() as session:
        while state.running:
            start_cycle = now_tz()
            msgs: List[str] = []

            # --- Новости по компаниям ---
            for company in sorted(state.companies):
                try:
                    news = await fetch_serpapi_news(session, company, num=6)
                    fresh = []
                    for n in news:
                        # фильтруем новые (не виденные) и достаточно свежие (за интервал)
                        if n["id"] in state.news_seen_ids:
                            continue
                        if n["dt"] and (start_cycle - n["dt"]).total_seconds() > state.interval_min * 60 + 120:
                            continue
                        fresh.append(n)
                        state.news_seen_ids.add(n["id"])
                    for n in fresh:
                        when = n["dt"].strftime("%Y-%m-%d %H:%M") if n["dt"] else ""
                        src = f" — {short(n['source'])}" if n["source"] else ""
                        ds = f" ({when})" if when else ""
                        msgs.append(f"📰 {company}{src}{ds}\n{n['title']}\n{n['url']}")
                except Exception:
                    # не падаем из-за одного провайдера
                    pass

            # --- Цены по тикерам ---
            for t in sorted(state.tickers):
                try:
                    price, chg, provider = await get_stock_price(session, t)
                    if price is None or chg is None:
                        continue
                    if abs(chg) >= state.price_threshold_pct:
                        arrow = "📈" if chg > 0 else "📉"
                        msgs.append(
                            f"{arrow} {t}: {price:.2f} USD ({chg:+.2f}%) • {provider}\n"
                            f"https://finance.yahoo.com/quote/{t}"
                        )
                except Exception:
                    pass

        # Отправляем сгруппировано, чтобы не спамить
        if msgs:
            text = "\n\n".join(msgs)
            # бьем на куски < 4000 символов
            for chunk in split_message(text):
                await bot.send_message(chat_id, chunk, disable_web_page_preview=False)

        # Ждем до следующего цикла
        await asyncio.sleep(state.interval_min * 60)


def split_message(text: str, limit: int = 4000):
    if len(text) <= limit:
        return [text]
    parts, buf, size = [], [], 0
    for block in text.split("\n\n"):
        if size + len(block) + 2 > limit:
            parts.append("\n\n".join(buf))
            buf, size = [block], len(block)
        else:
            buf.append(block);
            size += len(block) + 2
    if buf:
        parts.append("\n\n".join(buf))
    return parts


# ====== Команды ======
@dp.message(CommandStart())
async def cmd_start(m: Message):
    state = STATES.setdefault(m.chat.id, ChatState())
    await m.answer(
        "Привет! Я буду присылать свежие новости о компаниях и оповещать об изменениях цены акций.\n\n"
        "Команды:\n"
        "/watch_company <название>\n"
        "/unwatch_company <название>\n"
        "/watch_ticker <тикер>\n"
        "/unwatch_ticker <тикер>\n"
        "/list — показать подписки\n"
        f"/interval <минуты> — сейчас {state.interval_min}\n"
        f"/threshold <проценты> — сейчас {state.price_threshold_pct}%\n"
        "/start_feed — запустить мониторинг\n"
        "/stop_feed — остановить мониторинг"
    )


@dp.message(Command("watch_company"))
async def watch_company(m: Message):
    q = (m.text or "").split(maxsplit=1)
    if len(q) < 2:
        await m.answer("Использование: /watch_company <название компании>")
        return
    name = q[1].strip()
    state = STATES.setdefault(m.chat.id, ChatState())
    state.companies.add(name)
    await m.answer(f"Добавил компанию: «{name}». Использую Google News (SerpAPI).")


@dp.message(Command("unwatch_company"))
async def unwatch_company(m: Message):
    q = (m.text or "").split(maxsplit=1)
    if len(q) < 2:
        await m.answer("Использование: /unwatch_company <название компании>")
        return
    name = q[1].strip()
    state = STATES.setdefault(m.chat.id, ChatState())
    if name in state.companies:
        state.companies.remove(name)
        await m.answer(f"Убрал компанию: «{name}».")
    else:
        await m.answer("Такой компании нет в списке.")


@dp.message(Command("watch_ticker"))
async def watch_ticker(m: Message):
    q = (m.text or "").split(maxsplit=1)
    if len(q) < 2:
        await m.answer("Использование: /watch_ticker <тикер>, например /watch_ticker NVDA")
        return
    t = q[1].strip().upper()
    state = STATES.setdefault(m.chat.id, ChatState())
    state.tickers.add(t)
    await m.answer(f"Добавил тикер: {t}. Источник цен — Alpha Vantage.")


@dp.message(Command("unwatch_ticker"))
async def unwatch_ticker(m: Message):
    q = (m.text or "").split(maxsplit=1)
    if len(q) < 2:
        await m.answer("Использование: /unwatch_ticker <тикер>")
        return
    t = q[1].strip().upper()
    state = STATES.setdefault(m.chat.id, ChatState())
    if t in state.tickers:
        state.tickers.remove(t)
        await m.answer(f"Убрал тикер: {t}.")
    else:
        await m.answer("Этого тикера нет в списке.")


@dp.message(Command("list"))
async def cmd_list(m: Message):
    state = STATES.setdefault(m.chat.id, ChatState())
    companies = ", ".join(sorted(state.companies)) or "—"
    tickers = ", ".join(sorted(state.tickers)) or "—"
    await m.answer(
        f"Компании: {companies}\n"
        f"Тикеры: {tickers}\n"
        f"Интервал: {state.interval_min} мин\n"
        f"Порог цены: {state.price_threshold_pct}%\n"
        f"Мониторинг: {'включен' if state.running else 'выключен'}"
    )


@dp.message(Command("interval"))
async def cmd_interval(m: Message):
    q = (m.text or "").split(maxsplit=1)
    if len(q) < 2 or not q[1].isdigit():
        await m.answer("Использование: /interval <минуты>, напр. /interval 10")
        return
    state = STATES.setdefault(m.chat.id, ChatState())
    state.interval_min = max(2, int(q[1]))  # не меньше 2 минут
    await m.answer(f"Интервал проверок: {state.interval_min} минут.")


@dp.message(Command("threshold"))
async def cmd_threshold(m: Message):
    q = (m.text or "").split(maxsplit=1)
    if len(q) < 2:
        await m.answer("Использование: /threshold <проценты>, напр. /threshold 2.5")
        return
    try:
        val = float(q[1].replace(",", "."))
    except ValueError:
        await m.answer("Неверное число. Пример: /threshold 1.5")
        return
    state = STATES.setdefault(m.chat.id, ChatState())
    state.price_threshold_pct = max(0.1, val)
    await m.answer(f"Порог уведомления по цене: {state.price_threshold_pct}%.")


@dp.message(Command("start_feed"))
async def start_feed(m: Message):
    state = STATES.setdefault(m.chat.id, ChatState())
    if state.running:
        await m.answer("Мониторинг уже запущен.")
        return
    state.running = True
    bot = Bot(TELEGRAM_BOT_TOKEN)
    state.task = asyncio.create_task(monitor_chat(bot, m.chat.id))
    await m.answer("Мониторинг запущен ✅")


@dp.message(Command("stop_feed"))
async def stop_feed(m: Message):
    state = STATES.setdefault(m.chat.id, ChatState())
    state.running = False
    if state.task and not state.task.done():
        state.task.cancel()
    await m.answer("Мониторинг остановлен ⏸️")


@dp.message(F.text & ~F.via_bot)
async def fallback(m: Message):
    await m.answer("Неизвестная команда. Используй /help или /start.")


@dp.message(Command("help"))
async def help_cmd(m: Message):
    await cmd_start(m)


# ====== Запуск ======
async def main():
    bot = Bot(TELEGRAM_BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

import asyncio
import os
import hashlib
import feedparser
from datetime import datetime, timedelta
from collections import defaultdict

from aiogram import Bot
from aiohttp import web

# =========================
# НАСТРОЙКИ
# =========================

TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = -1003813589198

CHECK_INTERVAL = 3600  # проверка каждый час
DAILY_REPORT_HOUR = 20  # ежедневный отчёт в 20:00 (Алматы)

bot = Bot(token=TOKEN)

# =========================
# ГЕО-ФИЛЬТР
# =========================

GEO_KEYWORDS = [
    # --- Области ---
    "алтайский край",
    "новосибирская область",

    # --- Приграничные города ---
    "рубцовск",
    "славгород",
    "змеиногорск",
    "кулунда",
    "карасук",
    "купино",
    "баган",
    "чистоозер",

    # --- Барнаульский узел ---
    "барнаул",
    "новоалтайск",
    "павловск",
    "первомайск",
    "калманка",
    "тальменка",
    "заринск",
    "косиха",
    "ребриха",

    # --- Дополнительные маркеры ---
    "граница",
    "кпп",
    "казахстан",
]

# =========================
# ВЕСА РИСКА
# =========================

ECON_WEIGHTS = {
    "ящур": 5,
    "африканская чума": 5,
    "грипп птиц": 5,
    "карантин": 4,
    "запрет": 4,
    "эпидем": 5,
    "скот": 3,
    "птицефабрика": 3,
    "закрытие трассы": 3,
    "сход поезда": 4,
    "авария": 3,
    "мост": 4,
    "отключение газа": 4,
    "тэц": 4,
    "лэп": 4,
}

SEC_WEIGHTS = {
    "учения": 4,
    "погранич": 4,
    "усиление": 3,
    "перекрытие": 4,
    "проверки": 3,
}

# =========================
# RSS ИСТОЧНИКИ
# =========================

RSS_FEEDS = [
    # Алтай
    "https://news.google.com/rss/search?q=Алтайский+край+Казахстан&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=Барнаул+Алтайский+край&hl=ru&gl=RU&ceid=RU:ru",

    # Новосибирская область
    "https://news.google.com/rss/search?q=Новосибирская+область+Казахстан&hl=ru&gl=RU&ceid=RU:ru",
]

# =========================
# ХРАНЕНИЕ СОСТОЯНИЯ
# =========================

sent_news = set()
daily_stats = defaultdict(int)
weekly_history = []

# =========================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =========================

def is_relevant(title: str) -> bool:
    t = title.lower()
    return any(word in t for word in GEO_KEYWORDS)

def calculate_score(title: str):
    score = 0
    triggers = []
    t = title.lower()

    for word, weight in ECON_WEIGHTS.items():
        if word in t:
            score += weight
            triggers.append(word)

    for word, weight in SEC_WEIGHTS.items():
        if word in t:
            score += weight
            triggers.append(word)

    return min(score, 10), triggers

def risk_level(score: int):
    if score >= 7:
        return "🔴 Повышенный"
    elif score >= 4:
        return "🟡 Умеренный"
    else:
        return "🟢 Низкий"

def impact_forecast(triggers):
    text = " ".join(triggers)

    if any(x in text for x in ["ящур", "эпидем", "грипп", "скот"]):
        return "Возможное влияние: ограничения поставок агропродукции и рост цен."

    if any(x in text for x in ["мост", "сход поезда", "трассы"]):
        return "Возможное влияние: перебои и задержки в логистике."

    if any(x in text for x in ["газ", "тэц", "лэп"]):
        return "Возможное влияние: риски для энергоснабжения и инфраструктуры."

    return "Возможное влияние: требуется дополнительная оценка."

def hash_news(title, link):
    return hashlib.md5((title + link).encode()).hexdigest()

# =========================
# ОСНОВНАЯ ЛОГИКА
# =========================

async def check_news():
    global daily_stats

    for feed_url in RSS_FEEDS:
        feed = feedparser.parse(feed_url)

        for entry in feed.entries:
            title = entry.title
            link = entry.link

            if not is_relevant(title):
                continue

            news_id = hash_news(title, link)
            if news_id in sent_news:
                continue

            score, triggers = calculate_score(title)
            level = risk_level(score)
            forecast = impact_forecast(triggers)

            critical = ""
            if score >= 9:
                critical = "\n⚠️ КРИТИЧЕСКИЙ СИГНАЛ\n"

            text = (
                f"🚨 OSINT ABAY\n\n"
                f"{title}\n\n"
                f"Риск: {level} ({score}/10)\n"
                f"{critical}"
                f"{forecast}\n\n"
                f"Источник:\n{link}"
            )

            try:
                await bot.send_message(CHANNEL_ID, text)
                sent_news.add(news_id)
                daily_stats["total"] += 1
                daily_stats["risk_sum"] += score
                if score >= 7:
                    daily_stats["high"] += 1

                await asyncio.sleep(2)

            except Exception as e:
                if "RetryAfter" in str(e):
                    await asyncio.sleep(40)
                else:
                    print("Ошибка отправки:", e)

async def daily_report_loop():
    global daily_stats, weekly_history

    while True:
        now = datetime.utcnow() + timedelta(hours=6)  # Алматы

        if now.hour == DAILY_REPORT_HOUR:
            total = daily_stats["total"]
            avg = (
                round(daily_stats["risk_sum"] / total, 2)
                if total > 0 else 0
            )
            high = daily_stats["high"]

            report = (
                "📊 Итог дня\n\n"
                f"Всего сигналов: {total}\n"
                f"Критических: {high}\n"
                f"Средний риск: {avg}\n"
            )

            await bot.send_message(CHANNEL_ID, report)

            weekly_history.append(avg)
            if len(weekly_history) > 7:
                weekly_history.pop(0)

            trend = "Стабильно"
            if len(weekly_history) >= 2:
                if weekly_history[-1] > weekly_history[-2]:
                    trend = "Рост"
                elif weekly_history[-1] < weekly_history[-2]:
                    trend = "Снижение"

            await bot.send_message(
                CHANNEL_ID,
                f"📈 Динамика 7 дней: {trend}"
            )

            daily_stats = defaultdict(int)

            await asyncio.sleep(3600)

        await asyncio.sleep(60)

# =========================
# WEB ENDPOINT ДЛЯ RENDER
# =========================

async def healthcheck(request):
    return web.Response(text="OK")

async def main():
    app = web.Application()
    app.router.add_get("/", healthcheck)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    asyncio.create_task(daily_report_loop())

    while True:
        await check_news()
        await asyncio.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
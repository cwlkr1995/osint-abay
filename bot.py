import os
import asyncio
import feedparser
from aiogram import Bot
from aiohttp import web

CHANNEL_ID = -1003813589198
TOKEN = os.getenv("BOT_TOKEN")  # токен берём из Render Environment

KEYWORDS = [
    "Алтайский край",
    "Рубцовск",
    "Славгород",
    "Змеиногорск",
    "Кулунда",
    "Новосибирская область",
    "Карасук",
    "Барабинск",
    "граница",
    "Казахстан",
    "погранич",
]

RSS_FEEDS = [
    "https://news.google.com/rss/search?q=Алтайский+край+граница+Казахстан&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=Новосибирская+область+граница+Казахстан&hl=ru&gl=RU&ceid=RU:ru",
]

sent_news = set()

async def check_news(bot: Bot):
    for feed_url in RSS_FEEDS:
        feed = feedparser.parse(feed_url)
        for entry in feed.entries:
            title = entry.get("title", "")
            link = entry.get("link", "")

            if not title:
                continue

            if any(word.lower() in title.lower() for word in KEYWORDS):
                if title not in sent_news:
                    sent_news.add(title)
                    text = f"🚨 Новость (приграничье РФ)\n\n{title}\n\nИсточник:\n{link}"
                    await bot.send_message(CHANNEL_ID, text)

async def news_loop(bot: Bot):
    while True:
        await check_news(bot)
        await asyncio.sleep(3600)  # проверка каждый час

async def handle_root(request):
    return web.Response(text="OK")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_root)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()

async def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not set (Render -> Environment).")

    bot = Bot(token=TOKEN)

    # Запускаем веб-сервер + мониторинг одновременно
    await start_web_server()
    await news_loop(bot)

if __name__ == "__main__":
    asyncio.run(main())
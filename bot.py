import os
import asyncio
import feedparser
from aiogram import Bot

TOKEN = os.getenv("BOT_TOKEN")"
CHANNEL_ID = -1003813589198

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
    "погранич"
]

RSS_FEEDS = [
    "https://news.google.com/rss/search?q=Алтайский+край+граница+Казахстан&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=Новосибирская+область+граница+Казахстан&hl=ru&gl=RU&ceid=RU:ru"
]

sent_news = set()

async def check_news(bot):
    found = False

    for feed_url in RSS_FEEDS:
        feed = feedparser.parse(feed_url)

        for entry in feed.entries:
            title = entry.title
            link = entry.link

            if any(word.lower() in title.lower() for word in KEYWORDS):
                if title not in sent_news:
                    sent_news.add(title)
                    found = True

                    text = f"""
🚨 Новость (приграничье РФ)

{title}

Источник:
{link}
"""
                    await bot.send_message(CHANNEL_ID, text)

    if not found:
        await bot.send_message(CHANNEL_ID, "ℹ️ Новых приграничных новостей не обнаружено")

async def main():
    bot = Bot(token=TOKEN)

    while True:
        await check_news(bot)
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
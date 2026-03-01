import os
import asyncio
import json
import hashlib
import feedparser
from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiohttp import web

CHANNEL_ID = -1003813589198
TOKEN = os.getenv("BOT_TOKEN")

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

STATE_FILE = "sent_news.json"
CHECK_EVERY_SECONDS = 3600
SEND_DELAY_SECONDS = 2           # пауза между сообщениями (анти-флуд)
MAX_SEND_PER_CYCLE = 10          # максимум сообщений за одну проверку

def load_sent() -> set[str]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return set(data)
    except FileNotFoundError:
        pass
    except Exception:
        # если файл битый — начнём заново
        pass
    return set()

def save_sent(sent: set[str]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(list(sent))[-5000:], f, ensure_ascii=False)  # ограничим рост

def entry_id(title: str, link: str) -> str:
    # устойчивый ID, чтобы не зависеть только от заголовка
    raw = (title.strip() + "|" + link.strip()).encode("utf-8", errors="ignore")
    return hashlib.sha256(raw).hexdigest()

async def safe_send(bot: Bot, text: str) -> None:
    while True:
        try:
            await bot.send_message(CHANNEL_ID, text)
            return
        except TelegramRetryAfter as e:
            # Telegram сам говорит сколько ждать
            await asyncio.sleep(int(getattr(e, "retry_after", 30)) + 1)

async def check_news(bot: Bot, sent: set[str]) -> int:
    to_send: list[tuple[str, str]] = []

    for feed_url in RSS_FEEDS:
        feed = feedparser.parse(feed_url)
        for entry in feed.entries:
            title = entry.get("title", "")
            link = entry.get("link", "")
            if not title or not link:
                continue

            if not any(k.lower() in title.lower() for k in KEYWORDS):
                continue

            eid = entry_id(title, link)
            if eid in sent:
                continue

            to_send.append((title, link))

    # ограничим объем, чтобы не флудить при рестарте
    to_send = to_send[:MAX_SEND_PER_CYCLE]

    sent_count = 0
    for title, link in to_send:
        text = f"🚨 Новость (приграничье РФ)\n\n{title}\n\nИсточник:\n{link}"
        await safe_send(bot, text)
        sent.add(entry_id(title, link))
        sent_count += 1
        await asyncio.sleep(SEND_DELAY_SECONDS)

    if sent_count:
        save_sent(sent)

    return sent_count

# --- tiny web server for Render ---
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

async def news_loop(bot: Bot):
    sent = load_sent()

    # чтобы при первом запуске не улететь в лимиты — можно "прогреть" базу:
    # просто загрузим ленты и отметим первые N как уже виденные
    if not sent:
        warmup = 0
        for feed_url in RSS_FEEDS:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                title = entry.get("title", "")
                link = entry.get("link", "")
                if title and link:
                    sent.add(entry_id(title, link))
                    warmup += 1
                    if warmup >= 40:
                        break
            if warmup >= 40:
                break
        save_sent(sent)

    while True:
        try:
            await check_news(bot, sent)
        except Exception:
            # чтобы сервис не падал из-за разовых ошибок сети
            await asyncio.sleep(15)
        await asyncio.sleep(CHECK_EVERY_SECONDS)

async def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not set (Render -> Environment).")

    bot = Bot(token=TOKEN)

    await start_web_server()
    await news_loop(bot)

if __name__ == "__main__":
    asyncio.run(main())
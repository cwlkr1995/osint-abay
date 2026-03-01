import os
import asyncio
import json
import hashlib
import feedparser
from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiohttp import web

# =========================
# Render / Telegram settings
# =========================
CHANNEL_ID = -1003813589198
TOKEN = os.getenv("BOT_TOKEN")  # задаётся в Render -> Environment

# =========================
# Monitoring configuration
# =========================
# Сайты/поиск (Google News RSS по нужным запросам)
RSS_FEEDS = [
    "https://news.google.com/rss/search?q=Алтайский+край+граница+Казахстан&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=Новосибирская+область+граница+Казахстан&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=Рубцовск+Казахстан+граница&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=Карасук+Казахстан+граница&hl=ru&gl=RU&ceid=RU:ru",
]

# Гео/контекст: фильтрация по заголовку (можно расширять)
GEO_KEYWORDS = [
    "алтайский край",
    "новосибирская область",
    "рубцовск",
    "славгород",
    "змеиногорск",
    "кулунда",
    "карасук",
    "куйбышев",  # при желании убери/добавь
    "баган",
    "купино",
    "чистоозёр",
    "границ",
    "казахстан",
    "кпп",
]

# =========================
# Analytics: weights / logic
# =========================
# Экономические ключи и веса (агро, логистика, энергетика)
ECON_WEIGHTS = {
    # агро
    "эпизоот": 5,
    "ящур": 5,
    "африканская чума": 5,
    "ачс": 5,
    "грипп птиц": 5,
    "птич": 3,
    "карантин": 4,
    "ветслужб": 3,
    "россельхознадзор": 3,
    "запрет на вывоз": 4,
    "запрет на ввоз": 4,
    "ограничен": 3,
    "скот": 3,
    "ферм": 2,
    "падеж": 4,
    # логистика
    "закрыт": 3,  # закрыта/закрыли
    "перекрыт": 4,
    "сход поезда": 4,
    "авария": 3,
    "дтп": 2,
    "повреждение моста": 4,
    "обрушение моста": 5,
    "железн": 2,
    "жд": 2,
    "очеред": 3,  # очереди
    "простой": 2,
    "груз": 2,
    "перевоз": 2,
    "логист": 2,
    # энергетика
    "авария на тэц": 4,
    "тэц": 2,
    "отключение газа": 4,
    "газ": 2,
    "лэп": 3,
    "повреждение лэп": 4,
    "отключение света": 3,
    "электр": 2,
    "уголь": 2,
}

# Безопасность/ограничения и веса
SEC_WEIGHTS = {
    "учен": 4,         # учения
    "военн": 5,        # военные
    "погран": 4,       # пограничники/погранслужба
    "фсб": 3,
    "росгвард": 3,
    "усилен": 3,       # усиление
    "режим": 3,
    "провер": 3,       # проверки
    "досмотр": 3,
    "перекрыт": 4,
    "кпп": 3,
    "тамож": 3,
}

# =========================
# Runtime controls (anti-flood)
# =========================
STATE_FILE = "sent_news.json"
CHECK_EVERY_SECONDS = 3600       # проверка раз в час
SEND_DELAY_SECONDS = 2           # пауза между сообщениями
MAX_SEND_PER_CYCLE = 10          # максимум отправок за один цикл
WARMUP_MARK_AS_SEEN = 40         # сколько первых новостей пометить как "уже виденные" на первом запуске

# =========================
# Helpers: state persistence
# =========================
def load_sent() -> set[str]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return set(data)
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return set()

def save_sent(sent: set[str]) -> None:
    # ограничим рост файла
    data = sorted(list(sent))[-5000:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

def entry_id(title: str, link: str) -> str:
    raw = (title.strip() + "|" + link.strip()).encode("utf-8", errors="ignore")
    return hashlib.sha256(raw).hexdigest()

# =========================
# Analytics: scoring & forecast
# =========================
def _match_weights(title: str, weights: dict[str, int]) -> tuple[int, list[str]]:
    score = 0
    triggers: list[str] = []
    t = title.lower()
    for k, w in weights.items():
        if k in t:
            score += w
            triggers.append(k)
    return score, triggers

def calculate_score(title: str) -> tuple[int, list[str], str]:
    econ_score, econ_tr = _match_weights(title, ECON_WEIGHTS)
    sec_score, sec_tr = _match_weights(title, SEC_WEIGHTS)
    total = econ_score + sec_score
    triggers = econ_tr + sec_tr

    # Категория по доминирующим триггерам
    if econ_score >= sec_score and econ_score > 0:
        category = "Экономика"
    elif sec_score > 0:
        category = "Безопасность/ограничения"
    else:
        category = "Фон"

    # нормируем "из 10" (условно): не даём улетать в 99
    total10 = min(10, total)
    return total10, triggers, category

def risk_level(score10: int) -> str:
    if score10 >= 7:
        return "🔴 Повышенный"
    if score10 >= 4:
        return "🟡 Умеренный"
    return "🟢 Низкий"

def impact_forecast(triggers: list[str], category: str) -> str:
    t = " ".join(triggers)

    # Агро
    if any(x in t for x in ["ящур", "эпизоот", "ачс", "грипп птиц", "скот", "падеж"]):
        return "Возможное влияние на Казахстан: ограничения по ввозу/вывозу агропродукции, рост цен, усиление ветконтроля."

    # Логистика
    if any(x in t for x in ["сход поезда", "моста", "перекрыт", "закрыт", "очеред", "простой", "груз", "перевоз", "логист"]):
        return "Возможное влияние на Казахстан: задержки поставок, рост логистических издержек, локальный дефицит отдельных товаров."

    # Энергетика
    if any(x in t for x in ["тэц", "отключение газа", "лэп", "электр", "уголь"]):
        return "Возможное влияние на Казахстан: риски энергоснабжения/поставок топлива и вторичные логистические сбои."

    # Безопасность/режим
    if category == "Безопасность/ограничения":
        return "Возможное влияние на Казахстан: усиление контроля/проверок, замедление перемещения грузов и людей, рост транзакционных издержек."

    return "Возможное влияние на Казахстан: требуется уточнение контекста (пока сигнал слабый)."

def geo_relevant(title: str) -> bool:
    t = title.lower()
    return any(k in t for k in GEO_KEYWORDS)

# =========================
# Telegram sending with backoff
# =========================
async def safe_send(bot: Bot, text: str) -> None:
    while True:
        try:
            await bot.send_message(CHANNEL_ID, text)
            return
        except TelegramRetryAfter as e:
            # Telegram говорит, сколько ждать
            wait_s = int(getattr(e, "retry_after", 30)) + 1
            await asyncio.sleep(wait_s)

# =========================
# Core: RSS -> filter -> analytics -> send
# =========================
async def check_news(bot: Bot, sent: set[str]) -> int:
    candidates: list[tuple[str, str]] = []

    for feed_url in RSS_FEEDS:
        feed = feedparser.parse(feed_url)
        for entry in feed.entries:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            if not title or not link:
                continue

            # гео-фильтр по заголовку
            if not geo_relevant(title):
                continue

            eid = entry_id(title, link)
            if eid in sent:
                continue

            candidates.append((title, link))

    # Чтобы не фладить — ограничим отправку
    candidates = candidates[:MAX_SEND_PER_CYCLE]

    sent_count = 0
    for title, link in candidates:
        score10, triggers, category = calculate_score(title)
        level = risk_level(score10)
        forecast = impact_forecast(triggers, category)

        msg = (
            f"🚨 OSINT ABAY — Сигнал\n\n"
            f"{title}\n\n"
            f"Категория: {category}\n"
            f"Риск: {level} ({score10}/10)\n"
            f"Триггеры: {', '.join(triggers) if triggers else 'нет'}\n\n"
            f"{forecast}\n\n"
            f"Источник:\n{link}"
        )

        await safe_send(bot, msg)
        sent.add(entry_id(title, link))
        sent_count += 1
        await asyncio.sleep(SEND_DELAY_SECONDS)

    if sent_count:
        save_sent(sent)

    return sent_count

async def warmup_seen(sent: set[str]) -> None:
    if sent:
        return

    warmup = 0
    for feed_url in RSS_FEEDS:
        feed = feedparser.parse(feed_url)
        for entry in feed.entries:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            if not title or not link:
                continue
            sent.add(entry_id(title, link))
            warmup += 1
            if warmup >= WARMUP_MARK_AS_SEEN:
                break
        if warmup >= WARMUP_MARK_AS_SEEN:
            break

    save_sent(sent)

async def news_loop(bot: Bot):
    sent = load_sent()
    await warmup_seen(sent)

    while True:
        try:
            await check_news(bot, sent)
        except Exception:
            # не падаем при разовых сетевых/парсинговых ошибках
            await asyncio.sleep(15)
        await asyncio.sleep(CHECK_EVERY_SECONDS)

# =========================
# Tiny web server for Render
# =========================
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

# =========================
# Main
# =========================
async def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not set (Render -> Environment).")

    bot = Bot(token=TOKEN)

    # Запускаем веб-сервер и мониторинг параллельно
    await start_web_server()
    await news_loop(bot)

if __name__ == "__main__":
    asyncio.run(main())
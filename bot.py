import os
import asyncio
import json
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dtime, timezone
from email.utils import parsedate_to_datetime

import feedparser
from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiohttp import web

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Almaty")
except Exception:
    TZ = timezone(timedelta(hours=5))

# =========================
# Render / Telegram settings
# =========================
CHANNEL_ID = -1003813589198
TOKEN = os.getenv("BOT_TOKEN")  # Render -> Environment

# =========================
# STRICT freshness filter
# =========================
MAX_AGE_HOURS = 24          # только последние 24 часа
FUTURE_LEEWAY_MIN = 10      # допускаем до 10 минут "в будущем" из-за кривых дат источника

ARCHIVE_PHRASES = [
    "год назад",
    "в прошлом году",
    "в позапрошлом году",
    "архив",
    "ретроспектив",
    "вспомним",
    "в этот день",
    "годовщина",
    "итоги 20",
    "обзор за",
    "хроника",
]

YEAR_RE = re.compile(r"\b(20\d{2})\b")

def _get_entry_datetime_utc(entry) -> datetime | None:
    """
    Достаём datetime публикации из RSS.
    1) published_parsed / updated_parsed
    2) published / updated (строка RFC 2822 и т.п.)
    Всё приводим к UTC.
    """
    pub_struct = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if pub_struct:
        try:
            dt = datetime(*pub_struct[:6], tzinfo=timezone.utc)
            return dt
        except Exception:
            pass

    pub_str = entry.get("published") or entry.get("updated")
    if pub_str:
        try:
            dt = parsedate_to_datetime(pub_str)
            if dt.tzinfo is None:
                # если tz не указан, считаем UTC (лучше строго, чем ошибочно "свежее")
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None

    return None

def _looks_archival(title: str) -> bool:
    t = title.lower()

    # явные архивные/ретроспективные формулировки
    if any(p in t for p in ARCHIVE_PHRASES):
        return True

    # если в заголовке встречается "год" меньше текущего — почти наверняка архив/ретро/обзор
    now = datetime.now(timezone.utc)
    years = [int(y) for y in YEAR_RE.findall(t)]
    for y in years:
        if y <= now.year - 1:
            return True

    return False

def is_fresh_strict(entry, title: str, max_hours: int = MAX_AGE_HOURS) -> bool:
    """
    Строгая модель:
    - если дата публикации не найдена -> False
    - если заголовок выглядит архивным -> False
    - если старше max_hours -> False
    - если "в будущем" больше FUTURE_LEEWAY_MIN -> False
    """
    if _looks_archival(title):
        return False

    dt = _get_entry_datetime_utc(entry)
    if not dt:
        return False

    now = datetime.now(timezone.utc)

    # слишком "в будущем" — подозрительно
    if dt - now > timedelta(minutes=FUTURE_LEEWAY_MIN):
        return False

    age = now - dt
    return age <= timedelta(hours=max_hours)

# =========================
# RSS SOURCES (Google + прямые сайты)
# =========================
RSS_FEEDS = [
    # Google News (узкие запросы)
    "https://news.google.com/rss/search?q=Алтайский+край+граница+Казахстан&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=Новосибирская+область+граница+Казахстан&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=Барнаул+Алтайский+край&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=Барнаул+Казахстан+граница&hl=ru&gl=RU&ceid=RU:ru",

    # Региональные СМИ (RSS)
    "https://altapress.ru/rss",
    "https://www.amic.ru/rss",
    "https://ngs.ru/rss/",

    # Официальные источники (если RSS реально доступен)
    "https://www.altairegion22.ru/press-center/news/rss/",
    "https://www.nso.ru/rss",
    "https://fsvps.gov.ru/feed/",        # Россельхознадзор
    "https://zszd.rzd.ru/rss",           # Западно-Сибирская ЖД
]

# =========================
# Гео-фильтр
# =========================
GEO_KEYWORDS = [
    "алтайский край", "новосибирская область",
    "рубцовск", "славгород", "змеиногорск", "кулунда",
    "барнаул", "новоалтайск", "заринск", "тальменк", "калманк",
    "карасук", "купино", "баган", "чистооз",
    "границ", "кпп", "казахстан"
]

# =========================
# Весовая модель
# =========================
ECON_WEIGHTS = {
    "эпидем": 5, "эпизоот": 5, "ящур": 5, "ачс": 5,
    "африканская чума": 5, "грипп птиц": 5,
    "карантин": 4, "запрет на вывоз": 4, "запрет на ввоз": 4,
    "запрет": 3, "ограничен": 3,
    "скот": 3, "падеж": 4,
    "авария": 3, "сход поезда": 4,
    "повреждение моста": 4, "обрушение моста": 5, "мост": 3,
    "перекрыт": 4, "закрыт": 3,
    "очеред": 3, "простой": 2,
    "тэц": 2, "отключение газа": 4, "газ": 2,
    "лэп": 3, "повреждение лэп": 4, "отключение света": 3, "электр": 2,
}

SEC_WEIGHTS = {
    "погран": 4, "учен": 4, "военн": 5,
    "усилен": 3, "режим": 3,
    "провер": 3, "досмотр": 3,
    "перекрыт": 4, "кпп": 3, "тамож": 3,
}

# =========================
# Runtime controls
# =========================
STATE_FILE = "sent_news.json"
STATS_FILE = "daily_stats.json"

CHECK_EVERY_SECONDS = 3600
SEND_DELAY_SECONDS = 2
MAX_SEND_PER_CYCLE = 15
WARMUP_MARK_AS_SEEN = 40

DAILY_DIGEST_TIME_ALMATY = dtime(hour=20, minute=0)

# =========================
# Data models
# =========================
@dataclass
class Signal:
    title: str
    link: str
    score10: int
    category: str
    region: str
    is_critical: bool

# =========================
# JSON persistence
# =========================
def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception:
        return default

def save_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_sent() -> set[str]:
    data = load_json(STATE_FILE, [])
    return set(data) if isinstance(data, list) else set()

def save_sent(sent: set[str]) -> None:
    save_json(STATE_FILE, sorted(list(sent))[-8000:])

def entry_id(title: str, link: str) -> str:
    raw = (title.strip() + "|" + link.strip()).encode("utf-8", errors="ignore")
    return hashlib.sha256(raw).hexdigest()

# =========================
# Analytics: geo, scoring, critical, forecast
# =========================
def geo_relevant(title: str) -> bool:
    t = title.lower()
    return any(k in t for k in GEO_KEYWORDS)

def infer_region(title: str) -> str:
    t = title.lower()
    if any(x in t for x in ["барнаул", "новоалтайск", "заринск", "тальменк", "калманк"]):
        return "Алтайский край (Барнаульский узел)"
    if any(x in t for x in ["алтай", "рубцовск", "славгород", "змеиногорск", "кулунда"]):
        return "Алтайский край"
    if any(x in t for x in ["новосибир", "карасук", "купино", "баган", "чистооз"]):
        return "Новосибирская область"
    return "Приграничье РФ"

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
    score10 = min(10, total)

    if econ_score >= sec_score and econ_score > 0:
        category = "Экономика"
    elif sec_score > 0:
        category = "Безопасность/ограничения"
    else:
        category = "Фон"

    return score10, (econ_tr + sec_tr), category

def risk_level(score10: int) -> str:
    if score10 >= 7:
        return "🔴 Повышенный"
    if score10 >= 4:
        return "🟡 Умеренный"
    return "🟢 Низкий"

def impact_forecast(triggers: list[str], category: str) -> str:
    t = " ".join(triggers)
    if any(x in t for x in ["ящур", "эпидем", "эпизоот", "ачс", "африканская чума", "грипп птиц", "скот", "падеж", "карантин"]):
        return "Возможное влияние на Казахстан: ограничения по ввозу/вывозу агропродукции, рост цен, усиление ветконтроля."
    if any(x in t for x in ["сход поезда", "моста", "перекрыт", "закрыт", "очеред", "простой", "авария"]):
        return "Возможное влияние на Казахстан: задержки поставок, рост логистических издержек, локальный дефицит отдельных товаров."
    if any(x in t for x in ["тэц", "отключение газа", "лэп", "электр", "уголь", "отключение света"]):
        return "Возможное влияние на Казахстан: риски энергоснабжения/поставок топлива и вторичные логистические сбои."
    if category == "Безопасность/ограничения":
        return "Возможное влияние на Казахстан: усиление контроля/проверок, замедление перемещения грузов и людей, рост транзакционных издержек."
    return "Возможное влияние на Казахстан: требуется уточнение контекста (сигнал слабый)."

def is_critical_signal(score10: int, triggers: list[str]) -> bool:
    t = " ".join(triggers)
    if any(x in t for x in ["ящур", "эпидем", "эпизоот", "ачс", "африканская чума", "грипп птиц"]) and any(x in t for x in ["карантин", "запрет"]):
        return True
    if any(x in t for x in ["обрушение моста", "повреждение моста", "сход поезда", "перекрыт"]) and score10 >= 6:
        return True
    if any(x in t for x in ["погран", "кпп", "тамож", "режим", "провер", "досмотр", "усилен"]) and score10 >= 7:
        return True
    return score10 >= 9

# =========================
# Stats
# =========================
def today_key(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%Y-%m-%d")

def load_stats() -> dict:
    data = load_json(STATS_FILE, {})
    return data if isinstance(data, dict) else {}

def save_stats(stats: dict) -> None:
    save_json(STATS_FILE, stats)

def record_signal_to_stats(sig: Signal) -> None:
    stats = load_stats()
    key = today_key(datetime.now(TZ))
    day = stats.get(key, {
        "total": 0,
        "critical": 0,
        "avg_score_sum": 0,
        "by_category": {},
        "by_region": {},
    })
    day["total"] += 1
    day["critical"] += 1 if sig.is_critical else 0
    day["avg_score_sum"] += int(sig.score10)
    day["by_category"][sig.category] = day["by_category"].get(sig.category, 0) + 1
    day["by_region"][sig.region] = day["by_region"].get(sig.region, 0) + 1
    stats[key] = day

    keys_sorted = sorted(stats.keys())
    if len(keys_sorted) > 60:
        for k in keys_sorted[:-60]:
            stats.pop(k, None)

    save_stats(stats)

def compute_daily_digest(now: datetime) -> str:
    stats = load_stats()
    key = today_key(now)
    day = stats.get(key)

    if not day or day.get("total", 0) == 0:
        return (
            "📊 OSINT ABAY — Итог дня\n\n"
            f"Дата (Алматы): {now.astimezone(TZ).strftime('%d.%m.%Y')}\n\n"
            "Сигналов за день не зафиксировано.\n"
            "Фон: спокойный."
        )

    total = day.get("total", 0)
    critical = day.get("critical", 0)
    avg = round(day.get("avg_score_sum", 0) / max(1, total), 1)

    by_region = day.get("by_region", {})
    by_category = day.get("by_category", {})

    def fmt_map(m: dict) -> str:
        items = sorted(m.items(), key=lambda x: x[1], reverse=True)
        return "\n".join([f"• {k}: {v}" for k, v in items]) if items else "• нет"

    overall_risk = risk_level(int(round(avg)))

    return (
        "📊 OSINT ABAY — Итог дня\n\n"
        f"Дата (Алматы): {now.astimezone(TZ).strftime('%d.%m.%Y')}\n\n"
        f"Всего сигналов: {total}\n"
        f"Критические: {critical}\n"
        f"Средний риск: {overall_risk} ({avg}/10)\n\n"
        "По регионам:\n"
        f"{fmt_map(by_region)}\n\n"
        "По категориям:\n"
        f"{fmt_map(by_category)}"
    )

def compute_weekly_dynamics(now: datetime) -> str:
    stats = load_stats()
    end = now.astimezone(TZ).date()
    days = [(end - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(0, 7)]
    prev_days = [(end - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7, 14)]

    def agg(keys):
        total = 0
        critical = 0
        score_sum = 0
        by_cat = {}
        by_reg = {}
        for k in keys:
            d = stats.get(k)
            if not d:
                continue
            total += d.get("total", 0)
            critical += d.get("critical", 0)
            score_sum += d.get("avg_score_sum", 0)
            for ck, cv in d.get("by_category", {}).items():
                by_cat[ck] = by_cat.get(ck, 0) + cv
            for rk, rv in d.get("by_region", {}).items():
                by_reg[rk] = by_reg.get(rk, 0) + rv
        avg = round(score_sum / max(1, total), 1) if total else 0.0
        return total, critical, avg, by_cat, by_reg

    t7, c7, a7, cat7, reg7 = agg(days)
    tprev, _, _, _, _ = agg(prev_days)

    trend = "стабильно"
    if tprev == 0 and t7 > 0:
        trend = "рост (с нуля)"
    elif t7 > tprev:
        trend = f"рост (+{t7 - tprev})"
    elif t7 < tprev:
        trend = f"снижение (-{tprev - t7})"

    def top3(m):
        items = sorted(m.items(), key=lambda x: x[1], reverse=True)[:3]
        return ", ".join([f"{k}({v})" for k, v in items]) if items else "нет"

    return (
        "📈 OSINT ABAY — Динамика за 7 дней\n\n"
        f"Период (Алматы): {days[-1]} … {days[0]}\n\n"
        f"Сигналов: {t7} | Критических: {c7} | Средний риск: {risk_level(int(round(a7)))} ({a7}/10)\n"
        f"Тренд vs предыдущие 7 дней: {trend}\n\n"
        f"Топ-регионы: {top3(reg7)}\n"
        f"Топ-категории: {top3(cat7)}"
    )

# =========================
# Telegram sending with backoff
# =========================
async def safe_send(bot: Bot, text: str) -> None:
    while True:
        try:
            await bot.send_message(CHANNEL_ID, text)
            return
        except TelegramRetryAfter as e:
            wait_s = int(getattr(e, "retry_after", 30)) + 1
            await asyncio.sleep(wait_s)

# =========================
# Core loop
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

            # 1) строго свежие
            if not is_fresh_strict(entry, title):
                continue

            # 2) гео-совпадение
            if not geo_relevant(title):
                continue

            eid = entry_id(title, link)
            if eid in sent:
                continue

            candidates.append((title, link))

    candidates = candidates[:MAX_SEND_PER_CYCLE]

    sent_count = 0
    for title, link in candidates:
        score10, triggers, category = calculate_score(title)
        region = infer_region(title)
        critical = is_critical_signal(score10, triggers)
        forecast = impact_forecast(triggers, category)
        level = risk_level(score10)

        header = "⚠️ КРИТИЧЕСКИЙ СИГНАЛ\n\n" if critical else ""

        msg = (
            f"{header}"
            f"🚨 OSINT ABAY — Сигнал\n\n"
            f"{title}\n\n"
            f"Регион: {region}\n"
            f"Категория: {category}\n"
            f"Риск: {level} ({score10}/10)\n"
            f"Триггеры: {', '.join(triggers) if triggers else 'нет'}\n\n"
            f"{forecast}\n\n"
            f"Источник:\n{link}"
        )

        await safe_send(bot, msg)
        record_signal_to_stats(Signal(title, link, score10, category, region, critical))

        sent.add(entry_id(title, link))
        sent_count += 1
        await asyncio.sleep(SEND_DELAY_SECONDS)

    if sent_count:
        save_sent(sent)

    return sent_count

async def warmup_seen(sent: set[str]) -> None:
    """
    При первом запуске: помечаем часть записей как виденные,
    чтобы не зафлудить канал историей.
    """
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
            await asyncio.sleep(15)
        await asyncio.sleep(CHECK_EVERY_SECONDS)

# =========================
# Daily digest scheduler
# =========================
def next_run_at(target_time: dtime, tz) -> datetime:
    now = datetime.now(tz)
    today_target = datetime.combine(now.date(), target_time, tzinfo=tz)
    if now < today_target:
        return today_target
    return today_target + timedelta(days=1)

async def daily_digest_loop(bot: Bot):
    while True:
        run_at = next_run_at(DAILY_DIGEST_TIME_ALMATY, TZ)
        sleep_s = (run_at - datetime.now(TZ)).total_seconds()
        if sleep_s > 0:
            await asyncio.sleep(sleep_s)

        now = datetime.now(TZ)
        await safe_send(bot, compute_daily_digest(now))
        await safe_send(bot, compute_weekly_dynamics(now))
        await asyncio.sleep(5)

# =========================
# Web server for Render
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

    await start_web_server()
    await asyncio.gather(
        news_loop(bot),
        daily_digest_loop(bot),
    )

if __name__ == "__main__":
    asyncio.run(main())
#!/usr/bin/env python3
"""
Telegram-бот: погода (wttr.in) + дайджест новостей (на основе news_digest.py).
Перед запуском задайте токен бота:
    set TELEGRAM_BOT_TOKEN=ваш_токен
    python telegram_bot.py
"""

import os, sys, asyncio

try:
    from telegram import Update
    from telegram.ext import Application, CommandHandler, ContextTypes
except ImportError:
    sys.exit("Установите python-telegram-bot:  pip install python-telegram-bot")

import requests

from news_digest import (
    fetch_all_news,
    rank_articles,
    TOPIC_TERMS,
)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

WEATHER_URL = "https://wttr.in/{}?format=3&lang=ru&M"
HEADERS = {
    "User-Agent": "curl/8.0"
}

MAX_NEWS_TG = 10


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Привет! Я бот-дайджест.\n\n"
        "/news <тема> — сводка новостей (топ-10)\n"
        "/weather <город> — погода\n"
        "/topics — список доступных тем\n"
        "/help — помощь"
    )
    await update.message.reply_text(text)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Команды:\n"
        "/news политика — новости по теме\n"
        "/weather Москва — погода в городе\n"
        "/topics — какие темы поддерживаются\n\n"
        "Бот собирает новости из 15 российских RSS-лент "
        "(ТАСС, РБК, Коммерсантъ и др.) и ранжирует по важности."
    )
    await update.message.reply_text(text)


async def topics_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topics = sorted(TOPIC_TERMS.keys())
    text = "Доступные темы:\n" + "\n".join(f"  • {t}" for t in topics)
    text += "\n\nТакже можно ввести любую свою тему — бот подберёт ключевые слова."
    await update.message.reply_text(text)


async def weather_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    city = " ".join(context.args).strip() if context.args else "Москва"
    text = ""
    for attempt in range(3):
        try:
            resp = requests.get(WEATHER_URL.format(city), headers=HEADERS, timeout=15)
            resp.raise_for_status()
            text = resp.text.strip()
            if not text or "error" in text.lower():
                text = f"Город «{city}» не найден."
            break
        except Exception:
            if attempt < 2:
                import time; time.sleep(2)
            else:
                text = f"Не удалось получить погоду для «{city}»."
    await update.message.reply_text(text)


async def news_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = " ".join(context.args).strip() if context.args else ""
    if not topic:
        await update.message.reply_text("Укажите тему. Пример: /news спорт")
        return

    msg = await update.message.reply_text(f"Собираю новости по теме «{topic}»...")

    articles = fetch_all_news(topic)
    if not articles:
        await msg.edit_text(f"Новостей по теме «{topic}» не найдено.")
        return

    articles = rank_articles(articles, topic)
    top = articles[:MAX_NEWS_TG]

    lines = [f"Сводка по теме «{topic}» (топ-{len(top)}):\n"]
    for i, a in enumerate(top, 1):
        score_str = f"{a.score:.1f}"
        bar = _score_emoji(a.score)
        date_str = a.published.strftime("%d.%m %H:%M") if a.published else "?"
        src = a.source or "?"
        lines.append(
            f"{bar} {i}. {a.title}\n"
            f"   {src} | {date_str} | важность: {score_str}\n"
            f"   {a.link}"
        )

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n\n... (обрезано, слишком длинное сообщение)"

    await msg.edit_text(text, disable_web_page_preview=True)


def _score_emoji(score: float) -> str:
    if score >= 7.0:
        return "🟢"
    elif score >= 5.0:
        return "🟠"
    else:
        return "⚪"


def main():
    if not BOT_TOKEN:
        print("Ошибка: не задан TELEGRAM_BOT_TOKEN.")
        print("Задайте переменную окружения:")
        print("  Windows: set TELEGRAM_BOT_TOKEN=ваш_токен")
        print("  Linux/Mac: export TELEGRAM_BOT_TOKEN=ваш_токен")
        sys.exit(1)

    app = (Application.builder()
           .token(BOT_TOKEN)
           .connect_timeout(30)
           .read_timeout(60)
           .write_timeout(60)
           .build())

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("topics", topics_cmd))
    app.add_handler(CommandHandler("weather", weather_cmd))
    app.add_handler(CommandHandler("news", news_cmd))

    print("Бот запущен. Нажмите Ctrl+C для остановки.")
    app.run_polling()


if __name__ == "__main__":
    main()

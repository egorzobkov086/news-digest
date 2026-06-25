#!/usr/bin/env python3
"""
Telegram-бот (webhook-режим для PythonAnywhere).
Запуск: python bot_webhook.py
Или импортируется как WSGI-приложение: application
"""

import os, sys, json, time

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}" if BOT_TOKEN else "/webhook"
HOSTNAME = os.environ.get("PYTHONANYWHERE_SITE", "localhost")

try:
    from flask import Flask, request, jsonify
except ImportError:
    sys.exit("Установите Flask:  pip install flask")

try:
    from telegram import Update
    from telegram.ext import (
        Application, CommandHandler, ContextTypes,
    )
except ImportError:
    sys.exit("Установите python-telegram-bot:  pip install python-telegram-bot")

import requests as http_requests

from news_digest import (
    fetch_all_news,
    rank_articles,
    TOPIC_TERMS,
)

WEATHER_URL = "https://wttr.in/{}?format=3&lang=ru&M"
WEATHER_HEADERS = {"User-Agent": "curl/8.0"}
MAX_NEWS_TG = 10

# ---------- Flask ----------
app = Flask(__name__)

# ---------- Telegram Application ----------
_telegram_app: Application | None = None


def get_telegram_app() -> Application:
    global _telegram_app
    if _telegram_app is None:
        _telegram_app = (
            Application.builder()
            .token(BOT_TOKEN)
            .connect_timeout(30)
            .read_timeout(60)
            .write_timeout(60)
            .build()
        )
        _telegram_app.add_handler(CommandHandler("start", start_cmd))
        _telegram_app.add_handler(CommandHandler("help", help_cmd))
        _telegram_app.add_handler(CommandHandler("topics", topics_cmd))
        _telegram_app.add_handler(CommandHandler("weather", weather_cmd))
        _telegram_app.add_handler(CommandHandler("news", news_cmd))
    return _telegram_app


# ---------- Команды (те же, что в telegram_bot.py) ----------

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
        "/weather Москва — погода\n"
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
            resp = http_requests.get(
                WEATHER_URL.format(city), headers=WEATHER_HEADERS, timeout=15
            )
            resp.raise_for_status()
            text = resp.text.strip()
            if not text or "error" in text.lower():
                text = f"Город «{city}» не найден."
            break
        except Exception:
            if attempt < 2:
                time.sleep(2)
            else:
                text = f"Не удалось получить погоду для «{city}»."
    await update.message.reply_text(text)


async def news_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = " ".join(context.args).strip() if context.args else ""
    if not topic:
        await update.message.reply_text("Укажите тему. Пример: /news спорт")
        return

    msg = await update.message.reply_text(f"Собираю новости по теме «{topic}»...")

    try:
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
    except Exception as e:
        await msg.edit_text(f"Ошибка при сборе новостей: {e}")


def _score_emoji(score: float) -> str:
    if score >= 7.0:
        return "🟢"
    elif score >= 5.0:
        return "🟠"
    else:
        return "⚪"


# ---------- Webhook ----------

@app.route("/")
def index():
    return "Бот работает. Используйте @EgorDigestNewsBot в Telegram."


@app.route(WEBHOOK_PATH, methods=["POST"])
async def telegram_webhook():
    tg_app = get_telegram_app()
    data = request.get_json(force=True)
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return "OK"


# ---------- Настройка webhook-URL на Telegram ----------

def set_webhook():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook"
    webhook_url = f"https://{HOSTNAME}{WEBHOOK_PATH}"
    for _ in range(5):
        try:
            resp = http_requests.get(url, params={"url": webhook_url}, timeout=15)
            data = resp.json()
            if data.get("ok"):
                print(f"Webhook установлен: {webhook_url}")
                return
            else:
                print(f"Ошибка webhook: {data}")
        except Exception as e:
            print(f"Повторная попытка webhook... {e}")
            time.sleep(3)
    print("Не удалось установить webhook.")


# ---------- WSGI-совместимость ----------

# PythonAnywhere ищет переменную `application`
application = app

# ---------- Локальный запуск ----------

if __name__ == "__main__":
    if not BOT_TOKEN:
        print("Ошибка: не задан TELEGRAM_BOT_TOKEN")
        sys.exit(1)
    set_webhook()
    print(f"Запуск Flask на https://{HOSTNAME}/")
    app.run()

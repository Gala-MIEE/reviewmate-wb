"""Уведомления менеджеру в Telegram (необязательно): вопросы, критичные отзывы, проблемы с токеном."""
import requests

from . import config


def send(text: str):
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                      json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text[:4000]}, timeout=15)
    except requests.RequestException:
        pass

"""Настройки из переменных окружения (.env). Настройки, которые меняет менеджер, хранятся в БД (см. db.py)."""
import os
from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


# --- Wildberries ---
WB_API_TOKEN = os.getenv("WB_API_TOKEN", "").strip()
WB_FEEDBACKS_URL = os.getenv("WB_FEEDBACKS_URL", "https://feedbacks-api.wildberries.ru").rstrip("/")
WB_CONTENT_URL = os.getenv("WB_CONTENT_URL", "https://content-api.wildberries.ru").rstrip("/")
# Если в кабинете несколько брендов — обрабатываем только этот (пусто = все)
BRAND_FILTER = os.getenv("BRAND_FILTER", "Paperform").strip()
# Демо-режим: ни один кабинет не опрашивается и ответ покупателю не уходит
DEMO_MODE = _bool("DEMO_MODE", "1")

# --- Нейросеть (любой OpenAI-совместимый API: OpenAI, YandexGPT, прокси и т. п.) ---
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").rstrip("/")   # например https://api.openai.com/v1
LLM_API_KEY = (os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "").strip()
LLM_ENABLED = bool(LLM_BASE_URL and LLM_API_KEY and LLM_MODEL)

# --- Другие площадки (пусто = коннектор выключен, отзывы не запрашиваются) ---
OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID", "").strip()
OZON_API_KEY = os.getenv("OZON_API_KEY", "").strip()
OZON_API_URL = os.getenv("OZON_API_URL", "https://api-seller.ozon.ru").rstrip("/")
OZON_HAS_KEYS = bool(OZON_CLIENT_ID and OZON_API_KEY)
OZON_ENABLED = OZON_HAS_KEYS and not DEMO_MODE

YM_API_KEY = os.getenv("YM_API_KEY", "").strip()
YM_BUSINESS_ID = os.getenv("YM_BUSINESS_ID", "").strip()
YM_API_URL = os.getenv("YM_API_URL", "https://api.partner.market.yandex.ru").rstrip("/")
YM_HAS_KEYS = bool(YM_API_KEY and YM_BUSINESS_ID)
YM_ENABLED = YM_HAS_KEYS and not DEMO_MODE

PLATFORMS = (
    {"code": "wb", "title": "Wildberries"},
    {"code": "ozon", "title": "Ozon"},
    {"code": "ym", "title": "Яндекс Маркет"},
)

# --- Сервис ---
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "reviewmate.db"))
ADMIN_USER = os.getenv("ADMIN_USER", "manager")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")          # пусто = без пароля (только для локального запуска)
SECRET_KEY = os.getenv("SECRET_KEY", "change-me")
WORKER_TICK_SEC = int(os.getenv("WORKER_TICK_SEC", "60"))  # как часто воркер проверяет очередь автоотправки

# --- Уведомления в Telegram (необязательно) ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

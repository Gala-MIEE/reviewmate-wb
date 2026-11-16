"""Общая тестовая база до импорта приложения, чтобы тесты не трогали рабочую SQLite."""
import os
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["DEMO_MODE"] = "1"
os.environ["LLM_BASE_URL"] = ""
os.environ["ADMIN_PASSWORD"] = ""
os.environ["OZON_CLIENT_ID"] = ""
os.environ["OZON_API_KEY"] = ""
os.environ["YM_API_KEY"] = ""
os.environ["YM_BUSINESS_ID"] = ""

"""Фоновый процесс: опрос WB каждые N минут (по умолчанию 30) и отправка запланированных автоответов.
Запуск: python worker.py   (в Docker — отдельный сервис worker)."""
import time
from datetime import datetime, timedelta, timezone

from app import config, db, pipeline, sample_set


def main():
    db.init()
    loaded = sample_set.load_sample_set()
    mode = "ДЕМО (WB не вызывается)" if config.DEMO_MODE else "РАБОЧИЙ"
    print(f"[worker] старт, режим: {mode}, нейросеть: {'да' if config.LLM_ENABLED else 'нет, шаблоны'}", flush=True)
    if loaded.get("ozon") or loaded.get("ym"):
        print(f"[worker] тестовый набор: Ozon {loaded['ozon']}, Яндекс Маркет {loaded['ym']}", flush=True)
    pipeline.sync_products()
    last_products = datetime.now(timezone.utc)
    while True:
        s = db.get_settings()
        last = s.get("last_poll_at")
        interval = timedelta(minutes=int(s.get("poll_interval_min", 30)))
        due = (not last) or datetime.now(timezone.utc) - datetime.fromisoformat(last) >= interval or s.get("poll_requested")
        if due:
            db.set_setting("poll_requested", False)
            try:
                print("[worker] опрос WB:", pipeline.poll(), flush=True)
            except Exception as e:  # noqa: BLE001 — сбой WB не отменяет остальные площадки
                db.log("poll_crash", details=repr(e), level="error")
                print("[worker] ошибка опроса WB:", repr(e), flush=True)
            try:
                print("[worker] опрос других площадок:", pipeline.poll_others(), flush=True)
            except Exception as e:  # noqa: BLE001
                db.log("poll_others_crash", details=repr(e), level="error")
                print("[worker] ошибка опроса других площадок:", repr(e), flush=True)
        try:
            n = pipeline.send_due()
            if n:
                print(f"[worker] автоответов отправлено: {n}", flush=True)
        except Exception as e:  # noqa: BLE001
            db.log("send_crash", details=repr(e), level="error")
        if datetime.now(timezone.utc) - last_products > timedelta(hours=24):
            pipeline.sync_products()
            last_products = datetime.now(timezone.utc)
        time.sleep(config.WORKER_TICK_SEC)


if __name__ == "__main__":
    main()

"""Тестовые отзывы Ozon и Яндекс Маркета из xlsx.

Файл ищется в data/, в каталоге сервиса и на уровень выше.
Это не выгрузка кабинета: черновики можно проверять, на площадку ничего не уходит.
Повторный запуск не затирает уже загруженные id.
"""
import os
from datetime import datetime, timedelta, timezone

from . import analyzer, db

FILENAME = "paperform_test_sets_ozon_ym.xlsx"
REASON = "Тестовый набор: черновик для проверки ответа, на площадку не отправляется"
MOSCOW = timezone(timedelta(hours=3))


def sample_path() -> str | None:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.abspath(os.path.join(here, ".."))
    parent = os.path.abspath(os.path.join(root, ".."))
    for path in (
        os.getenv("SAMPLE_REVIEWS_PATH", ""),
        os.path.join(root, "data", FILENAME),
        os.path.join(root, FILENAME),
        os.path.join(parent, FILENAME),
    ):
        if path and os.path.isfile(path):
            return path
    return None


def load_sample_set(path: str | None = None) -> dict:
    """Кладёт новые строки файла в очередь менеджера с черновиком по шаблону.

    Автозагрузка файла рядом с проектом только в демо-режиме. В рабочем режиме
    тестовые отзывы не попадают в очередь к настоящим.
    """
    from . import config
    db.init()
    stats = {"ozon": 0, "ym": 0, "skipped": 0, "path": None}
    if path is None and not config.DEMO_MODE:
        return stats
    path = path or sample_path()
    stats["path"] = path
    if not path:
        return stats
    signature = db.get_settings().get("signature", "")
    fresh = []
    read_items = list(_read(path))
    with db.session() as con:
        for it in read_items:
            if db.item_exists(con, it["id"]):
                stats["skipped"] += 1
                continue
            full = " ".join(x for x in (it["text"], it["pros"], it["cons"]) if x)
            markup = analyzer.classify_rules(it["kind"], full, it.get("rating"))
            draft = "" if markup["scenario"] == "Токсичный/спам" else analyzer.draft_from_template(markup, it, signature)
            it.update(
                scenario=markup["scenario"], theme=markup["theme"], tone=markup["tone"], priority=markup["priority"],
                has_question=int(bool(markup.get("has_question"))), has_wish=int(bool(markup.get("has_wish"))),
                summary=markup.get("summary") or "", draft=draft, analyzed_by="rules",
                status="manager", route_reason=REASON,
            )
            db.insert_item(con, it)
            fresh.append(it["marketplace"])
    for code in fresh:
        stats[code] = stats.get(code, 0) + 1
    if fresh:
        db.set_setting("sample_loaded", True)
        db.log("sample_loaded", details=str({k: stats[k] for k in ("ozon", "ym", "skipped")}))
    return stats


def refresh_sample_drafts() -> dict:
    """Переписывает черновики тестового набора нейросетью. Уже отправленные не трогает."""
    from . import config
    if not config.LLM_ENABLED:
        return {"done": 0, "failed": 0, "waiting": 0}
    db.init()
    signature = db.get_settings().get("signature", "")
    with db.session() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM items WHERE is_sample=1 AND status IN ('new','manager') AND (analyzed_by IS NULL OR analyzed_by!='llm')"
        )]
    done = failed = 0
    print(f"черновики тестового набора: {len(rows)}", flush=True)
    for it in rows:
        try:
            result = analyzer.analyze(it, db.product_info(it.get("nm_id")), signature)
        except Exception as e:  # noqa: BLE001 — шаблонный черновик остаётся
            failed += 1
            print(f"пропуск {it['id']}: {e}"[:240], flush=True)
            continue
        db.update_item(
            it["id"], scenario=result["scenario"], theme=result["theme"], tone=result["tone"],
            priority=result["priority"], has_question=int(bool(result.get("has_question"))),
            has_wish=int(bool(result.get("has_wish"))), summary=result.get("summary", ""),
            draft=result.get("draft", ""), analyzed_by=result.get("analyzed_by"),
        )
        done += 1
        if done % 10 == 0 or done == len(rows):
            print(f"черновики {done} из {len(rows)}", flush=True)
    return {"done": done, "failed": failed, "waiting": len(rows)}


def _read(path: str) -> list[dict]:
    from openpyxl import load_workbook
    book = load_workbook(path, read_only=True, data_only=True)
    try:
        items = []
        for sheet in book.worksheets:
            if sheet.title == "Описание":
                continue
            rows = sheet.iter_rows(values_only=True)
            header = next(rows, None)
            if not header:
                continue
            idx = {str(name).strip(): i for i, name in enumerate(header) if name}
            for row in rows:
                item = _item(idx, row, sheet.title)
                if item:
                    items.append(item)
        return items
    finally:
        book.close()


def _item(idx: dict, row: tuple, sheet: str) -> dict | None:
    raw_id = _cell(row, idx, "id")
    if not raw_id:
        return None
    market = _market(_cell(row, idx, "площадка") or sheet)
    if not market:
        return None
    kind = "question" if "вопрос" in (_cell(row, idx, "тип") or "").lower() else "feedback"
    rating = _rating(_pick(row, idx, "оценка"))
    article = _cell(row, idx, "артикул")
    return {
        "id": f"{market}:{raw_id}",
        "kind": kind,
        "marketplace": market,
        "created_at": _created(_pick(row, idx, "дата")),
        "fetched_at": db.now_iso(),
        "nm_id": int(article) if article.isdigit() else None,
        "product_name": _cell(row, idx, "товар") or "Товар",
        "supplier_article": article,
        "brand": "Paperform",
        "user_name": _cell(row, idx, "имя"),
        "rating": rating,
        "text": _cell(row, idx, "текст"),
        "pros": _cell(row, idx, "достоинства"),
        "cons": _cell(row, idx, "недостатки"),
        "has_media": int(str(_pick(row, idx, "фото") or "0").strip() not in ("", "0", "0.0")),
        "is_sample": 1,
        "status": "new",
    }


def _market(value: str) -> str:
    text = value.lower()
    if "ozon" in text or "озон" in text:
        return "ozon"
    if "яндекс" in text or "маркет" in text or text == "ym":
        return "ym"
    return ""


def _cell(row: tuple, idx: dict, name: str) -> str:
    value = _pick(row, idx, name)
    if value is None:
        return ""
    return str(value).strip()


def _pick(row: tuple, idx: dict, name: str):
    i = idx.get(name)
    if i is None or i >= len(row):
        return None
    return row[i]


def _rating(value):
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _created(value) -> str:
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        dt = None
        for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            return db.now_iso()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MOSCOW)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()

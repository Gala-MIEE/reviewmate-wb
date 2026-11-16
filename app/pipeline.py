"""Основной цикл: забрать новое с WB → проанализировать → разложить по очередям → отправить запланированные автоответы."""
import json
import os
from datetime import datetime, timedelta, timezone

from . import analyzer, config, db, guard, notify
from .wb_client import WBAuthError, WBClient, WBError, token_expiry


def client():
    if config.DEMO_MODE:
        from .mock_wb import MockWBClient
        return MockWBClient()
    return WBClient()


def _iso(s: str | None) -> str:
    if not s:
        return db.now_iso()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).replace(microsecond=0).isoformat()
    except ValueError:
        return db.now_iso()


def _normalize(kind: str, raw: dict) -> dict:
    pd = raw.get("productDetails") or {}
    return {
        "id": raw["id"], "kind": kind, "marketplace": "wb", "created_at": _iso(raw.get("createdDate")), "fetched_at": db.now_iso(),
        "nm_id": pd.get("nmId"), "product_name": pd.get("productName"), "supplier_article": pd.get("supplierArticle"),
        "brand": pd.get("brandName"), "user_name": raw.get("userName") or "",
        "rating": raw.get("productValuation") if kind == "feedback" else None,
        "text": raw.get("text") or "", "pros": raw.get("pros") or "", "cons": raw.get("cons") or "",
        "has_media": int(bool(raw.get("photoLinks")) or bool(raw.get("video"))),
        "status": "new",
    }


ANSWER_LIMIT = {"wb": 5000, "ozon": 3000, "ym": 4096}
VERIFY_PER_POLL = 30


def _row_text(it) -> bool:
    return any(str(it[key] or "").strip() for key in ("text", "pros", "cons"))


def _day_start() -> str:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def _auto_sent_today() -> int:
    with db.session() as con:
        return con.execute("SELECT COUNT(*) FROM items WHERE status='sent_auto' AND answered_at>=?", (_day_start(),)).fetchone()[0]


def _auto_committed_today() -> int:
    """Уже отправленные сегодня и ещё запланированные. Иначе каждый опрос набирает лимит заново."""
    with db.session() as con:
        scheduled = con.execute("SELECT COUNT(*) FROM items WHERE status='scheduled'").fetchone()[0]
    return _auto_sent_today() + scheduled


def _remember_poll(code: str, res: dict) -> None:
    current = db.get_settings().get("last_poll_result") or {}
    if not isinstance(current, dict) or "new_feedbacks" in current:
        current = {}
    current[code] = res
    db.set_setting("last_poll_result", current)
    db.set_setting("last_poll_at", db.now_iso())


def _token_alerts() -> dict:
    raw = db.get_settings().get("token_alerts") or {}
    return dict(raw) if isinstance(raw, dict) else {}


def _set_token_alert(market: str, message: str | None) -> None:
    alerts = _token_alerts()
    legacy = db.get_settings().get("token_alert")
    if isinstance(legacy, str) and legacy.strip():
        low = legacy.lower()
        if "ozon" in low:
            bucket = "ozon"
        elif "яндекс" in low or "маркет" in low:
            bucket = "ym"
        else:
            bucket = "wb"
        alerts.setdefault(bucket, legacy)
    if message:
        alerts[market] = message
    else:
        alerts.pop(market, None)
    db.set_setting("token_alerts", alerts)
    db.set_setting("token_alert", None)


def _remote_answer(raw) -> str:
    if not isinstance(raw, dict):
        return ""
    answer = raw.get("answer")
    if isinstance(answer, dict):
        return (answer.get("text") or "").strip()
    if isinstance(answer, str):
        return answer.strip()
    return ""


def _wb_open_ids(wb, rows: list, complete_attr: str, count_method: str) -> set | None:
    """None — список неполный или подозрительно пустой, очередь по нему не закрываем."""
    if not getattr(wb, complete_attr, False):
        return None
    if rows:
        return {row["id"] for row in rows}
    try:
        count = int(getattr(wb, count_method)() or 0)
    except Exception as e:  # noqa: BLE001 — без сверки числа очередь не трогаем
        db.log("wb_count_skip", details=str(e)[:200], level="warn")
        return None
    if count > 0:
        db.log("wb_empty_list", details=f"{count_method}={count}, список пуст — очередь не закрываю", level="warn")
        return None
    return set()


def _close_wb(con, wb, feedback_ids: set | None, question_ids: set | None, res: dict) -> None:
    pending = con.execute(
        "SELECT id, kind FROM items WHERE marketplace='wb' AND status IN ('new','manager','scheduled')"
    ).fetchall()
    checked = 0
    for row in pending:
        if row["kind"] == "question":
            ids, getter = question_ids, "get_question"
        else:
            ids, getter = feedback_ids, "get_feedback"
        if ids is None or row["id"] in ids:
            continue
        if checked >= VERIFY_PER_POLL:
            break
        checked += 1
        try:
            raw = getattr(wb, getter)(row["id"])
        except Exception as e:  # noqa: BLE001 — сомнительную запись оставляем в очереди
            db.log("wb_reconcile_skip", row["id"], str(e)[:200], level="warn")
            continue
        answer = _remote_answer(raw)
        if answer:
            con.execute(
                "UPDATE items SET status='answered_in_wb', answered_at=?, answer_text=?, route_reason=? WHERE id=?",
                (db.now_iso(), answer, "Ответ уже есть в кабинете WB", row["id"]),
            )
            res["closed_in_wb"] += 1
        elif isinstance(raw, dict) and raw.get("id"):
            con.execute(
                "UPDATE items SET status='skipped', answered_at=?, route_reason=? WHERE id=?",
                (db.now_iso(), "Пропал из неотвеченных WB без текста ответа: архив или оценка без слов. В кабинете ответа нет.", row["id"]),
            )


def _close_upstream(con, market: str, feedback_ids: set | None, question_ids: set | None) -> int:
    pending = con.execute(
        "SELECT id, kind FROM items WHERE marketplace=? AND ifnull(is_sample,0)=0 "
        "AND status IN ('new','manager','scheduled')",
        (market,),
    ).fetchall()
    closed = 0
    now = db.now_iso()
    reason = "На площадке запись больше не числится неотвеченной"
    for row in pending:
        ids = question_ids if row["kind"] == "question" else feedback_ids
        if ids is None or row["id"] in ids:
            continue
        con.execute(
            "UPDATE items SET status='answered_upstream', answered_at=?, route_reason=? WHERE id=?",
            (now, reason, row["id"]),
        )
        closed += 1
    return closed


def sync_products(wb=None):
    """Описания товаров из карточек WB — чтобы нейросеть не придумывала характеристики. Нужен доступ «Контент» (чтение)."""
    wb = wb or client()
    try:
        cards = wb.product_cards()
    except (WBAuthError, WBError) as e:
        db.log("products_skip", details=f"Карточки не загружены (нет категории «Контент»?): {e}", level="warn")
        return 0
    with db.session() as con:
        for c in cards:
            if config.BRAND_FILTER and (c.get("brand") or "").lower() != config.BRAND_FILTER.lower():
                continue
            con.execute("INSERT INTO products(nm_id, title, vendor_code, brand, description, updated_at) VALUES (?,?,?,?,?,?) "
                        "ON CONFLICT(nm_id) DO UPDATE SET title=excluded.title, vendor_code=excluded.vendor_code, "
                        "brand=excluded.brand, description=excluded.description, updated_at=excluded.updated_at",
                        (c.get("nmID"), c.get("title"), c.get("vendorCode"), c.get("brand"), c.get("description") or "", db.now_iso()))
    return len(cards)


def snapshot_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(config.DB_PATH)), "wb_feedbacks_sample.json")


def load_snapshot(path: str | None = None) -> dict:
    """Заменяет содержимое базы сохранённой выгрузкой отзывов WB. Уже отвеченные не попадают в очередь менеджера."""
    path = path or snapshot_path()
    with open(path, encoding="utf-8") as f:
        raw_items = json.load(f)
    signature = db.get_settings().get("signature", "")
    stats = {"loaded": 0, "open": 0, "answered": 0, "skipped_brand": 0}
    with db.session() as con:
        con.execute("DELETE FROM items WHERE marketplace='wb'")
        for raw in raw_items:
            details = raw.get("productDetails") or {}
            brand = details.get("brandName") or ""
            if config.BRAND_FILTER and brand.lower() != config.BRAND_FILTER.lower():
                stats["skipped_brand"] += 1
                continue
            it = _normalize("feedback", raw)
            full_text = " ".join(x for x in (it["text"], it["pros"], it["cons"]) if x)
            markup = analyzer.classify_rules("feedback", full_text, it.get("rating"))
            answer = raw.get("answer") if isinstance(raw.get("answer"), dict) else {}
            open_review = raw.get("source") == "unanswered"
            if open_review:
                markup["draft"] = analyzer.draft_from_template(markup, it, signature)
                it["status"] = "manager"
                it["route_reason"] = "Неотвеченный отзыв из выгрузки WB"
                stats["open"] += 1
            else:
                markup["draft"] = ""
                it["status"] = "answered_in_wb"
                it["route_reason"] = "Уже есть ответ в кабинете WB"
                it["answer_text"] = (answer or {}).get("text") or ""
                it["answered_by"] = "wb"
                stats["answered"] += 1
            it.update(scenario=markup["scenario"], theme=markup["theme"], tone=markup["tone"], priority=markup["priority"],
                      has_question=int(bool(markup.get("has_question"))), has_wish=int(bool(markup.get("has_wish"))),
                      summary=markup.get("summary") or "", draft=markup.get("draft") or "", analyzed_by="rules")
            db.insert_item(con, it)
            if it["nm_id"]:
                con.execute("INSERT INTO products(nm_id, title, vendor_code, brand, description, updated_at) VALUES (?,?,?,?,?,?) "
                            "ON CONFLICT(nm_id) DO UPDATE SET title=excluded.title, vendor_code=excluded.vendor_code, "
                            "brand=excluded.brand, updated_at=excluded.updated_at",
                            (it["nm_id"], it["product_name"], it["supplier_article"], it["brand"], "", db.now_iso()))
            stats["loaded"] += 1
    db.set_setting("snapshot_loaded", True)
    _remember_poll("wb", {"new_feedbacks": stats["loaded"], "new_questions": 0, "to_manager": stats["open"],
                          "scheduled": 0, "closed_in_wb": stats["answered"], "error": None})
    db.log("snapshot_loaded", details=str(stats))
    return stats


def reanalyze_with_llm(batch_size: int = 8) -> dict:
    """Заново размечает отзывы с текстом нейросетью: сценарий, тема, тональность, вопрос, пожелание."""
    if not config.LLM_ENABLED:
        raise RuntimeError("Нейросеть не настроена: нужны адрес, ключ и модель")
    db.init()
    with db.session() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM items WHERE analyzed_by IS NULL OR analyzed_by!='llm'")]
    todo = [r for r in rows if analyzer.combined_text(r)]
    print(f"к разметке: {len(todo)}", flush=True)
    done = failed = 0
    index = 0
    size = batch_size
    while index < len(todo):
        chunk = todo[index:index + size]
        try:
            marked = analyzer.classify_batch(chunk)
        except Exception as e:  # noqa: BLE001 — пачку меньше и повторяем, одну запись пропускаем
            if len(chunk) == 1:
                print(f"пропуск {chunk[0]['id']}: {e}"[:240], flush=True)
                failed += 1
                index += 1
                size = batch_size
                continue
            size = max(1, len(chunk) // 2)
            print(f"повтор пачки по {size}: {str(e)[:160]}", flush=True)
            continue
        with db.session() as con:
            for it in chunk:
                mark = marked[it["id"]]
                con.execute(
                    "UPDATE items SET scenario=?, theme=?, tone=?, priority=?, has_question=?, has_wish=?, summary=?, analyzed_by='llm' WHERE id=?",
                    (mark["scenario"], mark["theme"], mark["tone"], mark["priority"], int(mark["has_question"]),
                     int(mark["has_wish"]), mark["summary"], it["id"]),
                )
        done += len(chunk)
        index += len(chunk)
        size = batch_size
        print(f"готово {done} из {len(todo)}", flush=True)
    signature = db.get_settings().get("signature", "")
    with db.session() as con:
        waiting = [dict(r) for r in con.execute("SELECT * FROM items WHERE status IN ('manager','new','scheduled')")]
    for it in waiting:
        if not analyzer.combined_text(it):
            continue
        result = analyzer.analyze(it, db.product_info(it.get("nm_id")), signature)
        db.update_item(it["id"], scenario=result["scenario"], theme=result["theme"], tone=result["tone"],
                       priority=result["priority"], has_question=int(bool(result.get("has_question"))),
                       has_wish=int(bool(result.get("has_wish"))), summary=result.get("summary", ""),
                       draft=result.get("draft", ""), analyzed_by=result.get("analyzed_by"))
    return {"done": done, "failed": failed, "waiting": len(waiting)}


def poll() -> dict:
    """Один опрос WB. Неожиданная ошибка не сбрасывает расписание: следующая проверка по интервалу."""
    try:
        return _poll_wb()
    except Exception as e:  # noqa: BLE001 — сбой WB не должен опрашивать кабинет каждую минуту
        res = {"new_feedbacks": 0, "new_questions": 0, "to_manager": 0, "scheduled": 0, "closed_in_wb": 0,
               "error": "Проверка Wildberries не выполнена, следующая по расписанию."}
        db.log("poll_crash", details=repr(e), level="error")
        _remember_poll("wb", res)
        return res


def _poll_wb() -> dict:
    """Один опрос WB. Вызывается воркером раз в poll_interval_min и кнопкой «Проверить сейчас»."""
    s = db.get_settings()
    if config.DEMO_MODE and s.get("snapshot_loaded"):
        db.set_setting("last_poll_at", db.now_iso())
        return {"new_feedbacks": 0, "new_questions": 0, "to_manager": 0, "scheduled": 0, "closed_in_wb": 0, "error": None}
    wb = client()
    res = {"new_feedbacks": 0, "new_questions": 0, "to_manager": 0, "scheduled": 0, "closed_in_wb": 0, "error": None}
    try:
        feedbacks = wb.unanswered_feedbacks()
        questions = wb.unanswered_questions()
    except WBAuthError as e:
        msg = f"WB отклонил токен: {e}. Создайте новый токен (категория «Вопросы и отзывы», чтение и запись)."
        _set_token_alert("wb", msg)
        db.log("wb_auth_error", details=str(e), level="error")
        notify.send("ReviewMate: " + msg)
        res["error"] = msg
        _remember_poll("wb", res)
        return res
    except WBError as e:
        res["error"] = str(e)
        db.log("wb_error", details=str(e), level="error")
        _remember_poll("wb", res)
        return res
    _set_token_alert("wb", None)

    incoming = [("feedback", f) for f in feedbacks] + [("question", q) for q in questions]
    if config.BRAND_FILTER:
        incoming = [(k, r) for k, r in incoming
                    if ((r.get("productDetails") or {}).get("brandName") or "").lower() == config.BRAND_FILTER.lower()]
    feedback_ids = _wb_open_ids(wb, feedbacks, "feedbacks_complete", "count_unanswered_feedbacks")
    question_ids = _wb_open_ids(wb, questions, "questions_complete", "count_unanswered_questions")

    new_items = []
    with db.session() as con:
        for kind, raw in incoming:
            if not db.item_exists(con, raw["id"]):
                it = _normalize(kind, raw)
                db.insert_item(con, it)
                new_items.append(it)
        _close_wb(con, wb, feedback_ids, question_ids, res)

    sent_today = _auto_committed_today()
    _finish_new(new_items, res, s, sent_today)

    _check_token_expiry()
    _remember_poll("wb", res)
    db.log("poll", details=str(res))
    return res


def _finish_new(new_items: list[dict], res: dict, s: dict, sent_today: int) -> None:
    for it in sorted(new_items, key=lambda x: x["created_at"]):
        if it["kind"] == "feedback" and not _row_text(it):
            db.update_item(it["id"], status="skipped", draft="", send_after=None,
                           route_reason="Оценка без текста — в разбор и автоответ не берём")
            continue
        a = analyzer.analyze(it, db.product_info(it.get("nm_id")), s.get("signature", ""))
        status, reason = guard.route(it, a, s, sent_today)
        send_after = None
        if status == "scheduled":
            send_after = (datetime.now(timezone.utc) + timedelta(minutes=int(s.get("auto_delay_min", 0)))).replace(microsecond=0).isoformat()
            sent_today += 1
            res["scheduled"] += 1
        else:
            res["to_manager"] += 1
        db.update_item(it["id"], scenario=a["scenario"], theme=a["theme"], tone=a["tone"], priority=a["priority"],
                       has_question=int(bool(a.get("has_question"))), has_wish=int(bool(a.get("has_wish"))),
                       summary=a.get("summary", ""), draft=a.get("draft", ""),
                       analyzed_by=a.get("analyzed_by"), status=status, route_reason=reason, send_after=send_after)
        res["new_feedbacks" if it["kind"] == "feedback" else "new_questions"] += 1
        db.log("new_" + it["kind"], it["id"], f"{a['scenario']} / {a['priority']} → {reason}")
        if it["kind"] == "question" or a["priority"] == "Критичный" or a.get("has_question"):
            what = "Вопрос" if it["kind"] == "question" else ("Критичный отзыв" if a["priority"] == "Критичный" else "Отзыв с вопросом")
            notify.send(f"ReviewMate — {what}\n{it.get('product_name')}\n«{(it.get('text') or '')[:300]}»")


def poll_others() -> dict:
    """Опрос Ozon и Яндекс Маркета. Площадка без ключей пропускается, тестовые отзывы не создаются."""
    summary = {}
    jobs = []
    if config.OZON_ENABLED:
        from .ozon_client import OzonAuthError, OzonClient, OzonError
        jobs.append(("ozon", OzonClient, (OzonAuthError, OzonError)))
    elif config.DEMO_MODE and config.OZON_HAS_KEYS:
        summary["ozon"] = {"skipped": "демо-режим: кабинет не опрашивается"}
        _remember_poll("ozon", summary["ozon"])
    else:
        summary["ozon"] = {"skipped": "нет ключа"}
        _remember_poll("ozon", summary["ozon"])
    if config.YM_ENABLED:
        from .ym_client import YandexMarketAuthError, YandexMarketClient, YandexMarketError
        jobs.append(("ym", YandexMarketClient, (YandexMarketAuthError, YandexMarketError)))
    elif config.DEMO_MODE and config.YM_HAS_KEYS:
        summary["ym"] = {"skipped": "демо-режим: кабинет не опрашивается"}
        _remember_poll("ym", summary["ym"])
    else:
        summary["ym"] = {"skipped": "нет ключа"}
        _remember_poll("ym", summary["ym"])
    s = db.get_settings()
    for code, factory, errors in jobs:
        try:
            _poll_market(code, factory, errors, s, summary)
        except Exception as e:  # noqa: BLE001 — сбой одной площадки не останавливает следующую
            res = {"new_feedbacks": 0, "new_questions": 0, "to_manager": 0, "scheduled": 0,
                   "error": "Проверка не выполнена, следующая по расписанию."}
            db.log(f"{code}_crash", details=repr(e), level="error")
            summary[code] = res
            _remember_poll(code, res)
    return summary


def _poll_market(code: str, factory, errors, s: dict, summary: dict) -> None:
    res = {"new_feedbacks": 0, "new_questions": 0, "to_manager": 0, "scheduled": 0, "error": None}
    question_auth = False
    try:
        client = factory()
        incoming = client.unanswered_feedbacks()
    except errors as e:
        res["error"] = str(e)[:300]
        db.log(f"{code}_error", details=res["error"], level="error")
        if type(e).__name__ in ("OzonAuthError", "YandexMarketAuthError"):
            _set_token_alert(code, res["error"])
        summary[code] = res
        _remember_poll(code, res)
        return
    questions = []
    questions_complete = False
    if hasattr(client, "unanswered_questions"):
        try:
            questions = client.unanswered_questions()
            questions_complete = getattr(client, "questions_complete", False)
        except errors as e:
            res["error"] = f"вопросы: {e}"[:300]
            db.log(f"{code}_questions_error", details=res["error"], level="error")
            if type(e).__name__ in ("OzonAuthError", "YandexMarketAuthError"):
                _set_token_alert(code, res["error"])
                question_auth = True
    else:
        questions_complete = True
    feedback_ids = _market_ids(client, incoming, code, "feedback")
    question_ids = _market_ids(client, questions, code, "question") if questions_complete else None
    new_items = []
    with db.session() as con:
            for raw in incoming + questions:
                name = (raw.get("product_name") or "").strip()
                if db.item_exists(con, raw["id"]):
                    if name and not name.startswith("SKU "):
                        con.execute(
                            "UPDATE items SET product_name=? WHERE id=? AND (product_name IS NULL OR product_name LIKE 'SKU %')",
                            (name, raw["id"]),
                        )
                    continue
                raw["created_at"] = _iso(raw.get("created_at"))
                raw["fetched_at"] = db.now_iso()
                db.insert_item(con, raw)
                new_items.append(raw)
            res["closed_upstream"] = _close_upstream(con, code, feedback_ids, question_ids)
    if not question_auth:
        _set_token_alert(code, None)
    _finish_new(new_items, res, s, _auto_committed_today())
    summary[code] = res
    _remember_poll(code, res)
    db.log(f"poll_{code}", details=str(res))


def _market_ids(client, rows: list, code: str, kind: str) -> set | None:
    if kind == "feedback":
        complete = getattr(client, "reviews_complete", False)
    else:
        complete = getattr(client, "questions_complete", False)
    if not complete:
        return None
    if rows:
        return {row["id"] for row in rows}
    count = _remote_unprocessed(client, kind)
    if count is None:
        return None
    if count > 0:
        db.log(f"{code}_empty_list", details=f"{kind}: счётчик {count}, список пуст — очередь не закрываю", level="warn")
        return None
    return set()


def _remote_unprocessed(client, kind: str) -> int | None:
    try:
        if kind == "feedback" and hasattr(client, "review_count"):
            data = client.review_count()
        elif kind == "question" and hasattr(client, "question_count"):
            data = client.question_count()
        elif kind == "feedback" and hasattr(client, "has_any_feedback"):
            return 0 if client.has_any_feedback() else None
        else:
            return None
    except Exception as e:  # noqa: BLE001
        db.log("count_skip", details=str(e)[:200], level="warn")
        return None
    if not isinstance(data, dict):
        return None
    for key in ("unprocessed", "countUnanswered"):
        if data.get(key) is not None:
            try:
                return int(data[key])
            except (TypeError, ValueError):
                return None
    return None


def _check_token_expiry():
    if config.DEMO_MODE or not config.WB_API_TOKEN:
        return
    exp = token_expiry(config.WB_API_TOKEN)
    if exp and exp - datetime.now(timezone.utc) < timedelta(days=14):
        _set_token_alert("wb", f"Токен WB истекает {exp:%d.%m.%Y}. Создайте новый и обновите WB_API_TOKEN.")


def _send_problem(market: str, exc: Exception) -> str | None:
    """Понятный текст для менеджера. None — ошибка не про площадку, её нужно поднять дальше."""
    title = {"wb": "Wildberries", "ozon": "Ozon", "ym": "Яндекс Маркет"}.get(market, "Площадка")
    name = type(exc).__name__
    raw = str(exc).strip()
    if name in ("OzonAuthError", "YandexMarketAuthError", "WBAuthError"):
        _set_token_alert(market if market in ANSWER_LIMIT else "wb", f"Площадка отклонила токен: {exc}")
        return f"Токен площадки отклонён: {exc}"
    network = name in (
        "OzonError", "YandexMarketError", "WBError", "RequestException", "ConnectionError",
        "Timeout", "ReadTimeout", "ConnectTimeout", "SSLError", "HTTPError",
    )
    if not network:
        return None
    if raw in {"500", "502", "503", "504"} or "не ответил" in raw or name in (
        "RequestException", "ConnectionError", "Timeout", "ReadTimeout", "ConnectTimeout", "SSLError",
    ):
        return f"{title} временно не отвечает, ответ не опубликован. Попробуйте ещё раз чуть позже."
    if raw == "429" or raw.startswith("429"):
        return f"{title} ограничил частоту запросов. Подождите немного и отправьте ответ ещё раз."
    return f"{title} не принял ответ: {raw}"


def send_answer(item_id: str, text: str, by: str) -> list[str]:
    """Публикация ответа на площадку записи. Пустой список — успех."""
    s = db.get_settings()
    it = db.get_item(item_id)
    if not it:
        return ["Запись не найдена"]
    if it["status"] == "skipped":
        return ["Запись снята с очереди"]
    if it["status"] in ("sent", "sent_auto", "answered_in_wb", "answered_upstream", "rejected"):
        return ["На это уже ответили"]
    if it["kind"] == "feedback" and not _row_text(it):
        return ["Оценка без текста — ответ не отправляется"]
    problems = guard.check_answer(text, s.get("stop_words", []))
    if problems:
        return problems
    market = it["marketplace"] if "marketplace" in it.keys() else "wb"
    sample = bool(it["is_sample"]) if "is_sample" in it.keys() else False
    limit = ANSWER_LIMIT.get(market, 5000)
    if len(text.strip()) > limit:
        return [f"Ответ длиннее {limit} знаков"]
    if market == "ym" and it["kind"] == "question":
        return ["На вопрос Яндекс Маркета сервис публикует ответ только вручную в кабинете"]
    publish = not sample and not (config.DEMO_MODE and market in ("ozon", "ym"))
    if publish:
        try:
            if market == "ozon":
                from .ozon_client import OzonClient
                ozon = OzonClient()
                if it["kind"] == "question":
                    ozon.answer_question(item_id, text.strip(), it["nm_id"])
                else:
                    ozon.answer_feedback(item_id, text.strip())
            elif market == "ym":
                from .ym_client import YandexMarketClient
                YandexMarketClient().answer_feedback(item_id, text.strip())
            else:
                wb = client()
                if it["kind"] == "question":
                    wb.answer_question(item_id, text.strip())
                else:
                    wb.answer_feedback(item_id, text.strip())
        except (WBAuthError, Exception) as e:
            problem = _send_problem(market, e)
            if problem is None:
                raise
            db.update_item(item_id, error=str(e)[:300])
            if not problem.startswith("Токен"):
                db.log("send_error", item_id, str(e), level="error")
            return [problem]
    db.update_item(item_id, status="sent_auto" if by == "auto" else "sent", answer_text=text.strip(),
                   answered_at=db.now_iso(), answered_by=by, error=None)
    db.log("sent", item_id, f"by={by}")
    return []


def send_due() -> int:
    """Отправить автоответы, у которых истекла задержка. Если автоответ выключили — запланированные уходят менеджеру."""
    s = db.get_settings()
    with db.session() as con:
        due = con.execute("SELECT id, draft FROM items WHERE status='scheduled' AND send_after<=?", (db.now_iso(),)).fetchall()
    n = 0
    limit = int(s.get("auto_daily_limit", 100))
    for r in due:
        if not s.get("auto_reply_enabled"):
            db.update_item(r["id"], status="manager", route_reason="Автоответ выключили до отправки — проверьте вручную")
            continue
        current = db.get_item(r["id"])
        if current and current["kind"] == "feedback" and not _row_text(current):
            db.update_item(r["id"], status="skipped", send_after=None, route_reason="Оценка без текста — автоответ отменён")
            continue
        if _auto_sent_today() >= limit:
            db.update_item(r["id"], status="manager", send_after=None, route_reason="Дневной лимит автоответов уже выбран — проверьте вручную")
            continue
        problems = send_answer(r["id"], r["draft"], "auto")
        if problems:
            db.update_item(r["id"], status="manager", route_reason="Автоотправка не удалась: " + "; ".join(problems))
        else:
            n += 1
    return n

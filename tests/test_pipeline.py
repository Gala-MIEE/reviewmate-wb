"""Тесты: python -m pytest -q  (используют демо-данные и временную БД, WB не вызывается)."""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_tmp = tempfile.mkdtemp()
os.environ.update({"DB_PATH": os.path.join(_tmp, "t.db"), "DEMO_MODE": "1", "LLM_BASE_URL": "", "ADMIN_PASSWORD": ""})

from unittest import mock  # noqa: E402

from app import analyzer, config, db, guard, pipeline  # noqa: E402
from app.wb_client import WBAuthError, WBClient  # noqa: E402

db.init()


def _items():
    with db.session() as con:
        return [dict(r) for r in con.execute("SELECT * FROM items")]


def test_guard_blocks_contacts_and_placeholders():
    sw = db.get_settings()["stop_words"]
    assert guard.check_answer("Спасибо за отзыв!", sw) == []
    assert guard.check_answer("Пишите в телеграм", sw)
    assert guard.check_answer("Звоните +7 900 123-45-67", sw)
    assert guard.check_answer("Плотность {уточнить}", sw)
    assert guard.check_answer("x", sw)


def test_rules_detect_question_in_feedback():
    c = analyzer.classify_rules("feedback", "Удобный планер. А будет версия на полгода?", 4)
    assert c["has_question"]


def test_poll_routes_with_auto_off():
    db.set_setting("auto_reply_enabled", False)
    res = pipeline.poll()
    assert res["error"] is None and res["new_feedbacks"] >= 8 and res["new_questions"] == 2
    items = _items()
    assert all(i["status"] == "manager" for i in items)          # автоответ выключен — всё менеджеру
    assert all(i["draft"] or i["scenario"] == "Токсичный/спам" for i in items)


def test_auto_mode_schedules_only_safe_reviews_and_sends():
    # новые записи, автоответ включён, без задержки
    db.set_setting("auto_reply_enabled", True)
    db.set_setting("auto_delay_min", 0)
    with db.session() as con:
        con.execute("DELETE FROM items")
    pipeline.poll()
    items = _items()
    sched = [i for i in items if i["status"] == "scheduled"]
    assert sched, "должен быть хотя бы один автоответ"
    for i in sched:
        assert i["kind"] == "feedback" and i["rating"] == 5 and not i["has_question"] and i["scenario"] == "Благодарность"
    for i in items:
        if i["kind"] == "question" or i["has_question"] or i["priority"] in ("Высокий", "Критичный"):
            assert i["status"] == "manager"
    sent = pipeline.send_due()
    assert sent == len(sched)
    assert all(db.get_item(i["id"])["status"] == "sent_auto" for i in sched)


def test_manager_answers_question():
    q = next(i for i in _items() if i["kind"] == "question" and i["status"] == "manager")
    assert pipeline.send_answer(q["id"], "Ответ {впишите}", "manager")      # плейсхолдер не пропускаем
    assert pipeline.send_answer(q["id"], "Здравствуйте! Плотность бумаги 100 г/м², маркеры не просвечивают.", "manager") == []
    assert db.get_item(q["id"])["status"] == "sent"


def test_wb_client_formats_and_auth_error():
    wb = WBClient("tkn")
    wb.MIN_INTERVAL = 0
    ok = mock.Mock(status_code=200, content=b'{"data":null,"error":false}', json=lambda: {"data": None, "error": False})
    with mock.patch.object(wb.s, "request", return_value=ok) as rq:
        wb.answer_question("Q1", "Текст")
        method, url = rq.call_args.args
        assert method == "PATCH" and url.endswith("/api/v1/questions")
        assert rq.call_args.kwargs["json"] == {"id": "Q1", "answer": {"text": "Текст"}, "state": "wbRu"}
        wb.answer_feedback("F1", "Спасибо")
        assert rq.call_args.args[0] == "POST" and rq.call_args.kwargs["json"] == {"id": "F1", "text": "Спасибо"}
    bad = mock.Mock(status_code=401, text="unauthorized")
    with mock.patch.object(wb.s, "request", return_value=bad):
        try:
            wb.unanswered_feedbacks()
            assert False
        except WBAuthError:
            pass


def _row(**extra):
    item = {
        "id": "x", "kind": "feedback", "marketplace": "wb", "created_at": "2026-01-01T00:00:00+00:00",
        "fetched_at": "2026-01-01T00:00:00+00:00", "nm_id": 1, "product_name": "Блокнот", "supplier_article": "1",
        "brand": "Paperform", "user_name": "Анна", "rating": 5, "text": "Спасибо", "pros": "", "cons": "",
        "has_media": 0, "status": "manager", "is_sample": 0,
    }
    item.update(extra)
    return item


def test_rules_keep_wish_inside_a_complaint():
    marked = analyzer.classify_rules("feedback", "хотелось бы крепче, ужасная бумага", 2)
    assert marked["has_wish"] is True
    assert marked["scenario"] == "Жалоба"


def test_complaint_template_is_not_a_thank_you_and_names_the_market():
    marked = analyzer.classify_rules("feedback", "запахло химией, опасно для ребёнка", 1)
    text = analyzer.draft_from_template(marked, {"user_name": "Анна", "product_name": "Гирлянда", "marketplace": "ozon"}, "")
    assert marked["theme"] == "Безопасность"
    assert "высокую оценку" not in text
    assert "Ozon" in text and "Wildberries" not in text


def test_mixed_template_fills_pros_and_cons():
    text = analyzer.draft_from_template(
        {"scenario": "Смешанный", "theme": "Общее"},
        {"user_name": "Ира", "product_name": "Блокнот", "marketplace": "wb", "pros": "цвет", "cons": "клей"},
        "",
    )
    assert "цвет" in text and "клей" in text and "{" not in text


def test_thanks_for_a_garland_does_not_talk_about_paper():
    text = analyzer.draft_from_template(
        {"scenario": "Благодарность", "theme": "Качество товара"},
        {"user_name": "Анна", "product_name": "Гирлянда растяжка", "marketplace": "wb"},
        "",
    )
    low = text.lower()
    assert "бумаг" not in low and "запис" not in low
    assert "Гирлянда" not in text and "покупка:" not in low
    assert "качество" in low


def test_product_title_is_not_forced_into_the_wrong_gender():
    thanks = analyzer.draft_from_template(
        {"scenario": "Благодарность", "theme": "Общее"},
        {"user_name": "Анна", "product_name": "Пиньята «Лама» на день рождения", "marketplace": "wb"},
        "",
    )
    complaint = analyzer.draft_from_template(
        {"scenario": "Жалоба", "theme": "Прочее"},
        {"user_name": "Анна", "product_name": "Пиньята «Лабубу» розовая", "marketplace": "ozon"},
        "",
    )
    assert "Пиньята" not in thanks and "Пиньята" not in complaint
    assert "покупка:" not in thanks.lower() and "покупка:" not in complaint.lower()
    assert "понравилось" in thanks.lower()


def test_mixed_review_without_pros_and_cons_stays_readable():
    text = analyzer.draft_from_template(
        {"scenario": "Смешанный", "theme": "Общее"},
        {"user_name": "Анна", "product_name": "Гирлянда", "marketplace": "ozon", "pros": "", "cons": ""},
        "",
    )
    assert "отдельные детали" not in text and "про замечание" not in text and "{" not in text
    assert "вам понравилось" in text and "Замечание передали команде" in text


def test_star_only_review_is_not_auto_answered():
    db.set_setting("auto_reply_enabled", True)
    row = _row(id="star-only", text="", pros="", cons="", rating=5, status="new")
    with db.session() as con:
        db.insert_item(con, row)
    try:
        res = {"new_feedbacks": 0, "new_questions": 0, "to_manager": 0, "scheduled": 0}
        pipeline._finish_new([row], res, db.get_settings(), 0)
        assert db.get_item("star-only")["status"] == "skipped"
        assert res["scheduled"] == 0
    finally:
        with db.session() as con:
            con.execute("DELETE FROM items WHERE id=?", ("star-only",))


def test_send_due_respects_daily_limit():
    db.set_setting("auto_reply_enabled", True)
    db.set_setting("auto_daily_limit", 0)
    with db.session() as con:
        db.insert_item(con, _row(id="limit-1", status="scheduled", draft="Спасибо за отзыв!", send_after="2000-01-01T00:00:00+00:00"))
    try:
        assert pipeline.send_due() == 0
        assert "лимит" in db.get_item("limit-1")["route_reason"]
        assert db.get_item("limit-1")["status"] == "manager"
    finally:
        db.set_setting("auto_daily_limit", 100)
        with db.session() as con:
            con.execute("DELETE FROM items WHERE id=?", ("limit-1",))


def test_token_alert_of_one_market_survives_the_other():
    pipeline._set_token_alert("ozon", "Ozon отклонил ключ")
    pipeline._set_token_alert("wb", None)
    alerts = pipeline._token_alerts()
    assert alerts["ozon"] == "Ozon отклонил ключ"
    assert "wb" not in alerts
    pipeline._set_token_alert("ozon", None)


def test_blank_setting_keeps_the_previous_number():
    from web import _form_int
    assert _form_int({"auto_delay_min": ""}, "auto_delay_min", 60, 0, 10080) == 60
    assert _form_int({"auto_min_rating": "9"}, "auto_min_rating", 5, 1, 5) == 5


def test_brand_filter_does_not_drop_a_review_wb_still_returns(monkeypatch):
    with db.session() as con:
        pending = [r["id"] for r in con.execute(
            "SELECT id FROM items WHERE marketplace='wb' AND status IN ('new','manager','scheduled')")]
        db.insert_item(con, _row(id="brand-other", text="Порвалось", rating=2, brand="Other"))
    raws = [{"id": item_id, "text": "текст отзыва", "productValuation": 5, "pros": "", "cons": "",
             "productDetails": {"brandName": "Paperform", "nmId": 1, "productName": "Блокнот"}} for item_id in pending]
    raws.append({"id": "brand-other", "text": "Порвалось", "productValuation": 2, "pros": "", "cons": "",
                 "productDetails": {"brandName": "Other", "nmId": 2, "productName": "Другой"}})

    class Fake:
        feedbacks_complete = True
        questions_complete = True

        def unanswered_feedbacks(self):
            return raws

        def unanswered_questions(self):
            return []

        def count_unanswered_feedbacks(self):
            return len(raws)

        def count_unanswered_questions(self):
            return 0

        def get_feedback(self, item_id):
            return {"id": item_id, "answer": {"text": "чужой ответ"}}

        def get_question(self, item_id):
            return {"id": item_id, "answer": {"text": "чужой ответ"}}

    monkeypatch.setattr(pipeline, "client", lambda: Fake())
    monkeypatch.setattr(config, "BRAND_FILTER", "Paperform")
    db.set_setting("snapshot_loaded", False)
    try:
        pipeline.poll()
        assert db.get_item("brand-other")["status"] == "manager"
    finally:
        with db.session() as con:
            con.execute("DELETE FROM items WHERE id=?", ("brand-other",))


def test_snapshot_keeps_other_markets(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BRAND_FILTER", "Paperform")
    path = tmp_path / "wb.json"
    path.write_text(
        '[{"id":"snap-1","text":"Спасибо","productValuation":5,"source":"unanswered",'
        '"productDetails":{"brandName":"Paperform","productName":"Блокнот","nmId":1}}]',
        encoding="utf-8",
    )
    with db.session() as con:
        db.insert_item(con, _row(id="ozon-keep", marketplace="ozon", text="Порвалось", rating=2))
        db.insert_item(con, _row(id="wb-old", text="Старый отзыв", rating=3))
    try:
        pipeline.load_snapshot(str(path))
        assert db.get_item("ozon-keep")["status"] == "manager"
        assert db.get_item("wb-old") is None
        assert db.get_item("snap-1")["marketplace"] == "wb"
    finally:
        db.set_setting("snapshot_loaded", False)
        with db.session() as con:
            con.execute("DELETE FROM items WHERE id IN ('ozon-keep','snap-1')")


def test_ordinary_praise_is_not_an_alarm():
    eyes = analyzer.classify_rules("feedback", "Яркие звёзды, сразу бросаются в глаза. Спасибо!", 5)
    mint = analyzer.classify_rules("feedback", "Конфетти мятного цвета, очень красивое. Спасибо!", 5)
    stickers = analyzer.classify_rules("feedback", "Наклейки в комплекте, супер", 5)
    intact = analyzer.classify_rules("feedback", "Без брака, дефектов нет. Брака не обнаружила.", 5)
    holes = analyzer.classify_rules("feedback", "Стильная, в комплекте игла, продели через дырочки", 5)
    reused = analyzer.classify_rules("feedback", "Мы просто приклеили и потом аккуратно отклеили", 5)
    glue = analyzer.classify_rules("feedback", "Приклеили шнурок на клей пистолет", 5)
    instead = analyzer.classify_rules("feedback", "Очень красиво, вместо 4 сделаем 5 и всё супер", 5)
    feared = analyzer.classify_rules("feedback", "Боялась, что придет помятой, пришло все целое, спасибо", 5)
    for marked in (eyes, mint, stickers, intact, holes, reused, glue, instead, feared):
        assert marked["scenario"] == "Благодарность"
        assert marked["priority"] == "Низкий"
        assert marked["theme"] not in ("Безопасность", "Брак и повреждения")
    crushed = analyzer.classify_rules("feedback", "Пришла вся мятая", 2)
    assert crushed["theme"] == "Брак и повреждения" and crushed["priority"] == "Высокий"


def test_holiday_goods_get_the_etalon_priority():
    def priority(text, rating=1):
        return analyzer.classify_rules("feedback", text, rating)["priority"]

    assert priority("Ребёнку попало конфетти в глаз, глаз покраснел, ездили к врачу") == "Критичный"
    assert priority("Ребёнок поранил руку о проволоку внутри пиньяты, острые края") == "Критичный"
    assert priority("Если товар пришёл с браком, буду писать в Роспотребнадзор и в суд") == "Критичный"
    assert priority("Пришла раздавленная, ухо оторвано, коробка вся мятая") == "Высокий"
    assert priority("В комплекте нет биты, праздник уже прошёл") == "Высокий"
    assert priority("Доставка заняла неделю, к празднику не успели") == "Высокий"
    assert priority("Отличный товар, на праздник самое то", 5) == "Низкий"


def test_sample_file_priorities_match_etalon():
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "paperform_test_sets_ozon_ym.xlsx"
    if not path.exists():
        return
    from openpyxl import load_workbook
    book = load_workbook(path, read_only=True, data_only=True)
    bad = []
    try:
        for sheet in book.worksheets:
            if sheet.title == "Описание":
                continue
            rows = sheet.iter_rows(values_only=True)
            header = [str(cell).strip() if cell else "" for cell in next(rows)]
            idx = {name: i for i, name in enumerate(header)}
            for row in rows:
                if not row or not row[idx["id"]]:
                    continue
                kind = "question" if "вопрос" in str(row[idx["тип"]] or "").lower() else "feedback"
                text = " ".join(str(row[idx[key]] or "") for key in ("текст", "достоинства", "недостатки"))
                rating = row[idx["оценка"]]
                try:
                    rating = int(float(rating)) if rating not in (None, "") else None
                except (TypeError, ValueError):
                    rating = None
                got = analyzer.classify_rules(kind, text, rating)["priority"]
                if got != row[idx["эталон_приоритет"]]:
                    bad.append(str(row[idx["id"]]))
    finally:
        book.close()
    assert bad == []


def test_unexpected_wb_error_keeps_the_schedule(monkeypatch):
    class Boom:
        def unanswered_feedbacks(self):
            raise RuntimeError("сеть оборвалась")

        def unanswered_questions(self):
            return []

    monkeypatch.setattr(pipeline, "client", lambda: Boom())
    db.set_setting("snapshot_loaded", False)
    res = pipeline.poll()
    assert "расписанию" in res["error"]
    saved = db.get_settings()["last_poll_result"]["wb"]
    assert saved["error"] == res["error"]
    assert db.get_settings()["last_poll_at"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)

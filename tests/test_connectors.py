"""Коннекторы Ozon и Яндекс Маркета не ходят в сеть в этих тестах."""
import json
from unittest.mock import Mock

from openpyxl import Workbook

from app import config, db, pipeline
from app.ozon_client import OzonClient, normalize_review
from app.sample_set import load_sample_set
from app.ym_client import YandexMarketClient, normalize_feedback


def test_ozon_review_becomes_internal_item():
    item = normalize_review({
        "id": "abc", "sku": 15, "text": "Пришло мятое", "rating": 2,
        "published_at": "2026-01-02T00:00:00Z", "photos_amount": 1, "videos_amount": 0, "status": "UNPROCESSED",
    })
    assert item["id"] == "ozon:abc"
    assert item["marketplace"] == "ozon"
    assert item["rating"] == 2 and item["has_media"] == 1
    assert "мятое" in item["text"]


def test_ozon_unanswered_requests_unprocessed(monkeypatch):
    captured = {}

    def post(self, url, json, timeout):
        captured["url"] = url
        captured["body"] = json
        response = Mock(status_code=200, headers={})
        response.content = b'{"reviews":[],"has_next":false}'
        response.json = lambda: json_loads(response.content)
        return response

    monkeypatch.setattr("requests.Session.post", post)
    assert OzonClient("client", "secret").unanswered_feedbacks() == []
    assert captured["url"].endswith("/v1/review/list")
    assert captured["body"]["status"] == "UNPROCESSED"
    assert captured["body"]["limit"] == 100


def test_ozon_questions_request_unprocessed(monkeypatch):
    calls = []

    def post(self, url, json, timeout):
        calls.append((url, json))
        response = Mock(status_code=200, headers={})
        if "product/info" in url:
            response.content = b'{"items":[{"sku":9,"name":"\\u041f\\u0438\\u043d\\u044c\\u044f\\u0442\\u0430 \\u00ab\\u041b\\u0430\\u043c\\u0430\\u00bb"}]}'
        else:
            response.content = '{"questions":[{"id":"q1","sku":9,"text":"Есть бита?","author_name":"Анна","published_at":"2026-01-01T00:00:00Z"}],"has_next":false}'.encode()
        response.json = lambda: json_loads(response.content)
        return response

    monkeypatch.setattr("requests.Session.post", post)
    items = OzonClient("client", "secret").unanswered_questions()
    assert calls[0][0].endswith("/v1/question/list")
    assert calls[0][1]["filter"]["status"] == "UNPROCESSED"
    assert items[0]["id"] == "ozon:q1" and items[0]["kind"] == "question"
    assert items[0]["nm_id"] == 9 and items[0]["user_name"] == "Анна"
    assert items[0]["product_name"] == "Пиньята «Лама»"
    assert "бита" in items[0]["text"]


def test_ozon_question_answer_is_not_a_review_comment(monkeypatch):
    db.init()
    with db.session() as con:
        db.insert_item(con, {
            "id": "ozon:q-send", "kind": "question", "marketplace": "ozon", "created_at": "2026-01-01T00:00:00+00:00",
            "fetched_at": "2026-01-01T00:00:00+00:00", "nm_id": 15, "product_name": "Пиньята", "supplier_article": "15",
            "brand": "Paperform", "user_name": "Анна", "rating": None, "text": "Есть бита?", "pros": "", "cons": "",
            "has_media": 0, "status": "manager", "is_sample": 0,
        })
    called = {}

    class Fake:
        def __init__(self, *args, **kwargs):
            pass

        def answer_question(self, item_id, text, sku):
            called["question"] = (item_id, text, sku)

        def answer_feedback(self, *args, **kwargs):
            raise AssertionError("вопрос ушёл как отзыв")

    monkeypatch.setattr(config, "DEMO_MODE", False)
    monkeypatch.setattr("app.ozon_client.OzonClient", Fake)
    try:
        assert pipeline.send_answer("ozon:q-send", "Да, бита лежит в комплекте.", "manager") == []
        assert called["question"] == ("ozon:q-send", "Да, бита лежит в комплекте.", 15)
        assert db.get_item("ozon:q-send")["status"] == "sent"
    finally:
        with db.session() as con:
            con.execute("DELETE FROM items WHERE id=?", ("ozon:q-send",))


def test_ym_feedback_splits_pros_and_cons():
    item = normalize_feedback({
        "feedbackId": 77,
        "createdAt": "2026-03-01T10:00:00+03:00",
        "author": "Ира",
        "description": {"comment": "долго везли", "advantages": "цвет", "disadvantages": "коробка"},
        "statistics": {"rating": 3},
        "identifiers": {"offerId": "PF-1"},
        "media": {"photos": ["a"], "videos": []},
    })
    assert item["id"] == "ym:77"
    assert item["marketplace"] == "ym"
    assert item["text"] == "долго везли" and item["pros"] == "цвет" and item["cons"] == "коробка"
    assert item["rating"] == 3 and item["has_media"] == 1
    assert item["supplier_article"] == "PF-1"


def test_ym_unanswered_asks_for_reaction(monkeypatch):
    captured = {}

    def post(self, url, json, params, timeout):
        captured["url"] = url
        captured["body"] = json
        captured["params"] = params
        response = Mock(status_code=200, headers={})
        response.content = b'{"status":"OK","result":{"feedbacks":[],"paging":{}}}'
        response.json = lambda: json_loads(response.content)
        return response

    monkeypatch.setattr("requests.Session.post", post)
    assert YandexMarketClient("token", "555").unanswered_feedbacks() == []
    assert "/v2/businesses/555/goods-feedback" in captured["url"]
    assert captured["body"]["reactionStatus"] == "NEED_REACTION"
    assert captured["params"]["limit"] == 50


def test_sample_file_waits_for_a_draft_and_does_not_publish(tmp_path, monkeypatch):
    db.init()
    book = Workbook()
    sheet = book.active
    sheet.title = "Ozon"
    sheet.append(["площадка", "тип", "id", "дата", "товар", "артикул", "имя", "оценка", "текст", "достоинства", "недостатки", "фото"])
    sheet.append(["Ozon", "отзыв", "t1", "10.11.2026 16:50", "Гирлянда", "1800547231", "Виктория", 1, "Перепутали заказ", None, None, 0])
    market = book.create_sheet("Яндекс Маркет")
    market.append(["площадка", "тип", "id", "дата", "товар", "артикул", "имя", "оценка", "текст", "достоинства", "недостатки", "фото"])
    market.append(["Яндекс Маркет", "вопрос", "77", "11.11.2026 08:03", "Пиньята", "1800276256", "Юлия", None, "Можно повесить несколько раз?", None, None, 0])
    path = tmp_path / "sample.xlsx"
    book.save(path)
    try:
        stats = load_sample_set(str(path))
        assert stats["ozon"] == 1 and stats["ym"] == 1
        review = db.get_item("ozon:t1")
        assert review["marketplace"] == "ozon" and review["is_sample"] == 1
        assert review["status"] == "manager" and review["rating"] == 1 and review["draft"]
        question = db.get_item("ym:77")
        assert question["kind"] == "question" and question["status"] == "manager"
        monkeypatch.setattr("app.ozon_client.OzonClient", lambda *a, **k: (_ for _ in ()).throw(AssertionError("сеть")))
        assert pipeline.send_answer("ozon:t1", "Здравствуйте! Проверим комплектацию и напишем, как оформить замену.", "manager") == []
        assert db.get_item("ozon:t1")["status"] == "sent"
        again = load_sample_set(str(path))
        assert again["ozon"] == 0 and again["skipped"] == 2
        db.update_item("ym:77", analyzed_by="llm", priority="Низкий", scenario="Благодарность", theme="Прочее")
        load_sample_set(str(path))
        kept = db.get_item("ym:77")
        assert kept["analyzed_by"] == "llm" and kept["priority"] == "Низкий" and kept["scenario"] == "Благодарность"
    finally:
        with db.session() as con:
            con.execute("DELETE FROM items WHERE is_sample=1")


def _open_row(**extra):
    item = {
        "id": "ozon:gone", "kind": "feedback", "marketplace": "ozon", "created_at": "2026-01-01T00:00:00+00:00",
        "fetched_at": "2026-01-01T00:00:00+00:00", "nm_id": 15, "product_name": "Пиньята", "supplier_article": "15",
        "brand": "Paperform", "user_name": "Анна", "rating": 5, "text": "Спасибо, всё понравилось", "pros": "", "cons": "",
        "has_media": 0, "status": "manager", "is_sample": 0,
    }
    item.update(extra)
    return item


class _QuietMarket:
    reviews_complete = True
    questions_complete = True

    def __init__(self, *args, **kwargs):
        pass

    def unanswered_feedbacks(self):
        return []

    def unanswered_questions(self):
        return []

    def review_count(self):
        return {"unprocessed": 0}

    def question_count(self):
        return {"unprocessed": 0}


def test_ozon_review_answered_in_cabinet_leaves_the_queue(monkeypatch):
    db.init()
    with db.session() as con:
        db.insert_item(con, _open_row())
    monkeypatch.setattr(config, "OZON_ENABLED", True)
    monkeypatch.setattr("app.ozon_client.OzonClient", _QuietMarket)
    try:
        pipeline.poll_others()
        assert db.get_item("ozon:gone")["status"] == "answered_upstream"
    finally:
        with db.session() as con:
            con.execute("DELETE FROM items WHERE id=?", ("ozon:gone",))


def test_ozon_empty_list_does_not_close_queue_when_counter_is_positive(monkeypatch):
    db.init()
    with db.session() as con:
        db.insert_item(con, _open_row(id="ozon:still"))

    class Counting(_QuietMarket):
        def review_count(self):
            return {"unprocessed": 4}

    monkeypatch.setattr(config, "OZON_ENABLED", True)
    monkeypatch.setattr("app.ozon_client.OzonClient", Counting)
    try:
        pipeline.poll_others()
        assert db.get_item("ozon:still")["status"] == "manager"
    finally:
        with db.session() as con:
            con.execute("DELETE FROM items WHERE id=?", ("ozon:still",))


def test_ym_question_is_not_published_as_a_review(monkeypatch):
    db.init()
    with db.session() as con:
        db.insert_item(con, _open_row(id="ym:q-real", marketplace="ym", kind="question", rating=None, text="Есть бита?"))

    class Fake:
        def __init__(self, *args, **kwargs):
            pass

        def answer_feedback(self, *args, **kwargs):
            raise AssertionError("вопрос ушёл как отзыв")

    monkeypatch.setattr("app.ym_client.YandexMarketClient", Fake)
    try:
        problems = pipeline.send_answer("ym:q-real", "Здравствуйте! Да, бита лежит в комплекте.", "manager")
        assert problems
        assert db.get_item("ym:q-real")["status"] == "manager"
    finally:
        with db.session() as con:
            con.execute("DELETE FROM items WHERE id=?", ("ym:q-real",))


def test_ozon_review_rejects_text_over_3000():
    db.init()
    with db.session() as con:
        db.insert_item(con, _open_row(id="ozon:long"))
    try:
        problems = pipeline.send_answer("ozon:long", "а" * 3001, "manager")
        assert problems and "3000" in problems[0]
        assert db.get_item("ozon:long")["status"] == "manager"
    finally:
        with db.session() as con:
            con.execute("DELETE FROM items WHERE id=?", ("ozon:long",))


def test_live_mode_does_not_import_the_sample_file(monkeypatch):
    monkeypatch.setattr(config, "DEMO_MODE", False)
    stats = load_sample_set()
    assert stats == {"ozon": 0, "ym": 0, "skipped": 0, "path": None}


def test_ozon_outage_is_a_clear_message(monkeypatch):
    from app.ozon_client import OzonError
    monkeypatch.setattr(config, "DEMO_MODE", False)
    db.init()
    with db.session() as con:
        db.insert_item(con, _open_row(id="ozon:down", text="Пришло мятое", rating=2))

    class Fake:
        def __init__(self, *args, **kwargs):
            pass

        def answer_feedback(self, *args, **kwargs):
            raise OzonError("500")

    monkeypatch.setattr("app.ozon_client.OzonClient", Fake)
    try:
        problems = pipeline.send_answer("ozon:down", "Здравствуйте! Заменим повреждённый товар.", "manager")
        assert problems and "временно не отвечает" in problems[0]
        assert "500" not in problems[0]
        assert db.get_item("ozon:down")["status"] == "manager"
    finally:
        with db.session() as con:
            con.execute("DELETE FROM items WHERE id=?", ("ozon:down",))


def test_demo_does_not_publish_to_ozon(monkeypatch):
    monkeypatch.setattr(config, "DEMO_MODE", True)
    db.init()
    with db.session() as con:
        db.insert_item(con, _open_row(id="ozon:demo-live", text="Пришло мятое", rating=2, is_sample=0))
    monkeypatch.setattr("app.ozon_client.OzonClient", lambda *a, **k: (_ for _ in ()).throw(AssertionError("кабинет")))
    try:
        assert pipeline.send_answer("ozon:demo-live", "Здравствуйте! Заменим повреждённый товар.", "manager") == []
        assert db.get_item("ozon:demo-live")["status"] == "sent"
    finally:
        with db.session() as con:
            con.execute("DELETE FROM items WHERE id=?", ("ozon:demo-live",))


def test_ozon_catalog_name_replaces_sku(monkeypatch):
    def post(self, url, json, timeout):
        response = Mock(status_code=200, headers={})
        if "product/info" in url:
            response.content = '{"items":[{"sku":15,"name":"Пиньята «Лама»"}]}'.encode()
        else:
            response.content = '{"reviews":[{"id":"r1","sku":15,"text":"мятое","rating":2,"published_at":"2026-01-02T00:00:00Z"}],"has_next":false}'.encode()
        response.json = lambda: json_loads(response.content)
        return response

    monkeypatch.setattr("requests.Session.post", post)
    items = OzonClient("client", "secret").unanswered_feedbacks()
    assert items[0]["product_name"] == "Пиньята «Лама»"


def test_other_marketplaces_skipped_without_keys(monkeypatch):
    monkeypatch.setattr(config, "OZON_ENABLED", False)
    monkeypatch.setattr(config, "YM_ENABLED", False)
    assert pipeline.poll_others() == {"ozon": {"skipped": "нет ключа"}, "ym": {"skipped": "нет ключа"}}


def json_loads(raw):
    return json.loads(raw)

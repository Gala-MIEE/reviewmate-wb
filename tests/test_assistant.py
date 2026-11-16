"""Словарь формулировок в CSV, справочник фактов, отчёт, выводы и вопросы к нейросети (нейросеть подменена)."""
import io
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import analyzer, assistant, config, db, report

PRODUCT = "Тестовая пиньята «Проверка» на день рождения"


def _add(item_id, text, rating, tone, theme="Прочее", priority="Низкий", kind="feedback", status="manager",
         product=PRODUCT, market="wb", has_question=0, user_name="Секретное Имя"):
    now = datetime.now(timezone.utc) - timedelta(days=1)
    with db.session() as con:
        db.insert_item(con, {
            "id": item_id, "kind": kind, "marketplace": market, "created_at": now.replace(microsecond=0).isoformat(),
            "fetched_at": db.now_iso(), "nm_id": None, "product_name": product, "supplier_article": "", "brand": "Paperform",
            "user_name": user_name, "rating": rating, "text": text, "pros": "", "cons": "", "has_media": 0,
            "scenario": "Жалоба" if tone == "Негатив" else "Благодарность", "theme": theme, "tone": tone,
            "priority": priority, "has_question": has_question, "has_wish": 0, "summary": "", "draft": "Черновик",
            "status": status, "analyzed_by": "rules",
        })


@pytest.fixture(scope="module", autouse=True)
def seeded():
    db.init()
    _add("t-a1", "Пиньята пришла раздавленная", 1, "Негатив", "Брак и повреждения", "Высокий")
    _add("t-a2", "Пришла мятая, ухо оторвано", 2, "Негатив", "Брак и повреждения", "Высокий")
    _add("t-a3", "Коробка смята, пиньята повреждена", 1, "Негатив", "Брак и повреждения", "Высокий")
    _add("t-a4", "Дети в восторге, спасибо!", 5, "Позитив")
    _add("t-a5", "Ребёнок поранил руку о проволоку", 1, "Негатив", "Безопасность", "Критичный")
    _add("t-a6", "Сколько конфет помещается?", None, "Нейтрально", kind="question", priority="Средний", has_question=1)


# ---------- словарь формулировок ----------

def test_keywords_come_from_csv():
    kb = analyzer.load_keywords()
    assert list(kb["themes"])[0] == "Безопасность"        # порядок строк = приоритет тем
    assert "порани" in kb["themes"]["Безопасность"]
    assert "в глаз" in kb["exact"]
    assert analyzer.QUESTION_WORD_RE.search("Подскажите, пожалуйста")
    assert analyzer.WISH_RE.search("хотелось бы больше цветов")


def test_keywords_csv_rejects_unknown_mode(tmp_path):
    bad = tmp_path / "k.csv"
    bad.write_text("группа,слово,поиск\nТема: Безопасность,ожог,где-нибудь\n", encoding="utf-8")
    with pytest.raises(ValueError, match="строка 2"):
        analyzer.load_keywords(str(bad))


def test_keywords_csv_rejects_unknown_group(tmp_path):
    bad = tmp_path / "k.csv"
    bad.write_text("группа,слово,поиск\nНегативчик,ужас,основа\n", encoding="utf-8")
    with pytest.raises(ValueError, match="неизвестная группа"):
        analyzer.load_keywords(str(bad))


def test_keywords_csv_accepts_excel_bom(tmp_path):
    path = tmp_path / "k.csv"
    path.write_text("﻿группа,слово,поиск\nТема: Цена,дорого,основа\n", encoding="utf-8")
    assert analyzer.load_keywords(str(path))["themes"] == {"Цена": ["дорого"]}


# ---------- справочник фактов ----------

def test_faq_answers_known_fact_without_llm():
    assert "маска и бита" in analyzer.faq_answer("Что входит в комплект? Есть ли бита?", "Пиньята Лама на день рождения")


def test_faq_keeps_unknown_fact_as_placeholder():
    answer = analyzer.faq_answer("Какая длина гирлянды?", "Гирлянда растяжка С Днем Рождения")
    assert "{уточнить" in answer


def test_faq_does_not_mix_products():
    assert "бита" not in analyzer.faq_answer("Что входит в комплект?", "Конфетти для праздника")


def test_rules_question_draft_uses_faq(monkeypatch):
    monkeypatch.setattr(config, "LLM_ENABLED", False)
    item = {"kind": "question", "text": "Есть ли бита в комплекте?", "pros": "", "cons": "", "rating": None,
            "product_name": "Пиньята Лама на день рождения", "user_name": "", "marketplace": "wb"}
    draft = analyzer.analyze(item, None, "")["draft"]
    assert "маска и бита" in draft and "{впишите" not in draft


def test_llm_prompt_gets_product_facts(monkeypatch):
    sent = {}

    def fake(messages, timeout=120):
        sent["user"] = messages[1]["content"]
        sent["system"] = messages[0]["content"]
        return json.dumps({"scenario": "Вопрос", "theme": "Комплектация", "tone": "Нейтрально", "priority": "Средний",
                           "has_question": True, "has_wish": False, "summary": "", "draft": "В комплект входят маска и бита."})
    monkeypatch.setattr(analyzer, "_llm", fake)
    analyzer.classify_llm({"kind": "question", "text": "Есть бита?", "product_name": "Пиньята Лама"}, None)
    assert "маска и бита" in sent["user"]
    assert "пиньяты" in sent["system"] and "ПРИМЕРЫ ХОРОШИХ ОТВЕТОВ" in sent["system"]


# ---------- отчёт ----------

def test_report_counts_match_data():
    rep = report.collect(30, product=PRODUCT)
    assert rep["reviews"] == 5 and rep["questions"] == 1
    assert rep["avg"] == round((1 + 2 + 1 + 5 + 1) / 5, 2)
    assert rep["neg_share"] == 80
    assert dict(rep["themes_neg"])["Брак и повреждения"] == 3
    assert any("Брак и повреждения" in x for x in rep["recurring"])
    assert len(rep["critical"]) == 1


def test_report_xlsx_has_sheets_and_numbers():
    from openpyxl import load_workbook
    rep = report.collect(30, product=PRODUCT)
    book = load_workbook(io.BytesIO(report.to_xlsx(rep, {"conclusion": "Вывод", "recommendations": [{"action": "Усилить упаковку", "owner": "склад"}]})))
    assert {"Сводка", "Выводы", "Товары", "Критичные"} <= set(book.sheetnames)
    values = {r[0]: r[1] for r in book["Сводка"].iter_rows(min_row=2, values_only=True)}
    assert values["Отзывов"] == 5


def test_samples_have_no_customer_names():
    rows = report.samples(30, product=PRODUCT)
    assert rows and "Секретное Имя" not in json.dumps(rows, ensure_ascii=False)


# ---------- нейросеть: выводы, вопрос, переписать ----------

def test_summary_is_cached_until_numbers_change(monkeypatch):
    calls = []

    def fake(messages, timeout=120):
        calls.append(messages)
        return json.dumps({"conclusion": "Главная проблема — повреждения пиньяты.",
                           "recommendations": [{"action": "Усилить упаковку", "owner": "склад"}]})
    monkeypatch.setattr(analyzer, "_llm", fake)
    rep = report.collect(30, product=PRODUCT)
    first = assistant.summarize(rep, report.samples(30, product=PRODUCT))
    second = assistant.summarize(rep, [])
    assert first["conclusion"] == second["conclusion"] and len(calls) == 1
    assert "Выводы: Главная проблема" in report.to_text(rep, first)


def test_ask_sends_numbers_but_not_names(monkeypatch):
    seen = {}

    def fake(messages, timeout=120):
        seen["text"] = messages[1]["content"]
        return "Чаще всего жалуются на повреждения: 3 отзыва."
    monkeypatch.setattr(analyzer, "_llm", fake)
    rep = report.collect(30, product=PRODUCT)
    answer = assistant.ask("Что не так с пиньятой?", rep, report.samples(30, product=PRODUCT))
    assert "3 отзыва" in answer
    assert "Секретное Имя" not in seen["text"] and '"reviews": 5' in seen["text"]


def test_rewrite_keeps_signature_and_placeholders(monkeypatch):
    def fake(messages, timeout=120):
        assert "Подпись" not in messages[1]["content"]
        return json.dumps({"variants": ["Коротко: {уточнить: длина}."]})
    monkeypatch.setattr(analyzer, "_llm", fake)
    out = assistant.rewrite({"product_name": "Гирлянда", "text": "Какая длина?"}, "Длинный ответ\n\nПодпись", "short", "Подпись")
    assert out == "Коротко: {уточнить: длина}.\n\nПодпись"


# ---------- страницы ----------

@pytest.fixture
def client():
    import web
    return web.app.test_client()


def test_pages_open_without_llm(client, monkeypatch):
    monkeypatch.setattr(config, "LLM_ENABLED", False)
    assert client.get("/ask").status_code == 200
    page = client.get("/analytics?days=30").get_data(as_text=True)
    assert "Скачать отчёт" in page and "Скопировать отчёт" in page
    r = client.get("/analytics/report.xlsx?days=30")
    assert r.status_code == 200 and r.mimetype.endswith("spreadsheetml.sheet")
    r = client.post("/item/t-a4", data={"action": "short", "text": "Спасибо!"}, follow_redirects=True)
    assert "Нейросеть не подключена" in r.get_data(as_text=True)


def test_ask_page_with_llm(client, monkeypatch):
    monkeypatch.setattr(config, "LLM_ENABLED", True)
    monkeypatch.setattr(analyzer, "_llm", lambda m, timeout=120: "Повреждения при доставке — главная тема.")
    page = client.post("/ask", data={"question": "Что не так?", "days": "30", "product": PRODUCT}).get_data(as_text=True)
    assert "Повреждения при доставке" in page


def test_variants_and_pick(client, monkeypatch):
    monkeypatch.setattr(config, "LLM_ENABLED", True)
    monkeypatch.setattr(analyzer, "_llm", lambda m, timeout=120: json.dumps({"variants": ["Вариант А", "Вариант Б", "Вариант В"]}))
    page = client.post("/item/t-a4", data={"action": "variants", "text": "Спасибо!"}).get_data(as_text=True)
    assert "Вариант Б" in page and "Взять этот вариант" in page
    client.post("/item/t-a4", data={"action": "use_variant", "variant": "Вариант Б"})
    assert db.get_item("t-a4")["draft"] == "Вариант Б"


def test_summary_button(client, monkeypatch):
    monkeypatch.setattr(config, "LLM_ENABLED", True)
    monkeypatch.setattr(analyzer, "_llm", lambda m, timeout=120: json.dumps(
        {"conclusion": "Итог периода.", "recommendations": [{"action": "Проверить партию", "owner": "производство"}]}))
    page = client.post("/analytics/summary", data={"days": "7"}, follow_redirects=True).get_data(as_text=True)
    assert "Итог периода." in page and "Проверить партию" in page

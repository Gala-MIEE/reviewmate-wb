"""Демо-режим: имитация API WB на тестовых данных. Ничего не отправляется на Wildberries.
Товары и отзывы ниже придуманы для проверки сервиса, это не реальные данные бренда."""
import json
import os
import random
from datetime import datetime, timedelta, timezone

from . import config

STATE = os.path.join(os.path.dirname(os.path.abspath(config.DB_PATH)), "demo_wb_state.json")

P = [
    (180000001, "Демо: пиньята «Лама» на день рождения", "PF-PIN-LAMA"),
    (180000002, "Демо: гирлянда-растяжка «Звёзды» синяя", "PF-GR-STAR-BL"),
    (180000003, "Демо: гирлянда многоразовая «С днём рождения» золотая", "PF-GR-HB-GOLD"),
    (180000004, "Демо: конфетти для праздника разноцветное", "PF-CONF-MIX"),
]

# (вид, индекс товара в P, оценка, текст, достоинства, недостатки)
SEED = [
    ("feedback", 0, 5, "Пиньята огромная и яркая, дети были в восторге! Бита и маска в комплекте. Спасибо!", "", ""),
    ("feedback", 1, 5, "Всё отлично, пришла быстро, звёзды плотные и аккуратные.", "Красиво смотрится на стене", ""),
    ("feedback", 0, 1, "Пиньята пришла раздавленная, ухо оторвано. Дарить такое нельзя.", "", "Брак"),
    ("feedback", 2, 4, "Красивая гирлянда, но буквы мелковаты. А будет такая же с надписью «С Новым годом»?", "", ""),
    ("feedback", 0, 1, "В комплекте нет биты, хотя на фото есть. Пришлось искать палку прямо на празднике.", "", ""),
    ("feedback", 3, 5, "", "Яркие цвета, ровные кружочки", ""),
    ("feedback", 3, 2, "Конфетти электризуются и липнут ко всему, потом долго собирали по квартире.", "", ""),
    ("question", 0, None, "Подскажите, сколько конфет помещается в пиньяту?", "", ""),
    ("question", 1, None, "Какая длина гирлянды в метрах?", "", ""),
    ("feedback", 2, 5, "Повесили на юбилей мамы, все гости фотографировались. Спасибо!", "", ""),
]

# Новые отзывы, которые «приходят» на каждом втором опросе: (вид, оценка, текст)
EXTRA = [
    ("feedback", 5, "Брали сыну на 5 лет, праздник удался. Разбивали все гости по очереди."),
    ("feedback", 4, "Красивая, но разбить не могли минут 15, взрослым пришлось помогать."),
    ("feedback", 2, "Доставка заняла неделю, к празднику не успели. Обидно."),
    ("question", None, "Гирлянда правда многоразовая? Можно повесить несколько раз?"),
    ("feedback", 1, "Ребёнку попало конфетти в глаз, ездили к врачу. Осторожно с маленькими детьми!"),
    ("feedback", 5, "Супер, всё как на фото!"),
]


def _now():
    return datetime.now(timezone.utc)


def _make(kind, pi, rating, text, pros="", cons="", minutes_ago=0):
    nm, name, art = P[pi]
    base = {
        "id": f"demo-{kind[0]}-{random.randint(10**7, 10**8)}",
        "text": text,
        "createdDate": (_now() - timedelta(minutes=minutes_ago)).isoformat(),
        "productDetails": {"nmId": nm, "productName": name, "supplierArticle": art, "brandName": "Paperform"},
        "wasViewed": False,
    }
    if kind == "feedback":
        base.update({"productValuation": rating, "pros": pros, "cons": cons, "userName": random.choice(["Анна", "Мария", "Игорь", "", "Ольга"]),
                     "photoLinks": [], "video": None, "answer": None})
    return kind, base


def _load():
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    st = {"feedbacks": [], "questions": [], "sent": [], "polls": 0}
    for i, (kind, pi, r, t, pros, cons) in enumerate(SEED):
        k, it = _make(kind, pi, r, t, pros, cons, minutes_ago=(len(SEED) - i) * 37)
        st["feedbacks" if k == "feedback" else "questions"].append(it)
    _save(st)
    return st


def _save(st):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


class MockWBClient:
    feedbacks_complete = True
    questions_complete = True

    def __init__(self, *a, **kw):
        pass

    def unanswered_feedbacks(self, date_from=None):
        st = _load()
        st["polls"] += 1
        if st["polls"] > 1 and st["polls"] % 2 == 0:   # на каждом втором опросе «приходит» новый отзыв/вопрос
            kind, r, t = random.choice(EXTRA)
            k, it = _make(kind, random.randrange(len(P)), r, t)
            st["feedbacks" if k == "feedback" else "questions"].append(it)
        _save(st)
        return list(st["feedbacks"])

    def unanswered_questions(self, date_from=None):
        return list(_load()["questions"])

    def count_unanswered_feedbacks(self):
        return len(_load()["feedbacks"])

    def count_unanswered_questions(self):
        return len(_load()["questions"])

    def get_feedback(self, feedback_id):
        return self._lookup("feedbacks", feedback_id)

    def get_question(self, question_id):
        return self._lookup("questions", question_id)

    def _lookup(self, coll, item_id):
        st = _load()
        for raw in st[coll]:
            if raw["id"] == item_id:
                return raw
        for raw in st["sent"]:
            if raw["id"] == item_id and raw.get("text"):
                return {"id": item_id, "answer": {"text": raw["text"]}}
        return None

    def _answer(self, coll, item_id, text, how):
        st = _load()
        st[coll] = [x for x in st[coll] if x["id"] != item_id]
        st["sent"].append({"id": item_id, "text": text, "how": how, "at": _now().isoformat()})
        _save(st)
        return {"data": None, "error": False}

    def answer_feedback(self, feedback_id, text):
        return self._answer("feedbacks", feedback_id, text, "answer")

    def answer_question(self, question_id, text):
        return self._answer("questions", question_id, text, "answer")

    def reject_question(self, question_id, text="Вопрос отклонён"):
        return self._answer("questions", question_id, text, "reject")

    def mark_question_viewed(self, question_id):
        return {}

    def product_cards(self):
        return [{"nmID": nm, "title": name, "vendorCode": art, "brand": "Paperform",
                 "description": "Демо-описание. В рабочем режиме описание берётся из карточки WB (категория «Контент»)."} for nm, name, art in P]

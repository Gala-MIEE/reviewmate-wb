"""Правила без нейросети: формы слов и ложные срабатывания.

Каждый случай — живая формулировка, которой нет в тестовом наборе xlsx: так видно,
что правила узнают слово в любой форме, а не только в той, что встретилась в наборе.
"""
import pytest

from app import analyzer


def rules(text, rating):
    return analyzer.classify_rules("feedback", text, rating)


@pytest.mark.parametrize("text, rating", [
    ("Сын поранил руку о край пиньяты", 1),
    ("Дочка поранилась, когда разбивала пиньяту", 1),
    ("Ребёнок травмировался битой", 1),
    ("У ребёнка аллергическая реакция на краску", 2),
    ("Дочка порезалась об острый край", 1),
    ("Сыну попало конфетти в глаз, ездили к врачу", 1),
    ("Если брак не заменят, пишу в Роспотребнадзор", 1),
])
def test_safety_and_legal_are_critical(text, rating):
    assert rules(text, rating)["priority"] == "Критичный"


@pytest.mark.parametrize("text, rating", [
    ("Пиньята пришла поцарапанная и раздавленная", 1),
    ("Пришла деформированная коробка и помятая пиньята", 2),
    ("Гирлянда пришла мятая", 2),
    ("Не успели к празднику, заказ задержали", 1),
])
def test_damage_and_missed_holiday_are_high(text, rating):
    assert rules(text, rating)["priority"] == "Высокий"


@pytest.mark.parametrize("text, rating", [
    ("Яркие звёзды, сразу бросаются в глаза. Спасибо!", 5),
    ("Конфетти мятного цвета, очень нежные, спасибо!", 5),
    ("Наклейки в комплекте, супер", 5),
    ("Шёлковая лента у гирлянды, очень красиво", 5),
    ("Совершенно безопасно для детей, спасибо", 5),
    ("Ничего не помялось, всё целое, спасибо", 5),
])
def test_praise_is_not_alarm(text, rating):
    r = rules(text, rating)
    assert r["scenario"] == "Благодарность"
    assert r["priority"] == "Низкий"
    assert r["theme"] not in ("Безопасность", "Брак и повреждения")


@pytest.mark.parametrize("text", ["Очень понравилось!", "Понравилась пиньята, рекомендую"])
def test_praise_word_forms_count_as_positive(text):
    assert rules(text, 5)["tone"] == "Позитив"


def test_liked_but_remark_is_mixed():
    r = rules("Понравилось, но маловато конфетти", 4)
    assert r["scenario"] == "Смешанный"


@pytest.mark.parametrize("word, stem", [
    ("понравилось", "понрав"), ("поранил", "порани"), ("травмировался", "травм"),
    ("аллергическая", "аллерг"), ("задержка", "задерж"), ("разочарование", "разочаров"),
    ("деформированная", "деформ"), ("поцарапанная", "поцарап"), ("порезался", "порезал"),
])
def test_stem_matches_any_ending(word, stem):
    assert analyzer._PATTERNS[stem].search(word)


@pytest.mark.parametrize("word, stem", [
    ("безопасно", "опасн"), ("в глаза", "в глаз"), ("мятного", "мятая"), ("шёлковая", "шёл"),
])
def test_stem_needs_word_start(word, stem):
    assert not analyzer._PATTERNS[stem].search(word)

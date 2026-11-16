"""Темы для соцсетей не ходят в сеть: ответ модели подменён."""
import json

from app import config, content_ideas


def _row(**extra):
    item = {
        "id": "1", "kind": "question", "theme": "Комплектация", "product_name": "Пиньята",
        "text": "Что входит в комплект? Есть ли бита?", "pros": "", "cons": "",
        "rating": None, "has_question": 1, "wish": False, "scenario": "Вопрос", "tone": "Нейтрально",
    }
    item.update(extra)
    return item


def test_kit_question_becomes_a_signal():
    questions, wishes, notes = content_ideas.sources([_row()])
    grouped = content_ideas._groups(questions, wishes, notes)
    assert grouped[0]["basis"] == "вопрос"
    assert grouped[0]["theme"] == "Комплектация"
    assert "комплект" in grouped[0]["examples"][0].lower()


def test_propose_drops_empty_titles(monkeypatch):
    monkeypatch.setattr(config, "LLM_ENABLED", True)

    def fake(messages, timeout=120):
        assert "Что входит в комплект" in messages[1]["content"]
        return json.dumps({"ideas": [
            {"title": "Что в комплекте пиньяты", "angle": "Показать биту, конфетти и как собрать фигуру.",
             "basis": "вопрос", "signals": "1 вопрос о комплектации", "products": "Пиньята"},
            {"title": "", "angle": "пусто"},
        ]})

    monkeypatch.setattr("app.analyzer._llm", fake)
    ideas = content_ideas.propose([{
        "basis": "вопрос", "theme": "Комплектация", "n": 1,
        "products": ["Пиньята"], "examples": ["Что входит в комплект?"],
    }])
    assert len(ideas) == 1
    assert ideas[0]["title"] == "Что в комплекте пиньяты"
    assert ideas[0]["basis"] == "вопрос"


def test_without_model_the_tab_explains(monkeypatch):
    monkeypatch.setattr(config, "LLM_ENABLED", False)
    result = content_ideas.for_period([], [], [])
    assert result["ideas"] == [] and result["pending"] is False
    assert "не подключена" in result["error"]

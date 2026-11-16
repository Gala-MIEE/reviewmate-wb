"""Проверки перед отправкой ответа на WB и правила маршрутизации «автоответ / менеджер»."""
import re

PHONE_RE = re.compile(r"(\+7|8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")
PLACEHOLDER_RE = re.compile(r"\{[^}]*\}")


def check_answer(text: str, stop_words: list[str]) -> list[str]:
    """Возвращает список проблем; пустой список = можно отправлять."""
    problems = []
    t = (text or "").strip()
    if len(t) < 2:
        problems.append("Ответ пустой")
    if len(t) > 5000:
        problems.append("Ответ длиннее 5000 знаков")
    if PLACEHOLDER_RE.search(t):
        problems.append("Остались поля в фигурных скобках — заполните их")
    if PHONE_RE.search(t):
        problems.append("В ответе номер телефона — площадки запрещают контакты в ответах")
    low = t.lower()
    hits = [w for w in stop_words if w and w.lower() in low]
    if hits:
        problems.append("Стоп-слова: " + ", ".join(hits))
    return problems


def route(item: dict, a: dict, settings: dict, auto_sent_today: int) -> tuple[str, str]:
    """Решает, куда идёт отзыв: ('scheduled', причина) — автоотправка с задержкой, или ('manager', причина)."""
    if item["kind"] == "question":
        return "manager", "Вопрос покупателя — отвечает менеджер"
    if a.get("has_question"):
        return "manager", "В отзыве есть вопрос — отвечает менеджер"
    if a.get("scenario") == "Токсичный/спам":
        return "manager", "Подозрение на спам или оскорбления — решает менеджер"
    if a.get("priority") in ("Критичный", "Высокий"):
        return "manager", f"Приоритет «{a.get('priority')}» — нужен менеджер"
    if not settings.get("auto_reply_enabled"):
        return "manager", "Автоответ выключен — черновик ждёт проверки"
    rating = item.get("rating") or 0
    if rating < int(settings.get("auto_min_rating", 5)):
        return "manager", f"Оценка {rating}★ ниже порога автоответа ({settings.get('auto_min_rating')}★)"
    if a.get("scenario") not in settings.get("auto_scenarios", []):
        return "manager", f"Сценарий «{a.get('scenario')}» не входит в автоответ"
    if settings.get("auto_skip_media") and item.get("has_media"):
        return "manager", "Отзыв с фото/видео — по настройке отвечает менеджер"
    if auto_sent_today >= int(settings.get("auto_daily_limit", 100)):
        return "manager", "Достигнут дневной лимит автоответов"
    problems = check_answer(a.get("draft", ""), settings.get("stop_words", []))
    if problems:
        return "manager", "Черновик не прошёл проверку: " + "; ".join(problems)
    return "scheduled", "Подходит под правила автоответа"

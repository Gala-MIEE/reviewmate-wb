"""Свободная работа с нейросетью: переписать черновик, три варианта ответа, вопрос к данным, выводы по отчёту.

Цифры в вопросах к данным и выводах берутся из report.collect() — нейросеть их не пересчитывает.
Имена покупателей в эти запросы не передаются.
"""
import hashlib
import json

from . import analyzer, config, db

STYLE = {
    "short": "Сделай ответ короче: 1–2 предложения, смысл сохрани.",
    "formal": "Сделай ответ официальнее и сдержаннее, без восклицательных знаков, смысл сохрани.",
    "warm": "Сделай ответ теплее и душевнее, но без сюсюканья и эмодзи, смысл сохрани.",
}

REWRITE_SYSTEM = analyzer.SYSTEM.split("РАЗМЕТКА")[0] + """
Сейчас нужно только переписать черновик ответа по просьбе менеджера. Сохрани все факты и все поля {уточнить: …}; новых фактов не добавляй.
Подпись бренда не пиши. Верни только JSON: {"variants": ["текст", ...]}"""

ASK_SYSTEM = """Ты аналитик отзывов бренда Paperform (гирлянды, пиньяты, конфетти) на Wildberries, Ozon и Яндекс Маркете.
Менеджер задаёт вопрос. Отвечай только по данным ниже: сводка посчитана программой и точна, отзывы — выборка самых значимых.
Правила:
- цифры бери из сводки, не пересчитывай и не придумывай новых;
- если в данных нет ответа, так и скажи и подскажи, какой фильтр выбрать;
- примеры из отзывов пересказывай коротко, без имён покупателей;
- отвечай по-русски, коротко и по делу: 3–8 предложений или короткий список; без markdown-заголовков."""

SUMMARY_SYSTEM = """Ты аналитик отзывов бренда Paperform (гирлянды, пиньяты, конфетти) на маркетплейсах.
По сводке, посчитанной программой, напиши выводы для руководителя.
Правила: цифры бери только из сводки; не придумывай причин, которых не видно из тем и примеров; простым языком.
Верни только JSON: {"conclusion":"2–3 предложения: главное за период","recommendations":[{"action":"что сделать","owner":"кто отвечает: производство, упаковка/склад, контент карточек, маркетинг, менеджер по отзывам"}]}
Рекомендаций 3, сначала самая важная."""


def enabled() -> bool:
    return config.LLM_ENABLED


def _item_context(item: dict) -> str:
    return (f"Товар: {item.get('product_name') or '—'}. Площадка: {item.get('marketplace') or 'wb'}. "
            f"Оценка: {item.get('rating') or '—'}/5. Имя покупателя: {item.get('user_name') or 'не указано'}.\n"
            f"Отзыв: {analyzer.combined_text(item)}")


def _strip_signature(draft: str, signature: str) -> str:
    text = (draft or "").strip()
    if signature and text.endswith(signature):
        text = text[: -len(signature)].rstrip()
    return text


def rewrite(item: dict, draft: str, mode: str, signature: str = "") -> str:
    """Один переписанный вариант: mode — short, formal, warm."""
    out = _variants(item, draft, STYLE[mode], 1, signature)
    return out[0]


def variants(item: dict, draft: str, signature: str = "") -> list[str]:
    """Три разных варианта ответа: короткий, стандартный, развёрнутый."""
    return _variants(item, draft, "Дай три разных варианта: короткий (1–2 предложения), стандартный и развёрнутый (до 5 предложений).", 3, signature)


def _variants(item: dict, draft: str, task: str, n: int, signature: str) -> list[str]:
    body = _strip_signature(draft, signature)
    user = f"{_item_context(item)}\n\nТекущий черновик:\n{body}\n\nЗадача: {task}"
    raw = analyzer._llm([{"role": "system", "content": REWRITE_SYSTEM}, {"role": "user", "content": user}])
    parsed = analyzer._parse_json(raw)
    texts = [str(t).strip() for t in (parsed.get("variants") or []) if str(t).strip()]
    if not texts:
        raise ValueError("Нейросеть не вернула вариантов")
    return [t + ("\n\n" + signature if signature else "") for t in texts[:n]]


def ask(question: str, rep: dict, rows: list[dict]) -> str:
    data = {"сводка": rep, "отзывы": rows}
    user = f"Вопрос менеджера: {question.strip()}\n\nДанные:\n{json.dumps(data, ensure_ascii=False, default=str)}"
    return analyzer._llm([{"role": "system", "content": ASK_SYSTEM}, {"role": "user", "content": user}]).strip()


def _signature(rep: dict) -> str:
    return hashlib.sha1(json.dumps(rep, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def cached_summary(rep: dict) -> dict | None:
    saved = (db.get_settings().get("report_summaries") or {}).get(str(rep["days"]))
    return saved if saved and saved.get("signature") == _signature(rep) else None


def summarize(rep: dict, rows: list[dict]) -> dict:
    """Выводы и три рекомендации. Кэш по периоду: пока сводка та же, повторный запрос не уходит в нейросеть."""
    hit = cached_summary(rep)
    if hit:
        return hit
    user = json.dumps({"сводка": rep, "примеры_отзывов": rows[:25]}, ensure_ascii=False, default=str)
    parsed = analyzer._parse_json(analyzer._llm([{"role": "system", "content": SUMMARY_SYSTEM}, {"role": "user", "content": user}]))
    conclusion = str(parsed.get("conclusion") or "").strip()
    recs = [{"action": str(r.get("action") or "").strip(), "owner": str(r.get("owner") or "").strip()}
            for r in (parsed.get("recommendations") or []) if isinstance(r, dict) and r.get("action")]
    if not conclusion:
        raise ValueError("Нейросеть не вернула выводов")
    result = {"signature": _signature(rep), "conclusion": conclusion, "recommendations": recs[:3], "at": db.now_iso()}
    saved = db.get_settings().get("report_summaries") or {}
    saved[str(rep["days"])] = result
    db.set_setting("report_summaries", saved)
    return result

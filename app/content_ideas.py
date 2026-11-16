"""Темы для соцсетей: нейросеть группирует вопросы, пожелания и замечания."""
import hashlib
import json
import threading

from . import analyzer, config, db

_lock = threading.Lock()
_running: set[str] = set()
_failed_at: dict[str, float] = {}

PROMPT = """Ты редактор соцсетей бренда Paperform: гирлянды, пиньяты, конфетти и праздничный декор.
По сигналам покупателей предложи темы для постов и сторис. Похожие сигналы схлопни в одну тему.
Не выдумывай запросы, которых нет во входе. Не обещай скидки, подарки и характеристики, которых покупатели не называли.
Если спрашивают о безопасности, тема — спокойное разъяснение, как устроен товар и как им пользоваться с детьми, без фразы «абсолютно безопасен».
Если просят новый цвет или вариант — тема про анонс или опрос этого варианта.
Если спрашивают, что входит в комплект — тема с подробным разбором комплектации.
Верни только JSON:
{"ideas":[{"title":"короткий заголовок темы","angle":"что именно сказать в посте","basis":"вопрос, пожелание или замечание","signals":"сколько сигналов и о чём, одной фразой","products":"товары через запятую"}]}
От 4 до 8 идей. Сначала те, что встречаются чаще."""


def sources(rows: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Те же группы, что кнопки «Вопросы», «Пожелания» и «Замечание в 5★»."""
    questions = [r for r in rows if r.get("has_question") or r.get("kind") == "question" or r.get("scenario") == "Вопрос"]
    wishes = [r for r in rows if r.get("wish") or r.get("scenario") == "Предложение"]
    notes = [r for r in rows if r.get("kind") == "feedback" and r.get("rating") == 5
             and (r.get("wish") or r.get("has_question") or r.get("tone") in ("Негатив", "Смешанная"))]
    return questions, wishes, notes


def refresh_all() -> dict:
    """Темы по всем отзывам с текстом. Подпись кэша совпадает с вкладкой «Все»."""
    db.init()
    text = "(length(trim(coalesce(text,'')))>0 OR length(trim(coalesce(pros,'')))>0 OR length(trim(coalesce(cons,'')))>0)"
    with db.session() as con:
        rows = [dict(r) for r in con.execute(f"SELECT * FROM items WHERE scenario IS NOT NULL AND {text}")]
    for row in rows:
        row["wish"] = bool(row.get("has_wish"))
    questions, wishes, notes = sources(rows)
    return refresh(questions, wishes, notes)


def refresh(questions: list[dict], wishes: list[dict], notes: list[dict]) -> dict:
    """Собирает темы сразу и записывает кэш. Для кнопки страницы используется фоновый for_period."""
    grouped = _groups(questions, wishes, notes)
    ideas = propose(grouped)
    saved = {"signature": _signature(grouped), "ideas": ideas, "source_n": _source_n(grouped)}
    db.set_setting("content_ideas", saved)
    return saved


def for_period(questions: list[dict], wishes: list[dict], notes: list[dict]) -> dict:
    """Кэш по составу отзывов. Если темы ещё не собраны, запускает сбор в фоне."""
    grouped = _groups(questions, wishes, notes)
    signature = _signature(grouped)
    cached = _read(signature)
    if cached:
        return cached
    if not config.LLM_ENABLED:
        return {"ideas": [], "pending": False, "source_n": _source_n(grouped), "error": "Нейросеть не подключена — темы для контента не собраны."}
    if _recently_failed(signature):
        return {"ideas": [], "pending": False, "source_n": _source_n(grouped), "error": "Не удалось собрать темы. Обновите страницу чуть позже."}
    _start(signature, grouped)
    return {"ideas": [], "pending": True, "source_n": _source_n(grouped), "error": ""}


def propose(grouped: list[dict]) -> list[dict]:
    """Один запрос к нейросети. grouped — уже свёрнутые сигналы."""
    if not grouped:
        return []
    raw = analyzer._llm([
        {"role": "system", "content": PROMPT},
        {"role": "user", "content": json.dumps({"signals": grouped}, ensure_ascii=False)},
    ], timeout=180)
    return _clean(analyzer._parse_json(raw))


def _groups(questions, wishes, notes) -> list[dict]:
    found: dict[str, dict] = {}
    for row, label in ((questions, "вопрос"), (wishes, "пожелание"), (notes, "замечание")):
        for item in row:
            slot = found.setdefault(item["id"], {"row": item, "labels": []})
            if label not in slot["labels"]:
                slot["labels"].append(label)
    buckets: dict[tuple, dict] = {}
    for slot in found.values():
        row = slot["row"]
        key = (row.get("theme") or "Прочее", tuple(slot["labels"]))
        bucket = buckets.setdefault(key, {"n": 0, "products": {}, "examples": []})
        bucket["n"] += 1
        name = (row.get("product_name") or "").strip()
        if name:
            bucket["products"][name] = bucket["products"].get(name, 0) + 1
        text = analyzer.combined_text(row)
        if text and len(bucket["examples"]) < 2:
            bucket["examples"].append(analyzer._clip(text, 180))
    grouped = []
    for (theme, labels), bucket in buckets.items():
        products = sorted(bucket["products"], key=bucket["products"].get, reverse=True)[:3]
        grouped.append({
            "basis": ", ".join(labels),
            "theme": theme,
            "n": bucket["n"],
            "products": products,
            "examples": bucket["examples"],
        })
    grouped.sort(key=lambda item: -item["n"])
    return grouped[:40]


def _signature(grouped: list[dict]) -> str:
    raw = json.dumps(grouped, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _source_n(grouped: list[dict]) -> int:
    return sum(item["n"] for item in grouped)


def _read(signature: str) -> dict | None:
    saved = db.get_settings().get("content_ideas") or {}
    if saved.get("signature") != signature or not saved.get("ideas"):
        return None
    return {"ideas": saved["ideas"], "pending": False, "source_n": saved.get("source_n") or 0, "error": ""}


def _recently_failed(signature: str) -> bool:
    import time
    return time.time() - _failed_at.get(signature, 0) < 120


def _start(signature: str, grouped: list[dict]) -> None:
    with _lock:
        if signature in _running:
            return
        _running.add(signature)

    def run():
        import time
        try:
            ideas = propose(grouped)
            if not ideas:
                raise RuntimeError("нейросеть не вернула темы")
            db.set_setting("content_ideas", {
                "signature": signature, "ideas": ideas, "source_n": _source_n(grouped),
            })
        except Exception:
            _failed_at[signature] = time.time()
        finally:
            with _lock:
                _running.discard(signature)

    threading.Thread(target=run, daemon=True).start()


def _clean(raw) -> list[dict]:
    rows = raw.get("ideas") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return []
    ideas = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        angle = str(row.get("angle") or "").strip()
        if not title or not angle:
            continue
        ideas.append({
            "title": title[:140],
            "angle": angle[:600],
            "basis": str(row.get("basis") or "").strip()[:80],
            "signals": str(row.get("signals") or "").strip()[:220],
            "products": str(row.get("products") or "").strip()[:220],
        })
    return ideas[:8]

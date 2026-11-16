"""Анализ отзыва/вопроса и подготовка черновика.

1) Если подключена нейросеть — классификация и черновик от LLM (с базой шаблонов и описанием товара).
2) Если нет или LLM ответила с ошибкой — классификация по ключевым словам и шаблон из базы знаний.
Цифры аналитики потом считаются кодом по сохранённой разметке.
"""
import csv
import json
import os
import re
import time

import requests

from . import config

KB_PATH = os.path.join(os.path.dirname(__file__), "..", "kb", "templates.csv")

SCENARIOS = ["Жалоба", "Благодарность", "Предложение", "Вопрос", "Смешанный", "Токсичный/спам"]
THEMES = ["Качество товара", "Брак и повреждения", "Соответствие описанию", "Комплектация", "Упаковка", "Доставка",
          "Цена", "Подарок", "Ассортимент", "Безопасность", "Прочее"]
TONES = ["Позитив", "Нейтрально", "Смешанная", "Негатив"]
PRIORITIES = ["Критичный", "Высокий", "Средний", "Низкий"]


def load_templates() -> list[dict]:
    with open(KB_PATH, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


FAQ_PATH = os.path.join(os.path.dirname(__file__), "..", "kb", "faq.csv")


def load_faq(path: str = FAQ_PATH) -> list[dict]:
    """Справочник фактов о товарах. Колонки: товар, вопрос, ключевые_слова, ответ.
    «товар» — часть названия («Пиньята», «Гирлянда») или «Все». Неизвестный факт пишется как {уточнить: …}."""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f) if (r.get("ответ") or "").strip()]


def faq_for(product_name: str) -> list[dict]:
    name = (product_name or "").lower()
    out = []
    for row in load_faq():
        key = (row.get("товар") or "").strip().lower()
        if key in ("", "все") or key in name:
            out.append(row)
    return out


def faq_answer(text: str, product_name: str) -> str:
    """Ответ из справочника для режима без нейросети: строка с наибольшим числом совпавших ключевых слов."""
    t = (text or "").lower()
    best, score = None, 0
    for row in faq_for(product_name):
        keys = [k.strip().lower() for k in (row.get("ключевые_слова") or "").split(";") if k.strip()]
        hits = sum(1 for k in keys if re.search(r"(?<![0-9a-zа-яё])" + re.escape(k), t))
        if hits > score:
            best, score = row, hits
    return best["ответ"].strip() if best else ""


# ---------- правила: словарь формулировок из kb/keywords.csv ----------
KEYWORDS_PATH = os.path.join(os.path.dirname(__file__), "..", "kb", "keywords.csv")
MODES = ("основа", "целиком", "подстрока")
GROUPS = ("Негатив", "Позитив", "Не успели к празднику", "Угроза жалобы", "Спам", "Вопрос", "Пожелание")


def load_keywords(path: str = KEYWORDS_PATH) -> dict:
    """Читает словарь формулировок. Порядок строк «Тема: …» — приоритет тем: побеждает первая найденная.

    Колонки: группа, слово, поиск. Поиск: «основа» — слово начинается с этих букв («порани» → «поранил»);
    «целиком» — только отдельным словом («в глаз», но не «в глаза»); «подстрока» — где угодно в тексте.
    """
    themes: dict[str, list[str]] = {}
    groups: dict[str, list[str]] = {g: [] for g in GROUPS}
    exact: set[str] = set()
    with open(path, encoding="utf-8-sig", newline="") as f:
        for n, row in enumerate(csv.DictReader(f), start=2):
            group = (row.get("группа") or "").strip()
            word = (row.get("слово") or "").strip().lower()
            mode = (row.get("поиск") or "основа").strip().lower() or "основа"
            if not group or not word:
                continue
            if mode not in MODES:
                raise ValueError(f"{path}, строка {n}: поиск «{mode}», нужно одно из: {', '.join(MODES)}")
            if mode == "целиком":
                exact.add(word)
            if group.startswith("Тема:"):
                themes.setdefault(group.split(":", 1)[1].strip(), []).append(word)
            elif group in groups:
                groups[group].append(word)
            else:
                raise ValueError(f"{path}, строка {n}: неизвестная группа «{group}»")
    return {"themes": themes, "groups": groups, "exact": exact}


_KB = load_keywords()
KW = _KB["themes"]
NEG = _KB["groups"]["Негатив"]
POS = _KB["groups"]["Позитив"]
MISSED_HOLIDAY = tuple(_KB["groups"]["Не успели к празднику"])
LEGAL_THREAT = tuple(_KB["groups"]["Угроза жалобы"])
TOXIC = _KB["groups"]["Спам"]
EXACT = _KB["exact"]
QUESTION_WORD_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in _KB["groups"]["Вопрос"]) + r")\b", re.I) \
    if _KB["groups"]["Вопрос"] else re.compile(r"(?!x)x")
WISH_RE = re.compile("|".join(re.escape(w) for w in _KB["groups"]["Пожелание"]), re.I) \
    if _KB["groups"]["Пожелание"] else re.compile(r"(?!x)x")


def _pattern(needle: str) -> re.Pattern:
    """Слово или фраза с начала слова.

    Последнее слово — основа: дальше допускаются любые буквы («порани» → «поранил», «поранилась»;
    «понрав» → «понравилось»). Начало слова обязательно, поэтому «клей» не находится в «наклейки»,
    «опасн» — в «безопасно». Слова из EXACT ищутся только целиком: «в глаз» не совпадает с «в глаза».
    Не-кириллические маркеры (t.me/, www.) ищутся как подстрока.
    """
    if not re.fullmatch(r"[0-9a-zа-яё ]+", needle):
        return re.compile(re.escape(needle))
    parts = needle.split()
    if needle in EXACT:
        body = r"\s+".join(re.escape(part) for part in parts)
        return re.compile(rf"(?<![0-9a-zа-яё]){body}(?![0-9a-zа-яё])")
    bits = [re.escape(part) for part in parts[:-1]]
    bits.append(re.escape(parts[-1]) + r"[0-9a-zа-яё]*")
    body = r"\s+".join(bits)
    return re.compile(rf"(?<![0-9a-zа-яё]){body}")


_PATTERNS = {word: _pattern(word) for word in {
    word for words in KW.values() for word in words
} | set(NEG) | set(POS) | set(TOXIC) | set(MISSED_HOLIDAY) | set(LEGAL_THREAT)}
_NEG_BEFORE = re.compile(
    r"(?:без|не|ни|боял[а-яё]*|переживал[а-яё]*)(?:[\s,]+[а-яё]+){0,4}[\s,]+$"
)
_NEG_AFTER = re.compile(r"^[\s,]*(?:нет|нету|не)\b")


def _negated(text: str, start: int, end: int) -> bool:
    before = text[max(0, start - 40):start]
    after = text[end:end + 12]
    return bool(_NEG_BEFORE.search(before) or _NEG_AFTER.search(after))


def _cnt(text, words, skip_negated: bool = True) -> int:
    found = 0
    for word in words:
        match = _PATTERNS[word].search(text)
        if not match:
            continue
        if skip_negated and _negated(text, match.start(), match.end()):
            continue
        found += 1
    return found


def combined_text(item: dict) -> str:
    return "\n".join(p.strip() for p in (item.get("text") or "", item.get("pros") or "", item.get("cons") or "") if p and p.strip())


def has_text(item: dict) -> bool:
    return bool(combined_text(item))


def has_question(text: str) -> bool:
    if not text:
        return False
    if QUESTION_WORD_RE.search(text):
        return True
    for mark in re.finditer(r"\?", text):
        start = max(text.rfind(".", 0, mark.start()), text.rfind("!", 0, mark.start()), text.rfind("\n", 0, mark.start()))
        sentence = re.sub(r"[^\wа-яё]+", "", text[start + 1:mark.end()], flags=re.I)
        if len(sentence) >= 8:
            return True
    return False


def has_wish(text: str) -> bool:
    return bool(text and WISH_RE.search(text))


def classify_rules(kind: str, text: str, rating: int | None) -> dict:
    t = " " + (text or "").lower() + " "
    has_q = has_question(text or "")
    wish = has_wish(text or "")
    theme = "Прочее"
    for name, words in KW.items():      # порядок словаря = приоритет тем
        if _cnt(t, words):
            theme = name
            break
    neg = _cnt(t, NEG) + (2 if rating and rating <= 2 else 0)
    pos = _cnt(t, POS) + (1 if rating == 5 else 0)
    if "не удоб" in t or "неудоб" in t:
        pos = max(0, pos - _cnt(t, ["удобн"]))
    if kind == "question" or (has_q and neg == 0 and (not rating or rating >= 4)):
        scenario, tone = "Вопрос", "Нейтрально"
    elif _cnt(t, TOXIC):
        scenario, tone = "Токсичный/спам", "Негатив"
    elif (wish or _cnt(t, KW["Ассортимент"])) and neg == 0 and (not rating or rating >= 4):
        scenario, tone = "Предложение", "Нейтрально"
    elif (" но " in t or ", но" in t) and pos and neg:
        scenario, tone = "Смешанный", "Смешанная"
    elif pos > neg and neg == 0:
        scenario, tone = "Благодарность", "Позитив"
    elif rating == 3 and neg == 0:
        scenario, tone = "Смешанный", "Смешанная"
    else:
        scenario, tone = "Жалоба", "Негатив"
    if rating and rating <= 2 and scenario == "Благодарность":
        scenario, tone = "Жалоба", "Негатив"
    elif rating and rating < 5 and scenario == "Благодарность":
        scenario, tone = "Смешанный", "Смешанная"
    missed_holiday = scenario == "Жалоба" and _cnt(t, MISSED_HOLIDAY, skip_negated=False)
    legal = _cnt(t, LEGAL_THREAT, skip_negated=False)
    if legal or (theme == "Безопасность" and scenario in ("Жалоба", "Смешанный")):
        prio = "Критичный"
    elif scenario == "Жалоба" and (theme in ("Брак и повреждения", "Комплектация") or missed_holiday):
        prio = "Высокий"
    elif scenario in ("Жалоба", "Смешанный", "Токсичный/спам") or kind == "question" or has_q:
        prio = "Средний"
    else:
        prio = "Низкий"
    return {"scenario": scenario, "theme": theme, "tone": tone, "priority": prio,
            "has_question": has_q or kind == "question", "has_wish": wish, "summary": ""}


PLATFORM_NAME = {"wb": "Wildberries", "ozon": "Ozon", "ym": "Яндекс Маркет"}
FALLBACK_TEMPLATE = {
    "Жалоба": "{Имя}, здравствуйте! Нам жаль, что покупка вас разочаровала. Напишите подробности в личном кабинете {Площадка} — разберёмся.",
    "Благодарность": "{Имя}, спасибо за отзыв! Рады, что вам понравилось.",
    "Предложение": "{Имя}, спасибо за идею! Передали её команде.",
    "Вопрос": "{Имя}, здравствуйте! {Ответ_на_вопрос}",
    "Смешанный": "{Имя}, спасибо за подробный отзыв! {Плюс} {Минус}",
    "Токсичный/спам": "",
}


def _mixed_clauses(plus: str, minus: str) -> tuple[str, str]:
    """Название поля не становится подлежащим: на Ozon достоинства и недостатки часто пустые."""
    liked = f"Рады, что вы отметили: {plus}." if plus else "Рады, что в покупке есть то, что вам понравилось."
    noted = f"Замечание «{minus}» передали команде — будем улучшать." if minus else "Замечание передали команде — будем улучшать."
    return liked, noted


def draft_from_template(c: dict, item: dict, signature: str) -> str:
    sc = c["scenario"]
    if sc == "Токсичный/спам":
        return ""
    tpls = load_templates()
    th = "Подарок" if c["theme"] == "Подарок" else ("Качество товара" if c["theme"] == "Брак и повреждения" else c["theme"])
    picked = next((x for x in tpls if x["сценарий"] == sc and x["тема"] == th), None) \
        or next((x for x in tpls if x["сценарий"] == sc and x["тема"] == "Общее"), None)
    s = picked["шаблон"] if picked else FALLBACK_TEMPLATE.get(sc, FALLBACK_TEMPLATE["Жалоба"])
    name = (item.get("user_name") or "").strip()
    product = item.get("product_name") or "товар"
    product = re.sub(r"^Демо:\s*", "", product)
    plus = (item.get("pros") or "").strip()
    minus = (item.get("cons") or "").strip()
    liked, noted = _mixed_clauses(plus, minus)
    market = PLATFORM_NAME.get(item.get("marketplace") or "wb", "площадке")
    s = (s.replace("{Товар}", product).replace("{Плюс}", liked).replace("{Минус}", noted)
         .replace("{Площадка}", market).replace("{Ответ_на_вопрос}", "{впишите ответ на вопрос}"))
    s = re.sub(r"\s{2,}", " ", s).strip()
    if name:
        s = s.replace("{Имя}", name)
    else:
        s = re.sub(r"^\{Имя\},\s*(\w)", lambda m: m.group(1).upper(), s)
    return s + ("\n\n" + signature if signature else "")


# ---------- нейросеть ----------
SYSTEM = """Ты — ReviewMate, ассистент бренда Paperform на маркетплейсах Wildberries, Ozon и Яндекс Маркет.
Paperform делает товары для праздника: гирлянды-растяжки («С днём рождения», «С Новым годом», «Я тебя люблю», «Звёзды», «Сердечки»), пиньяты (в комплекте маска и бита), мини-пиньяты и конфетти.
Их покупают к конкретной дате: день рождения ребёнка, юбилей, Новый год. Сорванный праздник для покупателя обиднее обычной неудачной покупки.

Твоя задача: разобрать отзыв или вопрос покупателя и написать черновик публичного ответа от имени бренда. Публикует ответ менеджер, ты готовишь черновик.

СТИЛЬ
- Тепло и по-человечески, на «вы», по имени, если оно есть. Без канцелярита («благодарим за обратную связь», «приносим извинения за доставленные неудобства») и без эмодзи.
- 2–4 предложения. Не больше одной фразы извинения.
- Отвечай на то, что написал покупатель: упомяни конкретную деталь (юбилей мамы, оторванное ухо пиньяты, мелкие буквы), а не общие слова.
- Не начинай с «Спасибо за отзыв!» в каждом ответе, если можно начать с сути.

СЦЕНАРИИ
- Благодарность: поблагодари, отметь деталь из отзыва, пожелай новых праздников. Без рекламы других товаров.
- Жалоба (брак, повреждение, не тот товар, нет биты, не хватает букв): признай проблему без оправданий; предложи возврат или обмен через личный кабинет площадки; скажи, что информация передана (контроль качества, склад, упаковка).
- Не успели к празднику: признай, что это обидно; сроки доставки устанавливает площадка, не обещай их изменить.
- Смешанный отзыв: коротко поблагодари за плюсы и отдельно ответь на замечание.
- Предложение (новый цвет, надпись, размер): поблагодари, скажи, что передал команде ассортимента; не обещай, что товар появится.
- Вопрос: ответь по фактам из справочника и описания товара. Если факта нет, оставь {уточнить: что именно}; не угадывай.
- Критичный случай (травма, аллергия, «попало в глаз», угроза суда или Роспотребнадзора): коротко и спокойно, без признания вины, без медицинских советов и обещаний; попроси описать ситуацию в личном кабинете площадки.
- Токсичный отзыв или спам: draft — пустая строка.

ЗАПРЕЩЕНО
- ссылки, телефоны, почта, мессенджеры, приглашения связаться вне площадки;
- обещания компенсаций, скидок, промокодов, подарков;
- характеристики, которых нет в справочнике и описании товара;
- спорить с покупателем, обвинять площадку, курьеров или покупателя.

ПРИМЕРЫ ХОРОШИХ ОТВЕТОВ
Отзыв 5★: «Повесили на юбилей мамы, все гости фотографировались».
Ответ: «Как здорово, что гирлянда украсила юбилей вашей мамы и попала на фото гостей! Пусть праздников будет больше.»
Отзыв 1★: «Пиньята пришла раздавленная, ухо оторвано. Дарить такое нельзя».
Ответ: «Очень жаль, что пиньята пришла в таком виде, особенно перед праздником. Оформите, пожалуйста, возврат в личном кабинете Wildberries. Мы передали информацию, чтобы усилить упаковку.»
Отзыв 4★: «Красивая гирлянда, но буквы мелковаты».
Ответ: «Рады, что гирлянда понравилась! Замечание про размер букв передали команде, учтём его в следующих партиях.»
Вопрос: «Что входит в комплект пиньяты?»
Ответ: «Здравствуйте! В комплект входят маска и бита. Наполнитель в комплект не входит.»

РАЗМЕТКА
scenario: Жалоба|Благодарность|Предложение|Вопрос|Смешанный|Токсичный/спам
theme: Качество товара|Брак и повреждения|Соответствие описанию|Комплектация|Упаковка|Доставка|Цена|Подарок|Ассортимент|Безопасность|Прочее
tone: Позитив|Нейтрально|Смешанная|Негатив — по смыслу текста, а не по числу звёзд. Жалоба в поле «достоинства» — негатив.
priority: Критичный (здоровье, безопасность, угрозы суда/СМИ)|Высокий (брак, не тот товар, нет биты, не успели к празднику)|Средний (прочие жалобы, вопросы)|Низкий (благодарности, предложения)
has_question: true, если покупатель задаёт продавцу вопрос и ждёт ответа (даже при оценке 5).
has_wish: true, если просит что-то изменить, добавить или улучшить.
Верни только JSON: {"scenario":"","theme":"","tone":"","priority":"","has_question":false,"has_wish":false,"summary":"суть в одном предложении","draft":"текст ответа без подписи"}"""


def _llm(messages: list[dict], timeout: int = 120) -> str:
    last = "нейросеть не ответила"
    for attempt in range(4):
        try:
            r = requests.post(f"{config.LLM_BASE_URL}/chat/completions",
                              headers={"Authorization": f"Bearer {config.LLM_API_KEY}"},
                              json={"model": config.LLM_MODEL, "messages": messages},
                              timeout=timeout)
        except requests.RequestException as e:
            last = str(e)[:200]
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 429:
            last = "429"
            time.sleep(min(float(r.headers.get("retry-after") or 15), 60))
            continue
        if r.status_code >= 500:
            last = f"{r.status_code}"
            time.sleep(2 ** attempt)
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code}: {r.text[:300]}")
        return r.json()["choices"][0]["message"]["content"]
    raise RuntimeError(last)


def _parse_json(s: str) -> dict:
    m = re.search(r"\{.*\}", s, re.S)
    return json.loads(m.group(0) if m else s)


def classify_llm(item: dict, product: dict | None, extra: str = "") -> dict:
    tpls = "\n".join(f"- [{t['сценарий']} / {t['тема']}] {t['шаблон']}" for t in load_templates())
    kind = "Вопрос покупателя" if item["kind"] == "question" else "Отзыв"
    facts = "\n".join(f"- {r['вопрос']} → {r['ответ']}" for r in faq_for(item.get("product_name") or "")) or "нет"
    user = f"""Справочник фактов о товаре (поля {{уточнить: …}} — неизвестные факты, их не выдумывай):
{facts}

База шаблонов (образец тона, адаптируй под ситуацию):
{tpls}

Товар: {item.get('product_name') or '—'} (артикул {item.get('supplier_article') or '—'})
Описание товара: {(product or {}).get('description', '')[:1500] or 'нет'}

{kind}. Площадка: {item.get('marketplace') or 'wb'}. Оценка: {item.get('rating') or '—'}/5. Имя: {item.get('user_name') or 'не указано'}.
Текст: {item.get('text') or ''}
Достоинства: {item.get('pros') or ''}
Недостатки: {item.get('cons') or ''}
{extra}"""
    out = _parse_json(_llm([{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]))
    if out.get("scenario") not in SCENARIOS or out.get("priority") not in PRIORITIES:
        raise ValueError(f"LLM вернула значения вне справочника: {out}")
    out["has_question"] = bool(out.get("has_question")) or item["kind"] == "question"
    out["has_wish"] = bool(out.get("has_wish"))
    if out.get("tone") not in TONES:
        raise ValueError(f"LLM вернула тональность вне справочника: {out.get('tone')}")
    return out


BATCH_SYSTEM = """Ты размечаешь отзывы покупателей бренда Paperform на маркетплейсе (Wildberries, Ozon или Яндекс Маркет).
Смотри на смысл текста, а не на число звёзд. Жалоба, написанная в поле «достоинства», остаётся негативом.
Оценка 4–5 может содержать претензию, вопрос или пожелание — тогда это не чистая благодарность.
Справочники:
scenario: Жалоба|Благодарность|Предложение|Вопрос|Смешанный|Токсичный/спам
theme: Качество товара|Брак и повреждения|Соответствие описанию|Комплектация|Упаковка|Доставка|Цена|Подарок|Ассортимент|Безопасность|Прочее
tone: Позитив|Нейтрально|Смешанная|Негатив
priority: Критичный|Высокий|Средний|Низкий
has_question: true, если покупатель задаёт вопрос продавцу и ждёт ответа.
has_wish: true, если просит что-то изменить, добавить или улучшить.
summary: одна короткая фраза по-русски, без оценки звёздами.
Верни только JSON вида {"items":[{"id":"","scenario":"","theme":"","tone":"","priority":"","has_question":false,"has_wish":false,"summary":""}]}.
Число объектов items должно совпасть с числом отзывов. id копируй без изменений."""


def _clip(value, limit: int) -> str:
    text = (value or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def classify_batch(items: list[dict]) -> dict[str, dict]:
    """Смысловая разметка пачки отзывов. Ключ — id отзыва."""
    payload = [{
        "id": it["id"],
        "marketplace": it.get("marketplace") or "wb",
        "rating": it.get("rating"),
        "text": _clip(it.get("text"), 700),
        "pros": _clip(it.get("pros"), 400),
        "cons": _clip(it.get("cons"), 400),
    } for it in items]
    raw = _llm([
        {"role": "system", "content": BATCH_SYSTEM},
        {"role": "user", "content": json.dumps({"reviews": payload}, ensure_ascii=False)},
    ], timeout=180)
    parsed = _parse_json(raw)
    rows = parsed.get("items") if isinstance(parsed, dict) else parsed
    if not isinstance(rows, list):
        raise ValueError("Нейросеть вернула не список разметок")
    wanted = {it["id"] for it in items}
    out = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("id") not in wanted:
            continue
        if row.get("scenario") not in SCENARIOS or row.get("theme") not in THEMES:
            continue
        if row.get("tone") not in TONES or row.get("priority") not in PRIORITIES:
            continue
        out[row["id"]] = {
            "scenario": row["scenario"],
            "theme": row["theme"],
            "tone": row["tone"],
            "priority": row["priority"],
            "has_question": bool(row.get("has_question")),
            "has_wish": bool(row.get("has_wish")),
            "summary": (row.get("summary") or "")[:400],
        }
    if len(out) < len(items):
        missing = wanted - out.keys()
        raise ValueError(f"Нейросеть пропустила отзывы: {len(missing)}")
    return out


def analyze(item: dict, product: dict | None, signature: str, extra: str = "") -> dict:
    full_text = " ".join(x for x in [item.get("text"), item.get("pros"), item.get("cons")] if x)
    if config.LLM_ENABLED:
        try:
            c = classify_llm(item, product, extra)
            draft = (c.get("draft") or "").strip()
            if signature and draft:
                draft += "\n\n" + signature
            c["draft"] = draft
            c["analyzed_by"] = "llm"
            c["has_question"] = c["has_question"] or has_question(full_text)
            c["has_wish"] = bool(c.get("has_wish")) or has_wish(full_text)
            return c
        except Exception as e:  # noqa: BLE001 — откатываемся на правила
            err = str(e)[:200]
        c = classify_rules(item["kind"], full_text, item.get("rating"))
        c["summary"] = f"Нейросеть недоступна ({err}); разбор по ключевым словам."
    else:
        c = classify_rules(item["kind"], full_text, item.get("rating"))
    c["draft"] = "" if c["scenario"] == "Токсичный/спам" else draft_from_template(c, item, signature)
    if "{впишите ответ на вопрос}" in c["draft"]:
        answer = faq_answer(full_text, item.get("product_name") or "")
        if answer:
            c["draft"] = c["draft"].replace("{впишите ответ на вопрос}", answer)
    c["analyzed_by"] = "rules"
    c["has_wish"] = has_wish(full_text)
    return c

"""Отчёт по отзывам за период: цифры считает код, нейросеть только пишет выводы поверх них.

collect()  — сводка по базе за период (по желанию — по площадке и товару);
to_text()  — тот же отчёт простым текстом (кнопка «Скопировать отчёт»);
to_xlsx()  — отчёт в Excel (кнопка «Скачать отчёт»).
"""
import io
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from . import analyzer, config, db

HAS_TEXT = "(length(trim(coalesce(text,'')))>0 OR length(trim(coalesce(pros,'')))>0 OR length(trim(coalesce(cons,'')))>0)"
MARKETS = {"wb": "Wildberries", "ozon": "Ozon", "ym": "Яндекс Маркет"}


def _rows(days: int, market: str = "", product: str = "") -> list[dict]:
    where = [f"scenario IS NOT NULL", HAS_TEXT]
    args: list = []
    if not config.DEMO_MODE:
        where.append("ifnull(is_sample,0)=0")
    if days > 0:
        where.append("created_at>=?")
        args.append((datetime.now(timezone.utc) - timedelta(days=days)).isoformat())
    if market:
        where.append("marketplace=?")
        args.append(market)
    if product:
        where.append("trim(product_name)=?")
        args.append(product.strip())
    with db.session() as con:
        return [dict(r) for r in con.execute(f"SELECT * FROM items WHERE {' AND '.join(where)}", args)]


def products(days: int = 0) -> list[str]:
    names = {(r["product_name"] or "").strip() for r in _rows(days)}
    return sorted(n for n in names if n)


def collect(days: int = 30, market: str = "", product: str = "") -> dict:
    rows = _rows(days, market, product)
    fb = [r for r in rows if r["kind"] == "feedback"]
    qs = [r for r in rows if r["kind"] == "question" or r.get("has_question")]
    rated = [r["rating"] for r in fb if r["rating"]]
    neg = [r for r in fb if r["tone"] == "Негатив"]
    prods = defaultdict(lambda: {"n": 0, "neg": 0, "sum": 0, "rn": 0, "q": 0, "themes": Counter()})
    for r in rows:
        p = prods[(r["product_name"] or "—").strip()]
        if r["kind"] == "question":
            p["q"] += 1
            continue
        p["n"] += 1
        if r.get("has_question"):
            p["q"] += 1
        if r["rating"]:
            p["sum"] += r["rating"]
            p["rn"] += 1
        if r["tone"] == "Негатив":
            p["neg"] += 1
            p["themes"][r["theme"]] += 1
    product_rows = []
    for name, p in prods.items():
        top = p["themes"].most_common(1)
        product_rows.append({
            "name": name, "reviews": p["n"], "avg": round(p["sum"] / p["rn"], 2) if p["rn"] else None,
            "negative": p["neg"], "questions": p["q"], "top_theme": top[0][0] if top else "",
        })
    product_rows.sort(key=lambda x: (-x["negative"], -x["reviews"]))
    recurring = []
    for p in product_rows:
        if p["negative"] >= 3 or (p["reviews"] >= 3 and p["negative"] / p["reviews"] >= 0.3):
            recurring.append(f"{p['name']}: {p['negative']} негативных из {p['reviews']}"
                             + (f", чаще всего «{p['top_theme']}»" if p["top_theme"] else ""))
    themes_neg = Counter(r["theme"] for r in neg).most_common()
    for theme, n in themes_neg:
        if n >= 3:
            recurring.append(f"Тема «{theme}»: {n} жалоб")
    hours = []
    for r in rows:
        if r["status"] in ("sent", "sent_auto") and r.get("answered_at"):
            hours.append((datetime.fromisoformat(r["answered_at"]) - datetime.fromisoformat(r["created_at"])).total_seconds() / 3600)
    by_market = Counter(r.get("marketplace") or "wb" for r in fb)
    return {
        "period": f"последние {days} дн." if days > 0 else "всё время",
        "days": days, "market": MARKETS.get(market, "все площадки"), "product": product or "все товары",
        "reviews": len(fb), "questions": len(qs),
        "avg": round(sum(rated) / len(rated), 2) if rated else None,
        "ratings": [(f"{i}★", sum(1 for x in rated if x == i)) for i in range(5, 0, -1)],
        "tone": Counter(r["tone"] for r in fb).most_common(),
        "neg_share": round(100 * len(neg) / len(fb)) if fb else 0,
        "themes_neg": themes_neg,
        "products": product_rows,
        "recurring": recurring,
        "critical": [{"text": analyzer.combined_text(r)[:300], "product": (r["product_name"] or "").strip(),
                      "market": MARKETS.get(r.get("marketplace") or "wb", ""), "date": (r["created_at"] or "")[:10],
                      "status": r["status"]} for r in rows if r["priority"] == "Критичный"],
        "by_market": [(MARKETS.get(k, k), v) for k, v in by_market.most_common()],
        "answered_auto": sum(1 for r in rows if r["status"] == "sent_auto"),
        "answered_manager": sum(1 for r in rows if r["status"] == "sent"),
        "response_hours": round(sum(hours) / len(hours), 1) if hours else None,
    }


def samples(days: int, market: str = "", product: str = "", limit: int = 60) -> list[dict]:
    """Отзывы для вольного вопроса: сначала критичные, негатив, вопросы и пожелания, затем свежие. Без имён покупателей."""
    rows = _rows(days, market, product)
    rank = {"Критичный": 0, "Высокий": 1, "Средний": 2, "Низкий": 3}

    def key(r):
        signal = 0 if (r["tone"] in ("Негатив", "Смешанная") or r.get("has_question") or r.get("has_wish")) else 1
        return (signal, rank.get(r["priority"], 4), -(datetime.fromisoformat(r["created_at"]).timestamp()))
    out = []
    for r in sorted(rows, key=key)[:limit]:
        out.append({"date": (r["created_at"] or "")[:10], "market": MARKETS.get(r.get("marketplace") or "wb", ""),
                    "product": (r["product_name"] or "").strip(), "rating": r["rating"], "kind": r["kind"],
                    "scenario": r["scenario"], "theme": r["theme"], "tone": r["tone"],
                    "text": analyzer.combined_text(r)[:350]})
    return out


def _fmt(v) -> str:
    return "—" if v is None else str(v).replace(".", ",")


def to_text(rep: dict, conclusions: dict | None = None) -> str:
    lines = [f"Отчёт по отзывам Paperform — {rep['period']}, {rep['market']}, {rep['product']}",
             f"Отзывов: {rep['reviews']}; средняя оценка: {_fmt(rep['avg'])}; негатив: {rep['neg_share']}%; "
             f"вопросов: {rep['questions']}; критичных: {len(rep['critical'])}"]
    if rep["by_market"]:
        lines.append("По площадкам: " + ", ".join(f"{k} {v}" for k, v in rep["by_market"]))
    lines.append("Оценки: " + ", ".join(f"{k} {v}" for k, v in rep["ratings"]))
    lines.append("Тональность: " + ", ".join(f"{k} {v}" for k, v in rep["tone"]))
    if rep["themes_neg"]:
        lines.append("Темы негатива: " + ", ".join(f"{k} {v}" for k, v in rep["themes_neg"]))
    if rep["recurring"]:
        lines.append("Повторяющиеся проблемы:")
        lines += [f"- {x}" for x in rep["recurring"]]
    top = [p for p in rep["products"] if p["negative"]][:5]
    if top:
        lines.append("Товары с негативом:")
        lines += [f"- {p['name']}: отзывов {p['reviews']}, средняя {_fmt(p['avg'])}, негатив {p['negative']}" for p in top]
    if rep["critical"]:
        lines.append("Критичные отзывы:")
        lines += [f"- {c['date']}, {c['product']}: {c['text'][:150]}" for c in rep["critical"]]
    if conclusions and conclusions.get("conclusion"):
        lines.append("")
        lines.append("Выводы: " + conclusions["conclusion"])
        for i, r in enumerate(conclusions.get("recommendations") or [], 1):
            lines.append(f"{i}. {r.get('action', '')} — {r.get('owner', '')}")
    return "\n".join(lines)


def to_xlsx(rep: dict, conclusions: dict | None = None) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    head = PatternFill("solid", fgColor="7B2D8E")

    def sheet(title, header, rows, widths):
        ws = wb.create_sheet(title)
        ws.append(header)
        for c in ws[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = head
        for r in rows:
            ws.append(list(r))
        for i, w in enumerate(widths):
            ws.column_dimensions[chr(65 + i)].width = w
        for row in ws.iter_rows(min_row=2):
            for c in row:
                c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.freeze_panes = "A2"
        return ws

    wb.remove(wb.active)
    summary = [("Период", rep["period"]), ("Площадки", rep["market"]), ("Товары", rep["product"]),
               ("Отзывов", rep["reviews"]), ("Средняя оценка", rep["avg"]), ("Доля негатива, %", rep["neg_share"]),
               ("Вопросов", rep["questions"]), ("Критичных", len(rep["critical"])),
               ("Ответов автоматически", rep["answered_auto"]), ("Ответов менеджером", rep["answered_manager"]),
               ("Среднее время ответа, ч", rep["response_hours"])]
    summary += [(f"Оценка {k}", v) for k, v in rep["ratings"]]
    summary += [(f"Тональность: {k}", v) for k, v in rep["tone"]]
    summary += [(f"Площадка: {k}", v) for k, v in rep["by_market"]]
    sheet("Сводка", ["Показатель", "Значение"], summary, [32, 40])
    if conclusions and conclusions.get("conclusion"):
        rows = [("Вывод", conclusions["conclusion"], "")]
        rows += [(f"Рекомендация {i}", r.get("action", ""), r.get("owner", ""))
                 for i, r in enumerate(conclusions.get("recommendations") or [], 1)]
        sheet("Выводы", ["", "Текст", "Ответственный"], rows, [18, 80, 28])
    sheet("Товары", ["Товар", "Отзывов", "Средняя оценка", "Негатив", "Вопросов", "Главная тема негатива"],
          [(p["name"], p["reviews"], p["avg"], p["negative"], p["questions"], p["top_theme"]) for p in rep["products"]],
          [50, 10, 15, 10, 10, 26])
    sheet("Темы негатива", ["Тема", "Отзывов"], rep["themes_neg"], [32, 12])
    sheet("Повторяющиеся проблемы", ["Проблема"], [(x,) for x in rep["recurring"]], [100])
    sheet("Критичные", ["Дата", "Площадка", "Товар", "Текст", "Статус"],
          [(c["date"], c["market"], c["product"], c["text"], c["status"]) for c in rep["critical"]], [12, 16, 40, 70, 16])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()

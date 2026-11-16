"""Веб-интерфейс менеджера. Запуск: python web.py  (или gunicorn -w 2 -b 0.0.0.0:8000 web:app)."""
import functools
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from flask import Flask, Response, abort, flash, redirect, render_template, request, url_for

from app import analyzer, assistant, config, db, pipeline, report, sample_set, content_ideas

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
db.init()
sample_set.load_sample_set()

STATUS_RU = {"new": "Новый", "manager": "Ждёт менеджера", "scheduled": "Автоответ запланирован", "sent": "Отвечено менеджером",
             "sent_auto": "Отвечено автоматически", "rejected": "Вопрос отклонён", "skipped": "Без ответа",
             "answered_in_wb": "Отвечено в кабинете WB", "answered_upstream": "Отвечено на площадке", "error": "Ошибка"}
TABS = {
    "manager": ("Ждут ответа", "status IN ('manager','new')"),
    "scheduled": ("Автоответ запланирован", "status='scheduled'"),
    "done": ("Обработано", "status IN ('sent','sent_auto','rejected','skipped','answered_in_wb','answered_upstream')"),
    "all": ("Все", "1=1"),
}
PRIO_ORDER = "CASE priority WHEN 'Критичный' THEN 0 WHEN 'Высокий' THEN 1 WHEN 'Средний' THEN 2 ELSE 3 END"
HAS_TEXT = "(length(trim(coalesce(text,'')))>0 OR length(trim(coalesce(pros,'')))>0 OR length(trim(coalesce(cons,'')))>0)"
LIVE = "" if config.DEMO_MODE else " AND ifnull(is_sample,0)=0"
def _form_int(form, name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(form.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


FOCUS = (
    ("crit", "Критичные"),
    ("low", "Не 5★"),
    ("neg", "Негатив"),
    ("question", "Вопросы"),
    ("wish", "Пожелания"),
    ("note5", "Замечание в 5★"),
    ("ideas", "Идеи для контента"),
)


def _platform_cards(rows: list[dict]) -> list[dict]:
    grouped = {p["code"]: [] for p in config.PLATFORMS}
    for r in rows:
        grouped.setdefault(r.get("marketplace") or "wb", []).append(r)
    enabled = {"wb": True, "ozon": config.OZON_ENABLED, "ym": config.YM_ENABLED}
    cards = []
    for p in config.PLATFORMS:
        chunk = grouped.get(p["code"], [])
        feedbacks = [r for r in chunk if r["kind"] == "feedback"]
        rated = [r["rating"] for r in feedbacks if r["rating"]]
        neg = [r for r in feedbacks if r["tone"] == "Негатив"]
        if p["code"] == "wb":
            state = "отзывы уже в базе"
        elif any(r.get("is_sample") for r in chunk) and not enabled[p["code"]]:
            state = "тестовый набор"
        elif enabled[p["code"]] and feedbacks:
            state = "подключено"
        elif enabled[p["code"]]:
            state = "подключено, отзывов пока нет"
        else:
            state = "не подключено"
        cards.append({
            "code": p["code"], "title": p["title"], "total": len(feedbacks),
            "avg": (sum(rated) / len(rated)) if rated else None,
            "neg_pct": round(100 * len(neg) / len(feedbacks)) if feedbacks else 0,
            "questions": sum(1 for r in chunk if r["has_question"] or r["kind"] == "question"),
            "state": state, "enabled": enabled[p["code"]],
        })
    return cards


def auth(f):
    @functools.wraps(f)
    def wrap(*a, **kw):
        if config.ADMIN_PASSWORD:
            au = request.authorization
            if not au or au.username != config.ADMIN_USER or au.password != config.ADMIN_PASSWORD:
                return Response("Нужен вход", 401, {"WWW-Authenticate": 'Basic realm="ReviewMate"'})
        return f(*a, **kw)
    return wrap


@app.template_filter("dt")
def fmt_dt(s):
    if not s:
        return "—"
    d = datetime.fromisoformat(s).astimezone(timezone(timedelta(hours=3)))
    return d.strftime("%d.%m %H:%M")


@app.context_processor
def common():
    s = db.get_settings()
    with db.session() as con:
        counts = {k: con.execute(f"SELECT COUNT(*) FROM items WHERE ({w}) AND {HAS_TEXT}{LIVE}").fetchone()[0] for k, (_, w) in TABS.items()}
        q_open = con.execute(f"SELECT COUNT(*) FROM items WHERE kind='question' AND status IN ('manager','new'){LIVE}").fetchone()[0]
        samples = {r[0]: r[1] for r in con.execute("SELECT marketplace, COUNT(*) FROM items WHERE ifnull(is_sample,0)=1 GROUP BY marketplace")}
    return {"S": s, "counts": counts, "q_open": q_open, "STATUS_RU": STATUS_RU, "DEMO": config.DEMO_MODE,
            "LLM": config.LLM_ENABLED, "TABS": TABS, "PLATFORMS": config.PLATFORMS,
            "OZON": config.OZON_ENABLED, "YM": config.YM_ENABLED,
            "OZON_KEYS": config.OZON_HAS_KEYS, "YM_KEYS": config.YM_HAS_KEYS,
            "SAMPLE_OZON": samples.get("ozon", 0), "SAMPLE_YM": samples.get("ym", 0)}


@app.route("/")
@auth
def inbox():
    tab = request.args.get("tab", "manager")
    if tab not in TABS:
        tab = "manager"
    kind = request.args.get("kind", "")
    market = request.args.get("market", "")
    if market not in {p["code"] for p in config.PLATFORMS}:
        market = ""
    where = f"({TABS[tab][1]}) AND {HAS_TEXT}{LIVE}" + (" AND kind=?" if kind in ("feedback", "question") else "")
    args: tuple = (kind,) if kind in ("feedback", "question") else ()
    if market:
        where += " AND marketplace=?"
        args = args + (market,)
    order = f"CASE kind WHEN 'question' THEN 0 ELSE 1 END, {PRIO_ORDER}, created_at" if tab == "manager" else "created_at DESC"
    with db.session() as con:
        rows = con.execute(f"SELECT * FROM items WHERE {where} ORDER BY {order} LIMIT 301", args).fetchall()
    truncated = len(rows) > 300
    return render_template("inbox.html", rows=rows[:300], tab=tab, kind=kind, market=market, truncated=truncated)


@app.route("/item/<item_id>", methods=["GET", "POST"])
@auth
def item(item_id):
    it = db.get_item(item_id)
    if not it:
        abort(404)
    if request.method == "POST":
        action = request.form.get("action")
        text = request.form.get("text", "")
        if action == "send":
            problems = pipeline.send_answer(item_id, text, "manager")
            if problems:
                flash("Не отправлено: " + "; ".join(problems), "error")
                db.update_item(item_id, draft=text)
                return redirect(url_for("item", item_id=item_id))
            names = {p["code"]: p["title"] for p in config.PLATFORMS}
            title = names.get(it["marketplace"] or "wb", "площадке")
            sample = bool(it["is_sample"]) if "is_sample" in it.keys() else False
            demo = config.DEMO_MODE and (it["marketplace"] or "wb") in ("wb", "ozon", "ym")
            if sample:
                flash(f"Ответ сохранён. Это тестовый отзыв: на {title} ничего не ушло.", "ok")
            elif demo:
                flash(f"Ответ опубликован (демо-режим: на {title} ничего не ушло)", "ok")
            else:
                flash(f"Ответ опубликован на {title}", "ok")
            return redirect(url_for("inbox"))
        if action == "save":
            db.update_item(item_id, draft=text)
            flash("Черновик сохранён", "ok")
        elif action == "regen":
            hint = request.form.get("hint", "").strip()
            s = db.get_settings()
            a = analyzer.analyze(dict(it), db.product_info(it["nm_id"]), s.get("signature", ""),
                                 extra=f"Пожелание менеджера к ответу: {hint}. Предыдущий черновик: {text}" if hint else "")
            db.update_item(item_id, draft=a.get("draft", ""))
            flash("Черновик пересоздан" + ("" if config.LLM_ENABLED else " по шаблону (нейросеть не подключена)"), "ok")
        elif action in ("short", "formal", "warm", "variants", "use_variant"):
            if action == "use_variant":
                db.update_item(item_id, draft=request.form.get("variant", ""))
                flash("Вариант подставлен в черновик. Проверьте и опубликуйте.", "ok")
                return redirect(url_for("item", item_id=item_id))
            if not assistant.enabled():
                flash("Нейросеть не подключена: переписать черновик можно только вручную.", "error")
                return redirect(url_for("item", item_id=item_id))
            signature = db.get_settings().get("signature", "")
            try:
                if action == "variants":
                    db.update_item(item_id, draft=text)
                    options = assistant.variants(dict(it), text, signature)
                    return _render_item(item_id, variants=options)
                db.update_item(item_id, draft=assistant.rewrite(dict(it), text, action, signature))
                flash("Черновик переписан", "ok")
            except Exception as e:  # noqa: BLE001 — менеджер видит причину, черновик не теряется
                db.update_item(item_id, draft=text)
                flash(f"Нейросеть не ответила, черновик сохранён без изменений: {str(e)[:200]}", "error")
        elif action == "cancel_auto":
            db.update_item(item_id, status="manager", send_after=None, route_reason="Автоответ отменён менеджером")
            flash("Автоответ отменён, отзыв в очереди менеджера", "ok")
        elif action == "skip":
            db.update_item(item_id, status="skipped", answered_at=db.now_iso(), answered_by="manager")
            db.log("skipped", item_id)
            flash("Отмечено как обработанное без ответа", "ok")
            return redirect(url_for("inbox"))
        elif action == "reject" and it["kind"] == "question":
            market = it["marketplace"] if "marketplace" in it.keys() else "wb"
            if market != "wb":
                db.update_item(item_id, status="rejected", answered_at=db.now_iso(), answered_by="manager")
                db.log("rejected", item_id)
                flash("Вопрос отмечен отклонённым. На площадку ничего не ушло.", "ok")
                return redirect(url_for("inbox"))
            try:
                pipeline.client().reject_question(item_id)
                db.update_item(item_id, status="rejected", answered_at=db.now_iso(), answered_by="manager")
                db.log("rejected", item_id)
                flash("Вопрос отклонён", "ok")
                return redirect(url_for("inbox"))
            except Exception as e:  # noqa: BLE001
                flash(f"WB вернул ошибку: {e}", "error")
        return redirect(url_for("item", item_id=item_id))
    return _render_item(item_id)


def _render_item(item_id: str, variants: list[str] | None = None):
    it = db.get_item(item_id)
    product = db.product_info(it["nm_id"])
    with db.session() as con:
        history = con.execute("SELECT * FROM log WHERE item_id=? ORDER BY id DESC LIMIT 20", (item_id,)).fetchall()
    return render_template("item.html", it=it, product=product, history=history, variants=variants or [])


@app.route("/poll", methods=["POST"])
@auth
def poll_now():
    db.set_setting("poll_requested", True)
    flash(f"Проверка площадок запущена. Новые отзывы появятся в течение {config.WORKER_TICK_SEC} секунд — обновите страницу.", "ok")
    return redirect(request.referrer or url_for("inbox"))


@app.route("/analytics")
@auth
def analytics():
    try:
        days = int(request.args.get("days", 0))
    except (TypeError, ValueError):
        days = 0
    focus = request.args.get("focus", "low")
    if focus not in {k for k, _ in FOCUS}:
        focus = "low"
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat() if days > 0 else ""
    query = f"SELECT * FROM items WHERE scenario IS NOT NULL AND {HAS_TEXT}{LIVE}" + (" AND created_at>=?" if since else "")
    with db.session() as con:
        rows = [dict(r) for r in con.execute(query, (since,) if since else ()).fetchall()]
        hidden = con.execute(f"SELECT COUNT(*) FROM items WHERE NOT {HAS_TEXT}{LIVE}" + (" AND created_at>=?" if since else ""),
                              (since,) if since else ()).fetchone()[0]
    for r in rows:
        r["body"] = analyzer.combined_text(r)
        r["wish"] = bool(r.get("has_wish"))
    fb = [r for r in rows if r["kind"] == "feedback"]
    qs = [r for r in rows if r["kind"] == "question"]
    rated = [r["rating"] for r in fb if r["rating"]]
    neg = [r for r in fb if r["tone"] == "Негатив"]
    low = [r for r in fb if (r["rating"] or 0) < 5]
    questions, wishes, notes5 = content_ideas.sources(rows)
    critical = sorted(
        (r for r in rows if r["priority"] == "Критичный"),
        key=lambda r: (r["status"] not in ("manager", "new", "scheduled"), r.get("created_at") or ""),
    )
    lists = {"crit": critical, "low": low, "neg": neg, "question": questions, "wish": wishes, "note5": notes5}
    if focus == "ideas":
        shown = []
    elif focus == "crit":
        shown = lists[focus]
    else:
        shown = sorted(lists[focus], key=lambda r: (r.get("rating") or 0, r.get("created_at") or ""))
    ideas = content_ideas.for_period(questions, wishes, notes5)
    prods = defaultdict(lambda: {"n": 0, "neg": 0, "sum": 0, "rn": 0, "q": 0, "themes": Counter()})
    for r in rows:
        p = prods[r["product_name"] or "—"]
        if r["kind"] == "question":
            p["q"] += 1
            continue
        p["n"] += 1
        if r["rating"]:
            p["sum"] += r["rating"]; p["rn"] += 1
        if r["tone"] == "Негатив":
            p["neg"] += 1; p["themes"][r["theme"]] += 1
    recurring = []
    for name, p in prods.items():
        if p["neg"] >= 3 or (p["n"] >= 3 and p["neg"] / p["n"] >= 0.3):
            top = p["themes"].most_common(1)
            recurring.append(f"{name}: {p['neg']} негативных из {p['n']}" + (f", чаще всего «{top[0][0]}»" if top else ""))
    for th, n in Counter(r["theme"] for r in neg).items():
        if n >= 3:
            recurring.append(f"Тема «{th}»: {n} жалоб")
    answered = [r for r in rows if r["status"] in ("sent", "sent_auto")]
    resp_hours = []
    for r in answered:
        if r["answered_at"]:
            resp_hours.append((datetime.fromisoformat(r["answered_at"]) - datetime.fromisoformat(r["created_at"])).total_seconds() / 3600)
    data = {
        "days": days, "total": len(fb), "questions": len(qs),
        "avg": (sum(rated) / len(rated)) if rated else None,
        "neg_share": (len(neg) / len(fb)) if fb else 0,
        "critical": critical,
        "tone": Counter(r["tone"] for r in fb).most_common(),
        "ratings": [(f"{i}★", sum(1 for x in rated if x == i)) for i in range(5, 0, -1)],
        "themes_neg": Counter(r["theme"] for r in neg).most_common(),
        "products": sorted(prods.items(), key=lambda kv: -kv[1]["neg"]),
        "recurring": recurring,
        "auto": sum(1 for r in rows if r["status"] == "sent_auto"),
        "manual": sum(1 for r in rows if r["status"] == "sent"),
        "resp_hours": (sum(resp_hours) / len(resp_hours)) if resp_hours else None,
        "focus": focus,
        "focuses": FOCUS,
        "counts": {k: (len(ideas["ideas"]) if k == "ideas" else len(lists[k])) for k, _ in FOCUS},
        "shown": shown,
        "ideas": ideas["ideas"],
        "ideas_pending": ideas["pending"],
        "ideas_error": ideas["error"],
        "ideas_source": ideas["source_n"],
        "hidden": hidden,
        "questions_n": len(questions),
        "platforms": _platform_cards(rows),
    }
    rep = report.collect(days)
    conclusions = assistant.cached_summary(rep) if assistant.enabled() else None
    return render_template("analytics.html", d=data, conclusions=conclusions,
                           report_text=report.to_text(rep, conclusions), llm=assistant.enabled())


@app.route("/analytics/summary", methods=["POST"])
@auth
def analytics_summary():
    days = _form_int(request.form, "days", 30, 0, 3650)
    if not assistant.enabled():
        flash("Нейросеть не подключена: выводы недоступны, цифры отчёта можно скачать.", "error")
        return redirect(url_for("analytics", days=days))
    try:
        assistant.summarize(report.collect(days), report.samples(days))
        flash("Выводы и рекомендации готовы", "ok")
    except Exception as e:  # noqa: BLE001
        flash(f"Нейросеть не ответила: {str(e)[:200]}", "error")
    return redirect(url_for("analytics", days=days) + "#summary")


@app.route("/analytics/report.xlsx")
@auth
def analytics_report():
    try:
        days = int(request.args.get("days", 30))
    except (TypeError, ValueError):
        days = 30
    rep = report.collect(days)
    conclusions = assistant.cached_summary(rep) if assistant.enabled() else None
    stamp = datetime.now(timezone(timedelta(hours=3))).strftime("%Y-%m-%d")
    period = f"{days}d" if days > 0 else "all"
    return Response(report.to_xlsx(rep, conclusions),
                    mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename=paperform_reviews_{period}_{stamp}.xlsx"})


ASK_EXAMPLES = (
    "Какие проблемы повторяются у пиньяты «Бомба»?",
    "Сравни площадки: где больше негатива и о чём?",
    "Что покупатели чаще всего спрашивают о гирляндах?",
    "Какие пожелания по новым товарам были за период?",
    "Почему у новогодних гирлянд оценка ниже?",
)


@app.route("/ask", methods=["GET", "POST"])
@auth
def ask():
    f = request.form if request.method == "POST" else request.args
    question = (f.get("question") or "").strip()
    days = _form_int(f, "days", 90, 0, 3650)
    market = f.get("market", "")
    if market not in {p["code"] for p in config.PLATFORMS}:
        market = ""
    product = (f.get("product") or "").strip()
    answer, error, rep = "", "", None
    if request.method == "POST" and question:
        rep = report.collect(days, market, product)
        if not assistant.enabled():
            error = "Нейросеть не подключена: задайте LLM_BASE_URL, ключ и модель в .env."
        elif not rep["reviews"] and not rep["questions"]:
            error = "За выбранный период и фильтры отзывов нет. Расширьте период или снимите фильтр."
        else:
            try:
                answer = assistant.ask(question, rep, report.samples(days, market, product))
                db.log("ask", details=question[:200])
            except Exception as e:  # noqa: BLE001
                error = f"Нейросеть не ответила: {str(e)[:200]}"
    return render_template("ask.html", question=question, days=days, market=market, product=product,
                           products=report.products(), answer=answer, error=error, rep=rep,
                           examples=ASK_EXAMPLES, llm=assistant.enabled())


@app.route("/settings", methods=["GET", "POST"])
@auth
def settings():
    if request.method == "POST":
        f = request.form
        db.set_setting("auto_reply_enabled", f.get("auto_reply_enabled") == "on")
        db.set_setting("auto_min_rating", _form_int(f, "auto_min_rating", 5, 1, 5))
        db.set_setting("auto_scenarios", f.getlist("auto_scenarios"))
        db.set_setting("auto_delay_min", _form_int(f, "auto_delay_min", 60, 0, 10080))
        db.set_setting("auto_daily_limit", _form_int(f, "auto_daily_limit", 100, 0, 10000))
        db.set_setting("auto_skip_media", f.get("auto_skip_media") == "on")
        db.set_setting("poll_interval_min", _form_int(f, "poll_interval_min", 30, 5, 1440))
        db.set_setting("signature", f.get("signature", "").strip())
        db.set_setting("stop_words", [w.strip() for w in f.get("stop_words", "").split(",") if w.strip()])
        db.log("settings_changed", details=f"auto={f.get('auto_reply_enabled') == 'on'}")
        flash("Настройки сохранены", "ok")
        return redirect(url_for("settings"))
    return render_template("settings.html", scenarios=["Благодарность", "Смешанный", "Предложение"])


@app.route("/log")
@auth
def log_page():
    with db.session() as con:
        rows = con.execute("SELECT * FROM log ORDER BY id DESC LIMIT 300").fetchall()
    return render_template("log.html", rows=rows)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)

"""Клиент API Wildberries: категория «Вопросы и отзывы» (+ «Контент» только на чтение, для описаний товаров).

Лимиты WB: для методов «Вопросы и отзывы» — не чаще ~1 запроса в секунду на аккаунт продавца,
при превышении — 429 и блокировка. Поэтому все запросы идут последовательно с паузой.
"""
import base64
import json
import time
from datetime import datetime, timezone

import requests

from . import config


class WBAuthError(Exception):
    """401/403 — токен просрочен, отозван или без нужной категории. Повторять бессмысленно."""


class WBError(Exception):
    pass


class WBClient:
    MIN_INTERVAL = 1.1  # сек между запросами

    def __init__(self, token: str = config.WB_API_TOKEN):
        self.token = token
        self._last = 0.0
        self.s = requests.Session()
        self.s.headers.update({"Authorization": token, "Content-Type": "application/json"})  # без "Bearer"

    # --- низкий уровень ---
    def _request(self, method: str, url: str, **kw):
        for attempt in range(5):
            wait = self.MIN_INTERVAL - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.s.request(method, url, timeout=30, **kw)
            except requests.RequestException as e:
                time.sleep(2 ** attempt)
                last_err = str(e)
                continue
            if r.status_code in (401, 403):
                raise WBAuthError(f"{r.status_code}: {r.text[:300]}")
            if r.status_code == 429:
                retry = float(r.headers.get("X-Ratelimit-Retry", "") or 60)
                time.sleep(min(retry, 120))
                last_err = "429 Too Many Requests"
                continue
            if r.status_code >= 500:
                time.sleep(2 ** attempt * 2)
                last_err = f"{r.status_code}"
                continue
            if r.status_code >= 400:
                raise WBError(f"{r.status_code}: {r.text[:500]}")
            if not r.content:
                return {}
            data = r.json()
            if isinstance(data, dict) and data.get("error"):
                raise WBError(data.get("errorText") or str(data))
            return data
        raise WBError(f"WB не ответил после повторов: {last_err}")

    def _fb(self, method, path, **kw):
        return self._request(method, config.WB_FEEDBACKS_URL + path, **kw)

    # --- отзывы ---
    def _batch(self, payload: dict, key: str) -> list:
        """Пустой data — ошибка, а не «отзывов нет»: иначе очередь закроется целиком."""
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise WBError("WB вернул пустой ответ вместо списка")
        if key not in data or not isinstance(data.get(key), list):
            raise WBError("WB вернул список в неожиданном виде")
        return data[key]

    def unanswered_feedbacks(self, date_from: int | None = None) -> list[dict]:
        self.feedbacks_complete = False
        out, skip, take = [], 0, 5000
        while True:
            params = {"isAnswered": "false", "take": take, "skip": skip, "order": "dateDesc"}
            if date_from:
                params["dateFrom"] = date_from
            batch = self._batch(self._fb("GET", "/api/v1/feedbacks", params=params), "feedbacks")
            out += batch
            if len(batch) < take:
                self.feedbacks_complete = True
                return out
            skip += take

    def count_unanswered_feedbacks(self) -> int:
        data = self._fb("GET", "/api/v1/feedbacks/count-unanswered").get("data") or {}
        return int(data.get("countUnanswered") or 0)

    def get_feedback(self, feedback_id: str) -> dict | None:
        data = self._fb("GET", "/api/v1/feedback", params={"id": feedback_id}).get("data")
        return data if isinstance(data, dict) and data.get("id") else None

    def answer_feedback(self, feedback_id: str, text: str):
        return self._fb("POST", "/api/v1/feedbacks/answer", json={"id": feedback_id, "text": text})

    # --- вопросы ---
    def unanswered_questions(self, date_from: int | None = None) -> list[dict]:
        self.questions_complete = False
        out, skip, take = [], 0, 10000
        while True:
            params = {"isAnswered": "false", "take": take, "skip": skip, "order": "dateDesc"}
            if date_from:
                params["dateFrom"] = date_from
            batch = self._batch(self._fb("GET", "/api/v1/questions", params=params), "questions")
            out += batch
            if len(batch) < take or skip + take >= 10000:
                self.questions_complete = len(batch) < take
                return out
            skip += take

    def count_unanswered_questions(self) -> int:
        data = self._fb("GET", "/api/v1/questions/count-unanswered").get("data") or {}
        return int(data.get("countUnanswered") or 0)

    def get_question(self, question_id: str) -> dict | None:
        data = self._fb("GET", "/api/v1/question", params={"id": question_id}).get("data")
        return data if isinstance(data, dict) and data.get("id") else None

    def answer_question(self, question_id: str, text: str):
        return self._fb("PATCH", "/api/v1/questions", json={"id": question_id, "answer": {"text": text}, "state": "wbRu"})

    def reject_question(self, question_id: str, text: str = "Вопрос отклонён"):
        return self._fb("PATCH", "/api/v1/questions", json={"id": question_id, "answer": {"text": text}, "state": "none"})

    def mark_question_viewed(self, question_id: str):
        return self._fb("PATCH", "/api/v1/questions", json={"id": question_id, "wasViewed": True})

    # --- контент (необязательно, нужен токен с категорией «Контент», чтение) ---
    def product_cards(self) -> list[dict]:
        cards, cursor = [], {"limit": 100}
        while True:
            body = {"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}}
            data = self._request("POST", config.WB_CONTENT_URL + "/content/v2/get/cards/list", json=body)
            batch = data.get("cards") or []
            cards += batch
            cur = data.get("cursor") or {}
            if len(batch) < cursor["limit"] or not cur.get("nmID"):
                return cards
            cursor = {"limit": 100, "updatedAt": cur.get("updatedAt"), "nmID": cur.get("nmID")}


def token_expiry(token: str) -> datetime | None:
    """Срок действия токена из поля exp (JWT). Токен WB действует 180 дней."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return datetime.fromtimestamp(exp, tz=timezone.utc) if exp else None
    except Exception:
        return None

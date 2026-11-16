"""Коннектор Яндекс Маркета: отзывы о товарах.

Токен с доступом «Общение с покупателями», заголовок Api-Key.
businessId — номер кабинета (GET /v2/campaigns).
Список — POST /v2/businesses/{businessId}/goods-feedback.
Ответ — POST /v2/businesses/{businessId}/goods-feedback/comments/update.
Без YM_API_KEY и YM_BUSINESS_ID площадка в опросе пропускается.
"""
import time

import requests

from . import config


class YandexMarketError(Exception):
    pass


class YandexMarketAuthError(YandexMarketError):
    """401/403 — токен не подходит или нет доступа к общению с покупателями."""


class YandexMarketClient:
    def __init__(self, api_key: str = config.YM_API_KEY, business_id: str = config.YM_BUSINESS_ID):
        if not api_key or not business_id:
            raise YandexMarketAuthError("Не заданы YM_API_KEY и YM_BUSINESS_ID")
        self.business_id = business_id
        self.s = requests.Session()
        self.s.headers.update({"Api-Key": api_key, "Content-Type": "application/json"})

    def _post(self, path: str, body: dict, params: dict | None = None) -> dict:
        last = "Яндекс Маркет не ответил"
        for attempt in range(4):
            try:
                response = self.s.post(config.YM_API_URL + path, json=body, params=params, timeout=40)
            except requests.RequestException as e:
                last = str(e)[:200]
                time.sleep(2 ** attempt)
                continue
            if response.status_code in (401, 403):
                raise YandexMarketAuthError(f"{response.status_code}: {response.text[:300]}")
            if response.status_code == 429:
                time.sleep(min(float(response.headers.get("retry-after") or 5), 30))
                last = "429"
                continue
            if response.status_code >= 500:
                time.sleep(2 ** attempt)
                last = f"{response.status_code}"
                continue
            if response.status_code >= 400:
                raise YandexMarketError(f"{response.status_code}: {response.text[:400]}")
            data = response.json() if response.content else {}
            if isinstance(data, dict) and data.get("status") not in (None, "OK"):
                raise YandexMarketError(str(data.get("errors") or data)[:400])
            return data
        raise YandexMarketError(last)

    def reviews(self, need_reaction: bool = True, limit_pages: int = 20) -> list[dict]:
        """need_reaction=True — только отзывы, на которые продавец ещё не ответил."""
        self.reviews_complete = False
        out, token = [], None
        path = f"/v2/businesses/{self.business_id}/goods-feedback"
        body = {"reactionStatus": "NEED_REACTION" if need_reaction else "ALL"}
        for _ in range(limit_pages):
            params = {"limit": 50}
            if token:
                params["pageToken"] = token
            data = self._post(path, body, params)
            result = data.get("result") or {}
            batch = result.get("feedbacks") or []
            out.extend(normalize_feedback(raw) for raw in batch)
            token = (result.get("paging") or {}).get("nextPageToken")
            if not token:
                self.reviews_complete = True
                return out
            time.sleep(0.2)
        return out

    def has_any_feedback(self) -> bool:
        """Один отзыв кабинета в любом статусе: пустой NEED_REACTION тогда правда, а не сбой."""
        path = f"/v2/businesses/{self.business_id}/goods-feedback"
        data = self._post(path, {"reactionStatus": "ALL"}, {"limit": 1})
        batch = (data.get("result") or {}).get("feedbacks") or []
        return bool(batch)

    def unanswered_feedbacks(self) -> list[dict]:
        return self.reviews(need_reaction=True)

    def answer_feedback(self, item_id: str, text: str) -> dict:
        raw_id = item_id.split(":", 1)[-1]
        try:
            feedback_id = int(raw_id)
        except ValueError:
            raise YandexMarketError(f"Нечисловой id отзыва: {raw_id}") from None
        path = f"/v2/businesses/{self.business_id}/goods-feedback/comments/update"
        return self._post(path, {"feedbackId": feedback_id, "comment": {"text": text}})


def normalize_feedback(raw: dict) -> dict:
    description = raw.get("description") or {}
    stats = raw.get("statistics") or {}
    media = raw.get("media") or {}
    identifiers = raw.get("identifiers") or {}
    offer = identifiers.get("offerId") or raw.get("offerId") or ""
    author = raw.get("author")
    if isinstance(author, dict):
        author = author.get("name") or ""
    photos = media.get("photos") or []
    videos = media.get("videos") or []
    feedback_id = raw.get("feedbackId") or raw.get("id")
    return {
        "id": f"ym:{feedback_id}",
        "kind": "feedback",
        "marketplace": "ym",
        "created_at": raw.get("createdAt") or "",
        "nm_id": None,
        "product_name": raw.get("productName") or (f"Артикул {offer}" if offer else "Товар Яндекс Маркета"),
        "supplier_article": str(offer),
        "brand": "",
        "user_name": author or "",
        "rating": stats.get("rating") if stats.get("rating") is not None else raw.get("rating"),
        "text": description.get("comment") or raw.get("text") or "",
        "pros": description.get("advantages") or "",
        "cons": description.get("disadvantages") or "",
        "has_media": int(bool(photos) or bool(videos)),
        "status": "new",
    }

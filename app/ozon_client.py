"""Коннектор Ozon Seller API: отзывы и вопросы покупателей.

Ключи: кабинет продавца → Настройки → API-ключи (Client-Id и Api-Key).
Отзывы — POST /v1/review/list, ответ — POST /v1/review/comment/create.
Вопросы — POST /v1/question/list, ответ — POST /v1/question/answer/create (2–3000 знаков).
Без OZON_CLIENT_ID и OZON_API_KEY клиент не создаётся и площадка в опросе пропускается.
"""
import time

import requests

from . import config


class OzonError(Exception):
    pass


class OzonAuthError(OzonError):
    """401/403 — неверный ключ или у кабинета нет доступа к отзывам."""


class OzonClient:
    def __init__(self, client_id: str = config.OZON_CLIENT_ID, api_key: str = config.OZON_API_KEY):
        if not client_id or not api_key:
            raise OzonAuthError("Не заданы OZON_CLIENT_ID и OZON_API_KEY")
        self.s = requests.Session()
        self.s.headers.update({"Client-Id": client_id, "Api-Key": api_key, "Content-Type": "application/json"})

    def _post(self, path: str, body: dict) -> dict:
        last = "Ozon не ответил"
        for attempt in range(4):
            try:
                response = self.s.post(config.OZON_API_URL + path, json=body, timeout=40)
            except requests.RequestException as e:
                last = str(e)[:200]
                time.sleep(2 ** attempt)
                continue
            if response.status_code in (401, 403):
                raise OzonAuthError(f"{response.status_code}: {response.text[:300]}")
            if response.status_code == 429:
                time.sleep(min(float(response.headers.get("retry-after") or 5), 30))
                last = "429"
                continue
            if response.status_code >= 500:
                time.sleep(2 ** attempt)
                last = f"{response.status_code}"
                continue
            if response.status_code >= 400:
                raise OzonError(f"{response.status_code}: {response.text[:400]}")
            return response.json() if response.content else {}
        raise OzonError(last)

    def reviews(self, status: str = "UNPROCESSED", limit_pages: int = 20) -> list[dict]:
        """status: UNPROCESSED — без ответа, PROCESSED — уже обработанные, ALL — все."""
        self.reviews_complete = False
        out, last_id = [], ""
        for _ in range(limit_pages):
            body = {"limit": 100, "sort_dir": "DESC", "status": status}
            if last_id:
                body["last_id"] = last_id
            data = self._post("/v1/review/list", body)
            batch = data.get("reviews") or []
            out.extend(normalize_review(raw) for raw in batch)
            if not data.get("has_next"):
                self.reviews_complete = True
                return out
            last_id = data.get("last_id") or ""
            if not last_id:
                self.reviews_complete = True
                return out
            time.sleep(0.3)
        return out

    def review_count(self) -> dict:
        return self._post("/v1/review/count", {})

    def unanswered_feedbacks(self) -> list[dict]:
        items = self.reviews("UNPROCESSED")
        self._attach_catalog_names(items)
        return items

    def questions(self, status: str = "UNPROCESSED", limit_pages: int = 20) -> list[dict]:
        """status: UNPROCESSED — без ответа продавца, NEW — новые, PROCESSED — уже с ответом."""
        self.questions_complete = False
        out, last_id = [], ""
        for _ in range(limit_pages):
            body = {"filter": {"status": status}, "limit": 100, "sort_dir": "DESC"}
            if last_id:
                body["last_id"] = last_id
            data = self._post("/v1/question/list", body)
            batch = data.get("questions") or []
            out.extend(normalize_question(raw) for raw in batch)
            if not data.get("has_next"):
                self.questions_complete = True
                return out
            last_id = data.get("last_id") or ""
            if not last_id:
                self.questions_complete = True
                return out
            time.sleep(0.3)
        return out

    def question_count(self) -> dict:
        return self._post("/v1/question/count", {})

    def unanswered_questions(self) -> list[dict]:
        items = self.questions("UNPROCESSED")
        self._attach_catalog_names(items)
        return items

    def product_names(self, skus: list[int]) -> dict[int, str]:
        """Название из каталога. В отзывах Ozon его нет, только SKU. Сбой каталога не роняет опрос."""
        found: dict[int, str] = {}
        unique = list(dict.fromkeys(skus))
        for start in range(0, len(unique), 100):
            chunk = unique[start:start + 100]
            try:
                data = self._post("/v3/product/info/list", {"sku": [str(sku) for sku in chunk]})
            except OzonError:
                return found
            rows = data.get("items") or (data.get("result") or {}).get("items") or []
            for item in rows:
                if not isinstance(item, dict):
                    continue
                name = (item.get("name") or "").strip()
                sku = item.get("sku")
                if not name or sku in (None, ""):
                    continue
                try:
                    found[int(sku)] = name
                except (TypeError, ValueError):
                    continue
        return found

    def _attach_catalog_names(self, items: list[dict]) -> None:
        missing = []
        for item in items:
            name = item.get("product_name") or ""
            sku = item.get("nm_id")
            if sku and (not name or str(name).startswith("SKU ")):
                missing.append(int(sku))
        if not missing:
            return
        names = self.product_names(missing)
        for item in items:
            sku = item.get("nm_id")
            if sku in names:
                item["product_name"] = names[sku]

    def answer_feedback(self, item_id: str, text: str) -> dict:
        review_id = item_id.split(":", 1)[-1]
        return self._post("/v1/review/comment/create", {
            "review_id": review_id, "text": text, "mark_review_as_processed": True,
        })

    def answer_question(self, item_id: str, text: str, sku) -> dict:
        question_id = item_id.split(":", 1)[-1]
        body = {"question_id": question_id, "text": text}
        if sku:
            body["sku"] = int(sku)
        return self._post("/v1/question/answer/create", body)


def normalize_review(raw: dict) -> dict:
    photos = int(raw.get("photos_amount") or 0)
    videos = int(raw.get("videos_amount") or 0)
    sku = raw.get("sku")
    return {
        "id": f"ozon:{raw['id']}",
        "kind": "feedback",
        "marketplace": "ozon",
        "created_at": raw.get("published_at") or "",
        "nm_id": sku if isinstance(sku, int) else None,
        "product_name": raw.get("product_name") or (f"SKU {sku}" if sku else "Товар Ozon"),
        "supplier_article": str(sku or ""),
        "brand": raw.get("brand") or "",
        "user_name": "",
        "rating": raw.get("rating"),
        "text": raw.get("text") or "",
        "pros": "",
        "cons": "",
        "has_media": int(photos + videos > 0),
        "status": "new",
    }


def normalize_question(raw: dict) -> dict:
    sku = raw.get("sku")
    return {
        "id": f"ozon:{raw['id']}",
        "kind": "question",
        "marketplace": "ozon",
        "created_at": raw.get("published_at") or raw.get("created_at") or "",
        "nm_id": sku if isinstance(sku, int) else None,
        "product_name": raw.get("product_name") or (f"SKU {sku}" if sku else "Товар Ozon"),
        "supplier_article": str(sku or ""),
        "brand": "",
        "user_name": raw.get("author_name") or "",
        "rating": None,
        "text": raw.get("text") or "",
        "pros": "",
        "cons": "",
        "has_media": 0,
        "status": "new",
    }

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from config import settings
from models import ListingItem

logger = logging.getLogger(__name__)


class OLXScraper:
    """Асинхронний скрапер OLX.ua для відстеження нових 1-кімнатних квартир у Дніпрі."""

    API_URL = "https://www.olx.ua/api/v1/offers/"

    def __init__(
        self,
        timeout: float = settings.REQUEST_TIMEOUT,
        proxy: Optional[str] = settings.PROXY_URL,
    ) -> None:
        self.timeout = timeout
        self.proxy = proxy
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
            "Referer": "https://www.olx.ua/",
            "Sec-Ch-Ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Linux"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

    def _parse_item(self, raw: dict[str, Any]) -> Optional[ListingItem]:
        """Парсинг та валідація сирих даних оголошення OLX."""
        external_id = str(raw.get("id", "")).strip()
        if not external_id:
            return None

        title = (raw.get("title") or "Оголошення без назви").strip()
        url = raw.get("url") or ""

        # 1. Екстракція ціни та валюти
        price = 0.0
        currency = "грн"
        price_label: Optional[str] = None

        params = raw.get("params") or []
        rooms_label = "1 кімната"
        is_one_room = False

        if isinstance(params, list):
            for param in params:
                if not isinstance(param, dict):
                    continue
                key = param.get("key")
                val_data = param.get("value")

                if key == "price" and isinstance(val_data, dict):
                    raw_val = val_data.get("value")
                    if raw_val is not None:
                        try:
                            price = float(raw_val)
                        except (ValueError, TypeError):
                            price = 0.0
                    raw_curr = val_data.get("currency")
                    currency = "грн" if raw_curr in ("UAH", None) else str(raw_curr)
                    price_label = val_data.get("label")

                elif key == "number_of_rooms_string" and isinstance(val_data, dict):
                    val_key = val_data.get("key")
                    if val_key == "odnokomnatnye":
                        is_one_room = True
                    rooms_label = val_data.get("label") or "1 кімната"

        # Додаткова перевірка ціни за кореневим ключем
        if price == 0.0 and raw.get("price") is not None:
            try:
                price = float(raw["price"])
            except (ValueError, TypeError):
                price = 0.0

        if not price_label:
            price_label = f"{int(price):,} {currency}".replace(",", " ")

        # Фільтр по ціні (якщо налаштовано в .env)
        if settings.MIN_PRICE is not None and price < settings.MIN_PRICE:
            return None
        if settings.MAX_PRICE is not None and price > settings.MAX_PRICE:
            return None

        # 2. Локація та район
        location_data = raw.get("location") or {}
        city_name = settings.CITY_NAME
        district_name: Optional[str] = None

        if isinstance(location_data, dict):
            city_dict = location_data.get("city")
            if isinstance(city_dict, dict) and city_dict.get("name"):
                city_name = city_dict.get("name").strip()

            dist_dict = location_data.get("district")
            if isinstance(dist_dict, dict) and dist_dict.get("name"):
                district_name = dist_dict.get("name").strip()

        # Фільтр за виключеними районами
        if district_name and settings.excluded_districts:
            lower_dist = district_name.lower()
            for excluded in settings.excluded_districts:
                if excluded.lower() in lower_dist:
                    logger.debug(
                        "Ad %s skipped due to excluded district: %s",
                        external_id,
                        district_name,
                    )
                    return None

        # Форматування рядка локації
        if district_name:
            location_str = f"м. {city_name}, {district_name} р-н"
        else:
            location_str = f"м. {city_name}"

        # 3. Екстракція фотографій (перше фото)
        image_url: Optional[str] = None
        photos = raw.get("photos")
        if isinstance(photos, list) and photos:
            first_photo = photos[0]
            if isinstance(first_photo, dict):
                raw_link = first_photo.get("link", "")
                if raw_link:
                    image_url = raw_link.replace("{width}x{height}", "1000x700")
                elif first_photo.get("url"):
                    image_url = str(first_photo["url"])

        # 4. Час створення та статус підняття в ТОП
        created_time: Optional[datetime] = None
        last_refresh_time: Optional[datetime] = None

        if raw.get("created_time"):
            try:
                created_time = datetime.fromisoformat(raw["created_time"])
            except (ValueError, TypeError):
                created_time = None

        if raw.get("last_refresh_time"):
            try:
                last_refresh_time = datetime.fromisoformat(raw["last_refresh_time"])
            except (ValueError, TypeError):
                last_refresh_time = None

        promo = raw.get("promotion") or {}
        is_promoted = bool(
            raw.get("is_promoted")
            or raw.get("promoted")
            or (isinstance(promo, dict) and promo.get("top_ad"))
        )

        return ListingItem(
            id=ListingItem.create_id("olx", external_id),
            source="OLX.ua",
            external_id=external_id,
            title=title,
            price=price,
            currency=currency,
            price_label=price_label,
            rooms=rooms_label,
            city=city_name,
            district=district_name,
            location=location_str,
            url=url,
            image_url=image_url,
            created_time=created_time,
            last_refresh_time=last_refresh_time,
            is_promoted=is_promoted,
        )

    def is_genuinely_new(self, item: ListingItem) -> bool:
        """
        Перевіряє, чи оголошення справді нове (без старих оголошень, піднятих у ТОП).
        Якщо оголошення створене більше ніж MAX_AD_AGE_HOURS тому, воно вважається старим.
        """
        if not item.created_time:
            return True

        now = datetime.now(timezone.utc)
        created = item.created_time
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)

        age_hours = (now - created).total_seconds() / 3600.0
        if age_hours > settings.MAX_AD_AGE_HOURS:
            logger.debug(
                "Ad %s is %0.1f hours old (> %d max), treated as old bumped to top.",
                item.external_id,
                age_hours,
                settings.MAX_AD_AGE_HOURS,
            )
            return False

        return True

    async def fetch_listings(
        self,
        limit: int = 40,
        max_retries: int = 3,
    ) -> list[ListingItem]:
        """
        Виконує прямий запит до OLX API з параметрами для 1-кімнатних квартир у Дніпрі.
        Підтримує проксі, ретраї та автоматичну обробку помилок мережі.
        """
        params: dict[str, Any] = {
            "category_id": settings.CATEGORY_ID,
            "city_id": settings.CITY_ID,
            "filter_enum_number_of_rooms_string[0]": "odnokomnatnye",
            "sort_by": "created_at:desc",
            "limit": limit,
        }

        if settings.MIN_PRICE is not None:
            params["filter_float_price:from"] = settings.MIN_PRICE
        if settings.MAX_PRICE is not None:
            params["filter_float_price:to"] = settings.MAX_PRICE

        client_kwargs: dict[str, Any] = {
            "follow_redirects": True,
            "timeout": self.timeout,
        }
        if self.proxy:
            client_kwargs["proxy"] = self.proxy

        for attempt in range(1, max_retries + 1):
            try:
                try:
                    client = httpx.AsyncClient(http2=True, **client_kwargs)
                except Exception:
                    client = httpx.AsyncClient(**client_kwargs)

                async with client:
                    response = await client.get(
                        self.API_URL,
                        params=params,
                        headers=self.headers,
                    )

                    if response.status_code == 200:
                        data = response.json()
                        raw_items = data.get("data", []) if isinstance(data, dict) else []
                        logger.info("Retrieved %d raw items from OLX API", len(raw_items))

                        items: list[ListingItem] = []
                        for raw in raw_items:
                            if isinstance(raw, dict):
                                item = self._parse_item(raw)
                                if item:
                                    items.append(item)
                        return items

                    logger.warning(
                        "OLX API returned HTTP %d on attempt %d/%d",
                        response.status_code,
                        attempt,
                        max_retries,
                    )
            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                logger.warning(
                    "Network error on attempt %d/%d for OLX API: %s",
                    attempt,
                    max_retries,
                    exc,
                )

            if attempt < max_retries:
                await asyncio.sleep(2.0 * attempt)

        logger.error("Failed to fetch listings from OLX API after %d attempts", max_retries)
        return []

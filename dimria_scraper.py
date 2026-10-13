import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import httpx
from playwright.async_api import async_playwright

from config import settings
from models import ListingItem

logger = logging.getLogger(__name__)

# Жорсткі стоп-слова для анти-ріелтор валідації DIM.RIA
REALTOR_HARD_TRIGGERS = [
    "комісі",        # комісія, комісійні, комісію
    "комисси",       # комиссия, комиссионные
    "агентств",      # агентство, агентства
    "агенств",       # поширена помилка
    "агенці",        # агенція нерухомості
    "колегам",       # колегам не турбувати / співпраця
    "коллегам",
    "співпрац",      # без співпраці / співпрацюю
    "сотруднич",     # без сотрудничества / сотрудничаю
    "ексклюзив",     # ексклюзивний договір
    "эксклюзив",
    "послуги рі",    # послуги ріелтора
    "услуги ри",     # услуги риелтора
    "риелторск",     # риелторские услуги
    "рієлторськ",
    "послуги аген",  # послуги агентства
    "100% коміс",
    "комісія 100%",
]


class DimRiaScraper:
    """
    Скрапер порталу DIM.RIA (dom.ria.com) для моніторингу оренди 1-кімнатних квартир від власників у Дніпрі.
    - Етап 1: Швидкий вибір ID через searchEngine/v2 з бекенд-фільтром «ВІД ВЛАСНИКА» (1437_1436).
    - Етап 2: Дедуплікація та відсіювання вже оброблених ID до запуску браузера.
    - Етап 3: On-Demand Playwright екстракція (headless Chromium з блокуванням медіа):
      * Отримання повного опису з #descriptionBlock
      * Багаторівнева анти-ріелтор фільтрація (стоп-слова + перевірка userType / agency у відповідях API)
      * Витягування характеристик (поверх, поверховість, площа, ціна, район)
      * Зчитування галереї фото
      * Клік по кнопці номера та перехоплення getOwnerAndAgencyData / відкритих контактів.
    """

    SEARCH_URL = "https://dom.ria.com/node/searchEngine/v2/"
    BASE_VIEW_URL = "https://dom.ria.com/uk/realty_pereglyad-{item_id}.html"

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
            "Referer": "https://dom.ria.com/uk/",
        }

    def _normalize_phone(self, raw_phone: str) -> Optional[str]:
        """Приводить телефонний номер до єдиного формату +38 (0XX) XXX-XX-XX."""
        if not raw_phone:
            return None
        digits = re.sub(r"\D", "", raw_phone)
        if digits.startswith("380") and len(digits) == 12:
            return f"+{digits[:2]} ({digits[2:5]}) {digits[5:8]}-{digits[8:10]}-{digits[10:]}"
        if digits.startswith("0") and len(digits) == 10:
            return f"+38 ({digits[:3]}) {digits[3:6]}-{digits[6:8]}-{digits[8:]}"
        if len(digits) >= 9:
            return f"+{digits}" if not raw_phone.startswith("+") else raw_phone
        return raw_phone.strip()

    def _is_realtor_by_text(self, title: str, description: str, specs_text: str = "") -> tuple[bool, str]:
        """
        Аналізує заголовок, опис та характеристики на маркери ріелтора / агентства.
        Враховує контекст (наприклад, 'без ріелтора' або 'без комісії' не є маркером ріелтора).
        """
        combined = f"{title} {description} {specs_text}".lower()

        # 1. Прямі жорсткі тригери
        for trigger in REALTOR_HARD_TRIGGERS:
            if trigger in combined:
                # Перевіряємо, чи немає перед тригером 'без '
                if f"без {trigger}" in combined:
                    continue
                return True, f"знайдено стоп-слово '{trigger}'"

        # 2. Абревіатури АН / А/Н
        if re.search(r"\bан\b|\bа/н\b|\bа\.н\.\b", combined):
            return True, "знайдено маркер агентства 'АН'"

        # 3. Маркери ріелтора (якщо не передує 'без')
        cleaned = re.sub(r"без\s+(?:рієлтор\w*|ріелтор\w*|риелтор\w*|посередник\w*|комісі\w*)", "", combined)
        for w in ["рієлтор", "ріелтор", "риелтор", "посередник"]:
            if w in cleaned:
                return True, f"знайдено маркер посередника '{w}'"

        return False, ""

    async def fetch_new_listing_ids(
        self,
        client: httpx.AsyncClient,
        limit: int = 20,
    ) -> list[int]:
        """
        Швидкий HTTP-запит до API пошуку DIM.RIA для отримання списку ID свіжих оголошень.
        Використовує обов'язкові параметри:
        - state_id: Дніпропетровська обл.
        - city_ids: Дніпро
        - ch: 209_1 (1 кімната), 1437_1436 (ТИП ПРОПОЗИЦІЇ «ВІД ВЛАСНИКА»)
        """
        params: dict[str, Any] = {
            "category": 1,         # Квартири
            "realty_type": 2,      # Квартира
            "operation": 3,        # Довгострокова оренда
            "state_id": settings.DIMRIA_STATE_ID,
            "city_ids": settings.DIMRIA_CITY_ID,
            "ch": "209_1,1437_1436",  # 1 кімната + ТІЛЬКИ ВІД ВЛАСНИКА
            "wo_dupl": 1,          # Без дублікатів
            "limit": limit,
            "sort": "inspected_sort",
        }

        try:
            resp = await client.get(self.SEARCH_URL, params=params, headers=self.headers, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                raw_items = data.get("items", [])
                if isinstance(raw_items, list):
                    # Фільтруємо валідні числові ID
                    ids = [int(x) for x in raw_items if str(x).isdigit()]
                    logger.info("DIM.RIA searchEngine returned %d candidate owner IDs", len(ids))
                    return ids
            else:
                logger.warning("DIM.RIA searchEngine HTTP %d: %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            logger.error("Failed fetching IDs from DIM.RIA searchEngine: %s", exc)

        return []

    async def fetch_listing_details_playwright(
        self,
        item_id: int,
        page: Any,
        timeout_ms: int = 18000,
    ) -> Optional[ListingItem]:
        """
        On-Demand вилучення повної картки оголошення DIM.RIA через підготовлену сторінку Playwright:
        1. Відкриття сторінки оголошення.
        2. Перехоплення getOwnerAndAgencyData для перевірки відсутності агентства та отримання номера.
        3. Зчитування опису з #descriptionBlock та перевірка на стоп-слова ріелторів.
        4. Зчитування параметрів (ціна, площа, поверх, поверхність, район, фото).
        5. Клік по кнопці контактів для вилучення телефону.
        """
        target_url = self.BASE_VIEW_URL.format(item_id=item_id)
        intercepted_owner_data: dict[str, Any] = {}
        intercepted_additional_data: dict[str, Any] = {}
        extracted_phone: Optional[str] = None

        # Перехоплювач відповідей API контактів
        async def handle_response(response):
            nonlocal extracted_phone
            low_url = response.url.lower()
            if "getownerandagencydata" in low_url:
                try:
                    data = await response.json()
                    if isinstance(data, dict):
                        intercepted_owner_data.update(data)
                        owner_obj = data.get("owner") or {}
                        phones_list = owner_obj.get("phones") or []
                        if isinstance(phones_list, list) and phones_list:
                            first_p = phones_list[0]
                            raw_p = first_p.get("phone_num") if isinstance(first_p, dict) else str(first_p)
                            if raw_p:
                                norm = self._normalize_phone(raw_p)
                                if norm:
                                    extracted_phone = norm
                                    logger.debug("Intercepted phone via getOwnerAndAgencyData: %s", norm)
                except Exception as err:
                    logger.debug("Failed parsing getOwnerAndAgencyData: %s", err)

            elif "getowneradditionaldata" in low_url:
                try:
                    data = await response.json()
                    if isinstance(data, dict):
                        intercepted_additional_data.update(data)
                except Exception as err:
                    logger.debug("Failed parsing getOwnerAdditionalData: %s", err)

        page.on("response", handle_response)

        try:
            logger.info("Opening DIM.RIA listing in Playwright: %s", target_url)
            await page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as nav_err:
            logger.warning("DIM.RIA navigation timeout/error for ID %d: %s", item_id, nav_err)

        current_url = page.url or target_url

        # 1. Заголовок оголошення
        title = "1-кімнатна квартира"
        try:
            h1_loc = page.locator("h1").first
            if await h1_loc.is_visible(timeout=1000):
                h1_text = (await h1_loc.inner_text()).strip()
                if h1_text:
                    title = h1_text
        except Exception:
            pass

        # 2. Опис оголошення з #descriptionBlock
        description = ""
        try:
            desc_loc = page.locator("#descriptionBlock, [data-testid='description']").first
            if await desc_loc.is_visible(timeout=1000):
                description = (await desc_loc.inner_text()).strip()
        except Exception:
            pass

        # 3. Текстова перевірка на ріелтора (заголовок + опис)
        if settings.ONLY_OWNERS:
            is_realtor, reason = self._is_realtor_by_text(title, description)
            if is_realtor:
                logger.info("DIM.RIA ID %d rejected as REALTOR: %s", item_id, reason)
                return None

        # 4. Ціна
        price = 0.0
        currency = "грн"
        price_label = "Ціна за домовленістю"
        try:
            price_elems = await page.locator("text=/\\d+[\\s\\xa0]*грн/").all_inner_texts()
            for p_text in price_elems:
                if "комунал" in p_text.lower():
                    continue
                m = re.search(r"(\d[\d\s\xa0]*)\s*грн", p_text)
                if m:
                    raw_digits = re.sub(r"\D", "", m.group(1))
                    if raw_digits and int(raw_digits) >= 1000:
                        price = float(raw_digits)
                        price_label = f"{int(price):,} грн".replace(",", " ")
                        break
        except Exception as p_err:
            logger.debug("Error parsing price on DIM.RIA ID %d: %s", item_id, p_err)

        # Фільтр цін із налаштувань
        if settings.MIN_PRICE is not None and price > 0 and price < settings.MIN_PRICE:
            logger.debug("DIM.RIA ID %d skipped: price %.0f < MIN_PRICE", item_id, price)
            return None
        if settings.MAX_PRICE is not None and price > 0 and price > settings.MAX_PRICE:
            logger.debug("DIM.RIA ID %d skipped: price %.0f > MAX_PRICE", item_id, price)
            return None

        # 5. Параметри квартири (площа, поверх, поверховість)
        floor: Optional[str] = None
        total_floors: Optional[str] = None
        total_area: Optional[str] = None

        try:
            # Шукаємо площу
            area_matches = await page.locator("text=/\\d+\\s*(?:м²|м2)/i").all_inner_texts()
            for a_text in area_matches:
                m_area = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:м²|м2)", a_text)
                if m_area:
                    total_area = f"{m_area.group(1)} м²"
                    break

            # Шукаємо поверх та поверховість
            floor_matches = await page.locator("text=/поверх/i").all_inner_texts()
            for f_text in floor_matches:
                m_floor = re.search(r"(\d+)\s*поверх з\s*(\d+)", f_text)
                if m_floor:
                    floor = m_floor.group(1)
                    total_floors = m_floor.group(2)
                    break
        except Exception as param_err:
            logger.debug("Error parsing parameters for DIM.RIA ID %d: %s", item_id, param_err)

        # 6. Район міста
        district_name: Optional[str] = None
        try:
            rayon_links = await page.locator("a[href*='-rayon-']").all()
            for r_link in rayon_links:
                r_text = (await r_link.inner_text()).strip()
                if "район" in r_text.lower():
                    clean_r = re.sub(r"(?i)(?:мікро)?район\s*", "", r_text).strip()
                    if clean_r:
                        district_name = clean_r
                        break
        except Exception:
            pass

        # Фільтр виключених районів
        if district_name and settings.excluded_districts:
            lower_dist = district_name.lower()
            for excluded in settings.excluded_districts:
                if excluded.lower() in lower_dist:
                    logger.debug("DIM.RIA ID %d skipped due to excluded district %s", item_id, district_name)
                    return None

        location_str = f"м. {settings.CITY_NAME}, {district_name} р-н" if district_name else f"м. {settings.CITY_NAME}"

        # 7. Фотографії
        photos: list[str] = []
        try:
            og_img = await page.locator("meta[property='og:image']").get_attribute("content")
            if og_img and "riastatic.com" in og_img:
                photos.append(og_img)

            img_locs = await page.locator("img[src*='cdn.riastatic.com/photosnew/dom/photo/']").all()
            for im in img_locs:
                src = await im.get_attribute("src")
                if src and "riastatic.com" in src:
                    # Нормалізуємо розмір фото до максимального
                    hi_res = src.replace("m.jpg", "xl.jpg").replace("s.jpg", "xl.jpg").replace("f.jpg", "xl.jpg")
                    if hi_res not in photos:
                        photos.append(hi_res)
        except Exception as img_err:
            logger.debug("Error collecting photos for DIM.RIA ID %d: %s", item_id, img_err)

        image_url = photos[0] if photos else None

        # 8. Клік по кнопці розкриття номера телефону
        phone_btn_selectors = [
            "button:has-text('XXX')",
            "a:has-text('XXX')",
            "button:has-text('Показати')",
            "a:has-text('Показати')",
        ]
        phone_btn = None
        for sel in phone_btn_selectors:
            try:
                candidate = page.locator(sel).first
                if await candidate.is_visible(timeout=1000):
                    phone_btn = candidate
                    break
            except Exception:
                continue

        if phone_btn:
            try:
                await phone_btn.scroll_into_view_if_needed(timeout=1500)
                await phone_btn.click(timeout=3000)
                logger.debug("Clicked DIM.RIA contact button for ID %d", item_id)
            except Exception as click_err:
                logger.debug("Click failed, attempting force click: %s", click_err)
                try:
                    await phone_btn.click(force=True, timeout=1500)
                except Exception:
                    pass

        # Очікуємо на прихід перехопленої відповіді getOwnerAndAgencyData
        for _ in range(6):
            if extracted_phone and intercepted_owner_data:
                break
            await asyncio.sleep(0.5)

        # Fallback на tel: лінки, якщо через мережу номер не прийшов
        if not extracted_phone:
            try:
                tel_links = page.locator("a[href^='tel:']")
                tel_cnt = await tel_links.count()
                for i in range(tel_cnt):
                    href = await tel_links.nth(i).get_attribute("href")
                    if href:
                        clean_tel = href.replace("tel:", "").strip()
                        norm = self._normalize_phone(clean_tel)
                        if norm:
                            extracted_phone = norm
                            break
            except Exception:
                pass

        # 9. Анти-ріелтор валідація даних автора з API (Рівень 3)
        author_name = "Власник"
        author_ads_count = 1

        if intercepted_owner_data:
            owner_dict = intercepted_owner_data.get("owner") or {}
            agency_dict = intercepted_owner_data.get("agency") or {}

            # Перевірка наявності агентства
            if agency_dict and (agency_dict.get("name") or agency_dict.get("agency_id")):
                logger.info("DIM.RIA ID %d rejected: Agency detected: %s", item_id, agency_dict.get("name"))
                return None

            # Перевірка типу користувача
            user_type_str = str(owner_dict.get("userTypeNameUk") or owner_dict.get("userTypeName") or "").lower()
            if any(w in user_type_str for w in ["рієлтор", "ріелтор", "риелтор", "агент"]):
                logger.info("DIM.RIA ID %d rejected: userType '%s'", item_id, user_type_str)
                return None

            if owner_dict.get("isInspectedRealtor") or owner_dict.get("asnuVerified") or owner_dict.get("ssnuVerified"):
                logger.info("DIM.RIA ID %d rejected: Verified realtor flags detected", item_id)
                return None

            # Ім'я власника
            if owner_dict.get("name"):
                author_name = str(owner_dict["name"]).strip()
            elif owner_dict.get("first_name"):
                author_name = str(owner_dict["first_name"]).strip()

        # Перевірка кількості активних оголошень акаунта
        if intercepted_additional_data and settings.ONLY_OWNERS:
            realties_count = intercepted_additional_data.get("realtiesCount") or {}
            all_count = realties_count.get("allCount")
            if isinstance(all_count, int):
                author_ads_count = all_count
                if author_ads_count > settings.MAX_OWNER_ACTIVE_ADS:
                    logger.info(
                        "DIM.RIA ID %d rejected: Author '%s' has %d active ads (> %d max allowed)",
                        item_id,
                        author_name,
                        author_ads_count,
                        settings.MAX_OWNER_ACTIVE_ADS,
                    )
                    return None

        # Дата створення (якщо доступно)
        created_time = datetime.now(timezone.utc)

        return ListingItem(
            id=ListingItem.create_id("dimria", str(item_id)),
            source="DIM.RIA",
            external_id=str(item_id),
            title=title,
            price=price,
            currency=currency,
            price_label=price_label,
            rooms="1 кімната",
            city=settings.CITY_NAME,
            district=district_name,
            location=location_str,
            url=current_url,
            image_url=image_url,
            photos=photos,
            description=description,
            phone=extracted_phone,
            floor=floor,
            total_floors=total_floors,
            total_area=total_area,
            author_name=author_name,
            author_ads_count=author_ads_count,
            is_owner=True,
            created_time=created_time,
            is_promoted=False,
        )

    def is_genuinely_new(self, item: ListingItem) -> bool:
        """Перевіряє вік оголошення (за аналогією до OLX)."""
        if not item.created_time:
            return True
        now = datetime.now(timezone.utc)
        created = item.created_time
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_hours = (now - created).total_seconds() / 3600.0
        return age_hours <= settings.MAX_AD_AGE_HOURS

    async def fetch_listings(
        self,
        limit: int = 20,
        proxy: Optional[str] = None,
        seen_checker: Optional[Callable[[str], Any]] = None,
    ) -> list[ListingItem]:
        """
        Повний цикл збору та фільтрації нових об'єктів з DIM.RIA:
        1. Отримує ID свіжих 1-кімнатних квартир від власників у Дніпрі.
        2. Якщо передано seen_checker (is_listing_seen), відфільтровує вже бачені ID ДО запуску Playwright.
        3. Для залишкових нових ID запускає браузерний контекст і вилучає повні дані та телефон.
        """
        active_proxy = proxy or self.proxy
        client_kwargs: dict[str, Any] = {
            "follow_redirects": True,
            "timeout": self.timeout,
        }
        if active_proxy:
            client_kwargs["proxy"] = active_proxy

        try:
            client = httpx.AsyncClient(http2=True, **client_kwargs)
        except Exception:
            client = httpx.AsyncClient(**client_kwargs)

        candidate_ids: list[int] = []
        async with client:
            candidate_ids = await self.fetch_new_listing_ids(client, limit=limit)

        if not candidate_ids:
            logger.info("No candidates returned from DIM.RIA searchEngine")
            return []

        # Дедуплікація до запуску важкого Playwright
        new_ids: list[int] = []
        for cid in candidate_ids:
            listing_id = ListingItem.create_id("dimria", str(cid))
            if seen_checker:
                try:
                    is_seen = seen_checker(listing_id)
                    if asyncio.iscoroutine(is_seen):
                        is_seen = await is_seen
                    if is_seen:
                        continue
                except Exception as check_err:
                    logger.debug("Error running seen_checker for %s: %s", listing_id, check_err)
            new_ids.append(cid)

        if not new_ids:
            logger.info("All %d DIM.RIA candidate IDs were already processed.", len(candidate_ids))
            return []

        logger.info("Processing %d fresh DIM.RIA listings via On-Demand Playwright...", len(new_ids))

        # Запуск Playwright для нових ID
        verified_items: list[ListingItem] = []
        try:
            async with async_playwright() as p:
                launch_kwargs: dict[str, Any] = {
                    "headless": True,
                    "args": [
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--disable-blink-features=AutomationControlled",
                    ],
                    "ignore_default_args": ["--enable-automation"],
                }
                if active_proxy:
                    try:
                        p_parsed = urlparse(active_proxy)
                        p_dict: dict[str, str] = {
                            "server": f"{p_parsed.scheme}://{p_parsed.hostname}:{p_parsed.port}"
                        }
                        if p_parsed.username:
                            p_dict["username"] = p_parsed.username
                        if p_parsed.password:
                            p_dict["password"] = p_parsed.password
                        launch_kwargs["proxy"] = p_dict
                    except Exception as p_err:
                        logger.warning("Failed parsing proxy for DIM.RIA Playwright: %s", p_err)

                browser = await p.chromium.launch(**launch_kwargs)
                try:
                    context = await browser.new_context(
                        user_agent=(
                            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                        ),
                        locale="uk-UA",
                        viewport={"width": 1280, "height": 800},
                    )

                    page = await context.new_page()

                    # Маскування автоматизації
                    try:
                        await page.add_init_script("""
                            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                            window.navigator.chrome = { runtime: {} };
                        """)
                    except Exception:
                        pass

                    # Блокування важких ресурсів для оптимізації трафіку та RAM
                    async def block_routes(route):
                        try:
                            if route.request.resource_type in ("image", "media", "font"):
                                await route.abort()
                            else:
                                await route.continue_()
                        except Exception:
                            pass

                    await page.route("**/*", block_routes)

                    for item_id in new_ids:
                        try:
                            item = await self.fetch_listing_details_playwright(item_id, page)
                            if item:
                                verified_items.append(item)
                                logger.info(
                                    "DIM.RIA listing verified: [%s] %s | %s | %s",
                                    item.id,
                                    item.title,
                                    item.price_label,
                                    item.phone or "без телефону",
                                )
                            # Коротка пауза між сторінками
                            await asyncio.sleep(0.5)
                        except Exception as page_err:
                            logger.warning("Error processing DIM.RIA item %d: %s", item_id, page_err)

                finally:
                    await browser.close()

        except Exception as pw_err:
            logger.error("Playwright batch processing failed for DIM.RIA: %s", pw_err)

        logger.info(
            "DIM.RIA parsing complete: %d verified OWNER listings obtained out of %d candidates",
            len(verified_items),
            len(new_ids),
        )
        return verified_items

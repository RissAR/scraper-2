import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from playwright.async_api import async_playwright

from config import settings
from models import ListingItem

logger = logging.getLogger(__name__)

# Жорсткі стоп-слова, наявність яких у тексті гарантує, що це ріелтор / посередник
REALTOR_HARD_TRIGGERS = [
    "комісі",      # комісія, комісійні, комісію
    "комисси",     # комиссия, комиссионные
    "агентств",    # агентство, агентства
    "агенств",     # поширена помилка
    "агенці",      # агенція нерухомості
    "колегам",     # колегам не турбувати / співпраця
    "коллегам",
    "співпрац",    # без співпраці / співпрацюю
    "сотруднич",   # без сотрудничества / сотрудничаю
    "ексклюзив",   # ексклюзивний договір
    "эксклюзив",
    "послуги рі",  # послуги ріелтора
    "услуги ри",   # услуги риелтора
    "риелторск",   # риелторские услуги
    "рієлторськ",
]


def _mask_proxy_str(proxy_url: Optional[str]) -> str:
    """Маскує облікові дані в URL проксі для безпечного логування."""
    if not proxy_url:
        return "None"
    url = str(proxy_url)
    if "@" in url:
        prefix, host_part = url.split("@", 1)
        scheme = prefix.split("://")[0] + "://" if "://" in prefix else ""
        return f"{scheme}***:***@{host_part}"
    return url


class OLXScraper:
    """
    Повноцінний асинхронний скрапер OLX.ua з розширеним функціоналом:
    - Витягування виключно об'єктів від ВЛАСНИКІВ (жорсткий багатошаровий анти-ріелтор фільтр).
    - Екстракція номерів телефонів (з API або тексту опису/заголовка).
    - Повний парсинг параметрів: поверх, поверховість, площа, опис, повна галерея фото.
    - Перевірка кількості активних оголошень в акаунта (виявлення прихованих ріелторів).
    - Захист від старих оголошень, піднятих у ТОП.
    """

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
        if settings.OLX_BEARER_TOKEN:
            self.headers["Authorization"] = f"Bearer {settings.OLX_BEARER_TOKEN.strip()}"

    def _extract_phone_from_text(self, text: str) -> Optional[str]:
        """Шукає та форматує номер телефону в тексті оголошення (опис, ім'я, заголовок)."""
        if not text:
            return None

        # Очищуємо HTML теги
        clean_text = re.sub(r"<[^>]+>", " ", text)

        patterns = [
            r"(?:\+?38\s*)?\(?0\d{2}\)?[\s\-_.]*\d{3}[\s\-_.]*\d{2}[\s\-_.]*\d{2}",
            r"(?:\+?38\s*)?\(?0\d{2}\)?[\s\-_.]*\d{2}[\s\-_.]*\d{2}[\s\-_.]*\d{3}",
            r"\b0\d{9}\b",
        ]
        for pattern in patterns:
            matches = re.findall(pattern, clean_text)
            for m in matches:
                digits = re.sub(r"\D", "", m)
                if digits.startswith("380") and len(digits) == 12:
                    return f"+{digits[:2]} ({digits[2:5]}) {digits[5:8]}-{digits[8:10]}-{digits[10:]}"
                if digits.startswith("0") and len(digits) == 10:
                    return f"+38 ({digits[:3]}) {digits[3:6]}-{digits[6:8]}-{digits[8:]}"

        return None

    def _is_realtor_by_text(self, title: str, description: str) -> tuple[bool, str]:
        """
        Аналізує текст заголовка та опису на наявність маркерів ріелтора/агентства.
        Враховує контекст (наприклад, 'без ріелтора' не є маркером ріелтора).
        """
        combined = f"{title} {description}".lower()

        # 1. Прямі жорсткі тригери
        for trigger in REALTOR_HARD_TRIGGERS:
            if trigger in combined:
                return True, f"знайдено стоп-слово '{trigger}'"

        # 2. Абревіатури АН / А/Н
        if re.search(r"\bан\b|\bа/н\b|\bа\.н\.\b", combined):
            return True, "знайдено маркер агентства 'АН'"

        # 3. Маркери ріелтора (якщо не передує 'без')
        cleaned = re.sub(r"без\s+(?:рієлтор\w*|ріелтор\w*|риелтор\w*|риэлтор\w*|посередник\w*)", "", combined)
        for w in ["рієлтор", "ріелтор", "риелтор", "риэлтор"]:
            if w in cleaned:
                return True, f"знайдено слово '{w}'"

        return False, ""

    async def fetch_author_active_ads(self, client: httpx.AsyncClient, user_id: int) -> int:
        """
        Отримує точну кількість активних оголошень в акаунта на OLX.
        Якщо оголошень > MAX_OWNER_ACTIVE_ADS — акаунт належить агентству / ріелтору.
        """
        if not user_id:
            return 1
        try:
            url = f"{self.API_URL}?user_id={user_id}&limit=1"
            resp = await client.get(url, headers=self.headers, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                meta = data.get("metadata", {})
                return int(meta.get("total_elements", 1))
        except Exception as exc:
            logger.debug("Failed fetching user %d ads count: %s", user_id, exc)
        return 1

    async def fetch_offer_details(
        self, client: httpx.AsyncClient, offer_id: str
    ) -> dict[str, Any]:
        """Отримує детальні дані одного оголошення (повний опис, всі фото, телефон)."""
        url = f"{self.API_URL}{offer_id}/"
        try:
            resp = await client.get(url, headers=self.headers, timeout=self.timeout)
            if resp.status_code == 200:
                return resp.json().get("data", {})
        except Exception as exc:
            logger.debug("Failed fetching details for offer %s: %s", offer_id, exc)
        return {}

    async def fetch_offer_phone(
        self,
        offer_url: Any,
        offer_id: Optional[str] = None,
        proxy: Optional[str] = None,
        timeout_ms: int = 15000,
    ) -> Optional[str]:
        """
        On-Demand Playwright екстрактор для витягування номера телефону власника:
        1. Відкриває сторінку знайденого власника через headless Chromium.
        2. Блокує важкі медіа-ресурси (зображення, медіа, шрифти, стилі) для максимальної швидкості та економії пам'яті.
        3. Безпечно знаходить та клікає кнопку 'Показати номер' / 'Показати телефон' з перевіркою видимості.
        4. Перехоплює відповідь limited-phones API або зчитує номер безпосередньо з DOM (tel: лінки / контейнер).
        """
        # Обробка виклику зі старим форматом або посиланням
        target_url = str(offer_url) if isinstance(offer_url, str) and offer_url.startswith("http") else None
        if not target_url and offer_id:
            target_url = f"https://www.olx.ua/d/uk/obyavlenie/-ID{offer_id}.html"

        if not target_url:
            return None

        def _normalize(phone_str: str) -> Optional[str]:
            digits = re.sub(r"\D", "", phone_str)
            if digits.startswith("380") and len(digits) == 12:
                return f"+{digits[:2]} ({digits[2:5]}) {digits[5:8]}-{digits[8:10]}-{digits[10:]}"
            if digits.startswith("0") and len(digits) == 10:
                return f"+38 ({digits[:3]}) {digits[3:6]}-{digits[6:8]}-{digits[8:]}"
            if len(digits) >= 9:
                return f"+{digits}" if not phone_str.startswith("+") else phone_str
            return None

        async def _extract_phone_playwright(use_proxy: bool = True) -> Optional[str]:
            extracted_phone: Optional[str] = None
            browser = None
            context = None
            page = None

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

                    # Підтримка активного проксі
                    active_proxy = (proxy or self.proxy) if use_proxy else None
                    if active_proxy:
                        try:
                            proxy_to_parse = active_proxy
                            if not (proxy_to_parse.startswith("http://") or proxy_to_parse.startswith("https://") or proxy_to_parse.startswith("socks5://")):
                                proxy_to_parse = f"http://{proxy_to_parse}"
                            parsed_proxy = urlparse(proxy_to_parse)
                            proxy_dict: dict[str, str] = {
                                "server": f"{parsed_proxy.scheme}://{parsed_proxy.hostname}:{parsed_proxy.port}"
                            }
                            if parsed_proxy.username:
                                proxy_dict["username"] = parsed_proxy.username
                            if parsed_proxy.password:
                                proxy_dict["password"] = parsed_proxy.password
                            launch_kwargs["proxy"] = proxy_dict
                        except Exception as p_err:
                            logger.warning("Failed parsing proxy for Playwright: %s", p_err)

                    browser = await p.chromium.launch(**launch_kwargs)
                    extra_headers = {
                        "sec-ch-ua": '"Google Chrome";v="128", "Chromium";v="128", "Not=A?Brand";v="24"',
                        "sec-ch-ua-mobile": "?0",
                        "sec-ch-ua-platform": '"Linux"',
                    }
                    if settings.OLX_BEARER_TOKEN:
                        extra_headers["Authorization"] = f"Bearer {settings.OLX_BEARER_TOKEN.strip()}"

                    context = await browser.new_context(
                        user_agent=(
                            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                        ),
                        locale="uk-UA",
                        viewport={"width": 1280, "height": 800},
                        extra_http_headers=extra_headers,
                    )

                    # Авторизація через OLX_COOKIES (гаряче оновлення з SQLite або з конфігурації)
                    active_cookies_str = None
                    try:
                        from database import db
                        active_cookies_str = await db.get_olx_cookies()
                    except Exception:
                        pass
                    if not active_cookies_str:
                        active_cookies_str = settings.OLX_COOKIES

                    if active_cookies_str and active_cookies_str.strip():
                        cookie_items = []
                        for pair in active_cookies_str.split(";"):
                            pair = pair.strip()
                            if "=" in pair:
                                c_key, c_val = pair.split("=", 1)
                                cookie_items.append({
                                    "name": c_key.strip(),
                                    "value": c_val.strip(),
                                    "domain": ".olx.ua",
                                    "path": "/",
                                })
                        if cookie_items:
                            try:
                                await context.add_cookies(cookie_items)
                                logger.debug("Added %d cookies to Playwright context", len(cookie_items))
                            except Exception as c_err:
                                logger.debug("Failed adding cookies: %s", c_err)

                    page = await context.new_page()

                    # Маскування автоматизації
                    try:
                        await page.add_init_script("""
                            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                            window.chrome = { runtime: {} };
                            if (navigator.userAgentData) {
                                Object.defineProperty(navigator, 'userAgentData', {
                                    get: () => ({
                                        brands: [
                                            { brand: 'Google Chrome', version: '128' },
                                            { brand: 'Chromium', version: '128' },
                                            { brand: 'Not=A?Brand', version: '24' }
                                        ],
                                        mobile: false,
                                        platform: 'Linux'
                                    })
                                });
                            }
                        """)
                    except Exception:
                        pass

                    # Блокування важких ресурсів: виключно media, image, font (стилі stylesheet обов'язково дозволені)
                    async def block_media_routes(route):
                        try:
                            if route.request.resource_type in ("image", "media", "font"):
                                await route.abort()
                            else:
                                await route.continue_()
                        except Exception:
                            pass

                    await page.route("**/*", block_media_routes)

                    # Перехоплення limited-phones через network response (Канал 1)
                    async def handle_response(response):
                        nonlocal extracted_phone
                        if "limited-phones" in response.url or "/phones" in response.url:
                            try:
                                data = await response.json()
                                phones = []
                                if isinstance(data, dict):
                                    d_data = data.get("data")
                                    if isinstance(d_data, dict):
                                        phones = d_data.get("phones", [])
                                    elif isinstance(d_data, list):
                                        phones = d_data
                                    if not phones and "phones" in data:
                                        phones = data.get("phones", [])
                                if phones:
                                    for raw_p in phones:
                                        norm = _normalize(str(raw_p))
                                        if norm:
                                            extracted_phone = norm
                                            logger.info("Successfully intercepted phone from limited-phones API: %s", norm)
                                            break
                            except Exception as json_err:
                                logger.debug("Failed parsing limited-phones JSON: %s", json_err)

                    page.on("response", handle_response)

                    # Безпечне відкриття сторінки
                    try:
                        await page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
                    except Exception as nav_err:
                        logger.debug("Navigation timeout/warning for %s: %s", target_url, nav_err)

                    # Безпечне закриття cookie-банера, якщо присутній
                    try:
                        cookie_btn = page.locator(
                            'button[data-testid="accept-cookies"], button#onetrust-accept-btn-handler, button:has-text("Прийняти"), button:has-text("Принять")'
                        ).first
                        if await cookie_btn.count() > 0 and await cookie_btn.is_visible(timeout=1000):
                            await cookie_btn.click(timeout=1000)
                            await asyncio.sleep(0.5)
                    except Exception:
                        pass

                    # Безпечний пошук кнопки розкриття телефону (перевірка всіх екземплярів селекторів)
                    button_selectors = [
                        'button[data-testid="show-phone"]',
                        'button[data-testid="ad-contact-phone"]',
                        'button[data-cy="ad-contact-phone"]',
                        '[data-testid="phones-container"] button',
                        'button:has-text("Показати")',
                        'button:has-text("Показать")',
                    ]

                    phone_btn = None
                    for sel in button_selectors:
                        try:
                            loc = page.locator(sel)
                            cnt = await loc.count()
                            for i in range(cnt):
                                candidate = loc.nth(i)
                                if await candidate.is_visible(timeout=500):
                                    phone_btn = candidate
                                    break
                            if phone_btn:
                                break
                        except Exception:
                            continue

                    if not phone_btn:
                        try:
                            await page.evaluate("window.scrollBy(0, 300)")
                            await asyncio.sleep(0.5)
                            for sel in button_selectors:
                                loc = page.locator(sel)
                                cnt = await loc.count()
                                for i in range(cnt):
                                    candidate = loc.nth(i)
                                    if await candidate.is_visible(timeout=500):
                                        phone_btn = candidate
                                        break
                                if phone_btn:
                                    break
                        except Exception:
                            pass

                    # Безпечний клік по кнопці з багаторівневим fallback
                    if phone_btn:
                        try:
                            await phone_btn.scroll_into_view_if_needed(timeout=1500)
                            await phone_btn.click(timeout=3000)
                            logger.debug("Clicked show phone button")
                        except Exception as click_err:
                            logger.debug("Direct click failed, attempting fallback force click: %s", click_err)
                            try:
                                await phone_btn.click(force=True, timeout=2000)
                            except Exception:
                                try:
                                    await phone_btn.dispatch_event("click")
                                except Exception:
                                    pass

                    # Очікування на оновлення відповіді мережі або DOM (1-2 с)
                    for _ in range(8):
                        if extracted_phone:
                            break
                        await asyncio.sleep(0.5)

                    # Fallback 1: Перевірка DOM на появу лінків a[href^="tel:"]
                    if not extracted_phone:
                        try:
                            tel_links = page.locator('a[href^="tel:"]')
                            tel_count = await tel_links.count()
                            for i in range(tel_count):
                                href = await tel_links.nth(i).get_attribute("href")
                                if href:
                                    clean_href = href.replace("tel:", "").strip()
                                    norm = _normalize(clean_href)
                                    if norm:
                                        extracted_phone = norm
                                        logger.info("Found phone via DOM tel: link: %s", norm)
                                        break
                        except Exception as dom_err:
                            logger.debug("Error checking tel: links in DOM: %s", dom_err)

                    # Fallback 2: Перевірка тексту контейнера контактів або кнопок (Канал 2)
                    if not extracted_phone:
                        try:
                            container = page.locator(
                                '[data-testid="phones-container"], [data-testid="show-phone"], [data-testid="ad-contact-phone"]'
                            )
                            c_count = await container.count()
                            for i in range(c_count):
                                text = await container.nth(i).inner_text()
                                if text and "xxx" not in text.lower():
                                    found = self._extract_phone_from_text(text)
                                    if found:
                                        extracted_phone = found
                                        logger.info("Found phone via DOM container text: %s", found)
                                        break
                        except Exception as c_err:
                            logger.debug("Error checking container text: %s", c_err)
            except Exception as exc:
                logger.warning("Playwright on-demand phone extraction failed for %s: %s", target_url, exc)
            finally:
                if page and not page.is_closed():
                    try:
                        await page.close()
                    except Exception:
                        pass
                if context:
                    try:
                        await context.close()
                    except Exception:
                        pass
                if browser:
                    try:
                        await browser.close()
                    except Exception:
                        pass

            return extracted_phone

        # Watchdog: таймаут 25с на виконання екстракції Playwright
        has_proxy = bool(proxy or self.proxy)
        try:
            phone_result = await asyncio.wait_for(_extract_phone_playwright(use_proxy=True), timeout=25.0)
            if phone_result:
                return phone_result
        except asyncio.TimeoutError:
            logger.warning("Watchdog: Playwright phone extraction timed out after 25s for %s", target_url)
        except Exception as exc:
            logger.warning("Watchdog caught unexpected error for %s: %s", target_url, exc)

        # Якщо з проксі не вдалося отримати номер, пробуємо пряме підключення як fallback
        if has_proxy:
            try:
                logger.info("Retrying Playwright phone extraction via direct connection for %s", target_url)
                return await asyncio.wait_for(_extract_phone_playwright(use_proxy=False), timeout=20.0)
            except Exception as direct_exc:
                logger.debug("Direct Playwright fallback failed for %s: %s", target_url, direct_exc)

        return None

    def _parse_params(self, params_list: list) -> dict[str, Any]:
        """Парсить параметри квартири з сирого списку params."""
        res: dict[str, Any] = {}
        if not isinstance(params_list, list):
            return res

        for p in params_list:
            if not isinstance(p, dict):
                continue
            key = p.get("key")
            val = p.get("value")
            if isinstance(val, dict):
                res[key] = val.get("label") or val.get("key")
            elif val is not None:
                res[key] = str(val)
        return res

    async def _parse_item_full(
        self,
        client: httpx.AsyncClient,
        raw: dict[str, Any],
        proxy: Optional[str] = None,
    ) -> Optional[ListingItem]:
        """
        Повний багатошаровий парсинг оголошення:
        1. Перевірка статусу власника (відсіювання ріелторів).
        2. Отримання детальних характеристик.
        3. Екстракція телефону.
        """
        external_id = str(raw.get("id", "")).strip()
        if not external_id:
            return None

        # --- КРОК 1: Базовий фільтр ріелторів на рівні об'єкта ---
        if settings.ONLY_OWNERS:
            if raw.get("business") is True:
                logger.debug("Ad %s rejected: raw.business is True", external_id)
                return None

            user_obj = raw.get("user") or {}
            if user_obj.get("b2c_business_page") is True:
                logger.debug("Ad %s rejected: b2c_business_page is True", external_id)
                return None

            company = user_obj.get("company_name")
            if company and str(company).strip():
                logger.debug("Ad %s rejected: company_name '%s'", external_id, company)
                return None

        title = (raw.get("title") or "Оголошення без назви").strip()
        url = raw.get("url") or ""

        # --- КРОК 2: Завантаження повних деталей оголошення ---
        details = await self.fetch_offer_details(client, external_id)
        description = (details.get("description") or "").strip()

        # --- КРОК 3: Текстовий анти-ріелтор фільтр за назвою та описом ---
        if settings.ONLY_OWNERS:
            is_realtor, reason = self._is_realtor_by_text(title, description)
            if is_realtor:
                logger.info("Ad %s rejected as REALTOR: %s", external_id, reason)
                return None

        # --- КРОК 4: Перевірка кількості оголошень акаунта ---
        user_id = raw.get("user", {}).get("id") or details.get("user", {}).get("id")
        user_name = (
            raw.get("user", {}).get("name")
            or raw.get("contact", {}).get("name")
            or "Власник"
        ).strip()

        author_ads_count = 1
        if user_id and settings.ONLY_OWNERS:
            author_ads_count = await self.fetch_author_active_ads(client, user_id)
            if author_ads_count > settings.MAX_OWNER_ACTIVE_ADS:
                logger.info(
                    "Ad %s rejected: Author '%s' (id: %s) has %d active ads (> %d max allowed for owner)",
                    external_id,
                    user_name,
                    user_id,
                    author_ads_count,
                    settings.MAX_OWNER_ACTIVE_ADS,
                )
                return None

        # --- КРОК 5: Екстракція ціни ---
        price = 0.0
        currency = "грн"
        price_label: Optional[str] = None

        all_params = details.get("params") or raw.get("params") or []
        parsed_params = self._parse_params(all_params)

        for p in all_params:
            if isinstance(p, dict) and p.get("key") == "price":
                v = p.get("value")
                if isinstance(v, dict):
                    try:
                        price = float(v.get("value") or 0.0)
                    except (ValueError, TypeError):
                        price = 0.0
                    raw_curr = v.get("currency")
                    currency = "грн" if raw_curr in ("UAH", None) else str(raw_curr)
                    price_label = v.get("label")
                break

        if price == 0.0 and raw.get("price") is not None:
            try:
                price = float(raw["price"])
            except (ValueError, TypeError):
                price = 0.0

        if not price_label:
            price_label = f"{int(price):,} {currency}".replace(",", " ")

        # Цінові межі з .env
        if settings.MIN_PRICE is not None and price < settings.MIN_PRICE:
            return None
        if settings.MAX_PRICE is not None and price > settings.MAX_PRICE:
            return None

        # --- КРОК 6: Локація та райони ---
        location_data = details.get("location") or raw.get("location") or {}
        city_name = settings.CITY_NAME
        district_name: Optional[str] = None

        if isinstance(location_data, dict):
            city_dict = location_data.get("city")
            if isinstance(city_dict, dict) and city_dict.get("name"):
                city_name = city_dict.get("name").strip()

            dist_dict = location_data.get("district")
            if isinstance(dist_dict, dict) and dist_dict.get("name"):
                district_name = dist_dict.get("name").strip()

        # Фільтр виключених районів
        if district_name and settings.excluded_districts:
            lower_dist = district_name.lower()
            for excluded in settings.excluded_districts:
                if excluded.lower() in lower_dist:
                    logger.debug("Ad %s skipped due to excluded district %s", external_id, district_name)
                    return None

        location_str = f"м. {city_name}, {district_name} р-н" if district_name else f"м. {city_name}"

        # --- КРОК 7: Фотографії ---
        photos_raw = details.get("photos") or raw.get("photos") or []
        photos: list[str] = []
        image_url: Optional[str] = None

        for ph in photos_raw:
            if isinstance(ph, dict):
                link = ph.get("link", "")
                if link:
                    formatted_link = link.replace("{width}x{height}", "1000x700")
                    photos.append(formatted_link)
                elif ph.get("url"):
                    photos.append(str(ph["url"]))

        if photos:
            image_url = photos[0]

        # --- КРОК 8: Витягування номера телефону ---
        # 1. Спочатку перевіряємо, чи вказано відкритий номер у тексті (опис, заголовок, ім'я)
        combined_text = f"{title} {user_name} {description}"
        phone = self._extract_phone_from_text(combined_text)

        # Перевіряємо контактні поля з API
        if not phone:
            contact = details.get("contact") or raw.get("contact") or {}
            raw_phone = contact.get("phone") or contact.get("phone_number")
            if raw_phone:
                phone = self._extract_phone_from_text(str(raw_phone))

        # 2. Якщо в тексті немає телефону — запускаємо On-Demand Playwright екстрактор
        if not phone and url:
            phone = await self.fetch_offer_phone(
                offer_url=url,
                offer_id=external_id,
                proxy=proxy,
            )

        # --- КРОК 9: Параметри квартири ---
        floor = parsed_params.get("floor")
        total_floors = parsed_params.get("total_floors")
        total_area = parsed_params.get("total_area")
        kitchen_area = parsed_params.get("kitchen_area")
        rooms_label = parsed_params.get("number_of_rooms_string") or "1 кімната"

        # Дати та просування
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
            photos=photos,
            description=description,
            phone=phone,
            floor=floor,
            total_floors=total_floors,
            total_area=total_area,
            kitchen_area=kitchen_area,
            author_name=user_name,
            author_ads_count=author_ads_count,
            is_owner=True,
            created_time=created_time,
            last_refresh_time=last_refresh_time,
            is_promoted=is_promoted,
        )

    def is_genuinely_new(self, item: ListingItem) -> bool:
        """Перевіряє, чи оголошення справді нове (без старих піднятих у ТОП)."""
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
        limit: int = 40,
        max_retries: int = 3,
        proxy: Optional[str] = None,
    ) -> list[ListingItem]:
        """
        Виконує повний скрапінг OLX API з автоматичним відсіюванням ріелторів,
        збагаченням повними параметрами, перевіркою акаунтів та отриманням телефонів.
        """
        params: dict[str, Any] = {
            "category_id": settings.CATEGORY_ID,
            "city_id": settings.CITY_ID,
            "filter_enum_number_of_rooms_string[0]": "odnokomnatnye",
            "sort_by": "created_at:desc",
            "limit": limit,
        }

        # Фільтр тільки від власників на рівні запиту
        if settings.ONLY_OWNERS:
            params["owner_type"] = "private"

        if settings.MIN_PRICE is not None:
            params["filter_float_price:from"] = settings.MIN_PRICE
        if settings.MAX_PRICE is not None:
            params["filter_float_price:to"] = settings.MAX_PRICE

        client_kwargs: dict[str, Any] = {
            "follow_redirects": True,
            "timeout": self.timeout,
        }
        active_proxy = proxy or self.proxy
        if active_proxy:
            client_kwargs["proxy"] = active_proxy
            client_kwargs["verify"] = False
            use_http2 = False
        else:
            use_http2 = True

        last_error: Optional[str] = None
        last_status: Optional[int] = None

        for attempt in range(1, max_retries + 1):
            try:
                async with httpx.AsyncClient(http2=use_http2, **client_kwargs) as client:
                    response = await client.get(
                        self.API_URL,
                        params=params,
                        headers=self.headers,
                    )
                    last_status = response.status_code

                    if response.status_code == 200:
                        data = response.json()
                        raw_items = data.get("data", []) if isinstance(data, dict) else []
                        logger.info("Retrieved %d raw offers from OLX API (owner_type=private)", len(raw_items))

                        items: list[ListingItem] = []
                        for raw in raw_items:
                            if isinstance(raw, dict):
                                item = await self._parse_item_full(client, raw, proxy=active_proxy)
                                if item:
                                    items.append(item)
                                    # Коротка пауза між запитами деталей для уникнення RateLimit
                                    await asyncio.sleep(0.05)

                        logger.info(
                            "Filtering complete: %d verified OWNER listings obtained out of %d raw",
                            len(items),
                            len(raw_items),
                        )
                        return items

                    logger.warning(
                        "OLX API returned HTTP %d on attempt %d/%d (proxy: %s)",
                        response.status_code,
                        attempt,
                        max_retries,
                        _mask_proxy_str(active_proxy),
                    )
                    if response.status_code in (403, 429):
                        last_error = f"HTTP {response.status_code} (Заблоковано або RateLimited OLX)"
                        break
                    else:
                        last_error = f"HTTP {response.status_code}"

            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "Network error on attempt %d/%d for OLX API (proxy: %s): %s",
                    attempt,
                    max_retries,
                    _mask_proxy_str(active_proxy),
                    exc,
                )
                if use_http2:
                    use_http2 = False

            if attempt < max_retries:
                await asyncio.sleep(1.5 * attempt)

        proxy_info = _mask_proxy_str(active_proxy) if active_proxy else "Direct"
        error_detail = last_error or (f"HTTP {last_status}" if last_status else "Невідома помилка")
        logger.error(
            "Failed to fetch listings from OLX API after %d attempts (proxy: %s): %s",
            max_retries,
            proxy_info,
            error_detail,
        )
        raise RuntimeError(f"OLX API request failed ({proxy_info}): {error_detail}")

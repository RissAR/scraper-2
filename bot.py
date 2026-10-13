import asyncio
import html
import logging
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from config import settings
from database import DatabaseManager
from dimria_scraper import DimRiaScraper
from exporter import export_listings_to_excel
from keyboards import (
    access_request_admin_keyboard,
    admin_main_keyboard,
    back_to_menu_keyboard,
    back_to_realtor_keyboard,
    cancel_fsm_keyboard,
    cookies_menu_keyboard,
    main_menu_keyboard,
    new_listings_notification_keyboard,
    pagination_keyboard,
    proxy_delete_keyboard,
    proxy_menu_keyboard,
    realtor_main_keyboard,
    unauthorized_keyboard,
    user_detail_keyboard,
    users_menu_keyboard,
)
from models import ListingItem
from olx_scraper import OLXScraper
from proxy_checker import check_proxy_health

Path("data").mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("data/bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("rieltor_bot")

bot = Bot(token=settings.BOT_TOKEN)
dp = Dispatcher()
db = DatabaseManager()
olx_scraper = OLXScraper()
dimria_scraper = DimRiaScraper()
scraper = olx_scraper


class ProxyStates(StatesGroup):
    waiting_for_proxy_url = State()


class CookieStates(StatesGroup):
    waiting_for_cookies = State()


# --- АВТОМАТИЧНИЙ ЗАПОБІЖНИК ТА АЛЕРТИ (CIRCUIT BREAKER & WATCHDOG) ---


class ScraperCircuitBreaker:
    """
    Автоматичний запобіжник для парсерів (Circuit Breaker).
    Фіксує послідовні збої, переводить скрапер у тимчасовий кулдаун (15 хв)
    та відстежує стан надсилання сповіщень адміністратору.
    """

    def __init__(self, name: str, cooldown_minutes: int = 15, max_errors: int = 3) -> None:
        self.name = name
        self.cooldown_minutes = cooldown_minutes
        self.max_errors = max_errors
        self.consecutive_errors: int = 0
        self.cooldown_until: Optional[datetime] = None
        self.alert_sent: bool = False
        self.last_error: str = ""

    def is_in_cooldown(self) -> bool:
        if self.cooldown_until:
            now = datetime.now(timezone.utc)
            if now < self.cooldown_until:
                return True
            self.cooldown_until = None
        return False

    def remaining_cooldown_minutes(self) -> int:
        if self.cooldown_until:
            diff = (self.cooldown_until - datetime.now(timezone.utc)).total_seconds()
            return max(1, int(diff / 60))
        return 0

    def record_success(self) -> bool:
        """Скидає лічильник помилок. Повертає True, якщо попередньо надсилався алерт про збій."""
        was_alerted = self.alert_sent
        self.consecutive_errors = 0
        self.cooldown_until = None
        self.alert_sent = False
        self.last_error = ""
        return was_alerted

    def record_error(self, error_desc: str) -> bool:
        """Реєструє критичну помилку. Повертає True, якщо досягнуто ліміт помилок і треба надіслати алерт."""
        self.consecutive_errors += 1
        self.last_error = str(error_desc)
        if self.consecutive_errors >= self.max_errors:
            self.cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=self.cooldown_minutes)
            if not self.alert_sent:
                self.alert_sent = True
                return True
        return False


olx_circuit_breaker = ScraperCircuitBreaker("OLX.ua")
dimria_circuit_breaker = ScraperCircuitBreaker("DIM.RIA")


async def notify_admins(bot_instance: Bot, message: str) -> None:
    """Надсилає системне сповіщення всім активним адміністраторам."""
    admin_ids = set(settings.admin_id_list)
    try:
        users = await db.list_users()
        for u in users:
            if u.get("role") == "admin" and u.get("is_active"):
                admin_ids.add(u["telegram_id"])
    except Exception:
        pass

    for aid in admin_ids:
        try:
            await bot_instance.send_message(
                chat_id=aid,
                text=message,
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logger.error("Failed sending admin alert to %d: %s", aid, exc)


# --- ДОПОМІЖНІ ФУНКЦІЇ БЕЗПЕКИ ТА ФОРМАТУВАННЯ ---


async def is_user_admin(user_id: int) -> bool:
    """Перевіряє, чи має користувач права адміністратора."""
    if settings.is_admin(user_id):
        return True
    role = await db.get_user_role(user_id)
    return role == "admin"


def mask_proxy(url: str) -> str:
    """Маскує облікові дані в URL проксі для безпечного відображення."""
    if "@" in url:
        prefix, host_part = url.split("@", 1)
        scheme = prefix.split("://")[0] + "://" if "://" in prefix else ""
        return f"{scheme}***:***@{host_part}"
    return url


def normalize_proxy_url(raw: str) -> str:
    """Нормалізує введену адресу проксі."""
    text = raw.strip()
    if not text:
        return ""
    # Якщо користувач ввів host:port:user:pass
    parts = text.split(":")
    if len(parts) == 4 and not text.startswith("http") and not text.startswith("socks"):
        host, port, user, password = parts
        return f"http://{user}:{password}@{host}:{port}"
    if not (
        text.startswith("http://")
        or text.startswith("https://")
        or text.startswith("socks5://")
        or text.startswith("socks4://")
    ):
        return f"http://{text}"
    return text


def format_listing_message(item: ListingItem) -> str:
    """Форматування тексту повідомлення про оголошення від власника для стрічки."""
    safe_title = html.escape(item.title)
    safe_location = html.escape(item.location)
    safe_price = html.escape(item.price_label or f"{int(item.price)} {item.currency}")
    safe_source = html.escape(item.source)
    author_info = html.escape(
        f"{item.author_name} ({item.author_ads_count} ог.)"
        if item.author_name
        else "Власник"
    )

    phone_str = (
        html.escape(item.phone)
        if item.phone
        else "Не розкрито автоматично (перегляньте за посиланням нижче)"
    )
    floor_info = (
        f"{item.floor}/{item.total_floors} пов."
        if item.floor and item.total_floors
        else (f"{item.floor} пов." if item.floor else None)
    )
    params_parts = []
    if floor_info:
        params_parts.append(floor_info)
    if item.total_area:
        params_parts.append(f"площа {item.total_area}")
    params_str = ", ".join(params_parts) if params_parts else "1 кімната"

    msg_lines = [
        f"🏠 <b>{safe_title}</b>",
        "",
        f"💰 <b>Ціна:</b> {safe_price}",
        f"📍 <b>Локація:</b> {safe_location}",
        f"👤 <b>Власник:</b> {author_info}",
        f"📞 <b>Телефон:</b> {phone_str}",
        f"📐 <b>Параметри:</b> {html.escape(params_str)}",
        f"📌 <b>Джерело:</b> {safe_source}",
        "",
        f'🔗 <a href="{item.url}">Переглянути на {safe_source}</a>',
    ]
    return "\n".join(msg_lines)


def format_dict_listing_message(item: dict) -> str:
    """Форматування оголошення зі словника (з БД)."""
    title = html.escape(item.get("title", "Без назви"))
    price = item.get("price", 0)
    currency = item.get("currency", "грн")
    district = html.escape(item.get("district") or "Не вказано")
    url = item.get("url", "")
    source = html.escape(item.get("source", "OLX.ua"))
    author = html.escape(item.get("author_name") or "Власник")
    ads_cnt = item.get("author_ads_count", 1)
    raw_phone = item.get("phone")
    phone = (
        html.escape(raw_phone)
        if raw_phone
        else "Не розкрито автоматично (перегляньте за посиланням нижче)"
    )

    floor = item.get("floor")
    total_floors = item.get("total_floors")
    area = item.get("total_area")
    floor_str = f"{floor}/{total_floors} пов." if floor and total_floors else ""

    return (
        f"🏠 <b>{title}</b>\n\n"
        f"💰 <b>Ціна:</b> {price:,.0f} {currency}\n"
        f"📍 <b>Район:</b> {district}\n"
        f"👤 <b>Власник:</b> {author} ({ads_cnt} ог.)\n"
        f"📞 <b>Телефон:</b> {phone}\n"
        f"📐 <b>Параметри:</b> {floor_str} {area or ''}\n"
        f"📌 <b>Джерело:</b> {source}\n\n"
        f'🔗 <a href="{url}">Переглянути оголошення</a>'
    )


async def send_listing_card(chat_id: int, item: ListingItem) -> bool:
    """Надсилає окрему картку оголошення у стрічку (з фото або текстом)."""
    caption = format_listing_message(item)
    try:
        if item.image_url:
            try:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=item.image_url,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                )
                return True
            except TelegramAPIError as exc:
                logger.warning(
                    "Failed photo send for %s: %s. Using text fallback.", item.id, exc
                )

        await bot.send_message(
            chat_id=chat_id,
            text=caption,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )
        return True
    except Exception as exc:
        logger.error("Failed sending card to %d: %s", chat_id, exc)
        return False


# --- ФОНОВИЙ МОНІТОРИНГ ТА РОЗДІЛЬНІ ПАРСЕРИ ---


async def check_for_new_listings(manual_trigger_chat_id: Optional[int] = None) -> int:
    """
    Один цикл перевірки наявності нових оголошень на OLX.ua та DIM.RIA з урахуванням роздільних станів.
    При знаходженні нових об'єктів надсилає сповіщення з кнопкою 'Показати об'єкти'.
    """
    parsers = await db.get_parsers_state()
    olx_active = parsers.get("olx", True)
    dimria_active = parsers.get("dimria", True)

    if manual_trigger_chat_id is None and not olx_active and not dimria_active:
        logger.debug("Both parsers are paused. Skipping scheduled check.")
        return 0

    sources = []
    if olx_active or manual_trigger_chat_id is not None:
        sources.append("OLX.ua")
    if (dimria_active or manual_trigger_chat_id is not None) and settings.DIMRIA_ENABLED:
        sources.append("DIM.RIA")

    sources_str = " + ".join(sources) if sources else "None"
    logger.info("Starting check cycle (%s)...", sources_str)

    working_proxies = await db.get_active_proxies()
    active_proxy = random.choice(working_proxies) if working_proxies else None

    collected_items: list[ListingItem] = []

    # 1. Опитування OLX.ua з автоматичним Circuit Breaker та ротацією проксі
    if olx_active or manual_trigger_chat_id is not None:
        if manual_trigger_chat_id is None and olx_circuit_breaker.is_in_cooldown():
            logger.warning(
                "OLX Scraper is in cooldown (%d min remaining). Skipping scheduled check.",
                olx_circuit_breaker.remaining_cooldown_minutes(),
            )
        else:
            olx_items = None
            candidate_proxies = [active_proxy] if active_proxy else []
            if working_proxies:
                for wp in working_proxies:
                    if wp not in candidate_proxies:
                        candidate_proxies.append(wp)
            if not candidate_proxies:
                candidate_proxies = [None]

            last_olx_err = None
            for p_candidate in candidate_proxies:
                try:
                    olx_items = await olx_scraper.fetch_listings(proxy=p_candidate)
                    break
                except Exception as exc:
                    last_olx_err = exc
                    logger.warning("OLX request with proxy %s failed: %s", mask_proxy(str(p_candidate)), exc)
                    if len(candidate_proxies) > 1:
                        await asyncio.sleep(1.0)

            if olx_items is not None:
                collected_items.extend(olx_items)
                if olx_circuit_breaker.record_success():
                    await notify_admins(
                        bot,
                        "✅ <b>РОБОТУ ВІДНОВЛЕНО:</b> Скрапер OLX.ua знову успішно отримує дані.",
                    )
            else:
                err_msg = str(last_olx_err or "Помилка відповіді API")
                logger.error("OLX Scraper encountered an exception: %s", err_msg)
                should_alert = olx_circuit_breaker.record_error(err_msg)
                if should_alert:
                    alert_text = (
                        f"⚠️ <b>УВАГА: Скрапер OLX.ua зафіксував 3 збої поспіль!</b>\n\n"
                        f"Помилка: <code>{html.escape(err_msg[:200])}</code>\n\n"
                        f"Скрапер тимчасово призупинено на 15 хв."
                    )
                    await notify_admins(bot, alert_text)

    # 2. Опитування DIM.RIA з автоматичним Circuit Breaker та ротацією проксі
    if (dimria_active or manual_trigger_chat_id is not None) and settings.DIMRIA_ENABLED:
        if manual_trigger_chat_id is None and dimria_circuit_breaker.is_in_cooldown():
            logger.warning(
                "DIM.RIA Scraper is in cooldown (%d min remaining). Skipping scheduled check.",
                dimria_circuit_breaker.remaining_cooldown_minutes(),
            )
        else:
            dimria_items = None
            candidate_proxies = [active_proxy] if active_proxy else []
            if working_proxies:
                for wp in working_proxies:
                    if wp not in candidate_proxies:
                        candidate_proxies.append(wp)
            if not candidate_proxies:
                candidate_proxies = [None]

            last_dimria_err = None
            for p_candidate in candidate_proxies:
                try:
                    dimria_items = await dimria_scraper.fetch_listings(
                        proxy=p_candidate,
                        seen_checker=db.is_listing_seen,
                    )
                    break
                except Exception as exc:
                    last_dimria_err = exc
                    logger.warning("DIM.RIA request with proxy %s failed: %s", mask_proxy(str(p_candidate)), exc)
                    if len(candidate_proxies) > 1:
                        await asyncio.sleep(1.0)

            if dimria_items is not None:
                collected_items.extend(dimria_items)
                if dimria_circuit_breaker.record_success():
                    await notify_admins(
                        bot,
                        "✅ <b>РОБОТУ ВІДНОВЛЕНО:</b> Скрапер DIM.RIA знову успішно отримує дані.",
                    )
            else:
                err_msg = str(last_dimria_err or "Помилка відповіді API")
                logger.error("DIM.RIA Scraper encountered an exception: %s", err_msg)
                should_alert = dimria_circuit_breaker.record_error(err_msg)
                if should_alert:
                    alert_text = (
                        f"⚠️ <b>УВАГА: Скрапер DIM.RIA зафіксував 3 збої поспіль!</b>\n\n"
                        f"Помилка: <code>{html.escape(err_msg[:200])}</code>\n\n"
                        f"Скрапер тимчасово призупинено на 15 хв."
                    )
                    await notify_admins(bot, alert_text)

    if not collected_items:
        logger.info("No items retrieved in current cycle.")
        return 0

    new_items: list[ListingItem] = []

    for item in reversed(collected_items):
        if await db.is_listing_seen(item.id):
            continue

        is_new = (
            dimria_scraper.is_genuinely_new(item)
            if item.source == "DIM.RIA"
            else olx_scraper.is_genuinely_new(item)
        )
        if not is_new:
            await db.mark_listing_seen(item, is_sent=False)
            continue

        await db.mark_listing_seen(item, is_sent=True)
        new_items.append(item)
        logger.info(
            "New listing registered: [%s] %s (%s)", item.id, item.title, item.price_label
        )

    new_count = len(new_items)
    logger.info("Check finished. New listings detected: %d", new_count)

    active_recipients = await db.get_active_recipients()
    configured_chats = settings.target_chat_ids + settings.admin_id_list
    recipients = sorted(list(set(active_recipients + configured_chats)))
    if manual_trigger_chat_id and manual_trigger_chat_id not in recipients:
        recipients.append(manual_trigger_chat_id)

    # Якщо знайдено нові об'єкти, надсилаємо сповіщення
    if new_count > 0 and recipients:
        notify_text = (
            f"✅ <b>Парсинг завершено успішно, об'єктів додано: {new_count}</b>\n\n"
            f"Натисніть кнопку нижче, щоб вивантажити нові оголошення стрічкою."
        )
        kb = new_listings_notification_keyboard(new_count)

        for cid in recipients:
            try:
                await bot.send_message(
                    chat_id=cid,
                    text=notify_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                )
            except TelegramForbiddenError:
                await db.set_user_status(cid, False)
                await db.remove_subscriber(cid)
            except Exception as err:
                logger.error("Failed sending notification to %d: %s", cid, err)

    return new_count


async def periodic_scanner_loop() -> None:
    """Фонова задача періодичного опитування OLX та DIM.RIA у реальному часі."""
    logger.info(
        "Background scanner loop initialized (interval: %d s).",
        settings.POLL_INTERVAL_SECONDS,
    )
    while True:
        try:
            await check_for_new_listings()
        except asyncio.CancelledError:
            logger.info("Background scanner loop cancelled.")
            break
        except Exception as exc:
            logger.exception("Unexpected error in scanner loop: %s", exc)

        await asyncio.sleep(settings.POLL_INTERVAL_SECONDS)


# --- ТЕКСТИ МЕНЮ ТА КАБІНЕТІВ ---


async def get_admin_menu_text() -> str:
    """Генерує актуальний текст головного меню адміністратора."""
    parsers = await db.get_parsers_state()
    olx_act = parsers.get("olx", True)
    dimria_act = parsers.get("dimria", True)

    olx_icon = "🟢 АКТИВНИЙ" if olx_act else "🔴 ЗУПИНЕНО"
    dimria_icon = "🟢 АКТИВНИЙ" if dimria_act else "🔴 ЗУПИНЕНО"

    stats = await db.get_stats()
    users = await db.list_users()
    realtors_cnt = sum(
        1 for u in users if u.get("role") == "realtor" and u.get("is_active")
    )
    pending_cnt = sum(1 for u in users if u.get("role") == "pending")

    excluded_str = (
        ", ".join(settings.excluded_districts)
        if settings.excluded_districts
        else "Усі райони"
    )
    price_str = (
        f"{settings.MIN_PRICE or 0:,.0f} - {settings.MAX_PRICE or '∞'} грн"
        if (settings.MIN_PRICE or settings.MAX_PRICE)
        else "Будь-яка"
    )

    return (
        f"🏢 <b>Панель управління RieltorBot (Адміністратор)</b>\n\n"
        f"• <b>OLX.ua парсер:</b> {olx_icon}\n"
        f"• <b>DIM.RIA парсер:</b> {dimria_icon}\n"
        f"• <b>Місто:</b> {settings.CITY_NAME} (1-кімн. квартири)\n"
        f"• <b>Фільтр:</b> 🛡️ Тільки від ВЛАСНИКІВ\n"
        f"• <b>Ціна:</b> {price_str}\n"
        f"• <b>Райони:</b> {html.escape(excluded_str)}\n"
        f"• <b>Інтервал:</b> {settings.POLL_INTERVAL_SECONDS // 60} хв ({settings.POLL_INTERVAL_SECONDS} с)\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"• Відстежено оголошень: <b>{stats['total_seen']}</b>\n"
        f"• Сповіщень відправлено: <b>{stats['total_sent']}</b>\n"
        f"• Користувачі: <b>{len(users)}</b> (активних ріелторів: {realtors_cnt}, очікують: {pending_cnt})\n"
        f"• Проксі в базі: <b>{stats['proxies_count']}</b> (активних: {stats['active_proxies_count']})\n\n"
        f"Оберіть потрібну дію нижче:"
    )


def get_realtor_menu_text(full_name: str) -> str:
    """Генерує вітальний текст Кабінету Ріелтора."""
    greeting = f", {html.escape(full_name)}" if full_name else ""
    return (
        f"🏠 <b>Кабінет Ріелтора — RieltorBot</b>\n\n"
        f"Вітаємо{greeting}!\n\n"
        f"Тут відображаються актуальні 1-кімнатні квартири в оренду в м. {settings.CITY_NAME} "
        f"<b>виключно від прямих власників</b>.\n\n"
        f"• <b>Оновлення бази:</b> кожні 2–4 хвилини у фоні\n"
        f"• <b>Джерела:</b> OLX.ua та DIM.RIA\n"
        f"• <b>Фільтрація:</b> автоблокування посередників та ріелторів\n\n"
        f"Оберіть дію нижче:"
    )


async def get_proxy_menu_text() -> tuple[str, bool]:
    """Генерує текст меню керування проксі."""
    proxies = await db.get_all_proxies()
    if not proxies:
        text = (
            "🌐 <b>Меню управління проксі</b>\n\n"
            "Наразі немає збережених проксі-серверів.\n"
            "Бот використовує пряме з'єднання або налаштування з .env.\n\n"
            "Натисніть <b>➕ Додати проксі</b>, щоб внести новий проксі."
        )
        return text, False

    lines = ["🌐 <b>Список та статус проксі:</b>\n"]
    for i, p in enumerate(proxies, start=1):
        status = p.get("status", "unknown")
        resp_ms = p.get("response_time_ms", 0.0)

        if status == "active":
            badge = f"🟢 Працює ({resp_ms} ms)"
        elif status == "unknown":
            badge = "⚪ Не перевірено"
        else:
            badge = f"🔴 {status}"

        masked_url = mask_proxy(p["url"])
        lines.append(f"{i}. <code>{html.escape(masked_url)}</code>\n   Статус: {badge}")

    lines.append("\nНатисніть <b>🔄 Перевірити всі проксі</b> для діагностики.")
    return "\n".join(lines), True


# --- ОБРОБНИКИ КОМАНД (/start, /menu) ---


@dp.message(CommandStart())
async def handle_start(message: Message, state: FSMContext) -> None:
    """Команда /start: перевірка прав, реєстрація та показ відповідного кабінету."""
    await state.clear()
    user_id = message.from_user.id if message.from_user else message.chat.id
    username = (message.from_user.username or "") if message.from_user else ""
    full_name = (message.from_user.full_name or "") if message.from_user else ""

    # Перевіряємо статус адміна
    if settings.is_admin(user_id):
        await db.add_user(
            telegram_id=user_id,
            username=username,
            full_name=full_name,
            role="admin",
            is_active=True,
        )
        parsers = await db.get_parsers_state()
        kb = admin_main_keyboard(parsers.get("olx", True), parsers.get("dimria", True))
        text = await get_admin_menu_text()
        await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    # Перевіряємо статус користувача в базі
    user = await db.get_user(user_id)
    if not user:
        text = (
            "🔒 <b>Доступ закрито</b>\n\n"
            "Цей бот призначений виключно для авторизованих ріелторів та адміністраторів.\n\n"
            "Натисніть кнопку нижче, щоб надіслати запит на отримання доступу:"
        )
        await message.answer(
            text, parse_mode=ParseMode.HTML, reply_markup=unauthorized_keyboard(user_id)
        )
        return

    role = user.get("role", "pending")
    is_active = bool(user.get("is_active", 1))

    if not is_active:
        await message.answer(
            "⛔️ <b>Ваш акаунт деактивовано або заблоковано адміністратором.</b>\n"
            "Для відновлення зверніться до технічної підтримки.",
            parse_mode=ParseMode.HTML,
        )
        return

    if role == "admin":
        parsers = await db.get_parsers_state()
        kb = admin_main_keyboard(parsers.get("olx", True), parsers.get("dimria", True))
        text = await get_admin_menu_text()
        await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    elif role == "realtor":
        text = get_realtor_menu_text(full_name)
        await message.answer(
            text, parse_mode=ParseMode.HTML, reply_markup=realtor_main_keyboard()
        )
    else:  # 'pending'
        await message.answer(
            "⏳ <b>Ваш запит на доступ очікує розгляду адміністратора.</b>\n\n"
            "Як тільки доступ буде схвалено, ви отримаєте сповіщення.",
            parse_mode=ParseMode.HTML,
        )


@dp.message(Command("menu"))
async def handle_menu_command(message: Message, state: FSMContext) -> None:
    """Команда /menu: відкриває меню відповідно до ролі користувача."""
    await state.clear()
    user_id = message.from_user.id if message.from_user else message.chat.id
    if await is_user_admin(user_id):
        parsers = await db.get_parsers_state()
        kb = admin_main_keyboard(parsers.get("olx", True), parsers.get("dimria", True))
        text = await get_admin_menu_text()
        await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    user = await db.get_user(user_id)
    if user and user.get("role") == "realtor" and user.get("is_active"):
        full_name = message.from_user.full_name if message.from_user else ""
        text = get_realtor_menu_text(full_name)
        await message.answer(
            text, parse_mode=ParseMode.HTML, reply_markup=realtor_main_keyboard()
        )
    else:
        await message.answer(
            "🔒 <b>Доступ закрито.</b> Натисніть /start для реєстрації або перевірки статусу.",
            parse_mode=ParseMode.HTML,
        )


# --- ПЕРЕМИКАННЯ СТАНУ ПАРСЕРІВ (АДМІН) ---


@dp.callback_query(F.data == "toggle_olx")
async def handle_toggle_olx(callback: CallbackQuery) -> None:
    """Перемикання стану парсера OLX.ua."""
    if not await is_user_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ заборонено.", show_alert=True)
        return

    parsers = await db.get_parsers_state()
    curr_olx = parsers.get("olx", True)
    new_olx = not curr_olx
    await db.set_parser_state("olx", new_olx)

    status_str = "активовано 🟢" if new_olx else "зупинено 🔴"
    await callback.answer(f"OLX.ua парсер {status_str}!", show_alert=False)

    text = await get_admin_menu_text()
    kb = admin_main_keyboard(is_olx=new_olx, is_dimria=parsers.get("dimria", True))
    if callback.message:
        await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


@dp.callback_query(F.data == "toggle_dimria")
async def handle_toggle_dimria(callback: CallbackQuery) -> None:
    """Перемикання стану парсера DIM.RIA."""
    if not await is_user_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ заборонено.", show_alert=True)
        return

    parsers = await db.get_parsers_state()
    curr_dimria = parsers.get("dimria", True)
    new_dimria = not curr_dimria
    await db.set_parser_state("dimria", new_dimria)

    status_str = "активовано 🟢" if new_dimria else "зупинено 🔴"
    await callback.answer(f"DIM.RIA парсер {status_str}!", show_alert=False)

    text = await get_admin_menu_text()
    kb = admin_main_keyboard(is_olx=parsers.get("olx", True), is_dimria=new_dimria)
    if callback.message:
        await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


@dp.callback_query(F.data == "toggle_all")
async def handle_toggle_all(callback: CallbackQuery) -> None:
    """Перемикання стану всіх парсерів одночасно."""
    if not await is_user_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ заборонено.", show_alert=True)
        return

    parsers = await db.get_parsers_state()
    both_active = parsers.get("olx", True) and parsers.get("dimria", True)
    new_state = not both_active
    await db.set_all_parsers_state(new_state)

    status_str = "активовано 🟢" if new_state else "зупинено 🔴"
    await callback.answer(f"Усі парсери {status_str}!", show_alert=False)

    text = await get_admin_menu_text()
    kb = admin_main_keyboard(is_olx=new_state, is_dimria=new_state)
    if callback.message:
        await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


# --- РАЗОВІ ТЕСТИ ПАРСЕРІВ (1 КОЛО) ---


@dp.callback_query(F.data == "test_olx_now")
async def handle_test_olx_now(callback: CallbackQuery) -> None:
    """Разове тестове коло OLX для швидкої діагностики."""
    if not await is_user_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ заборонено.", show_alert=True)
        return

    await callback.answer("🔍 Запускаю тестовий цикл OLX...")
    if callback.message:
        await callback.message.edit_text(
            "⏳ <b>Виконується тестовий запит до OLX.ua (1 коло)...</b>\n"
            "<i>Опитування першої сторінки та перевірка фільтрації власників.</i>",
            parse_mode=ParseMode.HTML,
        )

    working_proxies = await db.get_working_proxies()
    active_proxy = random.choice(working_proxies) if working_proxies else None

    try:
        items = await olx_scraper.fetch_listings(proxy=active_proxy, limit=20)
        new_count = 0
        for it in items:
            if not await db.is_listing_seen(it.id):
                new_count += 1

        proxy_str = mask_proxy(active_proxy) if active_proxy else "Пряме з'єднання"
        report_text = (
            "🔍 <b>Результати тесту OLX.ua (1 коло):</b>\n\n"
            f"• Статус запиту: 🟢 <b>Успішно</b>\n"
            f"• Знайдено від власників: <b>{len(items)}</b> об'єктів\n"
            f"• Нових (немає в базі): <b>{new_count}</b> об'єктів\n"
            f"• Використано проксі: <code>{html.escape(proxy_str)}</code>\n\n"
            "<i>Парсер готовий до автоматичної роботи.</i>"
        )
    except Exception as exc:
        logger.exception("Test OLX failed: %s", exc)
        report_text = (
            "❌ <b>Помилка під час тесту OLX.ua!</b>\n\n"
            f"Деталі помилки: <code>{html.escape(str(exc))}</code>"
        )

    if callback.message:
        await callback.message.edit_text(
            report_text,
            parse_mode=ParseMode.HTML,
            reply_markup=back_to_menu_keyboard(),
        )


@dp.callback_query(F.data == "test_dimria_now")
async def handle_test_dimria_now(callback: CallbackQuery) -> None:
    """Разове тестове коло DIM.RIA для швидкої діагностики."""
    if not await is_user_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ заборонено.", show_alert=True)
        return

    await callback.answer("🔍 Запускаю тестовий цикл DIM.RIA...")
    if callback.message:
        await callback.message.edit_text(
            "⏳ <b>Виконується тестовий запит до DIM.RIA (1 коло)...</b>\n"
            "<i>Опитування searchEngine/v2 та перевірка карток власників.</i>",
            parse_mode=ParseMode.HTML,
        )

    working_proxies = await db.get_working_proxies()
    active_proxy = random.choice(working_proxies) if working_proxies else None

    try:
        items = await dimria_scraper.fetch_listings(
            proxy=active_proxy,
            limit=10,
            seen_checker=db.is_listing_seen,
        )
        new_count = 0
        for it in items:
            if not await db.is_listing_seen(it.id):
                new_count += 1

        proxy_str = mask_proxy(active_proxy) if active_proxy else "Пряме з'єднання"
        report_text = (
            "🔍 <b>Результати тесту DIM.RIA (1 коло):</b>\n\n"
            f"• Статус запиту: 🟢 <b>Успішно</b>\n"
            f"• Знайдено від власників: <b>{len(items)}</b> об'єктів\n"
            f"• Нових (немає в базі): <b>{new_count}</b> об'єктів\n"
            f"• Використано проксі: <code>{html.escape(proxy_str)}</code>\n\n"
            "<i>Парсер готовий до автоматичної роботи.</i>"
        )
    except Exception as exc:
        logger.exception("Test DIM.RIA failed: %s", exc)
        report_text = (
            "❌ <b>Помилка під час тесту DIM.RIA!</b>\n\n"
            f"Деталі помилки: <code>{html.escape(str(exc))}</code>"
        )

    if callback.message:
        await callback.message.edit_text(
            report_text,
            parse_mode=ParseMode.HTML,
            reply_markup=back_to_menu_keyboard(),
        )


# --- СИСТЕМА ДОСТУПУ (RBAC) ТА КЕРУВАННЯ КОРИСТУВАЧАМИ ---


@dp.callback_query(F.data.startswith("request_access:"))
async def handle_request_access(callback: CallbackQuery) -> None:
    """Подача запиту на доступ від неавторизованого користувача."""
    user_id = callback.from_user.id
    username = callback.from_user.username or ""
    full_name = callback.from_user.full_name or ""

    user = await db.get_user(user_id)
    if user and user.get("role") == "realtor" and user.get("is_active"):
        await callback.answer("Ви вже маєте доступ!", show_alert=True)
        return

    await db.add_user(
        telegram_id=user_id,
        username=username,
        full_name=full_name,
        role="pending",
        is_active=False,
    )
    await callback.answer("📨 Запит надіслано адміністратору!", show_alert=False)

    if callback.message:
        await callback.message.edit_text(
            "⏳ <b>Ваш запит успішно надіслано адміністраторам!</b>\n\n"
            "Будь ласка, зачекайте на схвалення доступу. Після підтвердження вам надійде сповіщення.",
            parse_mode=ParseMode.HTML,
        )

    # Сповіщаємо всіх адміністраторів
    admin_notify_text = (
        f"🔔 <b>Новий запит на доступ до RieltorBot!</b>\n\n"
        f"• <b>Ім'я:</b> {html.escape(full_name)}\n"
        f"• <b>Username:</b> @{html.escape(username) if username else 'немає'}\n"
        f"• <b>Telegram ID:</b> <code>{user_id}</code>\n\n"
        f"Оберіть рішення для цього користувача:"
    )
    for admin_id in settings.admin_id_list:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=admin_notify_text,
                parse_mode=ParseMode.HTML,
                reply_markup=access_request_admin_keyboard(user_id),
            )
        except Exception as err:
            logger.warning("Failed notifying admin %d: %s", admin_id, err)


@dp.callback_query(F.data.startswith("approve_user:"))
async def handle_approve_user(callback: CallbackQuery) -> None:
    """Схвалення запиту користувача адміністратором."""
    if not await is_user_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ заборонено.", show_alert=True)
        return

    try:
        target_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Помилка ідентифікатора.", show_alert=True)
        return

    await db.set_user_role(target_id, "realtor")
    await db.set_user_status(target_id, True)
    await callback.answer("✅ Доступ для користувача активовано!", show_alert=False)

    if callback.message:
        await callback.message.edit_text(
            f"✅ <b>Користувача <code>{target_id}</code> схвалено як Ріелтора!</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=back_to_menu_keyboard(),
        )

    # Надсилаємо сповіщення користувачеві
    try:
        await bot.send_message(
            chat_id=target_id,
            text=(
                "🎉 <b>Ваш доступ до RieltorBot схвалено!</b>\n\n"
                "Тепер ви маєте доступ до бази перевірених квартир від власників у Дніпрі.\n"
                "Натисніть /start для переходу до кабінету ріелтора:"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=realtor_main_keyboard(),
        )
    except Exception as err:
        logger.warning("Could not notify approved user %d: %s", target_id, err)


@dp.callback_query(F.data.startswith("reject_user:"))
async def handle_reject_user(callback: CallbackQuery) -> None:
    """Відхилення запиту користувача адміністратором."""
    if not await is_user_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ заборонено.", show_alert=True)
        return

    try:
        target_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Помилка ідентифікатора.", show_alert=True)
        return

    await db.set_user_status(target_id, False)
    await db.set_user_role(target_id, "rejected")
    await callback.answer("❌ Запит відхилено.", show_alert=False)

    if callback.message:
        await callback.message.edit_text(
            f"❌ <b>Запит користувача <code>{target_id}</code> відхилено.</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=back_to_menu_keyboard(),
        )

    try:
        await bot.send_message(
            chat_id=target_id,
            text="❌ <b>Ваш запит на доступ до RieltorBot було відхилено.</b>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as err:
        logger.warning("Could not notify rejected user %d: %s", target_id, err)


@dp.callback_query(F.data == "users_menu")
async def handle_users_menu(callback: CallbackQuery) -> None:
    """Меню списку користувачів для адміністратора."""
    if not await is_user_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ заборонено.", show_alert=True)
        return

    users = await db.list_users()
    text = (
        "👥 <b>Керування користувачами та доступом</b>\n\n"
        f"Всього зареєстровано: <b>{len(users)}</b>\n\n"
        "Оберіть користувача зі списку для перегляду та зміни прав:"
    )
    if callback.message:
        await callback.message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=users_menu_keyboard(users),
        )
    await callback.answer()


@dp.callback_query(F.data.startswith("user_detail:"))
async def handle_user_detail(callback: CallbackQuery) -> None:
    """Детальна картка користувача та керування статусом."""
    if not await is_user_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ заборонено.", show_alert=True)
        return

    try:
        target_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Помилка ідентифікатора.", show_alert=True)
        return

    user = await db.get_user(target_id)
    if not user:
        await callback.answer("Користувача не знайдено.", show_alert=True)
        return

    u_name = user.get("full_name") or "Не вказано"
    u_username = f"@{user.get('username')}" if user.get("username") else "немає"
    u_role = user.get("role", "realtor")
    is_act = bool(user.get("is_active", 1))
    act_str = "🟢 Активний" if is_act else "🔴 Неактивний / Заблокований"
    created = str(user.get("created_at") or "")[:19].replace("T", " ")

    text = (
        f"👤 <b>Картка користувача:</b>\n\n"
        f"• <b>Telegram ID:</b> <code>{target_id}</code>\n"
        f"• <b>Ім'я:</b> {html.escape(u_name)}\n"
        f"• <b>Username:</b> {html.escape(u_username)}\n"
        f"• <b>Роль:</b> <code>{u_role}</code>\n"
        f"• <b>Статус:</b> {act_str}\n"
        f"• <b>Зареєстровано:</b> {created}\n"
    )

    if callback.message:
        await callback.message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=user_detail_keyboard(target_id, u_role, is_act),
        )
    await callback.answer()


@dp.callback_query(F.data.startswith("user_toggle_act:"))
async def handle_user_toggle_act(callback: CallbackQuery) -> None:
    """Перемикання активності користувача."""
    if not await is_user_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ заборонено.", show_alert=True)
        return

    try:
        target_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Помилка ідентифікатора.", show_alert=True)
        return

    user = await db.get_user(target_id)
    if not user:
        await callback.answer("Користувача не знайдено.", show_alert=True)
        return

    new_act = not bool(user.get("is_active", 1))
    await db.set_user_status(target_id, new_act)
    status_label = "активовано 🟢" if new_act else "деактивовано 🔴"
    await callback.answer(f"Користувача {status_label}", show_alert=False)

    user["is_active"] = 1 if new_act else 0
    u_name = user.get("full_name") or "Не вказано"
    u_username = f"@{user.get('username')}" if user.get("username") else "немає"
    u_role = user.get("role", "realtor")
    act_str = "🟢 Активний" if new_act else "🔴 Неактивний / Заблокований"
    created = str(user.get("created_at") or "")[:19].replace("T", " ")

    text = (
        f"👤 <b>Картка користувача:</b>\n\n"
        f"• <b>Telegram ID:</b> <code>{target_id}</code>\n"
        f"• <b>Ім'я:</b> {html.escape(u_name)}\n"
        f"• <b>Username:</b> {html.escape(u_username)}\n"
        f"• <b>Роль:</b> <code>{u_role}</code>\n"
        f"• <b>Статус:</b> {act_str}\n"
        f"• <b>Зареєстровано:</b> {created}\n"
    )
    if callback.message:
        await callback.message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=user_detail_keyboard(target_id, u_role, new_act),
        )


@dp.callback_query(F.data.startswith("make_admin:"))
async def handle_make_admin(callback: CallbackQuery) -> None:
    """Призначення користувача адміністратором."""
    if not await is_user_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ заборонено.", show_alert=True)
        return

    try:
        target_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Помилка ідентифікатора.", show_alert=True)
        return

    await db.set_user_role(target_id, "admin")
    await callback.answer("Користувача призначено адміністратором! 👑", show_alert=True)

    users = await db.list_users()
    if callback.message:
        await callback.message.edit_text(
            "👥 <b>Керування користувачами та доступом</b>\n\nОберіть користувача зі списку:",
            parse_mode=ParseMode.HTML,
            reply_markup=users_menu_keyboard(users),
        )


# --- КАБІНЕТ РІЕЛТОРА ---


@dp.callback_query(F.data == "realtor_status")
async def handle_realtor_status(callback: CallbackQuery) -> None:
    """Інформація про статус та підтримку для ріелтора."""
    text = (
        "ℹ️ <b>Мій статус та технічна підтримка</b>\n\n"
        "• <b>Ваш статус:</b> 🟢 Авторизований Ріелтор\n"
        "• <b>Режим моніторингу:</b> Постійний (2–4 хвилини)\n"
        "• <b>Джерела даних:</b> OLX.ua та DIM.RIA\n"
        "• <b>Фільтрація:</b> 100% перевірка власників, відсікання посередників\n\n"
        "💡 <i>Сповіщення про нові квартири надходять автоматично у цей чат.</i>\n\n"
        "З будь-яких технічних питань звертайтеся до адміністратора системи."
    )
    if callback.message:
        await callback.message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=back_to_realtor_keyboard(),
        )
    await callback.answer()


@dp.callback_query(F.data == "back_to_realtor")
async def handle_back_to_realtor(callback: CallbackQuery) -> None:
    """Повернення до головного меню Кабінету Ріелтора."""
    full_name = callback.from_user.full_name or ""
    text = get_realtor_menu_text(full_name)
    if callback.message:
        await callback.message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=realtor_main_keyboard(),
        )
    await callback.answer()


# --- ПЕРЕГЛЯД ОБ'ЄКТІВ У БАЗІ (ПАГІНАЦІЯ БЕЗ ЗАСМІЧЕННЯ) ---


@dp.callback_query(F.data.startswith("view_objects:"))
async def handle_view_objects(callback: CallbackQuery) -> None:
    """Компактний перегляд збережених об'єктів з пагінацією."""
    try:
        page_index = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        page_index = 0

    items, total = await db.get_listings(limit=1, offset=page_index)

    if total == 0 or not items:
        await callback.answer("У базі ще немає збережених об'єктів.", show_alert=True)
        return

    item = items[0]
    safe_title = html.escape(item.get("title", "Без назви"))
    price = item.get("price", 0)
    currency = item.get("currency", "грн")
    district = html.escape(item.get("district") or "Не вказано")
    url = item.get("url", "")
    source = html.escape(item.get("source", "OLX.ua"))
    created_val = str(item.get("created_time") or "")[:16].replace("T", " ")
    seen_val = str(item.get("seen_at") or "")[:16].replace("T", " ")

    author = html.escape(item.get("author_name") or "Власник")
    ads_cnt = item.get("author_ads_count", 1)
    phone = html.escape(item.get("phone") or "Не вказано / у чаті")
    floor = item.get("floor")
    total_floors = item.get("total_floors")
    area = item.get("total_area")
    floor_str = (
        f"{floor}/{total_floors} пов."
        if floor and total_floors
        else (f"{floor} пов." if floor else "-")
    )

    card_text = (
        f"📦 <b>Об'єкт #{page_index + 1} з {total} ({source})</b>\n\n"
        f"🏠 <b>{safe_title}</b>\n"
        f"💰 <b>Ціна:</b> {price:,.0f} {currency}\n"
        f"📍 <b>Район:</b> {district}\n"
        f"👤 <b>Автор:</b> {author} ({ads_cnt} активних ог.)\n"
        f"📞 <b>Телефон:</b> {phone}\n"
        f"📐 <b>Поверх / Площа:</b> {floor_str}, {area or '-'}\n"
        f"📅 <b>Опубліковано:</b> {created_val or 'Невідомо'}\n"
        f"🕒 <b>Зафіксовано в базі:</b> {seen_val}\n"
    )

    is_admin = await is_user_admin(callback.from_user.id)
    kb = pagination_keyboard(
        current_index=page_index, total_count=total, url=url, is_admin=is_admin
    )

    if callback.message:
        try:
            await callback.message.edit_text(
                card_text, parse_mode=ParseMode.HTML, reply_markup=kb
            )
        except Exception:
            pass
    await callback.answer()


@dp.callback_query(F.data == "noop")
async def handle_noop(callback: CallbackQuery) -> None:
    """Заглушка для неактивних кнопок пагінації."""
    await callback.answer()


# --- ВИВАНТАЖЕННЯ ОБ'ЄКТІВ СТРІЧКОЮ ПІСЛЯ СПОВІЩЕННЯ ---


@dp.callback_query(F.data.startswith("show_feed:"))
async def handle_show_feed(callback: CallbackQuery) -> None:
    """Вивантажує нещодавно додані об'єкти стрічкою після кліку на сповіщення."""
    try:
        count = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        count = 5

    await callback.answer("Вивантажую об'єкти стрічкою...")
    items = await db.get_recent_sent_listings(limit=min(count, 15))

    if not items:
        if callback.message:
            await callback.message.answer("Немає нових об'єктів для відображення.")
        return

    for it in reversed(items):
        msg_text = format_dict_listing_message(it)
        if callback.message:
            await callback.message.answer(
                msg_text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )
            await asyncio.sleep(0.1)

    is_admin = await is_user_admin(callback.from_user.id)
    back_kb = back_to_menu_keyboard() if is_admin else back_to_realtor_keyboard()

    if callback.message:
        await callback.message.answer(
            "✅ Усі нові об'єкти вивантажено.",
            reply_markup=back_kb,
        )


# --- ЕКСПОРТ В EXCEL (АДМІН) ---


@dp.callback_query(F.data == "export_excel")
async def handle_export_excel(callback: CallbackQuery) -> None:
    """Генерує та відправляє файл Excel з усіма об'єктами бази."""
    if not await is_user_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ заборонено.", show_alert=True)
        return

    await callback.answer("Генерую Excel файл...")
    items = await db.get_all_listings_for_export()

    if not items:
        await callback.answer("У базі даних немає записів для експорту.", show_alert=True)
        return

    excel_bytes = export_listings_to_excel(items)
    file = BufferedInputFile(excel_bytes, filename="rieltor_dnipro_listings.xlsx")

    if callback.message:
        await callback.message.answer_document(
            document=file,
            caption=(
                f"📊 <b>Експорт успішно сформовано!</b>\n\n"
                f"Всього об'єктів у таблиці: <b>{len(items)}</b>\n"
                f"Файл містить повні дані: ID, джерело, назва, ціна, телефон, район, посилання та дати."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=back_to_menu_keyboard(),
        )


# --- МЕНЮ ПРОКСІ (АДМІН) ---


@dp.callback_query(F.data == "proxy_menu")
@dp.callback_query(F.data == "back_to_proxy_menu")
async def handle_proxy_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Відображає меню керування проксі."""
    if not await is_user_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ заборонено.", show_alert=True)
        return

    await state.clear()
    text, has_proxies = await get_proxy_menu_text()
    if callback.message:
        await callback.message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=proxy_menu_keyboard(has_proxies=has_proxies),
        )
    await callback.answer()


@dp.callback_query(F.data == "proxy_check_all")
async def handle_proxy_check_all(callback: CallbackQuery) -> None:
    """Перевіряє працездатність та швидкість усіх збережених проксі."""
    if not await is_user_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ заборонено.", show_alert=True)
        return

    proxies = await db.get_all_proxies()
    if not proxies:
        await callback.answer("Немає проксі для перевірки.", show_alert=True)
        return

    await callback.answer("🔄 Перевіряю проксі, зачекайте кілька секунд...")
    if callback.message:
        await callback.message.edit_text(
            "⏳ <b>Виконується перевірка проксі через запити до OLX...</b>",
            parse_mode=ParseMode.HTML,
        )

    for p in proxies:
        is_ok, resp_ms, detail = await check_proxy_health(p["url"])
        status = "active" if is_ok else detail
        await db.update_proxy_status(
            proxy_id=p["id"], status=status, response_time_ms=resp_ms
        )

    text, has_proxies = await get_proxy_menu_text()
    if callback.message:
        await callback.message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=proxy_menu_keyboard(has_proxies=has_proxies),
        )


@dp.callback_query(F.data == "proxy_add_start")
async def handle_proxy_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Початок сценарію додавання проксі."""
    if not await is_user_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ заборонено.", show_alert=True)
        return

    await state.set_state(ProxyStates.waiting_for_proxy_url)
    text = (
        "➕ <b>Додавання нового проксі</b>\n\n"
        "Надішліть адресу проксі у будь-якому з наступних форматів:\n"
        "• <code>http://user:password@ip:port</code>\n"
        "• <code>socks5://user:password@ip:port</code>\n"
        "• <code>ip:port:user:password</code>\n"
        "• <code>http://ip:port</code>\n\n"
        "<i>Бот автоматично протестує проксі перед збереженням.</i>"
    )
    if callback.message:
        await callback.message.edit_text(
            text, parse_mode=ParseMode.HTML, reply_markup=cancel_fsm_keyboard()
        )
    await callback.answer()


@dp.message(ProxyStates.waiting_for_proxy_url)
async def handle_proxy_input(message: Message, state: FSMContext) -> None:
    """Обробка введеної адреси проксі користувачем."""
    raw_url = (message.text or "").strip()
    norm_url = normalize_proxy_url(raw_url)

    if not norm_url or "." not in norm_url:
        await message.answer(
            "⚠️ Некоректний формат проксі. Спробуйте ще раз або скасуйте дію:",
            reply_markup=cancel_fsm_keyboard(),
        )
        return

    status_msg = await message.answer("⏳ Тестую з'єднання з проксі...")
    is_working, resp_time, detail = await check_proxy_health(norm_url)

    status_str = "active" if is_working else detail
    saved = await db.add_proxy(norm_url)

    if saved:
        all_proxies = await db.get_all_proxies()
        for p in all_proxies:
            if p["url"] == norm_url:
                await db.update_proxy_status(p["id"], status_str, resp_time)
                break

        icon = "🟢" if is_working else "🔴"
        result_text = (
            f"✅ <b>Проксі успішно додано!</b>\n\n"
            f"• <b>Адреса:</b> <code>{html.escape(mask_proxy(norm_url))}</code>\n"
            f"• <b>Статус:</b> {icon} {status_str}\n"
            f"• <b>Час відгуку:</b> {resp_time} ms"
        )
    else:
        result_text = "⚠️ Такий проксі вже присутній у базі даних."

    await state.clear()
    await status_msg.delete()
    await message.answer(
        result_text, parse_mode=ParseMode.HTML, reply_markup=back_to_menu_keyboard()
    )


@dp.callback_query(F.data == "proxy_delete_menu")
async def handle_proxy_delete_menu(callback: CallbackQuery) -> None:
    """Меню вибору проксі для видалення."""
    if not await is_user_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ заборонено.", show_alert=True)
        return

    proxies = await db.get_all_proxies()
    if not proxies:
        await callback.answer("Немає проксі для видалення.", show_alert=True)
        return

    text = "❌ <b>Оберіть проксі, який потрібно видалити:</b>"
    kb = proxy_delete_keyboard(proxies)
    if callback.message:
        await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("proxy_del:"))
async def handle_proxy_delete_item(callback: CallbackQuery) -> None:
    """Видалення обраного проксі."""
    if not await is_user_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ заборонено.", show_alert=True)
        return

    try:
        proxy_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Помилка ідентифікатора проксі.", show_alert=True)
        return

    await db.delete_proxy(proxy_id)
    await callback.answer("Проксі успішно видалено!", show_alert=False)

    text, has_proxies = await get_proxy_menu_text()
    if callback.message:
        await callback.message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=proxy_menu_keyboard(has_proxies=has_proxies),
        )


# --- МЕНЮ СЕСІЙНИХ COOKIES OLX (HOT-RELOAD) ---


@dp.message(Command("set_cookies"))
async def cmd_set_cookies(message: Message, state: FSMContext) -> None:
    """Команда швидкого оновлення сесійних cookies OLX без перезапуску бота."""
    user_id = message.from_user.id if message.from_user else 0
    if not await is_user_admin(user_id):
        await message.answer("⛔️ Доступ заборонено.", parse_mode=ParseMode.HTML)
        return

    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) > 1 and "=" in parts[1]:
        cookies_val = parts[1].strip()
        await db.set_olx_cookies(cookies_val)
        await state.clear()
        await message.answer(
            "✅ <b>Сесійні cookies OLX успішно збережено в базі даних!</b>\n\n"
            "Нові cookies застосовано на льоту для всіх наступних запитів.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_to_menu_keyboard(),
        )
        return

    await state.set_state(CookieStates.waiting_for_cookies)
    await message.answer(
        "🍪 <b>Встановлення сесійних cookies OLX</b>\n\n"
        "Надішліть новий рядок cookies у форматі:\n"
        "<code>name=val; name2=val2</code>\n\n"
        "<i>Вони будуть збережені в SQLite та завантажуватимуться динамічно без перезапуску сервісу.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_fsm_keyboard(),
    )


@dp.callback_query(F.data == "cookies_menu")
async def handle_cookies_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Відображає меню керування сесійними cookies OLX."""
    if not await is_user_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ заборонено.", show_alert=True)
        return

    await state.clear()
    cookies_val = await db.get_olx_cookies()
    has_cookies = bool(cookies_val and cookies_val.strip())
    if has_cookies:
        preview = cookies_val[:30] + "..." if len(cookies_val) > 30 else cookies_val
        status_text = f"🟢 <b>Встановлено:</b> <code>{html.escape(preview)}</code>\n• Довжина: {len(cookies_val)} симв."
    else:
        status_text = "⚪️ <b>Не встановлено</b> (використовується анонімний режим)"

    text = (
        "🍪 <b>Управління сесійними cookies OLX (Hot-Reload)</b>\n\n"
        f"• <b>Поточний статус:</b> {status_text}\n\n"
        "<i>Сесійні cookies дозволяють Playwright обходити антифрод-перевірки та розкривати приховані номери телефонів власників. "
        "Оновлення відбувається миттєво без перезапуску бота.</i>"
    )
    if callback.message:
        await callback.message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=cookies_menu_keyboard(has_cookies),
        )
    await callback.answer()


@dp.callback_query(F.data == "input_cookies")
async def handle_input_cookies(callback: CallbackQuery, state: FSMContext) -> None:
    """Запит на введення нового рядка cookies."""
    if not await is_user_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ заборонено.", show_alert=True)
        return

    await state.set_state(CookieStates.waiting_for_cookies)
    text = (
        "🍪 <b>Надішліть новий рядок cookies OLX:</b>\n\n"
        "Формат: <code>name=val; name2=val2</code>\n\n"
        "<i>Скопіюйте cookies з DevTools у вкладці Network при перегляді сайту OLX.</i>"
    )
    if callback.message:
        await callback.message.edit_text(
            text, parse_mode=ParseMode.HTML, reply_markup=cancel_fsm_keyboard()
        )
    await callback.answer()


@dp.callback_query(F.data == "clear_cookies")
async def handle_clear_cookies(callback: CallbackQuery, state: FSMContext) -> None:
    """Очищення збережених cookies."""
    if not await is_user_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ заборонено.", show_alert=True)
        return

    await db.set_olx_cookies("")
    await callback.answer("Збережені cookies видалено!", show_alert=True)
    await handle_cookies_menu(callback, state)


@dp.message(CookieStates.waiting_for_cookies)
async def process_cookies_input(message: Message, state: FSMContext) -> None:
    """Обробка введеного рядка cookies."""
    user_id = message.from_user.id if message.from_user else 0
    if not await is_user_admin(user_id):
        await state.clear()
        return

    text = (message.text or "").strip()
    if not text or "=" not in text:
        await message.answer(
            "⚠️ Некоректний формат cookies. Очікується рядок формату <code>name=val; name2=val2</code>.\n"
            "Спробуйте ще раз або скасуйте дію:",
            parse_mode=ParseMode.HTML,
            reply_markup=cancel_fsm_keyboard(),
        )
        return

    await db.set_olx_cookies(text)
    await state.clear()
    await message.answer(
        "✅ <b>Сесійні cookies OLX успішно оновлено!</b>\n\n"
        "Нові cookies збережено в SQLite та підключено на льоту.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_to_menu_keyboard(),
    )


# --- НАВІГАЦІЯ ТА СИСТЕМНИЙ СТАТУС ---


@dp.callback_query(F.data == "back_to_main")
async def handle_back_to_main(callback: CallbackQuery, state: FSMContext) -> None:
    """Повернення в головне меню адміністратора."""
    await state.clear()
    if not await is_user_admin(callback.from_user.id):
        full_name = callback.from_user.full_name or ""
        text = get_realtor_menu_text(full_name)
        if callback.message:
            await callback.message.edit_text(
                text, parse_mode=ParseMode.HTML, reply_markup=realtor_main_keyboard()
            )
        await callback.answer()
        return

    parsers = await db.get_parsers_state()
    kb = admin_main_keyboard(parsers.get("olx", True), parsers.get("dimria", True))
    text = await get_admin_menu_text()
    if callback.message:
        await callback.message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
    await callback.answer()


@dp.callback_query(F.data == "show_status")
async def handle_show_status(callback: CallbackQuery) -> None:
    """Показ детального системного статусу."""
    if not await is_user_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ заборонено.", show_alert=True)
        return

    stats = await db.get_stats()
    parsers = await db.get_parsers_state()
    users = await db.list_users()

    olx_act = parsers.get("olx", True)
    dimria_act = parsers.get("dimria", True)

    olx_cb_status = (
        f"🔴 Кулдаун ({olx_circuit_breaker.remaining_cooldown_minutes()} хв)"
        if olx_circuit_breaker.is_in_cooldown()
        else (f"🟡 Збоїв: {olx_circuit_breaker.consecutive_errors}" if olx_circuit_breaker.consecutive_errors > 0 else "🟢 Норма")
    )
    dimria_cb_status = (
        f"🔴 Кулдаун ({dimria_circuit_breaker.remaining_cooldown_minutes()} хв)"
        if dimria_circuit_breaker.is_in_cooldown()
        else (f"🟡 Збоїв: {dimria_circuit_breaker.consecutive_errors}" if dimria_circuit_breaker.consecutive_errors > 0 else "🟢 Норма")
    )

    cookies_val = await db.get_olx_cookies()
    cookies_str = f"🟢 Встановлено ({len(cookies_val)} симв.)" if (cookies_val and cookies_val.strip()) else "⚪️ Не встановлено"

    excluded_str = (
        ", ".join(settings.excluded_districts)
        if settings.excluded_districts
        else "Усі райони"
    )

    text = (
        f"ℹ️ <b>Статус системи моніторингу RieltorBot</b>\n\n"
        f"• <b>OLX.ua парсер:</b> {'🟢 Працює' if olx_act else '🔴 На паузі'} ({olx_cb_status})\n"
        f"• <b>DIM.RIA парсер:</b> {'🟢 Працює' if dimria_act else '🔴 На паузі'} ({dimria_cb_status})\n"
        f"• <b>Сесійні cookies OLX:</b> {cookies_str}\n"
        f"• <b>Всього збережено оголошень:</b> {stats['total_seen']}\n"
        f"• <b>Надіслано сповіщень:</b> {stats['total_sent']}\n"
        f"• <b>Користувачів у системі:</b> {len(users)}\n"
        f"• <b>Збережених проксі:</b> {stats['proxies_count']} (активних: {stats['active_proxies_count']})\n"
        f"• <b>Місто:</b> {settings.CITY_NAME}\n"
        f"• <b>Виключені райони:</b> {html.escape(excluded_str)}\n"
        f"• <b>Інтервал перевірки:</b> {settings.POLL_INTERVAL_SECONDS} сек.\n"
    )
    if callback.message:
        await callback.message.edit_text(
            text, parse_mode=ParseMode.HTML, reply_markup=back_to_menu_keyboard()
        )
    await callback.answer()


# --- СТАРТ ДОДАТКУ ---


async def main() -> None:
    """Головна точка входу додатку."""
    logger.info("Initializing RieltorBot MVP with Two-Tier RBAC & Independent Parsers...")
    await db.init_db()

    # Автоматично реєструємо адмінів з config у таблиці users
    for admin_id in settings.admin_id_list:
        await db.add_user(
            telegram_id=admin_id,
            username="",
            full_name="Admin",
            role="admin",
            is_active=True,
        )

    # Початкове заповнення бази для запобігання спаму старими оголошеннями при першому старті
    if settings.INIT_MARK_AS_SEEN:
        stats = await db.get_stats()
        if stats["total_seen"] == 0:
            logger.info(
                "Fresh database detected. Initializing baseline listings without sending notifications..."
            )
            initial_items = await olx_scraper.fetch_listings(limit=40)
            if settings.DIMRIA_ENABLED:
                initial_dimria = await dimria_scraper.fetch_listings(
                    limit=15, seen_checker=db.is_listing_seen
                )
                initial_items.extend(initial_dimria)
            added, _ = await db.mark_listings_batch(initial_items, is_sent=False)
            logger.info("Baseline initialized with %d listings.", added)

    # Запускаємо фоновий сканер
    scanner_task = asyncio.create_task(periodic_scanner_loop())

    try:
        logger.info("Starting Telegram Bot polling...")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scanner_task.cancel()
        await bot.session.close()
        logger.info("Bot shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())

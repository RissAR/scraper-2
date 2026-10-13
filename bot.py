import asyncio
import html
import logging
import random
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
from exporter import export_listings_to_excel
from keyboards import (
    back_to_menu_keyboard,
    cancel_fsm_keyboard,
    main_menu_keyboard,
    new_listings_notification_keyboard,
    pagination_keyboard,
    proxy_delete_keyboard,
    proxy_menu_keyboard,
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
scraper = OLXScraper()


class ProxyStates(StatesGroup):
    waiting_for_proxy_url = State()


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
    if not (text.startswith("http://") or text.startswith("https://") or text.startswith("socks5://") or text.startswith("socks4://")):
        return f"http://{text}"
    return text


def format_listing_message(item: ListingItem) -> str:
    """Форматування тексту повідомлення про оголошення від власника для стрічки."""
    safe_title = html.escape(item.title)
    safe_location = html.escape(item.location)
    safe_price = html.escape(item.price_label or f"{int(item.price)} {item.currency}")
    safe_source = html.escape(item.source)
    author_info = html.escape(f"{item.author_name} ({item.author_ads_count} ог.)" if item.author_name else "Власник")

    phone_str = html.escape(item.phone) if item.phone else "<i>В описі / чаті OLX</i>"
    floor_info = f"{item.floor}/{item.total_floors} пов." if item.floor and item.total_floors else (f"{item.floor} пов." if item.floor else None)
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
    phone = html.escape(item.get("phone") or "В описі / чаті OLX")

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
                logger.warning("Failed photo send for %s: %s. Using text fallback.", item.id, exc)

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


async def check_for_new_listings(manual_trigger_chat_id: Optional[int] = None) -> int:
    """
    Один цикл перевірки наявності нових оголошень на OLX.ua.
    При знаходженні нових об'єктів надсилає сповіщення з кнопкою 'Показати об'єкти'.
    """
    is_active = await db.get_monitoring_status()
    if not is_active and manual_trigger_chat_id is None:
        logger.debug("Monitoring is paused. Skipping scheduled check.")
        return 0

    logger.info("Starting OLX check cycle...")

    # Отримуємо робочий проксі за наявності
    working_proxies = await db.get_working_proxies()
    active_proxy = random.choice(working_proxies) if working_proxies else None

    try:
        items = await scraper.fetch_listings(proxy=active_proxy)
    except Exception as exc:
        logger.error("Scraper encountered an exception: %s", exc)
        return 0

    if not items:
        logger.info("No items retrieved from OLX.")
        return 0

    new_items: list[ListingItem] = []

    for item in reversed(items):
        if await db.is_listing_seen(item.id):
            continue

        if not scraper.is_genuinely_new(item):
            await db.mark_listing_seen(item, is_sent=False)
            continue

        await db.mark_listing_seen(item, is_sent=True)
        new_items.append(item)
        logger.info("New listing registered: [%s] %s (%s)", item.id, item.title, item.price_label)

    new_count = len(new_items)
    logger.info("Check finished. New listings detected: %d", new_count)

    active_subs = await db.get_active_subscribers()
    configured_chats = settings.target_chat_ids
    recipients = sorted(list(set(active_subs + configured_chats)))
    if manual_trigger_chat_id and manual_trigger_chat_id not in recipients:
        recipients.append(manual_trigger_chat_id)

    # Якщо знайдено нові об'єкти, надсилаємо сповіщення згідно з ТЗ
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
                await db.remove_subscriber(cid)
            except Exception as err:
                logger.error("Failed sending notification to %d: %s", cid, err)

    return new_count


async def periodic_scanner_loop() -> None:
    """Фонова задача періодичного опитування OLX у реальному часі."""
    logger.info("Background scanner loop initialized (interval: %d s).", settings.POLL_INTERVAL_SECONDS)
    while True:
        try:
            await check_for_new_listings()
        except asyncio.CancelledError:
            logger.info("Background scanner loop cancelled.")
            break
        except Exception as exc:
            logger.exception("Unexpected error in scanner loop: %s", exc)

        await asyncio.sleep(settings.POLL_INTERVAL_SECONDS)


# --- ТЕКСТИ МЕНЮ ---


async def get_main_menu_text() -> str:
    """Генерує актуальний текст головного меню."""
    is_active = await db.get_monitoring_status()
    stats = await db.get_stats()
    status_icon = "🟢 АКТИВНИЙ" if is_active else "⏸ НА ПАУЗІ"

    excluded_str = (
        ", ".join(settings.excluded_districts) if settings.excluded_districts else "Усі райони"
    )
    price_str = (
        f"{settings.MIN_PRICE or 0:,.0f} - {settings.MAX_PRICE or '∞'} грн"
        if (settings.MIN_PRICE or settings.MAX_PRICE)
        else "Будь-яка"
    )

    return (
        f"🏢 <b>Панель управління RieltorBot (OLX Дніпро)</b>\n\n"
        f"• <b>Стан моніторингу:</b> {status_icon}\n"
        f"• <b>Місто:</b> {settings.CITY_NAME} (1-кімн. квартири)\n"
        f"• <b>Фільтр продавця:</b> 🛡️ Тільки від ВЛАСНИКІВ (без ріелторів)\n"
        f"• <b>Ціновий діапазон:</b> {price_str}\n"
        f"• <b>Райони:</b> {html.escape(excluded_str)}\n"
        f"• <b>Інтервал опитування:</b> {settings.POLL_INTERVAL_SECONDS // 60} хв ({settings.POLL_INTERVAL_SECONDS} с)\n\n"
        f"📊 <b>Статистика бази:</b>\n"
        f"• Всього відстежено: <b>{stats['total_seen']}</b>\n"
        f"• Сповіщень відправлено: <b>{stats['total_sent']}</b>\n"
        f"• Проксі в базі: <b>{stats['proxies_count']}</b> (активних: {stats['active_proxies_count']})\n\n"
        f"Оберіть потрібну дію нижче:"
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


# --- ОБРОБНИКИ КОМАНД ТА КНОПОК ---


@dp.message(CommandStart())
async def handle_start(message: Message, state: FSMContext) -> None:
    """Команда /start: реєстрація користувача та показ головного інлайн-меню."""
    await state.clear()
    chat_id = message.chat.id
    username = message.from_user.username or "" if message.from_user else ""
    full_name = message.from_user.full_name if message.from_user else ""

    await db.add_subscriber(chat_id=chat_id, username=username, full_name=full_name)
    is_active = await db.get_monitoring_status()
    text = await get_main_menu_text()
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard(is_active))


@dp.message(Command("menu"))
async def handle_menu_command(message: Message, state: FSMContext) -> None:
    """Команда /menu: відкриває головне меню."""
    await state.clear()
    is_active = await db.get_monitoring_status()
    text = await get_main_menu_text()
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard(is_active))


# --- ПЕРЕМИКАННЯ СТАНУ ПАРСЕРА (СТАРТ / СТОП) ---


@dp.callback_query(F.data == "monitoring_pause")
async def handle_monitoring_pause(callback: CallbackQuery) -> None:
    """Зупинка періодичного фонового парсера."""
    await db.set_monitoring_status(False)
    await callback.answer("⏸ Моніторинг зупинено!", show_alert=False)
    text = await get_main_menu_text()
    if callback.message:
        await callback.message.edit_text(
            text, parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard(False)
        )


@dp.callback_query(F.data == "monitoring_start")
async def handle_monitoring_start(callback: CallbackQuery) -> None:
    """Запуск періодичного фонового парсера."""
    await db.set_monitoring_status(True)
    await callback.answer("▶️ Моніторинг активовано!", show_alert=False)
    text = await get_main_menu_text()
    if callback.message:
        await callback.message.edit_text(
            text, parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard(True)
        )


# --- РУЧНИЙ ЗАПУСК ПЕРЕВІРКИ ---


@dp.callback_query(F.data == "manual_check")
async def handle_manual_check(callback: CallbackQuery) -> None:
    """Ручний запуск циклу парсингу OLX."""
    await callback.answer("🔍 Запускаю перевірку OLX...")
    if callback.message:
        await callback.message.edit_text(
            "⏳ <b>Виконується пошук свіжих оголошень на OLX.ua...</b>",
            parse_mode=ParseMode.HTML,
        )

    new_found = await check_for_new_listings(manual_trigger_chat_id=callback.from_user.id)
    is_active = await db.get_monitoring_status()
    menu_kb = main_menu_keyboard(is_active)

    if new_found == 0:
        result_text = (
            "✅ <b>Парсинг завершено!</b>\n\n"
            "Нових оголошень на цей момент не виявлено.\n"
            "Усі існуючі оголошення вже оброблено."
        )
        if callback.message:
            await callback.message.edit_text(result_text, parse_mode=ParseMode.HTML, reply_markup=menu_kb)
    else:
        # Сповіщення вже відправлено функцією check_for_new_listings
        text = await get_main_menu_text()
        if callback.message:
            await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=menu_kb)


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
    created_val = str(item.get("created_time") or "")[:16].replace("T", " ")
    seen_val = str(item.get("seen_at") or "")[:16].replace("T", " ")

    author = html.escape(item.get("author_name") or "Власник")
    ads_cnt = item.get("author_ads_count", 1)
    phone = html.escape(item.get("phone") or "Не вказано / у чаті")
    floor = item.get("floor")
    total_floors = item.get("total_floors")
    area = item.get("total_area")
    floor_str = f"{floor}/{total_floors} пов." if floor and total_floors else (f"{floor} пов." if floor else "-")

    card_text = (
        f"📦 <b>Об'єкт #{page_index + 1} з {total} (Власник)</b>\n\n"
        f"🏠 <b>{safe_title}</b>\n"
        f"💰 <b>Ціна:</b> {price:,.0f} {currency}\n"
        f"📍 <b>Район:</b> {district}\n"
        f"👤 <b>Автор:</b> {author} ({ads_cnt} активних ог.)\n"
        f"📞 <b>Телефон:</b> {phone}\n"
        f"📐 <b>Поверх / Площа:</b> {floor_str}, {area or '-'}\n"
        f"📅 <b>Опубліковано:</b> {created_val or 'Невідомо'}\n"
        f"🕒 <b>Зафіксовано в базі:</b> {seen_val}\n"
    )

    kb = pagination_keyboard(current_index=page_index, total_count=total, url=url)

    if callback.message:
        try:
            await callback.message.edit_text(card_text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:
            # Якщо текст не змінився при повторному кліку
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

    if callback.message:
        await callback.message.answer(
            "✅ Усі нові об'єкти вивантажено.",
            reply_markup=back_to_menu_keyboard(),
        )


# --- ЕКСПОРТ В EXCEL ---


@dp.callback_query(F.data == "export_excel")
async def handle_export_excel(callback: CallbackQuery) -> None:
    """Генерує та відправляє файл Excel з усіма об'єктами бази."""
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
                f"Файл містить повні дані: ID, назва, ціна, район, посилання та дати."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=back_to_menu_keyboard(),
        )


# --- МЕНЮ ПРОКСІ ---


@dp.callback_query(F.data == "proxy_menu")
@dp.callback_query(F.data == "back_to_proxy_menu")
async def handle_proxy_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Відображає меню керування проксі."""
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
        await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=cancel_fsm_keyboard())
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
        # Отримуємо ID щойно доданого проксі для збереження статусу
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
    await message.answer(result_text, parse_mode=ParseMode.HTML, reply_markup=back_to_menu_keyboard())


@dp.callback_query(F.data == "proxy_delete_menu")
async def handle_proxy_delete_menu(callback: CallbackQuery) -> None:
    """Меню вибору проксі для видалення."""
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


# --- НАВІГАЦІЯ: НАЗАД В ГОЛОВНЕ МЕНЮ ---


@dp.callback_query(F.data == "back_to_main")
async def handle_back_to_main(callback: CallbackQuery, state: FSMContext) -> None:
    """Повернення в головне меню."""
    await state.clear()
    is_active = await db.get_monitoring_status()
    text = await get_main_menu_text()
    if callback.message:
        await callback.message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard(is_active),
        )
    await callback.answer()


@dp.callback_query(F.data == "show_status")
async def handle_show_status(callback: CallbackQuery) -> None:
    """Показ системного статусу."""
    stats = await db.get_stats()
    is_active = await db.get_monitoring_status()
    excluded_str = (
        ", ".join(settings.excluded_districts) if settings.excluded_districts else "Усі райони"
    )

    text = (
        f"ℹ️ <b>Статус системи моніторингу</b>\n\n"
        f"• <b>Фоновий парсер:</b> {'🟢 Працює' if is_active else '⏸ На паузі'}\n"
        f"• <b>Всього збережено оголошень:</b> {stats['total_seen']}\n"
        f"• <b>Надіслано сповіщень:</b> {stats['total_sent']}\n"
        f"• <b>Активних підписників:</b> {stats['active_subscribers']}\n"
        f"• <b>Збережених проксі:</b> {stats['proxies_count']} (активних: {stats['active_proxies_count']})\n"
        f"• <b>Місто:</b> {settings.CITY_NAME}\n"
        f"• <b>Виключені райони:</b> {html.escape(excluded_str)}\n"
        f"• <b>Інтервал перевірки:</b> {settings.POLL_INTERVAL_SECONDS} сек.\n"
    )
    if callback.message:
        await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=back_to_menu_keyboard())
    await callback.answer()


# --- СТАРТ ДОДАТКУ ---


async def main() -> None:
    """Головна точка входу додатку."""
    logger.info("Initializing RieltorBot MVP with Inline UI...")
    await db.init_db()

    # Початкове заповнення бази для запобігання спаму старими оголошеннями при першому старті
    if settings.INIT_MARK_AS_SEEN:
        stats = await db.get_stats()
        if stats["total_seen"] == 0:
            logger.info("Fresh database detected. Initializing baseline listings without sending notifications...")
            initial_items = await scraper.fetch_listings(limit=40)
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

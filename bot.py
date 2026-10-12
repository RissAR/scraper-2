import asyncio
import html
import logging
from typing import Optional

from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import Command, CommandStart

from config import settings
from database import DatabaseManager
from models import ListingItem
from olx_scraper import OLXScraper

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


def format_listing_message(item: ListingItem) -> str:
    """
    Форматує текст повідомлення про нове оголошення згідно з вимогами ТЗ:
    - Заголовок
    - Ціна
    - Район / Локація
    - Джерело
    - Пряме посилання
    """
    safe_title = html.escape(item.title)
    safe_location = html.escape(item.location)
    safe_price = html.escape(item.price_label or f"{int(item.price)} {item.currency}")
    safe_source = html.escape(item.source)

    msg_lines = [
        f"🏠 <b>{safe_title}</b>",
        "",
        f"💰 <b>Ціна:</b> {safe_price}",
        f"📍 <b>Район/Локація:</b> {safe_location}",
        f"📌 <b>Джерело:</b> {safe_source}",
        "",
        f'🔗 <a href="{item.url}">Переглянути оголошення на {safe_source}</a>',
    ]
    return "\n".join(msg_lines)


async def send_listing_notification(item: ListingItem, chat_ids: list[int]) -> int:
    """
    Відправляє повідомлення з фотографією (або текстом при збої) усім цільовим отримувачам.
    Повертає кількість успішно відправлених повідомлень.
    """
    caption = format_listing_message(item)
    success_count = 0

    for chat_id in chat_ids:
        try:
            if item.image_url:
                try:
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=item.image_url,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                    )
                    success_count += 1
                    await asyncio.sleep(0.05)  # Захист від Telegram FloodLimit
                    continue
                except TelegramAPIError as photo_err:
                    logger.warning(
                        "Failed to send photo to chat %d for ad %s: %s. Falling back to text.",
                        chat_id,
                        item.id,
                        photo_err,
                    )

            # Текстове повідомлення (резервний варіант або якщо фото відсутнє)
            await bot.send_message(
                chat_id=chat_id,
                text=caption,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )
            success_count += 1
            await asyncio.sleep(0.05)

        except TelegramForbiddenError:
            logger.info("Bot was blocked by user %d. Deactivating subscriber.", chat_id)
            await db.remove_subscriber(chat_id)
        except Exception as exc:
            logger.error("Error sending notification to chat %d: %s", chat_id, exc)

    return success_count


async def check_for_new_listings() -> int:
    """
    Один цикл перевірки наявності нових оголошень на OLX.ua.
    """
    logger.info("Checking OLX.ua for new listings...")
    try:
        items = await scraper.fetch_listings()
    except Exception as exc:
        logger.error("Scraper encountered an unhandled exception: %s", exc)
        return 0

    if not items:
        logger.info("No items retrieved from OLX.")
        return 0

    # Збираємо всіх отримувачів: підписники з БД + ID з конфігу .env
    active_subs = await db.get_active_subscribers()
    configured_chats = settings.target_chat_ids
    all_recipients = sorted(list(set(active_subs + configured_chats)))

    new_found = 0

    # OLX сортує created_at:desc -> нові йдуть першими, відправляємо від старіших до новіших
    for item in reversed(items):
        # 1. Перевіряємо, чи бачили вже це оголошення в БД
        already_seen = await db.is_listing_seen(item.id)
        if already_seen:
            continue

        # 2. Перевіряємо справжність новизни (захист від піднятих старих у ТОП)
        if not scraper.is_genuinely_new(item):
            # Фіксуємо в БД як бачене, але сповіщення не надсилаємо
            await db.mark_listing_seen(item, is_sent=False)
            continue

        # 3. Нове оголошення — зберігаємо в БД
        await db.mark_listing_seen(item, is_sent=True)
        new_found += 1
        logger.info("New listing detected: [%s] %s (%s)", item.id, item.title, item.price_label)

        # 4. Миттєво надсилаємо в Telegram
        if all_recipients:
            sent_cnt = await send_listing_notification(item, all_recipients)
            logger.info("Sent notification for [%s] to %d recipients", item.id, sent_cnt)
        else:
            logger.warning(
                "New listing [%s] found, but no recipients configured! "
                "Set TELEGRAM_CHAT_ID in .env or run /start in bot.",
                item.id,
            )

    logger.info("Cycle finished. Newly processed listings: %d", new_found)
    return new_found


async def periodic_scanner_loop() -> None:
    """Фонова задача періодичного опитування OLX у реальному часі."""
    logger.info(
        "Starting background scanner loop (interval: %d seconds)...",
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


# --- ОБРОБНИКИ КОМАНД TELEGRAM BOT ---


@dp.message(CommandStart())
async def handle_start(message: types.Message) -> None:
    """Обробка команди /start: вітання, підписка та показ поточних фільтрів."""
    chat_id = message.chat.id
    username = message.from_user.username or "" if message.from_user else ""
    full_name = message.from_user.full_name if message.from_user else ""

    await db.add_subscriber(chat_id=chat_id, username=username, full_name=full_name)

    excluded_str = (
        ", ".join(settings.excluded_districts) if settings.excluded_districts else "Немає (усі райони)"
    )
    price_filter_str = (
        f"{settings.MIN_PRICE or 0:,.0f} - {settings.MAX_PRICE or '∞'} грн"
        if (settings.MIN_PRICE or settings.MAX_PRICE)
        else "Будь-яка"
    )

    welcome_text = (
        f"👋 <b>Вітаю, {html.escape(full_name or 'користувачу')}!</b>\n\n"
        f"Бот активовано для відстеження нових оголошень нерухомості.\n"
        f"Ваш Telegram Chat ID: <code>{chat_id}</code>\n\n"
        f"⚙️ <b>Поточні параметри моніторингу:</b>\n"
        f"• <b>Місто:</b> {settings.CITY_NAME}\n"
        f"• <b>Тип:</b> 1-кімнатна квартира (Оренда)\n"
        f"• <b>Ціновий діапазон:</b> {price_filter_str}\n"
        f"• <b>Виключені райони:</b> {html.escape(excluded_str)}\n"
        f"• <b>Інтервал оновлення:</b> {settings.POLL_INTERVAL_SECONDS // 60} хв ({settings.POLL_INTERVAL_SECONDS} с)\n"
        f"• <b>Джерело:</b> OLX.ua\n\n"
        f"Ви будете миттєво отримувати повідомлення, щойно з'явиться нове оголошення!\n\n"
        f"Доступні команди:\n"
        f"/status — Перевірити стан бота та статистику\n"
        f"/check — Примусово запустити пошук прямо зараз\n"
        f"/stop — Зупинити отримання сповіщень"
    )
    await message.answer(welcome_text, parse_mode=ParseMode.HTML)


@dp.message(Command("status"))
async def handle_status(message: types.Message) -> None:
    """Обробка команди /status: поточна статистика та налаштування."""
    stats = await db.get_stats()
    excluded_str = (
        ", ".join(settings.excluded_districts) if settings.excluded_districts else "Немає (усі райони)"
    )

    status_text = (
        f"📊 <b>Статус моніторингу</b>\n\n"
        f"• <b>Відстежено оголошень у базі:</b> {stats['total_seen']}\n"
        f"• <b>Надіслано сповіщень:</b> {stats['total_sent']}\n"
        f"• <b>Активних підписників:</b> {stats['active_subscribers']}\n"
        f"• <b>Інтервал опитування:</b> {settings.POLL_INTERVAL_SECONDS} сек.\n"
        f"• <b>Виключені райони:</b> {html.escape(excluded_str)}\n"
        f"• <b>Проксі:</b> {'Підключено' if settings.PROXY_URL else 'Вимкнено'}\n"
    )
    await message.answer(status_text, parse_mode=ParseMode.HTML)


@dp.message(Command("check"))
async def handle_check(message: types.Message) -> None:
    """Ручний запуск перевірки нових оголошень."""
    msg = await message.answer("🔍 Запускаю перевірку OLX.ua...")
    new_count = await check_for_new_listings()
    if new_count > 0:
        await msg.edit_text(f"✅ Перевірку завершено! Знайдено нових оголошень: {new_count}")
    else:
        await msg.edit_text("✅ Перевірку завершено! Нових оголошень наразі немає.")


@dp.message(Command("stop"))
async def handle_stop(message: types.Message) -> None:
    """Зупинка надсилання сповіщень."""
    await db.remove_subscriber(message.chat.id)
    await message.answer(
        "🔕 Ви відписалися від сповіщень. Щоб відновити моніторинг, надішліть команду /start."
    )


async def main() -> None:
    """Головна точка входу додатку."""
    logger.info("Initializing RieltorBot MVP (OLX.ua Dnipro)...")
    await db.init_db()

    # Якщо база порожня і ввімкнено початкову ініціалізацію, збережемо поточний стан без спаму
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

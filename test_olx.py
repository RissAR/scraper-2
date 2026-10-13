import asyncio
import logging

from database import DatabaseManager
from olx_scraper import OLXScraper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("test_olx")


async def main() -> None:
    logger.info("--- ЗАПУСК ТЕСТУ ПОВНОГО СКРАПЕРА OLX (ТІЛЬКИ ВЛАСНИКИ ДНІПРО) ---")
    scraper = OLXScraper()
    items = await scraper.fetch_listings(limit=10)

    print(f"\nОтримано перевірених оголошень від власників: {len(items)}")
    assert len(items) > 0, "Помилка: не знайдено жодного оголошення від власника на OLX!"

    db_manager = DatabaseManager(db_path="data/test_bot.db")
    await db_manager.init_db()

    # Первинне збереження в базу
    added, skipped = await db_manager.mark_listings_batch(items, is_sent=True)
    print(f"Перший прогін БД: додано нових = {added}, пропущено дублікатів = {skipped}")

    # Перевірка захисту від дублікатів
    added_again, skipped_again = await db_manager.mark_listings_batch(items, is_sent=True)
    print(f"Повторний прогін БД: додано нових = {added_again}, пропущено дублікатів = {skipped_again}")
    assert added_again == 0, "Помилка: повторні оголошення не були визнані дублікатами!"

    print("\n--- ДЕТАЛЬНИЙ ЗВІТ ПО ЗНАЙДЕНИХ ОБ'ЄКТАХ ВІД ВЛАСНИКІВ ---")
    for i, it in enumerate(items[:5], start=1):
        print(f"\n[{i}] ID: {it.id}")
        print(f"Заголовок: {it.title}")
        print(f"Ціна: {it.price_label}")
        print(f"Локація: {it.location}")
        print(f"👤 Власник: {it.author_name} (Активних оголошень: {it.author_ads_count})")
        print(f"📞 Телефон: {it.phone or 'Не вказано у відкритому доступі'}")
        print(f"📐 Параметри: поверх {it.floor or '?'}/{it.total_floors or '?'}, площа: {it.total_area or '?'}")
        print(f"📷 Фотографій всього: {len(it.photos)}")
        print(f"🔗 Посилання: {it.url}")
        print(f"Створено: {it.created_time}")
        print(f"Справді нове (< 48 год): {scraper.is_genuinely_new(it)}")

    # Отримуємо з бази збережені дані для валідації
    saved_items, total = await db_manager.get_listings(limit=5, offset=0)
    print(f"\nПеревірка вибірки з БД: всього {total} записів, поля першого запису:")
    if saved_items:
        first = saved_items[0]
        print(f"ID: {first['id']} | Власник: {first.get('author_name')} | Телефон: {first.get('phone')} | Площа: {first.get('total_area')}")

    print("\n✅ ПОВНИЙ ТЕСТ OLX SCRAPER УСПІШНО ПРОЙДЕНО!")


if __name__ == "__main__":
    asyncio.run(main())

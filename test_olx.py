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
    logger.info("--- ЗАПУСК ТЕСТУ OLX SCRAPER ДЛЯ МІСТА ДНІПРО ---")
    scraper = OLXScraper()
    items = await scraper.fetch_listings(limit=10)

    print(f"\nОтримано валідних оголошень: {len(items)}")
    assert len(items) > 0, "Помилка: не знайдено жодного оголошення на OLX!"

    db_manager = DatabaseManager(db_path="data/test_bot.db")
    await db_manager.init_db()

    # Первинне збереження
    added, skipped = await db_manager.mark_listings_batch(items, is_sent=True)
    print(f"Перший прогін БД: додано нових = {added}, пропущено дублікатів = {skipped}")

    # Повторне збереження тих самих оголошень (перевірка захисту від дублікатів)
    added_again, skipped_again = await db_manager.mark_listings_batch(items, is_sent=True)
    print(
        f"Повторний прогін БД: додано нових = {added_again}, "
        f"пропущено дублікатів = {skipped_again}"
    )
    assert added_again == 0, "Помилка: повторні оголошення не були визнані дублікатами!"

    print("\n--- ПЕРШІ 3 ОГОЛОШЕННЯ ДЛЯ ПОВІДОМЛЕНЬ В TELEGRAM ---")
    for i, it in enumerate(items[:3], start=1):
        print(f"\n[{i}] ID: {it.id}")
        print(f"Заголовок: {it.title}")
        print(f"Ціна: {it.price_label}")
        print(f"Локація: {it.location} (Район: {it.district or 'Не вказано'})")
        print(f"Джерело: {it.source}")
        print(f"URL: {it.url}")
        print(f"Фото: {it.image_url or 'Немає фото'}")
        print(f"Створено: {it.created_time}")
        print(f"Справді нове (< 48 год): {scraper.is_genuinely_new(it)}")

    print("\n✅ ТЕСТ OLX SCRAPER УСПІШНО ПРОЙДЕНО!")


if __name__ == "__main__":
    asyncio.run(main())

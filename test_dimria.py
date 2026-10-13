import asyncio
import logging

from database import DatabaseManager
from dimria_scraper import DimRiaScraper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("test_dimria")


async def main() -> None:
    logger.info("--- ЗАПУСК ТЕСТУ СКРАПЕРА DIM.RIA (ТІЛЬКИ ВЛАСНИКИ ДНІПРО) ---")
    scraper = DimRiaScraper()

    import os
    if os.path.exists("data/test_dimria.db"):
        os.remove("data/test_dimria.db")

    db_manager = DatabaseManager(db_path="data/test_dimria.db")
    await db_manager.init_db()

    # Запускаємо збір 3 об'єктів для детальної перевірки
    items = await scraper.fetch_listings(limit=3, seen_checker=db_manager.is_listing_seen)
    print(f"\nОтримано перевірених оголошень від власників DIM.RIA: {len(items)}")

    assert len(items) > 0, "Помилка: не знайдено жодного валідного оголошення від власника на DIM.RIA!"

    # Первинне збереження в тестову базу даних
    added, skipped = await db_manager.mark_listings_batch(items, is_sent=True)
    print(f"Перший прогін БД: додано нових = {added}, пропущено дублікатів = {skipped}")

    # Перевірка дедуплікації
    added_again, skipped_again = await db_manager.mark_listings_batch(items, is_sent=True)
    print(f"Повторний прогін БД: додано нових = {added_again}, пропущено дублікатів = {skipped_again}")
    assert added_again == 0, "Помилка: повторні оголошення не були визнані дублікатами!"

    print("\n--- ДЕТАЛЬНИЙ ЗВІТ ПО ЗНАЙДЕНИХ ОБ'ЄКТАХ DIM.RIA ---")
    for i, it in enumerate(items, start=1):
        print(f"\n[{i}] ID: {it.id}")
        print(f"Джерело: {it.source}")
        print(f"Заголовок: {it.title}")
        print(f"Ціна: {it.price_label}")
        print(f"Локація: {it.location}")
        print(f"👤 Власник: {it.author_name} (Активних оголошень: {it.author_ads_count})")
        print(f"📞 Телефон: {it.phone or 'Не вказано / не розкрито'}")
        print(f"📐 Параметри: поверх {it.floor or '?'}/{it.total_floors or '?'}, площа: {it.total_area or '?'}")
        print(f"📷 Фотографій: {len(it.photos)}")
        print(f"🔗 Посилання: {it.url}")
        print(f"Створено: {it.created_time}")
        print(f"Справді нове (< 48 год): {scraper.is_genuinely_new(it)}")

    # Перевірка зчитування з БД
    saved_items, total = await db_manager.get_listings(limit=5, offset=0)
    print(f"\nПеревірка вибірки з БД: всього {total} записів.")
    dimria_saved = [x for x in saved_items if x.get("source") == "DIM.RIA"]
    if dimria_saved:
        first = dimria_saved[0]
        print(f"Перший запис DIM.RIA у БД: ID: {first['id']} | Власник: {first.get('author_name')} | Телефон: {first.get('phone')} | Ціна: {first.get('price')}")

    print("\n✅ ПОВНИЙ ТЕСТ DIM.RIA SCRAPER УСПІШНО ПРОЙДЕНО!")


if __name__ == "__main__":
    asyncio.run(main())

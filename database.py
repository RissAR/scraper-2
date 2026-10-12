import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiosqlite

from config import settings
from models import ListingItem

logger = logging.getLogger(__name__)


class DatabaseManager:
    """Асинхронний менеджер SQLite бази даних для збереження оголошень та підписників."""

    def __init__(self, db_path: Path | str = settings.DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    async def init_db(self) -> None:
        """Створює необхідні таблиці та індекси, якщо вони ще не існують."""
        logger.info("Initializing SQLite database at: %s", self.db_path)
        async with aiosqlite.connect(self.db_path) as db:
            # Таблиця для відстеження оброблених оголошень
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_listings (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    price REAL NOT NULL,
                    currency TEXT NOT NULL,
                    url TEXT NOT NULL,
                    district TEXT,
                    created_time TEXT,
                    is_promoted INTEGER NOT NULL DEFAULT 0,
                    is_sent INTEGER NOT NULL DEFAULT 1,
                    seen_at TEXT NOT NULL
                );
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_seen_source ON seen_listings(source);"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_seen_at ON seen_listings(seen_at);"
            )

            # Таблиця підписників (чатів / користувачів бота)
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS subscribers (
                    chat_id INTEGER PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                """
            )
            await db.commit()
        logger.info("Database initialized successfully at: %s", self.db_path)

    async def is_listing_seen(self, listing_id: str) -> bool:
        """Перевіряє, чи було оголошення вже зафіксоване в БД."""
        query = "SELECT 1 FROM seen_listings WHERE id = ? LIMIT 1;"
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(query, (listing_id,)) as cursor:
                row = await cursor.fetchone()
                return row is not None

    async def mark_listing_seen(self, item: ListingItem, is_sent: bool = True) -> bool:
        """
        Зберігає оголошення у списку бачених (INSERT OR IGNORE).
        Повертає True, якщо оголошення нове, і False, якщо вже існувало.
        """
        sql = """
        INSERT OR IGNORE INTO seen_listings (
            id, source, external_id, title, price, currency,
            url, district, created_time, is_promoted, is_sent, seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            item.id,
            item.source,
            item.external_id,
            item.title,
            item.price,
            item.currency,
            item.url,
            item.district,
            item.created_time.isoformat() if item.created_time else None,
            1 if item.is_promoted else 0,
            1 if is_sent else 0,
            datetime.utcnow().isoformat(),
        )

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(sql, params)
            await db.commit()
            return cursor.rowcount > 0

    async def mark_listings_batch(
        self, items: list[ListingItem], is_sent: bool = False
    ) -> tuple[int, int]:
        """
        Батчеве додавання оголошень (наприклад, для первинного заповнення).
        Повертає кортеж (додано_нових, пропущено_старих).
        """
        if not items:
            return 0, 0

        sql = """
        INSERT OR IGNORE INTO seen_listings (
            id, source, external_id, title, price, currency,
            url, district, created_time, is_promoted, is_sent, seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        now_iso = datetime.utcnow().isoformat()
        params_list = [
            (
                item.id,
                item.source,
                item.external_id,
                item.title,
                item.price,
                item.currency,
                item.url,
                item.district,
                item.created_time.isoformat() if item.created_time else None,
                1 if item.is_promoted else 0,
                1 if is_sent else 0,
                now_iso,
            )
            for item in items
        ]

        async with aiosqlite.connect(self.db_path) as db:
            initial = db.total_changes
            await db.executemany(sql, params_list)
            await db.commit()
            added = db.total_changes - initial

        skipped = len(items) - added
        return added, skipped

    async def add_subscriber(
        self, chat_id: int, username: str = "", full_name: str = ""
    ) -> bool:
        """Додає або реактивує підписника."""
        sql = """
        INSERT INTO subscribers (chat_id, username, full_name, is_active, created_at)
        VALUES (?, ?, ?, 1, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            is_active = 1,
            username = excluded.username,
            full_name = excluded.full_name;
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                sql, (chat_id, username, full_name, datetime.utcnow().isoformat())
            )
            await db.commit()
            return True

    async def remove_subscriber(self, chat_id: int) -> bool:
        """Деактивує підписника."""
        sql = "UPDATE subscribers SET is_active = 0 WHERE chat_id = ?;"
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(sql, (chat_id,))
            await db.commit()
            return cursor.rowcount > 0

    async def get_active_subscribers(self) -> list[int]:
        """Повертає список active chat_id підписників."""
        sql = "SELECT chat_id FROM subscribers WHERE is_active = 1;"
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(sql) as cursor:
                rows = await cursor.fetchall()
                return [row[0] for row in rows]

    async def get_stats(self) -> dict:
        """Отримує статистику роботи бази даних."""
        stats = {
            "total_seen": 0,
            "total_sent": 0,
            "active_subscribers": 0,
        }
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM seen_listings;") as cur:
                row = await cur.fetchone()
                if row:
                    stats["total_seen"] = row[0]

            async with db.execute("SELECT COUNT(*) FROM seen_listings WHERE is_sent = 1;") as cur:
                row = await cur.fetchone()
                if row:
                    stats["total_sent"] = row[0]

            async with db.execute("SELECT COUNT(*) FROM subscribers WHERE is_active = 1;") as cur:
                row = await cur.fetchone()
                if row:
                    stats["active_subscribers"] = row[0]

        return stats

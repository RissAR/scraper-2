import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiosqlite

from config import settings
from models import ListingItem

logger = logging.getLogger(__name__)


class DatabaseManager:
    """Асинхронний менеджер SQLite бази даних для збереження оголошень, підписників, проксі та стану бота."""

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
                    seen_at TEXT NOT NULL,
                    phone TEXT,
                    description TEXT,
                    floor TEXT,
                    total_floors TEXT,
                    total_area TEXT,
                    kitchen_area TEXT,
                    author_name TEXT,
                    author_ads_count INTEGER NOT NULL DEFAULT 1,
                    is_owner INTEGER NOT NULL DEFAULT 1
                );
                """
            )
            # Перевірка та автоматичне додавання нових колонок, якщо таблиця вже існувала
            async with db.execute("PRAGMA table_info(seen_listings);") as cur:
                existing_cols = [row[1] for row in await cur.fetchall()]

            additional_cols = {
                "phone": "TEXT",
                "description": "TEXT",
                "floor": "TEXT",
                "total_floors": "TEXT",
                "total_area": "TEXT",
                "kitchen_area": "TEXT",
                "author_name": "TEXT",
                "author_ads_count": "INTEGER NOT NULL DEFAULT 1",
                "is_owner": "INTEGER NOT NULL DEFAULT 1",
            }
            for col_name, col_type in additional_cols.items():
                if col_name not in existing_cols:
                    try:
                        await db.execute(f"ALTER TABLE seen_listings ADD COLUMN {col_name} {col_type};")
                    except Exception as col_err:
                        logger.warning("Column %s could not be added: %s", col_name, col_err)

            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_seen_source ON seen_listings(source);"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_seen_at ON seen_listings(seen_at);"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_seen_is_owner ON seen_listings(is_owner);"
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

            # Таблиця збережених проксі
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS proxies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT UNIQUE NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'unknown',
                    response_time_ms REAL NOT NULL DEFAULT 0.0,
                    last_checked TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )

            # Таблиця системного стану бота (активність моніторингу та окремих парсерів)
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            # За замовчуванням парсери активні
            await db.execute(
                "INSERT OR IGNORE INTO bot_state (key, value) VALUES ('monitoring_active', '1');"
            )
            await db.execute(
                "INSERT OR IGNORE INTO bot_state (key, value) VALUES ('olx_active', '1');"
            )
            await db.execute(
                "INSERT OR IGNORE INTO bot_state (key, value) VALUES ('dimria_active', '1');"
            )

            # Таблиця користувачів з ролями (RBAC: 'admin', 'realtor', 'pending')
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    role TEXT NOT NULL DEFAULT 'realtor',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                """
            )
            # Автоматична міграція існуючих підписників у таблицю users
            await db.execute(
                """
                INSERT OR IGNORE INTO users (telegram_id, username, full_name, role, is_active, created_at)
                SELECT chat_id, username, full_name, 'realtor', is_active, created_at FROM subscribers;
                """
            )

            # Гарантоване призначення прав Адміністратора за ТЗ
            await db.execute(
                """
                INSERT INTO users (telegram_id, username, full_name, role, is_active, created_at)
                VALUES (123456789, 'admin', 'Admin', 'admin', 1, datetime('now'))
                ON CONFLICT(telegram_id) DO UPDATE SET role = 'admin', is_active = 1;
                """
            )

            await db.commit()
        logger.info("Database initialized successfully at: %s", self.db_path)

    # --- МЕТОДИ СТАНУ БОТА ТА ПАРСЕРІВ ---

    async def get_monitoring_status(self) -> bool:
        """Повертає True, якщо фоновий парсер запущено, і False, якщо на паузі."""
        sql = "SELECT value FROM bot_state WHERE key = 'monitoring_active' LIMIT 1;"
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(sql) as cur:
                row = await cur.fetchone()
                return row[0] == "1" if row else True

    async def set_monitoring_status(self, is_active: bool) -> None:
        """Оновлює глобальний стан фонового парсера."""
        sql = """
        INSERT INTO bot_state (key, value) VALUES ('monitoring_active', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value;
        """
        val_str = "1" if is_active else "0"
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(sql, (val_str,))
            await db.commit()

    async def get_parsers_state(self) -> dict[str, bool]:
        """Повертає словник станів парсерів: {'olx': bool, 'dimria': bool}."""
        sql = "SELECT key, value FROM bot_state WHERE key IN ('olx_active', 'dimria_active');"
        res = {"olx": True, "dimria": True}
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(sql) as cur:
                rows = await cur.fetchall()
                for k, v in rows:
                    if k == "olx_active":
                        res["olx"] = (v == "1")
                    elif k == "dimria_active":
                        res["dimria"] = (v == "1")
        return res

    async def set_parser_state(self, parser_name: str, is_active: bool) -> None:
        """Встановлює стан конкретного парсера ('olx' або 'dimria')."""
        key = f"{parser_name.lower()}_active"
        sql = """
        INSERT INTO bot_state (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value;
        """
        val_str = "1" if is_active else "0"
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(sql, (key, val_str))
            await db.commit()

    async def set_all_parsers_state(self, is_active: bool) -> None:
        """Встановлює стан для всіх парсерів одночасно."""
        val_str = "1" if is_active else "0"
        sql = """
        INSERT INTO bot_state (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value;
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(sql, ("olx_active", val_str))
            await db.execute(sql, ("dimria_active", val_str))
            await db.execute(sql, ("monitoring_active", val_str))
            await db.commit()

    async def get_bot_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Отримує довільний текстовий параметр із таблиці bot_state."""
        sql = "SELECT value FROM bot_state WHERE key = ? LIMIT 1;"
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(sql, (key,)) as cur:
                row = await cur.fetchone()
                return row[0] if row else default

    async def set_bot_setting(self, key: str, value: str) -> None:
        """Зберігає довільний текстовий параметр у таблиці bot_state."""
        sql = """
        INSERT INTO bot_state (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value;
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(sql, (key, value))
            await db.commit()

    async def get_olx_cookies(self) -> Optional[str]:
        """Повертає збережені сесійні cookies OLX (або значення за замовчуванням із settings)."""
        val = await self.get_bot_setting("olx_cookies")
        if val and val.strip():
            return val.strip()
        return settings.OLX_COOKIES

    async def set_olx_cookies(self, cookies: str) -> None:
        """Зберігає оновлені cookies OLX у базі даних (Hot-Reload)."""
        await self.set_bot_setting("olx_cookies", cookies.strip())

    async def get_active_proxies(self) -> list[str]:
        """Повертає список URL активних/робочих проксі для ротації."""
        return await self.get_working_proxies()

    # --- МЕТОДИ РОБОТИ З ОГОЛОШЕННЯМИ ---

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
            url, district, created_time, is_promoted, is_sent, seen_at,
            phone, description, floor, total_floors, total_area, kitchen_area,
            author_name, author_ads_count, is_owner
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            item.phone,
            item.description,
            item.floor,
            item.total_floors,
            item.total_area,
            item.kitchen_area,
            item.author_name,
            item.author_ads_count,
            1 if item.is_owner else 0,
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
            url, district, created_time, is_promoted, is_sent, seen_at,
            phone, description, floor, total_floors, total_area, kitchen_area,
            author_name, author_ads_count, is_owner
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                item.phone,
                item.description,
                item.floor,
                item.total_floors,
                item.total_area,
                item.kitchen_area,
                item.author_name,
                item.author_ads_count,
                1 if item.is_owner else 0,
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

    async def get_listings(
        self, limit: int = 1, offset: int = 0
    ) -> tuple[list[dict], int]:
        """Повертає сторінку оголошень та загальну кількість записів для пагінації."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # Загальна кількість
            async with db.execute("SELECT COUNT(*) FROM seen_listings WHERE is_sent = 1;") as cur:
                count_row = await cur.fetchone()
                total = count_row[0] if count_row else 0

            # Записи для поточної сторінки
            query = """
            SELECT *
            FROM seen_listings
            WHERE is_sent = 1
            ORDER BY seen_at DESC
            LIMIT ? OFFSET ?;
            """
            async with db.execute(query, (limit, offset)) as cur:
                rows = await cur.fetchall()
                items = [dict(r) for r in rows]

            return items, total

    async def get_recent_sent_listings(self, limit: int = 10) -> list[dict]:
        """Повертає останні надіслані оголошення."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            query = """
            SELECT *
            FROM seen_listings
            WHERE is_sent = 1
            ORDER BY seen_at DESC
            LIMIT ?;
            """
            async with db.execute(query, (limit,)) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def get_all_listings_for_export(self) -> list[dict]:
        """Повертає всі збережені оголошення для експорту в Excel."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            query = """
            SELECT *
            FROM seen_listings
            ORDER BY seen_at DESC;
            """
            async with db.execute(query) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    # --- МЕТОДИ ПІДПИСНИКІВ ---

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

    # --- МЕТОДИ КОРИСТУВАЧІВ ТА РОЛЕЙ (RBAC) ---

    async def get_user(self, telegram_id: int) -> Optional[dict]:
        """Отримує профіль користувача за його telegram_id."""
        if settings.is_admin(telegram_id):
            await self.get_user_role(telegram_id)

        sql = "SELECT * FROM users WHERE telegram_id = ? LIMIT 1;"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (telegram_id,)) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def get_user_role(self, telegram_id: int) -> Optional[str]:
        """
        Повертає роль користувача ('admin', 'realtor', 'pending') або 'deactivated'.
        Якщо telegram_id присутній у settings.admin_id_list, завжди повертає 'admin' та оновлює статус у БД.
        """
        if settings.is_admin(telegram_id):
            sql_admin = """
            INSERT INTO users (telegram_id, username, full_name, role, is_active, created_at)
            VALUES (?, 'admin', 'Admin', 'admin', 1, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET role = 'admin', is_active = 1;
            """
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(sql_admin, (telegram_id, datetime.utcnow().isoformat()))
                await db.commit()
            return "admin"

        sql = "SELECT role, is_active FROM users WHERE telegram_id = ? LIMIT 1;"
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(sql, (telegram_id,)) as cur:
                row = await cur.fetchone()
                if not row:
                    return None
                if row[1] == 0:
                    return "deactivated"
                return str(row[0])

    async def add_user(
        self,
        telegram_id: int,
        username: str = "",
        full_name: str = "",
        role: str = "realtor",
        is_active: bool = True,
    ) -> bool:
        """Додає або оновлює користувача з відповідною роллю."""
        sql = """
        INSERT INTO users (telegram_id, username, full_name, role, is_active, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            username = excluded.username,
            full_name = excluded.full_name,
            role = excluded.role,
            is_active = excluded.is_active;
        """
        now_str = datetime.utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                sql,
                (telegram_id, username, full_name, role, 1 if is_active else 0, now_str),
            )
            # Синхронізуємо зі subscribers для зворотної сумісності
            await db.execute(
                """
                INSERT INTO subscribers (chat_id, username, full_name, is_active, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    username = excluded.username,
                    full_name = excluded.full_name,
                    is_active = excluded.is_active;
                """,
                (telegram_id, username, full_name, 1 if is_active else 0, now_str),
            )
            await db.commit()
            return True

    async def list_users(self) -> list[dict]:
        """Повертає список усіх зареєстрованих користувачів."""
        sql = "SELECT * FROM users ORDER BY created_at DESC;"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def set_user_role(self, telegram_id: int, role: str) -> None:
        """Змінює роль користувача."""
        sql = "UPDATE users SET role = ? WHERE telegram_id = ?;"
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(sql, (role, telegram_id))
            await db.commit()

    async def set_user_status(self, telegram_id: int, is_active: bool) -> None:
        """Змінює статус активності користувача."""
        val = 1 if is_active else 0
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE users SET is_active = ? WHERE telegram_id = ?;", (val, telegram_id))
            await db.execute("UPDATE subscribers SET is_active = ? WHERE chat_id = ?;", (val, telegram_id))
            await db.commit()

    async def get_active_recipients(self) -> list[int]:
        """Повертає список ID користувачів зі статусом active та роллю 'admin' або 'realtor'."""
        sql = "SELECT telegram_id FROM users WHERE is_active = 1 AND role IN ('admin', 'realtor');"
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(sql) as cur:
                rows = await cur.fetchall()
                return [r[0] for r in rows]

    # --- МЕТОДИ КЕРУВАННЯ ПРОКСІ ---

    async def add_proxy(self, url: str) -> bool:
        """Додає новий проксі в базу."""
        sql = """
        INSERT OR IGNORE INTO proxies (url, is_active, status, response_time_ms, created_at)
        VALUES (?, 1, 'unknown', 0.0, ?);
        """
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(sql, (url.strip(), datetime.utcnow().isoformat()))
            await db.commit()
            return cur.rowcount > 0

    async def delete_proxy(self, proxy_id: int) -> bool:
        """Видаляє проксі за ID."""
        sql = "DELETE FROM proxies WHERE id = ?;"
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(sql, (proxy_id,))
            await db.commit()
            return cur.rowcount > 0

    async def get_all_proxies(self) -> list[dict]:
        """Повертає список усіх проксі."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM proxies ORDER BY id ASC;") as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def get_working_proxies(self) -> list[str]:
        """Повертає список URL робочих активних проксі."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            query = "SELECT url FROM proxies WHERE is_active = 1 AND status = 'active';"
            async with db.execute(query) as cur:
                rows = await cur.fetchall()
                return [r["url"] for r in rows]

    async def update_proxy_status(
        self, proxy_id: int, status: str, response_time_ms: float
    ) -> None:
        """Оновлює результат перевірки проксі."""
        sql = """
        UPDATE proxies
        SET status = ?, response_time_ms = ?, last_checked = ?
        WHERE id = ?;
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                sql,
                (
                    status,
                    response_time_ms,
                    datetime.utcnow().isoformat(),
                    proxy_id,
                ),
            )
            await db.commit()

    async def get_stats(self) -> dict:
        """Отримує статистику роботи бази даних."""
        stats = {
            "total_seen": 0,
            "total_sent": 0,
            "active_subscribers": 0,
            "proxies_count": 0,
            "active_proxies_count": 0,
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

            async with db.execute("SELECT COUNT(*) FROM proxies;") as cur:
                row = await cur.fetchone()
                if row:
                    stats["proxies_count"] = row[0]

            async with db.execute("SELECT COUNT(*) FROM proxies WHERE status = 'active';") as cur:
                row = await cur.fetchone()
                if row:
                    stats["active_proxies_count"] = row[0]

        return stats


# Глобальний екземпляр бази даних
db = DatabaseManager()

from pathlib import Path
from typing import Optional
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Конфігурація бота та скрапера з підтримкою .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Telegram Bot
    BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""  # ID абонента/каналу (можна декілька через кому)

    # Інтервал перевірки (у секундах, за ТЗ 2-4 хв = 120-240 с)
    POLL_INTERVAL_SECONDS: int = 180

    # Фільтри пошуку
    CITY_NAME: str = "Дніпро"
    CITY_ID: int = 121  # ID міста Дніпро в системі OLX
    CATEGORY_ID: int = 1760  # 1760 = Оренда квартир, 1758 = Продаж квартир
    MIN_PRICE: Optional[float] = None  # Мінімальна ціна (грн)
    MAX_PRICE: Optional[float] = None  # Максимальна ціна (грн)
    EXCLUDED_DISTRICTS: str = ""  # Райони для виключення через кому

    # Налаштування мережі та антиблокування
    PROXY_URL: Optional[str] = None  # http://user:pass@host:port або socks5://
    REQUEST_TIMEOUT: float = 15.0

    # База даних
    DB_PATH: Path = Path("data/rieltor_bot.db")

    # Фільтрація старих піднятих оголошень (години від публікації)
    MAX_AD_AGE_HOURS: int = 48

    # Чи позначати поточні оголошення прочитаними при першому старті (щоб уникнути спаму)
    INIT_MARK_AS_SEEN: bool = True

    @property
    def excluded_districts(self) -> list[str]:
        """Повертає список районів для виключення."""
        if not self.EXCLUDED_DISTRICTS or not self.EXCLUDED_DISTRICTS.strip():
            return []
        return [d.strip() for d in self.EXCLUDED_DISTRICTS.split(",") if d.strip()]

    @property
    def target_chat_ids(self) -> list[int]:
        """Повертає список валідних числових ID чатів із TELEGRAM_CHAT_ID."""
        if not self.TELEGRAM_CHAT_ID:
            return []
        ids = []
        for raw_id in self.TELEGRAM_CHAT_ID.split(","):
            raw_id = raw_id.strip()
            if not raw_id:
                continue
            try:
                ids.append(int(raw_id))
            except ValueError:
                pass
        return ids


settings = Settings()

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class ListingItem(BaseModel):
    """Модель окремого оголошення про нерухомість."""

    id: str = ""  # Унікальний складений ідентифікатор: 'olx:{external_id}'
    source: str = "OLX.ua"
    external_id: str
    title: str
    price: float
    currency: str = "грн"
    price_label: Optional[str] = None
    rooms: str = "1 кімната"
    city: str = "Дніпро"
    district: Optional[str] = None
    location: str = "м. Дніпро"
    url: str
    image_url: Optional[str] = None
    created_time: Optional[datetime] = None
    last_refresh_time: Optional[datetime] = None
    is_promoted: bool = False
    discovered_at: datetime = Field(default_factory=datetime.utcnow)

    @classmethod
    def create_id(cls, source: str, external_id: str) -> str:
        clean_source = source.lower().replace(".", "_")
        return f"{clean_source}:{external_id}"

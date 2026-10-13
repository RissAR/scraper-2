from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class ListingItem(BaseModel):
    """Модель окремого оголошення про нерухомість з розширеними параметрами."""

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
    photos: list[str] = []
    description: Optional[str] = None
    phone: Optional[str] = None
    floor: Optional[str] = None
    total_floors: Optional[str] = None
    total_area: Optional[str] = None
    kitchen_area: Optional[str] = None
    author_name: Optional[str] = None
    author_ads_count: int = 1
    is_owner: bool = True
    created_time: Optional[datetime] = None
    last_refresh_time: Optional[datetime] = None
    is_promoted: bool = False
    discovered_at: datetime = Field(default_factory=datetime.utcnow)

    @classmethod
    def create_id(cls, source: str, external_id: str) -> str:
        clean_source = source.lower().replace(".", "_")
        return f"{clean_source}:{external_id}"

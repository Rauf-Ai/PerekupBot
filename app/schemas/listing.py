from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class ListingInput:
    source_id: int
    external_id: str
    url: str
    title: str
    brand: str | None = None
    model: str | None = None
    generation: str | None = None
    year: int | None = None
    price: int | None = None
    mileage: int | None = None
    city: str | None = None
    region: str | None = None
    description: str | None = None
    seller_name: str | None = None
    seller_type: str | None = None
    phone: str | None = None
    photos: list[str] = field(default_factory=list)
    published_at: datetime | None = None
    original_text: str | None = None


@dataclass(slots=True)
class ScoreResult:
    score: int
    reasons: list[str]
    warnings: list[str]


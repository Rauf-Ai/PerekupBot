from datetime import datetime, timezone
from app.db.models import Listing
from app.schemas.listing import ScoreResult


def score_listing(listing: Listing, price_drop: int = 0) -> ScoreResult:
    score = 30
    reasons: list[str] = []
    warnings: list[str] = []
    if listing.published_at:
        published = listing.published_at if listing.published_at.tzinfo else listing.published_at.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - published).total_seconds()
        if 0 <= age <= 3600:
            score += 20
            reasons.append("свежее объявление")
    else:
        warnings.append("время публикации неизвестно")
    if listing.price is not None:
        if listing.price <= 200000:
            score += 12
            reasons.append("цена до 200 000 ₽")
    if listing.year is not None and listing.year >= 2010:
        score += 8
        reasons.append("год выпуска от 2010")
    if listing.mileage is not None and listing.mileage <= 200000:
        score += 8
        reasons.append("пробег до 200 000 км")
    if listing.seller_type == "private":
        score += 7
        reasons.append("частный продавец")
    if listing.brand and listing.model and listing.city:
        score += 5
        reasons.append("основные данные заполнены")
    if len(listing.photos) >= 3:
        score += 5
        reasons.append("есть фотографии")
    if price_drop > 0:
        score += 5
        reasons.append(f"цена снижена на {price_drop:,} ₽".replace(",", " "))
    if listing.price is None or listing.year is None:
        warnings.append("часть данных не указана продавцом")
    return ScoreResult(min(score, 100), reasons, warnings)

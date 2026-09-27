from difflib import SequenceMatcher
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.db.models import Listing, ListingAlias
from app.schemas.listing import ListingInput


def _similar(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0
    return SequenceMatcher(None, " ".join(a.casefold().split()), " ".join(b.casefold().split())).ratio()


def find_duplicate(db: Session, item: ListingInput) -> Listing | None:
    alias = db.scalar(select(ListingAlias).where(ListingAlias.source_id == item.source_id, ListingAlias.external_id == item.external_id))
    if alias:
        return db.get(Listing, alias.listing_id)
    direct = db.scalar(select(Listing).where(Listing.source_id == item.source_id, Listing.external_id == item.external_id))
    if direct:
        return direct
    alias = db.scalar(select(ListingAlias).where(ListingAlias.url == item.url))
    if alias:
        return db.get(Listing, alias.listing_id)
    direct = db.scalar(select(Listing).where(Listing.url == item.url))
    if direct:
        return direct
    if item.phone:
        candidates = db.scalars(select(Listing).where(Listing.phone == item.phone).limit(30)).all()
        for candidate in candidates:
            same_car = (item.brand and candidate.brand and item.brand.casefold() == candidate.brand.casefold()
                        and item.model and candidate.model and item.model.casefold() == candidate.model.casefold())
            if same_car and (item.year is None or candidate.year is None or item.year == candidate.year):
                same_detail = ((item.mileage is not None and candidate.mileage is not None and item.mileage == candidate.mileage)
                               or (item.price is not None and candidate.price is not None and item.price == candidate.price))
                same_city = not item.city or not candidate.city or item.city.casefold() == candidate.city.casefold()
                if same_detail and same_city and _similar(item.original_text or item.title, candidate.original_text or candidate.title) >= 0.75:
                    return candidate
    if item.brand and item.model and item.year and item.city and item.price:
        candidates = db.scalars(select(Listing).where(
            Listing.brand.ilike(item.brand), Listing.model.ilike(item.model), Listing.year == item.year,
            Listing.city.ilike(item.city), Listing.price == item.price).limit(30)).all()
        for candidate in candidates:
            if _similar(item.original_text or item.title, candidate.original_text or candidate.title) >= 0.72:
                return candidate
    return None

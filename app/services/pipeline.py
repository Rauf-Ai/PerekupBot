import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload
from app.db.models import Listing, ListingAlias, ListingPhoto, Notification, PriceHistory, Source, User, UserFilter, UserTelegramSource
from app.db.session import SessionLocal
from app.config.settings import get_settings
from app.schemas.listing import ListingInput
from app.services.deduplication import find_duplicate
from app.services.filtering import matches_filter
from app.services.custom_sources import custom_source_limit
from app.services.normalization import normalize_listing

log = logging.getLogger(__name__)


@dataclass
class IngestResult:
    listing_id: int
    new: bool
    duplicate: bool
    matched: int
    notification_ids: list[int]


def ingest(item: ListingInput) -> IngestResult:
    item = normalize_listing(item)
    now = datetime.now(timezone.utc)
    with SessionLocal.begin() as db:
        listing = find_duplicate(db, item)
        is_new = listing is None
        old_price = listing.price if listing else None
        if listing is None:
            listing = Listing(source_id=item.source_id, external_id=item.external_id, url=item.url,
                              title=item.title, first_seen_at=now)
            db.add(listing)
            db.flush()
        else:
            listing.last_seen_at = now
        alias = db.scalar(select(ListingAlias).where(ListingAlias.source_id == item.source_id,
                                                    ListingAlias.external_id == item.external_id))
        if alias is None:
            db.add(ListingAlias(listing_id=listing.id, source_id=item.source_id,
                                external_id=item.external_id, url=item.url))
        if is_new or listing.source_id == item.source_id:
            for name in ("title", "brand", "model", "generation", "year", "mileage", "city", "region",
                         "description", "seller_name", "seller_type", "phone", "published_at", "original_text"):
                value = getattr(item, name)
                if value is not None or is_new:
                    setattr(listing, name, value)
            if item.photos:
                listing.photos.clear()
                listing.photos.extend(ListingPhoto(position=i, url=url) for i, url in enumerate(item.photos))
        if item.price is not None and (is_new or listing.source_id == item.source_id):
            listing.price = item.price
        event_key = None
        if listing.price is not None and (is_new or old_price != listing.price):
            history = PriceHistory(listing_id=listing.id, old_price=old_price, new_price=listing.price, observed_at=now)
            db.add(history)
            db.flush()
            if is_new or old_price is None:
                event_key = "new"
            elif old_price is not None and listing.price < old_price:
                event_key = f"price:{history.id}"
        if event_key:
            source = db.get(Source, item.source_id)
            source_kind = source.kind
            is_custom_source = bool((source.config or {}).get("custom"))
            subscriber_ids = (set(db.scalars(select(UserTelegramSource.user_id).where(
                UserTelegramSource.source_id == source.id)).all()) if is_custom_source else set())
            settings = get_settings()
            admin_ids = {settings.telegram_admin_id} if settings.telegram_admin_id is not None else set()
            admin_ids.update(int(value.strip()) for value in settings.telegram_admin_ids.split(",")
                             if value.strip().isdigit())
            access_query = (User.telegram_id.in_(admin_ids) if admin_ids else False)
            access_query = or_(access_query, (User.active.is_(True) & or_(
                User.subscription_expires_at.is_(None),
                User.subscription_expires_at > now,
            )))
            users = db.scalars(select(User).options(selectinload(User.filters)).where(access_query)).all()
        else:
            users = []
        ids = []
        for user in users:
            filters = user.filters
            if (filters and filters.search_enabled and
                    source_kind in (filters.selected_sources or ["telegram", "avito", "autoru", "drom"]) and
                    (not is_custom_source or (user.id in subscriber_ids and custom_source_limit(
                        user.subscription_plan, is_admin=user.telegram_id in admin_ids))) and
                    matches_filter(listing, filters)):
                notification = Notification(user_id=user.id, listing_id=listing.id, event_key=event_key, created_at=now)
                db.add(notification)
                db.flush()
                ids.append(notification.id)
        result = IngestResult(listing.id, is_new, not is_new, len(ids), ids)
    if item.published_at:
        published = item.published_at if item.published_at.tzinfo else item.published_at.replace(tzinfo=timezone.utc)
        delay = (now - published).total_seconds()
        log.info("listing_detected listing_id=%s source_id=%s detection_delay_seconds=%.1f", result.listing_id, item.source_id, delay)
    return result

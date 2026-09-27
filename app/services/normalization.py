import re
from urllib.parse import urlsplit, urlunsplit
from app.schemas.listing import ListingInput
from app.extractors.rules import CITY_REGION, parse_int


def canonical_url(value: str) -> str:
    parts = urlsplit(value.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower().removeprefix("www."), parts.path.rstrip("/"), "", ""))


def normalize_listing(item: ListingInput) -> ListingInput:
    item.url = canonical_url(item.url)
    item.title = re.sub(r"\s+", " ", item.title).strip()[:500]
    item.price = parse_int(item.price)
    item.mileage = parse_int(item.mileage)
    item.year = parse_int(item.year)
    if item.city:
        item.city = item.city.strip().title()
        item.region = CITY_REGION.get(item.city.casefold(), item.region)
    item.photos = list(dict.fromkeys(p for p in item.photos if p))[:20]
    if item.phone:
        item.phone = re.sub(r"\D", "", item.phone)
        if len(item.phone) == 11 and item.phone.startswith("8"):
            item.phone = "7" + item.phone[1:]
    return item


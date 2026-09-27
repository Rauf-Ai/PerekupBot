from app.collectors.apify import ApifyCollector
import re

from app.schemas.listing import ListingInput


_MILEAGE_RE = re.compile(
    r"(?<!\d)(\d{1,3}(?:[\s\u00a0.]\d{3})+|\d+)\s*(?:км|km)(?![а-яёa-z])",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"(?<!\d)(?:19[5-9]\d|20[0-3]\d)(?!\d)")


def _number(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    digits = re.sub(r"\D", "", str(value))
    return int(digits) if digits else None


def _title_specs(title: str) -> tuple[int | None, int | None]:
    year_match = _YEAR_RE.search(title)
    mileage_match = _MILEAGE_RE.search(title)
    year = int(year_match.group()) if year_match else None
    mileage = _number(mileage_match.group(1)) if mileage_match else None
    return year, mileage


class AvitoCollector(ApifyCollector):
    kind = "avito"

    def map_row(self, row: dict) -> ListingInput | None:
        if row.get("isNew") is True:
            return None
        item = self.common(row, row.get("itemId") or row.get("id"))
        if item:
            title_year, title_mileage = _title_specs(item.title)
            item.year = item.year or _number(row.get("year")) or title_year
            item.mileage = (item.mileage or _number(row.get("mileageKm"))
                            or _number(row.get("mileage")) or title_mileage)
            address = row.get("locationAddress") or row.get("city") or row.get("location")
            if isinstance(address, dict):
                address = address.get("city") or address.get("name")
            item.city = (str(address).split(",", 1)[0].strip() if address else None)
            item.city = item.city or self.source.config.get("city_fallback")
            item.seller_type = ("dealer" if row["isShop"] else "private") if "isShop" in row else None
        return item

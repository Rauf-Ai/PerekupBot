from app.collectors.apify import ApifyCollector
from app.schemas.listing import ListingInput


class DromCollector(ApifyCollector):
    kind = "drom"

    def map_row(self, row: dict) -> ListingInput | None:
        if row.get("sold"):
            return None
        return self.common(row, row.get("bullId"))


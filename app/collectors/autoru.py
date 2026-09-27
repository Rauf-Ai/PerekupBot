from app.collectors.apify import ApifyCollector
from app.schemas.listing import ListingInput


class AutoRuCollector(ApifyCollector):
    kind = "autoru"

    def map_row(self, row: dict) -> ListingInput | None:
        if row.get("changeType") == "DELISTED":
            return None
        if row.get("currentPrice") is not None:
            row = {**row, "price": row["currentPrice"]}
        return self.common(row, row.get("listingId"))


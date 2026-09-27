from app.collectors.base import BaseCollector
from app.schemas.listing import ListingInput


class MockCollector(BaseCollector):
    kind = "mock"

    async def collect(self) -> list[ListingInput]:
        return [ListingInput(source_id=self.source.id, external_id=str(row["external_id"]),
                             url=row["url"], title=row["title"], brand=row.get("brand"),
                             model=row.get("model"), year=row.get("year"), price=row.get("price"),
                             mileage=row.get("mileage"), city=row.get("city"),
                             region=row.get("region") or self.source.region,
                             description=row.get("description"), photos=row.get("photos", []),
                             original_text=row.get("description") or row["title"])
                for row in self.source.config.get("items", [])]


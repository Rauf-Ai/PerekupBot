from app.collectors.base import BaseCollector
from app.schemas.listing import ListingInput


class VKCollector(BaseCollector):
    kind = "vk"

    async def collect(self) -> list[ListingInput]:
        raise RuntimeError("VK source is not configured: add an approved VK API integration before enabling")


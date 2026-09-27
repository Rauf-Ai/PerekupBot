from abc import ABC, abstractmethod
from app.db.models import Source
from app.schemas.listing import ListingInput


class BaseCollector(ABC):
    kind: str

    def __init__(self, source: Source):
        self.source = source

    @abstractmethod
    async def collect(self) -> list[ListingInput]:
        raise NotImplementedError


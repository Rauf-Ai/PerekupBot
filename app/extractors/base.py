from abc import ABC, abstractmethod
from app.schemas.listing import ListingInput


class ListingExtractor(ABC):
    @abstractmethod
    def extract(self, text: str, *, source_id: int, external_id: str, url: str, region: str | None) -> ListingInput | None:
        raise NotImplementedError


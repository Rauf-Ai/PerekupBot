from pathlib import Path
from telethon import TelegramClient
from app.collectors.base import BaseCollector
from app.extractors.rules import RuleListingExtractor
from app.config.settings import get_settings
from app.schemas.listing import ListingInput


class TelegramCollector(BaseCollector):
    kind = "telegram"

    def __init__(self, source, client: TelegramClient):
        super().__init__(source)
        self.client = client
        self.extractor = RuleListingExtractor()

    async def from_message(self, message) -> ListingInput | None:
        text = message.raw_text or ""
        identifier = self.source.identifier
        if identifier.startswith("-100") and identifier[1:].isdigit():
            url = f"https://t.me/c/{identifier[4:]}/{message.id}"
        else:
            url = f"https://t.me/{identifier}/{message.id}"
        item = self.extractor.extract(text, source_id=self.source.id, external_id=str(message.id),
                                      url=url, region=self.source.region)
        if not item:
            return None
        if not item.city and (self.source.config or {}).get("city_fallback"):
            item.city = self.source.config["city_fallback"]
        item.published_at = message.date
        if message.photo:
            folder = Path(get_settings().telegram_session_path).parent / "photos"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"tg-{self.source.id}-{message.id}.jpg"
            if not path.exists():
                saved = await self.client.download_media(message, file=str(path))
                if saved:
                    item.photos = [str(saved)]
            else:
                item.photos = [str(path)]
        return item

    async def collect(self) -> list[ListingInput]:
        # Reconcile recent posts and edits. On first run, establish a cursor without old alerts.
        identifier = self.source.identifier
        peer = int(identifier) if identifier.startswith("-100") and identifier[1:].isdigit() else identifier
        messages = [message async for message in self.client.iter_messages(peer, limit=25)]
        if not messages:
            return []
        if self.source.cursor is None:
            self.source.cursor = str(max(message.id for message in messages))
            return []
        current_cursor = int(self.source.cursor)
        items = []
        for message in reversed(messages):
            recently_edited = (message.edit_date is not None and self.source.last_checked_at is not None
                               and message.edit_date > self.source.last_checked_at)
            if message.id > current_cursor or recently_edited:
                item = await self.from_message(message)
                if item:
                    items.append(item)
        self.source.cursor = str(max(current_cursor, max(message.id for message in messages)))
        return items

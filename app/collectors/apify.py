import asyncio
import logging
import threading
from datetime import datetime, timedelta
from apify_client import ApifyClient
from app.config.settings import get_settings
from app.collectors.base import BaseCollector
from app.schemas.listing import ListingInput
from app.extractors.rules import extract_brand_model


logger = logging.getLogger(__name__)
_token_lock = threading.Lock()
_next_token_index = 0
_quota_exhausted_tokens: set[str] = set()


def _configured_tokens() -> list[str]:
    settings = get_settings()
    raw_tokens = [settings.apify_tokens, settings.apify_token]
    tokens = []
    for raw in raw_tokens:
        tokens.extend(part.strip() for part in raw.replace(";", ",").replace("\n", ",").split(",") if part.strip())
    return list(dict.fromkeys(tokens))


def _next_available_token(tokens: list[str]) -> str | None:
    global _next_token_index
    with _token_lock:
        for _ in range(len(tokens)):
            index = _next_token_index % len(tokens)
            _next_token_index = (index + 1) % len(tokens)
            token = tokens[index]
            if token not in _quota_exhausted_tokens:
                return token
    return None


def _is_monthly_limit_error(error: Exception) -> bool:
    message = str(error).lower()
    return "monthly usage hard limit exceeded" in message or "monthly usage limit exceeded" in message


def _mark_token_exhausted(token: str) -> None:
    with _token_lock:
        _quota_exhausted_tokens.add(token)


def _date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class ApifyAdapter:
    def __init__(self, token: str):
        self.client = ApifyClient(token)

    def run(self, actor_id: str, actor_input: dict, max_items: int | None = None,
            max_total_charge_usd: float | None = None) -> list[dict]:
        result = self.client.actor(actor_id).call(run_input=actor_input,
                                                  max_items=max_items,
                                                  max_total_charge_usd=max_total_charge_usd,
                                                  run_timeout=timedelta(seconds=300),
                                                  wait_duration=timedelta(seconds=330))
        if result is None or result.status != "SUCCEEDED":
            raise RuntimeError(f"Apify Actor did not succeed: {result.status if result else 'timeout'}")
        dataset_id = result.default_dataset_id
        if not dataset_id:
            raise RuntimeError("Apify Actor returned no dataset")
        return list(self.client.dataset(dataset_id).iterate_items())


class ApifyCollector(BaseCollector):
    kind = "apify"

    async def collect(self) -> list[ListingInput]:
        tokens = _configured_tokens()
        if not tokens:
            raise RuntimeError("APIFY_TOKEN or APIFY_TOKENS is required for enabled Apify sources")
        actor_input = self.source.config.get("input", {})
        # Apify's max_items also caps billed results for pay-per-result Actors.
        max_items = actor_input.get("maxItems") or actor_input.get("maxResults") or 20
        max_charge = self.source.config.get("max_total_charge_usd")
        rows = None
        for _ in range(len(tokens)):
            token = _next_available_token(tokens)
            if token is None:
                break
            try:
                rows = await asyncio.to_thread(ApifyAdapter(token).run, self.source.identifier,
                                               actor_input, max_items, max_charge)
                break
            except Exception as error:
                if not _is_monthly_limit_error(error):
                    raise
                _mark_token_exhausted(token)
                logger.warning("Apify monthly hard limit reached for one API account; trying next configured token")
        if rows is None:
            raise RuntimeError("All configured Apify API tokens have reached their monthly usage limit")
        items = []
        for row in rows:
            item = self.map_row(row)
            if item:
                items.append(item)
        return items

    def map_row(self, row: dict) -> ListingInput | None:
        raise NotImplementedError

    def common(self, row: dict, external_id: object, **kwargs) -> ListingInput | None:
        if external_id is None or not row.get("url") or not row.get("title"):
            return None
        inferred_brand, inferred_model = extract_brand_model(row["title"])
        return ListingInput(source_id=self.source.id, external_id=str(external_id), url=row["url"],
                            title=row["title"], brand=row.get("make") or inferred_brand,
                            model=row.get("model") or inferred_model,
                            generation=row.get("generation"), year=row.get("year"),
                            price=row.get("price"), mileage=row.get("mileageKm"),
                            city=row.get("city") or row.get("location"), region=self.source.region,
                            description=row.get("description"), seller_name=row.get("dealerName"),
                            seller_type=row.get("sellerType"),
                            photos=row.get("photos") or row.get("imageUrls") or row.get("images") or [],
                            published_at=_date(row.get("publishedAt") or row.get("postedAt")),
                            original_text=row.get("description") or row.get("title"), **kwargs)

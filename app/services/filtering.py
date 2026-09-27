from app.db.models import Listing, UserFilter
import json
from pathlib import Path


_CATALOG_PATH = Path(__file__).resolve().parents[1] / "config" / "vehicle_catalog.json"
_BRAND_CATALOG = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))["brands"]


def _brand_names(value: str | None) -> set[str]:
    normalized = (value or "").strip().casefold()
    if not normalized:
        return set()
    for item in _BRAND_CATALOG:
        names = [item["name"], *item.get("aliases", [])]
        if normalized in {name.casefold() for name in names}:
            return {name.casefold() for name in names}
    return {normalized}


def _matches_catalog_model(listing: Listing, model: str) -> bool:
    actual = (listing.model or "").strip().casefold()
    expected = model.strip().casefold()
    if actual == expected:
        return True
    title = (listing.title or "").casefold()
    return len(expected) >= 3 and expected in title


def matches_filter(listing: Listing, filters: UserFilter) -> bool:
    if listing.price is None or listing.price > filters.max_price:
        return False
    if not listing.region or listing.region.casefold() not in {x.casefold() for x in filters.regions}:
        return False
    city_map = filters.cities_by_region or {}
    region_cities = next((cities for region, cities in city_map.items()
                          if listing.region and region.casefold() == listing.region.casefold()), None)
    if region_cities is not None:
        # An empty list means that the user entered manual city selection but
        # has not selected any cities yet, so that region should not match.
        if not listing.city or listing.city.casefold() not in {city.casefold() for city in region_cities}:
            return False
    elif filters.cities and (not listing.city or listing.city.casefold() not in {x.casefold() for x in filters.cities}):
        return False
    if filters.brands:
        selected_brands = set().union(*(_brand_names(value) for value in filters.brands))
        if not (_brand_names(listing.brand) & selected_brands):
            return False
    models_by_brand = filters.models_by_brand or {}
    if models_by_brand:
        listing_brand = _brand_names(listing.brand)
        selected_brand_name = next((brand for brand in models_by_brand
                                    if listing_brand & _brand_names(brand)), None)
        if selected_brand_name is not None:
            selected_models = models_by_brand[selected_brand_name]
            if not selected_models or not any(_matches_catalog_model(listing, model) for model in selected_models):
                return False
    if filters.models and (not listing.model or listing.model.casefold() not in {x.casefold() for x in filters.models}):
        return False
    if listing.model and listing.model.casefold() in {x.casefold() for x in filters.hidden_models}:
        return False
    if filters.min_year is not None and (listing.year is None or listing.year < filters.min_year):
        return False
    if filters.max_mileage is not None and (listing.mileage is None or listing.mileage > filters.max_mileage):
        return False
    if filters.seller_type and listing.seller_type != filters.seller_type:
        return False
    return True

from app.db.models import Listing, UserFilter


def matches_filter(listing: Listing, filters: UserFilter) -> bool:
    if listing.price is None or listing.price > filters.max_price:
        return False
    if not listing.region or listing.region.casefold() not in {x.casefold() for x in filters.regions}:
        return False
    if filters.cities and (not listing.city or listing.city.casefold() not in {x.casefold() for x in filters.cities}):
        return False
    if filters.brands and (not listing.brand or listing.brand.casefold() not in {x.casefold() for x in filters.brands}):
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

import re
from app.extractors.base import ListingExtractor
from app.schemas.listing import ListingInput

BRANDS = {
    "лада": "Lada", "ваз": "Lada", "lada": "Lada", "toyota": "Toyota", "тойота": "Toyota",
    "renault": "Renault", "рено": "Renault", "kia": "Kia", "киа": "Kia", "hyundai": "Hyundai",
    "хендай": "Hyundai", "volkswagen": "Volkswagen", "фольксваген": "Volkswagen", "nissan": "Nissan",
    "ниссан": "Nissan", "chevrolet": "Chevrolet", "шевроле": "Chevrolet", "ford": "Ford", "форд": "Ford",
    "skoda": "Skoda", "шкода": "Skoda", "opel": "Opel", "опель": "Opel", "daewoo": "Daewoo",
    "дэу": "Daewoo", "газ": "GAZ", "уаз": "UAZ", "bmw": "BMW", "audi": "Audi",
    "mercedes": "Mercedes-Benz", "мерседес": "Mercedes-Benz", "mitsubishi": "Mitsubishi", "мицубиси": "Mitsubishi",
    "mazda": "Mazda", "мазда": "Mazda", "honda": "Honda", "хонда": "Honda", "subaru": "Subaru",
    "субару": "Subaru", "suzuki": "Suzuki", "сузуки": "Suzuki", "peugeot": "Peugeot", "пежо": "Peugeot",
    "citroen": "Citroen", "ситроен": "Citroen", "chery": "Chery", "чери": "Chery", "geely": "Geely",
    "джили": "Geely", "lifan": "Lifan", "лифан": "Lifan", "datsun": "Datsun", "датсун": "Datsun",
    "ravon": "Ravon", "равон": "Ravon", "fiat": "Fiat", "фиат": "Fiat", "haval": "Haval", "хавал": "Haval",
}
CITY_REGION = {
    "казань": "Татарстан", "набережные челны": "Татарстан", "нижнекамск": "Татарстан", "альметьевск": "Татарстан",
    "чебоксары": "Чувашия", "новочебоксарск": "Чувашия", "канаш": "Чувашия",
    "йошкар-ола": "Марий Эл", "волжск": "Марий Эл", "козьмодемьянск": "Марий Эл",
}
PRICE_RE = re.compile(r"(?<!\d)(\d[\d\s\u00a0]{2,8})\s*(?:₽|руб(?:\.|лей)?|р\b)", re.I)
PRICE_SHORT_RE = re.compile(r"(?<!\d)(\d{2,4})\s*(?:тыс(?!\s*\.?\s*км)\.?|т\.?\s*р\.?|к\b)", re.I)
PRICE_LABEL_RE = re.compile(r"(?:цена|стоимость)\s*[:\-]?\s*(\d[\d\s\u00a0]{4,8})(?!\s*км)", re.I)
YEAR_RE = re.compile(r"\b(?:19[8-9]\d|20[0-2]\d)\b")
MILEAGE_RE = re.compile(r"(?:пробег\s*[:\-]?\s*)?(\d[\d\s\u00a0]{1,7})\s*(?:км|тыс\.?\s*км)", re.I)
PHONE_RE = re.compile(r"(?:\+7|8)[\s(\-]*\d{3}[)\s\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")


def parse_int(value: str | int | None) -> int | None:
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value))
    return int(digits) if digits else None


def extract_brand_model(text: str) -> tuple[str | None, str | None]:
    lower = text.casefold()
    for token, brand in BRANDS.items():
        match = re.search(rf"(?<!\w){re.escape(token)}(?!\w)\s+([A-Za-zА-Яа-я0-9-]+)?", lower)
        if match:
            candidate = match.group(1)
            if candidate and YEAR_RE.fullmatch(candidate):
                candidate = None
            return brand, candidate.title() if candidate else None
    return None, None


class RuleListingExtractor(ListingExtractor):
    def extract(self, text: str, *, source_id: int, external_id: str, url: str, region: str | None) -> ListingInput | None:
        clean = text.strip()
        if not clean:
            return None
        first_line = clean.splitlines()[0].casefold()
        if re.search(r"\b(?:куплю|ищу|запчасти|разбор|аренда)\b", first_line):
            return None
        price_match = PRICE_RE.search(clean)
        price = parse_int(price_match.group(1)) if price_match else None
        if price is None:
            short_match = PRICE_SHORT_RE.search(clean)
            price = parse_int(short_match.group(1)) * 1000 if short_match else None
        if price is None:
            label_match = PRICE_LABEL_RE.search(clean)
            price = parse_int(label_match.group(1)) if label_match else None
        if price is None or price < 10000:
            return None
        brand, model = extract_brand_model(clean)
        if brand is None:
            return None
        lower = clean.casefold()
        city = next((name.title() for name in CITY_REGION if name in lower), None)
        if city:
            region = CITY_REGION[city.casefold()]
        year_match = YEAR_RE.search(clean)
        mileage_match = MILEAGE_RE.search(clean)
        mileage = parse_int(mileage_match.group(1)) if mileage_match else None
        if mileage_match and "тыс" in mileage_match.group(0).lower() and mileage is not None:
            mileage *= 1000
        phone_match = PHONE_RE.search(clean)
        lines = [line.strip() for line in clean.splitlines() if line.strip()]
        title = lines[0][:500]
        return ListingInput(source_id=source_id, external_id=external_id, url=url, title=title, brand=brand, model=model,
                            year=int(year_match.group()) if year_match else None, price=price, mileage=mileage,
                            city=city, region=region, description=clean, phone=phone_match.group() if phone_match else None,
                            original_text=clean)

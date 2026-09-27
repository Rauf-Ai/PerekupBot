import json
from pathlib import Path
from sqlalchemy import select
from app.config.settings import get_settings
from app.db.models import Source
from app.db.session import SessionLocal


def seed_sources() -> None:
    path = Path(get_settings().sources_config)
    definitions = json.loads(path.read_text(encoding="utf-8"))
    with SessionLocal.begin() as db:
        configured_keys = {item["key"] for item in definitions}
        for stored in db.scalars(select(Source)).all():
            if stored.key not in configured_keys:
                stored.enabled = False
        for item in definitions:
            source = db.scalar(select(Source).where(Source.key == item["key"]))
            if source is None:
                source = Source(key=item["key"], kind=item["kind"], identifier=item["identifier"])
                db.add(source)
            elif source.identifier != item["identifier"] or source.kind != item["kind"]:
                source.cursor = None
            source.kind = item["kind"]
            source.identifier = item["identifier"]
            source.region = item.get("region")
            source.config = item.get("config", {})
            source.enabled = item.get("enabled", True)
            source.interval_seconds = max(15, item.get("interval_seconds", 60))

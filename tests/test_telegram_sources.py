import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.collectors.apify import _is_monthly_limit_error
from app.collectors.telegram import TelegramCollector
from app.bot.handlers import CustomTelegramFlow, GEO_REGIONS, custom_region
from app.db.models import Base, Source, User, UserFilter, UserTelegramSource
from app.schemas.listing import ListingInput
from app.services.custom_sources import custom_source_limit, parse_telegram_source
from app.services.pipeline import ingest
from app.workers.main import maintain_telegram_connection, source_needed


class TelegramSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{Path(self.temp.name) / 'test.db'}")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        settings = SimpleNamespace(telegram_admin_id=999, telegram_admin_ids="")
        self.patches = [
            patch("app.services.pipeline.SessionLocal", self.sessions),
            patch("app.workers.main.SessionLocal", self.sessions),
            patch("app.services.pipeline.get_settings", return_value=settings),
            patch("app.workers.main.get_settings", return_value=settings),
            patch("app.bot.handlers.SessionLocal", self.sessions),
            patch("app.bot.handlers.get_settings", return_value=settings),
        ]
        for item in self.patches:
            item.start()
        with self.sessions.begin() as db:
            self.source = Source(key="custom", kind="telegram", identifier="customchannel",
                                 region="Татарстан", config={"custom": True}, enabled=True,
                                 interval_seconds=30)
            subscriber = User(telegram_id=101, active=True, subscription_plan="pro")
            subscriber.filters = UserFilter(max_price=300000, regions=["Татарстан"],
                                            selected_sources=["telegram"])
            other = User(telegram_id=102, active=True, subscription_plan="pro")
            other.filters = UserFilter(max_price=300000, regions=["Татарстан"],
                                       selected_sources=["telegram"])
            db.add_all([self.source, subscriber, other])
            db.flush()
            db.add(UserTelegramSource(user_id=subscriber.id, source_id=self.source.id))
            self.source_id, self.subscriber_id = self.source.id, subscriber.id

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.engine.dispose()
        self.temp.cleanup()

    def test_personal_channel_notifies_only_its_subscriber(self):
        item = ListingInput(source_id=self.source_id, external_id="42",
                            url="https://t.me/customchannel/42", title="Lada Granta 2013",
                            brand="Lada", model="Granta", price=190000,
                            city="Казань", region="Татарстан")
        result = ingest(item)
        self.assertEqual(result.matched, 1)
        with self.sessions.begin() as db:
            source = db.get(Source, self.source_id)
            self.assertTrue(source_needed(source))
            subscriber = db.get(User, self.subscriber_id)
            subscriber.filters.regions = ["Марий Эл"]
        with self.sessions() as db:
            self.assertFalse(source_needed(db.get(Source, self.source_id)))

    def test_private_channel_link_and_city_fallback(self):
        with self.sessions() as db:
            source = db.get(Source, self.source_id)
            source.identifier = "-1002112478292"
            source.config = {"city_fallback": "Чебоксары"}
            source.region = "Чувашия"
            message = SimpleNamespace(id=55, raw_text="Lada Granta 2013\nЦена 195000 руб.",
                                      date=None, photo=None)
            item = asyncio.run(TelegramCollector(source, None).from_message(message))
        self.assertEqual(item.url, "https://t.me/c/2112478292/55")
        self.assertEqual(item.city, "Чебоксары")

    def test_link_validation_and_plan_entitlements(self):
        self.assertEqual(parse_telegram_source("https://t.me/auto_tatar16"),
                         ("auto_tatar16", "@auto_tatar16"))
        self.assertEqual(parse_telegram_source("https://t.me/+Pmr7-3DyAJpiNmI8"),
                         ("invite:Pmr7-3DyAJpiNmI8", "Закрытый чат"))
        self.assertIsNone(parse_telegram_source("https://t.me/auto_tatar16/123"))
        self.assertEqual(custom_source_limit("plus"), 0)
        self.assertEqual(custom_source_limit("pro"), 3)
        self.assertEqual(custom_source_limit("team"), 20)

    def test_apify_rotates_when_remaining_credit_is_insufficient(self):
        self.assertTrue(_is_monthly_limit_error(Exception(
            "Your remaining usage of $0.002412 this billing cycle isn't enough for this run.")))

    def test_user_can_add_personal_channel_for_selected_region(self):
        index = next(i for i, region in enumerate(GEO_REGIONS) if region["name"] == "Татарстан")
        callback = SimpleNamespace(data=f"custom:region:{index}",
                                   from_user=SimpleNamespace(id=101),
                                   message=SimpleNamespace(edit_text=AsyncMock()),
                                   answer=AsyncMock())
        state = SimpleNamespace(get_state=AsyncMock(return_value=CustomTelegramFlow.region.state),
                                get_data=AsyncMock(return_value={"identifier": "anotherchannel",
                                                                 "title": "@anotherchannel"}),
                                clear=AsyncMock())
        asyncio.run(custom_region(callback, state))
        with self.sessions() as db:
            source = db.scalar(select(Source).where(Source.identifier == "anotherchannel"))
            self.assertIsNotNone(source)
            self.assertEqual(source.region, "Татарстан")
            self.assertIsNotNone(db.scalar(select(UserTelegramSource).where(
                UserTelegramSource.source_id == source.id,
                UserTelegramSource.user_id == self.subscriber_id)))

    def test_telegram_client_reconnects_after_disconnect(self):
        async def exercise():
            connected = asyncio.Event()

            class FakeClient:
                def __init__(self):
                    self.online = False
                    self.runs = 0

                async def run_until_disconnected(self):
                    self.runs += 1
                    if self.runs == 1:
                        return
                    await asyncio.Event().wait()

                async def connect(self):
                    self.online = True
                    connected.set()

                async def is_user_authorized(self):
                    return True

                def is_connected(self):
                    return self.online

            client = FakeClient()
            task = asyncio.create_task(maintain_telegram_connection(client))
            await asyncio.wait_for(connected.wait(), timeout=1)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return client

        client = asyncio.run(exercise())
        self.assertTrue(client.online)
        self.assertGreaterEqual(client.runs, 2)


if __name__ == "__main__":
    unittest.main()

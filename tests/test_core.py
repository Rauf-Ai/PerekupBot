import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from sqlalchemy import BigInteger, create_engine, select
from sqlalchemy.orm import sessionmaker
from app.db.models import Base, Listing, Notification, PriceHistory, Source, User, UserFilter
from app.collectors.avito import AvitoCollector
from app.collectors.apify import ApifyAdapter
from app.collectors.autoru import AutoRuCollector
from app.collectors.drom import DromCollector
from app.collectors.telegram import TelegramCollector
from app.bot.handlers import AdminMiddleware
from app.extractors.rules import RuleListingExtractor
from app.schemas.listing import ListingInput
from app.services.normalization import normalize_listing
from app.services.pipeline import ingest
from app.services.notifications import build_card, send_notification


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{Path(self.temp.name) / 'test.db'}")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.patch = patch("app.services.pipeline.SessionLocal", self.sessions)
        self.patch.start()
        with self.sessions.begin() as db:
            source = Source(key="test", kind="mock", identifier="test", region="Татарстан", config={})
            user = User(telegram_id=123)
            user.filters = UserFilter(max_price=200000, regions=["Татарстан", "Чувашия", "Марий Эл"])
            db.add_all([source, user])
            db.flush()
            self.source_id = source.id

    def tearDown(self):
        self.patch.stop()
        self.engine.dispose()
        self.temp.cleanup()

    def item(self, external_id="1", price=195000):
        return ListingInput(source_id=self.source_id, external_id=external_id,
                            url=f"https://example.org/car/{external_id}?tracking=1", title="Lada Granta 2013",
                            brand="Lada", model="Granta", year=2013, price=price,
                            mileage=170000, city="Казань", region="Татарстан",
                            original_text="Lada Granta 2013 Казань 170000 км")

    def test_new_duplicate_and_price_drop(self):
        initial = ingest(self.item(price=230000))
        self.assertTrue(initial.new)
        self.assertEqual(initial.notification_ids, [])
        duplicate = ingest(self.item(price=230000))
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(duplicate.notification_ids, [])
        drop = ingest(self.item(price=195000))
        self.assertEqual(len(drop.notification_ids), 1)
        with self.sessions() as db:
            listing = db.get(Listing, initial.listing_id)
            self.assertEqual(listing.price, 195000)
            self.assertEqual(listing.region, "Татарстан")
            self.assertEqual(len(db.scalars(select(PriceHistory)).all()), 2)
            notifications = db.scalars(select(Notification)).all()
            self.assertEqual(len(notifications), 1)
            self.assertTrue(notifications[0].event_key.startswith("price:"))
            card = build_card(listing, "Mock", notifications[0].event_key, 230000, 195000)
            self.assertIn("ЦЕНА СНИЖЕНА", card)
            self.assertIn("35 000 ₽", card)

    def test_telegram_id_supports_large_ids(self):
        self.assertIsInstance(User.__table__.c.telegram_id.type, BigInteger)

    def test_price_added_to_existing_listing_notifies_once(self):
        self.assertEqual(ingest(self.item(price=None)).notification_ids, [])
        self.assertEqual(len(ingest(self.item(price=195000)).notification_ids), 1)
        self.assertEqual(ingest(self.item(price=195000)).notification_ids, [])

    def test_concurrent_delivery_sends_once(self):
        notification_id = ingest(self.item()).notification_ids[0]
        messages = []

        class SlowBot:
            async def send_message(self, chat_id, text, **kwargs):
                messages.append(text)
                await asyncio.sleep(0.01)
                return SimpleNamespace(message_id=45)

        async def deliver():
            return await asyncio.gather(send_notification(SlowBot(), notification_id),
                                        send_notification(SlowBot(), notification_id))

        with patch("app.services.notifications.SessionLocal", self.sessions):
            results = asyncio.run(deliver())
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len(messages), 1)

    def test_new_match_only_once_and_city_filter(self):
        first = ingest(self.item())
        self.assertEqual(len(first.notification_ids), 1)
        self.assertEqual(ingest(self.item()).notification_ids, [])
        with self.sessions.begin() as db:
            filters = db.scalar(select(UserFilter))
            filters.cities = ["Чебоксары"]
        self.assertEqual(ingest(self.item(external_id="2")).notification_ids, [])

    def test_cross_source_duplicate_is_aliased(self):
        first = ingest(self.item())
        with self.sessions.begin() as db:
            other = Source(key="other", kind="mock", identifier="other", region="Татарстан", config={})
            db.add(other)
            db.flush()
            other_id = other.id
        second = self.item(external_id="different")
        second.source_id = other_id
        second.url = "https://other.example/car/different"
        result = ingest(second)
        self.assertTrue(result.duplicate)
        self.assertEqual(result.listing_id, first.listing_id)
        self.assertEqual(result.notification_ids, [])

    def test_rules_and_normalization(self):
        text = "Lada Granta 2013\nЦена: 195 000 руб. Пробег 170 тыс. км. Казань"
        item = RuleListingExtractor().extract(text, source_id=self.source_id, external_id="3",
                                              url="https://t.me/test/3", region="Татарстан")
        self.assertIsNotNone(item)
        item = normalize_listing(item)
        self.assertEqual(item.price, 195000)
        self.assertEqual(item.mileage, 170000)
        self.assertEqual(item.brand, "Lada")
        self.assertEqual(item.city, "Казань")
        self.assertIsNone(RuleListingExtractor().extract("Lada Granta 2013, пробег 170 тыс. км",
                         source_id=self.source_id, external_id="4", url="https://t.me/test/4", region="Татарстан"))
        self.assertIsNone(RuleListingExtractor().extract("Куплю Lada Granta до 195 000 руб.",
                         source_id=self.source_id, external_id="5", url="https://t.me/test/5", region="Татарстан"))

    def test_notification_delivery_records_sent_time(self):
        notification_id = ingest(self.item()).notification_ids[0]

        class FakeBot:
            def __init__(self):
                self.messages = []

            async def send_message(self, chat_id, text, **kwargs):
                self.messages.append((chat_id, text, kwargs))
                return SimpleNamespace(message_id=44)

        bot = FakeBot()
        with patch("app.services.notifications.SessionLocal", self.sessions):
            self.assertTrue(asyncio.run(send_notification(bot, notification_id)))
            self.assertFalse(asyncio.run(send_notification(bot, notification_id)))
        self.assertEqual(len(bot.messages), 1)
        self.assertEqual(bot.messages[0][0], 123)
        with self.sessions() as db:
            notification = db.get(Notification, notification_id)
            self.assertEqual(notification.status, "sent")
            self.assertIsNotNone(notification.notification_sent_at)

    def test_avito_actor_mapping(self):
        with self.sessions() as db:
            source = db.get(Source, self.source_id)
            row = {"itemId": "77", "url": "https://www.avito.ru/kazan/avtomobili/lada_granta_77",
                   "title": "Lada Granta 2013", "price": 195000, "mileageKm": 170000,
                   "locationAddress": "Казань, улица Ленина", "isShop": False, "photos": []}
            item = AvitoCollector(source).map_row(row)
        self.assertEqual(item.external_id, "77")
        self.assertEqual(item.brand, "Lada")
        self.assertEqual(item.model, "Granta")
        self.assertEqual(item.city, "Казань")
        self.assertEqual(item.seller_type, "private")

    def test_other_actor_mappings(self):
        with self.sessions() as db:
            source = db.get(Source, self.source_id)
            auto = AutoRuCollector(source).map_row({
                "listingId": "88", "url": "https://auto.ru/cars/used/sale/lada/granta/88/",
                "title": "Lada Granta 2013", "price": 195000, "mileageKm": 170000,
                "city": "Казань", "sellerType": "private"})
            drom = DromCollector(source).map_row({
                "bullId": 99, "url": "https://auto.drom.ru/kazan/lada/granta/99.html",
                "title": "Lada Granta, 2013", "price": 190000, "mileageKm": 175000,
                "location": "Казань", "photos": ["https://example.org/1.jpg"]})
        self.assertEqual(auto.external_id, "88")
        self.assertEqual(auto.city, "Казань")
        self.assertEqual(drom.external_id, "99")
        self.assertEqual(drom.photos, ["https://example.org/1.jpg"])

    def test_apify_adapter_runs_actor_and_reads_dataset(self):
        client = MagicMock()
        client.actor.return_value.call.return_value = SimpleNamespace(status="SUCCEEDED", default_dataset_id="dataset-1")
        client.dataset.return_value.iterate_items.return_value = iter([{"itemId": "77"}])
        with patch("app.collectors.apify.ApifyClient", return_value=client):
            rows = ApifyAdapter("token").run("owner/actor", {"citySlug": "kazan"})
        self.assertEqual(rows, [{"itemId": "77"}])
        self.assertEqual(client.actor.call_args.args, ("owner/actor",))
        self.assertEqual(client.dataset.call_args.args, ("dataset-1",))

    def test_telegram_collector_extracts_accessible_post(self):
        with self.sessions() as db:
            source = db.get(Source, self.source_id)
            source.identifier = "autorynokkzn"
            message = SimpleNamespace(id=51, raw_text="Lada Granta 2013\nЦена 195 000 руб. Казань",
                                      date=None, photo=None)
            item = asyncio.run(TelegramCollector(source, None).from_message(message))
        self.assertEqual(item.external_id, "51")
        self.assertEqual(item.url, "https://t.me/autorynokkzn/51")
        self.assertEqual(item.price, 195000)

    def test_admin_middleware_restricts_commands(self):
        calls = []

        async def handler(event, data):
            calls.append(event.from_user.id)
            return "ok"

        middleware = AdminMiddleware()
        with patch("app.bot.handlers.get_settings", return_value=SimpleNamespace(telegram_admin_id=123)):
            rejected = asyncio.run(middleware(handler, SimpleNamespace(from_user=SimpleNamespace(id=999)), {}))
            accepted = asyncio.run(middleware(handler, SimpleNamespace(from_user=SimpleNamespace(id=123)), {}))
        self.assertIsNone(rejected)
        self.assertEqual(accepted, "ok")
        self.assertEqual(calls, [123])


if __name__ == "__main__":
    unittest.main()

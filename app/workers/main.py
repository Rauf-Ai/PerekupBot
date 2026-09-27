import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
import redis.asyncio as redis
from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from telethon import TelegramClient, events
from app.bot.handlers import create_dispatcher
from app.collectors.avito import AvitoCollector
from app.collectors.autoru import AutoRuCollector
from app.collectors.drom import DromCollector
from app.collectors.mock import MockCollector
from app.collectors.telegram import TelegramCollector
from app.collectors.vk import VKCollector
from app.extractors.rules import RuleListingExtractor
from app.config.settings import get_settings
from app.db.models import CollectorRun, Source, User, utcnow
from app.db.seeds import seed_sources
from app.db.session import SessionLocal
from app.services.notifications import send_notification, send_pending
from app.services.pipeline import ingest

log = logging.getLogger(__name__)
COLLECTORS = {"avito": AvitoCollector, "autoru": AutoRuCollector, "drom": DromCollector,
              "vk": VKCollector, "mock": MockCollector}


def source_needed(kind: str) -> bool:
    """Avoid polling a billable/shared source when no active user selected it."""
    settings = get_settings()
    now = utcnow()
    admin_ids = {settings.telegram_admin_id} if settings.telegram_admin_id is not None else set()
    admin_ids.update(int(value.strip()) for value in settings.telegram_admin_ids.split(",")
                     if value.strip().isdigit())
    with SessionLocal() as db:
        users = db.scalars(select(User).options(selectinload(User.filters))).all()
    for user in users:
        filters = user.filters
        if not filters or not filters.search_enabled:
            continue
        has_access = user.telegram_id in admin_ids or (
            user.active and (user.subscription_expires_at is None or user.subscription_expires_at > now))
        if has_access and kind in (filters.selected_sources or ["telegram", "avito", "autoru", "drom"]):
            return True
    return False


async def process_items(source: Source, items, bot: Bot, run_id: int) -> None:
    new = duplicates = matched = sent = 0
    for item in items:
        try:
            result = ingest(item)
            new += int(result.new)
            duplicates += int(result.duplicate)
            matched += result.matched
            for notification_id in result.notification_ids:
                sent += int(await send_notification(bot, notification_id))
        except Exception:
            log.exception("listing_processing_failed source=%s external_id=%s", source.key, item.external_id)
    with SessionLocal.begin() as db:
        run = db.get(CollectorRun, run_id)
        run.listings_found = len(items)
        run.new_listings = new
        run.duplicates = duplicates
        run.matched = matched
        run.notifications_sent = sent
    log.info("collector_result source=%s found=%s new=%s duplicates=%s matched=%s sent=%s",
             source.key, len(items), new, duplicates, matched, sent)


async def run_source(source: Source, bot: Bot, client: TelegramClient | None = None) -> None:
    with SessionLocal.begin() as db:
        run = CollectorRun(source_id=source.id, started_at=utcnow(), status="running")
        db.add(run)
        db.flush()
        run_id = run.id
    log.info("collector_started source=%s kind=%s", source.key, source.kind)
    try:
        if source.kind == "telegram":
            if not client:
                raise RuntimeError("Telegram user session is unavailable")
            collector = TelegramCollector(source, client)
        else:
            collector_class = COLLECTORS[source.kind]
            collector = collector_class(source)
        items = await collector.collect()
        await process_items(source, items, bot, run_id)
        with SessionLocal.begin() as db:
            run = db.get(CollectorRun, run_id)
            run.status = "ok"
            run.finished_at = utcnow()
            stored = db.get(Source, source.id)
            stored.cursor = source.cursor
            stored.last_checked_at = run.finished_at
    except Exception as exc:
        log.exception("collector_failed source=%s", source.key)
        with SessionLocal.begin() as db:
            run = db.get(CollectorRun, run_id)
            run.status = "error"
            run.error = str(exc)[:2000]
            run.finished_at = utcnow()


async def source_loop(source: Source, bot: Bot, client: TelegramClient | None, redis_client):
    # Keep the configured polling cadence across worker restarts instead of
    # immediately launching another potentially billable Apify run.
    if source.last_checked_at:
        next_check = source.last_checked_at + timedelta(seconds=source.interval_seconds)
        startup_delay = (next_check - utcnow()).total_seconds()
        if startup_delay > 0:
            await asyncio.sleep(startup_delay)
    while True:
        try:
            if not source_needed(source.kind):
                await asyncio.sleep(source.interval_seconds)
                continue
            lock_key = f"collector-lock:{source.key}"
            token = uuid.uuid4().hex
            acquired = await redis_client.set(lock_key, token, nx=True, ex=max(300, source.interval_seconds * 2))
            if acquired:
                try:
                    await run_source(source, bot, client)
                finally:
                    await redis_client.eval(
                        "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end",
                        1, lock_key, token)
        except Exception:
            log.exception("collector_loop_failed source=%s", source.key)
        await asyncio.sleep(source.interval_seconds)


async def retry_loop(bot: Bot):
    while True:
        try:
            sent = await send_pending(bot)
            if sent:
                log.info("pending_notifications_sent count=%s", sent)
        except Exception:
            log.exception("notification_retry_failed")
        await asyncio.sleep(60)


async def main():
    settings = get_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    seed_sources()
    with SessionLocal() as db:
        sources = db.scalars(select(Source).where(Source.enabled.is_(True))).all()
    bot = Bot(settings.telegram_bot_token)
    redis_client = redis.from_url(settings.redis_url, decode_responses=True)
    client = None
    if settings.telegram_api_id and settings.telegram_api_hash:
        client = TelegramClient(settings.telegram_session_path, settings.telegram_api_id, settings.telegram_api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            log.error("Telegram user session is not authorized; run python -m app.tools.telegram_login")
            await client.disconnect()
            client = None
    source_by_username = {source.identifier.casefold().lstrip("@"): source for source in sources if source.kind == "telegram"}
    if source_by_username and client is None:
        log.warning("telegram_user_session_unavailable sources=%s; only Bot API channel posts can be received",
                    len(source_by_username))
    if client:
        @client.on(events.NewMessage)
        @client.on(events.MessageEdited)
        async def on_telegram_message(event):
            chat = await event.get_chat()
            source = source_by_username.get((getattr(chat, "username", None) or "").casefold())
            if not source:
                return
            try:
                item = await TelegramCollector(source, client).from_message(event.message)
                if item:
                    await run_event(source, item, bot)
            except Exception:
                log.exception("telegram_event_failed source=%s", source.key)

    tasks = [asyncio.create_task(retry_loop(bot))]
    for source in sources:
        if source.kind == "telegram" and client is None:
            continue
        tasks.append(asyncio.create_task(source_loop(source, bot, client, redis_client)))
    if client:
        tasks.append(asyncio.create_task(client.run_until_disconnected()))
    dispatcher = create_dispatcher()

    async def on_bot_channel_post(message):
        username = (getattr(message.chat, "username", None) or "").casefold()
        source = source_by_username.get(username)
        if not source:
            return
        item = RuleListingExtractor().extract(message.text or message.caption or "", source_id=source.id,
                                              external_id=str(message.message_id),
                                              url=f"https://t.me/{source.identifier}/{message.message_id}",
                                              region=source.region)
        if item:
            item.published_at = message.date
            if message.photo:
                item.photos = [message.photo[-1].file_id]
            await run_event(source, item, bot)

    dispatcher.channel_post.register(on_bot_channel_post)
    dispatcher.edited_channel_post.register(on_bot_channel_post)
    try:
        await dispatcher.start_polling(bot, allowed_updates=["message", "callback_query", "channel_post", "edited_channel_post"])
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if client:
            await client.disconnect()
        await redis_client.aclose()
        await bot.session.close()


async def run_event(source: Source, item, bot: Bot):
    with SessionLocal.begin() as db:
        run = CollectorRun(source_id=source.id, started_at=utcnow(), status="running")
        db.add(run)
        db.flush()
        run_id = run.id
    await process_items(source, [item], bot, run_id)
    with SessionLocal.begin() as db:
        run = db.get(CollectorRun, run_id)
        run.status = "ok"
        run.finished_at = utcnow()


if __name__ == "__main__":
    asyncio.run(main())

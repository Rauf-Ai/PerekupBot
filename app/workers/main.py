import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
import redis.asyncio as redis
from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from telethon import TelegramClient, events
from telethon import utils as telegram_utils
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from app.bot.handlers import create_dispatcher
from app.collectors.avito import AvitoCollector
from app.collectors.autoru import AutoRuCollector
from app.collectors.drom import DromCollector
from app.collectors.mock import MockCollector
from app.collectors.telegram import TelegramCollector
from app.collectors.vk import VKCollector
from app.extractors.rules import RuleListingExtractor
from app.config.settings import get_settings
from app.db.models import CollectorRun, Source, User, UserTelegramSource, utcnow
from app.db.seeds import seed_sources
from app.db.session import SessionLocal
from app.services.notifications import send_notification, send_pending
from app.services.custom_sources import custom_source_limit
from app.services.pipeline import ingest

log = logging.getLogger(__name__)
COLLECTORS = {"avito": AvitoCollector, "autoru": AutoRuCollector, "drom": DromCollector,
              "vk": VKCollector, "mock": MockCollector}


def source_needed(source: Source) -> bool:
    """Poll only sources selected by an active user in a covered region."""
    settings = get_settings()
    now = utcnow()
    admin_ids = {settings.telegram_admin_id} if settings.telegram_admin_id is not None else set()
    admin_ids.update(int(value.strip()) for value in settings.telegram_admin_ids.split(",")
                     if value.strip().isdigit())
    with SessionLocal() as db:
        users = db.scalars(select(User).options(selectinload(User.filters))).all()
        subscribers = (set(db.scalars(select(UserTelegramSource.user_id).where(
            UserTelegramSource.source_id == source.id)).all())
            if (source.config or {}).get("custom") else set())
    regions = set((source.config or {}).get("regions") or ([source.region] if source.region else []))
    for user in users:
        filters = user.filters
        if not filters or not filters.search_enabled:
            continue
        has_access = user.telegram_id in admin_ids or (
            user.active and (user.subscription_expires_at is None or user.subscription_expires_at > now))
        if (has_access and source.kind in (filters.selected_sources or ["telegram", "avito", "autoru", "drom"])
                and (not regions or regions.intersection(filters.regions or []))
                and (not (source.config or {}).get("custom") or
                     (user.id in subscribers and custom_source_limit(
                         user.subscription_plan, is_admin=user.telegram_id in admin_ids)))):
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
            if source.kind == "telegram" and (client is None or not client.is_connected()):
                await asyncio.sleep(source.interval_seconds)
                continue
            if not source_needed(source):
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


async def maintain_telegram_connection(client: TelegramClient):
    """Resume Telethon after a permanent network disconnect without stopping Bot API polling."""
    while True:
        try:
            await client.run_until_disconnected()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("telegram_connection_lost")
        log.warning("telegram_disconnected_reconnecting")
        delay = 5
        while not client.is_connected():
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    raise RuntimeError("Telegram user session is no longer authorized")
                log.info("telegram_reconnected")
                break
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("telegram_reconnect_failed")
                if client.is_connected():
                    await client.disconnect()
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)


def _rebuild_telegram_maps(sources: list[Source], by_username: dict, by_chat_id: dict) -> None:
    by_username.clear()
    by_chat_id.clear()
    for source in sources:
        if source.kind != "telegram":
            continue
        identifier = source.identifier
        if identifier.startswith("-100") and identifier[1:].isdigit():
            by_chat_id[int(identifier)] = source
        elif not identifier.startswith("invite:"):
            by_username[identifier.casefold().lstrip("@")] = source


async def _resolve_private_source(source: Source, client: TelegramClient) -> bool:
    if not source.identifier.startswith("invite:"):
        return True
    if not client.is_connected():
        return False
    invite_hash = source.identifier.split(":", 1)[1]
    try:
        invite = await client(CheckChatInviteRequest(invite_hash))
        if not getattr(invite, "chat", None):
            await client(ImportChatInviteRequest(invite_hash))
            invite = await client(CheckChatInviteRequest(invite_hash))
        chat = invite.chat
        source.identifier = str(telegram_utils.get_peer_id(chat))
        with SessionLocal.begin() as db:
            stored = db.get(Source, source.id)
            stored.identifier = source.identifier
            stored.config = {**(stored.config or {}), "title": getattr(chat, "title", "Закрытый чат"),
                             "resolution_error": None}
        log.info("telegram_private_source_joined source_id=%s", source.id)
        return True
    except Exception as error:
        with SessionLocal.begin() as db:
            stored = db.get(Source, source.id)
            stored.config = {**(stored.config or {}), "resolution_error": type(error).__name__}
        log.warning("telegram_private_source_unavailable source_id=%s error=%s", source.id, type(error).__name__)
        return False


async def source_registry_loop(bot: Bot, client: TelegramClient | None, redis_client,
                               source_tasks: dict[int, asyncio.Task], by_username: dict, by_chat_id: dict):
    """Pick up user-added Telegram channels without restarting the bot."""
    while True:
        try:
            with SessionLocal() as db:
                sources = db.scalars(select(Source).where(Source.enabled.is_(True))).all()
            ready = []
            for source in sources:
                if source.kind == "telegram" and client is None:
                    continue
                if source.kind == "telegram" and not await _resolve_private_source(source, client):
                    continue
                ready.append(source)
                if source.id not in source_tasks or source_tasks[source.id].done():
                    source_tasks[source.id] = asyncio.create_task(source_loop(source, bot, client, redis_client))
            active_ids = {source.id for source in ready}
            for source_id in list(source_tasks):
                if source_id not in active_ids:
                    source_tasks.pop(source_id).cancel()
            _rebuild_telegram_maps(ready, by_username, by_chat_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("source_registry_failed")
        await asyncio.sleep(30)


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
    source_by_username: dict[str, Source] = {}
    source_by_chat_id: dict[int, Source] = {}
    _rebuild_telegram_maps(sources, source_by_username, source_by_chat_id)
    if source_by_username and client is None:
        log.warning("telegram_user_session_unavailable sources=%s; only Bot API channel posts can be received",
                    len(source_by_username))
    if client:
        @client.on(events.NewMessage)
        @client.on(events.MessageEdited)
        async def on_telegram_message(event):
            chat = await event.get_chat()
            source = (source_by_username.get((getattr(chat, "username", None) or "").casefold())
                      or source_by_chat_id.get(event.chat_id))
            if not source:
                return
            if not source_needed(source):
                return
            try:
                item = await TelegramCollector(source, client).from_message(event.message)
                if item:
                    await run_event(source, item, bot)
            except Exception:
                log.exception("telegram_event_failed source=%s", source.key)

    source_tasks: dict[int, asyncio.Task] = {}
    tasks = [asyncio.create_task(retry_loop(bot)),
             asyncio.create_task(source_registry_loop(bot, client, redis_client, source_tasks,
                                                      source_by_username, source_by_chat_id))]
    if client:
        tasks.append(asyncio.create_task(maintain_telegram_connection(client)))
    dispatcher = create_dispatcher()

    async def on_bot_channel_post(message):
        username = (getattr(message.chat, "username", None) or "").casefold()
        source = source_by_username.get(username) or source_by_chat_id.get(message.chat.id)
        if not source:
            return
        if not source_needed(source):
            return
        item = RuleListingExtractor().extract(message.text or message.caption or "", source_id=source.id,
                                              external_id=str(message.message_id),
                                              url=(f"https://t.me/c/{source.identifier[4:]}/{message.message_id}"
                                                   if source.identifier.startswith("-100") else
                                                   f"https://t.me/{source.identifier}/{message.message_id}"),
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
        for task in [*tasks, *source_tasks.values()]:
            task.cancel()
        await asyncio.gather(*tasks, *source_tasks.values(), return_exceptions=True)
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

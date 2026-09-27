import logging
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload
from app.db.models import Listing, Notification, PriceHistory, User
from app.db.session import SessionLocal
from app.services.scoring import score_listing

log = logging.getLogger(__name__)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _money(value: int | None) -> str:
    return f"{value:,}".replace(",", " ") if value is not None else "не указана"


def _minutes_ago(value: datetime | None) -> str:
    if not value:
        return "неизвестно"
    minutes = max(0, int((datetime.now(timezone.utc) - _aware(value)).total_seconds() / 60))
    return f"{minutes} мин. назад"


def build_card(listing: Listing, source_name: str, event_key: str, old_price: int | None = None,
               new_price: int | None = None) -> str:
    display_price = new_price if new_price is not None else listing.price
    drop = max(0, (old_price or 0) - (display_price or 0)) if event_key.startswith("price:") else 0
    score = score_listing(listing, drop)
    lines = ["<b>ЦЕНА СНИЖЕНА</b>" if drop else "<b>НОВЫЙ ВАРИАНТ</b>", "",
             f"<b>{escape(listing.title)}</b>",
             f"{listing.year} год" if listing.year else "Год не указан", ""]
    if drop:
        lines += [f"Было: {_money(old_price)} ₽", f"Стало: {_money(display_price)} ₽", f"Снижение: {_money(drop)} ₽"]
    else:
        lines.append(f"Цена: {_money(display_price)} ₽")
    lines += [f"Пробег: {_money(listing.mileage)} км" if listing.mileage is not None else "Пробег не указан",
              escape(listing.city or listing.region or "Место не указано"), "",
              f"Оценка: {score.score}/100", f"Источник: {escape(source_name)}",
              f"Опубликовано: {_minutes_ago(listing.published_at)}"]
    if score.reasons:
        lines += ["", "Причины:"] + [f"• {escape(reason)}" for reason in score.reasons]
    if score.warnings:
        lines += ["", "Уточните данные:"] + [f"• {escape(warning)}" for warning in score.warnings]
    return "\n".join(lines)


def card_keyboard(listing: Listing) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Открыть объявление", url=listing.url)],
        [InlineKeyboardButton(text="Не интересно", callback_data=f"dismiss:{listing.id}"),
         InlineKeyboardButton(text="Скрыть модель", callback_data=f"hide_model:{listing.id}")],
    ])


async def send_notification(bot: Bot, notification_id: int) -> bool:
    now = datetime.now(timezone.utc)
    # Atomically reserve the send; expired reservations recover after a worker crash.
    with SessionLocal.begin() as db:
        claim = db.execute(update(Notification).where(
            Notification.id == notification_id,
            Notification.status.in_(["pending", "failed", "sending"]),
            Notification.next_attempt_at.is_(None) | (Notification.next_attempt_at <= now),
        ).values(status="sending", next_attempt_at=now + timedelta(minutes=10)))
        if claim.rowcount != 1:
            return False
    with SessionLocal() as db:
        notification = db.get(Notification, notification_id)
        if not notification or notification.status == "sent":
            return False
        listing = db.scalar(select(Listing).options(selectinload(Listing.photos), selectinload(Listing.source)).where(Listing.id == notification.listing_id))
        user = db.get(User, notification.user_id)
        if not listing or not user:
            return False
        old_price = None
        new_price = None
        if notification.event_key.startswith("price:"):
            history = db.get(PriceHistory, int(notification.event_key.split(":", 1)[1]))
            old_price = history.old_price if history else None
            new_price = history.new_price if history else None
        card = build_card(listing, listing.source.kind.capitalize(), notification.event_key, old_price, new_price)
        keyboard = card_keyboard(listing)
        photo = listing.photos[0].url if listing.photos else None
        telegram_id = user.telegram_id
        created_at = notification.created_at
        published_at = listing.published_at
    try:
        if photo:
            media = FSInputFile(photo) if Path(photo).is_file() else photo
            try:
                message = await bot.send_photo(telegram_id, media, caption=card, parse_mode="HTML", reply_markup=keyboard)
            except TelegramAPIError:
                log.warning("photo_send_failed notification_id=%s; sending text", notification_id)
                message = await bot.send_message(telegram_id, card, parse_mode="HTML", reply_markup=keyboard)
        else:
            message = await bot.send_message(telegram_id, card, parse_mode="HTML", reply_markup=keyboard)
    except Exception as exc:
        log.exception("notification_failed notification_id=%s", notification_id)
        with SessionLocal.begin() as db:
            notification = db.get(Notification, notification_id)
            notification.status = "failed"
            notification.attempts += 1
            notification.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=min(3600, 60 * 2 ** min(notification.attempts, 6)))
            notification.error = str(exc)[:2000]
        return False
    sent_at = datetime.now(timezone.utc)
    with SessionLocal.begin() as db:
        notification = db.get(Notification, notification_id)
        notification.status = "sent"
        notification.error = None
        notification.next_attempt_at = None
        notification.notification_sent_at = sent_at
        notification.telegram_message_id = message.message_id
    notification_delay = (sent_at - _aware(created_at)).total_seconds()
    detection_delay = (_aware(created_at) - _aware(published_at)).total_seconds() if published_at else None
    log.info("notification_sent id=%s notification_delay_seconds=%.1f detection_delay_seconds=%s",
             notification_id, notification_delay, f"{detection_delay:.1f}" if detection_delay is not None else "unknown")
    return True


async def send_pending(bot: Bot, limit: int = 50) -> int:
    with SessionLocal() as db:
        ids = db.scalars(select(Notification.id).where(Notification.status.in_(["pending", "failed", "sending"]),
                                                   (Notification.next_attempt_at.is_(None) | (Notification.next_attempt_at <= datetime.now(timezone.utc))))
                         .order_by(Notification.created_at).limit(limit)).all()
    sent = 0
    for notification_id in ids:
        sent += int(await send_notification(bot, notification_id))
    return sent

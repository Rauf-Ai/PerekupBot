from html import escape
from datetime import datetime, timedelta, timezone
import re
from aiogram import Dispatcher, F, Router
from aiogram import BaseMiddleware
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload
from app.db.models import Listing, PendingAccessGrant, Source, User, UserFilter
from app.db.session import SessionLocal
from app.services.filtering import matches_filter
from app.services.notifications import build_card, card_keyboard
from app.config.settings import get_settings

router = Router()


def _admin_ids() -> set[int]:
    settings = get_settings()
    ids = {settings.telegram_admin_id} if settings.telegram_admin_id is not None else set()
    ids.update(int(value.strip()) for value in settings.telegram_admin_ids.split(",")
               if value.strip().isdigit())
    return ids


def _is_owner(telegram_id: int) -> bool:
    return get_settings().telegram_admin_id == telegram_id


# Commercial package proposal. Only the 30-day access grant is enforced by the MVP;
# report/search quotas and faster polling remain product targets, not active limits.
PLANS = {
    "solo": {"title": "Старт", "price": 990, "reports": 1, "alerts": 100, "speed": "до 60 мин", "seats": 1},
    "plus": {"title": "Плюс", "price": 2490, "reports": 3, "alerts": 500, "speed": "до 30 мин", "seats": 1},
    "pro": {"title": "Профи", "price": 4990, "reports": 8, "alerts": 1500, "speed": "до 15 мин", "seats": 1},
    "business": {"title": "Бизнес", "price": 9990, "reports": 20, "alerts": 4000, "speed": "до 5 мин", "seats": 1},
    "team": {"title": "Команда", "price": 14990, "reports": 40, "alerts": 8000, "speed": "до 2 мин", "seats": 5},
}


class UserAccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        actor = getattr(event, "from_user", None)
        if actor is None or actor.id not in _admin_ids():
            # Let /start register the username, but never activate a new account by itself.
            if isinstance(event, Message) and event.text and event.text.split(maxsplit=1)[0].split("@", 1)[0] == "/start":
                return await handler(event, data)
            with SessionLocal() as db:
                user = db.scalar(select(User).where(User.telegram_id == actor.id)) if actor else None
                now = datetime.now(timezone.utc)
                has_access = bool(user and user.active and
                                  (user.subscription_expires_at is None or
                                   user.subscription_expires_at > now))
            if not has_access:
                message = "🔒 Доступ пока не открыт или тариф закончился. Обратитесь к администратору бота."
                if isinstance(event, CallbackQuery):
                    await event.answer(message, show_alert=True)
                elif isinstance(event, Message):
                    await event.answer(message)
                return None
        return await handler(event, data)


router.message.outer_middleware(UserAccessMiddleware())
router.callback_query.outer_middleware(UserAccessMiddleware())
REGIONS = ["Татарстан", "Чувашия", "Марий Эл"]
FIELDS = {"cities": "города", "brands": "марки", "models": "модели", "min_year": "минимальный год",
          "max_mileage": "максимальный пробег"}


class EditFilter(StatesGroup):
    value = State()


class AdminFlow(StatesGroup):
    lookup_target = State()


def _get_user(telegram_id: int, username: str | None = None) -> User:
    with SessionLocal.begin() as db:
        user = db.scalar(select(User).options(selectinload(User.filters)).where(User.telegram_id == telegram_id))
        normalized_username = username.casefold() if username else None
        pending = None
        if normalized_username:
            pending = db.scalar(select(PendingAccessGrant).where(
                or_(PendingAccessGrant.username == normalized_username,
                    PendingAccessGrant.telegram_id == telegram_id),
                PendingAccessGrant.subscription_plan.is_not(None),
            ))
        else:
            pending = db.scalar(select(PendingAccessGrant).where(
                PendingAccessGrant.telegram_id == telegram_id,
                PendingAccessGrant.subscription_plan.is_not(None),
            ))
        if pending and pending.claim_expires_at <= datetime.now(timezone.utc):
            db.delete(pending)
            pending = None
        now = datetime.now(timezone.utc)
        if user is None:
            user = User(telegram_id=telegram_id, username=username,
                        active=telegram_id in _admin_ids() or pending is not None)
            user.filters = UserFilter()
            db.add(user)
            db.flush()
        elif username:
            user.username = username
        if pending:
            user.active = True
            user.subscription_plan = pending.subscription_plan
            user.subscription_expires_at = now + timedelta(days=30)
            db.delete(pending)
        return user


def _admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔓 Найти и выдать доступ", callback_data="admin:find")],
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:list")],
        [InlineKeyboardButton(text="📊 Тарифы", callback_data="admin:tariffs")],
    ])


def _user_admin_keyboard(user: User) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{plan['title']} — {plan['price']:,} ₽ / 30 дней".replace(",", " "),
                                  callback_data=f"admin:plan:{user.id}:{code}")]
            for code, plan in PLANS.items()]
    if user.active:
        rows.append([InlineKeyboardButton(text="⛔ Отозвать доступ", callback_data=f"admin:revoke:{user.id}")])
    rows.append([InlineKeyboardButton(text="↩️ В админ-панель", callback_data="admin:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _pending_admin_keyboard(grant: PendingAccessGrant) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text=f"{plan['title']} — {plan['price']:,} ₽ / 30 дней".replace(",", " "),
        callback_data=f"admin:pending_plan:{grant.id}:{code}",
    )] for code, plan in PLANS.items()]
    rows.append([InlineKeyboardButton(text="Отменить", callback_data=f"admin:pending_cancel:{grant.id}")])
    rows.append([InlineKeyboardButton(text="↩️ В админ-панель", callback_data="admin:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _admin_user_text(user: User) -> str:
    plan = PLANS.get(user.subscription_plan or "", {"title": "Без тарифа", "price": 0})
    expiry = user.subscription_expires_at
    if expiry and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    state = "✅ доступ открыт" if user.active and (expiry is None or expiry > datetime.now(timezone.utc)) else "🔒 доступ закрыт"
    until = expiry.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC") if expiry else "—"
    username = f"@{escape(user.username)}" if user.username else "username не указан"
    return (f"<b>Пользователь</b> {username}\n"
            f"ID: <code>{user.telegram_id}</code>\n"
            f"Статус: {state}\n"
            f"Тариф: {escape(plan['title'])} ({plan['price']:,} ₽)\n".replace(",", " ")
            + f"Доступ до: {until}")


def _tariff_proposal_text() -> str:
    lines = ["<b>Тарифы на 30 дней — предложение</b>"]
    for plan in PLANS.values():
        cost = plan["reports"] * 90
        lines.append(
            f"\n<b>{plan['title']} — {plan['price']:,} ₽</b>".replace(",", " ")
            + f"\n• до {plan['alerts']:,} объявлений/мес.; цель по задержке: {plan['speed']}"
            .replace(",", " ")
            + f"\n• {plan['reports']} отч. Автотеки (себестоимость около {cost:,} ₽)".replace(",", " ")
            + f"\n• пользователей: до {plan['seats']}"
        )
    lines.append(
        "\n<i>В текущем MVP назначение тарифа открывает доступ на 30 дней. "
        "Квоты объявлений и отчётов, а также ускоренная проверка пока не включены. "
        "Сейчас Telegram-каналы дают быстрые события, а Apify-источники проверяются примерно раз в час.</i>"
    )
    return "".join(lines)


def _settings_text(filters: UserFilter) -> str:
    def show(items):
        return escape(", ".join(items)) if items else "любые"
    return ("<b>Настройки поиска</b>\n"
            f"Бюджет: {filters.max_price:,} ₽\n".replace(",", " ")
            + f"Регионы: {show(filters.regions)}\nГорода: {show(filters.cities)}\n"
            + f"Марки: {show(filters.brands)}\nМодели: {show(filters.models)}\n"
            + f"Год от: {filters.min_year or 'любой'}\nПробег до: {filters.max_mileage or 'любой'}\n"
            + f"Продавец: {filters.seller_type or 'любой'}")


def _settings_keyboard(filters: UserFilter) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="100 тыс.", callback_data="budget:100000"),
             InlineKeyboardButton(text="200 тыс.", callback_data="budget:200000"),
             InlineKeyboardButton(text="300 тыс.", callback_data="budget:300000"),
             InlineKeyboardButton(text="Своя сумма", callback_data="edit:max_price")]]
    for i, region in enumerate(REGIONS):
        rows.append([InlineKeyboardButton(text=("✅ " if region in filters.regions else "▫️ ") + region,
                                          callback_data=f"region:{i}")])
    rows += [[InlineKeyboardButton(text="Города", callback_data="edit:cities"),
              InlineKeyboardButton(text="Марки", callback_data="edit:brands"),
              InlineKeyboardButton(text="Модели", callback_data="edit:models")],
             [InlineKeyboardButton(text="Год от", callback_data="edit:min_year"),
              InlineKeyboardButton(text="Пробег до", callback_data="edit:max_mileage")],
             [InlineKeyboardButton(text="Любой продавец", callback_data="seller:any"),
              InlineKeyboardButton(text="Частник", callback_data="seller:private"),
              InlineKeyboardButton(text="Дилер", callback_data="seller:dealer")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("start"))
async def start(message: Message):
    if not message.from_user:
        return
    user = _get_user(message.from_user.id, message.from_user.username)
    if message.from_user.id not in _admin_ids() and not user.active:
        await message.answer("🔒 Доступ к поиску пока закрыт. Передайте администратору ваш username или Telegram ID для назначения тарифа.")
        return
    if message.from_user.id not in _admin_ids() and user.subscription_expires_at:
        expiry = user.subscription_expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry <= datetime.now(timezone.utc):
            await message.answer("⏳ Срок тарифа закончился. Напишите администратору бота для продления доступа.")
            return
    await message.answer("Поиск автомобилей включен. Новые объявления по вашим фильтрам будут приходить сюда.\n"
                         "Команды: /settings, /search, /latest, /sources")
    await message.answer(_settings_text(user.filters), parse_mode="HTML", reply_markup=_settings_keyboard(user.filters))


@router.message(Command("admin"))
async def admin_panel(message: Message, state: FSMContext):
    if not message.from_user or not _is_owner(message.from_user.id):
        await message.answer("⛔ Админ-панель доступна только владельцу бота.")
        return
    await state.clear()
    with SessionLocal() as db:
        total = db.scalar(select(func.count(User.id))) or 0
        pending = db.scalar(select(func.count(PendingAccessGrant.id)).where(
            PendingAccessGrant.subscription_plan.is_not(None),
            PendingAccessGrant.claim_expires_at > datetime.now(timezone.utc),
        )) or 0
        active = db.scalar(select(func.count(User.id)).where(
            User.active.is_(True),
            or_(User.subscription_expires_at.is_(None),
                User.subscription_expires_at > datetime.now(timezone.utc)))) or 0
    await message.answer(f"<b>Панель владельца</b>\nПользователей: {total}\nС активным доступом: {active}\nОжидают первого /start: {pending}",
                         parse_mode="HTML", reply_markup=_admin_keyboard())
    await message.answer(_tariff_proposal_text(), parse_mode="HTML")


@router.callback_query(F.data == "admin:home")
async def admin_home(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text("<b>Панель владельца</b>", parse_mode="HTML", reply_markup=_admin_keyboard())
    await callback.answer()


@router.callback_query(F.data == "admin:tariffs")
async def admin_tariffs(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.message.answer(_tariff_proposal_text(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "admin:find")
async def admin_find(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminFlow.lookup_target)
    await callback.message.answer("Отправьте <b>@username</b> или <b>числовой Telegram ID</b>.\n"
                                  "По username можно заранее назначить тариф: доступ включится, когда пользователь впервые нажмёт /start.\n"
                                  "Если username изменится, ожидающий доступ получит аккаунт с этим username; для точной идентификации используйте ID.\n"
                                  "По ID тоже можно заранее подготовить доступ; тариф начнёт действовать после /start.",
                                  parse_mode="HTML")
    await callback.answer()


@router.message(AdminFlow.lookup_target)
async def admin_lookup_user(message: Message, state: FSMContext):
    if not message.from_user or not _is_owner(message.from_user.id):
        await state.clear()
        return
    raw = (message.text or "").strip().split()[0] if (message.text or "").strip() else ""
    if raw.isdigit():
        telegram_id = int(raw)
        if telegram_id <= 0 or telegram_id > 9_223_372_036_854_775_807:
            await message.answer("Telegram ID должен быть положительным числом.")
            return
        if telegram_id in _admin_ids():
            await message.answer("Администратору нельзя назначить пользовательский тариф.", reply_markup=_admin_keyboard())
            await state.clear()
            return
        with SessionLocal.begin() as db:
            user = db.scalar(select(User).where(User.telegram_id == telegram_id))
            grant = None
            if user:
                db.expunge(user)
            else:
                grant = db.scalar(select(PendingAccessGrant).where(PendingAccessGrant.telegram_id == telegram_id))
                now = datetime.now(timezone.utc)
                if grant and grant.claim_expires_at <= now:
                    db.delete(grant)
                    grant = None
                if grant is None:
                    grant = PendingAccessGrant(telegram_id=telegram_id, claim_expires_at=now + timedelta(days=30))
                    db.add(grant)
                    db.flush()
                db.expunge(grant)
        await state.clear()
        if user:
            await message.answer(_admin_user_text(user), parse_mode="HTML", reply_markup=_user_admin_keyboard(user))
        else:
            deadline = grant.claim_expires_at.astimezone(timezone.utc).strftime("%d.%m.%Y")
            await message.answer(
                f"Пользователь с ID <code>{telegram_id}</code> ещё не запускал бота. Выберите тариф: он начнёт действовать на 30 дней после первого /start.\n"
                f"Ожидающий доступ можно активировать до {deadline}.",
                parse_mode="HTML", reply_markup=_pending_admin_keyboard(grant))
        return

    username = raw[1:] if raw.startswith("@") else raw
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        await message.answer("Введите <code>@username</code> или положительный числовой Telegram ID.", parse_mode="HTML")
        return
    username = username.casefold()
    with SessionLocal.begin() as db:
        user = db.scalar(select(User).where(func.lower(User.username) == username))
        if user:
            db.expunge(user)
        else:
            grant = db.scalar(select(PendingAccessGrant).where(PendingAccessGrant.username == username))
            now = datetime.now(timezone.utc)
            if grant and grant.claim_expires_at <= now:
                db.delete(grant)
                grant = None
            if grant is None:
                grant = PendingAccessGrant(username=username, claim_expires_at=now + timedelta(days=30))
                db.add(grant)
                db.flush()
            db.expunge(grant)
    if user is None:
        await state.clear()
        deadline = grant.claim_expires_at.astimezone(timezone.utc).strftime("%d.%m.%Y")
        await message.answer(
            f"Пользователь @{escape(username)} ещё не запускал бота или не найден. Выберите тариф: он начнёт действовать на 30 дней после первого /start.\n"
            f"Ожидающий доступ можно активировать до {deadline}.",
            parse_mode="HTML", reply_markup=_pending_admin_keyboard(grant))
        return
    if user.telegram_id in _admin_ids():
        await message.answer("Администратору нельзя назначить пользовательский тариф.", reply_markup=_admin_keyboard())
        await state.clear()
        return
    await state.clear()
    await message.answer(_admin_user_text(user), parse_mode="HTML", reply_markup=_user_admin_keyboard(user))


@router.callback_query(F.data.startswith("admin:pending_plan:"))
async def admin_assign_pending_plan(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    _, _, grant_id_text, plan_code = callback.data.split(":", 3)
    plan = PLANS.get(plan_code)
    if not plan or not grant_id_text.isdigit():
        await callback.answer("Некорректный тариф", show_alert=True)
        return
    with SessionLocal.begin() as db:
        grant = db.get(PendingAccessGrant, int(grant_id_text))
        if not grant or grant.claim_expires_at <= datetime.now(timezone.utc):
            if grant:
                db.delete(grant)
            expired = True
        else:
            grant.subscription_plan = plan_code
            recipient = f"@{grant.username}" if grant.username else f"ID {grant.telegram_id}"
            deadline = grant.claim_expires_at.astimezone(timezone.utc).strftime("%d.%m.%Y")
            expired = False
    if expired:
        await callback.message.edit_text("Ожидающий доступ истёк. Начните выдачу заново.", reply_markup=_admin_keyboard())
        await callback.answer("Срок истёк", show_alert=True)
        return
    await callback.message.edit_text(
        f"✅ Тариф {plan['title']} подготовлен для {escape(recipient)}. Он начнёт действовать на 30 дней после первого /start.\n"
        f"Ожидающий доступ действителен до {deadline}; попросите пользователя открыть бота и нажать /start.",
        parse_mode="HTML", reply_markup=_admin_keyboard())
    await callback.answer("Доступ будет включён после /start")


@router.callback_query(F.data.startswith("admin:pending_cancel:"))
async def admin_cancel_pending_grant(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    grant_id_text = callback.data.rsplit(":", 1)[-1]
    if grant_id_text.isdigit():
        with SessionLocal.begin() as db:
            grant = db.get(PendingAccessGrant, int(grant_id_text))
            if grant:
                db.delete(grant)
    await callback.message.edit_text("Ожидающий доступ отменён.", reply_markup=_admin_keyboard())
    await callback.answer("Отменено")


@router.callback_query(F.data == "admin:list")
async def admin_list_users(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    with SessionLocal() as db:
        users = db.scalars(select(User).order_by(User.created_at.desc()).limit(15)).all()
        lines = ["<b>Последние пользователи</b>"]
        now = datetime.now(timezone.utc)
        for user in users:
            expiry = user.subscription_expires_at
            if expiry and expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            active = user.active and (expiry is None or expiry > now)
            handle = f"@{user.username}" if user.username else "без username"
            lines.append(f"{'✅' if active else '🔒'} {escape(handle)} — {user.telegram_id}")
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔓 Найти пользователя", callback_data="admin:find")],
                         [InlineKeyboardButton(text="↩️ В панель", callback_data="admin:home")]]))
    await callback.answer()


@router.callback_query(F.data.startswith("admin:plan:"))
async def admin_assign_plan(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    _, _, user_id_text, plan_code = callback.data.split(":", 3)
    plan = PLANS.get(plan_code)
    if not plan or not user_id_text.isdigit():
        await callback.answer("Некорректный тариф", show_alert=True)
        return
    now = datetime.now(timezone.utc)
    with SessionLocal.begin() as db:
        user = db.get(User, int(user_id_text))
        if not user or user.telegram_id in _admin_ids():
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        expiry = user.subscription_expires_at
        if expiry is None or (expiry.replace(tzinfo=timezone.utc) if expiry.tzinfo is None else expiry) < now:
            expiry = now
        elif expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        user.active = True
        user.subscription_plan = plan_code
        user.subscription_expires_at = expiry + timedelta(days=30)
        telegram_id = user.telegram_id
        username = user.username
        expires_text = user.subscription_expires_at.strftime("%d.%m.%Y")
    delivery_error = False
    try:
        await callback.bot.send_message(telegram_id, f"✅ Вам открыт доступ по тарифу {plan['title']} на 30 дней. Доступ активен до {expires_text}.")
    except Exception:
        delivery_error = True
    recipient = f"@{escape(username)}" if username else f"ID <code>{telegram_id}</code>"
    status_text = f"✅ Тариф {plan['title']} назначен пользователю {recipient} до {expires_text}."
    if delivery_error:
        status_text += "\n\nℹ️ Доступ записан, но Telegram не принял уведомление. Попросите пользователя открыть бота и нажать /start."
    await callback.message.edit_text(status_text, parse_mode="HTML", reply_markup=_admin_keyboard())
    await callback.answer("Тариф назначен" if not delivery_error else "Тариф сохранён; уведомление не доставлено")


@router.callback_query(F.data.startswith("admin:revoke:"))
async def admin_revoke_access(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    user_id_text = callback.data.rsplit(":", 1)[-1]
    if not user_id_text.isdigit():
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    with SessionLocal.begin() as db:
        user = db.get(User, int(user_id_text))
        if not user or user.telegram_id in _admin_ids():
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        user.active = False
        user.subscription_plan = None
        user.subscription_expires_at = None
        username = user.username
        telegram_id = user.telegram_id
    await callback.message.edit_text(f"⛔ Доступ отозван у @{escape(username or 'unknown')}.",
                                     parse_mode="HTML", reply_markup=_admin_keyboard())
    try:
        await callback.bot.send_message(telegram_id, "⛔ Доступ к боту отозван администратором.")
    except Exception:
        pass
    await callback.answer("Доступ отозван")


@router.message(Command("settings", "search"))
async def settings(message: Message):
    if not message.from_user:
        return
    user = _get_user(message.from_user.id)
    await message.answer(_settings_text(user.filters), parse_mode="HTML", reply_markup=_settings_keyboard(user.filters))


@router.callback_query(F.data.startswith("budget:"))
async def budget(callback: CallbackQuery):
    amount = int(callback.data.split(":", 1)[1])
    with SessionLocal.begin() as db:
        filters = db.scalar(select(UserFilter).join(User).where(User.telegram_id == callback.from_user.id))
        filters.max_price = amount
        text = _settings_text(filters)
        keyboard = _settings_keyboard(filters)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer("Бюджет сохранен")


@router.callback_query(F.data.startswith("region:"))
async def region(callback: CallbackQuery):
    region_name = REGIONS[int(callback.data.split(":", 1)[1])]
    with SessionLocal.begin() as db:
        filters = db.scalar(select(UserFilter).join(User).where(User.telegram_id == callback.from_user.id))
        current = list(filters.regions)
        filters.regions = [name for name in current if name != region_name] if region_name in current else current + [region_name]
        text = _settings_text(filters)
        keyboard = _settings_keyboard(filters)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer("Регионы обновлены")


@router.callback_query(F.data.startswith("seller:"))
async def seller(callback: CallbackQuery):
    value = callback.data.split(":", 1)[1]
    with SessionLocal.begin() as db:
        filters = db.scalar(select(UserFilter).join(User).where(User.telegram_id == callback.from_user.id))
        filters.seller_type = None if value == "any" else value
        text = _settings_text(filters)
        keyboard = _settings_keyboard(filters)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer("Тип продавца сохранен")


@router.callback_query(F.data.startswith("edit:"))
async def edit(callback: CallbackQuery, state: FSMContext):
    field = callback.data.split(":", 1)[1]
    if field not in {*FIELDS, "max_price"}:
        await callback.answer("Неизвестное поле")
        return
    await state.set_state(EditFilter.value)
    await state.update_data(field=field)
    label = FIELDS.get(field, "бюджет")
    await callback.message.answer(f"Введите {label}. Для списка разделяйте значения запятыми. '-' сбрасывает фильтр.")
    await callback.answer()


@router.message(EditFilter.value)
async def edit_value(message: Message, state: FSMContext):
    if not message.from_user or not message.text:
        return
    field = (await state.get_data())["field"]
    raw = message.text.strip()
    if field in ("max_price", "min_year", "max_mileage"):
        if raw == "-" and field != "max_price":
            value = None
        elif raw.isdigit() and int(raw) > 0:
            value = int(raw)
        else:
            await message.answer("Введите положительное число или '-' для сброса.")
            return
        if field == "min_year" and value is not None and not 1950 <= value <= 2030:
            await message.answer("Год должен быть от 1950 до 2030.")
            return
    else:
        value = [] if raw == "-" else [part.strip() for part in raw.split(",") if part.strip()][:20]
    with SessionLocal.begin() as db:
        filters = db.scalar(select(UserFilter).join(User).where(User.telegram_id == message.from_user.id))
        setattr(filters, field, value)
        text = _settings_text(filters)
        keyboard = _settings_keyboard(filters)
    await state.clear()
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@router.message(Command("sources"))
async def sources(message: Message):
    with SessionLocal() as db:
        rows = db.scalars(select(Source).order_by(Source.kind, Source.key)).all()
    lines = ["<b>Источники</b>"]
    for source in rows:
        lines.append(f"{'✅' if source.enabled else '▫️'} {escape(source.kind)}: {escape(source.identifier)}")
    await message.answer("\n".join(lines)[:4000], parse_mode="HTML")


@router.message(Command("latest"))
async def latest(message: Message):
    if not message.from_user:
        return
    user = _get_user(message.from_user.id)
    with SessionLocal() as db:
        listings = db.scalars(select(Listing).options(selectinload(Listing.photos), selectinload(Listing.source))
                              .order_by(Listing.first_seen_at.desc()).limit(100)).all()
        selected = [listing for listing in listings if matches_filter(listing, user.filters)][:5]
        cards = [(build_card(item, item.source.kind.capitalize(), "new"), card_keyboard(item)) for item in selected]
    if not cards:
        await message.answer("Пока нет объявлений по текущим фильтрам.")
    for text, keyboard in cards:
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data.startswith("dismiss:"))
async def dismiss(callback: CallbackQuery):
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Скрыто")


@router.callback_query(F.data.startswith("hide_model:"))
async def hide_model(callback: CallbackQuery):
    listing_id = int(callback.data.split(":", 1)[1])
    with SessionLocal.begin() as db:
        listing = db.get(Listing, listing_id)
        filters = db.scalar(select(UserFilter).join(User).where(User.telegram_id == callback.from_user.id))
        if listing and listing.model and filters:
            filters.hidden_models = list(set(filters.hidden_models + [listing.model]))
            result = f"Модель {listing.model} скрыта"
        else:
            result = "Модель не определена"
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer(result)


def create_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    return dispatcher

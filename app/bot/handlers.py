from html import escape
from datetime import datetime, timedelta, timezone
import json
import re
from pathlib import Path
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

_GEO_PATH = Path(__file__).resolve().parents[1] / "config" / "geography.json"
GEO_REGIONS = json.loads(_GEO_PATH.read_text(encoding="utf-8"))["regions"]
REGION_NAMES = [region["name"] for region in GEO_REGIONS]
_VEHICLE_PATH = Path(__file__).resolve().parents[1] / "config" / "vehicle_catalog.json"
VEHICLE_BRANDS = json.loads(_VEHICLE_PATH.read_text(encoding="utf-8"))["brands"]


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
    "solo": {"title": "Старт", "price": 990, "duration_days": 30, "reports": 1, "alerts": 100, "speed": "до 60 мин", "seats": 1},
    "plus": {"title": "Плюс", "price": 2490, "duration_days": 30, "reports": 3, "alerts": 500, "speed": "до 30 мин", "seats": 1},
    "pro": {"title": "Профи", "price": 4990, "duration_days": 30, "reports": 8, "alerts": 1500, "speed": "до 15 мин", "seats": 1},
    "business": {"title": "Бизнес", "price": 9990, "duration_days": 30, "reports": 20, "alerts": 4000, "speed": "до 5 мин", "seats": 1},
    "team": {"title": "Команда", "price": 14990, "duration_days": None, "reports": 40, "alerts": 8000, "speed": "до 2 мин", "seats": 5},
}

SOURCE_LABELS = {
    "telegram": "Telegram",
    "avito": "Авито",
    "autoru": "Auto.ru",
    "drom": "Дром",
}

def _plan_period(plan: dict) -> str:
    days = plan.get("duration_days", 30)
    return "навсегда" if days is None else f"{days} дней"


class UserAccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        actor = getattr(event, "from_user", None)
        if actor is None or actor.id not in _admin_ids():
            callback_data = event.data if isinstance(event, CallbackQuery) else ""
            message_text = (event.text or "") if isinstance(event, Message) else ""
            public_callback = (callback_data == "menu:profile" or
                               callback_data.startswith(("support:", "subscription:")))
            support_command = bool(message_text and
                                   message_text.split(maxsplit=1)[0].split("@", 1)[0] == "/support")
            state = data.get("state")
            in_support_flow = bool(state and await state.get_state() == SupportFlow.message.state)
            if public_callback or support_command or in_support_flow:
                return await handler(event, data)
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
LEGACY_REGIONS = ["Татарстан", "Чувашия", "Марий Эл"]
FIELDS = {"min_year": "минимальный год", "max_mileage": "максимальный пробег"}


class EditFilter(StatesGroup):
    value = State()


class AdminFlow(StatesGroup):
    lookup_target = State()


class SupportFlow(StatesGroup):
    message = State()


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
            plan = PLANS.get(pending.subscription_plan or "", {})
            days = plan.get("duration_days", 30)
            user.subscription_expires_at = now + timedelta(days=days) if days is not None else None
            db.delete(pending)
        return user


def _admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔓 Найти и выдать доступ", callback_data="admin:find")],
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:list")],
        [InlineKeyboardButton(text="📊 Тарифы", callback_data="admin:tariffs")],
    ])


def _user_admin_keyboard(user: User) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{plan['title']} — {plan['price']:,} ₽ / {_plan_period(plan)}".replace(",", " "),
                                  callback_data=f"admin:plan:{user.id}:{code}")]
            for code, plan in PLANS.items()]
    if user.active:
        rows.append([InlineKeyboardButton(text="⛔ Отозвать доступ", callback_data=f"admin:revoke:{user.id}")])
    rows.append([InlineKeyboardButton(text="↩️ В админ-панель", callback_data="admin:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _pending_admin_keyboard(grant: PendingAccessGrant) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text=f"{plan['title']} — {plan['price']:,} ₽ / {_plan_period(plan)}".replace(",", " "),
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
    until = (expiry.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC") if expiry
             else ("навсегда" if plan.get("duration_days") is None else "—"))
    username = f"@{escape(user.username)}" if user.username else "username не указан"
    return (f"<b>Пользователь</b> {username}\n"
            f"ID: <code>{user.telegram_id}</code>\n"
            f"Статус: {state}\n"
            f"Тариф: {escape(plan['title'])} ({plan['price']:,} ₽)\n".replace(",", " ")
            + f"Доступ до: {until}")


def _tariff_proposal_text() -> str:
    lines = ["<b>Тарифы</b>"]
    for plan in PLANS.values():
        cost = plan["reports"] * 90
        lines.append(
            f"\n<b>{plan['title']} — {plan['price']:,} ₽ / {_plan_period(plan)}</b>".replace(",", " ")
            + f"\n• до {plan['alerts']:,} объявлений/мес.; цель по задержке: {plan['speed']}"
            .replace(",", " ")
            + f"\n• {plan['reports']} отч. Автотеки (себестоимость около {cost:,} ₽)".replace(",", " ")
            + f"\n• пользователей: до {plan['seats']}"
        )
    lines.append(
        "\n<i>В текущем MVP назначение тарифа открывает доступ на указанный срок; тариф Команда за 14 990 ₽ действует навсегда. "
        "Квоты объявлений и отчётов, а также ускоренная проверка пока не включены. "
        "Сейчас Telegram-каналы дают быстрые события, а Apify-источники проверяются примерно раз в час.</i>"
    )
    return "".join(lines)


def _settings_text(filters: UserFilter) -> str:
    def show(items):
        return escape(", ".join(items)) if items else "любые"
    selected_sources = [SOURCE_LABELS[key] for key in (filters.selected_sources or []) if key in SOURCE_LABELS]
    models_by_brand = filters.models_by_brand or {}
    model_summary = "; ".join(
        f"{brand}: {', '.join(models) if models else 'модели не выбраны'}"
        for brand, models in models_by_brand.items())
    if not model_summary:
        model_summary = ("все модели выбранных марок" if filters.brands else
                         (", ".join(filters.models) if filters.models else "любые"))
    city_map = filters.cities_by_region or {}
    custom_city_count = sum(len(cities) for cities in city_map.values())
    city_text = f"выбрано городов: {custom_city_count}" if city_map else ("все города регионов" if not filters.cities else show(filters.cities))
    return ("<b>Настройки поиска</b>\n"
            f"Поиск: {'🟢 включён' if filters.search_enabled else '⏸ остановлен'}\n"
            f"Бюджет: {filters.max_price:,} ₽\n".replace(",", " ")
            + f"Регионы: {escape(', '.join(filters.regions)) if filters.regions else 'не выбраны'}\nГорода: {city_text}\n"
            + f"Источники: {escape(', '.join(selected_sources)) if selected_sources else 'не выбраны'}\n"
            + f"Марки: {show(filters.brands)}\nМодели: {escape(model_summary)}\n"
            + f"Год от: {filters.min_year or 'любой'}\nПробег до: {filters.max_mileage or 'любой'}\n"
            + f"Продавец: {filters.seller_type or 'любой'}")


def _settings_keyboard(filters: UserFilter) -> InlineKeyboardMarkup:
    search_action = ("⏸ Остановить поиск" if filters.search_enabled else "▶️ Возобновить поиск")
    search_callback = "search:stop" if filters.search_enabled else "search:resume"
    rows = [
        [InlineKeyboardButton(text=f"💰 Бюджет · {filters.max_price:,} ₽".replace(",", " "), callback_data="menu:budget")],
        [InlineKeyboardButton(text="📍 Выбрать гео", callback_data="geo:home:0"),
         InlineKeyboardButton(text="🔎 Источники", callback_data="menu:sources")],
        [InlineKeyboardButton(text="🚘 Марки и модели", callback_data="menu:vehicles")],
        [InlineKeyboardButton(text="⚙️ Другие фильтры", callback_data="menu:filters"),
         InlineKeyboardButton(text="👤 Тариф и поддержка", callback_data="menu:profile")],
        [InlineKeyboardButton(text="📘 Инструкция", callback_data="menu:help")],
        [InlineKeyboardButton(text=search_action, callback_data=search_callback)],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _budget_keyboard() -> InlineKeyboardMarkup:
    values = [(100000, "100 тыс."), (200000, "200 тыс."), (300000, "300 тыс."),
              (500000, "500 тыс."), (1000000, "1 млн"), (3000000, "3 млн")]
    rows = [[InlineKeyboardButton(text=label, callback_data=f"budget:{value}") for value, label in values[i:i + 3]]
            for i in range(0, len(values), 3)]
    rows += [[InlineKeyboardButton(text="✍️ Своя сумма", callback_data="budget:custom")],
             [InlineKeyboardButton(text="↩️ Настройки", callback_data="menu:settings")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _filters_keyboard(filters: UserFilter) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="Год от", callback_data="edit:min_year"),
             InlineKeyboardButton(text="Пробег до", callback_data="edit:max_mileage")],
            [InlineKeyboardButton(text="Любой продавец", callback_data="seller:any"),
             InlineKeyboardButton(text="Частник", callback_data="seller:private"),
             InlineKeyboardButton(text="Дилер", callback_data="seller:dealer")],
            [InlineKeyboardButton(text="↩️ Настройки", callback_data="menu:settings")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _source_keyboard(filters: UserFilter) -> InlineKeyboardMarkup:
    selected = set(filters.selected_sources or [])
    rows = [[InlineKeyboardButton(
        text=f"{'✅' if key in selected else '▫️'} {label}", callback_data=f"source:toggle:{key}")]
        for key, label in SOURCE_LABELS.items()]
    rows.append([InlineKeyboardButton(text="↩️ Настройки", callback_data="menu:settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _vehicle_keyboard(filters: UserFilter, page: int = 0) -> InlineKeyboardMarkup:
    per_page = 8
    page_count = max(1, (len(VEHICLE_BRANDS) + per_page - 1) // per_page)
    page = max(0, min(page, page_count - 1))
    selected = {_canonical_vehicle_brand(brand).casefold() for brand in (filters.brands or [])}
    rows = []
    for index in range(page * per_page, min((page + 1) * per_page, len(VEHICLE_BRANDS))):
        brand = VEHICLE_BRANDS[index]["name"]
        mark = "✅" if brand.casefold() in selected else "▫️"
        rows.append([
            InlineKeyboardButton(text=f"{mark} {brand}", callback_data=f"vehicle:brand:{index}"),
            InlineKeyboardButton(text="Модели", callback_data=f"vehicle:models:{index}:0"),
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"vehicle:home:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{page_count}", callback_data="vehicle:noop"))
    if page + 1 < page_count:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"vehicle:home:{page + 1}"))
    rows += [nav, [InlineKeyboardButton(text="↩️ Настройки", callback_data="menu:settings")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _vehicle_text(filters: UserFilter) -> str:
    brands = list(dict.fromkeys(_canonical_vehicle_brand(brand) for brand in (filters.brands or [])))
    models_by_brand = filters.models_by_brand or {}
    selected_models = sum(len(models) for models in models_by_brand.values())
    brand_text = escape(", ".join(brands)) if brands else "не выбраны"
    return ("<b>Марки и модели</b>\nВыберите марки в каталоге, затем откройте их модели. "
            "Если у марки не задан список моделей, поиск идёт по всем моделям этой марки.\n\n"
            f"Марки: {brand_text}\nВыбрано конкретных моделей: {selected_models}")


def _canonical_vehicle_brand(value: str) -> str:
    normalized = value.strip().casefold()
    for brand in VEHICLE_BRANDS:
        if normalized in {name.casefold() for name in [brand["name"], *brand.get("aliases", [])]}:
            return brand["name"]
    return value.strip()


def _vehicle_models_keyboard(filters: UserFilter, brand_index: int, page: int = 0) -> InlineKeyboardMarkup:
    brand = VEHICLE_BRANDS[brand_index]
    models = brand["models"]
    per_page = 8
    page_count = max(1, (len(models) + per_page - 1) // per_page)
    page = max(0, min(page, page_count - 1))
    chosen = (filters.models_by_brand or {}).get(brand["name"])
    selected_all = chosen is None
    selected_brands = {_canonical_vehicle_brand(value).casefold() for value in (filters.brands or [])}
    rows = [[InlineKeyboardButton(text="✅ Марка выбрана" if brand["name"].casefold() in selected_brands else "▫️ Добавить марку",
                                  callback_data=f"vehicle:brand:{brand_index}")]]
    if selected_all:
        rows.append([InlineKeyboardButton(text="🎯 Выбрать модели вручную", callback_data=f"vehicle:manual:{brand_index}:{page}")])
    else:
        rows.append([InlineKeyboardButton(text="🌐 Все модели марки", callback_data=f"vehicle:all:{brand_index}:{page}")])
    for model_index in range(page * per_page, min((page + 1) * per_page, len(models))):
        model = models[model_index]
        checked = selected_all or model in chosen
        rows.append([InlineKeyboardButton(text=f"{'✅' if checked else '▫️'} {model}",
                                          callback_data=f"vehicle:model:{brand_index}:{model_index}:{page}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"vehicle:models:{brand_index}:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{page_count}", callback_data="vehicle:noop"))
    if page + 1 < page_count:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"vehicle:models:{brand_index}:{page + 1}"))
    rows += [nav, [InlineKeyboardButton(text="↩️ К маркам", callback_data="vehicle:home:0")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _vehicle_models_text(brand_index: int, filters: UserFilter) -> str:
    brand = VEHICLE_BRANDS[brand_index]
    chosen = (filters.models_by_brand or {}).get(brand["name"])
    selection = "Все модели" if chosen is None else f"Выбрано моделей: {len(chosen)}"
    return f"<b>{escape(brand['name'])}</b>\n{selection}. Отметьте нужные модели:"


def _geo_keyboard(filters: UserFilter, page: int = 0) -> InlineKeyboardMarkup:
    per_page = 8
    page_count = max(1, (len(GEO_REGIONS) + per_page - 1) // per_page)
    page = max(0, min(page, page_count - 1))
    rows = [[InlineKeyboardButton(text="🇷🇺 Все регионы · все города", callback_data="geo:all")]]
    for index in range(page * per_page, min((page + 1) * per_page, len(GEO_REGIONS))):
        name = GEO_REGIONS[index]["name"]
        mark = "✅" if name in filters.regions else "▫️"
        rows.append([
            InlineKeyboardButton(text=f"{mark} {name}", callback_data=f"geo:toggle:{index}:{page}"),
            InlineKeyboardButton(text="🏙 Города", callback_data=f"geo:cities:{index}:0:{page}"),
        ])
    page_buttons = []
    if page > 0:
        page_buttons.append(InlineKeyboardButton(text="◀️", callback_data=f"geo:home:{page - 1}"))
    page_buttons.append(InlineKeyboardButton(text=f"{page + 1}/{page_count}", callback_data="geo:noop"))
    if page + 1 < page_count:
        page_buttons.append(InlineKeyboardButton(text="▶️", callback_data=f"geo:home:{page + 1}"))
    rows.append(page_buttons)
    rows.append([InlineKeyboardButton(text="↩️ Настройки", callback_data="menu:settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _geo_city_keyboard(filters: UserFilter, region_index: int, page: int, region_page: int = 0) -> InlineKeyboardMarkup:
    region = GEO_REGIONS[region_index]
    cities = region["cities"]
    per_page = 8
    page_count = max(1, (len(cities) + per_page - 1) // per_page)
    page = max(0, min(page, page_count - 1))
    city_map = filters.cities_by_region or {}
    manual = region["name"] in city_map
    chosen = set(city_map.get(region["name"], []))
    rows = [[InlineKeyboardButton(
        text=("✅ Регион выбран" if region["name"] in filters.regions else "▫️ Добавить регион"),
        callback_data=f"geo:toggle:{region_index}:{region_page}")]]
    if manual:
        rows.append([InlineKeyboardButton(text="🌐 Все города региона", callback_data=f"geo:allcities:{region_index}:{page}:{region_page}")])
    else:
        rows.append([InlineKeyboardButton(text="🎯 Выбрать конкретные города", callback_data=f"geo:manual:{region_index}:{page}:{region_page}")])
    for city_index in range(page * per_page, min((page + 1) * per_page, len(cities))):
        city = cities[city_index]
        checked = city in chosen if manual else True
        rows.append([InlineKeyboardButton(
            text=f"{'✅' if checked else '▫️'} {city}",
            callback_data=f"geo:city:{region_index}:{city_index}:{page}:{region_page}")])
    page_buttons = []
    if page > 0:
        page_buttons.append(InlineKeyboardButton(text="◀️", callback_data=f"geo:cities:{region_index}:{page - 1}:{region_page}"))
    page_buttons.append(InlineKeyboardButton(text=f"{page + 1}/{page_count}", callback_data="geo:noop"))
    if page + 1 < page_count:
        page_buttons.append(InlineKeyboardButton(text="▶️", callback_data=f"geo:cities:{region_index}:{page + 1}:{region_page}"))
    rows.append(page_buttons)
    rows.append([InlineKeyboardButton(text="↩️ К регионам", callback_data=f"geo:home:{region_page}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("start"))
async def start(message: Message):
    if not message.from_user:
        return
    user = _get_user(message.from_user.id, message.from_user.username)
    if message.from_user.id not in _admin_ids() and not user.active:
        await message.answer(
            "🔒 Доступ к поиску пока закрыт. Через профиль можно запросить тариф, а в поддержку — написать администратору.",
            reply_markup=_profile_keyboard())
        return
    if message.from_user.id not in _admin_ids() and user.subscription_expires_at:
        expiry = user.subscription_expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry <= datetime.now(timezone.utc):
            await message.answer("⏳ Срок тарифа закончился. Запросите продление или напишите в поддержку.",
                                 reply_markup=_profile_keyboard())
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
                f"Пользователь с ID <code>{telegram_id}</code> ещё не запускал бота. Выберите тариф: срок начнётся после первого /start.\n"
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
            f"Пользователь @{escape(username)} ещё не запускал бота или не найден. Выберите тариф: срок начнётся после первого /start.\n"
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
        f"✅ Тариф {plan['title']} подготовлен для {escape(recipient)}. Срок действия: {_plan_period(plan)} после первого /start.\n"
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
        user.active = True
        user.subscription_plan = plan_code
        duration = plan.get("duration_days", 30)
        if duration is None:
            user.subscription_expires_at = None
        else:
            expiry = user.subscription_expires_at
            if expiry is None or (expiry.replace(tzinfo=timezone.utc) if expiry.tzinfo is None else expiry) < now:
                expiry = now
            elif expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            user.subscription_expires_at = expiry + timedelta(days=duration)
        telegram_id = user.telegram_id
        username = user.username
        expires_text = user.subscription_expires_at.strftime("%d.%m.%Y") if user.subscription_expires_at else "навсегда"
    delivery_error = False
    try:
        await callback.bot.send_message(telegram_id, f"✅ Вам открыт доступ по тарифу {plan['title']} на {_plan_period(plan)}. Доступ активен до {expires_text}.")
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


def _geo_overview_text(filters: UserFilter) -> str:
    selected = set(filters.regions or [])
    city_map = filters.cities_by_region or {}
    lines = ["<b>Выбор географии</b>",
             f"Выбрано регионов: {len(selected)} из {len(GEO_REGIONS)}.",
             "Внутри региона можно оставить все города или отметить конкретные."]
    custom = [f"{name}: {len(cities)}" for name, cities in city_map.items() if name in selected]
    if custom:
        lines.append("Города по выбранным регионам: " + "; ".join(custom))
    else:
        lines.append("Сейчас включены все города выбранных регионов.")
    lines.append("⚠️ Список регионов полный, но фактическое покрытие пока ограничено: Telegram-каналы подключены для Татарстана, Чувашии и Марий Эл; Apify для Авито, Auto.ru и Дром настроен на Казань. В остальных регионах сборщики пока не подключены.")
    return "\n".join(lines)


def _geo_city_text(filters: UserFilter, region_index: int) -> str:
    region = GEO_REGIONS[region_index]
    selected = set((filters.cities_by_region or {}).get(region["name"], []))
    manual = region["name"] in (filters.cities_by_region or {})
    mode = (f"Выбрано городов: {len(selected)}" if manual else "Выбраны все города региона")
    region_state = "регион включён" if region["name"] in filters.regions else "регион пока не включён"
    return f"<b>{escape(region['name'])}</b>\n{region_state}. {mode}.\nОтметьте нужные города или оставьте весь регион."


def _help_text() -> str:
    return ("<b>Как пользоваться ботом</b>\n"
            "1. В «Бюджет» выберите готовую сумму или введите свою. Для своей суммы отправьте только цифры, например <code>350000</code> — без пробелов, точек и ₽.\n"
            "2. В «Выбрать гео» отметьте регионы. В каждом регионе можно оставить все города или выбрать отдельные.\n"
            "3. В «Источники» включите площадки, от которых хотите получать объявления.\n"
            "4. В «Марки и модели» выберите автомобиль из каталога, а в «Другие фильтры» задайте год, пробег и тип продавца.\n"
            "5. Новые объявления приходят сюда автоматически. Кнопка «Остановить поиск» приостанавливает ваши уведомления и убирает ваш спрос на проверки.\n"
            "6. В «Тариф и поддержка» можно посмотреть срок тарифа, отправить запрос на смену тарифа и написать в поддержку. Подключение тарифа подтверждается администратором.\n\n"
            "Источники общие для сервиса: проверка площадки продолжается, пока она нужна хотя бы одному активному пользователю. Сейчас внешние Apify-сборщики настроены на Казань, а Telegram-каналы — на Татарстан, Чувашию и Марий Эл."
            )


def _profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Управление тарифом", callback_data="subscription:plans")],
        [InlineKeyboardButton(text="🆘 Обратиться в поддержку", callback_data="support:start")],
        [InlineKeyboardButton(text="↩️ Главное меню", callback_data="menu:settings")],
    ])


@router.callback_query(F.data.startswith("menu:"))
async def menu(callback: CallbackQuery):
    action = callback.data.split(":", 1)[1]
    with SessionLocal() as db:
        user = db.scalar(select(User).options(selectinload(User.filters)).where(User.telegram_id == callback.from_user.id))
    if not user or not user.filters:
        await callback.answer("Профиль не найден. Отправьте /start", show_alert=True)
        return
    if action == "settings":
        text, keyboard = _settings_text(user.filters), _settings_keyboard(user.filters)
    elif action == "budget":
        text, keyboard = "<b>Бюджет</b>\nВыберите сумму или нажмите «Своя сумма».", _budget_keyboard()
    elif action == "filters":
        text, keyboard = "<b>Другие фильтры</b>\nВыберите параметр для изменения.", _filters_keyboard(user.filters)
    elif action == "vehicles":
        text, keyboard = _vehicle_text(user.filters), _vehicle_keyboard(user.filters)
    elif action == "sources":
        text = ("<b>Источники объявлений</b>\nВключайте только нужные площадки. Сервис опрашивает общий источник, пока он нужен хотя бы одному активному пользователю.\n\nСейчас Apify (Авито, Auto.ru, Дром) настроен на Казань; Telegram-каналы подключены для Татарстана, Чувашии и Марий Эл.")
        keyboard = _source_keyboard(user.filters)
    elif action == "help":
        text, keyboard = _help_text(), InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Настройки", callback_data="menu:settings")]])
    elif action == "profile":
        plan = PLANS.get(user.subscription_plan or "")
        if plan:
            expiry = user.subscription_expires_at
            if expiry and expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry and expiry <= datetime.now(timezone.utc):
                plan_text = f"{plan['title']} — срок закончился"
            else:
                until = expiry.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC") if expiry else "навсегда"
                plan_text = f"{plan['title']} — {_plan_period(plan)}, действует до {until}"
        else:
            plan_text = "тариф не назначен"
        text = ("<b>Профиль и подписка</b>\n"
                f"Telegram ID: <code>{user.telegram_id}</code>\n"
                f"Текущий тариф: {escape(plan_text)}\n"
                "Для смены тарифа отправьте запрос администратору. Поддержка отвечает в этом чате.")
        keyboard = _profile_keyboard()
    else:
        await callback.answer("Неизвестный раздел")
        return
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@router.message(Command("help"))
async def help_command(message: Message):
    await message.answer(_help_text(), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu:settings")]]))


@router.callback_query(F.data.startswith("vehicle:home:"))
async def vehicle_home(callback: CallbackQuery):
    page = int(callback.data.rsplit(":", 1)[-1])
    with SessionLocal() as db:
        filters = db.scalar(select(UserFilter).join(User).where(User.telegram_id == callback.from_user.id))
    await callback.message.edit_text(_vehicle_text(filters), parse_mode="HTML",
                                     reply_markup=_vehicle_keyboard(filters, page))
    await callback.answer()


@router.callback_query(F.data.startswith("vehicle:brand:"))
async def vehicle_toggle_brand(callback: CallbackQuery):
    index = int(callback.data.rsplit(":", 1)[-1])
    if not 0 <= index < len(VEHICLE_BRANDS):
        await callback.answer("Марка не найдена", show_alert=True)
        return
    brand = VEHICLE_BRANDS[index]["name"]
    with SessionLocal.begin() as db:
        filters = db.scalar(select(UserFilter).join(User).where(User.telegram_id == callback.from_user.id))
        selected = list(filters.brands or [])
        selected_canonical = {_canonical_vehicle_brand(value).casefold() for value in selected}
        model_map = dict(filters.models_by_brand or {})
        if brand.casefold() in selected_canonical:
            selected = [value for value in selected if _canonical_vehicle_brand(value).casefold() != brand.casefold()]
            model_map.pop(brand, None)
            result = f"{brand} удалена"
        else:
            selected.append(brand)
            result = f"{brand} добавлена; выберите конкретные модели или оставьте все"
        filters.brands = selected
        filters.models_by_brand = model_map
        text, keyboard = _vehicle_text(filters), _vehicle_keyboard(filters)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer(result)


@router.callback_query(F.data.startswith("vehicle:models:"))
async def vehicle_models(callback: CallbackQuery):
    _, _, index_text, page_text = callback.data.split(":", 3)
    index, page = int(index_text), int(page_text)
    if not 0 <= index < len(VEHICLE_BRANDS):
        await callback.answer("Марка не найдена", show_alert=True)
        return
    with SessionLocal() as db:
        filters = db.scalar(select(UserFilter).join(User).where(User.telegram_id == callback.from_user.id))
    await callback.message.edit_text(_vehicle_models_text(index, filters), parse_mode="HTML",
                                     reply_markup=_vehicle_models_keyboard(filters, index, page))
    await callback.answer()


@router.callback_query(F.data.startswith("vehicle:manual:"))
async def vehicle_manual_models(callback: CallbackQuery):
    _, _, index_text, page_text = callback.data.split(":", 3)
    index, page = int(index_text), int(page_text)
    brand = VEHICLE_BRANDS[index]["name"]
    with SessionLocal.begin() as db:
        filters = db.scalar(select(UserFilter).join(User).where(User.telegram_id == callback.from_user.id))
        selected = list(filters.brands or [])
        if brand not in selected:
            selected.append(brand)
        filters.brands = selected
        model_map = dict(filters.models_by_brand or {})
        model_map[brand] = []
        filters.models_by_brand = model_map
        text, keyboard = _vehicle_models_text(index, filters), _vehicle_models_keyboard(filters, index, page)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer("Отметьте подходящие модели")


@router.callback_query(F.data.startswith("vehicle:all:"))
async def vehicle_all_models(callback: CallbackQuery):
    _, _, index_text, page_text = callback.data.split(":", 3)
    index, page = int(index_text), int(page_text)
    brand = VEHICLE_BRANDS[index]["name"]
    with SessionLocal.begin() as db:
        filters = db.scalar(select(UserFilter).join(User).where(User.telegram_id == callback.from_user.id))
        model_map = dict(filters.models_by_brand or {})
        model_map.pop(brand, None)
        filters.models_by_brand = model_map
        selected = list(filters.brands or [])
        if brand not in selected:
            selected.append(brand)
        filters.brands = selected
        text, keyboard = _vehicle_models_text(index, filters), _vehicle_models_keyboard(filters, index, page)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer("Включены все модели марки")


@router.callback_query(F.data.startswith("vehicle:model:"))
async def vehicle_toggle_model(callback: CallbackQuery):
    _, _, index_text, model_index_text, page_text = callback.data.split(":", 4)
    index, model_index, page = int(index_text), int(model_index_text), int(page_text)
    brand_data = VEHICLE_BRANDS[index]
    if not 0 <= model_index < len(brand_data["models"]):
        await callback.answer("Модель не найдена", show_alert=True)
        return
    brand, model = brand_data["name"], brand_data["models"][model_index]
    with SessionLocal.begin() as db:
        filters = db.scalar(select(UserFilter).join(User).where(User.telegram_id == callback.from_user.id))
        brands = list(filters.brands or [])
        if brand not in brands:
            brands.append(brand)
        filters.brands = brands
        model_map = dict(filters.models_by_brand or {})
        if brand not in model_map:
            # The catalog screen starts in "all models" mode. Unchecking one
            # model converts it to an explicit all-except-this selection.
            model_map[brand] = [name for name in brand_data["models"] if name != model]
        else:
            selected = list(model_map[brand])
            if model in selected:
                selected.remove(model)
            else:
                selected.append(model)
            model_map[brand] = selected
        filters.models_by_brand = model_map
        text, keyboard = _vehicle_models_text(index, filters), _vehicle_models_keyboard(filters, index, page)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer("Модель обновлена")


@router.callback_query(F.data == "vehicle:noop")
async def vehicle_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data == "subscription:plans")
async def subscription_plans(callback: CallbackQuery):
    rows = [[InlineKeyboardButton(
        text=f"Запросить: {plan['title']} · {plan['price']:,} ₽ / {_plan_period(plan)}".replace(",", " "),
        callback_data=f"subscription:request:{code}")]
        for code, plan in PLANS.items()]
    rows.append([InlineKeyboardButton(text="↩️ Профиль", callback_data="menu:profile")])
    await callback.message.edit_text(
        "<b>Управление тарифом</b>\nВыберите тариф, чтобы отправить запрос администратору. "
        "Оплата и включение тарифа подтверждаются вручную.", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data.startswith("subscription:request:"))
async def subscription_request(callback: CallbackQuery):
    code = callback.data.rsplit(":", 1)[-1]
    plan = PLANS.get(code)
    owner_id = get_settings().telegram_admin_id
    if not plan or not owner_id:
        await callback.answer("Не удалось отправить запрос. Напишите в поддержку.", show_alert=True)
        return
    username = f"@{callback.from_user.username}" if callback.from_user.username else "без username"
    await callback.bot.send_message(
        owner_id,
        f"💳 Запрос тарифа «{plan['title']}» ({plan['price']} ₽ / {_plan_period(plan)}).\n"
        f"Пользователь: {username}\nID: <code>{callback.from_user.id}</code>", parse_mode="HTML")
    await callback.message.edit_text(
        f"Запрос на тариф «{escape(plan['title'])}» отправлен администратору. "
        "Он свяжется с вами в этом чате после подтверждения.", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Профиль", callback_data="menu:profile")]]))
    await callback.answer("Запрос отправлен")


@router.callback_query(F.data == "support:start")
async def support_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SupportFlow.message)
    await callback.message.answer(
        "Напишите сообщение в поддержку или отправьте фото/файл. Я передам его администратору.\n"
        "Для отмены отправьте /cancel.")
    await callback.answer()


@router.message(Command("support"))
async def support_command(message: Message, state: FSMContext):
    await state.set_state(SupportFlow.message)
    await message.answer("Напишите сообщение в поддержку или отправьте фото/файл. Для отмены отправьте /cancel.")


@router.message(SupportFlow.message)
async def support_message(message: Message, state: FSMContext):
    if not message.from_user:
        return
    if message.text and message.text.split(maxsplit=1)[0].split("@", 1)[0] == "/cancel":
        await state.clear()
        await message.answer("Обращение отменено.")
        return
    owner_id = get_settings().telegram_admin_id
    if not owner_id:
        await state.clear()
        await message.answer("Поддержка временно недоступна. Попробуйте позже.")
        return
    username = f"@{message.from_user.username}" if message.from_user.username else "без username"
    await message.bot.send_message(
        owner_id,
        f"🆘 Обращение в поддержку\nID пользователя: {message.from_user.id}\n"
        f"Пользователь: {escape(username)}\nОтветьте на это сообщение, чтобы написать пользователю.",
        parse_mode="HTML")
    await message.bot.copy_message(chat_id=owner_id, from_chat_id=message.chat.id,
                                   message_id=message.message_id)
    await state.clear()
    await message.answer("Сообщение передано в поддержку. Ответ придёт сюда в боте.")


@router.message(F.reply_to_message)
async def support_admin_reply(message: Message):
    if not message.from_user or not _is_owner(message.from_user.id) or not message.reply_to_message:
        return
    source_text = message.reply_to_message.text or message.reply_to_message.caption or ""
    match = re.search(r"ID пользователя:\s*(\d+)", source_text)
    if not match:
        return
    target_id = int(match.group(1))
    await message.bot.send_message(target_id, "✉️ Ответ поддержки:")
    await message.bot.copy_message(chat_id=target_id, from_chat_id=message.chat.id,
                                   message_id=message.message_id)
    await message.answer("Ответ отправлен пользователю.")


@router.callback_query(F.data.startswith("source:toggle:"))
async def toggle_source(callback: CallbackQuery):
    source = callback.data.rsplit(":", 1)[-1]
    if source not in SOURCE_LABELS:
        await callback.answer("Неизвестный источник", show_alert=True)
        return
    with SessionLocal.begin() as db:
        filters = db.scalar(select(UserFilter).join(User).where(User.telegram_id == callback.from_user.id))
        selected = set(filters.selected_sources or [])
        if source in selected:
            selected.remove(source)
            result = f"{SOURCE_LABELS[source]} выключен"
        else:
            selected.add(source)
            result = f"{SOURCE_LABELS[source]} включён"
        filters.selected_sources = [key for key in SOURCE_LABELS if key in selected]
        keyboard = _source_keyboard(filters)
    await callback.message.edit_reply_markup(reply_markup=keyboard)
    await callback.answer(result)


@router.callback_query(F.data.startswith("geo:home:"))
async def geo_home(callback: CallbackQuery):
    page = int(callback.data.rsplit(":", 1)[-1])
    with SessionLocal() as db:
        filters = db.scalar(select(UserFilter).join(User).where(User.telegram_id == callback.from_user.id))
    await callback.message.edit_text(_geo_overview_text(filters), parse_mode="HTML", reply_markup=_geo_keyboard(filters, page))
    await callback.answer()


@router.callback_query(F.data == "geo:all")
async def geo_all(callback: CallbackQuery):
    with SessionLocal.begin() as db:
        filters = db.scalar(select(UserFilter).join(User).where(User.telegram_id == callback.from_user.id))
        filters.regions = REGION_NAMES.copy()
        filters.cities = []
        filters.cities_by_region = {}
        text, keyboard = _geo_overview_text(filters), _geo_keyboard(filters)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer("Выбраны все регионы и города")


@router.callback_query(F.data.startswith("geo:toggle:"))
async def geo_toggle_region(callback: CallbackQuery):
    _, _, index_text, page_text = callback.data.split(":", 3)
    index, page = int(index_text), int(page_text)
    region_name = GEO_REGIONS[index]["name"]
    with SessionLocal.begin() as db:
        filters = db.scalar(select(UserFilter).join(User).where(User.telegram_id == callback.from_user.id))
        selected = list(filters.regions or [])
        city_map = dict(filters.cities_by_region or {})
        if region_name in selected:
            selected.remove(region_name)
            city_map.pop(region_name, None)
            result = "Регион выключен"
        else:
            selected.append(region_name)
            result = "Регион включён"
        filters.regions = selected
        filters.cities_by_region = city_map
        text, keyboard = _geo_overview_text(filters), _geo_keyboard(filters, page)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer(result)


@router.callback_query(F.data.startswith("geo:cities:"))
async def geo_cities(callback: CallbackQuery):
    _, _, index_text, page_text, region_page_text = callback.data.split(":", 4)
    index, page, region_page = int(index_text), int(page_text), int(region_page_text)
    with SessionLocal() as db:
        filters = db.scalar(select(UserFilter).join(User).where(User.telegram_id == callback.from_user.id))
    await callback.message.edit_text(_geo_city_text(filters, index), parse_mode="HTML",
                                     reply_markup=_geo_city_keyboard(filters, index, page, region_page))
    await callback.answer()


@router.callback_query(F.data.startswith("geo:manual:"))
async def geo_manual_cities(callback: CallbackQuery):
    _, _, index_text, page_text, region_page_text = callback.data.split(":", 4)
    index, page, region_page = int(index_text), int(page_text), int(region_page_text)
    name = GEO_REGIONS[index]["name"]
    with SessionLocal.begin() as db:
        filters = db.scalar(select(UserFilter).join(User).where(User.telegram_id == callback.from_user.id))
        regions = list(filters.regions or [])
        if name not in regions:
            regions.append(name)
        filters.regions = regions
        city_map = dict(filters.cities_by_region or {})
        city_map[name] = []
        filters.cities_by_region = city_map
        filters.cities = []
        text, keyboard = _geo_city_text(filters, index), _geo_city_keyboard(filters, index, page, region_page)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer("Отметьте нужные города")


@router.callback_query(F.data.startswith("geo:allcities:"))
async def geo_all_cities(callback: CallbackQuery):
    _, _, index_text, page_text, region_page_text = callback.data.split(":", 4)
    index, page, region_page = int(index_text), int(page_text), int(region_page_text)
    name = GEO_REGIONS[index]["name"]
    with SessionLocal.begin() as db:
        filters = db.scalar(select(UserFilter).join(User).where(User.telegram_id == callback.from_user.id))
        regions = list(filters.regions or [])
        if name not in regions:
            regions.append(name)
        filters.regions = regions
        city_map = dict(filters.cities_by_region or {})
        city_map.pop(name, None)
        filters.cities_by_region = city_map
        filters.cities = []
        text, keyboard = _geo_city_text(filters, index), _geo_city_keyboard(filters, index, page, region_page)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer("Включены все города региона")


@router.callback_query(F.data.startswith("geo:city:"))
async def geo_toggle_city(callback: CallbackQuery):
    _, _, index_text, city_index_text, page_text, region_page_text = callback.data.split(":", 5)
    index, city_index = int(index_text), int(city_index_text)
    page, region_page = int(page_text), int(region_page_text)
    region = GEO_REGIONS[index]
    name, city = region["name"], region["cities"][city_index]
    with SessionLocal.begin() as db:
        filters = db.scalar(select(UserFilter).join(User).where(User.telegram_id == callback.from_user.id))
        selected_regions = list(filters.regions or [])
        if name not in selected_regions:
            selected_regions.append(name)
        filters.regions = selected_regions
        city_map = dict(filters.cities_by_region or {})
        if name not in city_map:
            city_map[name] = [value for value in region["cities"] if value != city]
        else:
            selected = list(city_map[name])
            if city in selected:
                selected.remove(city)
            else:
                selected.append(city)
            city_map[name] = selected
        filters.cities_by_region = city_map
        filters.cities = []
        text, keyboard = _geo_city_text(filters, index), _geo_city_keyboard(filters, index, page, region_page)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer("Город обновлён")


@router.callback_query(F.data == "geo:noop")
async def geo_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.startswith("search:"))
async def toggle_search(callback: CallbackQuery):
    enabled = callback.data.endswith("resume")
    with SessionLocal.begin() as db:
        filters = db.scalar(select(UserFilter).join(User).where(User.telegram_id == callback.from_user.id))
        filters.search_enabled = enabled
        text, keyboard = _settings_text(filters), _settings_keyboard(filters)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer("Поиск возобновлён" if enabled else "Поиск остановлен")


@router.callback_query(F.data.startswith("budget:"))
async def budget(callback: CallbackQuery, state: FSMContext):
    raw = callback.data.split(":", 1)[1]
    if raw == "custom":
        await state.set_state(EditFilter.value)
        await state.update_data(field="max_price")
        await callback.message.answer(
            "Введите бюджет <b>только цифрами</b>, например <code>350000</code>.\n"
            "❗ Без пробелов, точек и знака ₽.", parse_mode="HTML")
        await callback.answer("Жду сумму")
        return
    if not raw.isdigit() or not 1 <= int(raw) <= 2_000_000_000:
        await callback.answer("Некорректная сумма", show_alert=True)
        return
    amount = int(raw)
    with SessionLocal.begin() as db:
        filters = db.scalar(select(UserFilter).join(User).where(User.telegram_id == callback.from_user.id))
        filters.max_price = amount
        text = _settings_text(filters)
        keyboard = _settings_keyboard(filters)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer("Бюджет сохранен")


@router.callback_query(F.data.startswith("region:"))
async def region(callback: CallbackQuery):
    index = int(callback.data.split(":", 1)[1])
    if index >= len(LEGACY_REGIONS):
        await callback.answer("Откройте «Выбрать гео»", show_alert=True)
        return
    region_name = LEGACY_REGIONS[index]
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
            if field == "max_price":
                await message.answer("❗ Введите сумму только цифрами, например <code>350000</code>: без пробелов, точек и знака ₽.", parse_mode="HTML")
            else:
                await message.answer("Введите положительное число или '-' для сброса.")
            return
        if field == "max_price" and value is not None and value > 2_000_000_000:
            await message.answer("Сумма слишком большая. Введите число до 2000000000.")
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

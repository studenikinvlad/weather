"""A small aiogram interface that reuses each city's invitation."""

import asyncio
import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LinkPreviewOptions,
    Message,
    ReplyKeyboardMarkup,
)
from telethon import errors

from city_folders.alerts import WeatherAlerts
from city_folders.location import Geocoder, city_label, remember_city
from city_folders.models import CITIES, CURRENCIES, RATES_ENABLED, PilotError
from city_folders.publisher import WeatherPublisher
from city_folders.service import Provisioner
from city_folders.system_messages import SystemMessageCleaner
from city_folders.temporary_invites import TemporaryInvites

logger = logging.getLogger(__name__)


def city_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=city.name, callback_data=f"city:{city.id}"
                )
            ]
            for city in CITIES.values()
        ]
    )


def main_keyboard(has_cities=False, *, is_owner=False):
    rows = [
        [
            KeyboardButton(
                text="📍 Определить мой город", request_location=True
            )
        ],
        [KeyboardButton(text="🔎 Выбрать другой город")],
        [KeyboardButton(text="ℹ️ Как это работает")],
    ]
    if has_cities:
        rows.append([KeyboardButton(text="🏙 Мои города")])
        rows.append([KeyboardButton(text="⚙️ Настройки")])
    if is_owner:
        rows.append([KeyboardButton(text="🔄 Обновить погоду")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="Напиши город или отправь геометку",
    )


def invitation_text(name, previous, city_id):
    text = (
        f"{name} готов к добавлению.\n\n"
        "Добавь его в Telegram — погода будет всегда видна "
        "прямо в списке чатов."
    )
    if city_id in previous:
        text += "\n\nУже добавлял этот город? Открой его в списке чатов."
    elif previous:
        text += (
            "\n\nПри добавлении ещё одного города Telegram может предложить "
            "обновить существующую папку. Проверь выбранные города."
        )
    return text


def build_router(
    bot: Bot,
    provisioner: Provisioner,
    allowed_user_ids: frozenset[int],
    weather: WeatherPublisher,
    invites: TemporaryInvites,
    alerts: WeatherAlerts | None = None,
) -> Router:
    router = Router()
    geocoder = Geocoder()
    router.message.filter(F.chat.type == ChatType.PRIVATE)

    def allowed(user_id: int) -> bool:
        return (
            user_id == provisioner.state.account_id
            or not allowed_user_ids
            or user_id in allowed_user_ids
        )

    def keyboard(uid):
        return main_keyboard(
            bool(invites.ledger.cities_for(uid)),
            is_owner=bool(provisioner.state.account_id)
            and uid == provisioner.state.account_id,
        )

    refreshing = False

    def refresh_cities():
        return [
            key.rsplit(":", 1)[0]
            for key, channel in provisioner.state.channels.items()
            if key.endswith(":weather") and channel.ready
        ]

    @router.message(Command("refresh"))
    @router.message(F.text == "🔄 Обновить погоду")
    async def refresh_menu(message: Message):
        if (
            not message.from_user
            or not provisioner.state.account_id
            or message.from_user.id != provisioner.state.account_id
        ):
            return
        cities = refresh_cities()
        if not cities:
            await message.answer("Погодных каналов пока нет.")
            return
        rows = [
            [
                InlineKeyboardButton(
                    text=CITIES[city].name if city in CITIES else city,
                    callback_data=f"refresh:{city}",
                )
            ]
            for city in cities
        ]
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔄 Обновить все",
                    callback_data="refresh:all",
                )
            ]
        )
        await message.answer(
            "Какой прогноз обновить?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

    @router.callback_query(
        F.data.startswith("refresh:") | (F.data == "weather_refresh")
    )
    async def refresh_weather(query: CallbackQuery):
        nonlocal refreshing
        owner = provisioner.state.account_id
        if not owner or query.from_user.id != owner:
            await query.answer(
                "Обновление доступно только владельцу.", show_alert=True
            )
            return
        if (
            not isinstance(query.message, Message)
            or query.message.chat.type != ChatType.PRIVATE
            or query.message.chat.id != owner
            or query.data == "weather_refresh"
        ):
            await query.answer(
                "Открой меню обновления в личном чате с ботом.",
                show_alert=True,
            )
            return
        target = query.data.split(":", 1)[1]
        cities = refresh_cities()
        if target != "all":
            cities = [target] if target in cities else []
        if not cities:
            await query.answer("Канал недоступен. Открой /refresh заново.")
            return
        if refreshing or weather.lock.locked():
            await query.answer("Обновление уже идёт. Попробуй чуть позже.")
            return
        refreshing = True
        try:
            await query.answer("Обновляю погоду…")
            status = await query.message.answer("Обновление запущено…")
            failed = []
            for city in cities:
                if not await weather.update(city, force=True):
                    failed.append(
                        CITIES[city].name if city in CITIES else city
                    )
            text = (
                f"Обновлено каналов: {len(cities) - len(failed)} "
                f"из {len(cities)}."
            )
            if failed:
                text += "\nНе удалось обновить: " + ", ".join(failed)
            await status.edit_text(text)
        finally:
            refreshing = False

    @router.callback_query(F.data == "weather_current")
    async def retired_personal_weather(query: CallbackQuery):
        await query.answer("Эта кнопка больше не используется.")

    def alert_keyboard(uid):
        rows = []
        for city in invites.ledger.cities_for(uid):
            if city not in CITIES:
                continue
            enabled = alerts.enabled(uid, city)
            rows.append(
                [
                    InlineKeyboardButton(
                        text=("🔔 " if enabled else "🔕 ") + CITIES[city].name,
                        callback_data=f"alert:{int(not enabled)}:{city}",
                    )
                ]
            )
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @router.message(Command("settings"))
    @router.message(F.text == "⚙️ Настройки")
    async def settings(message: Message):
        if not message.from_user or not allowed(message.from_user.id):
            return
        if alerts is None:
            return
        await message.answer(
            "Предупреждения о погоде\n\n"
            "Включи нужный город: 🔕 выключено · 🔔 включено.\n"
            "Сообщу здесь о приближении дождя, снега или грозы "
            "с вероятностью от 60%, а также изменении температуры "
            "на 5° за час. Обычно за 15–75 минут, по доступному прогнозу.\n"
            "Сообщение удаляется через час, пока бот работает. "
            "По умолчанию всё выключено.\n"
            "Если городов нет — сначала выбери город через /add.",
            reply_markup=alert_keyboard(message.from_user.id),
        )

    @router.callback_query(F.data.startswith("alert:"))
    async def alert_setting(query: CallbackQuery):
        if (
            alerts is None
            or not allowed(query.from_user.id)
            or not isinstance(query.message, Message)
            or query.message.chat.type != ChatType.PRIVATE
        ):
            await query.answer()
            return
        parts = (query.data or "").split(":", 2)
        if len(parts) != 3 or parts[1] not in {"0", "1"}:
            await query.answer("Открой /settings заново.")
            return
        enabled = parts[1] == "1"
        if not alerts.set_enabled(query.from_user.id, parts[2], enabled):
            await query.answer("Сначала выбери этот город.")
            return
        await query.answer(
            "Предупреждения включены"
            if enabled
            else "Предупреждения выключены"
        )
        await query.message.edit_reply_markup(
            reply_markup=alert_keyboard(query.from_user.id)
        )

    def rates_keyboard(mask):
        rows = [
            [
                InlineKeyboardButton(
                    text=f"{'✅' if mask & (1 << i) else '▫️'} {name} · {code}",
                    callback_data=f"rates:{mask ^ (1 << i)}",
                )
            ]
            for i, (code, name) in enumerate(CURRENCIES.items())
        ]
        if mask:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="📁 Добавить выбранные в папку",
                        callback_data=f"ratesadd:{mask}",
                    )
                ]
            )
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @router.message(Command("rates"))
    @router.message(F.text == "💱 Курсы валют")
    async def rates_menu(message: Message):
        if not message.from_user or not allowed(message.from_user.id):
            return
        if not RATES_ENABLED:
            await message.answer(
                "Курсы валют пока недоступны. Сейчас здесь только погода.",
                reply_markup=keyboard(message.from_user.id),
            )
            return
        await message.answer(
            "💱 Выбери курсы для своей погодной папки.\n"
            "Доллар, евро и юань — официальный курс; "
            "биткоин — рыночный ориентир с задержкой около 15 минут.\n\n"
            "Нажми на валюту, чтобы включить или убрать её.",
            reply_markup=rates_keyboard(15),
        )

    @router.callback_query(
        F.data.startswith("rates:") | F.data.startswith("ratesadd:")
    )
    async def select_rates(query: CallbackQuery):
        if (
            not allowed(query.from_user.id)
            or not isinstance(query.message, Message)
            or query.message.chat.type != ChatType.PRIVATE
        ):
            await query.answer("Открой личный чат с ботом.")
            return
        if not RATES_ENABLED:
            await query.answer("Курсы валют пока недоступны.", show_alert=True)
            await query.message.edit_reply_markup(reply_markup=None)
            return
        action, raw = (query.data or "").split(":", 1)
        if not raw.isdecimal() or not 0 <= int(raw) <= 15:
            await query.answer("Открой меню курсов заново.")
            return
        mask = int(raw)
        await query.answer()
        if action == "rates":
            await query.message.edit_reply_markup(
                reply_markup=rates_keyboard(mask)
            )
            return
        codes = [code for i, code in enumerate(CURRENCIES) if mask & (1 << i)]
        try:
            url = await invites.issue(
                query.from_user.id, "rates", currencies=codes
            )
            if url is None:
                await query.message.answer(
                    "Подпишись на основной канал и повтори выбор валют.",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="Основной канал",
                                    url=invites.main_channel_url,
                                )
                            ]
                        ]
                    ),
                )
                return
        except (
            PilotError,
            errors.RPCError,
            TelegramAPIError,
            OSError,
            TimeoutError,
        ) as exc:
            if isinstance(exc, errors.FloodWaitError):
                provisioner.remember_flood(exc.seconds, request=exc.request)
            logger.warning("Currency invitation: %s", type(exc).__name__)
            await query.message.answer(
                str(exc)
                if isinstance(exc, PilotError)
                else "Не удалось подготовить каналы. Попробуй немного позже."
            )
            return
        await query.message.answer(
            f"Курсы готовы: {', '.join(codes)}.\n\n"
            + (
                "Добавь их в папку с погодой. В ссылке также твои города: "
                + ", ".join(
                    CITIES[city].name
                    for city in invites.ledger.cities_for(query.from_user.id)
                    if city in CITIES
                )
                + "."
                if any(
                    city in CITIES
                    for city in invites.ledger.cities_for(query.from_user.id)
                )
                else "Добавь их в Telegram-папку. Город можно выбрать позже."
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Добавить в папку →", url=url)]
                ]
            ),
        )

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        if message.from_user is None or not allowed(message.from_user.id):
            await message.answer(
                "Сейчас бот доступен участникам тестирования."
            )
            return
        await message.answer(
            "Погода прямо в списке чатов.\n\n"
            "Температура и погода всегда видны на аватарке — "
            "открывать приложение не нужно.\n\nВыбери город 👇",
            reply_markup=keyboard(message.from_user.id),
        )

    @router.message(Command("add"))
    @router.message(
        F.text.in_(
            {
                "➕ Добавить город",
                "🔎 Выбрать другой город",
                "Выбрать город",
                "＋ Ещё город",
            }
        )
    )
    async def add_city(message: Message):
        if message.from_user and allowed(message.from_user.id):
            await message.answer(
                "Напиши название города, можно с регионом, "
                "или отправь геометку.",
                reply_markup=keyboard(message.from_user.id),
            )

    @router.message(Command("help"))
    @router.message(F.text == "ℹ️ Как это работает")
    async def help_message(message: Message):
        if not message.from_user or not allowed(message.from_user.id):
            return
        await message.answer(
            "snowaa добавляет выбранный город в Telegram.\n\n"
            "🌡 Аватар показывает текущую температуру.\n"
            "💬 В списке чатов виден краткий прогноз.\n"
            "🕐 Внутри — прогноз на 20 часов и 11 дней.\n"
            "🔄 Всё обновляется автоматически.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Выбрать город", callback_data="choose"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Помощь с добавлением",
                            callback_data="folder_help",
                        )
                    ],
                    *(
                        [
                            [
                                InlineKeyboardButton(
                                    text="💱 Курсы валют",
                                    callback_data="rates:15",
                                )
                            ]
                        ]
                        if RATES_ENABLED
                        else []
                    ),
                ]
            ),
        )

    @router.callback_query(F.data.in_({"choose", "folder_help"}))
    async def help_action(query: CallbackQuery):
        if (
            not allowed(query.from_user.id)
            or not isinstance(query.message, Message)
            or query.message.chat.type != ChatType.PRIVATE
        ):
            await query.answer()
            return
        await query.answer()
        text = (
            "Напиши название города, можно с регионом, или отправь геометку."
        )
        if query.data == "folder_help":
            text = (
                "Ссылка не открывается? Запроси её снова "
                "в «Мои города» /cities.\n\n"
                "Две одинаковые папки? Telegram различает их по источнику, "
                "а не по названию. Проверь состав в настройках папок. "
                "Если решишь удалить лишнюю, оставь нужные чаты.\n\n"
                "Бот не видит твои папки и не может объединить их. "
                "Крестик в «Мои города» меняет только список бота."
            )
        await query.message.answer(
            text, reply_markup=keyboard(query.from_user.id)
        )

    @router.message(Command("cities"))
    @router.message(F.text == "🏙 Мои города")
    async def my_cities(message: Message):
        if not message.from_user or not allowed(message.from_user.id):
            return
        cities = [
            CITIES[k]
            for k in invites.ledger.cities_for(message.from_user.id)
            if k in CITIES
        ]
        if not cities:
            await message.answer(
                "Пока нет сохранённых городов. Напиши название города.",
                reply_markup=keyboard(message.from_user.id),
            )
            return
        await message.answer(
            "Сохранённые города. Выбери город, чтобы получить ссылку. "
            "Крестик убирает город только из списка бота.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=c.name, callback_data=f"city:{c.id}"
                        ),
                        InlineKeyboardButton(
                            text="✕", callback_data=f"remove:{c.id}"
                        ),
                    ]
                    for c in cities
                ]
            ),
        )

    @router.callback_query(F.data.startswith("remove:"))
    async def remove_city(query: CallbackQuery):
        if (
            not allowed(query.from_user.id)
            or not isinstance(query.message, Message)
            or query.message.chat.type != "private"
        ):
            await query.answer()
            return
        if alerts is not None:
            alerts.set_enabled(
                query.from_user.id, query.data.split(":", 1)[1], False
            )
        invites.ledger.remove_city(
            query.from_user.id, query.data.split(":", 1)[1]
        )
        await query.answer("Город убран из списка.")
        await query.message.edit_reply_markup(reply_markup=None)
        await query.message.answer(
            "Список обновлён. Подписка на канал остаётся — "
            "выйти из него можно в Telegram.",
            reply_markup=keyboard(query.from_user.id),
        )

    @router.message(F.location)
    @router.message(F.text & ~F.text.startswith("/"))
    async def find_city(message: Message) -> None:
        if not message.from_user or not allowed(message.from_user.id):
            return
        try:
            if message.location:
                city = await geocoder.reverse(
                    message.location.latitude, message.location.longitude
                )
                found = [city] if city else []
            else:
                found = await geocoder.search(message.text or "")
            cities = [remember_city(provisioner, city) for city in found]
        except PilotError as exc:
            await message.answer(str(exc))
            return
        if not cities:
            await message.answer(
                "Город не найден. Уточни название или геометку."
            )
            return
        await message.answer(
            'Подтверди город:\n\n© <a href="https://www.openstreetmap.org/copyright">'
            "OpenStreetMap contributors</a>",
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=city_label(city),
                            callback_data=f"city:{city.id}",
                        )
                    ]
                    for city in cities
                ]
            ),
        )

    @router.callback_query(
        F.data.startswith("city:") | F.data.startswith("check:")
    )
    async def choose_city(query: CallbackQuery) -> None:
        if not allowed(query.from_user.id):
            await query.answer("Доступ ограничен.", show_alert=True)
            return
        if (
            not isinstance(query.message, Message)
            or query.message.chat.type != ChatType.PRIVATE
        ):
            await query.answer("Открой личный чат с ботом.", show_alert=True)
            return
        city_id = (query.data or "").split(":", 1)[-1]
        if city_id not in CITIES:
            await query.answer("Выбери город из списка.", show_alert=True)
            return
        await query.answer()
        previous = invites.ledger.cities_for(query.from_user.id)
        try:
            url = await invites.issue(query.from_user.id, city_id)
            if url is None:
                await query.message.answer(
                    "Чтобы получить папку, подпишитесь на основной канал "
                    "и нажмите «Проверить подписку».",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="Подписаться",
                                    url=invites.main_channel_url,
                                )
                            ],
                            [
                                InlineKeyboardButton(
                                    text="Проверить подписку",
                                    callback_data=f"check:{city_id}",
                                )
                            ],
                        ]
                    ),
                )
                return
        except (
            PilotError,
            errors.RPCError,
            TelegramAPIError,
            OSError,
            TimeoutError,
        ) as exc:
            if isinstance(exc, errors.FloodWaitError):
                provisioner.remember_flood(exc.seconds, request=exc.request)
            # Never log request objects, credentials, or complete exceptions.
            logger.warning("Provisioning failed: %s", type(exc).__name__)
            await query.message.answer(
                str(exc)
                if isinstance(exc, PilotError)
                else "Не удалось подготовить папку. Попробуйте позже."
            )
            return
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"Добавить {CITIES[city_id].name} →", url=url
                    )
                ]
            ]
        )
        invites.ledger.add_city(query.from_user.id, city_id)
        text = invitation_text(CITIES[city_id].name, previous, city_id)
        if query.from_user.id == provisioner.state.account_id:
            text += (
                "\n\nЭто аккаунт владельца исходной папки. "
                "Проверь добавление с другого аккаунта."
            )
        await query.message.answer(text, reply_markup=keyboard)

    @router.my_chat_member(F.chat.type == ChatType.CHANNEL)
    async def bot_membership(event: ChatMemberUpdated) -> None:
        city_id = weather.weather_city(event.chat.id)
        if city_id is None:
            logger.warning(
                "Канал %s не привязан к городу; прогноз не опубликован.",
                event.chat.id,
            )
            return
        member = event.new_chat_member
        if member.status == "administrator" and member.can_post_messages:
            await weather.update(city_id, force=True)

    @router.errors()
    async def report_error(event: ErrorEvent) -> bool:
        logger.error("Update failed: %s", type(event.exception).__name__)
        return True

    return router


async def run_bot(
    bot: Bot,
    provisioner: Provisioner,
    allowed_user_ids: frozenset[int],
    weather: WeatherPublisher,
    invites: TemporaryInvites,
    rates=None,
) -> None:
    alerts = WeatherAlerts(bot, invites.ledger, provisioner, allowed_user_ids)
    system_messages = SystemMessageCleaner(provisioner)
    dispatcher = Dispatcher()
    dispatcher.include_router(
        build_router(
            bot, provisioner, allowed_user_ids, weather, invites, alerts
        )
    )
    worker = asyncio.create_task(weather.run(), name="weather-updates")
    cleanup = asyncio.create_task(invites.run(), name="invite-cleanup")
    alert_worker = asyncio.create_task(alerts.run(), name="weather-alerts")
    system_worker = asyncio.create_task(
        system_messages.run(), name="system-message-cleanup"
    )
    workers = [worker, cleanup, alert_worker, system_worker]
    if rates is not None and RATES_ENABLED:
        workers.append(
            asyncio.create_task(rates.run(), name="currency-updates")
        )
    try:
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
            close_bot_session=False,
            tasks_concurrency_limit=16,
        )
    finally:
        system_messages.stop.set()
        alerts.stop.set()
        weather.stop.set()
        invites.stop.set()
        if rates is not None and RATES_ENABLED:
            rates.stop.set()
        for task in workers:
            task.cancel()
        for task in workers:
            with suppress(asyncio.CancelledError):
                await task

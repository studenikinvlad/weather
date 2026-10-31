"""A small aiogram interface that reuses each city's invitation."""

import asyncio
import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from telethon import errors

from city_folders.location import Geocoder, remember_city
from city_folders.models import CITIES, PilotError
from city_folders.publisher import WeatherPublisher
from city_folders.service import Provisioner
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


def build_router(
    bot: Bot,
    provisioner: Provisioner,
    allowed_user_ids: frozenset[int],
    weather: WeatherPublisher,
    invites: TemporaryInvites,
) -> Router:
    router = Router()
    geocoder = Geocoder()
    router.message.filter(F.chat.type == ChatType.PRIVATE)

    def allowed(user_id: int) -> bool:
        return not allowed_user_ids or user_id in allowed_user_ids

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        if message.from_user is None or not allowed(message.from_user.id):
            await message.answer(
                "Сейчас бот доступен участникам тестирования."
            )
            return
        await message.answer(
            "Напиши город или отправь геометку — я найду его погодный канал.",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(
                    text="📍 Определить город", request_location=True
                )]], resize_keyboard=True,
            ),
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
            "Подтверди город:\nГеоданные: © OpenStreetMap contributors\n"
            "https://www.openstreetmap.org/copyright",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"{city.name}, {city.region or city.country}",
                    callback_data=f"city:{city.id}",
                )] for city in cities
            ]),
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
                provisioner.remember_flood(exc.seconds)
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
                        text=f"Добавить {CITIES[city_id].name}", url=url
                    )
                ]
            ]
        )
        note = (
            "\n\nВ этой ссылке только каналы выбранного города. "
            "Если ты уже добавлял другой город из нашей папки, "
            "Telegram сохранит его каналы. Чтобы оставить один город, "
            "убери прежние каналы в настройках своей папки."
        )
        if query.from_user.id == provisioner.state.account_id:
            note = (
                "\n\nТы вошёл как владелец исходной папки: "
                "у тебя в ней находятся каналы всех городов проекта. "
                "Проверь эту ссылку с другого аккаунта. "
                "Не удаляй каналы из исходной папки."
            )
        await query.message.answer(
            f"Каналы города {CITIES[city_id].name}. "
            "После добавления папку «Мой город» можно переименовать. "
            f"Ссылка временная: удаление запланировано через "
            f"{invites.ttl} секунд после начала её создания." + note,
            reply_markup=keyboard,
        )

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
) -> None:
    dispatcher = Dispatcher()
    dispatcher.include_router(
        build_router(bot, provisioner, allowed_user_ids, weather, invites)
    )
    worker = asyncio.create_task(weather.run(), name="weather-updates")
    cleanup = asyncio.create_task(invites.run(), name="invite-cleanup")
    try:
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
            close_bot_session=False,
            tasks_concurrency_limit=16,
        )
    finally:
        weather.stop.set()
        invites.stop.set()
        for task in (worker, cleanup):
            task.cancel()
        for task in (worker, cleanup):
            with suppress(asyncio.CancelledError):
                await task

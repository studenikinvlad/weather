"""Publish two durable messages per city and refresh them every 30 minutes."""

import asyncio
import logging
import time

import aiohttp
from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import LinkPreviewOptions
from telethon import errors, types, utils
from telethon.extensions import html

from city_folders.models import (
    CITIES,
    PilotError,
    WeatherMessage,
    WeatherPost,
    channel_key,
)
from city_folders.service import Provisioner
from city_folders.weather import OpenMeteo

logger = logging.getLogger(__name__)
INTERVAL = 30 * 60


class WeatherPublisher:
    """Use a shared state object; all writes happen on the same event loop."""

    def __init__(
        self, bot: Bot, service: Provisioner, source: OpenMeteo
    ) -> None:
        self.bot = bot
        self.service = service
        self.source = source
        self.lock = asyncio.Lock()
        self.stop = asyncio.Event()

    def weather_city(self, chat_id: int) -> str | None:
        for city_id in list(CITIES):
            item = self.service.state.channels.get(
                channel_key(city_id, "weather")
            )
            if item and item.channel_id is not None:
                peer = types.PeerChannel(item.channel_id)
                if utils.get_peer_id(peer) == chat_id:
                    return city_id
        return None

    async def channel_ready(self, city_id: str, kind: str) -> None:
        if kind == "weather":
            await self.update(city_id)

    async def write_message(
        self, chat_id: int, slot: WeatherMessage, text: str
    ) -> None:
        if slot.pending_text is not None and slot.message_id is None:
            # Never blindly resend an ambiguous sendMessage operation.
            matches = []
            self.service.check_cooldown()
            async for item in self.service.client.iter_messages(
                chat_id, limit=100
            ):
                if item.message == slot.pending_text:
                    matches.append(item.id)
            if len(matches) != 1:
                raise PilotError(
                    "Результат отправки неизвестен: автоматический повтор "
                    "остановлен во избежание дублей. Нужна сверка канала."
                )
            slot.message_id = matches[0]
            slot.pending_text = None
            self.service.save()
        if slot.message_id is not None:
            try:
                await self.bot.edit_message_text(
                    text=text,
                    chat_id=chat_id,
                    message_id=slot.message_id,
                    parse_mode="HTML",
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            except TelegramBadRequest as exc:
                description = exc.message.lower()
                if "message is not modified" in description:
                    return
                if "message to edit not found" in description:
                    slot.message_id = None
                    self.service.save()
                    raise PilotError(
                        "Публикация удалена; восстановлю пару при повторе."
                    ) from exc
                raise
            return
        slot.pending_text = html.parse(text)[0]
        self.service.save()
        try:
            sent = await self.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                disable_notification=True,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except (
            TelegramBadRequest,
            TelegramForbiddenError,
            TelegramRetryAfter,
        ):
            # These responses reject the send; network/5xx is ambiguous.
            slot.pending_text = None
            self.service.save()
            raise
        slot.message_id = sent.message_id
        slot.pending_text = None
        self.service.save()

    async def publish(self, city_id: str, post: WeatherPost) -> None:
        channel = self.service.state.channels[channel_key(city_id, "weather")]
        if channel.channel_id is None:
            raise PilotError("Нет ID погодного канала.")
        chat_id = utils.get_peer_id(types.PeerChannel(channel.channel_id))
        forecast = await self.source.fetch(city_id)
        weekly, brief = forecast.messages(city_id)
        # If the upper message was deleted, recreate our pair in order.
        if (
            post.weekly.message_id is None
            and post.weekly.pending_text is None
            and post.brief.message_id is not None
        ):
            try:
                await self.bot.delete_message(chat_id, post.brief.message_id)
            except TelegramBadRequest as exc:
                if "message to delete not found" not in exc.message.lower():
                    raise
            post.brief = WeatherMessage()
            post.pinned = False
            self.service.save()
        await self.write_message(chat_id, post.weekly, weekly)
        await self.write_message(chat_id, post.brief, brief)

    async def update(self, city_id: str, *, force: bool = False) -> bool:
        async with self.lock:
            channel = self.service.state.channels.get(
                channel_key(city_id, "weather")
            )
            if channel is None or not channel.ready:
                return False
            post = self.service.state.weather.setdefault(
                city_id, WeatherPost()
            )
            if not force and post.next_update > time.time():
                return True
            try:
                await self.publish(city_id, post)
            except Exception as exc:
                # Cancellation inherits BaseException and propagates normally.
                post.failures += 1
                delay = min(60 * 2 ** min(post.failures - 1, 5), INTERVAL)
                if isinstance(exc, TelegramRetryAfter):
                    delay = max(delay, exc.retry_after + 1)
                if isinstance(exc, errors.FloodWaitError):
                    self.service.remember_flood(exc.seconds)
                    delay = max(delay, exc.seconds + 1)
                post.next_update = time.time() + delay
                self.service.save()
                detail = str(exc) if isinstance(exc, PilotError) else ""
                logger.error(
                    "%s: погода не обновлена (%s). Повтор через %s с. %s",
                    city_id,
                    type(exc).__name__,
                    delay,
                    detail,
                )
                return False
            post.failures = 0
            post.last_success = time.time()
            post.next_update = post.last_success + INTERVAL
            self.service.save()
            logger.info("%s: прогноз обновлён", city_id)
            return True

    async def run(self) -> None:
        """A restart honours saved deadlines and fills empty channels now."""
        while not self.stop.is_set():
            for city_id in list(CITIES):
                if self.stop.is_set():
                    return
                await self.update(city_id)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=5)
            except TimeoutError:
                pass


def http_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))

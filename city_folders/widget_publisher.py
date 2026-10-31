"""Silent replacement of weather widgets with recoverable sends."""

import asyncio
import logging
import time
from contextlib import suppress
from dataclasses import asdict
from datetime import datetime

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import BufferedInputFile
from telethon import errors, functions, types

from city_folders.artwork import artwork_key
from city_folders.models import CITIES, PilotError, WeatherMessage, channel_key
from city_folders.publisher import WeatherPublisher
from city_folders.weather import Forecast
from city_folders.weather_layout import required_artwork, rich_forecasts

logger = logging.getLogger(__name__)
MESSAGE_DELAY = 1.0
LAYOUT_VERSION = 7


def contains_custom_emoji(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if isinstance(value, dict):
        return value.get("type") == "custom_emoji" or any(
            contains_custom_emoji(v) for v in value.values()
        )
    if isinstance(value, list):
        return any(contains_custom_emoji(v) for v in value)
    return False


class WidgetPublisher(WeatherPublisher):
    def __init__(self, bot, service, source, artwork, username):
        super().__init__(bot, service, source)
        self.artwork = artwork
        self.username = username
        self.emoji_retry_at = 0
        self.reconciled_cities = set()
        self.avatar_lock = asyncio.Lock()

    async def channel_ready(self, city_id, kind):
        # Asset uploads must not hold the folder provisioning lock or delay
        # a user's invitation. The publisher loop picks this city up.
        if kind == "weather":
            from city_folders.models import WeatherPost

            post = self.service.state.weather.setdefault(
                city_id, WeatherPost()
            )
            if not post.last_success:
                post.next_update = 0
                self.service.save()

    async def clear_old(self, city_id, post):
        channel = self.service.state.channels[channel_key(city_id, "weather")]
        if not post.delete_ids:
            return
        # Keep valid current/in-flight messages even if an old queue overlaps.
        keep = set(post.active_ids + post.staged_ids)
        post.delete_ids = list(
            dict.fromkeys(mid for mid in post.delete_ids if mid not in keep)
        )
        self.service.save()
        while post.delete_ids:
            batch = post.delete_ids[:100]
            await self.service.rpc(
                functions.channels.DeleteMessagesRequest(
                    channel=types.InputChannel(
                        channel.channel_id, channel.access_hash
                    ),
                    id=batch,
                )
            )
            # Persist each completed batch; failures retain remaining work.
            post.delete_ids = post.delete_ids[len(batch) :]
            self.service.save()

    async def reconcile_old(self, city_id, post):
        if city_id in self.reconciled_cities or not post.active_ids:
            return
        from city_folders.weather_cleanup import is_weather_post

        channel = self.service.state.channels[channel_key(city_id, "weather")]
        await self.service.ensure_connection()
        obsolete = []
        async for message in self.service.client.iter_messages(
            types.InputPeerChannel(channel.channel_id, channel.access_hash),
            limit=None,
            max_id=min(post.active_ids),
        ):
            if (
                message.id >= min(post.active_ids)
                or message.id in post.staged_ids
                or not is_weather_post(message)
            ):
                continue
            obsolete.append(message.id)
        post.delete_ids = list(dict.fromkeys(post.delete_ids + obsolete))
        self.service.save()
        await self.clear_old(city_id, post)
        self.reconciled_cities.add(city_id)

    async def recover(self, chat_id, post):
        if post.pending_kind is None:
            return
        await self.service.ensure_connection()
        matches = []
        async for msg in self.service.client.iter_messages(
            chat_id, limit=100, min_id=post.pending_after
        ):
            if msg.action:
                continue
            # This bot owns the managed channel's weather stream. Never
            # blindly replay a send with an unknown result.
            if post.pending_kind == "sticker":
                # Layout 3 sent the upload ID, which may be a WebP document.
                document = getattr(msg, "document", None)
                legacy_upload = (
                    post.staged_layout_version < 4
                    and document is not None
                    and document.mime_type == "image/webp"
                    and any(
                        isinstance(a, types.DocumentAttributeFilename)
                        and a.file_name == "weather.webp"
                        for a in document.attributes
                    )
                )
                if msg.sticker or legacy_upload:
                    matches.append(msg.id)
            elif post.pending_kind in {"hourly", "daily"} and msg.rich_message:
                marker = post.snapshot["local_time"]
                stamp = datetime.fromisoformat(marker)
                readable = f"🕒 Погода на {stamp:%H:%M} · {stamp:%d.%m.%Y}"
                content = str(msg.rich_message.to_dict())
                # A persisted latest-message cursor bounds this single-writer
                # stream. Require exactly one matching slot after that cursor.
                heading = (
                    "Следующие 20 часов"
                    if post.pending_kind == "hourly"
                    else "11 дней"
                )
                if (
                    marker in content
                    or readable in content
                    or heading in content
                ):
                    matches.append(msg.id)
            elif (
                post.pending_kind in {"brief", "current", "daily"}
                and msg.message
            ):
                if msg.message == post.snapshot.get(
                    post.pending_kind + "_plain"
                ):
                    matches.append(msg.id)
        if len(matches) != 1:
            raise PilotError(
                "Проверка незавершённой публикации: "
                "повтор остановлен, чтобы не создать дубликаты."
            )
        post.staged_ids.append(matches[0])
        post.pending_kind = None
        self.service.save()

    async def send_slot(self, chat_id, post, kind, send):
        await self.service.ensure_connection()
        latest = await self.service.client.get_messages(chat_id, limit=1)
        post.pending_after = latest[0].id if latest else 0
        post.pending_kind = kind
        self.service.save()
        try:
            msg = await send()
        except (
            TelegramBadRequest,
            TelegramRetryAfter,
            TelegramForbiddenError,
        ):
            post.pending_kind = None
            self.service.save()
            raise
        post.staged_ids.append(msg.message_id)
        post.pending_kind = None
        self.service.save()
        return msg

    async def avatar(self, city_id, forecast, post):
        async with self.avatar_lock:
            await self._avatar(city_id, forecast, post)

    async def _avatar(self, city_id, forecast, post):
        key = artwork_key(
            forecast.current.get("weather_code"),
            forecast.current.get("is_day"),
            forecast.current.get("temperature_2m"),
        )
        now = time.time()
        if key == post.avatar_key or now < post.avatar_retry_at:
            return
        setting = "bot_avatar_next_at"
        row = self.artwork.db.execute(
            "SELECT value FROM artwork_settings WHERE key=?",
            (setting,),
        ).fetchone()
        if row and row[0] > now:
            return

        # Global pacing for this bot; user-session FloodWait is independent.
        def delay(seconds):
            with self.artwork.db:
                self.artwork.db.execute(
                    "INSERT OR REPLACE INTO artwork_settings VALUES (?,?)",
                    (setting, time.time() + seconds),
                )

        delay(1.0)
        channel = self.service.state.channels[channel_key(city_id, "weather")]
        chat_id = -1000000000000 - channel.channel_id
        photo = self.artwork.image(key, "avatar") if key else None
        try:
            if photo is None:
                await self.bot.delete_chat_photo(chat_id=chat_id)
            else:
                await self.bot.set_chat_photo(
                    chat_id=chat_id,
                    photo=BufferedInputFile(photo, "weather.png"),
                )
        except TelegramRetryAfter as exc:
            delay(exc.retry_after + 1)
            post.avatar_retry_at = time.time() + exc.retry_after + 1
            self.service.save()
            raise
        post.avatar_key = key if photo is not None else None
        post.avatar_updated_at = time.time()
        post.avatar_retry_at = 0
        self.service.save()

    async def run_avatars(self):
        """Drain latest forecasts independently of slow message publication."""
        while not self.stop.is_set():
            posts = sorted(
                list(self.service.state.weather.items()),
                key=lambda item: item[1].avatar_updated_at,
            )
            for city, post in posts:
                if self.stop.is_set():
                    return
                snapshot = post.snapshot or post.last_forecast
                if not snapshot:
                    continue
                payload = dict(snapshot)
                for field in ("brief_plain", "current_plain", "daily_plain"):
                    payload.pop(field, None)
                payload["local_time"] = datetime.fromisoformat(
                    payload["local_time"]
                )
                forecast = Forecast(**payload)
                if (
                    abs(time.time() - forecast.local_time.timestamp())
                    > 3 * 3600
                ):
                    continue
                try:
                    await self.avatar(city, forecast, post)
                except Exception as exc:
                    post.avatar_retry_at = max(
                        post.avatar_retry_at, time.time() + 60
                    )
                    self.service.save()
                    logger.warning(
                        "Bot avatar %s: %s", city, type(exc).__name__
                    )
            await asyncio.sleep(0.5)

    async def publish(self, city_id, post):
        channel = self.service.state.channels[channel_key(city_id, "weather")]
        chat_id = -1000000000000 - channel.channel_id
        await self.clear_old(city_id, post)
        if post.snapshot:
            payload = dict(post.snapshot)
            payload.pop("brief_plain", None)
            payload.pop("current_plain", None)
            payload.pop("daily_plain", None)
            payload["local_time"] = datetime.fromisoformat(
                payload["local_time"]
            )
            forecast = Forecast(**payload)
        else:
            forecast = await self.source.fetch(city_id)
            if abs(time.time() - forecast.local_time.timestamp()) > 3 * 3600:
                raise PilotError("Источник вернул устаревшую погоду.")
            post.snapshot = asdict(forecast)
            post.snapshot["local_time"] = forecast.local_time.isoformat()
            self.service.save()
        await self.recover(chat_id, post)
        if abs(time.time() - forecast.local_time.timestamp()) > 3 * 3600:
            post.delete_ids.extend(post.staged_ids)
            post.staged_ids = []
            post.snapshot = {}
            post.staged_layout_version = 0
            post.staged_sticker_key = None
            self.service.save()
            raise PilotError(
                "Отложенный прогноз устарел; запрошу свежие данные."
            )
        if post.staged_layout_version != LAYOUT_VERSION:
            # Reconcile old in-flight sends first, then replace the old layout.
            post.delete_ids.extend(post.staged_ids)
            post.staged_ids = []
            key = artwork_key(
                forecast.current.get("weather_code"),
                forecast.current.get("is_day"),
                forecast.current.get("temperature_2m"),
            )
            post.staged_sticker_key = (
                key if key and self.artwork.image(key, "sticker") else None
            )
            post.staged_layout_version = LAYOUT_VERSION
            self.service.save()
        emoji = self.artwork.request(self.bot.id, required_artwork(forecast))
        hourly, daily = rich_forecasts(forecast, emoji)
        # Keep exact plain text to reconcile uncertain sends.
        brief = forecast.brief_text(city_id)
        from telethon.extensions import html

        post.snapshot["brief_plain"] = html.parse(brief)[0]
        post.snapshot["current_plain"] = html.parse(
            forecast.current_text(include_heading=False)
        )[0]
        post.snapshot["daily_plain"] = daily["text"]
        self.service.save()
        calls = [
            (
                "current",
                lambda: self.bot.send_message(
                    chat_id=chat_id,
                    text=post.snapshot["current_plain"],
                    disable_notification=True,
                ),
            ),
            (
                "hourly",
                lambda: self.bot.send_rich_message(
                    chat_id=chat_id,
                    rich_message=hourly,
                    disable_notification=True,
                ),
            ),
            (
                "daily",
                lambda: self.bot.send_message(
                    chat_id=chat_id,
                    **daily,
                    parse_mode=None,
                    disable_notification=True,
                ),
            ),
            (
                "brief",
                lambda: self.bot.send_message(
                    chat_id=chat_id,
                    text=brief,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    disable_notification=True,
                ),
            ),
        ]
        if not post.snapshot["current_plain"]:
            calls = [(kind, call) for kind, call in calls if kind != "current"]
        if post.staged_sticker_key:
            # Prepare the file before recording a message-send intent.
            # An upload failure must not look like an uncertain channel post.
            sticker = await self.artwork.sticker(
                self.bot,
                self.service.state.account_id,
                post.staged_sticker_key,
                self.username,
            )
            calls.insert(
                0,
                (
                    "sticker",
                    lambda: self.bot.send_sticker(
                        chat_id=chat_id,
                        sticker=sticker,
                        disable_notification=True,
                    ),
                ),
            )
        if not channel.branding_version:
            try:
                await self.bot.set_chat_title(
                    chat_id=chat_id,
                    title=f"{CITIES[city_id].name} · snowaa",
                )
            except TelegramBadRequest as exc:
                if "not modified" not in exc.message.lower():
                    raise
            channel.branding_version = 1
            self.service.save()
        if channel.branding_version < 2:
            try:
                await self.bot.set_chat_description(
                    chat_id=chat_id,
                    description=(
                        "snowaa · погода каждый час. "
                        "В прогнозах указано местное время города.\n"
                        "Погодные данные: Open-Meteo "
                        "https://open-meteo.com/ · CC BY 4.0\n"
                        "https://creativecommons.org/licenses/by/4.0/\n"
                        "Данные округлены и оформлены snowaa."
                    ),
                )
            except TelegramBadRequest as exc:
                if "not modified" not in exc.message.lower():
                    raise
            channel.branding_version = 2
            self.service.save()
        # Photo event precedes the new brief, preserving the chat-list preview.
        try:
            await self.avatar(city_id, forecast, post)
        except Exception as exc:
            delay = (
                exc.seconds + 1
                if isinstance(exc, errors.FloodWaitError)
                else 60
            )
            post.avatar_retry_at = max(
                post.avatar_retry_at, time.time() + delay
            )
            self.service.save()
            logger.warning("Avatar %s: %s", city_id, type(exc).__name__)

        async def repair_stripped():
            fallback, _ = rich_forecasts(forecast, {})
            index = next(
                i for i, (kind, _) in enumerate(calls) if kind == "hourly"
            )
            if len(post.staged_ids) <= index:
                return
            try:
                await self.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=post.staged_ids[index],
                    rich_message=fallback,
                )
            except TelegramBadRequest as exc:
                if "message is not modified" not in exc.message.lower():
                    raise

        if not self.artwork.enabled():
            await repair_stripped()
        for kind, call in calls[len(post.staged_ids) :]:
            if post.staged_ids:
                await asyncio.sleep(MESSAGE_DELAY)
            msg = await self.send_slot(chat_id, post, kind, call)
            if kind == "hourly" and emoji:
                if not contains_custom_emoji(msg.rich_message):
                    self.artwork.set_enabled(False)
                    logger.warning(
                        "Custom emoji removed by Telegram; using temperatures"
                    )
                if not self.artwork.enabled():
                    await repair_stripped()
        old = post.active_ids + [
            post.weekly.message_id,
            post.brief.message_id,
        ]
        post.delete_ids.extend(x for x in old if x)
        post.active_ids = post.staged_ids.copy()
        post.staged_ids = []
        post.last_forecast = post.snapshot.copy()
        post.snapshot = {}
        post.weekly = WeatherMessage()
        post.brief = WeatherMessage()
        post.layout_version = LAYOUT_VERSION
        post.staged_layout_version = 0
        post.staged_sticker_key = None
        post.generation += 1
        self.service.save()
        try:
            await self.clear_old(city_id, post)
            await self.reconcile_old(city_id, post)
        except Exception as exc:
            logger.warning(
                "Old messages await cleanup: %s", type(exc).__name__
            )

    async def update(self, city_id, *, force=False):
        post = self.service.state.weather.get(city_id)
        if post and post.layout_version < LAYOUT_VERSION and not post.failures:
            force = True
        if post and not force and post.next_update > time.time():
            async with self.lock:
                try:
                    await self.clear_old(city_id, post)
                    await self.reconcile_old(city_id, post)
                except Exception as exc:
                    logger.warning("Weather cleanup: %s", type(exc).__name__)
            if post.last_forecast:
                async with self.lock:
                    payload = dict(post.last_forecast)
                    payload.pop("brief_plain", None)
                    payload.pop("current_plain", None)
                    payload.pop("daily_plain", None)
                    payload["local_time"] = datetime.fromisoformat(
                        payload["local_time"]
                    )
                    forecast = Forecast(**payload)
                    if (
                        abs(time.time() - forecast.local_time.timestamp())
                        <= 3 * 3600
                    ):
                        try:
                            await self.avatar(city_id, forecast, post)
                        except Exception as exc:
                            delay = (
                                exc.seconds + 1
                                if isinstance(exc, errors.FloodWaitError)
                                else 60
                            )
                            post.avatar_retry_at = max(
                                post.avatar_retry_at, time.time() + delay
                            )
                            self.service.save()
                            logger.warning(
                                "Deferred artwork: %s", type(exc).__name__
                            )
            return True
        result = await super().update(city_id, force=force)
        if result and (post := self.service.state.weather.get(city_id)):
            post.next_update = max(post.next_update, post.last_success + 3600)
            self.service.save()
        return result

    async def run(self):
        worker = asyncio.create_task(
            self.artwork.run(
                self.bot, self.service.state.account_id, self.username
            )
        )
        avatars = asyncio.create_task(self.run_avatars())
        try:
            await super().run()
        finally:
            worker.cancel()
            avatars.cancel()
            for task in (worker, avatars):
                with suppress(asyncio.CancelledError):
                    await task

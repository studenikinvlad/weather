"""Opt-in private weather warnings with a durable deletion queue."""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

from city_folders.models import CITIES
from city_folders.weather import Forecast, number

logger = logging.getLogger(__name__)
TTL = 3600
COOLDOWN = 3 * 3600


def weather_kind(code):
    if code in (95, 96, 99):
        return "storm"
    if code in (71, 73, 75, 77, 85, 86):
        return "snow"
    if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        return "rain"
    if code in (0, 1, 2, 3, 45, 48):
        return "dry"
    return None


@dataclass(frozen=True)
class Warning:
    kind: str
    onset: float
    text: str


def upcoming_warning(city, forecast, now):
    """Only fresh, known transitions; missing fields are not weather."""
    age = now - forecast.local_time.timestamp()
    if not 0 <= age <= 90 * 60:
        return None
    previous = weather_kind(forecast.current.get("weather_code"))
    previous_temp = number(forecast.current.get("temperature_2m"))
    previous_time = forecast.local_time.timestamp()
    for hour in forecast.next_hours():
        stamp = hour["time"].timestamp()
        kind = weather_kind(hour.get("weather_code"))
        temp = number(hour.get("temperature_2m"))
        lead = stamp - now
        if 15 * 60 <= lead <= 75 * 60:
            chance = number(hour.get("precipitation_probability"))
            # Thunderstorms matter even when rain is already ongoing.
            wet = previous == "dry" and kind in {"rain", "snow", "storm"}
            storm = kind == "storm" and previous not in {None, "storm"}
            if (wet or storm) and chance is not None and chance >= 60:
                label = {
                    "rain": "🌧 Возможен дождь",
                    "snow": "❄️ Возможен снег",
                    "storm": "⛈ Возможна гроза",
                }[kind]
                return Warning(
                    kind,
                    stamp,
                    f"{city.name}: {label}\n"
                    f"Около {hour['time']:%H:%M} по местному времени "
                    f"· {round(chance)}%",
                )
            if (
                temp is not None
                and previous_temp is not None
                and 0 < stamp - previous_time <= 3600
                and abs(temp - previous_temp) >= 5
            ):
                direction = (
                    "Похолодание" if temp < previous_temp else "Потепление"
                )
                return Warning(
                    "temperature",
                    stamp,
                    f"{city.name}: {direction}\n"
                    f"До {round(temp):+d}° около {hour['time']:%H:%M} "
                    "по местному времени (изменение ≥5° за час).",
                )
        previous, previous_temp, previous_time = kind, temp, stamp
    return None


class WeatherAlerts:
    def __init__(self, bot, ledger, service, allowed_user_ids=frozenset()):
        self.bot = bot
        self.ledger = ledger
        self.db = ledger.db
        self.service = service
        self.allowed_user_ids = allowed_user_ids
        self.stop = asyncio.Event()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS weather_alert_runtime (
                key TEXT PRIMARY KEY, value REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS weather_alert_preferences (
                user_id INTEGER NOT NULL, city_id TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(user_id, city_id)
            );
            CREATE TABLE IF NOT EXISTS weather_alert_delivery (
                user_id INTEGER NOT NULL, city_id TEXT NOT NULL,
                kind TEXT NOT NULL, onset REAL NOT NULL,
                status TEXT NOT NULL, created_at REAL NOT NULL,
                message_id INTEGER, delete_at REAL NOT NULL,
                retry_at REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(user_id, city_id, kind, onset)
            );
        """)

    def enabled(self, uid, city):
        row = self.db.execute(
            "SELECT enabled FROM weather_alert_preferences "
            "WHERE user_id=? AND city_id=?",
            (uid, city),
        ).fetchone()
        return bool(row and row[0])

    def set_enabled(self, uid, city, value):
        if city not in self.ledger.cities_for(uid):
            return False
        with self.db:
            self.db.execute(
                "INSERT INTO weather_alert_preferences VALUES (?,?,?) "
                "ON CONFLICT(user_id,city_id) DO UPDATE SET "
                "enabled=excluded.enabled",
                (uid, city, int(value)),
            )
            if not value:
                self.db.execute(
                    "UPDATE weather_alert_delivery SET delete_at=0,retry_at=0 "
                    "WHERE user_id=? AND city_id=? AND status='sent'",
                    (uid, city),
                )
        return True

    async def cleanup(self, now):
        rows = self.db.execute(
            "SELECT * FROM weather_alert_delivery WHERE status='sent' "
            "AND retry_at<=? AND (delete_at<=? OR NOT EXISTS "
            "(SELECT 1 FROM user_cities c WHERE "
            "c.user_id=weather_alert_delivery.user_id "
            "AND c.city_id=weather_alert_delivery.city_id) OR NOT EXISTS "
            "(SELECT 1 FROM weather_alert_preferences p "
            "WHERE p.user_id=weather_alert_delivery.user_id "
            "AND p.city_id=weather_alert_delivery.city_id AND p.enabled=1)) "
            "LIMIT 50",
            (now, now),
        ).fetchall()
        for row in rows:
            key = (row["user_id"], row["city_id"], row["kind"], row["onset"])
            status, retry = "deleted", 0
            try:
                await self.bot.delete_message(
                    chat_id=row["user_id"], message_id=row["message_id"]
                )
            except TelegramBadRequest as exc:
                if "message to delete not found" not in exc.message.lower():
                    status = "delete_failed"
                    logger.warning(
                        "Alert deletion rejected: %s", type(exc).__name__
                    )
            except TelegramForbiddenError:
                status = "delete_failed"
            except Exception as exc:
                status = "sent"
                retry = now + (
                    exc.retry_after + 1
                    if isinstance(exc, TelegramRetryAfter)
                    else 60
                )
            with self.db:
                self.db.execute(
                    "UPDATE weather_alert_delivery SET status=?,retry_at=? "
                    "WHERE user_id=? AND city_id=? AND kind=? AND onset=?",
                    (status, retry, *key),
                )
        with self.db:
            # Keep unresolved delivery/deletion intents for diagnosis.
            self.db.execute(
                "DELETE FROM weather_alert_delivery WHERE status IN "
                "('deleted','rejected') "
                "AND created_at<?",
                (now - 7 * 86400,),
            )

    async def tick(self):
        now = time.time()
        await self.cleanup(now)
        cooldown = self.db.execute(
            "SELECT value FROM weather_alert_runtime WHERE key='send_after'"
        ).fetchone()
        if cooldown and cooldown[0] > now:
            return
        rows = self.db.execute(
            "SELECT p.user_id,p.city_id FROM weather_alert_preferences p "
            "JOIN user_cities c ON c.user_id=p.user_id AND "
            "c.city_id=p.city_id "
            "WHERE p.enabled=1"
        ).fetchall()
        for row in rows:
            uid, city_id = row
            if self.allowed_user_ids and uid not in self.allowed_user_ids:
                continue
            post = self.service.state.weather.get(city_id)
            if not post or not post.last_forecast or city_id not in CITIES:
                continue
            payload = dict(post.last_forecast)
            payload.pop("brief_plain", None)
            payload.pop("current_plain", None)
            payload.pop("daily_plain", None)
            try:
                payload["local_time"] = datetime.fromisoformat(
                    payload["local_time"]
                )
                warning = upcoming_warning(
                    CITIES[city_id], Forecast(**payload), now
                )
            except (ValueError, TypeError, KeyError):
                logger.warning("Invalid alert forecast for %s", city_id)
                continue
            if warning is None:
                continue
            recent = self.db.execute(
                "SELECT 1 FROM weather_alert_delivery WHERE user_id=? "
                "AND ((city_id=? AND created_at>?) OR created_at>?) "
                "AND status != 'rejected' LIMIT 1",
                (uid, city_id, now - COOLDOWN, now - 60),
            ).fetchone()
            if recent:
                continue
            key = (uid, city_id, warning.kind, warning.onset)
            with self.db:
                inserted = self.db.execute(
                    "INSERT OR IGNORE INTO weather_alert_delivery "
                    "(user_id,city_id,kind,onset,status,created_at,delete_at) "
                    "VALUES (?,?,?,?,'pending',?,?)",
                    (*key, now, now + TTL),
                ).rowcount
            if not inserted:
                continue
            # Persist intent BEFORE sending: ambiguous network outcomes must
            # not create repeated push notifications after restart.
            try:
                message = await self.bot.send_message(
                    chat_id=uid,
                    text=warning.text + "\n\nИсчезнет через час. /settings",
                    disable_notification=False,
                )
            except TelegramRetryAfter as exc:
                with self.db:
                    self.db.execute(
                        "DELETE FROM weather_alert_delivery WHERE user_id=? "
                        "AND city_id=? "
                        "AND kind=? AND onset=?",
                        key,
                    )
                    self.db.execute(
                        "INSERT OR REPLACE INTO weather_alert_runtime "
                        "VALUES (?,?)",
                        ("send_after", time.time() + exc.retry_after + 1),
                    )
                # Persist the rate limit without delaying the deletion worker.
                return
            except (TelegramForbiddenError, TelegramBadRequest) as exc:
                with self.db:
                    self.db.execute(
                        "UPDATE weather_alert_delivery SET status='rejected' "
                        "WHERE user_id=? AND city_id=? AND kind=? AND onset=?",
                        key,
                    )
                    if isinstance(exc, TelegramForbiddenError):
                        self.db.execute(
                            "UPDATE weather_alert_preferences SET enabled=0 "
                            "WHERE user_id=?",
                            (uid,),
                        )
            except Exception as exc:
                logger.warning(
                    "Alert delivery uncertain; no blind retry: %s",
                    type(exc).__name__,
                )
            else:
                with self.db:
                    self.db.execute(
                        "UPDATE weather_alert_delivery SET "
                        "status='sent',message_id=? "
                        "WHERE user_id=? AND city_id=? AND kind=? AND onset=?",
                        (message.message_id, *key),
                    )
            await asyncio.sleep(0.05)

    async def run(self):
        while not self.stop.is_set():
            try:
                await self.tick()
            except Exception as exc:
                logger.warning("Weather alert worker: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=30)
            except TimeoutError:
                pass

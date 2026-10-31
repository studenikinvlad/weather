"""Open-Meteo forecasts and Telegram-safe, compact Russian formatting."""

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from html import escape
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp

from city_folders.models import CITIES, PilotError

# Fixed city centres; a user's location never changes a shared forecast.
LOCATIONS = {
    "kazan": (55.7961, 49.1064, "Europe/Moscow"),
    "samara": (53.1959, 50.1002, "Europe/Samara"),
    "khabarovsk": (48.4827, 135.0838, "Asia/Vladivostok"),
}
WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
CURRENT = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,"
    "weather_code,wind_speed_10m,wind_gusts_10m,surface_pressure,is_day"
)
DAILY = (
    "temperature_2m_min,temperature_2m_max,weather_code,"
    "precipitation_probability_max,precipitation_sum,wind_speed_10m_max"
)


def number(value: Any) -> float | None:
    """Missing and invalid readings must not become invented zeroes."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def formatted(value: Any, *, signed: bool = False) -> str:
    value = number(value)
    if value is None:
        return "—"
    rounded = round(value)
    return f"{rounded:+d}" if signed else str(rounded)


def condition(code: Any, is_day: Any = 1) -> tuple[str, str]:
    if code == 0:
        return ("☀️" if is_day else "🌙", "Ясно")
    if code in (1, 2):
        return "🌤", "Переменная облачность"
    if code == 3:
        return "☁️", "Пасмурно"
    if code in (45, 48):
        return "🌫", "Туман"
    if code in (51, 53, 55):
        return "🌦", "Морось"
    if code in (56, 57, 66, 67):
        return "🧊", "Ледяные осадки"
    if code in (61, 63, 65):
        return "🌧", "Дождь"
    if code in (71, 73, 75, 77):
        return "❄️", "Снег"
    if code in (80, 81, 82):
        return "🌧", "Ливень"
    if code in (85, 86):
        return "🌨", "Снежные заряды"
    if code in (95, 96, 99):
        return "⛈", "Гроза"
    return "🌡", "Нет данных о состоянии"


@dataclass(frozen=True, slots=True)
class Forecast:
    current: dict[str, Any]
    daily: dict[str, Any]
    local_time: datetime
    timezone_name: str
    hourly: dict[str, Any] = field(default_factory=dict)

    def time_label(self):
        offset = self.local_time.strftime("%z")
        zone = f"UTC{offset[:3]}:{offset[3:]}"
        return (
            f"🕒 Погода на {self.local_time:%H:%M} · "
            f"{self.local_time:%d.%m.%Y}\n"
            f"Местное время · {zone}"
        )

    def day_label(self, day):
        delta = (day - self.local_time.date()).days
        if delta == 0:
            return "Сегодня"
        if delta == 1:
            return "Завтра"
        return f"{day:%d.%m} · {WEEKDAYS[day.weekday()]}"

    @classmethod
    def parse(cls, data: Any) -> "Forecast":
        try:
            current = dict(data["current"])
            daily = dict(data["daily"])
            offset = int(data["utc_offset_seconds"])
            fallback_zone = timezone(timedelta(seconds=offset))
            try:
                zone = ZoneInfo(str(data["timezone"]))
            except ZoneInfoNotFoundError:
                zone = fallback_zone
            stamp = current["time"]
            if isinstance(stamp, (int, float)):
                local = datetime.fromtimestamp(stamp, zone)
                current["time"] = local.isoformat()
            else:
                local = datetime.fromisoformat(stamp)
                if local.tzinfo is None:
                    local = local.replace(tzinfo=fallback_zone)
                local = local.astimezone(zone)
            # Open-Meteo daily epochs use the response offset for dates.
            daily["time"] = [
                datetime.fromtimestamp(d, fallback_zone).date().isoformat()
                if isinstance(d, (int, float))
                else d
                for d in daily["time"]
            ]
            days = daily["time"]
            if len(days) != 11:
                raise ValueError("Expected eleven days")
            for day in days:
                date.fromisoformat(day)
            for key in DAILY.split(","):
                if len(daily[key]) != 11:
                    raise ValueError("Incomplete daily values")
            if number(current["temperature_2m"]) is None:
                raise ValueError("Missing current temperature")
            hourly = dict(data["hourly"])
            hourly["time"] = [
                datetime.fromtimestamp(t, zone).isoformat()
                if isinstance(t, (int, float))
                else t
                for t in hourly["time"]
            ]
            times = hourly["time"]
            for key in (
                "temperature_2m",
                "weather_code",
                "is_day",
                "precipitation_probability",
            ):
                if len(hourly[key]) != len(times):
                    raise ValueError("Incomplete hourly values")
            forecast = cls(
                current, daily, local, str(data["timezone"]), hourly
            )
            if len(forecast.next_hours()) != 20:
                raise ValueError("Expected twenty future hours")
            return forecast
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise PilotError("Погодный API вернул неполные данные.") from exc

    def next_hours(self):
        now = self.local_time.timestamp()
        result = []
        for i, stamp in enumerate(self.hourly.get("time", [])):
            when = datetime.fromisoformat(stamp)
            if when.tzinfo is None:
                # Compatibility with ISO snapshots written before epoch input.
                when = when.replace(tzinfo=self.local_time.tzinfo)
            if when.timestamp() <= now:
                continue
            result.append(
                {
                    "time": when,
                    **{
                        key: values[i]
                        for key, values in self.hourly.items()
                        if key != "time"
                    },
                }
            )
            if len(result) == 20:
                break
        return result

    def summary(self):
        hours = self.next_hours()
        if len(hours) < 20 or any(
            condition(h.get("weather_code"))[0] == "🌡" for h in hours
        ):
            return "Прогноз осадков уточняется"
        for hour in hours:
            code = hour.get("weather_code")
            if code not in (0, 1, 2, 3, 45, 48):
                _, label = condition(code)
                day = (
                    "завтра "
                    if hour["time"].date() != self.local_time.date()
                    else ""
                )
                chance = number(hour.get("precipitation_probability"))
                probability = (
                    f" · {round(chance)}%" if chance is not None else ""
                )
                return (
                    f"{label.lower()} {day}с {hour['time']:%H:%M}{probability}"
                )
        return "20 ч без осадков"

    def current_heading(self):
        icon, label = condition(
            self.current.get("weather_code"), self.current.get("is_day", 1)
        )
        temperature = formatted(
            self.current.get("temperature_2m"), signed=True
        )
        return f"{icon} <b>{temperature}° · {label}</b>"

    def current_text(self, *, include_heading=True):
        rows = [self.current_heading()] if include_heading else []
        feels = number(self.current.get("apparent_temperature"))
        if feels is not None and include_heading:
            rows.append(f"Ощущается как {formatted(feels, signed=True)}°")
        wind = number(self.current.get("wind_speed_10m"))
        gusts = number(self.current.get("wind_gusts_10m"))
        wind_parts = []
        if wind is not None:
            wind_parts.append(f"{formatted(wind)} м/с")
        if gusts is not None:
            wind_parts.append(f"порывы {formatted(gusts)} м/с")
        if wind_parts:
            rows.append("💨 " + " · ".join(wind_parts))
        humidity = number(self.current.get("relative_humidity_2m"))
        if humidity is not None:
            rows.append(f"💧 {formatted(humidity)}%")
        pressure = number(self.current.get("surface_pressure"))
        if pressure is not None:
            rows.append(f"Давление {round(pressure * 0.750062)} мм рт. ст.")
        return "\n".join(rows)

    def simple_summary(self):
        dry_codes = (0, 1, 2, 3, 45, 48)
        current_code = self.current.get("weather_code")
        icon, label = condition(current_code)
        if current_code not in dry_codes and icon != "🌡":
            return f"Сейчас {label.lower()}"
        hours = self.next_hours()
        for hour in hours:
            code = hour.get("weather_code")
            icon, label = condition(code)
            if icon == "🌡":
                return "Прогноз осадков уточняется"
            if code not in dry_codes:
                seconds = (
                    hour["time"].timestamp() - self.local_time.timestamp()
                )
                if seconds < 3600:
                    return f"{label} в течение часа"
                count = math.ceil(seconds / 3600)
                word = (
                    "час" if count == 1 else "часа" if count < 5 else "часов"
                )
                return f"{label} через {count} {word}"
        return (
            "В ближайшие 20 часов без осадков"
            if len(hours) == 20
            else "Прогноз осадков уточняется"
        )

    def brief_text(self, city_id):
        feels = formatted(self.current.get("apparent_temperature"))
        return f"Ощущается как {feels}°\n{self.simple_summary()}"

    def preview_text(self):
        """Plain summary also used by rich-message chat-list fallbacks."""
        return self.brief_text(None).replace("<b>", "").replace("</b>", "")

    def messages(self, city_id: str) -> tuple[str, str]:
        rows = [f"<b>{escape(CITIES[city_id].name)} · 11 дней</b>"]
        for i, value in enumerate(self.daily["time"]):
            day = date.fromisoformat(value)
            low = formatted(self.daily["temperature_2m_min"][i], signed=True)
            high = formatted(self.daily["temperature_2m_max"][i], signed=True)
            icon, _ = condition(self.daily["weather_code"][i])
            rows.append(
                f"{icon} {high}° / {low}° · {day:%d.%m} · "
                f"{WEEKDAYS[day.weekday()]}"
            )
        return "\n".join(rows), self.current_text()


class OpenMeteo:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session

    async def fetch(self, city_id: str) -> Forecast:
        lat, lon, zone = LOCATIONS[city_id]
        params = {
            "latitude": str(lat),
            "longitude": str(lon),
            "timezone": zone,
            "forecast_days": "11",
            "timeformat": "unixtime",
            "hourly": "temperature_2m,weather_code,is_day,"
            "precipitation_probability",
            "current": CURRENT,
            "daily": DAILY,
            "wind_speed_unit": "ms",
            "temperature_unit": "celsius",
            "precipitation_unit": "mm",
        }
        async with self.session.get(
            "https://api.open-meteo.com/v1/forecast", params=params
        ) as response:
            response.raise_for_status()
            return Forecast.parse(await response.json())

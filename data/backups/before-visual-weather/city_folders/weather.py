"""Open-Meteo forecasts and Telegram-safe, compact Russian formatting."""

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html import escape
from typing import Any

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
    "weather_code,wind_speed_10m,is_day"
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

    @classmethod
    def parse(cls, data: Any) -> "Forecast":
        try:
            current = data["current"]
            daily = data["daily"]
            offset = int(data["utc_offset_seconds"])
            local = datetime.fromisoformat(current["time"]).replace(
                tzinfo=timezone(timedelta(seconds=offset))
            )
            days = daily["time"]
            if len(days) != 7:
                raise ValueError("Expected seven days")
            for day in days:
                date.fromisoformat(day)
            for key in DAILY.split(","):
                if len(daily[key]) != 7:
                    raise ValueError("Incomplete daily values")
            if number(current["temperature_2m"]) is None:
                raise ValueError("Missing current temperature")
            return cls(current, daily, local, str(data["timezone"]))
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise PilotError("Погодный API вернул неполные данные.") from exc

    def messages(self, city_id: str) -> tuple[str, str]:
        city = escape(CITIES[city_id].name)
        rows = ["Дата     Мин Макс Осадки Ветер"]
        conditions = []
        for i, value in enumerate(self.daily["time"]):
            day = date.fromisoformat(value)
            low = formatted(self.daily["temperature_2m_min"][i], signed=True)
            high = formatted(self.daily["temperature_2m_max"][i], signed=True)
            rain = formatted(self.daily["precipitation_probability_max"][i])
            wind = formatted(self.daily["wind_speed_10m_max"][i])
            rows.append(
                f"{day:%d.%m} {WEEKDAYS[day.weekday()]} "
                f"{low:>3} {high:>4} {rain:>5}% {wind:>4}"
            )
            icon, description = condition(self.daily["weather_code"][i])
            total = number(self.daily["precipitation_sum"][i])
            amount = "—" if total is None else f"{total:.1f}"
            conditions.append(
                f"{day:%d.%m} {icon} {description} · {amount} мм"
            )
        stamp = self.local_time.strftime("%d.%m.%Y %H:%M")
        weekly = (
            f"🌤 <b>{city} · прогноз на 7 дней</b>\n\n"
            "<pre>" + escape("\n".join(rows)) + "</pre>\n"
            "Температура: °C · осадки: вероятность · ветер: макс., м/с\n\n"
            + "\n".join(conditions)
            + f"\n\nДанные на {stamp} · местное время\n"
            + "Источник: Open-Meteo · обновление каждые 30 минут"
        )
        icon, description = condition(
            self.current.get("weather_code"), self.current.get("is_day", 1)
        )
        temperature = formatted(self.current["temperature_2m"], signed=True)
        feels = formatted(
            self.current.get("apparent_temperature"), signed=True
        )
        wind = formatted(self.current.get("wind_speed_10m"))
        humidity = formatted(self.current.get("relative_humidity_2m"))
        brief = (
            f"{icon} <b>{temperature} °C · {description}</b>\n"
            f"📍 {city} · данные на {self.local_time:%H:%M}\n"
            f"Ощущается как {feels} °C · 💨 {wind} м/с · 💧 {humidity}%\n"
            f"{self.local_time:%d.%m.%Y} · местное время · Open-Meteo"
        )
        return weekly, brief


class OpenMeteo:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session

    async def fetch(self, city_id: str) -> Forecast:
        lat, lon, zone = LOCATIONS[city_id]
        params = {
            "latitude": str(lat),
            "longitude": str(lon),
            "timezone": zone,
            "forecast_days": "7",
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

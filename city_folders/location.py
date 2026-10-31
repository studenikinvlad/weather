"""Online reverse geocoding and city search using OpenStreetMap."""

import asyncio
import os
import re
import time
from collections import OrderedDict
from dataclasses import replace
from math import isfinite

from aiohttp import ClientError, ClientSession, ClientTimeout

from city_folders.models import CITIES, City, PilotError
from city_folders.network import http_connector
from city_folders.weather import LOCATIONS

SETTLEMENTS = {"city", "town", "village", "hamlet", "municipality"}


class Geocoder:
    """Serialize and cache user-initiated requests to the public API."""

    def __init__(self):
        self.base_url = os.getenv(
            "GEOCODING_URL", "https://nominatim.openstreetmap.org"
        ).rstrip("/")
        self.lock = asyncio.Lock()
        self.next_request = 0.0
        self.cache = OrderedDict()

    async def request(self, endpoint, params):
        key = (endpoint, tuple(sorted(params.items())))
        async with self.lock:
            cached = self.cache.get(key)
            if cached and time.monotonic() - cached[0] < 86400:
                self.cache.move_to_end(key)
                return cached[1]
            await asyncio.sleep(max(0, self.next_request - time.monotonic()))
            self.next_request = time.monotonic() + 1.1
            try:
                async with ClientSession(
                    connector=http_connector(),
                    timeout=ClientTimeout(total=20),
                    headers={"User-Agent": "CityFolderWeatherBot/1.0"},
                ) as session:
                    async with session.get(
                        f"{self.base_url}/{endpoint}",
                        params={
                            "format": "jsonv2",
                            "addressdetails": 1,
                            "accept-language": "ru",
                            **params,
                        },
                    ) as response:
                        if response.status == 429:
                            self.next_request = time.monotonic() + 60
                        response.raise_for_status()
                        data = await response.json()
            except (ClientError, TimeoutError, ValueError) as exc:
                raise PilotError(
                    "Сервис определения города временно недоступен. "
                    "Попробуй ещё раз чуть позже."
                ) from exc
            self.cache[key] = (time.monotonic(), data)
            while len(self.cache) > 256:
                self.cache.popitem(last=False)
            return data

    @staticmethod
    def parse_city(data):
        if not isinstance(data, dict):
            return None
        if data.get("addresstype", data.get("type")) not in SETTLEMENTS:
            return None
        address = data.get("address", {})
        if not isinstance(address, dict):
            return None
        name = data.get("name")
        if not name:
            name = next(
                (
                    address[k]
                    for k in (
                        "city",
                        "town",
                        "village",
                        "hamlet",
                        "municipality",
                    )
                    if address.get(k)
                ),
                None,
            )
        try:
            lat, lon = float(data["lat"]), float(data["lon"])
            osm_id = int(data["osm_id"])
            osm_type = data["osm_type"]
            if (
                not name
                or osm_type not in {"node", "way", "relation"}
                or not isfinite(lat)
                or not isfinite(lon)
                or not -90 <= lat <= 90
                or not -180 <= lon <= 180
            ):
                return None
        except (KeyError, TypeError, ValueError):
            return None
        return City(
            id=f"osm_{osm_type[0]}{osm_id}",
            name=name,
            latitude=lat,
            longitude=lon,
            region=address.get("state", ""),
            country=address.get("country_code", "").lower(),
            country_name=address.get("country", ""),
        )

    async def search(self, query):
        query = re.sub(r"^(?:город\s+|г\.\s*)", "", query.strip(), flags=re.I)
        if not 2 <= len(query) <= 120:
            raise PilotError("Введи название города: от 2 до 120 символов.")
        data = await self.request(
            "search",
            {
                "q": query,
                "limit": 10,
                "featureType": "settlement",
            },
        )
        if not isinstance(data, list):
            raise PilotError("Сервис вернул неожиданный ответ. Повтори поиск.")
        cities = {}
        for row in data:
            city = self.parse_city(row)
            if city is not None:
                cities[city.id] = city
        if not cities:
            fallback = await self.request(
                "search",
                {
                    "city": query,
                    "limit": 10,
                },
            )
            if isinstance(fallback, list):
                for row in fallback:
                    city = self.parse_city(row)
                    if city is not None:
                        cities[city.id] = city
        return list(cities.values())[:6]

    async def reverse(self, latitude, longitude):
        if (
            not isfinite(latitude)
            or not isfinite(longitude)
            or not -90 <= latitude <= 90
            or not -180 <= longitude <= 180
        ):
            raise PilotError("Некорректные координаты геометки.")
        # Rounded query coordinates live only in a bounded in-memory cache.
        data = await self.request(
            "reverse",
            {
                "lat": round(latitude, 5),
                "lon": round(longitude, 5),
                "zoom": 14,
                "layer": "address",
            },
        )
        if not isinstance(data, dict):
            raise PilotError("Сервис вернул неожиданный ответ. Повтори поиск.")
        address = data.get("address", {})
        if not isinstance(address, dict):
            raise PilotError("Не удалось уточнить город. Введи его название.")
        name = next(
            (
                address[k]
                for k in ("city", "town", "village", "hamlet", "municipality")
                if address.get(k)
            ),
            None,
        )
        if not name:
            return None
        # Resolve the settlement itself, not a street/suburb or user position.
        results = await self.request(
            "search",
            {
                "city": name,
                "state": address.get("state", ""),
                "countrycodes": address.get("country_code", ""),
                "limit": 10,
            },
        )
        if not isinstance(results, list):
            raise PilotError("Не удалось уточнить город. Введи его название.")
        cities = [self.parse_city(row) for row in results]
        cities = [
            city
            for city in cities
            if city is not None and city.name.casefold() == name.casefold()
        ]
        if not cities:
            return self.parse_city(data)
        return min(
            cities,
            key=lambda city: (
                (city.latitude - latitude) ** 2
                + (city.longitude - longitude) ** 2
            ),
        )


def remember_city(service, city):
    """Persist settlement coordinates and keep existing channels reusable."""
    for existing in CITIES.values():
        if existing.id == city.id or (
            existing.name.casefold() == city.name.casefold()
            and existing.country == city.country
            and abs(existing.latitude - city.latitude) < 0.1
            and abs(existing.longitude - city.longitude) < 0.1
        ):
            return replace(
                existing,
                country_name=city.country_name or existing.country_name,
            )
    service.state.cities[city.id] = city
    service.save()
    CITIES[city.id] = city
    LOCATIONS[city.id] = (city.latitude, city.longitude, city.timezone)
    return city


def city_label(city):
    """Keep country identifiers stable; localize only their presentation."""
    country = city.country_name or {
        "ru": "Россия",
        "by": "Беларусь",
        "kz": "Казахстан",
        "ua": "Украина",
        "us": "США",
        "de": "Германия",
    }.get(city.country.lower(), city.country.upper())
    parts = [city.name, city.region, country]
    return ", ".join(dict.fromkeys(p for p in parts if p))

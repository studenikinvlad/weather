"""Shared currency channels: official FX and a delayed BTC market quote."""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree import ElementTree

from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import FSInputFile

from city_folders.models import (
    CURRENCIES,
    PilotError,
    WeatherPost,
    channel_key,
)
from city_folders.publisher import WeatherPublisher

MSK = timezone(timedelta(hours=3))
ASSETS = Path(__file__).resolve().parent.parent / "assets" / "currencies"
logger = logging.getLogger(__name__)


def positive(value):
    try:
        result = Decimal(str(value).replace(",", "."))
        if not result.is_finite() or result <= 0:
            raise ValueError
        return result
    except (InvalidOperation, ValueError):
        raise PilotError("Источник вернул некорректный курс.") from None


def parse_cbr(raw, today):
    root = ElementTree.fromstring(raw)
    effective = datetime.strptime(root.attrib["Date"], "%d.%m.%Y").date()
    if effective > today or (today - effective).days > 14:
        raise PilotError("Источник вернул курс на неподходящую дату.")
    rates = {}
    for item in root.findall("Valute"):
        code = item.findtext("CharCode")
        if code in {"USD", "EUR", "CNY"}:
            rates[code] = positive(item.findtext("Value")) / positive(
                item.findtext("Nominal")
            )
    if set(rates) != {"USD", "EUR", "CNY"}:
        raise PilotError("В ответе отсутствуют нужные валюты.")
    return rates, effective


def money(value):
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


class RateSource:
    def __init__(self, session):
        self.session = session
        self.fiat = None
        self.fiat_until = 0

    async def text(self, code):
        now = datetime.now(MSK)
        if code == "BTC":
            async with self.session.get(
                "https://blockchain.info/ticker"
            ) as response:
                response.raise_for_status()
                data = await response.json()
            price = positive(data["RUB"]["15m"])
            detail = "Рыночный ориентир · задержка около 15 минут"
            icon = "₿"
        else:
            if self.fiat is None or self.fiat_until <= time.time():
                async with self.session.get(
                    "https://www.cbr.ru/scripts/XML_daily.asp",
                    params={"date_req": now.strftime("%d/%m/%Y")},
                ) as response:
                    response.raise_for_status()
                    self.fiat = parse_cbr(await response.read(), now.date())
                self.fiat_until = min(
                    time.time() + 3500,
                    (
                        now.replace(hour=0, minute=0, second=0, microsecond=0)
                        + timedelta(days=1)
                    ).timestamp(),
                )
            rates, effective = self.fiat
            price = rates[code]
            detail = f"Официальный курс · действует с {effective:%d.%m.%Y}"
            icon = {"USD": "$", "EUR": "€", "CNY": "¥"}[code]
        checked = datetime.now(MSK)
        return (
            f"{icon} <b>1 {code} = {money(price)} ₽</b>\n\n"
            f"{CURRENCIES[code]} → Российский рубль\n"
            f"{detail}\n\n"
            f"🕒 Получено {checked:%H:%M · %d.%m.%Y} МСК\n"
            "snowaa"
        )


class RatesPublisher(WeatherPublisher):
    async def update(self, code, *, force=False):
        async with self.lock:
            channel = self.service.state.channels.get(
                channel_key(code, "rates")
            )
            if not channel or not channel.ready:
                return False
            post = self.service.state.rates.setdefault(code, WeatherPost())
            if not force and post.next_update > time.time():
                return True
            try:
                text = await self.source.text(code)
                chat = -1000000000000 - channel.channel_id
                avatar_path = ASSETS / f"{code}.png"
                if post.avatar_key != "currency-v1" and avatar_path.is_file():
                    await self.bot.set_chat_photo(
                        chat_id=chat, photo=FSInputFile(avatar_path)
                    )
                    post.avatar_key = "currency-v1"
                    self.service.save()
                if channel.branding_version < 1:
                    source = (
                        "Blockchain.com · https://blockchain.info/ticker"
                        if code == "BTC"
                        else "Банк России · https://www.cbr.ru/"
                    )
                    await self.bot.set_chat_description(
                        chat_id=chat,
                        description=(
                            f"snowaa · {CURRENCIES[code]} в рублях.\n"
                            "Справочный курс за одну единицу. "
                            "Курс покупки или продажи может отличаться.\n"
                            f"Источник: {source}"
                        ),
                    )
                    channel.branding_version = 1
                    self.service.save()
                await self.write_message(chat, post.brief, text)
                post.failures = 0
                post.last_success = time.time()
                post.next_update = time.time() + (
                    300 if code == "BTC" else 3600
                )
            except Exception as exc:
                post.failures += 1
                delay = min(60 * 2 ** min(post.failures - 1, 5), 1800)
                if isinstance(exc, TelegramRetryAfter):
                    delay = max(delay, exc.retry_after + 1)
                post.next_update = time.time() + delay
                logger.warning(
                    "Rate %s postponed: %s", code, type(exc).__name__
                )
                self.service.save()
                return False
            self.service.save()
            return True

    async def run(self):
        while not self.stop.is_set():
            for code in CURRENCIES:
                await self.update(code)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=5)
            except TimeoutError:
                pass

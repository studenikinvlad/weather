"""Native Telegram layout: 20 future hours and 11 collapsible days."""

from datetime import date

from aiogram.types import (
    InputRichBlockParagraph,
    InputRichBlockTable,
    InputRichMessage,
    MessageEntity,
    RichBlockTableCell,
    RichTextCustomEmoji,
)

from city_folders.artwork import artwork_key
from city_folders.weather import WEEKDAYS, condition, formatted


def card(code, daytime, temperature, emoji):
    key = artwork_key(code, daytime, temperature)
    ids = emoji.get(key)
    if not ids:
        icon, _ = condition(code, daytime)
        return [f"{icon}\n{formatted(temperature, signed=True)}°"]
    pieces = [
        RichTextCustomEmoji(custom_emoji_id=i, alternative_text="🌤")
        for i in ids
    ]
    return [pieces[0], pieces[1], "\n", pieces[2], pieces[3]]


def required_artwork(forecast):
    keys = set()
    for h in forecast.next_hours():
        keys.add(
            artwork_key(h["weather_code"], h["is_day"], h["temperature_2m"])
        )
    return sorted(keys - {None})


def rich_forecasts(forecast, emoji):
    hours = forecast.next_hours()
    cells = [[], []]
    for h in hours:
        cells[0].append(
            RichBlockTableCell(
                align="center",
                valign="middle",
                text=card(
                    h["weather_code"], h["is_day"], h["temperature_2m"], emoji
                ),
            )
        )
        cells[1].append(
            RichBlockTableCell(
                align="center",
                valign="middle",
                text=f"{h['time']:%H:%M}",
            )
        )
    hourly = InputRichMessage(
        blocks=[
            InputRichBlockParagraph(
                text="Следующие 20 часов · местное время",
            ),
            InputRichBlockTable(cells=cells, is_compact=True),
        ]
    )
    days = []
    for i, stamp in enumerate(forecast.daily["time"]):
        day = date.fromisoformat(stamp)
        heading = (
            f"{day:%d.%m} · {forecast.day_label(day)}"
            if (day - forecast.local_time.date()).days in (0, 1)
            else f"{day:%d.%m} · {WEEKDAYS[day.weekday()]}"
        )
        low = formatted(forecast.daily["temperature_2m_min"][i], signed=True)
        high = formatted(forecast.daily["temperature_2m_max"][i], signed=True)
        icon, _ = condition(forecast.daily["weather_code"][i])
        days.append(f"{icon} {high}° / {low}° · {heading}\n")
    # Classic message entities are supported independently of rich blocks.
    # Keep the period visible while the daily rows are collapsed.
    heading = "11 дней\n"
    body = "".join(days).rstrip()
    daily = {
        "text": heading + body,
        "entities": [
            MessageEntity(
                type="expandable_blockquote",
                offset=len(heading.encode("utf-16-le")) // 2,
                length=len(body.encode("utf-16-le")) // 2,
            )
        ],
    }
    return hourly, daily

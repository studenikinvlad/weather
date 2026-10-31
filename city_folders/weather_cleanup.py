"""Recognize historical weather widgets while preserving unrelated posts."""

import re

from telethon import types

from city_folders.weather import condition

STATES = {condition(code, day) for code in range(100) for day in (0, 1)}
ICONS = "(?:" + "|".join(re.escape(icon) for icon, _ in STATES) + ")"
LABELS = "(?:" + "|".join(re.escape(label) for _, label in STATES) + ")"
TEMP = r"(?:[+-]\d+|—)°"
HEADING = re.compile(rf"{ICONS} {TEMP} · {LABELS}")
DAY = re.compile(
    rf"{ICONS} {TEMP} / {TEMP} · \d{{2}}\.\d{{2}} · "
    r"(?:Сегодня|Завтра|Пн|Вт|Ср|Чт|Пт|Сб|Вс)"
)
DETAIL = re.compile(
    rf"(?:Ощущается как {TEMP}|💧 \d+%|Давление \d+ мм рт\. ст\.|"
    r"💨 (?:\d+ м/с(?: · порывы \d+ м/с)?|порывы \d+ м/с))"
)
SUMMARY = re.compile(
    rf"(?:↑ {TEMP} · )?(?:20 ч без осадков|Прогноз осадков уточняется|"
    + "(?:"
    + "|".join(re.escape(label.lower()) for _, label in STATES)
    + r") (?:завтра )?с \d{2}:\d{2}(?: · \d+%)?)"
)


FEELS = re.compile(r"Ощущается как (?:[+-]?\d+|—)°")
SIMPLE_SUMMARY = re.compile(
    rf"(?:В ближайшие 20 часов без осадков|Прогноз осадков уточняется|"
    rf"{LABELS} (?:в течение часа|через \d+ час(?:а|ов)?)|"
    + "Сейчас (?:"
    + "|".join(re.escape(label.lower()) for _, label in STATES)
    + "))"
)


def is_weather_post(message):
    if getattr(message, "action", None) or getattr(message, "fwd_from", None):
        return False
    rich = getattr(message, "rich_message", None)
    if rich:
        content = str(rich.to_dict())
        return (
            "Следующие 20 часов" in content and "table" in content.lower()
        ) or ("11 дней" in content and "blockquot" in content.lower())
    document = getattr(message, "document", None)
    if document:
        for attribute in document.attributes:
            if isinstance(attribute, types.DocumentAttributeSticker):
                pack = getattr(attribute.stickerset, "short_name", "")
                if pack.startswith("snowaa_s1_") and "_by_" in pack:
                    return True
            if (
                isinstance(attribute, types.DocumentAttributeFilename)
                and attribute.file_name == "weather.webp"
                and document.mime_type == "image/webp"
            ):
                return True
        return False
    rows = (getattr(message, "message", None) or "").strip().splitlines()
    if not rows:
        return False
    if (
        len(rows) >= 2
        and FEELS.fullmatch(rows[0])
        and SIMPLE_SUMMARY.fullmatch(rows[1])
    ):
        if len(rows) == 2:
            return True
        rows = rows[2:]

    if (
        len(rows) >= 3
        and HEADING.fullmatch(rows[0])
        and SUMMARY.fullmatch(rows[1])
        and rows[2] == "11 дней"
    ):
        rows = rows[2:]
    if rows[0] == "11 дней" or rows[0].endswith(" · 11 дней"):
        return len(rows) == 12 and all(DAY.fullmatch(row) for row in rows[1:])
    if HEADING.fullmatch(rows[0]):
        return (len(rows) == 2 and bool(SUMMARY.fullmatch(rows[1]))) or (
            1 <= len(rows) <= 5
            and all(DETAIL.fullmatch(row) for row in rows[1:])
        )
    return 1 <= len(rows) <= 4 and all(DETAIL.fullmatch(row) for row in rows)

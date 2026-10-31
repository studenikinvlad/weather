"""Interpret Telegram's current account configuration."""

from dataclasses import dataclass
from math import isfinite
from typing import Any

from telethon import types

from city_folders.models import PilotError

# Official fallback values, checked on 2026-09-06. Live values take priority.
# https://core.telegram.org/api/config
DEFAULT_LIMITS = {
    "dialog_filters_limit": (10, 30),
    "dialog_filters_chats_limit": (100, 200),
    "chatlist_invites_limit": (3, 100),
    "chatlists_joined_limit": (2, 20),
    "channels_limit": (500, 1000),
}


def decode_json(value: object) -> Any:
    """Decode MTProto JSONValue, which is not Python's built-in JSON."""

    if isinstance(value, types.JsonObject):
        return {item.key: decode_json(item.value) for item in value.value}
    if isinstance(value, types.JsonArray):
        return [decode_json(item) for item in value.value]
    if isinstance(value, (types.JsonString, types.JsonNumber, types.JsonBool)):
        return value.value
    if isinstance(value, types.JsonNull):
        return None
    raise PilotError("Telegram вернул неизвестный тип конфигурации.")


@dataclass(frozen=True, slots=True)
class Limits:
    """The configured limits, not a promise that an RPC will be allowed."""

    values: dict[str, int]
    fallback_keys: frozenset[str]

    @classmethod
    def from_config(cls, config: dict[str, Any], premium: bool) -> "Limits":
        values = {}
        fallback = set()
        suffix = "premium" if premium else "default"
        for base, defaults in DEFAULT_LIMITS.items():
            key = f"{base}_{suffix}"
            value = config.get(key)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
                or value < 0
                or int(value) != value
            ):
                value = defaults[int(premium)]
                fallback.add(base)
            values[base] = int(value)
        return cls(values, frozenset(fallback))

    def require(self, key: str, planned: int, label: str) -> None:
        available = self.values[key]
        if planned > available:
            raise PilotError(
                f"Недостаточно лимита: {label}. "
                f"Нужно {planned}, максимум {available}."
            )

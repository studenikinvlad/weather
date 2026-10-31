"""Domain state, independent of Telegram request classes."""

from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

ServiceKind = Literal["weather", "news", "rates"]
RATES_ENABLED = False  # Paused until currency channels are ready.
CURRENCIES = {
    "USD": "Доллар",
    "EUR": "Евро",
    "CNY": "Юань",
    "BTC": "Биткоин",
}


@dataclass(frozen=True, slots=True)
class City:
    """A stable catalogue entry used by the pilot."""

    id: str
    name: str
    latitude: float = 0
    longitude: float = 0
    timezone: str = "auto"
    region: str = ""
    country: str = ""
    country_name: str = ""


CITIES = {
    "kazan": City("kazan", "Казань", 55.7961, 49.1064, country="ru"),
    "samara": City("samara", "Самара", 53.1959, 50.1002, country="ru"),
    "khabarovsk": City(
        "khabarovsk", "Хабаровск", 48.4827, 135.0838, country="ru"
    ),
}
SERVICE_NAMES = {"weather": "Погода", "news": "Новости"}
FOLDER_TITLE = "Погода"
LEGACY_FOLDER_TITLE = "Мой город"


class StrictModel(BaseModel):
    """Reject unrecognized state instead of silently losing it."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ChannelState(StrictModel):
    """A persisted channel creation intent and its confirmed result."""

    temporary_title: str
    channel_id: int | None = None
    access_hash: int | None = None
    pending: bool = False
    ready: bool = False
    branding_version: int = 0


class InviteState(StrictModel):
    """One invitation for one city; its title is private to the owner."""

    url: str | None = None
    pending: bool = False
    services: list[ServiceKind] = Field(default_factory=lambda: ["weather"])


class WeatherMessage(StrictModel):
    """A send intent allows reconciliation after a lost Bot API response."""

    message_id: int | None = None
    pending_text: str | None = None


class WeatherPost(StrictModel):
    weekly: WeatherMessage = Field(default_factory=WeatherMessage)
    brief: WeatherMessage = Field(default_factory=WeatherMessage)
    next_update: float = 0
    last_success: float = 0
    failures: int = 0
    pinned: bool = False
    active_ids: list[int] = Field(default_factory=list)
    staged_ids: list[int] = Field(default_factory=list)
    staged_layout_version: int = 0
    staged_sticker_key: str | None = None
    delete_ids: list[int] = Field(default_factory=list)
    pending_kind: str | None = None
    pending_after: int = 0
    snapshot: dict = Field(default_factory=dict)
    last_forecast: dict = Field(default_factory=dict)
    avatar_key: str | None = None
    avatar_retry_at: float = 0
    avatar_updated_at: float = 0
    layout_version: int = 0
    generation: int = 0
    ephemeral_messages: list[dict] = Field(default_factory=list)


class ProjectState(StrictModel):
    """Small single-host state, to be replaced by a DB in the full service."""

    cities: dict[str, City] = Field(default_factory=dict)
    version: Literal[1] = 1
    project_id: str = Field(default_factory=lambda: uuid4().hex[:16])
    account_id: int | None = None
    bot_id: int | None = None
    folder_id: int | None = None
    folder_ready: bool = False
    cooldown_until: float = 0
    rpc_cooldowns: dict[str, float] = Field(default_factory=dict)
    channels: dict[str, ChannelState] = Field(default_factory=dict)
    invites: dict[str, InviteState] = Field(default_factory=dict)
    weather: dict[str, WeatherPost] = Field(default_factory=dict)
    rates: dict[str, WeatherPost] = Field(default_factory=dict)
    retired_weather_channels: dict[str, ChannelState] = Field(
        default_factory=dict
    )
    retired_weather_posts: dict[str, WeatherPost] = Field(default_factory=dict)


class PilotError(Exception):
    """An actionable error that is safe to show without secrets."""


def channel_key(city_id: str, kind: ServiceKind) -> str:
    """Return a catalogue identity, never a display-name lookup."""

    return f"{city_id}:{kind}"

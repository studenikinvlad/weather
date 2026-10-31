"""Configuration loaded only from the selected env file and environment."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

from city_folders.models import PilotError


@dataclass(frozen=True, slots=True)
class Settings:
    """Secrets are excluded from the object's representation."""

    api_id: int
    api_hash: str = field(repr=False)
    bot_token: str = field(repr=False)
    phone: str = field(repr=False)
    data_dir: Path
    allowed_user_ids: frozenset[int]
    subscription_check_enabled: bool = False
    main_channel_id: int = 0
    main_channel_url: str = ""
    folder_invite_ttl_seconds: int = 300

    @classmethod
    def load(cls, env_file: Path) -> "Settings":
        env_file = env_file.resolve()
        load_dotenv(env_file, override=False)
        enabled = (
            os.getenv("SUBSCRIPTION_CHECK_ENABLED", "false").strip().lower()
        )
        if enabled not in {"true", "false"}:
            raise PilotError("SUBSCRIPTION_CHECK_ENABLED: true или false.")
        try:
            main_channel_id = int(os.getenv("MAIN_CHANNEL_ID") or "0")
            ttl = int(os.getenv("FOLDER_INVITE_TTL_SECONDS") or "300")
            api_id = int(os.environ.get("TELEGRAM_API_ID", ""))
            allowed = frozenset(
                int(value.strip())
                for value in os.environ.get("ALLOWED_USER_IDS", "").split(",")
                if value.strip()
            )
        except ValueError as exc:
            raise PilotError(
                "Проверьте числовые ID и FOLDER_INVITE_TTL_SECONDS."
            ) from exc
        if ttl <= 0:
            raise PilotError("FOLDER_INVITE_TTL_SECONDS должен быть > 0.")
        api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()
        if api_id <= 0 or not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
            raise PilotError("Проверь TELEGRAM_API_ID и TELEGRAM_API_HASH.")
        if any(user_id <= 0 for user_id in allowed):
            raise PilotError("В ALLOWED_USER_IDS нужны положительные user ID.")
        data_dir = Path(os.environ.get("DATA_DIR", "data")).expanduser()
        if not data_dir.is_absolute():
            data_dir = env_file.parent / data_dir
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        return cls(
            api_id=api_id,
            api_hash=api_hash,
            bot_token=os.environ.get("BOT_TOKEN", "").strip(),
            phone=os.environ.get("TELEGRAM_PHONE", "").strip(),
            data_dir=data_dir.resolve(),
            allowed_user_ids=allowed,
            subscription_check_enabled=enabled == "true",
            main_channel_id=main_channel_id,
            main_channel_url=os.getenv("MAIN_CHANNEL_URL", "").strip(),
            folder_invite_ttl_seconds=ttl,
        )

    def validate_subscription(self) -> None:
        if not self.subscription_check_enabled:
            return
        url = urlsplit(self.main_channel_url)
        if (
            self.main_channel_id >= 0
            or url.scheme != "https"
            or url.netloc != "t.me"
            or not url.path.strip("/")
            or url.path.startswith("/addlist/")
            or url.query
            or url.fragment
        ):
            raise PilotError("Заполните MAIN_CHANNEL_ID и MAIN_CHANNEL_URL.")

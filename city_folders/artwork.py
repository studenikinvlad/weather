"""One shared SQLite catalogue for artwork and reusable Telegram emoji."""

import asyncio
import hashlib
import io
import logging
import math
import re
import sqlite3
import time
import zipfile
from pathlib import Path

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import BufferedInputFile, InputSticker
from PIL import Image

from city_folders.models import PilotError

PATTERN = re.compile(
    r"(sunny|partlycloudy|cloudy|rain|lightning|snow|fog)_"
    r"(day|night)_(0|[mp]\d+)\.png$"
)


def artwork_key(code, is_day, temperature):
    if (
        isinstance(temperature, bool)
        or not isinstance(temperature, (float, int))
        or not math.isfinite(temperature)
    ):
        return None
    temp = round(temperature)
    # Never show a false boundary temperature outside the supplied range.
    if not -35 <= temp <= 35:
        return None
    if code == 0:
        kind = "sunny"
    elif code in (1, 2):
        kind = "partlycloudy"
    elif code == 3:
        kind = "cloudy"
    elif code in (45, 48):
        kind = "fog"
    elif code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        kind = "rain"
    elif code in (71, 73, 75, 77, 85, 86):
        kind = "snow"
    elif code in (95, 96, 99):
        kind = "lightning"
    else:
        return None
    suffix = "0" if temp == 0 else f"{'p' if temp > 0 else 'm'}{abs(temp)}"
    return f"{kind}_{'day' if is_day else 'night'}_{suffix}"


def encode(image, format="WEBP"):
    output = io.BytesIO()
    image.save(output, format=format, lossless=True)
    return output.getvalue()


def quarters(raw):
    with Image.open(io.BytesIO(raw)) as source:
        image = source.convert("RGBA")
        width, height = image.size
        return [
            encode(
                image.crop(
                    (
                        x * width // 2,
                        y * height // 2,
                        (x + 1) * width // 2,
                        (y + 1) * height // 2,
                    )
                ).resize((100, 100), Image.Resampling.LANCZOS)
            )
            for y in range(2)
            for x in range(2)
        ]


class Artwork:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS artwork (
                key TEXT PRIMARY KEY, avatar BLOB NOT NULL,
                sticker BLOB NOT NULL, digest TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS regular_stickers (
                key TEXT NOT NULL, bot_id INTEGER NOT NULL,
                pack TEXT NOT NULL, remote_index INTEGER NOT NULL,
                upload_id TEXT NOT NULL, file_id TEXT,
                PRIMARY KEY(key, bot_id)
            );
            CREATE TABLE IF NOT EXISTS sticker_files (
                key TEXT NOT NULL, bot_id INTEGER NOT NULL,
                file_id TEXT NOT NULL, PRIMARY KEY(key, bot_id)
            );
            CREATE TABLE IF NOT EXISTS emoji_tiles (
                key TEXT NOT NULL, quadrant INTEGER NOT NULL,
                image BLOB NOT NULL, file_id TEXT, unique_id TEXT,
                emoji_id TEXT, pack TEXT, bot_id INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(key, quadrant, bot_id)
            );
        """)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS artwork_jobs (
                key TEXT PRIMARY KEY, requested_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS artwork_settings (
                key TEXT PRIMARY KEY, value REAL NOT NULL
            );
        """)
        columns = {
            r[1] for r in self.db.execute("PRAGMA table_info(emoji_tiles)")
        }
        if "remote_index" not in columns:
            self.db.execute(
                "ALTER TABLE emoji_tiles ADD COLUMN remote_index INTEGER"
            )
            self.db.commit()
        self.lock = asyncio.Lock()

    def close(self):
        self.db.close()

    def import_zip(self, path):
        images = {}
        with zipfile.ZipFile(path) as archive:
            for entry in archive.infolist():
                if entry.filename.startswith("__MACOSX/"):
                    continue
                name = Path(entry.filename).name
                if not PATTERN.fullmatch(name):
                    continue
                raw = archive.read(entry)
                with Image.open(io.BytesIO(raw)) as image:
                    if image.size not in {(240, 240), (512, 512)}:
                        raise PilotError(f"Неверный размер картинки: {name}")
                    variant = "avatar" if image.width == 240 else "sticker"
                    slot = images.setdefault(Path(name).stem, {})
                    if variant in slot:
                        raise PilotError(f"Дубликат картинки: {name}")
                    slot[variant] = raw
        if len(images) != 994 or any(len(v) != 2 for v in images.values()):
            raise PilotError("Нужны 994 пары аватарок и стикеров.")
        temperatures = ["0"] + [
            f"{sign}{i}" for sign in ("m", "p") for i in range(1, 36)
        ]
        expected = {
            f"{kind}_{daytime}_{temperature}"
            for kind in (
                "sunny",
                "partlycloudy",
                "cloudy",
                "rain",
                "lightning",
                "snow",
                "fog",
            )
            for daytime in ("day", "night")
            for temperature in temperatures
        }
        if set(images) != expected:
            raise PilotError("В архиве неполный набор состояний и температур.")
        with self.db:
            for key, pair in images.items():
                digest = hashlib.sha256(
                    pair["avatar"] + pair["sticker"]
                ).hexdigest()
                old = self.db.execute(
                    "SELECT digest FROM artwork WHERE key=?", (key,)
                ).fetchone()
                if old and old["digest"] == digest:
                    continue
                if old:
                    raise PilotError(
                        "Картинки уже загружены; нужна новая версия каталога."
                    )
                with Image.open(io.BytesIO(pair["sticker"])) as image:
                    sticker = encode(image.convert("RGBA"))
                self.db.execute(
                    "INSERT INTO artwork VALUES (?,?,?,?)",
                    (key, pair["avatar"], sticker, digest),
                )
                for index, raw in enumerate(quarters(pair["sticker"])):
                    self.db.execute(
                        "INSERT INTO emoji_tiles(key,quadrant,image) "
                        "VALUES (?,?,?)",
                        (key, index, raw),
                    )
        return len(images)

    def image(self, key, variant):
        if variant not in {"avatar", "sticker"}:
            raise ValueError(variant)
        row = self.db.execute(
            f"SELECT {variant} FROM artwork WHERE key=?", (key,)
        ).fetchone()
        return bytes(row[0]) if row else None

    async def sticker(self, bot, owner, key, username):
        """Reuse real stickers from 14 shared sets of up to 71 pictures."""
        async with self.lock:
            row = self.db.execute(
                "SELECT file_id FROM regular_stickers "
                "WHERE key=? AND bot_id=?",
                (key, bot.id),
            ).fetchone()
            if row and row[0]:
                return row[0]
            raw = self.image(key, "sticker")
            if raw is None or not PATTERN.fullmatch(key + ".png"):
                raise PilotError("Стикер отсутствует в каталоге.")
            group = key.rsplit("_", 1)[0]
            pack = f"snowaa_s1_{group}_by_{username}"
            try:
                remote = await bot.get_sticker_set(pack)
            except TelegramBadRequest as exc:
                if "STICKERSET_INVALID" not in exc.message.upper():
                    raise
                remote = None

            # Finish an interrupted addition before reserving another index,
            # including when the next requesting city needs a different image.
            pending = self.db.execute(
                "SELECT * FROM regular_stickers WHERE pack=? AND bot_id=? "
                "AND file_id IS NULL ORDER BY remote_index",
                (pack, bot.id),
            ).fetchall()
            for item in pending:
                remote = await self._finish_sticker(bot, owner, item, remote)
            row = self.db.execute(
                "SELECT file_id FROM regular_stickers "
                "WHERE key=? AND bot_id=?",
                (key, bot.id),
            ).fetchone()
            if row and row[0]:
                return row[0]
            uploaded = await bot.upload_sticker_file(
                user_id=owner,
                sticker=BufferedInputFile(raw, "weather.webp"),
                sticker_format="static",
            )
            index = len(remote.stickers) if remote else 0
            if index >= 71:
                raise PilotError("Общий набор стикеров неожиданно заполнен.")
            with self.db:
                self.db.execute(
                    "INSERT INTO regular_stickers VALUES (?,?,?,?,?,NULL)",
                    (key, bot.id, pack, index, uploaded.file_id),
                )
            item = self.db.execute(
                "SELECT * FROM regular_stickers WHERE key=? AND bot_id=?",
                (key, bot.id),
            ).fetchone()
            remote = await self._finish_sticker(bot, owner, item, remote)
            return remote.stickers[index].file_id

    async def _finish_sticker(self, bot, owner, item, remote):
        index = item["remote_index"]
        count = len(remote.stickers) if remote else 0
        if count not in (index, index + 1):
            raise PilotError("Общий набор стикеров изменён вне проекта.")
        if count == index:
            sticker = InputSticker(
                sticker=item["upload_id"],
                format="static",
                emoji_list=["🌤"],
            )
            if remote is None:
                await bot.create_new_sticker_set(
                    user_id=owner,
                    name=item["pack"],
                    title="snowaa · " + item["key"].rsplit("_", 1)[0],
                    sticker_type="regular",
                    stickers=[sticker],
                )
            else:
                await bot.add_sticker_to_set(
                    user_id=owner,
                    name=item["pack"],
                    sticker=sticker,
                )
            remote = await bot.get_sticker_set(item["pack"])
        if len(remote.stickers) != index + 1:
            raise PilotError("Telegram изменил порядок стикеров.")
        sticker = remote.stickers[index]
        if sticker.type != "regular" or sticker.set_name != item["pack"]:
            raise PilotError("Telegram не вернул стикер из нужного набора.")
        with self.db:
            self.db.execute(
                "UPDATE regular_stickers SET file_id=? "
                "WHERE key=? AND bot_id=?",
                (sticker.file_id, item["key"], bot.id),
            )
        return remote

    def available(self):
        return (
            self.db.execute("SELECT count(*) FROM artwork").fetchone()[0]
            == 994
        )

    def enabled(self):
        row = self.db.execute(
            "SELECT value FROM artwork_settings WHERE key='emoji_enabled'"
        ).fetchone()
        return row is None or bool(row[0])

    def set_enabled(self, value):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO artwork_settings VALUES (?,?)",
                ("emoji_enabled", int(value)),
            )

    def cached(self, bot_id, key):
        rows = self.db.execute(
            "SELECT emoji_id FROM emoji_tiles WHERE key=? AND bot_id=? "
            "ORDER BY quadrant",
            (key, bot_id),
        ).fetchall()
        return (
            [r[0] for r in rows]
            if len(rows) == 4 and all(r[0] for r in rows)
            else None
        )

    def request(self, bot_id, keys):
        result = {}
        if not self.enabled():
            return result
        with self.db:
            for key in keys:
                cached = self.cached(bot_id, key)
                if cached:
                    result[key] = cached
                else:
                    self.db.execute(
                        "INSERT INTO artwork_jobs VALUES (?,?) "
                        "ON CONFLICT(key) DO UPDATE SET "
                        "requested_at=excluded.requested_at",
                        (key, time.time()),
                    )
        return result

    async def run(self, bot, owner, username):
        logger = logging.getLogger(__name__)
        while True:
            row = self.db.execute(
                "SELECT value FROM artwork_settings WHERE key=?", ("retry_at",)
            ).fetchone()
            retry = row[0] if row else 0
            job = self.db.execute(
                "SELECT key FROM artwork_jobs WHERE requested_at>? "
                "ORDER BY requested_at LIMIT 1",
                (time.time() - 86400,),
            ).fetchone()
            if not self.enabled() or not job or retry > time.time():
                await asyncio.sleep(5)
                continue
            try:
                await self.emoji(bot, owner, username, job[0])
                with self.db:
                    self.db.execute(
                        "DELETE FROM artwork_jobs WHERE key=?", (job[0],)
                    )
            except Exception as exc:
                delay = (
                    exc.retry_after + 1
                    if isinstance(exc, TelegramRetryAfter)
                    else 60
                )
                with self.db:
                    self.db.execute(
                        "INSERT OR REPLACE INTO artwork_settings VALUES (?,?)",
                        ("retry_at", time.time() + delay),
                    )
                logger.warning(
                    "Artwork upload paused %s seconds: %s",
                    delay,
                    type(exc).__name__,
                )
            await asyncio.sleep(3)

    async def emoji(self, bot, owner, username, key):
        """Upload lazily and recover sets after lost replies."""
        async with self.lock:
            rows = self.db.execute(
                "SELECT * FROM emoji_tiles WHERE key=? AND bot_id=? "
                "ORDER BY quadrant",
                (key, bot.id),
            ).fetchall()
            if len(rows) == 4 and all(row["emoji_id"] for row in rows):
                return [row["emoji_id"] for row in rows]
            # Each immutable artwork has four pieces in one deterministic set.
            # Keep the order of already published emoji immutable.
            keys = [
                r[0]
                for r in self.db.execute(
                    "SELECT key FROM artwork ORDER BY key"
                )
            ]
            bucket = keys.index(key) // 50
            pack = f"snowaa_weather_v1_{bucket}_by_{username}"
            base = self.db.execute(
                "SELECT * FROM emoji_tiles WHERE key=? AND bot_id=0 "
                "ORDER BY quadrant",
                (key,),
            ).fetchall()
            if len(base) != 4:
                raise PilotError("Картинка отсутствует в каталоге.")
            with self.db:
                for row in base:
                    self.db.execute(
                        "INSERT OR IGNORE INTO emoji_tiles"
                        "(key,quadrant,image,pack,bot_id) VALUES (?,?,?,?,?)",
                        (key, row["quadrant"], row["image"], pack, bot.id),
                    )
            rows = self.db.execute(
                "SELECT * FROM emoji_tiles WHERE key=? AND bot_id=? "
                "ORDER BY quadrant",
                (key, bot.id),
            ).fetchall()
            for row in rows:
                if row["file_id"]:
                    continue
                uploaded = await bot.upload_sticker_file(
                    user_id=owner,
                    sticker=BufferedInputFile(row["image"], "tile.webp"),
                    sticker_format="static",
                )
                with self.db:
                    self.db.execute(
                        "UPDATE emoji_tiles SET file_id=?,unique_id=? "
                        "WHERE key=? AND quadrant=? AND bot_id=?",
                        (
                            uploaded.file_id,
                            uploaded.file_unique_id,
                            key,
                            row["quadrant"],
                            bot.id,
                        ),
                    )
            rows = self.db.execute(
                "SELECT * FROM emoji_tiles WHERE key=? AND bot_id=? "
                "ORDER BY quadrant",
                (key, bot.id),
            ).fetchall()
            try:
                remote = await bot.get_sticker_set(pack)
            except TelegramBadRequest as exc:
                if "STICKERSET_INVALID" not in exc.message.upper():
                    raise
                remote = None
            ids = []
            for row in rows:
                if row["emoji_id"]:
                    ids.append(row["emoji_id"])
                    continue
                index = row["remote_index"]
                if index is None:
                    index = len(remote.stickers) if remote else 0
                    with self.db:
                        self.db.execute(
                            "UPDATE emoji_tiles SET remote_index=?,pack=? "
                            "WHERE key=? AND quadrant=? AND bot_id=?",
                            (index, pack, key, row["quadrant"], bot.id),
                        )
                count = len(remote.stickers) if remote else 0
                if count < index or count > index + 1:
                    raise PilotError("Набор эмодзи изменён вне проекта.")
                if count == index:
                    sticker = InputSticker(
                        sticker=row["file_id"],
                        format="static",
                        emoji_list=["🌤"],
                    )
                    if remote is None:
                        await bot.create_new_sticker_set(
                            user_id=owner,
                            name=pack,
                            title=f"snowaa · погода {bucket + 1}",
                            sticker_type="custom_emoji",
                            stickers=[sticker],
                        )
                    else:
                        await bot.add_sticker_to_set(
                            user_id=owner, name=pack, sticker=sticker
                        )
                    remote = await bot.get_sticker_set(pack)
                    await asyncio.sleep(1)
                if len(remote.stickers) != index + 1:
                    raise PilotError("Telegram изменил порядок эмодзи.")
                emoji_id = remote.stickers[index].custom_emoji_id
                if not emoji_id:
                    raise PilotError(
                        "Telegram не вернул идентификатор эмодзи."
                    )
                with self.db:
                    self.db.execute(
                        "UPDATE emoji_tiles SET emoji_id=? "
                        "WHERE key=? AND quadrant=? AND bot_id=?",
                        (emoji_id, key, row["quadrant"], bot.id),
                    )
                ids.append(emoji_id)
            return ids

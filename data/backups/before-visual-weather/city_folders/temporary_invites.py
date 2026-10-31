"""Subscription-gated exports with a durable, restart-safe deletion ledger."""

import asyncio
import hashlib
import logging
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from telethon import errors, functions, types

from city_folders.models import InviteState, PilotError
from city_folders.service import Provisioner, invite_slug, peer_ids

logger = logging.getLogger(__name__)
SUBSCRIBED = {"creator", "administrator", "member"}


class InviteLedger:
    """Separate database: old state.json and other SQLite files stay intact."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.db.close()
            raise PilotError("Неизвестная версия очереди приглашений.")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS invites (
                id TEXT PRIMARY KEY, account_id INTEGER NOT NULL,
                folder_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                city_id TEXT NOT NULL, title TEXT NOT NULL,
                url TEXT, created_at REAL NOT NULL, expires_at REAL NOT NULL,
                status TEXT NOT NULL, next_attempt REAL NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS invite_due
                ON invites(status, next_attempt, expires_at);
            PRAGMA user_version=1;
        """)

    def close(self) -> None:
        self.db.close()

    def pending(self, service: Provisioner, uid: int, city: str, ttl: int):
        identity = uuid4().hex[:28]
        title = "tmp:" + identity
        now = time.time()
        with self.db:
            self.db.execute(
                "INSERT INTO invites(id,account_id,folder_id,user_id,city_id,"
                "title,created_at,expires_at,status) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    service.state.account_id,
                    service.state.folder_id,
                    uid,
                    city,
                    title,
                    now,
                    now + ttl,
                    "pending",
                ),
            )
        return identity, title

    def update(self, identity: str, status: str, url: str | None = None):
        with self.db:
            self.db.execute(
                "UPDATE invites SET status=?,url=COALESCE(?,url),"
                "next_attempt=0 WHERE id=?",
                (status, url, identity),
            )

    def retry(self, row, seconds: float):
        with self.db:
            self.db.execute(
                "UPDATE invites SET attempts=attempts+1,next_attempt=? "
                "WHERE id=?",
                (time.time() + seconds, row["id"]),
            )


class TemporaryInvites:
    def __init__(
        self,
        service: Provisioner,
        bot: Bot,
        ledger: InviteLedger,
        main_channel_id: int,
        main_channel_url: str,
        ttl: int = 300,
        *,
        subscription_check_enabled: bool = True,
    ) -> None:
        self.service = service
        self.bot = bot
        self.ledger = ledger
        self.main_channel_id = main_channel_id
        self.main_channel_url = main_channel_url
        self.ttl = ttl
        self.subscription_check_enabled = subscription_check_enabled
        self.stop = asyncio.Event()

    async def check_admin(self) -> None:
        chat = await self.bot.get_chat(self.main_channel_id)
        member = await self.bot.get_chat_member(
            self.main_channel_id, self.bot.id
        )
        if chat.type != "channel" or member.status != "administrator":
            raise PilotError(
                "Бот должен быть администратором основного канала."
            )

    async def subscribed(self, uid: int) -> bool:
        if not self.subscription_check_enabled:
            return True
        try:
            await self.check_admin()
            member = await self.bot.get_chat_member(self.main_channel_id, uid)
        except (TelegramAPIError, OSError, TimeoutError) as exc:
            logger.warning("Subscription check failed: %s", type(exc).__name__)
            raise PilotError(
                "Не удалось проверить подписку. Попробуйте позже."
            ) from None
        return member.status in SUBSCRIBED

    async def issue(self, uid: int, city: str) -> str | None:
        if uid <= 0:
            raise PilotError("Неизвестный пользователь.")
        # Existing Provisioner still owns all channel/folder mutations.
        await self.service.provision({city: ["weather"]}, create_invites=False)
        async with self.service.lock:
            if not await self.subscribed(uid):
                return None
            await self._cleanup()
            uncertain = self.ledger.db.execute(
                "SELECT 1 FROM invites WHERE status='pending' LIMIT 1"
            ).fetchone()
            if uncertain:
                raise PilotError(
                    "Проверяем результат предыдущей выдачи. Повторите позже."
                )
            folder = self.service.owned_folder(await self.service.folders())
            remote = await self.service.exported(folder)
            limits = await self.service.limits()
            if len(remote) >= limits.values["chatlist_invites_limit"]:
                raise PilotError(
                    "Свободных приглашений пока нет. "
                    "Попробуйте через несколько минут."
                )
            services = self.service.state.invites.get(
                city, InviteState()
            ).services
            peers = self.service.city_peers(city, services)
            identity, title = self.ledger.pending(
                self.service, uid, city, self.ttl
            )
            try:
                result = await self.service.export_invite(title, peers)
            except errors.RPCError as exc:
                if 400 <= exc.code < 500:
                    self.ledger.update(identity, "rejected")
                raise
            # Unknown transport outcomes retain the intent for reconciliation.
            url = result.invite.url
            self.ledger.update(identity, "active", url)
            invite_slug(url)
            if peer_ids(result.invite.peers) != peer_ids(peers):
                with self.ledger.db:
                    self.ledger.db.execute(
                        "UPDATE invites SET expires_at=0 WHERE id=?",
                        (identity,),
                    )
                raise PilotError("Telegram вернул другой состав приглашения.")
            expires = self.ledger.db.execute(
                "SELECT expires_at FROM invites WHERE id=?",
                (identity,),
            ).fetchone()[0]
            if expires <= time.time():
                await self._cleanup()
                raise PilotError("Срок выдачи истёк. Запросите новую ссылку.")
            logger.info(
                "Invite created id=%s city=%s user=%s", identity, city, uid
            )
            return url

    async def _cleanup(self) -> None:
        now = time.time()
        rows = self.ledger.db.execute(
            "SELECT * FROM invites WHERE next_attempt<=? AND "
            "(status='pending' OR (status='active' AND expires_at<=?)) "
            "ORDER BY expires_at LIMIT 100",
            (now, now),
        ).fetchall()
        for row in rows:
            try:
                if row["account_id"] != self.service.state.account_id:
                    raise PilotError("Очередь принадлежит другому аккаунту.")
                chatlist = types.InputChatlistDialogFilter(row["folder_id"])
                url = row["url"]
                if row["status"] == "pending":
                    result = await self.service.rpc(
                        functions.chatlists.GetExportedInvitesRequest(chatlist)
                    )
                    matches = [
                        x for x in result.invites if x.title == row["title"]
                    ]
                    if len(matches) != 1:
                        raise PilotError("Неоднозначный результат экспорта.")
                    url = matches[0].url
                    self.ledger.update(row["id"], "active", url)
                    logger.info("Invite recovered id=%s", row["id"])
                    if row["expires_at"] > time.time():
                        continue
                result = await self.service.rpc(
                    functions.chatlists.DeleteExportedInviteRequest(
                        chatlist=chatlist,
                        slug=invite_slug(url),
                    )
                )
                if result is not True:
                    raise PilotError("Telegram не подтвердил удаление.")
                self.ledger.update(row["id"], "deleted")
                logger.info("Invite deleted id=%s", row["id"])
            except Exception as exc:
                # Telethon 1.44 exposes some slug errors as generic RPCError.
                if isinstance(exc, errors.RPCError) and exc.message in {
                    "INVITE_SLUG_EXPIRED",
                    "INVITE_SLUG_INVALID",
                }:
                    try:
                        exported = await self.service.rpc(
                            functions.chatlists.GetExportedInvitesRequest(
                                chatlist
                            )
                        )
                        if not any(
                            item.url == url for item in exported.invites
                        ):
                            self.ledger.update(row["id"], "deleted")
                            logger.info(
                                "Invite already absent id=%s", row["id"]
                            )
                            continue
                    except Exception as check_error:
                        logger.warning(
                            "Invite reconciliation: %s",
                            type(check_error).__name__,
                        )
                delay = min(30 * 2 ** min(row["attempts"], 5), 900)
                if isinstance(exc, errors.FloodWaitError):
                    delay = max(delay, exc.seconds + 1)
                self.ledger.retry(row, delay)
                logger.warning(
                    "Invite delete/reconcile failed id=%s error=%s",
                    row["id"],
                    type(exc).__name__,
                )

    async def cleanup(self) -> None:
        async with self.service.lock:
            await self._cleanup()

    async def run(self) -> None:
        while not self.stop.is_set():
            try:
                await self.cleanup()
            except Exception as exc:
                logger.error("Invite worker failed: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=1)
            except TimeoutError:
                pass

    async def confirm_missing(self, identity: str) -> None:
        """Confirm an expired request after manual inspection."""
        async with self.service.lock:
            row = self.ledger.db.execute(
                "SELECT * FROM invites WHERE id=?",
                (identity,),
            ).fetchone()
            if (
                row is None
                or row["status"] != "pending"
                or row["account_id"] != self.service.state.account_id
                or row["expires_at"] > time.time()
            ):
                raise PilotError(
                    "Нужна истёкшая pending-запись этого аккаунта."
                )
            folders = await self.service.folders()
            folder = next(
                (f for f in folders if f.id == row["folder_id"]), None
            )
            if folder is None:
                raise PilotError(
                    "Исходная папка не найдена; нужна ручная сверка."
                )
            if isinstance(folder, types.DialogFilterChatlist):
                result = await self.service.rpc(
                    functions.chatlists.GetExportedInvitesRequest(
                        types.InputChatlistDialogFilter(row["folder_id"])
                    )
                )
                if any(item.title == row["title"] for item in result.invites):
                    raise PilotError(
                        "Ссылка найдена. Выполните cleanup-invites."
                    )
            # A normal non-shared DialogFilter cannot have exported links.
            self.ledger.update(identity, "absent_confirmed")
            logger.info("Missing export confirmed by operator id=%s", identity)

    def schedule_legacy(self) -> int:
        """Explicit admin command; never touch unrelated exported links."""
        count = 0
        if self.service.state.folder_id is None:
            return count
        with self.ledger.db:
            for city, saved in self.service.state.invites.items():
                if not saved.url:
                    continue
                invite_slug(saved.url)
                identity = (
                    "old:" + hashlib.sha256(saved.url.encode()).hexdigest()
                )
                result = self.ledger.db.execute(
                    "INSERT OR IGNORE INTO invites(id,account_id,folder_id,"
                    "user_id,city_id,title,url,created_at,expires_at,status) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        identity,
                        self.service.state.account_id,
                        self.service.state.folder_id,
                        0,
                        city,
                        "legacy",
                        saved.url,
                        time.time(),
                        0,
                        "active",
                    ),
                )
                count += result.rowcount
        return count

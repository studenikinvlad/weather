"""Provision city resources through the user account's MTProto session."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any
from urllib.parse import urlparse

from telethon import TelegramClient, errors, functions, types, utils

from city_folders.limits import Limits, decode_json
from city_folders.models import (
    CITIES,
    CURRENCIES,
    FOLDER_TITLE,
    LEGACY_FOLDER_TITLE,
    SERVICE_NAMES,
    ChannelState,
    InviteState,
    PilotError,
    ProjectState,
    ServiceKind,
    channel_key,
)
from city_folders.storage import StateStore

type Folder = types.DialogFilter | types.DialogFilterChatlist


def peer_ids(peers: list[Any]) -> set[int]:
    """Keep Telegram's peer type encoded in each ID."""

    return {utils.get_peer_id(peer) for peer in peers}


def invite_slug(url: str) -> str:
    """Accept only an actual Telegram folder invite returned by the API."""

    parsed = urlparse(url)
    parts = parsed.path.strip("/").split("/")
    if (
        parsed.scheme != "https"
        or parsed.netloc not in {"t.me", "telegram.me"}
        or len(parts) != 2
        or parts[0] != "addlist"
        or not parts[1]
        or parsed.query
        or parsed.fragment
    ):
        raise PilotError("Telegram вернул неожиданный формат ссылки папки.")
    return parts[1]


class Provisioner:
    """One writer per process; the CLI also holds a process-wide file lock.

    Creation intents survive crashes. An uncertain creation is reconciled
    with Telegram before any replacement can be attempted.
    """

    def __init__(
        self,
        client: TelegramClient,
        store: StateStore,
        state: ProjectState,
    ) -> None:
        self.client = client
        self.store = store
        self.state = state
        self.lock = asyncio.Lock()
        self.connection_lock = asyncio.Lock()
        self.me: types.User | None = None
        self.bot_user: types.InputUser | None = None
        self.on_channel_ready: Callable[[str, str], Awaitable[None]] | None = (
            None
        )

    def save(self) -> None:
        self.store.save(self.state)

    def check_cooldown(self, request=None) -> None:
        until = self.state.cooldown_until
        if request is not None:
            until = max(
                until, self.state.rpc_cooldowns.get(type(request).__name__, 0)
            )
        remaining = until - time.time()
        if remaining > 0:
            raise PilotError(
                f"Telegram попросил паузу. Повтори через {int(remaining) + 1} "
                "секунд. Состояние сохранено."
            )

    def remember_flood(self, seconds: int, *, request=None) -> None:
        # Telethon wraps calls when receive_updates=False. Preserve the
        # underlying method so its next call observes the saved pause.
        while isinstance(request, functions.InvokeWithoutUpdatesRequest):
            request = request.query
        until = time.time() + seconds + 1
        if request is None:
            self.state.cooldown_until = max(self.state.cooldown_until, until)
        else:
            name = type(request).__name__
            self.state.rpc_cooldowns[name] = max(
                self.state.rpc_cooldowns.get(name, 0),
                until,
            )
        self.save()

    async def ensure_connection(self) -> None:
        connected = getattr(self.client, "is_connected", None)
        if connected is not None and not connected():
            async with self.connection_lock:
                if not connected():
                    await self.client.connect()

    async def rpc(self, request: Any) -> Any:
        self.check_cooldown(request)
        await self.ensure_connection()
        try:
            return await self.client(request)
        except errors.FloodWaitError as exc:
            self.remember_flood(exc.seconds, request=request)
            raise

    async def bind_account(self) -> None:
        self.check_cooldown()
        me = await self.client.get_me()
        if not isinstance(me, types.User) or me.bot:
            raise PilotError("Для Telethon нужна сессия обычного аккаунта.")
        if self.state.account_id not in {None, me.id}:
            raise PilotError("Эта папка data принадлежит другому аккаунту.")
        self.me = me
        self.state.account_id = me.id
        self.save()

    async def bind_bot(self, bot_id: int, username: str) -> None:
        if self.state.bot_id not in {None, bot_id}:
            raise PilotError("В состоянии сохранён другой бот. Проверь токен.")
        entity = await self.client.get_entity(username)
        if not isinstance(entity, types.User) or not entity.bot:
            raise PilotError(
                "Не удалось найти бота через пользовательский API."
            )
        if entity.id != bot_id or entity.access_hash is None:
            raise PilotError("Данные бота в двух API не совпали.")
        self.bot_user = types.InputUser(entity.id, entity.access_hash)
        self.state.bot_id = bot_id
        self.save()

    async def limits(self) -> Limits:
        if self.me is None:
            raise PilotError("Сначала нужно проверить аккаунт.")
        result = await self.rpc(functions.help.GetAppConfigRequest(hash=0))
        config = (
            decode_json(result.config) if hasattr(result, "config") else {}
        )
        if not isinstance(config, dict):
            raise PilotError("Неверный формат конфигурации Telegram.")
        return Limits.from_config(config, bool(self.me.premium))

    async def folders(self) -> list[Folder]:
        result = await self.rpc(functions.messages.GetDialogFiltersRequest())
        return [
            folder
            for folder in result.filters
            if isinstance(
                folder, (types.DialogFilter, types.DialogFilterChatlist)
            )
        ]

    def owned_folder(self, folders: list[Folder]) -> Folder | None:
        folder = next(
            (item for item in folders if item.id == self.state.folder_id),
            None,
        )
        if folder is None:
            if self.state.folder_ready:
                raise PilotError(
                    "Исходная папка исчезла. Не создаю замену: "
                    "старые приглашения требуют отдельного восстановления."
                )
            return None
        if folder.title.text not in {
            FOLDER_TITLE,
            LEGACY_FOLDER_TITLE,
            self.temporary_folder_title,
        }:
            raise PilotError(
                "Название исходной папки изменено или её ID занят. "
                "Проверь папку в Telegram перед продолжением."
            )
        actual = peer_ids(folder.include_peers + folder.pinned_peers)
        known = peer_ids(self.all_peers())
        known.update(
            -1000000000000 - item.channel_id
            for item in self.state.retired_weather_channels.values()
            if item.channel_id is not None
        )
        if self.state.bot_id:
            known.add(self.state.bot_id)
        if actual - known or not actual:
            raise PilotError(
                "Состав исходной папки изменён вне проекта. "
                "Автоматическая перезапись остановлена."
            )
        if isinstance(folder, types.DialogFilter):
            flags = (
                "contacts",
                "non_contacts",
                "groups",
                "broadcasts",
                "bots",
                "exclude_muted",
                "exclude_read",
                "exclude_archived",
            )
            if folder.exclude_peers or any(
                getattr(folder, key) for key in flags
            ):
                raise PilotError("В исходной папке появились правила отбора.")
        return folder

    @property
    def temporary_folder_title(self) -> str:
        return f"CF{self.state.project_id[:10]}"

    def invite_title(self, city_id: str) -> str:
        # This label is visible only to the account that exports the link.
        return f"cfp:{self.state.project_id}:{city_id}"

    def chatlist(self) -> types.InputChatlistDialogFilter:
        if self.state.folder_id is None:
            raise PilotError("Исходная папка ещё не создана.")
        return types.InputChatlistDialogFilter(self.state.folder_id)

    async def exported(self, folder: Folder | None) -> list[Any]:
        if not isinstance(folder, types.DialogFilterChatlist):
            return []
        result = await self.rpc(
            functions.chatlists.GetExportedInvitesRequest(self.chatlist())
        )
        return result.invites

    def find_invite(self, invites: list[Any], city_id: str) -> Any | None:
        saved = self.state.invites.get(city_id)
        matches = [
            item
            for item in invites
            if item.title == self.invite_title(city_id)
            or (saved is not None and saved.url == item.url)
        ]
        if len(matches) > 1:
            raise PilotError(f"Найдено несколько приглашений: {city_id}.")
        if saved and saved.url and not matches:
            raise PilotError(
                f"Приглашение {city_id} удалено. Новую ссылку не создаю."
            )
        return matches[0] if matches else None

    def all_peers(self) -> list[types.InputPeerChannel]:
        return [
            types.InputPeerChannel(item.channel_id, item.access_hash)
            for item in self.state.channels.values()
            if item.channel_id is not None and item.access_hash is not None
        ]

    def city_peers(
        self, city_id: str, services: list[ServiceKind]
    ) -> list[types.InputPeerChannel]:
        peers = []
        for kind in services:
            item = self.state.channels.get(channel_key(city_id, kind))
            if (
                item is None
                or item.channel_id is None
                or item.access_hash is None
                or not item.ready
            ):
                raise PilotError(f"Канал {city_id}:{kind} ещё не готов.")
            peers.append(
                types.InputPeerChannel(item.channel_id, item.access_hash)
            )
        return peers

    async def reconcile_channels(self) -> list[Any]:
        """Recover only an exact temporary title owned by this account."""

        self.check_cooldown()
        await self.ensure_connection()
        dialogs = [dialog async for dialog in self.client.iter_dialogs()]
        for item in self.state.channels.values():
            if not item.pending or item.channel_id is not None:
                continue
            matches = [
                dialog.entity
                for dialog in dialogs
                if isinstance(dialog.entity, types.Channel)
                and dialog.entity.creator
                and dialog.entity.title == item.temporary_title
            ]
            if len(matches) != 1 or matches[0].access_hash is None:
                raise PilotError(
                    "Результат создания канала неизвестен. Повторный запуск "
                    "не нашёл единственный канал с временным названием "
                    f"{item.temporary_title}. "
                    "См. раздел восстановления README."
                )
            item.channel_id = matches[0].id
            item.access_hash = matches[0].access_hash
            item.pending = False
            self.save()
        return dialogs

    async def preflight(
        self,
        desired: dict[str, list[ServiceKind]],
        *,
        invites_needed: bool = True,
    ) -> None:
        """Check the complete requested batch before creating any resources."""

        limits = await self.limits()
        dialogs = await self.reconcile_channels()
        folders = await self.folders()
        own = self.owned_folder(folders)
        invites = await self.exported(own)
        extra_links = 0
        for city_id in desired if invites_needed else ():
            existing = self.find_invite(invites, city_id)
            saved = self.state.invites.get(city_id)
            if saved and saved.pending and existing is None:
                raise PilotError(
                    f"Результат экспорта {city_id} неизвестен. "
                    "Повторный экспорт остановлен; см. README."
                )
            extra_links += existing is None
        shared = sum(
            isinstance(f, types.DialogFilterChatlist) for f in folders
        )
        limits.require(
            "dialog_filters_limit", len(folders) + (own is None), "все папки"
        )
        limits.require(
            "chatlists_joined_limit",
            shared + (not isinstance(own, types.DialogFilterChatlist)),
            "общие папки (предварительная оценка)",
        )
        if invites_needed:
            limits.require(
                "chatlist_invites_limit",
                len(invites) + extra_links,
                "приглашения исходной папки",
            )
        desired_keys = {
            channel_key(city_id, kind)
            for city_id, services in desired.items()
            for kind in services
        }
        known_keys = {
            key
            for key, item in self.state.channels.items()
            if item.channel_id is not None
        }
        new_count = len(desired_keys - known_keys)
        limits.require(
            "dialog_filters_chats_limit",
            len(known_keys | desired_keys) + 1,
            "каналы внутри исходной папки",
        )
        joined = sum(dialog.is_channel for dialog in dialogs)
        limits.require(
            "channels_limit", joined + new_count, "участие в каналах"
        )

    async def ensure_channel(self, city_id: str, kind: ServiceKind) -> None:
        if self.bot_user is None:
            raise PilotError("Сначала нужно проверить токен бота.")
        key = channel_key(city_id, kind)
        item = self.state.channels.setdefault(
            key,
            ChannelState(temporary_title=f"CFP {self.state.project_id} {key}"),
        )
        if item.ready:
            if self.on_channel_ready is not None:
                await self.on_channel_ready(city_id, kind)
            return
        if item.channel_id is None:
            if item.pending:
                raise PilotError(
                    "Сначала нужно восстановить состояние канала."
                )
            item.pending = True
            self.save()
            try:
                result = await self.rpc(
                    functions.channels.CreateChannelRequest(
                        title=item.temporary_title,
                        about="Городские каналы проекта «Мой город».",
                        broadcast=True,
                        megagroup=False,
                    )
                )
            except errors.RPCError as exc:
                # A 4xx RPC rejects the operation; transport/5xx is uncertain.
                if 400 <= exc.code < 500:
                    item.pending = False
                    self.save()
                raise
            channels = [
                chat
                for chat in getattr(result, "chats", [])
                if isinstance(chat, types.Channel)
                and chat.title == item.temporary_title
                and chat.creator
            ]
            if len(channels) != 1 or channels[0].access_hash is None:
                raise PilotError(
                    "Создание отправлено. Повтори setup для сверки."
                )
            item.channel_id = channels[0].id
            item.access_hash = channels[0].access_hash
            item.pending = False
            self.save()
        if item.access_hash is None:
            raise PilotError(
                "Для сохранённого канала отсутствует access_hash."
            )
        channel = types.InputChannel(item.channel_id, item.access_hash)
        await self.rpc(
            functions.channels.EditAdminRequest(
                channel=channel,
                user_id=self.bot_user,
                admin_rights=types.ChatAdminRights(
                    post_messages=True,
                    edit_messages=True,
                    change_info=True,
                    delete_messages=True,
                ),
            )
        )
        if kind == "rates":
            title = f"{CURRENCIES[city_id]} · snowaa"
        else:
            title = (
                f"{CITIES[city_id].name} · snowaa"
                if kind == "weather"
                else f"{CITIES[city_id].name} · {SERVICE_NAMES[kind]}"
            )
        try:
            await self.rpc(functions.channels.EditTitleRequest(channel, title))
        except errors.ChatNotModifiedError:
            pass
        item.ready = True
        self.save()
        if self.on_channel_ready is not None:
            await self.on_channel_ready(city_id, kind)

    async def ensure_folder(self) -> Folder:
        folders = await self.folders()
        existing = self.owned_folder(folders)
        peers = self.all_peers()
        if not peers:
            raise PilotError("Для папки нужен хотя бы один канал.")
        if self.bot_user is not None:
            peers.append(
                types.InputPeerUser(
                    self.bot_user.user_id,
                    self.bot_user.access_hash,
                )
            )
        if self.state.folder_id is None:
            occupied = {folder.id for folder in folders}
            free = next((i for i in range(2, 256) if i not in occupied), None)
            if free is None:
                raise PilotError("Не найден свободный ID для папки.")
            self.state.folder_id = free
            self.save()
        if existing is None:
            existing = types.DialogFilter(
                id=self.state.folder_id,
                title=types.TextWithEntities(self.temporary_folder_title, []),
                pinned_peers=[],
                include_peers=peers,
                exclude_peers=[],
            )
            await self.rpc(
                functions.messages.UpdateDialogFilterRequest(
                    id=existing.id,
                    filter=existing,
                )
            )
            self.state.folder_ready = True
            self.save()
        # Keep DialogFilterChatlist after export: converting it back can
        # detach shared-folder semantics. Preserve all existing attributes.
        updated = deepcopy(existing)
        if existing.title.text == self.temporary_folder_title:
            updated.title = types.TextWithEntities(FOLDER_TITLE, [])
        included = peer_ids(updated.include_peers + updated.pinned_peers)
        updated.include_peers.extend(
            peer for peer in peers if utils.get_peer_id(peer) not in included
        )
        if bytes(updated) != bytes(existing):
            await self.rpc(
                functions.messages.UpdateDialogFilterRequest(
                    id=updated.id,
                    filter=updated,
                )
            )
        self.state.folder_ready = True
        self.save()
        return updated

    async def export_invite(self, title: str, peers: list):
        """Shared MTProto export adapter."""
        return await self.rpc(
            functions.chatlists.ExportChatlistInviteRequest(
                chatlist=self.chatlist(),
                title=title,
                peers=peers,
            )
        )

    async def ensure_invite(
        self, city_id: str, services: list[ServiceKind]
    ) -> str:
        folder = self.owned_folder(await self.folders())
        invites = await self.exported(folder)
        existing = self.find_invite(invites, city_id)
        item = self.state.invites.setdefault(city_id, InviteState())
        peers = self.city_peers(city_id, services)
        if existing is None:
            if item.pending:
                raise PilotError("Результат экспорта неизвестен; см. README.")
            item.pending = True
            self.save()
            try:
                result = await self.export_invite(
                    self.invite_title(city_id), peers
                )
            except errors.RPCError as exc:
                if 400 <= exc.code < 500:
                    item.pending = False
                    self.save()
                raise
            existing = result.invite
            item.url = existing.url
            self.save()
        if peer_ids(existing.peers) != peer_ids(peers):
            existing = await self.rpc(
                functions.chatlists.EditExportedInviteRequest(
                    chatlist=self.chatlist(),
                    slug=invite_slug(existing.url),
                    peers=peers,
                )
            )
        invite_slug(existing.url)
        if peer_ids(existing.peers) != peer_ids(peers):
            raise PilotError("Telegram вернул другой состав приглашения.")
        item.url = existing.url
        item.pending = False
        item.services = services.copy()
        self.save()
        return existing.url

    async def provision(
        self,
        desired: dict[str, list[ServiceKind]],
        *,
        create_invites: bool = True,
    ) -> dict[str, str]:
        async with self.lock:
            if self.me is None or self.bot_user is None:
                raise PilotError("Аккаунт и бот должны быть проверены.")
            if any(
                not (
                    (
                        key in CITIES
                        and all(k in {"weather", "news"} for k in kinds)
                    )
                    or (key in CURRENCIES and kinds == ["rates"])
                )
                for key, kinds in desired.items()
            ):
                raise PilotError("Выбери город из каталога проекта.")
            # A normal city request must never remove an added news channel.
            desired = {
                city_id: list(
                    dict.fromkeys(
                        self.state.invites.get(
                            city_id, InviteState(services=[])
                        ).services
                        + services
                    )
                )
                for city_id, services in desired.items()
            }
            await self.preflight(desired, invites_needed=create_invites)
            for city_id, services in desired.items():
                for kind in services:
                    await self.ensure_channel(city_id, kind)
            await self.ensure_folder()
            if not create_invites:
                for city_id, services in desired.items():
                    item = self.state.invites.setdefault(
                        city_id, InviteState()
                    )
                    item.services = services
                self.save()
                return {}
            return {
                city_id: await self.ensure_invite(city_id, services)
                for city_id, services in desired.items()
            }

    async def city_link(self, city_id: str) -> str:
        # Existing users cost no additional MTProto calls or invite slots.
        # `verify` is the explicit remote audit after manual Telegram edits.
        saved = self.state.invites.get(city_id)
        if saved and saved.url and not saved.pending:
            return saved.url
        return (await self.provision({city_id: ["weather"]}))[city_id]

    async def verify(self) -> list[str]:
        """Read remote links without creating or modifying Telegram data."""

        folder = self.owned_folder(await self.folders())
        if folder is None:
            raise PilotError("Сначала выполни setup или выбери город в боте.")
        exported = await self.exported(folder)
        report = []
        for city_id, saved in self.state.invites.items():
            remote = self.find_invite(exported, city_id)
            if remote is None or saved.pending or remote.url != saved.url:
                raise PilotError(f"Приглашение {city_id} ещё не подтверждено.")
            expected = self.city_peers(city_id, saved.services)
            if peer_ids(remote.peers) != peer_ids(expected):
                raise PilotError(f"Состав приглашения {city_id} отличается.")
            report.append(
                f"{CITIES[city_id].name}: ссылка совпадает, "
                f"каналов {len(expected)}."
            )
        return report

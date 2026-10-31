"""Resumable replacement of recorded project channels and folder."""

import asyncio
import json
import sqlite3
import time
from copy import deepcopy

from telethon import errors, functions, types

from city_folders.models import PilotError, ProjectState
from city_folders.service import Provisioner, peer_ids
from city_folders.storage import StateStore
from city_folders.weather import Forecast


def save_journal(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


async def rebuild(
    service, bot, weather, rates, ledger, directory, *, existing_only=False
):
    directory.mkdir(parents=True, exist_ok=True)
    journal_path = directory / "journal.json"
    old_store = StateStore(directory / "old-state.json")
    new_store = StateStore(directory / "new-state.json")
    if not journal_path.exists():
        # Verify recorded resources before preparing any destructive operation.
        service.owned_folder(await service.folders())
        channels = [c for c in service.state.channels.values() if c.channel_id]
        result = await service.rpc(
            functions.channels.GetChannelsRequest(
                [
                    types.InputChannel(c.channel_id, c.access_hash)
                    for c in channels
                ]
            )
        )
        owned = {
            c.id
            for c in result.chats
            if isinstance(c, types.Channel) and c.creator
        }
        if owned != {c.channel_id for c in channels}:
            raise PilotError(
                "Не подтверждено владение всеми каналами проекта."
            )
        old_store.save(service.state)
        with sqlite3.connect(directory / "catalogue-backup.sqlite3") as backup:
            ledger.db.backup(backup)
        fresh = ProjectState(
            account_id=service.state.account_id,
            bot_id=service.state.bot_id,
            cities=deepcopy(service.state.cities),
        )
        new_store.save(fresh)
        journal = {
            "phase": "creating",
            "deleted_channels": [],
            "folder_deleted": False,
        }
        save_journal(journal_path, journal)
    else:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    if journal["phase"] == "complete":
        print("Пересоздание уже завершено.", flush=True)
        return
    old = old_store.load()
    fresh = (
        service.store.load() if journal.get("activated") else new_store.load()
    )
    if existing_only:
        journal["existing_only"] = True
        save_journal(journal_path, journal)
    if journal.get("existing_only"):
        # Keep any old resource with no replacement; never create another chat.
        # Already allocated replacement IDs may still need rights or titles.
        for key, channel in old.channels.items():
            current = fresh.channels.get(key)
            if current is None or current.channel_id is None:
                if current is not None and current.pending:
                    raise PilotError(
                        "Сначала нужно сверить незавершённое создание."
                    )
                fresh.channels[key] = deepcopy(channel)
                city, kind = key.rsplit(":", 1)
                if kind == "weather" and city in old.weather:
                    fresh.weather[city] = deepcopy(old.weather[city])
                if kind == "rates" and city in old.rates:
                    fresh.rates[city] = deepcopy(old.rates[city])
        new_store.save(fresh)
    if old.account_id != service.state.account_id or fresh.bot_id != bot.id:
        raise PilotError("Аккаунт или бот не совпадает с планом пересоздания.")
    replacement = Provisioner(service.client, new_store, fresh)
    replacement.me = service.me
    replacement.bot_user = service.bot_user
    replacement.on_channel_ready = weather.channel_ready
    weather.service = replacement
    rates.service = replacement
    desired = {}
    for key in old.channels:
        city, kind = key.rsplit(":", 1)
        desired.setdefault(city, []).append(kind)
    if journal["phase"] == "creating":
        if (await bot.get_my_name()).name != "Настройки":
            await bot.set_my_name(name="Настройки")
        if journal.get("existing_only"):
            if any(c.channel_id is None for c in fresh.channels.values()):
                raise PilotError("Не подтверждены ID существующих чатов.")
            print("Завершаю перенос на уже существующих чатах.", flush=True)
        else:
            print(f"Создаю замену: {len(old.channels)} каналов.", flush=True)
        while True:
            try:
                await replacement.provision(desired, create_invites=False)
                break
            except errors.FloodWaitError as exc:
                if exc.seconds >= 60:
                    raise
                print(f"Пауза Telegram: {exc.seconds + 1} секунд.", flush=True)
                await asyncio.sleep(exc.seconds + 1)
        for city, kinds in desired.items():
            if "weather" in kinds:
                post = fresh.weather.get(city)
                if post and post.failures and post.next_update > time.time():
                    remaining = post.next_update - time.time()
                    if remaining > 59:
                        raise PilotError(
                            "Ожидается повтор прогноза; старые чаты сохранены."
                        )
                    await asyncio.sleep(remaining + 1)
                if not await weather.update(city):
                    raise PilotError(
                        "Новый прогноз не готов; старые каналы сохранены."
                    )
                if not fresh.weather[city].last_forecast:
                    raise PilotError("Новый прогноз ещё не опубликован.")
                await weather.avatar(
                    city,
                    Forecast(
                        **snapshot_payload(fresh.weather[city].last_forecast)
                    ),
                    fresh.weather[city],
                )
            if "rates" in kinds:
                post = fresh.rates.get(city)
                if post and post.failures and post.next_update > time.time():
                    remaining = post.next_update - time.time()
                    if remaining > 59:
                        raise PilotError(
                            "Ожидается повтор курса; старые чаты сохранены."
                        )
                    await asyncio.sleep(remaining + 1)
                if not await rates.update(city):
                    raise PilotError(
                        "Новый валютный канал не готов; "
                        "старые каналы сохранены."
                    )
                post = fresh.rates.get(city)
                if (
                    not post
                    or not post.last_success
                    or not post.brief.message_id
                ):
                    raise PilotError("Новый курс ещё не опубликован.")
        for channel in fresh.channels.values():
            member = await bot.get_chat_member(
                chat_id=-1000000000000 - channel.channel_id,
                user_id=bot.id,
            )
            if not getattr(member, "can_change_info", False):
                raise PilotError("Не подтверждены права бота в новом канале.")
        journal["phase"] = "retiring"
        save_journal(journal_path, journal)
        print(
            "Новые каналы готовы. Удаляю старые ресурсы проекта.", flush=True
        )
    # Only IDs from the verified backup are eligible for deletion.
    for channel in old.channels.values():
        if channel.channel_id in journal["deleted_channels"]:
            continue
        if channel.channel_id in {
            c.channel_id for c in fresh.channels.values()
        }:
            continue
        dialogs = [d.entity async for d in service.client.iter_dialogs()]
        remote = next(
            (
                c
                for c in dialogs
                if isinstance(c, types.Channel) and c.id == channel.channel_id
            ),
            None,
        )
        if remote is not None:
            if not remote.creator:
                raise PilotError("Владелец старого канала изменился.")
            await service.rpc(
                functions.channels.DeleteChannelRequest(
                    types.InputChannel(
                        channel.channel_id, channel.access_hash
                    ),
                )
            )
        journal["deleted_channels"].append(channel.channel_id)
        save_journal(journal_path, journal)
    if not journal["folder_deleted"]:
        folder = next(
            (f for f in await service.folders() if f.id == old.folder_id), None
        )
        if folder is not None:
            expected = {
                -1000000000000 - c.channel_id for c in old.channels.values()
            }
            if old.bot_id:
                expected.add(old.bot_id)
            if peer_ids(folder.include_peers + folder.pinned_peers) - expected:
                raise PilotError("В старую папку добавлены посторонние чаты.")
            await service.rpc(
                functions.messages.UpdateDialogFilterRequest(id=old.folder_id)
            )
        journal["folder_deleted"] = True
        save_journal(journal_path, journal)
    with ledger.db:
        ledger.db.execute(
            "UPDATE invites SET status='deleted' "
            "WHERE account_id=? AND folder_id=?",
            (old.account_id, old.folder_id),
        )
    service.store.save(fresh)
    journal["phase"] = "complete"
    save_journal(journal_path, journal)
    print(
        f"Готово: {len(fresh.channels)} чатов проекта, "
        f"папка {fresh.folder_id}.",
        flush=True,
    )


def snapshot_payload(snapshot):
    from datetime import datetime

    payload = dict(snapshot)
    for field in ("brief_plain", "current_plain", "daily_plain"):
        payload.pop(field, None)
    payload["local_time"] = datetime.fromisoformat(payload["local_time"])
    return payload


def activate_ready_rebuild(store, directory):
    """Switch to verified replacements while old-resource cleanup is paused."""
    journal_path = directory / "journal.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    if journal["phase"] != "retiring":
        raise PilotError("Новые чаты ещё не готовы к запуску.")
    if journal.get("activated"):
        return store.load()
    current = store.load()
    fresh = StateStore(directory / "new-state.json").load()
    if (current.account_id, current.bot_id) != (
        fresh.account_id,
        fresh.bot_id,
    ):
        raise PilotError("Аккаунт или бот переноса изменился.")
    for method, until in current.rpc_cooldowns.items():
        # Older Telethon recorded the wrapper for the paused retirement call.
        if method == "InvokeWithoutUpdatesRequest":
            method = "DeleteChannelRequest"
        fresh.rpc_cooldowns[method] = max(
            fresh.rpc_cooldowns.get(method, 0), until
        )
    fresh.cooldown_until = max(fresh.cooldown_until, current.cooldown_until)
    store.save(fresh)
    journal["activated"] = True
    save_journal(journal_path, journal)
    return fresh

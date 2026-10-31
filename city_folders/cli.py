"""Local commands: credentials and Telegram sessions stay on your computer."""

import argparse
import asyncio
import getpass
import logging
import os
import sys
import time
from pathlib import Path

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramAPIError
from filelock import FileLock, Timeout
from telethon import TelegramClient, errors, types, utils

from city_folders.artwork import Artwork
from city_folders.bot import run_bot
from city_folders.config import Settings
from city_folders.models import CITIES, CURRENCIES, PilotError, channel_key
from city_folders.network import PROXY, telegram_proxy, xray_tunnel
from city_folders.publisher import http_session
from city_folders.rates import RateSource, RatesPublisher
from city_folders.service import Provisioner
from city_folders.storage import StateStore
from city_folders.temporary_invites import InviteLedger, TemporaryInvites
from city_folders.weather import OpenMeteo
from city_folders.widget_publisher import WidgetPublisher


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Прототип Telegram-папки для Казани, Самары и Хабаровска."
    )
    result.add_argument("--env-file", type=Path, default=Path(".env"))
    commands = result.add_subparsers(dest="command", required=True)
    assets = commands.add_parser(
        "import-assets", help="Импорт погодных картинок"
    )
    assets.add_argument("archive", type=Path)
    commands.add_parser(
        "enable-emoji", help="Повторить проверку custom emoji в каналах"
    )
    commands.add_parser("login", help="Войти в аккаунт локально")
    commands.add_parser("inspect", help="Прочитать лимиты и занятые места")
    commands.add_parser(
        "setup", help="Подготовить каналы городов без приглашений"
    )
    commands.add_parser("run", help="Запустить бота и обновление погоды")
    rebuilding = commands.add_parser(
        "rebuild", help="Пересоздать каналы и папку проекта"
    )
    rebuilding.add_argument("--existing-only", action="store_true")
    commands.add_parser("setup-rates", help="Подготовить четыре канала валют")
    weather = commands.add_parser("weather-now", help="Обновить погоду сейчас")
    weather.add_argument("--city")
    commands.add_parser(
        "links", help="Показать статистику очереди приглашений"
    )
    commands.add_parser("verify", help="Проверить основной канал и папку")
    commands.add_parser("cleanup-invites", help="Удалить просроченные ссылки")
    commands.add_parser(
        "expire-legacy-links", help="Удалить старые постоянные ссылки проекта"
    )
    missing = commands.add_parser(
        "confirm-missing-invite", help="Подтвердить отсутствие pending-ссылки"
    )
    missing.add_argument("--id", required=True)
    news = commands.add_parser(
        "add-news", help="Добавить тестовый канал новостей"
    )
    news.add_argument("--city", choices=CITIES, required=True)
    community = commands.add_parser(
        "community", help="Прочитать Community каналов через Bot API"
    )
    community.add_argument("--city", choices=CITIES, required=True)
    return result


async def interactive_login(client: TelegramClient, phone: str) -> None:
    if await client.is_user_authorized():
        print("Сессия уже авторизована.")
        return
    if not phone:
        phone = await asyncio.to_thread(input, "Телефон с кодом страны: ")
    sent = await client.send_code_request(phone)
    code = await asyncio.to_thread(getpass.getpass, "Код Telegram: ")
    try:
        await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
    except errors.SessionPasswordNeededError:
        password = await asyncio.to_thread(getpass.getpass, "Пароль 2FA: ")
        await client.sign_in(password=password)
    print("Вход выполнен. Сессия сохранена локально.")


async def inspect_account(service: Provisioner) -> None:
    limits = await service.limits()
    folders = await service.folders()
    shared = sum(isinstance(f, types.DialogFilterChatlist) for f in folders)
    joined = sum(
        [dialog.is_channel async for dialog in service.client.iter_dialogs()]
    )
    print(f"Premium: {'да' if service.me and service.me.premium else 'нет'}")
    print(f"Папок сейчас: {len(folders)}, общих: {shared}.")
    print(f"Участие в каналах и супергруппах: {joined}.")
    for key, value in limits.values.items():
        source = (
            "документированное значение*"
            if key in limits.fallback_keys
            else "API"
        )
        print(f"{key}: {value} ({source})")
    if limits.fallback_keys:
        print(
            "* Ключ отсутствует в API; "
            "использован запасной лимит из документации."
        )
    print("Окончательное решение принимает Telegram при выполнении операции.")


async def inspect_community(
    bot: Bot, service: Provisioner, city_id: str
) -> None:
    found = False
    for kind in ("weather", "news"):
        item = service.state.channels.get(channel_key(city_id, kind))
        if item is None or item.channel_id is None:
            continue
        found = True
        chat_id = utils.get_peer_id(types.PeerChannel(item.channel_id))
        chat = await bot.get_chat(chat_id)
        if chat.community is None:
            print(f"{kind}: Community не возвращена Bot API.")
        else:
            print(f"{kind}: {chat.community.name}, ID {chat.community.id}")
    if not found:
        raise PilotError("У этого города пока нет каналов проекта.")


async def execute(args: argparse.Namespace, settings: Settings) -> None:
    if args.command == "enable-emoji":
        artwork = Artwork(settings.data_dir / "folder_invites.sqlite3")
        try:
            artwork.set_enabled(True)
        finally:
            artwork.close()
        print("Проверка эмодзи включена для следующего обновления.")
        return
    if args.command == "import-assets":
        artwork = Artwork(settings.data_dir / "folder_invites.sqlite3")
        try:
            print(f"Картинок: {artwork.import_zip(args.archive)}")
        finally:
            artwork.close()
        return
    store = StateStore(settings.data_dir / "state.json")
    state = store.load()
    if args.command == "rebuild":
        raise PilotError(
            "Старый перенос отключён. "
            "Супергруппы удаляются владельцем вручную."
        )
    if args.command == "links":
        ledger = InviteLedger(settings.data_dir / "folder_invites.sqlite3")
        try:
            for row in ledger.db.execute(
                "SELECT status,count(*) FROM invites GROUP BY status"
            ):
                print(f"{row[0]}: {row[1]}")
            print("Ссылки выдаются только ботом после проверки подписки.")
        finally:
            ledger.close()
        return
    client = TelegramClient(
        str(settings.data_dir / "user"),
        settings.api_id,
        settings.api_hash,
        request_retries=0,
        connection_retries=5,
        retry_delay=2,
        auto_reconnect=True,
        flood_sleep_threshold=0,
        raise_last_call_error=True,
        receive_updates=False,
        proxy=telegram_proxy(),
    )
    service = Provisioner(client, store, state)
    bot: Bot | None = None
    weather_http = None
    artwork = None
    ledger = None
    try:
        if args.command == "run":
            remaining = state.cooldown_until - time.time()
            if remaining > 0:
                logging.warning(
                    "Startup will resume after Telegram pause: %s seconds",
                    int(remaining) + 1,
                )
                await asyncio.sleep(remaining + 1)
        service.check_cooldown()
        await client.connect()
        if args.command == "login":
            await interactive_login(client, settings.phone)
            await service.bind_account()
            return
        if not await client.is_user_authorized():
            raise PilotError("Сначала выполни python -m city_folders login.")
        await service.bind_account()
        if args.command == "inspect":
            await inspect_account(service)
            return
        if args.command in {
            "cleanup-invites",
            "expire-legacy-links",
            "confirm-missing-invite",
        }:
            ledger = InviteLedger(settings.data_dir / "folder_invites.sqlite3")
            # Cleanup deliberately needs only the owning user session.
            temporary = TemporaryInvites(service, None, ledger, 0, "")
            if args.command == "confirm-missing-invite":
                await temporary.confirm_missing(args.id)
            if args.command == "expire-legacy-links":
                print(f"Запланировано: {temporary.schedule_legacy()}")
            await temporary.cleanup()
            print("Очередь обработана; ошибки повторятся при следующем run.")
            return
        if not settings.bot_token:
            raise PilotError("Для этой команды заполни BOT_TOKEN в .env.")
        bot = Bot(
            settings.bot_token, session=AiohttpSession(proxy=PROXY.get())
        )
        bot_me = await bot.get_me()
        if bot_me.username is None:
            raise PilotError("У бота отсутствует username.")
        await service.bind_bot(bot_me.id, bot_me.username)
        ledger = InviteLedger(settings.data_dir / "folder_invites.sqlite3")
        temporary = TemporaryInvites(
            service,
            bot,
            ledger,
            settings.main_channel_id,
            settings.main_channel_url,
            settings.folder_invite_ttl_seconds,
            subscription_check_enabled=settings.subscription_check_enabled,
        )
        # Recover expired links even if main-channel configuration is broken.
        await temporary.cleanup()
        if args.command in {"run", "verify"}:
            settings.validate_subscription()
            if settings.subscription_check_enabled:
                if args.command == "verify":
                    await temporary.check_admin()
                else:
                    try:
                        await temporary.check_admin()
                    except (PilotError, TelegramAPIError):
                        logging.warning(
                            "Main-channel admin access unavailable; "
                            "invitation issuance remains blocked."
                        )
            else:
                print("Проверка подписки временно отключена.")
        if args.command == "verify":
            service.owned_folder(await service.folders())
            print("Проверка настройки проекта завершена.")
            return
        from city_folders.channel_mode import retire_weather_groups

        await retire_weather_groups(service, bot, ledger)
        weather_http = http_session()
        artwork = Artwork(settings.data_dir / "folder_invites.sqlite3")
        if not artwork.available():
            raise PilotError(
                "Сначала выполни import-assets с архивом картинок."
            )
        weather = WidgetPublisher(
            bot, service, OpenMeteo(weather_http), artwork, bot_me.username
        )
        service.on_channel_ready = weather.channel_ready
        rates = RatesPublisher(bot, service, RateSource(weather_http))
        if args.command == "setup-rates":
            await service.provision(
                {code: ["rates"] for code in CURRENCIES}, create_invites=False
            )
            results = [
                await rates.update(code, force=True) for code in CURRENCIES
            ]
            if not all(results):
                raise PilotError("Часть курсов ожидает повторного обновления.")
            print("Четыре канала валют готовы.")
            return
        if args.command == "weather-now":
            results = [
                await weather.update(city_id, force=True)
                for city_id in CITIES
                if channel_key(city_id, "weather") in state.channels
                and (not args.city or city_id == args.city)
            ]
            if not results or not all(results):
                raise PilotError(
                    "Не все погодные каналы обновлены; см. вывод."
                )
            print("Погодные публикации обновлены.")
            return
        if args.command == "run":
            # Do not delete a webhook owned by another deployment silently.
            webhook = await bot.get_webhook_info()
            if webhook.url:
                raise PilotError(
                    "У бота установлен webhook. "
                    "Останови прежнее развёртывание и отключи webhook "
                    "перед запуском polling."
                )
            print(f"Бот @{bot_me.username} запущен. Остановка: Ctrl+C.")
            await run_bot(
                bot,
                service,
                settings.allowed_user_ids,
                weather,
                temporary,
                rates,
            )
        elif args.command == "community":
            await inspect_community(bot, service, args.city)
        else:
            desired = (
                {city_id: ["weather"] for city_id in CITIES}
                if args.command == "setup"
                else {args.city: ["weather", "news"]}
            )
            await service.provision(desired, create_invites=False)
            print("Каналы подготовлены. Приглашения выдаются через бота.")
    except errors.FloodWaitError as exc:
        service.remember_flood(exc.seconds, request=exc.request)
        raise PilotError(
            f"Telegram требует паузу {exc.seconds} секунд. "
            "Дождись её окончания и повтори команду."
        ) from exc
    finally:
        try:
            if artwork is not None:
                artwork.close()
            if ledger is not None:
                ledger.close()
            if weather_http is not None:
                await weather_http.close()
            if bot is not None:
                await bot.session.close()
        finally:
            await client.disconnect()


async def execute_connected(args, settings):
    if args.command in {"links", "import-assets", "enable-emoji"}:
        await execute(args, settings)
        return
    async with xray_tunnel(settings.data_dir):
        await execute(args, settings)


def main() -> None:
    args = parser().parse_args()
    if os.name == "posix":
        os.umask(0o077)
    logging.basicConfig(
        level=logging.WARNING, format="%(levelname)s %(message)s"
    )
    logging.getLogger("city_folders.publisher").setLevel(logging.INFO)
    logging.getLogger("city_folders.temporary_invites").setLevel(logging.INFO)
    # Telethon may include request metadata in internal error messages.
    logging.getLogger("telethon").setLevel(logging.CRITICAL)
    try:
        settings = Settings.load(args.env_file)
        with FileLock(str(settings.data_dir / "process.lock"), timeout=0):
            asyncio.run(execute_connected(args, settings))
    except Timeout:
        print(
            "Проект уже запущен. Останови run перед другой командой.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except PilotError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except (errors.RPCError, TelegramAPIError, OSError, ValueError) as exc:
        print(
            f"Операция остановлена: {type(exc).__name__}. "
            "Проверь настройки и раздел ошибок README. "
            "Не удаляй каталог data.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print("Остановлено.")

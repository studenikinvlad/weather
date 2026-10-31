"""Detach old weather groups locally without modifying Telegram chats."""

from aiogram.exceptions import TelegramBadRequest


async def retire_weather_groups(service, bot, ledger):
    state = service.state
    for key, channel in list(state.channels.items()):
        if not key.endswith(":weather") or channel.channel_id is None:
            continue
        try:
            chat = await bot.get_chat(-1000000000000 - channel.channel_id)
        except TelegramBadRequest as exc:
            if "chat not found" not in exc.message.lower():
                raise
            chat = None
        if chat is not None and chat.type == "channel":
            continue
        city = key.rsplit(":", 1)[0]
        archive_key = str(channel.channel_id)
        # Invalidate cached invitations before detaching the old binding.
        # No chat, message, folder or invitation is deleted through Telegram.
        with ledger.db:
            ledger.db.execute(
                "UPDATE invites SET status='deleted' "
                "WHERE account_id=? AND folder_id=? AND "
                "(city_id=? OR city_id LIKE 'rates:%')",
                (state.account_id, state.folder_id, city),
            )
        state.retired_weather_channels[archive_key] = state.channels.pop(key)
        if city in state.weather:
            state.retired_weather_posts[archive_key] = state.weather.pop(city)
        state.invites.pop(city, None)
        service.save()

"""Remove API-visible service events only from project-owned channels."""

import asyncio
import logging

from telethon import errors, functions, types

logger = logging.getLogger(__name__)
# Telegram does not allow deleting creation/migration records.
PROTECTED = {
    "MessageActionChannelCreate",
    "MessageActionChatCreate",
    "MessageActionChannelMigrateFrom",
    "MessageActionChatMigrateTo",
    "MessageActionTopicCreate",
}


class SystemMessageCleaner:
    def __init__(self, service):
        self.service = service
        self.stop = asyncio.Event()
        self.cursors = {}

    async def clean_channel(self, channel):
        peer = types.InputChannel(channel.channel_id, channel.access_hash)
        previous = self.cursors.get(channel.channel_id, 0)
        newest = previous
        await self.service.ensure_connection()
        # First pass includes old service events. Later passes are incremental.
        async for message in self.service.client.iter_messages(
            types.InputPeerChannel(channel.channel_id, channel.access_hash),
            limit=None,
            min_id=previous,
        ):
            newest = max(newest, message.id)
            if not isinstance(message, types.MessageService):
                continue
            if type(message.action).__name__ in PROTECTED:
                continue
            try:
                await self.service.rpc(
                    functions.channels.DeleteMessagesRequest(
                        channel=peer, id=[message.id]
                    )
                )
            except errors.RPCError as exc:
                if exc.message not in {
                    "MESSAGE_DELETE_FORBIDDEN",
                    "MESSAGE_ID_INVALID",
                }:
                    raise
                logger.warning(
                    "Service event cannot be removed: %s", exc.message
                )
        # A failed pass retains its cursor; deletions are safe to retry.
        self.cursors[channel.channel_id] = newest

    async def tick(self):
        for channel in list(self.service.state.channels.values()):
            if self.stop.is_set():
                return
            if not channel.ready or channel.channel_id is None:
                continue
            async with self.service.lock:
                try:
                    await self.clean_channel(channel)
                except errors.FloodWaitError as exc:
                    self.service.remember_flood(
                        exc.seconds, request=exc.request
                    )
                    return
                except Exception as exc:
                    logger.warning(
                        "Service-message cleanup: %s", type(exc).__name__
                    )

    async def run(self):
        while not self.stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=60)
            except TimeoutError:
                pass

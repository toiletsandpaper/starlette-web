from contextlib import asynccontextmanager
from typing import AsyncGenerator, AsyncIterator, Optional, Any, Dict, Set

import anyio
from anyio._core._tasks import TaskGroup
from anyio.streams.memory import (
    MemoryObjectReceiveStream,
    MemoryObjectSendStream,
    EndOfStream,
    ClosedResourceError,
)

from starlette_web.common.channels.layers.base import BaseChannelLayer
from starlette_web.common.channels.event import Event
from starlette_web.common.channels.exceptions import ListenerClosed


_empty = object()


class Channel:
    EXIT_MAX_DELAY = 60

    def __init__(self, channel_layer: BaseChannelLayer):
        self._task_group: Optional[TaskGroup] = None
        self._channel_layer = channel_layer
        self._subscribers: Dict[str, Set[MemoryObjectSendStream]] = dict()
        self._manager_lock = anyio.Lock()
        self._task_group_handler = None

    async def __aenter__(self) -> "Channel":
        await self.connect()
        self._task_group_handler = anyio.create_task_group()
        self._task_group = await self._task_group_handler.__aenter__()
        self._task_group.start_soon(self._listener)
        return self

    async def __aexit__(self, *args: Any, **kwargs: Any):
        try:
            self._task_group.cancel_scope.cancel()
            del self._task_group

            retval = await self._task_group_handler.__aexit__(*args)
            del self._task_group_handler
        finally:
            self._subscribers.clear()
            with anyio.fail_after(self.EXIT_MAX_DELAY, shield=True):
                await self.disconnect()

        return retval

    async def connect(self) -> None:
        await self._channel_layer.connect()

    async def disconnect(self) -> None:
        await self._channel_layer.disconnect()

    async def _listener(self) -> None:
        while True:
            try:
                event = await self._channel_layer.next_published()
            except ListenerClosed:
                break

            async with self._manager_lock:
                subscribers_list = list(self._subscribers.get(event.group, []))

            async with anyio.create_task_group() as nursery:
                for send_stream in subscribers_list:
                    nursery.start_soon(send_stream.send, event)

        async with self._manager_lock:
            for group in self._subscribers.keys():
                for recv_channel in self._subscribers[group]:
                    recv_channel.close()

    async def publish(self, group: str, message: Any) -> None:
        await self._channel_layer.publish(group, message)

    @asynccontextmanager
    async def subscribe(self, group: str) -> AsyncGenerator["Subscriber", None]:
        send_stream, receive_stream = anyio.create_memory_object_stream()

        try:
            async with self._manager_lock:
                if not self._subscribers.get(group):
                    await self._channel_layer.subscribe(group)
                    self._subscribers[group] = {
                        send_stream,
                    }
                else:
                    self._subscribers[group].add(send_stream)

            yield Subscriber(receive_stream)

        finally:
            try:
                with anyio.fail_after(self.EXIT_MAX_DELAY, shield=True):
                    async with self._manager_lock:
                        self._subscribers[group].remove(send_stream)
                        if not self._subscribers.get(group):
                            del self._subscribers[group]
                            await self._channel_layer.unsubscribe(group)

            finally:
                send_stream.close()


class Subscriber:
    def __init__(self, receive_stream: MemoryObjectReceiveStream) -> None:
        self._receive_stream = receive_stream

    async def __aiter__(self) -> AsyncIterator[Event]:
        async with self._receive_stream:
            try:
                while True:
                    event: Event = await self._receive_stream.receive()
                    yield event
            except (EndOfStream, ClosedResourceError):
                pass

from typing import Optional, Any

import anyio
import asyncpg
from anyio.streams.memory import (
    MemoryObjectReceiveStream,
    MemoryObjectSendStream,
)

from starlette_web.common.channels.event import Event
from starlette_web.common.channels.exceptions import ListenerClosed
from starlette_web.common.channels.layers.base import BaseChannelLayer
from starlette_web.common.http.exceptions import ImproperlyConfigured, NotSupportedError
from starlette_web.common.utils import get_available_options


class PostgreSQLChannelLayer(BaseChannelLayer):
    MAX_MESSAGE_BYTELEN = 8000
    _connection_closed_flag = object()

    def __init__(self, **options) -> None:
        # Options must be valid kwargs for asyncpg.connect
        super().__init__(**options)
        self._conn: Optional[asyncpg.connection.Connection] = None
        self._manager_lock = anyio.Lock()
        self._receive_stream: Optional[MemoryObjectReceiveStream] = None
        self._send_stream: Optional[MemoryObjectSendStream] = None

        self._connection_options = {
            key: value
            for key, value in options.items()
            if key in get_available_options(asyncpg.connect)
        }

    async def connect(self) -> None:
        try:
            async with self._manager_lock:
                self._send_stream, self._receive_stream = anyio.create_memory_object_stream()
                self._conn = await asyncpg.connect(**self._connection_options)
                self._conn.add_termination_listener(self._termination_listener)
        except ValueError as exc:
            raise ImproperlyConfigured(details=str(exc)) from exc

    async def disconnect(self) -> None:
        async with self._manager_lock:
            try:
                await self._conn.close()
            finally:
                self._send_stream.close()
                self._receive_stream.close()
                self._send_stream = None
                self._receive_stream = None

    async def subscribe(self, group: str) -> None:
        await self._conn.add_listener(group, self._listener)

    async def _listener(self, *args: Any) -> None:
        connection, pid, channel, payload = args
        event = Event(group=channel, message=payload)
        await self._send_stream.send(event)

    async def unsubscribe(self, group: str) -> None:
        await self._conn.remove_listener(group, self._listener)

    async def publish(self, group: str, message: Any) -> None:
        await self._validate_message(message)
        await self._conn.execute("SELECT pg_notify($1, $2);", group, message)

    async def _termination_listener(self, *args) -> None:
        await self._send_stream.send(self._connection_closed_flag)

    async def _validate_message(self, message):
        # https://www.postgresql.org/docs/current/sql-notify.html
        if type(message) != str:
            raise NotSupportedError(
                details="Publish message for PostgreSQL NOTIFY must be a string"
            )

        if len(message.encode("utf-8")) > self.MAX_MESSAGE_BYTELEN:
            raise NotSupportedError(
                details=f"Message byte-length must be at most "
                        f"{self.MAX_MESSAGE_BYTELEN} symbols"
            )

    async def next_published(self) -> Event:
        event: Event = await self._receive_stream.receive()
        if event is self._connection_closed_flag:
            raise ListenerClosed
        return event

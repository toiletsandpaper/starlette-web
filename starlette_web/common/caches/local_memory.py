# Adapted from https://github.com/django/django/blob/main/django/core/cache/backends/locmem.py

import math
import re
from typing import Any, Optional, Dict, Sequence, AsyncContextManager, List

import anyio
from anyio.lowlevel import checkpoint

from starlette_web.common.caches.base import BaseCache, CacheError
from starlette_web.common.caches.base_lock import BaseLock
from starlette_web.common.http.exceptions import ImproperlyConfigured
from starlette_web.common.utils.regex import redis_pattern_to_re_pattern


_caches: Dict[str, Dict[str, Any]] = {}
_expire_info: Dict[str, Dict[str, float]] = {}
_locks: Dict[str, Dict[str, float]] = {}


class _AsyncLocalMemoryLock(BaseLock):
    def __init__(
        self,
        name: str,
        timeout: Optional[float] = None,
        blocking_timeout: Optional[float] = None,
        **kwargs,
    ) -> None:
        super().__init__(name=name, timeout=timeout, blocking_timeout=blocking_timeout, **kwargs)
        self._manager_lock = kwargs["manager_lock"]
        self._cache_name = kwargs["cache_name"]
        global _locks
        _locks.setdefault(self._cache_name, {})
        self._cache_lock = _locks[self._cache_name]

    async def _acquire(self):
        if self._is_acquired:
            return

        while True:
            await checkpoint()
            async with self._manager_lock:
                if self._cache_lock.get(self._name, -1) < anyio.current_time():
                    self._cache_lock[self._name] = anyio.current_time() + self._timeout
                    self._acquire_event.set()
                    return

    async def _release(self):
        if not self._is_acquired:
            return

        async with self._manager_lock:
            self._cache_lock[self._name] = -1
            self._is_acquired = False


class LocalMemoryCache(BaseCache):
    def __init__(self, options):
        self.name = options.get("name", None)
        if self.name is None:
            raise ImproperlyConfigured('LocalMemoryCache must be instantiated with option "name"')

        super().__init__(options)
        self._manager_lock = anyio.Lock()
        self._locking_manager_lock = anyio.Lock()

        global _caches, _locks, _expire_info
        self._cache = _caches.setdefault(self.name, {})
        self._expire_info = _expire_info.setdefault(self.name, {})
        self._lock = _locks.setdefault(self.name, {})

    async def async_get(self, key: str) -> Any:
        async with self._manager_lock:
            if self._has_expired(key):
                self._delete_key(key)

            return self.serializer.deserialize(self._cache.get(key))

    async def async_set(self, key: str, value: Any, timeout: Optional[float] = 120) -> None:
        async with self._manager_lock:
            deadline = anyio.current_time() + timeout if timeout is not None else math.inf
            self._cache[key] = self.serializer.serialize(value)
            self._expire_info[key] = deadline

    async def async_delete(self, key: str) -> None:
        async with self._manager_lock:
            self._delete_key(key)

    async def async_keys(self, pattern: str) -> List[str]:
        try:
            re_pattern = redis_pattern_to_re_pattern(pattern)
        except re.error as exc:
            raise CacheError(details=str(exc)) from exc

        return [
            key
            for key in self._cache.keys()
            if not self._has_expired(key) and re.fullmatch(re_pattern, key)
        ]

    async def async_has_key(self, key: str) -> bool:
        async with self._manager_lock:
            if self._has_expired(key):
                self._delete_key(key)

            return key in self._cache

    def _delete_key(self, key: str) -> None:
        self._cache.pop(key, None)
        self._expire_info.pop(key, None)

    def _has_expired(self, key) -> bool:
        return self._expire_info.get(key, -1) < anyio.current_time()

    async def async_get_many(self, keys: Sequence[str]) -> Dict[str, Any]:
        result = dict()
        for key in keys:
            result[key] = await self.async_get(key)
        return result

    async def async_set_many(self, data: Dict[str, Any], timeout: Optional[float] = 120) -> None:
        for key, value in data.items():
            await self.async_set(key, value, timeout=timeout)

    async def async_delete_many(self, keys: Sequence[str]) -> None:
        async with self._manager_lock:
            for key in keys:
                self._delete_key(key)

    async def async_clear(self) -> None:
        async with self._manager_lock:
            self._expire_info.clear()
            self._cache.clear()

    def lock(
        self,
        name: str,
        timeout: Optional[float] = 20.0,
        blocking_timeout: Optional[float] = None,
        **kwargs,
    ) -> AsyncContextManager:
        return _AsyncLocalMemoryLock(
            name=name,
            timeout=timeout,
            blocking_timeout=blocking_timeout,
            manager_lock=self._locking_manager_lock,
            cache_name=self.name,
            **kwargs,
        )

"""Persistent JSON cache with TTL — survives MCP server restarts.

Single-process model: a shared dict held in memory, mirrored to a JSON file
on every write. Reads are served from memory. No external broker (Redis,
Memcached) is needed because:

* MCP server is single-process,
* cached payloads are small (kilobytes per entry, tens of entries),
* a restart-survival cache is the only persistence requirement.

Use the ``cached`` decorator on functions that hit slow / rate-limited
upstreams. Cache files live under ``$XDG_CACHE_HOME/tradingview-mcp/`` (or
``~/.cache/tradingview-mcp/`` as fallback). The directory can be overridden
with the ``TRADINGVIEW_MCP_CACHE_DIR`` env var — useful for tests.

Error responses (dicts with an ``error`` key) are NOT cached, so a transient
Yahoo 429 doesn't poison the cache for 24h.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

_LOCK = threading.RLock()
_STORE: dict[str, dict[str, Any]] | None = None  # lazy-loaded
_LOADED_PATH: Path | None = None


def _cache_path() -> Path:
    """Resolve the cache file path. Honors XDG and the test override env var."""
    override = os.environ.get("TRADINGVIEW_MCP_CACHE_DIR")
    if override:
        base = Path(override)
    else:
        xdg = os.environ.get("XDG_CACHE_HOME")
        base = Path(xdg) if xdg else Path.home() / ".cache"
        base = base / "tradingview-mcp"
    base.mkdir(parents=True, exist_ok=True)
    return base / "cache.json"


def _load() -> dict[str, dict[str, Any]]:
    """Load the cache file once per resolved path. Safe across path changes."""
    global _STORE, _LOADED_PATH
    path = _cache_path()
    if _STORE is not None and _LOADED_PATH == path:
        return _STORE
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {}
    _STORE = data
    _LOADED_PATH = path
    return _STORE


def _persist() -> None:
    """Atomically write the in-memory store to disk."""
    path = _cache_path()
    store = _STORE if _STORE is not None else {}
    fd, tmp = tempfile.mkstemp(prefix=".cache.", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(store, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _make_key(namespace: str, args: tuple, kwargs: dict) -> str:
    payload = json.dumps([args, sorted(kwargs.items())], default=str, sort_keys=True)
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
    return f"{namespace}:{digest}"


def cached(ttl_seconds: int, namespace: str) -> Callable:
    """Decorator: cache function result on disk for ``ttl_seconds``.

    Cache is keyed by ``(namespace, sha1(args, kwargs))``. Results that look
    like upstream failures (dict containing an ``"error"`` key) are not stored
    — we don't want a transient 429 to suppress real data for 24h.
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            key = _make_key(namespace, args, kwargs)
            now = time.time()
            with _LOCK:
                store = _load()
                entry = store.get(key)
                if entry and entry.get("expires_at", 0) > now:
                    return entry["value"]
            value = fn(*args, **kwargs)
            if isinstance(value, dict) and "error" in value:
                return value
            with _LOCK:
                store = _load()
                store[key] = {"expires_at": now + ttl_seconds, "value": value}
                _persist()
            return value
        wrapper.__cache_namespace__ = namespace  # type: ignore[attr-defined]
        return wrapper
    return decorator


def clear(namespace: str | None = None) -> int:
    """Drop entries (all, or those in *namespace*). Returns count removed."""
    with _LOCK:
        store = _load()
        if namespace is None:
            removed = len(store)
            store.clear()
        else:
            prefix = f"{namespace}:"
            keys = [k for k in store if k.startswith(prefix)]
            for k in keys:
                del store[k]
            removed = len(keys)
        _persist()
    return removed


def reset_for_tests() -> None:
    """Drop in-memory state. Tests use this with ``TRADINGVIEW_MCP_CACHE_DIR``."""
    global _STORE, _LOADED_PATH
    with _LOCK:
        _STORE = None
        _LOADED_PATH = None

"""Global outbound rate limiter — a single FIFO queue across the whole process.

At most one *external* HTTP request is **started** every ``interval`` seconds,
regardless of host. This turns the server into a "polite" client so upstreams
(Yahoo, Reddit, Stooq, TradingView, RSS, SEC, CoinGecko) stop rate-limiting /
blocking the container's egress.

Design decisions (see the conversation that introduced this):

* **One global queue, not per-host.** The operator wants a single shared
  throttle. Trade-off: a tool that fans out to ``K`` sources pays at least
  ``K * interval`` seconds of spacing.
* **Wait with a cap.** To keep that spacing from blowing past the *MCP client's*
  tool timeout, a caller that would have to wait longer than ``max_wait`` for its
  slot raises :class:`RateLimitTimeout` instead of blocking forever. The server
  sheds the request fast rather than hanging until the agent gives up.
* **Thread-safe.** FastMCP runs each synchronous tool in its own worker thread,
  so concurrent MCP requests reach this limiter from many threads at once. Slot
  reservation is guarded by a lock; the actual sleep happens *outside* the lock
  so queued callers don't serialize on the mutex itself.
* **Swappable backend.** The scheduling state ("when may the next slot start?")
  lives behind :class:`_Backend`. Today it's an in-memory timestamp guarded by a
  ``threading.Lock`` — zero new dependencies, correct for a single process. If
  the server is ever scaled to multiple replicas sharing one upstream budget, a
  ``RedisBackend`` implementing the same ``reserve()`` contract drops in via
  ``TRADINGVIEW_MCP_RATE_LIMIT_BACKEND=redis`` without touching any call site.
  (RabbitMQ/AMQP is deliberately *not* used: the MCP request/response model is
  synchronous, the shared state is a single number, and a broker would add two
  network round-trips and a whole service for no latency win.)

Config (env, read once when the process-global limiter is first used):

    TRADINGVIEW_MCP_RATE_LIMIT_ENABLED   "true"/"false"   (default true)
    TRADINGVIEW_MCP_RATE_LIMIT_RPS       float, req/sec    (default 1.0; <=0 disables)
    TRADINGVIEW_MCP_RATE_LIMIT_MAX_WAIT  float seconds     (default 30; 0 = wait forever)
    TRADINGVIEW_MCP_RATE_LIMIT_BACKEND   "memory" | "redis" (default "memory")
"""
from __future__ import annotations

import abc
import os
import socket
import threading
import time
import urllib.request
from typing import Callable, Optional

from tradingview_mcp.core.services.log import get_logger

_log = get_logger("ratelimit")


class RateLimitTimeout(Exception):
    """Raised when acquiring a slot would exceed ``max_wait`` seconds.

    Surfaced to the caller so the tool can fail fast (and the MCP client gets an
    error promptly) instead of blocking until the client-side timeout fires.
    """

    def __init__(self, wait: float, max_wait: float, label: Optional[str] = None):
        self.wait = wait
        self.max_wait = max_wait
        self.label = label
        where = f" for {label}" if label else ""
        super().__init__(
            f"outbound rate-limit queue full{where}: would wait {wait:.1f}s "
            f"(> max {max_wait:.1f}s)"
        )


# ─── Backends ─────────────────────────────────────────────────────────────────

class _Backend(abc.ABC):
    """Reserves the next slot and returns how long the caller must sleep.

    Implementations MUST NOT sleep — they only schedule. The facade does the
    sleeping outside any lock. This keeps the contract identical for the
    in-memory backend (a locked timestamp) and a future distributed one (an
    atomic Redis op), so swapping is a one-line factory change.
    """

    @abc.abstractmethod
    def reserve(self, interval: float, max_wait: float, label: Optional[str]) -> float:
        """Return seconds to sleep before this caller's slot, or raise
        :class:`RateLimitTimeout` if it would exceed ``max_wait``."""


class MemoryBackend(_Backend):
    """In-process scheduler: a single monotonic timestamp behind a lock."""

    def __init__(self, time_fn: Callable[[], float] = time.monotonic):
        self._time = time_fn
        self._lock = threading.Lock()
        self._next_at = 0.0  # earliest monotonic time the next slot may start

    def reserve(self, interval: float, max_wait: float, label: Optional[str]) -> float:
        with self._lock:
            now = self._time()
            scheduled = self._next_at if self._next_at > now else now
            wait = scheduled - now
            if max_wait > 0 and wait > max_wait:
                # Shed without reserving, so the next caller still sees the same
                # queue depth (this request never took a slot).
                raise RateLimitTimeout(wait, max_wait, label)
            self._next_at = scheduled + interval
        return wait


# ─── Limiter facade ───────────────────────────────────────────────────────────

class RateLimiter:
    """Process-global single-queue limiter: one request per ``interval`` seconds.

    Injectable ``sleep_fn`` (and the backend's ``time_fn``) keep this fully
    unit-testable with a fake clock — no real sleeping in tests.
    """

    def __init__(
        self,
        rps: float = 1.0,
        max_wait: float = 30.0,
        *,
        enabled: bool = True,
        backend: Optional[_Backend] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        self.enabled = enabled
        self.rps = rps
        self.max_wait = max_wait
        self._interval = 1.0 / rps if rps > 0 else 0.0
        self._sleep = sleep_fn
        self._backend = backend or MemoryBackend()

    def acquire(self, label: Optional[str] = None) -> float:
        """Block until this caller's slot is due; return the seconds waited.

        No-op (returns ``0.0``) when disabled or ``rps <= 0``. Raises
        :class:`RateLimitTimeout` when the wait would exceed ``max_wait``.
        """
        if not self.enabled or self._interval <= 0.0:
            return 0.0
        wait = self._backend.reserve(self._interval, self.max_wait, label)
        if wait > 0:
            if wait >= 1.0:
                _log.debug("queue: waiting %.1fs%s", wait,
                           f" before {label}" if label else "")
            self._sleep(wait)
        return wait


# ─── Process-global singleton + module-level convenience API ──────────────────

_INSTANCE: Optional[RateLimiter] = None
_INSTANCE_LOCK = threading.Lock()


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        _log.warning("bad %s=%r — using default %.3f", name, raw, default)
        return default


def _build_backend() -> _Backend:
    kind = os.environ.get("TRADINGVIEW_MCP_RATE_LIMIT_BACKEND", "memory").strip().lower()
    if kind in ("", "memory", "local", "inproc", "in-process"):
        return MemoryBackend()
    # Extension point: a RedisBackend(reserve via INCR/EXPIRE or a Lua token
    # bucket) plugs in here for multi-replica deployments. Not implemented yet —
    # fall back to memory so a misconfigured env never breaks startup.
    _log.warning("rate-limit backend %r not available — using in-memory backend", kind)
    return MemoryBackend()


def get_limiter() -> RateLimiter:
    """Return the process-global limiter, building it from env on first use."""
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                limiter = RateLimiter(
                    rps=_float_env("TRADINGVIEW_MCP_RATE_LIMIT_RPS", 1.0),
                    max_wait=_float_env("TRADINGVIEW_MCP_RATE_LIMIT_MAX_WAIT", 30.0),
                    enabled=_bool_env("TRADINGVIEW_MCP_RATE_LIMIT_ENABLED", True),
                    backend=_build_backend(),
                )
                if limiter.enabled and limiter._interval > 0:
                    _log.info(
                        "outbound rate limit active: 1 request / %.2fs "
                        "(max wait %.0fs)", limiter._interval, limiter.max_wait,
                    )
                else:
                    _log.info("outbound rate limit disabled")
                _INSTANCE = limiter
    return _INSTANCE


def acquire(label: Optional[str] = None) -> float:
    """Acquire a slot on the process-global outbound limiter.

    ``label`` is informational (host/feed name) and only used for debug logs.
    """
    return get_limiter().acquire(label)


def gated_urlopen(req, *, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, label: Optional[str] = None):
    """``urllib.request.urlopen`` gated by the global limiter.

    For the few call sites that fetch directly (not through the proxy-aware
    opener from ``build_opener_with_proxy``, which is already gated by a urllib
    handler). Preserves urllib's default-timeout semantics when ``timeout`` is
    omitted.
    """
    if label is None:
        if isinstance(req, urllib.request.Request):
            label = req.host
        elif isinstance(req, str):
            label = req
    acquire(label)
    return urllib.request.urlopen(req, timeout=timeout)


def reset_for_tests() -> None:
    """Drop the cached singleton so the next :func:`get_limiter` re-reads env."""
    global _INSTANCE
    _INSTANCE = None

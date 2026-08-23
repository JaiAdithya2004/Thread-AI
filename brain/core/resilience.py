"""
Resilience primitives: retry with exponential backoff+jitter, circuit breaker.

Usage:
    from brain.core.resilience import retry, CircuitBreaker, CircuitOpen

    @retry(max_attempts=3, base_delay=0.5)
    def call_external():
        ...

    _cb = CircuitBreaker("llm", failure_threshold=5, recovery_timeout=30)

    try:
        with _cb:
            result = call_external()
    except CircuitOpen:
        result = degraded_fallback()
"""
import random
import sys
import time
import threading
import functools
from typing import Callable, Tuple, Type


class CircuitOpen(Exception):
    """Raised when a circuit breaker is in the OPEN state."""
    def __init__(self, name: str):
        super().__init__(f"Circuit '{name}' is open — failing fast")
        self.name = name


class CircuitBreaker:
    """
    Three-state circuit breaker (CLOSED → OPEN → HALF-OPEN → CLOSED).

    failure_threshold: consecutive failures before tripping OPEN
    recovery_timeout:  seconds to wait before allowing one probe (HALF-OPEN)
    """
    CLOSED    = "closed"
    OPEN      = "open"
    HALF_OPEN = "half_open"

    def __init__(self, name: str, failure_threshold: int = 5,
                 recovery_timeout: float = 30.0):
        self.name              = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout  = recovery_timeout
        self._state            = self.CLOSED
        self._failures         = 0
        self._opened_at        = 0.0
        self._lock             = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def __enter__(self):
        with self._lock:
            if self._state == self.OPEN:
                if time.monotonic() - self._opened_at >= self.recovery_timeout:
                    self._state = self.HALF_OPEN
                    print(f"[circuit:{self.name}] HALF-OPEN — probing", file=sys.stderr)
                else:
                    raise CircuitOpen(self.name)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        with self._lock:
            if exc_type is None:
                # Success
                self._failures = 0
                if self._state == self.HALF_OPEN:
                    self._state = self.CLOSED
                    print(f"[circuit:{self.name}] CLOSED — recovered", file=sys.stderr)
            elif exc_type is CircuitOpen:
                # Already open — don't double-count
                pass
            else:
                self._failures += 1
                if self._state == self.HALF_OPEN or self._failures >= self.failure_threshold:
                    self._state    = self.OPEN
                    self._opened_at = time.monotonic()
                    print(
                        f"[circuit:{self.name}] OPEN after {self._failures} failure(s)",
                        file=sys.stderr,
                    )
        return False  # never suppress the exception


def retry(
    max_attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 16.0,
    jitter: float = 0.3,
    retryable: Tuple[Type[Exception], ...] = (Exception,),
    reraise_on: Tuple[Type[Exception], ...] = (),
):
    """
    Decorator: retry with exponential backoff + full jitter.

    max_attempts: total attempts including the first
    base_delay:   initial sleep in seconds (doubles each retry)
    max_delay:    cap on sleep
    jitter:       fraction of delay to randomise (0 = no jitter, 1 = full jitter)
    retryable:    exception types to retry on (default: all)
    reraise_on:   exception types to NEVER retry — always reraise immediately
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except reraise_on as e:
                    raise
                except retryable as e:
                    last_exc = e
                    if attempt == max_attempts:
                        break
                    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                    sleep = delay * (1 - jitter + random.random() * jitter * 2)
                    print(
                        f"[retry:{fn.__name__}] attempt {attempt}/{max_attempts} "
                        f"failed: {e!r} — retrying in {sleep:.2f}s",
                        file=sys.stderr,
                    )
                    time.sleep(sleep)
            raise last_exc
        return wrapper
    return decorator


# ── Pre-built breakers (one per external system) ─────────────────────────────
# These are module-level singletons so state is shared across all callers
# within the same process.

_breakers: dict[str, CircuitBreaker] = {}
_breakers_lock = threading.Lock()


def get_breaker(name: str, failure_threshold: int = 5,
                recovery_timeout: float = 30.0) -> CircuitBreaker:
    """Return (or create) the named circuit breaker."""
    with _breakers_lock:
        if name not in _breakers:
            _breakers[name] = CircuitBreaker(
                name, failure_threshold=failure_threshold,
                recovery_timeout=recovery_timeout,
            )
        return _breakers[name]


def breaker_states() -> dict:
    """Return {name: state} for all breakers — used by /api/status."""
    with _breakers_lock:
        return {n: b.state for n, b in _breakers.items()}

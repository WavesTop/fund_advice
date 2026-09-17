"""Bounded retries for idempotent public GET requests; never retry schema failures."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import random
import socket
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

TRANSIENT = frozenset((408, 429, 500, 502, 503, 504))
MAX_BYTES = 10 * 1024 * 1024
# Installed only inside the isolated collection worker, before it starts threads.
_observer: Callable[[dict], None] | None = None


def set_observer(observer: Callable[[dict], None] | None) -> None:
    global _observer
    _observer = observer


def _retry_after(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0.0


def get_bytes(request: Request, *, timeout: float, attempts: int = 3,
              opener: Callable = urlopen, sleep: Callable = time.sleep,
              clock: Callable = time.monotonic, jitter: Callable = random.random,
              observe: Callable[[dict], None] | None = None) -> bytes:
    if request.get_method() != "GET":
        raise ValueError("重试传输只支持幂等 GET")
    if not isinstance(attempts, int) or isinstance(attempts, bool) or not 0 < timeout <= 90 or not 1 <= attempts <= 5:
        raise ValueError("重试次数或请求总时限无效")
    observe = observe or _observer
    deadline = clock() + timeout
    for attempt in range(1, attempts + 1):
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError("来源请求总预算耗尽")
        event = {"attempt": attempt, "url": request.full_url, "status": None, "error": None}
        error: Exception | None = None
        retryable, retry_after = False, 0.0
        try:
            with opener(request, timeout=remaining) as response:
                event["status"] = getattr(response, "status", 200)
                body = response.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    raise ValueError("来源响应超过允许大小")
                if not body:
                    raise ValueError("来源响应为空")
                if clock() > deadline:
                    raise TimeoutError("读取响应超过来源请求总预算")
                event["body"] = body
                event["media_type"] = response.headers.get("Content-Type", "application/octet-stream") if hasattr(response, "headers") else "application/octet-stream"
                return body
        except HTTPError as exc:
            event["status"] = exc.code
            error = exc
            retryable = exc.code in TRANSIENT
            retry_after = _retry_after(exc.headers.get("Retry-After") if exc.headers else None)
            exc.close()
        except (URLError, TimeoutError, ConnectionError, socket.timeout) as exc:
            error, retryable = exc, True
        except Exception as exc:
            error = exc
        finally:
            if error is not None:
                event["error"] = f"{type(error).__name__}: {error}"[:1000]
            if observe is not None:
                observe(event)
        if error is None:
            raise RuntimeError("无效传输状态")
        if not retryable or attempt == attempts:
            raise error
        delay = max(retry_after, 0.25 * 2 ** (attempt - 1) + jitter() * 0.1)
        # Respect Retry-After instead of retrying earlier when it exceeds our budget.
        if delay >= deadline - clock():
            raise error
        sleep(delay)
    raise RuntimeError("无效重试状态")

"""업비트 Rate Limit 대응 토큰 버킷.

공식 한도 (docs.upbit.com/kr/reference/rate-limits 기준):
  Exchange  order            : 초당 12회   (포켓 단위)
  Exchange  default          : 초당 30회   (주문취소/주문조회/자산조회)
  Exchange  order-cancel-all : 2초당 1회
  Quotation market/candle/trade/ticker/orderbook : 각 그룹 초당 10회 (IP 단위)

기본값은 공식 한도보다 낮게 잡는다. 429 가 반복되면 업비트는 418 로
일시 차단하므로, 한도를 꽉 채워 쓰는 것이 실익보다 위험이 크다.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

# (초당 허용 요청 수, 버킷 용량) — 공식 한도의 약 70% 수준
DEFAULT_LIMITS: dict[str, tuple[float, float]] = {
    "order": (8.0, 8.0),
    "default": (20.0, 20.0),
    "order-cancel-all": (0.5, 1.0),
    "market": (7.0, 7.0),
    "candle": (7.0, 7.0),
    "trade": (7.0, 7.0),
    "ticker": (7.0, 7.0),
    "orderbook": (7.0, 7.0),
}


@dataclass
class _Bucket:
    rate: float
    capacity: float
    tokens: float
    updated: float


class RateLimiter:
    """그룹별 토큰 버킷. 스레드 안전."""

    def __init__(self, limits: dict[str, tuple[float, float]] | None = None) -> None:
        merged = dict(DEFAULT_LIMITS)
        if limits:
            merged.update({k: tuple(v) for k, v in limits.items()})  # type: ignore[misc]
        now = time.monotonic()
        self._buckets = {
            name: _Bucket(rate=rate, capacity=cap, tokens=cap, updated=now)
            for name, (rate, cap) in merged.items()
        }
        self._lock = threading.Lock()

    def acquire(self, group: str, tokens: float = 1.0, timeout: float = 30.0) -> None:
        """토큰이 찰 때까지 블로킹. 알 수 없는 그룹은 default 로 처리."""
        bucket = self._buckets.get(group) or self._buckets["default"]
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                bucket.tokens = min(
                    bucket.capacity, bucket.tokens + (now - bucket.updated) * bucket.rate
                )
                bucket.updated = now
                if bucket.tokens >= tokens:
                    bucket.tokens -= tokens
                    return
                wait = (tokens - bucket.tokens) / bucket.rate
            if time.monotonic() + wait > deadline:
                raise TimeoutError(f"rate limit 대기 초과: group={group}")
            time.sleep(min(wait, 0.25))

    def penalize(self, group: str, seconds: float = 1.0) -> None:
        """429/418 수신 시 해당 그룹 버킷을 비워 강제 감속."""
        with self._lock:
            bucket = self._buckets.get(group) or self._buckets["default"]
            bucket.tokens = 0.0
            bucket.updated = time.monotonic() + seconds

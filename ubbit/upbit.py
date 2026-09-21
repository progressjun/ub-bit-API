"""업비트 Open API REST 클라이언트 (시세 + 거래).

문서: https://docs.upbit.com/kr/reference
- 시세(Quotation) : 인증 불필요, IP 단위 rate limit
- 거래(Exchange)  : JWT 인증 필요, 포켓 단위 rate limit
"""
from __future__ import annotations

import time
from typing import Any, Mapping

import requests

from .auth import auth_header, encode_query
from .logutil import get_logger
from .ratelimit import RateLimiter

log = get_logger("upbit")

BASE_URL = "https://api.upbit.com"


class UpbitError(RuntimeError):
    def __init__(self, status: int, payload: Any, path: str) -> None:
        self.status = status
        self.payload = payload
        self.path = path
        super().__init__(f"[{status}] {path} -> {payload}")

    @property
    def name(self) -> str:
        if isinstance(self.payload, dict):
            return str(self.payload.get("error", {}).get("name", ""))
        return ""


class UpbitClient:
    """재시도/레이트리밋/인증을 포함한 얇은 REST 래퍼."""

    def __init__(
        self,
        access_key: str | None = None,
        secret_key: str | None = None,
        *,
        limiter: RateLimiter | None = None,
        timeout: float = 10.0,
        max_retries: int = 4,
        base_url: str = BASE_URL,
    ) -> None:
        self.access_key = access_key
        self.secret_key = secret_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.base_url = base_url.rstrip("/")
        self.limiter = limiter or RateLimiter()
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": "ubbit/0.1"})

    # ------------------------------------------------------------------ core
    @property
    def has_keys(self) -> bool:
        return bool(self.access_key and self.secret_key)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        group: str = "default",
        private: bool = False,
    ) -> Any:
        url = f"{self.base_url}{path}"
        query = encode_query(params)
        headers: dict[str, str] = {}
        if private:
            if not self.has_keys:
                raise RuntimeError("API 키가 없습니다. UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY 를 설정하세요.")
            headers.update(auth_header(self.access_key, self.secret_key, params))

        backoff = 0.5
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self.limiter.acquire(group)
            try:
                if method == "GET":
                    full = f"{url}?{query}" if query else url
                    resp = self.session.get(full, headers=headers, timeout=self.timeout)
                elif method == "DELETE":
                    full = f"{url}?{query}" if query else url
                    resp = self.session.delete(full, headers=headers, timeout=self.timeout)
                else:  # POST
                    headers["Content-Type"] = "application/x-www-form-urlencoded"
                    resp = self.session.post(
                        url, data=query.encode("utf-8"), headers=headers, timeout=self.timeout
                    )
            except requests.RequestException as exc:  # 네트워크 계층 오류
                last_exc = exc
                log.warning("네트워크 오류 (%s/%s) %s: %s", attempt, self.max_retries, path, exc)
                time.sleep(backoff)
                backoff *= 2
                continue

            if resp.status_code in (429, 418):
                self.limiter.penalize(group, seconds=backoff)
                log.warning("rate limit %s (%s/%s) %s", resp.status_code, attempt, self.max_retries, path)
                time.sleep(backoff)
                backoff *= 2
                continue

            if 500 <= resp.status_code < 600:
                log.warning("서버 오류 %s (%s/%s) %s", resp.status_code, attempt, self.max_retries, path)
                time.sleep(backoff)
                backoff *= 2
                continue

            try:
                payload = resp.json()
            except ValueError:
                payload = resp.text

            if resp.status_code >= 400:
                # 4xx 는 재시도해도 동일하므로 즉시 실패시킨다.
                raise UpbitError(resp.status_code, payload, path)
            return payload

        raise UpbitError(-1, f"재시도 초과: {last_exc}", path)

    # ------------------------------------------------------------- quotation
    def markets(self, is_details: bool = False) -> list[dict]:
        return self._request(
            "GET", "/v1/market/all", params={"isDetails": str(is_details).lower()}, group="market"
        )

    def candles(self, market: str, unit: int = 5, count: int = 200, to: str | None = None) -> list[dict]:
        """분봉. unit ∈ {1,3,5,10,15,30,60,240}. 응답은 최신순이므로 뒤집어 반환."""
        params: dict[str, Any] = {"market": market, "count": min(count, 200)}
        if to:
            params["to"] = to
        rows = self._request("GET", f"/v1/candles/minutes/{unit}", params=params, group="candle")
        return list(reversed(rows))

    def candles_days(self, market: str, count: int = 200, to: str | None = None) -> list[dict]:
        params: dict[str, Any] = {"market": market, "count": min(count, 200)}
        if to:
            params["to"] = to
        rows = self._request("GET", "/v1/candles/days", params=params, group="candle")
        return list(reversed(rows))

    def ticker(self, markets: list[str]) -> list[dict]:
        return self._request("GET", "/v1/ticker", params={"markets": ",".join(markets)}, group="ticker")

    def orderbook(self, markets: list[str]) -> list[dict]:
        return self._request(
            "GET", "/v1/orderbook", params={"markets": ",".join(markets)}, group="orderbook"
        )

    # -------------------------------------------------------------- exchange
    def accounts(self) -> list[dict]:
        return self._request("GET", "/v1/accounts", group="default", private=True)

    def orders_chance(self, market: str) -> dict:
        """마켓별 주문 가능 정보: 수수료율, 최소 주문금액, 잔고를 서버에서 직접 받는다."""
        return self._request(
            "GET", "/v1/orders/chance", params={"market": market}, group="default", private=True
        )

    def order_detail(self, uuid_: str) -> dict:
        return self._request("GET", "/v1/order", params={"uuid": uuid_}, group="default", private=True)

    def place_order(
        self,
        market: str,
        side: str,
        ord_type: str,
        *,
        volume: str | None = None,
        price: str | None = None,
        identifier: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {"market": market, "side": side, "ord_type": ord_type}
        if volume is not None:
            params["volume"] = volume
        if price is not None:
            params["price"] = price
        if identifier is not None:
            params["identifier"] = identifier
        return self._request("POST", "/v1/orders", params=params, group="order", private=True)

    def market_buy(self, market: str, krw_amount: float, identifier: str | None = None) -> dict:
        """시장가 매수: side=bid, ord_type=price, price=매수 총액(KRW)."""
        return self.place_order(
            market, "bid", "price", price=_num(krw_amount), identifier=identifier
        )

    def market_sell(self, market: str, volume: float, identifier: str | None = None) -> dict:
        """시장가 매도: side=ask, ord_type=market, volume=매도 수량."""
        return self.place_order(
            market, "ask", "market", volume=_num(volume, 8), identifier=identifier
        )

    def limit_order(self, market: str, side: str, price: float, volume: float) -> dict:
        return self.place_order(
            market, side, "limit", price=_num(price), volume=_num(volume, 8)
        )

    def cancel_order(self, uuid_: str) -> dict:
        return self._request("DELETE", "/v1/order", params={"uuid": uuid_}, group="default", private=True)

    # -------------------------------------------------------------- helpers
    def wait_fill(self, uuid_: str, timeout: float = 15.0, interval: float = 0.4) -> dict:
        """주문 체결 완료까지 폴링. 미체결 잔량이 남아도 timeout 시 마지막 상태를 반환."""
        deadline = time.monotonic() + timeout
        detail: dict = {}
        while time.monotonic() < deadline:
            detail = self.order_detail(uuid_)
            if detail.get("state") in ("done", "cancel"):
                return detail
            time.sleep(interval)
        return detail


def _num(value: float, decimals: int = 8) -> str:
    """지수표기(1e-05)를 피하고 업비트가 받는 십진 문자열로 변환."""
    text = f"{value:.{decimals}f}".rstrip("0").rstrip(".")
    return text or "0"

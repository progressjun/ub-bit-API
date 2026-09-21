"""과거 캔들로 실제 엔진을 고속 재생한다.

백테스터와 무엇이 다른가: 백테스터는 전략 함수만 호출한다. replay 는
TradingEngine 을 그대로 돌린다. 리스크 게이트, 세션 제어, SQLite 영속화,
주문 실패 처리, 킬스위치까지 실거래와 같은 코드 경로를 탄다.

240분봉 전략은 종목당 연 10여 회만 진입한다. 실시간 페이퍼로는 첫 거래를
보는 데 몇 주가 걸린다. replay 는 그 몇 주를 몇 초로 압축해, 배포 전에
'이 봇이 실제로 무엇을 하는지'를 눈으로 확인하게 한다.

한계는 백테스트와 동일하다. 호가 소진, 거래소 지연, 부분체결을 재현하지 않는다.
"""
from __future__ import annotations

from datetime import datetime
from typing import Sequence

from .engine import TradingEngine
from .logutil import get_logger
from .strategy import Candle

log = get_logger("replay")


class ReplayClient:
    """UpbitClient 중 엔진이 쓰는 메서드만 과거 데이터로 대체한다."""

    def __init__(self, data: dict[str, Sequence[Candle]], window: int) -> None:
        self.data = data
        self.window = window
        self.cursor = 0

    def _slice(self, market: str) -> list[Candle]:
        candles = self.data.get(market, [])
        end = min(self.cursor, len(candles))
        return list(candles[max(0, end - self.window) : end])

    def candles(self, market: str, unit: int = 5, count: int = 200, to=None):
        window = self._slice(market)[-count:]
        return [{
            "candle_date_time_kst": c.ts, "opening_price": c.open, "high_price": c.high,
            "low_price": c.low, "trade_price": c.close,
            "candle_acc_trade_volume": c.volume, "candle_acc_trade_price": c.value,
        } for c in window]

    def ticker(self, markets):
        out = []
        for market in markets:
            window = self._slice(market)
            if window:
                out.append({"market": market, "trade_price": window[-1].close})
        return out

    def orderbook(self, markets):
        out = []
        for market in markets:
            window = self._slice(market)
            if not window:
                continue
            price = window[-1].close
            out.append({"market": market, "orderbook_units": [
                {"ask_price": price * 1.0005, "ask_size": 1e12,
                 "bid_price": price * 0.9995, "bid_size": 1e12}
            ]})
        return out

    def markets(self, is_details: bool = False):
        return [{"market": m} for m in self.data]

    def current_time(self) -> datetime | None:
        for candles in self.data.values():
            if self.cursor and self.cursor <= len(candles):
                try:
                    return datetime.fromisoformat(candles[self.cursor - 1].ts)
                except ValueError:
                    return None
        return None


def run_replay(
    engine: TradingEngine,
    data: dict[str, Sequence[Candle]],
    *,
    start_at: int,
    step: int = 1,
) -> None:
    """엔진을 캔들 단위로 전진시킨다.

    세션 판정은 실제 시각이 아니라 캔들 타임스탬프로 한다. 그렇지 않으면
    과거 재생인데 '지금이 새벽이라 매매 금지' 같은 판정이 섞인다.
    """
    length = max(len(c) for c in data.values())
    client = ReplayClient(data, window=engine.cfg.engine.candle_count)
    engine.client = client
    engine.markets = list(data)
    engine.cfg.engine.universe_auto = False     # 재생 구간의 종목은 이미 고정돼 있다

    # 엔진의 시계를 캔들 타임스탬프로 교체한다. 일일 손실한도·매매횟수·
    # 쿨다운·세션 판정이 전부 이 시계를 쓰므로, 여기서만 바꾸면 전부 일관된다.
    original_clock = engine.clock
    engine.clock = lambda: client.current_time() or datetime.now()
    try:
        for cursor in range(start_at, length + 1, step):
            client.cursor = cursor
            engine.tick()
    finally:
        engine.clock = original_clock

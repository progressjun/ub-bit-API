"""매매 종목 자동 선별 (universe selection).

단타에서 종목 선택은 전략 파라미터가 아니라 전제 조건이다.
봉당 기대 이동폭이 왕복 비용을 못 넘기는 종목에서는 어떤 진입 규칙도
수수료에 진다. 그래서 '무엇을 살까'보다 '어디서는 아예 매매하지 않을까'를
먼저 정한다.

선별 기준 (전부 통과해야 후보)
  유동성    : 최근 24시간 거래대금 >= min_turnover_krw
              얇은 호가는 슬리피지가 수수료보다 크다.
  변동성    : ATR% 중앙값 >= 손익분기 × atr_cost_multiple
              이 조건이 이 모듈의 존재 이유다.
  과열 배제  : ATR% 중앙값 <= atr_max_pct
              변동성이 너무 크면 손절이 갭으로 건너뛰어 손실이 설계값을 넘는다.
  실측 슬리피지 : 주문 예정 금액으로 호가창을 소진했을 때의 왕복 비용이
              목표 순익을 잠식하지 않을 것.
  경보 제외  : 유의종목/주의종목(market_warning) 은 후보에서 제외.

정렬은 '비용 대비 이동폭 배수(edge_ratio)'로 한다. 절대 변동성이 아니라
비용 대비 비율이어야 종목 간 비교가 성립한다.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Sequence

from .fees import CostModel, estimate_slippage_from_orderbook
from .indicators import atr as _atr
from .logutil import get_logger
from .strategy import Candle
from .upbit import UpbitClient, UpbitError

log = get_logger("universe")


@dataclass
class UniverseParams:
    top_n: int = 4
    min_turnover_krw: float = 30_000_000_000.0   # 24h 거래대금 300억
    atr_cost_multiple: float = 4.0
    atr_max_pct: float = 0.05
    sample_bars: int = 120
    probe_order_krw: float = 500_000.0           # 슬리피지 실측에 쓸 가상 주문 규모
    max_round_trip_cost: float = 0.004           # 왕복 비용 상한 0.4%
    exclude_warning: bool = True
    exclude: tuple[str, ...] = ()


@dataclass
class Candidate:
    market: str
    atr_pct: float
    edge_ratio: float          # ATR% ÷ 손익분기 — 클수록 비용을 넘기기 쉽다
    turnover_krw: float
    slip_buy: float
    slip_sell: float
    round_trip_cost: float
    warning: bool

    def line(self) -> str:
        return (f"{self.market:<12} ATR% {self.atr_pct*100:>6.3f}  비용대비 ×{self.edge_ratio:>5.1f}  "
                f"24h거래대금 {self.turnover_krw/1e8:>8,.0f}억  "
                f"실측왕복비용 {self.round_trip_cost*100:>6.3f}%"
                + ("  [유의종목]" if self.warning else ""))


def build_universe(
    client: UpbitClient,
    cost: CostModel,
    unit: int,
    params: UniverseParams,
) -> list[Candidate]:
    """KRW 마켓 전체를 훑어 매매 자격을 갖춘 종목만 반환한다."""
    try:
        markets = [m for m in client.markets(is_details=True) if m["market"].startswith("KRW-")]
    except UpbitError as exc:
        log.error("마켓 목록 조회 실패: %s", exc)
        return []

    warning_map = {
        m["market"]: (m.get("market_event", {}).get("warning", False)
                      or m.get("market_warning") == "CAUTION")
        for m in markets
    }
    symbols = [m["market"] for m in markets if m["market"] not in params.exclude]

    # 1차: 거래대금으로 후보를 좁힌다 (ticker 는 한 번에 조회 가능해 비용이 싸다)
    turnover: dict[str, float] = {}
    for chunk in _chunks(symbols, 100):
        try:
            for t in client.ticker(chunk):
                turnover[t["market"]] = float(t.get("acc_trade_price_24h") or 0.0)
        except UpbitError as exc:
            log.warning("ticker 조회 실패: %s", exc)

    liquid = [s for s in symbols if turnover.get(s, 0.0) >= params.min_turnover_krw]
    liquid.sort(key=lambda s: turnover[s], reverse=True)
    log.info("유동성 통과 %d종목 / 전체 %d종목 (기준 24h %s억)",
             len(liquid), len(symbols), f"{params.min_turnover_krw / 1e8:,.0f}")

    floor = cost.breakeven_edge * params.atr_cost_multiple
    candidates: list[Candidate] = []

    # 2차: 변동성 — 캔들은 종목당 1회 호출이므로 유동성 상위 일부만 본다
    for market in liquid[: params.top_n * 8]:
        if params.exclude_warning and warning_map.get(market):
            continue
        try:
            rows = client.candles(market, unit=unit, count=params.sample_bars)
        except UpbitError as exc:
            log.warning("[%s] 캔들 조회 실패: %s", market, exc)
            continue
        candles = [Candle.from_upbit(r) for r in rows]
        if len(candles) < 30:
            continue
        series = _atr([c.high for c in candles], [c.low for c in candles],
                      [c.close for c in candles], 14)
        pcts = [v / candles[i].close for i, v in enumerate(series) if v and candles[i].close]
        if not pcts:
            continue
        median = statistics.median(pcts)
        if median < floor or median > params.atr_max_pct:
            continue
        candidates.append(Candidate(
            market=market, atr_pct=median, edge_ratio=median / cost.breakeven_edge,
            turnover_krw=turnover[market], slip_buy=0.0, slip_sell=0.0,
            round_trip_cost=cost.fee_buy + cost.fee_sell, warning=bool(warning_map.get(market)),
        ))

    candidates.sort(key=lambda c: c.edge_ratio, reverse=True)

    # 3차: 실측 슬리피지 — 상위 후보에만 적용 (호가 조회는 종목당 1회)
    survivors: list[Candidate] = []
    for cand in candidates[: params.top_n * 3]:
        try:
            book = client.orderbook([cand.market])[0]
        except (UpbitError, IndexError) as exc:
            log.warning("[%s] 호가 조회 실패: %s", cand.market, exc)
            continue
        buy_slip, sell_slip = estimate_slippage_from_orderbook(book, params.probe_order_krw)
        if buy_slip == float("inf") or sell_slip == float("inf"):
            log.info("[%s] 제외 — 주문 %s원이 호가창을 초과",
                     cand.market, f"{params.probe_order_krw:,.0f}")
            continue
        cand.slip_buy, cand.slip_sell = buy_slip, sell_slip
        cand.round_trip_cost = cost.fee_buy + cost.fee_sell + buy_slip + sell_slip
        if cand.round_trip_cost > params.max_round_trip_cost:
            log.info("[%s] 제외 — 실측 왕복비용 %.3f%% > 한도 %.3f%%",
                     cand.market, cand.round_trip_cost * 100, params.max_round_trip_cost * 100)
            continue
        survivors.append(cand)

    survivors.sort(key=lambda c: c.edge_ratio, reverse=True)
    return survivors[: params.top_n]


def _chunks(seq: Sequence[str], size: int):
    for i in range(0, len(seq), size):
        yield list(seq[i : i + size])

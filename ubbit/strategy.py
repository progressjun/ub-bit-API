"""수수료 인지형(fee-aware) 추세추종 전략.

설계 전제를 먼저 적는다. 이 전제가 깨지면 전략도 무효다.

  전제 1. 왕복 비용(수수료+슬리피지)은 고정이고 예측 가능하다.
          기대 수익은 고정이 아니며 예측이 빗나간다.
          따라서 "비용 대비 기대 이동폭 배수"를 진입의 1차 필터로 둔다.

  전제 2. 승률이 아니라 손익비가 비용을 이긴다.
          왕복 0.2% 비용에서 평균 익절 0.3%짜리 스캘핑은
          승률 60%여도 기대값이 0 근처다. 목표 순익은 비용의 배수로 잡는다.

  전제 3. 회전수는 전략 변수다. 하루 20회전이면 알파가 0일 때
          비용만으로 하루 -3.9%다. 그래서 일일 매매 횟수를 명시적으로 막는다.

진입 조건 (전부 AND)
  - 레짐이 상승추세: EMA(fast) > EMA(slow), EMA(slow) 기울기 > 0
  - 돌파: 종가가 직전 N봉 최고가(당봉 제외) 위
  - 변동성 충분: ATR% >= breakeven_edge * atr_cost_multiple
  - 변동성 과열 아님: ATR% <= atr_max_pct
  - RSI 중립~강세 구간: rsi_min <= RSI <= rsi_max (과열 꼭지 배제)
  - 거래대금 확장: 최근 거래대금 > 평균 * volume_expansion
  - 기대 이동폭(ATR 기준 목표가)이 손익분기를 넘김

청산 조건 (OR)
  - 하드 손절: 진입가 - ATR * stop_atr
  - 트레일링 스톱: 보유 중 최고가 - ATR * trail_atr (익절 전환 후 활성)
  - 목표 순익 도달: net_return >= take_profit_net
  - 레짐 이탈: EMA(fast) < EMA(slow)
  - 시간 손절: max_hold_bars 초과 시, 순손익 무관하게 정리
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .fees import CostModel
from .indicators import atr, donchian, ema, rsi, slope_pct, sma


@dataclass
class Candle:
    ts: str
    open: float
    high: float
    low: float
    close: float
    volume: float          # 체결량
    value: float           # 체결대금(KRW)

    @classmethod
    def from_upbit(cls, row: dict) -> "Candle":
        return cls(
            ts=row.get("candle_date_time_kst") or row.get("candle_date_time_utc", ""),
            open=float(row["opening_price"]),
            high=float(row["high_price"]),
            low=float(row["low_price"]),
            close=float(row["trade_price"]),
            volume=float(row["candle_acc_trade_volume"]),
            value=float(row["candle_acc_trade_price"]),
        )


@dataclass
class StrategyParams:
    ema_fast: int = 20
    ema_slow: int = 60
    trend_slope_lookback: int = 10
    breakout_period: int = 20
    rsi_period: int = 14
    rsi_min: float = 50.0
    rsi_max: float = 78.0
    atr_period: int = 14
    atr_cost_multiple: float = 4.0     # ATR% 가 손익분기의 몇 배 이상이어야 진입하는가
    atr_max_pct: float = 0.05          # 봉당 ATR 5% 초과 = 카오스, 진입 금지
    volume_period: int = 20
    volume_expansion: float = 1.2
    stop_atr: float = 1.5              # 손절: ATR * 1.5
    trail_atr: float = 2.0             # 트레일링: 최고가 - ATR * 2.0
    take_profit_atr: float = 3.0       # 목표: ATR * 3.0 (손익비 2:1)
    min_net_target: float = 0.004      # 목표 순익 하한 0.4% (= 비용의 2배)
    max_hold_bars: int = 96            # 5분봉 96봉 = 8시간
    warmup_bars: int = 80


@dataclass
class Snapshot:
    """한 시점의 지표 묶음. 로그와 판단 근거를 동시에 남기기 위한 구조."""
    ts: str
    close: float
    ema_fast: float | None = None
    ema_slow: float | None = None
    slow_slope: float | None = None
    rsi: float | None = None
    atr: float | None = None
    atr_pct: float | None = None
    donchian_up: float | None = None
    donchian_dn: float | None = None
    vol_ratio: float | None = None
    regime: str = "unknown"

    def ready(self) -> bool:
        return None not in (self.ema_fast, self.ema_slow, self.rsi, self.atr, self.donchian_up)


@dataclass
class Signal:
    action: str                      # "buy" | "sell" | "hold"
    reason: str
    snapshot: Snapshot
    meta: dict[str, Any] = field(default_factory=dict)


class FeeAwareTrendStrategy:
    def __init__(self, params: StrategyParams, cost: CostModel) -> None:
        self.p = params
        self.cost = cost

    # ------------------------------------------------------------- 지표 계산
    def compute(self, candles: Sequence[Candle]) -> list[Snapshot]:
        closes = [c.close for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        values = [c.value for c in candles]

        ef = ema(closes, self.p.ema_fast)
        es = ema(closes, self.p.ema_slow)
        slope = slope_pct(es, self.p.trend_slope_lookback)
        rs = rsi(closes, self.p.rsi_period)
        at = atr(highs, lows, closes, self.p.atr_period)
        up, dn = donchian(highs, lows, self.p.breakout_period)
        vol_avg = sma(values, self.p.volume_period)

        snaps: list[Snapshot] = []
        for i, candle in enumerate(candles):
            atr_pct = (at[i] / candle.close) if (at[i] and candle.close) else None
            vol_ratio = (values[i] / vol_avg[i]) if vol_avg[i] else None
            snap = Snapshot(
                ts=candle.ts,
                close=candle.close,
                ema_fast=ef[i],
                ema_slow=es[i],
                slow_slope=slope[i],
                rsi=rs[i],
                atr=at[i],
                atr_pct=atr_pct,
                donchian_up=up[i],
                donchian_dn=dn[i],
                vol_ratio=vol_ratio,
            )
            snap.regime = self.classify_regime(snap)
            snaps.append(snap)
        return snaps

    # ------------------------------------------------------------- 레짐 분류
    def classify_regime(self, s: Snapshot) -> str:
        """차트 '읽기'의 실체. 주관적 패턴이 아니라 재현 가능한 상태 분류다."""
        if not s.ready():
            return "unknown"
        cost_floor = self.cost.breakeven_edge * self.p.atr_cost_multiple
        if s.atr_pct is not None and s.atr_pct > self.p.atr_max_pct:
            return "chaos"           # 변동성 과열: 슬리피지 예측 불가 → 관망
        if s.atr_pct is not None and s.atr_pct < cost_floor:
            return "dead"            # 비용 대비 움직임 부족: 매매할수록 손해
        up_trend = s.ema_fast > s.ema_slow and (s.slow_slope or 0.0) > 0
        down_trend = s.ema_fast < s.ema_slow and (s.slow_slope or 0.0) < 0
        if up_trend:
            return "bull_trend"
        if down_trend:
            return "bear_trend"
        return "range"

    # ------------------------------------------------------------- 진입 판단
    def entry_signal(self, candles: Sequence[Candle], snaps: list[Snapshot]) -> Signal:
        i = len(snaps) - 1
        s = snaps[i]
        if i < self.p.warmup_bars or not s.ready():
            return Signal("hold", "워밍업 부족", s)

        if s.regime == "chaos":
            return Signal("hold", f"변동성 과열 ATR%={pct(s.atr_pct)} > {pct(self.p.atr_max_pct)}", s)
        if s.regime == "dead":
            floor = self.cost.breakeven_edge * self.p.atr_cost_multiple
            return Signal("hold", f"기대이동폭 부족 ATR%={pct(s.atr_pct)} < 비용기준 {pct(floor)}", s)
        if s.regime != "bull_trend":
            return Signal("hold", f"레짐 불일치 regime={s.regime}", s)

        if s.close <= (s.donchian_up or float("inf")):
            return Signal("hold", f"돌파 미성립 close={s.close:,.0f} <= {s.donchian_up:,.0f}", s)

        if not (self.p.rsi_min <= (s.rsi or 0) <= self.p.rsi_max):
            return Signal("hold", f"RSI 범위 밖 {s.rsi:.1f} ∉ [{self.p.rsi_min},{self.p.rsi_max}]", s)

        if (s.vol_ratio or 0) < self.p.volume_expansion:
            return Signal("hold", f"거래대금 미확장 ratio={s.vol_ratio or 0:.2f} < {self.p.volume_expansion}", s)

        # 비용을 넘길 목표가가 실제로 설정되는지 최종 확인
        target_net = self.target_net(s)
        if target_net <= self.cost.breakeven_edge:
            return Signal("hold", f"목표 순익 {pct(target_net)} <= 손익분기 {pct(self.cost.breakeven_edge)}", s)

        stop_price = s.close - (s.atr or 0) * self.p.stop_atr
        take_price = self.cost.required_exit_price(s.close, target_net)
        risk = (s.close - stop_price) / s.close if s.close else 0.0
        reward = (take_price - s.close) / s.close if s.close else 0.0
        rr = reward / risk if risk > 0 else 0.0

        return Signal(
            "buy",
            (
                f"돌파진입 regime={s.regime} close={s.close:,.0f} > D{self.p.breakout_period}={s.donchian_up:,.0f} "
                f"ATR%={pct(s.atr_pct)} RSI={s.rsi:.1f} vol×{s.vol_ratio:.2f} "
                f"목표순익={pct(target_net)} 손절={pct(-risk)} RR={rr:.2f}"
            ),
            s,
            {
                "stop_price": stop_price,
                "take_price": take_price,
                "target_net": target_net,
                "risk_pct": risk,
                "rr": rr,
            },
        )

    def target_net(self, s: Snapshot) -> float:
        """목표 순수익률 = max(ATR 기반 목표, 비용 하한)."""
        atr_target = ((s.atr or 0.0) * self.p.take_profit_atr) / s.close if s.close else 0.0
        atr_net = self.cost.gross_to_net(atr_target)
        return max(atr_net, self.p.min_net_target)

    # ------------------------------------------------------------- 청산 판단
    def exit_signal(self, position: "PositionView", snaps: list[Snapshot]) -> Signal:
        s = snaps[-1]
        if not s.ready():
            return Signal("hold", "지표 미준비", s)

        net = self.cost.net_return(position.entry_price, s.close)

        if s.close <= position.stop_price:
            return Signal("sell", f"하드손절 close={s.close:,.0f} <= stop={position.stop_price:,.0f} net={pct(net)}", s,
                          {"exit_kind": "stop"})

        trail = position.peak_price - (s.atr or 0.0) * self.p.trail_atr
        if position.armed and s.close <= trail:
            return Signal("sell", f"트레일링 청산 close={s.close:,.0f} <= trail={trail:,.0f} net={pct(net)}", s,
                          {"exit_kind": "trail"})

        if net >= position.target_net:
            return Signal("sell", f"목표달성 net={pct(net)} >= {pct(position.target_net)}", s,
                          {"exit_kind": "take_profit"})

        if s.ema_fast < s.ema_slow:
            return Signal("sell", f"레짐 이탈 EMA{self.p.ema_fast}<EMA{self.p.ema_slow} net={pct(net)}", s,
                          {"exit_kind": "regime"})

        if position.bars_held >= self.p.max_hold_bars:
            return Signal("sell", f"시간손절 {position.bars_held}봉 경과 net={pct(net)}", s,
                          {"exit_kind": "time"})

        return Signal("hold", f"보유 유지 net={pct(net)} peak={position.peak_price:,.0f}", s)


@dataclass
class PositionView:
    """전략이 청산 판단에 필요한 최소 포지션 정보."""
    market: str
    entry_price: float
    qty: float
    stop_price: float
    target_net: float
    peak_price: float
    bars_held: int
    armed: bool = False     # 트레일링 스톱 활성화 여부 (손익분기 돌파 후 True)


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.3f}%"

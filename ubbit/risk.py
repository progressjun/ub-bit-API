"""리스크 관리. 전략이 '언제 살까'를 정하면, 여기서 '얼마나, 언제까지 멈출까'를 정한다.

자동매매에서 계좌를 실제로 죽이는 것은 잘못된 신호가 아니라
사이즈와 정지 규칙의 부재다. 그래서 게이트는 전부 하드 정지로 둔다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .fees import CostModel


@dataclass
class RiskParams:
    risk_per_trade: float = 0.005       # 1회 거래에서 감수할 자본 비율 (0.5%)
    max_position_pct: float = 0.20      # 단일 종목 최대 비중
    max_total_exposure: float = 0.60    # 전체 투입 비중 상한 (현금 40% 유지)
    max_concurrent: int = 3             # 동시 보유 종목 수
    daily_loss_limit: float = 0.03      # 일일 -3% 도달 시 당일 매매 중단
    daily_trade_limit: int = 8          # 일일 신규 진입 횟수 상한 (수수료 드래그 통제)
    consecutive_loss_pause: int = 3     # 연속 손절 N회 시 쿨다운
    cooldown_minutes: int = 60
    reentry_cooldown_minutes: int = 30  # 같은 종목 청산 후 재진입 금지 시간
    min_order_krw: float = 5_000.0
    max_order_krw: float = 1_000_000.0
    # 최소 주문금액 대비 안전 배수. 왜 필요한가:
    #   업비트 최소 주문금액은 매수뿐 아니라 매도에도 적용된다.
    #   정확히 5,000원어치를 샀다가 가격이 1%만 빠지면 평가액이 4,950원이 되고,
    #   그 순간 매도 주문이 거부된다. 손절이 영원히 실패하는 상태가 된다.
    #   2.0 이면 50% 하락까지 매도 가능 금액이 유지된다.
    dust_guard: float = 2.0
    # 신호 강도에 따른 사이즈 배수 상한. 1.0 이면 강도와 무관하게 동일 사이즈.
    conviction_max: float = 1.6


@dataclass
class RiskState:
    day: date = field(default_factory=date.today)
    day_start_equity: float = 0.0
    realized_pnl_today: float = 0.0
    trades_today: int = 0
    consecutive_losses: int = 0
    paused_until: datetime | None = None
    last_exit_at: dict[str, datetime] = field(default_factory=dict)


class RiskManager:
    def __init__(self, params: RiskParams, cost: CostModel) -> None:
        self.p = params
        self.cost = cost
        self.state = RiskState()

    # --------------------------------------------------------------- 일 단위
    def roll_day(self, equity: float, now: datetime | None = None) -> None:
        now = now or datetime.now()
        if self.state.day != now.date() or self.state.day_start_equity == 0.0:
            self.state.day = now.date()
            self.state.day_start_equity = equity
            self.state.realized_pnl_today = 0.0
            self.state.trades_today = 0

    def daily_drawdown(self) -> float:
        if self.state.day_start_equity <= 0:
            return 0.0
        return self.state.realized_pnl_today / self.state.day_start_equity

    # --------------------------------------------------------------- 진입 게이트
    def can_open(
        self,
        market: str,
        equity: float,
        open_positions: int,
        exposure_krw: float,
        now: datetime | None = None,
    ) -> tuple[bool, str]:
        now = now or datetime.now()
        self.roll_day(equity, now)

        if self.state.paused_until and now < self.state.paused_until:
            return False, f"쿨다운 중 (~{self.state.paused_until:%H:%M})"
        if self.daily_drawdown() <= -self.p.daily_loss_limit:
            return False, f"일일 손실한도 도달 {self.daily_drawdown()*100:.2f}% <= -{self.p.daily_loss_limit*100:.1f}%"
        if self.state.trades_today >= self.p.daily_trade_limit:
            return False, f"일일 매매횟수 상한 {self.state.trades_today}/{self.p.daily_trade_limit}"
        if open_positions >= self.p.max_concurrent:
            return False, f"동시보유 상한 {open_positions}/{self.p.max_concurrent}"
        if equity > 0 and exposure_krw / equity >= self.p.max_total_exposure:
            return False, f"총 익스포저 상한 {exposure_krw/equity*100:.1f}% >= {self.p.max_total_exposure*100:.0f}%"
        last_exit = self.state.last_exit_at.get(market)
        if last_exit and (now - last_exit).total_seconds() < self.p.reentry_cooldown_minutes * 60:
            return False, f"{market} 재진입 쿨다운"
        return True, "ok"

    # --------------------------------------------------------------- 사이징
    @property
    def entry_floor_krw(self) -> float:
        """진입 최소 금액. 최소 주문금액 × 안전 배수.

        이 아래로는 사지 않는다. 사더라도 못 파는 포지션이 되기 때문이다.
        """
        return self.p.min_order_krw * max(self.p.dust_guard, 1.0)

    def sellable(self, value_krw: float) -> bool:
        """지금 이 평가금액으로 시장가 매도가 가능한가."""
        return value_krw >= self.p.min_order_krw

    def position_size_krw(
        self,
        equity: float,
        available_krw: float,
        entry_price: float,
        stop_price: float,
        exposure_krw: float,
        conviction: float = 0.0,
    ) -> tuple[float, str]:
        """손절폭 기반 사이징. 손절에 걸렸을 때의 손실이 risk_per_trade 가 되도록 역산한다.

        고정 금액 배팅이 아니라 손절폭 역산을 쓰는 이유: 변동성이 큰 종목에
        같은 금액을 넣으면 같은 신호여도 손실 크기가 몇 배씩 달라진다.
        """
        if entry_price <= 0 or stop_price <= 0 or stop_price >= entry_price:
            return 0.0, "손절가 무효"

        stop_distance = (entry_price - stop_price) / entry_price
        # 손절 체결 시 실제 손실률은 비용까지 포함해야 한다
        loss_at_stop = abs(self.cost.net_return(entry_price, stop_price))
        if loss_at_stop <= 0:
            return 0.0, "손실률 계산 실패"

        # 신호가 강할수록 사이즈를 키운다. 단 리스크 한도 자체는 건드리지 않는다.
        # 키우는 것은 '이번 거래에 거는 자본 비율'이지 '손절 폭'이 아니다.
        boost = 1.0 + max(min(conviction, 1.0), 0.0) * (max(self.p.conviction_max, 1.0) - 1.0)
        size = equity * self.p.risk_per_trade * boost / loss_at_stop
        caps = {
            "risk": size,
            "position_pct": equity * self.p.max_position_pct,
            "exposure": max(equity * self.p.max_total_exposure - exposure_krw, 0.0),
            "cash": available_krw * 0.995,          # 수수료/체결오차 여유
            "max_order": self.p.max_order_krw,
        }
        size = min(caps.values())
        binding = min(caps, key=lambda k: caps[k])

        floor = self.entry_floor_krw
        if size < floor:
            return 0.0, (f"진입 최소금액 미달 {size:,.0f} < {floor:,.0f}원 "
                         f"(최소주문 {self.p.min_order_krw:,.0f} × 안전배수 "
                         f"{self.p.dust_guard:.1f}, 제약: {binding})")
        note = f"사이즈 {size:,.0f}원 (손절폭 {stop_distance*100:.2f}%, 제약: {binding}"
        if boost > 1.001:
            note += f", 신호강도 ×{boost:.2f}"
        return float(int(size)), note + ")"

    # --------------------------------------------------------------- 결과 반영
    def record_exit(self, market: str, realized_pnl: float, now: datetime | None = None) -> None:
        now = now or datetime.now()
        self.state.realized_pnl_today += realized_pnl
        self.state.last_exit_at[market] = now
        if realized_pnl < 0:
            self.state.consecutive_losses += 1
            if self.state.consecutive_losses >= self.p.consecutive_loss_pause:
                self.state.paused_until = now.replace(microsecond=0) + _minutes(self.p.cooldown_minutes)
                self.state.consecutive_losses = 0
        else:
            self.state.consecutive_losses = 0

    def record_entry(self) -> None:
        self.state.trades_today += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "day": str(self.state.day),
            "day_start_equity": self.state.day_start_equity,
            "realized_pnl_today": self.state.realized_pnl_today,
            "daily_dd_pct": self.daily_drawdown() * 100,
            "trades_today": self.state.trades_today,
            "consecutive_losses": self.state.consecutive_losses,
            "paused_until": self.state.paused_until.isoformat() if self.state.paused_until else None,
        }


def _minutes(n: int):
    from datetime import timedelta
    return timedelta(minutes=n)

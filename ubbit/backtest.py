"""백테스트. 실거래와 동일한 CostModel/Strategy/RiskManager 를 사용한다.

백테스트가 실거래보다 좋게 나오는 흔한 원인 셋을 구조적으로 차단했다.
  미래참조 : donchian 은 당봉을 제외한 직전 N봉만 본다. 판단은 종가 확정 후,
             체결은 다음 봉 시가로 처리한다.
  비용 누락 : 진입/청산 모두 CostModel.fill_* 를 통과한다.
  체결 가정 : 손절은 봉 내 저가가 아니라 다음 봉 시가로 체결한다(갭 반영).

그래도 재현하지 못하는 것: 호가 소진, 거래소 지연/장애, 본인의 개입.
따라서 백테스트 수치는 상한선이며, 실거래 기대치는 그보다 낮다고 본다.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Sequence

from .fees import CostModel
from .risk import RiskManager, RiskParams
from .strategy import Candle, FeeAwareTrendStrategy, PositionView, StrategyParams


@dataclass
class BTTrade:
    market: str
    entry_ts: str
    exit_ts: str
    entry_price: float
    exit_price: float
    qty: float
    entry_krw: float
    exit_krw: float
    fee: float
    net_pnl: float
    net_return: float
    bars: int
    exit_kind: str


@dataclass
class BTResult:
    initial: float
    final: float
    trades: list[BTTrade] = field(default_factory=list)
    equity_curve: list[tuple[str, float]] = field(default_factory=list)
    blocked: dict[str, int] = field(default_factory=dict)

    @property
    def total_return(self) -> float:
        return self.final / self.initial - 1.0 if self.initial else 0.0

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.net_pnl > 0)

    @property
    def win_rate(self) -> float:
        return self.wins / len(self.trades) if self.trades else 0.0

    @property
    def fees_paid(self) -> float:
        return sum(t.fee for t in self.trades)

    @property
    def profit_factor(self) -> float | None:
        gain = sum(t.net_pnl for t in self.trades if t.net_pnl > 0)
        loss = -sum(t.net_pnl for t in self.trades if t.net_pnl < 0)
        return gain / loss if loss > 0 else None

    @property
    def max_drawdown(self) -> float:
        peak = self.initial
        mdd = 0.0
        for _, value in self.equity_curve:
            peak = max(peak, value)
            if peak > 0:
                mdd = min(mdd, value / peak - 1.0)
        return mdd

    @property
    def avg_net_return(self) -> float:
        return sum(t.net_return for t in self.trades) / len(self.trades) if self.trades else 0.0

    @property
    def avg_bars(self) -> float:
        return sum(t.bars for t in self.trades) / len(self.trades) if self.trades else 0.0

    def summary(self, cost: CostModel, bars_per_day: float) -> str:
        n = len(self.trades)
        days = max(len(self.equity_curve) / bars_per_day, 1e-9)
        pf = self.profit_factor
        exits: dict[str, int] = {}
        for t in self.trades:
            exits[t.exit_kind] = exits.get(t.exit_kind, 0) + 1
        exit_mix = " ".join(f"{k}={v}" for k, v in sorted(exits.items(), key=lambda kv: -kv[1])) or "-"
        blocked = " ".join(f"{k}={v}" for k, v in sorted(self.blocked.items(), key=lambda kv: -kv[1])[:4]) or "-"
        fee_share = (self.fees_paid / abs(self.final - self.initial)) if self.final != self.initial else float("inf")
        return (
            f"기간 {days:.1f}일 | 거래 {n}건 ({n/days:.2f}건/일)\n"
            f"최종자산 {self.final:,.0f}원  총수익률 {self.total_return*100:+.2f}%  "
            f"MDD {self.max_drawdown*100:.2f}%\n"
            f"승률 {self.win_rate*100:.1f}%  손익비(PF) {_fmt_pf(pf)}"
            f"  평균 순수익/건 {self.avg_net_return*100:+.3f}%  평균보유 {self.avg_bars:.1f}봉\n"
            f"지불 수수료+슬리피지 {self.fees_paid:,.0f}원 "
            f"(손익 절대값 대비 {fee_share*100:.0f}%)\n"
            f"청산 사유: {exit_mix}\n"
            f"진입 차단 사유 상위: {blocked}\n"
            f"비용 전제: {cost.describe()}"
        )


def _fmt_pf(pf: float | None) -> str:
    return f"{pf:.2f}" if pf is not None else "n/a"


def run_backtest(
    market: str,
    candles: Sequence[Candle],
    *,
    cost: CostModel,
    sparams: StrategyParams,
    rparams: RiskParams,
    initial_krw: float = 1_000_000.0,
) -> BTResult:
    strategy = FeeAwareTrendStrategy(sparams, cost)
    risk = RiskManager(rparams, cost)
    snaps = strategy.compute(candles)

    cash = initial_krw
    result = BTResult(initial=initial_krw, final=initial_krw)
    position: PositionView | None = None
    entry_krw = 0.0
    entry_ts = ""
    entry_bar = 0
    pending: tuple[str, dict] | None = None   # 다음 봉 시가에 체결할 주문

    for i in range(len(candles)):
        candle = candles[i]

        # --- 직전 봉에서 확정된 주문을 이번 봉 시가로 체결 -------------------
        if pending:
            kind, meta = pending
            pending = None
            if kind == "buy":
                qty, fill_price, fee = cost.fill_buy(meta["krw"], candle.open)
                if qty > 0:
                    cash -= meta["krw"]
                    entry_krw = meta["krw"]
                    entry_ts = candle.ts
                    entry_bar = i
                    position = PositionView(
                        market=market, entry_price=fill_price, qty=qty,
                        stop_price=fill_price - meta["atr"] * sparams.stop_atr,
                        target_net=meta["target_net"], peak_price=fill_price,
                        bars_held=0, armed=False,
                    )
                    risk.record_entry()
            elif kind == "sell" and position:
                proceeds, fill_price, fee = cost.fill_sell(position.qty, candle.open)
                cash += proceeds
                net_pnl = proceeds - entry_krw
                entry_fee = entry_krw * cost.fee_buy / (1 + cost.fee_buy)
                result.trades.append(BTTrade(
                    market=market, entry_ts=entry_ts, exit_ts=candle.ts,
                    entry_price=position.entry_price, exit_price=fill_price,
                    qty=position.qty, entry_krw=entry_krw, exit_krw=proceeds,
                    fee=entry_fee + fee, net_pnl=net_pnl,
                    net_return=net_pnl / entry_krw if entry_krw else 0.0,
                    bars=i - entry_bar, exit_kind=meta.get("exit_kind", "unknown"),
                ))
                risk.record_exit(market, net_pnl, now=_fake_now(candle.ts))
                position = None
                entry_krw = 0.0

        equity = cash + (position.qty * candle.close if position else 0.0)
        result.equity_curve.append((candle.ts, equity))
        result.final = equity

        window = snaps[: i + 1]
        if i < sparams.warmup_bars:
            continue

        # --- 청산 판단 (종가 확정 기준, 다음 봉 시가 체결) -------------------
        if position:
            position.bars_held = i - entry_bar
            position.peak_price = max(position.peak_price, candle.close)
            if not position.armed and cost.net_return(position.entry_price, candle.close) > 0:
                position.armed = True
            sig = strategy.exit_signal(position, window)
            if sig.action == "sell":
                pending = ("sell", {"exit_kind": sig.meta.get("exit_kind", "unknown")})
            continue

        # --- 진입 판단 -------------------------------------------------------
        sig = strategy.entry_signal(candles[: i + 1], window)
        if sig.action != "buy":
            key = sig.reason.split()[0]
            result.blocked[key] = result.blocked.get(key, 0) + 1
            continue

        risk.roll_day(equity, now=_fake_now(candle.ts))
        ok, why = risk.can_open(market, equity, 0, 0.0, now=_fake_now(candle.ts))
        if not ok:
            key = why.split()[0]
            result.blocked[key] = result.blocked.get(key, 0) + 1
            continue

        size, why = risk.position_size_krw(equity, cash, candle.close, sig.meta["stop_price"], 0.0)
        if size <= 0:
            result.blocked["사이즈"] = result.blocked.get("사이즈", 0) + 1
            continue

        pending = ("buy", {
            "krw": size,
            "atr": sig.snapshot.atr or 0.0,
            "target_net": sig.meta["target_net"],
        })

    return result


def fee_sensitivity(
    market: str,
    candles: Sequence[Candle],
    *,
    base_cost: CostModel,
    sparams: StrategyParams,
    rparams: RiskParams,
    initial_krw: float = 1_000_000.0,
) -> list[tuple[str, BTResult]]:
    """같은 전략을 비용 전제만 바꿔 돌린다.

    이 표에서 0% 수수료일 때만 흑자면 그 전략은 전략이 아니라 비용 차익이다.
    실거래에서는 반드시 손실로 끝난다.
    """
    scenarios = [
        ("수수료 0% (이론 상한)", replace(base_cost, fee_buy=0.0, fee_sell=0.0, slippage_buy=0.0, slippage_sell=0.0)),
        ("수수료만 0.05%", replace(base_cost, slippage_buy=0.0, slippage_sell=0.0)),
        ("기본 (수수료+슬리피지 5bp)", base_cost),
        ("슬리피지 2배 (10bp)", replace(base_cost, slippage_buy=0.001, slippage_sell=0.001)),
        ("슬리피지 4배 (20bp)", replace(base_cost, slippage_buy=0.002, slippage_sell=0.002)),
    ]
    out = []
    for label, cost in scenarios:
        # min_net_target 은 비용에 종속되므로 시나리오마다 하한을 재설정한다
        sp = replace(sparams, min_net_target=max(sparams.min_net_target, cost.breakeven_edge * 2))
        out.append((label, run_backtest(
            market, candles, cost=cost, sparams=sp, rparams=rparams, initial_krw=initial_krw
        )))
    return out


def _fake_now(ts: str):
    """캔들 타임스탬프를 datetime 으로. 리스크 매니저의 일/쿨다운 계산용."""
    from datetime import datetime
    try:
        return datetime.fromisoformat(ts.replace("Z", ""))
    except ValueError:
        return datetime.now()

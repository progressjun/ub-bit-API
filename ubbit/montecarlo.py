"""몬테카를로 수익성 시뮬레이션.

백테스트는 '과거에 실제로 일어난 한 경로'의 결과다. 그 경로가 운이었을
가능성을 분리하지 못한다. 같은 전략을 다시 1년 돌리면 무슨 일이 벌어지는가를
답하려면 분포가 필요하다.

두 가지를 서로 다른 강도로 돌린다.

  1) 거래 순서 재표집 (trade bootstrap)
     실현된 거래의 순수익률을 블록 단위로 복원추출해 자본 곡선을 다시 만든다.
     빠르고, 손익 분포가 같을 때 '순서 운'이 결과를 얼마나 흔드는지 본다.
     한계: 거래 분포 자체가 미래에도 같다고 가정한다. 이건 강한 가정이다.

  2) 합성 시장 경로 (synthetic path)
     과거 수익률을 블록 부트스트랩해 '일어난 적 없는 가격 시계열'을 만들고,
     그 위에서 전략을 처음부터 다시 돌린다. 진입 판단까지 전부 재실행되므로
     과거 경로에 대한 과적합을 직접 때린다. 느리지만 훨씬 강한 검정이다.

블록 부트스트랩을 쓰는 이유: 수익률을 한 개씩 뽑으면 추세와 연속 손실이
사라진다. 추세추종 전략은 연속성이 있어야 평가가 성립한다. 블록 단위로
뽑아야 '연달아 손절 맞는 구간'이 귀무세계에도 존재한다.
"""
from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field
from typing import Sequence

from .backtest import BTTrade, run_backtest
from .fees import CostModel
from .risk import RiskParams
from .strategy import Candle, StrategyParams


@dataclass
class MCParams:
    iterations: int = 10_000
    block_size: int = 5           # 연속 손익 구조 보존
    horizon_trades: int = 0       # 0 = 표본과 같은 거래 수
    ruin_drawdown: float = 0.30   # 이 낙폭에 닿으면 '사실상 중단'으로 본다
    seed: int = 20260921


@dataclass
class MCResult:
    label: str
    iterations: int
    horizon: int
    returns: list[float] = field(default_factory=list)
    drawdowns: list[float] = field(default_factory=list)
    trade_counts: list[int] = field(default_factory=list)
    p_profit: float = 0.0
    p_ruin: float = 0.0
    p_loss_10: float = 0.0
    note: str = ""
    benchmark: list[float] = field(default_factory=list)   # 같은 경로의 단순보유
    benchmark_median: float | None = None
    p_beat_benchmark: float | None = None

    def q(self, values: Sequence[float], p: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = min(int(p * len(ordered)), len(ordered) - 1)
        return ordered[idx]

    def render(self) -> str:
        r, d = self.returns, self.drawdowns
        if not r:
            return f"[{self.label}] 표본 없음"
        median_trades = statistics.median(self.trade_counts) if self.trade_counts else self.horizon
        mag = [abs(v) for v in d]
        lines = [
            f"[{self.label}]  경로 {self.iterations:,}개 / 경로당 거래 중앙값 {median_trades:.0f}건",
            f"  수익률 분포   5%: {self.q(r,.05)*100:+8.2f}%   25%: {self.q(r,.25)*100:+8.2f}%"
            f"   50%: {self.q(r,.50)*100:+8.2f}%   75%: {self.q(r,.75)*100:+8.2f}%"
            f"   95%: {self.q(r,.95)*100:+8.2f}%",
            # 낙폭은 음수라 그대로 정렬하면 분위수 의미가 뒤집힌다. 크기로 본다.
            f"  최대낙폭 분포 50%: {self.q(mag,.50)*100:7.2f}%   75%: {self.q(mag,.75)*100:7.2f}%"
            f"   95%: {self.q(mag,.95)*100:7.2f}%   최악: {max(mag)*100:7.2f}%"
            f"   (깊을수록 나쁨)",
            f"  흑자 확률     {self.p_profit*100:.1f}%",
            f"  -10% 이하 확률 {self.p_loss_10*100:.1f}%",
            f"  -{int(self.ruin*100)}% 낙폭 도달 확률 {self.p_ruin*100:.1f}%",
        ]
        if self.benchmark_median is not None:
            lines.append(
                f"  단순보유 중앙값 {self.benchmark_median*100:+.2f}%  →  "
                f"전략 초과 {(self.q(r,.50) - self.benchmark_median)*100:+.2f}%p, "
                f"단순보유를 이길 확률 {self.p_beat_benchmark*100:.1f}%")
        if self.note:
            lines.append(f"  {self.note}")
        return "\n".join(lines)

    ruin: float = 0.30


def _block_indices(n: int, horizon: int, block: int, rng: random.Random) -> list[int]:
    """블록 부트스트랩 인덱스. 연속 구간을 통째로 뽑아 자기상관을 보존한다."""
    out: list[int] = []
    while len(out) < horizon:
        start = rng.randrange(n)
        for k in range(block):
            out.append((start + k) % n)
            if len(out) >= horizon:
                break
    return out


def _path_stats(equity_path: Sequence[float], initial: float) -> tuple[float, float]:
    peak = initial
    mdd = 0.0
    for value in equity_path:
        peak = max(peak, value)
        if peak > 0:
            mdd = min(mdd, value / peak - 1.0)
    final = equity_path[-1] if equity_path else initial
    return final / initial - 1.0, mdd


# ------------------------------------------------------- 1) 거래 순서 재표집
def simulate_trades(
    trades: Sequence[BTTrade],
    *,
    initial_krw: float = 1_000_000.0,
    params: MCParams | None = None,
) -> MCResult:
    """실현 거래를 블록 복원추출해 자본 곡선을 다시 만든다.

    베팅 비중은 실제 기록(entry_krw / equity_at_entry)을 그대로 쓴다.
    고정 금액이 아니라 비중으로 재현해야 복리 효과가 맞는다.
    """
    p = params or MCParams()
    rng = random.Random(p.seed)
    if not trades:
        return MCResult("거래 재표집", 0, 0, note="거래 없음")

    pairs = [
        (t.net_return, (t.entry_krw / t.equity_at_entry) if t.equity_at_entry else 0.2)
        for t in trades
    ]
    horizon = p.horizon_trades or len(pairs)
    result = MCResult("거래 순서 재표집", p.iterations, horizon, ruin=p.ruin_drawdown)

    for _ in range(p.iterations):
        equity = initial_krw
        path = [equity]
        for idx in _block_indices(len(pairs), horizon, p.block_size, rng):
            net, weight = pairs[idx]
            equity *= 1.0 + net * weight
            path.append(equity)
            if equity <= initial_krw * 0.05:      # 사실상 전멸
                break
        ret, mdd = _path_stats(path, initial_krw)
        result.returns.append(ret)
        result.drawdowns.append(mdd)
        result.trade_counts.append(horizon)

    n = len(result.returns)
    result.p_profit = sum(1 for r in result.returns if r > 0) / n
    result.p_loss_10 = sum(1 for r in result.returns if r <= -0.10) / n
    result.p_ruin = sum(1 for d in result.drawdowns if d <= -p.ruin_drawdown) / n
    result.note = ("가정: 거래 손익 분포가 미래에도 동일. 시장 국면이 바뀌면 이 분포가 먼저 바뀐다.")
    return result


# ------------------------------------------------------- 2) 합성 시장 경로
def synthesize(candles: Sequence[Candle], block: int, rng: random.Random) -> list[Candle]:
    """과거 수익률을 블록 부트스트랩해 새 가격 시계열을 만든다.

    봉의 고가/저가/거래대금은 원본 봉의 '종가 대비 비율'을 그대로 옮겨 붙인다.
    변동성 구조와 거래대금 패턴을 유지해야 ATR·거래대금 필터가 의미를 갖는다.
    """
    n = len(candles)
    if n < 3:
        return list(candles)

    logret = [math.log(candles[i].close / candles[i - 1].close)
              for i in range(1, n) if candles[i - 1].close > 0]
    if not logret:
        return list(candles)

    idx = _block_indices(len(logret), n - 1, block, rng)
    out = [candles[0]]
    price = candles[0].close
    for step, j in enumerate(idx, start=1):
        src = candles[j + 1]
        price *= math.exp(logret[j])
        base = src.close or 1.0
        out.append(Candle(
            ts=candles[step].ts,
            open=price * (src.open / base),
            high=price * (src.high / base),
            low=price * (src.low / base),
            close=price,
            volume=src.volume,
            value=src.value,
        ))
    return out


def simulate_engine(
    market_candles: dict[str, Sequence[Candle]],
    cfg,
    *,
    iterations: int = 40,
    block: int = 20,
    seed: int = 20260921,
    ruin_drawdown: float = 0.30,
    start_frac: float = 0.15,
    progress=None,
) -> MCResult:
    """합성 경로 위에서 TradingEngine 전체를 돌린다.

    종목별 백테스트를 평균내면 포트폴리오 의미가 틀어진다. 실제 엔진은
    하나의 자본으로 전 종목을 돌리고 동시보유·일일한도로 스스로를 제한한다.
    그 제약까지 포함한 수익률이 사용자가 실제로 받는 숫자다.

    비교 기준으로 같은 합성 경로의 단순보유(종목 균등) 수익률을 함께 잰다.
    전략이 시장보다 나은지가 핵심이고, 절대 수익률만으로는 답이 안 나온다.
    """
    import os
    import tempfile
    from dataclasses import replace as _replace

    from .engine import TradingEngine
    from .replay import run_replay

    import logging

    rng = random.Random(seed)
    result = MCResult("엔진 합성 경로", iterations, 0, ruin=ruin_drawdown)
    result.benchmark = []
    tmpdir = tempfile.mkdtemp(prefix="ubbit-mc-")

    # 경로 하나당 수천 줄의 엔진 로그가 나온다. 시뮬레이션 동안만 억제한다.
    engine_log = logging.getLogger("engine")
    saved_level = engine_log.level
    engine_log.setLevel(logging.ERROR)

    for it in range(iterations):
        fake = {m: synthesize(c, block, rng) for m, c in market_candles.items()}
        length = max(len(c) for c in fake.values())
        start = max(cfg.strategy.warmup_bars + 5, int(length * start_frac))

        run_cfg = _replace(
            cfg,
            engine=_replace(cfg.engine, db_path=os.path.join(tmpdir, f"{it}.db"),
                            universe_auto=False, markets=list(fake),
                            liquidity_min_value_krw=0.0,
                            reconcile_on_start=False),
        )
        engine = TradingEngine(run_cfg)
        try:
            run_replay(engine, fake, start_at=start)
            total, _, _ = engine.broker.equity({m: c[-1].close for m, c in fake.items()})
            curve = [r["total_krw"] for r in engine.store.conn.execute(
                "SELECT total_krw FROM equity ORDER BY ts").fetchall()]
            ret, mdd = _path_stats(curve or [total], cfg.engine.initial_krw)
            result.returns.append(ret)
            result.drawdowns.append(mdd)
            result.trade_counts.append(engine.store.performance()["trades"])
            hold = [c[-1].close / c[start].close - 1.0 for c in fake.values()]
            result.benchmark.append(sum(hold) / len(hold))
        finally:
            engine.store.close()
            try:
                os.remove(run_cfg.engine.db_path)
            except OSError:
                pass
        if progress:
            progress(it + 1, iterations)

    engine_log.setLevel(saved_level)

    n = len(result.returns) or 1
    result.horizon = int(statistics.median(result.trade_counts)) if result.trade_counts else 0
    result.p_profit = sum(1 for r in result.returns if r > 0) / n
    result.p_loss_10 = sum(1 for r in result.returns if r <= -0.10) / n
    result.p_ruin = sum(1 for d in result.drawdowns if d <= -ruin_drawdown) / n
    if result.benchmark:
        beats = sum(1 for r, b in zip(result.returns, result.benchmark) if r > b)
        result.p_beat_benchmark = beats / n
        result.benchmark_median = statistics.median(result.benchmark)
    result.note = "엔진 전체(동시보유·일일한도·사이징 포함) 재실행. 호가 소진·거래소 지연은 미반영."
    return result


def simulate_synthetic(
    market_candles: dict[str, Sequence[Candle]],
    *,
    cost: CostModel,
    sparams: StrategyParams,
    rparams: RiskParams,
    initial_krw: float = 1_000_000.0,
    iterations: int = 200,
    block: int = 20,
    seed: int = 20260921,
    ruin_drawdown: float = 0.30,
) -> MCResult:
    """합성 시계열 위에서 전략을 처음부터 재실행한다.

    경로마다 전 종목을 새로 만들고, 종목별 결과를 자본 비중으로 합산한다.
    진입 판단부터 다시 돌기 때문에 '과거 그 자리에서만 통했던' 규칙은 여기서 무너진다.
    """
    rng = random.Random(seed)
    result = MCResult("합성 시장 경로", iterations, 0, ruin=ruin_drawdown)
    markets = list(market_candles)
    if not markets:
        return result

    for _ in range(iterations):
        rets: list[float] = []
        mdds: list[float] = []
        trades = 0
        for market in markets:
            fake = synthesize(market_candles[market], block, rng)
            bt = run_backtest(market, fake, cost=cost, sparams=sparams,
                              rparams=rparams, initial_krw=initial_krw)
            rets.append(bt.total_return)
            mdds.append(bt.max_drawdown)
            trades += len(bt.trades)
        # 종목에 균등 배분한 포트폴리오로 합산
        result.returns.append(sum(rets) / len(rets))
        result.drawdowns.append(sum(mdds) / len(mdds))
        result.trade_counts.append(trades)

    n = len(result.returns) or 1
    result.horizon = int(statistics.median(result.trade_counts)) if result.trade_counts else 0
    result.p_profit = sum(1 for r in result.returns if r > 0) / n
    result.p_loss_10 = sum(1 for r in result.returns if r <= -0.10) / n
    result.p_ruin = sum(1 for d in result.drawdowns if d <= -ruin_drawdown) / n
    result.note = ("가정: 수익률의 블록 구조가 미래에도 유지. 실제 체결·호가 소진은 미반영.")
    return result

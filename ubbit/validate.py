"""전략 엣지의 통계적 입증.

백테스트 수익률은 증거가 아니다. 조합을 충분히 많이 돌리면 아무 전략이나
흑자 구간이 나온다. 따라서 '입증'은 아래 네 가지를 동시에 통과하는 것으로 정의한다.

  표본      : 풀링된 OOS 거래 수 >= min_trades
              단일 종목 20건은 통계가 아니라 일화다. 종목을 풀링해 수백 건을 만든다.

  구간 분리 : 성과는 워크포워드 OOS 구간에서만 집계한다.
              학습에 쓴 구간의 성과는 보고하지 않는다.

  유의성    : 순열검정(permutation test) p-value < alpha / (검정한 가설 수)
              귀무가설은 "진입 시점 선택에 정보가 없다"이다. 같은 종목·같은 보유
              기간으로 무작위 진입을 반복해 귀무분포를 만들고, 관측값의 위치를 본다.
              본페로니 보정을 거는 이유: 가설을 3개 돌리면 하나가 우연히 유의할
              확률이 3배로 오른다.

  견고성    : 부트스트랩 95% 신뢰구간 하한 > 0
              그리고 수익 종목 비율 >= breadth
              한 종목의 대박에 기댄 성과는 다음 달에 사라진다.

하나라도 미달이면 결론은 '입증 실패'다. '유망함', '가능성 있음' 같은 중간
판정은 두지 않는다. 실거래 투입 여부는 이분법 결정이기 때문이다.
"""
from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field
from typing import Sequence

from .backtest import BTTrade, run_backtest
from .fees import CostModel
from .logutil import get_logger
from .risk import RiskParams
from .strategy import Candle, StrategyParams

log = get_logger("validate")


@dataclass
class ValidationRules:
    min_trades: int = 200          # 풀링 OOS 거래 수 하한
    min_markets: int = 8           # 최소 종목 수
    alpha: float = 0.05
    hypotheses: int = 1            # 본페로니 분모 (검정한 진입방식 × 타임프레임 수)
    bootstrap_iters: int = 5_000
    permutation_iters: int = 2_000
    breadth: float = 0.55          # 수익 종목 비율 하한
    max_top5_share: float = 0.60   # 상위 5거래가 총이익에서 차지하는 비중 상한
    seed: int = 20260921


@dataclass
class MarketResult:
    market: str
    trades: list[BTTrade]
    total_return: float
    max_drawdown: float


@dataclass
class Evidence:
    label: str
    markets: int
    trades: int
    mean_net: float                # 거래당 평균 순수익률
    median_net: float
    win_rate: float
    profit_factor: float | None
    ci_low: float                  # 부트스트랩 95% 신뢰구간
    ci_high: float
    p_value: float
    p_threshold: float
    breadth: float                 # 수익 종목 비율
    total_fees: float
    gross_edge_before_cost: float  # 비용 전 평균 수익률 (비용이 얼마나 먹는지)
    per_market: dict[str, float] = field(default_factory=dict)
    # 베타 검증 — 이 수익이 전략의 것인지 시장 상승의 것인지 가른다
    buy_hold_mean: float = 0.0      # 같은 구간 단순보유의 종목당 평균 수익률
    exposure_ratio: float = 0.0     # 시장에 노출된 시간 비율
    hold_matched_mean: float = 0.0  # 노출 시간을 맞춘 단순보유 기대수익 (공정 비교 기준)
    top5_pnl_share: float = 0.0     # 상위 5거래가 총이익에서 차지하는 비중
    failures: list[str] = field(default_factory=list)

    @property
    def proven(self) -> bool:
        return not self.failures

    def render(self) -> str:
        verdict = "입증됨" if self.proven else "입증 실패"
        lines = [
            f"[{self.label}]  판정: {verdict}",
            f"  종목 {self.markets}개 / OOS 거래 {self.trades}건",
            f"  거래당 평균 순수익  {self.mean_net*100:+.4f}%   (중앙값 {self.median_net*100:+.4f}%)",
            f"  비용 전 평균       {self.gross_edge_before_cost*100:+.4f}%  "
            f"→ 비용이 {abs(self.gross_edge_before_cost - self.mean_net)*100:.4f}%p 잠식",
            f"  승률 {self.win_rate*100:.1f}%  손익비 "
            + (f"{self.profit_factor:.2f}" if self.profit_factor else "n/a"),
            f"  부트스트랩 95% CI   [{self.ci_low*100:+.4f}%, {self.ci_high*100:+.4f}%]",
            f"  순열검정 p-value    {self.p_value:.4f}  (기준 {self.p_threshold:.4f}, 본페로니 보정)",
            f"  수익 종목 비율      {self.breadth*100:.0f}%",
            f"  지불 수수료 총액    {self.total_fees:,.0f}원",
            f"  시장 노출 시간      {self.exposure_ratio*100:.1f}%",
            f"  단순보유 벤치마크    종목당 {self.buy_hold_mean*100:+.2f}%  "
            f"(노출시간 보정 {self.hold_matched_mean*100:+.4f}%/거래)",
            f"  상위 5거래 이익비중  {self.top5_pnl_share*100:.0f}%",
        ]
        if self.failures:
            lines.append("  탈락 사유:")
            lines.extend(f"    · {f}" for f in self.failures)
        return "\n".join(lines)


# ------------------------------------------------------------------ 통계 도구
def bootstrap_ci(
    values: Sequence[float], iters: int, rng: random.Random, level: float = 0.95
) -> tuple[float, float]:
    """거래별 순수익률의 평균에 대한 퍼센타일 부트스트랩 신뢰구간.

    거래 순서를 가정하지 않는다. 자기상관이 있으면 구간이 좁게 나올 수 있으나,
    여기서는 '평균이 0보다 큰가'라는 방향성 판정에만 쓴다.
    """
    n = len(values)
    if n < 2:
        return (0.0, 0.0)
    means = []
    for _ in range(iters):
        means.append(sum(values[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    lo = means[int((1 - level) / 2 * iters)]
    hi = means[int((1 + level) / 2 * iters) - 1]
    return lo, hi


def permutation_pvalue(
    observed_mean: float,
    market_candles: dict[str, Sequence[Candle]],
    trade_specs: list[tuple[str, int]],
    cost: CostModel,
    iters: int,
    rng: random.Random,
) -> float:
    """귀무가설: 진입 시점 선택에 정보가 없다.

    실제 거래와 같은 (종목, 보유봉수) 조합으로 무작위 시점에 진입했다고 가정하고
    평균 순수익률의 귀무분포를 만든다. 관측 평균이 그 분포의 상위 몇 %인지가 p-value.

    보유 기간 분포를 고정하는 이유: 보유가 길면 변동성이 커져 평균이 달라진다.
    그 효과를 귀무분포에도 똑같이 넣어야 '진입 시점 선택'만 검정된다.
    """
    if not trade_specs:
        return 1.0
    ge = 0
    for _ in range(iters):
        sample = []
        for market, bars in trade_specs:
            candles = market_candles.get(market)
            if not candles or len(candles) <= bars + 2:
                continue
            i = rng.randrange(0, len(candles) - bars - 1)
            entry = candles[i + 1].open          # 실거래와 동일하게 다음 봉 시가 체결
            exit_ = candles[i + 1 + bars].open
            sample.append(cost.net_return(entry, exit_))
        if not sample:
            continue
        if sum(sample) / len(sample) >= observed_mean:
            ge += 1
    return (ge + 1) / (iters + 1)          # +1 보정: p=0 을 보고하지 않는다


# -------------------------------------------------------------------- 검증
def collect_oos_trades(
    market_candles: dict[str, Sequence[Candle]],
    *,
    cost: CostModel,
    params: StrategyParams,
    rparams: RiskParams,
    train_bars: int,
    test_bars: int,
    initial_krw: float = 1_000_000.0,
) -> list[MarketResult]:
    """고정 파라미터를 학습 구간 이후 전 구간에 적용해 OOS 거래를 수집한다.

    파라미터 탐색 없이 '설정 그대로'를 검증하는 경로다. 탐색을 섞으면
    검정해야 할 가설 수가 폭증해 본페로니 분모가 커지고, 같은 표본으로는
    어떤 결과도 유의하지 않게 된다.
    """
    out: list[MarketResult] = []
    for market, candles in market_candles.items():
        if len(candles) < train_bars + test_bars:
            continue
        oos = candles[train_bars:]          # 앞 구간은 파라미터 설계에 쓴 것으로 간주
        result = run_backtest(market, oos, cost=cost, sparams=params,
                              rparams=rparams, initial_krw=initial_krw)
        out.append(MarketResult(market, result.trades, result.total_return,
                                result.max_drawdown))
    return out


def evaluate(
    label: str,
    results: list[MarketResult],
    market_candles: dict[str, Sequence[Candle]],
    *,
    cost: CostModel,
    rules: ValidationRules,
) -> Evidence:
    rng = random.Random(rules.seed)
    all_trades = [t for r in results for t in r.trades]
    traded_markets = [r for r in results if r.trades]
    returns = [t.net_return for t in all_trades]

    if not returns:
        return Evidence(
            label=label, markets=len(traded_markets), trades=0, mean_net=0.0,
            median_net=0.0, win_rate=0.0, profit_factor=None, ci_low=0.0, ci_high=0.0,
            p_value=1.0, p_threshold=rules.alpha / max(rules.hypotheses, 1),
            breadth=0.0, total_fees=0.0, gross_edge_before_cost=0.0,
            failures=["OOS 거래 0건 — 검정 불가"])

    mean_net = sum(returns) / len(returns)
    gains = sum(r for r in returns if r > 0)
    losses = -sum(r for r in returns if r < 0)
    ci_low, ci_high = bootstrap_ci(returns, rules.bootstrap_iters, rng)

    specs = [(t.market, max(t.bars, 1)) for t in all_trades]
    p_value = permutation_pvalue(mean_net, market_candles, specs, cost,
                                 rules.permutation_iters, rng)
    p_threshold = rules.alpha / max(rules.hypotheses, 1)

    per_market = {r.market: sum(t.net_pnl for t in r.trades) for r in traded_markets}
    profitable = sum(1 for v in per_market.values() if v > 0)
    breadth = profitable / len(per_market) if per_market else 0.0

    gross = sum((1 + t.net_return) * cost.breakeven_multiple - 1 for t in all_trades) / len(all_trades)

    # --- 베타 검증 -------------------------------------------------------
    # 단순보유 수익률: 전략이 거래한 종목의 OOS 구간 시작~끝 수익률
    bh: list[float] = []
    total_bars = 0
    for r in traded_markets:
        candles = market_candles.get(r.market)
        if not candles or len(candles) < 2:
            continue
        bh.append(candles[-1].close / candles[0].close - 1.0)
        total_bars += len(candles)
    buy_hold_mean = sum(bh) / len(bh) if bh else 0.0

    # 노출 시간 비율: 포지션을 들고 있던 봉수 / 전체 봉수
    held_bars = sum(max(t.bars, 1) for t in all_trades)
    exposure = held_bars / total_bars if total_bars else 0.0

    # 공정 비교: 같은 시간만 시장에 노출됐을 때 단순보유가 거래당 벌었을 기대값
    avg_bars = held_bars / len(all_trades)
    bars_per_market = total_bars / len(traded_markets) if traded_markets else 1
    hold_matched = buy_hold_mean * (avg_bars / bars_per_market) if bars_per_market else 0.0

    # 이익 집중도: 상위 5거래가 총이익의 대부분이면 재현성이 낮다
    profits = sorted((t.net_pnl for t in all_trades if t.net_pnl > 0), reverse=True)
    top5_share = (sum(profits[:5]) / sum(profits)) if profits else 0.0

    failures: list[str] = []
    if len(all_trades) < rules.min_trades:
        failures.append(f"OOS 거래 {len(all_trades)}건 < 기준 {rules.min_trades}건 (표본 부족)")
    if len(traded_markets) < rules.min_markets:
        failures.append(f"거래 발생 종목 {len(traded_markets)}개 < 기준 {rules.min_markets}개")
    if mean_net <= 0:
        failures.append(f"거래당 평균 순수익 {mean_net*100:+.4f}% <= 0")
    if ci_low <= 0:
        failures.append(f"부트스트랩 CI 하한 {ci_low*100:+.4f}% <= 0 (평균이 0과 구분되지 않음)")
    if p_value >= p_threshold:
        failures.append(f"순열검정 p={p_value:.4f} >= 기준 {p_threshold:.4f} "
                        f"(무작위 진입과 구분되지 않음)")
    if breadth < rules.breadth:
        failures.append(f"수익 종목 비율 {breadth*100:.0f}% < 기준 {rules.breadth*100:.0f}% "
                        f"(소수 종목 의존)")
    if mean_net > 0 and mean_net <= hold_matched:
        failures.append(f"노출시간 보정 단순보유 {hold_matched*100:+.4f}% 이상을 못 넘김 "
                        f"(전략 알파가 아니라 시장 베타)")
    if top5_share > rules.max_top5_share:
        failures.append(f"상위 5거래가 총이익의 {top5_share*100:.0f}% "
                        f"> 기준 {rules.max_top5_share*100:.0f}% (소수 거래 의존)")

    return Evidence(
        label=label, markets=len(traded_markets), trades=len(all_trades),
        mean_net=mean_net, median_net=statistics.median(returns),
        win_rate=sum(1 for r in returns if r > 0) / len(returns),
        profit_factor=(gains / losses) if losses > 0 else None,
        ci_low=ci_low, ci_high=ci_high, p_value=p_value, p_threshold=p_threshold,
        breadth=breadth, total_fees=sum(t.fee for t in all_trades),
        gross_edge_before_cost=gross, per_market=per_market,
        buy_hold_mean=buy_hold_mean, exposure_ratio=exposure,
        hold_matched_mean=hold_matched, top5_pnl_share=top5_share,
        failures=failures,
    )

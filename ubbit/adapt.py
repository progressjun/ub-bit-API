"""전략 파라미터의 주기적 재적합(walk-forward)과 승격 게이트.

문제 제기
  시장은 변한다. 고정 파라미터는 언젠가 열화된다. 따라서 재적합은 필요하다.
  그러나 재적합은 자동매매에서 가장 비싼 실패 경로이기도 하다.
  500일 4시간봉에서 거래가 20건이면, 파라미터 81조합 중 1등은 거의 확실히
  노이즈다. 그 1등을 실거래에 올리면 다음 달에 무너진다.

따라서 이 모듈은 '최적화기'가 아니라 '승격 심사기'로 설계했다.

  분리 원칙 : 최적화는 전략 파라미터만 건드린다.
              비용 모델(CostModel)과 리스크 게이트(RiskParams)는 절대 대상이 아니다.
              수수료를 낙관적으로 가정하면 모든 백테스트가 흑자로 보인다.

  검증 원칙 : 학습 구간에서 고른 파라미터는 반드시 미사용 구간(OOS)에서 재평가한다.
              보고하는 성과는 학습 성과가 아니라 OOS 성과다.

  승격 원칙 : 챔피언(현재 운영 중) 대비 도전자가 아래를 모두 만족해야 교체된다.
              - OOS 거래 수 >= min_trades (표본 하한)
              - OOS 수익률이 챔피언보다 margin 이상 우월
              - OOS MDD <= max_drawdown
              - 수익 구간 비율 >= consistency (한 폴드 대박에 의존하지 않을 것)
              하나라도 미달이면 챔피언을 유지한다. '교체하지 않음'이 기본값이다.

  탐색 원칙 : 그리드를 의도적으로 작게 유지한다. 조합 수가 많을수록
              표본 대비 자유도가 커져 과적합 확률이 올라간다.
"""
from __future__ import annotations

import itertools
import json
import os
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from typing import Any, Sequence

from .backtest import BTResult, run_backtest
from .fees import CostModel
from .logutil import get_logger
from .risk import RiskParams
from .strategy import Candle, StrategyParams

log = get_logger("adapt")

PARAM_FILE = "data/params.json"

# 탐색 대상. 의도적으로 4개 축, 총 3×3×3×3 = 81 조합으로 제한한다.
# 여기에 축을 추가하려면 표본(거래 수)을 먼저 늘려야 한다.
SEARCH_GRID: dict[str, list[Any]] = {
    "atr_cost_multiple": [3.0, 4.0, 6.0],
    "take_profit_atr": [2.0, 3.0, 4.0],
    "stop_atr": [1.0, 1.5, 2.0],
    "breakout_period": [10, 20, 40],
}


@dataclass
class PromotionRules:
    min_trades: int = 30              # OOS 최소 거래 수. 이하면 통계가 아니라 일화다.
    min_folds: int = 3
    margin: float = 0.01              # 챔피언 대비 최소 우월폭 (OOS 총수익률)
    max_drawdown: float = 0.15
    consistency: float = 0.6          # 수익 폴드 비율 하한
    require_positive: bool = True     # OOS 총수익률이 음수면 무조건 탈락


@dataclass
class WalkForwardConfig:
    train_bars: int = 1200
    test_bars: int = 300
    step_bars: int = 300


@dataclass
class OOSReport:
    params: dict[str, Any]
    total_return: float
    trades: int
    win_rate: float
    max_drawdown: float
    profit_factor: float | None
    positive_fold_ratio: float
    fold_returns: list[float] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _apply(base: StrategyParams, combo: dict[str, Any]) -> StrategyParams:
    return replace(base, **combo)


def _score(result: BTResult) -> float:
    """학습 구간 내 후보 선별용 점수.

    수익률만 보면 거래 1건짜리 요행이 1등이 된다. 그래서 거래 수가 적을수록
    점수를 깎고, MDD 로 나눠 위험조정 기준으로 만든다.
    """
    n = len(result.trades)
    if n == 0:
        return -1e9
    mdd = abs(result.max_drawdown) or 0.01
    sample_penalty = min(n / 20.0, 1.0)     # 20건 미만이면 비례 감점
    return (result.total_return / mdd) * sample_penalty


def walk_forward(
    market: str,
    candles: Sequence[Candle],
    *,
    cost: CostModel,
    base_params: StrategyParams,
    rparams: RiskParams,
    wf: WalkForwardConfig | None = None,
    grid: dict[str, list[Any]] | None = None,
    initial_krw: float = 1_000_000.0,
) -> OOSReport | None:
    """폴드별로 학습→선별→OOS 평가를 반복하고, OOS 성과만 집계해 보고한다."""
    wf = wf or WalkForwardConfig()
    grid = grid or SEARCH_GRID
    keys = list(grid)
    combos = [dict(zip(keys, values)) for values in itertools.product(*grid.values())]

    need = wf.train_bars + wf.test_bars
    if len(candles) < need:
        log.warning("[%s] 캔들 부족: %d봉 < 필요 %d봉", market, len(candles), need)
        return None

    fold_returns: list[float] = []
    oos_trades: list = []
    oos_equity: list[tuple[str, float]] = []
    chosen: list[dict[str, Any]] = []
    equity = initial_krw

    start = 0
    while start + need <= len(candles):
        train = candles[start : start + wf.train_bars]
        test = candles[start + wf.train_bars : start + need]

        best_combo, best_score = None, -1e18
        for combo in combos:
            params = _apply(base_params, combo)
            if params.min_net_target <= cost.breakeven_edge:
                continue                                    # 구조적으로 이길 수 없는 조합
            res = run_backtest(market, train, cost=cost, sparams=params,
                               rparams=rparams, initial_krw=initial_krw)
            score = _score(res)
            if score > best_score:
                best_combo, best_score = combo, score

        if best_combo is None:
            start += wf.step_bars
            continue

        # OOS 평가: 학습 구간에서 고른 파라미터를 미사용 구간에 그대로 적용
        oos = run_backtest(market, test, cost=cost, sparams=_apply(base_params, best_combo),
                           rparams=rparams, initial_krw=equity)
        fold_returns.append(oos.total_return)
        oos_trades.extend(oos.trades)
        oos_equity.extend(oos.equity_curve)
        chosen.append(best_combo)
        equity = oos.final
        log.info("[%s] fold %d: 학습선택=%s → OOS %+.2f%% (%d건)",
                 market, len(fold_returns), best_combo, oos.total_return * 100, len(oos.trades))
        start += wf.step_bars

    if not fold_returns:
        return None

    # 최종 권고 파라미터: 폴드별 선택 중 최빈값 (한 폴드의 우연에 끌려가지 않도록)
    winner = _mode_of(chosen)
    aggregate = BTResult(initial=initial_krw, final=equity, trades=oos_trades,
                         equity_curve=oos_equity)
    wins = sum(1 for t in oos_trades if t.net_pnl > 0)
    return OOSReport(
        params=winner,
        total_return=equity / initial_krw - 1.0,
        trades=len(oos_trades),
        win_rate=wins / len(oos_trades) if oos_trades else 0.0,
        max_drawdown=aggregate.max_drawdown,
        profit_factor=aggregate.profit_factor,
        positive_fold_ratio=sum(1 for r in fold_returns if r > 0) / len(fold_returns),
        fold_returns=fold_returns,
    )


def _mode_of(dicts: list[dict[str, Any]]) -> dict[str, Any]:
    """축별 최빈값. 폴드마다 다른 조합이 뽑혀도 중심값을 취한다."""
    if not dicts:
        return {}
    out: dict[str, Any] = {}
    for key in dicts[0]:
        counts: dict[Any, int] = {}
        for d in dicts:
            counts[d[key]] = counts.get(d[key], 0) + 1
        out[key] = max(counts, key=lambda k: counts[k])
    return out


# --------------------------------------------------------------------- 승격
def evaluate_promotion(
    challenger: OOSReport,
    champion: OOSReport | None,
    rules: PromotionRules,
) -> tuple[bool, list[str]]:
    """도전자를 승격할지 판정. 반환값의 리스트에는 탈락 사유가 전부 담긴다."""
    reasons: list[str] = []
    if challenger.trades < rules.min_trades:
        reasons.append(f"OOS 거래 {challenger.trades}건 < 최소 {rules.min_trades}건 (표본 부족)")
    if len(challenger.fold_returns) < rules.min_folds:
        reasons.append(f"폴드 {len(challenger.fold_returns)}개 < 최소 {rules.min_folds}개")
    if rules.require_positive and challenger.total_return <= 0:
        reasons.append(f"OOS 수익률 {challenger.total_return*100:+.2f}% <= 0")
    if abs(challenger.max_drawdown) > rules.max_drawdown:
        reasons.append(f"OOS MDD {challenger.max_drawdown*100:.2f}% > 한도 {rules.max_drawdown*100:.0f}%")
    if challenger.positive_fold_ratio < rules.consistency:
        reasons.append(f"수익 폴드 비율 {challenger.positive_fold_ratio*100:.0f}% "
                       f"< 기준 {rules.consistency*100:.0f}% (단일 구간 의존)")
    if champion is not None and challenger.total_return < champion.total_return + rules.margin:
        reasons.append(f"챔피언 대비 우월폭 부족 "
                       f"{(challenger.total_return - champion.total_return)*100:+.2f}%p "
                       f"< 요구 {rules.margin*100:.1f}%p")
    return (not reasons), reasons


# ----------------------------------------------------------------- 저장/로드
class ParamStore:
    """승격된 파라미터의 디스크 저장소. 엔진은 여기서만 파라미터를 읽는다.

    최적화 프로세스와 실행 프로세스를 파일로 분리한 이유: 실행 루프 안에서
    최적화를 돌리면 루프가 멈추고, 그 사이 손절이 나가지 못한다.
    """

    def __init__(self, path: str = PARAM_FILE) -> None:
        self.path = path
        self._mtime = 0.0
        self._cache: dict[str, dict[str, Any]] = {}
        self.reload()

    def reload(self) -> bool:
        """파일이 바뀐 경우에만 다시 읽는다. 변경되었으면 True."""
        if not os.path.exists(self.path):
            return False
        mtime = os.path.getmtime(self.path)
        if mtime == self._mtime:
            return False
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                self._cache = json.load(fh)
            self._mtime = mtime
            return True
        except (OSError, json.JSONDecodeError) as exc:
            log.error("파라미터 파일 읽기 실패 (%s). 기존 값을 유지합니다: %s", self.path, exc)
            return False

    def params_for(self, market: str, base: StrategyParams, grid_keys: Sequence[str] | None = None) -> StrategyParams:
        """저장된 값 중 '탐색 대상 축'만 base 에 덮어쓴다.

        화이트리스트 방식인 이유: 파라미터 파일이 손상되거나 조작되어도
        min_net_target 같은 안전 하한이 무력화되지 않도록 하기 위함이다.
        """
        entry = self._cache.get(market)
        if not entry:
            return base
        allowed = set(grid_keys or SEARCH_GRID.keys())
        overrides = {k: v for k, v in (entry.get("params") or {}).items() if k in allowed}
        if not overrides:
            return base
        return replace(base, **overrides)

    def champion(self, market: str) -> OOSReport | None:
        entry = self._cache.get(market)
        if not entry or "oos" not in entry:
            return None
        return OOSReport(**entry["oos"])

    def promote(self, market: str, report: OOSReport, note: str = "") -> None:
        self._cache[market] = {
            "params": report.params,
            "oos": report.as_dict(),
            "promoted_at": datetime.now().isoformat(timespec="seconds"),
            "note": note,
        }
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._cache, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)      # 원자적 교체: 엔진이 반쪽 파일을 읽지 않도록
        self._mtime = os.path.getmtime(self.path)

    def describe(self, market: str) -> str:
        entry = self._cache.get(market)
        if not entry:
            return f"{market}: 승격된 파라미터 없음 (config 기본값 사용)"
        oos = entry.get("oos", {})
        return (f"{market}: {entry['params']} | OOS {oos.get('total_return', 0)*100:+.2f}% "
                f"{oos.get('trades', 0)}건 MDD {oos.get('max_drawdown', 0)*100:.2f}% "
                f"| 승격 {entry.get('promoted_at', '?')}")

"""통계 검증 도구 자체의 정확성 검증.

검정기가 틀리면 '입증됨' 판정 자체가 무의미하다. 그래서 답을 아는
인공 표본으로 검정기를 먼저 검정한다.
"""
import random
import unittest

from ubbit.backtest import BTTrade
from ubbit.fees import CostModel
from ubbit.validate import (
    Evidence,
    MarketResult,
    ValidationRules,
    bootstrap_ci,
    evaluate,
    permutation_pvalue,
)

from .test_strategy import make_candles


def trade(market="KRW-A", net=0.01, bars=5):
    return BTTrade(market=market, entry_ts="t0", exit_ts="t1", entry_price=100.0,
                   exit_price=100 * (1 + net), qty=1.0, entry_krw=100_000.0,
                   exit_krw=100_000.0 * (1 + net), fee=200.0,
                   net_pnl=100_000.0 * net, net_return=net, bars=bars,
                   exit_kind="take_profit")


class TestBootstrap(unittest.TestCase):
    def test_zero_mean_ci_contains_zero(self):
        rng = random.Random(7)
        sample = [rng.gauss(0.0, 0.01) for _ in range(600)]
        lo, hi = bootstrap_ci(sample, 2000, rng)
        self.assertLess(lo, 0.0)
        self.assertGreater(hi, 0.0)

    def test_positive_mean_ci_excludes_zero(self):
        rng = random.Random(7)
        sample = [rng.gauss(0.006, 0.01) for _ in range(600)]
        lo, _ = bootstrap_ci(sample, 2000, rng)
        self.assertGreater(lo, 0.0)

    def test_tiny_sample_returns_zero_width(self):
        self.assertEqual(bootstrap_ci([0.01], 100, random.Random(1)), (0.0, 0.0))

    def test_wider_ci_for_smaller_sample(self):
        rng = random.Random(3)
        big = [rng.gauss(0.002, 0.01) for _ in range(1000)]
        small = big[:40]
        lo_b, hi_b = bootstrap_ci(big, 1500, random.Random(1))
        lo_s, hi_s = bootstrap_ci(small, 1500, random.Random(1))
        self.assertGreater(hi_s - lo_s, hi_b - lo_b, "표본이 작으면 구간이 넓어야 한다")


class TestPermutation(unittest.TestCase):
    def setUp(self):
        rng = random.Random(11)
        prices = [100.0]
        for _ in range(800):
            prices.append(prices[-1] * (1 + rng.gauss(0, 0.01)))
        self.candles = {"KRW-A": make_candles(prices, span=0.004)}
        self.specs = [("KRW-A", 5)] * 120

    def test_random_strategy_is_not_significant(self):
        """평균 0 근처의 관측값은 귀무분포와 구분되지 않아야 한다."""
        p = permutation_pvalue(0.0, self.candles, self.specs, CostModel(),
                               400, random.Random(5))
        self.assertGreater(p, 0.05)

    def test_implausibly_high_mean_is_significant(self):
        p = permutation_pvalue(0.05, self.candles, self.specs, CostModel(),
                               400, random.Random(5))
        self.assertLess(p, 0.05)

    def test_pvalue_never_zero(self):
        """p=0 을 보고하면 '확실하다'는 오해를 준다. +1 보정이 걸려야 한다."""
        p = permutation_pvalue(10.0, self.candles, self.specs, CostModel(),
                               100, random.Random(5))
        self.assertGreater(p, 0.0)

    def test_no_trades_returns_one(self):
        self.assertEqual(permutation_pvalue(0.5, self.candles, [], CostModel(),
                                            100, random.Random(1)), 1.0)


class TestEvaluate(unittest.TestCase):
    def setUp(self):
        rng = random.Random(2)
        prices = [100.0]
        for _ in range(1200):
            prices.append(prices[-1] * (1 + rng.gauss(0, 0.008)))
        self.candles = {f"KRW-{c}": make_candles(prices, span=0.004) for c in "ABCDEFGHIJ"}
        self.rules = ValidationRules(min_trades=100, min_markets=5,
                                     bootstrap_iters=800, permutation_iters=300)

    def _results(self, net_by_market):
        return [MarketResult(m, [trade(m, net) for _ in range(30)],
                             net * 30, -0.05)
                for m, net in net_by_market.items()]

    def test_no_trades_is_failure(self):
        ev = evaluate("빈 표본", [], self.candles, cost=CostModel(), rules=self.rules)
        self.assertFalse(ev.proven)
        self.assertIn("검정 불가", ev.failures[0])

    def test_losing_strategy_is_rejected(self):
        results = self._results({f"KRW-{c}": -0.01 for c in "ABCDEFGHIJ"})
        ev = evaluate("적자", results, self.candles, cost=CostModel(), rules=self.rules)
        self.assertFalse(ev.proven)
        self.assertTrue(any("평균 순수익" in f for f in ev.failures))

    def test_single_market_dependency_is_rejected(self):
        """한 종목만 크게 벌고 나머지가 지는 구조는 입증으로 치지 않는다."""
        nets = {f"KRW-{c}": -0.004 for c in "ABCDEFGHIJ"}
        nets["KRW-A"] = 0.20
        ev = evaluate("편중", self._results(nets), self.candles,
                      cost=CostModel(), rules=self.rules)
        self.assertFalse(ev.proven)
        self.assertTrue(any("소수 종목 의존" in f for f in ev.failures))

    def test_small_sample_is_rejected_even_if_profitable(self):
        results = [MarketResult("KRW-A", [trade("KRW-A", 0.05) for _ in range(5)], 0.25, -0.01)]
        ev = evaluate("소표본", results, self.candles, cost=CostModel(), rules=self.rules)
        self.assertFalse(ev.proven)
        self.assertTrue(any("표본 부족" in f for f in ev.failures))
        self.assertTrue(any("종목" in f for f in ev.failures))

    def test_bonferroni_tightens_threshold(self):
        loose = ValidationRules(hypotheses=1)
        tight = ValidationRules(hypotheses=9)
        results = self._results({f"KRW-{c}": 0.01 for c in "ABCDEFGHIJ"})
        ev_loose = evaluate("단일", results, self.candles, cost=CostModel(),
                            rules=ValidationRules(min_trades=100, min_markets=5,
                                                  bootstrap_iters=500, permutation_iters=200,
                                                  hypotheses=loose.hypotheses))
        ev_tight = evaluate("다중", results, self.candles, cost=CostModel(),
                            rules=ValidationRules(min_trades=100, min_markets=5,
                                                  bootstrap_iters=500, permutation_iters=200,
                                                  hypotheses=tight.hypotheses))
        self.assertGreater(ev_loose.p_threshold, ev_tight.p_threshold)

    def test_cost_impact_is_reported(self):
        results = self._results({f"KRW-{c}": 0.01 for c in "ABCDEFGHIJ"})
        ev = evaluate("비용", results, self.candles, cost=CostModel(), rules=self.rules)
        self.assertGreater(ev.gross_edge_before_cost, ev.mean_net,
                           "비용 전 수익률이 순수익률보다 커야 한다")

    def test_all_failures_listed(self):
        ev = evaluate("전부탈락",
                      [MarketResult("KRW-A", [trade("KRW-A", -0.02)], -0.02, -0.5)],
                      self.candles, cost=CostModel(), rules=self.rules)
        self.assertGreaterEqual(len(ev.failures), 4)
        self.assertIn("입증 실패", ev.render())


if __name__ == "__main__":
    unittest.main()


class TestBetaCheck(unittest.TestCase):
    """전략 수익이 시장 상승의 재포장인지 가려내는 검사."""

    def _rising_market(self, n=1200, step=0.003):
        prices, p = [], 100.0
        for _ in range(n):
            p *= 1 + step
            prices.append(p)
        return make_candles(prices, span=0.004)

    def test_strategy_losing_to_buy_and_hold_is_rejected(self):
        """상승장에서 조금 버는 전략은 단순보유를 못 이기면 알파가 아니다."""
        candles = {f"KRW-{c}": self._rising_market() for c in "ABCDEFGHIJ"}
        # 보유 기간을 길게(600봉) 잡아 노출 시간을 크게 만든다
        results = [
            MarketResult(m, [trade(m, 0.001, bars=600) for _ in range(30)], 0.03, -0.02)
            for m in candles
        ]
        rules = ValidationRules(min_trades=100, min_markets=5,
                                bootstrap_iters=500, permutation_iters=200)
        ev = evaluate("베타", results, candles, cost=CostModel(), rules=rules)
        self.assertFalse(ev.proven)
        self.assertTrue(any("시장 베타" in f for f in ev.failures), ev.failures)
        self.assertGreater(ev.buy_hold_mean, 0)
        self.assertGreater(ev.exposure_ratio, 0)

    def test_concentration_in_few_trades_is_rejected(self):
        """총이익의 대부분이 5거래에서 나오면 재현성이 없다."""
        candles = {f"KRW-{c}": self._rising_market(step=0.0) for c in "ABCDEFGHIJ"}
        trades = [trade("KRW-A", 0.50, bars=3) for _ in range(5)]
        trades += [trade("KRW-A", 0.001, bars=3) for _ in range(200)]
        results = [MarketResult("KRW-A", trades, 1.0, -0.01)]
        results += [MarketResult(m, [trade(m, 0.001, bars=3) for _ in range(20)], 0.02, -0.01)
                    for m in list(candles)[1:]]
        rules = ValidationRules(min_trades=100, min_markets=5,
                                bootstrap_iters=500, permutation_iters=200)
        ev = evaluate("집중", results, candles, cost=CostModel(), rules=rules)
        self.assertFalse(ev.proven)
        self.assertTrue(any("소수 거래 의존" in f for f in ev.failures), ev.failures)

    def test_exposure_ratio_is_reported(self):
        candles = {f"KRW-{c}": self._rising_market(step=0.0) for c in "ABCDE"}
        results = [MarketResult(m, [trade(m, 0.01, bars=10) for _ in range(30)], 0.3, -0.01)
                   for m in candles]
        ev = evaluate("노출", results, candles, cost=CostModel(),
                      rules=ValidationRules(min_trades=100, min_markets=5,
                                            bootstrap_iters=300, permutation_iters=100))
        self.assertGreater(ev.exposure_ratio, 0.0)
        self.assertLess(ev.exposure_ratio, 1.0)
        self.assertIn("시장 노출 시간", ev.render())

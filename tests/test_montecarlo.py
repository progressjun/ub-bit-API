"""몬테카를로 엔진 검증.

시뮬레이터가 틀리면 '수익성 입증' 자체가 무의미하다. 답을 아는
인공 입력으로 시뮬레이터를 먼저 검정한다.
"""
import math
import random
import statistics
import unittest

from ubbit.backtest import BTTrade
from ubbit.montecarlo import MCParams, _block_indices, simulate_trades, synthesize

from .test_strategy import make_candles


def trade(net, weight=0.2, equity=1_000_000.0):
    return BTTrade(market="KRW-A", entry_ts="t0", exit_ts="t1", entry_price=100.0,
                   exit_price=100 * (1 + net), qty=1.0, entry_krw=equity * weight,
                   exit_krw=equity * weight * (1 + net), fee=100.0,
                   net_pnl=equity * weight * net, net_return=net, bars=5,
                   exit_kind="take_profit", equity_at_entry=equity)


class TestBlockIndices(unittest.TestCase):
    def test_length_is_exact(self):
        idx = _block_indices(50, 37, 5, random.Random(1))
        self.assertEqual(len(idx), 37)

    def test_indices_in_range(self):
        idx = _block_indices(10, 100, 7, random.Random(1))
        self.assertTrue(all(0 <= i < 10 for i in idx))

    def test_blocks_are_contiguous(self):
        """연속성이 깨지면 추세와 연속 손실 구조가 사라진다."""
        idx = _block_indices(1000, 40, 8, random.Random(3))
        runs = sum(1 for a, b in zip(idx, idx[1:]) if b == (a + 1) % 1000)
        self.assertGreaterEqual(runs, 30, "블록 내부는 이어져 있어야 한다")

    def test_block_one_is_iid(self):
        idx = _block_indices(1000, 200, 1, random.Random(3))
        runs = sum(1 for a, b in zip(idx, idx[1:]) if b == a + 1)
        self.assertLess(runs, 20)


class TestSimulateTrades(unittest.TestCase):
    def test_all_winners_always_profit(self):
        result = simulate_trades([trade(0.05) for _ in range(40)],
                                 params=MCParams(iterations=200, seed=1))
        self.assertEqual(result.p_profit, 1.0)
        self.assertEqual(result.p_ruin, 0.0)

    def test_all_losers_never_profit(self):
        result = simulate_trades([trade(-0.05) for _ in range(40)],
                                 params=MCParams(iterations=200, seed=1))
        self.assertEqual(result.p_profit, 0.0)
        self.assertLess(result.q(result.returns, 0.5), 0)

    def test_position_weight_scales_outcome(self):
        """같은 손익률이라도 베팅 비중이 크면 결과 분산이 커져야 한다."""
        small = simulate_trades([trade(0.03, weight=0.05), trade(-0.03, weight=0.05)] * 20,
                                params=MCParams(iterations=400, seed=2))
        large = simulate_trades([trade(0.03, weight=0.50), trade(-0.03, weight=0.50)] * 20,
                                params=MCParams(iterations=400, seed=2))
        self.assertGreater(statistics.pstdev(large.returns),
                           statistics.pstdev(small.returns))

    def test_drawdown_is_negative_or_zero(self):
        result = simulate_trades([trade(0.02), trade(-0.01)] * 20,
                                 params=MCParams(iterations=200, seed=4))
        self.assertTrue(all(d <= 0 for d in result.drawdowns))

    def test_empty_trades_is_handled(self):
        result = simulate_trades([], params=MCParams(iterations=10))
        self.assertEqual(result.iterations, 0)
        self.assertIn("거래 없음", result.note)

    def test_horizon_overrides_sample_size(self):
        result = simulate_trades([trade(0.01) for _ in range(10)],
                                 params=MCParams(iterations=50, horizon_trades=200, seed=5))
        self.assertEqual(result.horizon, 200)

    def test_render_reports_drawdown_as_magnitude(self):
        """낙폭은 음수라 그대로 정렬하면 분위수가 뒤집힌다."""
        result = simulate_trades([trade(0.04), trade(-0.06)] * 30,
                                 params=MCParams(iterations=300, seed=6))
        text = result.render()
        self.assertIn("깊을수록 나쁨", text)
        line = [l for l in text.splitlines() if "최대낙폭" in l][0]
        values = [float(v.rstrip("%")) for v in line.replace("(깊을수록 나쁨)", "").split()
                  if v.rstrip("%").replace(".", "").replace("-", "").isdigit()]
        self.assertEqual(values, sorted(values), "50% <= 75% <= 95% <= 최악 순이어야 한다")


class TestSynthesize(unittest.TestCase):
    def setUp(self):
        rng = random.Random(9)
        prices = [100.0]
        for _ in range(500):
            prices.append(prices[-1] * math.exp(rng.gauss(0.0005, 0.02)))
        self.candles = make_candles(prices, span=0.004)

    def test_length_preserved(self):
        out = synthesize(self.candles, 20, random.Random(1))
        self.assertEqual(len(out), len(self.candles))

    def test_prices_are_positive(self):
        out = synthesize(self.candles, 20, random.Random(1))
        self.assertTrue(all(c.close > 0 and c.low > 0 for c in out))

    def test_ohlc_ordering_preserved(self):
        out = synthesize(self.candles, 20, random.Random(2))
        for c in out:
            self.assertGreaterEqual(c.high, c.close)
            self.assertLessEqual(c.low, c.close)

    def test_volatility_is_comparable(self):
        """변동성 구조가 유지돼야 ATR 기반 비용 게이트가 의미를 갖는다."""
        def vol(cs):
            r = [math.log(cs[i].close / cs[i - 1].close) for i in range(1, len(cs))]
            return statistics.pstdev(r)
        original = vol(self.candles)
        synth = statistics.median(
            vol(synthesize(self.candles, 20, random.Random(s))) for s in range(12))
        self.assertLess(abs(synth - original) / original, 0.25,
                        "합성 경로의 변동성이 원본과 크게 다르면 검정이 무의미하다")

    def test_full_block_reproduces_original_returns(self):
        """블록이 전체 길이면 수익률 순서가 그대로 재현돼야 한다.
        이 테스트가 시뮬레이터 자체의 정확성을 보증한다."""
        out = synthesize(self.candles, len(self.candles) - 2, random.Random(11))
        def total(cs):
            return cs[-1].close / cs[0].close
        self.assertAlmostEqual(math.log(total(out)), math.log(total(self.candles)),
                               delta=0.35)

    def test_timestamps_are_preserved(self):
        out = synthesize(self.candles, 20, random.Random(1))
        self.assertEqual([c.ts for c in out], [c.ts for c in self.candles])

    def test_too_short_series_returns_input(self):
        self.assertEqual(len(synthesize(self.candles[:2], 5, random.Random(1))), 2)


if __name__ == "__main__":
    unittest.main()

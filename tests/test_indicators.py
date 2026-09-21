import unittest

from ubbit.indicators import atr, donchian, ema, rsi, sma, slope_pct


class TestIndicators(unittest.TestCase):
    def test_sma_known_values(self):
        self.assertEqual(sma([1, 2, 3, 4, 5], 3)[-1], 4.0)
        self.assertIsNone(sma([1, 2], 3)[-1])

    def test_ema_seeds_with_sma(self):
        values = [1.0] * 10
        out = ema(values, 5)
        self.assertAlmostEqual(out[4], 1.0)
        self.assertAlmostEqual(out[-1], 1.0)

    def test_ema_reacts_faster_than_sma(self):
        values = [10.0] * 20 + [20.0] * 5
        self.assertGreater(ema(values, 10)[-1], sma(values, 10)[-1])

    def test_rsi_bounds_and_extremes(self):
        rising = [float(i) for i in range(1, 40)]
        self.assertAlmostEqual(rsi(rising, 14)[-1], 100.0)
        falling = list(reversed(rising))
        self.assertLess(rsi(falling, 14)[-1], 1.0)

    def test_atr_on_constant_range(self):
        closes = [100.0] * 30
        highs = [101.0] * 30
        lows = [99.0] * 30
        self.assertAlmostEqual(atr(highs, lows, closes, 14)[-1], 2.0, places=6)

    def test_donchian_excludes_current_bar(self):
        """당봉을 포함하면 돌파 신호가 영원히 성립하지 않는다(미래참조 방지 검증)."""
        highs = [1.0] * 20 + [99.0]
        lows = [0.0] * 21
        up, _ = donchian(highs, lows, 20)
        self.assertEqual(up[20], 1.0)   # 마지막 봉의 99 는 제외되어야 한다

    def test_slope_sign(self):
        up = slope_pct([float(i) for i in range(1, 20)], 5)
        self.assertGreater(up[-1], 0)
        down = slope_pct([float(i) for i in range(20, 1, -1)], 5)
        self.assertLess(down[-1], 0)

    def test_short_series_returns_none(self):
        self.assertTrue(all(v is None for v in ema([1.0, 2.0], 10)))
        self.assertTrue(all(v is None for v in rsi([1.0, 2.0], 14)))


if __name__ == "__main__":
    unittest.main()

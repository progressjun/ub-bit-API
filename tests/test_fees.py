"""비용 모델 불변식 검증. 이 테스트가 깨지면 전략 전체가 무효다."""
import unittest
from dataclasses import replace

from ubbit.fees import CostModel, estimate_slippage_from_orderbook, infer_tick_size


class TestCostModel(unittest.TestCase):
    def setUp(self):
        self.c = CostModel()

    def test_breakeven_matches_upbit_fee(self):
        fee_only = replace(self.c, slippage_buy=0.0, slippage_sell=0.0)
        # 0.05%/0.05% → 1.0005/0.9995 - 1 = 0.100050...%
        self.assertAlmostEqual(fee_only.breakeven_edge, 0.0010005, places=7)

    def test_breakeven_price_yields_zero_net(self):
        entry = 100_000_000.0
        exit_at_be = self.c.required_exit_price(entry, 0.0)
        self.assertAlmostEqual(self.c.net_return(entry, exit_at_be), 0.0, places=12)

    def test_net_below_breakeven_is_loss(self):
        entry = 100.0
        just_under = entry * (1 + self.c.breakeven_edge * 0.99)
        self.assertLess(self.c.net_return(entry, just_under), 0.0)

    def test_required_gross_round_trip(self):
        for target in (0.001, 0.004, 0.01, 0.05):
            gross = self.c.required_gross_move(target)
            self.assertAlmostEqual(self.c.gross_to_net(gross), target, places=12)

    def test_fill_round_trip_matches_net_return(self):
        """체결 시뮬레이션과 net_return 공식이 일치해야 한다."""
        entry_price, exit_price, krw = 50_000_000.0, 50_500_000.0, 1_000_000.0
        qty, _, _ = self.c.fill_buy(krw, entry_price)
        proceeds, _, _ = self.c.fill_sell(qty, exit_price)
        realized = proceeds / krw - 1.0
        self.assertAlmostEqual(realized, self.c.net_return(entry_price, exit_price), places=10)

    def test_drag_increases_with_turnover(self):
        drags = [self.c.drag_per_day(n) for n in (1, 5, 10, 20)]
        self.assertEqual(drags, sorted(drags))
        self.assertGreater(self.c.drag_per_day(20), 0.03)  # 일 20회전 = 하루 3% 이상 비용

    def test_zero_cost_model_has_zero_breakeven(self):
        free = replace(self.c, fee_buy=0, fee_sell=0, slippage_buy=0, slippage_sell=0)
        self.assertAlmostEqual(free.breakeven_edge, 0.0, places=12)
        self.assertAlmostEqual(free.net_return(100, 110), 0.10, places=12)


class TestOrderbook(unittest.TestCase):
    BOOK = {"orderbook_units": [
        {"ask_price": 1010.0, "ask_size": 10.0, "bid_price": 1000.0, "bid_size": 10.0},
        {"ask_price": 1020.0, "ask_size": 10.0, "bid_price": 990.0, "bid_size": 10.0},
    ]}

    def test_tick_inferred_from_gaps(self):
        self.assertAlmostEqual(infer_tick_size(self.BOOK), 10.0)

    def test_small_order_slippage_is_half_spread(self):
        buy, sell = estimate_slippage_from_orderbook(self.BOOK, 1_000.0)
        mid = 1005.0
        self.assertAlmostEqual(buy, (1010.0 - mid) / mid, places=9)
        self.assertAlmostEqual(sell, (mid - 1000.0) / mid, places=9)

    def test_order_larger_than_book_is_rejected(self):
        buy, sell = estimate_slippage_from_orderbook(self.BOOK, 10_000_000.0)
        self.assertEqual(buy, float("inf"))
        self.assertEqual(sell, float("inf"))

    def test_deeper_order_has_worse_slippage(self):
        small, _ = estimate_slippage_from_orderbook(self.BOOK, 1_000.0)
        large, _ = estimate_slippage_from_orderbook(self.BOOK, 15_000.0)
        self.assertGreater(large, small)


if __name__ == "__main__":
    unittest.main()

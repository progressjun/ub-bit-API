"""최소 주문금액 관련 검증.

업비트 최소 주문금액(5,000원)은 매수뿐 아니라 매도에도 적용된다.
정확히 5,000원어치를 사면 가격이 1%만 빠져도 매도 주문이 거부되고,
손절이 영원히 실패하는 포지션이 된다. 그 경로를 전부 막았는지 확인한다.
"""
import os
import tempfile
import unittest

from ubbit.config import Config, EngineParams
from ubbit.engine import TradingEngine, _conviction
from ubbit.fees import CostModel
from ubbit.risk import RiskManager, RiskParams
from ubbit.session import SessionParams
from ubbit.state import Position
from ubbit.strategy import FeeAwareTrendStrategy, Signal, Snapshot, StrategyParams

from .test_engine import FakeClient
from .test_strategy import breakout_candles


class TestEntryFloor(unittest.TestCase):
    def setUp(self):
        self.risk = RiskManager(RiskParams(max_order_krw=10_000_000), CostModel())
        self.risk.roll_day(10_000_000)

    def test_floor_is_min_order_times_guard(self):
        self.assertEqual(self.risk.entry_floor_krw, 10_000.0)

    def test_never_buys_below_floor(self):
        """5,000원짜리 주문은 손절 한 번이면 못 파는 포지션이 된다."""
        size, why = self.risk.position_size_krw(40_000, 40_000, 100.0, 97.0, 0.0)
        self.assertEqual(size, 0.0)
        self.assertIn("진입 최소금액 미달", why)

    def test_buys_above_floor(self):
        size, _ = self.risk.position_size_krw(1_000_000, 1_000_000, 100.0, 97.0, 0.0)
        self.assertGreaterEqual(size, self.risk.entry_floor_krw)

    def test_guard_is_configurable(self):
        loose = RiskManager(RiskParams(dust_guard=1.0), CostModel())
        self.assertEqual(loose.entry_floor_krw, 5_000.0)
        strict = RiskManager(RiskParams(dust_guard=4.0), CostModel())
        self.assertEqual(strict.entry_floor_krw, 20_000.0)

    def test_sellable_threshold(self):
        self.assertFalse(self.risk.sellable(4_999))
        self.assertTrue(self.risk.sellable(5_000))

    def test_floor_survives_a_50_percent_drawdown(self):
        """안전배수 2.0 의 의미: 반토막이 나도 매도 가능 금액이 남는다."""
        entry = self.risk.entry_floor_krw
        self.assertTrue(self.risk.sellable(entry * 0.5))


class TestConviction(unittest.TestCase):
    def setUp(self):
        self.cost = CostModel()
        self.p = StrategyParams()

    def _sig(self, atr_pct, rr, vol_ratio):
        snap = Snapshot(ts="t", close=100.0, atr_pct=atr_pct, vol_ratio=vol_ratio)
        return Signal("buy", "", snap, {"rr": rr})

    def test_weak_signal_scores_zero(self):
        floor = self.cost.breakeven_edge * self.p.atr_cost_multiple
        self.assertEqual(_conviction(self._sig(floor, 1.2, 1.0), self.p, self.cost), 0.0)

    def test_one_weak_axis_blocks_boost(self):
        """세 축이 동시에 좋을 때만 크게 건다. 하나라도 미달이면 기본 사이즈."""
        floor = self.cost.breakeven_edge * self.p.atr_cost_multiple
        strong_atr_weak_vol = self._sig(floor * 1.8, 3.0, self.p.volume_expansion)
        self.assertEqual(_conviction(strong_atr_weak_vol, self.p, self.cost), 0.0)

    def test_all_strong_scores_high(self):
        floor = self.cost.breakeven_edge * self.p.atr_cost_multiple
        score = _conviction(self._sig(floor * 2.0, 3.0, self.p.volume_expansion + 1.0),
                            self.p, self.cost)
        self.assertGreater(score, 0.9)

    def test_score_is_bounded(self):
        floor = self.cost.breakeven_edge * self.p.atr_cost_multiple
        score = _conviction(self._sig(floor * 50, 99, 99), self.p, self.cost)
        self.assertLessEqual(score, 1.0)

    def test_missing_atr_is_zero(self):
        self.assertEqual(_conviction(self._sig(None, 3.0, 2.0), self.p, self.cost), 0.0)

    def test_boost_never_exceeds_risk_caps(self):
        """사이즈를 키워도 포지션 비중 상한은 절대 넘지 않는다."""
        risk = RiskManager(RiskParams(max_position_pct=0.10, max_order_krw=10_000_000),
                           CostModel())
        risk.roll_day(1_000_000)
        size, _ = risk.position_size_krw(1_000_000, 1_000_000, 100.0, 97.0, 0.0,
                                         conviction=1.0)
        self.assertLessEqual(size, 1_000_000 * 0.10)


class TestSellSideGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = Config(
            mode="paper",
            engine=EngineParams(markets=["KRW-TEST"], candle_unit=5, candle_count=200,
                                initial_krw=10_000_000,
                                db_path=os.path.join(self.tmp, "d.db"),
                                kill_switch_file=os.path.join(self.tmp, ".KILL"),
                                liquidity_min_value_krw=0.0),
            cost=CostModel(), strategy=StrategyParams(),
            risk=RiskParams(max_order_krw=5_000_000),
            session=SessionParams(enabled=False),
        )

    def _engine(self):
        e = TradingEngine(self.cfg)
        e.client = FakeClient(breakout_candles())
        return e

    def test_unsellable_position_is_flagged_not_retried(self):
        """평가액이 최소주문 미만이면 주문을 내지 않고 상태를 남긴다."""
        engine = self._engine()
        sold = []
        engine.broker.sell = lambda m, q, p: sold.append(m)
        pos = Position(market="KRW-TEST", entry_price=100.0, qty=30.0, entry_krw=3_000.0,
                       stop_price=90.0, target_net=0.01, peak_price=100.0)
        engine.store.upsert_position(pos)

        engine._close_position(pos, 100.0, "stop", "테스트")   # 평가 3,000원

        self.assertEqual(sold, [], "거부될 주문을 내면 안 된다")
        saved = engine.store.get_position("KRW-TEST")
        self.assertIsNotNone(saved)
        self.assertTrue(saved.meta.get("dust"))
        self.assertEqual(len(engine.store.recent_trades()), 0)
        engine.store.close()

    def test_recovered_position_can_sell_again(self):
        engine = self._engine()
        engine.broker.holdings["KRW-TEST"] = 30.0       # 페이퍼 장부에도 보유를 만들어 둔다
        pos = Position(market="KRW-TEST", entry_price=100.0, qty=30.0, entry_krw=3_000.0,
                       stop_price=90.0, target_net=0.01, peak_price=100.0,
                       meta={"dust": True, "dust_notified": True})
        engine.store.upsert_position(pos)

        engine._close_position(pos, 300.0, "stop", "테스트")   # 평가 9,000원

        self.assertIsNone(engine.store.get_position("KRW-TEST"))
        self.assertEqual(len(engine.store.recent_trades()), 1)
        engine.store.close()

    def test_notifies_once_not_every_cycle(self):
        engine = self._engine()
        sent = []
        engine.notifier.send = lambda kind, text: sent.append(kind)
        pos = Position(market="KRW-TEST", entry_price=100.0, qty=30.0, entry_krw=3_000.0,
                       stop_price=90.0, target_net=0.01, peak_price=100.0)
        engine.store.upsert_position(pos)
        for _ in range(5):
            pos = engine.store.get_position("KRW-TEST")
            engine._close_position(pos, 100.0, "stop", "테스트")
        self.assertEqual(sent.count("error"), 1, "매 주기 알림을 보내면 알림 피로가 쌓인다")
        engine.store.close()


if __name__ == "__main__":
    unittest.main()

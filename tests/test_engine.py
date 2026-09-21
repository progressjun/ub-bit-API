"""엔진 전체 경로 검증. 가짜 클라이언트로 실주문 없이 매수→청산 사이클을 돌린다.

이 테스트의 목적은 수익성 검증이 아니라 '주문 경로가 실제로 동작하는가'다.
자동매매에서 가장 비싼 버그는 신호 오류가 아니라 청산이 안 나가는 것이다.
"""
import os
import tempfile
import unittest

from ubbit.config import Config, EngineParams
from ubbit.engine import TradingEngine
from ubbit.fees import CostModel
from ubbit.risk import RiskParams
from ubbit.session import SessionParams
from ubbit.strategy import StrategyParams

from .test_strategy import breakout_candles, make_candles, uptrend


class FakeClient:
    """UpbitClient 인터페이스 중 엔진이 쓰는 부분만 흉내낸다."""

    def __init__(self, candles):
        self.candles_data = candles

    def _to_rows(self, candles):
        return [{
            "candle_date_time_kst": c.ts, "opening_price": c.open, "high_price": c.high,
            "low_price": c.low, "trade_price": c.close,
            "candle_acc_trade_volume": c.volume, "candle_acc_trade_price": c.value,
        } for c in candles]

    def candles(self, market, unit=5, count=200, to=None):
        return self._to_rows(self.candles_data[-count:])

    def orderbook(self, markets):
        price = self.candles_data[-1].close
        return [{"orderbook_units": [
            {"ask_price": price * 1.0005, "ask_size": 1e9,
             "bid_price": price * 0.9995, "bid_size": 1e9}
        ]}]


class TestEngineCycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = Config(
            mode="paper",
            engine=EngineParams(
                markets=["KRW-TEST"], candle_unit=5, candle_count=200,
                initial_krw=10_000_000, db_path=os.path.join(self.tmp, "t.db"),
                kill_switch_file=os.path.join(self.tmp, ".KILL"),
                liquidity_min_value_krw=0.0,
            ),
            cost=CostModel(),
            strategy=StrategyParams(),
            risk=RiskParams(max_order_krw=5_000_000),
            session=SessionParams(enabled=False),   # 시간대 의존 없이 매매 경로만 검증
        )

    def _engine(self, candles):
        engine = TradingEngine(self.cfg)
        engine.client = FakeClient(candles)
        return engine

    def test_full_buy_then_stop_out_cycle(self):
        rising = breakout_candles()
        engine = self._engine(rising)
        engine.tick()
        position = engine.store.get_position("KRW-TEST")
        self.assertIsNotNone(position, "상승 추세 돌파에서 진입이 발생해야 한다")
        self.assertGreater(position.qty, 0)
        self.assertLess(position.stop_price, position.entry_price)

        # 급락을 붙여 손절 경로를 태운다
        crash = [c.close for c in rising] + [rising[-1].close * 0.80] * 3
        engine.client = FakeClient(make_candles(crash, span=0.003))
        engine.tick()
        self.assertIsNone(engine.store.get_position("KRW-TEST"), "손절가 이탈 시 청산되어야 한다")

        trades = engine.store.recent_trades()
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["exit_kind"], "stop")
        self.assertLess(trades[0]["net_pnl"], 0)
        self.assertGreater(trades[0]["fee_krw"], 0, "수수료가 기록되어야 한다")
        engine.store.close()

    def test_take_profit_cycle_nets_positive(self):
        rising = breakout_candles()
        engine = self._engine(rising)
        engine.tick()
        self.assertIsNotNone(engine.store.get_position("KRW-TEST"))

        spike = [c.close for c in rising] + [rising[-1].close * 1.30] * 2
        engine.client = FakeClient(make_candles(spike, span=0.003))
        engine.tick()
        trades = engine.store.recent_trades()
        self.assertEqual(len(trades), 1)
        self.assertGreater(trades[0]["net_pnl"], 0)
        # 비용 차감 후에도 목표 순익 이상이어야 한다
        self.assertGreaterEqual(trades[0]["net_return"], self.cfg.strategy.min_net_target * 0.9)
        engine.store.close()

    def test_kill_switch_blocks_new_entries(self):
        open(self.cfg.engine.kill_switch_file, "w").close()
        engine = self._engine(breakout_candles())
        engine.tick()
        self.assertIsNone(engine.store.get_position("KRW-TEST"))
        engine.store.close()

    def test_position_survives_restart(self):
        rising = breakout_candles()
        engine = self._engine(rising)
        engine.tick()
        self.assertIsNotNone(engine.store.get_position("KRW-TEST"))
        engine.store.close()

        reborn = self._engine(rising)     # 같은 db_path 로 재기동
        self.assertIsNotNone(reborn.store.get_position("KRW-TEST"),
                             "재기동 후에도 포지션이 복원되어야 한다")
        reborn.store.close()

    def test_session_window_blocks_entry_and_flattens(self):
        """업무 시간 밖에서는 진입하지 않고, 마감 구간에서는 보유분을 정리한다."""
        from datetime import datetime

        import ubbit.engine as engine_mod
        from ubbit.session import KST

        self.cfg.session = SessionParams(enabled=True, start="09:00", end="18:00")
        engine = self._engine(breakout_candles())

        # 운영 시간(화요일 11:00)에는 진입
        original = engine_mod.evaluate_session
        engine_mod.evaluate_session = lambda p, now=None: original(
            p, datetime(2026, 9, 22, 11, 0, tzinfo=KST))
        try:
            engine.tick()
            self.assertIsNotNone(engine.store.get_position("KRW-TEST"))

            # 마감 이후(18:30)에는 전량 청산
            engine_mod.evaluate_session = lambda p, now=None: original(
                p, datetime(2026, 9, 22, 18, 30, tzinfo=KST))
            engine.tick()
            self.assertIsNone(engine.store.get_position("KRW-TEST"))
            trades = engine.store.recent_trades()
            self.assertEqual(trades[0]["exit_kind"], "session_close")

            # 개장 전(08:00)에는 신규 진입 없음
            engine_mod.evaluate_session = lambda p, now=None: original(
                p, datetime(2026, 9, 23, 8, 0, tzinfo=KST))
            engine.tick()
            self.assertIsNone(engine.store.get_position("KRW-TEST"))
        finally:
            engine_mod.evaluate_session = original
        engine.store.close()

    def test_sell_failure_keeps_position(self):
        """매도 주문이 실패하면 포지션을 지우지 않아야 한다 (유실된 보유분 방지)."""
        from ubbit.broker import Fill

        engine = self._engine(breakout_candles())
        engine.tick()
        position = engine.store.get_position("KRW-TEST")
        self.assertIsNotNone(position)

        engine.broker.sell = lambda market, qty, ref: Fill(
            False, market, "ask", 0, 0, 0, 0, error="거래소 오류")
        engine._close_position(position, position.entry_price, "stop", "테스트")
        self.assertIsNotNone(engine.store.get_position("KRW-TEST"))
        self.assertEqual(len(engine.store.recent_trades()), 0)
        engine.store.close()

    def test_no_entry_in_dead_volatility(self):
        flat = make_candles(uptrend(step=0.0002), span=0.00005)
        engine = self._engine(flat)
        engine.tick()
        self.assertIsNone(engine.store.get_position("KRW-TEST"))
        engine.store.close()


if __name__ == "__main__":
    unittest.main()

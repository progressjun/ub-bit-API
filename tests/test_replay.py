"""엔진 재생 검증.

replay 의 존재 이유는 '백테스터가 아니라 엔진 자체'를 돌리는 것이다.
따라서 여기서 확인할 것은 수익률이 아니라, 재생 중에도 리스크 게이트와
시계가 실거래와 동일하게 작동하는가이다.
"""
import os
import tempfile
import unittest
from datetime import datetime

from ubbit.config import Config, EngineParams
from ubbit.engine import TradingEngine
from ubbit.fees import CostModel
from ubbit.replay import ReplayClient, run_replay
from ubbit.risk import RiskParams
from ubbit.session import SessionParams
from ubbit.strategy import Candle, StrategyParams


def series(n=400, start=100.0):
    """추세·횡보·돌파가 반복되는 합성 시계열."""
    prices, p = [], start
    for i in range(n):
        if i % 80 < 50:
            p *= 1 + 0.012 * (1 if i % 2 else -1) + 0.0008
        else:
            p *= 1.02
        prices.append(p)
    return prices


def candles(prices, span=0.004):
    out = []
    for i, p in enumerate(prices):
        ts = datetime(2026, 1, 1, 0, 0) .replace(day=1 + (i * 4) // 24 % 28,
                                                 hour=(i * 4) % 24)
        out.append(Candle(ts=ts.isoformat(), open=p, high=p * (1 + span),
                          low=p * (1 - span), close=p, volume=1000.0,
                          value=1_000_000_000.0 * (2.0 if i % 80 >= 50 else 1.0)))
    return out


class TestReplayClient(unittest.TestCase):
    def setUp(self):
        self.data = {"KRW-A": candles(series()), "KRW-B": candles(series(start=50.0))}
        self.client = ReplayClient(self.data, window=200)

    def test_never_serves_future_candles(self):
        """미래 봉이 새면 백테스트 전체가 무의미해진다."""
        self.client.cursor = 120
        rows = self.client.candles("KRW-A", count=200)
        self.assertEqual(len(rows), 120)
        self.assertEqual(rows[-1]["trade_price"], self.data["KRW-A"][119].close)

    def test_window_is_bounded(self):
        self.client.cursor = 400
        self.assertEqual(len(self.client.candles("KRW-A", count=200)), 200)

    def test_ticker_matches_cursor(self):
        self.client.cursor = 77
        self.assertEqual(self.client.ticker(["KRW-A"])[0]["trade_price"],
                         self.data["KRW-A"][76].close)

    def test_current_time_tracks_cursor(self):
        self.client.cursor = 50
        self.assertEqual(self.client.current_time().isoformat(),
                         self.data["KRW-A"][49].ts)

    def test_unknown_market_is_empty(self):
        self.client.cursor = 100
        self.assertEqual(self.client.candles("KRW-NONE"), [])


class TestRunReplay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data = {f"KRW-{c}": candles(series(start=100.0 + i * 7))
                     for i, c in enumerate("ABCD")}
        self.cfg = Config(
            mode="paper",
            engine=EngineParams(markets=list(self.data), candle_unit=240,
                                candle_count=200, initial_krw=10_000_000,
                                db_path=os.path.join(self.tmp, "r.db"),
                                kill_switch_file=os.path.join(self.tmp, ".KILL"),
                                liquidity_min_value_krw=0.0),
            cost=CostModel(), strategy=StrategyParams(warmup_bars=80),
            risk=RiskParams(max_order_krw=3_000_000, daily_trade_limit=4),
            session=SessionParams(enabled=False),
        )

    def test_replay_produces_trades_and_restores_clock(self):
        engine = TradingEngine(self.cfg)
        original_clock = engine.clock
        run_replay(engine, self.data, start_at=90)
        self.assertIs(engine.clock, original_clock, "재생 후 시계가 복원되어야 한다")
        perf = engine.store.performance()
        self.assertGreater(perf["trades"], 0, "재생 구간에서 거래가 발생해야 한다")
        self.assertGreater(perf["fees_paid"], 0, "수수료가 집계되어야 한다")
        engine.store.close()

    def test_daily_trade_limit_uses_candle_clock(self):
        """이 테스트가 잡아낸 실제 버그: 시계를 주입하지 않으면 재생 전체가
        '하루'로 집계돼 일일 매매 한도 4회에서 멈춘다."""
        engine = TradingEngine(self.cfg)
        run_replay(engine, self.data, start_at=90)
        trades = engine.store.performance()["trades"]
        self.assertGreater(trades, self.cfg.risk.daily_trade_limit,
                           f"거래 {trades}건 — 일일 한도({self.cfg.risk.daily_trade_limit})에 "
                           f"묶였다면 시계가 전진하지 않은 것이다")
        engine.store.close()

    def test_positions_persist_across_replay(self):
        engine = TradingEngine(self.cfg)
        run_replay(engine, self.data, start_at=90)
        engine.store.close()
        reborn = TradingEngine(self.cfg)
        self.assertGreater(reborn.store.performance()["trades"], 0)
        reborn.store.close()

    def test_kill_switch_stops_new_entries_during_replay(self):
        open(self.cfg.engine.kill_switch_file, "w").close()
        engine = TradingEngine(self.cfg)
        run_replay(engine, self.data, start_at=90)
        self.assertEqual(engine.store.performance()["trades"], 0)
        self.assertEqual(len(engine.store.all_positions()), 0)
        engine.store.close()


if __name__ == "__main__":
    unittest.main()

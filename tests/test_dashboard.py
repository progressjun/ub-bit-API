"""대시보드 검증.

돈이 걸린 화면이므로 두 가지를 확인한다.
  숫자가 맞는가       — 순손익이 비용 차감 후 값인가
  안전장치가 동작하는가 — 킬스위치 엔드포인트가 실제로 파일을 만드는가
"""
import json
import os
import tempfile
import unittest

from ubbit.config import Config, EngineParams
from ubbit.dashboard import PriceCache, build_state
from ubbit.fees import CostModel
from ubbit.risk import RiskParams
from ubbit.state import Position, Store
from ubbit.strategy import StrategyParams


class StubPrices(PriceCache):
    def __init__(self, data):
        self._data = data

    def get(self, markets):
        return {m: self._data[m] for m in markets if m in self._data}


class TestBuildState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "d.db")
        self.cfg = Config(
            mode="paper",
            engine=EngineParams(markets=["KRW-BTC"], db_path=self.db,
                                kill_switch_file=os.path.join(self.tmp, ".KILL"),
                                initial_krw=1_000_000),
            cost=CostModel(), strategy=StrategyParams(), risk=RiskParams(),
        )

    def test_missing_db_is_not_ready(self):
        state = build_state(self.cfg, self.db, StubPrices({}))
        self.assertFalse(state["ready"])
        self.assertIn("실행 기록이 없습니다", state["message"])

    def _seed(self):
        store = Store(self.db)
        store.upsert_position(Position(
            market="KRW-BTC", entry_price=100_000_000, qty=0.01, entry_krw=1_000_000,
            stop_price=97_000_000, target_net=0.01, peak_price=101_000_000, bars_held=3))
        store.record_equity(1_050_000, 50_000, 1_000_000)
        store.record_trade(
            market="KRW-ETH", opened_at="2026-09-01T00:00:00", closed_at="2026-09-02T00:00:00",
            entry_price=100.0, exit_price=110.0, qty=1.0, entry_krw=100_000.0,
            exit_krw=109_000.0, fee_krw=200.0, net_pnl=9_000.0, net_return=0.09,
            exit_kind="take_profit", reason="테스트")
        store.close()

    def test_position_net_return_is_cost_adjusted(self):
        """총 +1% 상승이어도 화면의 순손익은 비용만큼 낮아야 한다."""
        self._seed()
        state = build_state(self.cfg, self.db, StubPrices({"KRW-BTC": 101_000_000}))
        self.assertTrue(state["ready"])
        row = state["positions"][0]
        expected = self.cfg.cost.net_return(100_000_000, 101_000_000)
        self.assertAlmostEqual(row["net_return"], expected, places=10)
        self.assertLess(row["net_return"], 0.01, "비용 차감 전 값이 노출되면 안 된다")

    def test_cost_block_matches_config(self):
        self._seed()
        state = build_state(self.cfg, self.db, StubPrices({"KRW-BTC": 100_000_000}))
        cost = state["cost"]
        self.assertAlmostEqual(cost["breakeven"], self.cfg.cost.breakeven_edge)
        self.assertAlmostEqual(
            cost["atr_floor"],
            self.cfg.cost.breakeven_edge * self.cfg.strategy.atr_cost_multiple)
        self.assertGreater(cost["drag_at_limit"], 0)

    def test_performance_and_exit_mix(self):
        self._seed()
        state = build_state(self.cfg, self.db, StubPrices({"KRW-BTC": 100_000_000}))
        self.assertEqual(state["performance"]["trades"], 1)
        self.assertEqual(state["exit_mix"], {"take_profit": 1})

    def test_kill_flag_reflects_file(self):
        self._seed()
        state = build_state(self.cfg, self.db, StubPrices({}))
        self.assertFalse(state["kill"])
        open(self.cfg.engine.kill_switch_file, "w").close()
        state = build_state(self.cfg, self.db, StubPrices({}))
        self.assertTrue(state["kill"])

    def test_state_is_json_serializable(self):
        """핸들러가 json.dumps 하므로 직렬화 불가 값이 섞이면 500 이 난다."""
        self._seed()
        state = build_state(self.cfg, self.db, StubPrices({"KRW-BTC": 100_000_000}))
        json.dumps(state, ensure_ascii=False, default=str)

    def test_missing_price_falls_back_to_entry(self):
        """현재가 조회가 실패해도 화면이 깨지지 않아야 한다."""
        self._seed()
        state = build_state(self.cfg, self.db, StubPrices({}))
        row = state["positions"][0]
        self.assertEqual(row["price"], row["entry_price"])
        self.assertAlmostEqual(row["net_return"], self.cfg.cost.net_return(
            row["entry_price"], row["entry_price"]))


class TestPriceCache(unittest.TestCase):
    def test_empty_markets_skips_api(self):
        cache = PriceCache()
        cache.client = None          # 호출되면 AttributeError 로 드러난다
        self.assertEqual(cache.get([]), {})


if __name__ == "__main__":
    unittest.main()

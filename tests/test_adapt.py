"""재적합·승격 게이트 검증.

여기서 지켜야 할 성질은 단 하나다.
"애매하면 승격하지 않는다." 자동 재적합의 기본값은 거부여야 한다.
"""
import json
import os
import tempfile
import unittest

from ubbit.adapt import (
    OOSReport,
    ParamStore,
    PromotionRules,
    evaluate_promotion,
    walk_forward,
)
from ubbit.fees import CostModel
from ubbit.risk import RiskParams
from ubbit.strategy import StrategyParams

from .test_strategy import breakout_candles


def report(**kw):
    base = dict(params={"stop_atr": 1.5}, total_return=0.10, trades=50, win_rate=0.5,
                max_drawdown=-0.05, profit_factor=1.5, positive_fold_ratio=0.8,
                fold_returns=[0.02] * 5)
    base.update(kw)
    return OOSReport(**base)


class TestPromotionGate(unittest.TestCase):
    def setUp(self):
        self.rules = PromotionRules()

    def test_clean_challenger_is_promoted(self):
        ok, reasons = evaluate_promotion(report(), None, self.rules)
        self.assertTrue(ok, reasons)

    def test_small_sample_is_rejected(self):
        ok, reasons = evaluate_promotion(report(trades=17), None, self.rules)
        self.assertFalse(ok)
        self.assertTrue(any("표본 부족" in r for r in reasons))

    def test_negative_oos_is_rejected_even_with_many_trades(self):
        ok, reasons = evaluate_promotion(report(total_return=-0.05, trades=500), None, self.rules)
        self.assertFalse(ok)
        self.assertTrue(any("수익률" in r for r in reasons))

    def test_single_fold_dependency_is_rejected(self):
        """한 구간의 대박에 기댄 성과는 승격하지 않는다."""
        ok, reasons = evaluate_promotion(
            report(positive_fold_ratio=0.2, fold_returns=[0.5, -0.02, -0.02, -0.02, -0.02]),
            None, self.rules)
        self.assertFalse(ok)
        self.assertTrue(any("단일 구간 의존" in r for r in reasons))

    def test_excess_drawdown_is_rejected(self):
        ok, reasons = evaluate_promotion(report(max_drawdown=-0.30), None, self.rules)
        self.assertFalse(ok)
        self.assertTrue(any("MDD" in r for r in reasons))

    def test_marginal_improvement_over_champion_is_rejected(self):
        """챔피언보다 조금 나은 정도로는 교체하지 않는다 (교체 자체가 비용이다)."""
        champion = report(total_return=0.10)
        challenger = report(total_return=0.105)
        ok, reasons = evaluate_promotion(challenger, champion, self.rules)
        self.assertFalse(ok)
        self.assertTrue(any("우월폭 부족" in r for r in reasons))

    def test_clear_improvement_over_champion_is_promoted(self):
        ok, _ = evaluate_promotion(report(total_return=0.25), report(total_return=0.10), self.rules)
        self.assertTrue(ok)

    def test_all_failures_are_reported_together(self):
        ok, reasons = evaluate_promotion(
            report(trades=5, total_return=-0.2, max_drawdown=-0.5, positive_fold_ratio=0.0),
            None, self.rules)
        self.assertFalse(ok)
        self.assertGreaterEqual(len(reasons), 4, "탈락 사유는 전부 보고되어야 한다")


class TestParamStore(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "params.json")
        self.store = ParamStore(self.path)
        self.base = StrategyParams()

    def test_missing_file_falls_back_to_base(self):
        self.assertEqual(self.store.params_for("KRW-BTC", self.base), self.base)

    def test_promote_then_apply(self):
        self.store.promote("KRW-BTC", report(params={"stop_atr": 2.0, "breakout_period": 40}))
        applied = ParamStore(self.path).params_for("KRW-BTC", self.base)
        self.assertEqual(applied.stop_atr, 2.0)
        self.assertEqual(applied.breakout_period, 40)

    def test_non_whitelisted_keys_are_ignored(self):
        """파라미터 파일이 안전 하한을 무력화할 수 없어야 한다."""
        self.store.promote("KRW-BTC", report(params={
            "stop_atr": 2.0,
            "min_net_target": 0.0,          # 손익분기 아래로 목표를 끌어내리려는 시도
            "max_hold_bars": 99999,
        }))
        applied = ParamStore(self.path).params_for("KRW-BTC", self.base)
        self.assertEqual(applied.stop_atr, 2.0)
        self.assertEqual(applied.min_net_target, self.base.min_net_target)
        self.assertEqual(applied.max_hold_bars, self.base.max_hold_bars)

    def test_corrupt_file_keeps_previous_values(self):
        self.store.promote("KRW-BTC", report(params={"stop_atr": 2.0}))
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{ broken json")
        self.store.reload()
        self.assertEqual(self.store.params_for("KRW-BTC", self.base).stop_atr, 2.0)

    def test_promote_writes_atomically(self):
        self.store.promote("KRW-BTC", report())
        with open(self.path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertIn("KRW-BTC", data)
        self.assertIn("promoted_at", data["KRW-BTC"])
        self.assertFalse(os.path.exists(self.path + ".tmp"))


class TestWalkForward(unittest.TestCase):
    def test_insufficient_candles_returns_none(self):
        result = walk_forward(
            "KRW-TEST", breakout_candles(), cost=CostModel(),
            base_params=StrategyParams(), rparams=RiskParams(),
        )
        self.assertIsNone(result, "캔들이 부족하면 조용히 실패하지 말고 None 을 돌려야 한다")


if __name__ == "__main__":
    unittest.main()

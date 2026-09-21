"""전략·리스크 게이트 검증. 핵심은 '비용을 못 넘기면 진입하지 않는다'."""
import unittest
from dataclasses import replace

from ubbit.fees import CostModel
from ubbit.risk import RiskManager, RiskParams
from ubbit.strategy import Candle, FeeAwareTrendStrategy, PositionView, StrategyParams


def make_candles(prices, vol=1_000.0, value=1_000_000_000.0, span=0.004, values=None):
    """종가 리스트로 캔들 생성. span 은 봉당 고가-저가 폭(=변동성 조절 손잡이)."""
    out = []
    for i, p in enumerate(prices):
        out.append(Candle(
            ts=f"2026-01-01T{i//60:02d}:{i%60:02d}:00",
            open=p, high=p * (1 + span), low=p * (1 - span), close=p,
            volume=vol, value=values[i] if values else value,
        ))
    return out


def uptrend(n=200, start=100.0, step=0.004):
    prices, p = [], start
    for i in range(n):
        p *= 1 + step * (1.0 if i % 5 else 0.4)
        prices.append(p)
    return prices


def breakout_candles(chop=0.013, drift=0.0006, rally=0.03, span=0.003):
    """전략의 진입 조건을 전부 만족하는 합성 시나리오.

    추세 형성(80봉) → 횡보 눌림(70봉, RSI 냉각) → 거래대금 동반 돌파(1봉).
    단조 상승 시계열은 RSI 95 이상이라 rsi_max 에 걸려 진입이 안 나온다.
    즉 '계속 오르기만 하는 종목'에는 이 전략이 진입하지 않는다.
    """
    prices, p = [], 80.0
    for _ in range(80):
        p *= 1.0028
        prices.append(p)
    for i in range(70):
        p *= (1 + drift) * (1 + chop if i % 2 else 1 - chop)
        prices.append(p)
    prices.append(prices[-1] * (1 + rally))
    values = [1_000_000_000.0] * len(prices)
    values[-1] = 2_500_000_000.0
    return make_candles(prices, span=span, values=values)


class TestEntryGating(unittest.TestCase):
    def setUp(self):
        self.cost = CostModel()
        self.params = StrategyParams()
        self.strategy = FeeAwareTrendStrategy(self.params, self.cost)

    def test_low_volatility_is_rejected_as_dead(self):
        """ATR% 가 비용 기준선 아래면 아무리 추세여도 진입 금지."""
        candles = make_candles(uptrend(step=0.0002), span=0.00005)
        snaps = self.strategy.compute(candles)
        self.assertEqual(snaps[-1].regime, "dead")
        self.assertEqual(self.strategy.entry_signal(candles, snaps).action, "hold")

    def test_extreme_volatility_is_rejected_as_chaos(self):
        candles = make_candles(uptrend(step=0.02), span=0.09)
        snaps = self.strategy.compute(candles)
        self.assertEqual(snaps[-1].regime, "chaos")
        self.assertEqual(self.strategy.entry_signal(candles, snaps).action, "hold")

    def test_cost_floor_scales_with_fees(self):
        """수수료가 오르면 진입 하한도 함께 올라가야 한다."""
        expensive = replace(self.cost, fee_buy=0.0025, fee_sell=0.0025)   # BTC/USDT 마켓 수준
        strat = FeeAwareTrendStrategy(self.params, expensive)
        # ATR% 가 저비용 하한(0.80%)과 고비용 하한(2.41%) 사이에 오도록 변동성을 잡는다
        candles = make_candles(uptrend(step=0.004), span=0.008)
        cheap_snap = self.strategy.compute(candles)[-1]
        pricey_snap = strat.compute(candles)[-1]
        mult = self.params.atr_cost_multiple
        self.assertGreater(cheap_snap.atr_pct, self.cost.breakeven_edge * mult)
        self.assertLess(cheap_snap.atr_pct, expensive.breakeven_edge * mult)
        self.assertNotEqual(cheap_snap.regime, "dead")
        self.assertEqual(pricey_snap.regime, "dead")

    def test_downtrend_never_buys(self):
        prices = [100.0 * (0.996 ** i) for i in range(200)]
        candles = make_candles(prices, span=0.01)
        snaps = self.strategy.compute(candles)
        self.assertEqual(self.strategy.entry_signal(candles, snaps).action, "hold")

    def test_buy_signal_target_clears_breakeven(self):
        """매수 신호가 나오면 목표 순익은 반드시 손익분기를 넘어야 한다."""
        candles = breakout_candles()
        snaps = self.strategy.compute(candles)
        sig = self.strategy.entry_signal(candles, snaps)
        self.assertEqual(sig.action, "buy", sig.reason)
        self.assertGreater(sig.meta["target_net"], self.cost.breakeven_edge)
        self.assertGreaterEqual(sig.meta["target_net"], self.params.min_net_target)
        self.assertLess(sig.meta["stop_price"], snaps[-1].close)
        self.assertGreater(sig.meta["rr"], 1.0, "손익비가 1 미만인 진입은 허용하지 않는다")

    def test_overbought_breakout_is_rejected(self):
        """RSI 상한은 '이미 다 간 자리'를 거른다. 단조 상승 시계열은 진입 불가."""
        candles = make_candles(uptrend(step=0.01), span=0.003)
        snaps = self.strategy.compute(candles)
        sig = self.strategy.entry_signal(candles, snaps)
        self.assertEqual(sig.action, "hold")
        self.assertIn("RSI", sig.reason)

    def test_volume_gate_blocks_thin_breakout(self):
        """거래대금이 붙지 않은 돌파는 진입하지 않는다."""
        candles = breakout_candles()
        candles[-1].value = candles[-1].value / 5    # 돌파봉 거래대금만 축소
        snaps = self.strategy.compute(candles)
        sig = self.strategy.entry_signal(candles, snaps)
        self.assertEqual(sig.action, "hold")
        self.assertIn("거래대금", sig.reason)

    def test_higher_fees_block_an_otherwise_valid_entry(self):
        """같은 차트라도 수수료가 오르면 진입이 막혀야 한다 — 이 프로젝트의 핵심 주장."""
        candles = breakout_candles(chop=0.008, rally=0.02)
        self.assertEqual(self.strategy.entry_signal(candles, self.strategy.compute(candles)).action, "buy")
        pricey = replace(self.cost, fee_buy=0.0025, fee_sell=0.0025)
        strat = FeeAwareTrendStrategy(self.params, pricey)
        sig = strat.entry_signal(candles, strat.compute(candles))
        self.assertEqual(sig.action, "hold")
        self.assertIn("기대이동폭", sig.reason)

    def test_warmup_blocks_early_bars(self):
        candles = make_candles(uptrend(n=50, step=0.005), span=0.006)
        snaps = self.strategy.compute(candles)
        self.assertEqual(self.strategy.entry_signal(candles, snaps).action, "hold")


class TestExitLogic(unittest.TestCase):
    def setUp(self):
        self.cost = CostModel()
        self.params = StrategyParams()
        self.strategy = FeeAwareTrendStrategy(self.params, self.cost)
        self.candles = make_candles(uptrend(step=0.004), span=0.005)
        self.snaps = self.strategy.compute(self.candles)
        self.price = self.candles[-1].close

    def _pos(self, **kw):
        base = dict(
            market="KRW-TEST", entry_price=self.price, qty=1.0,
            stop_price=self.price * 0.95, target_net=0.01,
            peak_price=self.price, bars_held=1, armed=False,
        )
        base.update(kw)
        return PositionView(**base)

    def test_hard_stop_triggers(self):
        pos = self._pos(stop_price=self.price * 1.01)
        sig = self.strategy.exit_signal(pos, self.snaps)
        self.assertEqual(sig.action, "sell")
        self.assertEqual(sig.meta["exit_kind"], "stop")

    def test_take_profit_uses_net_not_gross(self):
        """목표 달성 판정은 비용 차감 후 수익률로 해야 한다."""
        entry = self.price / (1 + self.cost.required_gross_move(0.004))
        pos = self._pos(entry_price=entry, target_net=0.004, stop_price=entry * 0.9)
        sig = self.strategy.exit_signal(pos, self.snaps)
        self.assertEqual(sig.action, "sell")
        self.assertEqual(sig.meta["exit_kind"], "take_profit")

    def test_gross_gain_below_breakeven_does_not_take_profit(self):
        entry = self.price / (1 + self.cost.breakeven_edge * 0.5)   # 총 +0.1%, 순 -0.1%
        pos = self._pos(entry_price=entry, target_net=0.004, stop_price=entry * 0.9)
        sig = self.strategy.exit_signal(pos, self.snaps)
        self.assertNotEqual(sig.meta.get("exit_kind"), "take_profit")

    def test_trailing_only_after_armed(self):
        peak = self.price * 1.5
        unarmed = self._pos(peak_price=peak, stop_price=self.price * 0.5, armed=False)
        self.assertNotEqual(self.strategy.exit_signal(unarmed, self.snaps).meta.get("exit_kind"), "trail")
        armed = self._pos(peak_price=peak, stop_price=self.price * 0.5, armed=True)
        self.assertEqual(self.strategy.exit_signal(armed, self.snaps).meta.get("exit_kind"), "trail")

    def test_time_stop(self):
        pos = self._pos(stop_price=self.price * 0.5, target_net=99.0,
                        bars_held=self.params.max_hold_bars + 1)
        sig = self.strategy.exit_signal(pos, self.snaps)
        self.assertEqual(sig.meta.get("exit_kind"), "time")


class TestRiskGates(unittest.TestCase):
    def setUp(self):
        self.cost = CostModel()
        self.risk = RiskManager(RiskParams(max_order_krw=10_000_000), self.cost)
        self.risk.roll_day(10_000_000)

    def test_size_scales_inversely_with_stop_distance(self):
        wide, _ = self.risk.position_size_krw(10_000_000, 10_000_000, 100.0, 95.0, 0.0)
        tight, _ = self.risk.position_size_krw(10_000_000, 10_000_000, 100.0, 99.0, 0.0)
        self.assertGreater(tight, wide)

    def test_risk_per_trade_is_respected(self):
        """손절에 걸렸을 때 손실이 risk_per_trade 를 넘지 않아야 한다."""
        equity, entry, stop = 10_000_000.0, 100.0, 97.0
        size, _ = self.risk.position_size_krw(equity, equity, entry, stop, 0.0)
        loss = size * abs(self.cost.net_return(entry, stop))
        self.assertLessEqual(loss, equity * self.risk.p.risk_per_trade * 1.01)

    def test_invalid_stop_rejected(self):
        size, why = self.risk.position_size_krw(10_000_000, 10_000_000, 100.0, 100.0, 0.0)
        self.assertEqual(size, 0.0)
        self.assertIn("손절가", why)

    def test_daily_loss_limit_halts_trading(self):
        self.risk.record_exit("KRW-BTC", -400_000)
        ok, why = self.risk.can_open("KRW-ETH", 10_000_000, 0, 0.0)
        self.assertFalse(ok)
        self.assertIn("일일 손실한도", why)

    def test_daily_trade_limit_halts_trading(self):
        for _ in range(self.risk.p.daily_trade_limit):
            self.risk.record_entry()
        ok, why = self.risk.can_open("KRW-ETH", 10_000_000, 0, 0.0)
        self.assertFalse(ok)
        self.assertIn("일일 매매횟수", why)

    def test_consecutive_losses_trigger_cooldown(self):
        for _ in range(self.risk.p.consecutive_loss_pause):
            self.risk.record_exit("KRW-BTC", -1_000)
        self.assertIsNotNone(self.risk.state.paused_until)
        ok, why = self.risk.can_open("KRW-ETH", 10_000_000, 0, 0.0)
        self.assertFalse(ok)
        self.assertIn("쿨다운", why)

    def test_reentry_cooldown_is_per_market(self):
        self.risk.record_exit("KRW-BTC", 1_000)
        self.assertFalse(self.risk.can_open("KRW-BTC", 10_000_000, 0, 0.0)[0])
        self.assertTrue(self.risk.can_open("KRW-ETH", 10_000_000, 0, 0.0)[0])

    def test_exposure_cap(self):
        ok, why = self.risk.can_open("KRW-ETH", 10_000_000, 0, 7_000_000)
        self.assertFalse(ok)
        self.assertIn("익스포저", why)

    def test_min_order_floor(self):
        size, why = self.risk.position_size_krw(100_000, 100_000, 100.0, 50.0, 0.0)
        self.assertEqual(size, 0.0)
        self.assertIn("최소주문", why)


if __name__ == "__main__":
    unittest.main()

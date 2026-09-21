"""재기동 잔고 대조 검증.

여기서 지켜야 할 성질은 하나다.
"봇은 사용자 자산을 임의로 처분하지 않는다."
관리 대상이 아닌 보유분은 경고만 하고 절대 팔지 않는다.
"""
import os
import tempfile
import unittest
from datetime import datetime

from ubbit.reconcile import reconcile
from ubbit.state import Position, Store


class StubBroker:
    def __init__(self, balances):
        self._balances = balances
        self.sold = []

    def balances(self):
        return dict(self._balances)

    def sell(self, market, qty, ref):      # 호출되면 테스트가 잡아낸다
        self.sold.append(market)
        raise AssertionError("대조 과정에서 매도가 발생하면 안 된다")


class FailingBroker(StubBroker):
    def balances(self):
        raise RuntimeError("거래소 응답 없음")


def clock():
    return datetime(2026, 9, 21, 12, 0, 0)


class TestReconcile(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "r.db")
        self.store = Store(self.db)

    def tearDown(self):
        self.store.close()

    def _pos(self, market="KRW-BTC", qty=0.01):
        self.store.upsert_position(Position(
            market=market, entry_price=100_000_000, qty=qty, entry_krw=1_000_000,
            stop_price=97_000_000, target_net=0.01, peak_price=100_000_000))

    def test_matching_balance_is_clean(self):
        self._pos(qty=0.01)
        report = reconcile(self.store, StubBroker({"KRW": 50_000, "KRW-BTC": 0.01}),
                           {"KRW-BTC": 100_000_000}, clock)
        self.assertTrue(report.clean)
        self.assertEqual(report.checked, 1)
        self.assertIsNotNone(self.store.get_position("KRW-BTC"))

    def test_missing_coin_stops_management(self):
        """계좌에 코인이 없으면 관리를 중단해야 한다.
        그대로 두면 손절 주문이 매 주기 실패하고 봇은 지키는 중이라 착각한다."""
        self._pos()
        report = reconcile(self.store, StubBroker({"KRW": 1_000_000}),
                           {"KRW-BTC": 95_000_000}, clock)
        self.assertFalse(report.clean)
        self.assertEqual(len(report.dropped), 1)
        self.assertIsNone(self.store.get_position("KRW-BTC"))
        trades = self.store.recent_trades()
        self.assertEqual(trades[0]["exit_kind"], "reconcile_missing")
        self.assertEqual(trades[0]["net_pnl"], 0.0, "손익을 추정해서 사실처럼 기록하면 안 된다")
        self.assertIn("업비트 거래내역", trades[0]["reason"])

    def test_partial_balance_is_scaled_down(self):
        self._pos(qty=0.01)
        report = reconcile(self.store, StubBroker({"KRW-BTC": 0.004}),
                           {"KRW-BTC": 100_000_000}, clock)
        self.assertEqual(len(report.adjusted), 1)
        pos = self.store.get_position("KRW-BTC")
        self.assertAlmostEqual(pos.qty, 0.004)
        self.assertAlmostEqual(pos.entry_krw, 400_000.0, places=6,
                               msg="투입금액도 함께 축소돼야 수익률이 맞는다")

    def test_tiny_shortfall_is_tolerated(self):
        """수수료·반올림 수준의 오차로 포지션을 흔들면 안 된다."""
        self._pos(qty=0.01)
        report = reconcile(self.store, StubBroker({"KRW-BTC": 0.00999}),
                           {"KRW-BTC": 100_000_000}, clock)
        self.assertTrue(report.clean)

    def test_orphan_holding_is_reported_never_sold(self):
        """봇이 모르는 보유분은 사용자의 다른 자산일 수 있다. 절대 팔지 않는다."""
        broker = StubBroker({"KRW": 100_000, "KRW-ETH": 2.5})
        report = reconcile(self.store, broker, {"KRW-ETH": 3_000_000}, clock)
        self.assertEqual(len(report.orphans), 1)
        self.assertEqual(report.orphans[0].market, "KRW-ETH")
        self.assertEqual(broker.sold, [], "미관리 보유분을 매도하면 안 된다")
        self.assertIn("직접 처리", report.orphans[0].detail)

    def test_krw_balance_is_not_an_orphan(self):
        report = reconcile(self.store, StubBroker({"KRW": 5_000_000}), {}, clock)
        self.assertTrue(report.clean)

    def test_broker_failure_does_not_touch_positions(self):
        """잔고 조회가 실패했다고 포지션을 지우면, 통신 장애가 곧 포지션 유실이 된다."""
        self._pos()
        report = reconcile(self.store, FailingBroker({}), {}, clock)
        self.assertTrue(report.clean)
        self.assertEqual(report.checked, 0)
        self.assertIsNotNone(self.store.get_position("KRW-BTC"))

    def test_report_lines_are_human_readable(self):
        self._pos()
        report = reconcile(self.store, StubBroker({"KRW-DOGE": 1000.0}),
                           {"KRW-DOGE": 150.0}, clock)
        text = "\n".join(report.lines())
        self.assertIn("[제거]", text)
        self.assertIn("[미관리 보유]", text)


class TestNotifier(unittest.TestCase):
    def test_disabled_without_url(self):
        from ubbit.notify import Notifier, NotifyParams
        self.assertFalse(Notifier(NotifyParams()).enabled)
        Notifier(NotifyParams()).send("entry", "무시되어야 함")   # 예외 없이 통과

    def test_send_never_raises_on_bad_url(self):
        from ubbit.notify import Notifier, NotifyParams
        n = Notifier(NotifyParams(webhook_url="http://127.0.0.1:1/none", timeout=0.2))
        n.send("error", "전송 실패해도 매매는 계속돼야 한다")

    def test_kind_gates(self):
        from ubbit.notify import Notifier, NotifyParams
        sent = []
        n = Notifier(NotifyParams(webhook_url="http://x", on_entry=False))
        n._post = lambda text: sent.append(text)
        import threading
        original = threading.Thread
        threading.Thread = lambda target, args, daemon: type(
            "T", (), {"start": lambda self: target(*args)})()
        try:
            n.send("entry", "꺼져 있음")
            n.send("exit", "켜져 있음")
        finally:
            threading.Thread = original
        self.assertEqual(len(sent), 1)
        self.assertIn("켜져 있음", sent[0])


if __name__ == "__main__":
    unittest.main()


class TestConfigGuards(unittest.TestCase):
    """설정 단계에서 막아야 할 것들. 여기서 못 막으면 실거래 중에 드러난다."""

    def test_missing_config_file_raises(self):
        """오타 하나로 다른 타임프레임·다른 한도로 매매하게 두면 안 된다."""
        from ubbit.config import load_config
        with self.assertRaises(FileNotFoundError):
            load_config("존재하지-않는-설정.yaml")

    def test_no_path_uses_defaults(self):
        from ubbit.config import load_config
        cfg = load_config(None)
        self.assertEqual(cfg.mode, "paper")

    def test_live_without_keys_is_rejected(self):
        from ubbit.config import Config, validate
        cfg = Config(mode="live")
        with self.assertRaises(ValueError) as ctx:
            validate(cfg)
        self.assertIn("UPBIT_ACCESS_KEY", str(ctx.exception))

    def test_cli_returns_error_code_instead_of_traceback(self):
        import io
        import sys as _sys

        from ubbit.cli import main
        err = io.StringIO()
        original = _sys.stderr
        _sys.stderr = err
        try:
            code = main(["-c", "없는파일.yaml", "costs"])
        finally:
            _sys.stderr = original
        self.assertEqual(code, 2)
        self.assertIn("설정 파일 없음", err.getvalue())

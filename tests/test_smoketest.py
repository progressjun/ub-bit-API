"""스모크 테스트 자체의 안전장치 검증.

이 명령은 실제 돈을 쓴다. 따라서 '무엇을 막는가'가 '무엇을 하는가'보다 중요하다.
"""
import unittest
from unittest.mock import patch

from ubbit.config import Config
from ubbit.fees import CostModel
from ubbit.smoketest import MAX_AMOUNT, SmokeTestError, run


class StubClient:
    """업비트 응답을 흉내낸다. 부분체결·다중 체결 케이스를 포함한다."""

    def __init__(self, *, price=100_000_000.0, fee=0.0005, min_total=5_000.0,
                 balance=1_000_000.0, sell_ok=True):
        self.price = price
        self.fee = fee
        self.min_total = min_total
        self.balance = balance
        self.sell_ok = sell_ok
        self.orders = []

    def orders_chance(self, market):
        return {"bid_fee": str(self.fee), "ask_fee": str(self.fee),
                "market": {"bid": {"min_total": str(self.min_total)}},
                "bid_account": {"balance": str(self.balance)}}

    def orderbook(self, markets):
        return [{"orderbook_units": [
            {"ask_price": self.price * 1.0002, "ask_size": 100.0,
             "bid_price": self.price * 0.9998, "bid_size": 100.0}]}]

    def market_buy(self, market, krw, identifier=None):
        self.orders.append(("bid", krw))
        return {"uuid": "buy-1"}

    def market_sell(self, market, volume, identifier=None):
        if not self.sell_ok:
            from ubbit.upbit import UpbitError
            raise UpbitError(400, {"error": {"name": "insufficient_funds"}}, "/v1/orders")
        self.orders.append(("ask", volume))
        return {"uuid": "sell-1"}

    def wait_fill(self, uuid_, timeout=20.0):
        krw = 5_000.0
        if uuid_ == "buy-1":
            funds = krw / (1 + self.fee)
            qty = funds / (self.price * 1.0002)
            # 두 건으로 부분 체결된 상황
            return {"state": "done", "executed_volume": str(qty),
                    "paid_fee": str(funds * self.fee),
                    "trades": [{"funds": str(funds / 2), "volume": str(qty / 2)},
                               {"funds": str(funds / 2), "volume": str(qty / 2)}]}
        funds = krw / (1 + self.fee)
        qty = funds / (self.price * 1.0002)
        proceeds = qty * self.price * 0.9998
        return {"state": "done", "executed_volume": str(qty),
                "paid_fee": str(proceeds * self.fee),
                "trades": [{"funds": str(proceeds), "volume": str(qty)}]}


def cfg(fee=0.0005):
    return Config(access_key="ak", secret_key="sk", cost=CostModel(fee_buy=fee, fee_sell=fee))


class TestGuards(unittest.TestCase):
    def test_missing_keys_is_rejected(self):
        with self.assertRaises(SmokeTestError) as ctx:
            run(Config(), "KRW-BTC", 5_000)
        self.assertIn("UPBIT_ACCESS_KEY", str(ctx.exception))

    def test_amount_cap_is_enforced(self):
        """스모크 테스트에 큰 금액을 허용할 이유가 없다."""
        with self.assertRaises(SmokeTestError):
            run(cfg(), "KRW-BTC", MAX_AMOUNT + 1)

    def test_below_min_total_is_rejected(self):
        with patch("ubbit.smoketest.UpbitClient", lambda *a, **k: StubClient(min_total=5_000.0)):
            with self.assertRaises(SmokeTestError) as ctx:
                run(cfg(), "KRW-BTC", 1_000)
        self.assertIn("최소", str(ctx.exception))

    def test_insufficient_balance_is_rejected(self):
        with patch("ubbit.smoketest.UpbitClient", lambda *a, **k: StubClient(balance=100.0)):
            with self.assertRaises(SmokeTestError) as ctx:
                run(cfg(), "KRW-BTC", 5_000)
        self.assertIn("잔고 부족", str(ctx.exception))

    def test_sell_failure_is_loud_and_fatal(self):
        """매도 실패는 코인을 들고 있는 상태다. 조용히 넘어가면 안 된다."""
        with patch("ubbit.smoketest.UpbitClient", lambda *a, **k: StubClient(sell_ok=False)):
            with self.assertRaises(SmokeTestError) as ctx:
                run(cfg(), "KRW-BTC", 5_000)
        self.assertIn("매도 실패", str(ctx.exception))


class TestHappyPath(unittest.TestCase):
    def test_round_trip_passes_and_parses_partial_fills(self):
        stub = StubClient()
        with patch("ubbit.smoketest.UpbitClient", lambda *a, **k: stub):
            code = run(cfg(), "KRW-BTC", 5_000)
        self.assertEqual(code, 0)
        self.assertEqual([side for side, _ in stub.orders], ["bid", "ask"])

    def test_fee_mismatch_fails_the_test(self):
        """config 수수료가 서버와 다르면 모든 손익 계산이 틀어진다."""
        stub = StubClient(fee=0.0025)          # 서버는 0.25%
        with patch("ubbit.smoketest.UpbitClient", lambda *a, **k: stub):
            code = run(cfg(fee=0.0005), "KRW-BTC", 5_000)   # config 는 0.05%
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()

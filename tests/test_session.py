import unittest
from datetime import datetime

from ubbit.session import KST, SessionParams, evaluate


def at(day, hh, mm=0):
    return datetime(2026, 9, day, hh, mm, tzinfo=KST)


class TestSession(unittest.TestCase):
    def setUp(self):
        self.p = SessionParams()      # 평일 09:00~18:00, 종료 10분전 진입중단

    def test_open_during_business_hours(self):
        state = evaluate(self.p, at(21 + 1, 11))   # 2026-09-22 화요일 11:00
        self.assertTrue(state.can_enter)
        self.assertFalse(state.must_flatten)

    def test_closed_before_open(self):
        self.assertFalse(evaluate(self.p, at(22, 8, 59)).can_enter)

    def test_no_entry_in_final_minutes(self):
        state = evaluate(self.p, at(22, 17, 55))
        self.assertFalse(state.can_enter)
        self.assertFalse(state.must_flatten, "진입만 막고 청산 강제는 아직 아니다")
        self.assertIn("신규 진입 중단", state.reason)

    def test_flatten_after_close(self):
        state = evaluate(self.p, at(22, 18, 0))
        self.assertFalse(state.can_enter)
        self.assertTrue(state.must_flatten)

    def test_weekend_is_flatten(self):
        state = evaluate(self.p, at(26, 11))       # 2026-09-26 토요일
        self.assertFalse(state.can_enter)
        self.assertTrue(state.must_flatten)

    def test_disabled_runs_24h(self):
        state = evaluate(SessionParams(enabled=False), at(26, 3))
        self.assertTrue(state.can_enter)
        self.assertFalse(state.must_flatten)

    def test_flatten_can_be_disabled(self):
        p = SessionParams(flatten_at_close=False)
        self.assertFalse(evaluate(p, at(22, 20)).must_flatten)


if __name__ == "__main__":
    unittest.main()

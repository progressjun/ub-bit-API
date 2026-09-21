"""순수 파이썬 기술지표. numpy/pandas 의존성 없음.

모든 함수는 입력과 같은 길이의 리스트를 돌려주며, 계산 불가 구간은 None.
전략이 None 을 만나면 그 캔들은 판단을 보류한다(= 매매하지 않는다).
"""
from __future__ import annotations

from typing import Sequence

Num = float | None


def sma(values: Sequence[float], period: int) -> list[Num]:
    out: list[Num] = [None] * len(values)
    if period <= 0:
        return out
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= period:
            total -= values[i - period]
        if i >= period - 1:
            out[i] = total / period
    return out


def ema(values: Sequence[float], period: int) -> list[Num]:
    out: list[Num] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    k = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def rsi(closes: Sequence[float], period: int = 14) -> list[Num]:
    """Wilder RSI."""
    out: list[Num] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        gains += max(diff, 0.0)
        losses += max(-diff, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(diff, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-diff, 0.0)) / period
        out[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def true_range(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> list[Num]:
    out: list[Num] = [None] * len(closes)
    for i in range(len(closes)):
        if i == 0:
            out[i] = highs[i] - lows[i]
            continue
        prev_close = closes[i - 1]
        out[i] = max(highs[i] - lows[i], abs(highs[i] - prev_close), abs(lows[i] - prev_close))
    return out


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> list[Num]:
    """Wilder ATR. 변동성 = 기대 이동폭의 대리변수이자 손절폭의 기준."""
    trs = true_range(highs, lows, closes)
    out: list[Num] = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    seed = sum(t for t in trs[1 : period + 1] if t is not None) / period
    out[period] = seed
    prev = seed
    for i in range(period + 1, len(closes)):
        tr = trs[i] or 0.0
        prev = (prev * (period - 1) + tr) / period
        out[i] = prev
    return out


def donchian(highs: Sequence[float], lows: Sequence[float], period: int = 20) -> tuple[list[Num], list[Num]]:
    """(상단, 하단). 당해 캔들을 제외한 직전 N봉 기준 — 미래참조 방지."""
    up: list[Num] = [None] * len(highs)
    dn: list[Num] = [None] * len(lows)
    for i in range(len(highs)):
        if i < period:
            continue
        window_h = highs[i - period : i]
        window_l = lows[i - period : i]
        up[i] = max(window_h)
        dn[i] = min(window_l)
    return up, dn


def stdev(values: Sequence[float], period: int) -> list[Num]:
    out: list[Num] = [None] * len(values)
    for i in range(len(values)):
        if i < period - 1:
            continue
        window = values[i - period + 1 : i + 1]
        mean = sum(window) / period
        var = sum((v - mean) ** 2 for v in window) / period
        out[i] = var ** 0.5
    return out


def slope_pct(values: Sequence[Num], lookback: int) -> list[Num]:
    """lookback 봉 전 대비 변화율. 추세 기울기의 단순 대리지표."""
    out: list[Num] = [None] * len(values)
    for i in range(len(values)):
        if i < lookback:
            continue
        prev, cur = values[i - lookback], values[i]
        if prev in (None, 0) or cur is None:
            continue
        out[i] = (cur - prev) / prev
    return out


def rolling_max(values: Sequence[float], period: int) -> list[Num]:
    out: list[Num] = [None] * len(values)
    for i in range(len(values)):
        if i < period - 1:
            continue
        out[i] = max(values[i - period + 1 : i + 1])
    return out

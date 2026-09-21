"""매매 세션(업무 시간) 제어.

암호화폐 시장은 24시간이지만 봇을 24시간 돌릴 이유는 없다.
운영 시간을 제한하는 것은 편의가 아니라 리스크 관리다.

  한국 새벽 시간대(02~07시)는 거래대금이 낮고 호가가 얇아
  같은 신호여도 슬리피지가 커진다. 비용이 오르면 손익분기가 오르고,
  손익분기가 오르면 같은 전략이 적자로 뒤집힌다.

  포지션을 들고 자는 것은 '갭 리스크를 무보수로 떠안는 것'이다.
  단타 전략의 손절폭은 봉 단위로 설계되는데, 밤사이 급락은 그 손절폭을
  통째로 건너뛴다. 설계된 손실 한도가 지켜지지 않는다.

기본값은 평일 09:00~18:00 (KST), 종료 10분 전 신규 진입 중단,
종료 시각에 전량 청산이다. 종료 청산은 손실 중이어도 실행한다.
'조금만 더 기다리면 회복한다'는 판단이 계좌를 죽이는 전형적 경로이기 때문이다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone

KST = timezone(timedelta(hours=9))


@dataclass
class SessionParams:
    enabled: bool = True
    start: str = "09:00"
    end: str = "18:00"
    days: tuple[int, ...] = (0, 1, 2, 3, 4)      # 월=0 ... 일=6
    no_entry_before_close_minutes: int = 10
    flatten_at_close: bool = True


@dataclass
class SessionState:
    can_enter: bool
    must_flatten: bool
    reason: str


def now_kst() -> datetime:
    return datetime.now(KST)


def _parse(hhmm: str) -> time:
    hour, minute = hhmm.split(":")
    return time(int(hour), int(minute))


def evaluate(params: SessionParams, now: datetime | None = None) -> SessionState:
    """현재 시각이 매매 가능 구간인지, 강제 청산 구간인지 판정한다."""
    if not params.enabled:
        return SessionState(True, False, "세션 제한 없음 (24시간 운영)")

    now = now or now_kst()
    start, end = _parse(params.start), _parse(params.end)
    current = now.time()

    if now.weekday() not in params.days:
        return SessionState(False, params.flatten_at_close,
                            f"비운영 요일 ({'월화수목금토일'[now.weekday()]})")

    if current < start:
        return SessionState(False, False, f"개장 전 (시작 {params.start})")

    if current >= end:
        return SessionState(False, params.flatten_at_close, f"장 종료 (종료 {params.end})")

    cutoff = (datetime.combine(now.date(), end)
              - timedelta(minutes=params.no_entry_before_close_minutes)).time()
    if current >= cutoff:
        return SessionState(False, False,
                            f"종료 {params.no_entry_before_close_minutes}분 전 — 신규 진입 중단")

    return SessionState(True, False, f"운영 중 ({params.start}~{params.end})")

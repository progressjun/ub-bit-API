"""재기동 시 거래소 실제 잔고와 로컬 DB 대조.

무인 운용에서 가장 위험한 상태는 '봇이 사실과 다른 것을 믿는 것'이다.

  DB 에 포지션이 있는데 계좌에 코인이 없다
      → 손절 주문이 매번 실패한다. 봇은 관리 중이라 믿지만 실제로는 아무것도
        지켜지지 않는다. 사용자가 앱에서 직접 팔았거나, 매수 체결이 기록만 되고
        실제로는 실패했을 때 생긴다.

  계좌에 코인이 있는데 DB 에 없다
      → 손절 없이 방치된다. 봇이 죽은 사이 체결됐거나, 사용자의 장기 보유분이다.

두 번째 경우에 봇이 자동으로 팔면 안 된다. 사용자의 다른 자산일 수 있다.
경고만 하고 건드리지 않는다. 자동매매가 사용자 자산을 임의 처분하는 것은
어떤 편의보다 큰 사고다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .logutil import get_logger

log = get_logger("reconcile")

DUST_RATIO = 0.99        # 실제 수량이 기록의 99% 미만이면 불일치로 본다
DUST_FLOOR = 1e-8


@dataclass
class Mismatch:
    kind: str            # missing | partial | orphan
    market: str
    recorded_qty: float
    actual_qty: float
    detail: str


@dataclass
class ReconcileReport:
    checked: int = 0
    adjusted: list[Mismatch] = field(default_factory=list)
    dropped: list[Mismatch] = field(default_factory=list)
    orphans: list[Mismatch] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.adjusted or self.dropped or self.orphans)

    def lines(self) -> list[str]:
        out = [f"포지션 {self.checked}건 대조"]
        for m in self.dropped:
            out.append(f"  [제거] {m.market} — {m.detail}")
        for m in self.adjusted:
            out.append(f"  [수량조정] {m.market} — {m.detail}")
        for m in self.orphans:
            out.append(f"  [미관리 보유] {m.market} — {m.detail}")
        if self.clean:
            out.append("  불일치 없음")
        return out


def reconcile(store, broker, prices: dict[str, float], clock) -> ReconcileReport:
    """DB 포지션과 거래소 잔고를 맞춘다. 반환값은 사람이 읽을 보고서다.

    prices 는 제거 처리 시 청산가를 기록하기 위한 참고값이다. 값이 없으면
    진입가를 쓰고, 그 경우 손익 0 으로 기록된다(추정치를 사실처럼 남기지 않는다).
    """
    report = ReconcileReport()
    try:
        balances = broker.balances()
    except Exception as exc:                          # noqa: BLE001
        log.error("잔고 조회 실패 — 대조를 건너뜁니다: %s", exc)
        return report

    positions = store.all_positions()
    report.checked = len(positions)
    managed = {p.market for p in positions}

    for pos in positions:
        actual = float(balances.get(pos.market, 0.0))

        if actual <= DUST_FLOOR:
            price = prices.get(pos.market) or pos.entry_price
            exit_krw = actual_proceeds = 0.0
            store.record_trade(
                market=pos.market, opened_at=pos.opened_at,
                closed_at=clock().isoformat(timespec="seconds"),
                entry_price=pos.entry_price, exit_price=price, qty=pos.qty,
                entry_krw=pos.entry_krw, exit_krw=pos.entry_krw,
                fee_krw=0.0, net_pnl=0.0, net_return=0.0,
                exit_kind="reconcile_missing",
                reason="거래소 잔고 없음 — 손익 미상으로 0 기록. 실제 손익은 업비트 거래내역 확인 필요",
            )
            store.delete_position(pos.market)
            report.dropped.append(Mismatch(
                "missing", pos.market, pos.qty, actual,
                f"기록 {pos.qty:.8f} / 실제 0 — 관리 중단. "
                f"손익은 0 으로 기록했으므로 업비트 거래내역에서 확인하세요",
            ))
            continue

        if actual < pos.qty * DUST_RATIO:
            before = pos.qty
            pos.qty = actual
            pos.entry_krw = pos.entry_krw * (actual / before) if before else 0.0
            store.upsert_position(pos)
            report.adjusted.append(Mismatch(
                "partial", pos.market, before, actual,
                f"기록 {before:.8f} → 실제 {actual:.8f} 로 축소 (부분 체결/부분 매도 추정)",
            ))

    for market, qty in balances.items():
        if market == "KRW" or market in managed or qty <= DUST_FLOOR:
            continue
        value = qty * prices.get(market, 0.0)
        report.orphans.append(Mismatch(
            "orphan", market, 0.0, qty,
            f"{qty:.8f} 보유 (약 {value:,.0f}원) — 봇이 관리하지 않습니다. "
            f"손절이 걸리지 않으므로 직접 처리하거나 계좌를 분리하세요",
        ))

    return report

"""주문 실행 계층. Paper 와 Live 가 완전히 동일한 인터페이스를 갖는다.

Paper 와 Live 의 코드 경로를 분리하지 않는 이유: 실거래에서만 실행되는
코드가 있으면 그 코드는 한 번도 검증되지 않은 채 돈을 움직이게 된다.
"""
from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from .fees import CostModel, estimate_slippage_from_orderbook
from .logutil import get_logger
from .upbit import UpbitClient, UpbitError

log = get_logger("broker")


@dataclass
class Fill:
    ok: bool
    market: str
    side: str             # "bid" | "ask"
    price: float          # 평균 체결단가
    qty: float
    krw: float            # 매수: 투입원화 / 매도: 회수원화 (수수료 반영 후)
    fee: float
    raw: Any = None
    error: str = ""
    uncertain: bool = False
    identifier: str = ""


class Broker(ABC):
    mode: str = "abstract"

    @abstractmethod
    def balances(self) -> dict[str, float]: ...
    @abstractmethod
    def buy(self, market: str, krw_amount: float, ref_price: float) -> Fill: ...
    @abstractmethod
    def sell(self, market: str, qty: float, ref_price: float) -> Fill: ...
    @abstractmethod
    def equity(self, prices: dict[str, float]) -> tuple[float, float, float]: ...


# ---------------------------------------------------------------------- Paper
class PaperBroker(Broker):
    """가상 체결. 비용 모델을 그대로 적용하므로 실거래 대비 낙관 편향이 없다.

    다만 구조적으로 재현 못 하는 것이 둘 있다: 호가 소진(대형 주문의
    실제 슬리피지)과 거래소 장애/지연. 페이퍼 성과는 상한선으로만 읽어야 한다.
    """
    mode = "paper"

    def __init__(self, cost: CostModel, initial_krw: float = 1_000_000.0) -> None:
        self.cost = cost
        self.cash = initial_krw
        self.holdings: dict[str, float] = {}

    def balances(self) -> dict[str, float]:
        return {"KRW": self.cash, **self.holdings}

    def buy(self, market: str, krw_amount: float, ref_price: float) -> Fill:
        if krw_amount > self.cash:
            return Fill(False, market, "bid", 0, 0, 0, 0, error=f"잔고 부족 {self.cash:,.0f} < {krw_amount:,.0f}")
        qty, price, fee = self.cost.fill_buy(krw_amount, ref_price)
        if qty <= 0:
            return Fill(False, market, "bid", 0, 0, 0, 0, error="수량 0")
        self.cash -= krw_amount
        self.holdings[market] = self.holdings.get(market, 0.0) + qty
        return Fill(True, market, "bid", price, qty, krw_amount, fee, raw={"paper": True})

    def sell(self, market: str, qty: float, ref_price: float) -> Fill:
        held = self.holdings.get(market, 0.0)
        qty = min(qty, held)
        if qty <= 0:
            return Fill(False, market, "ask", 0, 0, 0, 0, error="보유 수량 없음")
        proceeds, price, fee = self.cost.fill_sell(qty, ref_price)
        self.cash += proceeds
        remaining = held - qty
        if remaining <= 1e-12:
            self.holdings.pop(market, None)
        else:
            self.holdings[market] = remaining
        return Fill(True, market, "ask", price, qty, proceeds, fee, raw={"paper": True})

    def equity(self, prices: dict[str, float]) -> tuple[float, float, float]:
        exposure = sum(qty * prices.get(m, 0.0) for m, qty in self.holdings.items())
        return self.cash + exposure, self.cash, exposure


# ----------------------------------------------------------------------- Live
class LiveBroker(Broker):
    """실주문. 모든 주문은 체결 확인까지 폴링하고 평균 체결단가를 역산한다.

    시장가 주문만 사용한다. 지정가는 미체결 관리(취소/재주문/부분체결)가
    필요한데, 그 로직의 버그는 조용히 포지션을 어긋나게 만든다.
    """
    mode = "live"

    def __init__(self, client: UpbitClient, cost: CostModel, *, fill_timeout: float = 20.0, journal=None) -> None:
        self.client = client
        self.cost = cost
        self.fill_timeout = fill_timeout
        self.journal = journal
        self.unresolved = False

    def balances(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for acc in self.client.accounts():
            currency = acc["currency"]
            amount = float(acc["balance"]) + float(acc.get("locked", 0) or 0)
            key = "KRW" if currency == "KRW" else f"KRW-{currency}"
            out[key] = out.get(key, 0.0) + amount
        return out

    def refresh_cost(self, market: str, krw_amount: float) -> CostModel:
        """주문 직전 호가창으로 슬리피지를 실측해 비용 모델을 갱신한다."""
        try:
            books = self.client.orderbook([market])
            if not books:
                return self.cost
            buy_slip, sell_slip = estimate_slippage_from_orderbook(books[0], krw_amount)
            if buy_slip == float("inf") or sell_slip == float("inf"):
                return self.cost
            from dataclasses import replace
            return replace(self.cost, slippage_buy=buy_slip, slippage_sell=sell_slip)
        except UpbitError as exc:
            log.warning("호가 조회 실패, 기본 슬리피지 사용: %s", exc)
            return self.cost

    def buy(self, market: str, krw_amount: float, ref_price: float) -> Fill:
        # Budget includes fee; Upbit market-buy price excludes the fee.
        return self._execute(market, "bid", krw_amount / (1 + self.cost.fee_buy))

    def sell(self, market: str, qty: float, ref_price: float) -> Fill:
        return self._execute(market, "ask", qty)

    def _execute(self, market: str, side: str, amount: float) -> Fill:
        if self.unresolved or (self.journal and self.journal.pending_intents()):
            return Fill(False, market, side, 0, 0, 0, 0, error="미확인 주문 대조 필요", uncertain=True)
        ident = uuid.uuid4().hex[:24]
        if self.journal:
            self.journal.intent(ident, market, side, "pending", {"amount": amount})
        try:
            method = self.client.market_buy if side == "bid" else self.client.market_sell
            resp = method(market, amount, identifier=ident)
            detail = self.client.wait_fill(resp["uuid"], timeout=self.fill_timeout)
        except UpbitError as exc:
            # A submission 4xx is a known rejection; a later lookup 4xx is not.
            if 400 <= exc.status < 500 and 'resp' not in locals():
                if self.journal:
                    self.journal.intent(ident, market, side, "rejected", {"error": exc.name})
                return Fill(False, market, side, 0, 0, 0, 0, error=str(exc), identifier=ident)
            try:
                detail = self.client.order_by_identifier(ident)
            except Exception:
                detail = {}
        except Exception:
            detail = {}
        if detail.get("state") not in ("done", "cancel"):
            self.unresolved = True
            if self.journal:
                self.journal.intent(ident, market, side, "unknown", detail)
            return Fill(False, market, side, 0, 0, 0, 0, error="주문 결과 미확인: 자동 주문 정지. 식별자 " + ident, uncertain=True, identifier=ident)
        fill = _fill_from_detail(detail, market, side)
        fill.identifier = ident
        if fill.qty > 0 and fill.price <= 0:
            self.unresolved = True
            fill.ok, fill.uncertain, fill.error = False, True, "체결금액 미확인"
        if self.journal:
            self.journal.intent(ident, market, side, "unknown" if fill.uncertain else "filled" if fill.ok else "rejected", detail)
        return fill

    def equity(self, prices: dict[str, float]) -> tuple[float, float, float]:
        bal = self.balances()
        cash = bal.get("KRW", 0.0)
        exposure = sum(qty * prices.get(m, 0.0) for m, qty in bal.items() if m != "KRW")
        return cash + exposure, cash, exposure


def _fill_from_detail(detail: dict, market: str, side: str) -> Fill:
    """체결 내역(trades)에서 평균 체결단가와 수수료를 역산한다.

    업비트 응답의 executed_volume / paid_fee 를 신뢰하되, 개별 체결 리스트가
    있으면 그것으로 가중평균 단가를 계산한다(부분체결 대응).
    """
    trades = detail.get("trades") or []
    qty = float(detail.get("executed_volume") or 0.0)
    fee = float(detail.get("paid_fee") or 0.0)

    if trades:
        funds = sum(float(t["funds"]) for t in trades)
        volume = sum(float(t["volume"]) for t in trades)
    else:
        volume = qty
        funds = float(detail.get("executed_funds") or 0.0)

    if volume <= 0:
        return Fill(False, market, side, 0, 0, 0, 0, raw=detail,
                    error=f"체결 없음 state={detail.get('state')}")

    avg_price = funds / volume
    krw = funds + fee if side == "bid" else funds - fee
    return Fill(True, market, side, avg_price, volume, krw, fee, raw=detail)

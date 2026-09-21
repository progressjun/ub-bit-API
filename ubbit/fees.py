"""수수료·슬리피지 비용 모델.

이 파일이 이 프로젝트의 핵심이다. 전략 로직보다 먼저 성립해야 하는 것은
"왕복 비용을 넘기는 기대 이동폭이 있는가" 라는 진입 조건이기 때문이다.

확정 팩트
  업비트 KRW 마켓 거래 수수료: 매수 0.05%, 매도 0.05% (2026-09 기준)
  최소 주문 금액: KRW 마켓 5,000원 (정책 변경 가능 → orders_chance 로 실측 권장)

모델 정의
  매수 체결단가  P_in_eff  = P_in  * (1 + slip_buy)
  매도 체결단가  P_out_eff = P_out * (1 - slip_sell)
  투입 원화     C = qty * P_in_eff  * (1 + fee_buy)
  회수 원화     R = qty * P_out_eff * (1 - fee_sell)

  손익분기 배수 B = (1 + fee_buy)(1 + slip_buy) / ((1 - fee_sell)(1 - slip_sell))
  즉 P_out 이 P_in 대비 (B - 1) 이상 올라야 비로소 0원이다.

  수수료만 0.05%/0.05% 라면 B - 1 = 0.1001%
  슬리피지 0.05%/0.05% 를 더하면 B - 1 = 0.2005%

왕복 비용이 0.2% 라는 뜻은, 일 10회전 시 하루 2.0% 의 확정 비용을
총수익에서 먼저 빼고 시작한다는 뜻이다. 회전수 자체가 전략 변수다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

KRW_MIN_ORDER = 5_000.0


@dataclass(frozen=True)
class CostModel:
    fee_buy: float = 0.0005          # 업비트 KRW 마켓 매수 수수료
    fee_sell: float = 0.0005         # 업비트 KRW 마켓 매도 수수료
    slippage_buy: float = 0.0005     # 시장가 매수 체결 불리분 (호가 스프레드 기반 추정치)
    slippage_sell: float = 0.0005    # 시장가 매도 체결 불리분
    min_order_krw: float = KRW_MIN_ORDER

    # ------------------------------------------------------------ 기본 배수
    @property
    def buy_multiple(self) -> float:
        return (1.0 + self.fee_buy) * (1.0 + self.slippage_buy)

    @property
    def sell_multiple(self) -> float:
        return (1.0 - self.fee_sell) * (1.0 - self.slippage_sell)

    @property
    def breakeven_multiple(self) -> float:
        """매수가 대비 매도가가 이 배수 이상이어야 손익 0."""
        return self.buy_multiple / self.sell_multiple

    @property
    def breakeven_edge(self) -> float:
        """손익분기까지 필요한 상승률. 예: 0.002005 == 0.2005%"""
        return self.breakeven_multiple - 1.0

    @property
    def fee_only_edge(self) -> float:
        """슬리피지를 제외한 순수 수수료 손익분기 (체결 품질 평가용 기준선)."""
        return (1.0 + self.fee_buy) / (1.0 - self.fee_sell) - 1.0

    # ------------------------------------------------------------ 손익 계산
    def net_return(self, entry_price: float, exit_price: float) -> float:
        """비용 차감 후 순수익률. 전략·백테스트·실거래가 모두 이 함수만 쓴다."""
        if entry_price <= 0:
            return 0.0
        return (exit_price * self.sell_multiple) / (entry_price * self.buy_multiple) - 1.0

    def gross_to_net(self, gross_return: float) -> float:
        """총수익률(비용 전)을 순수익률로 환산."""
        return (1.0 + gross_return) / self.breakeven_multiple - 1.0

    def required_exit_price(self, entry_price: float, target_net: float = 0.0) -> float:
        """목표 순수익률을 달성하는 매도 호가."""
        return entry_price * self.breakeven_multiple * (1.0 + target_net)

    def required_gross_move(self, target_net: float) -> float:
        """목표 순수익률에 필요한 총상승률."""
        return self.breakeven_multiple * (1.0 + target_net) - 1.0

    # ------------------------------------------------------------ 체결 시뮬
    def fill_buy(self, krw_amount: float, price: float) -> tuple[float, float, float]:
        """원화 투입액 → (체결수량, 실체결단가, 수수료원화).

        업비트 시장가 매수(ord_type=price)는 전달한 금액 안에서
        수수료를 포함해 처리되므로, 실제 매수에 쓰이는 금액은
        krw_amount / (1 + fee_buy) 로 보수적으로 잡는다.
        """
        if krw_amount <= 0 or price <= 0:
            return 0.0, 0.0, 0.0
        effective_price = price * (1.0 + self.slippage_buy)
        spend = krw_amount / (1.0 + self.fee_buy)
        fee = krw_amount - spend
        qty = spend / effective_price
        return qty, effective_price, fee

    def fill_sell(self, qty: float, price: float) -> tuple[float, float, float]:
        """수량 → (회수 원화, 실체결단가, 수수료원화)."""
        if qty <= 0 or price <= 0:
            return 0.0, 0.0, 0.0
        effective_price = price * (1.0 - self.slippage_sell)
        gross = qty * effective_price
        fee = gross * self.fee_sell
        return gross - fee, effective_price, fee

    # ------------------------------------------------------------ 회전 비용
    def drag_per_day(self, round_trips_per_day: float) -> float:
        """하루 N회전 시 확정 비용 드래그. 알파가 0이면 이만큼 그대로 손실."""
        return 1.0 - (1.0 - self.breakeven_edge) ** round_trips_per_day

    def max_round_trips(self, expected_net_edge: float, budget: float = 0.5) -> float:
        """1회전당 기대 순엣지가 주어졌을 때, 비용이 기대수익의 budget 비율을
        넘지 않는 최대 회전 강도를 알려주는 보조 지표."""
        if expected_net_edge <= 0:
            return 0.0
        return budget * expected_net_edge / self.breakeven_edge

    def describe(self) -> str:
        return (
            f"수수료 매수 {self.fee_buy*100:.3f}% / 매도 {self.fee_sell*100:.3f}%, "
            f"슬리피지 {self.slippage_buy*100:.3f}%/{self.slippage_sell*100:.3f}% → "
            f"손익분기 {self.breakeven_edge*100:.4f}% "
            f"(수수료만: {self.fee_only_edge*100:.4f}%), "
            f"일 10회전 드래그 {self.drag_per_day(10)*100:.2f}%"
        )


def estimate_slippage_from_orderbook(orderbook: dict, krw_amount: float) -> tuple[float, float]:
    """호가창으로 실제 슬리피지를 추정한다. (매수 슬리피지, 매도 슬리피지)

    중간가(mid) 대비 주문 금액을 소진하는 데 필요한 가중평균 체결단가의
    괴리를 계산한다. 유동성이 얇은 알트코인에서 고정 슬리피지 가정이
    얼마나 위험한지 실측으로 드러내기 위한 함수다.
    """
    units = orderbook.get("orderbook_units") or []
    if not units:
        return 0.0005, 0.0005
    best_ask = float(units[0]["ask_price"])
    best_bid = float(units[0]["bid_price"])
    mid = (best_ask + best_bid) / 2.0
    if mid <= 0:
        return 0.0005, 0.0005

    def _walk(side_price: str, side_size: str) -> float:
        remaining = krw_amount
        cost = 0.0
        filled = 0.0
        for unit in units:
            price = float(unit[side_price])
            size = float(unit[side_size])
            level_krw = price * size
            take = min(remaining, level_krw)
            if take <= 0:
                break
            qty = take / price
            cost += qty * price
            filled += qty
            remaining -= take
            if remaining <= 0:
                break
        if filled <= 0 or remaining > 0:
            # 호가창을 다 먹어도 안 채워지는 주문 = 그 종목에 그 사이즈는 부적합
            return float("inf")
        return cost / filled

    avg_ask = _walk("ask_price", "ask_size")
    avg_bid = _walk("bid_price", "bid_size")
    buy_slip = (avg_ask - mid) / mid if avg_ask != float("inf") else float("inf")
    sell_slip = (mid - avg_bid) / mid if avg_bid != float("inf") else float("inf")
    return max(buy_slip, 0.0), max(sell_slip, 0.0)


def infer_tick_size(orderbook: dict) -> float:
    """호가 단위를 하드코딩 테이블 대신 실제 호가창 간격에서 추론한다.

    업비트 호가 단위 정책은 개정 이력이 있어 테이블을 박아두면 조용히
    틀린다. 지정가 주문을 낼 때만 필요하며, 실패 시 0 을 반환한다.
    """
    units = orderbook.get("orderbook_units") or []
    if len(units) < 2:
        return 0.0
    gaps = []
    for series in ("ask_price", "bid_price"):
        prices = [float(u[series]) for u in units]
        for a, b in zip(prices, prices[1:]):
            gap = abs(b - a)
            if gap > 0:
                gaps.append(round(gap, 10))
    return min(gaps) if gaps else 0.0


def align_price_to_tick(price: float, tick: float, side: str) -> float:
    """지정가를 호가 단위에 맞춘다. 매수는 내림, 매도는 올림(체결에 보수적)."""
    if tick <= 0:
        return price
    steps = price / tick
    aligned = (math.floor(steps) if side == "bid" else math.ceil(steps)) * tick
    return round(aligned, 10)

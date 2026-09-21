"""실주문 경로 스모크 테스트.

이 프로젝트에서 유일하게 검증되지 않은 코드 경로가 LiveBroker 다.
페이퍼는 테스트로 덮여 있지만, 실주문 응답을 파싱하는 부분
(_fill_from_detail, 평균 체결단가 역산, 수수료 집계)은 업비트 응답 형식에
대한 가정 위에 서 있다. 그 가정은 실제 주문을 내봐야만 확인된다.

그래서 최소 금액으로 한 번 사고 즉시 판다. 확인하는 것은 수익이 아니라
아래 네 가지다.

  주문이 실제로 체결되는가
  체결 응답에서 수량·단가·수수료를 정확히 읽어내는가
  모델이 예측한 왕복 비용과 실제 왕복 비용이 일치하는가
  비용 모델의 수수료율이 계좌에 적용된 실제 수수료율과 같은가

노출 시간은 수 초, 예상 손실은 왕복 비용(5,000원 기준 약 10~30원)이다.
그 돈으로 '봇이 실제로 주문을 낼 수 있는가'를 사는 것이다.
"""
from __future__ import annotations

import time
from dataclasses import replace

from .broker import LiveBroker
from .config import Config
from .fees import CostModel, estimate_slippage_from_orderbook
from .logutil import get_logger
from .upbit import UpbitClient, UpbitError

log = get_logger("smoketest")

MAX_AMOUNT = 20_000.0      # 스모크 테스트에 이보다 큰 금액을 허용할 이유가 없다


class SmokeTestError(RuntimeError):
    pass


def run(cfg: Config, market: str, amount: float) -> int:
    if not (cfg.access_key and cfg.secret_key):
        raise SmokeTestError("UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY 환경변수가 필요합니다.")
    if amount > MAX_AMOUNT:
        raise SmokeTestError(f"스모크 테스트 금액 상한은 {MAX_AMOUNT:,.0f}원입니다.")

    client = UpbitClient(cfg.access_key, cfg.secret_key)

    print("=" * 76)
    print(f"실주문 스모크 테스트 — {market}, {amount:,.0f}원")
    print("=" * 76)

    # 1) 서버가 알려주는 실제 수수료율·최소주문금액을 먼저 읽는다.
    try:
        chance = client.orders_chance(market)
    except UpbitError as exc:
        raise SmokeTestError(f"주문 가능 정보 조회 실패: {exc}\n"
                             f"  확인: API 키 권한('자산조회'+'주문하기'), 허용 IP 등록") from exc

    bid_fee = float(chance["bid_fee"])
    ask_fee = float(chance["ask_fee"])
    min_total = float(chance["market"]["bid"]["min_total"])
    krw_balance = float(chance["bid_account"]["balance"])

    print(f"\n[1/5] 계좌 확인")
    print(f"  서버 수수료율   매수 {bid_fee*100:.4f}%  매도 {ask_fee*100:.4f}%")
    print(f"  최소 주문금액   {min_total:,.0f}원")
    print(f"  주문가능 원화   {krw_balance:,.0f}원")

    mismatch = (abs(bid_fee - cfg.cost.fee_buy) > 1e-6
                or abs(ask_fee - cfg.cost.fee_sell) > 1e-6)
    if mismatch:
        print(f"  [경고] config 수수료({cfg.cost.fee_buy*100:.4f}%/"
              f"{cfg.cost.fee_sell*100:.4f}%)가 서버 값과 다릅니다. config 를 맞추세요.")
    if amount < min_total:
        raise SmokeTestError(f"주문 금액 {amount:,.0f}원 < 최소 {min_total:,.0f}원")
    if krw_balance < amount:
        raise SmokeTestError(f"원화 잔고 부족: {krw_balance:,.0f}원 < {amount:,.0f}원")

    # 2) 주문 직전 호가로 예상 비용을 계산해 둔다. 나중에 실측과 대조한다.
    books = client.orderbook([market])
    if not books:
        raise SmokeTestError("호가 조회 실패")
    buy_slip, sell_slip = estimate_slippage_from_orderbook(books[0], amount)
    units = books[0]["orderbook_units"][0]
    mid = (float(units["ask_price"]) + float(units["bid_price"])) / 2.0
    predicted = replace(CostModel(fee_buy=bid_fee, fee_sell=ask_fee),
                        slippage_buy=buy_slip, slippage_sell=sell_slip)

    print(f"\n[2/5] 주문 전 예측")
    print(f"  중간가          {mid:,.2f}")
    print(f"  호가 기반 슬리피지  매수 {buy_slip*100:.4f}%  매도 {sell_slip*100:.4f}%")
    print(f"  예상 왕복 비용     {predicted.breakeven_edge*100:.4f}%  "
          f"({amount * predicted.breakeven_edge:,.0f}원)")

    broker = LiveBroker(client, predicted)

    # 3) 매수
    print(f"\n[3/5] 시장가 매수 {amount:,.0f}원 …")
    buy = broker.buy(market, amount, mid)
    if not buy.ok:
        raise SmokeTestError(f"매수 실패: {buy.error}")
    print(f"  체결 수량   {buy.qty:.8f}")
    print(f"  평균 단가   {buy.price:,.2f}   (중간가 대비 {(buy.price/mid-1)*100:+.4f}%)")
    print(f"  투입 원화   {buy.krw:,.2f}")
    print(f"  수수료      {buy.fee:,.2f}원  (실효 {buy.fee/max(buy.krw-buy.fee,1)*100:.4f}%)")

    if buy.qty <= 0:
        raise SmokeTestError("체결 수량이 0입니다. 응답 파싱이 잘못되었을 수 있습니다.")

    # 4) 즉시 매도. 실패하면 사용자가 직접 처리해야 하므로 크게 알린다.
    print(f"\n[4/5] 시장가 매도 {buy.qty:.8f} …")
    time.sleep(0.5)
    sell = broker.sell(market, buy.qty, buy.price)
    if not sell.ok:
        print("\n" + "!" * 76)
        print(f"매도 실패: {sell.error}")
        print(f"{market} {buy.qty:.8f} 를 보유한 상태입니다. 업비트 앱에서 직접 매도하세요.")
        print("!" * 76)
        raise SmokeTestError("매도 실패 — 수동 처리 필요")
    print(f"  체결 수량   {sell.qty:.8f}")
    print(f"  평균 단가   {sell.price:,.2f}")
    print(f"  회수 원화   {sell.krw:,.2f}")
    print(f"  수수료      {sell.fee:,.2f}원")

    # 5) 예측 대 실측
    actual_cost = 1.0 - sell.krw / buy.krw
    predicted_cost = predicted.breakeven_edge
    gap = actual_cost - predicted_cost
    price_move = sell.price / buy.price - 1.0

    print(f"\n[5/5] 예측 대 실측")
    print(f"  예측 왕복 비용   {predicted_cost*100:>8.4f}%")
    print(f"  실측 왕복 손실   {actual_cost*100:>8.4f}%  ({buy.krw - sell.krw:,.2f}원)")
    print(f"  차이            {gap*100:>+8.4f}%p")
    print(f"  (그 사이 가격 변동 {price_move*100:+.4f}% 포함)")

    print("\n" + "=" * 76)
    ok = True
    if abs(gap) > 0.005:
        print("[실패] 실측 비용이 예측과 0.5%p 넘게 벌어졌습니다.")
        print("       config 의 slippage 값을 실측에 맞게 올리고 전략을 재검증하세요.")
        ok = False
    elif abs(gap) > 0.002:
        print("[주의] 실측 비용이 예측과 0.2%p 벌어졌습니다. 슬리피지 가정을 조정하세요.")
    else:
        print("[통과] 주문 경로가 정상 동작하고, 비용 모델이 실측과 일치합니다.")
    if mismatch:
        print("[실패] config 수수료율이 서버 값과 다릅니다. 먼저 맞추세요.")
        ok = False
    print(f"\n이번 테스트 비용: {buy.krw - sell.krw:,.0f}원")
    print("=" * 76)
    return 0 if ok else 1

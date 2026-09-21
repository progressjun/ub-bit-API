"""명령줄 진입점.

  python -m ubbit costs              비용 구조 분석 (주문 없음, 키 불필요)
  python -m ubbit fetch KRW-BTC      캔들 수집 → data/candles/*.json
  python -m ubbit backtest KRW-BTC   백테스트 + 수수료 민감도
  python -m ubbit doctor             API 키/권한/잔고/슬리피지 실측 점검
  python -m ubbit paper              가상매매 (실주문 없음)
  python -m ubbit live --yes-i-know  실주문 (명시적 동의 필요)
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .adapt import ParamStore, evaluate_promotion, walk_forward
from .backtest import fee_sensitivity, run_backtest
from .config import Config, load_config
from .engine import TradingEngine
from .fees import CostModel, estimate_slippage_from_orderbook, infer_tick_size
from .logutil import setup_logging
from .strategy import Candle
from .universe import build_universe
from .upbit import UpbitClient, UpbitError

CANDLE_DIR = "data/candles"


# --------------------------------------------------------------------- costs
def cmd_costs(cfg: Config, args: argparse.Namespace) -> int:
    cost = cfg.cost
    print("=" * 74)
    print("비용 구조 분석 (업비트 KRW 마켓)")
    print("=" * 74)
    print(f"  매수 수수료        {cost.fee_buy*100:.4f}%")
    print(f"  매도 수수료        {cost.fee_sell*100:.4f}%")
    print(f"  매수 슬리피지 가정  {cost.slippage_buy*100:.4f}%")
    print(f"  매도 슬리피지 가정  {cost.slippage_sell*100:.4f}%")
    print(f"  손익분기 상승률    {cost.breakeven_edge*100:.4f}%  (수수료만: {cost.fee_only_edge*100:.4f}%)")
    print()
    print("  목표 순수익률별 필요 총상승률")
    for target in (0.002, 0.005, 0.01, 0.02, 0.05):
        print(f"    순 {target*100:>4.1f}% 달성 → 총 {cost.required_gross_move(target)*100:>6.3f}% 상승 필요")
    print()
    print("  회전수별 확정 비용 드래그 (알파 0 가정)")
    for trips in (1, 3, 5, 10, 20, 50):
        drag = cost.drag_per_day(trips)
        print(f"    일 {trips:>2}회전 → 일 {drag*100:>5.2f}% / 20영업일 누적 {(1-(1-drag)**20)*100:>5.1f}%")
    print()
    print("  손익분기 승률 (목표:손절 = R:1 구조)")
    for rr in (1.0, 1.5, 2.0, 3.0):
        target = cfg.strategy.min_net_target
        stop = target / rr
        be_wr = stop / (target + stop)
        print(f"    RR {rr:>3.1f} (익절 {target*100:.2f}% / 손절 {stop*100:.2f}%) → 손익분기 승률 {be_wr*100:.1f}%")
    print()
    floor = cost.breakeven_edge * cfg.strategy.atr_cost_multiple
    print(f"  현재 설정의 진입 변동성 하한: 봉당 ATR% >= {floor*100:.3f}%")
    print(f"  현재 설정의 목표 순익 하한  : {cfg.strategy.min_net_target*100:.3f}% "
          f"(= 손익분기의 {cfg.strategy.min_net_target/cost.breakeven_edge:.1f}배)")
    print("=" * 74)
    return 0


# --------------------------------------------------------------------- fetch
def cmd_fetch(cfg: Config, args: argparse.Namespace) -> int:
    client = UpbitClient()
    os.makedirs(CANDLE_DIR, exist_ok=True)
    market = args.market
    unit = args.unit or cfg.engine.candle_unit
    want = args.count

    rows: list[dict] = []
    to = None
    while len(rows) < want:
        batch = client.candles(market, unit=unit, count=min(200, want - len(rows)), to=to)
        if not batch:
            break
        rows = batch + rows
        to = batch[0]["candle_date_time_utc"]
        print(f"\r수집 {len(rows)}/{want}봉 ... {rows[0]['candle_date_time_kst']}", end="", flush=True)
    print()

    path = os.path.join(CANDLE_DIR, f"{market}_{unit}m.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False)
    print(f"저장: {path} ({len(rows)}봉, {rows[0]['candle_date_time_kst']} ~ {rows[-1]['candle_date_time_kst']})")
    return 0


def _load_candles(market: str, unit: int, count: int | None = None) -> list[Candle]:
    path = os.path.join(CANDLE_DIR, f"{market}_{unit}m.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            rows = json.load(fh)
    else:
        print(f"[알림] 로컬 캔들 없음 → API 에서 {count or 200}봉 조회 ({path} 에 저장하려면 fetch 사용)")
        rows = UpbitClient().candles(market, unit=unit, count=count or 200)
    return [Candle.from_upbit(r) for r in rows]


# ------------------------------------------------------------------ backtest
def cmd_backtest(cfg: Config, args: argparse.Namespace) -> int:
    unit = args.unit or cfg.engine.candle_unit
    candles = _load_candles(args.market, unit, args.count)
    if len(candles) < cfg.strategy.warmup_bars + 20:
        print(f"캔들 부족: {len(candles)}봉. 최소 {cfg.strategy.warmup_bars+20}봉 필요.")
        return 1
    bars_per_day = 1440 / unit

    print("=" * 74)
    print(f"백테스트 {args.market} {unit}분봉 {len(candles)}봉 "
          f"({candles[0].ts} ~ {candles[-1].ts})")
    print("=" * 74)
    result = run_backtest(
        args.market, candles, cost=cfg.cost, sparams=cfg.strategy,
        rparams=cfg.risk, initial_krw=cfg.engine.initial_krw,
    )
    print(result.summary(cfg.cost, bars_per_day))

    if args.trades and result.trades:
        print("\n체결 내역")
        for t in result.trades:
            print(f"  {t.entry_ts} → {t.exit_ts}  {t.entry_price:>12,.2f} → {t.exit_price:>12,.2f}  "
                  f"{t.net_return*100:>+7.3f}%  {t.net_pnl:>+10,.0f}원  {t.bars:>3}봉  {t.exit_kind}")

    print("\n" + "=" * 74)
    print("수수료·슬리피지 민감도 — 이 표가 전략의 생존 조건이다")
    print("=" * 74)
    print(f"{'비용 시나리오':<28}{'손익분기':>9}{'거래':>6}{'수익률':>10}{'MDD':>9}{'승률':>8}{'평균/건':>10}")
    for label, res in fee_sensitivity(
        args.market, candles, base_cost=cfg.cost, sparams=cfg.strategy,
        rparams=cfg.risk, initial_krw=cfg.engine.initial_krw,
    ):
        print(f"{label:<28}", end="")
        print(f"{_be_of(label, cfg.cost)*100:>8.3f}%"
              f"{len(res.trades):>6}"
              f"{res.total_return*100:>+9.2f}%"
              f"{res.max_drawdown*100:>8.2f}%"
              f"{res.win_rate*100:>7.1f}%"
              f"{res.avg_net_return*100:>+9.3f}%")
    print("=" * 74)
    print("판단 기준: '수수료 0%' 행에서만 흑자라면 전략이 아니라 비용 차익이다.")
    print("          슬리피지 2배 행에서 적자로 뒤집히면 실거래 생존력이 없다.")
    return 0


def _be_of(label: str, base: CostModel) -> float:
    from dataclasses import replace
    if "0%" in label:
        return replace(base, fee_buy=0, fee_sell=0, slippage_buy=0, slippage_sell=0).breakeven_edge
    if "수수료만" in label:
        return replace(base, slippage_buy=0, slippage_sell=0).breakeven_edge
    if "2배" in label:
        return replace(base, slippage_buy=0.001, slippage_sell=0.001).breakeven_edge
    if "4배" in label:
        return replace(base, slippage_buy=0.002, slippage_sell=0.002).breakeven_edge
    return base.breakeven_edge


# ---------------------------------------------------------------------- scan
def cmd_scan(cfg: Config, args: argparse.Namespace) -> int:
    """종목 × 타임프레임의 구조적 적합성 판정.

    전략을 고르기 전에 답해야 할 질문은 하나다.
    "이 종목의 이 타임프레임에서, 봉당 기대 이동폭이 왕복 비용을 넘기는가."
    넘기지 못하면 어떤 전략을 얹어도 수수료가 먼저 계좌를 깎는다.
    """
    import statistics

    from .indicators import atr as _atr

    client = UpbitClient()
    markets = args.markets or cfg.engine.markets
    units = args.units or [5, 15, 60, 240]
    cost = cfg.cost
    floor = cost.breakeven_edge * cfg.strategy.atr_cost_multiple

    print("=" * 84)
    print(f"구조 적합성 스캔 — 손익분기 {cost.breakeven_edge*100:.4f}%, "
          f"진입하한 ATR% >= {floor*100:.3f}% (손익분기 × {cfg.strategy.atr_cost_multiple:.1f})")
    print("=" * 84)
    print(f"{'종목':<11}{'TF':>6}{'ATR%중앙':>10}{'상위25%':>9}{'최대':>8}"
          f"{'하한초과':>9}{'회전당순엣지':>12}  판정")

    for market in markets:
        for unit in units:
            try:
                rows = client.candles(market, unit=unit, count=args.count)
            except UpbitError as exc:
                print(f"{market:<11}{unit:>5}m  조회 실패: {exc}")
                continue
            candles = [Candle.from_upbit(r) for r in rows]
            if len(candles) < 30:
                continue
            series = _atr([c.high for c in candles], [c.low for c in candles],
                          [c.close for c in candles], cfg.strategy.atr_period)
            pcts = sorted(v / candles[i].close for i, v in enumerate(series) if v)
            if not pcts:
                continue
            median = statistics.median(pcts)
            q3 = pcts[int(len(pcts) * 0.75)]
            share = sum(1 for p in pcts if p >= floor) / len(pcts)
            # 목표가를 ATR × take_profit_atr 로 잡았을 때의 회전당 기대 순엣지
            edge = cost.gross_to_net(median * cfg.strategy.take_profit_atr)
            if share >= 0.5 and edge > cost.breakeven_edge * 2:
                verdict = "적합"
            elif share >= 0.2:
                verdict = "경계 — 신호 희소"
            else:
                verdict = "부적합 — 비용이 이동폭을 잠식"
            print(f"{market:<11}{unit:>5}m{median*100:>9.3f}%{q3*100:>8.3f}%{pcts[-1]*100:>7.2f}%"
                  f"{share*100:>8.1f}%{edge*100:>11.3f}%  {verdict}")
    print("=" * 84)
    print("해석: '부적합' 조합은 전략 파라미터를 바꿔서 해결되지 않는다. 비용 구조의 문제다.")
    print("      '적합'은 진입 자격일 뿐 수익 보장이 아니다. 백테스트로 별도 검증할 것.")
    return 0

# ------------------------------------------------------------------ universe
def cmd_universe(cfg: Config, args: argparse.Namespace) -> int:
    """오늘 매매 자격이 있는 종목을 선별해 보여준다 (주문 없음)."""
    unit = args.unit or cfg.engine.candle_unit
    params = cfg.universe
    if args.top:
        from dataclasses import replace as _replace
        params = _replace(params, top_n=args.top)

    print("=" * 96)
    print(f"유니버스 선별 — {unit}분봉 기준 | 손익분기 {cfg.cost.breakeven_edge*100:.4f}% | "
          f"변동성 하한 ATR% >= {cfg.cost.breakeven_edge*params.atr_cost_multiple*100:.3f}%")
    print(f"유동성 하한 24h {params.min_turnover_krw/1e8:,.0f}억 | "
          f"실측 왕복비용 상한 {params.max_round_trip_cost*100:.2f}% "
          f"(주문 {params.probe_order_krw:,.0f}원 기준)")
    print("=" * 96)
    candidates = build_universe(UpbitClient(), cfg.cost, unit, params)
    if not candidates:
        print("자격 종목 없음. 오늘 이 타임프레임에서는 매매하지 않는 것이 맞다.")
        print("변동성이 비용을 못 넘기는 날에 억지로 매매하면 수수료만 지불한다.")
        return 0
    for cand in candidates:
        print("  " + cand.line())
    print("=" * 96)
    print("config 에 붙여넣을 형식:")
    print("engine:\n  markets:")
    for cand in candidates:
        print(f"    - {cand.market}")
    print("\n또는 engine.universe_auto: true 로 두면 엔진이 주기적으로 스스로 갱신한다.")
    return 0


# ------------------------------------------------------------------ optimize
def cmd_optimize(cfg: Config, args: argparse.Namespace) -> int:
    """워크포워드 재적합 + 승격 심사.

    승격은 기본적으로 '거부'다. 통과 조건을 전부 만족해야만 파라미터가 바뀐다.
    """
    unit = args.unit or cfg.engine.candle_unit
    store = ParamStore(cfg.engine.param_file)
    exit_code = 0

    for market in (args.markets or cfg.engine.markets):
        candles = _load_candles(market, unit, args.count)
        need = cfg.walk_forward.train_bars + cfg.walk_forward.test_bars
        print("\n" + "=" * 78)
        print(f"워크포워드 {market} {unit}분봉 {len(candles)}봉 "
              f"(학습 {cfg.walk_forward.train_bars} / 검증 {cfg.walk_forward.test_bars} / "
              f"이동 {cfg.walk_forward.step_bars})")
        print("=" * 78)
        if len(candles) < need:
            print(f"캔들 부족: {len(candles)}봉 < 필요 {need}봉. "
                  f"먼저 `fetch {market} --unit {unit} --count {need + 1000}` 를 실행하세요.")
            exit_code = 1
            continue

        report = walk_forward(
            market, candles, cost=cfg.cost, base_params=cfg.strategy,
            rparams=cfg.risk, wf=cfg.walk_forward, initial_krw=cfg.engine.initial_krw,
        )
        if report is None:
            print("평가 가능한 폴드 없음.")
            exit_code = 1
            continue

        print(f"\nOOS 종합 (학습에 쓰지 않은 구간만 집계)")
        print(f"  권고 파라미터   {report.params}")
        print(f"  OOS 수익률      {report.total_return*100:+.2f}%")
        print(f"  OOS 거래 수     {report.trades}건")
        print(f"  OOS 승률        {report.win_rate*100:.1f}%")
        print(f"  OOS MDD         {report.max_drawdown*100:.2f}%")
        print(f"  수익 폴드 비율   {report.positive_fold_ratio*100:.0f}% "
              f"({len(report.fold_returns)}개 폴드: "
              f"{', '.join(f'{r*100:+.1f}%' for r in report.fold_returns)})")

        champion = store.champion(market)
        ok, reasons = evaluate_promotion(report, champion, cfg.promotion)
        print(f"\n현재 챔피언: {store.describe(market)}")
        if ok:
            if args.apply:
                store.promote(market, report, note=f"{unit}m walk-forward")
                print(f"  → 승격 완료. {cfg.engine.param_file} 에 기록했습니다.")
                print("     엔진은 engine.adaptive_params: true 일 때만 이 값을 사용합니다.")
            else:
                print("  → 승격 조건 충족. 반영하려면 --apply 를 붙여 다시 실행하세요.")
        else:
            print("  → 승격 거부. 챔피언을 유지합니다.")
            for reason in reasons:
                print(f"     · {reason}")
    return exit_code


# -------------------------------------------------------------------- doctor
def cmd_doctor(cfg: Config, args: argparse.Namespace) -> int:
    print("=" * 74)
    print("점검 시작")
    print("=" * 74)
    public = UpbitClient()
    try:
        tickers = public.ticker(cfg.engine.markets)
        print(f"[OK] 시세 API 정상 — {len(tickers)}종목")
        for t in tickers:
            print(f"     {t['market']:<10} {t['trade_price']:>14,.2f}  전일대비 {t['signed_change_rate']*100:>+6.2f}%")
    except UpbitError as exc:
        print(f"[실패] 시세 API: {exc}")
        return 1

    print("\n호가 기반 실측 슬리피지 (주문 100만원 기준)")
    for market in cfg.engine.markets:
        try:
            book = public.orderbook([market])[0]
            buy_slip, sell_slip = estimate_slippage_from_orderbook(book, 1_000_000)
            tick = infer_tick_size(book)
            rt = cfg.cost.fee_buy + cfg.cost.fee_sell + buy_slip + sell_slip
            verdict = "양호" if rt < 0.003 else "주의 — 비용이 목표순익을 잠식"
            print(f"     {market:<10} 매수 {buy_slip*100:>6.4f}%  매도 {sell_slip*100:>6.4f}%  "
                  f"호가단위 {tick:>10,.4f}  왕복비용 {rt*100:.4f}%  {verdict}")
        except (UpbitError, IndexError) as exc:
            print(f"     {market:<10} 조회 실패: {exc}")

    if not (cfg.access_key and cfg.secret_key):
        print("\n[건너뜀] API 키 없음 → 계좌 점검 불가. UPBIT_ACCESS_KEY/UPBIT_SECRET_KEY 설정 후 재실행.")
        return 0

    private = UpbitClient(cfg.access_key, cfg.secret_key)
    try:
        accounts = private.accounts()
        print(f"\n[OK] 인증 성공 — 계좌 {len(accounts)}건")
        for acc in accounts:
            bal = float(acc["balance"])
            if bal <= 0:
                continue
            print(f"     {acc['currency']:<8} 잔고 {bal:>18,.8f}  평단 {acc.get('avg_buy_price','0')}")
    except UpbitError as exc:
        print(f"\n[실패] 인증: {exc}")
        print("      확인: 키 오타, 허용 IP 미등록, '자산조회' 권한 누락")
        return 1

    market = cfg.engine.markets[0]
    try:
        chance = private.orders_chance(market)
        bid_fee = float(chance["bid_fee"])
        ask_fee = float(chance["ask_fee"])
        min_total = float(chance["market"]["bid"]["min_total"])
        print(f"\n[OK] 주문 권한 확인 — {market}")
        print(f"     서버 실측 수수료: 매수 {bid_fee*100:.4f}%  매도 {ask_fee*100:.4f}%")
        print(f"     최소 주문금액: {min_total:,.0f}원")
        if abs(bid_fee - cfg.cost.fee_buy) > 1e-6 or abs(ask_fee - cfg.cost.fee_sell) > 1e-6:
            print(f"     [경고] config 의 수수료({cfg.cost.fee_buy*100:.4f}%/{cfg.cost.fee_sell*100:.4f}%)와 "
                  f"서버 값이 다릅니다. config 를 서버 값으로 맞추세요.")
        if abs(min_total - cfg.cost.min_order_krw) > 1e-6:
            print(f"     [경고] config 최소주문({cfg.cost.min_order_krw:,.0f})과 서버 값이 다릅니다.")
    except UpbitError as exc:
        print(f"\n[실패] 주문 권한: {exc}")
        print("      확인: '주문하기' 권한 누락 여부")
        return 1

    print("\n점검 통과. live 실행 전 paper 로 최소 2주 검증을 권장합니다.")
    return 0


# ---------------------------------------------------------------- paper/live
def cmd_paper(cfg: Config, args: argparse.Namespace) -> int:
    cfg.mode = "paper"
    TradingEngine(cfg).run()
    return 0


def cmd_live(cfg: Config, args: argparse.Namespace) -> int:
    if not args.yes_i_know:
        print("실거래는 --yes-i-know 플래그가 필요합니다.")
        print("실행 전 확인: doctor 통과 / paper 검증 / 출금권한 미부여 / 허용 IP 등록 / 소액 시작")
        return 2
    cfg.mode = "live"
    from .config import validate
    validate(cfg)
    print("!" * 74)
    print("실거래 모드로 시작합니다. 실제 자금이 움직입니다.")
    print(f"종목: {', '.join(cfg.engine.markets)}")
    print(f"1회 최대 주문: {cfg.risk.max_order_krw:,.0f}원 / 일 최대 {cfg.risk.daily_trade_limit}회")
    print(f"일일 손실한도: {cfg.risk.daily_loss_limit*100:.1f}%")
    print(f"긴급 정지: 작업 디렉터리에 {cfg.engine.kill_switch_file} 파일 생성")
    print("!" * 74)
    TradingEngine(cfg).run()
    return 0


# ----------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ubbit", description="업비트 수수료 인지형 자동매매")
    parser.add_argument("-c", "--config", default="config.yaml", help="설정 파일 경로")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("costs", help="비용 구조 분석").set_defaults(func=cmd_costs)

    p_fetch = sub.add_parser("fetch", help="캔들 수집")
    p_fetch.add_argument("market")
    p_fetch.add_argument("--unit", type=int, default=None)
    p_fetch.add_argument("--count", type=int, default=2000)
    p_fetch.set_defaults(func=cmd_fetch)

    p_bt = sub.add_parser("backtest", help="백테스트 + 수수료 민감도")
    p_bt.add_argument("market")
    p_bt.add_argument("--unit", type=int, default=None)
    p_bt.add_argument("--count", type=int, default=None)
    p_bt.add_argument("--trades", action="store_true", help="개별 체결 내역 출력")
    p_bt.set_defaults(func=cmd_backtest)

    p_scan = sub.add_parser("scan", help="종목×타임프레임 구조 적합성 스캔")
    p_scan.add_argument("--markets", nargs="*", default=None)
    p_scan.add_argument("--units", nargs="*", type=int, default=None)
    p_scan.add_argument("--count", type=int, default=200)
    p_scan.set_defaults(func=cmd_scan)

    p_uni = sub.add_parser("universe", help="오늘 매매 자격 종목 선별")
    p_uni.add_argument("--unit", type=int, default=None)
    p_uni.add_argument("--top", type=int, default=None)
    p_uni.set_defaults(func=cmd_universe)

    p_opt = sub.add_parser("optimize", help="워크포워드 재적합 + 승격 심사")
    p_opt.add_argument("--markets", nargs="*", default=None)
    p_opt.add_argument("--unit", type=int, default=None)
    p_opt.add_argument("--count", type=int, default=None)
    p_opt.add_argument("--apply", action="store_true", help="승격 조건 충족 시 실제로 반영")
    p_opt.set_defaults(func=cmd_optimize)

    sub.add_parser("doctor", help="키/권한/슬리피지 점검").set_defaults(func=cmd_doctor)
    sub.add_parser("paper", help="가상매매 실행").set_defaults(func=cmd_paper)

    p_live = sub.add_parser("live", help="실거래 실행")
    p_live.add_argument("--yes-i-know", action="store_true", help="실자금 투입 동의")
    p_live.set_defaults(func=cmd_live)

    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg.engine.log_level)
    return args.func(cfg, args)


if __name__ == "__main__":
    sys.exit(main())

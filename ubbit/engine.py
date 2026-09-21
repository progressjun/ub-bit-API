"""메인 트레이딩 루프.

루프 1회 = [잔고 동기화] → [종목별 캔들 수집] → [보유 포지션 청산 판단]
        → [신규 진입 판단] → [리스크 게이트] → [주문] → [상태 저장]

청산을 진입보다 먼저 처리한다. 현금이 부족해 손절을 못 내는 상황이
자동매매에서 가장 흔한 사고 유형이기 때문이다.
"""
from __future__ import annotations

import os
import signal
import time
from dataclasses import replace
from typing import Callable
from datetime import datetime

from .adapt import ParamStore
from .broker import Broker, Fill, LiveBroker, PaperBroker
from .config import Config
from .fees import CostModel
from .logutil import get_logger
from .risk import RiskManager
from .session import KST
from .session import evaluate as evaluate_session
from .state import Position, Store
from .strategy import Candle, FeeAwareTrendStrategy, PositionView, Signal, pct
from .universe import build_universe
from .upbit import UpbitClient, UpbitError

log = get_logger("engine")


def w(value: float, decimals: int = 0) -> str:
    """로그용 천 단위 구분 포맷. %-포맷은 콤마 플래그를 지원하지 않는다."""
    return f"{value:,.{decimals}f}"


class TradingEngine:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.client = UpbitClient(cfg.access_key or None, cfg.secret_key or None)
        self.cost = cfg.cost
        self.strategy = FeeAwareTrendStrategy(cfg.strategy, self.cost)
        self.risk = RiskManager(cfg.risk, self.cost)
        self.store = Store(cfg.engine.db_path)
        self.broker: Broker = (
            LiveBroker(self.client, self.cost)
            if cfg.is_live
            else PaperBroker(self.cost, cfg.engine.initial_krw)
        )
        self._stop = False
        self._last_bar_ts: dict[str, str] = {}
        self.params_store = ParamStore(cfg.engine.param_file) if cfg.engine.adaptive_params else None
        # 주입 가능한 시계. 실거래는 실제 시각, replay 는 캔들 타임스탬프를 쓴다.
        # 이것이 없으면 재생 중 1200봉이 전부 '같은 날'로 집계돼 일일 한도에 걸린다.
        self.clock: Callable[[], datetime] = datetime.now
        self.markets: list[str] = list(cfg.engine.markets)
        self._universe_refreshed_at: float = 0.0
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def strategy_for(self, market: str) -> FeeAwareTrendStrategy:
        """종목별 전략 인스턴스.

        adaptive_params 가 켜져 있으면 승격된 파라미터를 덮어쓴다.
        덮어쓰기는 화이트리스트 축에만 적용되므로, 최적화기가 안전 하한
        (min_net_target 등)이나 리스크 게이트를 건드릴 수 없다.
        """
        if not self.params_store:
            return self.strategy
        params = self.params_store.params_for(market, self.cfg.strategy)
        if params == self.cfg.strategy:
            return self.strategy
        return FeeAwareTrendStrategy(params, self.cost)

    def refresh_universe(self) -> None:
        """매매 종목 자동 갱신. 보유 중인 종목은 청산 전까지 목록에서 빼지 않는다."""
        if not self.cfg.engine.universe_auto:
            return
        now = time.monotonic()
        if now - self._universe_refreshed_at < self.cfg.engine.universe_refresh_minutes * 60:
            return
        self._universe_refreshed_at = now
        candidates = build_universe(
            self.client, self.cost, self.cfg.engine.candle_unit, self.cfg.universe
        )
        if not candidates:
            log.warning("유니버스 선별 결과 0종목 — 기존 목록을 유지합니다: %s", self.markets)
            return
        held = [p.market for p in self.store.all_positions()]
        selected = [c.market for c in candidates]
        self.markets = list(dict.fromkeys(selected + held))
        log.info("유니버스 갱신 (%d종목)", len(selected))
        for cand in candidates:
            log.info("   %s", cand.line())
        dropped = [m for m in held if m not in selected]
        if dropped:
            log.info("   보유 중이라 유지: %s", ", ".join(dropped))

    def _session_now(self) -> datetime | None:
        """세션 판정용 시각. 기본 시계를 쓰되 tz 정보가 없으면 KST 로 간주한다."""
        now = self.clock()
        if now is datetime.now:      # pragma: no cover - 방어
            return None
        return now if now.tzinfo else now.replace(tzinfo=KST)

    def _handle_signal(self, *_args) -> None:
        log.warning("종료 신호 수신. 현재 루프 종료 후 정지합니다. (포지션은 청산하지 않음)")
        self._stop = True

    # ------------------------------------------------------------------ 진입점
    def run(self) -> None:
        log.info("=" * 78)
        log.info("모드=%s  종목=%s  캔들=%s분  주기=%ss",
                 self.broker.mode,
                 "자동선별" if self.cfg.engine.universe_auto else ",".join(self.markets),
                 self.cfg.engine.candle_unit, self.cfg.engine.poll_seconds)
        sess = self.cfg.session
        log.info("세션: %s | 종료 %d분 전 진입중단 | 장마감 전량청산=%s",
                 f"{sess.start}~{sess.end} KST ({''.join('월화수목금토일'[d] for d in sess.days)})"
                 if sess.enabled else "24시간",
                 sess.no_entry_before_close_minutes, sess.flatten_at_close)
        log.info("파라미터: %s", "적응형(data/params.json)" if self.params_store else "고정(config)")
        log.info("비용모델: %s", self.cost.describe())
        log.info("진입 변동성 하한: ATR%% >= %s (손익분기 × %.1f)",
                 pct(self.cost.breakeven_edge * self.cfg.strategy.atr_cost_multiple),
                 self.cfg.strategy.atr_cost_multiple)
        log.info("리스크: 1회 %.2f%% / 일손실한도 %.1f%% / 일매매 %d회 / 동시 %d종목",
                 self.cfg.risk.risk_per_trade * 100, self.cfg.risk.daily_loss_limit * 100,
                 self.cfg.risk.daily_trade_limit, self.cfg.risk.max_concurrent)
        log.info("=" * 78)

        while not self._stop:
            started = time.monotonic()
            try:
                self.tick()
            except UpbitError as exc:
                log.error("업비트 API 오류: %s", exc)
            except Exception:  # noqa: BLE001 - 루프는 어떤 예외에도 살아남아야 한다
                log.exception("루프 예외 발생. 다음 주기에 재시도합니다.")
            elapsed = time.monotonic() - started
            time.sleep(max(self.cfg.engine.poll_seconds - elapsed, 1.0))

        log.info("정지 완료. 보유 포지션 %d건은 유지됩니다.", len(self.store.all_positions()))
        self.store.close()

    # -------------------------------------------------------------------- tick
    def tick(self) -> None:
        killed = os.path.exists(self.cfg.engine.kill_switch_file)
        session = evaluate_session(self.cfg.session, self._session_now())

        self.refresh_universe()

        candles_by_market: dict[str, list[Candle]] = {}
        prices: dict[str, float] = {}
        held_markets = [p.market for p in self.store.all_positions()]
        for market in dict.fromkeys(self.markets + held_markets):
            rows = self.client.candles(
                market, unit=self.cfg.engine.candle_unit, count=self.cfg.engine.candle_count
            )
            if not rows:
                continue
            candles = [Candle.from_upbit(r) for r in rows]
            candles_by_market[market] = candles
            prices[market] = candles[-1].close

        now = self.clock()
        total, cash, exposure = self.broker.equity(prices)
        self.risk.roll_day(total, now)
        self.store.record_equity(total, cash, exposure, now)

        # 1) 장 마감 강제 청산 — 손익과 무관하게 실행한다.
        #    포지션을 들고 자면 손절폭이 갭으로 건너뛰어 설계된 손실 한도가 무너진다.
        if session.must_flatten:
            self._flatten_all(candles_by_market, session.reason)
            self._log_status(total, cash, exposure, session.reason)
            return

        # 2) 청산 우선 — 현금이 없어 손절을 못 내는 상황을 구조적으로 배제한다.
        for position in self.store.all_positions():
            candles = candles_by_market.get(position.market)
            if not candles:
                log.warning("[%s] 캔들 조회 실패 — 이번 주기 청산 판단 보류", position.market)
                continue
            self._manage_position(position, candles)

        if killed:
            log.warning("킬스위치(%s) 감지 → 신규 진입 차단. 청산 로직만 동작합니다.",
                        self.cfg.engine.kill_switch_file)
            self._log_status(total, cash, exposure, "킬스위치")
            return

        if not session.can_enter:
            self._log_status(total, cash, exposure, session.reason)
            return

        # 3) 신규 진입
        open_positions = len(self.store.all_positions())
        for market in self.markets:
            candles = candles_by_market.get(market)
            if not candles or self.store.get_position(market):
                continue
            strategy = self.strategy_for(market)
            snaps = strategy.compute(candles)
            sig = strategy.entry_signal(candles, snaps)
            if sig.action != "buy":
                log.debug("[%s] %s", market, sig.reason)
                continue
            if candles[-1].value < self.cfg.engine.liquidity_min_value_krw:
                log.info("[%s] 진입 보류 - 거래대금 부족 %s원", market, w(candles[-1].value))
                continue

            ok, why = self.risk.can_open(market, total, open_positions, exposure, now)
            if not ok:
                log.info("[%s] 진입 차단 - %s", market, why)
                continue

            if self._open_position(market, sig, cash, total, exposure):
                open_positions += 1
                total, cash, exposure = self.broker.equity(prices)

        self._log_status(total, cash, exposure, session.reason)

    def _flatten_all(self, candles_by_market: dict[str, list[Candle]], reason: str) -> None:
        positions = self.store.all_positions()
        if not positions:
            return
        log.warning("전량 청산 시작 (%s) — %d종목", reason, len(positions))
        for position in positions:
            candles = candles_by_market.get(position.market)
            if not candles:
                try:
                    rows = self.client.ticker([position.market])
                    price = float(rows[0]["trade_price"])
                except (UpbitError, IndexError, KeyError) as exc:
                    log.error("[%s] 청산가 조회 실패 — 다음 주기 재시도: %s", position.market, exc)
                    continue
            else:
                price = candles[-1].close
            self._close_position(position, price, "session_close", f"장마감 청산 ({reason})")

    # -------------------------------------------------------------- 포지션 진입
    def _open_position(self, market: str, sig: Signal, cash: float, equity: float, exposure: float) -> bool:
        entry_price = sig.snapshot.close
        stop_price = sig.meta["stop_price"]
        size_krw, why = self.risk.position_size_krw(equity, cash, entry_price, stop_price, exposure)
        if size_krw <= 0:
            log.info("[%s] 진입 취소 - %s", market, why)
            return False

        cost = self._effective_cost(market, size_krw)
        # 실측 슬리피지로 비용이 커지면 진입 근거가 무너질 수 있으므로 재검증한다.
        if cost.breakeven_edge >= sig.meta["target_net"]:
            log.warning("[%s] 진입 취소 - 실측 비용 %s 가 목표순익 %s 를 잠식",
                        market, pct(cost.breakeven_edge), pct(sig.meta["target_net"]))
            return False

        log.info("[%s] 매수 시도 %s원 | %s | %s", market, w(size_krw), sig.reason, why)
        fill = self.broker.buy(market, size_krw, entry_price)
        self.store.record_order(market, "bid", self.broker.mode,
                                {"krw": size_krw, "ref_price": entry_price}, fill.raw or fill.error)
        if not fill.ok:
            log.error("[%s] 매수 실패: %s", market, fill.error)
            return False

        position = Position(
            market=market,
            entry_price=fill.price,
            qty=fill.qty,
            entry_krw=fill.krw,
            stop_price=fill.price - (sig.snapshot.atr or 0.0) * self.cfg.strategy.stop_atr,
            target_net=sig.meta["target_net"],
            peak_price=fill.price,
            bars_held=0,
            opened_at=self.clock().isoformat(timespec="seconds"),
            meta={"entry_reason": sig.reason, "entry_regime": sig.snapshot.regime},
        )
        self.store.upsert_position(position)
        self.risk.record_entry()
        log.info("[%s] 매수 체결 단가=%s 수량=%.8f 투입=%s 수수료=%s 손절=%s 목표순익=%s",
                 market, w(fill.price, 2), fill.qty, w(fill.krw), w(fill.fee),
                 w(position.stop_price, 2), pct(position.target_net))
        return True

    # -------------------------------------------------------------- 포지션 관리
    def _manage_position(self, position: Position, candles: list[Candle]) -> None:
        market = position.market
        strategy = self.strategy_for(market)
        snaps = strategy.compute(candles)
        last_ts = candles[-1].ts

        # 봉이 바뀐 경우에만 보유 봉수를 증가시킨다(같은 봉 내 중복 카운트 방지).
        if self._last_bar_ts.get(market) != last_ts:
            self._last_bar_ts[market] = last_ts
            position.bars_held += 1

        price = candles[-1].close
        position.peak_price = max(position.peak_price, price)
        if not position.armed and self.cost.net_return(position.entry_price, price) > 0:
            position.armed = True   # 손익분기 돌파 후에만 트레일링 작동

        view = PositionView(
            market=market, entry_price=position.entry_price, qty=position.qty,
            stop_price=position.stop_price, target_net=position.target_net,
            peak_price=position.peak_price, bars_held=position.bars_held, armed=position.armed,
        )
        sig = strategy.exit_signal(view, snaps)
        if sig.action != "sell":
            self.store.upsert_position(position)
            log.debug("[%s] %s", market, sig.reason)
            return

        self._close_position(position, price, sig.meta.get("exit_kind", "unknown"), sig.reason)

    def _close_position(self, position: Position, price: float, exit_kind: str, reason: str) -> None:
        """청산 실행과 기록. 매도 실패 시 포지션을 지우지 않고 다음 주기에 재시도한다.

        포지션을 먼저 지우고 주문을 내면, 주문이 실패했을 때 봇은 보유 사실을
        잊는다. 그 코인은 손절 없이 계좌에 남는다.
        """
        market = position.market
        log.info("[%s] 매도 시도 - %s", market, reason)
        fill = self.broker.sell(market, position.qty, price)
        self.store.record_order(market, "ask", self.broker.mode,
                                {"qty": position.qty, "ref_price": price}, fill.raw or fill.error)
        if not fill.ok:
            log.error("[%s] 매도 실패: %s (포지션 유지, 다음 주기 재시도)", market, fill.error)
            self.store.upsert_position(position)
            return

        net_pnl = fill.krw - position.entry_krw
        net_ret = net_pnl / position.entry_krw if position.entry_krw else 0.0
        entry_fee = position.entry_krw * self.cost.fee_buy / (1 + self.cost.fee_buy)
        self.store.record_trade(
            market=market, opened_at=position.opened_at,
            closed_at=self.clock().isoformat(timespec="seconds"),
            entry_price=position.entry_price, exit_price=fill.price, qty=fill.qty,
            entry_krw=position.entry_krw, exit_krw=fill.krw,
            fee_krw=entry_fee + fill.fee, net_pnl=net_pnl, net_return=net_ret,
            exit_kind=exit_kind, reason=reason,
        )
        self.store.delete_position(market)
        self.risk.record_exit(market, net_pnl, self.clock())
        log.info("[%s] 매도 체결 단가=%s 회수=%s 순손익=%s (%s) 수수료합=%s",
                 market, w(fill.price, 2), w(fill.krw), w(net_pnl), pct(net_ret),
                 w(entry_fee + fill.fee))

    # ------------------------------------------------------------------ 보조
    def _effective_cost(self, market: str, krw_amount: float) -> CostModel:
        if not self.cfg.engine.dynamic_slippage:
            return self.cost
        if isinstance(self.broker, LiveBroker):
            return self.broker.refresh_cost(market, krw_amount)
        try:
            from .fees import estimate_slippage_from_orderbook
            books = self.client.orderbook([market])
            if not books:
                return self.cost
            buy_slip, sell_slip = estimate_slippage_from_orderbook(books[0], krw_amount)
            if float("inf") in (buy_slip, sell_slip):
                return self.cost
            return replace(self.cost, slippage_buy=buy_slip, slippage_sell=sell_slip)
        except UpbitError:
            return self.cost

    def _log_status(self, total: float, cash: float, exposure: float, session: str = "") -> None:
        perf = self.store.performance()
        rs = self.risk.snapshot()
        held = ", ".join(p.market for p in self.store.all_positions()) or "-"
        drag = self.cost.drag_per_day(rs["trades_today"]) * 100
        log.info(
            "자산=%s 현금=%s 투입=%s | 보유=[%s] | 당일 %.2f%% %d/%d회(비용 -%.2f%%) | "
            "누적 %d건 순손익=%s 수수료=%s 승률=%.1f%% | %s",
            w(total), w(cash), w(exposure), held, rs["daily_dd_pct"], rs["trades_today"],
            self.cfg.risk.daily_trade_limit, drag, perf["trades"], w(perf["net_pnl"]),
            w(perf["fees_paid"]), perf["win_rate"] * 100, session,
        )

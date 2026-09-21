"""로컬 웹 대시보드.

봇이 도는 동안 브라우저에서 상태를 본다. 표준 라이브러리 http.server 만 쓰고
외부 CDN 을 로드하지 않는다. 오프라인·사내망에서도 그대로 뜬다.

기본은 127.0.0.1 바인딩이다. 이 페이지는 잔고와 포지션을 노출하므로
외부에 열면 안 된다. --host 로 바꿀 수 있게 두되 경고를 띄운다.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .config import Config
from .logutil import get_logger
from .state import Store
from .upbit import UpbitClient, UpbitError

log = get_logger("dashboard")

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


class PriceCache:
    """현재가 캐시. 브라우저가 3초마다 폴링해도 업비트 rate limit 을 건드리지 않게."""

    def __init__(self, ttl: float = 5.0) -> None:
        self.ttl = ttl
        self._data: dict[str, float] = {}
        self._at = 0.0
        self._lock = threading.Lock()
        self.client = UpbitClient()

    def get(self, markets: list[str]) -> dict[str, float]:
        if not markets:
            return {}
        with self._lock:
            if time.monotonic() - self._at < self.ttl and set(markets) <= set(self._data):
                return dict(self._data)
            try:
                rows = self.client.ticker(markets)
                self._data = {r["market"]: float(r["trade_price"]) for r in rows}
                self._at = time.monotonic()
            except UpbitError as exc:
                log.warning("현재가 조회 실패: %s", exc)
            return dict(self._data)


def build_state(cfg: Config, db_path: str, prices: PriceCache) -> dict:
    """대시보드가 그릴 모든 데이터를 한 번에 만든다."""
    if not os.path.exists(db_path):
        return {"ready": False, "db_path": db_path,
                "message": "아직 실행 기록이 없습니다. paper 또는 replay 를 먼저 실행하세요."}

    store = Store(db_path)
    try:
        positions = store.all_positions()
        live = prices.get([p.market for p in positions])
        cost = cfg.cost

        pos_rows = []
        for p in positions:
            price = live.get(p.market, p.entry_price)
            net = cost.net_return(p.entry_price, price)
            stop_gap = (price - p.stop_price) / price if price else 0.0
            pos_rows.append({
                "market": p.market, "entry_price": p.entry_price, "price": price,
                "qty": p.qty, "entry_krw": p.entry_krw, "net_return": net,
                "net_pnl": p.entry_krw * net, "stop_price": p.stop_price,
                "stop_gap": stop_gap, "target_net": p.target_net,
                "peak_price": p.peak_price, "bars_held": p.bars_held,
                "armed": p.armed, "opened_at": p.opened_at,
                "reason": (p.meta or {}).get("entry_reason", ""),
            })

        curve = [dict(r) for r in store.conn.execute(
            "SELECT ts, total_krw, cash_krw, exposure_krw FROM equity ORDER BY ts").fetchall()]
        trades = store.recent_trades(200)
        perf = store.performance()

        # 당일 집계 — 회전수의 확정 비용을 함께 보여준다
        today = str(date.today())
        today_trades = [t for t in trades if t["closed_at"].startswith(today)]
        today_pnl = sum(t["net_pnl"] for t in today_trades)

        exit_mix: dict[str, int] = {}
        for t in trades:
            exit_mix[t["exit_kind"]] = exit_mix.get(t["exit_kind"], 0) + 1

        latest = curve[-1] if curve else None
        first = curve[0] if curve else None
        equity = latest["total_krw"] if latest else cfg.engine.initial_krw
        peak = max((c["total_krw"] for c in curve), default=equity)

        fresh = None
        if latest:
            try:
                fresh = (datetime.now() - datetime.fromisoformat(latest["ts"])).total_seconds()
            except ValueError:
                fresh = None

        return {
            "ready": True,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "mode": cfg.mode,
            "db_path": db_path,
            "kill": os.path.exists(cfg.engine.kill_switch_file),
            "kill_file": cfg.engine.kill_switch_file,
            "engine": {
                "candle_unit": cfg.engine.candle_unit,
                "universe_auto": cfg.engine.universe_auto,
                "markets": cfg.engine.markets,
                "entry_mode": cfg.strategy.entry_mode,
                "poll_seconds": cfg.engine.poll_seconds,
                "seconds_since_update": fresh,
            },
            "cost": {
                "fee_buy": cost.fee_buy, "fee_sell": cost.fee_sell,
                "slippage_buy": cost.slippage_buy, "slippage_sell": cost.slippage_sell,
                "breakeven": cost.breakeven_edge, "fee_only": cost.fee_only_edge,
                "atr_floor": cost.breakeven_edge * cfg.strategy.atr_cost_multiple,
                "min_net_target": cfg.strategy.min_net_target,
                "drag_at_limit": cost.drag_per_day(cfg.risk.daily_trade_limit),
            },
            "risk": {
                "daily_loss_limit": cfg.risk.daily_loss_limit,
                "daily_trade_limit": cfg.risk.daily_trade_limit,
                "max_concurrent": cfg.risk.max_concurrent,
                "risk_per_trade": cfg.risk.risk_per_trade,
                "max_order_krw": cfg.risk.max_order_krw,
            },
            "equity": {
                "total": equity,
                "cash": latest["cash_krw"] if latest else cfg.engine.initial_krw,
                "exposure": latest["exposure_krw"] if latest else 0.0,
                "initial": first["total_krw"] if first else cfg.engine.initial_krw,
                "peak": peak,
                "drawdown": (equity / peak - 1.0) if peak else 0.0,
            },
            "today": {"trades": len(today_trades), "pnl": today_pnl,
                      "drag": cost.drag_per_day(len(today_trades))},
            "performance": perf,
            "exit_mix": exit_mix,
            "positions": pos_rows,
            "curve": curve[-600:],
            "trades": trades[:60],
        }
    finally:
        store.close()


class Handler(BaseHTTPRequestHandler):
    cfg: Config
    db_path: str
    prices: PriceCache
    engine = None            # `ubbit run` 일 때만 채워진다

    def log_message(self, fmt, *args):     # 요청 로그로 콘솔을 덮지 않는다
        return

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(WEB_DIR, "dashboard.html"), "rb") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            except OSError as exc:
                self._send(500, str(exc).encode(), "text/plain; charset=utf-8")
            return
        if path == "/api/live":
            engine = getattr(Handler, "engine", None)
            payload = {
                "attached": engine is not None,
                "paused": bool(engine and (engine.paused
                                           or os.path.exists(self.cfg.engine.kill_switch_file))),
                "markets": dict(engine.live) if engine else {},
                "watching": list(engine.markets) if engine else [],
            }
            self._send(200, json.dumps(payload, ensure_ascii=False, default=str).encode(),
                       "application/json; charset=utf-8")
            return
        if path == "/api/state":
            try:
                payload = build_state(self.cfg, self.db_path, self.prices)
            except Exception as exc:                      # noqa: BLE001
                log.exception("상태 생성 실패")
                payload = {"ready": False, "message": f"상태 생성 실패: {exc}"}
            self._send(200, json.dumps(payload, ensure_ascii=False, default=str).encode(),
                       "application/json; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/kill":
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            body = {}
        target = bool(body.get("engage"))
        engine = getattr(Handler, "engine", None)
        if engine is not None:
            engine.paused = target
        path = self.cfg.engine.kill_switch_file
        try:
            if target:
                open(path, "w").close()
                log.warning("대시보드에서 킬스위치 작동 → %s", path)
            elif os.path.exists(path):
                os.remove(path)
                log.warning("대시보드에서 킬스위치 해제 → %s", path)
        except OSError as exc:
            self._send(500, json.dumps({"error": str(exc)}).encode(),
                       "application/json; charset=utf-8")
            return
        self._send(200, json.dumps({"kill": os.path.exists(path)}).encode(),
                   "application/json; charset=utf-8")


def serve(cfg: Config, db_path: str, host: str, port: int, open_browser: bool = True) -> None:
    Handler.cfg = cfg
    Handler.db_path = db_path
    Handler.prices = PriceCache()
    serve_forever(cfg, host, port, open_browser)


def _bind(host: str, port: int, tries: int = 20) -> ThreadingHTTPServer:
    """포트가 이미 쓰이고 있으면 다음 포트를 찾는다.

    '포트 사용 중'으로 조용히 죽으면 사용자는 창이 안 열리는 이유를 알 수 없다.
    """
    last: OSError | None = None
    for offset in range(tries):
        try:
            return ThreadingHTTPServer((host, port + offset), Handler)
        except OSError as exc:
            last = exc
            if exc.errno not in (48, 98):      # EADDRINUSE (mac/linux)
                raise
            log.warning("포트 %d 사용 중 — %d 로 재시도", port + offset, port + offset + 1)
    raise OSError(f"{port}~{port + tries - 1} 범위에 빈 포트가 없습니다: {last}")


def _can_open_browser() -> bool:
    """GUI 가 없는 환경(서버, 일부 WSL, 컨테이너)에서는 브라우저를 띄울 수 없다."""
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        if "microsoft" in os.uname().release.lower():
            return True          # WSL 은 wslview 로 윈도우 브라우저가 열린다
        return False
    return True


def serve_forever(cfg: Config, host: str, port: int, open_browser: bool = True) -> None:
    server = _bind(host, port)
    port = server.server_address[1]
    url = f"http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}"
    print()
    print("\u250c" + "\u2500" * 60 + "\u2510")
    print("\u2502" + f"  브라우저에서 열어주세요".ljust(59) + "\u2502")
    print("\u2502" + f"     {url}".ljust(59) + "\u2502")
    print("\u2514" + "\u2500" * 60 + "\u2518")
    print(f"  DB {Handler.db_path}   모드 {cfg.mode}   종료 Ctrl+C")
    if host not in ("127.0.0.1", "localhost"):
        print("  [경고] 로컬 외부에 열려 있습니다. 잔고와 포지션이 노출됩니다.")
    print()

    if open_browser:
        if _can_open_browser():
            def _open():
                try:
                    if not webbrowser.open(url):
                        print(f"  [안내] 브라우저 자동 실행 실패 — 위 주소를 직접 여세요.")
                except Exception:                 # noqa: BLE001
                    print(f"  [안내] 브라우저 자동 실행 실패 — 위 주소를 직접 여세요.")
            threading.Timer(0.8, _open).start()
        else:
            print("  [안내] GUI 가 없는 환경입니다. 위 주소를 직접 여세요.")
            print("         원격 서버라면: ssh -L {0}:127.0.0.1:{0} <사용자>@<서버>".format(port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n대시보드 종료")
    finally:
        server.server_close()

"""Authenticated engine service for the private Sites console.

Run behind HTTPS on a host whose outbound IP is allowlisted at Upbit.
No endpoint accepts API keys, places ad-hoc orders, or withdraws assets.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import load_config
from .dashboard import PriceCache, build_state
from .engine import TradingEngine
from .logutil import setup_logging, get_logger
from .state import Store
from .upbit import UpbitClient

log = get_logger("remote")


class InstanceLock:
    """OS lock released on process exit; prevents two bots sharing a ledger."""
    def __init__(self, path):
        self.file = open(path, "a+b")
        try:
            if os.fstat(self.file.fileno()).st_size == 0:
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("이미 같은 장부를 사용하는 엔진이 실행 중입니다.") from None

    def close(self):
        self.file.close()


class EngineService:
    def __init__(self, cfg, token):
        if len(token) < 32:
            raise ValueError("UBBIT_BRIDGE_TOKEN은 32자 이상의 임의 문자열이어야 합니다.")
        self.cfg, self.token = cfg, token
        Path(cfg.engine.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.instance = InstanceLock(cfg.engine.db_path + ".lock")
        self.engine = TradingEngine(cfg)
        self.engine.paused = True
        Path(cfg.engine.kill_switch_file).parent.mkdir(parents=True, exist_ok=True)
        Path(cfg.engine.kill_switch_file).touch()
        self.stop_event = threading.Event()
        self.command_lock = threading.Lock()
        self.thread = None
        self.last_tick = None
        self.last_tick_at = 0.0
        self.snapshot = None
        self.checks = None
        self.check_at = 0.0
        self.error = ""
        self.prices = PriceCache()

    def check(self):
        if not self.cfg.is_live:
            result = {"ok": True, "message": "모의매매 엔진이 연결되었습니다. 실제 주문은 발생하지 않습니다."}
        else:
            try:
                # Separate client: never race the engine's requests.Session.
                client = UpbitClient(self.cfg.access_key, self.cfg.secret_key, timeout=5, max_retries=1)
                accounts = client.accounts()
                market = self.cfg.engine.markets[0] if self.cfg.engine.markets else "KRW-BTC"
                chance = client.orders_chance(market)
                if not isinstance(accounts, list) or not isinstance(chance, dict) or "bid_fee" not in chance:
                    raise ValueError("unexpected response")
                if abs(float(chance["bid_fee"]) - self.cfg.cost.fee_buy)>1e-9 or abs(float(chance["ask_fee"]) - self.cfg.cost.fee_sell)>1e-9:
                    result = {"ok": False, "message": "실제 수수료와 엔진 설정이 다릅니다. 실행 서버의 비용 설정을 수정해 주세요."}
                else:
                    result = {"ok": True, "message": "자산·주문 조회와 수수료 확인을 통과했습니다. 주문하기 권한과 실제 체결은 아직 검증되지 않았습니다."}
            except Exception:
                result = {"ok": False, "message": "업비트 키·허용 IP·자산조회 및 주문조회 권한을 확인해 주세요."}
        self.checks, self.check_at = result, time.time()
        return result

    def start(self):
        if self.cfg.is_live and not self.check()["ok"]:
            self.error = "실거래 연결 점검 실패"
            return
        if not self.engine.startup_checks():
            self.error = self.engine._halted_reason or "잔고 대조 실패"
            return
        self.thread = threading.Thread(target=self._loop, name="ubbit-engine", daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.stop_event.is_set() and not self.engine._stop:
            started = time.monotonic()
            try:
                self.engine.tick()
                if self.engine._stop:
                    self.error = self.engine._halted_reason or "엔진 정지"
                    break
                self.snapshot = build_state(self.cfg, self.cfg.engine.db_path, self.prices)
                self.last_tick = datetime.now(timezone.utc).isoformat()
                self.last_tick_at = time.time()
                self.error = ""
            except Exception:
                self.error = "평가 실패. 업비트 연결과 실행 서버 로그를 확인해 주세요."
                log.exception("Engine evaluation failed")
            self.stop_event.wait(max(1,self.cfg.engine.poll_seconds-(time.monotonic()-started)))

    def status(self):
        running = bool(self.thread and self.thread.is_alive() and not self.engine._stop)
        stale = not self.last_tick_at or time.time()-self.last_tick_at > max(90,self.cfg.engine.poll_seconds*3)
        state = dict(self.snapshot) if self.snapshot else {"ready": False,"mode": self.cfg.mode}
        state["kill"] = self.engine.paused or os.path.exists(self.cfg.engine.kill_switch_file)
        return {"connected": True,"running": running,"stale": stale,"last_tick": self.last_tick,
                "halted_reason": self.error or self.engine._halted_reason,"keys_configured": bool(self.cfg.access_key and self.cfg.secret_key),
                "checks": self.checks,"state": state,
                "live": {"watching": list(self.engine.markets),"markets": dict(self.engine.live)}}

    def control(self, body, actor):
        action, ident = body.get("action"), body.get("requestId", "")
        if action not in ("pause", "resume") or not re.fullmatch(r"[a-zA-Z0-9-]{16,80}", ident):
            return 400, {"error": "잘못된 제어 요청"}
        with self.command_lock:
            store = Store(self.cfg.engine.db_path)
            try:
                previous = store.get_runtime("control:" + ident)
                if previous:
                    if previous["action"] != action or previous["actor"] != actor:
                        return 409, {"error": "요청 식별자 충돌"}
                    # Duplicate is a readback; never replay an older control.
                    return 200, {"ok": True,"duplicate": True,"paused": self.engine.paused}
                if action == "resume":
                    status = self.status()
                    if not status["running"] or status["stale"] or store.pending_intents():
                        return 409, {"error": "엔진 상태 확인 필요"}
                    if self.cfg.is_live:
                        if body.get("confirm") != "실거래 시작":
                            return 409, {"error": "실거래 시작 확인 필요"}
                        if time.time()-self.check_at>300 or not self.checks or not self.checks["ok"]:
                            if not self.check()["ok"]:
                                return 409, {"error": "업비트 연결 점검 실패"}
                    Path(self.cfg.engine.kill_switch_file).unlink(missing_ok=True)
                    self.engine.paused = False
                else:
                    self.engine.paused = True
                    Path(self.cfg.engine.kill_switch_file).touch()
                result = {"action": action,"actor": actor,"at": datetime.now(timezone.utc).isoformat()}
                store.set_runtime("control:" + ident, result)
                return 200, {"ok": True,"paused": self.engine.paused}
            finally:
                store.close()

    def close(self):
        self.stop_event.set()
        self.engine._stop = True
        if self.thread:
            self.thread.join(timeout=45)
        if not self.thread or not self.thread.is_alive():
            self.engine.store.close()
            self.instance.close()


class RemoteHandler(BaseHTTPRequestHandler):
    service: EngineService
    def log_message(self, *_):
        pass
    def send_json(self, status, data):
        encoded=json.dumps(data,ensure_ascii=False,allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length",str(len(encoded)))
        self.send_header("Cache-Control","no-store")
        self.end_headers()
        self.wfile.write(encoded)
    def authorized(self):
        token=self.headers.get("Authorization","")
        return hmac.compare_digest(token.encode(),("Bearer " + self.service.token).encode())
    def do_GET(self):
        if not self.authorized():
            return self.send_json(401,{"error":"unauthorized"})
        if self.path != "/v1/snapshot":
            return self.send_json(404,{"error":"not found"})
        return self.send_json(200,self.service.status())
    def do_POST(self):
        if not self.authorized():
            return self.send_json(401,{"error":"unauthorized"})
        try:
            length=int(self.headers.get("Content-Length","0"))
            if not 0 < length <= 2048 or not self.headers.get("Content-Type","").startswith("application/json"):
                return self.send_json(400,{"error":"invalid request"})
            body=json.loads(self.rfile.read(length))
            if not isinstance(body,dict):
                return self.send_json(400,{"error":"invalid JSON object"})
            if self.path == "/v1/control":
                status,data=self.service.control(body,self.headers.get("X-Ubbit-Actor","operator")[:200])
                return self.send_json(status,data)
            if self.path == "/v1/check":
                return self.send_json(200,self.service.check())
            return self.send_json(404,{"error":"not found"})
        except (ValueError, TypeError):
            return self.send_json(400,{"error":"invalid request"})
        except Exception:
            log.exception("Control API error")
            return self.send_json(500,{"error":"엔진 로그 확인 필요"})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",default="config.sites.yaml")
    parser.add_argument("--port",type=int,default=8778)
    parser.add_argument("--host",default="127.0.0.1")
    parser.add_argument("--live",action="store_true")
    args=parser.parse_args()
    cfg=load_config(args.config)
    cfg.mode="live" if args.live else "paper"
    cfg.engine.db_path="data/sites-live.db" if args.live else "data/sites-paper.db"
    cfg.engine.kill_switch_file="data/sites-live.KILL" if args.live else "data/sites-paper.KILL"
    cfg.engine.halt_on_mismatch=True
    if args.live and (os.environ.get("UBBIT_ALLOW_LIVE") != "1" or not cfg.access_key or not cfg.secret_key):
        parser.error("실거래는 UBBIT_ALLOW_LIVE=1 및 서버의 UPBIT_ACCESS_KEY/UPBIT_SECRET_KEY 설정이 필요합니다.")
    setup_logging(cfg.engine.log_level)
    service=EngineService(cfg,os.environ.get("UBBIT_BRIDGE_TOKEN", ""))
    RemoteHandler.service=service
    server=ThreadingHTTPServer((args.host,args.port),RemoteHandler)
    server.timeout=1
    service.start()
    print(f"UBBIT {cfg.mode} engine bridge: http://{args.host}:{args.port} (신규 진입 중단 상태)",flush=True)
    try:
        while not service.engine._stop:
            server.handle_request()
    finally:
        server.server_close()
        service.close()


if __name__ == "__main__":
    main()

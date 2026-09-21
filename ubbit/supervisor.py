"""엔진 + 대시보드 동시 실행.

명령 하나로 매매 엔진을 백그라운드 스레드에 띄우고, 같은 프로세스에서
대시보드를 서빙한다. 사용자는 터미널 하나를 열고 브라우저 창 하나를 보면 된다.

왜 브라우저가 직접 매매하지 않는가
  시크릿 키로 JWT 를 서명해야 하는데, 키가 페이지에 있으면 개발자도구·확장
  프로그램·소스보기로 그대로 노출된다. 출금 권한이 없어도 남의 계좌로 원하는
  가격에 체결시키는 것은 가능하다.
  그리고 브라우저는 백그라운드 탭의 타이머를 분당 1회까지 제한하고, 절전
  상태에서는 아예 멈춘다. 손절이 필요한 순간에 멈추는 런타임은 쓸 수 없다.

그래서 매매는 이 프로세스가 하고, 브라우저는 보기와 제어만 한다.
프로세스가 죽으면 대시보드도 같이 죽으므로 '창은 떠 있는데 매매는 멈춘'
상태가 생기지 않는다. 그 상태가 제일 위험하다.
"""
from __future__ import annotations

import threading
import time

from .config import Config
from .dashboard import Handler, PriceCache, serve_forever
from .engine import TradingEngine
from .logutil import get_logger

log = get_logger("run")


def run(cfg: Config, host: str, port: int, open_browser: bool = True) -> int:
    engine = TradingEngine(cfg)

    if not engine.startup_checks():
        engine.store.close()
        return 1

    thread = threading.Thread(target=engine.run, name="engine", daemon=True)
    thread.start()
    time.sleep(0.4)          # 첫 로그가 대시보드 안내보다 먼저 나오도록

    Handler.cfg = cfg
    Handler.db_path = cfg.engine.db_path
    Handler.prices = PriceCache()
    Handler.engine = engine

    try:
        serve_forever(cfg, host, port, open_browser)
    finally:
        log.info("대시보드 종료 — 엔진 정지 요청")
        engine._stop = True
        thread.join(timeout=cfg.engine.poll_seconds + 10)
        if thread.is_alive():
            log.warning("엔진이 제한 시간 안에 멈추지 않았습니다.")
    return 0

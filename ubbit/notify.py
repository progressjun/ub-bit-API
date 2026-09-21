"""웹훅 알림 (Slack / Discord / 임의 엔드포인트).

무인 운용의 전제는 '문제가 생기면 사람이 안다'는 것이다. 대시보드는
누군가 열어봐야 보인다. 계좌가 일일 손실한도에 닿거나 주문이 실패하는
순간은 밀어서 알려야 한다.

원칙
  알림 실패가 매매를 막으면 안 된다. 모든 예외를 삼키고 로그만 남긴다.
  체결 하나하나를 다 보내지 않는다. 알림 피로가 쌓이면 정작 중요한 것을 놓친다.
  기본 전송 대상은 진입/청산/정지/실패/재기동 대조 결과다.
"""
from __future__ import annotations

import json
import threading
import urllib.request
from dataclasses import dataclass

from .logutil import get_logger

log = get_logger("notify")


@dataclass
class NotifyParams:
    webhook_url: str = ""
    on_entry: bool = True
    on_exit: bool = True
    on_halt: bool = True         # 손실한도·쿨다운·킬스위치
    on_error: bool = True        # 주문 실패, 대조 불일치
    timeout: float = 5.0


class Notifier:
    def __init__(self, params: NotifyParams, label: str = "ubbit") -> None:
        self.p = params
        self.label = label

    @property
    def enabled(self) -> bool:
        return bool(self.p.webhook_url)

    def _post(self, text: str) -> None:
        body = json.dumps({"text": text, "content": text}).encode("utf-8")
        req = urllib.request.Request(
            self.p.webhook_url, data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.p.timeout) as resp:
                if resp.status >= 300:
                    log.warning("알림 전송 실패 status=%s", resp.status)
        except Exception as exc:                      # noqa: BLE001
            log.warning("알림 전송 실패: %s", exc)

    def send(self, kind: str, text: str) -> None:
        """kind ∈ {entry, exit, halt, error, info}. 실패해도 절대 예외를 올리지 않는다."""
        if not self.enabled:
            return
        gate = {"entry": self.p.on_entry, "exit": self.p.on_exit,
                "halt": self.p.on_halt, "error": self.p.on_error}.get(kind, True)
        if not gate:
            return
        icon = {"entry": "🟢", "exit": "🔵", "halt": "🟠", "error": "🔴"}.get(kind, "⚪")
        # 전송은 별도 스레드. 웹훅 지연이 매매 루프를 붙잡으면 안 된다.
        threading.Thread(
            target=self._post, args=(f"{icon} [{self.label}] {text}",), daemon=True
        ).start()

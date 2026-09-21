#!/usr/bin/env bash
# systemd 를 못 쓰는 환경(맥/윈도우 WSL)용 감시 루프.
# 프로세스가 죽으면 15초 후 재시작하고, 10분 안에 5회 넘게 죽으면 멈춘다.
set -u
CONFIG="${1:-config.proven.yaml}"
CMD=(python3 -m ubbit -c "$CONFIG" live --yes-i-know)

fails=0; window_start=$(date +%s)
while true; do
  echo "[run.sh] 시작: ${CMD[*]}"
  "${CMD[@]}"; code=$?
  [ $code -eq 0 ] && { echo "[run.sh] 정상 종료"; exit 0; }

  now=$(date +%s)
  if [ $((now - window_start)) -gt 600 ]; then fails=0; window_start=$now; fi
  fails=$((fails + 1))
  if [ $fails -gt 5 ]; then
    echo "[run.sh] 10분 내 ${fails}회 비정상 종료 — 중단합니다. 로그를 확인하세요." >&2
    exit 1
  fi
  echo "[run.sh] 종료코드 ${code}. 15초 후 재시작 (${fails}/5)" >&2
  sleep 15
done

#!/usr/bin/env bash
# 원클릭 실행 — macOS / Linux / WSL
#
#   ./start.sh                 페이퍼 (주문 없음, 키 불필요)
#   ./start.sh live            실주문 (환경변수 키 필요)
#
# 하는 일: 파이썬 확인 → 가상환경 → 의존성 설치 → 캔들 확보 → 엔진+웹창 실행
set -euo pipefail
cd "$(dirname "$0")"

MODE="${1:-paper}"
CONFIG="${CONFIG:-config.scalp24.yaml}"
PORT="${PORT:-8777}"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf '\n\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

say "[1/5] 파이썬 확인"
PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
      PY="$cand"; break
    fi
  fi
done
[ -n "$PY" ] || die "파이썬 3.10 이상이 필요합니다.  https://www.python.org/downloads/"
echo "  $($PY --version)"

say "[2/5] 가상환경 + 의존성"
[ -d .venv ] || "$PY" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt
echo "  requests, PyYAML 준비 완료"

say "[3/5] 설정 확인"
[ -f "$CONFIG" ] || die "설정 파일이 없습니다: $CONFIG"
python -m ubbit -c "$CONFIG" costs >/dev/null || die "설정이 올바르지 않습니다. 위 오류를 확인하세요."
echo "  $CONFIG 정상"

say "[4/5] 캔들 데이터"
UNIT=$(python -c "from ubbit.config import load_config; print(load_config('$CONFIG').engine.candle_unit)")
COUNT=$(ls data/candles/*_"${UNIT}"m.json 2>/dev/null | wc -l | tr -d ' ')
if [ "$COUNT" -lt 5 ]; then
  echo "  ${UNIT}분봉 캔들이 부족합니다($COUNT종목). 수집합니다 — 몇 분 걸립니다."
  python scripts/fetch_all.py 24
else
  echo "  ${UNIT}분봉 ${COUNT}종목 보유"
fi

if [ "$MODE" = "live" ]; then
  say "[5/5] 실주문 모드"
  [ -n "${UPBIT_ACCESS_KEY:-}" ] && [ -n "${UPBIT_SECRET_KEY:-}" ] || die \
"환경변수가 없습니다.

  export UPBIT_ACCESS_KEY=...
  export UPBIT_SECRET_KEY=...

키 발급: https://upbit.com/mypage/open_api_management
'자산조회' + '주문하기'만 체크하고 출금 권한은 주지 마세요. 허용 IP 등록은 필수입니다."
  echo "  키 확인 완료. 먼저 5,000원 실주문 테스트를 권합니다:"
  echo "    python -m ubbit -c $CONFIG smoketest --yes-i-know"
  echo
  read -r -p "  실주문 모드로 시작할까요? (yes 입력) " ans
  [ "$ans" = "yes" ] || die "취소했습니다."
  exec python -m ubbit -c "$CONFIG" run --live --yes-i-know --port "$PORT"
else
  say "[5/5] 페이퍼 모드로 시작 (실제 주문 없음)"
  exec python -m ubbit -c "$CONFIG" run --port "$PORT"
fi

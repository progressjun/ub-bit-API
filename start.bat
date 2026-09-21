@echo off
REM 원클릭 실행 - Windows
REM   start.bat          페이퍼 (주문 없음, 키 불필요)
REM   start.bat live     실주문 (환경변수 키 필요)
setlocal
cd /d "%~dp0"
set "MODE=%~1"
if "%MODE%"=="" set "MODE=paper"
if "%CONFIG%"=="" set "CONFIG=config.scalp24.yaml"
if "%PORT%"=="" set "PORT=8777"

echo.
echo [1/4] 파이썬 확인
python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" 2>nul
if errorlevel 1 (
  echo   파이썬 3.10 이상이 필요합니다. https://www.python.org/downloads/
  exit /b 1
)
python --version

echo.
echo [2/4] 가상환경 + 의존성
if not exist .venv python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt

echo.
echo [3/4] 캔들 데이터 확인
python scripts\fetch_all.py 24

echo.
if /i "%MODE%"=="live" (
  if "%UPBIT_ACCESS_KEY%"=="" goto nokey
  if "%UPBIT_SECRET_KEY%"=="" goto nokey
  echo [4/4] 실주문 모드
  echo   먼저 5,000원 테스트를 권합니다:
  echo     python -m ubbit -c %CONFIG% smoketest --yes-i-know
  set /p ANS="  실주문으로 시작할까요? (yes 입력) "
  if /i not "%ANS%"=="yes" exit /b 1
  python -m ubbit -c %CONFIG% run --live --yes-i-know --port %PORT%
) else (
  echo [4/4] 페이퍼 모드로 시작 ^(실제 주문 없음^)
  python -m ubbit -c %CONFIG% run --port %PORT%
)
exit /b 0

:nokey
echo   환경변수가 없습니다.
echo     set UPBIT_ACCESS_KEY=...
echo     set UPBIT_SECRET_KEY=...
echo   키 발급: https://upbit.com/mypage/open_api_management
exit /b 1

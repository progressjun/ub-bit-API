# UBBIT · GPT Sites

기존 Python 엔진은 유지하고 GPT Sites에 개인용 시세·제어 콘솔을 추가했습니다.

## 실행 범위

- Sites: 실제 Upbit 원화 마켓, 1/5/15/60/240분봉, 종목 검색, 엔진 상태, 포지션, 거래 기록/CSV, 연결 점검, 신규 진입 중단/시작.
- 엔진 서버: 기존 전략·리스크 관리·모의/실주문, SQLite 장부, 브라우저와 독립적인 20초 평가 루프.
- Sites는 Python 상주 프로세스나 업비트 허용 IP를 대신 제공하지 않습니다. 엔진 서버와 키 연결이 필요합니다.
- 실계좌 연결·주문하기 권한·실체결은 API 키와 허용 IP가 준비된 뒤 별도 검증해야 합니다. 이번 구현 검증은 실제 공개 시세와 가짜 주문 응답을 사용했습니다. 실제 주문은 전송하지 않았습니다.

## 연결

1. Python 3.10 이상이 있는 계속 켜 둘 PC 또는 고정 IP 서버에 저장소를 받습니다.
2. `pip install -r requirements.txt` 실행 후 32자 이상 무작위 연결 토큰을 준비합니다. 토큰·키는 채팅, Git, 브라우저 저장소에 넣지 않습니다.
3. 엔진 환경에 `UBBIT_BRIDGE_TOKEN`을 설정하고 `python -m ubbit.remote --config config.sites.yaml`을 실행합니다. Windows에서는 `./start-sites.ps1`이 설치와 비공개 토큰 입력을 지원합니다.
4. `127.0.0.1:8778` 앞에 HTTPS 역방향 프록시 또는 인증 가능한 고정 터널을 연결합니다. `deploy/Caddyfile.example`과 `deploy/ubbit-bridge.service`를 제공합니다. 외부에서 HTTP 8778 포트에 직접 접근하도록 열지 마세요. 리버스 프록시의 요청/응답 로그에 Authorization을 기록하지 않습니다.
5. Sites 서버 환경에 `ENGINE_URL=https://엔진의-실제-도메인`과 비밀값 `ENGINE_TOKEN`(위 연결 토큰과 동일)을 설정합니다. URL은 서버 운영자가 지정하며 요청에서 임의 주소를 받지 않습니다.
6. Sites 연결 설정에서 점검하고 모의매매를 시작합니다. 현재 연결이 없거나 실패하면 잔고나 거래 결과를 임의로 채우지 않습니다.

실거래 전환 시 실행 서버 환경에 `UPBIT_ACCESS_KEY`, `UPBIT_SECRET_KEY`, `UBBIT_ALLOW_LIVE=1`을 설정한 뒤 `python -m ubbit.remote --config config.sites.yaml --live`로 시작합니다. Windows는 `./start-sites.ps1 -Live`에서 키를 가려서 입력합니다. 키는 실행 중 프로세스 환경에만 전달됩니다.

실거래 서비스는 **매수·매도 모두 대기 상태**로 시작합니다. 연결 점검과 잔고 대조 후 Sites에서 `실거래 시작`을 직접 입력해야 주문 평가를 시작합니다. 시작한 뒤 `신규 매수 중단`을 누르면 기존 포지션의 청산은 계속 평가합니다. 프로세스를 재시작하면 다시 전체 주문 대기로 돌아갑니다.

## 업비트 설정

- 실제 실행 서버의 출발 IP를 등록합니다. Sites의 주소/IP를 등록하는 것이 아닙니다.
- 권한은 **자산조회 + 주문조회 + 주문하기**가 필요합니다. 출금 권한은 사용하지 않습니다.
- 조회 점검 통과는 실제 주문 권한/체결 검증이 아닙니다. UI에 이를 구분합니다.
- `config.sites.yaml` 초기값: 주문 예산 최대 10,000원, 일 신규 진입 3회, 동시 보유 2종목, 일 실현손실 한도 2%, 신호강도 증액 없음. 이 수치는 손실을 보장하는 한도가 아닙니다. 갭/슬리피지/장애로 초과할 수 있습니다.
- 전략 수익성은 입증되지 않았습니다. 비용·리스크 설정과 테스트 결과를 검토한 뒤 운용하세요.

## 상태와 복구

- `data/sites-paper.db`와 `data/sites-live.db`는 별도 장부입니다. SQLite에 거래·포지션·리스크·제어 이력을 저장합니다. 소스에 커밋하지 않습니다.
- 같은 장부는 OS 잠금으로 엔진 두 개가 동시에 사용하지 못합니다.
- 모의 잔고는 초기자산 + 실현손익 - 남은 포지션 원가로 복원합니다. 일일 진입 횟수와 쿨다운도 복원합니다.
- 신규 매수 중단은 전량매도가 아닙니다. 이미 제출된 주문을 취소하지 않으며 보유분 청산은 계속됩니다. 서버 종료·절전·장애 시 손절도 중단됩니다.
- 거래소에 제출하기 전에 주문 식별자를 저장합니다. 전송 오류/체결 상태 미확인은 자동 재주문하지 않고 장부에 남기며 엔진을 중단합니다. 재시작으로 이 차단을 해제할 수 없습니다.
- 미확인 주문이 있으면 `order_intents.identifier`로 업비트 주문 조회를 수행하고 실제 체결/장부를 대조해야 합니다. DB를 백업하고 주문별 포지션·수수료·잔량을 복구한 뒤 운영자가 상태를 `applied` 또는 확실한 미접수인 경우 `rejected`로 확정해야 합니다. 임의로 상태만 바꾸거나 DB를 지워 재기동하면 안 됩니다. 자동 복구 기능은 제공하지 않습니다.
- terminal 부분매도는 잔여 수량과 원가를 남깁니다. 미종결 부분체결은 최종 체결로 확정하지 않습니다.
- 실거래 수수료가 설정과 다르면 점검을 거부합니다. 조회할 수 없는 시세·연결은 오류로 표시합니다.

## 개발과 검증

```powershell
$env:PYTHONUTF8='1'
python -m unittest discover -s tests -t .
node --test tests/site.test.mjs
node scripts/dev.mjs
# http://127.0.0.1:8787
```

개발 서버의 테스트 사용자 주입은 `scripts/dev.mjs`의 루프백 전용 서버에만 있습니다. 배포 Worker에는 없으며 Sites의 `oai-authenticated-user-id`가 없는 API 요청은 401입니다. 사이트는 소유자 비공개로 배포합니다. 다른 사용자와 공유하면 사이트 접근 권한을 가진 사용자는 같은 엔진을 제어하므로 임의로 공개하거나 공유하지 않습니다.

```powershell
node scripts/build.mjs
node scripts/validate-artifact.mjs
```

서버 빌드는 `dist/server/index.js`에 UI를 포함한 Worker ESM을 생성합니다. 서버의 기본 내보내기는 `fetch(request, env)`입니다. 배포 파일은 `.openai/hosting.json`과 `dist/server/index.js`만 필요합니다. Python은 엔진 서버에 별도로 실행해야 합니다.

## Windows 백그라운드 실행

전용 가상환경 `.venv`를 사용합니다. `scripts/windows-input.mjs`는 숨긴 표준입력으로 키와 연결 토큰을 받아 `%LOCALAPPDATA%/UBBIT/credentials.dpapi.json`에 Windows DPAPI(CurrentUser)로 암호화합니다. 이 파일과 장부는 Git/OneDrive 밖에 있으며 현재 Windows 사용자만 디렉터리에 접근하도록 ACL을 설정합니다. Sites Secret 저장소와 별도로 PC 엔진이 실행할 때 필요한 로컬 복사본입니다. 브라우저에는 키를 보내지 않습니다.

```powershell
./scripts/windows-engine.ps1 -Action Status
./scripts/windows-engine.ps1 -Action Probe
./scripts/windows-engine.ps1 -Action Start
./scripts/windows-engine.ps1 -Action Stop
```

- `Probe`는 IP 및 인증 조회만 수행합니다. `Start`는 백그라운드로 실행하고 모든 실주문을 대기시킵니다.
- 종료는 기록된 PID와 생성 시각을 함께 확인합니다. 이 실행기가 만든 엔진과 터널만 종료합니다.
- 장부: `%LOCALAPPDATA%/UBBIT/data/sites-live.db`. 로그: 같은 UBBIT 폴더의 `engine.*.log`, `tunnel.*.log`.
- PC가 꺼지거나 절전하면 엔진도 중단됩니다. Windows 자동 시작이나 전원 설정은 변경하지 않습니다.
- 현재 연결은 Cloudflare Quick Tunnel입니다. 개발·연결 점검용 임시 연결이며, 터널 재시작 시 주소가 바뀔 수 있습니다. `Status`의 새 URL을 Sites의 `ENGINE_URL`에 반영하고 재배포해야 합니다. 장기 운용에는 계정에 등록한 고정 터널/도메인이 필요합니다. [Cloudflare 안내](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/)
- API 점검 실패 시 PC 서비스는 연결을 유지하되, 사용자가 다시 점검을 통과하기 전에는 실거래 시작을 허용하지 않습니다.

## 근거 문서

- [업비트 인증·IP·권한](https://docs.upbit.com/kr/reference/auth)
- [주문 생성](https://docs.upbit.com/kr/reference/new-order)
- [식별자로 주문 조회](https://docs.upbit.com/kr/reference/get-order)
- [요청 수 제한](https://docs.upbit.com/kr/reference/rate-limits)

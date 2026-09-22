param([switch]$Live)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.10 이상이 필요합니다.' }
}
& '.venv\Scripts\python.exe' -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw '의존성 설치 실패' }
function Read-PrivateValue([string]$Prompt) {
    $value = Read-Host $Prompt -AsSecureString
    return [System.Net.NetworkCredential]::new('', $value).Password
}
if (-not $env:UBBIT_BRIDGE_TOKEN) {
    $env:UBBIT_BRIDGE_TOKEN = Read-PrivateValue 'Sites ENGINE_TOKEN과 동일한 32자 이상 연결 토큰'
}
$arguments = @('-m', 'ubbit.remote', '--config', 'config.sites.yaml')
if ($Live) {
    if (-not $env:UPBIT_ACCESS_KEY) { $env:UPBIT_ACCESS_KEY = Read-PrivateValue 'Upbit Access Key (화면에 표시되지 않음)' }
    if (-not $env:UPBIT_SECRET_KEY) { $env:UPBIT_SECRET_KEY = Read-PrivateValue 'Upbit Secret Key (화면에 표시되지 않음)' }
    $env:UBBIT_ALLOW_LIVE = '1'
    $arguments += '--live'
}
Write-Host '엔진은 신규 진입 중단 상태로 시작합니다. Sites에서 점검 후 시작하세요.'
& '.venv\Scripts\python.exe' @arguments
exit $LASTEXITCODE

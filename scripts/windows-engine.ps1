param([ValidateSet('Install','Start','Stop','Status','Probe')][string]$Action='Status')
$ErrorActionPreference='Stop'
$repoPath=Split-Path -Parent $PSScriptRoot
$runtimePath=Join-Path $env:LOCALAPPDATA 'UBBIT'
$secretPath=Join-Path $runtimePath 'credentials.dpapi.json'
$processPath=Join-Path $runtimePath 'process.json'
$pythonPath=Join-Path $repoPath '.venv\Scripts\python.exe'
function Unseal([string]$value) { return [System.Net.NetworkCredential]::new('',(ConvertTo-SecureString $value)).Password }
function Get-OwnProcess($record) {
    if (-not $record) { return $null }
    $p=Get-Process -Id $record.id -ErrorAction SilentlyContinue
    if ($p -and $p.StartTime.ToUniversalTime().ToString('o') -eq ([datetime]$record.started).ToUniversalTime().ToString('o')) { return $p }
    return $null
}
if ($Action -eq 'Install') {
    $inputData=[Console]::In.ReadLine() | ConvertFrom-Json
    foreach ($name in @('UPBIT_ACCESS_KEY','UPBIT_SECRET_KEY','UBBIT_BRIDGE_TOKEN')) {
        $value=[string]$inputData.$name
        if ($value.Length -lt 32 -or $value -match '[\r\n]') { throw 'Invalid credential input' }
    }
    New-Item -ItemType Directory -Path $runtimePath -Force | Out-Null
    $sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl=New-Object System.Security.AccessControl.DirectorySecurity
    $acl.SetOwner($sid)
    $acl.SetAccessRuleProtection($true,$false)
    $rule=New-Object System.Security.AccessControl.FileSystemAccessRule($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow')
    $acl.AddAccessRule($rule)
    Set-Acl -LiteralPath $runtimePath -AclObject $acl
    $sealed=@{}
    foreach ($name in @('UPBIT_ACCESS_KEY','UPBIT_SECRET_KEY','UBBIT_BRIDGE_TOKEN')) {
        $sealed[$name]=ConvertFrom-SecureString (ConvertTo-SecureString ([string]$inputData.$name) -AsPlainText -Force)
    }
    $sealed | ConvertTo-Json | Set-Content -LiteralPath $secretPath -Encoding UTF8
    Write-Output '{"installed":true,"storage":"Windows DPAPI CurrentUser","plaintextOnDisk":false}'
    exit
}
if ($Action -eq 'Stop') {
    if (Test-Path -LiteralPath $processPath) {
        $record=Get-Content -Raw -LiteralPath $processPath | ConvertFrom-Json
        foreach ($key in @('engine','tunnel')) { $p=Get-OwnProcess $record.$key; if ($p) { Stop-Process -Id $p.Id } }
    }
    Write-Output '{"stopped":true}'
    exit
}
if ($Action -eq 'Status') {
    if (-not (Test-Path -LiteralPath $processPath)) { Write-Output '{"running":false}';exit }
    $record=Get-Content -Raw -LiteralPath $processPath | ConvertFrom-Json
    @{engineRunning=[bool](Get-OwnProcess $record.engine);tunnelRunning=[bool](Get-OwnProcess $record.tunnel);url=$record.url;runtimePath=$runtimePath} | ConvertTo-Json -Compress
    exit
}
if (-not (Test-Path -LiteralPath $secretPath)) { throw 'Install credentials first' }
$sealed=Get-Content -Raw -LiteralPath $secretPath | ConvertFrom-Json
$before=@{}
try {
    foreach ($name in @('UPBIT_ACCESS_KEY','UPBIT_SECRET_KEY','UBBIT_BRIDGE_TOKEN','PYTHONUTF8','UBBIT_ALLOW_LIVE')) { $before[$name]=[Environment]::GetEnvironmentVariable($name,'Process') }
    foreach ($name in @('UPBIT_ACCESS_KEY','UPBIT_SECRET_KEY','UBBIT_BRIDGE_TOKEN')) { [Environment]::SetEnvironmentVariable($name,(Unseal $sealed.$name),'Process') }
    $env:PYTHONUTF8='1'
    if ($Action -eq 'Probe') {
        Push-Location -LiteralPath $repoPath
        try { & $pythonPath -m ubbit.windows_probe; if ($LASTEXITCODE -ne 0) { throw 'Probe failed' } } finally { Pop-Location }
        exit
    }
    if (Test-Path -LiteralPath $processPath) {
        $old=Get-Content -Raw -LiteralPath $processPath | ConvertFrom-Json
        if ((Get-OwnProcess $old.engine) -or (Get-OwnProcess $old.tunnel)) { throw 'UBBIT is already running. Use Status or Stop first.' }
    }
    if (Get-NetTCPConnection -LocalPort 8778 -State Listen -ErrorAction SilentlyContinue) { throw 'Port 8778 is already in use' }
    $env:UBBIT_ALLOW_LIVE='1'
    $argsList=@('-m','ubbit.remote','--config','config.sites.yaml','--live','--data-dir',('"'+(Join-Path $runtimePath 'data')+'"'))
    $engine=Start-Process -FilePath $pythonPath -ArgumentList $argsList -WorkingDirectory $repoPath -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtimePath 'engine.out.log') -RedirectStandardError (Join-Path $runtimePath 'engine.err.log') -PassThru
    $record=@{engine=@{id=$engine.Id;started=$engine.StartTime.ToUniversalTime().ToString('o')};tunnel=$null;url=$null}
    $record | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $processPath -Encoding UTF8
    # Do not give Upbit secrets to the tunnel process.
    foreach ($name in @('UPBIT_ACCESS_KEY','UPBIT_SECRET_KEY','UBBIT_BRIDGE_TOKEN','UBBIT_ALLOW_LIVE')) { [Environment]::SetEnvironmentVariable($name,$null,'Process') }
    $cloudflared=(Get-Command cloudflared -ErrorAction Stop).Source
    $tunnel=Start-Process -FilePath $cloudflared -ArgumentList @('tunnel','--url','http://127.0.0.1:8778','--no-autoupdate') -WorkingDirectory $runtimePath -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtimePath 'tunnel.out.log') -RedirectStandardError (Join-Path $runtimePath 'tunnel.err.log') -PassThru
    $record.tunnel=@{id=$tunnel.Id;started=$tunnel.StartTime.ToUniversalTime().ToString('o')}
    $record | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $processPath -Encoding UTF8
    $deadline=[DateTime]::UtcNow.AddSeconds(40)
    do {
        Start-Sleep -Milliseconds 500
        $tunnelText=Get-Content -LiteralPath (Join-Path $runtimePath 'tunnel.err.log') -Raw -ErrorAction SilentlyContinue
        if ($tunnelText -match 'https://[a-z0-9-]+\.trycloudflare\.com') { $record.url=$Matches[0]; break }
        if ($tunnel.HasExited -or $engine.HasExited) { throw 'Engine or tunnel exited. Check runtime logs.' }
    } while ([DateTime]::UtcNow -lt $deadline)
    $record | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $processPath -Encoding UTF8
    @{started=$true;url=$record.url;runtimePath=$runtimePath;liveOrdersEnabled=$false} | ConvertTo-Json -Compress
} finally {
    foreach ($name in $before.Keys) { [Environment]::SetEnvironmentVariable($name,$before[$name],'Process') }
}

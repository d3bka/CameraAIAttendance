<#
.SYNOPSIS
  Autostart Camera Attendance AI on Windows: the platform server and the AI worker start at logon
  and restart automatically if they crash. No admin rights needed (tasks run as the current user).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1
  powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Port 8080 -WorkerConfig data\cameras-school-1.json
  powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Uninstall
#>
param(
    [int]$Port = 8080,
    [string]$WorkerConfig = "",
    [switch]$Uninstall
)
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ServerTask = "CameraAttendance Platform"
$WorkerTask = "CameraAttendance AI Worker"

if ($Uninstall) {
    foreach ($name in @($ServerTask, $WorkerTask)) {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "Removed: $name"
        }
    }
    return
}

$LogDir = Join-Path $Root "data\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$py = (Get-Command py -ErrorAction SilentlyContinue).Source
if (-not $py) { throw "Python launcher 'py' not found. Install Python 3.10+ from python.org" }
$venvPython = Join-Path $Root ".venv\Scripts\python.exe"

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

# --- platform server
$serverCmd = "/c cd /d `"$Root`" && `"$py`" -3 app.py --port $Port >> `"$LogDir\server.log`" 2>&1"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $serverCmd -WorkingDirectory $Root
Register-ScheduledTask -TaskName $ServerTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "Installed: $ServerTask  ->  http://127.0.0.1:$Port"

# --- AI worker (all cameras of the school, schedule-driven)
if ($WorkerConfig -eq "") {
    $found = Get-ChildItem -Path (Join-Path $Root "data") -Filter "cameras-school-*.json" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { $WorkerConfig = $found.FullName }
}
if ($WorkerConfig -ne "") {
    if (-not (Test-Path $WorkerConfig)) { throw "Worker config not found: $WorkerConfig" }
    if (-not (Test-Path $venvPython)) { throw "AI environment missing. Run: py -3 -m venv .venv; .venv\Scripts\pip install -r requirements-ai.txt" }
    $cfg = (Resolve-Path $WorkerConfig).Path
    $workerCmd = "/c cd /d `"$Root`" && `"$venvPython`" face_worker.py --config `"$cfg`" >> `"$LogDir\worker.log`" 2>&1"
    $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $workerCmd -WorkingDirectory $Root
    Register-ScheduledTask -TaskName $WorkerTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
    Write-Host "Installed: $WorkerTask  (config: $cfg)"
} else {
    Write-Host "AI worker not installed: create a config first with  py -3 manage.py worker-config --school-id <id>"
}

Write-Host "Start now without re-login:  Start-ScheduledTask -TaskName '$ServerTask'; Start-ScheduledTask -TaskName '$WorkerTask'"
Write-Host "Logs: $LogDir"

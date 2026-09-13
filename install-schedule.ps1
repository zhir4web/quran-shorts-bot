param(
    [ValidateSet('private','unlisted','public')][string]$Privacy = 'private',
    [string]$Time = '12:00'
)
$ErrorActionPreference = 'Stop'
$botRoot = $PSScriptRoot
$botPython = Join-Path $botRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $botPython)) { throw 'Run setup.bat first.' }
if (-not (Test-Path -LiteralPath (Join-Path $botRoot 'state\youtube_token.json'))) { throw 'Run run.bat auth first.' }
$botAt = [DateTime]::ParseExact($Time, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture)
$botAction = New-ScheduledTaskAction -Execute $botPython -Argument ('"' + (Join-Path $botRoot 'bot.py') + '" run --privacy ' + $Privacy) -WorkingDirectory $botRoot
$botTrigger = New-ScheduledTaskTrigger -Daily -At $botAt
$botSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$botIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$botPrincipal = New-ScheduledTaskPrincipal -UserId $botIdentity -LogonType Interactive -RunLevel Limited
$botTask = New-ScheduledTask -Action $botAction -Trigger $botTrigger -Settings $botSettings -Principal $botPrincipal
Register-ScheduledTask -TaskName 'QuranShortsBot' -InputObject $botTask
Write-Host "Scheduled daily at $Time (computer local time), privacy: $Privacy. Computer must be on and you must be signed in."


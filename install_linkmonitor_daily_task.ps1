param(
    [string]$TaskName = "LinkMonitor Sentiment Daily",
    [string]$ProjectRoot = $PSScriptRoot,
    [string]$At = "12:00"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$dailyScript = Join-Path $ProjectRoot "run_linkmonitor_daily.ps1"

if (!(Test-Path -LiteralPath $dailyScript)) {
    throw "Script giornaliero non trovato: $dailyScript"
}

$time = [datetime]::ParseExact($At, "HH:mm", [Globalization.CultureInfo]::InvariantCulture)
$powershellExe = Join-Path $PSHOME "powershell.exe"
$arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$dailyScript`""

$action = New-ScheduledTaskAction `
    -Execute $powershellExe `
    -Argument $arguments `
    -WorkingDirectory $ProjectRoot

$trigger = New-ScheduledTaskTrigger -Daily -At $time

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6)

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Esegue estrazione LinkMonitor, classificazione Codex e apply sentiment ogni giorno alle $At." `
    -Force | Out-Null

Write-Host "Task registrato: $TaskName"
Write-Host "Orario: ogni giorno alle $At"
Write-Host "Script: $dailyScript"
Write-Host "Nota: con LogonType Interactive il task parte quando l'utente e' loggato."

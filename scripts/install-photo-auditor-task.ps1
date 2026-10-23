$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = (Get-Command python).Source
if (!(Test-Path -LiteralPath (Join-Path $projectRoot "runs/private/photo-auditor.dat"))) {
    throw "Сначала выполните scripts/set-photo-auditor-token.ps1"
}
$taskAction = New-ScheduledTaskAction -Execute (Join-Path $env:SystemRoot "System32/conhost.exe") -Argument "--headless `"$pythonPath`" -m photo_auditor run" -WorkingDirectory $projectRoot
$taskTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$taskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "AgentsMY Photo Auditor" -Action $taskAction -Trigger $taskTrigger -Settings $taskSettings -Description "Контроль фотографий @foto_auditor_bot" -Force | Out-Null
Write-Output "Готово. Запуск: Start-ScheduledTask -TaskName 'AgentsMY Photo Auditor'"

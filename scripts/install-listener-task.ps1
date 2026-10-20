# Регистрирует задачу Планировщика «AgentsMY Listener»: запуск слушателя при входе пользователя.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$script = Join-Path $root "scripts\listener.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "AgentsMY Listener" -Action $action -Trigger $trigger -Settings $settings -Description "Слушатель оркестратора Agents_MY" -Force | Out-Null
Write-Output "Задача «AgentsMY Listener» зарегистрирована. Запустить сейчас: Start-ScheduledTask -TaskName 'AgentsMY Listener'"

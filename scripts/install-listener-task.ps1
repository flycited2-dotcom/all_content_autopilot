# Регистрирует задачу Планировщика «AgentsMY Listener»: слушатель оркестратора при входе пользователя.
# Запускается pythonw напрямую (без окна и без обёрток) — тогда «Остановить» в Планировщике действительно
# останавливает слушатель. Журнал: runs\chat\listener.log. ORCH_API_TOKEN — пользовательская переменная Windows.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = (Get-Command python).Source
$pythonw = Join-Path (Split-Path -Parent $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python }
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m listener" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "AgentsMY Listener" -Action $action -Trigger $trigger -Settings $settings -Description "Слушатель оркестратора Agents_MY" -Force | Out-Null
Write-Output "Задача «AgentsMY Listener» зарегистрирована ($pythonw -m listener). Запуск: Start-ScheduledTask -TaskName 'AgentsMY Listener'"

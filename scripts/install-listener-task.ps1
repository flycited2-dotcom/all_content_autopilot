# Регистрирует задачу Планировщика «AgentsMY Listener»: слушатель оркестратора при входе пользователя.
# Запуск в СКРЫТОЙ консоли: conhost.exe --headless python -m listener. Все дочерние процессы
# (claude, ssh, служебные команды) наследуют эту невидимую консоль и не открывают окон.
# «Остановить» в Планировщике останавливает слушатель. Журнал: runs\chat\listener.log.
# ORCH_API_TOKEN — пользовательская переменная Windows.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = (Get-Command python).Source
$conhost = Join-Path $env:SystemRoot "System32\conhost.exe"
$action = New-ScheduledTaskAction -Execute $conhost -Argument "--headless `"$python`" -m listener" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "AgentsMY Listener" -Action $action -Trigger $trigger -Settings $settings -Description "Слушатель оркестратора Agents_MY (скрытая консоль)" -Force | Out-Null
Write-Output "Задача «AgentsMY Listener» зарегистрирована (conhost --headless $python -m listener). Запуск: Start-ScheduledTask -TaskName 'AgentsMY Listener'"
